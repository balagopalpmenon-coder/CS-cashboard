"""Pull respond.io contacts + message history into local SQLite (respond.db). READ-ONLY.

First run: every contact's full history. Later runs: refresh all contact records
(cheap, ~8 calls) and re-read messages only for contacts whose lastInteractionTime
moved since the previous sync (minus a 2h overlap), stopping at the first
message already stored.

  python sync.py          # incremental
  python sync.py --full   # re-read every contact's history
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

from respond_mcp import MCP

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "respond.db")
CFG = json.load(open(os.path.join(HERE, "config.json")))
TZ = ZoneInfo(CFG["timezone"])
CTX = ("Reading workspace data for an internal read-only dashboard that measures "
       "customer service response times and conversation handling performance.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, phone TEXT, country TEXT,
  status TEXT, lifecycle TEXT, assignee_id INTEGER, assignee_name TEXT, tags TEXT,
  created_at INTEGER);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, contact_id INTEGER, channel_id INTEGER, ts INTEGER,
  traffic TEXT, source TEXT, user_id INTEGER, workflow_id INTEGER, broadcast_id INTEGER,
  type TEXT, text TEXT, delivery TEXT);
CREATE INDEX IF NOT EXISTS ix_msg_contact_ts ON messages(contact_id, ts);
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def db():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def _next_cursor(page):
    nxt = (page.get("pagination") or {}).get("next")
    return int(nxt.split("cursorId=")[1].split("&")[0]) if nxt else None


def paged(m, tool, args, limit):
    cur = None
    while True:
        params = {"limit": limit}
        if cur:
            params["cursorId"] = cur
        page = m.call(tool, {**args, "params": params, "context": CTX})
        yield from page.get("items", [])
        cur = _next_cursor(page)
        if not cur:
            return


def contacts(m, filt=None):
    return paged(m, "search_contacts", {
        "search": "", "timezone": CFG["timezone"], "filter": filt or {"$and": []}}, 100)


def _epoch(v):
    """Epoch seconds from ms/us numbers, digit strings or ISO strings."""
    if isinstance(v, str):
        if not v.lstrip("-").isdigit():
            return int(dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
        v = int(v)
    v = int(v)
    while v > 10**11:  # ms or us -> s
        v //= 1000
    return v


def msg_ts(x):
    # Status timestamps are epoch ms; messageId encodes epoch us. Some older
    # messages come back with string timestamps, so normalise everything.
    if x.get("status"):
        try:
            return _epoch(x["status"][0]["timestamp"])
        except (TypeError, ValueError):
            pass
    return _epoch(x["messageId"])


def sync_messages(m, con, contact_id):
    known = {r[0] for r in con.execute("SELECT id FROM messages WHERE contact_id=?", (contact_id,))}
    rows = []
    for x in paged(m, "list_messages", {"identifier": f"id:{contact_id}"}, 50):
        mid = int(x["messageId"])
        if mid in known:
            break  # newest-first: everything older is already stored
        s = x.get("sender") or {}
        msg = x.get("message") or {}
        text = msg.get("text")
        if text is None and msg.get("type") == "attachment":
            text = (msg.get("attachment") or {}).get("type", "attachment")
        rows.append((mid, int(x["contactId"]), x.get("channelId"), msg_ts(x), x["traffic"],
                     s.get("source"), s.get("userId"), s.get("workflowId"), s.get("broadcastHistoryId"),
                     msg.get("type"), text, (x["status"][-1]["value"] if x.get("status") else None)))
    # Also refresh delivery state of the last few already-stored outgoing messages is not needed
    # for response-time metrics, so it is skipped to keep calls low.
    con.executemany("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def main(full=False):
    started = time.time()
    con = db()
    m = MCP()

    for u in paged(m, "list_space_users", {}, 100):
        con.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?)", (
            u["id"], f"{u.get('firstName') or ''} {u.get('lastName') or ''}".strip(), u.get("email"), u.get("role")))

    n = 0
    for c in contacts(m):
        a = c.get("assignee") or {}
        con.execute("INSERT OR REPLACE INTO contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            c["id"], c.get("firstName"), c.get("lastName"), c.get("phone"), c.get("countryCode"),
            c.get("status"), c.get("lifecycle"), a.get("id"),
            f"{a.get('firstName') or ''} {a.get('lastName') or ''}".strip() or None,
            json.dumps(c.get("tags") or []), c.get("created_at")))
        n += 1
    con.commit()

    last = con.execute("SELECT v FROM meta WHERE k='last_sync'").fetchone()
    if full or not last:
        ids = [r[0] for r in con.execute("SELECT id FROM contacts")]
    else:
        since = dt.datetime.fromtimestamp(int(last[0]) - 7200, TZ).strftime("%Y-%m-%d %H:%M:%S")
        ids = [c["id"] for c in contacts(m, {"$and": [{
            "category": "contactField", "field": "lastInteractionTime",
            "operator": "isTimestampAfter", "value": since}]})]

    new = 0
    for i, cid in enumerate(ids, 1):
        try:
            new += sync_messages(m, con, cid)
        except Exception as e:  # one bad contact must not kill the run
            print(f"  contact {cid}: {e}", file=sys.stderr)
        if i % 50 == 0:
            con.commit()
            print(f"  {i}/{len(ids)} contacts, {new} new messages", flush=True)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync', ?)", (str(int(started)),))
    con.commit()
    print(f"{dt.datetime.now(TZ):%Y-%m-%d %H:%M} synced {n} contacts, "
          f"re-read {len(ids)} histories, {new} new messages in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main(full="--full" in sys.argv)
    try:  # Claude reads any new / changed chats that didn't book (see ai_reasons.py)
        import ai_reasons
        ai_reasons.run()
    except Exception as e:  # never let the AI step break the sync
        print(f"ai_reasons failed: {e}", file=sys.stderr)
