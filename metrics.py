"""Turn raw respond.io messages into customer-service performance tables.

Definitions (all times in the workspace timezone from config.json):
- conversation: starts at a customer message that arrives with no earlier message,
  or after a silence >= new_conversation_gap_hours on that contact.
- human reply: an outgoing message sent by a person - source `echo` (typed in the
  WhatsApp Business app on the phone) or `user` (typed in the respond.io inbox).
  Automatic messages - the WhatsApp app's greeting/away replies, Workflows,
  broadcasts, AI Agent, API - are removed before any analysis: they never count
  as a reply, never start/extend a conversation, and don't appear in counts.
- turn: the customer starts waiting at their first message after a human reply;
  the turn ends at the next human reply. Wait time = reply_ts - wait_start.
- business-hours time: only seconds that fall inside config business_hours count,
  so a 23:00 message answered at 09:05 costs 5 minutes, not 10 hours.
- courtesy closer: a final "thanks"/"ok"/emoji/sticker - a customer message that
  doesn't need an answer, so it's never counted as missed.
"""
import datetime as dt
import json
import os
import re
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "respond.db")
FILLER = {"oh", "ohh", "ohhh", "ooh", "ah", "aw", "aww", "sir", "madam", "maam", "ma", "am", "so", "much", "very",
          "a", "lot", "you", "and", "then", "bye", "good", "night", "have", "nice", "day", "again", "dear",
          "all", "for", "the", "info", "information", "help", "update", "yes", "yeah", "yep", "alright", "k",
          "too", "as", "well", "also", "a", "great", "evening", "morning", "your", "guys", "team", "fetch",
          "i", "will", "we", "really", "definitely", "surely", "sure", "no", "it", "is", "that", "s", "to",
          "everything", "service", "services", "friends", "again", "once", "more", "kind", "lovely", "hehe", "haha"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def load_config():
    with open(os.path.join(HERE, "config.json")) as f:
        return json.load(f)


# ------------------------------------------------------------- business hours

def _hours(cfg):
    out = {}
    for i, d in enumerate(DAYS):
        span = cfg["business_hours"].get(d)
        if span:
            (h1, m1), (h2, m2) = [map(int, s.split(":")) for s in span]
            out[i] = (dt.time(h1, m1), dt.time(h2, m2))
    return out


def business_seconds(a, b, hours, tz):
    """Seconds between epoch a and b that fall inside business hours."""
    if b <= a:
        return 0
    start, end = dt.datetime.fromtimestamp(a, tz), dt.datetime.fromtimestamp(b, tz)
    total, day = 0.0, start.date()
    while day <= end.date():
        span = hours.get(day.weekday())
        if span:
            o = dt.datetime.combine(day, span[0], tz)
            c = dt.datetime.combine(day, span[1], tz)
            lo, hi = max(o, start), min(c, end)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += dt.timedelta(days=1)
    return total


def in_hours(ts, hours, tz):
    t = dt.datetime.fromtimestamp(ts, tz)
    span = hours.get(t.weekday())
    return bool(span) and span[0] <= t.time() < span[1]


# ------------------------------------------------------------- classification

def _is_courtesy(row, closers, intent=frozenset()):
    if row["type"] == "sticker":
        return True
    text = row["text"] if isinstance(row["text"], str) else ""
    text = text.strip().lower()
    if not text:
        return False
    if not re.search(r"[a-z0-9؀-ۿ]", text) and len(text) <= 8:
        return True  # emoji-only
    if "?" in text:
        return False
    # "Ohh okay thank you", "No worries, thanks!", "Ok sir": a closer phrase plus only
    # filler words. "Ok how much" keeps a real word, so it still needs an answer.
    rest = f" {re.sub(r'[^a-z0-9؀-ۿ ]+', ' ', text)} "
    hit = False
    for c in sorted(closers, key=len, reverse=True):
        c = re.sub(r"[^a-z0-9؀-ۿ ]+", " ", c).strip()
        if c and f" {c} " in rest:
            rest, hit = rest.replace(f" {c} ", " "), True
    if hit and all(w in FILLER for w in rest.split()):
        return True
    # "Oh I understand thank you!", "I've sorted it out, thank you though": a thank-you in a short
    # message with no question and no booking words is a goodbye, not a request.
    words = re.findall(r"[a-z\u0600-\u06ff']+", text)
    return (bool(THANKS.search(text)) and len(words) <= 12
            and not (set(words) & intent))


THANKS = re.compile(r"\b(thanks?|thank ?you|thank u|thx|thnx|tysm|ty|shukran|grazie|merci|gracias|danke|obrigad[oa])\b|شكرا")


def load(cfg=None):
    cfg = cfg or load_config()
    con = sqlite3.connect(DB)
    msgs = pd.read_sql("SELECT * FROM messages ORDER BY contact_id, ts, id", con)
    contacts = pd.read_sql("SELECT * FROM contacts", con)
    users = dict(con.execute("SELECT id, name FROM users").fetchall())
    con.close()
    labels = cfg["sender_labels"]
    closers = {c.lower() for c in cfg["courtesy_closers"]}
    pay_re = re.compile("|".join(re.escape(p) for p in cfg["payment_link_patterns"]), re.I)

    def role(r):
        if r["traffic"] == "incoming":
            return "customer"
        return "human" if r["source"] in ("echo", "user") else "auto"

    def who(r):
        if r["source"] == "user":
            return users.get(r["user_id"], f"User {r['user_id']}")
        return labels.get(r["source"], r["source"] or "Unknown")

    msgs["role"] = msgs.apply(role, axis=1)
    msgs["who"] = msgs.apply(who, axis=1)

    # The WhatsApp Business app's own greeting/away messages arrive as `echo`, same as
    # the rep typing. Reclassify them as automated: listed in config, or a text that
    # repeats verbatim and (almost) always fires within seconds of a customer message.
    norm = msgs["text"].fillna("").str.strip()
    prev_in = msgs["ts"].where(msgs["traffic"] == "incoming").groupby(msgs["contact_id"]).ffill()
    lag = msgs["ts"] - prev_in
    echo = (msgs["source"] == "echo") & (norm.str.len() >= 15)
    stats = pd.DataFrame({"t": norm[echo], "fast": (lag[echo] <= 15)}).groupby("t")["fast"].agg(["size", "mean"])
    auto_texts = set(stats[(stats["size"] >= 5) & (stats["mean"] >= 0.8)].index)
    auto_texts |= {t.strip() for t in cfg.get("auto_reply_texts", [])}
    is_auto = (msgs["source"] == "echo") & norm.isin(auto_texts)
    msgs.loc[is_auto, "role"] = "auto"
    msgs.loc[is_auto, "who"] = "WhatsApp auto-reply"
    msgs.attrs["auto_texts"] = sorted(auto_texts)
    intent_words = frozenset(cfg.get("intent_words", []))
    msgs["courtesy"] = msgs.apply(lambda r: r["role"] == "customer" and _is_courtesy(r, closers, intent_words), axis=1)
    # Context rule: after the team signs off ("Have a great day ❤️"), a short customer reply
    # with no question and no booking words ("You too", "God willing", "Will do") ends the chat.
    signoff_re = re.compile("|".join(re.escape(x) for x in cfg.get("team_signoffs", [])) or "$^", re.I)
    intent = set(cfg.get("intent_words", []))
    human_txt = msgs["text"].where(msgs["role"] == "human")
    last_team = human_txt.groupby(msgs["contact_id"]).ffill().fillna("")
    after_signoff = last_team.str.contains(signoff_re)

    def short_ack(r):
        t = r["text"] if isinstance(r["text"], str) else ""
        if r["type"] not in ("text", "sticker") or "?" in t:
            return False
        words = re.findall(r"[a-z\u0600-\u06ff']+", t.lower())
        return len(words) <= 6 and not (set(words) & intent)

    ctx = (msgs["role"] == "customer") & after_signoff & msgs.apply(short_ack, axis=1)
    msgs["courtesy"] = msgs["courtesy"] | ctx
    msgs["pay_link"] = (msgs["role"] != "customer") & msgs["text"].fillna("").str.contains(pay_re)
    contacts["name"] = (contacts["first_name"].fillna("") + " " + contacts["last_name"].fillna("")).str.strip()
    return msgs, contacts


def build(cfg=None, now=None):
    """Returns dict of DataFrames: conversations, turns, waiting, messages, contacts."""
    cfg = cfg or load_config()
    tz = ZoneInfo(cfg["timezone"])
    hours = _hours(cfg)
    gap = cfg["new_conversation_gap_hours"] * 3600
    now = now or int(dt.datetime.now(tz).timestamp())
    msgs, contacts = load(cfg)
    auto_texts = msgs.attrs.get("auto_texts", [])
    msgs = msgs[msgs["role"] != "auto"].copy()
    msgs.attrs["auto_texts"] = auto_texts

    convs, turns, waiting = [], [], []
    for cid, g in msgs.groupby("contact_id", sort=False):
        conv, prev_ts, wait = None, None, None  # wait = (start_ts, msg_row) of unanswered customer msg

        def close_turn(missed_by_ts):
            nonlocal wait
            if wait and not wait[2]:  # a real (non-courtesy) question went unanswered
                turns.append({"contact_id": cid, "conv_id": conv["conv_id"], "wait_start": wait[0],
                              "reply_ts": None, "responder": None, "missed": True,
                              "raw_s": missed_by_ts - wait[0], "biz_s": None})
                conv["missed_turns"] += 1
            wait = None

        for r in g.itertuples(index=False):
            new_conv = r.role == "customer" and (prev_ts is None or r.ts - prev_ts >= gap)
            if new_conv:
                if conv:
                    close_turn(r.ts)
                    convs.append(conv)
                conv = {"conv_id": f"{cid}-{r.ts}", "contact_id": cid, "start_ts": r.ts,
                        "first_reply_ts": None, "responder": None, "n_in": 0, "n_human": 0,
                        "pay_link": False, "last_ts": r.ts, "missed_turns": 0,
                        "after_hours": not in_hours(r.ts, hours, tz), "last_role": None,
                        "last_courtesy": False}
            prev_ts = r.ts
            if conv is None:
                continue  # business-initiated messages before the customer ever wrote
            conv["last_ts"], conv["last_role"] = r.ts, r.role
            if r.role == "customer":
                conv["n_in"] += 1
                conv["last_courtesy"] = r.courtesy
                if wait is None:
                    wait = (r.ts, r, r.courtesy)
                elif wait[2] and not r.courtesy:
                    wait = (r.ts, r, False)  # a real question followed a "thanks": clock starts now
            else:  # human (automatic messages were dropped above)
                conv["n_human"] += 1
                conv["pay_link"] |= bool(r.pay_link)
                if conv["first_reply_ts"] is None:
                    conv["first_reply_ts"], conv["responder"] = r.ts, r.who
                if wait:
                    turns.append({"contact_id": cid, "conv_id": conv["conv_id"], "wait_start": wait[0],
                                  "reply_ts": r.ts, "responder": r.who, "missed": False,
                                  "raw_s": r.ts - wait[0],
                                  "biz_s": business_seconds(wait[0], r.ts, hours, tz)})
                    wait = None
        if conv:
            window = cfg.get("waiting_window_hours", 72) * 3600
            if wait and not wait[2] and now - wait[0] > window:
                close_turn(now)  # went cold without a reply: a missed message, not "waiting now"
            elif wait and not wait[2]:
                waiting.append({"contact_id": cid, "conv_id": conv["conv_id"], "since": wait[0],
                                "last_text": g[g.ts >= wait[0]].text.fillna("").iloc[-1][:140]})
            convs.append(conv)

    C = pd.DataFrame(convs)
    T = pd.DataFrame(turns)
    W = pd.DataFrame(waiting, columns=["contact_id", "conv_id", "since", "last_text"])
    if C.empty:
        return {"conversations": C, "turns": T, "waiting": W, "messages": msgs, "contacts": contacts}

    C["frt_raw_s"] = C["first_reply_ts"] - C["start_ts"]
    C["frt_biz_s"] = [business_seconds(a, b, hours, tz) if pd.notna(b) else None
                      for a, b in zip(C["start_ts"], C["first_reply_ts"])]
    C["answered"] = C["first_reply_ts"].notna()
    # A conversation with no human reply at all, whose customer asked something real.
    C["unanswered"] = ~C["answered"] & ~((C["n_in"] == 1) & C["last_courtesy"])
    C["start"] = pd.to_datetime(C["start_ts"], unit="s", utc=True).dt.tz_convert(tz)
    C["responder"] = C["responder"].fillna("— no reply —")
    C = C.merge(contacts[["id", "name", "phone", "lifecycle", "status"]],
                left_on="contact_id", right_on="id", how="left").drop(columns="id")

    if not T.empty:
        T["start"] = pd.to_datetime(T["wait_start"], unit="s", utc=True).dt.tz_convert(tz)
    if not W.empty:
        W["waiting_min"] = (now - W["since"]) / 60
        W["waiting_biz_min"] = [business_seconds(s, now, hours, tz) / 60 for s in W["since"]]
        W["since_dt"] = pd.to_datetime(W["since"], unit="s", utc=True).dt.tz_convert(tz)
        W = W.merge(contacts[["id", "name", "phone", "lifecycle"]],
                    left_on="contact_id", right_on="id", how="left").drop(columns="id")
        W = W.sort_values("since")
    msgs["dt"] = pd.to_datetime(msgs["ts"], unit="s", utc=True).dt.tz_convert(tz)
    return {"conversations": C, "turns": T, "waiting": W, "messages": msgs, "contacts": contacts}


if __name__ == "__main__":
    d = build()
    C, T, W = d["conversations"], d["turns"], d["waiting"]
    print(f"{len(C)} conversations, {len(T)} turns, {len(W)} waiting now")
    a = C[C.answered]
    print("median first reply (biz min):", round(a.frt_biz_s.median() / 60, 1),
          "| p90:", round(a.frt_biz_s.quantile(.9) / 60, 1))
    print("unanswered convs:", int(C.unanswered.sum()), "| pay-link rate:", round(C.pay_link.mean() * 100, 1), "%")
    print(C.groupby("responder").size())


# ------------------------------------------------------------- lost-trip analysis

LOST_REASONS = [
    "Driver unavailable",
    "Don't service route",
    "Said price was too high",
    "Stopped responding after price was sent",
    "Stopped responding",
    "We didn't reply",
    "Payment problem",
    "Other / unclear",
]


def _rx(words):
    return re.compile("|".join(re.sub(r"\((?!\?)", "(?:", w) for w in words), re.I)


def _unavail_sig(m, idx):
    """Time of our last refusal in each chat, unless the customer accepted an alternative after it."""
    ref = m[m["unavail"]].groupby("conv_id")["ts"].max().reindex(idx)
    acc = m[m["accepted"]].groupby("conv_id")["ts"].max().reindex(idx)
    return ref.where(~(acc > ref))


def lost_trips(d, cfg=None, now=None):
    """Label every conversation that didn't turn into a trip with ONE reason.

    Converted = a booking confirmation was sent ("Your booking with Fetch Pet Taxi is confirmed", booking
    ref FP-...), in this chat or within confirmation_window_days after it (credited to one chat only).
    Payment links do NOT count.
    Skipped (neither converted nor lost): chats still in progress (last message < in_progress_hours
    ago) and follow-ups from customers who booked in the previous few days (e.g. "is the driver here?").

    Clear signals (driver unavailable, route not serviced, price objection, other) - the LATEST one in
    the chat wins. Without any, the structural reasons apply (stopped responding / we didn't reply).
    Reasons:
      1. Driver unavailable - the team said they were fully booked / no driver / can't make the time,
         even if another time + price was offered - unless the customer accepted that alternative.
      1b. Don't service route - the team said we don't operate there / don't do that route (same rule).
      (Other / unclear wins next if the team said it's against policy, or the
       customer said they booked elsewhere / sorted it out.)
      2. Said price was too high - the customer objected to the price.
      3. Stopped responding after price was sent - a price was quoted and the customer then went quiet.
      4. Stopped responding - the team had the last word, no price had been quoted.
      5. We didn't reply - the customer's last real message never got a reply.
      6. Other / unclear - anything else (e.g. the customer politely closed without a clear reason).
    """
    cfg = cfg or load_config()
    lc = cfg.get("lost_trips", {})
    tz = ZoneInfo(cfg["timezone"])
    now = now or int(dt.datetime.now(tz).timestamp())
    C, M = d["conversations"], d["messages"]
    if C.empty:
        return C.assign(outcome=[], reason=[])

    booked_rx = _rx(lc.get("booking_confirmed", []))
    unavail_rx = _rx(lc.get("driver_unavailable", []))
    unavail_ignore_rx = _rx(lc.get("driver_unavailable_ignore", []) or ["$^"])
    accepted_rx = _rx(lc.get("accepted_alternative", []) or ["$^"])
    intent_rx = _rx([rf"\b{w}\b" for w in cfg.get("intent_words", [])] or ["$^"])
    price_rx = _rx(lc.get("price_quote", []))
    objection_rx = _rx(lc.get("price_objection", []))
    other_team_rx = _rx(lc.get("other_signals_team", []) or ["$^"])
    payment_rx = _rx(lc.get("payment_problem", []) or ["$^"])
    route_rx = _rx(lc.get("route_not_serviced", []) or ["$^"])
    other_cust_rx = _rx(lc.get("other_signals_customer", []) or ["$^"])
    in_progress = lc.get("in_progress_hours", 24) * 3600
    followup = lc.get("recent_booking_days", 3) * 86400

    # messages -> conversation (conversations are contiguous per contact, ordered by start)
    starts = C[["conv_id", "contact_id", "start_ts"]].sort_values(["contact_id", "start_ts"])
    m = M.sort_values(["contact_id", "ts"])
    m = pd.merge_asof(m.sort_values("ts"), starts.sort_values("start_ts"), left_on="ts", right_on="start_ts",
                      by="contact_id", direction="backward").dropna(subset=["conv_id"])
    text = m["text"].fillna("")
    m = m.assign(
        booked=(m["role"] == "human") & text.str.contains(booked_rx),
        # "please book 1-2 days in advance, we get booked out quickly" is advice, not a refusal
        unavail=(m["role"] == "human") & text.str.contains(unavail_rx) & ~text.str.contains(unavail_ignore_rx),
        price=(m["role"] == "human") & text.str.contains(price_rx),
        objection=(m["role"] == "customer") & text.str.contains(objection_rx),
        real_cust=(m["role"] == "customer") & ~m["courtesy"],
        accepted=(m["role"] == "customer") & (text.str.contains(accepted_rx) | (m["type"] == "location")),
        cust_question=(m["role"] == "customer") & ~m["courtesy"] & (text.str.contains("?", regex=False)
                                                                     | text.str.contains(intent_rx)),
        other_team=(m["role"] == "human") & text.str.contains(other_team_rx),
        payment=(m["role"] == "customer") & text.str.contains(payment_rx),
        route=(m["role"] == "human") & text.str.contains(route_rx),
        other_cust=(m["role"] == "customer") & text.str.contains(other_cust_rx),
    )
    g = m.groupby("conv_id")
    idx = g.size().index

    def last_ts(mask):
        return m[mask].groupby("conv_id")["ts"].max().reindex(idx)

    price_ts = last_ts(m["price"]).fillna(0)
    agg = pd.DataFrame({
        "booked_msg": g["booked"].any(),
        "price_quoted": g["price"].any(),
        "last_msg_ts": g["ts"].max(),
        # When each clear signal last appeared. A team "we can't" message only counts if no new price was
        # quoted after it (they found another option); the customer's own words always count.
        # Refusal counts (even if we then offered another time + price) unless the customer accepted
        # the alternative afterwards - sent an address / pin / "yes, book it".
        "sig_unavail": _unavail_sig(m, idx),
        "sig_route": last_ts(m["route"]).where(lambda t: t > price_ts),
        "sig_other": pd.concat([last_ts(m["other_team"]).where(lambda t: t > price_ts),
                                last_ts(m["other_cust"])], axis=1).max(axis=1),
        "sig_price": last_ts(m["objection"]),
        "sig_payment": last_ts(m["payment"]),
    })
    # who had the last *meaningful* word: team, or a real (non sign-off) customer message
    meaningful = m[(m["role"] == "human") | m["real_cust"]]
    agg["last_by"] = meaningful.groupby("conv_id")["role"].last()
    agg["last_question_ts"] = m[m["cust_question"]].groupby("conv_id")["ts"].max()

    X = C.merge(agg, left_on="conv_id", right_index=True, how="left")
    # A trip = a booking confirmation was sent. It's credited to the chat where the sale started: the
    # earliest not-yet-credited chat of that customer in the window before the confirmation. Other chats
    # in that window (e.g. "I've paid" the next day, where the confirmation was sent) become follow-ups.
    window = lc.get("confirmation_window_days", 7) * 86400
    newreq_rx = _rx(lc.get("new_request_customer", []) or ["$^"])
    is_newreq = set(m.loc[((m["role"] == "customer") & m["text"].fillna("").str.contains(newreq_rx))
                          | m["price"] | m["unavail"], "conv_id"])
    confs = m.loc[m["booked"], ["contact_id", "ts"]].sort_values("ts")
    X["converted"] = False
    X["confirmed_later"] = False
    credited, followups = set(), set()
    by_contact = {k: v.sort_values("start_ts") for k, v in X[["conv_id", "contact_id", "start_ts"]].groupby("contact_id")}
    for cid, ts in confs.itertuples(index=False):
        cand = by_contact.get(cid)
        if cand is None:
            continue
        cand = cand[(cand["start_ts"] <= ts) & (cand["start_ts"] >= ts - window)]
        free = cand[~cand["conv_id"].isin(credited)]
        if free.empty:
            continue  # another confirmation for the same sale (e.g. a corrected ref)
        # Credit the most recent chat where a trip was actually asked for / quoted (repeat customers book
        # several trips); fall back to the earliest open chat if none looks like a request.
        asked = free[free["conv_id"].isin(is_newreq)]
        win = asked.iloc[-1]["conv_id"] if len(asked) else free.iloc[0]["conv_id"]
        credited.add(win)
        followups |= set(cand["conv_id"]) - {win}
    X.loc[X["conv_id"].isin(credited), "converted"] = True
    has_own = X["booked_msg"].fillna(False).astype(bool)
    X.loc[X["converted"] & ~has_own, "confirmed_later"] = True
    conv_ts = X.loc[X["converted"], ["contact_id", "start_ts"]]

    def recent_booking(r):
        prev = conv_ts[(conv_ts["contact_id"] == r.contact_id) & (conv_ts["start_ts"] < r.start_ts)]
        return bool(len(prev)) and r.start_ts - prev["start_ts"].max() <= followup

    X["outcome"] = "Lost"
    X.loc[X["converted"], "outcome"] = "Converted"
    open_ = ~X["converted"] & (now - X["last_msg_ts"].fillna(now) < in_progress)
    X.loc[open_, "outcome"] = "In progress"
    # A recent customer is only "following up on a booking" if this chat doesn't look like a NEW trip
    # request (asking availability / a time / price, or we quoted or turned them down).
    newreq = X["conv_id"].isin(is_newreq).values
    fu = ((X["outcome"] == "Lost") & ~newreq & X.apply(recent_booking, axis=1)) | \
         ((X["outcome"] == "Lost") & X["conv_id"].isin(followups))
    X.loc[fu, "outcome"] = "Follow-up on a booking"

    def reason(r):
        if r.outcome != "Lost":
            return None
        R = LOST_REASONS
        # The latest clear signal wins: "fully booked tonight" ... then "too expensive anyway" = price.
        sig = {"Driver unavailable": r.sig_unavail, "Don't service route": r.sig_route,
               "Other / unclear": r.sig_other, "Said price was too high": r.sig_price,
               "Payment problem": r.sig_payment}
        sig = {k: v for k, v in sig.items() if pd.notna(v)}
        if sig:
            win = max(sig, key=sig.get)
            # The customer asked a real question (a "?", or about price/time/booking) AFTER that and never
            # heard back: our miss is what lost it. Pleading or goodbyes don't count.
            if r.last_by == "customer" and pd.notna(r.last_question_ts) and r.last_question_ts > sig[win]:
                return "We didn't reply"
            return win
        if r.last_by == "human":
            return "Stopped responding after price was sent" if r.price_quoted else "Stopped responding"
        if r.last_by == "customer":
            return "We didn't reply"
        return "Other / unclear"

    X["reason"] = X.apply(reason, axis=1)
    X["rule_reason"] = X["reason"]
    X["why"] = None
    X["why_quote"] = None
    X["reason_by"] = "rules"

    # Claude's reading (ai_reasons.py) replaces the rules for any chat it has read in its current state.
    # Bookings stay rule-based: a sent confirmation always means "Converted".
    try:
        import ai_reasons
        ai = ai_reasons.load_results()
    except Exception:
        ai = {}
    if ai:
        for i, r in X[X["outcome"] != "Converted"].iterrows():
            res = ai.get(r["conv_id"])
            if not res or res["last_msg_ts"] != int(r["last_msg_ts"]):
                continue
            if res["reason"] == ai_reasons.FOLLOW_UP:
                X.at[i, "outcome"], X.at[i, "reason"] = "Follow-up on a booking", None
            elif res["reason"] == ai_reasons.IN_PROGRESS:
                X.at[i, "outcome"], X.at[i, "reason"] = "In progress", None
            else:
                X.at[i, "outcome"], X.at[i, "reason"] = "Lost", res["reason"]
            X.at[i, "why"], X.at[i, "why_quote"], X.at[i, "reason_by"] = res["explanation"], res["key_quote"], "Claude"
    return X
