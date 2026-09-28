"""Claude reads each chat that didn't end in a booking confirmation and says why.

Results are cached in respond.db (table ai_reasons), keyed by conversation + the time of its last
message, so a chat is only re-read (and re-paid for) when new messages arrive. Bookings themselves are
still decided by the rule "a booking confirmation was sent" - Claude never overrides that.

  python ai_reasons.py            # read pending chats (batch at half price if many, else direct)
  python ai_reasons.py --status   # how many are done / pending / waiting in a batch

Uses the Anthropic API key from the file named in config.json -> ai.anthropic_key_file.
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

import metrics

PROMPT_VERSION = 2  # v2 added "Payment problem"
# Chats read with an older prompt are only re-read if they could be affected by what changed:
# (only the CUSTOMER's messages in that chat are checked - we mention "payment link" in almost every chat)
REREAD_IF = (r"cash|transfer|apple ?pay|google ?pay|\bcard\b|declin|not working|doesn.?t work|isn.?t working|"
             r"failed|error|scam|can.?t pay|cannot pay|unable to pay|link (is )?not|link expired")
FOLLOW_UP = "About a trip already booked"
IN_PROGRESS = "Still in progress"
LABELS = metrics.LOST_REASONS + [FOLLOW_UP, IN_PROGRESS]

SYSTEM = f"""You review WhatsApp chats for Fetch Pet Taxi, a pet taxi service in Dubai / the UAE. Customers
message to book rides for their pets (vet visits, airport, grooming, relocation). The Fetch team replies
from WhatsApp. Automatic greetings have already been removed from the chat you see.

Every chat you are given did NOT end with a booking confirmation being sent. Decide the ONE main reason it
did not turn into a trip, using exactly one of these labels:

- "Driver unavailable": we could not do the date/time the customer asked for (fully booked, no driver free,
  can't reach them by that time). This still applies if we offered another time and/or quoted a price,
  UNLESS the customer accepted the other time (e.g. said yes, sent their address/pin to go ahead) - then
  availability was not the reason. General advice like "please book 1-2 days in advance, we get booked out
  quickly" is NOT a refusal.
- "Don't service route": we told them we don't go to/from that place or area, or don't do that route.
- "Said price was too high": the customer clearly objected to the price (too expensive, too much, found
  cheaper, out of budget). Merely asking for a discount or "is this the best price?" is not enough on its own.
- "Stopped responding after price was sent": we quoted a price and the customer went quiet or left without
  a clear reason.
- "Stopped responding": the customer went quiet before any price was quoted (e.g. never answered "when would
  you like to book?").
- "We didn't reply": the customer's last real question or request never got an answer from us, and that is
  what most plausibly lost the booking. A final "thanks"/"ok"/emoji/goodbye does not need an answer.
- "Payment problem": the customer wanted to book but got stuck paying - the payment link / Apple Pay / card
  failed or confused them, they wanted to pay cash or by bank transfer, or didn't trust paying online.
- "Other / unclear": not a winnable trip request or no clear reason: against policy (e.g. unvaccinated
  animals), wanted a different vehicle, booked someone else / a friend drove / plans changed, job
  applicants, sales pitches, tests, spam, wrong number.
- "{FOLLOW_UP}": the chat is only about a trip that was already booked/confirmed earlier (driver ETA,
  "we have landed", refunds, receipts, confirming an existing pickup) - no new trip was being requested.
- "{IN_PROGRESS}": the conversation is clearly still open - e.g. we or the customer are waiting on a reply
  about a trip that could still be booked, and the last message was recent (see the hours given).

When several things happened, pick what actually ended it - usually the last decisive thing. Be literal and
evidence-based; do not guess beyond the chat.

Return JSON with:
- "reason": one label, exactly as written above
- "explanation": one plain-English sentence (max 25 words) a busy owner can read, e.g. "Customer wanted 9am;
  we were fully booked and offered 10:15; customer declined."
- "key_quote": the short message (max 15 words, copied from the chat) that best shows the reason, or "" if none
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "enum": LABELS},
        "explanation": {"type": "string"},
        "key_quote": {"type": "string"},
    },
    "required": ["reason", "explanation", "key_quote"],
    "additionalProperties": False,
}

DDL = """
CREATE TABLE IF NOT EXISTS ai_reasons (
  conv_id TEXT PRIMARY KEY, last_msg_ts INTEGER, prompt_v INTEGER, reason TEXT,
  explanation TEXT, key_quote TEXT, model TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS ai_meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _cfg():
    cfg = metrics.load_config()
    return cfg, cfg.get("ai", {})


def _client(ai):
    key_file = os.path.expanduser(ai.get("anthropic_key_file", ""))
    key = None
    if key_file and os.path.exists(key_file):
        for line in open(key_file):
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def _db():
    con = sqlite3.connect(metrics.DB)
    con.executescript(DDL)
    return con


def load_results():
    """conv_id -> dict(reason, explanation, key_quote, last_msg_ts) for the dashboard / metrics."""
    try:
        con = _db()
        rows = con.execute("SELECT conv_id, last_msg_ts, reason, explanation, key_quote, prompt_v "
                           "FROM ai_reasons").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    return {r[0]: {"last_msg_ts": r[1], "reason": r[2], "explanation": r[3], "key_quote": r[4], "prompt_v": r[5]}
            for r in rows}


def _transcript(M, row, tz, now):
    msgs = M[(M["contact_id"] == row.contact_id) & (M["ts"] >= row.start_ts) & (M["ts"] <= row.last_msg_ts)]
    lines = []
    for m in msgs.tail(120).itertuples():
        text = m.text if isinstance(m.text, str) and m.text.strip() else f"[{m.type}]"
        who = "Customer" if m.role == "customer" else "Fetch team"
        lines.append(f"[{dt.datetime.fromtimestamp(m.ts, tz):%a %d %b %H:%M}] {who}: {text[:600]}")
    hours = (now - row.last_msg_ts) / 3600
    return (f"Chat with customer (started {row.start:%a %d %b %Y %H:%M}). The last message was "
            f"{hours:.0f} hours ago.\n\n" + "\n".join(lines))


def pending(d=None, X=None):
    """Chats that need a (new) Claude reading: not booked, not brand-new, not already read as-is."""
    cfg, ai = _cfg()
    d = d or metrics.build(cfg)
    X = X if X is not None else metrics.lost_trips(d, cfg)
    now = int(time.time())
    min_age = ai.get("min_age_hours", 2) * 3600
    done = load_results()
    todo = X[(X["outcome"] != "Converted") & (now - X["last_msg_ts"] >= min_age)]
    M = d["messages"]
    cust = M[(M["role"] == "customer") & M["text"].fillna("").str.contains(REREAD_IF, case=False)]

    def mentions(row):
        return bool(len(cust[(cust["contact_id"] == row.contact_id) & (cust["ts"] >= row.start_ts)
                             & (cust["ts"] <= row.last_msg_ts)]))

    def needs(row):
        r = done.get(row.conv_id)
        if not r or r["last_msg_ts"] != int(row.last_msg_ts):
            return True  # never read, or new messages since
        return r["prompt_v"] < PROMPT_VERSION and mentions(row)  # prompt changed in a way that matters here

    todo = todo[[needs(r) for r in todo.itertuples()]]
    return d, todo


def _params(user_text, model, effort):
    return dict(
        model=model, max_tokens=4000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": user_text}],
    )


def _save(con, conv_id, last_ts, data, model, prompt_v=PROMPT_VERSION):
    if data.get("reason") not in LABELS:
        return False
    con.execute("INSERT OR REPLACE INTO ai_reasons VALUES (?,?,?,?,?,?,?,?)", (
        conv_id, int(last_ts), prompt_v, data["reason"], data.get("explanation", "")[:300],
        data.get("key_quote", "")[:200], model, int(time.time())))
    return True


def _text(msg):
    return next((b.text for b in msg.content if b.type == "text"), "")


def collect_batch(client, con, verbose=True):
    """If a batch was submitted earlier, store its results once it has finished."""
    row = con.execute("SELECT v FROM ai_meta WHERE k='batch'").fetchone()
    if not row:
        return None
    info = json.loads(row[0])
    batch = client.messages.batches.retrieve(info["id"])
    if batch.processing_status != "ended":
        if verbose:
            print(f"batch {info['id']} still {batch.processing_status} "
                  f"({batch.request_counts.succeeded} done, {batch.request_counts.processing} processing)")
        return False
    ok = bad = 0
    for res in client.messages.batches.results(info["id"]):
        cid, last_ts = info["map"][res.custom_id]
        if res.result.type == "succeeded" and res.result.message.stop_reason != "refusal":
            try:
                ok += _save(con, cid, last_ts, json.loads(_text(res.result.message)), info["model"],
                            info.get("prompt_v", 1))
            except (ValueError, KeyError):
                bad += 1
        else:
            bad += 1
    con.execute("DELETE FROM ai_meta WHERE k='batch'")
    con.commit()
    if verbose:
        print(f"batch {info['id']} collected: {ok} saved, {bad} failed (will retry next run)")
    return True


def run(verbose=True):
    cfg, ai = _cfg()
    if not ai.get("enabled", False):
        return
    tz = ZoneInfo(cfg["timezone"])
    model, effort = ai.get("model", "claude-opus-5"), ai.get("effort", "medium")
    client = _client(ai)
    con = _db()

    if collect_batch(client, con, verbose) is False:
        return  # a batch is still running; don't submit overlapping work

    d, todo = pending()
    if todo.empty:
        if verbose:
            print("ai_reasons: nothing pending")
        return
    now = int(time.time())
    M = d["messages"]

    if len(todo) > ai.get("direct_limit", 20):
        # Many chats (e.g. the first run): one batch at half price, collected on a later run.
        reqs, mapping = [], {}
        for i, row in enumerate(todo.itertuples()):
            cid = f"c{i}"
            mapping[cid] = [row.conv_id, int(row.last_msg_ts)]
            reqs.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
                **_params(_transcript(M, row, tz, now), model, effort))))
        batch = client.messages.batches.create(requests=reqs)
        con.execute("INSERT OR REPLACE INTO ai_meta VALUES ('batch', ?)",
                    (json.dumps({"id": batch.id, "model": model, "prompt_v": PROMPT_VERSION, "map": mapping}),))
        con.commit()
        if verbose:
            print(f"ai_reasons: submitted batch {batch.id} with {len(reqs)} chats")
        return

    # A handful of new chats: read them straight away. Server-side fallback covers the rare refusal.
    ok = 0
    for row in todo.itertuples():
        try:
            msg = client.beta.messages.create(
                **_params(_transcript(M, row, tz, now), model, effort),
                betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            if msg.stop_reason == "refusal":
                continue
            ok += _save(con, row.conv_id, row.last_msg_ts, json.loads(_text(msg)), msg.model)
        except (anthropic.APIError, ValueError) as e:
            print(f"  {row.conv_id}: {e}", file=sys.stderr)
    con.commit()
    if verbose:
        print(f"ai_reasons: read {ok}/{len(todo)} chats directly")


def status():
    _, todo = pending()
    con = _db()
    n_done = con.execute("SELECT COUNT(*) FROM ai_reasons").fetchone()[0]
    b = con.execute("SELECT v FROM ai_meta WHERE k='batch'").fetchone()
    print(f"read by Claude: {n_done} · pending: {len(todo)} · batch running: "
          f"{json.loads(b[0])['id'] if b else 'no'}")


if __name__ == "__main__":
    status() if "--status" in sys.argv else run()
