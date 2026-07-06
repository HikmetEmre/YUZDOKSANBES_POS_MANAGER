# config.py
# =====================================================
# SINGLE SOURCE OF TRUTH for café settings.
# Change values here; backend and frontend both read from this
# (frontend pulls it via the /config endpoint).
# =====================================================

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
# The REAL PINs come from environment variables so they never live in source
# control (this file is committed to git). If the env vars are missing the app
# falls back to the OBVIOUS placeholders below and logs a warning at startup.
# Set them once before going live via set_credentials.bat, or manually:
#   Windows:  setx POS_CHECKOUT_PIN 12345678   (then reopen the POS window)
_PLACEHOLDER_CHECKOUT_PIN = "0000"
_PLACEHOLDER_WAITER_PIN   = "1111"
CHECKOUT_PIN = os.environ.get("POS_CHECKOUT_PIN", _PLACEHOLDER_CHECKOUT_PIN)  # desk / cashier
WAITER_PIN   = os.environ.get("POS_WAITER_PIN",   _PLACEHOLDER_WAITER_PIN)    # waiters' phones

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
PORT        = 8000
CURRENCY    = "₺"

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

# Default low-stock threshold (red flag when quantity < this). Per-item editable in DB.
STOCK_THRESHOLD_DEFAULT = 5

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
BACKUP_DIR         = "backups"   # folder for snapshots (put on a USB/2nd drive if possible)
BACKUP_EVERY_MIN   = 60          # snapshot interval in minutes
BACKUP_KEEP_HOURLY = 24          # keep this many recent hourly backups
BACKUP_KEEP_DAILY  = 14          # plus one-per-day for this many days

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