# config.py
# =====================================================
# SINGLE SOURCE OF TRUTH for café settings.
# Change values here; backend and frontend both read from this
# (frontend pulls it via the /config endpoint).
# =====================================================

import json
import os
import secrets
from pathlib import Path

import paths

DB_PATH     = "pos.db"
# Two PINs = two power levels (see ARCHITECTURE.md §3).
#   CHECKOUT_PIN: full power — payments, cancel, revenue, management.
#   WAITER_PIN:   order-taking only. A waiter with this PIN CANNOT reach
#                 payment/cancel/revenue even by typing the checkout URL,
#                 because the server checks the role behind every request.
#
# The REAL PINs never live in source control (this file is committed to git).
# Where they come from, first match wins:
#   1. credentials.json in the data folder — the INSTALLER writes this from
#      its PIN page: {"checkout_pin": "...", "waiter_pin": "..."}. It wins so
#      the PINs chosen at install (or edited in that file, as the setup guide
#      says) always apply — even on a PC with old dev-era env vars.
#   2. environment variables POS_CHECKOUT_PIN / POS_WAITER_PIN
#      (development: set_credentials.bat, or  setx POS_CHECKOUT_PIN 12345678)
#   3. the OBVIOUS placeholders below, with a loud warning at startup.
_PLACEHOLDER_CHECKOUT_PIN = "0000"
_PLACEHOLDER_WAITER_PIN   = "1111"


def _load_file_pins() -> dict:
    try:
        f = Path(paths.data_path("credentials.json"))
        if f.exists():
            d = json.loads(f.read_text(encoding="utf-8-sig"))   # -sig: tolerate a BOM
            return {k: str(v).strip() for k, v in d.items() if str(v).strip()}
    except Exception:
        pass   # unreadable file → fall through to placeholders (and the warning)
    return {}


_FILE_PINS = _load_file_pins()
CHECKOUT_PIN = (_FILE_PINS.get("checkout_pin") or os.environ.get("POS_CHECKOUT_PIN")
                or _PLACEHOLDER_CHECKOUT_PIN)   # desk / cashier
WAITER_PIN   = (_FILE_PINS.get("waiter_pin") or os.environ.get("POS_WAITER_PIN")
                or _PLACEHOLDER_WAITER_PIN)     # waiters' phones

# True when either PIN is still the built-in placeholder (i.e. env var not set).
# order_server logs a loud warning at startup so this can't slip into production.
USING_PLACEHOLDER_PINS = (
    CHECKOUT_PIN == _PLACEHOLDER_CHECKOUT_PIN or WAITER_PIN == _PLACEHOLDER_WAITER_PIN
)

# Brute-force protection for the PIN login (/auth): after AUTH_MAX_FAILS wrong
# PINs from one device within AUTH_WINDOW_SEC, that device is locked out until
# its oldest failure ages past the window. A correct PIN clears the counter.
AUTH_MAX_FAILS  = 5
AUTH_WINDOW_SEC = 300       # 5 minutes


def _load_secret_key() -> str:
    """
    Key used to sign role tokens. Priority:
      1. POS_SECRET_KEY env var (best for servers / shared deployments), else
      2. a per-install random key persisted next to the app (secret_key.txt),
         so it's unique per café, survives restarts, and never ships in source.
    Changing the key only forces everyone to re-enter their PIN once.
    """
    env = os.environ.get("POS_SECRET_KEY")
    if env:
        return env
    key_file = Path(paths.data_path("secret_key.txt"))
    try:
        if key_file.exists():
            existing = key_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        generated = secrets.token_hex(32)
        key_file.write_text(generated, encoding="utf-8")
        return generated
    except Exception:
        # Last resort: an ephemeral key (tokens reset whenever the app restarts).
        return secrets.token_hex(32)


SECRET_KEY  = _load_secret_key()
HOST        = "0.0.0.0"     # 0.0.0.0 = reachable by tablets/phones on the LAN
# Port 80 = no ":8000" in any address (http://siparis.local/masa15). If port 80
# is already taken on this PC, the server falls back to FALLBACK_PORT by itself
# and every link / QR code uses the port it actually got.
PORT          = 80
FALLBACK_PORT = 8000
CURRENCY    = "₺"

# Friendly LAN name, announced by this PC over mDNS ("Bonjour") — no router
# setup needed. Phones on the café WiFi reach the POS at http://siparis.local
# and QR codes open http://siparis.local/masa15. If mDNS can't start, QR codes
# fall back to this PC's IP address automatically.
MDNS_ENABLED = True
MDNS_NAME    = "siparis"       # → siparis.local
# Open the POS in the browser when the server is started by double-click.
# Set the POS_NO_BROWSER environment variable to skip it (tests, service use).
OPEN_BROWSER_ON_START = not os.environ.get("POS_NO_BROWSER")

# Reports: café name shown on PDFs, and an optional logo (centered, top).
# Drop a PNG at LOGO_PATH whenever you have it; if missing, the name prints instead.
CAFE_NAME   = "YUZDOKSANBES"
LOGO_PATH   = "logo.png"     # relative to the app folder, or an absolute path

# Default expense categories (shown in the dropdown). Add/remove freely.
# Prices are NOT fixed here — staff enter the actual price each time they buy.
EXPENSE_CATEGORIES = [
    "Kahve Çekirdeği",
    "Süt (Karton)",
    "Şurup",
    "Çikolata",
    "Islak Mendil",
    "Un",
    "Şeker",
    "Çay",
    "Bardak / Kapak",
    "Temizlik Malzemesi",
    "Kira",
    "Elektrik",
    "Su Faturası",
    "Personel",
    "Diğer",
]

# Staff names for stock-count logs ("counted by"). Plus an "other" free-text option in UI.
STAFF_NAMES = ["Mehmet", "Tuğçe", "Sabri", "Melis"]

# Reasons offered when a saved product or a whole bill is cancelled (POS ✕ /
# İptal Et, waiter ✕). "Diğer" requires a typed reason. Every cancellation is
# recorded in the "İptal Edilenler" log (POS İptaller tab + dashboard page).
CANCEL_REASONS      = ["Yanlış Ürün", "Soğuk/Eksik", "Diğer"]
CANCEL_REASON_OTHER = "Diğer"

# Default low-stock threshold (red flag when quantity < this). Per-item editable in DB.
STOCK_THRESHOLD_DEFAULT = 5

# Coffee machine: maintenance is due after this many coffees (dashboard
# "Kahve Makinesi" tab). Only the STARTING value — change it in the tab.
COFFEE_MAINT_THRESHOLD_DEFAULT = 1000
# Menu sections whose items count as coffees made on the machine, plus single
# items from other sections. Only the starting value — editable in the tab.
COFFEE_SECTIONS_DEFAULT    = ["ESPRESSO BAR", "ESPRESSO BAR ICED", "BREW BAR"]
COFFEE_EXTRA_ITEMS_DEFAULT = ["Shot"]

# Items to seed into the stock list on first run (expense categories + stock-only extras).
# After first run the DB is the source of truth; editing this list won't change existing stock.
STOCK_SEED_EXTRA = [
    "Karton Bardak", "Kapak", "Pipet", "Peçete", "Çöp Poşeti",
    "Bulaşık Deterjanı", "Eldiven",
]

# =====================================================
# AUTO-BACKUP (see ARCHITECTURE.md §8)
# Uses SQLite's online backup API for safe, consistent snapshots while
# the DB is in use. Rolling retention so a corrupt live DB never wipes
# the last good copy.
# =====================================================
BACKUP_DIR         = "backups"   # local folder for snapshots (next to the app)
BACKUP_EVERY_MIN   = 60          # snapshot interval in minutes (hourly)
BACKUP_KEEP_HOURLY = 24          # keep this many recent hourly backups
BACKUP_KEEP_DAILY  = 14          # plus one-per-day for this many days
BACKUP_MAX_AGE_DAYS = 30         # hard cap: auto-delete ANY snapshot older than
                                 # this (local + mirrors). The single newest is
                                 # always kept, so a long-idle PC can't wipe all.

# Extra OFF-SITE backup folders (mirrors). After each local snapshot, the file
# is copied to every folder here — e.g. a Google Drive folder so backups sync
# to the cloud automatically. An offline/unmounted drive (G: not present, Drive
# app not running) is skipped with a warning and NEVER crashes the POS.
# Snapshots go into a per-PC subfolder of each mirror (e.g.
# G:\Drive'ım\pos-backups\<BACKUP_MACHINE_NAME>\), and only that subfolder is
# pruned (same retention as the local folder) — so another POS PC sharing the
# Drive folder, or manual copies placed in it, are never deleted by this PC.
#
# None = FIND Google Drive automatically, at every backup: the drive letter G:
# first, then D:–Z:, looking for the Drive folder in Turkish ("Drive'ım") or
# English ("My Drive") Windows/Google Drive. pos-backups is created inside it.
# Checked every time (not once) because the POS often starts at login BEFORE
# the Google Drive app has mounted its drive. A list = use exactly those folders.
BACKUP_MIRRORS       = None
GDRIVE_FOLDER_NAMES  = ["Drive'ım", "My Drive"]   # Turkish first (the cafés), then English
GDRIVE_BACKUP_FOLDER = "pos-backups"
# Live status picture for the owner's phone (monitor.py): every MONITOR_EVERY_SEC
# the POS overwrites ONE image, <Google Drive>\pos-monitor\canli_durum.png, with
# open tables (duration, totals) and today's revenue + cancellations. Open it in
# the Google Drive app and pull down to refresh. MONITOR_FOLDER = an explicit
# folder instead of auto-detect; POS_MONITOR_DIR env overrides (empty = off).
MONITOR_ENABLED     = True
MONITOR_EVERY_SEC   = 30
MONITOR_FOLDER      = None
MONITOR_FOLDER_NAME = "pos-monitor"
MONITOR_FILE        = "canli_durum.png"
# Override per PC without rebuilding: POS_BACKUP_MIRRORS = folders separated by
# ";" (e.g. "D:\Yedek;G:\Drive'ım\pos-backups"), or set it EMPTY to turn
# mirroring off (tests use this so test snapshots never reach Google Drive).
if "POS_BACKUP_MIRRORS" in os.environ:
    BACKUP_MIRRORS = [p.strip() for p in os.environ["POS_BACKUP_MIRRORS"].split(";") if p.strip()]
BACKUP_MACHINE_NAME = ""         # subfolder name; "" = this PC's hostname

# Automatic daily report (daily_reports.py): every day the POS saves its
# "Günlük Rapor" PDF by itself to <data>\reports\gunluk\ and to
# <Google Drive>\pos-reports\<PC>\, at DAILY_REPORT_AT ("23:59" = that same
# evening; a time before 12:00, e.g. "04:00", = the next morning). Anything
# paid/cancelled/entered for the day after that is added once the day is over
# (the PDF is made again). Missed days (PC off) are made at the next start, up
# to DAILY_REPORT_CATCHUP_DAYS back; days with no activity are skipped.
# POS_REPORT_MIRRORS env overrides the Drive folder (";"-separated; EMPTY =
# local only — tests use this).
DAILY_REPORT_ENABLED      = True
DAILY_REPORT_AT           = "23:59"
DAILY_REPORT_CHECK_MIN    = 10
DAILY_REPORT_CATCHUP_DAYS = 31
DAILY_REPORT_FOLDER_NAME  = "pos-reports"

# =====================================================
# LOGGING (system health / audit trail)
# A rolling on-disk log so problems are never lost: failed backups, unhandled
# 500 errors, PIN brute-force lockouts, scheduler activity and startup warnings
# all land in logs/pos.log. It ROTATES (never grows unbounded) and also prints
# to the console so a window/.bat still shows live output.
# =====================================================
LOG_DIR          = "logs"        # folder next to the app (created on first run)
LOG_FILE         = "pos.log"     # current file; older ones roll to pos.log.1 … .N
LOG_LEVEL        = "INFO"         # DEBUG | INFO | WARNING | ERROR
LOG_MAX_BYTES    = 1_000_000     # rotate after ~1 MB per file
LOG_BACKUP_COUNT = 7             # keep this many rotated files

# =====================================================
# TABLE AREAS
# Each area has a display name and its list of table numbers.
# IMPORTANT: table numbers must be UNIQUE across ALL areas
# (the system identifies a table by its number alone).
# To add/remove a table, just edit the number lists below.
# =====================================================
AREAS = [
    {"id": "indoor", "name": "İç Mekan",     "tables": [11, 12, 13, 14, 15]},
    {"id": "winter", "name": "Kış Bahçesi",  "tables": [21, 22, 23, 24, 25, 26, 27, 28]},
    {"id": "garden", "name": "Bahçe",        "tables": [31, 32, 33, 34, 35, 36, 37, 38, 39, 40]},
    {"id": "backyard", "name": "Arka Bahçe", "tables": [50, 51, 52, 53, 54, 55, 56, 57]},
]

# Auto-backup the SQLite DB this many times per day is handled in Step 3.

MENU = {
    "ESPRESSO BAR": [
        ("Espresso", 140), ("Americano", 170), ("Latte", 190),
        ("Cortado", 190), ("Flat White", 200), ("Cappuccino", 190),
        ("White Chocolate Mocha", 200), ("Caramel Latte", 200),
        ("Salted Caramel Latte", 200), ("Vanilla Latte", 200),
    ],
    "ESPRESSO BAR ICED": [
        ("Americano ICED", 180), ("Latte ICED", 200),
        ("Flat White ICED", 210), ("White Chocolate Mocha ICED", 210),
        ("Caramel Latte ICED", 210), ("Salted Caramel Latte ICED", 210),
        ("Vanilla Latte ICED", 210),
    ],
    "BREW BAR": [
        ("Filter Coffee", 170), ("Iced Filter Coffee", 180),
        ("V60", 235), ("AeroPress", 235),
        ("Turkish Coffee", 110), ("Double Turkish Coffee", 200),
    ],
    "EXTRAS": [
        ("Milk", 30), ("Shot", 50), ("Sauce", 30),
    ],
    "TEAS": [
        ("Demleme Siyah Cay", 60), ("Harman Demleme Cay", 120),
        ("Yesil Cay", 180), ("Ada Cay", 180),
        ("Ihlamur Cay", 180), ("Nane Limon Cay", 180),
        ("Kis Cay", 180), ("Kusburnu Cay", 180),
    ],
    "HOT DRINKS": [
        ("Hot Chocolate", 220), ("195 Special", 195),
    ],
    "SOFT DRINKS": [
        ("Churchill", 150), ("Premium Mineral Soda", 90),
        ("Premium Su", 40),
    ],
    "ICED BAR": [
        ("Meyveli Soda", 90), ("Iced Tea", 120),
        ("Sprite", 130), ("Redbull", 170),
    ],
    "MILKSHAKES": [
        ("Chocolate Milkshake", 220),
        ("Muzlu Milkshake", 220),
        ("Cilekli Milkshake", 220),
    ],
    "COLD DRINKS": [
        ("Cool Lime", 200), ("Cool Berry", 200),
    ],
    "DESSERT BAR": [
        ("Berry Croissant", 270), ("Berry Drizzle", 350),
        ("San Sebastian", 270), ("Cookie", 140),
        ("Magnolia", 220), ("Very Berry Muffin", 190),
        ("Tiramisu", 230), ("Lotus Cup", 250),
        ("Orman Meyveli Cup", 240), ("Mono Profiterol", 250),
    ],
    "SNACK BAR": [
        ("Tereyagli Kruvasan", 190),
        ("Uc Peynirli Sandwich", 250),
        ("Hindi Fume Sandwich", 250),
    ],
}

# Flatten menu into a name -> price lookup. The SERVER uses this to compute
# totals from authoritative prices, so a tampered client cannot under/over-charge.
PRICE_LOOKUP = {name: price for items in MENU.values() for (name, price) in items}