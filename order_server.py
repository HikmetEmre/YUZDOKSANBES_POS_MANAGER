# order_server.py
# =====================================================
# YUZDOKSANBES POS — Backend (Step 1: hardened)
#
# Runs on the café's MAIN COMPUTER. Tablets/phones connect to it over the LAN
# (e.g. http://192.168.1.50:8000). The server is the single source of truth and
# the only thing that writes to the database.
#
# Step 1 upgrades vs original:
#   - SQLite WAL mode + busy timeout  -> safe concurrent access from many tablets
#   - Connection context-manager      -> connections always close, even on error
#   - SERVER-side total calculation   -> client cannot tamper with prices/totals
#   - SERVER-side PIN auth            -> PIN no longer shipped to the browser
#   - Pydantic validation + clean errors
#   - Frontend split into /static (index.html, style.css, app.js) + /config API
# =====================================================

from contextlib import contextmanager
from datetime import datetime, timedelta, date as Date
from pathlib import Path
from typing import List, Optional
import json
import logging
import logging.handlers
import math
import os
import re
import sqlite3
import sys
import threading
import warnings

# --selftest (run by build.bat on the BUILT .exe): must pick a throwaway data
# folder BEFORE config/paths are imported — the database opens at import time.
# Never touches the real data folder, never mirrors backups, never opens a browser.
if "--selftest" in sys.argv:
    import tempfile
    os.environ["POS_DATA_DIR"] = tempfile.mkdtemp(prefix="yuzdoksanbes_selftest_")
    os.environ["POS_BACKUP_MIRRORS"] = ""
    os.environ["POS_MONITOR_DIR"] = ""      # self-test never writes to Google Drive
    os.environ["POS_NO_BROWSER"] = "1"

# FastAPI marks @app.on_event as deprecated (lifespan handlers replace it). It
# still works fine; keep the warning out of the staff-facing console window.
# (the message starts with a line break, hence the leading \s*)
warnings.filterwarnings("ignore", message=r"\s*on_event is deprecated", category=DeprecationWarning)

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import JSONResponse, FileResponse, Response, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
import uvicorn

import config
import backup
import paths
import auth
import mdns
import daily_reports
from version import __version__, APP_NAME

app = FastAPI(title="YUZDOKSANBES POS")

log = logging.getLogger("order_server")


# =====================================================
# LOGGING — persistent system-health record (see config.py LOGGING block)
# Without this, the app's log.info()/warning()/exception() calls have nowhere to
# go (no handler) so nothing is saved. This wires the ROOT logger to a rotating
# file (logs/pos.log) PLUS the console, so backups, PIN lockouts, unhandled 500s
# and startup warnings are all captured on disk AND shown live in the window.
# =====================================================
def _setup_logging() -> None:
    """Configure root logging once (file + console). Idempotent: re-running only
    refreshes OUR handlers, never duplicates them, and never crashes the app."""
    root = logging.getLogger()
    level = getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO)
    root.setLevel(level)
    # fontTools (used by the PDF reports) logs hundreds of INFO "subset" lines
    # per report — keep it to real warnings so the console and pos.log stay readable.
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    # Drop any handler we added on a previous call (safe re-entry / reloads).
    for h in list(root.handlers):
        if getattr(h, "_pos_handler", False):
            root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Rotating file handler — the on-disk audit trail. Wrapped so a locked/unwritable
    # log folder can never stop the POS from starting; console logging still works.
    try:
        log_dir = Path(paths.data_path(config.LOG_DIR))
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(log_dir / config.LOG_FILE),
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh._pos_handler = True
        root.addHandler(fh)
    except Exception as e:
        logging.getLogger("order_server").warning("File logging disabled: %s", e)

    # Console handler — live output when run from a window / the .bat launcher.
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch._pos_handler = True
    root.addHandler(ch)


_setup_logging()


# =====================================================
# DATABASE
# =====================================================

@contextmanager
def db():
    """Yields a connection that is ALWAYS committed+closed (or rolled back on error)."""
    conn = sqlite3.connect(paths.data_path(config.DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL lets readers and one writer work concurrently -> no 'database is locked'
    # when several tablets poll while one writes.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")  # wait up to 10s for a lock
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            table_no      INTEGER NOT NULL,
            status        TEXT NOT NULL DEFAULT 'open',
            created_at    TEXT NOT NULL,
            paid_at       TEXT,
            payment_type  TEXT,
            total_amount  REAL DEFAULT 0,
            barista_status TEXT DEFAULT 'hazirlaniyor'
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id     INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            quantity     REAL NOT NULL,
            price        REAL NOT NULL,
            created_at   TEXT NOT NULL,
            paid         INTEGER NOT NULL DEFAULT 0,   -- 0 = unpaid, 1 = paid (Step 2)
            paid_at      TEXT,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS sales_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            table_no     INTEGER,
            total_amount REAL,
            payment_type TEXT,
            paid_at      TEXT
        );

        -- Step 2: each payment event (one 'settle' can create several rows,
        -- e.g. a cash portion AND a card portion for the same selection).
        CREATE TABLE IF NOT EXISTS payments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id     INTEGER NOT NULL,
            table_no     INTEGER NOT NULL,
            amount       REAL NOT NULL,
            payment_type TEXT NOT NULL,
            paid_at      TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        );

        -- Dashboard: calendar tasks / to-do. Each task is tied to a date.
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_date   TEXT NOT NULL,            -- 'YYYY-MM-DD' (the day it's due)
            title       TEXT NOT NULL,
            category    TEXT DEFAULT 'genel',     -- e.g. cleaning, stock, order, genel
            done        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            done_at     TEXT
        );

        -- Gelir/Gider: expenses (qty × unit_price, or a flat amount).
        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_date TEXT NOT NULL,           -- 'YYYY-MM-DD'
            category    TEXT NOT NULL,            -- e.g. 'Kahve Çekirdeği', 'Süt'
            description TEXT,
            quantity    REAL NOT NULL DEFAULT 1,
            unit_price  REAL NOT NULL DEFAULT 0,
            amount      REAL NOT NULL,            -- quantity × unit_price (server-computed)
            created_at  TEXT NOT NULL
        );

        -- Gelir/Gider: manual/custom income entries (beyond POS sales).
        CREATE TABLE IF NOT EXISTS manual_income (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            income_date TEXT NOT NULL,            -- 'YYYY-MM-DD'
            description TEXT NOT NULL,
            amount      REAL NOT NULL,
            created_at  TEXT NOT NULL
        );

        -- Stock: current quantity per item (recount-replace model).
        CREATE TABLE IF NOT EXISTS stock_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            unit        TEXT DEFAULT 'adet',
            quantity    REAL NOT NULL DEFAULT 0,
            threshold   REAL NOT NULL DEFAULT 5,
            updated_at  TEXT,
            updated_by  TEXT
        );

        -- Stock: every count logged (who + when + value).
        CREATE TABLE IF NOT EXISTS stock_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     INTEGER NOT NULL,
            item_name   TEXT NOT NULL,
            quantity    REAL NOT NULL,
            counted_by  TEXT NOT NULL,
            counted_at  TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES stock_items(id) ON DELETE CASCADE
        );

        -- Menu: products + prices, editable from the admin "Ayarlar" tab.
        -- Seeded once from config.MENU, then the DB is the source of truth so
        -- price/name/section edits take effect everywhere (POS, waiter, QR).
        CREATE TABLE IF NOT EXISTS menu_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL,            -- section, e.g. 'ESPRESSO BAR'
            name        TEXT NOT NULL UNIQUE,     -- product identity is its name
            price       REAL NOT NULL DEFAULT 0,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT
        );

        -- Small key/value store for settings edited from the dashboard
        -- (e.g. the coffee-machine maintenance threshold).
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- Coffee machine maintenance log. Each entry resets the coffee counter;
        -- coffee_count = how many coffees were made since the previous one.
        CREATE TABLE IF NOT EXISTS coffee_maintenance (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            done_at      TEXT NOT NULL,           -- ISO local time
            note         TEXT,
            coffee_count INTEGER
        );

        -- Tables & areas (dashboard Ayarlar → Masalar). Seeded ONCE from
        -- config.AREAS; after that the café edits them in the dashboard.
        -- A table is identified by its number alone, so table_no is unique
        -- across ALL areas.
        CREATE TABLE IF NOT EXISTS table_areas (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS cafe_tables (
            table_no INTEGER PRIMARY KEY,
            area_id  INTEGER NOT NULL
        );

        -- "İptal Edilenler": every cancelled product, with its reason. Written
        -- in the SAME transaction as the cancellation itself.
        CREATE TABLE IF NOT EXISTS cancellations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            cancelled_at TEXT NOT NULL,           -- ISO local time
            table_no     INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            quantity     REAL NOT NULL,           -- units cancelled
            unit_price   REAL NOT NULL,           -- authoritative menu price
            amount       REAL NOT NULL,           -- quantity × unit_price
            reason       TEXT NOT NULL,           -- one of config.CANCEL_REASONS
            note         TEXT,                    -- typed reason for "Diğer"
            kind         TEXT NOT NULL,           -- 'item' (✕) | 'order' (whole bill)
            cancelled_by TEXT NOT NULL,           -- 'waiter' | 'checkout'
            ordered_at   TEXT                     -- when the line was ordered
        );
        CREATE INDEX IF NOT EXISTS idx_cancel_at ON cancellations(cancelled_at);

        -- Recipes (dashboard "Reçeteler"): one row per recipe, free text.
        -- NOCASE so "latte" and "Latte" can't both exist.
        CREATE TABLE IF NOT EXISTS recipes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            ingredients TEXT NOT NULL DEFAULT '',
            steps       TEXT NOT NULL DEFAULT '',
            updated_at  TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_menu_cat      ON menu_items(category, sort_order, id);
        CREATE INDEX IF NOT EXISTS idx_orders_open   ON orders(table_no, status);
        CREATE INDEX IF NOT EXISTS idx_items_order   ON order_items(order_id);
        CREATE INDEX IF NOT EXISTS idx_sales_paid_at ON sales_log(paid_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_date    ON tasks(task_date);
        CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
        CREATE INDEX IF NOT EXISTS idx_income_date   ON manual_income(income_date);
        CREATE INDEX IF NOT EXISTS idx_stock_logs    ON stock_logs(item_id, counted_at);
        """)

        # --- Migration for databases created before Step 2 ---
        # Adds the new columns if they don't exist yet (safe to run every boot).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(order_items)").fetchall()}
        if "paid" not in cols:
            conn.execute("ALTER TABLE order_items ADD COLUMN paid INTEGER NOT NULL DEFAULT 0")
        if "paid_at" not in cols:
            conn.execute("ALTER TABLE order_items ADD COLUMN paid_at TEXT")
        # Step 2.1: per-unit payment. paid_qty = how many of `quantity` are settled.
        # A line is fully paid when paid_qty >= quantity.
        if "paid_qty" not in cols:
            conn.execute("ALTER TABLE order_items ADD COLUMN paid_qty REAL NOT NULL DEFAULT 0")
            # Back-fill: any line previously marked fully paid gets paid_qty = quantity.
            conn.execute("UPDATE order_items SET paid_qty = quantity WHERE paid = 1")
        # Roles step: items from the QR 'customer' surface arrive 'pending'
        # and need staff to accept; staff-created items are 'confirmed'.
        if "item_status" not in cols:
            conn.execute("ALTER TABLE order_items ADD COLUMN item_status TEXT NOT NULL DEFAULT 'confirmed'")

        # Concurrency guard: at most ONE open order per table. First collapse
        # any pre-existing duplicates (merge their items into the oldest order,
        # then drop the empties), then enforce it with a partial-unique index so
        # a race can't recreate them. create_order catches the resulting
        # IntegrityError and reuses the winning order.
        dup_tables = conn.execute(
            "SELECT table_no FROM orders WHERE status='open'"
            " GROUP BY table_no HAVING COUNT(*) > 1"
        ).fetchall()
        for d in dup_tables:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM orders WHERE table_no=? AND status='open' ORDER BY id",
                (d["table_no"],)
            ).fetchall()]
            keeper, extras = ids[0], ids[1:]
            for ex in extras:
                conn.execute("UPDATE order_items SET order_id=? WHERE order_id=?", (keeper, ex))
                conn.execute("DELETE FROM orders WHERE id=?", (ex,))
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_per_table"
            " ON orders(table_no) WHERE status='open'"
        )

        # Coffee-machine counter defaults. INSERT OR IGNORE = only on the first
        # run; after that the values are edited in the dashboard. coffee_start
        # makes the counter begin at 0 the day the feature goes live.
        for key, value in (
            ("coffee_start",       datetime.now().isoformat()),
            ("coffee_threshold",   str(config.COFFEE_MAINT_THRESHOLD_DEFAULT)),
            ("coffee_sections",    json.dumps(config.COFFEE_SECTIONS_DEFAULT, ensure_ascii=False)),
            ("coffee_extra_items", json.dumps(config.COFFEE_EXTRA_ITEMS_DEFAULT, ensure_ascii=False)),
        ):
            conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (key, value))

        # Table names: every table keeps its internal number (orders, QR links
        # /masa{n}, history) plus a display name the café can change
        # ("54", "Toplantı Masası"). Old DBs: name = the number.
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(cafe_tables)").fetchall()}
        if "name" not in tcols:
            conn.execute("ALTER TABLE cafe_tables ADD COLUMN name TEXT")
        conn.execute("UPDATE cafe_tables SET name = CAST(table_no AS TEXT) WHERE name IS NULL OR TRIM(name) = ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_table_name ON cafe_tables(name COLLATE NOCASE)")

        # Tables/areas: copy config.AREAS into the DB exactly once. The flag
        # (not "is the table empty?") decides, so a café that deliberately
        # deletes every area doesn't get the defaults back on the next start.
        if not conn.execute("SELECT 1 FROM app_settings WHERE key='tables_seeded'").fetchone():
            for i, area in enumerate(config.AREAS):
                cur = conn.execute("INSERT OR IGNORE INTO table_areas (name, sort_order) VALUES (?, ?)",
                                   (area["name"], i))
                aid = cur.lastrowid or conn.execute(
                    "SELECT id FROM table_areas WHERE name=?", (area["name"],)).fetchone()["id"]
                for n in area["tables"]:
                    conn.execute("INSERT OR IGNORE INTO cafe_tables (table_no, area_id, name) VALUES (?, ?, ?)",
                                 (n, aid, str(n)))
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('tables_seeded', '1')")

        # Sold-items ledger: ONE row per line per payment, dated when THAT
        # payment happened (the same clock as sales_log revenue). Reports count
        # quantities from here, so split payments, tables still open at report
        # time and bills paid across midnight all land on the right day.
        # `batch` ties a payment's ledger rows to its sales_log rows, so
        # deleting a sale ("Satışı sil") also removes the items it sold.
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sold_items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id     INTEGER,
            item_id      INTEGER,
            table_no     INTEGER,
            product_name TEXT NOT NULL,
            qty          REAL NOT NULL,
            unit_price   REAL NOT NULL,
            amount       REAL NOT NULL,
            paid_at      TEXT NOT NULL,
            batch        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sold_paid_at ON sold_items(paid_at);
        CREATE INDEX IF NOT EXISTS idx_sold_batch   ON sold_items(batch);
        """)
        scols = {r["name"] for r in conn.execute("PRAGMA table_info(sales_log)").fetchall()}
        if "batch" not in scols:
            conn.execute("ALTER TABLE sales_log ADD COLUMN batch TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_batch ON sales_log(batch)")
        # One-time back-fill from data paid before the ledger existed. The old
        # schema only kept the LAST payment time per line, so that is the best
        # date available for history; everything paid from now on is exact.
        if not conn.execute("SELECT 1 FROM app_settings WHERE key='sold_items_backfilled'").fetchone():
            conn.execute(
                "INSERT INTO sold_items (order_id, item_id, table_no, product_name, qty,"
                "                        unit_price, amount, paid_at, batch)"
                " SELECT i.order_id, i.id, o.table_no, i.product_name, i.paid_qty,"
                "        i.price, i.paid_qty * i.price, COALESCE(i.paid_at, o.paid_at), NULL"
                " FROM order_items i JOIN orders o ON o.id = i.order_id"
                " WHERE i.paid_qty > 0 AND COALESCE(i.paid_at, o.paid_at) IS NOT NULL"
                "   AND NOT EXISTS (SELECT 1 FROM sold_items s WHERE s.item_id = i.id)")
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('sold_items_backfilled', '1')")


init_db()


def seed_stock_if_empty():
    """First-run seed: create stock items from expense categories + extras.
    After this the DB owns the list; editing config won't disturb existing stock."""
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM stock_items").fetchone()["c"]
        if n > 0:
            return
        skip = ("Kira", "Elektrik", "Su Faturası", "Personel", "Diğer")
        names = list(dict.fromkeys(
            [c for c in config.EXPENSE_CATEGORIES if c not in skip] + config.STOCK_SEED_EXTRA
        ))
        for nm in names:
            conn.execute(
                "INSERT OR IGNORE INTO stock_items (name, quantity, threshold) VALUES (?, 0, ?)",
                (nm, config.STOCK_THRESHOLD_DEFAULT)
            )


seed_stock_if_empty()


def seed_menu_if_empty():
    """First-run seed: copy config.MENU into the menu_items table, preserving
    section + item order. After this the DB owns the menu; the admin "Ayarlar"
    tab edits it, and editing config.MENU won't disturb the live menu."""
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM menu_items").fetchone()["c"]
        if n > 0:
            return
        now = datetime.now().isoformat()
        order = 0
        for category, items in config.MENU.items():
            for (name, price) in items:
                conn.execute(
                    "INSERT OR IGNORE INTO menu_items (category, name, price, sort_order, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (category, name, price, order, now)
                )
                order += 1


seed_menu_if_empty()


# --- Live menu cache ---------------------------------------------------------
# Read once from the DB and kept in memory so per-item price lookups don't hit
# SQLite in hot loops. load_menu() rebuilds both structures and atomically swaps
# the module globals; every admin edit calls it so changes show up everywhere.
_MENU_SECTIONS = []     # ordered [(category, [{"id","name","price"}, ...]), ...]
_PRICE = {}             # product_name -> authoritative price


def load_menu():
    global _MENU_SECTIONS, _PRICE
    with db() as conn:
        rows = conn.execute(
            "SELECT id, category, name, price FROM menu_items ORDER BY sort_order, id"
        ).fetchall()
    sections, by_cat, price = [], {}, {}
    for r in rows:
        price[r["name"]] = r["price"]
        if r["category"] not in by_cat:
            by_cat[r["category"]] = []
            sections.append((r["category"], by_cat[r["category"]]))
        by_cat[r["category"]].append({"id": r["id"], "name": r["name"], "price": r["price"]})
    _MENU_SECTIONS = sections      # atomic reference swap (readers see old or new)
    _PRICE = price


load_menu()


# Live cache of the café's areas + tables (edited in Ayarlar → Masalar).
# Same pattern as the menu: reload after every edit, swap references atomically.
_AREAS = []          # [{"id": "a3", "db_id": 3, "name": "İç Mekan", "tables": [11, 12, …]}, …]
_TABLE_SET = set()   # every valid table number
_TABLE_NAMES = {}    # table number -> display name ("12", "Toplantı Masası")


def table_label(no) -> str:
    """What people read: a digits-only name → 'Masa 54'; a text name as written
    ('Toplantı Masası'). A table deleted since (old history) → 'Masa <no>'."""
    name = _TABLE_NAMES.get(no)
    if name is None:
        try:
            name = _TABLE_NAMES.get(int(no))
        except (TypeError, ValueError):
            name = None
    if not name:
        return f"Masa {no}"
    return f"Masa {name}" if name.isdigit() else name


def load_areas():
    global _AREAS, _TABLE_SET, _TABLE_NAMES
    with db() as conn:
        areas = conn.execute("SELECT id, name FROM table_areas ORDER BY sort_order, id").fetchall()
        rows = conn.execute("SELECT table_no, area_id, name FROM cafe_tables ORDER BY table_no").fetchall()
    _TABLE_NAMES = {r["table_no"]: (r["name"] or str(r["table_no"])) for r in rows}
    by_area = {a["id"]: [] for a in areas}
    for r in rows:
        by_area.setdefault(r["area_id"], []).append(r["table_no"])
    # "a<id>" is safe for the HTML ids / onclick handlers the POS and waiter pages build.
    _AREAS = [{"id": f"a{a['id']}", "db_id": a["id"], "name": a["name"], "tables": by_area[a["id"]]}
              for a in areas]
    _TABLE_SET = {r["table_no"] for r in rows}


load_areas()


# The port the server actually runs on (80, or FALLBACK_PORT if 80 is taken).
# Set by main() before uvicorn starts; used for QR codes, links and mDNS.
ACTIVE_PORT = config.PORT


def _port_suffix() -> str:
    return "" if ACTIVE_PORT == 80 else f":{ACTIVE_PORT}"


def public_base() -> str:
    """Base URL phones use: http://siparis.local when mDNS runs, else the IP."""
    host = mdns.hostname() if mdns.is_running() else mdns.lan_ip()
    return f"http://{host}{_port_suffix()}"


@app.on_event("startup")
def _startup():
    log.info("YUZDOKSANBES POS %s starting on %s:%s (data → %s)",
             __version__, config.HOST, ACTIVE_PORT, paths.app_dir())
    # Loudly warn if the app is running on the built-in placeholder PINs
    # (i.e. POS_CHECKOUT_PIN / POS_WAITER_PIN were not set). Never deploy like this.
    if config.USING_PLACEHOLDER_PINS:
        log.warning(
            "SECURITY: placeholder login PIN(s) in use. Set POS_CHECKOUT_PIN and "
            "POS_WAITER_PIN before deployment (run set_credentials.bat)."
        )
    # Begin automatic DB backups (see ARCHITECTURE.md §8 / backup.py).
    backup.start_backup_scheduler()
    # Announce siparis.local on the LAN (never fatal — see mdns.py).
    mdns.start(ACTIVE_PORT)
    # Live status picture on Google Drive for the owner's phone (monitor.py).
    if config.MONITOR_ENABLED:
        threading.Thread(target=_monitor_loop, daemon=True, name="live-monitor").start()
    # Save each finished day's report PDF automatically (daily_reports.py).
    if config.DAILY_REPORT_ENABLED:
        _start_daily_reports()
    # Installed here (not at import) because uvicorn sets up its loggers just
    # before startup and would otherwise not have our filter yet.
    _access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _QuietProbeFilter) for f in _access.filters):
        _access.addFilter(_QuietProbeFilter())
    _print_banner()


def _day_has_activity(day: str) -> bool:
    """Anything worth a daily report on this day: a sale, an expense, extra
    income or a task. A closed day makes no empty PDF."""
    nxt = (Date.fromisoformat(day) + timedelta(days=1)).isoformat()
    with db() as conn:
        return bool(conn.execute(
            "SELECT EXISTS(SELECT 1 FROM sales_log WHERE paid_at >= ? AND paid_at < ?)"
            "    OR EXISTS(SELECT 1 FROM sold_items WHERE paid_at >= ? AND paid_at < ?)"
            "    OR EXISTS(SELECT 1 FROM expenses WHERE expense_date = ?)"
            "    OR EXISTS(SELECT 1 FROM manual_income WHERE income_date = ?)"
            "    OR EXISTS(SELECT 1 FROM tasks WHERE task_date = ?) AS a",
            (day, nxt, day, nxt, day, day, day)).fetchone()["a"])


def _daily_report_state():
    with db() as conn:
        return _setting(conn, daily_reports.STATE_KEY)


def _set_daily_report_state(day: str) -> None:
    with db() as conn:
        _set_setting(conn, daily_reports.STATE_KEY, day)


def _start_daily_reports() -> None:
    daily_reports.start(_build_daily_pdf, _day_has_activity,
                        _daily_report_state, _set_daily_report_state)


def monitor_data() -> dict:
    """Everything the live status picture shows (monitor.py draws it)."""
    today = Date.today()
    area_of = {n: a["name"] for a in _AREAS for n in a["tables"]}
    with db() as conn:
        orders = conn.execute(
            "SELECT id, table_no, created_at FROM orders WHERE status='open'").fetchall()
        tables = []
        for o in orders:
            items = conn.execute(
                "SELECT product_name, price, paid_qty, item_status FROM order_items WHERE order_id=?",
                (o["id"],)).fetchall()
            total = _order_total(conn, o["id"])
            paid = sum(_price_of(i["product_name"], i["price"]) * (i["paid_qty"] or 0) for i in items)
            tables.append({
                "table_no": o["table_no"], "area": area_of.get(o["table_no"], ""),
                "name": _TABLE_NAMES.get(o["table_no"], str(o["table_no"])),
                "label": table_label(o["table_no"]),
                "open_seconds": _open_seconds(o["created_at"]),
                "total": total, "paid": paid, "remaining": max(0.0, total - paid),
                "pending": any(i["item_status"] == "pending" for i in items),
            })
        revenue = conn.execute(
            "SELECT COALESCE(SUM(total_amount), 0) AS s FROM sales_log WHERE paid_at >= ? AND paid_at < ?",
            (today.isoformat(), (today + timedelta(days=1)).isoformat())).fetchone()["s"]
        cx = conn.execute(
            "SELECT cancelled_at, table_no, product_name, quantity, amount, reason, note"
            " FROM cancellations WHERE cancelled_at >= ? AND cancelled_at < ?"
            " ORDER BY cancelled_at DESC, id DESC",
            (today.isoformat(), (today + timedelta(days=1)).isoformat())).fetchall()
    tables.sort(key=lambda t: -t["open_seconds"])          # longest-waiting first
    return {
        "cafe": config.CAFE_NAME, "version": __version__,
        "updated": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        "tables": tables,
        "open_total": sum(t["total"] for t in tables),
        "today_revenue": revenue,
        "cancel_count": len(cx), "cancel_amount": sum(c["amount"] for c in cx),
        "cancels": [{"time": c["cancelled_at"][11:16], "table_no": c["table_no"],
                     "table_label": table_label(c["table_no"]),
                     "product": c["product_name"], "qty": c["quantity"],
                     "amount": c["amount"], "reason": c["reason"], "note": c["note"]} for c in cx],
    }


def _monitor_loop() -> None:
    """Background: rewrite the Drive status picture every MONITOR_EVERY_SEC.
    A failure (Drive offline, disk full…) is logged once per kind and retried."""
    import time as _t
    import monitor
    last_err = None
    while True:
        try:
            monitor.write_snapshot(monitor_data())
            last_err = None
        except Exception as e:
            if repr(e) != last_err:
                log.warning("Live monitor snapshot failed (will retry): %r", e)
                last_err = repr(e)
        _t.sleep(max(5, config.MONITOR_EVERY_SEC))


class _QuietProbeFilter(logging.Filter):
    """Hide Windows' proxy auto-detect probe ("GET /wpad.dat" → 404) from the
    console and pos.log. Windows asks port 80 for it on its own; since the POS
    runs on port 80 the request lands here. Answering 404 ("no proxy here") is
    correct — it just isn't worth a log line. Every other request is logged."""
    _HIDDEN = ("/wpad.dat",)

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        path = str(args[2]) if len(args) >= 3 else ""
        return path.split("?", 1)[0].lower() not in self._HIDDEN


@app.on_event("shutdown")
def _shutdown():
    mdns.stop()


def _print_banner() -> None:
    """Clear Turkish start-up screen for the console window staff will see."""
    ip_base = f"http://{mdns.lan_ip()}{_port_suffix()}"
    name_base = f"http://{mdns.hostname()}{_port_suffix()}" if mdns.is_running() else None
    main = name_base or ip_base
    lines = [
        "",
        "=" * 64,
        f"  {APP_NAME} {__version__}  —  ÇALIŞIYOR",
        "=" * 64,
        f"  Kasa (POS)     : {main}/",
        f"  Garson         : {main}/garson",
        f"  Yönetim paneli : {main}/dashboard",
        f"  QR kodları     : {main}/qr-codes   (yazdırmak için)",
        f"  Müşteri örneği : {main}/masa15",
    ]
    if name_base:
        lines.append(f"  Yedek adres    : {ip_base}/   (siparis.local açılmazsa)")
    else:
        lines.append("  Not: siparis.local başlatılamadı — IP adresi kullanılıyor.")
    if ACTIVE_PORT != config.PORT:
        lines.append(f"  Not: port {config.PORT} dolu olduğu için {ACTIVE_PORT} kullanılıyor.")
    lines += [
        f"  Veriler        : {paths.app_dir()}",
        "-" * 64,
        "  Bu pencere AÇIK kalmalı. Kapatırsanız POS durur.",
        "=" * 64,
        "",
    ]
    try:
        print("\n".join(lines), flush=True)
    except Exception:
        pass   # a console that can't print must never stop the POS


# =====================================================
# MODELS
# =====================================================

class Item(BaseModel):
    product_name: str
    quantity: float = Field(gt=0)
    price: float = Field(ge=0)  # client-sent price is IGNORED for totals (see compute below)


class OrderCreate(BaseModel):
    table_no: int = Field(ge=1)
    items: List[Item]


class PaymentRequest(BaseModel):
    # NOTE: total_amount removed on purpose — the server computes it. Client cannot set it.
    table_no: int = Field(ge=1)
    payment_type: str  # 'cash' | 'card'


# --- Step 2: split-by-item payment across methods ---

class PaymentSplit(BaseModel):
    """One portion of a payment, e.g. {method:'cash', amount:190}."""
    method: str          # 'cash' | 'card'
    amount: float = Field(gt=0)


class PayLine(BaseModel):
    """Settle `qty` units of a specific order line, e.g. 4 of the 12 teas."""
    item_id: int
    qty: float = Field(gt=0)


class PayItemsRequest(BaseModel):
    table_no: int = Field(ge=1)
    lines: List[PayLine] = Field(min_length=1)        # which units are being settled
    splits: List[PaymentSplit] = Field(min_length=1)  # how it's paid (cash, card, or both)


class CancelRequest(BaseModel):
    table_no: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=40)
    note: Optional[str] = Field(default=None, max_length=120)


class StatusUpdate(BaseModel):
    order_id: int
    status: str


class RemoveItem(BaseModel):
    table_no: int = Field(ge=1)
    item_id: int
    reason: str = Field(min_length=1, max_length=40)
    note: Optional[str] = Field(default=None, max_length=120)
    qty: Optional[int] = Field(default=None, ge=1)    # None = the whole line


class AcceptItems(BaseModel):
    table_no: int = Field(ge=1)
    item_ids: Optional[List[int]] = None  # None = accept ALL pending items on the table


class PinRequest(BaseModel):
    pin: str


# --- Dashboard: tasks ---
class TaskCreate(BaseModel):
    task_date: str                     # 'YYYY-MM-DD'
    title: str
    category: Optional[str] = "genel"

class TaskUpdate(BaseModel):
    id: int
    title: Optional[str] = None
    task_date: Optional[str] = None
    category: Optional[str] = None
    done: Optional[bool] = None

class TaskId(BaseModel):
    id: int


# --- Gelir/Gider ---
class ExpenseCreate(BaseModel):
    expense_date: str                  # 'YYYY-MM-DD'
    category: str
    description: Optional[str] = ""
    quantity: float = Field(gt=0, default=1)
    unit_price: float = Field(ge=0)

class IncomeCreate(BaseModel):
    income_date: str                   # 'YYYY-MM-DD'
    description: str
    amount: float = Field(gt=0)

class EntryId(BaseModel):
    id: int


# --- Tables & areas (Ayarlar → Masalar) ---
class AreaIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Bölge adı boş olamaz")
        return v


class AreaRename(AreaIn):
    id: int


class TablesAdd(BaseModel):
    area_id: int
    spec: str = Field(min_length=1, max_length=200)   # "16", "16-20", "16-20, 25, 30-32"


class TableRef(BaseModel):
    table_no: int


class TableRename(BaseModel):
    table_no: int
    name: str = Field(min_length=1, max_length=60)    # checked by _clean_table_name


# --- Recipes ---
class RecipeIn(BaseModel):
    name:        str = Field(min_length=1, max_length=80)
    ingredients: str = Field(default="", max_length=2000)
    steps:       str = Field(default="", max_length=4000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Reçete adı boş olamaz")
        return v


class RecipeUpdate(RecipeIn):
    id: int


# --- Coffee machine ---
class CoffeeSettings(BaseModel):
    threshold: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    sections:  Optional[List[str]] = None    # menu sections whose items count


class CoffeeMaintenance(BaseModel):
    note: Optional[str] = Field(default=None, max_length=200)


# --- Stock ---
class StockCount(BaseModel):
    id: int                            # stock item id
    quantity: float = Field(ge=0)
    counted_by: str

class StockItemCreate(BaseModel):
    name: str
    unit: Optional[str] = "adet"
    threshold: Optional[float] = None

class StockThreshold(BaseModel):
    id: int
    threshold: float = Field(ge=0)


# --- Menu / Ayarlar (admin editable menu) ---
class MenuItemCreate(BaseModel):
    category: str
    name: str
    price: float = Field(ge=0)

class MenuItemUpdate(BaseModel):
    id: int
    name: Optional[str] = None
    price: Optional[float] = Field(default=None, ge=0)
    category: Optional[str] = None

class SectionRename(BaseModel):
    old: str
    new: str

class SectionRef(BaseModel):
    category: str


# =====================================================
# HELPERS
# =====================================================

def _price_of(product_name: str, fallback: float) -> float:
    """Authoritative price from the live menu cache; falls back to the client
    price only for off-menu/custom items not in the lookup."""
    return float(_PRICE.get(product_name, fallback))


def _order_total(conn, order_id: int) -> float:
    rows = conn.execute(
        "SELECT product_name, quantity, price FROM order_items WHERE order_id=?",
        (order_id,)
    ).fetchall()
    return sum(_price_of(r["product_name"], r["price"]) * r["quantity"] for r in rows)


def _validate_payment_type(pt: str) -> str:
    if pt not in ("cash", "card"):
        raise HTTPException(status_code=400, detail="payment_type must be 'cash' or 'card'")
    return pt


def _valid_table(table_no: int) -> bool:
    """True if table_no is one of the café's tables (Ayarlar → Masalar)."""
    return table_no in _TABLE_SET


# In-memory rate limiting. Endpoints run in a threadpool, so every read-
# modify-write of these dicts is guarded by _throttle_lock.
from collections import defaultdict
import threading
import time as _time

_throttle_lock = threading.Lock()

# Customer (QR) order rate limit: max N orders per table per time window.
_recent_orders = defaultdict(list)        # table_no -> [unix timestamps]
CUSTOMER_MAX_PER_WINDOW = 5               # max QR orders ...
CUSTOMER_WINDOW_SEC     = 60              # ... per this many seconds, per table

# PIN-login brute-force limit: failed /auth attempts per client IP.
_auth_fails = defaultdict(list)           # client_ip -> [unix timestamps of failures]


# =====================================================
# AUTH (server-side PIN)
# =====================================================

@app.post("/auth")
def auth_login(req: PinRequest, request: Request):
    """
    Staff post their PIN. The server matches it to a role and returns a
    signed token. The checkout PIN yields a 'checkout' token (full power);
    the waiter PIN yields a 'waiter' token (order-only). The PIN itself
    never ships to the browser, and the role cannot be forged client-side.

    Brute force is throttled per client IP (see config.AUTH_MAX_FAILS): too
    many wrong PINs lock that device out until its failures age past the window.
    """
    ip = request.client.host if request.client else "unknown"
    now_t = _time.time()

    with _throttle_lock:
        fails = [t for t in _auth_fails[ip] if now_t - t < config.AUTH_WINDOW_SEC]
        _auth_fails[ip] = fails
        if len(fails) >= config.AUTH_MAX_FAILS:
            retry = int(config.AUTH_WINDOW_SEC - (now_t - min(fails))) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Çok fazla hatalı deneme. {retry} saniye sonra tekrar deneyin."
            )

    role = auth.role_for_pin(req.pin)
    if role is None:
        with _throttle_lock:
            _auth_fails[ip].append(now_t)
        raise HTTPException(status_code=401, detail="Hatalı şifre")

    with _throttle_lock:
        _auth_fails.pop(ip, None)   # a correct PIN clears the device's failures
    return {"ok": True, "role": role, "token": auth.make_token(role)}


# =====================================================
# CONFIG (frontend pulls menu + table count from here)
# =====================================================

@app.get("/config")
def get_config():
    return {
        "areas": [{"id": a["id"], "name": a["name"], "tables": a["tables"]} for a in _AREAS],
        "table_names": {str(n): name for n, name in _TABLE_NAMES.items()},
        "cancel_reasons": config.CANCEL_REASONS,
        "cancel_reason_other": config.CANCEL_REASON_OTHER,
        "currency": config.CURRENCY,
        "menu": {cat: [{"name": i["name"], "price": i["price"]} for i in items]
                 for cat, items in _MENU_SECTIONS},
    }


# =====================================================
# MENU / AYARLAR (admin-editable menu) — checkout only
# Edits go to the menu_items table, then load_menu() refreshes the live cache
# so /config (and therefore POS, waiter and QR) reflect them on next load.
# =====================================================

@app.get("/menu/all", dependencies=[Depends(auth.require("checkout"))])
def menu_all():
    """Full menu grouped by section, for the Ayarlar editor."""
    return {
        "sections": [{"category": cat, "items": items} for cat, items in _MENU_SECTIONS],
        "categories": [cat for cat, _ in _MENU_SECTIONS],
    }


@app.post("/menu/add-item", dependencies=[Depends(auth.require("checkout"))])
def menu_add_item(m: MenuItemCreate):
    name = m.name.strip()
    category = m.category.strip()
    if not name or not category:
        raise HTTPException(status_code=400, detail="Bölüm ve ürün adı gerekli")
    with db() as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM menu_items"
        ).fetchone()["n"]
        try:
            cur = conn.execute(
                "INSERT INTO menu_items (category, name, price, sort_order, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (category, name, m.price, nxt, datetime.now().isoformat())
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Bu isimde bir ürün zaten var")
    load_menu()
    return {"status": "created", "id": cur.lastrowid}


@app.post("/menu/update-item", dependencies=[Depends(auth.require("checkout"))])
def menu_update_item(m: MenuItemUpdate):
    sets, vals = [], []
    if m.name is not None:     sets.append("name=?");     vals.append(m.name.strip())
    if m.price is not None:    sets.append("price=?");    vals.append(m.price)
    if m.category is not None: sets.append("category=?"); vals.append(m.category.strip())
    if not sets:
        return {"status": "noop"}
    vals.append(m.id)
    with db() as conn:
        try:
            conn.execute(f"UPDATE menu_items SET {', '.join(sets)} WHERE id=?", vals)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Bu isimde bir ürün zaten var")
    load_menu()
    return {"status": "updated"}


@app.post("/menu/delete-item", dependencies=[Depends(auth.require("checkout"))])
def menu_delete_item(e: EntryId):
    with db() as conn:
        conn.execute("DELETE FROM menu_items WHERE id=?", (e.id,))
    load_menu()
    return {"status": "deleted"}


@app.post("/menu/rename-section", dependencies=[Depends(auth.require("checkout"))])
def menu_rename_section(s: SectionRename):
    new = s.new.strip()
    if not new:
        raise HTTPException(status_code=400, detail="Bölüm adı boş olamaz")
    with db() as conn:
        conn.execute("UPDATE menu_items SET category=? WHERE category=?", (new, s.old))
    load_menu()
    return {"status": "updated"}


@app.post("/menu/delete-section", dependencies=[Depends(auth.require("checkout"))])
def menu_delete_section(s: SectionRef):
    """Delete a whole section and every product in it. Past sales are unaffected
    (order_items keep their own name + price snapshot)."""
    with db() as conn:
        conn.execute("DELETE FROM menu_items WHERE category=?", (s.category,))
    load_menu()
    return {"status": "deleted"}


# =====================================================
# TABLES & AREAS (Ayarlar → Masalar) — checkout only
# Edits go to table_areas / cafe_tables, then load_areas() refreshes the live
# cache, so /config (POS, waiter) and /qr-codes show them on their next load.
# Past sales keep their table numbers, so removing a table never touches reports.
# =====================================================

MAX_TABLE_NO = 9999
NAMED_TABLE_START = 1001      # internal numbers for tables added by NAME ("Toplantı Masası")
_TABLE_NAME_RE = re.compile(r"^[\w .\-/'&]+$")


def _clean_table_name(raw: str) -> str:
    """Validate a table's display name. Digits-only names are normalised
    ('054' → '54'). 400 with a Turkish message on bad input."""
    name = " ".join((raw or "").split())
    if not name:
        raise HTTPException(status_code=400, detail="Masa adı boş olamaz")
    if len(name) > 30:
        raise HTTPException(status_code=400, detail="Masa adı en fazla 30 karakter olabilir")
    if not _TABLE_NAME_RE.match(name) or "_" in name:
        raise HTTPException(status_code=400,
                            detail="Masa adında yalnızca harf, rakam, boşluk ve - . / ' & kullanılabilir")
    low = name.casefold()
    if low == "masa" or low.startswith("masa "):
        raise HTTPException(status_code=400,
                            detail='"Masa" yazmanıza gerek yok — örn. "12" veya "Toplantı Masası"')
    if name.isdigit():
        n = int(name)
        if n < 1 or n > MAX_TABLE_NO:
            raise HTTPException(status_code=400, detail=f"Masa numarası 1–{MAX_TABLE_NO} arasında olmalı")
        name = str(n)
    return name


def _name_taken(conn, name: str, except_no=None):
    """The table row already using this name (case-insensitive), or None."""
    key = name.casefold()
    for r in conn.execute("SELECT table_no, name FROM cafe_tables"):
        if r["table_no"] != except_no and (r["name"] or "").casefold() == key:
            return r
    return None


def _open_tables(conn) -> set:
    """Tables with an open bill (incl. unaccepted QR orders) — can't be removed."""
    return {r["table_no"] for r in conn.execute("SELECT table_no FROM orders WHERE status='open'")}


def _parse_table_spec(spec: str) -> list:
    """'16', '16-20', '16-20, 25 30-32' → sorted unique numbers. 400 on bad input."""
    nums = set()
    for part in spec.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                if a > b:
                    a, b = b, a
                if b - a >= 200:
                    raise HTTPException(status_code=400, detail="Bir seferde en fazla 200 masa eklenebilir")
                nums.update(range(a, b + 1))
            else:
                nums.add(int(part))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Geçersiz masa numarası: {part}")
    if not nums:
        raise HTTPException(status_code=400, detail="Masa numarası girin")
    if len(nums) > 200:
        raise HTTPException(status_code=400, detail="Bir seferde en fazla 200 masa eklenebilir")
    bad = [n for n in nums if n < 1 or n > MAX_TABLE_NO]
    if bad:
        raise HTTPException(status_code=400, detail=f"Masa numarası 1–{MAX_TABLE_NO} arasında olmalı")
    return sorted(nums)


def _area_or_404(conn, area_id: int):
    a = conn.execute("SELECT id, name FROM table_areas WHERE id=?", (area_id,)).fetchone()
    if not a:
        raise HTTPException(status_code=404, detail="Bölge bulunamadı")
    return a


@app.get("/tables/all", dependencies=[Depends(auth.require("checkout"))])
def tables_all():
    with db() as conn:
        open_t = _open_tables(conn)
    return {"areas": [{"id": a["db_id"], "name": a["name"],
                       "tables": [{"no": n, "name": _TABLE_NAMES.get(n, str(n)),
                                   "label": table_label(n), "open": n in open_t}
                                  for n in a["tables"]]}
                      for a in _AREAS]}


@app.post("/tables/area-add", dependencies=[Depends(auth.require("checkout"))])
def tables_area_add(a: AreaIn):
    with db() as conn:
        nxt = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM table_areas").fetchone()["n"]
        try:
            conn.execute("INSERT INTO table_areas (name, sort_order) VALUES (?, ?)", (a.name, nxt))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Bu isimde bir bölge zaten var")
    load_areas()
    return {"status": "created"}


@app.post("/tables/area-rename", dependencies=[Depends(auth.require("checkout"))])
def tables_area_rename(a: AreaRename):
    with db() as conn:
        _area_or_404(conn, a.id)
        try:
            conn.execute("UPDATE table_areas SET name=? WHERE id=?", (a.name, a.id))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Bu isimde bir bölge zaten var")
    load_areas()
    return {"status": "updated"}


@app.post("/tables/area-delete", dependencies=[Depends(auth.require("checkout"))])
def tables_area_delete(e: EntryId):
    """Delete an area and its tables — refused while any of them has an open bill."""
    with db() as conn:
        _area_or_404(conn, e.id)
        tables = [r["table_no"] for r in conn.execute(
            "SELECT table_no FROM cafe_tables WHERE area_id=? ORDER BY table_no", (e.id,))]
        busy = [n for n in tables if n in _open_tables(conn)]
        if busy:
            raise HTTPException(status_code=400,
                                detail="Açık adisyonu olan masa var: " + ", ".join(table_label(n) for n in busy))
        conn.execute("DELETE FROM cafe_tables WHERE area_id=?", (e.id,))
        conn.execute("DELETE FROM table_areas WHERE id=?", (e.id,))
    load_areas()
    log.info("Area deleted (id %s) with tables %s", e.id, tables)
    return {"status": "deleted"}


@app.post("/tables/add", dependencies=[Depends(auth.require("checkout"))])
def tables_add(t: TablesAdd):
    """Add tables to an area. The box takes either numbers ('16', '16-20, 25')
    or ONE name with letters ('Toplantı Masası'), which gets the next free
    internal number from NAMED_TABLE_START. All-or-nothing: any clash (number
    or name, in any area) → nothing added, the clash is reported."""
    spec = t.spec.strip()
    with db() as conn:
        _area_or_404(conn, t.area_id)
        if any(ch.isalpha() for ch in spec):                      # one named table
            name = _clean_table_name(spec)
            if _name_taken(conn, name):
                raise HTTPException(status_code=400, detail=f'"{name}" adında bir masa zaten var')
            used = {r["table_no"] for r in conn.execute("SELECT table_no FROM cafe_tables")}
            no = NAMED_TABLE_START
            while no in used:
                no += 1
            if no > MAX_TABLE_NO:
                raise HTTPException(status_code=400, detail="Masa sınırına ulaşıldı")
            rows = [(no, t.area_id, name)]
        else:
            nums = _parse_table_spec(spec)
            taken = conn.execute(
                f"SELECT c.table_no, a.name AS area FROM cafe_tables c JOIN table_areas a ON a.id = c.area_id"
                f" WHERE c.table_no IN ({','.join('?' for _ in nums)}) ORDER BY c.table_no", nums).fetchall()
            if taken:
                raise HTTPException(status_code=400, detail="Bu masa numaraları zaten var: " +
                                    ", ".join(f"{r['table_no']} ({r['area']})" for r in taken[:10]))
            name_clash = [r for r in (_name_taken(conn, str(n)) for n in nums) if r]
            if name_clash:
                raise HTTPException(status_code=400, detail="Bu isimler başka masada kullanılıyor: " +
                                    ", ".join(f"{r['name']} (no {r['table_no']})" for r in name_clash[:10]))
            rows = [(n, t.area_id, str(n)) for n in nums]
        try:
            conn.executemany("INSERT INTO cafe_tables (table_no, area_id, name) VALUES (?, ?, ?)", rows)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Bu masa zaten var")
    load_areas()
    added = [r[0] for r in rows]
    return {"status": "added", "added": added, "labels": [table_label(n) for n in added]}


@app.post("/tables/rename", dependencies=[Depends(auth.require("checkout"))])
def tables_rename(t: TableRename):
    """Change only the display name. The internal number stays, so printed QR
    codes, the open bill and past records are untouched — allowed even while
    the table has an open bill."""
    name = _clean_table_name(t.name)
    with db() as conn:
        row = conn.execute("SELECT table_no, name FROM cafe_tables WHERE table_no=?", (t.table_no,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Masa bulunamadı")
        if _name_taken(conn, name, except_no=t.table_no):
            raise HTTPException(status_code=400, detail=f'"{name}" adında bir masa zaten var')
        try:
            conn.execute("UPDATE cafe_tables SET name=? WHERE table_no=?", (name, t.table_no))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail=f'"{name}" adında bir masa zaten var')
    load_areas()
    log.info("Table %s renamed: %s → %s", t.table_no, row["name"], name)
    return {"status": "renamed", "table_no": t.table_no, "name": name, "label": table_label(t.table_no)}


@app.post("/tables/delete", dependencies=[Depends(auth.require("checkout"))])
def tables_delete(t: TableRef):
    with db() as conn:
        if t.table_no in _open_tables(conn):
            raise HTTPException(status_code=400,
                                detail=f"{table_label(t.table_no)} için açık adisyon var — önce kapatın")
        conn.execute("DELETE FROM cafe_tables WHERE table_no=?", (t.table_no,))
    log.info("Table %s (%s) removed", t.table_no, table_label(t.table_no))
    load_areas()
    return {"status": "deleted"}


# =====================================================
# ORDERS
# =====================================================

@app.post("/create-order")
def create_order(order: OrderCreate, role: str = Depends(auth.current_role)):
    # QR customers' items arrive 'pending' (staff must accept). Staff items
    # (waiter or checkout) are 'confirmed' immediately.
    item_status = "pending" if role == "customer" else "confirmed"

    # Guards apply ONLY to QR customers; staff are trusted.
    if role == "customer":
        if not _valid_table(order.table_no):
            raise HTTPException(status_code=400, detail="Geçersiz masa numarası")
        now_t = _time.time()
        with _throttle_lock:
            recent = [t for t in _recent_orders[order.table_no] if now_t - t < CUSTOMER_WINDOW_SEC]
            if len(recent) >= CUSTOMER_MAX_PER_WINDOW:
                raise HTTPException(status_code=429, detail="Çok fazla sipariş, lütfen biraz bekleyin")
            recent.append(now_t)
            _recent_orders[order.table_no] = recent

    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM orders WHERE table_no=? AND status='open'",
            (order.table_no,)
        ).fetchone()

        if existing:
            order_id = existing["id"]
        else:
            try:
                cur = conn.execute(
                    "INSERT INTO orders (table_no, created_at) VALUES (?, ?)",
                    (order.table_no, datetime.now().isoformat())
                )
                order_id = cur.lastrowid
            except sqlite3.IntegrityError:
                # Another request opened this table's order between our SELECT
                # and INSERT (idx_one_open_per_table). Reuse the winner.
                order_id = conn.execute(
                    "SELECT id FROM orders WHERE table_no=? AND status='open'",
                    (order.table_no,)
                ).fetchone()["id"]

        now = datetime.now().isoformat()
        for item in order.items:
            conn.execute(
                "INSERT INTO order_items (order_id, product_name, quantity, price, created_at, item_status)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (order_id, item.product_name, item.quantity,
                 _price_of(item.product_name, item.price), now, item_status)
            )

        total = _order_total(conn, order_id)
        conn.execute("UPDATE orders SET total_amount=? WHERE id=?", (total, order_id))

    return {"status": "order_saved", "order_id": order_id, "total": total, "item_status": item_status}


@app.get("/table/{table_no}")
def get_table(table_no: int):
    with db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE table_no=? AND status='open'",
            (table_no,)
        ).fetchone()

        if not order:
            return {"status": "empty", "name": _TABLE_NAMES.get(table_no), "label": table_label(table_no),
                    "valid": _valid_table(table_no)}

        items = conn.execute(
            "SELECT id, product_name, quantity, price, created_at, paid_qty, item_status FROM order_items"
            " WHERE order_id=? ORDER BY created_at ASC",
            (order["id"],)
        ).fetchall()

        total = _order_total(conn, order["id"])
        # paid/unpaid are computed PER UNIT now: paid_qty units are settled.
        paid_total = sum(_price_of(i["product_name"], i["price"]) * i["paid_qty"]
                         for i in items)
        unpaid_total = total - paid_total

        return {
            "status": "open",
            "name": _TABLE_NAMES.get(table_no), "label": table_label(table_no),
            "valid": _valid_table(table_no),
            "order_id": order["id"],
            "barista_status": order["barista_status"] or "hazırlanıyor",
            "created_at": order["created_at"],
            "open_seconds": _open_seconds(order["created_at"]),
            "items": [dict(i) for i in items],
            "total": total,
            "paid_total": paid_total,
            "unpaid_total": unpaid_total,
        }


@app.get("/tables-status")
def tables_status():
    with db() as conn:
        rows = conn.execute(
            "SELECT table_no, created_at FROM orders WHERE status='open'"
        ).fetchall()
        # Tables that currently have unaccepted (pending) QR items.
        pend = conn.execute(
            "SELECT DISTINCT o.table_no FROM orders o"
            " JOIN order_items i ON i.order_id = o.id"
            " WHERE o.status='open' AND i.item_status='pending'"
        ).fetchall()
    return {
        "open_tables": [r["table_no"] for r in rows],
        "pending_tables": [r["table_no"] for r in pend],
        # How long each open table has been open, by the SERVER's clock — a
        # phone/tablet with a wrong clock still shows the right duration.
        "open_seconds": {str(r["table_no"]): _open_seconds(r["created_at"]) for r in rows},
    }


def _open_seconds(created_at: str) -> int:
    """Seconds since an order was opened (0 if the timestamp is unreadable)."""
    try:
        return max(0, int((datetime.now() - datetime.fromisoformat(created_at)).total_seconds()))
    except (TypeError, ValueError):
        return 0


@app.get("/pending", dependencies=[Depends(auth.require("waiter"))])
def pending_orders():
    """All unaccepted QR items, grouped by table, for the 'Bekleyen' tab."""
    with db() as conn:
        rows = conn.execute(
            "SELECT o.table_no, i.id, i.product_name, i.quantity, i.price, i.created_at"
            " FROM orders o JOIN order_items i ON i.order_id = o.id"
            " WHERE o.status='open' AND i.item_status='pending'"
            " ORDER BY o.table_no, i.created_at",
        ).fetchall()
    tables = {}
    for r in rows:
        tables.setdefault(r["table_no"], []).append({
            "id": r["id"], "product_name": r["product_name"],
            "quantity": r["quantity"], "price": r["price"], "created_at": r["created_at"],
        })
    return {"tables": [{"table_no": t, "table_label": table_label(t), "items": items}
                       for t, items in tables.items()]}


def _check_reason(reason: str, note: Optional[str]):
    """Validate a cancellation reason → (reason, note-or-None). 400 otherwise."""
    reason = (reason or "").strip()
    note = (note or "").strip() or None
    if reason not in config.CANCEL_REASONS:
        raise HTTPException(status_code=400, detail="Geçerli bir iptal nedeni seçin")
    if reason == config.CANCEL_REASON_OTHER and not note:
        raise HTTPException(status_code=400, detail="'Diğer' için iptal nedenini yazın")
    return reason, note


def _log_cancellation(conn, *, table_no, product_name, quantity, price, reason, note,
                      kind, role, ordered_at):
    unit = _price_of(product_name, price)
    conn.execute(
        "INSERT INTO cancellations (cancelled_at, table_no, product_name, quantity, unit_price,"
        " amount, reason, note, kind, cancelled_by, ordered_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), table_no, product_name, quantity, unit,
         round(quantity * unit, 2), reason, note, kind, role, ordered_at))


@app.post("/cancel-order")
def cancel_order(data: CancelRequest, role: str = Depends(auth.require("checkout"))):
    """Cancel a whole open bill, with a reason. Every line is written to the
    İptal Edilenler log first (same transaction). Refused if any unit is already
    paid (split payment) — deleting it would break the books."""
    reason, note = _check_reason(data.reason, data.note)
    with db() as conn:
        order = conn.execute(
            "SELECT id FROM orders WHERE table_no=? AND status='open'", (data.table_no,)
        ).fetchone()
        if not order:
            raise HTTPException(status_code=400, detail="Açık adisyon yok")
        items = conn.execute(
            "SELECT product_name, quantity, price, paid_qty, created_at FROM order_items"
            " WHERE order_id=? AND item_status='confirmed' ORDER BY id", (order["id"],)
        ).fetchall()
        if any((i["paid_qty"] or 0) > 0 for i in items):
            raise HTTPException(status_code=400,
                                detail="Ödenmiş ürün var — kalan ürünleri ✕ ile iptal edin")
        for i in items:
            _log_cancellation(conn, table_no=data.table_no, product_name=i["product_name"],
                              quantity=i["quantity"], price=i["price"], reason=reason, note=note,
                              kind="order", role=role, ordered_at=i["created_at"])
        # ON DELETE CASCADE removes the items (incl. any unaccepted QR items).
        conn.execute("DELETE FROM orders WHERE id=?", (order["id"],))
    log.info("Order cancelled: table %s, %d line(s), reason %s%s (by %s)",
             data.table_no, len(items), reason, f" ({note})" if note else "", role)
    return {"status": "cancelled", "lines": len(items)}


@app.post("/update-order-status", dependencies=[Depends(auth.require("waiter"))])
def update_order_status(data: StatusUpdate):
    with db() as conn:
        conn.execute(
            "UPDATE orders SET barista_status=? WHERE id=?",
            (data.status, data.order_id)
        )
    return {"status": "updated"}


@app.post("/accept-order", dependencies=[Depends(auth.require("waiter"))])
def accept_order(data: AcceptItems):
    """Confirm pending QR items on a table (waiter or checkout). If item_ids
    is omitted, accept ALL pending items on the table."""
    with db() as conn:
        order = conn.execute(
            "SELECT id FROM orders WHERE table_no=? AND status='open'",
            (data.table_no,)
        ).fetchone()
        if not order:
            raise HTTPException(status_code=400, detail="Açık adisyon yok")

        # item_ids is None  -> accept ALL pending items on the table.
        # item_ids is []    -> explicit empty selection, accept nothing.
        if data.item_ids is not None:
            if not data.item_ids:
                return {"status": "accepted"}
            placeholders = ",".join("?" for _ in data.item_ids)
            conn.execute(
                f"UPDATE order_items SET item_status='confirmed'"
                f" WHERE order_id=? AND item_status='pending' AND id IN ({placeholders})",
                (order["id"], *data.item_ids)
            )
        else:
            conn.execute(
                "UPDATE order_items SET item_status='confirmed'"
                " WHERE order_id=? AND item_status='pending'",
                (order["id"],)
            )
    return {"status": "accepted"}


@app.post("/refuse-order", dependencies=[Depends(auth.require("waiter"))])
def refuse_order(data: AcceptItems):
    """Reject/delete pending QR items (mistake or prank). Only touches items
    that are still 'pending' — confirmed/paid items are never affected.
    If item_ids is omitted, refuses ALL pending items on the table.
    If refusing empties a brand-new order entirely, the empty order is removed."""
    with db() as conn:
        order = conn.execute(
            "SELECT id FROM orders WHERE table_no=? AND status='open'",
            (data.table_no,)
        ).fetchone()
        if not order:
            raise HTTPException(status_code=400, detail="Açık adisyon yok")

        # item_ids is None  -> refuse ALL pending items on the table.
        # item_ids is []    -> explicit empty selection, refuse nothing.
        if data.item_ids is not None:
            if not data.item_ids:
                return {"status": "refused"}
            placeholders = ",".join("?" for _ in data.item_ids)
            conn.execute(
                f"DELETE FROM order_items"
                f" WHERE order_id=? AND item_status='pending' AND id IN ({placeholders})",
                (order["id"], *data.item_ids)
            )
        else:
            conn.execute(
                "DELETE FROM order_items WHERE order_id=? AND item_status='pending'",
                (order["id"],)
            )

        # If the order now has no items at all, remove the empty tab.
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM order_items WHERE order_id=?", (order["id"],)
        ).fetchone()["c"]
        if remaining == 0:
            conn.execute("DELETE FROM orders WHERE id=?", (order["id"],))

    return {"status": "refused"}


@app.post("/remove-item")
def remove_item(data: RemoveItem, role: str = Depends(auth.require("waiter"))):
    """Remove ONE saved line from a table's open order (customer changed their
    mind / wrong item entered). Waiter or checkout. A line with any PAID units
    can't be removed — that would break the books. If it was the order's last
    line, the now-empty order is removed so the table turns free.

    Needs a reason (config.CANCEL_REASONS); `qty` cancels only part of the line
    (e.g. 1 of 3 Lattes). Every cancellation is written to the İptal Edilenler
    log in the same transaction."""
    reason, note = _check_reason(data.reason, data.note)
    with db() as conn:
        order = conn.execute(
            "SELECT id FROM orders WHERE table_no=? AND status='open'",
            (data.table_no,)
        ).fetchone()
        if not order:
            raise HTTPException(status_code=400, detail="Açık adisyon yok")
        item = conn.execute(
            "SELECT product_name, quantity, price, paid_qty, created_at FROM order_items"
            " WHERE id=? AND order_id=?",
            (data.item_id, order["id"])
        ).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Ürün bulunamadı")
        if (item["paid_qty"] or 0) > 0:
            raise HTTPException(status_code=400, detail="Ödenmiş ürün silinemez")
        line_qty = item["quantity"]
        qty = line_qty if data.qty is None else data.qty
        if qty > line_qty:
            raise HTTPException(status_code=400,
                                detail=f"En fazla {line_qty:g} adet iptal edilebilir")

        _log_cancellation(conn, table_no=data.table_no, product_name=item["product_name"],
                          quantity=qty, price=item["price"], reason=reason, note=note,
                          kind="item", role=role, ordered_at=item["created_at"])
        if qty < line_qty:                       # partial: keep the rest on the bill
            conn.execute("UPDATE order_items SET quantity=? WHERE id=?",
                         (line_qty - qty, data.item_id))
        else:
            conn.execute("DELETE FROM order_items WHERE id=?", (data.item_id,))
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM order_items WHERE order_id=?", (order["id"],)
        ).fetchone()["c"]
        if remaining == 0:
            conn.execute("DELETE FROM orders WHERE id=?", (order["id"],))
        else:
            conn.execute("UPDATE orders SET total_amount=? WHERE id=?",
                         (_order_total(conn, order["id"]), order["id"]))

    log.info("Item cancelled: table %s, %s ×%g of %g, reason %s%s (by %s)",
             data.table_no, item["product_name"], qty, line_qty, reason,
             f" ({note})" if note else "", role)
    return {"status": "removed", "cancelled_qty": qty, "order_closed": remaining == 0}


@app.get("/cancellations", dependencies=[Depends(auth.require("checkout"))])
def cancellations(start: Optional[Date] = None, end: Optional[Date] = None):
    """İptal Edilenler log for [start, end] (inclusive; default today)."""
    start = start or Date.today()
    end = end or start
    if start > end:
        raise HTTPException(status_code=400, detail="Bitiş tarihi başlangıçtan önce olamaz")
    if (end - start).days + 1 > MAX_REPORT_DAYS:
        raise HTTPException(status_code=400, detail=f"En fazla {MAX_REPORT_DAYS} gün seçilebilir")
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM cancellations WHERE cancelled_at >= ? AND cancelled_at < ?"
            " ORDER BY cancelled_at DESC, id DESC",
            (start.isoformat(), (end + timedelta(days=1)).isoformat())
        ).fetchall()
    rows = [{**dict(r), "table_label": table_label(r["table_no"])} for r in rows]
    by = {}
    for r in rows:
        b = by.setdefault(r["reason"], {"reason": r["reason"], "count": 0, "qty": 0.0, "amount": 0.0})
        b["count"] += 1; b["qty"] += r["quantity"]; b["amount"] += r["amount"]
    order = {name: i for i, name in enumerate(config.CANCEL_REASONS)}
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "rows": rows,
        "by_reason": sorted(by.values(), key=lambda b: order.get(b["reason"], 99)),
        "totals": {"count": len(rows), "qty": sum(r["quantity"] for r in rows),
                   "amount": round(sum(r["amount"] for r in rows), 2)},
    }


# =====================================================
# PAYMENT (Step 1: full-table pay, server-calculated total)
# Step 2 will add split-by-item on top of this.
# =====================================================

@app.post("/pay", dependencies=[Depends(auth.require("checkout"))])
def pay_table(payment: PaymentRequest):
    """Settle every remaining CONFIRMED unit with one method. QR items still
    waiting for approval are not charged; they move to a fresh open bill."""
    _validate_payment_type(payment.payment_type)
    with db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE table_no=? AND status='open'",
            (payment.table_no,)
        ).fetchone()

        if not order:
            raise HTTPException(status_code=400, detail="No open order")

        rows = conn.execute(
            "SELECT id, product_name, quantity, price, paid_qty FROM order_items"
            " WHERE order_id=? AND item_status='confirmed' AND quantity - paid_qty > 1e-9",
            (order["id"],)
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=400, detail="Ödenecek ürün yok")
        lines = [(r["id"], r["product_name"], r["quantity"] - r["paid_qty"],
                  _price_of(r["product_name"], r["price"])) for r in rows]
        total = sum(q * u for _, _, q, u in lines)   # AUTHORITATIVE: only what is still owed
        now = datetime.now().isoformat()

        conn.execute(
            "UPDATE order_items SET paid_qty=quantity, paid=1, paid_at=?"
            " WHERE order_id=? AND item_status='confirmed' AND paid_qty < quantity",
            (now, order["id"])
        )
        _record_sale(conn, order["id"], payment.table_no, lines,
                     [(payment.payment_type, total)], now)
        _close_order(conn, order["id"], payment.table_no, payment.payment_type, now)

    return {"status": "payment_completed", "total": total}


def _record_sale(conn, order_id: int, table_no: int, lines, splits, now: str) -> str:
    """Write one payment: a sold_items row per line paid (what was sold, for the
    reports' quantities) and a sales_log row per method (the revenue). Both
    share a batch id and the same timestamp. lines = [(item_id, name, qty,
    unit_price)], splits = [(method, amount)]."""
    import uuid
    batch = uuid.uuid4().hex
    for item_id, name, qty, unit in lines:
        conn.execute(
            "INSERT INTO sold_items (order_id, item_id, table_no, product_name, qty,"
            "                        unit_price, amount, paid_at, batch)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, item_id, table_no, name, qty, unit, qty * unit, now, batch))
    for method, amount in splits:
        conn.execute(
            "INSERT INTO payments (order_id, table_no, amount, payment_type, paid_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (order_id, table_no, amount, method, now))
        conn.execute(
            "INSERT INTO sales_log (table_no, total_amount, payment_type, paid_at, batch)"
            " VALUES (?, ?, ?, ?, ?)",
            (table_no, amount, method, now, batch))
    return batch


def _close_order(conn, order_id: int, table_no: int, label: str, now: str) -> None:
    """Mark a fully paid bill 'paid'. QR items still waiting for approval are
    NOT part of what was paid: they move to a new open bill on the same table,
    so they are neither charged/reported as sold nor lost, and staff still see
    them in the pending list."""
    pending = conn.execute(
        "SELECT id FROM order_items WHERE order_id=? AND item_status='pending'",
        (order_id,)).fetchall()
    conn.execute(
        "UPDATE orders SET status='paid', paid_at=? WHERE id=?", (now, order_id))
    if pending:
        cur = conn.execute("INSERT INTO orders (table_no, created_at) VALUES (?, ?)",
                           (table_no, now))
        conn.execute(
            f"UPDATE order_items SET order_id=? WHERE id IN ({','.join('?' * len(pending))})",
            (cur.lastrowid, *[r["id"] for r in pending]))
        conn.execute("UPDATE orders SET total_amount=? WHERE id=?",
                     (_order_total(conn, cur.lastrowid), cur.lastrowid))
    conn.execute("UPDATE orders SET payment_type=?, total_amount=? WHERE id=?",
                 (label, _order_total(conn, order_id), order_id))


@app.post("/pay-items", dependencies=[Depends(auth.require("checkout"))])
def pay_items(req: PayItemsRequest):
    """
    Settle a chosen QUANTITY of one or more order lines, paid across one or
    more methods. This is what makes every real-life café case work:

      - Pay everything one method:     lines = all remaining, splits = [{card, total}]
      - Go Dutch (pay only mine):      lines = [{my latte, 1}], splits = [{cash, 190}]
      - Split a 12-tea line 3 ways:    lines = [{tea, 4}], splits = [...]  (repeat per person)
      - Half cash / half card:         lines = selection, splits = [{cash, X},{card, Y}]

    The server:
      1. checks each line belongs to this open order and qty <= what's unpaid,
      2. computes the AUTHORITATIVE amount for those units (config prices),
      3. requires the splits to sum to that amount (no over/under-paying),
      4. increments paid_qty per line + logs each split to payments & sales_log,
      5. closes the table once every unit is paid.
    """
    for s in req.splits:
        _validate_payment_type(s.method)

    with db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE table_no=? AND status='open'",
            (req.table_no,)
        ).fetchone()
        if not order:
            raise HTTPException(status_code=400, detail="Açık adisyon yok")

        order_id = order["id"]

        # Aggregate the requested qty PER item_id first, so a line that repeats
        # the same item_id can't be validated twice against the same starting
        # paid_qty (which would over-charge while only crediting one line).
        want = {}
        for line in req.lines:
            want[line.item_id] = want.get(line.item_id, 0.0) + line.qty

        # Validate every line and compute the authoritative amount being paid.
        selected_total = 0.0
        names = {}
        for item_id, qty in want.items():
            row = conn.execute(
                "SELECT product_name, quantity, price, paid_qty, item_status FROM order_items"
                " WHERE id=? AND order_id=?",
                (item_id, order_id)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="Ürün bu adisyona ait değil")
            if row["item_status"] == "pending":
                raise HTTPException(status_code=400,
                                    detail=f"{row['product_name']}: onaylanmamış QR ürünü ödenemez")
            names[item_id] = (row["product_name"], _price_of(row["product_name"], row["price"]))

            remaining_qty = row["quantity"] - row["paid_qty"]
            if qty > remaining_qty + 1e-9:
                raise HTTPException(
                    status_code=400,
                    detail=f"{row['product_name']}: en fazla {remaining_qty:g} adet ödenebilir"
                )

            selected_total += _price_of(row["product_name"], row["price"]) * qty

        split_total = sum(s.amount for s in req.splits)
        if abs(split_total - selected_total) > 0.01:
            raise HTTPException(
                status_code=400,
                detail=f"Ödeme toplamı {split_total:.0f}₺, seçili tutar {selected_total:.0f}₺ olmalı"
            )

        now = datetime.now().isoformat()

        # Apply paid_qty increments ATOMICALLY: the WHERE clause re-checks the
        # remaining units inside the UPDATE, so two checkout devices settling the
        # same table concurrently can't both consume the same units. A rowcount
        # of 0 means we lost the race -> roll the whole request back.
        for item_id, qty in want.items():
            cur = conn.execute(
                "UPDATE order_items SET paid_qty = paid_qty + ?, paid=CASE WHEN paid_qty + ? >= quantity - 1e-9 THEN 1 ELSE paid END, paid_at=?"
                " WHERE id=? AND order_id=? AND quantity - paid_qty + 1e-9 >= ?",
                (qty, qty, now, item_id, order_id, qty)
            )
            if cur.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Ödeme çakışması — bu ürünler bu sırada ödenmiş olabilir, lütfen tekrar deneyin"
                )

        # Record the payment: sold_items (quantities) + payments/sales_log
        # (revenue), one batch, same timestamp.
        _record_sale(conn, order_id, req.table_no,
                     [(iid, names[iid][0], qty, names[iid][1]) for iid, qty in want.items()],
                     [(sp.method, sp.amount) for sp in req.splits], now)

        # Fully settled when no CONFIRMED line has unpaid units left (QR items
        # still waiting for approval are not part of this bill, see _close_order).
        remaining_units = conn.execute(
            "SELECT COALESCE(SUM(quantity - paid_qty), 0) AS r FROM order_items"
            " WHERE order_id=? AND item_status='confirmed'",
            (order_id,)
        ).fetchone()["r"]

        order_closed = remaining_units <= 1e-9
        if order_closed:
            methods = {sp.method for sp in req.splits}
            label = methods.pop() if len(methods) == 1 else "split"
            _close_order(conn, order_id, req.table_no, label, now)

    return {
        "status": "items_paid",
        "paid_amount": selected_total,
        "order_closed": order_closed,
        "remaining_total": round(_remaining_total_after(req.table_no), 2) if not order_closed else 0,
    }


def _remaining_total_after(table_no: int) -> float:
    """Helper: current unpaid total for a table (for the response)."""
    with db() as conn:
        order = conn.execute(
            "SELECT id FROM orders WHERE table_no=? AND status='open'", (table_no,)
        ).fetchone()
        if not order:
            return 0.0
        rows = conn.execute(
            "SELECT product_name, quantity, price, paid_qty FROM order_items WHERE order_id=?",
            (order["id"],)
        ).fetchall()
        return sum(_price_of(r["product_name"], r["price"]) * (r["quantity"] - r["paid_qty"])
                   for r in rows)


# =====================================================
# SALES / REPORTING
# =====================================================

@app.delete("/sales-log/{log_id}", dependencies=[Depends(auth.require("checkout"))])
def delete_sales_log(log_id: int):
    """Correct a mistaken sale. Its sold items leave the reports too, once every
    revenue row of that payment is deleted (one leg of a cash+card split alone
    keeps the items: the goods were still sold)."""
    with db() as conn:
        row = conn.execute("SELECT batch FROM sales_log WHERE id=?", (log_id,)).fetchone()
        conn.execute("DELETE FROM sales_log WHERE id=?", (log_id,))
        if row and row["batch"] and not conn.execute(
                "SELECT 1 FROM sales_log WHERE batch=?", (row["batch"],)).fetchone():
            conn.execute("DELETE FROM sold_items WHERE batch=?", (row["batch"],))
    return {"status": "deleted"}


@app.get("/sales", dependencies=[Depends(auth.require("checkout"))])
def get_sales():
    with db() as conn:
        rows = conn.execute(
            "SELECT table_no, total_amount, payment_type, paid_at FROM sales_log"
            " ORDER BY paid_at DESC"
        ).fetchall()
    return [{**dict(r), "table_label": table_label(r["table_no"])} for r in rows]


@app.get("/sales-summary", dependencies=[Depends(auth.require("checkout"))])
def get_sales_summary():
    with db() as conn:
        today = datetime.now().strftime("%Y-%m-%d")
        today_rows = conn.execute(
            "SELECT total_amount, payment_type FROM sales_log WHERE paid_at LIKE ?",
            (f"{today}%",)
        ).fetchall()
        recent_rows = conn.execute(
            "SELECT id, table_no, total_amount, payment_type, paid_at FROM sales_log"
            " ORDER BY paid_at DESC LIMIT 100"
        ).fetchall()

    today_total = sum(r["total_amount"] for r in today_rows)
    today_cash  = sum(r["total_amount"] for r in today_rows if r["payment_type"] == "cash")
    today_card  = sum(r["total_amount"] for r in today_rows if r["payment_type"] == "card")

    return {
        "today": {
            "total": today_total,
            "cash":  today_cash,
            "card":  today_card,
            "count": len(today_rows),
        },
        "recent": [{**dict(r), "table_label": table_label(r["table_no"])} for r in recent_rows],
    }


# =====================================================
# DASHBOARD (admin/checkout only): home summary + calendar tasks
# Day boundary = midnight (a 1am sale counts as the new day).
# =====================================================

@app.get("/dashboard/summary", dependencies=[Depends(auth.require("checkout"))])
def dashboard_summary():
    """Today's live numbers for the Home panel: revenue, sale count, items sold."""
    today = datetime.now().strftime("%Y-%m-%d")
    with db() as conn:
        rows = conn.execute(
            "SELECT total_amount, payment_type FROM sales_log WHERE paid_at LIKE ?",
            (f"{today}%",)
        ).fetchall()
        # Items actually sold today = units whose payment happened today.
        item_rows = _sold_items(conn, today, _next_day(today))
    total = sum(r["total_amount"] for r in rows)
    cash  = sum(r["total_amount"] for r in rows if r["payment_type"] == "cash")
    card  = sum(r["total_amount"] for r in rows if r["payment_type"] == "card")
    items_sold = round(sum(r["qty"] for r in item_rows), 3)
    with db() as conn:
        coffee = _coffee_summary(conn)
    return {
        "coffee_count": coffee["count"], "coffee_threshold": coffee["threshold"],
        "coffee_due": coffee["due"], "coffee_near": coffee["near"],
        "date": today,
        "revenue": total, "cash": cash, "card": card,
        "sale_count": len(rows),
        "items_sold": items_sold,
        "top_items": [{"name": r["name"], "qty": r["qty"]} for r in item_rows[:8]],
        "stock_check_day": datetime.now().weekday() in (2, 5),   # Wed=2, Sat=5
        "low_stock_count": _low_stock_count(),
    }


def _whole(x) -> int:
    """Round half-up to a whole unit — matches the dashboard's Math.round, so
    the DÜŞÜK flag agrees with the numbers shown (legacy rows may be fractional)."""
    return math.floor((x or 0) + 0.5)


def _is_low(quantity, threshold) -> bool:
    return _whole(quantity) < _whole(threshold)


def _low_stock_count() -> int:
    with db() as conn:
        rows = conn.execute("SELECT quantity, threshold FROM stock_items").fetchall()
    return sum(1 for r in rows if _is_low(r["quantity"], r["threshold"]))


# =====================================================
# COFFEE MACHINE — coffee counter + maintenance (dashboard "Kahve Makinesi")
# The count is COMPUTED from order_items every time (never a running tally),
# so cancelled orders, refused/unaccepted QR items and lines removed with ✕
# can never be counted, and nothing in the order code needs to know about it.
# =====================================================

def _setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _coffee_cfg(conn):
    """(sections, extra_items, threshold) from the settings."""
    sections = json.loads(_setting(conn, "coffee_sections", "[]"))
    extras   = json.loads(_setting(conn, "coffee_extra_items", "[]"))
    threshold = int(_setting(conn, "coffee_threshold", config.COFFEE_MAINT_THRESHOLD_DEFAULT))
    return sections, extras, threshold


def _coffee_anchor(conn) -> str:
    """Count coffees from here: the latest maintenance, or the go-live time."""
    start = _setting(conn, "coffee_start", "") or ""
    last = conn.execute("SELECT MAX(done_at) AS d FROM coffee_maintenance").fetchone()["d"]
    return max(start, last or "")


def _coffee_filter(sections, extras):
    """SQL condition + params: line is a coffee (its menu section is counted,
    or it's one of the extra items like 'Shot'). Saved/accepted lines only."""
    conds, params = [], []
    if sections:
        conds.append(f"m.category IN ({','.join('?' for _ in sections)})")
        params += sections
    if extras:
        conds.append(f"i.product_name IN ({','.join('?' for _ in extras)})")
        params += extras
    cond = "(" + " OR ".join(conds) + ")" if conds else "0"
    # INNER JOIN orders: an orphaned line (order deleted) never counts.
    sql = (" FROM order_items i JOIN orders o ON o.id = i.order_id"
           " LEFT JOIN menu_items m ON m.name = i.product_name"
           f" WHERE i.item_status='confirmed' AND i.created_at >= ? AND {cond}")
    return sql, params


def _coffee_count(conn, since: str = None) -> int:
    sections, extras, _ = _coffee_cfg(conn)
    sql, params = _coffee_filter(sections, extras)
    since = _coffee_anchor(conn) if since is None else since
    n = conn.execute("SELECT COALESCE(SUM(i.quantity), 0) AS n" + sql,
                     (since, *params)).fetchone()["n"]
    return _whole(n)


def _coffee_summary(conn) -> dict:
    """Counter numbers shared by the Kahve Makinesi tab and the Anasayfa banner."""
    _, _, threshold = _coffee_cfg(conn)
    count = _coffee_count(conn)
    due = count >= threshold
    return {"count": count, "threshold": threshold, "due": due,
            "near": (not due) and count >= 0.9 * threshold}


# =====================================================
# RECIPES — dashboard "Reçeteler": a simple editable table (checkout only)
# =====================================================

def _recipe_row(conn, rid: int) -> dict:
    r = conn.execute("SELECT id, name, ingredients, steps, updated_at FROM recipes WHERE id=?",
                     (rid,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Reçete bulunamadı")
    return dict(r)


@app.get("/recipes", dependencies=[Depends(auth.require("checkout"))])
def recipes_list():
    with db() as conn:
        rows = conn.execute("SELECT id, name, ingredients, steps, updated_at FROM recipes"
                            " ORDER BY name COLLATE NOCASE").fetchall()
        menu = [r["name"] for r in conn.execute(
            "SELECT name FROM menu_items ORDER BY sort_order, id")]
    return {"recipes": [dict(r) for r in rows], "menu_names": menu}


@app.post("/recipes/add", dependencies=[Depends(auth.require("checkout"))])
def recipes_add(r: RecipeIn):
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO recipes (name, ingredients, steps, updated_at) VALUES (?, ?, ?, ?)",
                (r.name, r.ingredients, r.steps, datetime.now().isoformat()))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Bu isimde bir reçete zaten var")
        return _recipe_row(conn, cur.lastrowid)


@app.post("/recipes/update", dependencies=[Depends(auth.require("checkout"))])
def recipes_update(r: RecipeUpdate):
    with db() as conn:
        _recipe_row(conn, r.id)                      # 404 if it doesn't exist
        try:
            conn.execute(
                "UPDATE recipes SET name=?, ingredients=?, steps=?, updated_at=? WHERE id=?",
                (r.name, r.ingredients, r.steps, datetime.now().isoformat(), r.id))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Bu isimde bir reçete zaten var")
        return _recipe_row(conn, r.id)


@app.post("/recipes/delete", dependencies=[Depends(auth.require("checkout"))])
def recipes_delete(e: EntryId):
    with db() as conn:
        conn.execute("DELETE FROM recipes WHERE id=?", (e.id,))
    return {"status": "deleted"}


@app.get("/coffee/status", dependencies=[Depends(auth.require("checkout"))])
def coffee_status():
    with db() as conn:
        summary = _coffee_summary(conn)
        sections, extras, _ = _coffee_cfg(conn)
        anchor = _coffee_anchor(conn)
        sql, params = _coffee_filter(sections, extras)
        recent = conn.execute(
            "SELECT i.created_at, o.table_no, i.product_name, i.quantity" + sql +
            " ORDER BY i.created_at DESC, i.id DESC LIMIT 60",
            (anchor, *params)
        ).fetchall()
        today_count = _coffee_count(conn, since=datetime.now().strftime("%Y-%m-%d"))
        all_sections = [r["category"] for r in conn.execute(
            "SELECT category FROM menu_items GROUP BY category ORDER BY MIN(sort_order), MIN(id)")]
        maint = conn.execute(
            "SELECT id, done_at, note, coffee_count FROM coffee_maintenance"
            " ORDER BY done_at DESC, id DESC").fetchall()
    return {
        **summary,
        "since": anchor,
        "today_count": today_count,
        "sections": [{"name": s, "selected": s in sections} for s in all_sections],
        "extra_items": extras,
        "recent": [{"time": r["created_at"], "table_no": r["table_no"],
                    "table_label": table_label(r["table_no"]),
                    "product": r["product_name"], "qty": r["quantity"]} for r in recent],
        "maintenance": [dict(r) for r in maint],
    }


@app.post("/coffee/settings", dependencies=[Depends(auth.require("checkout"))])
def coffee_settings(s: CoffeeSettings):
    with db() as conn:
        if s.threshold is not None:
            _set_setting(conn, "coffee_threshold", str(s.threshold))
        if s.sections is not None:
            known = {r["category"] for r in conn.execute("SELECT DISTINCT category FROM menu_items")}
            unknown = [x for x in s.sections if x not in known]
            if unknown:
                raise HTTPException(status_code=400, detail="Bilinmeyen bölüm: " + ", ".join(unknown))
            _set_setting(conn, "coffee_sections", json.dumps(s.sections, ensure_ascii=False))
        return _coffee_summary(conn)


@app.post("/coffee/maintenance", dependencies=[Depends(auth.require("checkout"))])
def coffee_maintenance(m: CoffeeMaintenance):
    """Record a maintenance: snapshot the current count, then the counter
    starts again from 0 (the anchor moves to this entry's time)."""
    with db() as conn:
        count = _coffee_count(conn)
        conn.execute(
            "INSERT INTO coffee_maintenance (done_at, note, coffee_count) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), (m.note or "").strip() or None, count))
    log.info("Coffee machine maintenance logged after %s coffees", count)
    return {"status": "logged", "coffee_count": count}


@app.post("/coffee/delete-maintenance", dependencies=[Depends(auth.require("checkout"))])
def coffee_delete_maintenance(e: EntryId):
    """Delete a maintenance entry (e.g. pressed by mistake). The counter then
    falls back to the previous maintenance automatically."""
    with db() as conn:
        conn.execute("DELETE FROM coffee_maintenance WHERE id=?", (e.id,))
    return {"status": "deleted"}


@app.get("/tasks", dependencies=[Depends(auth.require("checkout"))])
def list_tasks(start: Optional[str] = None, end: Optional[str] = None):
    """Tasks in a date range [start, end] inclusive (YYYY-MM-DD). No range = all."""
    with db() as conn:
        if start and end:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE task_date >= ? AND task_date <= ?"
                " ORDER BY task_date, done, id",
                (start, end)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY task_date, done, id"
            ).fetchall()
    return {"tasks": [dict(r) for r in rows]}


@app.post("/tasks", dependencies=[Depends(auth.require("checkout"))])
def create_task(t: TaskCreate):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (task_date, title, category, created_at) VALUES (?, ?, ?, ?)",
            (t.task_date, t.title.strip(), (t.category or "genel"), datetime.now().isoformat())
        )
        tid = cur.lastrowid
    return {"status": "created", "id": tid}


@app.post("/tasks/update", dependencies=[Depends(auth.require("checkout"))])
def update_task(t: TaskUpdate):
    sets, vals = [], []
    if t.title is not None:     sets.append("title=?");     vals.append(t.title.strip())
    if t.task_date is not None: sets.append("task_date=?"); vals.append(t.task_date)
    if t.category is not None:  sets.append("category=?");  vals.append(t.category)
    if t.done is not None:
        sets.append("done=?");    vals.append(1 if t.done else 0)
        sets.append("done_at=?"); vals.append(datetime.now().isoformat() if t.done else None)
    if not sets:
        return {"status": "noop"}
    vals.append(t.id)
    with db() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
    return {"status": "updated"}


@app.post("/tasks/delete", dependencies=[Depends(auth.require("checkout"))])
def delete_task(t: TaskId):
    with db() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (t.id,))
    return {"status": "deleted"}


# =====================================================
# GELİR / GİDER (Income / Expenses) — admin/checkout only
# =====================================================

@app.get("/finance/categories", dependencies=[Depends(auth.require("checkout"))])
def finance_categories():
    """Expense category list from config (for the dropdown)."""
    return {"categories": config.EXPENSE_CATEGORIES}


@app.get("/finance/summary", dependencies=[Depends(auth.require("checkout"))])
def finance_summary(year: Optional[int] = None, month: Optional[int] = None):
    """Monthly summary: sales income + manual income + expenses + net."""
    now = datetime.now()
    y = year or now.year
    m = month or now.month
    prefix = f"{y:04d}-{m:02d}"
    with db() as conn:
        # Sales income (auto from POS)
        sales = conn.execute(
            "SELECT COALESCE(SUM(total_amount),0) AS total FROM sales_log WHERE paid_at LIKE ?",
            (f"{prefix}%",)
        ).fetchone()["total"]
        # Manual income
        mi_rows = conn.execute(
            "SELECT * FROM manual_income WHERE income_date LIKE ? ORDER BY income_date",
            (f"{prefix}%",)
        ).fetchall()
        manual_total = sum(r["amount"] for r in mi_rows)
        # Expenses
        exp_rows = conn.execute(
            "SELECT * FROM expenses WHERE expense_date LIKE ? ORDER BY expense_date DESC",
            (f"{prefix}%",)
        ).fetchall()
        exp_total = sum(r["amount"] for r in exp_rows)
        # Group expenses by category
        cat_totals = {}
        for r in exp_rows:
            cat_totals[r["category"]] = cat_totals.get(r["category"], 0) + r["amount"]
    total_income = sales + manual_total
    net = total_income - exp_total
    return {
        "year": y, "month": m,
        "sales_income": sales,
        "manual_income": manual_total,
        "total_income": total_income,
        "total_expenses": exp_total,
        "net_income": net,
        "expense_by_category": [{"category": k, "total": v} for k, v in sorted(cat_totals.items())],
        "expenses": [dict(r) for r in exp_rows],
        "manual_incomes": [dict(r) for r in mi_rows],
    }


@app.post("/finance/add-expense", dependencies=[Depends(auth.require("checkout"))])
def add_expense(e: ExpenseCreate):
    amount = round(e.quantity * e.unit_price, 2)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO expenses (expense_date, category, description, quantity, unit_price, amount, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (e.expense_date, e.category.strip(), (e.description or "").strip(),
             e.quantity, e.unit_price, amount, datetime.now().isoformat())
        )
    return {"status": "created", "id": cur.lastrowid, "amount": amount}


@app.post("/finance/delete-expense", dependencies=[Depends(auth.require("checkout"))])
def delete_expense(e: EntryId):
    with db() as conn:
        conn.execute("DELETE FROM expenses WHERE id=?", (e.id,))
    return {"status": "deleted"}


@app.post("/finance/add-income", dependencies=[Depends(auth.require("checkout"))])
def add_income(i: IncomeCreate):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO manual_income (income_date, description, amount, created_at)"
            " VALUES (?, ?, ?, ?)",
            (i.income_date, i.description.strip(), i.amount, datetime.now().isoformat())
        )
    return {"status": "created", "id": cur.lastrowid}


@app.post("/finance/delete-income", dependencies=[Depends(auth.require("checkout"))])
def delete_income(i: EntryId):
    with db() as conn:
        conn.execute("DELETE FROM manual_income WHERE id=?", (i.id,))
    return {"status": "deleted"}


@app.get("/dashboard")
def dashboard_page():
    return FileResponse(paths.resource_path("static/dashboard.html"))


# =====================================================
# STOCK — admin/checkout only
# =====================================================

@app.get("/stock/config", dependencies=[Depends(auth.require("checkout"))])
def stock_config():
    """Staff names + default threshold for the UI."""
    return {"staff": config.STAFF_NAMES, "threshold_default": config.STOCK_THRESHOLD_DEFAULT}


@app.get("/stock/items", dependencies=[Depends(auth.require("checkout"))])
def stock_items():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_items ORDER BY name"
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["low"] = _is_low(r["quantity"], r["threshold"])   # red flag
        items.append(d)
    low_count = sum(1 for i in items if i["low"])
    return {"items": items, "low_count": low_count}


@app.post("/stock/count", dependencies=[Depends(auth.require("checkout"))])
def stock_count(c: StockCount):
    """Recount-replace: set the true quantity and log who counted it.
    Stock is tracked in WHOLE units, so the count is rounded server-side."""
    now = datetime.now().isoformat()
    qty = _whole(c.quantity)
    with db() as conn:
        item = conn.execute("SELECT name FROM stock_items WHERE id=?", (c.id,)).fetchone()
        if not item:
            raise HTTPException(status_code=400, detail="Ürün bulunamadı")
        conn.execute(
            "UPDATE stock_items SET quantity=?, updated_at=?, updated_by=? WHERE id=?",
            (qty, now, c.counted_by.strip(), c.id)
        )
        conn.execute(
            "INSERT INTO stock_logs (item_id, item_name, quantity, counted_by, counted_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (c.id, item["name"], qty, c.counted_by.strip(), now)
        )
    return {"status": "counted"}


@app.post("/stock/add-item", dependencies=[Depends(auth.require("checkout"))])
def stock_add_item(i: StockItemCreate):
    th = i.threshold if i.threshold is not None else config.STOCK_THRESHOLD_DEFAULT
    th = _whole(th)
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO stock_items (name, unit, quantity, threshold) VALUES (?, ?, 0, ?)",
                (i.name.strip(), (i.unit or "adet"), th)
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Bu ürün zaten var")
    return {"status": "created", "id": cur.lastrowid}


@app.post("/stock/delete-item", dependencies=[Depends(auth.require("checkout"))])
def stock_delete_item(e: EntryId):
    with db() as conn:
        conn.execute("DELETE FROM stock_items WHERE id=?", (e.id,))
    return {"status": "deleted"}


@app.post("/stock/set-threshold", dependencies=[Depends(auth.require("checkout"))])
def stock_set_threshold(t: StockThreshold):
    with db() as conn:
        conn.execute("UPDATE stock_items SET threshold=? WHERE id=?", (_whole(t.threshold), t.id))
    return {"status": "updated"}


@app.post("/stock/delete-log", dependencies=[Depends(auth.require("checkout"))])
def stock_delete_log(e: EntryId):
    """Delete a single count-history row. Does not change the item's current
    quantity (that's the latest count, not this historical record)."""
    with db() as conn:
        conn.execute("DELETE FROM stock_logs WHERE id=?", (e.id,))
    return {"status": "deleted"}


@app.get("/stock/logs", dependencies=[Depends(auth.require("checkout"))])
def stock_logs(item_id: Optional[int] = None, limit: int = 50):
    with db() as conn:
        if item_id:
            rows = conn.execute(
                "SELECT * FROM stock_logs WHERE item_id=? ORDER BY counted_at DESC LIMIT ?",
                (item_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_logs ORDER BY counted_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return {"logs": [dict(r) for r in rows]}


# =====================================================
# REPORTS (PDF) — admin/checkout only
# =====================================================

def _fmt_expense(r) -> dict:
    """One expense row → report line, shared by the daily and monthly PDFs:
    'Kategori — açıklama (2×50)'."""
    return {"description": f'{r["category"]} — {r["description"] or ""}'
                           f' ({r["quantity"]:g}×{r["unit_price"]:.0f})',
            "amount": r["amount"]}


def _sold_items(conn, start: str, end_excl: str) -> list:
    """Products sold in [start, end_excl) — ISO dates/timestamps — from the
    sold_items ledger: each unit counted on the day its payment happened, the
    same clock as sales_log revenue. [{name, qty, total}], most sold first."""
    rows = conn.execute(
        "SELECT product_name AS name, SUM(qty) AS qty, SUM(amount) AS total"
        " FROM sold_items WHERE paid_at >= ? AND paid_at < ?"
        " GROUP BY product_name HAVING SUM(qty) > 1e-9"
        " ORDER BY qty DESC, name", (start, end_excl)).fetchall()
    items = [{"name": r["name"], "qty": round(r["qty"], 3), "total": round(r["total"], 2)}
             for r in rows]
    revenue = conn.execute(
        "SELECT COALESCE(SUM(total_amount), 0) AS t FROM sales_log"
        " WHERE paid_at >= ? AND paid_at < ?", (start, end_excl)).fetchone()["t"]
    if abs(revenue - sum(i["total"] for i in items)) > 0.01:
        # Expected only for history from before the ledger (old split/deleted
        # sales). New payments always write both sides together.
        log.debug("Report %s..%s: items total %.2f != revenue %.2f", start, end_excl,
                  sum(i["total"] for i in items), revenue)
    return items


def _next_day(day: str) -> str:
    return (Date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _build_daily_pdf(day: str) -> bytes:
    """Gather one day's full picture (sales, Ek Gelir, Gider, Görevler) and build
    the comprehensive 'Günlük Rapor' PDF."""
    import reports
    with db() as conn:
        rows = conn.execute(
            "SELECT total_amount, payment_type FROM sales_log WHERE paid_at LIKE ?",
            (f"{day}%",)
        ).fetchall()
        sold = _sold_items(conn, day, _next_day(day))
        exp_rows = conn.execute(
            "SELECT category, description, quantity, unit_price, amount"
            " FROM expenses WHERE expense_date = ? ORDER BY id",
            (day,)
        ).fetchall()
        mi_rows = conn.execute(
            "SELECT description, amount FROM manual_income WHERE income_date = ? ORDER BY id",
            (day,)
        ).fetchall()
        task_rows = conn.execute(
            "SELECT title, category, done FROM tasks WHERE task_date = ? ORDER BY done, id",
            (day,)
        ).fetchall()

    cash = sum(r["total_amount"] for r in rows if r["payment_type"] == "cash")
    card = sum(r["total_amount"] for r in rows if r["payment_type"] == "card")
    sales_total   = cash + card
    manual_total  = sum(r["amount"] for r in mi_rows)
    expense_total = sum(r["amount"] for r in exp_rows)
    income_total  = sales_total + manual_total
    totals = {
        "cash": cash, "card": card, "count": len(rows),
        "sales_total": sales_total, "manual": manual_total,
        "income_total": income_total, "expense_total": expense_total,
        "net": income_total - expense_total,
    }
    expenses = [_fmt_expense(r) for r in exp_rows]
    manual_incomes = [{"description": r["description"], "amount": r["amount"]} for r in mi_rows]
    tasks = [{"title": r["title"], "category": r["category"], "done": r["done"]} for r in task_rows]

    return reports.build_daily_pdf(day, sold, totals, expenses=expenses,
                                   manual_incomes=manual_incomes, tasks=tasks)


@app.get("/reports/daily", dependencies=[Depends(auth.require("checkout"))])
def report_daily(date: Optional[str] = None):
    """PDF for one day. date='YYYY-MM-DD' (default: today)."""
    day = date or datetime.now().strftime("%Y-%m-%d")
    try:
        day = Date.fromisoformat(day).isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz tarih (YYYY-AA-GG)")
    pdf = _build_daily_pdf(day)
    fname = f"gunluk_{day}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/reports/auto-status", dependencies=[Depends(auth.require("checkout"))])
def report_auto_status():
    """Automatic daily reports: on/off, newest day saved, where they are."""
    st = daily_reports.status() if config.DAILY_REPORT_ENABLED else {}
    with db() as conn:
        last = _setting(conn, daily_reports.STATE_KEY)
    folder = daily_reports.local_dir()
    files = sorted(folder.glob("gunluk_*.pdf")) if folder.is_dir() else []
    return {"enabled": config.DAILY_REPORT_ENABLED, "at": config.DAILY_REPORT_AT,
            "last_checked_day": last,
            "last_saved": files[-1].stem[len("gunluk_"):] if files else None,
            "count": len(files), "folder": str(folder),
            "mirrors": st.get("mirrors", []), "last_error": st.get("last_error")}


MAX_REPORT_DAYS = 366


@app.get("/reports/range", dependencies=[Depends(auth.require("checkout"))])
def report_range(start: Date, end: Date):
    """PDF for any period, both days inclusive (e.g. 2026-09-07 → 2026-11-15).
    Same data rules as the daily report, summed over the period."""
    import reports
    if start > end:
        raise HTTPException(status_code=400, detail="Bitiş tarihi başlangıçtan önce olamaz")
    if (end - start).days + 1 > MAX_REPORT_DAYS:
        raise HTTPException(status_code=400, detail=f"En fazla {MAX_REPORT_DAYS} günlük rapor alınabilir")
    s, e = start.isoformat(), end.isoformat()
    e_next = (end + timedelta(days=1)).isoformat()     # timestamps < next day = whole end day
    with db() as conn:
        rows = conn.execute(
            "SELECT total_amount, payment_type, substr(paid_at,1,10) AS day FROM sales_log"
            " WHERE paid_at >= ? AND paid_at < ?", (s, e_next)).fetchall()
        sold = _sold_items(conn, s, e_next)
        exp_rows = conn.execute(
            "SELECT category, description, quantity, unit_price, amount, expense_date"
            " FROM expenses WHERE expense_date >= ? AND expense_date <= ?"
            " ORDER BY expense_date, id", (s, e)).fetchall()
        mi_rows = conn.execute(
            "SELECT description, amount FROM manual_income WHERE income_date >= ? AND income_date <= ?"
            " ORDER BY income_date, id", (s, e)).fetchall()
        task_rows = conn.execute(
            "SELECT task_date AS date, title, category FROM tasks"
            " WHERE done=1 AND task_date >= ? AND task_date <= ? ORDER BY task_date, id",
            (s, e)).fetchall()

    cash = sum(r["total_amount"] for r in rows if r["payment_type"] == "cash")
    card = sum(r["total_amount"] for r in rows if r["payment_type"] == "card")
    sales_total   = cash + card
    manual_total  = sum(r["amount"] for r in mi_rows)
    expense_total = sum(r["amount"] for r in exp_rows)
    income_total  = sales_total + manual_total
    totals = {
        "cash": cash, "card": card, "count": len(rows),
        "sales_total": sales_total, "manual": manual_total,
        "income_total": income_total, "expense_total": expense_total,
        "net": income_total - expense_total,
    }
    per_day = {}
    for r in rows:
        d = per_day.setdefault(r["day"], {"date": r["day"], "total": 0.0, "count": 0})
        d["total"] += r["total_amount"]
        d["count"] += 1
    daily_rows = [per_day[k] for k in sorted(per_day)]
    pdf = reports.build_range_pdf(
        s, e,
        sold,
        daily_rows, totals,
        expenses=[_fmt_expense(r) for r in exp_rows],
        manual_incomes=[{"description": r["description"], "amount": r["amount"]} for r in mi_rows],
        done_tasks=[{"date": r["date"], "title": r["title"], "category": r["category"]} for r in task_rows],
    )
    fname = f"rapor_{s}_{e}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/reports/monthly", dependencies=[Depends(auth.require("checkout"))])
def report_monthly(year: Optional[int] = None, month: Optional[int] = None):
    """PDF for one month (default: current month)."""
    import reports
    now = datetime.now()
    y = year or now.year
    m = month or now.month
    prefix = f"{y:04d}-{m:02d}"
    with db() as conn:
        rows = conn.execute(
            "SELECT total_amount, payment_type FROM sales_log WHERE paid_at LIKE ?",
            (f"{prefix}%",)
        ).fetchall()
        daily = conn.execute(
            "SELECT substr(paid_at,1,10) AS date, SUM(total_amount) AS total"
            " FROM sales_log WHERE paid_at LIKE ?"
            " GROUP BY substr(paid_at,1,10) ORDER BY date",
            (f"{prefix}%",)
        ).fetchall()
        tasks = conn.execute(
            "SELECT task_date AS date, title, category FROM tasks"
            " WHERE done=1 AND task_date LIKE ? ORDER BY task_date",
            (f"{prefix}%",)
        ).fetchall()
        # Expenses for the monthly report
        exp_rows = conn.execute(
            "SELECT category, description, quantity, unit_price, amount, expense_date"
            " FROM expenses WHERE expense_date LIKE ? ORDER BY expense_date",
            (f"{prefix}%",)
        ).fetchall()
        # Manual income for the monthly report
        manual_rows = conn.execute(
            "SELECT description, amount FROM manual_income WHERE income_date LIKE ? ORDER BY income_date",
            (f"{prefix}%",)
        ).fetchall()
    cash = sum(r["total_amount"] for r in rows if r["payment_type"] == "cash")
    card = sum(r["total_amount"] for r in rows if r["payment_type"] == "card")
    manual_total = sum(r["amount"] for r in manual_rows)
    totals = {
        "cash": cash, "card": card,
        "manual": manual_total,
        "grand": cash + card + manual_total,
        "count": len(rows)
    }
    daily_rows = [{"date": r["date"], "total": r["total"]} for r in daily]
    done_tasks = [{"date": r["date"], "title": r["title"], "category": r["category"]} for r in tasks]
    expenses = [_fmt_expense(r) for r in exp_rows] if exp_rows else None
    manual_incomes = [{"description": r["description"], "amount": r["amount"]}
                      for r in manual_rows] if manual_rows else None
    pdf = reports.build_monthly_pdf(y, m, daily_rows, totals, done_tasks,
                                    expenses=expenses, manual_incomes=manual_incomes)
    fname = f"aylik_{prefix}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# =====================================================
# ERROR HANDLER (clean JSON instead of raw 500s)
# =====================================================

@app.exception_handler(Exception)
async def unhandled(request, exc):
    # Log the real cause server-side; never leak internals (SQL, paths,
    # stack details) to the LAN client.
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal_error"})


# =====================================================
# STATIC FRONTEND  (must be mounted LAST so it doesn't shadow API routes)
# Serves /  -> static/index.html
# =====================================================

def table_url(table: int) -> str:
    """What a table's QR code opens: http://siparis.local/masa15 (or the IP
    if mDNS isn't running)."""
    return f"{public_base()}/masa{table}"


@app.get("/qr")
def qr_code(table: int):
    """PNG QR code that opens the customer order page for a given table."""
    import qrcode, io
    url = table_url(table)
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"X-Order-URL": url})


@app.get("/qr-codes", response_class=HTMLResponse)
def qr_codes_page():
    """Printable sheet: every table's QR code, grouped by area."""
    from html import escape
    base = public_base()
    cards = []
    for area in _AREAS:
        if not area["tables"]:
            continue
        items = "".join(
            f'<div class="qr"><img src="/qr?table={n}" alt="{escape(table_label(n))}">'
            f'<div class="t">{escape(table_label(n))}</div>'
            f'<div class="u">{escape(table_url(n).replace("http://", ""))}</div></div>'
            for n in area["tables"])
        cards.append(f'<section><h2>{escape(area["name"])}</h2><div class="grid">{items}</div></section>')
    note = ("" if mdns.is_running() else
            "<p class='warn'>siparis.local şu an çalışmıyor — QR kodları IP adresini içeriyor. "
            "Bilgisayarın IP'si değişirse yeniden yazdırmanız gerekir.</p>")
    return f"""<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<title>QR Kodları — {escape(config.CAFE_NAME)}</title>
<style>
 body {{ font-family: Arial, sans-serif; margin: 24px; color:#1a1a1a; }}
 header {{ display:flex; justify-content:space-between; align-items:center; gap:12px; }}
 button {{ padding:10px 18px; font-weight:bold; border:none; border-radius:8px; background:#1a1a1a; color:#fff; cursor:pointer; }}
 h2 {{ margin:26px 0 10px; border-bottom:2px solid #1a1a1a; padding-bottom:4px; }}
 .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(170px, 1fr)); gap:14px; }}
 .qr {{ border:1.5px dashed #999; border-radius:10px; padding:10px; text-align:center; break-inside:avoid; }}
 .qr img {{ width:150px; height:150px; }}
 .t {{ font-size:20px; font-weight:bold; }}
 .u {{ font-size:11px; color:#555; word-break:break-all; }}
 .warn {{ background:#fff3cd; border:1px solid #f9a825; padding:10px; border-radius:8px; }}
 @media print {{ button, .hint {{ display:none; }} body {{ margin:8mm; }} section {{ break-inside:avoid-page; }} }}
</style></head><body>
<header><div><h1 style="margin:0;">{escape(config.CAFE_NAME)} — Masa QR Kodları</h1>
<div class="hint" style="color:#666;font-size:13px;">Müşteriler kodu okutunca sipariş sayfası açılır: {escape(base.replace("http://", ""))}/masa…</div></div>
<button onclick="window.print()">🖨 Yazdır</button></header>
{note}
{''.join(cards)}
</body></html>"""


@app.get("/masa{table_no}", include_in_schema=False)
def table_short_link(table_no: int):
    """Short customer link printed in the QR codes: /masa15 → /order?table=15.
    An unknown table still lands on the order page, which shows its error box."""
    return RedirectResponse(url=f"/order?table={table_no}", status_code=302)


@app.get("/garson", include_in_schema=False)
def waiter_short_link():
    return RedirectResponse(url="/waiter", status_code=302)


@app.get("/health")
def health():
    """Liveness + identity check (the launcher uses it so a second
    double-click opens the browser instead of starting a second server)."""
    return {"app": "yuzdoksanbes", "version": __version__,
            "port": ACTIVE_PORT, "url": public_base(), "mdns": mdns.is_running()}


_FRONTEND_PAGES = {"/", "/waiter", "/order", "/dashboard", "/qr-codes"}
_FRONTEND_EXTS = (".html", ".css", ".js")


@app.middleware("http")
async def frontend_no_cache(request: Request, call_next):
    """Make browsers re-check the frontend (HTML/CSS/JS) on every load.
    Without a Cache-Control header, browsers guess a freshness lifetime and can
    keep serving an OLD style.css/app.js for hours after an update. 'no-cache'
    = always revalidate; the ETag makes that a cheap 304 on the LAN."""
    response = await call_next(request)
    path = request.url.path
    if path in _FRONTEND_PAGES or path.endswith(_FRONTEND_EXTS):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/waiter")
def waiter_page():
    return FileResponse(paths.resource_path("static/waiter.html"))

@app.get("/order")
def order_page():
    return FileResponse(paths.resource_path("static/order.html"))

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(paths.resource_path("static/favicon.ico"), media_type="image/x-icon")


app.mount("/", StaticFiles(directory=paths.resource_path("static"), html=True), name="static")


# =====================================================
# LAUNCHER (double-click / desktop shortcut / Windows startup)
# =====================================================

def _running_instance_url():
    """If OUR server already answers on 80 or the fallback port, return its URL."""
    import urllib.request
    for port in (config.PORT, config.FALLBACK_PORT):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as r:
                if json.loads(r.read().decode("utf-8")).get("app") == "yuzdoksanbes":
                    return f"http://localhost{'' if port == 80 else f':{port}'}/"
        except Exception:
            continue
    return None


def _port_free(port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((config.HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> None:
    global ACTIVE_PORT
    import webbrowser
    # Turkish text + arrows in the banner/logs: never let an old console code
    # page turn a print() into a crash.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    url = _running_instance_url()
    if url:
        # Already running (e.g. started at Windows login) → just show it.
        print(f"{APP_NAME} zaten çalışıyor: {url}", flush=True)
        if config.OPEN_BROWSER_ON_START:
            webbrowser.open(url)
        return

    if _port_free(config.PORT):
        ACTIVE_PORT = config.PORT
    else:
        ACTIVE_PORT = config.FALLBACK_PORT
        log.warning("Port %s is busy — using %s instead", config.PORT, ACTIVE_PORT)

    if config.OPEN_BROWSER_ON_START:
        local = f"http://localhost{_port_suffix()}/"
        threading.Timer(2.0, lambda: webbrowser.open(local)).start()
    uvicorn.run(app, host=config.HOST, port=ACTIVE_PORT)


def _selftest() -> int:
    """Prove the BUILT program has everything it needs (build.bat runs this on
    the .exe and refuses to make an installer if it fails). Exercises the code
    paths that load optional-looking libraries late — PDF reports (fpdf →
    fontTools, unittest.mock…), QR codes (qrcode/PIL), mDNS (zeroconf) — against
    a throwaway database, with Turkish text and ₺."""
    import io
    import shutil
    import traceback
    print(f"{APP_NAME} {__version__} self-test (data: {paths.app_dir()})", flush=True)
    try:
        import qrcode, zeroconf, fpdf, PIL, reports   # noqa: F401  (import check)
        today = Date.today()
        with db() as conn:   # a little real data so every report section renders
            conn.execute("INSERT INTO sales_log (table_no, total_amount, payment_type, paid_at)"
                         " VALUES (11, 330, 'cash', ?)", (datetime.now().isoformat(),))
            conn.execute("INSERT INTO expenses (expense_date, category, description, quantity,"
                         " unit_price, amount, created_at) VALUES (?, 'Süt', 'Şişe süt ığüşöç', 2, 50, 100, ?)",
                         (today.isoformat(), datetime.now().isoformat()))
        checks = {
            "daily PDF":   report_daily(date=today.isoformat()).body,
            "monthly PDF": report_monthly(year=today.year, month=today.month).body,
            "range PDF":   report_range(start=today - timedelta(days=30), end=today).body,
        }
        buf = io.BytesIO()
        qrcode.make(table_url(15)).save(buf, format="PNG")
        checks["QR code PNG"] = buf.getvalue()
        import monitor
        checks["live monitor PNG"] = monitor.render_png(monitor_data())
        os.environ["POS_REPORT_MIRRORS"] = ""      # self-test never writes to Google Drive
        tomorrow_morning = datetime.combine(today + timedelta(days=1), datetime.min.time()) + timedelta(hours=5)
        saved = daily_reports.run_once(tomorrow_morning, _build_daily_pdf, _day_has_activity,
                                       lambda: None, lambda d: None)
        checks["automatic daily report"] = (
            (daily_reports.local_dir() / daily_reports.file_name(today)).read_bytes()
            if today.isoformat() in saved else b"")
        for name, data in checks.items():
            ok = data[:4] in (b"%PDF", b"\x89PNG")
            print(f"  {'OK  ' if ok else 'FAIL'} {name} ({len(data)} bytes)", flush=True)
            if not ok:
                raise RuntimeError(f"{name} is not valid")
        print("SELFTEST OK", flush=True)
        return 0
    except Exception:
        traceback.print_exc()
        print("SELFTEST FAILED", flush=True)
        return 1
    finally:
        logging.shutdown()
        shutil.rmtree(paths.app_dir(), ignore_errors=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        main()
    except Exception:
        # In the installed .exe a crash would close the window instantly —
        # keep it open so staff can read / photograph the error.
        log.exception("POS failed to start")
        if getattr(sys, "frozen", False):
            try:
                input("\nPOS başlatılamadı. Hata yukarıda. Kapatmak için Enter'a basın…")
            except Exception:
                pass
        raise