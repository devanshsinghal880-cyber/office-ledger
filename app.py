"""Office Ledger: Streamlit + SQLite.

Run locally:  streamlit run app.py
Settings (environment variables):
  LEDGER_DB          path of the SQLite file (default: ./data/ledger.db)
  MANAGER_PASSWORD   first-run password for Manager (default: manager@123)
  HR_PASSWORD        first-run password for HR (default: hr@123)
"""
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from datetime import date, timedelta

import pandas as pd
import streamlit as st

DB_PATH = os.environ.get("LEDGER_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ledger.db"))
DEFAULT_HEADS = ["Kitchen", "Legal", "Salary", "Marketing", "Incentive", "Rent", "Internet", "Communication", "Traveling"]
MODES = ["Cash", "UPI", "Bank"]
ADD_NEW = "+ Add new sub head…"

st.set_page_config(page_title="Office Ledger", page_icon="₹", layout="wide")


# ---------------------------------------------------------------- database
@st.cache_resource
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY, date TEXT NOT NULL, type TEXT NOT NULL CHECK (type IN ('Receive','Pay')),
            head TEXT NOT NULL, sub_head TEXT DEFAULT '', mode TEXT NOT NULL, amount REAL NOT NULL CHECK (amount > 0),
            remarks TEXT DEFAULT '', added_by TEXT, edited_by TEXT, created_at REAL);
        CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS heads (name TEXT PRIMARY KEY, position INTEGER);
        CREATE TABLE IF NOT EXISTS sub_heads (head TEXT, name TEXT, PRIMARY KEY (head, name));
        CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, salt TEXT, hash TEXT);
        """
    )
    if not con.execute("SELECT 1 FROM settings LIMIT 1").fetchone():
        # first run: starting values from seed.json next to this file, if present
        seed = {"open_date": date.today().replace(day=1).isoformat(), "open_cash": 0, "open_bank": 0, "heads": DEFAULT_HEADS, "sub_heads": {}}
        seed_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed.json")
        if os.path.exists(seed_file):
            with open(seed_file, encoding="utf-8") as f:
                seed.update(json.load(f))
        con.executemany("INSERT INTO settings VALUES (?,?)", [("open_date", seed["open_date"]), ("open_cash", str(seed["open_cash"])),
                                                             ("open_bank", str(seed["open_bank"]))])
        con.executemany("INSERT INTO heads VALUES (?,?)", [(h, i) for i, h in enumerate(seed["heads"])])
        con.executemany("INSERT OR IGNORE INTO sub_heads VALUES (?,?)", [(h, s) for h, subs in seed["sub_heads"].items() for s in subs])
    if not con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        set_password(con, "Manager", os.environ.get("MANAGER_PASSWORD", "manager@123"))
        set_password(con, "HR", os.environ.get("HR_PASSWORD", "hr@123"))
    return con


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def set_password(con, user, password):
    salt = secrets.token_hex(16)
    con.execute("INSERT OR REPLACE INTO users VALUES (?,?,?)", (user, salt, hash_pw(password, salt)))


def check_password(user, password):
    row = db().execute("SELECT salt, hash FROM users WHERE name=?", (user,)).fetchone()
    return bool(row) and secrets.compare_digest(hash_pw(password, row[0]), row[1])


def settings():
    s = dict(db().execute("SELECT key, value FROM settings").fetchall())
    return {"open_date": s["open_date"], "open_cash": float(s["open_cash"]), "open_bank": float(s["open_bank"])}


def heads():
    return [r[0] for r in db().execute("SELECT name FROM heads ORDER BY position, name")]


def sub_heads(head):
    return [r[0] for r in db().execute("SELECT name FROM sub_heads WHERE head=? ORDER BY name COLLATE NOCASE", (head,))]


def entries_df():
    df = pd.read_sql_query("SELECT * FROM entries", db())
    if df.empty:
        df = pd.DataFrame(columns=["id", "date", "type", "head", "sub_head", "mode", "amount", "remarks", "added_by", "edited_by", "created_at"])
    df["amount"] = df["amount"].astype(float)
    return df


# ---------------------------------------------------------------- maths
def is_bank(mode):
    return mode != "Cash"


def summary(df, cfg, start, end):
    """Opening, in/out and closing for [start, end] (ISO dates). UPI counts as Bank."""
    v = df[df["date"] >= cfg["open_date"]]
    start = max(start, cfg["open_date"])
    before, within = v[v["date"] < start], v[(v["date"] >= start) & (v["date"] <= end)]

    def tot(d, typ, bank):
        m = (d["type"] == typ) & (d["mode"].map(is_bank) == bank)
        return float(d.loc[m, "amount"].sum())

    r = {"open_cash": cfg["open_cash"] + tot(before, "Receive", False) - tot(before, "Pay", False),
         "open_bank": cfg["open_bank"] + tot(before, "Receive", True) - tot(before, "Pay", True),
         "in_cash": tot(within, "Receive", False), "in_bank": tot(within, "Receive", True),
         "out_cash": tot(within, "Pay", False), "out_bank": tot(within, "Pay", True)}
    r["in"], r["out"] = r["in_cash"] + r["in_bank"], r["out_cash"] + r["out_bank"]
    r["close_cash"] = r["open_cash"] + r["in_cash"] - r["out_cash"]
    r["close_bank"] = r["open_bank"] + r["in_bank"] - r["out_bank"]
    return r


def periods(d):
    week_start = d - timedelta(days=d.weekday())
    month_end = (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return [("Day", d, d), ("Week", week_start, week_start + timedelta(days=6)), ("Month", d.replace(day=1), month_end)]


def inr(x):
    sign = "−" if x < 0 else ""
    x = round(abs(x), 2)
    whole, frac = divmod(x, 1)
    s = str(int(whole))
    if len(s) > 3:  # Indian grouping: 12,34,567
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(i - 2, 0):i] for i in range(len(head), 0, -2)][::-1])
        s = head + "," + tail
    return f"{sign}₹{s}" + (f".{round(frac * 100):02d}" if frac else "")


# ---------------------------------------------------------------- look
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Bodoni+Moda:opsz,wght@6..96,500;6..96,600&family=Outfit:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap');
:root{--gold:#b8924a;--in:#2e8b62;--out:#b23a48;--line:rgba(184,146,74,.35)}
html, body, [class*="css"], .stMarkdown, input, select, textarea, button {font-family:"Outfit",system-ui,sans-serif}
h1,h2,h3{font-family:"Bodoni Moda",Georgia,serif !important;font-weight:500 !important;letter-spacing:.01em}
.block-container{padding-top:2rem;max-width:1100px}
.eyebrow{font-size:.68rem;letter-spacing:.28em;text-transform:uppercase;color:var(--gold);margin:0}
.hero{border:1px solid var(--line);border-top:2px solid var(--gold);padding:22px 26px;border-radius:4px;margin-bottom:18px}
.hero .big{font-family:"Bodoni Moda",Georgia,serif;font-size:clamp(2rem,6vw,3.2rem);line-height:1.1}
.hero .row{display:flex;gap:32px;flex-wrap:wrap;margin-top:10px;font-family:"DM Mono",monospace}
.hero small,.pc small{display:block;font-size:.62rem;letter-spacing:.2em;text-transform:uppercase;opacity:.7;font-family:"Outfit",sans-serif}
.pc{border:1px solid var(--line);border-radius:4px;padding:18px;height:100%}
.pc h3{margin:0 0 2px;font-size:1.3rem}
.pc .rng{font-size:.8rem;opacity:.7;margin-bottom:12px}
.io{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
.io div{padding:8px 10px;border-left:2px solid;font-family:"DM Mono",monospace}
.io .i{color:var(--in);border-color:var(--in);background:rgba(46,139,98,.08)}
.io .o{color:var(--out);border-color:var(--out);background:rgba(178,58,72,.08)}
.pc table{width:100%;font-size:.85rem;border-collapse:collapse}
.pc td{padding:3px 0;border:0}
.pc td:last-child{text-align:right;font-family:"DM Mono",monospace}
.pc tr.t td{border-top:1px solid var(--line);padding-top:7px;font-weight:500}
.pc tr.g td{color:var(--gold)}
div[data-testid="stTabs"] button p{letter-spacing:.14em;text-transform:uppercase;font-size:.78rem}
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------- login
def login_screen():
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown('<p class="eyebrow">Cash · UPI · Bank</p>', unsafe_allow_html=True)
        st.title("Office Ledger")
        with st.form("login"):
            user = st.selectbox("User", ["Manager", "HR"])
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary", width="stretch"):
                if check_password(user, pw):
                    st.session_state.user = user
                    st.rerun()
                else:
                    time.sleep(1)
                    st.error("Wrong user or password.")


if "user" not in st.session_state:
    db()
    login_screen()
    st.stop()

USER = st.session_state.user
cfg = settings()
df = entries_df()

top_l, top_r = st.columns([4, 1])
with top_l:
    st.markdown('<p class="eyebrow">Cash · UPI · Bank</p>', unsafe_allow_html=True)
    st.title("Office Ledger")
with top_r:
    st.caption(f"Signed in as **{USER}**")
    if st.button("Sign out"):
        st.session_state.clear()
        st.rerun()

if "flash" in st.session_state:
    st.toast(st.session_state.pop("flash"))

tab_over, tab_new, tab_all, tab_set = st.tabs(["Overview", "New Entry", "All Entries", "Settings"])

# ---------------------------------------------------------------- overview
with tab_over:
    d = st.date_input("Show balances for", value=date.today(), format="DD/MM/YYYY", key="rep_date")
    day = summary(df, cfg, d.isoformat(), d.isoformat())
    st.markdown(
        f"""<div class="hero"><p class="eyebrow">Closing balance · {d.strftime('%A, %d %B %Y')}</p>
        <div class="big">{inr(day['close_cash'] + day['close_bank'])}</div>
        <div class="row"><div><small>Cash</small>{inr(day['close_cash'])}</div><div><small>Bank + UPI</small>{inr(day['close_bank'])}</div></div></div>""",
        unsafe_allow_html=True,
    )
    cols = st.columns(3)
    pers = periods(d)
    for col, (name, a, b) in zip(cols, pers):
        r = summary(df, cfg, a.isoformat(), b.isoformat())
        rng = a.strftime("%a, %d %b %Y") if a == b else f"{a.strftime('%d %b')} – {b.strftime('%d %b %Y')}"
        col.markdown(
            f"""<div class="pc"><h3>{name}</h3><div class="rng">{rng}</div>
            <div class="io"><div class="i"><small>Money in</small>{inr(r['in'])}</div><div class="o"><small>Money out</small>{inr(r['out'])}</div></div>
            <table>
            <tr><td>Opening balance</td><td>{inr(r['open_cash'] + r['open_bank'])}</td></tr>
            <tr><td>Cash in / out</td><td>{inr(r['in_cash'])} / {inr(r['out_cash'])}</td></tr>
            <tr><td>Bank+UPI in / out</td><td>{inr(r['in_bank'])} / {inr(r['out_bank'])}</td></tr>
            <tr class="t"><td>Closing cash</td><td>{inr(r['close_cash'])}</td></tr>
            <tr><td>Closing bank</td><td>{inr(r['close_bank'])}</td></tr>
            <tr class="t g"><td>Closing total</td><td>{inr(r['close_cash'] + r['close_bank'])}</td></tr>
            </table></div>""",
            unsafe_allow_html=True,
        )
    st.subheader("By Head")
    rows = []
    for h in sorted(set(heads()) | set(df["head"]), key=lambda x: (x not in heads(), x)):
        row = {"Head": h}
        for name, a, b in pers:
            m = (df["head"] == h) & (df["date"] >= a.isoformat()) & (df["date"] <= b.isoformat())
            row[f"{name} in"] = df.loc[m & (df["type"] == "Receive"), "amount"].sum()
            row[f"{name} out"] = df.loc[m & (df["type"] == "Pay"), "amount"].sum()
        rows.append(row)
    st.dataframe(pd.DataFrame(rows).style.format({c: lambda v: inr(v) if v else "—" for c in rows[0] if c != "Head"} if rows else {}),
                 hide_index=True, width="stretch")


# ---------------------------------------------------------------- new / edit entry
def entry_form(prefix, existing=None):
    e = existing or {}
    typ = st.radio("Type", ["Receive", "Pay"], horizontal=True, key=f"{prefix}_type",
                   index=["Receive", "Pay"].index(e.get("type", "Receive")))
    c1, c2 = st.columns(2)
    dt = c1.date_input("Date", value=date.fromisoformat(e["date"]) if e else date.today(), format="DD/MM/YYYY", key=f"{prefix}_date")
    amt = c2.number_input("Amount (₹)", min_value=0.0, step=100.0, value=float(e.get("amount", 0.0)), key=f"{prefix}_amt")
    c3, c4 = st.columns(2)
    hl = heads()
    if e and e["head"] not in hl:
        hl.append(e["head"])
    head = c3.selectbox("Head", hl, index=hl.index(e["head"]) if e else 0, key=f"{prefix}_head")
    subs = sub_heads(head)
    if e.get("sub_head") and e["sub_head"] not in subs:
        subs.append(e["sub_head"])
    opts = ["— None —"] + subs + [ADD_NEW]
    pending = st.session_state.pop(f"{prefix}_sub_pending", None)
    if pending in opts:
        st.session_state[f"{prefix}_sub"] = pending
    sub = c4.selectbox("Sub Head", opts, index=opts.index(e["sub_head"]) if e.get("sub_head") else 0, key=f"{prefix}_sub")
    if sub == ADD_NEW:
        new_sub = c4.text_input(f"New sub head for {head}", key=f"{prefix}_newsub").strip()
        if c4.button("Add sub head", key=f"{prefix}_addsub"):
            if new_sub:
                db().execute("INSERT OR IGNORE INTO sub_heads VALUES (?,?)", (head, new_sub))
                st.session_state[f"{prefix}_sub_pending"] = new_sub
                st.session_state.flash = f"Sub head '{new_sub}' added"
                st.rerun()
            else:
                c4.warning("Type a name first.")
    mode = st.radio("Payment mode", MODES, horizontal=True, index=MODES.index(e.get("mode", "Cash")), key=f"{prefix}_mode")
    remarks = st.text_input("Remarks (optional)", value=e.get("remarks", "") or "", key=f"{prefix}_rem")
    return {"type": typ, "date": dt.isoformat(), "amount": amt, "head": head,
            "sub_head": "" if sub in ("— None —", ADD_NEW) else sub, "mode": mode, "remarks": remarks.strip()}


def reset_form(prefix):
    for k in list(st.session_state):
        if k.startswith(prefix + "_"):
            del st.session_state[k]


with tab_new:
    st.markdown('<p class="eyebrow">Record a transaction</p>', unsafe_allow_html=True)
    new = entry_form("new")
    if st.button("Save Entry", type="primary"):
        if new["amount"] <= 0:
            st.error("Enter an amount greater than 0.")
        else:
            db().execute("INSERT INTO entries (id,date,type,head,sub_head,mode,amount,remarks,added_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex[:12], new["date"], new["type"], new["head"], new["sub_head"], new["mode"],
                          new["amount"], new["remarks"], USER, time.time()))
            reset_form("new")
            st.session_state.flash = "Entry saved"
            st.rerun()
    st.subheader("Today's entries")
    today = df[df["date"] == date.today().isoformat()].sort_values("created_at", ascending=False)
    if today.empty:
        st.caption("No entries today yet.")
    else:
        st.dataframe(today[["type", "head", "sub_head", "mode", "amount", "remarks", "added_by"]].rename(columns=str.title),
                     hide_index=True, width="stretch")


# ---------------------------------------------------------------- all entries
def colour_rows(row):
    c = "rgba(46,139,98,.12)" if row["Type"] == "Receive" else "rgba(178,58,72,.12)"
    return [f"background-color:{c}"] * len(row)


with tab_all:
    f1, f2, f3, f4, f5 = st.columns(5)
    d_from = f1.date_input("From", value=None, format="DD/MM/YYYY", key="f_from")
    d_to = f2.date_input("To", value=None, format="DD/MM/YYYY", key="f_to")
    f_head = f3.selectbox("Head", ["All"] + sorted(set(heads()) | set(df["head"])), key="f_head")
    f_type = f4.selectbox("Type", ["All", "Receive", "Pay"], key="f_type")
    f_mode = f5.selectbox("Mode", ["All"] + MODES, key="f_mode")
    v = df.copy()
    if d_from:
        v = v[v["date"] >= d_from.isoformat()]
    if d_to:
        v = v[v["date"] <= d_to.isoformat()]
    if f_head != "All":
        v = v[v["head"] == f_head]
    if f_type != "All":
        v = v[v["type"] == f_type]
    if f_mode != "All":
        v = v[v["mode"] == f_mode]
    v = v.sort_values(["date", "created_at"], ascending=False)
    t_in, t_out = v.loc[v["type"] == "Receive", "amount"].sum(), v.loc[v["type"] == "Pay", "amount"].sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Entries", len(v))
    m2.metric("Money in", inr(t_in))
    m3.metric("Money out", inr(t_out))
    m4.metric("Net", inr(t_in - t_out))
    show = v[["date", "type", "head", "sub_head", "mode", "amount", "remarks", "added_by", "id"]].rename(
        columns={"date": "Date", "type": "Type", "head": "Head", "sub_head": "Sub Head", "mode": "Mode", "amount": "Amount",
                 "remarks": "Remarks", "added_by": "Added By", "id": "ID"})
    st.dataframe(show.style.apply(colour_rows, axis=1).format({"Amount": inr}), hide_index=True, width="stretch")
    st.download_button("Download CSV (Excel)", show.drop(columns="ID").to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"office-ledger-{date.today().isoformat()}.csv", mime="text/csv")

    st.subheader("Edit or delete an entry")
    if v.empty:
        st.caption("No entries to edit.")
    else:
        labels = {r.id: f"{r.date} · {r.type} · {r.head} · {inr(r.amount)} · {r.sub_head or ''}" for r in v.itertuples()}
        pick = st.selectbox("Choose entry", list(labels), format_func=labels.get, key="edit_pick")
        cur = v[v["id"] == pick].iloc[0].to_dict()
        with st.container(border=True):
            upd = entry_form(f"edit{pick}", cur)
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("Update", type="primary"):
                if upd["amount"] <= 0:
                    st.error("Enter an amount greater than 0.")
                else:
                    db().execute("UPDATE entries SET date=?,type=?,head=?,sub_head=?,mode=?,amount=?,remarks=?,edited_by=? WHERE id=?",
                                 (upd["date"], upd["type"], upd["head"], upd["sub_head"], upd["mode"], upd["amount"],
                                  upd["remarks"], USER, pick))
                    st.session_state.flash = "Entry updated"
                    st.rerun()
            confirm = b2.checkbox("Confirm delete", key=f"del_ok_{pick}")
            if b2.button("Delete", disabled=not confirm):
                db().execute("DELETE FROM entries WHERE id=?", (pick,))
                st.session_state.flash = "Entry deleted"
                st.rerun()


# ---------------------------------------------------------------- settings
with tab_set:
    st.subheader("Opening balance")
    with st.form("opening"):
        c1, c2, c3 = st.columns(3)
        od = c1.date_input("Opening date", value=date.fromisoformat(cfg["open_date"]), format="DD/MM/YYYY")
        oc = c2.number_input("Cash in hand (₹)", value=cfg["open_cash"], step=100.0)
        ob = c3.number_input("Bank balance (₹)", value=cfg["open_bank"], step=100.0)
        st.caption("UPI moves money in and out of the bank, so UPI is counted with Bank. Entries before the opening date are not counted.")
        if st.form_submit_button("Save opening balance", type="primary"):
            db().executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                             [("open_date", od.isoformat()), ("open_cash", str(oc)), ("open_bank", str(ob))])
            st.session_state.flash = "Opening balance saved"
            st.rerun()

    st.subheader("Heads & Sub Heads")
    with st.form("add_head", clear_on_submit=True):
        nh = st.text_input("New head", placeholder="e.g. Stationery")
        if st.form_submit_button("Add head") and nh.strip():
            pos = (db().execute("SELECT MAX(position) FROM heads").fetchone()[0] or 0) + 1
            db().execute("INSERT OR IGNORE INTO heads VALUES (?,?)", (nh.strip(), pos))
            st.session_state.flash = f"Head '{nh.strip()}' added"
            st.rerun()
    for h in heads():
        with st.expander(h):
            subs = sub_heads(h)
            st.write(", ".join(subs) if subs else "No sub heads yet.")
            a1, a2 = st.columns([3, 1])
            ns = a1.text_input("Add sub head", key=f"ns_{h}", label_visibility="collapsed", placeholder=f"New sub head for {h}")
            if a2.button("Add", key=f"nsb_{h}") and ns.strip():
                db().execute("INSERT OR IGNORE INTO sub_heads VALUES (?,?)", (h, ns.strip()))
                st.session_state.flash = f"Sub head '{ns.strip()}' added"
                st.rerun()
            if subs:
                r1, r2 = st.columns([3, 1])
                rs = r1.selectbox("Remove sub head", subs, key=f"rs_{h}", label_visibility="collapsed")
                if r2.button("Remove", key=f"rsb_{h}"):
                    db().execute("DELETE FROM sub_heads WHERE head=? AND name=?", (h, rs))
                    st.rerun()
            if st.checkbox(f"Remove head '{h}'", key=f"rh_ok_{h}") and st.button("Confirm remove", key=f"rh_{h}"):
                db().execute("DELETE FROM heads WHERE name=?", (h,))
                st.session_state.flash = f"Head '{h}' removed (old entries are kept)"
                st.rerun()

    st.subheader("Change my password")
    with st.form("pw", clear_on_submit=True):
        p_old = st.text_input("Current password", type="password")
        p_new = st.text_input("New password (min 8 characters)", type="password")
        if st.form_submit_button("Change password"):
            if not check_password(USER, p_old):
                st.error("Current password is wrong.")
            elif len(p_new) < 8:
                st.error("New password must be at least 8 characters.")
            else:
                set_password(db(), USER, p_new)
                st.success("Password changed.")
