#!/usr/bin/env python3
"""Grants Manager - local grant management app.

Zero external dependencies: Python stdlib + SQLite only.
Run:  python3 server.py            (starts server, prints URL)
      python3 server.py --launch   (starts server and opens browser)
If the server is already running, --launch just opens the browser.
"""
import base64
import difflib
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
import uuid
import webbrowser
import zlib
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

SERVER = None   # the running ThreadingHTTPServer, for graceful self-shutdown

STANDARD_CATEGORIES = [
    "Personnel", "Fringe", "Tuition", "Travel",
    "Equipment", "Supplies", "Publication",
    "Facilities & Administration", "Other",
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
    # Requests are served on threads, so two writes can collide (e.g. two
    # browser tabs saving at once). Wait politely instead of failing instantly.
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
    # money actually released so far on a grant that is funded in instalments
    # (the award total stays the full figure); 0 = the whole award is available
    ("grants", "funded_to_date", "REAL DEFAULT 0"),
    # 1 = the People-tab appointments are the plan: payroll Workday still holds
    # beyond them (people moved or leaving, not yet redistributed there) is
    # treated as released in the projections
    ("grants", "plan_basis", "INTEGER DEFAULT 0"),
    # last day the person is expected to stay; months after the last
    # appointment up to here show as "pending" funding on the People tab
    ("people", "plan_end", "TEXT DEFAULT ''"),
    ("appointments", "annual_tuition", "REAL DEFAULT 0"),
    ("appointments", "auto_charge", "INTEGER DEFAULT 0"),
    # 1 = this appointment carries only the fringe on a salary paid by another
    # appointment (e.g. salary on one grant, its fringe on "Other")
    ("appointments", "fringe_only", "INTEGER DEFAULT 0"),
    # share of the person's total salary carried by this appointment (%)
    ("appointments", "pct", "REAL DEFAULT 100"),
    # pay-period date of a Workday journal line (payroll month bucketing)
    ("workday_lines", "budget_date", "TEXT DEFAULT ''"),
    # Workday fast-entry status: '' needs entry, 'sent' emailed/waiting to
    # post, 'done' the user marked it entered in Workday (hidden from the
    # dashboard list), 'na' not a Workday expense (cleared automatically once
    # a Workday line links to the expense)
    ("expenses", "wd_entry", "TEXT DEFAULT ''"),
    # explicit worktag typed at entry time — used for expenses (e.g. "Other"
    # external accounts) that have no grant-level Workday mapping to pull one from
    ("expenses", "wd_worktag", "TEXT DEFAULT ''"),
    # additional documents beyond the main receipt (a P-card authorization
    # form, a spreadsheet...): JSON list of {"path","name","added"}. Kept on the
    # row itself so deleting/restoring an expense carries them along.
    ("expenses", "extra_files", "TEXT DEFAULT ''"),
    # P-card purchases. The university's reconciliation form asks for these on
    # every card transaction, so they are recorded at entry time rather than
    # reconstructed from memory at month end.
    ("expenses", "pcard", "INTEGER DEFAULT 0"),
    # "Purchased by (if different from cardholder)" — usually blank
    ("expenses", "pcard_buyer", "TEXT DEFAULT ''"),
    # per-expense override; normally inherited from the card set up in Settings
    ("expenses", "pcard_holder", "TEXT DEFAULT ''"),
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
    else:
        # Add any standard category introduced after this database was seeded
        # (e.g. "Facilities & Administration"), so imports can auto-map to it.
        have = {r[0] for r in conn.execute(
            "SELECT name FROM categories WHERE grant_id IS NULL")}
        for i, name in enumerate(STANDARD_CATEGORIES):
            if name not in have:
                conn.execute(
                    "INSERT INTO categories (name, grant_id, sort) "
                    "VALUES (?, NULL, ?)", (name, i))
    conn.commit()
    conn.close()


# --------------------------------------------------------------- backups
#
# One automatic, dated copy of the live database on every app start (at most
# one per calendar day), pruned after BACKUP_RETENTION_DAYS. This is a local
# safety net independent of OneDrive — protects against sync conflicts,
# accidental bulk edits, or a corrupted live file.

def sqlite_snapshot(src_path, dest_path):
    """Copy a database with SQLite's backup API. A plain file copy can catch
    the file halfway through another request's write (a torn, unusable
    backup); this always yields a consistent snapshot, even while the app is
    saving something."""
    src = sqlite3.connect(src_path, timeout=15)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


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
    sqlite_snapshot(DB_PATH, dest)
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
        r_old_id = data["id"]
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
        if table == "expenses":
            new_id = id_remap.get(("expenses", r_old_id)) if taken else data["id"]
            conn.execute("UPDATE workday_lines SET status='imported', "
                         "expense_id=? WHERE expense_id=? AND status='deleted'",
                         (new_id, r_old_id))

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


def json_safe(o):
    """Replace infinities/NaN with 0 on the way OUT.

    Python's json writes them as bare `Infinity`/`NaN`, which is not valid
    JSON — one such value anywhere in /api/state makes the browser's
    JSON.parse throw and the whole app fail to load, with no way to delete the
    offending row from inside the app. reject_wild_numbers() keeps them from
    being stored in the first place; this is the escape hatch for a database
    that already picked one up.
    """
    if isinstance(o, float):
        return o if math.isfinite(o) else 0
    if isinstance(o, dict):
        return {k: json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [json_safe(v) for v in o]
    return o


def reject_wild_numbers(o, depth=0):
    """Refuse infinities/NaN on the way IN.

    JavaScript turns anything past ~1.8e308 into Infinity, so pasting a long
    row of digits into an amount box is enough to produce one — no malice
    required. Stored, it poisons every total silently (NaN loses all
    comparisons) and breaks the JSON the app is served with.
    """
    if depth > 40:
        raise ValueError("That data is nested too deeply to store.")
    if isinstance(o, float) and not math.isfinite(o):
        raise ValueError("That number is too large (or not a number) to "
                         "store — check the amount you typed.")
    if isinstance(o, dict):
        for v in o.values():
            reject_wild_numbers(v, depth + 1)
    elif isinstance(o, (list, tuple)):
        for v in o:
            reject_wild_numbers(v, depth + 1)
    return o


def rows_to_list(rows):
    return [dict(r) for r in rows]


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(conn, key, value, commit=True):
    conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    if commit:
        conn.commit()


WIN_RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} \
    | {"lpt%d" % i for i in range(1, 10)}


def _win_safe(stem):
    """A file/folder name Windows will accept: not a device name (CON, AUX,
    NUL, COM1...) even with an extension, and no trailing dot or space."""
    stem = stem.strip(" .")
    if stem.split(".")[0].lower() in WIN_RESERVED:
        stem = "_" + stem
    return stem


def slugify(name, limit=40):
    """Folder-safe form of a grant name. Kept short on purpose: OneDrive
    folders are already deep, and Windows refuses any path over ~260
    characters — a 75-character folder name was enough to make saving a
    receipt fail there."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:limit].strip("_-")
    return _win_safe(s or "grant")


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
        if grant.get("status") != "active":     # never book salary on a closed grant
            continue
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
                if amt <= 0 or (a.get("fringe_only") and label == "Salary"):
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


# The two reports the app understands, and the columns that identify each.
# Kept here so the format checks, the error messages and the example workbook
# can never drift apart from what wd_ingest_rows() actually reads.
WD_DETAIL_KEYS = {"Accounting Date", "Transaction Amount"}
WD_SUMMARY_KEYS = {"Object Class", "Budget", "Available Balance"}
WD_DETAIL_COLUMNS = ["Accounting Date", "Budget Date", "Operational Transaction",
                     "Award", "Grant", "Worker", "Supplier", "Ledger Account",
                     "Transaction Amount", "Object Class", "Spend Category"]
# Column order matches UARK Workday's "Grant Budget vs Actuals" export exactly,
# so the example workbook is a faithful stand-in for a real one.
WD_SUMMARY_COLUMNS = ["Award", "Grant", "Grant Start Date", "Grant End Date",
                      "Object Class", "Budget", "Commitment", "Obligation",
                      "Actuals", "Available Balance"]
# an .xlsx is a zip; refuse one that expands to more than this (zip bomb)
XLSX_MAX_UNPACKED = 100 * 1024 * 1024   # a real Workday export is a few MB


def sniff_spreadsheet(path):
    """Say plainly what a file that isn't a readable .xlsx actually is.

    People export the wrong thing far more often than they hit a real bug:
    Workday's "Print" gives a PDF, older Excel and some Windows setups give
    .xls, "Export to CSV" gives text, and Numbers/Sheets hand back their own
    formats. Every one of those used to be renamed to .xlsx on upload and then
    fail deep inside the XML parser. Returns an explanation, or None if the
    file looks like a genuine .xlsx.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return "The file could not be read."
    if head[:4] == b"%PDF":
        return ("That's a PDF. In Workday use the “Export to Excel” button "
                "(the little grid icon above the report), not Print or "
                "Download PDF.")
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return ("That's the older Excel format (.xls). Open it in Excel and "
                "use File → Save As → Excel Workbook (.xlsx), then upload "
                "that.")
    if head[:2] != b"PK":
        return ("That isn't an Excel workbook. If you exported a CSV or text "
                "file, run the Workday export again and choose Excel "
                "(.xlsx).")
    import zipfile
    try:
        z = zipfile.ZipFile(path)
        try:
            names = z.namelist()
            unpacked = sum(i.file_size for i in z.infolist())
        finally:
            z.close()      # Windows can't move or delete a file that is still open
    except zipfile.BadZipFile:
        return "The file is damaged — try exporting it from Workday again."
    if not any(n.startswith("xl/") for n in names):
        if any(n.startswith("word/") for n in names):
            return "That's a Word document, not a Workday export."
        if any(n.startswith("ppt/") for n in names):
            return "That's a PowerPoint file, not a Workday export."
        if any(n.endswith(".iwa") or n.startswith("Index/") for n in names):
            return ("That's an Apple Numbers file. Open it and use File → "
                    "Export To → Excel, then upload the .xlsx.")
        return ("That isn't an Excel workbook — it's a different kind of zip "
                "file.")
    if unpacked > XLSX_MAX_UNPACKED:
        return ("That workbook is far too large to be a grant report — "
                "check you exported the right thing.")
    if not any(n.startswith("xl/worksheets/") for n in names):
        return "That workbook has no worksheets in it."
    return None


def wd_describe_bad_headers(headers):
    """Explain which of the two reports a sheet almost matched, and what's
    missing — instead of silently importing nothing."""
    hset = {h for h in headers if h}
    if not hset:
        return ("The first sheet has no column headings. Make sure you upload "
                "the exported report itself, not a copy you've edited.")
    detail_missing = sorted(WD_DETAIL_KEYS - hset)
    summary_missing = sorted(WD_SUMMARY_KEYS - hset)
    # whichever report it's closer to is almost certainly the one intended
    if len(detail_missing) <= len(summary_missing):
        kind, missing, cols = ("transactions", detail_missing,
                               WD_DETAIL_COLUMNS)
    else:
        kind, missing, cols = ("balances", summary_missing, WD_SUMMARY_COLUMNS)
    found = ", ".join(sorted(hset)[:8]) + ("…" if len(hset) > 8 else "")
    return ("This looks like it's meant to be the %s report, but the "
            "column%s %s %s missing. The %s report needs these columns, "
            "spelled exactly this way: %s. Columns found instead: %s. "
            "Download the example file to see the expected layout."
            % (kind, "" if len(missing) == 1 else "s",
               ", ".join("“%s”" % m for m in missing),
               "is" if len(missing) == 1 else "are",
               kind, ", ".join(cols), found))


def _xlsx_shared_strings(z):
    import xml.etree.ElementTree as ET
    shared = []
    if "xl/sharedStrings.xml" not in z.namelist():
        return shared
    with z.open("xl/sharedStrings.xml") as f:
        for _ev, el in ET.iterparse(f):
            if el.tag == _XLSX_NS + "si":
                parts = []
                for ch in el:                       # plain <t> or rich <r><t>;
                    if ch.tag == _XLSX_NS + "t":    # phonetic <rPh> is skipped
                        parts.append(ch.text or "")
                    elif ch.tag == _XLSX_NS + "r":
                        t = ch.find(_XLSX_NS + "t")
                        parts.append(t.text or "" if t is not None else "")
                shared.append("".join(parts))
                el.clear()
    return shared


def _xlsx_grid(z, member, shared, max_rows=None):
    """Rows of one worksheet as lists of strings, streamed so a very large
    sheet never has to sit in memory as one XML tree."""
    import xml.etree.ElementTree as ET
    grid = []
    with z.open(member) as f:
        for _ev, row in ET.iterparse(f):
            if row.tag != _XLSX_NS + "row":
                continue
            cells = {}
            nxt = 0
            for c in row.iter(_XLSX_NS + "c"):
                ref = c.get("r") or ""
                col = re.match(r"[A-Z]+", ref)
                if col:
                    idx = 0
                    for ch in col.group(0):     # column letters -> 0-based index
                        idx = idx * 26 + (ord(ch) - 64)
                    idx -= 1
                else:
                    idx = nxt
                nxt = idx + 1
                t = c.get("t")
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(_XLSX_NS + "t"))
                else:
                    v = c.find(_XLSX_NS + "v")
                    val = v.text if v is not None and v.text is not None else ""
                    if t == "s" and val != "":
                        try:
                            val = shared[int(val)]
                        except (ValueError, IndexError):
                            val = ""
                cells[idx] = val
            row.clear()
            if cells:
                grid.append([cells.get(i, "") for i in range(max(cells) + 1)])
            if max_rows and len(grid) >= max_rows:
                break
    return grid


def _xlsx_sheets(z):
    """[(name, member_path, visible)] in workbook order, plus whether the
    workbook uses Excel's 1904 date system. Falls back to sheet1 if the
    workbook part can't be read."""
    import xml.etree.ElementTree as ET
    ns_main = _XLSX_NS
    rid_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = {r.get("Id"): r.get("Target") for r in rels}
        pr = wb.find(ns_main + "workbookPr")
        d1904 = (pr is not None and str(pr.get("date1904", "")).lower()
                 in ("1", "true"))
        out = []
        for sh in wb.iter(ns_main + "sheet"):
            tgt = target.get(sh.get(rid_attr)) or ""
            tgt = tgt.lstrip("/")
            member = tgt if tgt.startswith("xl/") else "xl/" + tgt
            if member in z.namelist():
                out.append((sh.get("name") or "", member,
                            sh.get("state", "visible") == "visible"))
        if out:
            return out, d1904
    except (KeyError, ET.ParseError):
        pass
    return [("Sheet1", "xl/worksheets/sheet1.xml", True)], False


def parse_xlsx(path):
    """Minimal stdlib .xlsx reader -> (headers, rows-as-dicts).

    Uses the first VISIBLE sheet whose column headings look like a Workday
    report (a hidden cover sheet, or the report on sheet 2, no longer makes a
    good export look unrecognised); if none looks right, the first visible
    sheet is used so the error can name what's actually in it."""
    import zipfile
    bad = sniff_spreadsheet(path)
    if bad:
        raise ValueError(bad)
    z = zipfile.ZipFile(path)
    try:
        return _parse_xlsx_open(z)
    finally:
        z.close()    # Windows can't move or delete a file that is still open


def _parse_xlsx_open(z):
    shared = _xlsx_shared_strings(z)
    sheets, d1904 = _xlsx_sheets(z)
    visible = [x for x in sheets if x[2]] or sheets
    chosen = None
    for _name, member, _vis in visible:
        head = _xlsx_grid(z, member, shared, max_rows=8)
        if head:
            hrow = find_header_row(head)
            known = WD_DETAIL_KEYS | WD_SUMMARY_KEYS | set(WD_DETAIL_COLUMNS) \
                | set(WD_SUMMARY_COLUMNS)
            if sum(1 for c in head[hrow] if str(c).strip() in known) >= 2:
                chosen = member
                break
    if chosen is None:
        chosen = visible[0][1]
    grid = _xlsx_grid(z, chosen, shared)
    if not grid:
        raise ValueError("The first sheet of that workbook is empty.")
    hrow = find_header_row(grid)
    headers = [str(h).strip() for h in grid[hrow]]
    date_cols = {h for h in headers if "date" in h.lower()}
    rows = []
    for raw in grid[hrow + 1:]:
        d = {headers[i]: raw[i] if i < len(raw) else ""
             for i in range(len(headers)) if headers[i]}
        if d1904:   # Excel's Mac-era epoch is 1,462 days later than the default
            for h in date_cols:
                v = str(d.get(h, "")).strip()
                if re.match(r"^\d+(\.\d+)?$", v):
                    d[h] = str(int(float(v)) + 1462)
        rows.append(d)
    return headers, rows


def find_header_row(grid):
    """Index of the row holding the column names.

    Workday exports often lead with a title ("RPT - Grant Budget vs Actuals"),
    a filter summary, or a blank row, so the headings are not always row 1 —
    and taking row 1 regardless made a perfectly good export look like an
    unrecognised format. Pick whichever of the first few rows names the most
    columns we recognise; fall back to the first non-empty row.
    """
    known = WD_DETAIL_KEYS | WD_SUMMARY_KEYS | set(WD_DETAIL_COLUMNS) \
        | set(WD_SUMMARY_COLUMNS)
    best, best_hits = None, 0
    for i, row in enumerate(grid[:8]):
        hits = sum(1 for c in row if str(c).strip() in known)
        if hits > best_hits:
            best, best_hits = i, hits
    if best is not None and best_hits >= 2:
        return best
    for i, row in enumerate(grid):
        if any(str(c).strip() for c in row):
            return i
    return 0


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
    """'$1,500.00', '(102.00)', '1.234,56', '12,5', 1500.0 -> float (0.0 if
    unparseable). Understands both US (1,234.56) and European (1.234,56)
    thousands/decimal conventions: whichever of . or , comes LAST is the
    decimal point; a lone comma is a decimal only when 1-2 digits follow."""
    s = str(v).replace("$", "").replace("\u00a0", "").replace(" ", "").strip()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith("-"):
        neg, s = True, s[1:]
    elif s.endswith("-"):
        neg, s = True, s[:-1]
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):       # 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:                                 # 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        if len(tail) in (1, 2) and "," not in head:
            s = head + "." + tail             # 12,5 / 1234,56
        else:
            s = s.replace(",", "")            # 1,234 / 1,234,567
    try:
        n = float(s or 0)
    except ValueError:
        return 0.0
    if n != n or n in (float("inf"), float("-inf")):
        return 0.0
    return -n if neg else n


def _wd_grant_code(grant_str):
    m = re.match(r"\s*(GR\w+)", str(grant_str))
    return m.group(1) if m else str(grant_str).strip()


def _wd_is_total(grant_str, object_class=""):
    """True for a report's total/subtotal line, which names no real grant and
    must never be ingested as one (Workday appends 'Total' rows that would
    otherwise show up as a phantom grant asking to be matched)."""
    g = str(grant_str).strip().lower()
    oc = str(object_class).strip().lower()
    if not g:
        return True
    if g.startswith("total") or g.startswith("grand total") or g in (
            "total", "totals", "subtotal", "grand total"):
        return True
    return oc in ("total", "totals", "subtotal", "grand total")


def _wd_norm_name(s):
    """Compact a grant name/code for fuzzy matching: drop any leading GR code,
    then keep only lowercase letters and digits."""
    s = re.sub(r"^\s*GR\w+\s*[-—|:]*\s*", "", str(s), flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _wd_worktag(worktags, label):
    m = re.search(r"^%s:\s*(.+)$" % re.escape(label), str(worktags), re.M)
    return m.group(1).strip() if m else ""


# object-class keyword -> standard category name (auto-seeded, user-overridable)
WD_CAT_HINTS = [
    ("fringe", "Fringe"), ("personnel", "Personnel"), ("salar", "Personnel"),
    ("tuition", "Tuition"), ("foreign travel", "Foreign Travel"),
    ("travel", "Travel"), ("equipment", "Equipment"), ("suppl", "Supplies"),
    ("materials", "Supplies"), ("public", "Publication"),
    ("facilit", "Facilities & Administration"),
    ("administrat", "Facilities & Administration"),
    ("indirect", "Facilities & Administration"),
    ("f&a", "Facilities & Administration"),
    ("professional", "Other"), ("direct cost", "Other"),
    ("other", "Other"),  # keep last: least specific catch-all
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


# ------------------------------------------------- example workbook
#
# A two-sheet .xlsx with obviously fake data, generated on demand so it can
# never fall out of step with WD_*_COLUMNS above. Written by hand because the
# app has no third-party dependencies — an .xlsx is just a zip of XML.

WD_EXAMPLE_DETAIL_ROWS = [
    ["2026-03-04", "2026-03-04", "Supplier Invoice: SINV-100241", "AWD-000123",
     "GR000123 Example Grant — Soil Microbiome", "", "Example Lab Supply Co",
     "6300:Supplies", 412.75, "Supplies", "Laboratory Supplies"],
    ["2026-03-11", "2026-03-11", "Expense Report: EXP-004417", "AWD-000123",
     "GR000123 Example Grant — Soil Microbiome", "Doe, Jane", "",
     "6500:Travel", 1284.10, "Travel", "Airfare - Domestic"],
    ["2026-03-31", "2026-03-31", "Payroll: PAY-2026-03", "AWD-000123",
     "GR000123 Example Grant — Soil Microbiome", "Roe, Alex", "",
     "6100:Salaries", 4166.67, "Personnel", "Salaries - Postdoctoral"],
    ["2026-03-31", "2026-03-31", "Payroll: PAY-2026-03", "AWD-000123",
     "GR000123 Example Grant — Soil Microbiome", "Roe, Alex", "",
     "6150:Fringe", 1145.83, "Fringe", "Fringe Benefits"],
    ["2026-04-02", "2026-04-02", "Supplier Invoice: SINV-100388", "AWD-000456",
     "GR000456 Example Grant — Field Trial", "", "Example Seed Supply",
     "6300:Supplies", 87.40, "Supplies", "Field Supplies"],
]

WD_EXAMPLE_SUMMARY_ROWS = [
    ["AWD-000123", "GR000123 Example Grant — Soil Microbiome", "2025-07-01", "2027-06-30",
     "UA System Sponsored Programs: 01_Personnel", 150000, 0, 0, 48000, 102000],
    ["AWD-000123", "GR000123 Example Grant — Soil Microbiome", "2025-07-01", "2027-06-30",
     "UA System Sponsored Programs: 02_Fringe", 41250, 0, 0, 13200, 28050],
    ["AWD-000123", "GR000123 Example Grant — Soil Microbiome", "2025-07-01", "2027-06-30",
     "UA System Sponsored Programs: 04_Travel", 12000, 0, 0, 3100, 8900],
    ["AWD-000123", "GR000123 Example Grant — Soil Microbiome", "2025-07-01", "2027-06-30",
     "UA System Sponsored Programs: 05_Supplies", 20000, 1500, 0, 6200, 12300],
    ["AWD-000123", "GR000123 Example Grant — Soil Microbiome", "2025-07-01", "2027-06-30",
     "UA System Sponsored Programs: 09_Facilities & Administration", 60000, 0, 0, 19200, 40800],
    ["AWD-000456", "GR000456 Example Grant — Field Trial", "2026-01-01", "2028-08-31",
     "UA System Sponsored Programs: 05_Supplies", 8000, 0, 0, 900, 7100],
    ["AWD-000456", "GR000456 Example Grant — Field Trial", "2026-01-01", "2028-08-31",
     "UA System Sponsored Programs: 06_Equipment", 15000, 0, 0, 0, 15000],
]


def _xlsx_sheet_xml(columns, rows):
    """One worksheet as SpreadsheetML, with inline strings so there is no
    shared-string table to keep in sync."""
    def esc(v):
        return (str(v).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    def cell(col, rownum, v, style=""):
        ref = "%s%d" % (col, rownum)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return '<c r="%s"%s><v>%s</v></c>' % (ref, style, v)
        if v == "" or v is None:
            return ''
        return ('<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s'
                '</t></is></c>' % (ref, style, esc(v)))

    def colname(i):
        name = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            name = chr(65 + r) + name
        return name

    # width each column to its widest value, so nothing opens as ####
    widths = []
    for i, head in enumerate(columns):
        longest = max([len(str(head))]
                      + [len(str(r[i])) for r in rows if i < len(r)])
        widths.append('<col min="%d" max="%d" width="%.1f" customWidth="1"/>'
                      % (i + 1, i + 1, min(52, max(9, longest + 2))))
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/'
           'spreadsheetml/2006/main">'
           # keep the headings on screen while scrolling a long month
           '<sheetViews><sheetView workbookViewId="0">'
           '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
           'state="frozen"/></sheetView></sheetViews>'
           '<cols>%s</cols><sheetData>' % "".join(widths)]
    money_cols = {i for i, c in enumerate(columns) if str(c) == "Amount"}
    for n, row in enumerate([columns] + rows, start=1):
        # style 1 = bold header, style 2 = two-decimal money (see _XLSX_STYLES)
        cells = "".join(
            cell(colname(i), n, v,
                 ' s="1"' if n == 1 else (' s="2"' if i in money_cols else ''))
            for i, v in enumerate(row))
        out.append('<row r="%d">%s</row>' % (n, cells))
    out.append("</sheetData></worksheet>")
    return "".join(out)


_XLSX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
    '2006/main">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
    '<cellXfs count="3"><xf xfId="0"/>'
    '<xf xfId="0" fontId="1" applyFont="1"/>'
    # style 2: money, always two decimals — 88.4 in a column of dollars
    # reads as an error to whoever is checking the totals
    '<xf xfId="0" numFmtId="2" applyNumberFormat="1"/></cellXfs>'
    '</styleSheet>')


def write_xlsx(path, sheets):
    """Write a real .xlsx — an OOXML package built by hand, because this app
    has no third-party dependencies. `sheets` is [(name, columns, rows)]."""
    import zipfile
    ns = "http://schemas.openxmlformats.org/"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="%spackage/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/'
                   'vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType='
                   '"application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType='
                   '"application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.styles+xml"/>%s</Types>'
                   % (ns, "".join(
                       '<Override PartName="/xl/worksheets/sheet%d.xml" '
                       'ContentType="application/vnd.openxmlformats-'
                       'officedocument.spreadsheetml.worksheet+xml"/>' % i
                       for i in range(1, len(sheets) + 1))))
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="%spackage/2006/relationships">'
                   '<Relationship Id="rId1" Type="%sofficeDocument/2006/'
                   'relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>' % (ns, ns))
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="%sspreadsheetml/2006/main" '
                   'xmlns:r="%sofficeDocument/2006/relationships"><sheets>%s'
                   '</sheets></workbook>'
                   % (ns, ns, "".join(
                       '<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
                       % (name, i, i)
                       for i, (name, _, _) in enumerate(sheets, start=1))))
        rels = "".join(
            '<Relationship Id="rId%d" Type="%sofficeDocument/2006/'
            'relationships/worksheet" Target="worksheets/sheet%d.xml"/>'
            % (i, ns, i) for i in range(1, len(sheets) + 1))
        rels += ('<Relationship Id="rIdStyles" Type="%sofficeDocument/2006/'
                 'relationships/styles" Target="styles.xml"/>' % ns)
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="%spackage/2006/relationships">%s'
                   '</Relationships>' % (ns, rels))
        z.writestr("xl/styles.xml", _XLSX_STYLES)
        for i, (_, cols, rows) in enumerate(sheets, start=1):
            z.writestr("xl/worksheets/sheet%d.xml" % i,
                       _xlsx_sheet_xml(cols, rows))
    return path


def wd_example_workbook(path):
    """The example .xlsx. Sheet 1 = Balances (the "Grant Budget vs Actuals"
    export most people run, matching its columns exactly); sheet 2 =
    Transactions (the optional drill-down export)."""
    return write_xlsx(path, [
        ("Balances", WD_SUMMARY_COLUMNS, WD_EXAMPLE_SUMMARY_ROWS),
        ("Transactions", WD_DETAIL_COLUMNS, WD_EXAMPLE_DETAIL_ROWS)])


# An import may add to or correct the current month and the one before it
# (a report for last month is normal). Anything older is history: it is held
# back and shown to the person first, because a stale or wrongly picked report
# would otherwise quietly rewrite figures that were settled long ago.
STALE_MONTHS = 1


def wd_month_floor(today=None):
    """'YYYY-MM' of the oldest month an import may touch without asking."""
    t = today or date.today()
    n = t.year * 12 + (t.month - 1) - STALE_MONTHS
    return "%04d-%02d" % (n // 12, n % 12 + 1)


class WDStale(Exception):
    """An import would change records more than a month back. `info` says what
    and how many; nothing has been written."""

    def __init__(self, info):
        super().__init__(info.get("message", "stale import"))
        self.info = info


def _wd_has_history(conn):
    return conn.execute(
        "SELECT 1 WHERE EXISTS (SELECT 1 FROM workday_lines) OR EXISTS "
        "(SELECT 1 FROM workday_balances) OR EXISTS (SELECT 1 FROM expenses "
        "WHERE source IN ('workday','dbr'))").fetchone() is not None


def wd_ingest_file(conn, path, allow_stale=False):
    """Parse one .xlsx export; returns ('detail'|'summary'|'unknown', rows)."""
    headers, rows = parse_xlsx(path)
    try:
        file_date = date.fromtimestamp(os.path.getmtime(path)).isoformat()
    except (OSError, ValueError, OverflowError):
        file_date = None
    return wd_ingest_rows(conn, headers, rows, allow_stale=allow_stale,
                          file_date=file_date)


def wd_ingest_csv(conn, text, allow_stale=False):
    """Parse a RaaS CSV response; returns ('detail'|'summary'|'unknown', rows)."""
    import csv
    import io
    rdr = list(csv.reader(io.StringIO(text)))
    if not rdr:
        return "unknown", 0
    headers = [h.strip() for h in rdr[0]]
    rows = [{headers[i]: (r[i] if i < len(r) else "")
             for i in range(len(headers)) if headers[i]} for r in rdr[1:]]
    return wd_ingest_rows(conn, headers, rows, allow_stale=allow_stale,
                          file_date=_wd_report_date(headers, rows))


WD_REPORT_DATE_COLUMNS = ("as of date", "as of", "through date", "report date")


def _wd_report_date(headers, rows):
    """The date a balances report says it is as of, if it carries one (a
    report built with a fixed 'As Of Date' prompt). A live report has no such
    column and returns None: it is today's snapshot by construction."""
    for h in headers:
        if h.strip().lower() in WD_REPORT_DATE_COLUMNS:
            days = [excel_date(r.get(h)) for r in rows]
            days = [d for d in days if d]
            if days:
                return max(days)
    return None


def _wd_note_grant(conn, grant_full, award="", start="", end=""):
    """Stash the name/award/dates a report carries for a grant code, so the
    dashboard can offer to CREATE that grant (already named and dated) instead
    of only mapping it to one that exists. Never overwrites a good value with
    a blank, so a later dateless transaction report can't wipe the dates a
    balances report gave us."""
    code = _wd_grant_code(grant_full)
    if not code:
        return
    info = get_setting(conn, "wd_grant_info", {}) or {}
    cur = info.get(code, {})
    # the friendly part after the code, if the report spells one out
    rest = grant_full[len(code):].lstrip(" -\u2014|:").strip()
    merged = {
        "code": code,
        "grant_name": grant_full,
        "name": rest or cur.get("name", ""),
        "award": award or cur.get("award", ""),
        "start": start or cur.get("start", ""),
        "end": end or cur.get("end", ""),
    }
    if merged != cur:
        info[code] = merged
        set_setting(conn, "wd_grant_info", info, commit=False)


def wd_ingest_rows(conn, headers, rows, allow_stale=False, file_date=None):
    """Ingest parsed report rows; returns ('detail'|'summary'|'unknown', count).

    Raises WDStale (having written nothing the caller can't roll back) when the
    report would add transactions dated more than a month back, or replace
    balances with an older snapshot than the one already held — unless
    `allow_stale`. `file_date` is when the file was produced, if known."""
    import hashlib
    hset = set(headers)
    floor = wd_month_floor()
    history = _wd_has_history(conn)
    if {"Accounting Date", "Transaction Amount"} <= hset:
        seen, new, stale = {}, 0, []
        for r in rows:
            d = excel_date(r.get("Accounting Date"))
            bd = excel_date(r.get("Budget Date"))
            grant_full = str(r.get("Grant", "")).strip()
            amount = _wd_num(r.get("Transaction Amount"))
            if not grant_full or not d or _wd_is_total(grant_full):
                continue
            worktags = r.get("Worktags", "")
            _wd_note_grant(conn, grant_full,
                           award=str(r.get("Award", "")).strip())
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
            if cur.rowcount and d[:7] < floor:
                stale.append(d)
        if stale and history and not allow_stale:
            raise WDStale({
                "kind": "detail", "count": len(stale),
                "oldest": min(stale), "newest": max(stale), "floor": floor,
                "message": "%d new transaction%s dated %s to %s — more than a "
                           "month back." % (len(stale),
                                            "" if len(stale) == 1 else "s",
                                            min(stale), max(stale))})
        return "detail", new
    if {"Object Class", "Budget", "Available Balance"} <= hset:
        if file_date and history and not allow_stale:
            held = conn.execute("SELECT MAX(as_of) FROM workday_balances"
                                ).fetchone()[0] or ""
            if file_date < held or file_date[:7] < floor:
                raise WDStale({
                    "kind": "summary", "count": 0, "oldest": file_date,
                    "newest": held, "floor": floor,
                    "message": "this balances report was produced on %s%s — "
                               "older than a month or than the figures you "
                               "already have." % (
                                   file_date, (" (you hold figures as of %s)"
                                               % held) if held else "")})
        n = 0
        seen = {}   # grant code -> object classes this report carries
        for r in rows:
            grant_full = str(r.get("Grant", "")).strip()
            if _wd_is_total(grant_full, r.get("Object Class")):  # Total row
                continue
            _wd_note_grant(conn, grant_full,
                           award=str(r.get("Award", "")).strip(),
                           start=excel_date(r.get("Grant Start Date")),
                           end=excel_date(r.get("Grant End Date")))
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
                 min(file_date, date.today().isoformat()) if file_date
                 else date.today().isoformat()))
            seen.setdefault(_wd_grant_code(grant_full), set()).add(
                str(r.get("Object Class", "")).strip())
            n += 1
        # This report is the newer snapshot of every grant it names: a spend
        # category it no longer lists (reclassified away, or zeroed out) must
        # not linger and keep being added to the totals.
        for code, ocs in seen.items():
            conn.execute(
                "DELETE FROM workday_balances WHERE grant_code=? AND "
                "object_class NOT IN (%s)" % ",".join("?" * len(ocs)),
                [code] + sorted(ocs))
        return "summary", n
    raise ValueError(wd_describe_bad_headers(headers))


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


def wd_autolink_grants(conn):
    """Resolve every Workday grant code an import brought in, so the user isn't
    asked to map grants the app can figure out itself.

    A code is linked to an existing grant only when its name matches exactly
    (ignoring case/punctuation/the GR code), or the grant's own name appears in
    the Workday name as a WHOLE WORD and is at least 4 letters long — so a
    grant called "FFAR" is found inside a long Workday title, but one called
    "AI" can't swallow every name containing those letters. Exactly one such
    grant -> link; none -> CREATE the grant from the report's name/dates;
    several -> leave it for the user to choose. Returns {"matched","created"}.
    """
    out = {"matched": 0, "created": 0}
    mapped = {r[0] for r in conn.execute(
        "SELECT wd_key FROM workday_map WHERE kind='grant'")}
    known = rows_to_list(conn.execute(
        "SELECT grant_code, grant_name FROM workday_lines WHERE grant_code!='' "
        "UNION SELECT grant_code, grant_name FROM workday_balances "
        "WHERE grant_code!=''"))
    ginfo = get_setting(conn, "wd_grant_info", {}) or {}
    grants = [g for g in rows_to_list(conn.execute("SELECT id, name FROM grants"))
              if g["name"].strip().lower() != "other"]   # "Other" = external bucket
    for k in known:
        code = k["grant_code"]
        if code in mapped or _wd_is_total(code):
            continue
        info = ginfo.get(code, {})
        full = (info.get("grant_name") or k["grant_name"] or code).strip()
        friendly = (info.get("name") or "").strip()
        cand = {_wd_norm_name(full)}
        if friendly:
            cand.add(_wd_norm_name(friendly))
        cand.discard("")
        raw = (full + " " + friendly).lower()
        hits = set()
        for g in grants:
            n = _wd_norm_name(g["name"])
            if not n:
                continue
            if n in cand:
                hits.add(g["id"])
            elif len(n) >= 4 and re.search(
                    r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(g["name"].strip().lower()),
                    raw):
                hits.add(g["id"])
        if len(hits) > 1:
            continue  # ambiguous -> let the user pick
        if len(hits) == 1:
            gid = next(iter(hits))
            out["matched"] += 1
        else:
            name = friendly or full or code
            notes = ("Workday award %s" % info["award"]) if info.get("award") else ""
            gid = conn.execute(
                "INSERT INTO grants (name, start_date, end_date, status, notes) "
                "VALUES (?,?,?, 'active', ?)",
                (name, info.get("start", ""), info.get("end", ""), notes)).lastrowid
            grants.append({"id": gid, "name": name})
            out["created"] += 1
        conn.execute("INSERT OR REPLACE INTO workday_map (kind, wd_key, target_id) "
                     "VALUES ('grant', ?, ?)", (code, gid))
        mapped.add(code)
    return out


def wd_apply_balances(conn):
    """Reflect an imported Budget-vs-Actuals report in the app's own model, so
    grant cards show real numbers with no manual work.

    A grant's award and budget lines are written from the report (award = the
    summed Budget column, one line per category in year 1 — the report is a
    cumulative whole-grant snapshot) ONLY when they're the app's to write:
    the grant is brand new, or still holds exactly what the last import wrote.
    If you've set or edited the award yourself, it's left alone and reported
    back as a difference to look at — an import must never overwrite work you
    did by hand. Actuals stay in workday_balances; the dashboard shows them as
    'spent'. Returns {"updated": n, "conflicts": [{grant, yours, workday}]}.
    """
    out = {"updated": 0, "conflicts": []}
    gmap = {r["wd_key"]: r["target_id"] for r in conn.execute(
        "SELECT wd_key, target_id FROM workday_map WHERE kind='grant' "
        "AND target_id IS NOT NULL")}
    cmap = {r["wd_key"]: r["target_id"] for r in conn.execute(
        "SELECT wd_key, target_id FROM workday_map WHERE kind='category' "
        "AND target_id IS NOT NULL")}
    per_gid = {}
    for b in rows_to_list(conn.execute("SELECT * FROM workday_balances")):
        gid = gmap.get(b["grant_code"])
        if gid:
            per_gid.setdefault(gid, []).append(b)
    applied = get_setting(conn, "wd_applied", {}) or {}
    wm = conn.execute("SELECT COALESCE(MAX(id), 0) FROM expenses").fetchone()[0]
    for gid, brows in per_gid.items():
        g = conn.execute("SELECT * FROM grants WHERE id=?", (gid,)).fetchone()
        if not g:
            continue
        total_budget = round(sum(b["budget"] or 0 for b in brows), 2)
        if (g["funded_to_date"] or 0) > 0:
            # released in instalments: the report's budget is what has been
            # released so far; the award total and budget lines stay yours
            if total_budget > g["funded_to_date"] + 0.005:
                conn.execute("UPDATE grants SET funded_to_date=? WHERE id=?",
                             (total_budget, gid))
                out["updated"] += 1
            elif total_budget < g["funded_to_date"] - 0.005:
                out["conflicts"].append({"grant": g["name"] + " (released so far)",
                                         "yours": g["funded_to_date"],
                                         "workday": total_budget})
            continue
        mine = g["initial_amount"] or 0
        has_lines = conn.execute("SELECT 1 FROM budget_lines WHERE grant_id=? "
                                 "LIMIT 1", (gid,)).fetchone() is not None
        fresh = mine == 0 and not has_lines
        prev = applied.get(str(gid))
        prev_total = prev.get("total") if isinstance(prev, dict) else prev
        untouched = (prev_total is not None
                     and abs(prev_total - mine) < 0.005)
        if not (fresh or untouched):
            if abs(total_budget - mine) >= 1:
                out["conflicts"].append({"grant": g["name"], "yours": mine,
                                         "workday": total_budget})
            continue
        cat_budget = {}
        for b in brows:
            cid = cmap.get(b["object_class"])
            if cid is None:
                continue
            cid = _wd_category_on_grant(conn, cid, gid)
            if cid is not None:
                cat_budget[cid] = cat_budget.get(cid, 0) + (b["budget"] or 0)
        conn.execute("DELETE FROM budget_lines WHERE grant_id=?", (gid,))
        for cid, amt in cat_budget.items():
            conn.execute("INSERT INTO budget_lines (grant_id, category_id, year, "
                         "amount) VALUES (?,?,1,?)", (gid, cid, amt))
        conn.execute("UPDATE grants SET initial_amount=? WHERE id=?",
                     (total_budget, gid))
        # "wm" = the newest expense id at import time. Anything you add
        # afterwards is a charge Workday hasn't posted yet, so the dashboard
        # adds it on top of Workday's actuals instead of ignoring it.
        applied[str(gid)] = {"total": total_budget, "wm": wm}
        out["updated"] += 1
    set_setting(conn, "wd_applied", applied, commit=False)
    return out


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
    # Workers whose Workday name doesn't match a person by name, but that the
    # user has since resolved: mapped to a person id, or to NULL meaning
    # "not a person / already handled — stop asking".
    wmap = {r["wd_key"].strip().lower(): r["target_id"] for r in conn.execute(
        "SELECT wd_key, target_id FROM workday_map WHERE kind='worker'")}
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
        if person_id is None and worker.lower() in wmap:
            person_id = wmap[worker.lower()]  # resolved earlier (or ignored)
        # payroll buckets by pay period (Budget Date): adjustments often post
        # months after the month they pay for
        month = ((ln["budget_date"] or ln["date"]))[:7]
        is_payroll = bool(worker) and cat_names.get(cid) in ("Personnel", "Fringe")
        if is_payroll and person_id is None:
            # Worker not yet tied to a person. Keep each such line as its own
            # expense: bucketing by (grant, category, month, person_id) would
            # merge two DIFFERENT unnamed workers into one row (they share a
            # NULL person), and naming one would then grab the other's money.
            # Once the user matches the worker, the map endpoint relinks these.
            pd = ln["budget_date"] or ln["date"]
            label = cat_names.get(cid, "Salary")
            cur = conn.execute(
                "INSERT INTO expenses (grant_id, category_id, year, date, "
                "amount, description, person_id, source, salary_month) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (gid, cid, budget_year_for(grant, pd), pd, ln["amount"],
                 "%s — %s (%s)" % (label, worker, month),
                 None, "workday", month))
            eid, st = cur.lastrowid, "created"
        elif is_payroll:
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
            # same grant + same amount + same spend category + within 45 days.
            # Category matters: a $100 airfare and a $100 supplies charge a
            # few weeks apart are two different purchases.
            for ex in conn.execute(
                    "SELECT * FROM expenses WHERE grant_id=? AND source='manual' "
                    "AND (category_id=? OR category_id IS NULL) "
                    "AND ABS(amount-?)<0.01 AND ABS(julianday(date)-julianday(?))<=45 "
                    "ORDER BY ABS(julianday(date)-julianday(?))",
                    (gid, cid, ln["amount"], ln["date"], ln["date"])):
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
    # Renaming whatever arrives to .xlsx (the old behaviour) only moved the
    # failure deeper, where the message was about XML rather than about the
    # file the person actually picked.
    if not fname.lower().endswith(".xlsx"):
        ext = os.path.splitext(fname)[1].lower() or "(no extension)"
        hints = {
            ".xls": "Open it in Excel and use File → Save As → Excel Workbook "
                    "(.xlsx).",
            ".csv": "Run the Workday export again and choose Excel (.xlsx) "
                    "rather than CSV.",
            ".pdf": "Use Workday's “Export to Excel” button, not Print.",
            ".numbers": "Open it in Numbers and use File → Export To → Excel.",
            ".txt": "Run the Workday export again and choose Excel (.xlsx).",
        }
        raise ValueError("“%s” is a %s file — Grants Manager reads Workday's "
                         ".xlsx exports. %s"
                         % (fname, ext, hints.get(ext, "Export the report "
                                                  "from Workday as Excel "
                                                  "(.xlsx).")))
    raw = base64.b64decode(payload["data"])
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("%s is too large (max 20 MB) — is it really an "
                         "Excel export?" % fname)
    os.makedirs(WD_IMPORT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(WD_IMPORT_DIR, f"{stamp}_{fname}")
    with open(path, "wb") as f:
        f.write(raw)
    # Keep the file's own date: how old a report is matters to the import's
    # "more than a month back" check, not when it was uploaded.
    try:
        t = float(payload.get("modified")) / 1000.0
        if 946684800 < t <= time.time():
            os.utime(path, (t, t))
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    return path


def wd_import(conn, allow_stale=False, discard_stale=False):
    """Scan the workday_imports folder, ingest every .xlsx, reconcile.

    A file that would change records more than a month back is left in the
    folder and reported under "stale"; the caller re-runs with allow_stale to
    take it anyway, or discard_stale to set it aside."""
    os.makedirs(WD_IMPORT_DIR, exist_ok=True)
    if allow_stale:
        make_backup(force=True)     # the person chose to reach into the past
    files, new_lines, balance_rows = [], 0, 0
    for name in sorted(os.listdir(WD_IMPORT_DIR)):
        if not name.lower().endswith(".xlsx") or name.startswith("~"):
            continue
        path = os.path.join(WD_IMPORT_DIR, name)
        conn.execute("SAVEPOINT wd_file")
        try:
            kind, n = wd_ingest_file(conn, path, allow_stale=allow_stale)
            conn.execute("RELEASE wd_file")
        except WDStale as e:
            conn.execute("ROLLBACK TO wd_file")
            conn.execute("RELEASE wd_file")
            item = {"file": name, "kind": "stale", "rows": 0, "stale": e.info}
            if discard_stale:
                try:
                    skip = os.path.join(WD_IMPORT_DIR, "skipped")
                    os.makedirs(skip, exist_ok=True)
                    os.replace(path, os.path.join(skip, name))
                    item["kind"] = "skipped"
                except OSError:
                    pass
            files.append(item)
            continue
        except Exception as e:  # noqa: BLE001 — one bad file shouldn't stop the rest
            conn.execute("ROLLBACK TO wd_file")   # drop whatever it half-added
            conn.execute("RELEASE wd_file")
            files.append({"file": name, "kind": "error", "rows": 0,
                          "error": str(e)})
            # Every import rescans this whole folder, so a file left here
            # would report the same failure forever. Set it aside instead of
            # deleting it — the person may have picked the wrong file of two,
            # and it's theirs.
            try:
                reject = os.path.join(WD_IMPORT_DIR, "not-readable")
                os.makedirs(reject, exist_ok=True)
                os.replace(path, os.path.join(reject, name))
            except OSError:
                pass
            continue
        files.append({"file": name, "kind": kind, "rows": n})
        # Done with it: set it aside so a later import can't read this older
        # snapshot again and overwrite newer figures (e.g. from a PDF report).
        try:
            done = os.path.join(WD_IMPORT_DIR, "imported")
            os.makedirs(done, exist_ok=True)
            os.replace(path, os.path.join(done, name))
        except OSError:
            pass
        if kind == "detail":
            new_lines += n
        elif kind == "summary":
            balance_rows += n
    wd_seed_category_maps(conn)
    link = wd_autolink_grants(conn)
    bal = wd_apply_balances(conn)
    conn.commit()
    res = wd_match(conn)
    res.update({"files": files, "new_lines": new_lines,
                "balance_rows": balance_rows,
                "grants_created": link["created"],
                "grants_matched": link["matched"],
                "budget_conflicts": bal["conflicts"],
                "stale": [{"file": f["file"], **f["stale"]}
                          for f in files if f["kind"] == "stale"]})
    return res


# ------------------------------------------------------ PDF text-layer reader
#
# Just enough PDF to read the text a scanner's OCR (or Workday itself) put in:
# plain/Flate content streams, simple fonts and ToUnicode maps. Standard
# library only; anything it cannot decode comes back as "no text" and the
# importer says so plainly.

PDF_MAX_BYTES = 60 * 1024 * 1024
PDF_MAX_STREAM = 40 * 1024 * 1024

# Helvetica advance widths (1/1000 em) for the characters that matter here;
# anything else falls back to the digit width. Only used to find where a word
# ENDS, so right-aligned number columns can be told apart.
_PDF_HELV = {" ": 278, ".": 278, ",": 278, ":": 278, ";": 278, "/": 278, "\\": 278,
         "|": 260, "!": 278, "'": 191, "i": 222, "j": 222, "l": 222, "I": 278,
         "f": 278, "t": 278, "r": 333, "(": 333, ")": 333, "-": 333, "_": 556,
         "m": 833, "w": 722, "M": 833, "W": 944, "$": 556, "%": 889}


def _pdf_char_w(c):
    if c in _PDF_HELV:
        return _PDF_HELV[c]
    if c.isupper():
        return 667
    return 556 if (c.isdigit() or c.islower()) else 556


# ------------------------------------------------------------ PDF objects

class _PdfFile:
    def __init__(self, raw):
        self.raw = raw
        self.pos = {}
        for m in re.finditer(rb"(?<![0-9])(\d+)[ \t\r\n]+(\d+)[ \t\r\n]+obj\b", raw):
            self.pos[int(m.group(1))] = m.end()
        self._cache = {}

    def body(self, num):
        """Raw bytes of object `num` up to (not including) its stream."""
        if num in self._cache:
            return self._cache[num]
        start = self.pos.get(num)
        if start is None:
            return b""
        end_obj = self.raw.find(b"endobj", start)
        s = self.raw.find(b"stream", start, end_obj if end_obj > 0 else None)
        end = s if s >= 0 else (end_obj if end_obj >= 0 else start)
        out = self.raw[start:end]
        self._cache[num] = out
        return out

    def stream(self, num):
        start = self.pos.get(num)
        if start is None:
            return b""
        d = self.body(num)
        s = self.raw.find(b"stream", start + len(d))
        if s < 0:
            return b""
        s += 6
        if self.raw[s:s + 2] == b"\r\n":
            s += 2
        elif self.raw[s:s + 1] in (b"\n", b"\r"):
            s += 1
        e = self.raw.find(b"endstream", s)
        data = self.raw[s:e] if e >= 0 else b""
        if re.search(rb"/FlateDecode|/Fl\b", d):
            try:
                dobj = zlib.decompressobj()
                data = dobj.decompress(data, PDF_MAX_STREAM)
            except zlib.error:
                return b""
        elif re.search(rb"/(DCT|CCITT|JBIG2|JPX|LZW|ASCII85|RunLength)", d):
            return b""
        return data

    def refs(self, blob):
        return [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", blob)]


def _pdf_kids(pdf, num, out, seen):
    if num in seen:
        return
    seen.add(num)
    d = pdf.body(num)
    if re.search(rb"/Type\s*/Pages\b", d) or (b"/Kids" in d and not re.search(rb"/Type\s*/Page\b", d)):
        m = re.search(rb"/Kids\s*\[(.*?)\]", d, re.S)
        for k in (pdf.refs(m.group(1)) if m else []):
            _pdf_kids(pdf, k, out, seen)
    else:
        out.append(num)


def _pdf_inherited(pdf, num, key, depth=0):
    d = pdf.body(num)
    m = re.search(key + rb"\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", d)
    if m:
        return m.group(1), num
    p = re.search(rb"/Parent\s+(\d+)\s+\d+\s+R", d)
    if p and depth < 12:
        return _pdf_inherited(pdf, int(p.group(1)), key, depth + 1)
    return None, num


def _pdf_res_blob(pdf, num, depth=0):
    """Bytes holding the page's /Resources dictionary (own, referenced, or inherited)."""
    d = pdf.body(num)
    m = re.search(rb"/Resources\s*(\d+)\s+\d+\s+R", d)
    if m:
        return pdf.body(int(m.group(1)))
    if re.search(rb"/Resources\s*<<", d):
        return d
    p = re.search(rb"/Parent\s+(\d+)\s+\d+\s+R", d)
    if p and depth < 12:
        return _pdf_res_blob(pdf, int(p.group(1)), depth + 1)
    return b""


def _pdf_fonts(pdf, page_num):
    """name -> {'uni': dict|None, 'wide': bool} for the page's fonts."""
    blob = _pdf_res_blob(pdf, page_num)
    m = re.search(rb"/Font\s*(<<.*?>>|\d+\s+\d+\s+R)", blob, re.S)
    if m:
        blob = pdf.body(int(m.group(1).split()[0])) if m.group(1).endswith(b"R") else m.group(1)
    out = {}
    for name, ref in re.findall(rb"/([A-Za-z0-9_.+-]+)\s+(\d+)\s+\d+\s+R", blob):
        fd = pdf.body(int(ref))
        if b"/Font" not in fd and b"/Subtype" not in fd:
            continue
        info = {"uni": None, "wide": bool(re.search(rb"/Subtype\s*/Type0", fd))}
        m = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", fd)
        if m:
            info["uni"] = _pdf_tounicode(pdf.stream(int(m.group(1))))
        out[name.decode("latin-1")] = info
    return out


def _pdf_tounicode(data):
    cmap = {}
    txt = data.decode("latin-1", "replace")

    def u(h):
        try:
            return bytes.fromhex(h).decode("utf-16-be", "replace")
        except ValueError:
            return ""
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
        for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            cmap[int(a, 16)] = u(b)
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
        for a, b, c in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            base = u(c)
            for i in range(int(a, 16), int(b, 16) + 1):
                if base:
                    cmap[i] = base[:-1] + chr(ord(base[-1]) + i - int(a, 16))
    return cmap


# ------------------------------------------------------ content streams

_PDF_TOK = re.compile(
    rb"\s*(?:(%[^\r\n]*)|(\()|(<<)|(>>)|<([0-9A-Fa-f\s]*)>|(\[)|(\])|"
    rb"(/[^\s/\[\]()<>]*)|([+-]?(?:\d+\.?\d*|\.\d+))|([A-Za-z'\"*][A-Za-z0-9'\"*]*))")


def _pdf_read_string(s, i):
    depth, out = 1, bytearray()
    n = len(s)
    while i < n:
        c = s[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= n:
                break
            e = s[i]
            if e in b"nrtbf":
                out.append({110: 10, 114: 13, 116: 9, 98: 8, 102: 12}[e])
            elif 48 <= e <= 55:
                j, v = i, 0
                while j < n and j < i + 3 and 48 <= s[j] <= 55:
                    v = v * 8 + s[j] - 48
                    j += 1
                out.append(v & 255)
                i = j - 1
            elif e in (10, 13):
                if e == 13 and s[i + 1:i + 2] == b"\n":
                    i += 1
            else:
                out.append(e)
        elif c == 0x28:
            depth += 1
            out.append(c)
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
            out.append(c)
        else:
            out.append(c)
        i += 1
    return bytes(out), n


def _pdf_mul(a, b):
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def _pdf_decode(bs, font):
    if font and font["wide"]:
        step = 2
        codes = [int.from_bytes(bs[i:i + step], "big") for i in range(0, len(bs) - step + 1, step)]
    else:
        codes = list(bs)
    uni = font["uni"] if font else None
    if uni:
        return "".join(uni.get(c, "") for c in codes)
    if font and font["wide"]:
        return ""           # two-byte font with no map: unreadable
    return bytes(codes).decode("cp1252", "replace")


def pdf_page_runs(pdf, page_num):
    """[(x, y, text, size)] for one page, in PDF points (origin bottom-left)."""
    fonts = _pdf_fonts(pdf, page_num)
    d = pdf.body(page_num)
    m = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", d)
    streams = pdf.refs(m.group(1)) if m else []
    data = b"\n".join(pdf.stream(n) for n in streams)
    runs, stack, ops = [], [], []
    ctm, tm, tlm = [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 0, 0]
    font, size, tc, tw, tz, tl, unreadable = None, 10.0, 0.0, 0.0, 1.0, 0.0, 0

    def show(bs):
        nonlocal tm
        txt = _pdf_decode(bs, font)
        if not txt:
            return ""
        t = _pdf_mul(tm, ctm)
        runs.append((t[4], t[5], txt, size * abs(t[3] if t[3] else t[0])))
        w = sum(_pdf_char_w(ch) for ch in txt) / 1000.0 * size + tc * len(txt) + tw * txt.count(" ")
        tm = _pdf_mul([1, 0, 0, 1, w * tz, 0], tm)
        return txt

    i, n = 0, len(data)
    while i < n:
        m = _PDF_TOK.match(data, i)
        if not m:
            i += 1
            continue
        i = m.end()
        g = m.groups()
        if g[0] is not None:
            continue
        if g[1] is not None:
            s, i = _pdf_read_string(data, i)
            ops.append(s)
        elif g[2] is not None or g[3] is not None:
            continue
        elif g[4] is not None:
            h = re.sub(rb"\s", b"", g[4])
            if len(h) % 2:
                h += b"0"
            ops.append(bytes.fromhex(h.decode()))
        elif g[5] is not None:
            ops.append("[")
        elif g[6] is not None:
            j = len(ops) - 1
            while j >= 0 and ops[j] != "[":
                j -= 1
            arr = ops[j + 1:]
            del ops[j:]
            ops.append(("ARR", arr))
        elif g[7] is not None:
            ops.append(("NAME", g[7][1:].decode("latin-1")))
        elif g[8] is not None:
            ops.append(float(g[8]))
        else:
            op = g[9].decode()
            try:
                if op == "q":
                    stack.append(list(ctm))
                elif op == "Q" and stack:
                    ctm = stack.pop()
                elif op == "cm" and len(ops) >= 6:
                    ctm = _pdf_mul([float(x) for x in ops[-6:]], ctm)
                elif op == "BT":
                    tm, tlm = [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 0, 0]
                elif op == "Tf" and len(ops) >= 2:
                    font = fonts.get(ops[-2][1]) if isinstance(ops[-2], tuple) else None
                    size = float(ops[-1])
                elif op == "Tc":
                    tc = float(ops[-1])
                elif op == "Tw":
                    tw = float(ops[-1])
                elif op == "Tz":
                    tz = float(ops[-1]) / 100.0
                elif op == "TL":
                    tl = float(ops[-1])
                elif op == "Tm" and len(ops) >= 6:
                    tm = [float(x) for x in ops[-6:]]
                    tlm = list(tm)
                elif op in ("Td", "TD") and len(ops) >= 2:
                    if op == "TD":
                        tl = -float(ops[-1])
                    tlm = _pdf_mul([1, 0, 0, 1, float(ops[-2]), float(ops[-1])], tlm)
                    tm = list(tlm)
                elif op == "T*":
                    tlm = _pdf_mul([1, 0, 0, 1, 0, -tl], tlm)
                    tm = list(tlm)
                elif op == "Tj" and ops and isinstance(ops[-1], bytes):
                    show(ops[-1])
                elif op in ("'", '"') and ops and isinstance(ops[-1], bytes):
                    tlm = _pdf_mul([1, 0, 0, 1, 0, -tl], tlm)
                    tm = list(tlm)
                    show(ops[-1])
                elif op == "TJ" and ops and isinstance(ops[-1], tuple):
                    buf = []
                    t0 = _pdf_mul(tm, ctm)
                    n0 = len(runs)
                    for el in ops[-1][1]:
                        if isinstance(el, bytes):
                            buf.append(show(el))
                        elif isinstance(el, float):
                            tm = _pdf_mul([1, 0, 0, 1, -el / 1000.0 * size * tz, 0], tm)
                            if el < -250:
                                buf.append(" ")
                    del runs[n0:]
                    joined = "".join(buf)
                    if joined.strip():
                        runs.append((t0[4], t0[5], joined, size * abs(t0[3] if t0[3] else t0[0])))
            except (ValueError, IndexError):
                pass
            ops = []
    return runs


def pdf_read_pages(raw):
    """[{'w','h','runs'}] in reading order, or raises ValueError."""
    if raw[:4] != b"%PDF":
        raise ValueError("not a PDF")
    pdf = _PdfFile(raw)
    root = re.search(rb"/Root\s+(\d+)\s+\d+\s+R", raw)
    pages_ref = None
    if root:
        m = re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", pdf.body(int(root.group(1))))
        pages_ref = int(m.group(1)) if m else None
    leaves = []
    if pages_ref:
        _pdf_kids(pdf, pages_ref, leaves, set())
    if not leaves:
        leaves = [n for n in sorted(pdf.pos)
                  if re.search(rb"/Type\s*/Page\b", pdf.body(n))]
    out = []
    for n in leaves:
        mb, _ = _pdf_inherited(pdf, n, rb"/MediaBox")
        w = h = 0
        if mb:
            v = [float(x) for x in re.findall(rb"[-\d.]+", mb)[:4]]
            if len(v) == 4:
                w, h = v[2] - v[0], v[3] - v[1]
        out.append({"w": w, "h": h, "num": n, "runs": pdf_page_runs(pdf, n)})
    return pdf, out


def pdf_page_jpeg(pdf, page_num):
    """Raw JPEG bytes of the scan drawn on a page, or None."""
    blob = _pdf_res_blob(pdf, page_num)
    m = re.search(rb"/XObject\s*(<<.*?>>|\d+\s+\d+\s+R)", blob, re.S)
    if not m:
        return None
    x = m.group(1)
    if x.endswith(b"R"):
        x = pdf.body(int(x.split()[0]))
    for ref in pdf.refs(x):
        d = pdf.body(ref)
        if b"/DCTDecode" in d and b"/Image" in d:
            start = pdf.pos[ref]
            s0 = pdf.raw.find(b"stream", start + len(d))
            if s0 < 0:
                continue
            s0 += 6
            if pdf.raw[s0:s0 + 2] == b"\r\n":
                s0 += 2
            elif pdf.raw[s0:s0 + 1] in (b"\n", b"\r"):
                s0 += 1
            e = pdf.raw.find(b"endstream", s0)
            data = pdf.raw[s0:e].rstrip(b"\r\n")
            if data[:2] == b"\xff\xd8":
                return data
    return None


# ------------------------------------------------------------ the report

PDF_COLS = ["budget", "commitment", "obligation", "actuals", "available"]
_PDF_DIGITFIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1",
                           "|": "1", "i": "1", "S": "5", "s": "5", "B": "8", "Z": "2"})


def _pdf_words(runs):
    """Runs -> [dict(x0,x1,y,size,t)] one per whitespace-separated word."""
    out = []
    for x, y, txt, sz in runs:
        pos = 0.0
        for m in re.finditer(r"\S+", txt):
            pre = sum(_pdf_char_w(c) for c in txt[:m.start()]) / 1000.0 * sz
            w = sum(_pdf_char_w(c) for c in m.group()) / 1000.0 * sz
            out.append({"x0": x + pre, "x1": x + pre + w, "y": y, "size": sz,
                        "t": m.group()})
    return out


def _pdf_money(t):
    """'3.000.00' / "'10,515.68" / '(1,969.40)' / '0_00' -> float, else None."""
    s = t.strip().replace("'", "").replace("`", "").replace("’", "")
    neg = s.startswith("(") or s.startswith("-") or s.endswith(")")
    s = s.strip("()-$ ")
    if not s or not re.fullmatch(r"[0-9OolIDSB|][0-9OolIDSB|,._ ]*", s):
        return None
    s = s.translate(_PDF_DIGITFIX)
    m = re.fullmatch(r"(\d[\d,._ ]*)[.,_](\d\d)", s)
    if m:
        whole = re.sub(r"\D", "", m.group(1))
        v = int(whole or "0") + int(m.group(2)) / 100.0
    elif s == "0":          # a bare digit other than 0 is a clipped number, not a figure
        v = 0.0
    else:
        return None
    return -v if neg else v


def _pdf_fuzzy(word, target, cut=0.72):
    return difflib.SequenceMatcher(None, re.sub(r"[^a-z]", "", word.lower()), target).ratio() >= cut


def _pdf_lines(words, tol=3.5):
    """Group words into text lines by y."""
    rows = []
    for w in sorted(words, key=lambda w: -w["y"]):
        for r in rows:
            if abs(r["y"] - w["y"]) <= tol:
                r["w"].append(w)
                break
        else:
            rows.append({"y": w["y"], "w": [w]})
    for r in rows:
        r["w"].sort(key=lambda w: w["x0"])
        r["text"] = " ".join(w["t"] for w in r["w"])
    return rows


def _pdf_month_end(y, m):
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


_PDF_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _pdf_period(text):
    """'FY 2026-2027 -Sep' (UA fiscal year runs Jul-Jun) -> '2026-09-30'."""
    m = re.search(r"FY\s*(\d{4})\s*[-–]\s*(\d{4})\s*[-–]?\s*([A-Za-z]{3})", text)
    if not m:
        return ""
    mon = m.group(3).lower()
    best = difflib.get_close_matches(mon, _PDF_MONTHS, n=1, cutoff=0.6)
    if not best:
        return ""
    mi = _PDF_MONTHS.index(best[0]) + 1
    y1, y2 = int(m.group(1)), int(m.group(2))
    return _pdf_month_end(y1 if mi >= 7 else y2, mi)


def _pdf_norm_code(t, prefix):
    s = re.sub(r"[^0-9A-Za-z]", "", t)
    if len(s) < 7:
        return ""
    head, tail = s[:2].upper(), s[2:].translate(_PDF_DIGITFIX)
    if head == prefix and re.fullmatch(r"\d{5,6}", tail):
        return head + tail
    return ""


def pdf_parse_page(page):
    """One page -> report dict, or None if it is not a budget-vs-actual summary."""
    words = _pdf_words(page["runs"])
    if not words:
        return None
    lines = _pdf_lines(words)
    alltext = " ".join(l["text"] for l in lines)
    low = alltext.lower()
    is_proj = "funding" in low and "project" in low
    heads = [w for w in words if _pdf_fuzzy(w["t"], "budget") or _pdf_fuzzy(w["t"], "commitment")
             or _pdf_fuzzy(w["t"], "obligation") or _pdf_fuzzy(w["t"], "actuals")
             or _pdf_fuzzy(w["t"], "available")]
    hit = {h for w in heads for h in PDF_COLS if _pdf_fuzzy(w["t"], h)}
    detail = ("details" in low and "view" in low) or ("transaction" in low and "journal" in low)
    grantish = ("departmental" in low or "grant budget" in low or "budget vs actual" in low
                or "budget vs" in low)
    if detail or not (is_proj or (grantish and len(hit) >= 3)):
        return None
    rep = {"kind": "project" if is_proj else "grant", "problems": [], "rows": [],
           "total": None, "code": "", "award": "", "grant_name": "", "as_of": _pdf_period(alltext)}
    if not rep["as_of"]:
        rep["problems"].append("Could not read the report period (FY … - Month).")
    # --- grant / project code: majority vote over every token that looks like one
    votes = {}
    for w in words:
        c = _pdf_norm_code(w["t"], "PR" if is_proj else "GR")
        if c:
            votes[c] = votes.get(c, 0) + 1
    if votes:
        rep["code"] = max(votes, key=votes.get)
    else:
        rep["problems"].append("Could not read the grant number.")
    for w in words:
        m = re.fullmatch(r"AWD[-_ ]?([0-9OolI]{6})\W*", w["t"])
        if m:
            rep["award"] = "AWD-" + m.group(1).translate(_PDF_DIGITFIX)
            break
    # friendly name = the "Grant:" line after the code
    for l in lines:
        if rep["code"] and rep["code"] in l["text"].replace("O", "0") and ":" in l["text"][:12]:
            rep["grant_name"] = re.sub(r"\s+", " ", l["text"].split(":")[-1]).strip()
            break
    if is_proj:
        return _pdf_parse_project(rep, lines)
    # --- column positions from the header words
    hdr = {}
    for h in PDF_COLS:
        c = [w for w in heads if _pdf_fuzzy(w["t"], h)]
        if c:
            hdr[h] = c
    ys = sorted(w["y"] for ws in hdr.values() for w in ws)
    ymed = ys[len(ys) // 2]
    colx = {}
    for h, ws in hdr.items():
        w = min(ws, key=lambda w: abs(w["y"] - ymed))
        if abs(w["y"] - ymed) < 14:
            colx[h] = (w["x0"] + w["x1"]) / 2
    top = ymed - 8
    # --- money tokens inside the table
    cand = [w for w in words if w["y"] < top and w["y"] > 60]
    cand.sort(key=lambda w: (-round(w["y"]), w["x0"]))
    merged = []
    for w in sorted(cand, key=lambda w: (-w["y"], w["x0"])):
        if merged and abs(merged[-1]["y"] - w["y"]) < 2.0 and w["x0"] - merged[-1]["x1"] < 3.0 \
                and _pdf_money(merged[-1]["t"] + w["t"]) is not None \
                and re.search(r"\d", w["t"]) and re.fullmatch(r"[\d.,_'OolI|]+", w["t"]):
            p = merged[-1]
            merged[-1] = {"x0": p["x0"], "x1": w["x1"], "y": p["y"], "size": p["size"],
                          "t": p["t"] + w["t"]}
        else:
            merged.append(dict(w))
    xmin = min(colx.values()) - 60 if colx else 0
    money = [(w, _pdf_money(w["t"])) for w in merged if w["x1"] > xmin]
    money = [(w, v) for w, v in money if v is not None]
    # --- labels
    labels = []
    objx = [w["x0"] for w in words if _pdf_fuzzy(w["t"], "object")]
    lx0 = (min(objx) - 15) if objx else (xmin - 140)
    lx1 = (min(colx.values()) - 20) if colx else xmin
    lw = [w for w in words if lx0 <= w["x0"] <= lx1 and top > w["y"] > 60]
    lw.sort(key=lambda w: (-w["y"], w["x0"]))
    i = 0
    while i < len(lw):
        w = lw[i]
        t = w["t"]
        if re.fullmatch(r"[0-9OolI]", t) and i + 1 < len(lw) and abs(lw[i + 1]["y"] - w["y"]) < 2 \
                and lw[i + 1]["x0"] - w["x1"] < 4:
            t = t + lw[i + 1]["t"]
            i += 1
        m = re.fullmatch(r"([0-9OolI]{2})[_\-.]?([A-Za-z].*)?", t)
        if m and (m.group(2) or re.fullmatch(r"\d\d", m.group(1))) and len(t) >= 2 and not re.fullmatch(r"\d\d", t):
            nn = m.group(1).translate(_PDF_DIGITFIX)
            labels.append({"nn": nn, "y": w["y"], "x0": w["x0"], "text": m.group(2) or ""})
        i += 1
    for lb in labels:   # add the words of the same / following lines up to the next label
        ext = [w["t"] for w in lw if lb["x0"] - 2 <= w["x0"] and 0 <= lb["y"] - w["y"] < 18
               and w["x0"] > lb["x0"]]
        lb["text"] = (lb["text"] + " " + " ".join(ext)).strip()
    # --- rows = y-clusters of money
    clus = []
    for w, v in sorted(money, key=lambda t: -t[0]["y"]):
        for c in clus:
            if abs(c["y"] - w["y"]) <= 3.5:
                c["m"].append((w, v))
                break
        else:
            clus.append({"y": w["y"], "m": [(w, v)]})

    # Numbers are right-aligned under centred headings: find the one offset that
    # best lines the page's numbers up with the five heading centres.
    def assign(off):
        cost, m = 0.0, {}
        for w, v in money:
            k, d = min(((h, abs(cx + off - w["x1"])) for h, cx in colx.items()),
                       key=lambda kv: kv[1])
            cost += d
            m[id(w)] = k
        return cost, m
    colmap = {}
    if len(colx) >= 4 and money:
        best = min((assign(o) for o in range(0, 60)), key=lambda t: t[0])
        colmap = best[1]

    def colof2(w):
        return colmap.get(id(w))
    totw = [w for w in words if _pdf_fuzzy(w["t"], "total", 0.6) and w["x0"] < xmin and w["y"] < top]
    rows = []
    for c in clus:
        vals = {}
        for w, v in c["m"]:
            k = colof2(w)
            if k and k not in vals:
                vals[k] = v
        is_total = any(abs(t["y"] - c["y"]) < 6 for t in totw)
        rows.append({"y": c["y"], "vals": vals, "total": is_total})
    # attach labels
    used = set()
    for lb in labels:
        best = None
        for idx, r in enumerate(rows):
            if r["total"] or idx in used:
                continue
            d = r["y"] - lb["y"]
            if -4 <= d <= 60 and (best is None or d < best[0]):
                best = (d, idx)
        if best:
            rows[best[1]]["label"] = lb
            used.add(best[1])
        else:
            rows.append({"y": lb["y"], "vals": {}, "total": False, "label": lb})
    rows.sort(key=lambda r: -r["y"])
    for r in rows:
        if r["total"]:
            rep["total"] = {k: r["vals"].get(k) for k in PDF_COLS}
            continue
        lb = r.get("label")
        rep["rows"].append({"nn": lb["nn"] if lb else "", "label": lb["text"] if lb else "",
                            **{k: r["vals"].get(k) for k in PDF_COLS}})
    return rep


def _pdf_parse_project(rep, lines):
    """'Project Budget vs Actual with Funding Sources': the 'Expense' line holds
    Budget, Commitment, Obligation, Actuals, Remaining in that order."""
    for want in ("expense", "total expenses"):
        for l in lines:
            first = " ".join(w["t"] for w in l["w"][:2]).lower()
            if want == "expense" and not _pdf_fuzzy(l["w"][0]["t"], "expense", 0.8):
                continue
            if want == "total expenses" and not re.search(r"total\s*expenses", l["text"], re.I):
                continue
            nums = [_pdf_money(w["t"]) for w in l["w"][1:]]
            nums = [v for v in nums if v is not None]
            if len(nums) == 5:
                rep["rows"].append({"nn": "", "label": "Expenses",
                                    **dict(zip(PDF_COLS, nums))})
                return rep
    rep["rows"].append({"nn": "", "label": "Expenses", **{k: None for k in PDF_COLS}})
    return rep


# ------------------------------------------- Budget-vs-Actual PDF import
#
# The "RPT - Departmental Grant Budget vs Actual" page, as a PDF — usually a
# paper copy run through a scanner. The scanner's text layer is read directly
# (no OCR engine, nothing installed): words come with positions, rows and
# columns are rebuilt from those, and every row is then checked with the
# report's own arithmetic. Anything that does not add up is shown next to the
# scanned page for the person to fix before anything is written.

WD_OC_PREFIX = "UA System Sponsored Programs: "
WD_OC_DEFAULT = {
    "01": "01_Personnel", "03": "03_Fringe Benefits", "06": "06_Tuition",
    "07": "07_Foreign Travel", "08": "08_Domestic Travel", "09": "09_Equipment",
    "10": "10_Materials and Supplies", "12": "12_Publication Costs",
    "17": "17_Other Direct Costs", "18": "18_Facilities and Administration"}
PDF_PROJECT_CLASS = "Project: Expenses"
PDF_SIGN = {"budget": 1, "commitment": -1, "obligation": -1, "actuals": -1,
            "available": -1}
PDF_MAX_PAGES = 400


def _pdf_num(v):
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x) or abs(x) > 1e10:
        return None
    return round(x, 2)


def wd_pdf_known_classes(conn):
    """Two-digit object-class number -> the full key this install already uses
    (e.g. '03' -> 'UA System Sponsored Programs: 03_Fringe Benefits')."""
    keys = {r[0] for r in conn.execute(
        "SELECT object_class FROM workday_balances UNION "
        "SELECT wd_key FROM workday_map WHERE kind='category' UNION "
        "SELECT object_class FROM workday_lines")}
    out = {}
    for k in sorted(x for x in keys if x):
        m = re.search(r":\s*(\d\d)_", k)
        if m:
            out.setdefault(m.group(1), k)
    return out


def _wd_pdf_class_for(known, nn, label):
    if nn in known:
        return known[nn]
    if nn in WD_OC_DEFAULT:
        return WD_OC_PREFIX + WD_OC_DEFAULT[nn]
    clean = re.sub(r"[^A-Za-z &/-]", "", label or "").strip()
    return WD_OC_PREFIX + nn + "_" + (clean or "Other")


def wd_pdf_check(conn, rep):
    """Validate one parsed (or hand-corrected) report. Never writes.

    Returns the report with: rows normalised, blanks solved where the row's own
    arithmetic allows, `problems` (anything that needs a person), `blocking`
    (the subset that must be fixed before import), `warnings`, `ok`, and the
    `plan` of what an import would change."""
    kind = "project" if rep.get("kind") == "project" else "grant"
    code = re.sub(r"[^A-Za-z0-9]", "", str(rep.get("code") or "")).upper()
    as_of = str(rep.get("as_of") or "")
    # the page number and the scan picture come back from the browser: keep
    # only an integer and a JPEG data URL, nothing else is ever echoed
    page_no = rep.get("page")
    page_no = page_no if isinstance(page_no, int) and not isinstance(page_no, bool) else None
    jpeg = rep.get("jpeg")
    if not (isinstance(jpeg, str) and re.fullmatch(r"data:image/jpeg;base64,[A-Za-z0-9+/=]*", jpeg)):
        jpeg = None
    out = {"page": page_no, "kind": kind, "code": code,
           "award": str(rep.get("award") or "")[:40],
           "grant_name": str(rep.get("grant_name") or "")[:200],
           "as_of": as_of, "scope": rep.get("scope") if rep.get("scope") == "all" or (
               isinstance(rep.get("scope"), int) and not isinstance(rep.get("scope"), bool)) else None,
           "jpeg": jpeg,
           "confirmed": bool(rep.get("confirmed"))}
    problems, blocking, warnings = [], [], []

    def bad(msg, hard=True):
        problems.append(msg)
        if hard:
            blocking.append(msg)

    if not re.fullmatch(r"(?:GR|PR)\d{5,6}", code):
        bad("The grant number is missing or unreadable — type it in "
            "(like GR012345).")
    try:
        datetime.strptime(as_of, "%Y-%m-%d")
    except ValueError:
        bad("The report date is missing — enter the last day of the report "
            "month (YYYY-MM-DD).")
    known = wd_pdf_known_classes(conn)
    existing = {r["object_class"]: r for r in conn.execute(
        "SELECT * FROM workday_balances WHERE grant_code=?", (code,))}
    rows = []
    for r in (rep.get("rows") or [])[:60]:
        row = {"nn": re.sub(r"\D", "", str(r.get("nn") or ""))[:2],
               "label": str(r.get("label") or "")[:60],
               "object_class": str(r.get("object_class") or "").strip()[:120],
               "inferred": bool(r.get("inferred"))}
        for k in PDF_COLS:
            row[k] = _pdf_num(r.get(k))
        rows.append(row)
    for row in rows:
        if kind == "project":
            row["object_class"] = PDF_PROJECT_CLASS
        elif not row["object_class"] and row["nn"]:
            row["object_class"] = _wd_pdf_class_for(known, row["nn"], row["label"])
    taken = {x["object_class"] for x in rows if x["object_class"]}
    for row in rows:      # a row with no readable label: match by its budget
        if not row["object_class"] and row["budget"] is not None:
            cand = [oc for oc, b in existing.items()
                    if oc not in taken and abs((b["budget"] or 0) - row["budget"]) < 0.005]
            if len(cand) == 1:
                row["object_class"] = cand[0]
                row["inferred"] = True
                taken.add(cand[0])
    seen = {}
    for row in rows:
        row["problems"], row["calc"] = [], []
        miss = [k for k in PDF_COLS if row[k] is None]
        if len(miss) == 1:     # one blank cell: the row's own arithmetic gives it
            k = miss[0]
            s = sum(PDF_SIGN[j] * row[j] for j in PDF_COLS if j != k)
            row[k] = round(-s / PDF_SIGN[k], 2)
            row["calc"].append(k)
        elif miss:
            row["problems"].append("%d figures could not be read — type them "
                                   "in from the page." % len(miss))
            bad("A row has figures that could not be read.")
        if all(row[k] is not None for k in PDF_COLS):
            diff = round(sum(PDF_SIGN[j] * row[j] for j in PDF_COLS), 2)
            if abs(diff) > 0.011:
                fix = {}
                for k in PDF_COLS:
                    s = sum(PDF_SIGN[j] * row[j] for j in PDF_COLS if j != k)
                    fix[k] = round(-s / PDF_SIGN[k], 2)
                row["fix"] = fix
                row["problems"].append(
                    "Doesn't add up (Budget − Commitment − Obligation − Actuals "
                    "should equal Available; off by %.2f)." % abs(diff))
                bad("A row does not add up.", hard=False)
        if not row["object_class"]:
            row["problems"].append("Choose which spending category this row is.")
            bad("A row has no spending category.")
        elif row["object_class"] in seen:
            row["problems"].append("Same category as another row.")
            bad("Two rows are the same category.")
        seen[row["object_class"]] = True
    if not rows:
        bad("No rows were read from this page.")
    # the report's own Total row, where the scan gave it
    tot = rep.get("total") or {}
    total_ok = False
    if kind == "grant" and rows:
        checked = 0
        for k in PDF_COLS:
            tv = _pdf_num(tot.get(k))
            vals = [r[k] for r in rows]
            if tv is None or any(v is None for v in vals):
                continue
            checked += 1
            if abs(round(sum(vals), 2) - tv) > 0.011:
                bad("The %s column adds up to %.2f here but the report's Total "
                    "says %.2f — a row may be missing or misread."
                    % (k, sum(vals), tv), hard=False)
        total_ok = checked >= 3 and not any("Total" in p for p in problems)
        if not checked:
            warnings.append("The Total row could not be read from the scan, so "
                            "rows were checked one by one only.")
    if kind == "grant" and existing and rows:
        gone = sorted(oc for oc in existing if oc not in seen)
        if gone and not total_ok:
            warnings.append("Your records list %d categor%s this page doesn't: "
                            "%s. They are kept as they are." % (
                                len(gone), "y" if len(gone) == 1 else "ies",
                                ", ".join(g.split(":")[-1].strip() for g in gone)))
    out.update({"rows": rows, "total": {k: _pdf_num(tot.get(k)) for k in PDF_COLS}
                if tot else None, "problems": problems, "blocking": blocking,
                "warnings": warnings, "total_ok": total_ok,
                "ok": not problems})
    out["stale"] = wd_pdf_stale(conn, code, as_of)
    out["plan"] = wd_pdf_plan(conn, out)
    if blocking:   # a half-read table would give a misleading "what changes" list
        out["plan"] = {k: v for k, v in out["plan"].items()
                       if k in ("mapped", "grant_id", "grant_name", "driven", "note")}
        out["plan"]["pending"] = True
    return out


def wd_pdf_stale(conn, code, as_of):
    """Why importing this report would reach more than a month back, or ''."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of or ""):
        return ""
    floor = wd_month_floor()
    held = conn.execute("SELECT MAX(as_of) FROM workday_balances WHERE "
                        "grant_code=?", (code,)).fetchone()[0] or ""
    if as_of[:7] < floor:
        why = "It is a report for %s, more than a month ago" % as_of[:7]
    elif held and as_of < held:
        why = "It is older than the figures you already have"
    else:
        return ""
    if held and as_of < held:
        why += " (you hold figures as of %s)" % held
    return why + ". Importing it would overwrite newer figures and add ledger corrections dated in the past."


def wd_pdf_plan(conn, rep):
    """What importing this report would change in the ledger and budget."""
    code, kind, as_of = rep["code"], rep["kind"], rep["as_of"]
    plan = {"mapped": False}
    if not re.fullmatch(r"(?:GR|PR)\d{5,6}", code or ""):
        return plan
    m = conn.execute("SELECT target_id FROM workday_map WHERE kind='grant' AND "
                     "wd_key=?", (code,)).fetchone()
    g = conn.execute("SELECT * FROM grants WHERE id=?", (m["target_id"],)
                     ).fetchone() if m and m["target_id"] else None
    if not g:
        plan["note"] = ("No grant in the app is linked to %s yet. Importing will "
                        "link it to a grant with the same name, or create one "
                        "from this report." % code)
        return plan
    g = dict(g)
    gid = g["id"]
    plan.update({"mapped": True, "grant_id": gid, "grant_name": g["name"]})
    applied = get_setting(conn, "wd_applied", {}) or {}
    if str(gid) in applied:
        plan["driven"] = True
        plan["note"] = ("This grant's figures come straight from Workday "
                        "reports, so importing just refreshes them.")
        return plan
    rows = [r for r in rep["rows"] if all(r[k] is not None for k in PDF_COLS)]
    years = sorted({r[0] for r in conn.execute(
        "SELECT year FROM budget_lines WHERE grant_id=? UNION "
        "SELECT year FROM expenses WHERE grant_id=?", (gid, gid)) if r[0]})
    byears = sorted({r[0] for r in conn.execute(
        "SELECT year FROM budget_lines WHERE grant_id=?", (gid,))})
    pdf_total = round(sum(r["budget"] for r in rows), 2)
    scope = rep.get("scope")
    if scope in (None, ""):
        scope = (get_setting(conn, "wd_scope", {}) or {}).get(code)
    if scope in (None, ""):
        scope = "all"
        if len(byears) > 1:        # a one-year cycle of a multi-year program?
            tot_all = conn.execute("SELECT COALESCE(SUM(amount),0) FROM "
                                   "budget_lines WHERE grant_id=?", (gid,)).fetchone()[0]
            for y in byears:
                ty = conn.execute("SELECT COALESCE(SUM(amount),0) FROM budget_lines "
                                  "WHERE grant_id=? AND year=?", (gid, y)).fetchone()[0]
                if abs(ty - pdf_total) < 1.0 and abs(tot_all - pdf_total) >= 1.0:
                    scope = y
                    break
    scope = "all" if str(scope).lower() == "all" else (
        int(scope) if str(scope).isdigit() else "all")
    yc, ya = ("", ()) if scope == "all" else (" AND year=?", (scope,))
    # Workday's actuals stop at the report date: what you entered after it is
    # not in them, so it must not be "reconciled" away.
    dc, da = (" AND date<=?", (as_of,)) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of or "") else ("", ())
    ledger = {r[0]: r[1] or 0 for r in conn.execute(
        "SELECT category_id, SUM(amount) FROM expenses WHERE grant_id=?" + yc + dc
        + " GROUP BY category_id", (gid,) + ya + da)}
    bud_app = {r[0]: r[1] or 0 for r in conn.execute(
        "SELECT category_id, SUM(amount) FROM budget_lines WHERE grant_id=?" + yc
        + " GROUP BY category_id", (gid,) + ya)}
    cmap = {r["wd_key"]: r["target_id"] for r in conn.execute(
        "SELECT wd_key, target_id FROM workday_map WHERE kind='category'")}
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM categories")}
    pdf_act, pdf_bud, unmapped = {}, {}, []
    for r in rows:
        if kind == "project":
            if len(bud_app) == 1:
                cid = next(iter(bud_app))
            else:
                p = conn.execute("SELECT id FROM categories WHERE name='Personnel' "
                                 "AND grant_id IS NULL").fetchone()
                cid = p["id"] if p else None
        else:
            oc = r["object_class"]
            cid = cmap[oc] if oc in cmap else _wd_guess_category(conn, oc)
            if cid is not None:
                cid = _wd_category_on_grant(conn, cid, gid)
        if cid is None:
            unmapped.append(r["object_class"].split(":")[-1].strip())
            continue
        pdf_act[cid] = round(pdf_act.get(cid, 0) + r["actuals"], 2)
        pdf_bud[cid] = round(pdf_bud.get(cid, 0) + r["budget"], 2)
    recon = []
    for cid in sorted(set(pdf_act) | ({c for c in ledger if c is not None}
                                      if not unmapped else set())):
        delta = round(pdf_act.get(cid, 0) - (ledger.get(cid) or 0), 2)
        if abs(delta) >= 0.005:
            recon.append({"category_id": cid, "category": names.get(cid, "?"),
                          "workday": pdf_act.get(cid, 0),
                          "yours": round(ledger.get(cid) or 0, 2), "delta": delta})
    up, down = [], []
    if kind == "grant":
        for cid in sorted(set(pdf_bud) | set(bud_app)):
            if cid is None:
                continue
            d = round(pdf_bud.get(cid, 0) - (bud_app.get(cid) or 0), 2)
            item = {"category_id": cid, "category": names.get(cid, "?"),
                    "workday": pdf_bud.get(cid, 0),
                    "yours": round(bud_app.get(cid) or 0, 2), "delta": d}
            if d > 0.005:
                up.append(item)
            elif d < -0.005 and not unmapped:
                down.append(item)
    released = None
    if (g.get("funded_to_date") or 0) > 0:
        # an award released in instalments: what Workday shows is what has been
        # released so far (kept in "Released so far"), not a change to the award
        up, down = [], []
        if kind == "grant" and scope == "all" and rep.get("total_ok") and not unmapped:
            released = {"yours": round(g["funded_to_date"], 2), "workday": pdf_total}
    year = scope if isinstance(scope, int) else budget_year_for(g, as_of)
    plan.update({"scope": scope, "years": years or [1], "year": year,
                 "reconcile": recon, "budget_up": up, "budget_down": down,
                 "budget_default": bool(up) and not down,
                 "released": released,
                 "unmapped": unmapped,
                 "committed": round(sum(r["obligation"] + r["commitment"]
                                        for r in rows), 2)})
    return plan


def _wd_pdf_store(conn, rep):
    """Write one validated report's rows into workday_balances."""
    code = rep["code"]
    prev = conn.execute("SELECT grant_name, award FROM workday_balances WHERE "
                        "grant_code=? LIMIT 1", (code,)).fetchone()
    info = (get_setting(conn, "wd_grant_info", {}) or {}).get(code, {})
    name = (prev["grant_name"] if prev and prev["grant_name"] else
            info.get("grant_name") or
            (code + " " + re.sub(r"\s+I\s+", " | ", rep["grant_name"])).strip())
    award = (prev["award"] if prev and prev["award"] else "") or rep["award"]
    _wd_note_grant(conn, name, award=award)
    for r in rep["rows"]:
        conn.execute(
            "INSERT INTO workday_balances (grant_code, grant_name, award, "
            "object_class, budget, commitment, obligation, actuals, available, "
            "as_of) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(grant_code, object_class) DO UPDATE SET "
            "grant_name=excluded.grant_name, award=excluded.award, "
            "budget=excluded.budget, commitment=excluded.commitment, "
            "obligation=excluded.obligation, actuals=excluded.actuals, "
            "available=excluded.available, as_of=excluded.as_of",
            (code, name, award, r["object_class"], r["budget"], r["commitment"],
             r["obligation"], r["actuals"], r["available"], rep["as_of"]))
    if rep["total_ok"]:     # a verified, complete table: drop categories it no longer lists
        keep = [r["object_class"] for r in rep["rows"]]
        conn.execute("DELETE FROM workday_balances WHERE grant_code=? AND "
                     "object_class NOT IN (%s)" % ",".join("?" * len(keep)),
                     [code] + keep)


def wd_pdf_apply(conn, reports, choices):
    """Import the checked reports. Everything happens in one transaction; a
    safety copy of the database is made first."""
    choices = choices or {}
    res = {"reports": [], "skipped": [], "expenses_added": 0, "budget_added": 0.0}
    accepted = []
    for rep in reports or []:
        c = wd_pdf_check(conn, rep)
        label = c["code"] or "page %s" % c.get("page")
        if c["blocking"]:
            res["skipped"].append({"code": label, "why": c["blocking"][0]})
        elif c.get("stale") and not ((choices.get(c["code"]) or {}).get("allow_stale")
                                     or choices.get("allow_stale")):
            res["skipped"].append({"code": label, "why": "Held back: " + c["stale"],
                                   "stale": True})
        elif c["problems"] and not c["confirmed"]:
            res["skipped"].append({"code": label, "why": "Not confirmed: " + c["problems"][0]})
        else:
            accepted.append(c)
    if not accepted:
        return res
    make_backup(force=True)
    for c in accepted:
        _wd_pdf_store(conn, c)
    wd_seed_category_maps(conn)
    for c in accepted:     # the project report's single row follows its grant's main category
        if c["kind"] != "project":
            continue
        m = conn.execute("SELECT target_id FROM workday_map WHERE kind='grant' AND "
                         "wd_key=?", (c["code"],)).fetchone()
        row = conn.execute("SELECT category_id FROM budget_lines WHERE grant_id=? "
                           "GROUP BY category_id ORDER BY SUM(amount) DESC LIMIT 1",
                           (m["target_id"],)).fetchone() if m and m["target_id"] else None
        if row:
            conn.execute("INSERT OR IGNORE INTO workday_map (kind, wd_key, target_id) "
                         "VALUES ('category', ?, ?)", (PDF_PROJECT_CLASS, row["category_id"]))
    link = wd_autolink_grants(conn)
    wd_apply_balances(conn)
    scopes = get_setting(conn, "wd_scope", {}) or {}
    for c in accepted:
        plan = wd_pdf_plan(conn, c)
        item = {"code": c["code"], "grant": plan.get("grant_name") or c["code"],
                "rows": len(c["rows"]), "reconciled": [], "budget": []}
        res["reports"].append(item)
        if not plan.get("mapped") or plan.get("driven"):
            continue
        ch = choices.get(c["code"]) or {}
        scopes[c["code"]] = plan["scope"]
        gid = plan["grant_id"]
        rel = plan.get("released")
        if rel:
            # an award released in instalments: a bigger budget in the report is
            # a new instalment and is taken; a smaller one is never applied
            # silently (it would shrink what you can spend)
            if rel["workday"] > rel["yours"] + 0.005:
                conn.execute("UPDATE grants SET funded_to_date=? WHERE id=?",
                             (rel["workday"], gid))
                item["released"] = "Released so far %.2f → %.2f" % (rel["yours"], rel["workday"])
            elif rel["workday"] < rel["yours"] - 0.005:
                item["released"] = ("Released so far left at %.2f (the report shows "
                                    "%.2f — edit the grant if that is right)"
                                    % (rel["yours"], rel["workday"]))
        if ch.get("reconcile", True):
            for r in plan["reconcile"]:
                conn.execute(
                    "INSERT INTO expenses (grant_id, category_id, year, date, "
                    "amount, description, source) VALUES (?,?,?,?,?,?, 'dbr')",
                    (gid, r["category_id"], plan["year"], c["as_of"], r["delta"],
                     "Workday report of %s — %s (brings your ledger in line with "
                     "Workday)" % (c["as_of"], r["category"])))
                res["expenses_added"] += 1
                item["reconciled"].append("%s %+.2f" % (r["category"], r["delta"]))
        if ch.get("budget", plan["budget_default"]) and plan["budget_up"]:
            added = 0.0
            for b in plan["budget_up"]:
                cur = conn.execute("SELECT id, amount FROM budget_lines WHERE "
                                   "grant_id=? AND category_id=? AND year=?",
                                   (gid, b["category_id"], plan["year"])).fetchone()
                if cur:
                    conn.execute("UPDATE budget_lines SET amount=? WHERE id=?",
                                 (round(cur["amount"] + b["delta"], 2), cur["id"]))
                else:
                    conn.execute("INSERT INTO budget_lines (grant_id, category_id, "
                                 "year, amount) VALUES (?,?,?,?)",
                                 (gid, b["category_id"], plan["year"], b["delta"]))
                added += b["delta"]
                item["budget"].append("%s %+.2f" % (b["category"], b["delta"]))
            conn.execute("UPDATE grants SET initial_amount=ROUND(COALESCE("
                         "initial_amount,0)+?,2) WHERE id=?", (added, gid))
            res["budget_added"] = round(res["budget_added"] + added, 2)
    set_setting(conn, "wd_scope", scopes, commit=False)
    res["grants_created"] = link["created"]
    set_setting(conn, "workday_last_sync", {
        "date": date.today().isoformat(),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "summary": "PDF report: %d grant%s updated · %d ledger adjustment%s" % (
            len(res["reports"]), "" if len(res["reports"]) == 1 else "s",
            res["expenses_added"], "" if res["expenses_added"] == 1 else "s")},
        commit=False)
    conn.commit()
    return res


def wd_pdf_preview(conn, payload):
    """Upload -> parsed, checked reports (+ the scanned page where a person
    needs to look at it)."""
    raw = base64.b64decode(payload.get("data") or "")
    name = os.path.basename(str(payload.get("name") or "report.pdf"))
    if len(raw) > PDF_MAX_BYTES:
        raise ValueError("%s is too large (max %d MB)." % (name, PDF_MAX_BYTES // 1048576))
    try:
        pdf, pages = pdf_read_pages(raw)
    except Exception as e:  # noqa: BLE001 — a damaged PDF must not become a traceback
        raise ValueError("“%s” couldn't be opened as a PDF (%s)." % (name, e))
    if len(pages) > PDF_MAX_PAGES:
        raise ValueError("That PDF has %d pages — too many to be a grant report."
                         % len(pages))
    if not any(p["runs"] for p in pages):
        raise ValueError(
            "“%s” has no readable text — it's a picture of a page with no "
            "text layer. Re-scan it with “searchable PDF”/OCR switched on, or "
            "use Workday's “Export to Excel” button instead." % name)
    reports, skipped = [], 0
    for i, pg in enumerate(pages):
        rep = pdf_parse_page(pg)
        if rep is None:
            skipped += 1
            continue
        rep["page"] = i + 1
        c = wd_pdf_check(conn, rep)
        if (c["problems"] or c["warnings"]) and len([r for r in reports if r.get("jpeg")]) < 12:
            j = pdf_page_jpeg(pdf, pg["num"])
            if j and len(j) < 6 * 1048576:
                c["jpeg"] = "data:image/jpeg;base64," + base64.b64encode(j).decode()
        reports.append(c)
    if not reports:
        raise ValueError(
            "No “Budget vs Actual” tables were found in “%s” (%d pages read). "
            "This importer reads Workday's RPT - Departmental Grant Budget vs "
            "Actual and Project Budget vs Actual reports." % (name, len(pages)))
    return {"file": name, "pages": len(pages), "skipped_pages": skipped,
            "reports": reports}


# RaaS (Report-as-a-Service): pull the same two reports straight from Workday
# over HTTPS. The password is kept ONLY in this dict (process memory) — never
# written to the database or any file; the user re-types it once per session.
WD_SESSION = {"password": None}


class WDAuthError(RuntimeError):
    pass


WD_HOST_SUFFIXES = (".workday.com", ".myworkday.com", ".workdaysuv.com")
WD_MAX_RESPONSE = 60 * 1024 * 1024


def wd_check_raas_url(url):
    """The Workday password is sent to this URL, so it must be an https
    address on a Workday domain — never plain http, never an arbitrary host
    someone pasted from an email. Returns '' if fine, else the reason."""
    u = urlparse(str(url).strip())
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host:
        return "A Workday report link must start with https://"
    if not any(host.endswith(x) for x in WD_HOST_SUFFIXES):
        return ("%s isn't a Workday address (expected something ending in "
                "myworkday.com or workday.com), so your password won't be "
                "sent there." % host)
    return ""


def wd_fetch_raas(conn, password, allow_stale=False):
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
    if allow_stale:
        make_backup(force=True)
    files = []
    for label, url in pairs:
        url = (url or "").strip()
        if not url:
            continue
        if "format=" not in url:
            url += ("&" if "?" in url else "?") + "format=csv"
        bad = wd_check_raas_url(url)
        if bad:
            raise RuntimeError(bad)
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Basic " + cred)

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):   # never forward the password
                return None
        try:
            with urllib.request.build_opener(_NoRedirect).open(
                    req, timeout=120) as resp:
                body = resp.read(WD_MAX_RESPONSE + 1)
                if len(body) > WD_MAX_RESPONSE:
                    raise RuntimeError("The %s report is far larger than a "
                                       "grant report should be." % label)
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise WDAuthError(
                    "Workday rejected the login (HTTP %d) on the %s report. "
                    "Check the password; if it keeps failing, UARK may only "
                    "allow SSO logins for your account, which blocks RaaS."
                    % (e.code, label))
            if 300 <= e.code < 400:
                raise WDAuthError(
                    "Workday redirected the %s request (probably to a "
                    "single-sign-on page), which blocks direct connection. "
                    "Use the file import instead." % label)
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
        conn.execute("SAVEPOINT wd_raas")
        try:
            kind, n = wd_ingest_csv(conn, text, allow_stale=allow_stale)
            conn.execute("RELEASE wd_raas")
        except WDStale as e:
            conn.execute("ROLLBACK TO wd_raas")
            conn.execute("RELEASE wd_raas")
            files.append({"file": "RaaS %s" % label, "kind": "stale", "rows": 0,
                          "stale": e.info})
            continue
        files.append({"file": "RaaS %s" % label, "kind": kind, "rows": n})
        if kind == "unknown":
            files[-1]["error"] = ("Response didn't look like the expected "
                                  "report (missing the standard columns).")
    wd_seed_category_maps(conn)
    link = wd_autolink_grants(conn)
    bal = wd_apply_balances(conn)
    conn.commit()
    res = wd_match(conn)
    res["files"] = files
    res["stale"] = [{"file": f["file"], **f["stale"]} for f in files
                    if f["kind"] == "stale"]
    res["budget_conflicts"] = bal["conflicts"]
    res["grants_created"] = link["created"]
    res["grants_matched"] = link["matched"]
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
    line has linked yet (i.e. not visible as posted) and not marked 'na' (not
    a Workday expense) or 'done' (the user says it's already been entered).
    """
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    linked = {r[0] for r in conn.execute(
        "SELECT expense_id FROM workday_lines WHERE expense_id IS NOT NULL")}
    queue = [e for e in rows_to_list(conn.execute(
        "SELECT e.id, e.date, e.amount, e.description, e.grant_id, "
        "e.category_id, e.receipt_path, e.extra_files, e.wd_entry, e.wd_worktag, "
        "c.name AS category, p.name AS person, g.name AS grant_name "
        "FROM expenses e "
        "LEFT JOIN categories c ON c.id=e.category_id "
        "LEFT JOIN people p ON p.id=e.person_id "
        "JOIN grants g ON g.id=e.grant_id "
        "WHERE e.source='manual' AND e.date>=? AND (e.wd_entry IS NULL OR "
        "e.wd_entry NOT IN ('na','done')) ORDER BY e.date DESC", (cutoff,)))
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


# Kept in Application Support, not in data/: data/ is inside the OneDrive
# folder, and an .app bundle that a sync client rewrites (or copies to another
# Mac) loses the code identity macOS remembers the Automation approval against.
MAIL_HELPER_DIR = os.path.expanduser(
    "~/Library/Application Support/Grants Manager")
MAIL_HELPER_APP = os.path.join(MAIL_HELPER_DIR, "Grants Manager.app")
MAIL_JOB_PATH = os.path.join(MAIL_HELPER_DIR, "outlook_job.applescript")
MAIL_RESULT_PATH = os.path.join(MAIL_HELPER_DIR, "outlook_job.result")

# The applet's whole job: read the script we just wrote, run it, write down
# what happened. It is compiled once, on this machine, with the paths baked in
# — nothing inside the bundle is ever rewritten afterwards, because changing a
# bundle's contents changes its code hash and macOS would then treat it as a
# different app and ask for Automation permission all over again.
_MAIL_HELPER_SRC = '''on run
\tset jobFile to "%(job)s"
\tset outFile to "%(out)s"
\ttry
\t\tset src to (read (POSIX file jobFile) as «class utf8»)
\t\trun script src
\t\tset res to "OK"
\ton error errMsg
\t\tset res to "ERR " & errMsg
\tend try
\ttry
\t\tset fh to open for access (POSIX file outFile) with write permission
\t\tset eof fh to 0
\t\twrite res to fh as «class utf8»
\t\tclose access fh
\tend try
end run'''


def mac_mail_helper():
    """Path to a tiny app bundle named “Grants Manager”, built on first use.

    macOS attributes an Automation prompt to the app *responsible* for the
    Apple Event, and a detached `python3` started by a launcher script is not
    a recognisable app — so the alert ends up naming whatever happened to
    start the server: “Terminal”, “python3”, or the editor a developer ran it
    from. People are being asked to let an unfamiliar program read their mail,
    which is exactly the prompt they should refuse.

    Sending through an applet of our own fixes the attribution at the source:
    the event now comes from a bundle whose name really is Grants Manager, so
    that is what the prompt says, and the approval it records is one the user
    can find and revoke under Privacy & Security → Automation.

    Returns None if the bundle can't be built (then we fall back to plain
    osascript, which still sends — just with a vaguer prompt).
    """
    import subprocess
    plist = os.path.join(MAIL_HELPER_APP, "Contents", "Info.plist")
    if os.path.isdir(MAIL_HELPER_APP) and os.path.isfile(plist):
        return MAIL_HELPER_APP
    try:
        os.makedirs(MAIL_HELPER_DIR, exist_ok=True)
        if os.path.isdir(MAIL_HELPER_APP):
            shutil.rmtree(MAIL_HELPER_APP)
        src = _MAIL_HELPER_SRC % {"job": MAIL_JOB_PATH, "out": MAIL_RESULT_PATH}
        r = subprocess.run(["osacompile", "-o", MAIL_HELPER_APP, "-e", src],
                           capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.isfile(plist):
            return None
        # LSUIElement keeps it out of the Dock; the usage string is what the
        # permission alert shows underneath the app name.
        subprocess.run(["defaults", "write", plist, "LSUIElement", "-bool",
                        "true"], capture_output=True, timeout=30)
        subprocess.run(["defaults", "write", plist,
                        "NSAppleEventsUsageDescription", "-string",
                        "Grants Manager uses Microsoft Outlook to send your "
                        "expense report to you, with the receipts attached."],
                       capture_output=True, timeout=30)
        subprocess.run(["defaults", "write", plist, "CFBundleName", "-string",
                        "Grants Manager"], capture_output=True, timeout=30)
        # ad-hoc signature so macOS has a stable identity to remember the
        # Automation approval against, instead of re-asking every launch
        subprocess.run(["codesign", "--force", "--sign", "-", MAIL_HELPER_APP],
                       capture_output=True, timeout=60)
        return MAIL_HELPER_APP
    except (OSError, subprocess.SubprocessError):
        return None


def run_applescript_as_app(lines, timeout=90):
    """Run an AppleScript through the “Grants Manager” helper applet.

    Returns True if the helper ran it, False if there is no helper to run it
    with (caller falls back to osascript). Raises RuntimeError with Outlook's
    own words if the script itself failed.
    """
    import subprocess
    app = mac_mail_helper()
    if not app:
        return False
    try:
        with open(MAIL_JOB_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        if os.path.exists(MAIL_RESULT_PATH):
            os.remove(MAIL_RESULT_PATH)
        subprocess.run(["open", "-n", "-a", app], capture_output=True,
                       timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isfile(MAIL_RESULT_PATH):
            with open(MAIL_RESULT_PATH, encoding="utf-8",
                      errors="replace") as f:
                res = f.read().strip()
            os.remove(MAIL_RESULT_PATH)
            if res.startswith("OK"):
                return True
            raise RuntimeError(res[4:].strip() or "unknown error")
        time.sleep(0.25)
    raise RuntimeError("Outlook did not respond in time")


class OutlookUnavailable(RuntimeError):
    """Outlook can't be driven here (the new Outlook has no automation, or it
    isn't installed). Not a failure: the caller prepares an outbox instead."""


OUTBOX_DIR = os.path.join(DATA_DIR, "outbox")


def reveal_folder(path):
    """Show a folder in Explorer / Finder. Best effort."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 — a folder we just created
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


def prepare_outbox(to, subject, body, files, label):
    """When the email can't be sent for the person, do everything except press
    Send: one folder holding the attachments (already named for the
    accountant) and the email text, opened on screen, plus a mailto: draft.
    Returns (folder, mailto_url)."""
    from urllib.parse import quote
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    folder = os.path.join(OUTBOX_DIR, "%s_%s" % (
        datetime.now().strftime("%Y%m%d_%H%M%S"), slugify(label, 24)))
    os.makedirs(folder, exist_ok=True)
    for f in files:
        if f and os.path.isfile(f):
            shutil.copy2(f, os.path.join(folder, os.path.basename(f)))
    with open(os.path.join(folder, "0 - email text.txt"), "w",
              encoding="utf-8-sig", newline="\r\n") as f:
        f.write("To: %s\nSubject: %s\n\n%s\n" % (to, subject, body))
    # keep only the most recent few outboxes
    olds = sorted(d for d in os.listdir(OUTBOX_DIR)
                  if os.path.isdir(os.path.join(OUTBOX_DIR, d)))
    for d in olds[:-20]:
        shutil.rmtree(os.path.join(OUTBOX_DIR, d), ignore_errors=True)
    reveal_folder(folder)
    mailto = "mailto:%s?subject=%s&body=%s" % (
        quote(to, safe="@,"), quote(subject), quote(body[:1500]))
    return folder, mailto


def wd_send_mail(to, cc, subject, body, attachment=None):
    """Compose and send through Microsoft Outlook — macOS (AppleScript) or
    Windows (Outlook COM via PowerShell). Outlook lets us attach the receipt
    PDF, which a mailto: link cannot. First use on a Mac asks macOS for
    permission to control Outlook.

    `attachment` may be a single path or a list of paths. Bodies are plain
    text: the monthly report is a tab-separated table meant to be pasted into
    a spreadsheet, and HTML would fight that.
    """
    import subprocess
    attachments = ([attachment] if isinstance(attachment, str)
                   else list(attachment or []))
    attachments = [a for a in attachments if a and os.path.isfile(a)]
    if sys.platform == "darwin":
        def q(s):  # AppleScript string literal escaping
            return str(s).replace("\\", "\\\\").replace('"', '\\"')
        content = 'plain text content:"%s"' % q(body)
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
        hint = ("Check that Microsoft Outlook is installed and signed in, and "
                "that Grants Manager is allowed to control it (System Settings "
                "→ Privacy & Security → Automation → Grants Manager). If you "
                "use “New Outlook”, enable AppleScript support or use "
                "“Copy as text” instead.")
        # Preferred path: send from our own “Grants Manager” applet, so the
        # macOS permission alert names this app rather than whatever process
        # happens to be hosting the server.
        try:
            if run_applescript_as_app(lines):
                return
        except RuntimeError as e:
            raise RuntimeError("Outlook could not send: %s. %s" % (e, hint))
        args = ["osascript"]
        for ln in lines:
            args += ["-e", ln]
    elif sys.platform == "win32":
        # Everything variable travels in a JSON file (pure ASCII, \\u escapes),
        # so a long body or hundreds of attachments can't hit Windows'
        # command-line limit, and no user text is ever spliced into script code.
        import tempfile
        tmpd = tempfile.mkdtemp(prefix="gm_mail_")
        pj = os.path.join(tmpd, "mail.json")
        with open(pj, "w", encoding="ascii") as f:
            json.dump({"to": to, "cc": cc or "", "subject": subject,
                       "body": body.replace("\r", ""),
                       "attachments": attachments}, f)
        script = ("$ErrorActionPreference='Stop'; "
                  "$d = Get-Content -Raw -Encoding UTF8 -LiteralPath '%s' | "
                  "ConvertFrom-Json; "
                  "$o = New-Object -ComObject Outlook.Application; "
                  "$m = $o.CreateItem(0); $m.To = [string]$d.to; "
                  "if ($d.cc) { $m.CC = [string]$d.cc }; "
                  "$m.Subject = [string]$d.subject; $m.Body = [string]$d.body; "
                  "foreach ($a in @($d.attachments)) { if ($a) { "
                  "$null = $m.Attachments.Add([string]$a) } }; $m.Send()"
                  % pj.replace("'", "''"))
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        hint = "Check that Microsoft Outlook (desktop) is installed and signed in."
    else:
        raise OutlookUnavailable("Sending needs Microsoft Outlook on macOS or "
                                 "Windows.")
    else_kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    try:
        r = subprocess.run(args, capture_output=True, timeout=90, **else_kw)
    except FileNotFoundError:
        raise OutlookUnavailable("Could not talk to Outlook on this system. "
                                 + hint)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Outlook did not respond. " + hint)
    finally:
        if sys.platform == "win32":
            shutil.rmtree(tmpd, ignore_errors=True)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        if sys.platform == "win32" and any(
                m in err for m in ("80040154", "Class not registered",
                                   "COM class factory", "Outlook.Application")):
            # "New Outlook" (and Outlook-less PCs) have no automation object
            raise OutlookUnavailable("Classic Outlook isn't installed.")
        raise RuntimeError("Outlook could not send: %s. %s"
                           % (err or "unknown error", hint))


# ------------------------------------------------------- monthly report
#
# Goes to the ACCOUNT OWNER, not the accountant: the owner checks it and
# forwards it on. Columns are named the way Workday names them so the
# accountant can key them in (or import the attached CSV) without translating.

REPORT_COLUMNS = ["Date", "Amount", "Spend Category", "Business Purpose",
                  "Grant / Worktag", "Award", "Cost Center", "Fund",
                  "Person", "Receipt"]

# The p-card block, appended to the right of the ordinary columns and blank on
# any expense that was not a card purchase, so one sheet still covers the whole
# month. The cost centre is already in "Cost Center" above, and the reason for
# the purchase is already in "Business Purpose" — no point printing either twice.
PCARD_COLUMNS = ["P-card", "Print cardholder's name", "Name on the P-card",
                 "Purchased by (if different from cardholder)"]

REPORT_XLSX_COLUMNS = REPORT_COLUMNS + PCARD_COLUMNS


def month_bounds(month):
    """'YYYY-MM' -> (first_day, last_day) as ISO strings."""
    # validated explicitly: `month` reaches a filename in report_xlsx_path(),
    # so anything path-shaped must never get that far
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(month or "")):
        raise ValueError("Month must look like 2026-07")
    y, mo = int(month[:4]), int(month[5:7])
    first = date(y, mo, 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def receipt_export_name(n, width, row):
    ext = os.path.splitext(row["_receipt_path"])[1].lower()
    purpose = re.sub(r"[^A-Za-z0-9 ._()-]+", " ",
                     row["Business Purpose"] or row["Spend Category"]
                     or row["_grant"] or "")
    purpose = re.sub(r"\s+", " ", purpose).strip(" .-")[:40].strip(" .-") \
        or "receipt"
    amt = "%.2f" % abs(row["_amount"]) + (" credit" if row["_amount"] < 0 else "")
    return "%0*d - %s - %s - %s%s" % (width, n, row["Date"], amt, purpose, ext)


def extra_export_name(n, width, k, row, x):
    """Name for an additional document: same line number as its expense plus
    '(+k)', and the original file's own name so the accountant can see WHAT it
    is (e.g. '03 (+1) - 2026-09-15 - 412.75 - Lab supplies - P-card form.pdf')."""
    ext = os.path.splitext(x["path"])[1].lower()
    purpose = re.sub(r"[^A-Za-z0-9 ._()-]+", " ",
                     row["Business Purpose"] or row["Spend Category"] or "")
    purpose = re.sub(r"\s+", " ", purpose).strip(" .-")[:30].strip(" .-") or "expense"
    stem = re.sub(r"[^A-Za-z0-9 ._()-]+", " ", os.path.splitext(x["name"])[0])
    stem = re.sub(r"\s+", " ", stem).strip(" .-")[:30].strip(" .-") or "document"
    amt = "%.2f" % abs(row["_amount"]) + (" credit" if row["_amount"] < 0 else "")
    return "%0*d (+%d) - %s - %s - %s - %s%s" % (width, n, k, row["Date"], amt,
                                                 purpose, stem, ext)


REPORT_SELECT = (
    "SELECT e.*, c.name AS category, p.name AS person, g.name AS grant_name "
    "FROM expenses e LEFT JOIN categories c ON c.id=e.category_id "
    "LEFT JOIN people p ON p.id=e.person_id "
    "JOIN grants g ON g.id=e.grant_id ")


def report_rows(conn, month):
    """Every expense dated inside `month` (bar imported actuals), shaped for an accountant."""
    start, end = month_bounds(month)
    return _shape_report_rows(conn, conn.execute(
        REPORT_SELECT + "WHERE e.date>=? AND e.date<=? "
        # imported actuals and ledger adjustments are already in Workday
        "AND e.source NOT IN ('dbr','adjust') ORDER BY g.name, e.date",
        (start, end)))


def bundle_rows(conn, ids):
    """The chosen expenses (only ones entered by hand — imported Workday
    charges are already in Workday), shaped exactly like the monthly report."""
    ids = sorted({int(i) for i in ids})[:300]
    if not ids:
        return []
    return _shape_report_rows(conn, conn.execute(
        REPORT_SELECT + "WHERE e.source='manual' AND e.id IN (%s) "
        "ORDER BY g.name, e.date, e.id" % ",".join("?" * len(ids)), ids))


def _shape_report_rows(conn, expenses):
    ps = wd_push_state(conn)
    card = get_setting(conn, "pcard", {}) or {}
    out = []
    for e in expenses:
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
            # p-card block: only filled in when the purchase was on a card, so
            # the accountant can see at a glance which rows need a form
            "P-card": "Yes" if e["pcard"] else "",
            "Print cardholder's name": ((e["pcard_holder"] or "").strip()
                                        or card.get("cardholder", ""))
                                       if e["pcard"] else "",
            "Name on the P-card": card.get("card_name", "") if e["pcard"] else "",
            "Purchased by (if different from cardholder)":
                (e["pcard_buyer"] or "") if e["pcard"] else "",
            "_amount": e["amount"],
            "_grant": e["grant_name"],
            "_receipt_path": e["receipt_path"] or "",
            "_extras": expense_files(e["extra_files"]),
            "_pcard": bool(e["pcard"]),
            "_manual": e["source"] == "manual",
        })
    # Name each receipt so the accountant can tell which row it belongs to
    # without opening it: "<line> - <date> - <amount> - <purpose>.pdf". The
    # line number is the row's position in the table (1 = first expense), and
    # the Receipt cell shows this same name, so the file and the row always
    # match — in the preview, the workbook and the attachments alike.
    width = max(2, len(str(len(out))))
    for n, r in enumerate(out, 1):
        files = []
        if r["_receipt_path"]:
            files.append((r["_receipt_path"], receipt_export_name(n, width, r)))
        for k, x in enumerate(r["_extras"], 1):
            files.append((x["path"], extra_export_name(n, width, k, r, x)))
        r["_files"] = files
        r["Receipt"] = "; ".join(name for _, name in files)
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
                                if not r["_receipt_path"] and r["_manual"]),
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


def report_xlsx_path(data):
    """The month's expenses as a real Excel workbook.

    Two sheets: every expense on the first, and only the p-card purchases on
    the second, because those are the rows that need a reconciliation form and
    nobody wants to filter for them by hand.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    return write_expense_workbook(
        os.path.join(REPORTS_DIR, "expenses_%s.xlsx" % data["month"]),
        data["rows"], data["changes"])


def write_expense_workbook(path, rows, changes=()):
    """Expenses -> .xlsx: the 14 agreed columns, one expense per line, the
    receipt file name(s) in the Receipt column. A second sheet lists just the
    P-card purchases when there are any."""
    data = {"rows": rows, "changes": list(changes)}

    def row_of(r):
        out = []
        for c in REPORT_XLSX_COLUMNS:
            v = r.get(c, "")
            # amounts go in as numbers so the accountant can sum the column
            out.append(round(r["_amount"], 2) if c == "Amount" else csv_safe(v))
        return out

    sheets = [("Expenses", REPORT_XLSX_COLUMNS,
               [row_of(r) for r in data["rows"]])]
    pcard = [r for r in data["rows"] if r.get("_pcard")]
    if pcard:
        cols = ["Date", "Amount", "Business Purpose", "Grant / Worktag",
                "Receipt"] + PCARD_COLUMNS[1:]
        sheets.append(("P-card purchases", cols,
                       [[round(r["_amount"], 2) if c == "Amount"
                         else csv_safe(r.get(c, "")) for c in cols]
                        for r in pcard]))
    if data["changes"]:
        sheets.append(("Changes this month",
                       ["When", "Action", "Description", "Detail"],
                       [[c["at"].replace("T", " ")[:16], c["action"],
                         csv_safe(c["descr"] or ""), csv_safe(c["detail"] or "")]
                        for c in data["changes"]]))
    return write_xlsx(path, sheets)


# A message is roughly a third bigger once encoded for email, and most servers
# refuse anything past ~25 MB — so budget the RAW attachments (workbook included)
# well under that, and fall back to a zip, then to a ready-to-send folder.
RECEIPT_ATTACH_LIMIT = 13 * 1024 * 1024


def _stage_row_files(rows, stage):
    """Copy every receipt/document of `rows` into `stage` under its export
    name, and write the final names back into each row's Receipt cell (a file
    that can't be found is marked "(file missing)"). Returns
    (staged_paths, total_bytes, missing_count)."""
    if os.path.isdir(stage):
        shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)
    used, staged, total, missing = set(), [], 0, 0
    for r in rows:
        shown = []
        for rel, name in r["_files"]:
            full = receipt_abspath(rel) or ""
            if not os.path.isfile(full):
                shown.append("(file missing)")
                missing += 1
                continue
            while name.lower() in used:   # Windows names ignore case
                stem, ext = os.path.splitext(name)
                name = stem + "+" + ext
            used.add(name.lower())
            dest = os.path.join(stage, name)
            shutil.copy2(full, dest)
            shown.append(name)            # keep the cell and the attachment in step
            staged.append(dest)
            total += os.path.getsize(dest)
        r["Receipt"] = "; ".join(shown)
        r["_nattached"] = sum(1 for x in shown if x != "(file missing)")
    return staged, total, missing


def report_receipt_attachments(data, reserve=1024 * 1024):
    """Each receipt as its own attachment, named exactly as the Receipt column
    names it (see receipt_export_name), so a row in the table can be matched to
    a file by eye. Returns (attach, note, staged): `attach` is what to email
    (the individual files, or one zip if they'd be too big together), `staged`
    always the individual renamed files, and `note` a line for the email when
    something was zipped or missing. `reserve` is room kept for the workbook.
    """
    rows = [r for r in data["rows"] if r["_files"]]
    if not rows:
        return [], "", []
    staged, total, missing = _stage_row_files(
        rows, os.path.join(REPORTS_DIR, "receipts_%s" % data["month"]))
    note = ""
    if missing:
        note = ("%d receipt file(s) recorded in the app could not be found on "
                "disk and are marked “(file missing)” in the table."
                % missing)
    if total + reserve > RECEIPT_ATTACH_LIMIT:
        import zipfile
        out = os.path.join(REPORTS_DIR, "receipts_%s.zip" % data["month"])
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for pth in staged:
                z.write(pth, os.path.basename(pth))
        note = (("%s " % note if note else "")
                + "The %d receipts came to %.0f MB, too much for one email, so "
                  "they are attached as a single zip instead — the names "
                  "inside it still match the Receipt column."
                  % (len(staged), total / 1048576.0))
        return [out], note, staged
    return staged, note, staged


# ------------------------------------------------- expense bundle (one email)
#
# Several expenses -> ONE email: a spreadsheet with a line per expense (the
# receipt's file name in the Receipt column) and ONE zip holding every receipt
# and supporting document under exactly those names.

BUNDLES_DIR = os.path.join(DATA_DIR, "bundles")


def bundle_names(n_expenses, n_files):
    day = date.today().isoformat()
    return ("Workday expenses - %s (%d expense%s).xlsx"
            % (day, n_expenses, "" if n_expenses == 1 else "s"),
            "Receipts - %s (%d file%s).zip" % (day, n_files, "" if n_files == 1 else "s"))


def bundle_text(rows, xlsx_name, zip_name, n_files, missing, owner_name=""):
    total = sum(r["_amount"] for r in rows)
    grants = sorted({r["_grant"] for r in rows})
    lines = ["Hi,", "",
             "Please enter the %d expenses in the attached spreadsheet in "
             "Workday — one expense per line, %s in total%s."
             % (len(rows), money_str(total),
                " (%s)" % grants[0] if len(grants) == 1
                else " across %d grants" % len(grants)),
             "", "Spreadsheet: %s" % xlsx_name]
    if n_files:
        lines += ["Receipts and supporting documents: %s (%d file%s zipped "
                  "together). The Receipt column on each line gives that "
                  "file's name inside the zip; the number at the start of a "
                  "name is the line in the spreadsheet."
                  % (zip_name, n_files, "" if n_files == 1 else "s")]
    else:
        lines += ["There are no receipts for these expenses."]
    pcard = sum(1 for r in rows if r.get("_pcard"))
    if pcard:
        lines.append("%d of them were P-card purchases (the spreadsheet has a "
                     "“P-card purchases” sheet listing just those)." % pcard)
    if missing:
        lines.append("%d file(s) recorded in the app could not be found and "
                     "are marked “(file missing)”." % missing)
    nore = sum(1 for r in rows if not r["_receipt_path"])
    if nore:
        lines.append("%d expense(s) have no main receipt attached." % nore)
    lines += ["", "Thank you!"]
    return "\n".join(lines)


def bundle_preview(conn, ids):
    """What a bundle WOULD contain — read-only, nothing is written."""
    rows = bundle_rows(conn, ids)
    if not rows:
        raise ValueError("Pick expenses you entered yourself — imported "
                         "Workday charges are already in Workday.")
    files = size = missing = 0
    for r in rows:
        for rel, _name in r["_files"]:
            ap = receipt_abspath(rel)
            if ap:
                files += 1
                size += os.path.getsize(ap)
            else:
                missing += 1
    xlsx_name, zip_name = bundle_names(len(rows), files)
    total = sum(r["_amount"] for r in rows)
    grants = sorted({r["_grant"] for r in rows})
    return {
        "rows": [{"line": n, "date": r["Date"], "grant": r["Grant / Worktag"],
                  "purpose": r["Business Purpose"], "amount": r["_amount"],
                  "receipt": r["Receipt"], "pcard": bool(r["_pcard"])}
                 for n, r in enumerate(rows, 1)],
        "count": len(rows), "total": total, "files": files, "missing": missing,
        "size_mb": round(size / 1048576.0, 1),
        "too_big": size + 1048576 > RECEIPT_ATTACH_LIMIT,
        "xlsx_name": xlsx_name, "zip_name": zip_name,
        "subject": "Workday expense entries — %d expenses — %s — %s" % (
            len(rows), money_str(total),
            grants[0] if len(grants) == 1 else "%d grants" % len(grants)),
        "body": bundle_text(rows, xlsx_name, zip_name, files, missing),
        "ids": [int(i) for i in ids],
    }


def build_bundle(conn, ids):
    """Write the spreadsheet and the zip. Returns (rows, xlsx_path, zip_path or
    None, n_files, missing)."""
    import zipfile
    rows = bundle_rows(conn, ids)
    if not rows:
        raise ValueError("Pick expenses you entered yourself — imported "
                         "Workday charges are already in Workday.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = os.path.join(BUNDLES_DIR, stamp)
    os.makedirs(bdir, exist_ok=True)
    stage = os.path.join(bdir, "_receipts")
    staged, _total, missing = _stage_row_files(rows, stage)   # names -> rows
    xlsx_name, zip_name = bundle_names(len(rows), len(staged))
    xlsx = write_expense_workbook(os.path.join(bdir, xlsx_name), rows)
    zpath = None
    if staged:
        zpath = os.path.join(bdir, zip_name)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for pth in staged:
                z.write(pth, os.path.basename(pth))
    shutil.rmtree(stage, ignore_errors=True)
    olds = sorted(d for d in os.listdir(BUNDLES_DIR)      # keep the last few
                  if os.path.isdir(os.path.join(BUNDLES_DIR, d)))
    for d in olds[:-20]:
        shutil.rmtree(os.path.join(BUNDLES_DIR, d), ignore_errors=True)
    return rows, xlsx, zpath, len(staged), missing


def report_text(data, owner_name="", note=""):
    """The email body: one sentence, and only what a person needs to read.

    Everything else now lives in the attached workbook — the table used to be
    pasted into the body, which made the message long and awkward to read, and
    Excel is where these numbers were always going to end up.
    """
    who = (owner_name or "").strip()
    lead = ("%s's expense report for the month of %s."
            % (who, data["label"]) if who
            else "Expense report for the month of %s." % data["label"])
    lines = [lead, "",
             "%d expense(s), total %s — the full table is attached as "
             "expenses_%s.xlsx." % (len(data["rows"]),
                                    money_str(data["total"]), data["month"])]
    receipts = sum(r.get("_nattached", 0) for r in data["rows"])
    if receipts:
        lines.append("%d receipt/document file(s) are attached, each named as the "
                     "Receipt column in the workbook names it — the number at "
                     "the start of a file name is its line in the table "
                     "(1 = the first expense listed)." % receipts)
    pcard = sum(1 for r in data["rows"] if r.get("_pcard"))
    if pcard:
        lines.append("%d of them were P-card purchases; the workbook has a "
                     "“P-card purchases” sheet listing just those." % pcard)
    if note:
        lines.append(note)
    if data["missing_receipts"]:
        lines.append("%d hand-entered expense(s) have no receipt attached — "
                     "worth checking before this goes to the accountant."
                     % data["missing_receipts"])
    lines += ["", "Amounts are as recorded in Grants Manager; Workday remains "
              "the system of record."]
    return "\n".join(lines)


def wd_state(conn):
    mappings = rows_to_list(conn.execute("SELECT * FROM workday_map ORDER BY kind, wd_key"))
    mapped_g = {m["wd_key"] for m in mappings if m["kind"] == "grant"}
    mapped_c = {m["wd_key"] for m in mappings if m["kind"] == "category"}
    known = rows_to_list(conn.execute(
        "SELECT grant_code, grant_name FROM workday_lines WHERE grant_code!='' "
        "UNION SELECT grant_code, grant_name FROM workday_balances"))
    ginfo = get_setting(conn, "wd_grant_info", {}) or {}
    unmapped_grants = []
    for k in known:
        if k["grant_code"] in mapped_g:
            continue
        entry = dict(k)
        entry.update(ginfo.get(k["grant_code"], {}))  # name/award/start/end
        unmapped_grants.append(entry)
    ocs = [r[0] for r in conn.execute(
        "SELECT DISTINCT object_class FROM workday_lines WHERE object_class!='' "
        "UNION SELECT DISTINCT object_class FROM workday_balances "
        "WHERE object_class!=''")]
    unmapped_cats = [k for k in ocs if k not in mapped_c]
    # Workers on already-mapped payroll lines that we couldn't tie to a person.
    # Only surfaced once their object class is mapped to Personnel/Fringe — the
    # point at which a name would actually attach to a charge.
    cmap = {m["wd_key"]: m["target_id"] for m in mappings if m["kind"] == "category"}
    catname = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM categories")}
    payroll_ocs = [oc for oc, tid in cmap.items()
                   if tid is not None and catname.get(tid) in ("Personnel", "Fringe")]
    person_names = {r["name"].strip().lower() for r in conn.execute(
        "SELECT name FROM people")}
    worker_seen = {m["wd_key"].strip().lower() for m in mappings
                   if m["kind"] == "worker"}
    unmatched_workers = []
    if payroll_ocs:
        qm = ",".join("?" * len(payroll_ocs))
        for r in conn.execute(
                "SELECT TRIM(worker) AS worker, COUNT(*) AS n, "
                "ROUND(SUM(amount),2) AS total FROM workday_lines "
                "WHERE TRIM(worker)!='' AND object_class IN (%s) "
                "GROUP BY LOWER(TRIM(worker)) ORDER BY total DESC" % qm,
                payroll_ocs):
            if (r["worker"].lower() not in person_names
                    and r["worker"].lower() not in worker_seen):
                unmatched_workers.append(
                    {"worker": r["worker"], "lines": r["n"], "amount": r["total"]})
    # Grants whose award/budget the IMPORT wrote and the user hasn't since
    # changed: for these Workday's actuals are the ledger. Any grant the user
    # built or edited by hand keeps the app's own expenses as its ledger.
    driven = {}
    applied = get_setting(conn, "wd_applied", {}) or {}
    for gid_s, a in applied.items():
        g = conn.execute("SELECT initial_amount FROM grants WHERE id=?",
                         (int(gid_s),)).fetchone()
        tot = a.get("total") if isinstance(a, dict) else a
        if g and tot is not None and abs(tot - (g["initial_amount"] or 0)) < 0.005:
            driven[gid_s] = a.get("wm", 0) if isinstance(a, dict) else 0
    return {
        "balance_driven": driven,
        "import_dir": WD_IMPORT_DIR,
        "raas": get_setting(conn, "workday_raas", {}) or {},
        "push_cfg": get_setting(conn, "workday_push", {}) or {},
        "pcard": get_setting(conn, "pcard", {}) or {},
        "reports_sent": get_setting(conn, "reports_sent", {}) or {},
        "last_sync": get_setting(conn, "workday_last_sync"),
        "session_unlocked": bool(WD_SESSION["password"]),
        "push": wd_push_state(conn),
        "mappings": mappings,
        "unmapped_grants": unmapped_grants,
        "unmapped_categories": unmapped_cats,
        "unmatched_workers": unmatched_workers,
        "balances": rows_to_list(conn.execute(
            "SELECT * FROM workday_balances ORDER BY grant_code, object_class")),
        "lines": rows_to_list(conn.execute(
            "SELECT * FROM workday_lines ORDER BY date DESC, id DESC LIMIT 300")),
        "counts": dict(conn.execute(
            "SELECT status, COUNT(*) FROM workday_lines GROUP BY status").fetchall()),
    }


# ---------------------------------------------------------------- API state

APP_VERSION = "1.5.0"
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
        blocker = update_blocker()
        info.update(can_install=not blocker, blocker=blocker)
    return info


# ------------------------------------------------------------ self-update
#
# One click installs the newest release over the running copy. The rule that
# keeps users' work safe: only PROGRAM files are ever written (server.py, the
# app/ folder, README). data/ and receipts/ are not on that list, so an update
# cannot overwrite the database, backups or receipts — there is nothing to
# "copy your data across". The old program is kept in data/code_backups/, the
# database is backed up first, and any failure rolls the files back.

UPDATE_MAX_ZIP = 40 * 1024 * 1024
UPDATE_MAX_UNPACKED = 120 * 1024 * 1024
UPDATE_CODE_BACKUPS_KEPT = 2
UPDATE_ASSET = "GrantsManager.zip"
UPDATE_LOCK = threading.Lock()
_UPDATE_EXTS = (".html", ".css", ".js", ".png", ".gif", ".json", ".md")


class UpdateError(Exception):
    """A problem worth showing the user, in plain words."""


def update_blocker():
    """Why this copy can't update itself in place ('' when it can)."""
    if os.path.isdir(os.path.join(BASE_DIR, ".git")):
        return ("This copy is a developer checkout (it has a .git folder), "
                "so update it with git instead.")
    probe = os.path.join(BASE_DIR, ".update-write-test")
    try:
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
    except OSError:
        return ("Grants Manager can't write to its own folder, so it can't "
                "update itself. Download the new version instead.")
    return ""


def _update_wanted(rel):
    """Only program files are ever installed. The two starter scripts are
    deliberately excluded: a running .bat is read line by line by Windows, so
    rewriting it under itself can break the launch that is replacing it."""
    if rel in ("server.py", "README.md"):
        return True
    parts = rel.split("/")
    return (parts[0] == "app" and 2 <= len(parts) <= 3
            and rel.lower().endswith(_UPDATE_EXTS))


def update_fetch_zip(version, dest):
    """Download the release zip for `version` to `dest` (HTTPS, this repo only)
    and check it against the SHA-256 GitHub publishes for the asset."""
    import hashlib
    import urllib.error
    import urllib.request
    if not re.match(r"^\d+(\.\d+){1,3}$", str(version)):
        raise UpdateError("Unrecognised version number.")
    tag = "v%s" % version
    digest = None
    try:  # the published checksum is best-effort: rate limits shouldn't block
        req = urllib.request.Request(
            "https://api.github.com/repos/%s/releases/tags/%s"
            % (UPDATE_REPO, tag),
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "GrantsManager/%s" % APP_VERSION})
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.loads(r.read(1_000_000).decode("utf-8", "replace"))
        for a in rel.get("assets") or []:
            if a.get("name") == UPDATE_ASSET and str(
                    a.get("digest") or "").startswith("sha256:"):
                digest = a["digest"].split(":", 1)[1].lower()
    except Exception:  # noqa: BLE001
        pass
    url = "https://github.com/%s/releases/download/%s/%s" % (
        UPDATE_REPO, tag, UPDATE_ASSET)
    h, size = hashlib.sha256(), 0
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "GrantsManager/%s" % APP_VERSION})
        with urllib.request.urlopen(req, timeout=60) as r, \
                open(dest, "wb") as out:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > UPDATE_MAX_ZIP:
                    raise UpdateError("The download is larger than expected.")
                h.update(chunk)
                out.write(chunk)
    except urllib.error.HTTPError as e:
        raise UpdateError("GitHub returned an error (HTTP %d) for that "
                          "version." % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError("Couldn't download the update — check your internet "
                          "connection (%s)." % getattr(e, "reason", e))
    if digest and h.hexdigest() != digest:
        raise UpdateError("The download didn't match GitHub's checksum, so it "
                          "was discarded. Nothing was changed.")


def update_stage(zip_path, want_version, stage_dir):
    """Validate a downloaded release and unpack ONLY its program files into
    `stage_dir`. Returns the relative paths staged. Nothing live is touched."""
    import zipfile
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise UpdateError("The downloaded file isn't a valid update package.")
    wanted, total = [], 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        rel = name[len("GrantsManager/"):] if name.startswith(
            "GrantsManager/") else None
        if rel is None:
            raise UpdateError("The update package has an unexpected layout.")
        parts = rel.split("/")
        if (rel.startswith("/") or "" in parts or ".." in parts
                or ":" in rel):
            raise UpdateError("The update package contains an unsafe path.")
        if not _update_wanted(rel):
            continue
        total += info.file_size
        if total > UPDATE_MAX_UNPACKED:
            raise UpdateError("The update package is larger than expected.")
        wanted.append((rel, info))
    rels = {r for r, _ in wanted}
    for need in ("server.py", "app/index.html", "app/app.js",
                 "app/styles.css"):
        if need not in rels:
            raise UpdateError("The update package is incomplete (missing %s)."
                              % need)
    src = zf.read("GrantsManager/server.py").decode("utf-8")
    m = re.search(r'^APP_VERSION = "([^"]+)"', src, re.M)
    if not m or m.group(1) != str(want_version):
        raise UpdateError("The package holds version %s, not the expected %s."
                          % (m.group(1) if m else "?", want_version))
    try:
        compile(src, "server.py", "exec")
    except SyntaxError:
        raise UpdateError("The new version failed a safety check.")
    shutil.rmtree(stage_dir, ignore_errors=True)
    for rel, info in wanted:
        dst = os.path.join(stage_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with zf.open(info) as fin, open(dst, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    # the new code must import cleanly on THIS machine's Python before we
    # put it in place — a version that can't start must never replace one
    # that can
    import subprocess
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import server; "
             "print(server.APP_VERSION)", stage_dir],
            capture_output=True, text=True, timeout=60, cwd=stage_dir)
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError("The new version couldn't be test-loaded.")
    if r.returncode != 0 or r.stdout.strip() != str(want_version):
        raise UpdateError("The new version failed to load on this computer "
                          "(it may need a newer Python). Nothing was changed.")
    return sorted(rels)


def update_apply(stage_dir, rels, old_version):
    """Swap the staged program files in, keeping the old ones. Rolls back on
    any error. Never touches data/ or receipts/ (not in `rels`)."""
    for rel in rels:
        assert rel.split("/")[0] not in ("data", "receipts"), rel
    bdir = os.path.join(DATA_DIR, "code_backups", "v%s" % old_version)
    shutil.rmtree(bdir, ignore_errors=True)
    for rel in rels:
        cur = os.path.join(BASE_DIR, *rel.split("/"))
        if os.path.isfile(cur):
            dst = os.path.join(bdir, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(cur, dst)
    order = [r for r in rels if r != "server.py"] + ["server.py"]
    done = []
    try:
        for rel in order:
            dst = os.path.join(BASE_DIR, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".update-tmp"
            shutil.copyfile(os.path.join(stage_dir, *rel.split("/")), tmp)
            os.replace(tmp, dst)
            done.append(rel)
    except Exception as e:  # noqa: BLE001 — put everything back
        for rel in done:
            dst = os.path.join(BASE_DIR, *rel.split("/"))
            old = os.path.join(bdir, *rel.split("/"))
            try:
                if os.path.isfile(old):
                    shutil.copyfile(old, dst)
                else:
                    os.remove(dst)
            except OSError:
                pass
        raise UpdateError("Couldn't install the update (%s). Your current "
                          "version was restored." % e)
    root = os.path.join(DATA_DIR, "code_backups")  # keep only the newest few
    olds = sorted((d for d in os.listdir(root) if d.startswith("v")),
                  key=lambda d: os.path.getmtime(os.path.join(root, d)))
    for d in olds[:-UPDATE_CODE_BACKUPS_KEPT]:
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    return bdir


UPDATE_STATE = {"state": "idle", "message": ""}


def update_trial(stage_dir, version):
    """Actually START the new version — beside the running one, on a spare
    port, against a COPY of the user's database — before touching anything.
    That exercises database migrations on real data, port binding and the
    first page load, which a mere import test can't. Raises UpdateError."""
    import subprocess
    import urllib.request
    data_dir = os.path.join(stage_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    if os.path.isfile(DB_PATH):
        sqlite_snapshot(DB_PATH, os.path.join(data_dir, "grants.db"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    logp = os.path.join(stage_dir, "trial.log")
    with open(logp, "wb") as log:
        kw = dict(cwd=stage_dir, stdin=subprocess.DEVNULL, stdout=log,
                  stderr=log)
        if os.name == "nt":
            kw["creationflags"] = 0x08000000   # no console window
        proc = subprocess.Popen([sys.executable,
                                 os.path.join(stage_dir, "server.py"),
                                 "--port", str(port)], **kw)
    ok, why = False, "it didn't start within 30 seconds"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                why = "it stopped right after starting"
                break
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/ping" % port, timeout=2) as r:
                    j = json.loads(r.read(10000).decode())
                if j.get("version") == str(version):
                    req = urllib.request.Request(
                        "http://127.0.0.1:%d/api/state" % port,
                        headers={"X-Grants-App": "1"})
                    with urllib.request.urlopen(req, timeout=15) as r2:
                        json.loads(r2.read().decode())   # builds from real data
                    ok = True
                    break
            except Exception:  # noqa: BLE001 — not up yet
                pass
            time.sleep(0.4)
    finally:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
    if not ok:
        raise UpdateError("The new version didn't pass its start-up test on "
                          "this computer (%s), so it was NOT installed. Your "
                          "current version and data are untouched." % why)


def update_rollback(bdir, rels):
    """Put the previous program files back (used when a freshly installed
    version fails to come up)."""
    for rel in rels:
        dst = os.path.join(BASE_DIR, *rel.split("/"))
        old = os.path.join(bdir, *rel.split("/"))
        try:
            if os.path.isfile(old):
                shutil.copyfile(old, dst)
            elif os.path.isfile(dst):
                os.remove(dst)
        except OSError:
            pass


def update_restart(bdir, rels, version):
    """Start the freshly installed copy in the background. It finds this old
    one on the port and takes over (graceful /api/shutdown), so the page can
    simply wait for the new version to answer. Delayed so this request's
    response is delivered first. If the new copy never takes over, this
    (still-running) copy restores the previous files, so the next launch
    can't hit a broken install."""
    UPDATE_STATE.update(state="restarting", message="", version=str(version))

    def go():
        import subprocess
        time.sleep(0.8)
        try:
            log = open(os.path.join(DATA_DIR, "last_start.log"), "ab")
        except OSError:
            log = subprocess.DEVNULL
        kw = dict(cwd=BASE_DIR, stdin=subprocess.DEVNULL, stdout=log,
                  stderr=log, close_fds=True)
        if os.name == "nt":  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
            kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
        else:
            kw["start_new_session"] = True
        try:
            subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "server.py")],
                             **kw)
        except OSError:
            pass
        # If the new copy takes over, we're shut down and never get past here.
        time.sleep(35)
        update_rollback(bdir, rels)
        UPDATE_STATE.update(
            state="rolled_back",
            message="The new version couldn't start on this computer, so your "
                    "previous version was put back. Nothing was lost.")
    threading.Thread(target=go, daemon=True).start()


def update_install():
    """The whole one-click update. Returns what the UI should tell the user."""
    if not UPDATE_LOCK.acquire(blocking=False):
        raise UpdateError("An update is already in progress.")
    try:
        blocker = update_blocker()
        if blocker:
            raise UpdateError(blocker)
        check_for_update(force=True)
        c = read_update_cache()
        latest = c.get("latest") or ""
        if not latest or _version_tuple(latest) <= _version_tuple(APP_VERSION):
            raise UpdateError("You're already on the newest version (%s)."
                              % APP_VERSION)
        work = os.path.join(DATA_DIR, "update_work")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
        try:
            zpath = os.path.join(work, UPDATE_ASSET)
            update_fetch_zip(latest, zpath)
            stage = os.path.join(work, "stage")
            rels = update_stage(zpath, latest, stage)
            update_trial(stage, latest)
            db_backup = make_backup(force=True)
            bdir = update_apply(stage, rels, APP_VERSION)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        update_restart(bdir, rels, latest)
        return {"ok": True, "version": latest, "from": APP_VERSION,
                "db_backup": os.path.basename(db_backup or "")}
    finally:
        UPDATE_LOCK.release()


# Changes every time the app starts. The dashboard hangs dismissed alerts off
# it, which is what makes "hidden until I open the app again" mean exactly
# that: a page reload keeps them hidden, quitting and reopening brings them
# back. A date or a browser session can't express that — one is too coarse,
# the other survives a quit or dies on a reload depending on the browser.
RUN_ID = uuid.uuid4().hex[:12]


def full_state(conn):
    return {
        "version": APP_VERSION,
        "run_id": RUN_ID,
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


def receipt_abspath(rel):
    """Absolute path of a stored receipt, or None if `rel` is empty, escapes
    the receipts folder, or isn't a file. Every place that turns a stored or
    client-supplied receipt path into a file goes through here."""
    if not rel:
        return None
    try:
        p = safe_under(RECEIPTS_DIR, str(rel))
    except (PermissionError, ValueError):
        return None
    return p if os.path.isfile(p) else None


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
    nc = os.path.normcase
    if nc(full) != nc(root) and not nc(full).startswith(nc(root) + os.sep):
        raise PermissionError("path outside allowed directory")
    return full


RECEIPT_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic",
                ".heif", ".tif", ".tiff", ".bmp", ".txt", ".csv", ".xlsx",
                ".xls", ".doc", ".docx", ".eml", ".msg"}
MAX_STORED_PATH = 235   # Windows stops at 260; leave room for OneDrive sync
# Big receipts are ACCEPTED — the page only warns from 20 MB up (large files
# can make the monthly email too big to send). This is just a safety ceiling,
# comfortably below the request limit (a file grows ~1/3 when sent as text).
MAX_RECEIPT_BYTES = 150 * 1024 * 1024


MAX_EXTRA_FILES = 20


def expense_files(raw):
    """The additional documents on an expense, as [{'path','name'}] — tolerant
    of an empty or damaged value."""
    try:
        v = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    out = []
    for x in v if isinstance(v, list) else []:
        if isinstance(x, dict) and isinstance(x.get("path"), str) and x["path"]:
            out.append({"path": x["path"],
                        "name": str(x.get("name") or os.path.basename(x["path"]))[:120],
                        "added": str(x.get("added") or "")})
    return out


def save_receipt(grant, payload):
    """payload: {name, data(base64)} -> relative path under receipts/.

    Never overwrites an existing file, refuses file types a browser could run
    as a web page (.html/.svg...), and keeps the whole path short enough for
    Windows. Raises ValueError with a message the user can act on."""
    orig = os.path.basename(str(payload.get("name") or "receipt"))
    stem, ext = os.path.splitext(orig)
    ext = ext.lower()
    if ext not in RECEIPT_EXTS:
        raise ValueError("A %s file can't be attached as a receipt. Use a PDF "
                         "or a photo (JPG/PNG)." % (ext or "typeless"))
    try:
        raw = base64.b64decode(payload["data"], validate=False)
    except (ValueError, KeyError):
        raise ValueError("That receipt file couldn't be read.")
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError("That receipt file is %d MB, which is more than Grants "
                         "Manager can store (limit %d MB). Save it as a smaller "
                         "PDF (for photos: export at a lower quality) and try "
                         "again." % (len(raw) // 1048576,
                                     MAX_RECEIPT_BYTES // 1048576))
    stem = _win_safe(re.sub(r"[^A-Za-z0-9._ -]+", "_", stem)) or "receipt"
    sub = os.path.join(slugify(grant["name"]), str(date.today().year))
    folder = os.path.join(RECEIPTS_DIR, sub)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for attempt in range(5):
        tag = uuid.uuid4().hex[:6]
        room = MAX_STORED_PATH - len(os.path.abspath(folder)) - 1 \
            - len("%s_%s_" % (stamp, tag)) - len(ext)
        if room < 8:
            raise ValueError(
                "Grants Manager is stored too deep in your folders for "
                "Windows to save receipts. Move the whole Grants Manager "
                "folder somewhere shorter, like C:\\GrantsManager, then try "
                "again.")
        name = "%s_%s_%s%s" % (stamp, tag, stem[:room].rstrip(" ."), ext)
        path = os.path.join(folder, name)
        try:
            os.makedirs(folder, exist_ok=True)
            with open(path, "xb") as f:   # 'x': never clobber another receipt
                f.write(raw)
            return os.path.relpath(path, RECEIPTS_DIR).replace("\\", "/")
        except FileExistsError:
            continue
        except OSError as e:
            raise ValueError("Couldn't save the receipt file (%s). The expense "
                             "was not added." % (e.strerror or e))
    raise ValueError("Couldn't pick a free name for the receipt — try again.")


TABLES = {
    "grants": ["name", "agency", "initial_amount", "start_date", "end_date",
               "status", "notes", "exclude_from_history", "nce_end_date",
               "funded_to_date", "plan_basis"],
    "categories": ["name", "grant_id", "sort"],
    "people": ["name", "role", "plan_end"],
    "appointments": ["person_id", "grant_id", "monthly_salary", "fringe_rate",
                     "start_date", "end_date", "notes", "annual_tuition",
                     "auto_charge", "pct", "fringe_only"],
    "expenses": ["grant_id", "category_id", "year", "date", "amount",
                 "description", "person_id", "receipt_path", "source",
                 "wd_entry", "wd_worktag", "pcard", "pcard_buyer",
                 "pcard_holder"],
}


DATE_FIELDS = ("date", "start_date", "end_date", "nce_end_date", "plan_end")
MONEY_FIELDS = ("amount", "initial_amount", "monthly_salary", "fringe_rate",
                "annual_tuition", "pct", "funded_to_date")
# a dollar figure no grant will ever legitimately reach; past this the number
# is a typo or a paste accident, and letting it in wrecks every chart's scale
MONEY_LIMIT = 1e12


def grant_status_rule(conn, rid, data):
    """A closed grant drops out of every total, the historic one included,
    until the person ticks it there again; reopening puts it back. Applies only
    when the status actually changes and the request didn't say otherwise."""
    if data.get("status") in ("active", "closed") \
            and "exclude_from_history" not in data:
        cur = conn.execute("SELECT status FROM grants WHERE id=?",
                           (rid,)).fetchone()
        if cur is not None and cur["status"] != data["status"]:
            data["exclude_from_history"] = 1 if data["status"] == "closed" else 0
    return data


def validate_row(table, data, creating):
    """Check a row before it reaches SQLite.

    Without this the user sees the database's own complaint — "NOT NULL
    constraint failed: expenses.date" — and a mistyped date like "03/14" is
    stored verbatim, where it sorts wrong, lands in no budget year, and
    quietly disappears from every month's report.
    """
    for f in DATE_FIELDS:
        v = data.get(f)
        if v is None or v == "":
            continue
        try:
            # "2026-9-3" parses, but stored as typed it sorts after
            # "2026-09-30" and falls out of that month's report: normalise.
            data[f] = datetime.strptime(str(v), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise ValueError("“%s” isn't a date the app can read. Use the "
                             "date picker, or type it as YYYY-MM-DD." % v)
    if data.get("wd_entry") not in (None, "", "sent", "done", "na"):
        raise ValueError("Unknown Workday status.")
    for f in MONEY_FIELDS:
        if f not in data or data[f] is None or data[f] == "":
            continue
        try:
            n = float(data[f])
        except (TypeError, ValueError):
            raise ValueError("“%s” isn't a number." % (data[f],))
        if not math.isfinite(n) or abs(n) > MONEY_LIMIT:
            raise ValueError("That amount is out of range — check for an "
                             "extra digit or a stray paste.")
        data[f] = n
    if table == "expenses" and creating:
        if not data.get("date"):
            raise ValueError("An expense needs a date.")
        if not data.get("grant_id"):
            raise ValueError("An expense needs a grant.")
    if table == "grants":
        s, e = data.get("start_date"), data.get("end_date")
        if s and e and e < s:
            raise ValueError("The end date is before the start date.")
        if "funded_to_date" in data and data["funded_to_date"] in ("", None):
            data["funded_to_date"] = 0
        if data.get("funded_to_date", 0) < 0:
            raise ValueError("“Released so far” can't be negative.")
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # ------------------------------------------------------------ security
    def is_local(self):
        host = (self.client_address[0] or "")
        return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def check_host(self):
        """Refuse requests that address this server by a *domain name*.

        Without this, a page on evil.example can point its own hostname at
        127.0.0.1 (short DNS TTL, then re-answer with the loopback address —
        "DNS rebinding"). The victim's browser then treats evil.example:8765
        as same-origin with Grants Manager: `is_local()` sees a loopback
        client so it looks like this computer, and the CSRF check passes too
        because Origin and Host now agree. Every grant, expense and receipt is
        readable and writable by that page.

        The defence is that a rebinding attack always arrives under a NAME.
        Legitimate use is always by address — 127.0.0.1 or localhost — so a
        request addressed to any other name is refused.
        """
        host = (self.headers.get("Host") or "").strip()
        if not host:          # HTTP/1.0 clients may omit it
            return True
        if host.startswith("["):                       # [::1]:8765
            name = host[1:host.find("]")] if "]" in host else host[1:]
        else:                                          # host:8765 / bare IPv6
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        name = name.strip().rstrip(".").lower()
        if name in ("localhost", "0.0.0.0"):
            return True
        try:
            ipaddress.ip_address(name)   # a literal address can't be rebound
            return True
        except ValueError:
            pass
        self.send_json({"error": "Grants Manager only answers at its own "
                        "address. Open it at http://127.0.0.1:%d, not "
                        "through some other web address." % PORT}, 403)
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
    def end_headers(self):
        # No page may frame the app (clickjacking), and browsers must trust
        # our Content-Type rather than guessing one.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        super().end_headers()

    def send_json(self, obj, code=200):
        body = json.dumps(json_safe(obj), allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, where, exc):
        """One place that decides what a failed write is allowed to say.

        Messages we wrote ourselves (ValueError/RuntimeError/PermissionError)
        are meant for the person using the app, so they pass through. Anything
        else is a bug or a database complaint — "NOT NULL constraint failed:
        expenses.date", "'NoneType' object is not iterable" — which tells the
        user nothing and tells a stranger a little about our internals. Log it
        here, show a plain sentence there.
        """
        if isinstance(exc, json.JSONDecodeError):
            self.send_json({"error": "That request wasn't understood."}, 400)
            return
        if isinstance(exc, (ValueError, RuntimeError, PermissionError)):
            self.send_json({"error": str(exc)}, 400)
            return
        print("%s failed: %r" % (where, exc))
        self.send_json({"error": "Something went wrong saving that. Nothing "
                        "was changed. If it keeps happening, quit and reopen "
                        "Grants Manager — your data and backups are intact."},
                       500)

    # Bodies are read whole into memory (base64 uploads), so cap them rather
    # than trusting a client-supplied Content-Length.
    MAX_BODY = 260 * 1024 * 1024

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        if length > self.MAX_BODY:
            raise ValueError("Request too large")
        return reject_wild_numbers(json.loads(self.rfile.read(length)))

    def send_file(self, path, download_name=None, untrusted=False):
        if not os.path.isfile(path):
            self.send_json({"error": "not found"}, 404)
            return
        ctypes = {".html": "text/html", ".js": "application/javascript",
                  ".css": "text/css", ".png": "image/png", ".jpg": "image/jpeg",
                  ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".pdf": "application/pdf", ".svg": "image/svg+xml",
                  ".csv": "text/csv", ".heic": "image/heic",
                  ".webp": "image/webp", ".json": "application/json",
                  ".xlsx": "application/vnd.openxmlformats-officedocument."
                           "spreadsheetml.sheet"}
        ext = os.path.splitext(path)[1].lower()
        ctype = ctypes.get(ext, "application/octet-stream")
        if untrusted and ext not in (".pdf", ".png", ".jpg", ".jpeg", ".gif",
                                     ".webp"):
            # a receipt is someone else's file: never let a browser render it
            # as a page (a script in an .html/.svg would run as the app)
            ctype = "application/octet-stream"
            download_name = download_name or os.path.basename(path)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # The app's own code (HTML/JS/CSS) must never be served stale, or after
        # someone unzips a new version their browser keeps showing the old UI
        # from cache until a manual hard-refresh. Force a re-fetch for these;
        # images and receipts can still cache normally.
        if ext in (".html", ".js", ".css"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        if download_name:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------- routing
    def do_GET(self):
        if not self.check_host():
            return
        path = unquote(urlparse(self.path).path)
        # Unauthenticated identity probe. Carries no user data — it exists so a
        # second launch can tell "Grants Manager is already running here" from
        # "some unrelated program owns this port" without needing the key.
        if path == "/api/ping":
            out = {"app": "grants-manager", "version": APP_VERSION}
            if self.is_local():   # only a program on THIS machine gets these
                out["dir"] = BASE_DIR
                out["pid"] = os.getpid()
            self.send_json(out)
            return
        try:
            if path == "/" or path == "/index.html":
                self.send_file(os.path.join(APP_DIR, "index.html"))
            elif path.startswith("/app/"):
                self.send_file(safe_under(APP_DIR, path[len("/app/"):]))
            elif path.startswith("/receipts/"):
                self.send_file(safe_under(RECEIPTS_DIR, path[len("/receipts/"):]),
                               untrusted=True)
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
            elif path == "/api/workday/example.xlsx":
                # regenerated on every request so it always matches the
                # columns the importer actually looks for
                ex = os.path.join(DATA_DIR, "workday_example.xlsx")
                wd_example_workbook(ex)
                self.send_file(ex, download_name=(
                    "Workday export example (fake data).xlsx"))
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
                    # the preview shows exactly the workbook's columns, so
                    # what you check is what your accountant receives
                    d["columns"] = REPORT_XLSX_COLUMNS
                    d["owner_email"] = (get_setting(conn, "workday_push", {})
                                        or {}).get("owner_email", "")
                    self.send_json(d)
                finally:
                    conn.close()
            elif path == "/api/update/status":
                self.send_json(dict(UPDATE_STATE))
            elif path == "/api/trash":
                conn = db()
                try:
                    self.send_json({"batches": trash_list(conn)})
                finally:
                    conn.close()
            elif path == "/api/backup/download":
                # serve the consistent snapshot just taken, not the live file
                snap = make_backup(force=True)
                self.send_file(snap, download_name=(
                    f"grants_backup_{date.today().isoformat()}.db"))
            else:
                self.send_json({"error": "not found"}, 404)
        except PermissionError:
            self.send_json({"error": "not found"}, 404)
        except ValueError as e:
            # a bad parameter (e.g. ?month=abc) is the caller's mistake, not
            # a server fault — say what's wrong instead of "500"
            self.send_json({"error": str(e)}, 400)
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
            body = b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
            name = f"{slugify(grant['name'])}_ledger.csv"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
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
        body = b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="workday_entry_sheet.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.check_host() or not self.check_not_csrf():
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
                validate_row(table, data, creating=True)
                data.pop("receipt_path", None)   # only the server sets this
                # grant_id 0 on an appointment = "unassigned": costs no grant
                # carries yet (assigned to a grant later from the People tab)
                if table in ("expenses", "appointments") and not (
                        table == "appointments" and data.get("grant_id") == 0):
                    grant = conn.execute("SELECT * FROM grants WHERE id=?",
                                         (data.get("grant_id"),)).fetchone()
                    if grant is None:
                        raise ValueError("That grant no longer exists — "
                                         "reload the page and try again.")
                also = data.pop("also", None) if table == "expenses" else None
                saved = None
                if table == "expenses" and data.get("receipt"):
                    saved = save_receipt(dict(grant), data.pop("receipt"))
                    data["receipt_path"] = saved
                cols = [c for c in TABLES[table] if c in data]
                try:
                    cur = conn.execute(
                        f"INSERT INTO {table} ({','.join(cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})",
                        [data[c] for c in cols])
                    if table == "expenses":
                        audit_log(conn, "added", cur.lastrowid,
                                  data.get("grant_id"), data.get("description"),
                                  data.get("amount"))
                    also_id = None
                    if also:   # the other half of a split: same transaction
                        also = validate_row("expenses", dict(also), creating=True)
                        also.pop("receipt_path", None)
                        also.pop("receipt", None)
                        if not conn.execute("SELECT 1 FROM grants WHERE id=?",
                                            (also.get("grant_id"),)).fetchone():
                            raise ValueError("The grant you're splitting with "
                                             "no longer exists — reload the "
                                             "page and try again.")
                        c2 = [c for c in TABLES["expenses"] if c in also]
                        cur2 = conn.execute(
                            f"INSERT INTO expenses ({','.join(c2)}) "
                            f"VALUES ({','.join('?' * len(c2))})",
                            [also[c] for c in c2])
                        also_id = cur2.lastrowid
                        audit_log(conn, "added", also_id, also.get("grant_id"),
                                  also.get("description"), also.get("amount"))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    if saved:   # don't leave an orphan file behind
                        try:
                            os.remove(os.path.join(RECEIPTS_DIR, saved))
                        except OSError:
                            pass
                    raise
                self.send_json({"id": cur.lastrowid, "also_id": also_id}
                               if also_id else {"id": cur.lastrowid})
                return
            m = re.match(r"/api/expenses/(\d+)/files$", path)
            if m:
                rid = int(m.group(1))
                exp = conn.execute(
                    "SELECT e.*, g.name AS gname FROM expenses e "
                    "JOIN grants g ON g.id=e.grant_id WHERE e.id=?",
                    (rid,)).fetchone()
                if exp is None:
                    raise ValueError("That expense no longer exists — reload "
                                     "the page and try again.")
                files = expense_files(exp["extra_files"])
                if len(files) >= MAX_EXTRA_FILES:
                    raise ValueError("An expense can hold up to %d additional "
                                     "documents." % MAX_EXTRA_FILES)
                payload = data.get("file") or {}
                shown = re.sub(r"[\x00-\x1f]", "", os.path.basename(
                    str(payload.get("name") or "document")))[:120] or "document"
                saved = save_receipt({"name": exp["gname"]}, payload)
                files.append({"path": saved, "name": shown,
                              "added": date.today().isoformat()})
                try:
                    conn.execute("UPDATE expenses SET extra_files=? WHERE id=?",
                                 (json.dumps(files), rid))
                    audit_log(conn, "edited", rid, exp["grant_id"],
                              exp["description"], exp["amount"],
                              "attached “%s”" % shown)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    try:
                        os.remove(os.path.join(RECEIPTS_DIR, saved))
                    except OSError:
                        pass
                    raise
                self.send_json({"files": files})
                return
            m = re.match(r"/api/expenses/(\d+)/files/remove$", path)
            if m:
                rid = int(m.group(1))
                exp = conn.execute("SELECT * FROM expenses WHERE id=?",
                                   (rid,)).fetchone()
                if exp is None:
                    raise ValueError("That expense no longer exists.")
                files = expense_files(exp["extra_files"])
                gone = [f for f in files if f["path"] == data.get("path")]
                files = [f for f in files if f["path"] != data.get("path")]
                # the file itself stays on disk: removing a document from an
                # expense is not a reason to destroy the only copy of it
                conn.execute("UPDATE expenses SET extra_files=? WHERE id=?",
                             (json.dumps(files), rid))
                if gone:
                    audit_log(conn, "edited", rid, exp["grant_id"],
                              exp["description"], exp["amount"],
                              "removed attachment “%s”" % gone[0]["name"])
                conn.commit()
                self.send_json({"files": files})
                return
            # Update record: /api/<table>/<id>
            m = re.match(r"/api/(grants|categories|people|appointments|expenses)"
                         r"/(\d+)$", path)
            if m:
                table, rid = m.group(1), int(m.group(2))
                validate_row(table, data, creating=False)
                data.pop("receipt_path", None)   # only the server sets this
                if table == "expenses" and data.get("receipt"):
                    grant = conn.execute(
                        "SELECT g.* FROM grants g JOIN expenses e ON e.grant_id=g.id "
                        "WHERE e.id=?", (rid,)).fetchone()
                    if grant is None:
                        raise ValueError("That expense no longer exists — "
                                         "reload the page and try again.")
                    data["receipt_path"] = save_receipt(dict(grant),
                                                        data.pop("receipt"))
                if table == "grants":
                    grant_status_rule(conn, rid, data)
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

                before_rows = {r["id"]: dict(r) for r in conn.execute(
                    f"SELECT * FROM expenses WHERE id IN ({marks})", ids)}
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
                for rid, b in before_rows.items():
                    a = conn.execute("SELECT * FROM expenses WHERE id=?",
                                     (rid,)).fetchone()
                    change = audit_describe_change(conn, b, dict(a)) if a else None
                    if change:
                        audit_log(conn, "edited", rid, a["grant_id"],
                                  a["description"], a["amount"], change)
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
                self.send_json(wd_import(conn, bool(data.get("allow_stale")),
                                         bool(data.get("discard_stale"))))
                return
            if path == "/api/workday/upload_import":
                errors = []
                for f in data.get("files") or []:
                    try:
                        wd_save_uploaded(f)
                    except Exception as e:  # noqa: BLE001
                        errors.append("%s: %s" % (f.get("name", "?"), e))
                # rescans the whole folder, incl. these
                res = wd_import(conn, bool(data.get("allow_stale")),
                                bool(data.get("discard_stale")))
                if errors:
                    res["upload_errors"] = errors
                self.send_json(res)
                return
            if path == "/api/workday/pdf/preview":
                self.send_json(wd_pdf_preview(conn, data))
                return
            if path == "/api/workday/pdf/check":
                self.send_json({"reports": [wd_pdf_check(conn, r)
                                            for r in (data.get("reports") or [])[:60]]})
                return
            if path == "/api/workday/pdf/apply":
                try:
                    res = wd_pdf_apply(conn, (data.get("reports") or [])[:60],
                                       data.get("choices") or {})
                except Exception:
                    conn.rollback()
                    raise
                self.send_json(res)
                return
            if path == "/api/workday/push_config":
                cfg = get_setting(conn, "workday_push", {}) or {}
                for k in ("owner_email", "owner_name"):
                    if k in data:
                        cfg[k] = (data.get(k) or "").strip()
                set_setting(conn, "workday_push", cfg)
                self.send_json({"ok": True})
                return
            if path == "/api/pcard_config":
                set_setting(conn, "pcard", {
                    "cardholder": (data.get("cardholder") or "").strip(),
                    "card_name": (data.get("card_name") or "").strip()})
                self.send_json({"ok": True})
                return
            if path == "/api/report/send":
                cfg = get_setting(conn, "workday_push", {}) or {}
                to = (data.get("to") or cfg.get("owner_email") or "").strip()
                if not to:
                    self.send_json({"error": "Type the email address the "
                                    "report should go to."}, 400)
                    return
                month = data.get("month") or date.today().strftime("%Y-%m")
                d = report_data(conn, month)
                if not d["rows"]:
                    self.send_json({"error": "No expenses recorded for %s — "
                                    "nothing to report yet." % d["label"]}, 400)
                    return
                # receipts are staged FIRST: staging can mark a missing file in
                # its row, so the table must be rendered after it, not before
                receipts, note, staged = report_receipt_attachments(d)
                xlsx = report_xlsx_path(d)
                attach = [xlsx] + receipts
                who = (cfg.get("owner_name") or "").strip()
                subject = ("%s — expense report, %s" % (who, d["label"])
                           if who else "Expense report — %s" % d["label"])
                body = report_text(d, who, note)
                too_big = (sum(os.path.getsize(a) for a in attach)
                           > RECEIPT_ATTACH_LIMIT)
                try:
                    if too_big:
                        raise OutlookUnavailable("too big for one email")
                    wd_send_mail(to, "", subject, body, attachment=attach)
                except OutlookUnavailable as why:
                    folder, mailto = prepare_outbox(
                        to, subject, body, [xlsx] + staged, d["month"])
                    self.send_json({
                        "ok": True, "fallback": True, "to": to,
                        "reason": "size" if too_big else "outlook",
                        "detail": str(why), "folder": folder,
                        "folder_name": os.path.basename(folder),
                        "mailto": mailto, "count": len(d["rows"]),
                        "attachments": [os.path.basename(a)
                                        for a in [xlsx] + staged]})
                    return
                sent = get_setting(conn, "reports_sent", {}) or {}
                sent[month] = datetime.now().isoformat(timespec="seconds")
                set_setting(conn, "reports_sent", sent)
                # remember the address actually used, so Settings never has
                # to be opened just to set it (and a corrected address sticks)
                if cfg.get("owner_email") != to:
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
                # the receipt plus any supporting documents (P-card form...);
                # every path must resolve inside the receipts folder
                wanted = ([data.get("receipt_path")] if data.get("receipt_path")
                          else []) + list(data.get("attachment_paths") or [])
                attach, seen = [], set()
                for rel in wanted[:60]:
                    ap = receipt_abspath(rel)
                    if ap and ap not in seen:
                        seen.add(ap)
                        attach.append(ap)
                subj = data.get("subject") or "Workday expense entry"
                text = data.get("body") or ""
                # a big receipt would be bounced by the mail server (a file
                # grows ~1/3 when sent), so prepare a folder instead of sending
                too_big = sum(os.path.getsize(a) for a in attach) \
                    > RECEIPT_ATTACH_LIMIT
                try:
                    if too_big:
                        raise OutlookUnavailable("receipt too big for one email")
                    wd_send_mail(to, "", subj, text, attach)
                except OutlookUnavailable as why:
                    folder, mailto = prepare_outbox(to, subj, text, attach,
                                                    "expense")
                    self.send_json({
                        "ok": True, "fallback": True, "to": to,
                        "reason": "size" if too_big else "outlook",
                        "detail": str(why),
                        "folder": folder,
                        "folder_name": os.path.basename(folder),
                        "mailto": mailto,
                        "attachments": [os.path.basename(a) for a in attach]})
                    return
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
            if path == "/api/workday/bundle/preview":
                self.send_json(bundle_preview(conn, data.get("expense_ids") or []))
                return
            if path == "/api/workday/bundle/send":
                to = (data.get("to") or "").strip()
                if not to:
                    self.send_json({"error": "Type the email address the "
                                    "bundle should go to."}, 400)
                    return
                ids = [int(i) for i in (data.get("expense_ids") or [])]
                rows, xlsx, zpath, n_files, missing = build_bundle(conn, ids)
                pv = bundle_preview(conn, ids)
                # the body must describe the files actually built
                body = bundle_text(rows, os.path.basename(xlsx),
                                   os.path.basename(zpath) if zpath else "",
                                   n_files, missing)
                attach = [xlsx] + ([zpath] if zpath else [])
                too_big = sum(os.path.getsize(a) for a in attach) > RECEIPT_ATTACH_LIMIT
                why = None
                if data.get("prepare_only"):
                    why = "prepared"
                elif too_big:
                    why = "size"
                else:
                    try:
                        wd_send_mail(to, "", pv["subject"], body, attachment=attach)
                    except OutlookUnavailable:
                        why = "outlook"
                if why:
                    folder, mailto = prepare_outbox(to, pv["subject"], body,
                                                    attach, "bundle")
                    self.send_json({
                        "ok": True, "fallback": True, "reason": why, "to": to,
                        "folder": folder, "folder_name": os.path.basename(folder),
                        "mailto": mailto, "count": len(rows),
                        "attachments": [os.path.basename(a) for a in attach]})
                    return
                for eid in ids:
                    conn.execute("UPDATE expenses SET wd_entry='sent' WHERE id=? "
                                 "AND source='manual'", (eid,))
                cfg = get_setting(conn, "workday_push", {}) or {}
                cfg["owner_email"] = to
                set_setting(conn, "workday_push", cfg)
                conn.commit()
                self.send_json({"ok": True, "to": to, "count": len(rows),
                                "attachments": [os.path.basename(a) for a in attach]})
                return
            if path == "/api/outbox/open":
                if not self.is_local():
                    self.send_json({"error": "not found"}, 404)
                    return
                reveal_folder(safe_under(OUTBOX_DIR, str(data.get("name") or "")))
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
                for k in ("summary_url", "detail_url"):
                    u = (data.get(k) or "").strip()
                    bad = wd_check_raas_url(u) if u else ""
                    if bad:
                        self.send_json({"error": bad}, 400)
                        return
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
                    res = wd_fetch_raas(conn, pw, bool(data.get("allow_stale")))
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
                # Linking a worker to a person attaches the charges already
                # imported under that name, not just future ones.
                if data.get("kind") == "worker" and data.get("target_id"):
                    conn.execute(
                        "UPDATE expenses SET person_id=? WHERE id IN "
                        "(SELECT expense_id FROM workday_lines WHERE "
                        "expense_id IS NOT NULL AND "
                        "LOWER(TRIM(worker))=LOWER(TRIM(?)))",
                        (data["target_id"], data["wd_key"]))
                if data.get("kind") in ("grant", "category"):
                    wd_apply_balances(conn)
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
            if path == "/api/shutdown":
                # Used by a newer copy taking over the port. Loopback only —
                # a program already on this machine could stop us anyway; the
                # CSRF gate above already blocks any web page from reaching it.
                if not self.is_local():
                    self.send_json({"error": "not found"}, 404)
                    return
                self.send_json({"ok": True})
                if SERVER is not None:
                    threading.Thread(target=SERVER.shutdown, daemon=True).start()
                return
            if path == "/api/update/install":
                # Replaces program files, so only from the machine running the
                # app (the CSRF gate above already blocks web pages).
                if not self.is_local():
                    self.send_json({"error": "Updates can only be started "
                                    "from the computer that runs Grants "
                                    "Manager."}, 403)
                    return
                try:
                    self.send_json(update_install())
                except UpdateError as e:
                    self.send_json({"error": str(e)}, 400)
                except Exception as e:  # noqa: BLE001
                    self.send_json({"error": "The update failed (%s). Nothing "
                                    "was changed." % e}, 500)
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
                test = None
                try:
                    test = sqlite3.connect(tmp_path)
                    tables = {r[0] for r in test.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    check = [r[0] for r in test.execute(
                        "PRAGMA integrity_check")]
                except sqlite3.DatabaseError:
                    if test:
                        test.close()
                    os.remove(tmp_path)
                    self.send_json({"error": "That file isn't a valid "
                                    "database."}, 400)
                    return
                test.close()
                if check != ["ok"]:
                    # right table names but damaged pages (a truncated or
                    # half-synced copy): replacing a healthy database with it
                    # would lose everything
                    os.remove(tmp_path)
                    self.send_json({"error": "That backup file is damaged "
                                    "(it failed SQLite's integrity check), so "
                                    "it was NOT restored and your current "
                                    "data is untouched. Try an earlier "
                                    "backup."}, 400)
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
                try:
                    os.replace(tmp_path, DB_PATH)
                except OSError as e:   # Windows refuses while the file is in use
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    self.send_json({"error": "Couldn't restore right now — "
                                    "Windows says the data file is in use (%s). "
                                    "Close other Grants Manager tabs and try "
                                    "again." % (e.strerror or e)}, 400)
                    return
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
            self.send_error_json("POST %s" % path, e)
        finally:
            conn.close()

    def do_DELETE(self):
        if not self.check_host() or not self.check_not_csrf():
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
                # A Workday code that pointed at this grant must not bring it
                # back at the next import (the auto-linker would re-create it):
                # keep the code, but mark it "ignored". Restoring from the
                # trash re-links it.
                for mrow in conn.execute(
                        "SELECT id, target_id FROM workday_map WHERE "
                        "kind='grant' AND target_id=?", (rid,)).fetchall():
                    trash_capture(conn, batch, summary, "unlink", "workday_map",
                                  mrow["id"], column="target_id",
                                  old_value=mrow["target_id"])
                conn.execute("UPDATE workday_map SET target_id=NULL WHERE "
                             "kind='grant' AND target_id=?", (rid,))

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
                # A charge the user deleted must stay deleted when the same
                # Workday report is imported again — but come back if they
                # restore it from the trash (see trash_restore).
                conn.execute("UPDATE workday_lines SET status='deleted' "
                             "WHERE expense_id=? AND status IN "
                             "('imported','matched')", (rid,))
            conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
            conn.commit()
            self.send_json({"ok": True, "batch_id": batch})
        except Exception as e:  # noqa: BLE001
            self.send_error_json("DELETE %s" % path, e)
        finally:
            conn.close()


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

1. **Extract the zip to a real folder first.** On Windows, right-click
   `GrantsManager.zip` — **Extract All...**; on Mac, double-click the zip.
   Do NOT run the starter straight from inside the zip preview — Windows
   runs it from a temporary place where the app files aren't, and it fails
   with a "server.py: No such file" error. Put the extracted folder anywhere
   (Documents, OneDrive, Desktop...) and keep it together — your database
   lives inside it.
2. Start the app:
   - **Mac**: double-click **Start Grants Manager (Mac).command**.
   - **Windows**: double-click **Start Grants Manager (Windows).bat**.

   The **first time only**, your Mac or Windows will probably warn that it
   can't verify the app. That is normal for any program not sold through
   Apple's or Microsoft's store \u2014 nothing is wrong. Let it through once
   (see "If your Mac or Windows blocks it" below) and it opens normally after
   that. If you prefer the command line: Mac `python3 server.py --launch`,
   Windows `py server.py --launch`.
3. Your browser opens at <http://127.0.0.1:8765> with an **empty
   database**. Leave the terminal window open while you use the app.

## If your Mac or Windows blocks it

The app isn't code-signed with a paid Apple/Microsoft certificate, so the
system asks you to confirm the first launch. You do this **once** per
computer; it is not a real error, and it needs no administrator.

**Mac** \u2014 if you see "Apple could not verify ... is free of malware",
click Done, then open **System Settings \u2192 Privacy & Security**, scroll to
the **Security** section, and click **Open Anyway** next to the blocked
starter (confirm with your password/Touch ID). On older macOS instead
**right-click** the starter \u2192 **Open** \u2192 **Open**. Last resort: in
the **Terminal** app type `xattr -dr com.apple.quarantine ` and drag this
folder onto the window, then press Return.

**Windows** \u2014 if you see the blue "Windows protected your PC"
(SmartScreen) box, click **More info**, then **Run anyway**. If the browser
flagged the download, choose **Keep**.

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
(Mac or Windows), with the receipt PDF attached and you CC'd. If Outlook can't
be controlled (the "new Outlook" or none installed), the app prepares a folder
with the files and a ready-to-send email draft instead. Monthly report receipts
are renamed like `03 - 2026-09-15 - 412.75 - Lab supplies.pdf`, matching the
Receipt column of the workbook row they belong to. Click ✏️ on an expense to
attach extra documents (a P-card authorization form, a spreadsheet); they go
with it in the Workday email and the monthly report. Tick expenses and press
"Bundle & send" to email several at once: one spreadsheet (a line per expense,
receipt names in a column) plus one zip of all the receipts.

## Updating to a newer version

When a newer version exists, the app tells you (bell icon, and at the bottom
of **⚙ Settings**). Click it, then **Update now**. The app downloads the
release from this project's GitHub page, installs it over the current copy
and restarts itself. **Your data is never touched** — grants, expenses,
receipts, backups and settings stay exactly where they are. It backs up your
database first and keeps your previous version in `data/code_backups/`, so you
can go back. It works only from the computer running the app, in a folder the
app can write to; otherwise it points you to the download instead.

*By hand instead* (or when coming from a version older than 1.4.7, which
can't update itself): download the new `GrantsManager.zip`, extract it, and
copy **both** your `data` folder (database, backups, settings) **and** your
`receipts` folder from the old copy into the new one, replacing the empty
ones. Then start the new copy — it takes over from the old one on its own.

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
- **The app shows different data than I expected** — you probably opened
  a different copy (folder) of Grants Manager. Every copy keeps its own data
  inside its own folder, and opening a copy replaces whichever copy was
  already running. Open the folder that holds your real `data` folder.
- **Stop the app** — close the terminal window.
"""

WIN_BAT = "\r\n".join([
    "@echo off",
    "setlocal",
    'cd /d "%~dp0"',
    "",
    "rem If server.py isn't beside us, this was launched from INSIDE the .zip.",
    'if not exist "server.py" goto notextracted',
    "",
    "rem Find a working Python 3.9+ without tripping the Store alias. Each",
    "rem candidate is run with an argument (the alias only opens the Store when",
    "rem run with none) and must report 3.9 or newer - an old Python 2 on the",
    "rem PATH must not be mistaken for a working install.",
    'set "PY="',
    'for %%C in ("py -3" "python" "python3") do (',
    '  if not defined PY (',
    '    %%~C -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=%%~C"',
    '  )',
    ')',
    "if defined PY goto havepython",
    'py -3 --version >nul 2>&1 && goto tooold',
    'python --version >nul 2>&1 && goto tooold',
    'python3 --version >nul 2>&1 && goto tooold',
    "goto nopython",
    "",
    ":havepython",
    "%PY% server.py --launch",
    "if %errorlevel%==0 goto :eof",
    "goto startfailed",
    "",
    ":notextracted",
    "echo.",
    "echo   It looks like you opened this from INSIDE the .zip file.",
    "echo   Windows cannot run the app from there.",
    "echo.",
    "echo   Do this instead:",
    "echo     1. Close this window.",
    'echo     2. Right-click GrantsManager.zip and choose "Extract All...".',
    "echo     3. Open the extracted folder.",
    "echo     4. Double-click this file again.",
    "echo.",
    "pause",
    "goto :eof",
    "",
    ":tooold",
    "echo.",
    "echo   The Python on this computer is too old - this app needs Python 3.9",
    "echo   or newer. Installing a current one is free and needs no admin rights.",
    "echo   Opening the Microsoft Store: click \"Get\" (or \"Update\"), wait for it",
    "echo   to finish, then double-click this file again.",
    "echo.",
    'start "" "ms-windows-store://search/?query=Python 3"',
    "pause",
    "goto :eof",
    "",
    ":nopython",
    "echo.",
    "echo   Python 3 isn't installed yet. It's a free, one-time install -",
    "echo   no admin rights needed. Opening the Microsoft Store: click",
    'echo   "Get", wait for it to finish, then double-click this file again.',
    "echo.",
    'start "" "ms-windows-store://search/?query=Python 3"',
    "pause",
    "goto :eof",
    "",
    ":startfailed",
    "echo.",
    "echo   Python is installed, but the app did not start - see the error",
    "echo   above. If it mentions a missing file, make sure you EXTRACTED the",
    "echo   whole folder rather than running from inside the .zip.",
    "echo   Still stuck? Email samuelbf@uark.edu with a screenshot.",
    "echo.",
    "pause",
]) + "\r\n"

MAC_COMMAND = ("#!/bin/bash\n"
               "cd \"$(dirname \"$0\")\"\n"
               "if [ ! -f server.py ]; then\n"
               "  echo\n"
               "  echo \"  This looks like it was opened from inside the .zip.\"\n"
               "  echo \"  Double-click GrantsManager.zip to extract it first,\"\n"
               "  echo \"  open the extracted folder, then double-click this again.\"\n"
               "  echo\n"
               "  read -n 1 -s -r -p \"Press any key to close...\"\n"
               "  echo; exit 1\n"
               "fi\n"
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
    ]
    # the Instructions walkthrough clips — swept rather than listed, so a new
    # one is shared automatically instead of showing as a broken image
    help_dir = os.path.join(APP_DIR, "help")
    if os.path.isdir(help_dir):
        for name in sorted(os.listdir(help_dir)):
            if name.lower().endswith((".gif", ".png")):
                include.append(("app/help/" + name,
                                os.path.join(help_dir, name)))
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


def our_server_info(port):
    """Ask whatever is on `port` to identify itself. Returns the ping dict
    (app/version/dir/pid) if it's Grants Manager, else None."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as resp:
            data = json.loads(resp.read())
        if isinstance(data, dict) and data.get("app") == "grants-manager":
            return data
    except Exception:  # noqa: BLE001 — any failure means "not us"
        pass
    return None


def is_our_server(port):
    return our_server_info(port) is not None


def _pids_on_port(port):
    """PIDs listening on `port`, via lsof (mac/linux) or netstat (windows).
    Best-effort; returns [] if the tools aren't available."""
    import subprocess
    pids = set()
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, text=True, timeout=8).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                        and parts[1].endswith(":%d" % port):
                    if parts[-1].isdigit():
                        pids.add(int(parts[-1]))
        else:
            out = subprocess.run(["lsof", "-ti", "tcp:%d" % port,
                                  "-sTCP:LISTEN"], capture_output=True,
                                 text=True, timeout=8).stdout
            pids.update(int(x) for x in out.split() if x.isdigit())
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    pids.discard(os.getpid())
    return list(pids)


def _kill_pid(pid):
    import subprocess
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=8)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass


def takeover_port(port):
    """Free `port` from an existing Grants Manager instance so this (newer)
    copy can run the current code. Ask it to quit gracefully first; if it's an
    older build that can't, or it doesn't let go, kill the process on the port.
    Returns True if the port is free afterwards."""
    import urllib.request
    info = our_server_info(port) or {}
    # 1. graceful: newer builds honour /api/shutdown
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/shutdown" % port, data=b"{}",
            headers={"Content-Type": "application/json", "X-Grants-App": "1"},
            method="POST")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:  # noqa: BLE001 — older build or already gone
        pass
    if _wait_port_free(port, 4):
        return True
    # 2. force: kill the process holding the port (we've confirmed it's ours)
    pids = _pids_on_port(port)
    if info.get("pid") and info["pid"] not in pids:
        pids.append(info["pid"])
    for pid in pids:
        _kill_pid(pid)
    return _wait_port_free(port, 5)


def _wait_port_free(port, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if not port_in_use(port):
            return True
        time.sleep(0.25)
    return not port_in_use(port)


def find_free_port(start, tries=50):
    port = start
    for _ in range(tries):
        if not port_in_use(port):
            return port
        port += 1
    raise RuntimeError(f"No free port found near {start}")


def main():
    global PORT
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    launch = "--launch" in sys.argv
    if "--port" in sys.argv:
        try:
            PORT = int(sys.argv[sys.argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    if port_in_use(PORT):
        info = our_server_info(PORT)
        if info is not None:
            same_install = (info.get("dir") == BASE_DIR
                            and info.get("version") == APP_VERSION)
            if same_install:
                # this exact copy is already running — just show it
                url = f"http://127.0.0.1:{PORT}"
                if launch:
                    webbrowser.open(url)
                    return
                print(f"Already running at {url}")
                return
            # A different or older copy is running. The user opened THIS one to
            # use it, so take the port over and run the current code.
            print("A different Grants Manager is running on port %d — "
                  "switching to this version..." % PORT)
            if not takeover_port(PORT):
                # couldn't free it; run beside it rather than not at all
                PORT = find_free_port(PORT + 1)
        else:
            # Something unrelated owns the port. Never open the browser to a
            # stranger's server; use our own free port instead.
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
    # Look for a new release in the background — never delays startup, and is
    # silently skipped when there's no internet.
    threading.Thread(target=check_for_update, daemon=True).start()
    try:   # older versions kept a secret here for the removed phone feature
        os.remove(os.path.join(DATA_DIR, "access_key.txt"))
    except OSError:
        pass
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
    # Listen on this computer only. Nothing else on the network can reach the
    # app, and Windows never shows its "allow this app through the firewall"
    # prompt (which needs admin rights) for a loopback-only listener.
    global SERVER
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = False   # server_close() then waits for requests
    SERVER = server
    print(f"Grants Manager running at {url}  (Ctrl+C to stop)")
    if launch:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # let a request already in progress (an email being sent, an import)
        # finish before the process exits, instead of cutting it off half-done
        server.server_close()


if __name__ == "__main__":
    main()
