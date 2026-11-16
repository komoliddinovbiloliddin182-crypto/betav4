"""SQLite ma'lumotlar bazasi: ulanish, jadvallar va yordamchi funksiyalar.

Jadvallar:
  users, sessions, managed_bots, shop_products, shop_cart, shop_orders,
  support_tickets, quiz_results,
  settings              — kalit/qiymat sozlamalar (bot narxi, sinov muddati va h.k.),
  balance_topups        — mijoz to'lov cheki so'rovlari (admin tasdiqlaydi/rad etadi),
  balance_ledger        — balans harakatlari tarixi (+to'lov, -bot xaridi),
  admin_chats           — admin ↔ foydalanuvchi 1:1 suhbati,
  admin_broadcasts      — admin → barcha xabar (ommaviy yuborish).
"""
import asyncio
import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "botbuilder.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Bitta global ulanish — aiogram polling va FastAPI bilan birga ishlaydi.
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")
db_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    username           TEXT NOT NULL UNIQUE,
    password_hash      TEXT NOT NULL,
    is_admin           INTEGER NOT NULL DEFAULT 0,
    is_blocked         INTEGER NOT NULL DEFAULT 0,
    balance            INTEGER NOT NULL DEFAULT 0,
    ever_created_bot   INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS managed_bots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username          TEXT NOT NULL,
    token_ciphertext  TEXT NOT NULL,
    token_hash        TEXT NOT NULL UNIQUE,
    template          TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    trial_ends_at     TEXT NOT NULL,
    is_active         INTEGER NOT NULL DEFAULT 1,
    is_paid           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shop_products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id      INTEGER NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    price       INTEGER NOT NULL,
    description TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shop_cart (
    bot_id      INTEGER NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
    tg_user_id  INTEGER NOT NULL,
    product_id  INTEGER NOT NULL REFERENCES shop_products(id) ON DELETE CASCADE,
    qty         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (bot_id, tg_user_id, product_id)
);

CREATE TABLE IF NOT EXISTS shop_orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id         INTEGER NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
    tg_user_id     INTEGER NOT NULL,
    tg_username    TEXT,
    customer_name  TEXT,
    customer_phone TEXT,
    items_json     TEXT NOT NULL,
    total          INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'new',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id      INTEGER NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
    tg_user_id  INTEGER NOT NULL,
    tg_username TEXT,
    ticket_no   INTEGER NOT NULL,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quiz_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id      INTEGER NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
    tg_user_id  INTEGER NOT NULL,
    tg_username TEXT,
    score       INTEGER NOT NULL,
    total       INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balance_topups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount       INTEGER NOT NULL,
    check_path   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    admin_note   TEXT,
    reviewed_by  INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL,
    reviewed_at  TEXT
);

CREATE TABLE IF NOT EXISTS balance_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta       INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    ref_id      INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_chats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    to_user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body          TEXT NOT NULL,
    is_read       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def init_db() -> None:
    """Barcha jadvallarni yaratadi va eski DB uchun migratsiyani bajaradi."""
    db.executescript(SCHEMA)

    # managed_bots.owner_id (eski jadval uchun)
    cols = {row["name"] for row in db.execute("PRAGMA table_info(managed_bots)")}
    if "owner_id" not in cols:
        db.execute(
            "ALTER TABLE managed_bots ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id)"
        )
    if "is_paid" not in cols:
        db.execute("ALTER TABLE managed_bots ADD COLUMN is_paid INTEGER NOT NULL DEFAULT 0")

    # users: is_blocked / balance / ever_created_bot (eski jadval uchun)
    user_cols = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
    if "is_blocked" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0")
    if "balance" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN balance INTEGER NOT NULL DEFAULT 0")
    if "ever_created_bot" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN ever_created_bot INTEGER NOT NULL DEFAULT 0")

    db.commit()


def count_users() -> int:
    return db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


# ── Sozlamalar (kalit/qiymat) ──

def get_setting(key: str, default: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


def get_site_admin_id() -> int:
    """Sayt adminining user id sini qaytaradi (ADMIN_USERNAME orqali)."""
    from auth import ADMIN_USERNAME
    row = db.execute("SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone()
    if not row:
        raise RuntimeError("Sayt admini topilmadi.")
    return row["id"]
