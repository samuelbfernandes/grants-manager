#!/usr/bin/env python3
"""Grants Manager - local grant management app.

Zero external dependencies: Python stdlib + SQLite only.
Run:  python3 server.py            (starts server, prints URL)
      python3 server.py --launch   (starts server and opens browser)
If the server is already running, --launch just opens the browser.
"""
import base64
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import threading
import uuid
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

PORT = 8765
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, "app")
DATA_DIR = os.path.join(BASE_DIR, "data")
RECEIPTS_DIR = os.path.join(BASE_DIR, "receipts")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
DB_PATH = os.path.join(DATA_DIR, "grants.db")
BACKUP_RETENTION_DAYS = 30
TRASH_RETENTION_DAYS = 30

# The server listens on every interface so a phone on the same Wi-Fi can reach
# it. That also means anyone else on that network can, so requests arriving
# from off-machine must carry an access key; requests from this computer
# (loopback) never need one. The key lives beside the database, not in it, so
# restoring a backup can't hand someone an old key.
ACCESS_KEY_PATH = os.path.join(DATA_DIR, "access_key.txt")
ACCESS_KEY = None


def _load_key(path):
    """Read a key file, creating one on first run. 160 bits of urandom."""
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(path) as f:
            k = f.read().strip()
        if k:
            return k
    except OSError:
        pass
    k = base64.urlsafe_b64encode(os.urandom(20)).decode().rstrip("=")
    with open(path, "w") as f:
        f.write(k)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return k


def load_access_key():
    """Load the key that guards access from other devices."""
    global ACCESS_KEY
    ACCESS_KEY = _load_key(ACCESS_KEY_PATH)
    return ACCESS_KEY

STANDARD_CATEGORIES = [
    "Personnel", "Fringe", "Tuition", "Travel",
    "Equipment", "Supplies", "Publication", "Other",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  agency TEXT DEFAULT '',
  initial_amount REAL DEFAULT 0,
  start_date TEXT DEFAULT '',
  end_date TEXT DEFAULT '',
  status TEXT DEFAULT 'active',
  notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  grant_id INTEGER,
  sort INTEGER DEFAULT 100
);
CREATE TABLE IF NOT EXISTS budget_lines (
  id INTEGER PRIMARY KEY,
  grant_id INTEGER NOT NULL,
  category_id INTEGER NOT NULL,
  year INTEGER NOT NULL DEFAULT 1,
  amount REAL NOT NULL DEFAULT 0,
  UNIQUE(grant_id, category_id, year)
);
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL,
  grant_id INTEGER NOT NULL,
  monthly_salary REAL NOT NULL DEFAULT 0,
  fringe_rate REAL NOT NULL DEFAULT 0,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY,
  grant_id INTEGER NOT NULL,
  category_id INTEGER,
  year INTEGER DEFAULT 1,
  date TEXT NOT NULL,
  amount REAL NOT NULL,
  description TEXT DEFAULT '',
  person_id INTEGER,
  receipt_path TEXT DEFAULT '',
  source TEXT DEFAULT 'manual',
  appointment_id INTEGER,
  salary_month TEXT
);
CREATE TABLE IF NOT EXISTS workday_map (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  wd_key TEXT NOT NULL,
  target_id INTEGER,
  UNIQUE(kind, wd_key)
);
CREATE TABLE IF NOT EXISTS workday_lines (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT UNIQUE,
  date TEXT,
  budget_date TEXT,
  grant_code TEXT,
  grant_name TEXT,
  award TEXT,
  object_class TEXT,
  spend_category TEXT,
  ledger TEXT,
  worker TEXT,
  supplier TEXT,
  amount REAL,
  txn TEXT,
  status TEXT DEFAULT 'pending',
  expense_id INTEGER,
  imported_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS workday_balances (
  id INTEGER PRIMARY KEY,
  grant_code TEXT,
  grant_name TEXT,
  award TEXT,
  object_class TEXT,
  budget REAL, commitment REAL, obligation REAL, actuals REAL, available REAL,
  as_of TEXT,
  UNIQUE(grant_code, object_class)
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  action TEXT NOT NULL,
  expense_id INTEGER,
  grant_id INTEGER,
  descr TEXT,
  amount REAL,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS trash (
  id INTEGER PRIMARY KEY,
  batch_id TEXT NOT NULL,
  batch_summary TEXT NOT NULL,
  kind TEXT NOT NULL,
  table_name TEXT NOT NULL,
  row_id INTEGER NOT NULL,
  column_name TEXT,
  old_value TEXT,
  row_json TEXT,
  deleted_at TEXT NOT NULL
);
"""


def db():
    # Requests are served on threads, so two writes can collide (e.g. the phone
    # and the Mac saving at once). Wait politely instead of failing instantly.
    # Deliberately NOT using WAL: it adds -wal/-shm side files, which in a
    # OneDrive-synced folder are a sync-conflict hazard rather than a help.
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


MIGRATIONS = [  # (table, column, DDL type/default)
    ("grants", "exclude_from_history", "INTEGER DEFAULT 0"),
    ("grants", "nce_end_date", "TEXT DEFAULT ''"),
    ("appointments", "annual_tuition", "REAL DEFAULT 0"),
    ("appointments", "auto_charge", "INTEGER DEFAULT 0"),
    # share of the person's total salary carried by this appointment (%)
    ("appointments", "pct", "REAL DEFAULT 100"),
    # pay-period date of a Workday journal line (payroll month bucketing)
    ("workday_lines", "budget_date", "TEXT DEFAULT ''"),
    # Workday fast-entry status: '' needs entry, 'sent' entered/waiting to
    # post, 'na' not a Workday expense (cleared automatically once a Workday
    # line links to the expense)
    ("expenses", "wd_entry", "TEXT DEFAULT ''"),
    # explicit worktag typed at entry time — used for expenses (e.g. "Other"
    # external accounts) that have no grant-level Workday mapping to pull one from
    ("expenses", "wd_worktag", "TEXT DEFAULT ''"),
]


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(RECEIPTS_DIR, exist_ok=True)
    conn = db()
    conn.executescript(SCHEMA)
    for table, col, ddl in MIGRATIONS:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # Seed standard categories once.
    n = conn.execute(
        "SELECT COUNT(*) FROM categories WHERE grant_id IS NULL").fetchone()[0]
    if n == 0:
        for i, name in enumerate(STANDARD_CATEGORIES):
            conn.execute(
                "INSERT INTO categories (name, grant_id, sort) VALUES (?, NULL, ?)",
                (name, i))
    conn.commit()
    conn.close()


# --------------------------------------------------------------- backups
#
# One automatic, dated copy of the live database on every app start (at most
# one per calendar day), pruned after BACKUP_RETENTION_DAYS. This is a local
# safety net independent of OneDrive — protects against sync conflicts,
# accidental bulk edits, or a corrupted live file.

def make_backup(force=False):
    """Copy the live db into data/backups/. Returns the path, or None if a
    backup already exists for today and force=False."""
    if not os.path.isfile(DB_PATH):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    today = date.today().isoformat()
    existing = os.listdir(BACKUP_DIR)
    if not force and any(f.startswith(f"grants_{today.replace('-', '')}") for f in existing):
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"grants_{stamp}.db")
    shutil.copyfile(DB_PATH, dest)
    return dest


def prune_backups():
    if not os.path.isdir(BACKUP_DIR):
        return
    cutoff = date.today() - timedelta(days=BACKUP_RETENTION_DAYS)
    for f in os.listdir(BACKUP_DIR):
        m = re.match(r"grants_(\d{8})_\d{6}\.db$", f)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if d < cutoff:
            os.remove(os.path.join(BACKUP_DIR, f))


# ----------------------------------------------------------------- trash
#
# Deletions are captured here before they happen, so any grant/person/
# appointment/expense/category/budget-line delete (including the cascaded
# rows it takes with it) can be restored as one unit. Two kinds of entries:
# 'delete' (a row that was removed — restored via re-INSERT) and 'unlink'
# (a foreign key that was set NULL on a row that itself wasn't deleted, e.g.
# an expense's person_id when the person is removed — restored via UPDATE).

FK_TABLE = {"grant_id": "grants", "category_id": "categories",
            "person_id": "people", "appointment_id": "appointments"}
TRASH_RESTORE_ORDER = ["grants", "categories", "people", "budget_lines",
                       "appointments", "expenses"]


def new_batch_id():
    return uuid.uuid4().hex[:12]


def trash_capture(conn, batch_id, summary, kind, table, row_id,
                  column=None, old_value=None, row=None):
    conn.execute(
        "INSERT INTO trash (batch_id, batch_summary, kind, table_name, "
        "row_id, column_name, old_value, row_json, deleted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (batch_id, summary, kind, table, row_id, column,
         str(old_value) if old_value is not None else None,
         json.dumps(dict(row)) if row is not None else None,
         datetime.now().isoformat(timespec="seconds")))


def trash_restore(conn, batch_id):
    rows = rows_to_list(conn.execute(
        "SELECT * FROM trash WHERE batch_id=? ORDER BY id", (batch_id,)))
    if not rows:
        raise ValueError("Nothing to restore — this was already restored "
                         "or has aged out of the trash.")
    deletes = sorted((r for r in rows if r["kind"] == "delete"),
                     key=lambda r: TRASH_RESTORE_ORDER.index(r["table_name"])
                     if r["table_name"] in TRASH_RESTORE_ORDER else 99)
    unlinks = [r for r in rows if r["kind"] == "unlink"]
    id_remap = {}  # (table, old_id) -> new_id, only set on an id collision

    for r in deletes:
        table = r["table_name"]
        data = json.loads(r["row_json"])
        for col, fk_table in FK_TABLE.items():
            if col in data and data[col] is not None:
                key = (fk_table, data[col])
                if key in id_remap:
                    data[col] = id_remap[key]
        taken = conn.execute(f"SELECT 1 FROM {table} WHERE id=?",
                             (data["id"],)).fetchone()
        if taken:
            old_id = data.pop("id")
            cols = list(data.keys())
            cur = conn.execute(
                f"INSERT INTO {table} ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [data[c] for c in cols])
            id_remap[(table, old_id)] = cur.lastrowid
        else:
            cols = list(data.keys())
            conn.execute(
                f"INSERT INTO {table} ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [data[c] for c in cols])

    for r in unlinks:
        table, col, row_id = r["table_name"], r["column_name"], r["row_id"]
        val = r["old_value"]
        if val is not None:
            key = (FK_TABLE.get(col), int(val))
            if key in id_remap:
                val = id_remap[key]
        conn.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (val, row_id))

    conn.execute("DELETE FROM trash WHERE batch_id=?", (batch_id,))
    conn.commit()


def trash_list(conn):
    cutoff = (datetime.now() - timedelta(days=TRASH_RETENTION_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT batch_id, batch_summary, deleted_at, COUNT(*) AS n "
        "FROM trash WHERE deleted_at>=? GROUP BY batch_id "
        "ORDER BY deleted_at DESC", (cutoff,)).fetchall()
    return [{"batch_id": r["batch_id"], "summary": r["batch_summary"],
             "deleted_at": r["deleted_at"], "rows": r["n"]} for r in rows]


def prune_trash(conn):
    cutoff = (datetime.now() - timedelta(days=TRASH_RETENTION_DAYS)).isoformat()
    conn.execute("DELETE FROM trash WHERE deleted_at<?", (cutoff,))
    conn.commit()


def money_str(v):
    return "${:,.2f}".format(v or 0)


# --------------------------------------------------------------- audit log
#
# Records what a person changed by hand, so the monthly report can show the
# account owner "here is what moved this month" and they can vouch for it.
# Only human edits are logged — Workday imports are the system's own record
# and would just add noise.

AUDIT_WATCH = ("amount", "date", "description", "category_id", "grant_id",
               "person_id", "wd_worktag")


def audit_log(conn, action, expense_id=None, grant_id=None, descr=None,
              amount=None, detail=None):
    conn.execute(
        "INSERT INTO audit (at, action, expense_id, grant_id, descr, amount, "
        "detail) VALUES (?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), action, expense_id,
         grant_id, descr, amount, detail))


def audit_describe_change(conn, before, after):
    """Human-readable 'what changed' for an expense edit, or None if nothing
    a person would care about actually moved."""
    names = {"amount": "amount", "date": "date", "description": "description",
             "category_id": "category", "grant_id": "grant",
             "person_id": "person", "wd_worktag": "worktag"}
    def label(field, val):
        if val in (None, ""):
            return "—"
        if field == "category_id":
            r = conn.execute("SELECT name FROM categories WHERE id=?", (val,)).fetchone()
            return r["name"] if r else str(val)
        if field == "grant_id":
            r = conn.execute("SELECT name FROM grants WHERE id=?", (val,)).fetchone()
            return r["name"] if r else str(val)
        if field == "person_id":
            r = conn.execute("SELECT name FROM people WHERE id=?", (val,)).fetchone()
            return r["name"] if r else str(val)
        if field == "amount":
            return money_str(val)
        return str(val)
    bits = []
    for f in AUDIT_WATCH:
        if f not in after:
            continue
        old, new = before[f], after[f]
        if f == "amount":
            if abs((old or 0) - (new or 0)) < 0.005:
                continue
        elif str(old or "") == str(new or ""):
            continue
        bits.append("%s: %s → %s" % (names[f], label(f, old), label(f, new)))
    return "; ".join(bits) if bits else None


def rows_to_list(rows):
    return [dict(r) for r in rows]


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()


def slugify(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "grant"


def budget_year_for(grant, d):
    """Budget year (1-based) of date d, using the grant's start date anniversary."""
    start = grant.get("start_date") or ""
    if not start:
        return 1
    try:
        s = datetime.strptime(start, "%Y-%m-%d").date()
        x = datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return 1
    if x < s:
        return 1
    years = x.year - s.year
    if (x.month, x.day) < (s.month, s.day):
        years -= 1
    return max(1, years + 1)


# ---------------------------------------------------------------- salary gen

def month_range(start, end):
    """Yield (year, month) covering the inclusive date span."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


def generate_salaries(conn, through=None):
    """Create missing monthly salary + fringe expense rows for all appointments.

    Months are prorated by days covered. Idempotent via (appointment_id,
    salary_month, category). Only generates up to `through` (default: today).
    """
    today = date.today()
    if through:
        try:
            through_d = datetime.strptime(through + "-01", "%Y-%m-%d").date()
            # end of that month
            nxt = (through_d.replace(day=28) + timedelta(days=4)).replace(day=1)
            today = min(nxt - timedelta(days=1), today) if False else nxt - timedelta(days=1)
        except ValueError:
            pass
    cats = {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM categories WHERE grant_id IS NULL")}
    personnel_id, fringe_id = cats.get("Personnel"), cats.get("Fringe")
    created = 0
    apps = rows_to_list(conn.execute(
        "SELECT a.*, p.name AS person_name FROM appointments a "
        "JOIN people p ON p.id = a.person_id WHERE a.auto_charge = 1"))
    for a in apps:
        try:
            a_start = datetime.strptime(a["start_date"], "%Y-%m-%d").date()
            a_end = datetime.strptime(a["end_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        gen_end = min(a_end, today)
        if gen_end < a_start:
            continue
        grant = conn.execute("SELECT * FROM grants WHERE id=?",
                             (a["grant_id"],)).fetchone()
        if not grant:
            continue
        grant = dict(grant)
        for (y, m) in month_range(a_start, gen_end):
            mkey = f"{y:04d}-{m:02d}"
            first = date(y, m, 1)
            nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
            last = nxt - timedelta(days=1)
            cov_start = max(first, a_start)
            cov_end = min(last, gen_end)
            if cov_end < cov_start:
                continue
            frac = (cov_end - cov_start + timedelta(days=1)).days / last.day
            frac = min(1.0, frac)
            sal = round(a["monthly_salary"] * frac, 2)
            fri = round(sal * a["fringe_rate"] / 100.0, 2)
            exp_date = cov_end.isoformat()
            yr = budget_year_for(grant, exp_date)
            for cat_id, amt, label in (
                    (personnel_id, sal, "Salary"),
                    (fringe_id, fri, "Fringe")):
                if amt <= 0:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM expenses WHERE appointment_id=? AND "
                    "salary_month=? AND category_id=?",
                    (a["id"], mkey, cat_id)).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO expenses (grant_id, category_id, year, date, "
                    "amount, description, person_id, source, appointment_id, "
                    "salary_month) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (a["grant_id"], cat_id, yr, exp_date, amt,
                     f"{label} — {a['person_name']} ({mkey})",
                     a["person_id"], "salary", a["id"], mkey))
                created += 1
    conn.commit()
    return created


# ------------------------------------------------------------ Workday sync
#
# One-way: exports of the UARK Workday report "RPT - Grant Budget Vs Actuals"
# (summary + its Actuals drill-down) dropped into ../workday_imports are
# parsed and reconciled against the ledger. Nothing is ever pushed back.

WD_IMPORT_DIR = os.path.join(os.path.dirname(BASE_DIR), "workday_imports")
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def parse_xlsx(path):
    """Minimal stdlib .xlsx reader -> (headers, rows-as-dicts). Sheet 1 only."""
    import zipfile
    import xml.etree.ElementTree as ET
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
            shared.append("".join(t.text or "" for t in si.iter(_XLSX_NS + "t")))
    root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    grid = []
    for row in root.iter(_XLSX_NS + "row"):
        cells = {}
        for c in row.iter(_XLSX_NS + "c"):
            ref = c.get("r") or ""
            col = re.match(r"[A-Z]+", ref)
            if not col:
                continue
            # column letters -> 0-based index
            idx = 0
            for ch in col.group(0):
                idx = idx * 26 + (ord(ch) - 64)
            idx -= 1
            t = c.get("t")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(_XLSX_NS + "t"))
            else:
                v = c.find(_XLSX_NS + "v")
                val = v.text if v is not None else ""
                if t == "s" and val != "":
                    val = shared[int(val)]
            cells[idx] = val
        if cells:
            width = max(cells) + 1
            grid.append([cells.get(i, "") for i in range(width)])
    if not grid:
        return [], []
    headers = [str(h).strip() for h in grid[0]]
    rows = []
    for raw in grid[1:]:
        d = {headers[i]: raw[i] if i < len(raw) else ""
             for i in range(len(headers)) if headers[i]}
        rows.append(d)
    return headers, rows


def excel_date(v):
    """Excel serial, ISO-ish, or MM/DD/YYYY string -> YYYY-MM-DD ('' if bad)."""
    s = str(v).strip()
    if not s:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))
    try:
        serial = int(float(s))
        return (date(1899, 12, 30) + timedelta(days=serial)).isoformat()
    except (ValueError, OverflowError):
        return ""


def _wd_num(v):
    """'$1,500.00', '(102.00)', 1500.0 -> float (0.0 if unparseable)."""
    s = str(v).replace(",", "").replace("$", "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s or 0)
    except ValueError:
        return 0.0


def _wd_grant_code(grant_str):
    m = re.match(r"\s*(GR\w+)", str(grant_str))
    return m.group(1) if m else str(grant_str).strip()


def _wd_worktag(worktags, label):
    m = re.search(r"^%s:\s*(.+)$" % re.escape(label), str(worktags), re.M)
    return m.group(1).strip() if m else ""


# object-class keyword -> standard category name (auto-seeded, user-overridable)
WD_CAT_HINTS = [
    ("fringe", "Fringe"), ("personnel", "Personnel"), ("salar", "Personnel"),
    ("tuition", "Tuition"), ("foreign travel", "Foreign Travel"),
    ("travel", "Travel"), ("equipment", "Equipment"), ("suppl", "Supplies"),
    ("materials", "Supplies"), ("public", "Publication"),
    ("indirect", "Indirect (F&A)"), ("f&a", "Indirect (F&A)"),
]


def _wd_guess_category(conn, object_class):
    low = str(object_class).lower()
    names = {r["name"].lower(): r["id"] for r in conn.execute(
        "SELECT id, name FROM categories WHERE grant_id IS NULL")}
    for key, cat in WD_CAT_HINTS:
        if key in low and cat.lower() in names:
            return names[cat.lower()]
    return None


def _wd_category_on_grant(conn, cat_id, grant_id):
    """Remap a category to one usable on this grant (standard passes through,
    other grants' custom categories map by name, else standard Other)."""
    c = conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
    if not c or c["grant_id"] is None or c["grant_id"] == grant_id:
        return cat_id
    same = conn.execute(
        "SELECT id FROM categories WHERE name=? AND (grant_id=? OR grant_id IS NULL)",
        (c["name"], grant_id)).fetchone()
    if same:
        return same["id"]
    other = conn.execute(
        "SELECT id FROM categories WHERE name='Other' AND grant_id IS NULL").fetchone()
    return other["id"] if other else None


def wd_ingest_file(conn, path):
    """Parse one .xlsx export; returns ('detail'|'summary'|'unknown', rows)."""
    headers, rows = parse_xlsx(path)
    return wd_ingest_rows(conn, headers, rows)


def wd_ingest_csv(conn, text):
    """Parse a RaaS CSV response; returns ('detail'|'summary'|'unknown', rows)."""
    import csv
    import io
    rdr = list(csv.reader(io.StringIO(text)))
    if not rdr:
        return "unknown", 0
    headers = [h.strip() for h in rdr[0]]
    rows = [{headers[i]: (r[i] if i < len(r) else "")
             for i in range(len(headers)) if headers[i]} for r in rdr[1:]]
    return wd_ingest_rows(conn, headers, rows)


def wd_ingest_rows(conn, headers, rows):
    """Ingest parsed report rows; returns ('detail'|'summary'|'unknown', count)."""
    import hashlib
    hset = set(headers)
    if {"Accounting Date", "Transaction Amount"} <= hset:
        seen, new = {}, 0
        for r in rows:
            d = excel_date(r.get("Accounting Date"))
            bd = excel_date(r.get("Budget Date"))
            grant_full = str(r.get("Grant", "")).strip()
            amount = _wd_num(r.get("Transaction Amount"))
            if not grant_full or not d:
                continue
            worktags = r.get("Worktags", "")
            key = "|".join([d, bd, _wd_grant_code(grant_full),
                            str(r.get("Ledger Account", "")).strip(),
                            "%.2f" % amount,
                            str(r.get("Operational Transaction", "")).strip(),
                            str(r.get("Worker", "")).strip(),
                            str(r.get("Supplier", "")).strip()])
            seen[key] = seen.get(key, 0) + 1  # identical lines are legitimate
            fp = hashlib.sha1(("%s#%d" % (key, seen[key])).encode()).hexdigest()
            cur = conn.execute(
                "INSERT OR IGNORE INTO workday_lines (fingerprint, date, "
                "budget_date, grant_code, grant_name, award, object_class, "
                "spend_category, ledger, worker, supplier, amount, txn, "
                "status, imported_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                (fp, d, bd, _wd_grant_code(grant_full), grant_full,
                 str(r.get("Award", "")).strip(),
                 str(r.get("Object Class", "")).strip()
                 or _wd_worktag(worktags, "Object Class"),
                 str(r.get("Spend Category", "")).strip()
                 or _wd_worktag(worktags, "Spend Category"),
                 str(r.get("Ledger Account", "")).strip(),
                 str(r.get("Worker", "")).strip(),
                 str(r.get("Supplier", "")).strip(),
                 amount, str(r.get("Operational Transaction", "")).strip(),
                 datetime.now().isoformat(timespec="seconds")))
            new += cur.rowcount
        return "detail", new
    if {"Object Class", "Budget", "Available Balance"} <= hset:
        n = 0
        for r in rows:
            grant_full = str(r.get("Grant", "")).strip()
            if not grant_full:  # the Total row
                continue
            def num(k):
                return _wd_num(r.get(k))
            conn.execute(
                "INSERT INTO workday_balances (grant_code, grant_name, award, "
                "object_class, budget, commitment, obligation, actuals, "
                "available, as_of) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(grant_code, object_class) DO UPDATE SET "
                "grant_name=excluded.grant_name, award=excluded.award, "
                "budget=excluded.budget, commitment=excluded.commitment, "
                "obligation=excluded.obligation, actuals=excluded.actuals, "
                "available=excluded.available, as_of=excluded.as_of",
                (_wd_grant_code(grant_full), grant_full,
                 str(r.get("Award", "")).strip(),
                 str(r.get("Object Class", "")).strip(),
                 num("Budget"), num("Commitment"), num("Obligation"),
                 num("Actuals"), num("Available Balance"),
                 date.today().isoformat()))
            n += 1
        return "summary", n
    return "unknown", 0


def wd_seed_category_maps(conn):
    """Auto-map object classes seen in imports to standard categories."""
    keys = {r[0] for r in conn.execute(
        "SELECT DISTINCT object_class FROM workday_lines WHERE object_class!='' "
        "UNION SELECT DISTINCT object_class FROM workday_balances "
        "WHERE object_class!=''")}
    mapped = {r["wd_key"] for r in conn.execute(
        "SELECT wd_key FROM workday_map WHERE kind='category'")}
    for k in keys - mapped:
        guess = _wd_guess_category(conn, k)
        if guess:
            conn.execute("INSERT OR IGNORE INTO workday_map (kind, wd_key, "
                         "target_id) VALUES ('category', ?, ?)", (k, guess))


def wd_match(conn):
    """Reconcile pending Workday lines with the ledger.

    Payroll lines (worker + Personnel/Fringe) replace the app's projected
    salary charges for that month; other lines link to a manual expense with
    the same grant + amount within 45 days, or become new 'workday' expenses.
    Lines whose grant/category is not yet mapped stay pending.
    """
    gmap = {r["wd_key"]: r["target_id"] for r in conn.execute(
        "SELECT wd_key, target_id FROM workday_map WHERE kind='grant'")}
    cmap = {r["wd_key"]: r["target_id"] for r in conn.execute(
        "SELECT wd_key, target_id FROM workday_map WHERE kind='category'")}
    cat_names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM categories")}
    people = {r["name"].strip().lower(): r["id"] for r in conn.execute(
        "SELECT id, name FROM people")}
    linked = {r[0] for r in conn.execute(
        "SELECT expense_id FROM workday_lines WHERE expense_id IS NOT NULL")}
    out = {"matched": 0, "created": 0, "ignored": 0, "pending": 0}
    for ln in rows_to_list(conn.execute(
            "SELECT * FROM workday_lines WHERE status='pending' ORDER BY date, id")):
        if ln["grant_code"] not in gmap or ln["object_class"] not in cmap:
            out["pending"] += 1
            continue
        gid, cid = gmap[ln["grant_code"]], cmap[ln["object_class"]]
        if gid is None or cid is None:  # explicitly ignored key
            conn.execute("UPDATE workday_lines SET status='ignored' WHERE id=?",
                         (ln["id"],))
            out["ignored"] += 1
            continue
        grant = conn.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
        if not grant:
            out["pending"] += 1
            continue
        grant = dict(grant)
        cid = _wd_category_on_grant(conn, cid, gid)
        worker = (ln["worker"] or "").strip()
        person_id = people.get(worker.lower())
        # payroll buckets by pay period (Budget Date): adjustments often post
        # months after the month they pay for
        month = ((ln["budget_date"] or ln["date"]))[:7]
        is_payroll = bool(worker) and cat_names.get(cid) in ("Personnel", "Fringe")
        if is_payroll:
            # projected charge for this person/grant/month -> replace with actual;
            # further same-month lines (semi-monthly pay) accumulate onto it
            pd = ln["budget_date"] or ln["date"]  # date within the pay month
            ex = conn.execute(
                "SELECT * FROM expenses WHERE grant_id=? AND category_id=? AND "
                "salary_month=? AND person_id IS ? AND source IN ('salary','workday') "
                "ORDER BY source='workday' DESC",
                (gid, cid, month, person_id)).fetchone()
            label = cat_names.get(cid, "Salary")
            if ex and ex["source"] == "salary":
                conn.execute(
                    "UPDATE expenses SET amount=?, date=?, source='workday', "
                    "description=? WHERE id=?",
                    (ln["amount"], pd,
                     "%s — %s (%s)" % (label, worker, month),
                     ex["id"]))
                eid, st = ex["id"], "matched"
            elif ex:  # already actualized this month: add this pay line
                conn.execute(
                    "UPDATE expenses SET amount=ROUND(amount+?,2), date=MAX(date,?) "
                    "WHERE id=?", (ln["amount"], pd, ex["id"]))
                eid, st = ex["id"], "matched"
            else:
                cur = conn.execute(
                    "INSERT INTO expenses (grant_id, category_id, year, date, "
                    "amount, description, person_id, source, salary_month) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (gid, cid, budget_year_for(grant, pd), pd,
                     ln["amount"],
                     "%s — %s (%s)" % (label, worker, month),
                     person_id, "workday", month))
                eid, st = cur.lastrowid, "created"
        else:
            match = None
            for ex in conn.execute(
                    "SELECT * FROM expenses WHERE grant_id=? AND source='manual' "
                    "AND ABS(amount-?)<0.01 AND ABS(julianday(date)-julianday(?))<=45 "
                    "ORDER BY category_id IS ? DESC, "
                    "ABS(julianday(date)-julianday(?))",
                    (gid, ln["amount"], ln["date"], cid, ln["date"])):
                if ex["id"] not in linked:
                    match = ex
                    break
            if match:
                eid, st = match["id"], "matched"
            else:
                desc = ln["txn"] or ln["spend_category"] or "Posted charge"
                if ln["supplier"]:
                    desc += " — " + ln["supplier"]
                cur = conn.execute(
                    "INSERT INTO expenses (grant_id, category_id, year, date, "
                    "amount, description, person_id, source) VALUES (?,?,?,?,?,?,?,?)",
                    (gid, cid, budget_year_for(grant, ln["date"]), ln["date"],
                     ln["amount"], desc, person_id, "workday"))
                eid, st = cur.lastrowid, "created"
        linked.add(eid)
        conn.execute("UPDATE workday_lines SET status=?, expense_id=? WHERE id=?",
                     ("matched" if st == "matched" else "imported", eid, ln["id"]))
        out["matched" if st == "matched" else "created"] += 1
    conn.commit()
    return out


def wd_save_uploaded(payload):
    """One uploaded {name, data(base64)} -> saved into workday_imports/,
    named uniquely so repeat uploads (often all sharing Workday's generic
    export filename) never clobber each other."""
    fname = os.path.basename(payload.get("name") or "import.xlsx")
    fname = re.sub(r"[^A-Za-z0-9._ -]+", "_", fname)
    if not fname.lower().endswith(".xlsx"):
        fname += ".xlsx"
    raw = base64.b64decode(payload["data"])
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("%s is too large (max 20 MB) — is it really an "
                         "Excel export?" % fname)
    os.makedirs(WD_IMPORT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(WD_IMPORT_DIR, f"{stamp}_{fname}")
    with open(path, "wb") as f:
        f.write(raw)
    return path


def wd_import(conn):
    """Scan the workday_imports folder, ingest every .xlsx, reconcile."""
    os.makedirs(WD_IMPORT_DIR, exist_ok=True)
    files, new_lines, balance_rows = [], 0, 0
    for name in sorted(os.listdir(WD_IMPORT_DIR)):
        if not name.lower().endswith(".xlsx") or name.startswith("~"):
            continue
        path = os.path.join(WD_IMPORT_DIR, name)
        try:
            kind, n = wd_ingest_file(conn, path)
        except Exception as e:  # noqa: BLE001 — one bad file shouldn't stop the rest
            files.append({"file": name, "kind": "error", "rows": 0, "error": str(e)})
            continue
        files.append({"file": name, "kind": kind, "rows": n})
        if kind == "detail":
            new_lines += n
        elif kind == "summary":
            balance_rows += n
    wd_seed_category_maps(conn)
    conn.commit()
    res = wd_match(conn)
    res.update({"files": files, "new_lines": new_lines,
                "balance_rows": balance_rows})
    return res


# RaaS (Report-as-a-Service): pull the same two reports straight from Workday
# over HTTPS. The password is kept ONLY in this dict (process memory) — never
# written to the database or any file; the user re-types it once per session.
WD_SESSION = {"password": None}


class WDAuthError(RuntimeError):
    pass


def wd_fetch_raas(conn, password):
    """Fetch the configured RaaS URLs as CSV, ingest, reconcile."""
    import urllib.error
    import urllib.request
    cfg = get_setting(conn, "workday_raas", {}) or {}
    user = (cfg.get("username") or "").strip()
    if not user:
        raise RuntimeError("No Workday username saved — fill in the "
                           "Direct connection card first.")
    pairs = [("summary", cfg.get("summary_url")),
             ("detail", cfg.get("detail_url"))]
    if not any(u for _, u in pairs):
        raise RuntimeError("No RaaS URLs saved — fill in the "
                           "Direct connection card first.")
    cred = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    files = []
    for label, url in pairs:
        url = (url or "").strip()
        if not url:
            continue
        if "format=" not in url:
            url += ("&" if "?" in url else "?") + "format=csv"
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Basic " + cred)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise WDAuthError(
                    "Workday rejected the login (HTTP %d) on the %s report. "
                    "Check the password; if it keeps failing, UARK may only "
                    "allow SSO logins for your account, which blocks RaaS."
                    % (e.code, label))
            raise RuntimeError("Workday returned HTTP %d for the %s report."
                               % (e.code, label))
        except urllib.error.URLError as e:
            raise RuntimeError("Could not reach Workday (%s). VPN/network?"
                               % getattr(e, "reason", e))
        text = body.decode("utf-8-sig", "replace")
        if "html" in ctype.lower() or text.lstrip()[:1] == "<":
            raise WDAuthError(
                "Workday answered with a login page instead of the %s report "
                "— the account is probably SSO-only, so RaaS basic "
                "authentication is blocked." % label)
        kind, n = wd_ingest_csv(conn, text)
        files.append({"file": "RaaS %s" % label, "kind": kind, "rows": n})
        if kind == "unknown":
            files[-1]["error"] = ("Response didn't look like the expected "
                                  "report (missing the standard columns).")
    wd_seed_category_maps(conn)
    conn.commit()
    res = wd_match(conn)
    res["files"] = files
    res["new_lines"] = sum(f["rows"] for f in files if f["kind"] == "detail")
    res["balance_rows"] = sum(f["rows"] for f in files if f["kind"] == "summary")
    set_setting(conn, "workday_last_sync", {
        "date": date.today().isoformat(),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "summary": "%d new transactions · %d matched · %d added" % (
            res["new_lines"], res["matched"], res["created"]),
    })
    return res


def wd_push_state(conn):
    """Everything the 'push to Workday' fast-entry queue needs.

    Queue = manual expenses from the last 90 days that no imported Workday
    line has linked yet (i.e. not visible as posted) and not marked 'na'.
    """
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    linked = {r[0] for r in conn.execute(
        "SELECT expense_id FROM workday_lines WHERE expense_id IS NOT NULL")}
    queue = [e for e in rows_to_list(conn.execute(
        "SELECT e.id, e.date, e.amount, e.description, e.grant_id, "
        "e.category_id, e.receipt_path, e.wd_entry, e.wd_worktag, "
        "c.name AS category, p.name AS person, g.name AS grant_name "
        "FROM expenses e "
        "LEFT JOIN categories c ON c.id=e.category_id "
        "LEFT JOIN people p ON p.id=e.person_id "
        "JOIN grants g ON g.id=e.grant_id "
        "WHERE e.source='manual' AND e.date>=? AND (e.wd_entry IS NULL OR "
        "e.wd_entry!='na') ORDER BY e.date DESC", (cutoff,)))
        if e["id"] not in linked]
    # Workday codes per app grant (learned from imports via the mapping)
    codes = {}
    for m in conn.execute("SELECT wd_key, target_id FROM workday_map "
                          "WHERE kind='grant' AND target_id IS NOT NULL"):
        r = conn.execute(
            "SELECT grant_name, award FROM workday_lines WHERE grant_code=? "
            "UNION SELECT grant_name, award FROM workday_balances "
            "WHERE grant_code=? LIMIT 1", (m["wd_key"], m["wd_key"])).fetchone()
        codes[m["target_id"]] = {"grant_code": m["wd_key"],
                                 "wd_grant_name": r["grant_name"] if r else "",
                                 "award": r["award"] if r else ""}
    # most common Workday spend category per app category (from imports)
    cmap = {r["wd_key"]: r["target_id"] for r in conn.execute(
        "SELECT wd_key, target_id FROM workday_map WHERE kind='category'")}
    spend, seen = {}, {}
    for r in conn.execute(
            "SELECT object_class, spend_category, COUNT(*) AS n FROM "
            "workday_lines WHERE spend_category!='' "
            "GROUP BY object_class, spend_category ORDER BY n"):
        cid = cmap.get(r["object_class"])
        if cid is not None and r["n"] >= seen.get(cid, 0):
            seen[cid], spend[cid] = r["n"], r["spend_category"]
    return {"queue": queue, "codes": codes, "spend_suggest": spend,
            "profiles": get_setting(conn, "workday_worktags", {}) or {}}


def wd_send_mail(to, cc, subject, body, attachment=None, html=None):
    """Compose and send through Microsoft Outlook — macOS (AppleScript) or
    Windows (Outlook COM via PowerShell). Outlook lets us attach the receipt
    PDF, which a mailto: link cannot. First use on a Mac asks macOS for
    permission to control Outlook.

    `attachment` may be a single path or a list of paths. `html`, when given,
    is sent as the message body instead of `body` (which stays the plain-text
    fallback) — the monthly report needs a real table an accountant can read.
    """
    import subprocess
    attachments = ([attachment] if isinstance(attachment, str)
                   else list(attachment or []))
    attachments = [a for a in attachments if a and os.path.isfile(a)]
    if sys.platform == "darwin":
        def q(s):  # AppleScript string literal escaping
            return str(s).replace("\\", "\\\\").replace('"', '\\"')
        content = ('content:"%s"' % q(html) if html
                   else 'plain text content:"%s"' % q(body))
        lines = [
            'tell application "Microsoft Outlook"',
            'set msg to make new outgoing message with properties '
            '{subject:"%s", %s}' % (q(subject), content),
            'make new recipient at msg with properties '
            '{email address:{address:"%s"}}' % q(to),
        ]
        if cc:
            lines.append('make new cc recipient at msg with properties '
                         '{email address:{address:"%s"}}' % q(cc))
        for a in attachments:
            lines.append('make new attachment at msg with properties '
                         '{file:POSIX file "%s"}' % q(a))
        lines += ['send msg', 'end tell']
        args = ["osascript"]
        for ln in lines:
            args += ["-e", ln]
        hint = ("Check that Microsoft Outlook is installed and signed in, and "
                "that this app is allowed to control it (System Settings → "
                "Privacy & Security → Automation). If you use “New Outlook”, "
                "enable AppleScript support or use “Copy as text” instead.")
    elif sys.platform == "win32":
        def q(s):  # PowerShell single-quoted literal escaping
            return str(s).replace("'", "''")
        ps = ["$o = New-Object -ComObject Outlook.Application",
              "$m = $o.CreateItem(0)",
              "$m.To = '%s'" % q(to)]
        if cc:
            ps.append("$m.CC = '%s'" % q(cc))
        ps.append("$m.Subject = '%s'" % q(subject))
        if html:
            ps.append("$m.HTMLBody = '%s'" % q(html.replace("\r", "")))
        else:
            # single-quoted PS strings keep literal newlines as-is
            ps.append("$m.Body = '%s'" % q(body.replace("\r", "")))
        for a in attachments:
            ps.append("$null = $m.Attachments.Add('%s')" % q(a))
        ps.append("$m.Send()")
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "; ".join(ps)]
        hint = "Check that Microsoft Outlook (desktop) is installed and signed in."
    else:
        raise RuntimeError("Sending needs Microsoft Outlook on macOS or Windows.")
    try:
        r = subprocess.run(args, capture_output=True, timeout=90)
    except FileNotFoundError:
        raise RuntimeError("Could not talk to Outlook on this system. " + hint)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Outlook did not respond. " + hint)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError("Outlook could not send: %s. %s"
                           % (err or "unknown error", hint))


# ------------------------------------------------------- monthly report
#
# Goes to the ACCOUNT OWNER, not the accountant: the owner checks it and
# forwards it on. Columns are named the way Workday names them so the
# accountant can key them in (or import the attached CSV) without translating.

REPORT_COLUMNS = ["Date", "Amount", "Spend Category", "Business Purpose",
                  "Grant / Worktag", "Award", "Cost Center", "Fund",
                  "Person", "Receipt", "Entered"]


def month_bounds(month):
    """'YYYY-MM' -> (first_day, last_day) as ISO strings."""
    # validated explicitly: `month` reaches a filename in report_csv_path(),
    # so anything path-shaped must never get that far
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(month or "")):
        raise ValueError("Month must look like 2026-07")
    y, mo = int(month[:4]), int(month[5:7])
    first = date(y, mo, 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def report_rows(conn, month):
    """Every expense dated inside `month`, shaped for an accountant."""
    start, end = month_bounds(month)
    ps = wd_push_state(conn)
    out = []
    for e in conn.execute(
            "SELECT e.*, c.name AS category, p.name AS person, g.name AS grant_name "
            "FROM expenses e LEFT JOIN categories c ON c.id=e.category_id "
            "LEFT JOIN people p ON p.id=e.person_id "
            "JOIN grants g ON g.id=e.grant_id "
            "WHERE e.date>=? AND e.date<=? ORDER BY g.name, e.date",
            (start, end)):
        code = ps["codes"].get(e["grant_id"], {})
        prof = ps["profiles"].get(str(e["grant_id"]), {})
        out.append({
            "Date": e["date"],
            "Amount": "%.2f" % e["amount"],
            "Spend Category": ps["spend_suggest"].get(e["category_id"],
                                                      e["category"] or ""),
            "Business Purpose": e["description"] or "",
            "Grant / Worktag": (e["wd_worktag"] or code.get("wd_grant_name")
                                or code.get("grant_code") or e["grant_name"]),
            "Award": code.get("award", ""),
            "Cost Center": prof.get("cost_center", ""),
            "Fund": prof.get("fund", ""),
            "Person": e["person"] or "",
            "Receipt": os.path.basename(e["receipt_path"]) if e["receipt_path"] else "",
            "Entered": "by hand" if e["source"] == "manual" else "from Workday",
            "_amount": e["amount"],
            "_grant": e["grant_name"],
            "_receipt_path": e["receipt_path"] or "",
        })
    return out


def report_changes(conn, month):
    start, end = month_bounds(month)
    return rows_to_list(conn.execute(
        "SELECT * FROM audit WHERE at>=? AND at<=? ORDER BY at",
        (start + "T00:00:00", end + "T23:59:59")))


def report_data(conn, month):
    rows = report_rows(conn, month)
    changes = report_changes(conn, month)
    by_grant = {}
    for r in rows:
        by_grant.setdefault(r["_grant"], {"n": 0, "total": 0.0})
        by_grant[r["_grant"]]["n"] += 1
        by_grant[r["_grant"]]["total"] += r["_amount"]
    return {
        "month": month,
        "label": datetime.strptime(month + "-01", "%Y-%m-%d").strftime("%B %Y"),
        "rows": rows,
        "changes": changes,
        "total": sum(r["_amount"] for r in rows),
        "by_grant": by_grant,
        "missing_receipts": sum(1 for r in rows
                                if not r["Receipt"] and r["Entered"] == "by hand"),
    }


def csv_safe(v):
    """Stop a spreadsheet treating a cell as a formula.

    Excel/Sheets execute a cell starting with = + - @ (and will skip leading
    tabs/CRs to find one). Expense descriptions are free text, so a pasted or
    imported value could otherwise run when the accountant opens the file.
    """
    s = "" if v is None else str(v)
    if s and s.lstrip("\t\r ")[:1] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def report_csv_path(data):
    """CSV in the same column order as the table — for Workday import."""
    import csv
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, "expenses_%s.csv" % data["month"])
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in data["rows"]:
            # neutralise anything a spreadsheet would run as a formula
            w.writerow({k: (r[k] if isinstance(r.get(k), (int, float))
                            else csv_safe(r.get(k, "")))
                        for k in REPORT_COLUMNS})
    return path


def report_receipts_zip(data):
    """One zip of the month's receipts, so the accountant gets them together."""
    import zipfile
    paths = [r["_receipt_path"] for r in data["rows"] if r["_receipt_path"]]
    if not paths:
        return None
    os.makedirs(REPORTS_DIR, exist_ok=True)
    out = os.path.join(REPORTS_DIR, "receipts_%s.zip" % data["month"])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in paths:
            full = os.path.join(RECEIPTS_DIR, rel)
            if os.path.isfile(full):
                z.write(full, os.path.basename(rel))
    return out


def report_html(data, owner=""):
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;"))
    th = "".join('<th style="text-align:left;padding:7px 9px;border-bottom:2px solid #444;'
                 'font-size:12px;text-transform:uppercase;letter-spacing:.04em;'
                 'white-space:nowrap">%s</th>' % esc(c) for c in REPORT_COLUMNS)
    trs = []
    for i, r in enumerate(data["rows"]):
        bg = "#ffffff" if i % 2 == 0 else "#f7f8fa"
        tds = "".join(
            '<td style="padding:6px 9px;border-bottom:1px solid #e6e8ec;'
            'font-size:13px;%s">%s</td>'
            % ("text-align:right;white-space:nowrap" if c == "Amount" else "",
               esc(r[c]))
            for c in REPORT_COLUMNS)
        trs.append('<tr style="background:%s">%s</tr>' % (bg, tds))
    grant_rows = "".join(
        '<tr><td style="padding:5px 9px;font-size:13px">%s</td>'
        '<td style="padding:5px 9px;font-size:13px;text-align:right">%d</td>'
        '<td style="padding:5px 9px;font-size:13px;text-align:right;'
        'white-space:nowrap"><strong>%s</strong></td></tr>'
        % (esc(g), v["n"], money_str(v["total"]))
        for g, v in sorted(data["by_grant"].items()))
    changes = ""
    if data["changes"]:
        rows = "".join(
            '<tr><td style="padding:5px 9px;font-size:12.5px;white-space:nowrap">%s</td>'
            '<td style="padding:5px 9px;font-size:12.5px">%s</td>'
            '<td style="padding:5px 9px;font-size:12.5px">%s</td></tr>'
            % (esc(c["at"].replace("T", " ")[:16]), esc(c["action"]),
               esc("%s%s" % (c["descr"] or "",
                             " — " + c["detail"] if c["detail"] else "")))
            for c in data["changes"])
        changes = (
            '<h3 style="font-size:15px;margin:26px 0 8px">Changes made in the app '
            'this month</h3>'
            '<p style="font-size:13px;color:#555;margin:0 0 8px">Entries added, '
            'edited or removed by hand — so you can confirm the list above '
            'reflects what actually happened.</p>'
            '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;'
            'width:100%%;border:1px solid #e6e8ec">%s</table>' % rows)
    warn = ""
    if data["missing_receipts"]:
        warn = ('<p style="background:#fbf1de;border-left:3px solid #8a5a10;'
                'padding:10px 14px;font-size:13px;margin:0 0 18px">'
                '<strong>%d hand-entered expense(s) have no receipt attached.</strong> '
                'Worth checking before this goes to the accountant.</p>'
                % data["missing_receipts"])
    return """<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#16202e;max-width:1000px">
<h2 style="margin:0 0 4px;font-size:20px">Grant expenses — %s</h2>
<p style="margin:0 0 18px;color:#555;font-size:13.5px">%d expense(s) · total <strong>%s</strong>%s</p>
%s
<p style="font-size:13.5px;margin:0 0 16px">Please check the table below. Once it looks right, forward this email to your accountant — the attached CSV has the same rows in Workday's column order, ready to key in or import.</p>
<h3 style="font-size:15px;margin:0 0 8px">By grant</h3>
<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:0 0 22px;border:1px solid #e6e8ec">%s</table>
<h3 style="font-size:15px;margin:0 0 8px">All expenses</h3>
<div style="overflow-x:auto"><table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%%;border:1px solid #e6e8ec"><thead><tr style="background:#eef1f5">%s</tr></thead><tbody>%s</tbody></table></div>
%s
<p style="font-size:12px;color:#777;margin:26px 0 0;border-top:1px solid #e6e8ec;padding-top:12px">
Generated by Grants Manager on %s%s. Amounts are as recorded in the app; Workday remains the system of record.</p>
</div>""" % (
        esc(data["label"]), len(data["rows"]), money_str(data["total"]),
        (" · %d receipt(s) attached" % sum(1 for r in data["rows"] if r["Receipt"]))
        if any(r["Receipt"] for r in data["rows"]) else "",
        warn, grant_rows, th, "".join(trs), changes,
        date.today().isoformat(),
        " for " + esc(owner) if owner else "")


def report_text(data):
    lines = ["Grant expenses — %s" % data["label"], "",
             "%d expense(s), total %s" % (len(data["rows"]),
                                          money_str(data["total"])), ""]
    for g, v in sorted(data["by_grant"].items()):
        lines.append("  %-34s %3d   %s" % (g[:34], v["n"], money_str(v["total"])))
    lines += ["", "Full details are in the attached CSV.", ""]
    return "\n".join(lines)


def wd_state(conn):
    mappings = rows_to_list(conn.execute("SELECT * FROM workday_map ORDER BY kind, wd_key"))
    mapped_g = {m["wd_key"] for m in mappings if m["kind"] == "grant"}
    mapped_c = {m["wd_key"] for m in mappings if m["kind"] == "category"}
    known = rows_to_list(conn.execute(
        "SELECT grant_code, grant_name FROM workday_lines WHERE grant_code!='' "
        "UNION SELECT grant_code, grant_name FROM workday_balances"))
    unmapped_grants = [k for k in known if k["grant_code"] not in mapped_g]
    ocs = [r[0] for r in conn.execute(
        "SELECT DISTINCT object_class FROM workday_lines WHERE object_class!='' "
        "UNION SELECT DISTINCT object_class FROM workday_balances "
        "WHERE object_class!=''")]
    unmapped_cats = [k for k in ocs if k not in mapped_c]
    return {
        "import_dir": WD_IMPORT_DIR,
        "raas": get_setting(conn, "workday_raas", {}) or {},
        "push_cfg": get_setting(conn, "workday_push", {}) or {},
        "reports_sent": get_setting(conn, "reports_sent", {}) or {},
        "last_sync": get_setting(conn, "workday_last_sync"),
        "session_unlocked": bool(WD_SESSION["password"]),
        "push": wd_push_state(conn),
        "mappings": mappings,
        "unmapped_grants": unmapped_grants,
        "unmapped_categories": unmapped_cats,
        "balances": rows_to_list(conn.execute(
            "SELECT * FROM workday_balances ORDER BY grant_code, object_class")),
        "lines": rows_to_list(conn.execute(
            "SELECT * FROM workday_lines ORDER BY date DESC, id DESC LIMIT 300")),
        "counts": dict(conn.execute(
            "SELECT status, COUNT(*) FROM workday_lines GROUP BY status").fetchall()),
    }


# ---------------------------------------------------------------- API state

APP_VERSION = "1.2.0"
UPDATE_REPO = "samuelbfernandes/grants-manager"
UPDATE_API = "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO
UPDATE_CACHE_PATH = os.path.join(DATA_DIR, "update_check.json")
UPDATE_INTERVAL_DAYS = 15


def _version_tuple(v):
    """'v1.2.3' -> (1, 2, 3). Unparseable parts sort as 0."""
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def read_update_cache():
    try:
        with open(UPDATE_CACHE_PATH) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def check_for_update(force=False):
    """Ask GitHub for the latest release, at most once every 15 days.

    Entirely best-effort: with no internet this does nothing at all — it does
    not record the attempt, so the next launch simply tries again, and the app
    never blocks, warns, or errors because of it.
    """
    cache = read_update_cache()
    if not force and cache.get("checked"):
        try:
            age = (date.today() - date.fromisoformat(cache["checked"])).days
            if 0 <= age < UPDATE_INTERVAL_DAYS:
                return cache
        except ValueError:
            pass
    import urllib.request
    try:
        req = urllib.request.Request(UPDATE_API, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "GrantsManager/%s" % APP_VERSION})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read(1_000_000).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — offline, DNS, rate limit, anything
        return cache
    if not isinstance(data, dict):
        return cache
    fresh = {
        "checked": date.today().isoformat(),
        "latest": str(data.get("tag_name") or "").lstrip("vV"),
        "name": str(data.get("name") or "")[:200],
        "notes": str(data.get("body") or "")[:4000],
        "url": "",
        "published": str(data.get("published_at") or "")[:10],
    }
    # Only trust a link that actually points at this project's releases.
    link = str(data.get("html_url") or "")
    if link.startswith("https://github.com/%s/releases/" % UPDATE_REPO):
        fresh["url"] = link
    try:
        with open(UPDATE_CACHE_PATH, "w") as f:
            json.dump(fresh, f)
    except OSError:
        pass
    return fresh


def update_info():
    """What the UI needs: only flagged when a newer version really exists."""
    c = read_update_cache()
    latest = c.get("latest") or ""
    info = {"current": APP_VERSION, "available": False}
    if latest and _version_tuple(latest) > _version_tuple(APP_VERSION):
        info.update(available=True, latest=latest, notes=c.get("notes") or "",
                    url=c.get("url") or "", published=c.get("published") or "",
                    name=c.get("name") or "")
    return info


def full_state(conn):
    return {
        "version": APP_VERSION,
        "update": update_info(),
        "grants": rows_to_list(conn.execute(
            "SELECT * FROM grants ORDER BY status, end_date")),
        "categories": rows_to_list(conn.execute(
            "SELECT * FROM categories ORDER BY sort, name")),
        "budget_lines": rows_to_list(conn.execute("SELECT * FROM budget_lines")),
        "people": rows_to_list(conn.execute("SELECT * FROM people ORDER BY name")),
        "appointments": rows_to_list(conn.execute("SELECT * FROM appointments")),
        "expenses": rows_to_list(conn.execute(
            "SELECT * FROM expenses ORDER BY date DESC, id DESC")),
        "today": date.today().isoformat(),
    }


def safe_under(base, untrusted):
    """Resolve `untrusted` (from a URL) inside `base` and refuse anything that
    escapes it.

    os.path.join(base, "/etc/passwd") returns "/etc/passwd" — join DISCARDS the
    base when the second argument is absolute — so a leading-".." check alone
    lets an absolute path walk straight out. Strip any leading separator, join,
    fully resolve (following symlinks), and require the result to still sit
    under the resolved base.
    """
    rel = untrusted.replace("\\", "/").lstrip("/")
    rel = os.path.normpath(rel)
    if rel.startswith("..") or os.path.isabs(rel):
        raise PermissionError("path outside allowed directory")
    full = os.path.realpath(os.path.join(base, rel))
    root = os.path.realpath(base)
    if full != root and not full.startswith(root + os.sep):
        raise PermissionError("path outside allowed directory")
    return full


def save_receipt(grant, payload):
    """payload: {name, data(base64)} -> relative path under receipts/."""
    fname = os.path.basename(payload.get("name") or "receipt")
    fname = re.sub(r"[^A-Za-z0-9._ -]+", "_", fname)
    raw = base64.b64decode(payload["data"])
    if len(raw) > 30 * 1024 * 1024:
        raise ValueError("Receipt file too large (max 30 MB)")
    sub = os.path.join(slugify(grant["name"]), str(date.today().year))
    folder = os.path.join(RECEIPTS_DIR, sub)
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(folder, f"{stamp}_{fname}")
    with open(path, "wb") as f:
        f.write(raw)
    return os.path.relpath(path, RECEIPTS_DIR)


TABLES = {
    "grants": ["name", "agency", "initial_amount", "start_date", "end_date",
               "status", "notes", "exclude_from_history", "nce_end_date"],
    "categories": ["name", "grant_id", "sort"],
    "people": ["name", "role"],
    "appointments": ["person_id", "grant_id", "monthly_salary", "fringe_rate",
                     "start_date", "end_date", "notes", "annual_tuition",
                     "auto_charge", "pct"],
    "expenses": ["grant_id", "category_id", "year", "date", "amount",
                 "description", "person_id", "receipt_path", "source",
                 "wd_entry", "wd_worktag"],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # ------------------------------------------------------------ security
    def is_local(self):
        host = (self.client_address[0] or "")
        return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def presented_key(self):
        q = urlparse(self.path).query
        for part in q.split("&"):
            if part.startswith("k="):
                return unquote(part[2:])
        hdr = self.headers.get("X-Grants-Key")
        if hdr:
            return hdr
        cookie = self.headers.get("Cookie") or ""
        for c in cookie.split(";"):
            c = c.strip()
            if c.startswith("gmkey="):
                return c[len("gmkey="):]
        return None

    def check_access(self):
        """Off-machine requests need the access key. Returns True to continue."""
        if self.is_local() or ACCESS_KEY is None:
            return True
        import hmac
        given = self.presented_key() or ""
        if hmac.compare_digest(given, ACCESS_KEY):
            return True
        self.send_json({"error": "This computer isn't authorised to open "
                        "Grants Manager. Ask the owner for the link that "
                        "includes the access key."}, 403)
        return False

    def check_not_csrf(self):
        """Block another website from driving this app through the browser.

        A page on evil.example can POST here with a 'simple' content type and
        no preflight. Two cheap, standard defences: reject a cross-site
        Origin outright, and require a custom header that a cross-origin
        request cannot set without triggering a preflight we never answer.
        """
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host") or ""
            allowed = {"http://" + host, "https://" + host,
                       "http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT}
            if origin not in allowed:
                self.send_json({"error": "Blocked a change requested by "
                                "another website."}, 403)
                return False
        if self.headers.get("X-Grants-App") != "1":
            self.send_json({"error": "Blocked a change that didn't come from "
                            "the Grants Manager page itself."}, 403)
            return False
        return True

    # ------------------------------------------------------------- helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Bodies are read whole into memory (base64 uploads), so cap them rather
    # than trusting a client-supplied Content-Length.
    MAX_BODY = 260 * 1024 * 1024

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        if length > self.MAX_BODY:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(length))

    def send_file(self, path, download_name=None):
        if not os.path.isfile(path):
            self.send_json({"error": "not found"}, 404)
            return
        ctypes = {".html": "text/html", ".js": "application/javascript",
                  ".css": "text/css", ".png": "image/png", ".jpg": "image/jpeg",
                  ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".pdf": "application/pdf", ".svg": "image/svg+xml",
                  ".csv": "text/csv", ".heic": "image/heic",
                  ".webp": "image/webp", ".json": "application/json"}
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctypes.get(ext, "application/octet-stream"))
        if download_name:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------- routing
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        # Unauthenticated identity probe. Carries no user data — it exists so a
        # second launch can tell "Grants Manager is already running here" from
        # "some unrelated program owns this port" without needing the key.
        if path == "/api/ping":
            self.send_json({"app": "grants-manager"})
            return
        if not self.check_access():
            return
        try:
            if path == "/" or path == "/index.html":
                self.send_file(os.path.join(APP_DIR, "index.html"))
            elif path.startswith("/app/"):
                self.send_file(safe_under(APP_DIR, path[len("/app/"):]))
            elif path.startswith("/receipts/"):
                self.send_file(safe_under(RECEIPTS_DIR, path[len("/receipts/"):]))
            elif path == "/api/state":
                conn = db()
                try:
                    self.send_json(full_state(conn))
                finally:
                    conn.close()
            elif path == "/api/workday/state":
                conn = db()
                try:
                    self.send_json(wd_state(conn))
                finally:
                    conn.close()
            elif path == "/api/workday/entry_sheet.csv":
                conn = db()
                try:
                    self.send_entry_sheet(conn)
                finally:
                    conn.close()
            elif path.startswith("/api/export/"):
                self.handle_export(path)
            elif path.startswith("/api/report/preview"):
                q = urlparse(self.path).query
                month = dict(p.split("=", 1) for p in q.split("&") if "=" in p
                             ).get("month") or date.today().strftime("%Y-%m")
                conn = db()
                try:
                    d = report_data(conn, month)
                    d["rows"] = [{k: v for k, v in r.items()
                                  if not k.startswith("_")} for r in d["rows"]]
                    d["columns"] = REPORT_COLUMNS
                    d["owner_email"] = (get_setting(conn, "workday_push", {})
                                        or {}).get("owner_email", "")
                    self.send_json(d)
                finally:
                    conn.close()
            elif path == "/api/lan_info":
                self.send_json({"lan_ip": lan_ip(), "port": PORT,
                                "key": ACCESS_KEY})
            elif path == "/api/trash":
                conn = db()
                try:
                    self.send_json({"batches": trash_list(conn)})
                finally:
                    conn.close()
            elif path == "/api/backup/download":
                make_backup(force=True)
                self.send_file(DB_PATH, download_name=(
                    f"grants_backup_{date.today().isoformat()}.db"))
            else:
                self.send_json({"error": "not found"}, 404)
        except PermissionError:
            self.send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            # don't hand absolute paths / internals to the caller
            print("GET %s failed: %r" % (path, e))
            self.send_json({"error": "Something went wrong handling that "
                            "request."}, 500)

    def handle_export(self, path):
        m = re.match(r"/api/export/grant/(\d+)\.csv", path)
        if not m:
            self.send_json({"error": "bad export path"}, 404)
            return
        gid = int(m.group(1))
        conn = db()
        try:
            grant = conn.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
            if not grant:
                self.send_json({"error": "grant not found"}, 404)
                return
            rows = conn.execute(
                "SELECT e.date, c.name AS category, e.year, e.amount, "
                "e.description, p.name AS person, e.receipt_path, e.source "
                "FROM expenses e LEFT JOIN categories c ON c.id=e.category_id "
                "LEFT JOIN people p ON p.id=e.person_id "
                "WHERE e.grant_id=? ORDER BY e.date", (gid,)).fetchall()
            import csv
            import io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["Date", "Category", "Budget Year", "Amount",
                        "Description", "Person", "Receipt", "Source"])
            for r in rows:
                w.writerow([r["date"], csv_safe(r["category"]), r["year"],
                            f"{r['amount']:.2f}",
                            csv_safe(r["description"]), csv_safe(r["person"] or ""),
                            csv_safe(r["receipt_path"] or ""), csv_safe(r["source"])])
            body = buf.getvalue().encode()
            name = f"{slugify(grant['name'])}_ledger.csv"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            conn.close()

    def send_entry_sheet(self, conn):
        """CSV of the fast-entry queue, labeled with Workday's field names,
        ready to hand to whoever types the expenses into Workday."""
        import csv
        import io
        ps = wd_push_state(conn)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Date", "Amount (USD)", "Spend Category (suggested)",
                    "Business Purpose / Memo", "Grant (worktag)", "Award",
                    "Cost Center", "Fund", "Additional Worktags", "Person",
                    "Receipt file", "Status", "App ref #"])
        for e in ps["queue"]:
            code = ps["codes"].get(e["grant_id"], {})
            prof = ps["profiles"].get(str(e["grant_id"]), {})
            w.writerow([
                e["date"], "%.2f" % e["amount"],
                csv_safe(ps["spend_suggest"].get(e["category_id"],
                                                 e["category"] or "")),
                csv_safe(e["description"]),
                csv_safe(e["wd_worktag"] or code.get("wd_grant_name")
                         or code.get("grant_code") or e["grant_name"]),
                csv_safe(code.get("award", "")),
                csv_safe(prof.get("cost_center", "")),
                csv_safe(prof.get("fund", "")),
                csv_safe(prof.get("extra", "")), csv_safe(e["person"] or ""),
                csv_safe(os.path.basename(e["receipt_path"] or "")),
                "Sent, waiting" if e["wd_entry"] == "sent"
                else "Not sent yet",
                e["id"]])
        body = buf.getvalue().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Disposition",
                         'attachment; filename="workday_entry_sheet.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.check_access() or not self.check_not_csrf():
            return
        path = unquote(urlparse(self.path).path)
        conn = db()
        try:
            data = self.read_body()
            # Create record: /api/<table>
            m = re.match(r"/api/(grants|categories|people|appointments|expenses)$",
                         path)
            if m:
                table = m.group(1)
                if table == "expenses" and data.get("receipt"):
                    grant = dict(conn.execute("SELECT * FROM grants WHERE id=?",
                                              (data["grant_id"],)).fetchone())
                    data["receipt_path"] = save_receipt(grant, data.pop("receipt"))
                cols = [c for c in TABLES[table] if c in data]
                cur = conn.execute(
                    f"INSERT INTO {table} ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [data[c] for c in cols])
                if table == "expenses":
                    audit_log(conn, "added", cur.lastrowid, data.get("grant_id"),
                              data.get("description"), data.get("amount"))
                conn.commit()
                self.send_json({"id": cur.lastrowid})
                return
            # Update record: /api/<table>/<id>
            m = re.match(r"/api/(grants|categories|people|appointments|expenses)"
                         r"/(\d+)$", path)
            if m:
                table, rid = m.group(1), int(m.group(2))
                if table == "expenses" and data.get("receipt"):
                    grant = dict(conn.execute(
                        "SELECT g.* FROM grants g JOIN expenses e ON e.grant_id=g.id "
                        "WHERE e.id=?", (rid,)).fetchone())
                    data["receipt_path"] = save_receipt(grant, data.pop("receipt"))
                cols = [c for c in TABLES[table] if c in data]
                if cols:
                    before = (conn.execute("SELECT * FROM expenses WHERE id=?",
                                           (rid,)).fetchone()
                              if table == "expenses" else None)
                    conn.execute(
                        f"UPDATE {table} SET {','.join(c + '=?' for c in cols)} "
                        f"WHERE id=?", [data[c] for c in cols] + [rid])
                    if before is not None:
                        after = conn.execute("SELECT * FROM expenses WHERE id=?",
                                             (rid,)).fetchone()
                        change = audit_describe_change(conn, before, dict(after))
                        if change:
                            audit_log(conn, "edited", rid, after["grant_id"],
                                      after["description"], after["amount"],
                                      change)
                    conn.commit()
                self.send_json({"ok": True})
                return
            if path == "/api/expenses/bulk":
                ids = [int(i) for i in (data.get("ids") or [])]
                if not ids:
                    self.send_json({"error": "Nothing selected"}, 400)
                    return
                marks = ",".join("?" * len(ids))

                if data.get("action") == "delete":
                    batch = new_batch_id()
                    rows = conn.execute(
                        f"SELECT * FROM expenses WHERE id IN ({marks})", ids).fetchall()
                    total = sum(r["amount"] for r in rows)
                    summary = ("%d expenses (%s)" % (len(rows), money_str(total))
                               if len(rows) > 1 else
                               "Expense — %s" % (rows[0]["description"] or "untitled"))
                    for r in rows:
                        trash_capture(conn, batch, summary, "delete",
                                      "expenses", r["id"], row=r)
                        audit_log(conn, "deleted", r["id"], r["grant_id"],
                                  r["description"], r["amount"])
                    conn.execute(f"DELETE FROM expenses WHERE id IN ({marks})", ids)
                    conn.commit()
                    self.send_json({"ok": True, "count": len(rows),
                                    "batch_id": batch})
                    return

                # field updates — only ever the columns we explicitly allow
                fields = data.get("fields") or {}
                allowed = ("grant_id", "category_id", "person_id", "year",
                           "date", "wd_entry", "source")
                sets = {k: v for k, v in fields.items() if k in allowed}
                if not sets:
                    self.send_json({"error": "No changes given"}, 400)
                    return
                # moving expenses to another grant: remap each row's category to
                # one valid on the destination, and recompute its budget year
                if "grant_id" in sets:
                    gid = int(sets["grant_id"])
                    grant = conn.execute("SELECT * FROM grants WHERE id=?",
                                         (gid,)).fetchone()
                    if not grant:
                        self.send_json({"error": "Grant not found"}, 400)
                        return
                    grant = dict(grant)
                    for r in conn.execute(
                            f"SELECT * FROM expenses WHERE id IN ({marks})", ids):
                        cid = sets.get("category_id", r["category_id"])
                        cid = _wd_category_on_grant(conn, cid, gid) if cid else cid
                        conn.execute(
                            "UPDATE expenses SET grant_id=?, category_id=?, year=? "
                            "WHERE id=?",
                            (gid, cid,
                             budget_year_for(grant, sets.get("date", r["date"])),
                             r["id"]))
                    sets.pop("grant_id", None)
                    sets.pop("category_id", None)
                    sets.pop("year", None)
                if sets:
                    assign = ",".join(f"{k}=?" for k in sets)
                    conn.execute(
                        f"UPDATE expenses SET {assign} WHERE id IN ({marks})",
                        list(sets.values()) + ids)
                conn.commit()
                self.send_json({"ok": True, "count": len(ids)})
                return
            if path == "/api/budget_line":
                conn.execute(
                    "INSERT INTO budget_lines (grant_id, category_id, year, amount) "
                    "VALUES (?,?,?,?) ON CONFLICT(grant_id, category_id, year) "
                    "DO UPDATE SET amount=excluded.amount",
                    (data["grant_id"], data["category_id"], data["year"],
                     data["amount"]))
                conn.commit()
                self.send_json({"ok": True})
                return
            if path == "/api/generate_salaries":
                n = generate_salaries(conn, data.get("through"))
                self.send_json({"created": n})
                return
            if path == "/api/workday/import":
                self.send_json(wd_import(conn))
                return
            if path == "/api/workday/upload_import":
                errors = []
                for f in data.get("files") or []:
                    try:
                        wd_save_uploaded(f)
                    except Exception as e:  # noqa: BLE001
                        errors.append("%s: %s" % (f.get("name", "?"), e))
                res = wd_import(conn)  # rescans the whole folder, incl. these
                if errors:
                    res["upload_errors"] = errors
                self.send_json(res)
                return
            if path == "/api/workday/push_config":
                set_setting(conn, "workday_push", {
                    "owner_email": (data.get("owner_email") or "").strip()})
                self.send_json({"ok": True})
                return
            if path == "/api/report/send":
                cfg = get_setting(conn, "workday_push", {}) or {}
                to = (data.get("to") or cfg.get("owner_email") or "").strip()
                if not to:
                    self.send_json({"error": "Add your email under ⚙ Settings "
                                    "so the report has somewhere to go."}, 400)
                    return
                month = data.get("month") or date.today().strftime("%Y-%m")
                d = report_data(conn, month)
                if not d["rows"]:
                    self.send_json({"error": "No expenses recorded for %s — "
                                    "nothing to report yet." % d["label"]}, 400)
                    return
                attach = [report_csv_path(d)]
                zp = report_receipts_zip(d)
                if zp:
                    attach.append(zp)
                wd_send_mail(to, "", "Grant expenses — %s" % d["label"],
                             report_text(d), attachment=attach,
                             html=report_html(d, to))
                sent = get_setting(conn, "reports_sent", {}) or {}
                sent[month] = datetime.now().isoformat(timespec="seconds")
                set_setting(conn, "reports_sent", sent)
                if not cfg.get("owner_email"):
                    cfg["owner_email"] = to
                    set_setting(conn, "workday_push", cfg)
                self.send_json({"ok": True, "to": to, "count": len(d["rows"]),
                                "attachments": [os.path.basename(a) for a in attach]})
                return
            if path == "/api/workday/send_email":
                to = (data.get("to") or "").strip()
                if not to:
                    self.send_json({"error": "Recipient email is required"}, 400)
                    return
                attach = None
                if data.get("receipt_path"):
                    p = os.path.join(RECEIPTS_DIR, data["receipt_path"])
                    if os.path.isfile(p):
                        attach = p
                wd_send_mail(to, "",
                             data.get("subject") or "Workday expense entry",
                             data.get("body") or "", attach)
                for eid in data.get("expense_ids") or []:
                    conn.execute("UPDATE expenses SET wd_entry='sent' WHERE id=?",
                                 (int(eid),))
                conn.commit()
                # remember it as the owner address for next time
                cfg = get_setting(conn, "workday_push", {}) or {}
                cfg["owner_email"] = to
                set_setting(conn, "workday_push", cfg)
                self.send_json({"ok": True})
                return
            if path == "/api/workday/worktags":
                prof = get_setting(conn, "workday_worktags", {}) or {}
                gid = str(data["grant_id"])
                prof[gid] = {"cost_center": (data.get("cost_center") or "").strip(),
                             "fund": (data.get("fund") or "").strip(),
                             "extra": (data.get("extra") or "").strip()}
                set_setting(conn, "workday_worktags", prof)
                self.send_json({"ok": True})
                return
            if path == "/api/workday/raas_config":
                cfg = {"summary_url": (data.get("summary_url") or "").strip(),
                       "detail_url": (data.get("detail_url") or "").strip(),
                       "username": (data.get("username") or "").strip(),
                       "auto": 1 if data.get("auto") else 0}
                set_setting(conn, "workday_raas", cfg)
                self.send_json({"ok": True})
                return
            if path == "/api/workday/sync":
                # the login popup can supply/update the Workday email
                user = (data.get("username") or "").strip()
                if user:
                    cfg = get_setting(conn, "workday_raas", {}) or {}
                    if user != cfg.get("username"):
                        cfg["username"] = user
                        set_setting(conn, "workday_raas", cfg)
                pw = data.get("password") or WD_SESSION["password"]
                if not pw:
                    self.send_json({"error": "password_required"}, 401)
                    return
                try:
                    res = wd_fetch_raas(conn, pw)
                except WDAuthError as e:
                    WD_SESSION["password"] = None
                    self.send_json({"error": str(e)}, 401)
                    return
                WD_SESSION["password"] = pw  # this process only, never on disk
                self.send_json(res)
                return
            if path == "/api/workday/map":
                conn.execute(
                    "INSERT INTO workday_map (kind, wd_key, target_id) "
                    "VALUES (?,?,?) ON CONFLICT(kind, wd_key) "
                    "DO UPDATE SET target_id=excluded.target_id",
                    (data["kind"], data["wd_key"], data.get("target_id")))
                conn.commit()
                self.send_json(wd_match(conn))
                return
            m = re.match(r"/api/workday/lines/(\d+)/ignore$", path)
            if m:
                conn.execute(
                    "UPDATE workday_lines SET status='ignored' "
                    "WHERE id=? AND status='pending'", (int(m.group(1)),))
                conn.commit()
                self.send_json({"ok": True})
                return
            if path == "/api/export_clean":
                self.send_json({"path": make_clean_copy()})
                return
            if path == "/api/backup/now":
                p = make_backup(force=True)
                self.send_json({"ok": True, "path": p})
                return
            if path == "/api/backup/restore":
                payload = data.get("file")
                if not payload or not payload.get("data"):
                    self.send_json({"error": "No file provided"}, 400)
                    return
                raw = base64.b64decode(payload["data"])
                if len(raw) > 200 * 1024 * 1024:
                    self.send_json({"error": "That's too large to be a "
                                    "Grants Manager backup (max 200 MB)"}, 400)
                    return
                tmp_path = DB_PATH + ".restore_tmp"
                with open(tmp_path, "wb") as f:
                    f.write(raw)
                try:
                    test = sqlite3.connect(tmp_path)
                    tables = {r[0] for r in test.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    test.close()
                except sqlite3.DatabaseError:
                    os.remove(tmp_path)
                    self.send_json({"error": "That file isn't a valid "
                                    "database."}, 400)
                    return
                required = {"grants", "expenses", "people", "appointments"}
                if not required.issubset(tables):
                    os.remove(tmp_path)
                    self.send_json({"error": "That file doesn't look like a "
                                    "Grants Manager backup (missing expected "
                                    "tables)."}, 400)
                    return
                make_backup(force=True)  # safety copy of what we're overwriting
                conn.close()
                os.replace(tmp_path, DB_PATH)
                self.send_json({"ok": True, "message": "Restored. Quit and "
                               "restart Grants Manager now to finish loading "
                               "the restored data."})
                return
            m = re.match(r"/api/trash/([0-9a-f]{6,32})/restore$", path)
            if m:
                trash_restore(conn, m.group(1))
                self.send_json({"ok": True})
                return
            self.send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 400)
        finally:
            conn.close()
            threading.Thread(target=write_snapshot, daemon=True).start()

    def do_DELETE(self):
        if not self.check_access() or not self.check_not_csrf():
            return
        path = unquote(urlparse(self.path).path)
        conn = db()
        try:
            m = re.match(r"/api/(grants|categories|people|appointments|expenses"
                         r"|budget_lines)/(\d+)$", path)
            if not m:
                self.send_json({"error": "not found"}, 404)
                return
            table, rid = m.group(1), int(m.group(2))
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?",
                               (rid,)).fetchone()
            if not row:
                self.send_json({"error": "not found"}, 404)
                return
            batch = new_batch_id()

            if table == "grants":
                summary = f"Grant \"{row['name']}\""
                n_exp = conn.execute("SELECT COUNT(*) FROM expenses WHERE "
                                     "grant_id=?", (rid,)).fetchone()[0]
                n_appt = conn.execute("SELECT COUNT(*) FROM appointments "
                                      "WHERE grant_id=?", (rid,)).fetchone()[0]
                if n_exp or n_appt:
                    summary += f" (+{n_exp} expenses, {n_appt} appointments)"
                for child_table, col in (
                        ("budget_lines", "grant_id"), ("expenses", "grant_id"),
                        ("appointments", "grant_id"), ("categories", "grant_id")):
                    for crow in conn.execute(
                            f"SELECT * FROM {child_table} WHERE {col}=?", (rid,)):
                        trash_capture(conn, batch, summary, "delete",
                                     child_table, crow["id"], row=crow)
                    conn.execute(f"DELETE FROM {child_table} WHERE {col}=?", (rid,))

            elif table == "people":
                summary = f"Person \"{row['name']}\""
                for crow in conn.execute(
                        "SELECT * FROM appointments WHERE person_id=?", (rid,)):
                    trash_capture(conn, batch, summary, "delete",
                                 "appointments", crow["id"], row=crow)
                conn.execute("DELETE FROM appointments WHERE person_id=?", (rid,))
                for crow in conn.execute(
                        "SELECT id, person_id FROM expenses WHERE person_id=?",
                        (rid,)):
                    trash_capture(conn, batch, summary, "unlink", "expenses",
                                 crow["id"], column="person_id",
                                 old_value=crow["person_id"])
                conn.execute(
                    "UPDATE expenses SET person_id=NULL WHERE person_id=?", (rid,))

            elif table == "appointments":
                p = conn.execute("SELECT name FROM people WHERE id=?",
                                 (row["person_id"],)).fetchone()
                summary = f"Appointment — {p['name'] if p else '?'}"
                for crow in conn.execute(
                        "SELECT id, appointment_id FROM expenses "
                        "WHERE appointment_id=?", (rid,)):
                    trash_capture(conn, batch, summary, "unlink", "expenses",
                                 crow["id"], column="appointment_id",
                                 old_value=crow["appointment_id"])
                conn.execute(
                    "UPDATE expenses SET appointment_id=NULL "
                    "WHERE appointment_id=?", (rid,))

            elif table == "expenses":
                summary = f"Expense — {(row['description'] or '').strip() or 'untitled'} (${row['amount']:.2f})"
            elif table == "categories":
                summary = f"Category \"{row['name']}\""
            else:
                summary = f"Budget line #{rid}"

            trash_capture(conn, batch, summary, "delete", table, rid, row=row)
            if table == "expenses":
                audit_log(conn, "deleted", rid, row["grant_id"],
                          row["description"], row["amount"])
            conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
            conn.commit()
            self.send_json({"ok": True, "batch_id": batch})
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 400)
        finally:
            conn.close()
            threading.Thread(target=write_snapshot, daemon=True).start()


# ------------------------------------------------------- phone snapshot

def _month_key(d):
    return d[:7]


def _add_months(ym, n):
    y, m = int(ym[:4]), int(ym[5:7])
    t = y * 12 + (m - 1) + n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def _months_between(a, b):
    if a > b:
        return 0
    return (int(b[:4]) * 12 + int(b[5:7])) - (int(a[:4]) * 12 + int(a[5:7])) + 1


def compute_projections(conn):
    """Per-grant future commitments — same rules as the web UI."""
    today = date.today().isoformat()
    out = {}
    for a in conn.execute("SELECT * FROM appointments"):
        g = conn.execute("SELECT * FROM grants WHERE id=?", (a["grant_id"],)).fetchone()
        if not g or g["status"] != "active":
            continue
        g_end = g["nce_end_date"] or g["end_date"]
        ends = sorted(x for x in [a["end_date"] and _month_key(a["end_date"]),
                                  g_end and _month_key(g_end)] if x)
        if not ends:
            continue
        end_ym = ends[0]
        charged = [_month_key(e["date"]) for e in conn.execute(
            "SELECT e.date FROM expenses e JOIN categories c ON c.id=e.category_id "
            "WHERE e.grant_id=? AND e.person_id=? AND e.amount>0 AND c.name='Personnel'",
            (a["grant_id"], a["person_id"]))]
        start_ym = _month_key(a["start_date"] or today)
        if charged:
            nxt = _add_months(max(charged), 1)
            start_ym = max(start_ym, nxt)
        else:
            start_ym = max(start_ym, _month_key(today))
        months = _months_between(start_ym, end_ym)
        if months <= 0:
            continue
        sal = a["monthly_salary"] * months
        fri = sal * (a["fringe_rate"] or 0) / 100
        tui = (a["annual_tuition"] or 0) / 12 * months
        d = out.setdefault(a["grant_id"], {"salary": 0, "fringe": 0, "tuition": 0})
        d["salary"] += sal
        d["fringe"] += fri
        d["tuition"] += tui
    return out


def write_snapshot():
    """Write a read-only, phone-formatted snapshot next to GrantsApp
    (inside the OneDrive-synced folder) so it can be opened on an iPhone."""
    try:
        conn = db()
        fmt = lambda v: "${:,.0f}".format(v)
        # Grant/category names are free text (and can arrive from a Workday
        # import), so escape them rather than trusting them in HTML.
        esc = lambda t: (str(t if t is not None else "").replace("&", "&amp;")
                         .replace("<", "&lt;").replace(">", "&gt;"))
        projs = compute_projections(conn)
        grants = rows_to_list(conn.execute(
            "SELECT * FROM grants ORDER BY status, end_date"))
        cards = []
        tot_sal_now = tot_sal_proj = tot_avail = 0.0
        for g in grants:
            if g["status"] != "active" or g["name"] == "Other":
                continue
            spent = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE grant_id=?",
                (g["id"],)).fetchone()[0]
            avail = g["initial_amount"] - spent
            per_cat = {}
            for r in conn.execute(
                    "SELECT c.name, COALESCE(SUM(b.amount),0) AS bud FROM budget_lines b "
                    "JOIN categories c ON c.id=b.category_id WHERE b.grant_id=? "
                    "GROUP BY c.name", (g["id"],)):
                per_cat[r["name"]] = {"budget": r["bud"], "spent": 0.0}
            for r in conn.execute(
                    "SELECT c.name, COALESCE(SUM(e.amount),0) AS sp FROM expenses e "
                    "JOIN categories c ON c.id=e.category_id WHERE e.grant_id=? "
                    "GROUP BY c.name", (g["id"],)):
                per_cat.setdefault(r["name"], {"budget": 0.0, "spent": 0.0})
                per_cat[r["name"]]["spent"] = r["sp"]
            p = projs.get(g["id"], {"salary": 0, "fringe": 0, "tuition": 0})
            sal_now = per_cat.get("Personnel", {"budget": 0, "spent": 0})
            sal_now = sal_now["budget"] - sal_now["spent"]
            fri_rem = per_cat.get("Fringe", {"budget": 0, "spent": 0})
            fri_rem = fri_rem["budget"] - fri_rem["spent"]
            tui_rem = per_cat.get("Tuition", {"budget": 0, "spent": 0})
            tui_rem = tui_rem["budget"] - tui_rem["spent"]
            sal_proj = (sal_now - p["salary"]
                        - max(0, p["fringe"] - max(0, fri_rem))
                        - max(0, p["tuition"] - max(0, tui_rem)))
            proj_avail = avail - p["salary"] - p["fringe"] - p["tuition"]
            tot_sal_now += max(0, sal_now)
            tot_sal_proj += max(0, sal_proj)
            tot_avail += avail
            end = g["nce_end_date"] or g["end_date"] or "—"
            rows = "".join(
                f"<tr><td>{esc(n)}</td><td class=n>{fmt(v['budget'] - v['spent'])}</td></tr>"
                for n, v in sorted(per_cat.items())
                if v["budget"] or v["spent"])
            neg = ' style="color:#d24545"' if sal_proj < 0 else ""
            cards.append(
                f"<div class=card><h2>{esc(g['name'])}</h2>"
                f"<div class=sub>{esc(g['agency'])} · ends {esc(end)}</div>"
                f"<div class=big>{fmt(avail)} <small>available now</small></div>"
                f"<div class=big2{neg}>{fmt(sal_proj)} <small>salary after "
                f"projections</small></div>"
                f"<table><tr><th>Category</th><th class=n>Remaining</th></tr>{rows}"
                f"</table></div>")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Grants Snapshot</title><style>
body{{font-family:-apple-system,Helvetica,sans-serif;background:#f7f8fa;color:#1a2333;
margin:0;padding:14px;font-size:15px}}
h1{{font-size:20px;margin:4px 0 2px}} h2{{font-size:16px;margin:0 0 2px}}
.sub{{color:#6b7688;font-size:12.5px;margin-bottom:8px}}
.card{{background:#fff;border:1px solid #e5e8ee;border-radius:12px;padding:14px;
margin:10px 0;box-shadow:0 1px 3px rgba(20,30,60,.06)}}
.big{{font-size:21px;font-weight:700}} .big2{{font-size:16px;font-weight:600;color:#1d9a6c}}
.big small,.big2 small{{font-size:11px;color:#6b7688;font-weight:500}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th{{text-align:left;color:#6b7688;font-size:10.5px;text-transform:uppercase;
padding:4px 0;border-bottom:1px solid #e5e8ee}}
td{{padding:4px 0;border-bottom:1px solid #f0f2f6}} .n{{text-align:right}}
.stats{{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}}
.stat{{flex:1;min-width:140px;background:#fff;border:1px solid #e5e8ee;
border-radius:12px;padding:10px}}
.stat .l{{font-size:10px;color:#6b7688;text-transform:uppercase;font-weight:600}}
.stat .v{{font-size:17px;font-weight:700}}
@media(prefers-color-scheme:dark){{body{{background:#12161f;color:#e6eaf2}}
.card,.stat{{background:#1a2030;border-color:#2a3245}}
td{{border-color:#232b3d}}th{{border-color:#2a3245}}}}
</style></head><body>
<h1>💰 Grants Snapshot</h1>
<div class=sub>Read-only · updated {stamp} · open the app on your Mac to edit</div>
<div class=stats>
<div class=stat><div class=l>Salary to hire (projected)</div><div class=v style="color:#1d9a6c">{fmt(tot_sal_proj)}</div></div>
<div class=stat><div class=l>Salary now</div><div class=v>{fmt(tot_sal_now)}</div></div>
<div class=stat><div class=l>Available (all active)</div><div class=v>{fmt(tot_avail)}</div></div>
</div>
{''.join(cards)}
<div class=sub style="margin-top:14px">Generated automatically by Grants Manager
whenever data changes. This file lives in OneDrive, so it syncs to your phone.</div>
</body></html>"""
        out = os.path.join(os.path.dirname(BASE_DIR), "Grants Snapshot.html")
        with open(out, "w") as f:
            f.write(html)
        conn.close()
    except Exception:  # noqa: BLE001 — snapshot must never break the app
        pass


CLEAN_README = """# Grants Manager

A local grant-management app for research faculty. Everything runs and
stays on **your** computer — no accounts, no cloud, no internet needed.
This copy starts completely empty: it contains the app only, no data.

## Before you install — the one requirement

The app needs **Python 3** (free). Check whether you already have it:

- **Mac** — open Terminal (Cmd+Space, type "Terminal") and run:

      python3 --version

  If it prints a version (3.9 or newer), you're ready. If a dialog offers
  to install "command line developer tools", accept it — that installs
  Python for you.

- **Windows** — the easiest way: just double-click **Start Grants Manager
  (Windows).bat** (below). If Python is missing it opens the **Microsoft
  Store** for you — click **Get** (no admin rights needed), wait, then
  double-click the starter again. That's it.

  Prefer to do it by hand? Open Command Prompt (Windows key, type "cmd")
  and run `py --version`. If not found, either install from the Microsoft
  Store (search "Python 3") or from <https://www.python.org/downloads/>
  (there, **check "Add python.exe to PATH"** during setup).

There is nothing else to install: the app uses only what ships with Python.

## Install & run

1. Unzip this folder anywhere (Documents, OneDrive, Desktop...). Keep the
   folder together — your database will live inside it.
2. Start the app:
   - **Mac**: double-click **Start Grants Manager (Mac).command**.
     First time: if macOS blocks it, right-click it → Open → Open.
     Alternative: in Terminal, `python3 server.py --launch`
   - **Windows**: double-click **Start Grants Manager (Windows).bat**.
     Alternative: in Command Prompt, `py server.py --launch`
3. Your browser opens at <http://127.0.0.1:8765> with an **empty
   database**. Leave the terminal window open while you use the app.

## First steps

Click **+ New grant**, enter the award, then click any number in the
budget matrix to set budgets by category and year. Add expenses from the
dashboard (drop receipt PDFs/photos onto the form). Add people and
appointments to see hiring projections on the **Summary** tab. The
**Instructions** tab inside the app explains every feature.

## Workday (optional)

When the app opens it asks whether to **connect to Workday or work
offline** — everything works fully offline. Connected, it can pull your
official balances and posted charges (drop report exports into a
`workday_imports/` folder next to the app, or set up a direct RaaS
connection in the ⇅ Workday panel), and push new expenses as
workday-ready emails to your financial team through **Microsoft Outlook**
(Mac or Windows), with the receipt PDF attached and you CC'd.

## Use it on your iPhone

With the app running on your computer and the phone on the **same Wi-Fi**:

1. The terminal shows a line like `On your iPhone: http://192.168.1.x:8765`
2. Open that address in Safari on the phone.
3. Tap **Share → Add to Home Screen** — it installs like a native app with
   its own icon and opens full screen. (The computer must be running the
   app while you use it from the phone.)

## Your data

Everything lives in this folder: `data/grants.db` (the database) and
`receipts/`.

**Automatic backups.** Each time the app starts it saves a dated copy of
your data into `data/backups/` (one per day, kept 30 days). You can also
download a copy any time from **Settings (gear icon) > Backups & data
safety**, and restore from one there if something goes wrong.

**Undo.** Deleting a grant, person, appointment or expense sends it to
**Settings > Recently deleted** for 30 days, so a mistaken delete can be
put back — restoring a grant also brings back its expenses and
appointments.

### One important warning if you keep this in OneDrive/Dropbox

Cloud sync is fine for *having* a copy elsewhere, but **never run Grants
Manager on two computers at the same time**, and wait for sync to finish
before opening it on another machine. Databases don't merge the way
documents do: if two copies are open at once, the sync service can't
combine them and will either overwrite one or leave a "conflicted copy"
file beside the real one. If you ever see such a file, don't delete it —
it may contain work missing from the main file. Check both, or restore a
backup from the Settings panel.

## Troubleshooting

- **Browser doesn't open** — start it yourself and visit
  <http://127.0.0.1:8765>.
- **`python3` / `py` not found** — install Python (see above).
- **The app opens showing someone's existing data** — another copy of
  Grants Manager is already running on this computer (the starter then
  just opens that one). Close the other copy first (quit its terminal
  window), then start this one.
- **Stop the app** — close the terminal window.
"""

WIN_BAT = ("@echo off\r\n"
           "cd /d \"%~dp0\"\r\n"
           "py -3 server.py --launch\r\n"
           "if %errorlevel%==0 goto :eof\r\n"
           "python server.py --launch\r\n"
           "if %errorlevel%==0 goto :eof\r\n"
           "echo.\r\n"
           "echo   Couldn't start Python - see any error above this message.\r\n"
           "echo.\r\n"
           "echo   If Python isn't installed yet, that's a free, one-time\r\n"
           "echo   install - no admin rights needed. Opening the Microsoft\r\n"
           "echo   Store: click \"Get\", wait for it to finish, then\r\n"
           "echo   double-click this file again.\r\n"
           "echo.\r\n"
           "echo   If Python IS already installed and you still see this,\r\n"
           "echo   email samuelbf@uark.edu with a screenshot of this window.\r\n"
           "echo.\r\n"
           "start \"\" \"ms-windows-store://search/?query=Python 3\"\r\n"
           "pause\r\n")

MAC_COMMAND = ("#!/bin/bash\n"
               "cd \"$(dirname \"$0\")\"\n"
               "python3 server.py --launch\n"
               "if [ $? -eq 0 ]; then exit; fi\n"
               "echo\n"
               "echo \"  Couldn't start Python 3.\"\n"
               "echo \"  If macOS just offered to install "
               "'command line developer\"\n"
               "echo \"  tools', click Install, wait for it to finish, then\"\n"
               "echo \"  double-click this file again - that installs Python\"\n"
               "echo \"  for you, no App Store or admin password beyond your\"\n"
               "echo \"  own login needed.\"\n"
               "echo\n"
               "echo \"  Still stuck? Email samuelbf@uark.edu with a\"\n"
               "echo \"  screenshot of this window.\"\n"
               "echo\n"
               "read -n 1 -s -r -p \"Press any key to close...\"\n"
               "echo\n")


def make_clean_copy():
    """Zip the app code WITHOUT any data (no db, receipts, or import scripts).

    Returns the path of the zip, written next to the GrantsApp folder.
    """
    import zipfile
    out = os.path.join(os.path.dirname(BASE_DIR), "Grants Manager (shareable).zip")
    include = [
        ("server.py", os.path.join(BASE_DIR, "server.py")),
        ("app/index.html", os.path.join(APP_DIR, "index.html")),
        ("app/styles.css", os.path.join(APP_DIR, "styles.css")),
        ("app/app.js", os.path.join(APP_DIR, "app.js")),
        ("app/chart.umd.js", os.path.join(APP_DIR, "chart.umd.js")),
        ("app/icon.png", os.path.join(APP_DIR, "icon.png")),
        ("app/manifest.json", os.path.join(APP_DIR, "manifest.json")),
    ]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, src in include:
            if os.path.isfile(src):
                z.write(src, "GrantsManager/" + arc)
        now = datetime.now().timetuple()[:6]
        for name, content in (("GrantsManager/README.md", CLEAN_README),
                             ("GrantsManager/Start Grants Manager (Windows).bat", WIN_BAT)):
            info = zipfile.ZipInfo(name, date_time=now)
            info.external_attr = 0o644 << 16  # normal file, avoids some unzip
            info.compress_type = zipfile.ZIP_DEFLATED  # tools defaulting to 0600
            z.writestr(info, content)
        info = zipfile.ZipInfo(
            "GrantsManager/Start Grants Manager (Mac).command", date_time=now)
        info.external_attr = 0o755 << 16  # executable so double-click works
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, MAC_COMMAND)
    return out


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_our_server(port):
    """True only if the thing listening on `port` is actually Grants Manager
    (not some unrelated process a user happens to have running there)."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as resp:
            data = json.loads(resp.read())
        return isinstance(data, dict) and data.get("app") == "grants-manager"
    except Exception:  # noqa: BLE001 — any failure means "not us"
        return False


def find_free_port(start, tries=50):
    port = start
    for _ in range(tries):
        if not port_in_use(port):
            return port
        port += 1
    raise RuntimeError(f"No free port found near {start}")


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    global PORT
    launch = "--launch" in sys.argv
    if port_in_use(PORT):
        if is_our_server(PORT):
            url = f"http://127.0.0.1:{PORT}"
            if launch:
                webbrowser.open(url)
                return
            print(f"Already running at {url}")
            return
        # Something else — unrelated to this app — is using our usual port.
        # Never silently open the browser to a stranger's server; find our
        # own free port instead.
        PORT = find_free_port(PORT + 1)
    url = f"http://127.0.0.1:{PORT}"
    try:
        init_db()
    except sqlite3.DatabaseError as e:
        # Most likely causes: a half-synced/corrupted file from cloud sync, or
        # a "conflicted copy" left behind. Say so in plain language and point
        # at the backups rather than dumping a traceback on a non-technical user.
        bak = []
        if os.path.isdir(BACKUP_DIR):
            bak = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
        print("\n  Grants Manager could not open your data file.")
        print(f"  ({e})\n")
        print(f"  The file is: {DB_PATH}")
        if bak:
            print("\n  You have automatic backups. To recover, rename or move the")
            print("  file above, then copy the newest backup into its place:")
            for f in bak[-3:]:
                print(f"    {os.path.join(BACKUP_DIR, f)}")
        else:
            print("\n  No automatic backups were found next to it.")
        print("\n  If this folder is synced (OneDrive/Dropbox), also check for a")
        print("  'conflicted copy' file beside it — it may hold your newest work.")
        print("  Help: samuelbf@uark.edu\n")
        sys.exit(1)
    write_snapshot()
    # Look for a new release in the background — never delays startup, and is
    # silently skipped when there's no internet.
    threading.Thread(target=check_for_update, daemon=True).start()
    try:
        make_backup()  # one automatic dated copy per day, pruned after 30
        prune_backups()
        conn = db()
        try:
            prune_trash(conn)
        finally:
            conn.close()
    except OSError:
        pass
    try:
        make_clean_copy()  # keep the shareable (data-free) zip fresh
    except OSError:
        pass
    load_access_key()
    # bind all interfaces so the app is reachable from a phone on the same Wi-Fi
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Grants Manager running at {url}  (Ctrl+C to stop)")
    ip = lan_ip()
    if ip:
        print(f"On your iPhone (same Wi-Fi): http://{ip}:{PORT}/?k={ACCESS_KEY}")
        print("  ^ that link includes your access key — anyone with it can see")
        print("    your grants, so treat it like a password.")
    if launch:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
