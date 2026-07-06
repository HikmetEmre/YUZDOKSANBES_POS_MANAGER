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
from datetime import datetime
from typing import List, Optional
import logging
import sqlite3

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

import config
import backup
import paths
import auth

app = FastAPI(title="YUZDOKSANBES POS")

log = logging.getLogger("order_server")


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


@app.on_event("startup")
def _startup():
    # Loudly warn if the app is running on the built-in placeholder PINs
    # (i.e. POS_CHECKOUT_PIN / POS_WAITER_PIN were not set). Never deploy like this.
    if config.USING_PLACEHOLDER_PINS:
        log.warning(
            "SECURITY: placeholder login PIN(s) in use. Set POS_CHECKOUT_PIN and "
            "POS_WAITER_PIN before deployment (run set_credentials.bat)."
        )
    # Begin automatic DB backups (see ARCHITECTURE.md §8 / backup.py).
    backup.start_backup_scheduler()


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


class StatusUpdate(BaseModel):
    order_id: int
    status: str


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
    """True if table_no belongs to a real area (config.AREAS)."""
    return any(table_no in area["tables"] for area in config.AREAS)


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
        raise HTTPException(status_code=401, detail="Hatalı PIN")

    with _throttle_lock:
        _auth_fails.pop(ip, None)   # a correct PIN clears the device's failures
    return {"ok": True, "role": role, "token": auth.make_token(role)}


# =====================================================
# CONFIG (frontend pulls menu + table count from here)
# =====================================================

@app.get("/config")
def get_config():
    return {
        "areas": config.AREAS,
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
            return {"status": "empty"}

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
            "order_id": order["id"],
            "barista_status": order["barista_status"] or "hazırlanıyor",
            "created_at": order["created_at"],
            "items": [dict(i) for i in items],
            "total": total,
            "paid_total": paid_total,
            "unpaid_total": unpaid_total,
        }


@app.get("/tables-status")
def tables_status():
    with db() as conn:
        rows = conn.execute(
            "SELECT table_no FROM orders WHERE status='open'"
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
    }


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
    return {"tables": [{"table_no": t, "items": items} for t, items in tables.items()]}


@app.post("/cancel-order", dependencies=[Depends(auth.require("checkout"))])
def cancel_order(data: CancelRequest):
    with db() as conn:
        # ON DELETE CASCADE removes the items automatically.
        conn.execute(
            "DELETE FROM orders WHERE table_no=? AND status='open'",
            (data.table_no,)
        )
    return {"status": "cancelled"}


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


# =====================================================
# PAYMENT (Step 1: full-table pay, server-calculated total)
# Step 2 will add split-by-item on top of this.
# =====================================================

@app.post("/pay", dependencies=[Depends(auth.require("checkout"))])
def pay_table(payment: PaymentRequest):
    _validate_payment_type(payment.payment_type)
    with db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE table_no=? AND status='open'",
            (payment.table_no,)
        ).fetchone()

        if not order:
            raise HTTPException(status_code=400, detail="No open order")

        total = _order_total(conn, order["id"])  # AUTHORITATIVE total
        now = datetime.now().isoformat()

        # Settle every remaining unit so per-item reporting (dashboard
        # items_sold / top_items / daily PDF) counts items paid this way too.
        conn.execute(
            "UPDATE order_items SET paid_qty=quantity, paid=1, paid_at=?"
            " WHERE order_id=? AND paid_qty < quantity",
            (now, order["id"])
        )
        conn.execute(
            "UPDATE orders SET status='paid', paid_at=?, payment_type=?, total_amount=? WHERE id=?",
            (now, payment.payment_type, total, order["id"])
        )
        conn.execute(
            "INSERT INTO sales_log (table_no, total_amount, payment_type, paid_at)"
            " VALUES (?, ?, ?, ?)",
            (payment.table_no, total, payment.payment_type, now)
        )

    return {"status": "payment_completed", "total": total}


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
        for item_id, qty in want.items():
            row = conn.execute(
                "SELECT product_name, quantity, price, paid_qty FROM order_items"
                " WHERE id=? AND order_id=?",
                (item_id, order_id)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="Ürün bu adisyona ait değil")

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

        # Record + log each payment portion immediately (keeps dashboard accurate
        # even across multiple Dutch rounds on the same table).
        for s in req.splits:
            conn.execute(
                "INSERT INTO payments (order_id, table_no, amount, payment_type, paid_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (order_id, req.table_no, s.amount, s.method, now)
            )
            conn.execute(
                "INSERT INTO sales_log (table_no, total_amount, payment_type, paid_at)"
                " VALUES (?, ?, ?, ?)",
                (req.table_no, s.amount, s.method, now)
            )

        # Fully settled when no line has unpaid units left.
        remaining_units = conn.execute(
            "SELECT COALESCE(SUM(quantity - paid_qty), 0) AS r FROM order_items WHERE order_id=?",
            (order_id,)
        ).fetchone()["r"]

        order_closed = remaining_units <= 1e-9
        if order_closed:
            order_total = _order_total(conn, order_id)
            methods = {s.method for s in req.splits}
            label = methods.pop() if len(methods) == 1 else "split"
            conn.execute(
                "UPDATE orders SET status='paid', paid_at=?, payment_type=?, total_amount=? WHERE id=?",
                (now, label, order_total, order_id)
            )

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
    with db() as conn:
        conn.execute("DELETE FROM sales_log WHERE id=?", (log_id,))
    return {"status": "deleted"}


@app.get("/sales", dependencies=[Depends(auth.require("checkout"))])
def get_sales():
    with db() as conn:
        rows = conn.execute(
            "SELECT table_no, total_amount, payment_type, paid_at FROM sales_log"
            " ORDER BY paid_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


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
        "recent": [dict(r) for r in recent_rows],
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
        # Items actually sold today = paid units on orders paid today.
        item_rows = conn.execute(
            "SELECT i.product_name, SUM(i.paid_qty) AS qty"
            " FROM order_items i JOIN orders o ON o.id = i.order_id"
            " WHERE o.status='paid' AND o.paid_at LIKE ? AND i.paid_qty > 0"
            " GROUP BY i.product_name ORDER BY qty DESC",
            (f"{today}%",)
        ).fetchall()
    total = sum(r["total_amount"] for r in rows)
    cash  = sum(r["total_amount"] for r in rows if r["payment_type"] == "cash")
    card  = sum(r["total_amount"] for r in rows if r["payment_type"] == "card")
    items_sold = sum(r["qty"] or 0 for r in item_rows)
    return {
        "date": today,
        "revenue": total, "cash": cash, "card": card,
        "sale_count": len(rows),
        "items_sold": items_sold,
        "top_items": [{"name": r["product_name"], "qty": r["qty"]} for r in item_rows[:8]],
        "stock_check_day": datetime.now().weekday() in (2, 5),   # Wed=2, Sat=5
        "low_stock_count": _low_stock_count(),
    }


def _low_stock_count() -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM stock_items WHERE quantity < threshold"
        ).fetchone()["c"]


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
        d["low"] = r["quantity"] < r["threshold"]   # red flag
        items.append(d)
    low_count = sum(1 for i in items if i["low"])
    return {"items": items, "low_count": low_count}


@app.post("/stock/count", dependencies=[Depends(auth.require("checkout"))])
def stock_count(c: StockCount):
    """Recount-replace: set the true quantity and log who counted it."""
    now = datetime.now().isoformat()
    with db() as conn:
        item = conn.execute("SELECT name FROM stock_items WHERE id=?", (c.id,)).fetchone()
        if not item:
            raise HTTPException(status_code=400, detail="Ürün bulunamadı")
        conn.execute(
            "UPDATE stock_items SET quantity=?, updated_at=?, updated_by=? WHERE id=?",
            (c.quantity, now, c.counted_by.strip(), c.id)
        )
        conn.execute(
            "INSERT INTO stock_logs (item_id, item_name, quantity, counted_by, counted_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (c.id, item["name"], c.quantity, c.counted_by.strip(), now)
        )
    return {"status": "counted"}


@app.post("/stock/add-item", dependencies=[Depends(auth.require("checkout"))])
def stock_add_item(i: StockItemCreate):
    th = i.threshold if i.threshold is not None else config.STOCK_THRESHOLD_DEFAULT
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
        conn.execute("UPDATE stock_items SET threshold=? WHERE id=?", (t.threshold, t.id))
    return {"status": "updated"}


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

@app.get("/reports/daily", dependencies=[Depends(auth.require("checkout"))])
def report_daily(date: Optional[str] = None):
    """PDF for one day. date='YYYY-MM-DD' (default: today)."""
    import reports
    day = date or datetime.now().strftime("%Y-%m-%d")
    with db() as conn:
        rows = conn.execute(
            "SELECT total_amount, payment_type FROM sales_log WHERE paid_at LIKE ?",
            (f"{day}%",)
        ).fetchall()
        items = conn.execute(
            "SELECT i.product_name AS name, SUM(i.paid_qty) AS qty,"
            "       SUM(i.paid_qty * i.price) AS total"
            " FROM order_items i JOIN orders o ON o.id = i.order_id"
            " WHERE o.status='paid' AND o.paid_at LIKE ? AND i.paid_qty > 0"
            " GROUP BY i.product_name ORDER BY qty DESC",
            (f"{day}%",)
        ).fetchall()
    cash = sum(r["total_amount"] for r in rows if r["payment_type"] == "cash")
    card = sum(r["total_amount"] for r in rows if r["payment_type"] == "card")
    totals = {"cash": cash, "card": card, "grand": cash + card, "count": len(rows)}
    sold = [{"name": r["name"], "qty": r["qty"], "total": r["total"]} for r in items]
    pdf = reports.build_daily_pdf(day, sold, totals)
    fname = f"gunluk_{day}.pdf"
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
    expenses = [{"description": f'{r["category"]} — {r["description"] or ""} ({r["quantity"]}×{r["unit_price"]:.0f})',
                 "amount": r["amount"]} for r in exp_rows] if exp_rows else None
    pdf = reports.build_monthly_pdf(y, m, daily_rows, totals, done_tasks, expenses=expenses)
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

def _lan_ip() -> str:
    """Best-effort detection of this machine's LAN IP (so phones can reach it)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packets sent; just picks the outbound iface
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@app.get("/qr")
def qr_code(table: int):
    """PNG QR code that opens the customer order page for a given table.
    Encodes the LAN URL so a phone on the same WiFi can reach the server."""
    import qrcode, io
    url = f"http://{_lan_ip()}:{config.PORT}/order?table={table}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"X-Order-URL": url})


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
if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT)