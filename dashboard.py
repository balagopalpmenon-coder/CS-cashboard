"""Customer-service performance dashboard over respond.io chats (read-only).

streamlit run dashboard.py --server.port 8789
"""
import datetime as dt
import json
import os
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import metrics

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = metrics.load_config()
TZ = ZoneInfo(CFG["timezone"])
TARGET = CFG["first_reply_target_min"]

# Reference data-viz palette (validated): categorical slot 1/2, sequential blue, status.
BLUE, ORANGE = "#2a78d6", "#eb6834"
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
GOOD, WARN, SERIOUS, CRIT = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"
MUTED = "#898781"

st.set_page_config(page_title="CS Performance", page_icon="💬", layout="wide")

# SHARE_MODE=1 = the copy served to business partners over the internet: same dashboard, behind a password.
SHARE = os.environ.get("SHARE_MODE") == "1"
if SHARE:
    import hmac
    _pw_file = os.path.join(HERE, "share.env")
    _pw = open(_pw_file).read().strip().split("=", 1)[-1] if os.path.exists(_pw_file) else ""
    if not _pw:
        st.error("Sharing isn't set up (no password).")
        st.stop()
    if not st.session_state.get("authed"):
        st.title("Customer service performance")
        entered = st.text_input("Password", type="password")
        if entered and hmac.compare_digest(entered, _pw):
            st.session_state["authed"] = True
            st.rerun()
        elif entered:
            st.error("Wrong password")
        st.stop()


st.markdown("""<style>
div[class*="st-key-picker"] div:has(> input),
div[class*="st-key-picker"] div[data-baseweb="select"] > div {
    border: 2px solid #2a78d6 !important; background: rgba(42,120,214,0.10) !important;
    cursor: pointer; }
div[class*="st-key-picker"] div:has(> input):hover,
div[class*="st-key-picker"] div[data-baseweb="select"] > div:hover { background: rgba(42,120,214,0.18) !important; }
div[class*="st-key-picker"] input { cursor: pointer; font-weight: 600; }
/* phones: headline tiles two per row instead of one long column */
@media (max-width: 640px) {
  .st-key-kpis { flex-direction: row !important; flex-wrap: wrap !important; gap: 0.5rem 0.75rem !important; }
  .st-key-kpis > div { flex: 1 1 calc(50% - 0.75rem) !important; min-width: 0 !important; width: auto !important; }
  .st-key-kpis [data-testid="stMetricValue"] { font-size: 1.6rem !important; }
  .st-key-kpis [data-testid="stMetricLabel"] p { font-size: 0.8rem !important; }
}
div[class*="st-key-picker"] label p { font-size: 0.95rem; font-weight: 600; color: #2a78d6; }
</style>""", unsafe_allow_html=True)


def picker(label, options, key, **kw):
    """A dropdown that looks clickable: instruction above, blue outline."""
    with st.container(key=f"picker_{key}"):
        return st.selectbox(label, options, key=key, **kw)


def chart(fig, **kw):
    """Plotly chart; the hover toolbar is hidden in the shared (phone-friendly) copy."""
    st.plotly_chart(fig, config={"displayModeBar": not SHARE, "displaylogo": False}, **kw)


@st.cache_data(ttl=300, max_entries=2)
def data(sync_marker, cfg_mtime):  # args bust the cache on each new sync / config edit
    d = metrics.build(CFG)
    d["lost"] = metrics.lost_trips(d, CFG)
    return d


def last_sync():
    try:
        con = sqlite3.connect(metrics.DB)
        v = con.execute("SELECT v FROM meta WHERE k='last_sync'").fetchone()
        return dt.datetime.fromtimestamp(int(v[0]), TZ) if v else None
    except sqlite3.Error:
        return None


def fmt_min(s):
    if s is None or pd.isna(s):
        return "—"
    m = s / 60
    if m < 1:
        return f"{s:.0f}s"
    if m < 90:
        return f"{m:.0f} min"
    return f"{m / 60:.1f} h"


def style(fig, h=300):
    fig.update_layout(height=h, margin=dict(l=8, r=8, t=8, b=8), bargap=0.25,
                      hoverlabel=dict(font_size=13), legend=dict(orientation="h", y=1.08, x=0))
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="rgba(137,135,129,0.2)", zeroline=False)
    return fig


_ls = last_sync()
d = data(_ls.timestamp() if _ls else 0, os.path.getmtime(os.path.join(HERE, "config.json")))
C, T, W, M = d["conversations"], d["turns"], d["waiting"], d["messages"]

# ------------------------------------------------------------------ header + filters
ls = last_sync()
st.title("Customer service performance")
st.caption(f"WhatsApp Business via respond.io · read-only · last sync "
           f"{ls:%a %d %b %H:%M} · business hours from config.json · first-reply target {TARGET} min"
           if ls else "No sync yet - run `python sync.py`")

if C.empty:
    st.info("No conversations synced yet.")
    st.stop()

today = dt.datetime.now(TZ).date()
with st.container(border=True):
    f1, f2, f3 = st.columns([2, 2, 3])
    preset = f1.selectbox("Period", ["Last 7 days", "Last 30 days", "Month to date", "Today", "All time", "Custom"])
    if preset == "Custom":
        rng = f2.date_input("Dates", (today - dt.timedelta(days=13), today))
        d0, d1 = (rng[0], rng[1]) if len(rng) == 2 else (rng[0], rng[0])
    else:
        d1 = today
        d0 = {"Last 7 days": today - dt.timedelta(days=6), "Last 30 days": today - dt.timedelta(days=29),
              "Month to date": today.replace(day=1), "Today": today,
              "All time": C["start"].min().date()}[preset]
        f2.text_input("Dates", f"{d0:%d %b} – {d1:%d %b %Y}", disabled=True)
    people = sorted(p for p in C["responder"].unique() if p != "— no reply —")
    who = f3.multiselect("Responder", people, default=people,
                         help="Who sent the first human reply. 'CS rep (WhatsApp app)' = typed on the phone.")

    WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    g1, g2 = st.columns(2)
    wd_pick = g1.pills("Days of week", WEEKDAYS, selection_mode="multi", default=[],
                       help="Click the days you want to include. Nothing selected = every day. "
                            "Filters by the day the customer wrote.")
    hr0, hr1 = g2.slider("Hours of day", 0, 24, (0, 24), format="%d:00",
                         help="Filters by the hour the customer wrote (their first message for conversations, "
                              "each message for reply times). 9–21 = 09:00 up to 20:59.")
    invert = g2.toggle("Show outside these hours instead", help="Show messages OUTSIDE the selected hours instead - e.g. pick 9–21 "
                                       "and switch this on to see night-time performance.")
    wd_idx = {WEEKDAYS.index(w) for w in wd_pick} if wd_pick else set(range(7))  # none picked = all days


    def when_ok(ts):
        """Weekday + hour-of-day filter on a tz-aware datetime Series."""
        inside = (ts.dt.hour >= hr0) & (ts.dt.hour < hr1)
        return ts.dt.dayofweek.isin(wd_idx) & (~inside if invert else inside)


    in_range = (C["start"].dt.date >= d0) & (C["start"].dt.date <= d1) & when_ok(C["start"])
    c = C[in_range & (C["responder"].isin(who) | (C["responder"] == "— no reply —"))]
    t = T[(T["start"].dt.date >= d0) & (T["start"].dt.date <= d1) & when_ok(T["start"])] if not T.empty else T
    t = t[t["responder"].isin(who) | t["missed"]] if not t.empty else t
    filtered = len(wd_idx) < 7 or (hr0, hr1) != (0, 24) or invert
    if filtered:
        st.caption(f"Showing {', '.join(w for w in WEEKDAYS if WEEKDAYS.index(w) in wd_idx)} · "
                   f"{'outside' if invert else 'between'} {hr0:02d}:00–{hr1:02d}:00")
    ans = c[c["answered"]]
    # Business-hours time is 0 by definition outside business hours, so once the user scopes
    # hours of the day, every speed metric switches to plain clock time.
    hour_filtered = (hr0, hr1) != (0, 24) or invert
    FRT, WAIT = ("frt_raw_s", "raw_s") if hour_filtered else ("frt_biz_s", "biz_s")
    UNIT = "clock time" if hour_filtered else "business hours"
    if hour_filtered:
        st.caption("⏱ Hours filter on - reply speeds below are plain clock time, not business-hours time.")

# ------------------------------------------------------------------ explanations (ⓘ popovers)
_bh = CFG["business_hours"]
_spans = {tuple(v) for v in _bh.values() if v}
BH_TXT = (f"{next(iter(_spans))[0]}–{next(iter(_spans))[1]} every day" if len(_spans) == 1 and len(_bh) == 7
          else ", ".join(f"{d.title()} {v[0]}–{v[1]}" for d, v in _bh.items() if v))
GAP = CFG["new_conversation_gap_hours"]
SPEED = ("**Clock time** is used because an hours filter is on - it's the real time the customer waited."
         if hour_filtered else
         f"**Business hours only** ({BH_TXT}): time outside those hours doesn't count. A message at 23:00 "
         f"answered at 09:05 counts as 5 min, not 10 h. Pick an hours filter to switch to plain clock time.")
COMMON = ("*Automatic replies (the WhatsApp greeting, away messages, Workflows) are ignored everywhere - "
          "only messages typed by a person count as a reply.*")
EXPLAIN = {
    "convs": f"""**Conversations**

A conversation starts when a customer messages for the first time, or after **{GAP} hours of silence** in their chat.
Counted by the date and hour of the customer's **first** message, within your filters.

{COMMON}""",
    "frt": f"""**Median first reply**

For each conversation: the time from the customer's first message to the **first reply typed by a person**.
The *median* is the middle value - half of conversations were answered faster than this, half slower.

{SPEED}

The small grey figure underneath is the same thing in plain clock time.

{COMMON}""",
    "p90": f"""**90% answered within**

Line up every conversation's first-reply time from fastest to slowest: 9 out of 10 were answered within
this time. It shows the *slow tail* that the median hides - if it's high, some customers wait a long time
even when the median looks great.

{SPEED}""",
    "target": f"""**First reply ≤ {TARGET} min**

Out of conversations that got a reply, the share whose first human reply came within **{TARGET} minutes**.
Conversations with no reply at all are not in this % - they're counted in *Never answered*.

{SPEED}

Change the target in `config.json` → `first_reply_target_min`.""",
    "never": f"""**Never answered**

Conversations where the customer asked something and **no person ever replied**.

Not counted: a chat where the customer only sent a sign-off ("thanks", "you too", 👍, a sticker).
The WhatsApp auto-greeting does **not** count as a reply.""",
    "pay": """**Payment link sent**

Share of conversations in which the team sent a payment link (Wio or Stripe).
Used as a stand-in for "turned into a booking" - respond.io doesn't know about actual payments.

Link patterns are in `config.json` → `payment_link_patterns`.""",
    "waiting": f"""**Waiting for a reply right now**

Customers whose **last real message has no reply from a person**, from the last
**{CFG.get('waiting_window_hours', 72)} hours**. Older ones are counted as missed in the stats instead.

A chat is treated as **finished, not waiting**, when the customer's last message is a sign-off:
"thanks", "you too", "no thank you", "God willing", an emoji or sticker - or any short reply with no
question after the team said goodbye ("Have a great day ❤️").

**Overdue** = waiting more than {CFG['unanswered_alert_min']} minutes of business hours.
The day/hour filters don't apply here - everyone waiting is always shown.

**Clear** hides a customer you've dealt with (e.g. you called them). It's saved on this dashboard only -
nothing changes in respond.io or WhatsApp. If that customer sends a *new* message later, they'll show up
again. Cleared customers still count in the stats. Undo with **Restore** under *Cleared*.""",
    "daily": f"""**Daily trend**

*Left:* conversations started each day, split into answered (blue) and never answered (red).

*Right:* the median first reply for conversations started that day. Dashed line = your {TARGET} min target.

{SPEED}""",
    "buckets": f"""**How fast customers got their first reply**

Every conversation in the period sorted into a speed band by its first human reply time.
The % on each bar is its share of all conversations. *No reply* = never answered.

{SPEED}""",
    "heatmap": """**When customers message**

Number of new conversations by the **weekday and hour** of the customer's first message.
Darker = busier. Use it to check your staffing matches when customers actually write.""",
    "hourly": f"""**Reply speed by hour of day**

Unlike the first-reply numbers, this uses **every customer message**, not just the first one in a chat.

*Top:* for messages sent in each hour, the median **clock time** until a person replied.
Orange = over the {TARGET} min target. Very long waits (usually overnight) are cut off at the top,
with the real value printed on the bar.

*Bottom:* how many customer messages arrived in each hour - answered (blue) or never answered (red).
A message counts as never answered if the chat went quiet ({GAP} h) or {CFG.get('waiting_window_hours', 72)} h
passed with no reply. Sign-offs like "thanks" are not counted.""",
    "responder": f"""**By responder**

Each conversation is credited to **whoever sent the first human reply**.

- **CS rep (WhatsApp app)** - replies typed on the WhatsApp Business phone app. respond.io can't tell *who*
  held the phone, so everyone using it is grouped together.
- **Named people** (e.g. Rehan Khan) - replies typed in the respond.io inbox.

Columns: *Share* = % of all conversations · *Median first reply* ({UNIT}) · *90th pct* = slow tail ·
*≤ {TARGET} min* = % answered within target · *Median reply (all msgs)* = typical wait for **any** reply
in the chat, not just the first · *Replies sent* = customer messages this person answered ·
*Payment link sent* = % of their conversations with a Wio/Stripe link · *After-hours convs* = started
outside {BH_TXT}.""",
    "lost": """**Why chats didn't turn into a trip**

Every conversation that didn't become a trip gets **one** main reason.

If the chat contains a clear reason - reasons 1, 2, 3, 7 and 8 below - **the latest one in the chat wins**:
"fully booked at 5pm" → new time offered → "too expensive" counts as *price*. If there's no clear reason, it
comes down to who went quiet (4, 5, 6).

1. **Driver unavailable** - we couldn't do the time/date they asked for ("fully booked at 5pm", "no drivers
   tonight"). Still counts if we offered another time and a price - **unless the customer accepted** that
   alternative (sent their address / pin, or said yes / book it). Advice like "book 1–2 days in advance, we get
   booked out quickly" doesn't count.
2. **Don't service route** - we told them we don't go there ("we do not operate within Abu Dhabi",
   "not able to do drop offs to Al Ain", "we only do Dubai ↔ Ajman").
3. **Said price was too high** - the customer complained about the price ("too expensive", "too high",
   "found cheaper", "out of budget"). Just *asking* for a discount doesn't count.
4. **Stopped responding after price was sent** - we quoted a price (AED …) and the customer went quiet.
5. **Stopped responding** - we had the last word, but no price had been quoted yet (often the customer
   never answered "when would you like to book?").
6. **We didn't reply** - the customer's last real message never got an answer. Our miss.
7. **Payment problem** - they wanted to book but got stuck paying: the link / Apple Pay / card failed, or they
   wanted to pay cash or by transfer.
8. **Other / unclear** - not a lost sale we could win: against policy (e.g. unvaccinated
   puppies), wanted a van, booked someone else / a friend drove, job applicants, sales pitches, tests.

**How the reason is decided:** Claude (Anthropic's AI, model Opus 5) reads each chat that didn't book - like
a person would - and picks the reason, with a one-line explanation and the key message. You see these under
**Read the chats**. Each chat is read once; it's re-read only if new messages arrive. Chats Claude hasn't read
yet (brand new ones) fall back to word-matching rules. Whether a trip was *booked* is never up to Claude - it's
always "a booking confirmation was sent".""",
    "lost_counts": f"""**What counts as converted?**

A conversation **became a trip** only if we sent a **booking confirmation** - "Your booking with Fetch Pet
Taxi is confirmed" / "Booking ref: FP-…". Sending a payment link on its own doesn't count.

If the confirmation was sent a bit later in a separate chat (within {CFG['lost_trips'].get('confirmation_window_days', 7)} days),
the trip is credited to the chat where the customer first asked - once only, so nothing is double-counted.

**Not counted** (left out of the conversion rate):
- *In progress* - last message less than {CFG['lost_trips'].get('in_progress_hours', 24)} h ago, still might book.
- *Follow-up on a booking* - the customer booked in the previous {CFG['lost_trips'].get('recent_booking_days', 3)}
  days and is just checking on it ("is the driver on the way?").

Conversion rate = became a trip ÷ (became a trip + didn't convert).""",
    "review": """**Review conversations**

Read the actual chat behind the numbers. Pick a list - slowest first replies, never answered,
answered but no payment link, or most recent - then choose a conversation.
The `+5 min` next to each message is the time since the previous message. Automatic replies are hidden.""",
}


def info(key, where=None, label="How it's calculated"):
    """Small ⓘ button that opens the explanation for `key` on click."""
    with (where or st).popover(label, icon=":material/info:", type="tertiary", help="How is this calculated?"):
        st.markdown(EXPLAIN[key])


def heading(text, key, sub=False):
    """Section title with an ⓘ button beside it."""
    a, b = st.columns([3, 1], vertical_alignment="center")
    if sub:
        a.markdown(f"**{text}**")
    else:
        a.subheader(text)
    info(key, b)


# ------------------------------------------------------------------ KPI tiles
within = (ans[FRT] <= TARGET * 60).mean() if len(ans) else None
with st.container(border=True):
    k_row = st.container(horizontal=True, wrap=True, horizontal_alignment="distribute", gap="medium", key="kpis")
    k = [k_row.container(width=165) for _ in range(6)]
    tiles = [
        ("convs", "Chats", f"{len(c):,}", None),
        ("frt", "Median first reply", fmt_min(ans[FRT].median() if len(ans) else None),
         None if hour_filtered or not len(ans) else f"{fmt_min(ans['frt_raw_s'].median())} clock time"),
        ("p90", "90% answered within", fmt_min(ans[FRT].quantile(.9) if len(ans) else None), None),
        ("target", f"First reply ≤ {TARGET} min", f"{within * 100:.0f}%" if within is not None else "—", None),
        ("never", "Never answered", f"{int(c['unanswered'].sum())}", None),
        ("pay", "Payment link sent", f"{c['pay_link'].mean() * 100:.0f}%" if len(c) else "—", None),
    ]
    for col, (key, label, value, delta) in zip(k, tiles):
        with col:
            st.metric(label, value, delta=delta, delta_color="off")
            info(key, label="Info")

# ------------------------------------------------------------------ waiting now
# "Clear" is stored only in this dashboard's local database - nothing is sent to respond.io.
# A cleared item is keyed by (customer, time their unanswered message arrived): if the same
# customer writes again later, that's a new wait and it shows up again.
def _cleared_db():
    con = sqlite3.connect(metrics.DB)
    con.execute("CREATE TABLE IF NOT EXISTS cleared_waits (contact_id INTEGER, since INTEGER, "
                "cleared_at INTEGER, PRIMARY KEY (contact_id, since))")
    return con


def clear_waits(rows):
    st.session_state["sel_gen"] = st.session_state.get("sel_gen", 0) + 1
    with _cleared_db() as con:
        con.executemany("INSERT OR IGNORE INTO cleared_waits VALUES (?,?,?)",
                        [(int(r.contact_id), int(r.since), int(dt.datetime.now(TZ).timestamp())) for r in rows])


def restore_waits(rows):
    st.session_state["sel_gen"] = st.session_state.get("sel_gen", 0) + 1
    with _cleared_db() as con:
        con.executemany("DELETE FROM cleared_waits WHERE contact_id=? AND since=?",
                        [(int(r.contact_id), int(r.since)) for r in rows])


with _cleared_db() as _con:
    cleared = pd.read_sql("SELECT * FROM cleared_waits", _con)

alert = CFG["unanswered_alert_min"]
with st.container(border=True):
    heading("Waiting for a reply right now", "waiting")
    if filtered:
        st.caption("Day/hour filters don't apply here - everyone waiting is always shown.")
    w = W.copy()
    if not w.empty:
        key = w["contact_id"].astype(str) + ":" + w["since"].astype(str)
        gone = set(cleared["contact_id"].astype(str) + ":" + cleared["since"].astype(str))
        cleared_now = w[key.isin(gone)]
        w = w[~key.isin(gone)].reset_index(drop=True)
    else:
        cleared_now = w
    if w.empty:
        st.success("✓ Nobody is waiting - every customer's last real message has a reply "
                   "(or you've cleared it).")
    else:
        w["Waiting"] = w["waiting_min"].apply(lambda m: fmt_min(m * 60))
        w["Business-hours wait"] = w["waiting_biz_min"].apply(lambda m: fmt_min(m * 60))
        w["Flag"] = w["waiting_biz_min"].apply(lambda m: "● Overdue" if m > alert else "○ Recent")
        late = int((w["waiting_biz_min"] > alert).sum())
        st.caption(f"{len(w)} customers whose last message has no reply · {late} over {alert} business-hours minutes. "
                   f"Last {CFG.get('waiting_window_hours', 72)} h only. Oldest first. "
                   "Tick rows and press **Clear** to hide ones you've dealt with.")
        sel = st.dataframe(
            w[["Flag", "name", "phone", "since_dt", "Waiting", "Business-hours wait", "lifecycle", "last_text"]]
            .rename(columns={"name": "Customer", "phone": "Phone", "since_dt": "Since",
                             "lifecycle": "Stage", "last_text": "Their last message"}),
            hide_index=True, width="stretch", height=min(38 * (len(w) + 1), 320),
            on_select="rerun", selection_mode="multi-row",
            key=f"waiting_sel_{st.session_state.get('sel_gen', 0)}",
            column_config={"Since": st.column_config.DatetimeColumn(format="ddd D MMM, HH:mm")})
        picked = [i for i in (sel.selection.rows if sel else []) if i < len(w)]
        row = st.container(horizontal=True)
        if row.button(f"Clear selected ({len(picked)})", icon=":material/done_all:", type="primary",
                      disabled=not picked):
            clear_waits(w.iloc[picked].itertuples())
            st.rerun()
        if row.button("Clear all", icon=":material/clear_all:"):
            clear_waits(w.itertuples())
            st.rerun()
    if not cleared_now.empty:
        with st.expander(f"Cleared ({len(cleared_now)}) - hidden from the list above"):
            cn = cleared_now.reset_index(drop=True)
            rsel = st.dataframe(cn[["name", "phone", "since_dt", "last_text"]].rename(columns={
                "name": "Customer", "phone": "Phone", "since_dt": "Since", "last_text": "Their last message"}),
                hide_index=True, width="stretch", on_select="rerun", selection_mode="multi-row", key=f"cleared_sel_{st.session_state.get('sel_gen', 0)}",
                column_config={"Since": st.column_config.DatetimeColumn(format="ddd D MMM, HH:mm")})
            rp = [i for i in (rsel.selection.rows if rsel else []) if i < len(cn)]
            if st.button(f"Restore selected ({len(rp)})", icon=":material/undo:", disabled=not rp):
                restore_waits(cn.iloc[rp].itertuples())
                st.rerun()

# ------------------------------------------------------------------ trends
with st.container(border=True):
    heading("Daily trend", "daily")
    days = [x for x in pd.date_range(d0, d1, freq="D").date if x.weekday() in wd_idx]
    day_lbl = [f"{x:%a %d %b}" for x in days]
    daily = c.groupby(c["start"].dt.date).agg(
        convs=("conv_id", "size"), missed=("unanswered", "sum")).reindex(days, fill_value=0)
    frt_d = ans.groupby(ans["start"].dt.date)[FRT].median().reindex(days) / 60

    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("**Conversations per day**")
        fig = go.Figure()
        fig.add_bar(x=day_lbl, y=daily["convs"] - daily["missed"], name="Answered", marker_color=BLUE,
                    hovertemplate="%{x}<br>Answered: %{y}<extra></extra>")
        fig.add_bar(x=day_lbl, y=daily["missed"], name="Never answered", marker_color=CRIT,
                    hovertemplate="%{x}<br>Never answered: %{y}<extra></extra>")
        fig.update_layout(barmode="stack")
        fig.update_xaxes(type="category")
        chart(style(fig), width="stretch")
    with b.container(border=True):
        st.markdown(f"**Median first reply per day** · {UNIT}, dashed line = {TARGET} min target")
        fig = go.Figure()
        fig.add_scatter(x=day_lbl, y=frt_d.values, mode="lines+markers", name="Median first reply",
                        line=dict(color=BLUE, width=2), marker=dict(size=8), connectgaps=False,
                        hovertemplate="%{x}<br>Median: %{y:.1f} min<extra></extra>")
        fig.add_hline(y=TARGET, line=dict(color=MUTED, dash="dash", width=1))
        fig.update_layout(showlegend=False)
        fig.update_yaxes(rangemode="tozero")
        fig.update_xaxes(type="category")
        chart(style(fig), width="stretch")

# ------------------------------------------------------------------ distribution + heatmap
with st.container(border=True):
    st.subheader("First replies & busy times")
    a, b = st.columns(2)
    with a.container(border=True):
        heading("How fast customers got their first reply", "buckets", sub=True)
        edges = [0, 5, 15, 60, 240, float("inf")]
        names = ["≤ 5 min", "5–15 min", "15–60 min", "1–4 h", "> 4 h"]
        cols = [GOOD, BLUE, WARN, SERIOUS, CRIT]
        buckets = pd.cut(ans[FRT] / 60, edges, labels=names, include_lowest=True).value_counts().reindex(names)
        x = names + ["No reply"]
        y = list(buckets.fillna(0).astype(int)) + [int(c["unanswered"].sum())]
        tot = max(sum(y), 1)
        fig = go.Figure(go.Bar(x=x, y=y, marker_color=cols + [MUTED],
                               text=[f"{v / tot * 100:.0f}%" for v in y], textposition="outside",
                               hovertemplate="%{x}: %{y} conversations<extra></extra>"))
        fig.update_layout(showlegend=False)
        chart(style(fig), width="stretch")
    with b.container(border=True):
        heading("When customers message (new conversations by weekday × hour)", "heatmap", sub=True)
        hm = c.assign(wd=c["start"].dt.dayofweek, hr=c["start"].dt.hour).pivot_table(
            index="wd", columns="hr", values="conv_id", aggfunc="size", fill_value=0
        ).reindex(index=range(7), columns=range(24), fill_value=0)
        fig = go.Figure(go.Heatmap(
            z=hm.values, x=[f"{h:02d}" for h in range(24)], y=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            colorscale=[[i / (len(SEQ) - 1), s] for i, s in enumerate(SEQ)], xgap=2, ygap=2,
            hovertemplate="%{y} %{x}:00<br>%{z} conversations<extra></extra>", colorbar=dict(thickness=10)))
        fig.update_yaxes(autorange="reversed", showgrid=False)
        chart(style(fig), width="stretch")

# ------------------------------------------------------------------ response by hour
with st.container(border=True):
    if not t.empty:
        heading("Reply speed by hour of day", "hourly")
        st.caption("Grouped by the hour the customer sent their message. Wait = plain clock time until a team "
                   "member replied. Very long waits (usually overnight) are cut off at the top, with their real value "
                   "printed on the bar. Hover any bar for details.")
        th = t.assign(hr=t["start"].dt.hour)
        hours_x = [f"{h:02d}:00" for h in range(24)]
        got = th[~th["missed"]]
        wait = (got.groupby("hr")["raw_s"].median() / 60).reindex(range(24))
        n_ans = got.groupby("hr").size().reindex(range(24), fill_value=0)
        n_miss = th[th["missed"]].groupby("hr").size().reindex(range(24), fill_value=0)

        # Scale to the ordinary hours; outliers (e.g. 3 h at 02:00) would flatten everything else,
        # so anything above the scale is cut off at the top and keeps its real value as a label.
        normal = wait[wait <= TARGET * 2].dropna()
        ycap = max(normal.max() if len(normal) else TARGET, TARGET) * 1.3 if wait.notna().any() else TARGET * 1.3
        top = wait.max() if wait.notna().any() else TARGET
        ymax = top * 1.3 if top < TARGET * 0.8 else ycap  # all hours fast: zoom right in

        with st.container(border=True):
            st.markdown(f"**How long customers waited for a reply** · orange = over the {TARGET} min target")
            fig = go.Figure(go.Bar(
                x=hours_x, y=wait.clip(upper=ycap).values,
                marker_color=[BLUE if (pd.isna(v) or v <= TARGET) else SERIOUS for v in wait.values],
                text=[fmt_min(v * 60) if pd.notna(v) else "" for v in wait.values],
                textposition=["inside" if pd.notna(v) and v > ycap else "outside" for v in wait.values],
                insidetextanchor="end", textfont=dict(size=11), cliponaxis=False,
                customdata=list(zip(n_ans.values, [fmt_min(v * 60) for v in wait.values])),
                hovertemplate="%{x}<br>Median wait: %{customdata[1]}<br>based on %{customdata[0]} replies<extra></extra>"))
            if ymax > TARGET:
                fig.add_hline(y=TARGET, line=dict(color=MUTED, dash="dash", width=1))
            fig.update_layout(showlegend=False)
            fig.update_xaxes(type="category", tickangle=-45)
            fig.update_yaxes(range=[0, ymax], ticksuffix=" min")
            chart(style(fig, 280), width="stretch")

        with st.container(border=True):
            st.markdown("**How many customer messages came in** · red = never answered")
            fig = go.Figure()
            fig.add_bar(x=hours_x, y=n_ans.values, name="Answered", marker_color=BLUE,
                        hovertemplate="%{x}<br>%{y} answered<extra></extra>")
            fig.add_bar(x=hours_x, y=n_miss.values, name="Never answered", marker_color=CRIT,
                        hovertemplate="%{x}<br>%{y} never answered<extra></extra>")
            fig.update_layout(barmode="stack")
            fig.update_xaxes(type="category", tickangle=-45)
            fig.update_yaxes(tickformat="d", rangemode="tozero")  # whole messages only
            chart(style(fig, 280), width="stretch")
        over = [hours_x[h] for h in range(24) if pd.notna(wait[h]) and wait[h] > TARGET]
        worst = n_miss.idxmax() if n_miss.sum() else None
        notes = []
        if over:
            notes.append(f"Median wait over {TARGET} min at {', '.join(over)} (orange bars).")
        if worst is not None:
            notes.append(f"Most unanswered messages arrive around {hours_x[worst]} ({int(n_miss[worst])}).")
        if notes:
            st.caption(" ".join(notes))

# ------------------------------------------------------------------ per responder
with st.container(border=True):
    heading("By responder", "responder")
    rows = []
    for r, g in c[c["answered"]].groupby("responder"):
        tg = t[(t["responder"] == r)] if not t.empty else t
        rows.append({
            "Responder": r, "Conversations": len(g), "Share": len(g) / max(len(c), 1),
            "Median first reply": fmt_min(g[FRT].median()),
            **({} if hour_filtered else {"Median first reply (clock)": fmt_min(g["frt_raw_s"].median())}),
            "90th pct first reply": fmt_min(g[FRT].quantile(.9)),
            f"≤ {TARGET} min": (g[FRT] <= TARGET * 60).mean(),
            "Median reply (all msgs)": fmt_min(tg[WAIT].median() if len(tg) else None),
            "Replies sent": int(len(tg)),
            "Payment link sent": g["pay_link"].mean(),
            "After-hours convs": int(g["after_hours"].sum()),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows).sort_values("Conversations", ascending=False), hide_index=True,
                     width="stretch", column_config={
                         "Share": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1),
                         f"≤ {TARGET} min": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1),
                         "Payment link sent": st.column_config.NumberColumn(format="percent")})
    st.caption("Replies typed on the WhatsApp Business phone app reach respond.io as 'echo' messages with no user "
               "attached - they're grouped as **CS rep (WhatsApp app)**. Replies typed in the respond.io inbox "
               "show the team member's name.")

# ------------------------------------------------------------------ conversation review
def show_transcript(row):
    """Render one conversation's messages (automatic replies are already excluded)."""
    end = C[(C["contact_id"] == row["contact_id"]) & (C["start_ts"] > row["start_ts"])]["start_ts"].min()
    msgs = M[(M["contact_id"] == row["contact_id"]) & (M["ts"] >= row["start_ts"])]
    if pd.notna(end):
        msgs = msgs[msgs["ts"] < end]
    st.caption(f"{row['name'] or ''} {row['phone'] or ''} · stage: {row['lifecycle']} · "
               f"{row['n_in']} from customer / {row['n_human']} from team · automatic replies hidden")
    box = st.container(height=420)
    prev = None
    for m in msgs.itertuples():
        gap = f" · +{fmt_min(m.ts - prev)}" if prev else ""
        prev = m.ts
        text = m.text if isinstance(m.text, str) and m.text else f"[{m.type}]"
        with box.chat_message("user" if m.role == "customer" else "assistant",
                              avatar="🧑" if m.role == "customer" else "💬"):
            st.markdown(f"<small style='color:{MUTED}'>{m.dt:%a %d %b %H:%M}{gap} · "
                        f"{m.who if m.role != 'customer' else 'Customer'}</small>", unsafe_allow_html=True)
            st.text(text)



with st.container(border=True):
    heading("Review conversations", "review")
    view = st.radio("Show", ["Slowest first reply", "Never answered", "No payment link sent", "Most recent"],
                    horizontal=True)
    rv = c.copy()
    if view == "Slowest first reply":
        rv = rv[rv["answered"]].sort_values(FRT, ascending=False)
    elif view == "Never answered":
        rv = rv[rv["unanswered"]].sort_values("start_ts", ascending=False)
    elif view == "No payment link sent":
        rv = rv[rv["answered"] & ~rv["pay_link"]].sort_values("start_ts", ascending=False)
    else:
        rv = rv.sort_values("start_ts", ascending=False)
    rv = rv.head(200)
    rv["First reply"] = rv[FRT].apply(fmt_min)
    rv["label"] = rv.apply(lambda r: f"{r['start']:%a %d %b %H:%M} · {r['name'] or 'No name'} · {r['phone'] or ''} · "
                                     f"first reply {r['First reply']}", axis=1)
    if rv.empty:
        st.caption("Nothing here for this period.")
    else:
        pick = picker(f"👇 Click to choose a conversation to read ({len(rv)} in this list)", rv["label"],
                      key="review_pick")
        show_transcript(rv[rv["label"] == pick].iloc[0])

# ------------------------------------------------------------------ lost trips
with st.container(border=True):
    heading("Why chats didn't turn into a trip", "lost")
    X = d["lost"]
    x = X[X["conv_id"].isin(c["conv_id"])]  # same date / day / hour / responder filters as above
    n_conv, n_lost = int((x["outcome"] == "Converted").sum()), int((x["outcome"] == "Lost").sum())
    n_open = int((x["outcome"] == "In progress").sum())
    n_fu = int((x["outcome"] == "Follow-up on a booking").sum())
    n_ask = n_conv + n_lost
    period = preset.lower() if preset != "Custom" else f"{d0:%d %b} – {d1:%d %b}"

    # Make it obvious when the filters at the top are hiding chats from this section
    in_period = X[(X["start"].dt.date >= d0) & (X["start"].dt.date <= d1)]
    hidden = in_period[~in_period["conv_id"].isin(x["conv_id"])]
    if len(hidden):
        why_hidden = []
        if len(wd_idx) < 7:
            why_hidden.append("only " + ", ".join(w for w in WEEKDAYS if WEEKDAYS.index(w) in wd_idx))
        if (hr0, hr1) != (0, 24) or invert:
            why_hidden.append(f"{'outside' if invert else 'only'} {hr0:02d}:00–{hr1:02d}:00")
        if set(who) != set(people):
            why_hidden.append("only " + ", ".join(who) if who else "no responders selected")
        n_hid_lost = int((hidden["outcome"] == "Lost").sum())
        st.warning(f"⚠️ **Filters are on** ({'; '.join(why_hidden)}). {len(hidden)} chats from this period are hidden"
                   + (f", including {n_hid_lost} that didn't book" if n_hid_lost else "")
                   + ". Clear the day / hour / responder filters at the top to see everything.")
    if n_ask:
        st.markdown(f"#### {period.capitalize()}: {len(x)} chats")
        b1, b2, b3 = st.columns(3)
        b1.metric("✅ Booked", f"{n_conv}", help="We sent them a booking confirmation.")
        b2.metric("❌ Didn't book", f"{n_lost}", help="Asked about a trip, the chat ended, no booking.")
        b3.metric("⏳ Not decided yet / not a new trip", f"{n_open + n_fu}",
                  help=f"{n_open} still being discussed · {n_fu} asking about a ride already booked "
                       "(driver ETA, refunds…).")
        st.caption(f"{n_conv} + {n_lost} + {n_open + n_fu} = {len(x)} chats. "
                   f"Booking rate: **{n_conv / n_ask * 100:.0f}%** of chats that have finished "
                   f"({n_conv} of {n_conv + n_lost}).")
    info("lost_counts", label="How is this counted?")

    lost = x[x["outcome"] == "Lost"]
    if len(lost):
        n_ai = int((lost["reason_by"] == "Claude").sum())
        st.caption(f"🤖 Claude read {n_ai} of these {len(lost)} chats to decide the reason"
                   + ("." if n_ai == len(lost) else f"; the other {len(lost) - n_ai} use word-matching until Claude "
                      "reads them (new chats are read within ~30 minutes)."))
    if lost.empty:
        st.caption("No lost chats in this selection.")
    else:
        counts = lost["reason"].value_counts()
        counts = counts[counts > 0].sort_values()  # biggest ends up on top of a horizontal bar chart
        SHORT = {
            "Driver unavailable": "We had no driver free at the time they wanted",
            "Don't service route": "We don't go to / from that place",
            "Said price was too high": "They told us it was too expensive",
            "Stopped responding after price was sent": "We sent the price, they went quiet",
            "Stopped responding": "They went quiet before we got to a price",
            "We didn't reply": "Their last message never got an answer from us",
        "Payment problem": "They got stuck paying, or wanted to pay another way",
            "Other / unclear": "Not a real trip request, or no clear reason",
        }
        st.markdown(f"##### Why the {n_lost} chats didn't book")
        fig = go.Figure(go.Bar(
            y=[f"<b>{r}</b><br><span style='font-size:11px;color:{MUTED}'>{SHORT.get(r, '')}</span>"
               for r in counts.index],
            x=counts.values, orientation="h", marker_color=BLUE,
            text=[f"{v} chat{'s' if v != 1 else ''} ({v / n_lost * 100:.0f}%)" for v in counts.values],
            textposition="outside",
            cliponaxis=False, customdata=[SHORT.get(r, "") for r in counts.index],
            hovertemplate="%{customdata}<br>%{x} chats<extra></extra>"))
        fig.update_layout(showlegend=False)
        fig.update_xaxes(showticklabels=False, showgrid=False, range=[0, counts.max() * 1.35])
        fig.update_yaxes(showgrid=False, tickfont=dict(size=13), ticklabelstandoff=8)
        style(fig, 40 + 58 * len(counts))
        fig.update_xaxes(showgrid=False)
        chart(fig, width="stretch")

    # Reader: every lost reason, plus booked and still-in-progress chats (so today's chats are readable too)
    st.markdown("##### Read the chats")
    groups = {f"Didn't book · {r}": lost[lost["reason"] == r] for r in metrics.LOST_REASONS}
    groups["Booked ✓"] = x[x["outcome"] == "Converted"]
    groups["Still in progress (last message under 24 h ago)"] = x[x["outcome"] == "In progress"]
    groups["Messaging about a trip already booked"] = x[x["outcome"] == "Follow-up on a booking"]
    groups = {k: v for k, v in groups.items() if len(v)}
    # Search by name or any part of the phone number, across every list
    q = st.text_input("🔎 Find a chat by name or phone number", placeholder="e.g. Usha, or 956 50",
                      key="chat_search").strip()
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        hit = x["name"].fillna("").str.contains(q, case=False, regex=False)
        if len(digits) >= 3:
            hit |= x["phone"].fillna("").str.replace(r"\D", "", regex=True).str.contains(digits, regex=False)
        found = x[hit]
        tag = {cid: (f"Didn't book · {r.reason}" if r.outcome == "Lost" else
                     {"Converted": "Booked ✓", "In progress": "Still in progress",
                      "Follow-up on a booking": "About a trip already booked"}[r.outcome])
               for cid, r in zip(found["conv_id"], found.itertuples())}
        groups = {f"Search results for “{q}”": found.assign(_tag=found["conv_id"].map(tag))} if len(found) else {}
        if not len(found):
            st.caption(f"No chats match “{q}” in this period. Try **All time** in the period filter at the top.")
    if not groups:
        if not q:
            st.caption("No chats in this selection.")
    else:
        why = picker("👇 Click to choose which chats to read", list(groups), key="lost_reason",
                     format_func=lambda k: f"{k} ({len(groups[k])})")
        lv = groups[why].sort_values("start_ts", ascending=False).head(200).copy()
        lv["label"] = lv.apply(lambda r: f"{r['start']:%a %d %b %H:%M} · {r['name'] or 'No name'} · {r['phone'] or ''}"
                                         + (f" · {r['_tag']}" if "_tag" in lv and pd.notna(r.get("_tag")) else ""),
                               axis=1)
        lpick = picker(f"👇 Click to choose a chat to read ({len(lv)} in this list, newest first)", lv["label"],
                       key="lost_pick")
        sel_row = lv[lv["label"] == lpick].iloc[0]
        if isinstance(sel_row.get("why"), str) and sel_row["why"]:
            quote = f"  \n> “{sel_row['why_quote']}”" if sel_row.get("why_quote") else ""
            st.info(f"**Why (read by Claude):** {sel_row['why']}{quote}")
        elif sel_row["outcome"] == "Lost":
            st.caption("Reason from word-matching rules - Claude hasn't read this chat yet.")
        show_transcript(sel_row)

with st.expander("How these numbers are calculated"):
    st.markdown(metrics.__doc__)
    st.markdown(f"""
- **Business hours** (edit `config.json`): {json.dumps(CFG['business_hours'])}
- **New conversation** after {CFG['new_conversation_gap_hours']} h of silence.
- **Automatic replies ignored everywhere**, detected as: {'; '.join(f'"{x}"' for x in M.attrs.get('auto_texts', [])) or 'none yet'}
  plus anything sent by a Workflow, broadcast, AI Agent or API. Add missed ones to `auto_reply_texts` in config.json.
- **Payment link** = an outgoing message containing {', '.join(CFG['payment_link_patterns'])}.
- Conversation close/resolve history isn't exposed by respond.io's API - only each contact's *current* status - so
  resolution time isn't measured.
""")
