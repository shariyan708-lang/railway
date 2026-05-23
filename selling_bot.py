from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "telegram_selling_bot.sqlite3"
MAX_TEXT = 3900


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_cents(value: str | int | None, default: int = 0) -> int:
    if value is None:
        return default
    text = str(value).strip().replace(",", "")
    if not text:
        return default
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return default
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(cents: int | None, currency: str) -> str:
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    amount = f"{sign}{cents // 100}.{cents % 100:02d}"
    code = (currency or "").strip()
    if code.upper() in {"USD", "USDT", "DOLLAR"} or code == "$":
        return f"${amount}"
    return f"{amount} {code}".strip()


def chunk_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(True):
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


def parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for item in raw.replace(" ", "").split(","):
        if item and item.lstrip("-").isdigit():
            ids.add(int(item))
    return ids


class Store:
    def __init__(self, db_path: Path | None = None, database_url: str = ""):
        self.lock = threading.RLock()
        self.database_url = database_url.strip()
        self.is_pg = self.database_url.startswith(("postgres://", "postgresql://"))
        self.settings_cache_seconds = float(os.getenv("SETTINGS_CACHE_SECONDS", "5"))
        self._settings_cache: dict[str, str] | None = None
        self._settings_cache_until = 0.0
        self._channels_cache: dict[bool, tuple[float, list[Any]]] = {}
        if self.is_pg:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL mode needs psycopg. Install requirements.txt on Render."
                ) from exc
            self.conn = psycopg.connect(self.database_url, row_factory=dict_row)
            self.conn.autocommit = True
        else:
            path = db_path or DEFAULT_DB
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(path), check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA temp_store=MEMORY")
            self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()
        self.init_migrations()
        self.init_indexes()
        self.ensure_defaults()

    def q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_pg else sql

    def execute(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        *,
        one: bool = False,
        all_rows: bool = False,
    ) -> Any:
        with self.lock:
            cur = self.conn.execute(self.q(sql), params)
            result = None
            if one:
                result = cur.fetchone()
            elif all_rows:
                result = cur.fetchall()
            if not self.is_pg and not sql.lstrip().upper().startswith("SELECT"):
                self.conn.commit()
            return result if (one or all_rows) else cur

    def execute_many(self, sql: str, rows: list[tuple[Any, ...]]) -> int:
        if not rows:
            return 0
        with self.lock:
            self.begin()
            try:
                cur = self.conn.cursor()
                cur.executemany(self.q(sql), rows)
                count = int(cur.rowcount or 0)
                self.commit()
                return count
            except Exception:
                self.rollback()
                raise

    def begin(self) -> None:
        self.conn.execute("BEGIN" if self.is_pg else "BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    def init_schema(self) -> None:
        if self.is_pg:
            statements = [
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    referred_by BIGINT,
                    balance_cents INTEGER NOT NULL DEFAULT 0,
                    is_banned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS channels (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    chat_id TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS products (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    emoji TEXT NOT NULL DEFAULT '📦',
                    description TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS product_variants (
                    id BIGSERIAL PRIMARY KEY,
                    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    days INTEGER NOT NULL DEFAULT 0,
                    price_cents INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS stock_items (
                    id BIGSERIAL PRIMARY KEY,
                    variant_id BIGINT NOT NULL REFERENCES product_variants(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available',
                    sold_to BIGINT REFERENCES users(user_id),
                    sold_at TEXT,
                    created_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS user_variant_prices (
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    variant_id BIGINT NOT NULL REFERENCES product_variants(id) ON DELETE CASCADE,
                    price_cents INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, variant_id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id),
                    product_id BIGINT NOT NULL REFERENCES products(id),
                    variant_id BIGINT NOT NULL REFERENCES product_variants(id),
                    stock_item_id BIGINT REFERENCES stock_items(id),
                    price_cents INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    delivered_content TEXT,
                    created_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS topups (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id),
                    amount_cents INTEGER NOT NULL,
                    method TEXT NOT NULL DEFAULT '',
                    txn_ref TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    admin_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                "CREATE TABLE IF NOT EXISTS admin_states (admin_id BIGINT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS audit_logs (id BIGSERIAL PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)",
            ]
        else:
            statements = [
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    referred_by INTEGER,
                    balance_cents INTEGER NOT NULL DEFAULT 0,
                    is_banned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    chat_id TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    emoji TEXT NOT NULL DEFAULT '📦',
                    description TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS product_variants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    days INTEGER NOT NULL DEFAULT 0,
                    price_cents INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS stock_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    variant_id INTEGER NOT NULL REFERENCES product_variants(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available',
                    sold_to INTEGER REFERENCES users(user_id),
                    sold_at TEXT,
                    created_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS user_variant_prices (
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    variant_id INTEGER NOT NULL REFERENCES product_variants(id) ON DELETE CASCADE,
                    price_cents INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, variant_id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    variant_id INTEGER NOT NULL REFERENCES product_variants(id),
                    stock_item_id INTEGER REFERENCES stock_items(id),
                    price_cents INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    delivered_content TEXT,
                    created_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS topups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    amount_cents INTEGER NOT NULL,
                    method TEXT NOT NULL DEFAULT '',
                    txn_ref TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    admin_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                "CREATE TABLE IF NOT EXISTS admin_states (admin_id INTEGER PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)",
            ]
        with self.lock:
            for statement in statements:
                self.conn.execute(statement)
            if not self.is_pg:
                self.conn.commit()

    def init_indexes(self) -> None:
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance_cents)",
            "CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)",
            "CREATE INDEX IF NOT EXISTS idx_users_banned_created ON users(is_banned, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_channels_enabled_sort ON channels(enabled, sort_order, id)",
            "CREATE INDEX IF NOT EXISTS idx_products_active_sort ON products(active, sort_order, id)",
            "CREATE INDEX IF NOT EXISTS idx_variants_product_active_sort ON product_variants(product_id, active, sort_order, id)",
            "CREATE INDEX IF NOT EXISTS idx_stock_variant_status_id ON stock_items(variant_id, status, id)",
            "CREATE INDEX IF NOT EXISTS idx_stock_sold_to ON stock_items(sold_to)",
            "CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_topups_status_created ON topups(status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_admin_states_updated ON admin_states(updated_at)",
        ]
        with self.lock:
            for statement in indexes:
                self.conn.execute(statement)
            if not self.is_pg:
                self.conn.commit()

    def invalidate_settings_cache(self) -> None:
        self._settings_cache = None
        self._settings_cache_until = 0.0

    def invalidate_channels_cache(self) -> None:
        self._channels_cache.clear()

    def table_columns(self, table: str) -> set[str]:
        with self.lock:
            if self.is_pg:
                rows = self.conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                    (table,),
                ).fetchall()
                return {str(row["column_name"]) for row in rows}
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            return {str(row["name"]) for row in rows}

    def add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        if column in self.table_columns(table):
            return
        with self.lock:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            if not self.is_pg:
                self.conn.commit()

    def init_migrations(self) -> None:
        self.add_column_if_missing(
            "users",
            "referred_by",
            "BIGINT" if self.is_pg else "INTEGER",
        )
        self.add_column_if_missing(
            "products",
            "emoji",
            "TEXT NOT NULL DEFAULT '📦'",
        )

    def ensure_defaults(self) -> None:
        defaults = {
            "bot_name": "Fluorite Gift",
            "currency": "USD",
            "join_required": "1",
            "welcome_text": "Welcome. Use the buttons below.",
            "join_text": "ACCESS DENIED!\n\nYou must join our channels to unlock the bot features.",
            "verify_failed_text": "Please join all required channels, then tap Verify again.",
            "help_text": "Need help? Contact support.",
            "contact_text": "Support: @your_support_username",
            "payment_methods": "Send payment to admin, then send:\n/pay amount transaction_id",
            "low_balance_text": "Insufficient balance. Please top up and try again.",
            "empty_stock_text": "This variant is out of stock.",
            "banned_text": "Your account is banned. Contact support.",
            "order_success_text": "Purchase successful. Your item is delivered below.",
            "bot_username": os.getenv("BOT_USERNAME", ""),
            "referral_enabled": "1",
            "referral_reward": "0.01",
            "owner_url": "https://t.me/your_support_username",
            "channel_url": "https://t.me/your_channel",
            "info_text": "HOW IT WORKS\n\nStep 1: Earn balance by inviting friends.\nStep 2: Visit the shop to buy your keys.\nStep 3: Use your balance to purchase products.\n\nServer Status: Operational",
            "redeem_text": "Redeem is available through support. Contact owner for redeem code help.",
        }
        for key, value in defaults.items():
            self.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
        self.invalidate_settings_cache()

    def setting(self, key: str, default: str = "") -> str:
        return self.settings_map().get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        if key == "bot_username":
            value = value.strip().lstrip("@")
        self.execute(
            """
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.invalidate_settings_cache()

    def settings_map(self) -> dict[str, str]:
        if self._settings_cache is not None and time.monotonic() < self._settings_cache_until:
            return dict(self._settings_cache)
        rows = self.execute("SELECT key, value FROM settings", all_rows=True)
        settings = {str(row["key"]): str(row["value"]) for row in rows}
        self._settings_cache = settings
        self._settings_cache_until = time.monotonic() + self.settings_cache_seconds
        return dict(settings)

    def set_state(self, admin_id: int, state: str, data: dict[str, Any] | None = None) -> None:
        self.execute(
            """
            INSERT INTO admin_states(admin_id, state, data, updated_at) VALUES(?, ?, ?, ?)
            ON CONFLICT(admin_id) DO UPDATE SET
                state = excluded.state,
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (admin_id, state, json.dumps(data or {}), now_iso()),
        )

    def get_state(self, admin_id: int) -> tuple[str, dict[str, Any]] | None:
        row = self.execute("SELECT state, data FROM admin_states WHERE admin_id = ?", (admin_id,), one=True)
        if not row:
            return None
        try:
            data = json.loads(row["data"] or "{}")
        except json.JSONDecodeError:
            data = {}
        return str(row["state"]), data

    def clear_state(self, admin_id: int) -> None:
        self.execute("DELETE FROM admin_states WHERE admin_id = ?", (admin_id,))

    def log(self, actor: str, action: str, details: str = "") -> None:
        self.execute(
            "INSERT INTO audit_logs(actor, action, details, created_at) VALUES(?, ?, ?, ?)",
            (actor, action, details, now_iso()),
        )

    def upsert_user(
        self,
        tg_user: dict[str, Any],
        referrer_id: int | None = None,
        referral_reward_cents: int = 0,
    ) -> Any:
        stamp = now_iso()
        user_id = int(tg_user["id"])
        username = tg_user.get("username") or ""
        first_name = tg_user.get("first_name") or ""
        last_name = tg_user.get("last_name") or ""
        clean_referrer = referrer_id if referrer_id and referrer_id != user_id else None
        with self.lock:
            self.begin()
            try:
                existing = self.conn.execute(
                    self.q("SELECT user_id FROM users WHERE user_id = ?"),
                    (user_id,),
                ).fetchone()
                if existing:
                    self.conn.execute(
                        self.q(
                            """
                            UPDATE users
                            SET username = ?, first_name = ?, last_name = ?, updated_at = ?
                            WHERE user_id = ?
                            """
                        ),
                        (username, first_name, last_name, stamp, user_id),
                    )
                else:
                    self.conn.execute(
                        self.q(
                            """
                            INSERT INTO users(user_id, username, first_name, last_name, referred_by, created_at, updated_at)
                            VALUES(?, ?, ?, ?, ?, ?, ?)
                            """
                        ),
                        (user_id, username, first_name, last_name, clean_referrer, stamp, stamp),
                    )
                    if clean_referrer and referral_reward_cents > 0:
                        self.conn.execute(
                            self.q(
                                "UPDATE users SET balance_cents = balance_cents + ?, updated_at = ? WHERE user_id = ?"
                            ),
                            (referral_reward_cents, stamp, clean_referrer),
                        )
                        self.conn.execute(
                            self.q(
                                "INSERT INTO audit_logs(actor, action, details, created_at) VALUES(?, ?, ?, ?)"
                            ),
                            (
                                "system",
                                "referral_reward",
                                f"referrer={clean_referrer}; new_user={user_id}; amount_cents={referral_reward_cents}",
                                stamp,
                            ),
                        )
                self.commit()
            except Exception:
                self.rollback()
                raise
        return self.user(user_id)

    def user(self, user_id: int) -> Any:
        return self.execute("SELECT * FROM users WHERE user_id = ?", (user_id,), one=True)

    def users(self, limit: int = 20, offset: int = 0, balance_only: bool = False) -> list[Any]:
        where = "WHERE balance_cents > 0" if balance_only else ""
        return self.execute(
            f"SELECT * FROM users {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
            all_rows=True,
        )

    def user_count(self, balance_only: bool = False) -> int:
        where = "WHERE balance_cents > 0" if balance_only else ""
        row = self.execute(f"SELECT COUNT(*) AS n FROM users {where}", one=True)
        return int(row["n"])

    def referral_count(self, referrer_id: int) -> int:
        row = self.execute(
            "SELECT COUNT(*) AS n FROM users WHERE referred_by = ?",
            (referrer_id,),
            one=True,
        )
        return int(row["n"])

    def all_user_ids(self) -> list[int]:
        rows = self.execute("SELECT user_id FROM users WHERE is_banned = 0 ORDER BY created_at ASC", all_rows=True)
        return [int(row["user_id"]) for row in rows]

    def adjust_balance(self, user_id: int, amount_cents: int, actor: str) -> None:
        self.execute(
            "UPDATE users SET balance_cents = balance_cents + ?, updated_at = ? WHERE user_id = ?",
            (amount_cents, now_iso(), user_id),
        )
        self.log(actor, "adjust_balance", f"user={user_id}; amount_cents={amount_cents}")

    def set_ban(self, user_id: int, banned: bool, actor: str) -> None:
        self.execute(
            "UPDATE users SET is_banned = ?, updated_at = ? WHERE user_id = ?",
            (1 if banned else 0, now_iso(), user_id),
        )
        self.log(actor, "ban" if banned else "unban", f"user={user_id}")

    def channels(self, enabled_only: bool = False) -> list[Any]:
        cached = self._channels_cache.get(enabled_only)
        if cached and time.monotonic() < cached[0]:
            return list(cached[1])
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = self.execute(
            f"SELECT * FROM channels {where} ORDER BY sort_order ASC, id ASC",
            all_rows=True,
        )
        self._channels_cache[enabled_only] = (time.monotonic() + 10, rows)
        return list(rows)

    def add_channel(self, title: str, link: str, chat_id: str) -> None:
        self.execute(
            "INSERT INTO channels(title, link, chat_id, enabled, sort_order, created_at) VALUES(?, ?, ?, 1, 0, ?)",
            (title.strip(), link.strip(), chat_id.strip(), now_iso()),
        )
        self.invalidate_channels_cache()

    def delete_channel(self, channel_id: int) -> None:
        self.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        self.invalidate_channels_cache()

    def toggle_channel(self, channel_id: int) -> None:
        self.execute(
            "UPDATE channels SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END WHERE id = ?",
            (channel_id,),
        )
        self.invalidate_channels_cache()

    def products(self, active_only: bool = False) -> list[Any]:
        where = "WHERE p.active = 1" if active_only else ""
        return self.execute(
            f"""
            SELECT p.*,
                (SELECT COUNT(*) FROM product_variants v WHERE v.product_id = p.id) AS variant_count
            FROM products p
            {where}
            ORDER BY p.sort_order ASC, p.id ASC
            """,
            all_rows=True,
        )

    def product(self, product_id: int) -> Any:
        return self.execute("SELECT * FROM products WHERE id = ?", (product_id,), one=True)

    def add_product(self, title: str, description: str = "", emoji: str = "📦") -> int:
        stamp = now_iso()
        emoji = (emoji or "📦").strip()[:8]
        if self.is_pg:
            row = self.execute(
                """
                INSERT INTO products(title, emoji, description, active, sort_order, created_at, updated_at)
                VALUES(?, ?, ?, 1, 0, ?, ?) RETURNING id
                """,
                (title.strip(), emoji, description.strip(), stamp, stamp),
                one=True,
            )
            return int(row["id"])
        cur = self.execute(
            """
            INSERT INTO products(title, emoji, description, active, sort_order, created_at, updated_at)
            VALUES(?, ?, ?, 1, 0, ?, ?)
            """,
            (title.strip(), emoji, description.strip(), stamp, stamp),
        )
        return int(cur.lastrowid)

    def set_product_emoji(self, product_id: int, emoji: str) -> None:
        self.execute(
            "UPDATE products SET emoji = ?, updated_at = ? WHERE id = ?",
            ((emoji or "📦").strip()[:8], now_iso(), product_id),
        )

    def toggle_product(self, product_id: int) -> None:
        self.execute(
            "UPDATE products SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END, updated_at = ? WHERE id = ?",
            (now_iso(), product_id),
        )

    def delete_product(self, product_id: int) -> None:
        self.execute("DELETE FROM products WHERE id = ?", (product_id,))

    def variants(self, product_id: int | None = None, active_only: bool = False) -> list[Any]:
        clauses = []
        params: list[Any] = []
        if product_id is not None:
            clauses.append("v.product_id = ?")
            params.append(product_id)
        if active_only:
            clauses.append("v.active = 1")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        return self.execute(
            f"""
            SELECT v.*, p.title AS product_title, p.emoji AS product_emoji,
                (SELECT COUNT(*) FROM stock_items s WHERE s.variant_id = v.id AND s.status = 'available') AS stock_count,
                (SELECT COUNT(*) FROM stock_items s WHERE s.variant_id = v.id) AS total_stock_count
            FROM product_variants v
            JOIN products p ON p.id = v.product_id
            {where}
            ORDER BY p.sort_order ASC, v.sort_order ASC, v.id ASC
            """,
            tuple(params),
            all_rows=True,
        )

    def variant(self, variant_id: int) -> Any:
        return self.execute(
            """
            SELECT v.*, p.title AS product_title, p.emoji AS product_emoji,
                (SELECT COUNT(*) FROM stock_items s WHERE s.variant_id = v.id AND s.status = 'available') AS stock_count,
                (SELECT COUNT(*) FROM stock_items s WHERE s.variant_id = v.id) AS total_stock_count
            FROM product_variants v
            JOIN products p ON p.id = v.product_id
            WHERE v.id = ?
            """,
            (variant_id,),
            one=True,
        )

    def add_variant(self, product_id: int, title: str, days: int, price_cents: int) -> int:
        stamp = now_iso()
        if self.is_pg:
            row = self.execute(
                """
                INSERT INTO product_variants(product_id, title, days, price_cents, active, sort_order, created_at, updated_at)
                VALUES(?, ?, ?, ?, 1, 0, ?, ?) RETURNING id
                """,
                (product_id, title.strip(), days, price_cents, stamp, stamp),
                one=True,
            )
            return int(row["id"])
        cur = self.execute(
            """
            INSERT INTO product_variants(product_id, title, days, price_cents, active, sort_order, created_at, updated_at)
            VALUES(?, ?, ?, ?, 1, 0, ?, ?)
            """,
            (product_id, title.strip(), days, price_cents, stamp, stamp),
        )
        return int(cur.lastrowid)

    def toggle_variant(self, variant_id: int) -> None:
        self.execute(
            "UPDATE product_variants SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END, updated_at = ? WHERE id = ?",
            (now_iso(), variant_id),
        )

    def delete_variant(self, variant_id: int) -> None:
        self.execute("DELETE FROM product_variants WHERE id = ?", (variant_id,))

    def add_stock(self, variant_id: int, lines: list[str]) -> int:
        clean = [line.strip() for line in lines if line.strip()]
        stamp = now_iso()
        self.execute_many(
            "INSERT INTO stock_items(variant_id, content, status, created_at) VALUES(?, ?, 'available', ?)",
            [(variant_id, item, stamp) for item in clean],
        )
        return len(clean)

    def stock_items(self, variant_id: int, limit: int = 15) -> list[Any]:
        return self.execute(
            """
            SELECT * FROM stock_items
            WHERE variant_id = ? AND status = 'available'
            ORDER BY id ASC
            LIMIT ?
            """,
            (variant_id, limit),
            all_rows=True,
        )

    def delete_stock_ids(self, variant_id: int, ids: list[int]) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cur = self.execute(
            f"DELETE FROM stock_items WHERE variant_id = ? AND status = 'available' AND id IN ({placeholders})",
            tuple([variant_id] + ids),
        )
        return int(cur.rowcount or 0)

    def effective_price(self, user_id: int, variant_id: int, default_price: int | None = None) -> int:
        row = self.execute(
            "SELECT price_cents FROM user_variant_prices WHERE user_id = ? AND variant_id = ?",
            (user_id, variant_id),
            one=True,
        )
        if row:
            return int(row["price_cents"])
        if default_price is not None:
            return int(default_price)
        variant = self.variant(variant_id)
        return int(variant["price_cents"]) if variant else 0

    def custom_price_map(self, user_id: int, variant_ids: list[int]) -> dict[int, int]:
        if not variant_ids:
            return {}
        placeholders = ",".join("?" for _ in variant_ids)
        rows = self.execute(
            f"""
            SELECT variant_id, price_cents
            FROM user_variant_prices
            WHERE user_id = ? AND variant_id IN ({placeholders})
            """,
            tuple([user_id] + variant_ids),
            all_rows=True,
        )
        return {int(row["variant_id"]): int(row["price_cents"]) for row in rows}

    def set_custom_price(self, user_id: int, variant_id: int, price_cents: int | None) -> None:
        stamp = now_iso()
        if price_cents is None:
            self.execute(
                "DELETE FROM user_variant_prices WHERE user_id = ? AND variant_id = ?",
                (user_id, variant_id),
            )
            return
        self.execute(
            """
            INSERT INTO user_variant_prices(user_id, variant_id, price_cents, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_id, variant_id) DO UPDATE SET
                price_cents = excluded.price_cents,
                updated_at = excluded.updated_at
            """,
            (user_id, variant_id, price_cents, stamp, stamp),
        )

    def purchase(self, user_id: int, variant_id: int) -> dict[str, Any]:
        stamp = now_iso()
        with self.lock:
            self.begin()
            try:
                user = self.conn.execute(self.q("SELECT * FROM users WHERE user_id = ?"), (user_id,)).fetchone()
                variant = self.conn.execute(
                    self.q(
                        """
                        SELECT v.*, p.title AS product_title, p.emoji AS product_emoji, p.active AS product_active
                        FROM product_variants v
                        JOIN products p ON p.id = v.product_id
                        WHERE v.id = ?
                        """
                    ),
                    (variant_id,),
                ).fetchone()
                if not user:
                    self.rollback()
                    return {"ok": False, "reason": "missing_user"}
                if int(user["is_banned"]):
                    self.rollback()
                    return {"ok": False, "reason": "banned"}
                if not variant or not int(variant["active"]) or not int(variant["product_active"]):
                    self.rollback()
                    return {"ok": False, "reason": "missing_variant"}

                custom = self.conn.execute(
                    self.q("SELECT price_cents FROM user_variant_prices WHERE user_id = ? AND variant_id = ?"),
                    (user_id, variant_id),
                ).fetchone()
                price = int(custom["price_cents"]) if custom else int(variant["price_cents"])
                if int(user["balance_cents"]) < price:
                    self.rollback()
                    return {"ok": False, "reason": "low_balance", "price_cents": price}

                stock = self.conn.execute(
                    self.q(
                        """
                        SELECT * FROM stock_items
                        WHERE variant_id = ? AND status = 'available'
                        ORDER BY id ASC
                        LIMIT 1
                        """
                    ),
                    (variant_id,),
                ).fetchone()
                if not stock:
                    self.rollback()
                    return {"ok": False, "reason": "empty_stock"}

                self.conn.execute(
                    self.q("UPDATE users SET balance_cents = balance_cents - ?, updated_at = ? WHERE user_id = ?"),
                    (price, stamp, user_id),
                )
                self.conn.execute(
                    self.q("UPDATE stock_items SET status = 'sold', sold_to = ?, sold_at = ? WHERE id = ?"),
                    (user_id, stamp, stock["id"]),
                )
                if self.is_pg:
                    order = self.conn.execute(
                        self.q(
                            """
                            INSERT INTO orders(user_id, product_id, variant_id, stock_item_id, price_cents, status, delivered_content, created_at)
                            VALUES(?, ?, ?, ?, ?, 'delivered', ?, ?) RETURNING id
                            """
                        ),
                        (
                            user_id,
                            variant["product_id"],
                            variant_id,
                            stock["id"],
                            price,
                            stock["content"],
                            stamp,
                        ),
                    ).fetchone()
                    order_id = int(order["id"])
                else:
                    cur = self.conn.execute(
                        self.q(
                            """
                            INSERT INTO orders(user_id, product_id, variant_id, stock_item_id, price_cents, status, delivered_content, created_at)
                            VALUES(?, ?, ?, ?, ?, 'delivered', ?, ?)
                            """
                        ),
                        (
                            user_id,
                            variant["product_id"],
                            variant_id,
                            stock["id"],
                            price,
                            stock["content"],
                            stamp,
                        ),
                    )
                    order_id = int(cur.lastrowid)
                self.commit()
                return {
                    "ok": True,
                    "order_id": order_id,
                    "variant": variant,
                    "price_cents": price,
                    "content": stock["content"],
                }
            except Exception:
                self.rollback()
                raise

    def create_topup(self, user_id: int, amount_cents: int, method: str, txn_ref: str) -> int:
        stamp = now_iso()
        if self.is_pg:
            row = self.execute(
                """
                INSERT INTO topups(user_id, amount_cents, method, txn_ref, status, created_at, updated_at)
                VALUES(?, ?, ?, ?, 'pending', ?, ?) RETURNING id
                """,
                (user_id, amount_cents, method, txn_ref, stamp, stamp),
                one=True,
            )
            return int(row["id"])
        cur = self.execute(
            """
            INSERT INTO topups(user_id, amount_cents, method, txn_ref, status, created_at, updated_at)
            VALUES(?, ?, ?, ?, 'pending', ?, ?)
            """,
            (user_id, amount_cents, method, txn_ref, stamp, stamp),
        )
        return int(cur.lastrowid)

    def topups(self, pending_only: bool = False, limit: int = 15) -> list[Any]:
        where = "WHERE t.status = 'pending'" if pending_only else ""
        return self.execute(
            f"""
            SELECT t.*, u.username, u.first_name
            FROM topups t
            LEFT JOIN users u ON u.user_id = t.user_id
            {where}
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            (limit,),
            all_rows=True,
        )

    def topup(self, topup_id: int) -> Any:
        return self.execute("SELECT * FROM topups WHERE id = ?", (topup_id,), one=True)

    def update_topup(self, topup_id: int, status: str, note: str = "") -> Any:
        stamp = now_iso()
        with self.lock:
            self.begin()
            try:
                topup = self.conn.execute(self.q("SELECT * FROM topups WHERE id = ?"), (topup_id,)).fetchone()
                if not topup:
                    self.rollback()
                    return None
                if topup["status"] == "pending" and status == "approved":
                    self.conn.execute(
                        self.q("UPDATE users SET balance_cents = balance_cents + ?, updated_at = ? WHERE user_id = ?"),
                        (int(topup["amount_cents"]), stamp, int(topup["user_id"])),
                    )
                self.conn.execute(
                    self.q("UPDATE topups SET status = ?, admin_note = ?, updated_at = ? WHERE id = ?"),
                    (status, note, stamp, topup_id),
                )
                self.commit()
                return self.topup(topup_id)
            except Exception:
                self.rollback()
                raise

    def orders(self, limit: int = 15, user_id: int | None = None) -> list[Any]:
        where = ""
        params: list[Any] = []
        if user_id is not None:
            where = "WHERE o.user_id = ?"
            params.append(user_id)
        params.append(limit)
        return self.execute(
            f"""
            SELECT o.*, p.title AS product_title, p.emoji AS product_emoji, v.title AS variant_title, v.days,
                   u.username, u.first_name
            FROM orders o
            LEFT JOIN products p ON p.id = o.product_id
            LEFT JOIN product_variants v ON v.id = o.variant_id
            LEFT JOIN users u ON u.user_id = o.user_id
            {where}
            ORDER BY o.created_at DESC
            LIMIT ?
            """,
            tuple(params),
            all_rows=True,
        )

    def stats(self) -> dict[str, int]:
        row = self.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS users,
                (SELECT COUNT(*) FROM users WHERE is_banned = 1) AS banned,
                (SELECT COUNT(*) FROM products WHERE active = 1) AS products,
                (SELECT COUNT(*) FROM product_variants WHERE active = 1) AS variants,
                (SELECT COUNT(*) FROM stock_items WHERE status = 'available') AS stock,
                (SELECT COUNT(*) FROM orders) AS orders,
                (SELECT COALESCE(SUM(price_cents), 0) FROM orders WHERE status = 'delivered') AS sales,
                (SELECT COUNT(*) FROM orders WHERE created_at LIKE ?) AS today_orders,
                (SELECT COALESCE(SUM(price_cents), 0) FROM orders WHERE status = 'delivered' AND created_at LIKE ?) AS today_sales,
                (SELECT COUNT(*) FROM topups WHERE status = 'pending') AS topups
            """,
            (f"{datetime.now(timezone.utc).date().isoformat()}%", f"{datetime.now(timezone.utc).date().isoformat()}%"),
            one=True,
        )
        return {key: int(row[key]) for key in row.keys()}


class TelegramAPI:
    def __init__(self, token: str):
        self.token = token.strip()
        self.base = f"https://api.telegram.org/bot{self.token}/" if self.token else ""
        self.opener = urllib.request.build_opener()

    def request(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 35) -> dict[str, Any]:
        if not self.token:
            return {"ok": False, "description": "BOT_TOKEN is missing"}
        data = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.base + method,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {"ok": False, "description": exc.read().decode("utf-8", "replace")}
        except Exception as exc:
            return {"ok": False, "description": str(exc)}

    def get_updates(self, offset: int, timeout: int = 25) -> dict[str, Any]:
        return self.request(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout + 10,
        )

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last: dict[str, Any] = {"ok": True}
        parts = chunk_text(text)
        for index, part in enumerate(parts):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": part,
                "disable_web_page_preview": True,
            }
            if reply_markup and index == len(parts) - 1:
                payload["reply_markup"] = reply_markup
            last = self.request("sendMessage", payload)
        return last

    def edit_message(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MAX_TEXT],
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageText", payload)

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> dict[str, Any]:
        return self.request(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:180], "show_alert": alert},
        )

    def copy_message(self, chat_id: int, from_chat_id: int, message_id: int) -> dict[str, Any]:
        return self.request(
            "copyMessage",
            {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id},
        )

    def get_chat_member(self, chat_id: str, user_id: int) -> dict[str, Any]:
        return self.request("getChatMember", {"chat_id": chat_id, "user_id": user_id})


class BotApp:
    def __init__(self, store: Store, api: TelegramAPI, admins: set[int]):
        self.store = store
        self.api = api
        self.admins = admins
        self.stop_event = threading.Event()
        self.join_cache_seconds = int(os.getenv("JOIN_CACHE_SECONDS", "300"))
        self.join_success_cache: dict[int, float] = {}
        self.broadcast_delay = float(os.getenv("BROADCAST_DELAY_SECONDS", "0.035"))

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def currency(self) -> str:
        return self.store.setting("currency", "USD")

    def referral_reward_cents(self) -> int:
        if self.store.setting("referral_enabled", "1") != "1":
            return 0
        return parse_cents(self.store.setting("referral_reward", "0.01"), 1)

    def start_referrer(self, text: str) -> int | None:
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or parts[0].lower() != "/start":
            return None
        payload = parts[1].strip()
        return int(payload) if payload.isdigit() else None

    def bot_username(self) -> str:
        username = self.store.setting("bot_username", "").strip().lstrip("@")
        if username:
            return username
        username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
        if username:
            return username
        response = self.api.request("getMe", {}, timeout=10)
        if response.get("ok"):
            username = str(response.get("result", {}).get("username") or "").strip()
            if username:
                self.store.set_setting("bot_username", username)
                return username
        return ""

    def page(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        message_id: int | None = None,
    ) -> None:
        if message_id:
            response = self.api.edit_message(chat_id, message_id, text, reply_markup)
            if response.get("ok"):
                return
        self.api.send_message(chat_id, text, reply_markup)

    def contact_admin_keyboard(self) -> dict[str, Any]:
        owner_url = self.store.setting("owner_url", "").strip()
        rows = []
        if owner_url:
            rows.append([{"text": "💬 Contact Admin", "url": owner_url}])
        rows.append([{"text": "⬅️ Back", "callback_data": "u:home"}])
        return {"inline_keyboard": rows}

    def run(self) -> None:
        if not self.api.token:
            raise RuntimeError("BOT_TOKEN is required")
        print("Telegram selling bot started.")
        offset = 0
        while not self.stop_event.is_set():
            response = self.api.get_updates(offset)
            if not response.get("ok"):
                print("Polling error:", response.get("description"))
                time.sleep(5)
                continue
            for update in response.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                try:
                    self.handle_update(update)
                except Exception:
                    traceback.print_exc()

    def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self.handle_message(update["message"])
        elif "callback_query" in update:
            self.handle_callback(update["callback_query"])

    def handle_message(self, msg: dict[str, Any]) -> None:
        if "from" not in msg or "chat" not in msg:
            return
        tg_user = msg["from"]
        user_id = int(tg_user["id"])
        chat_id = int(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        user = self.store.upsert_user(
            tg_user,
            referrer_id=self.start_referrer(text),
            referral_reward_cents=self.referral_reward_cents(),
        )

        if text == "/cancel" and self.is_admin(user_id):
            self.store.clear_state(user_id)
            self.api.send_message(chat_id, "Canceled.", self.admin_keyboard())
            return

        if self.is_admin(user_id):
            state = self.store.get_state(user_id)
            if state and not text.startswith("/"):
                self.handle_admin_state(msg, state[0], state[1])
                return

        if self.is_admin(user_id):
            self.store.clear_state(user_id)
            self.api.send_message(chat_id, "👑 Admin account detected. Only the admin panel is enabled for this account.", self.admin_reply_keyboard())
            self.show_admin_home(chat_id)
            return

        if text in {"/admin", "admin"}:
            self.api.send_message(chat_id, "Admin panel is not available for this account.")
            return

        if int(user["is_banned"]):
            self.api.send_message(chat_id, self.store.setting("banned_text"))
            return

        command = text.split(maxsplit=1)[0].lower() if text else ""
        if command not in {"/start", "/menu"} and not self.join_ok(user_id):
            self.show_join_gate(chat_id)
            return
        button = text.lower()
        if command in {"/start", "/menu"}:
            self.show_user_home(chat_id, user)
        elif command == "/shop" or "buy key" in button:
            self.show_products(chat_id, user_id)
        elif command == "/balance":
            self.show_balance(chat_id, user_id)
        elif command == "/invite" or "invite friends" in button:
            self.show_invite_friends(chat_id, user_id)
        elif command == "/profile" or "profile" in button:
            self.show_profile(chat_id, user_id)
        elif command == "/info" or "info bot" in button:
            self.show_info_bot(chat_id)
        elif "redeem" in button:
            self.api.send_message(chat_id, self.store.setting("redeem_text"), self.reply_keyboard())
        elif command == "/topup":
            self.show_topup(chat_id)
        elif command == "/orders":
            self.show_user_orders(chat_id, user_id)
        elif command == "/help":
            self.api.send_message(chat_id, self.store.setting("help_text"), self.user_keyboard())
        elif command == "/contact":
            self.api.send_message(chat_id, self.store.setting("contact_text"), self.user_keyboard())
        elif command == "/pay":
            self.handle_pay(chat_id, user_id, text)
        else:
            if not self.join_ok(user_id):
                self.show_join_gate(chat_id)
            else:
                self.api.send_message(chat_id, "Choose an option.", self.user_keyboard())

    def handle_callback(self, query: dict[str, Any]) -> None:
        user_id = int(query["from"]["id"])
        chat_id = int(query["message"]["chat"]["id"])
        message_id = int(query["message"]["message_id"])
        data = query.get("data") or ""
        user = self.store.upsert_user(query["from"])
        self.api.answer_callback(query["id"])

        if data.startswith("adm:") or data.startswith("ap:") or data.startswith("av:") or data.startswith("au:") or data.startswith("at:"):
            if not self.is_admin(user_id):
                self.api.answer_callback(query["id"], "Admin only.", alert=True)
                return
            self.handle_admin_callback(chat_id, message_id, user_id, data)
            return

        if self.is_admin(user_id):
            self.api.answer_callback(query["id"], "Admin account uses admin panel only.", alert=True)
            self.show_admin_home(chat_id)
            return

        if int(user["is_banned"]):
            self.api.send_message(chat_id, self.store.setting("banned_text"))
            return

        if data == "verify":
            if self.join_ok(user_id):
                self.api.edit_message(chat_id, message_id, self.store.setting("welcome_text"), self.user_keyboard())
            else:
                self.api.answer_callback(query["id"], self.store.setting("verify_failed_text"), alert=True)
            return

        if not self.join_ok(user_id):
            self.show_join_gate(chat_id)
            return

        if data == "u:home":
            self.show_user_home(chat_id, user, message_id=message_id)
        elif data == "u:products":
            self.show_products(chat_id, user_id, message_id=message_id)
        elif data.startswith("u:p:"):
            self.show_product_variants(chat_id, user_id, int(data.rsplit(":", 1)[1]), message_id=message_id)
        elif data.startswith("u:v:"):
            self.show_variant(chat_id, user_id, int(data.rsplit(":", 1)[1]), message_id=message_id)
        elif data.startswith("u:buy:"):
            self.buy(chat_id, user_id, int(data.rsplit(":", 1)[1]), message_id=message_id)
        elif data == "u:invite":
            self.show_invite_friends(chat_id, user_id, message_id=message_id)
        elif data == "u:invite_copy":
            self.send_referral_copy(chat_id, user_id)
        elif data == "u:profile":
            self.show_profile(chat_id, user_id, message_id=message_id)
        elif data == "u:info":
            self.show_info_bot(chat_id, message_id=message_id)
        elif data == "u:redeem":
            self.api.send_message(chat_id, self.store.setting("redeem_text"), self.user_keyboard())
        elif data == "u:balance":
            self.show_balance(chat_id, user_id, message_id=message_id)
        elif data == "u:topup":
            self.show_topup(chat_id)
        elif data == "u:orders":
            self.show_user_orders(chat_id, user_id, message_id=message_id)
        elif data == "u:help":
            self.api.send_message(chat_id, self.store.setting("help_text"), self.user_keyboard())
        elif data == "u:contact":
            self.api.send_message(chat_id, self.store.setting("contact_text"), self.user_keyboard())

    def user_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "🛒 Buy Key", "callback_data": "u:products"}, {"text": "👥 Invite Friends", "callback_data": "u:invite"}],
                [{"text": "💳 Profile", "callback_data": "u:profile"}, {"text": "ℹ️ Info Bot", "callback_data": "u:info"}],
                [{"text": "🎁 Redeem", "callback_data": "u:redeem"}],
            ]
        }

    def reply_keyboard(self) -> dict[str, Any]:
        return {
            "keyboard": [
                [{"text": "🛒 Buy Key"}, {"text": "👥 Invite Friends"}],
                [{"text": "💳 Profile"}, {"text": "ℹ️ Info Bot"}],
                [{"text": "🎁 Redeem"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    def join_keyboard(self) -> dict[str, Any]:
        rows = []
        for i, channel in enumerate(self.store.channels(enabled_only=True), start=1):
            rows.append([{"text": f"JOIN CHANNEL {i}", "url": channel["link"]}])
        rows.append([{"text": "VERIFY", "callback_data": "verify"}])
        return {"inline_keyboard": rows}

    def join_ok(self, user_id: int) -> bool:
        if self.store.setting("join_required", "1") != "1":
            return True
        cached_until = self.join_success_cache.get(user_id, 0)
        if cached_until > time.monotonic():
            return True
        channels = self.store.channels(enabled_only=True)
        verifiable = [channel for channel in channels if (channel["chat_id"] or "").strip()]
        if not verifiable:
            return True
        valid = {"creator", "administrator", "member"}
        for channel in verifiable:
            response = self.api.get_chat_member(str(channel["chat_id"]).strip(), user_id)
            if not response.get("ok"):
                return False
            if response.get("result", {}).get("status") not in valid:
                return False
        self.join_success_cache[user_id] = time.monotonic() + self.join_cache_seconds
        return True

    def show_join_gate(self, chat_id: int) -> None:
        self.api.send_message(chat_id, self.store.setting("join_text"), self.join_keyboard())

    def show_user_home(self, chat_id: int, user: Any, message_id: int | None = None) -> None:
        if not self.join_ok(int(user["user_id"])):
            self.show_join_gate(chat_id)
            return
        text = (
            f"🎉 {self.store.setting('welcome_text')}\n\n"
            f"💰 Wallet: {money(int(user['balance_cents']), self.currency())}\n"
            f"👥 Invites: {self.store.referral_count(int(user['user_id']))}\n\n"
            "👇 Choose an option from the menu below."
        )
        if message_id:
            self.page(chat_id, text, self.user_keyboard(), message_id)
        else:
            self.api.send_message(chat_id, text, self.reply_keyboard())

    def referral_link(self, user_id: int) -> str:
        username = self.bot_username()
        if not username:
            return ""
        return f"https://t.me/{username}?start={user_id}"

    def show_invite_friends(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        reward = money(self.referral_reward_cents(), self.currency())
        active = "Active" if self.store.setting("referral_enabled", "1") == "1" else "Paused"
        link = self.referral_link(user_id)
        if not link:
            self.api.send_message(
                chat_id,
                "Referral link is not configured yet. Admin must set Bot Username from /admin > Settings.",
                self.user_keyboard(),
            )
            return
        referral_count = self.store.referral_count(user_id)
        text = (
            "🚀 GROW YOUR BALANCE\n\n"
            "Share your link and earn money for every friend who joins.\n\n"
            f"💸 You get: {reward} per join\n"
            f"✅ Status: Payouts {active}\n"
            f"👥 Friends invited: {referral_count}\n\n"
            "📍 Your unique link:\n"
            f"{link}\n\n"
            "📋 Tap COPY LINK to receive the link alone, then tap and hold it to copy.\n\n"
            "📣 Start sharing now and get your keys for free."
        )
        share_url = "https://t.me/share/url?" + urllib.parse.urlencode(
            {
                "url": link,
                "text": f"Join {self.store.setting('bot_name', 'our bot')} and start earning rewards.",
            }
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "📋 COPY LINK", "callback_data": "u:invite_copy"}],
                [{"text": "📤 QUICK SHARE", "url": share_url}],
                [{"text": "⬅️ Back", "callback_data": "u:home"}],
            ]
        }
        self.page(chat_id, text, keyboard, message_id)

    def send_referral_copy(self, chat_id: int, user_id: int) -> None:
        link = self.referral_link(user_id)
        if not link:
            self.api.send_message(chat_id, "Referral link is not configured yet.", self.user_keyboard())
            return
        self.api.send_message(chat_id, f"📋 Tap and hold to copy:\n\n{link}", self.user_keyboard())

    def show_profile(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        user = self.store.user(user_id)
        orders = self.store.orders(limit=5, user_id=user_id)
        username = f"@{user['username']}" if user and user["username"] else "-"
        lines = [
            "💳 PROFILE",
            "",
            f"👤 Name: {user['first_name'] if user else '-'} {user['last_name'] if user and user['last_name'] else ''}".strip(),
            f"🆔 User ID: {user_id}",
            f"👤 Username: {username}",
            f"📅 Member Since: {user['created_at'][:10] if user else '-'}",
            "",
            f"💰 Wallet: {money(int(user['balance_cents']) if user else 0, self.currency())}",
            f"👥 Invites: {self.store.referral_count(user_id)}",
            f"🛒 Orders: {len(orders)} latest shown",
        ]
        for order in orders:
            lines.append(
                f"#{order['id']} {order['product_emoji'] or '📦'} {order['product_title']} / {order['variant_title']} - {money(order['price_cents'], self.currency())}"
            )
        self.page(chat_id, "\n".join(lines), self.user_keyboard(), message_id)

    def show_info_bot(self, chat_id: int, message_id: int | None = None) -> None:
        owner_url = self.store.setting("owner_url", "").strip()
        channel_url = self.store.setting("channel_url", "").strip()
        rows = []
        buttons = []
        if channel_url:
            buttons.append({"text": "💬 CHANNEL", "url": channel_url})
        if owner_url:
            buttons.append({"text": "👨‍💻 OWNER", "url": owner_url})
        if buttons:
            rows.append(buttons)
        rows.append([{"text": "👥 Invite Friends", "callback_data": "u:invite"}])
        rows.append([{"text": "⬅️ Back", "callback_data": "u:home"}])
        self.page(chat_id, f"ℹ️ INFO BOT\n\n{self.store.setting('info_text')}", {"inline_keyboard": rows}, message_id)

    def show_products(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        products = self.store.products(active_only=True)
        if not products:
            self.page(chat_id, "🛒 BUY KEY\n\nNo products are available right now.", self.user_keyboard(), message_id)
            return
        rows = [[{"text": f"{p['emoji'] or '📦'} {p['title']} ({p['variant_count']} variants)", "callback_data": f"u:p:{p['id']}"}] for p in products]
        rows.append([{"text": "⬅️ Back", "callback_data": "u:home"}])
        self.page(chat_id, "🛒 BUY KEY\n\nSelect a product:", {"inline_keyboard": rows}, message_id)

    def show_product_variants(self, chat_id: int, user_id: int, product_id: int, message_id: int | None = None) -> None:
        product = self.store.product(product_id)
        variants = self.store.variants(product_id, active_only=True)
        if not product:
            self.page(chat_id, "Product not found.", self.user_keyboard(), message_id)
            return
        if not variants:
            self.page(chat_id, "No variants are available for this product.", self.user_keyboard(), message_id)
            return
        rows = []
        custom_prices = self.store.custom_price_map(user_id, [int(v["id"]) for v in variants])
        for v in variants:
            price = custom_prices.get(int(v["id"]), int(v["price_cents"]))
            rows.append(
                [
                    {
                        "text": f"⏳ {v['title']} - {money(price, self.currency())} - 🔑 {v['stock_count']}",
                        "callback_data": f"u:v:{v['id']}",
                    }
                ]
            )
        rows.append([{"text": "⬅️ Back to Products", "callback_data": "u:products"}])
        text = f"{product['emoji'] or '📦'} {product['title']}\n\n{product['description'] or ''}\n\n⏳ Choose duration/variant:"
        self.page(chat_id, text, {"inline_keyboard": rows}, message_id)

    def show_variant(self, chat_id: int, user_id: int, variant_id: int, message_id: int | None = None) -> None:
        variant = self.store.variant(variant_id)
        if not variant or not int(variant["active"]):
            self.page(chat_id, "Variant not found.", self.user_keyboard(), message_id)
            return
        price = self.store.effective_price(user_id, variant_id, int(variant["price_cents"]))
        text = (
            f"{variant['product_emoji'] or '📦'} Product: {variant['product_title']}\n"
            f"⏳ Variant: {variant['title']}\n"
            f"📅 Days: {variant['days']}\n"
            f"💰 Price: {money(price, self.currency())}\n"
            f"🔑 Stock: {variant['stock_count']}"
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "🛒 Buy Now", "callback_data": f"u:buy:{variant_id}"}],
                [{"text": "⬅️ Back", "callback_data": f"u:p:{variant['product_id']}"}],
            ]
        }
        self.page(chat_id, text, keyboard, message_id)

    def buy(self, chat_id: int, user_id: int, variant_id: int, message_id: int | None = None) -> None:
        result = self.store.purchase(user_id, variant_id)
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "low_balance":
                text = (
                    "💰 INSUFFICIENT BALANCE\n\n"
                    f"{self.store.setting('low_balance_text')}\n\n"
                    "Please contact admin to add balance."
                )
                self.page(chat_id, text, self.contact_admin_keyboard(), message_id)
            elif reason == "empty_stock":
                self.page(chat_id, self.store.setting("empty_stock_text"), self.user_keyboard(), message_id)
            else:
                self.page(chat_id, "Purchase failed. Contact support.", self.contact_admin_keyboard(), message_id)
            return
        variant = result["variant"]
        text = (
            f"✅ {self.store.setting('order_success_text')}\n\n"
            f"🧾 Order ID: {result['order_id']}\n"
            f"{variant['product_emoji'] or '📦'} Product: {variant['product_title']}\n"
            f"⏳ Variant: {variant['title']} ({variant['days']} days)\n"
            f"💰 Price: {money(result['price_cents'], self.currency())}\n\n"
            f"{result['content']}"
        )
        self.page(chat_id, text, self.user_keyboard(), message_id)
        self.api.send_message(chat_id, f"📋 Tap and hold to copy your key:\n\n{result['content']}")
        self.notify_admins(
            "🛒 New order\n"
            f"🧾 Order ID: {result['order_id']}\n"
            f"👤 User ID: {user_id}\n"
            f"{variant['product_emoji'] or '📦'} Product: {variant['product_title']}\n"
            f"⏳ Variant: {variant['title']} ({variant['days']} days)\n"
            f"💰 Price: {money(result['price_cents'], self.currency())}"
        )

    def show_balance(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        user = self.store.user(user_id)
        balance = int(user["balance_cents"]) if user else 0
        self.page(chat_id, f"💰 WALLET\n\nYour balance: {money(balance, self.currency())}", self.user_keyboard(), message_id)

    def show_topup(self, chat_id: int) -> None:
        self.api.send_message(chat_id, self.store.setting("payment_methods"), self.user_keyboard())

    def handle_pay(self, chat_id: int, user_id: int, text: str) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            self.api.send_message(chat_id, "Use: /pay amount transaction_id")
            return
        amount = parse_cents(parts[1], 0)
        if amount <= 0:
            self.api.send_message(chat_id, "Amount must be greater than zero.")
            return
        topup_id = self.store.create_topup(user_id, amount, "manual", parts[2])
        self.api.send_message(chat_id, f"Top-up request #{topup_id} submitted for {money(amount, self.currency())}.")
        self.notify_admins(
            "New top-up request\n"
            f"ID: {topup_id}\n"
            f"User ID: {user_id}\n"
            f"Amount: {money(amount, self.currency())}\n"
            f"Reference: {parts[2]}"
        )

    def show_user_orders(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        orders = self.store.orders(user_id=user_id)
        if not orders:
            self.page(chat_id, "🛒 ORDERS\n\nNo orders yet.", self.user_keyboard(), message_id)
            return
        lines = ["Your latest orders:"]
        for o in orders:
            lines.append(
                f"#{o['id']} - {o['product_title']} / {o['variant_title']} - {money(o['price_cents'], self.currency())} - {o['created_at'][:10]}"
            )
        self.page(chat_id, "\n".join(lines), self.user_keyboard(), message_id)

    def admin_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "📦 Products", "callback_data": "adm:products"}, {"text": "➕ Add Product", "callback_data": "adm:add_product"}],
                [{"text": "👥 Users", "callback_data": "adm:users:0"}, {"text": "💰 Balance", "callback_data": "adm:balances:0"}],
                [{"text": "📣 Broadcast", "callback_data": "adm:broadcast"}, {"text": "💬 Direct Message", "callback_data": "adm:dm"}],
                [{"text": "💭 Channels", "callback_data": "adm:channels"}, {"text": "⚙️ Settings", "callback_data": "adm:settings"}],
                [{"text": "🛒 Orders", "callback_data": "adm:orders"}, {"text": "⏳ Top-ups", "callback_data": "adm:topups"}],
            ]
        }

    def admin_reply_keyboard(self) -> dict[str, Any]:
        return {
            "keyboard": [[{"text": "👑 Admin Panel"}]],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    def show_admin_home(self, chat_id: int, message_id: int | None = None) -> None:
        s = self.store.stats()
        text = (
            "👑 Master Admin Panel\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "📊 QUICK STATS\n"
            f"┣ 👥 Users: {s['users']}\n"
            f"┣ 📦 Products: {s['products']}\n"
            f"┣ 🧩 Variants: {s['variants']}\n"
            f"┣ 🔑 Keys: {s['stock']}\n"
            f"┣ 🛒 Orders: {s['orders']}\n"
            f"┗ 💰 Revenue: {money(s['sales'], self.currency())}\n\n"
            "📅 TODAY\n"
            f"┣ 🛒 Orders: {s['today_orders']}\n"
            f"┗ 💵 Sales: {money(s['today_sales'], self.currency())}\n\n"
            f"⏳ Pending Top-ups: {s['topups']}\n"
            f"🚫 Banned Users: {s['banned']}\n\n"
            "🎲 Select a menu below:"
        )
        self.page(chat_id, text, self.admin_keyboard(), message_id)

    def handle_admin_callback(self, chat_id: int, message_id: int, admin_id: int, data: str) -> None:
        if data == "adm:home":
            self.store.clear_state(admin_id)
            self.show_admin_home(chat_id, message_id=message_id)
        elif data == "adm:products":
            self.admin_products(chat_id, message_id=message_id)
        elif data == "adm:add_product":
            self.store.set_state(admin_id, "add_product")
            self.api.send_message(chat_id, "Send product info:\nProduct name\nDescription optional\n\nDefault emoji is 📦. You can set emoji from product detail after creating it.\n\n/cancel to stop.")
        elif data.startswith("ap:"):
            self.handle_product_admin(chat_id, admin_id, data, message_id)
        elif data.startswith("av:"):
            self.handle_variant_admin(chat_id, admin_id, data)
        elif data.startswith("adm:users:"):
            self.admin_users(chat_id, int(data.rsplit(":", 1)[1]), balance_only=False, message_id=message_id)
        elif data.startswith("adm:balances:"):
            self.admin_users(chat_id, int(data.rsplit(":", 1)[1]), balance_only=True, message_id=message_id)
        elif data.startswith("au:"):
            self.handle_user_admin(chat_id, admin_id, data)
        elif data == "adm:broadcast":
            self.store.set_state(admin_id, "broadcast")
            self.api.send_message(chat_id, "Send the message/media to broadcast to all users.\n/cancel to stop.")
        elif data == "adm:dm":
            self.store.set_state(admin_id, "dm_target")
            self.api.send_message(chat_id, "Send target user ID.\n/cancel to stop.")
        elif data == "adm:channels":
            self.admin_channels(chat_id)
        elif data == "adm:add_channel":
            self.store.set_state(admin_id, "add_channel")
            self.api.send_message(chat_id, "Send channel info:\nTitle\nJoin link\nChat ID or @username\n\n/cancel to stop.")
        elif data.startswith("adm:del_channel:"):
            self.store.delete_channel(int(data.rsplit(":", 1)[1]))
            self.admin_channels(chat_id)
        elif data.startswith("adm:toggle_channel:"):
            self.store.toggle_channel(int(data.rsplit(":", 1)[1]))
            self.admin_channels(chat_id)
        elif data == "adm:settings":
            self.admin_settings(chat_id, message_id=message_id)
        elif data.startswith("adm:set:"):
            key = data.split(":", 2)[2]
            self.store.set_state(admin_id, "set_setting", {"key": key})
            self.api.send_message(chat_id, f"Send new value for {key}.\n/cancel to stop.")
        elif data == "adm:toggle_join":
            current = self.store.setting("join_required", "1")
            self.store.set_setting("join_required", "0" if current == "1" else "1")
            self.admin_settings(chat_id, message_id=message_id)
        elif data == "adm:toggle_referral":
            current = self.store.setting("referral_enabled", "1")
            self.store.set_setting("referral_enabled", "0" if current == "1" else "1")
            self.admin_settings(chat_id, message_id=message_id)
        elif data == "adm:orders":
            self.admin_orders(chat_id)
        elif data == "adm:topups":
            self.admin_topups(chat_id)
        elif data.startswith("at:"):
            self.handle_topup_admin(chat_id, data)

    def admin_products(self, chat_id: int, message_id: int | None = None) -> None:
        rows = []
        text = ["Products:"]
        for p in self.store.products(active_only=False):
            status = "active" if int(p["active"]) else "hidden"
            text.append(f"#{p['id']} {p['emoji'] or '📦'} {p['title']} - {p['variant_count']} variants - {status}")
            rows.append([{"text": f"#{p['id']} {p['emoji'] or '📦'} {p['title']}", "callback_data": f"ap:view:{p['id']}"}])
        rows.append([{"text": "Add Product", "callback_data": "adm:add_product"}, {"text": "Back", "callback_data": "adm:home"}])
        self.page(chat_id, "\n".join(text) if len(text) > 1 else "No products yet.", {"inline_keyboard": rows}, message_id)

    def handle_product_admin(self, chat_id: int, admin_id: int, data: str, message_id: int | None = None) -> None:
        parts = data.split(":")
        action = parts[1]
        product_id = int(parts[2])
        if action == "view":
            self.admin_product_detail(chat_id, product_id, message_id=message_id)
        elif action == "addvar":
            self.store.set_state(admin_id, "add_variant", {"product_id": product_id})
            self.api.send_message(chat_id, "Send variant info:\nVariant title\nDays\nPrice\n\nExample:\n7 Day\n7\n12.50")
        elif action == "emoji":
            self.store.set_state(admin_id, "set_product_emoji", {"product_id": product_id})
            self.api.send_message(chat_id, "Send one emoji for this product.\nExample: 💎")
        elif action == "toggle":
            self.store.toggle_product(product_id)
            self.admin_product_detail(chat_id, product_id, message_id=message_id)
        elif action == "delete":
            self.store.delete_product(product_id)
            self.admin_products(chat_id, message_id=message_id)

    def admin_product_detail(self, chat_id: int, product_id: int, message_id: int | None = None) -> None:
        product = self.store.product(product_id)
        if not product:
            self.api.send_message(chat_id, "Product not found.", self.admin_keyboard())
            return
        variants = self.store.variants(product_id, active_only=False)
        lines = [
            f"Product #{product['id']}: {product['emoji'] or '📦'} {product['title']}",
            f"Status: {'active' if int(product['active']) else 'hidden'}",
            f"Description: {product['description'] or '-'}",
            "",
            "Variants:",
        ]
        rows = []
        for v in variants:
            lines.append(
                f"#{v['id']} {v['title']} ({v['days']} days) - {money(v['price_cents'], self.currency())} - stock {v['stock_count']}/{v['total_stock_count']}"
            )
            rows.append([{"text": f"Variant #{v['id']} {v['title']}", "callback_data": f"av:view:{v['id']}"}])
        rows.extend(
            [
                [{"text": "➕ Add Variant", "callback_data": f"ap:addvar:{product_id}"}, {"text": "🎨 Set Emoji", "callback_data": f"ap:emoji:{product_id}"}],
                [{"text": "Toggle Active", "callback_data": f"ap:toggle:{product_id}"}, {"text": "Delete Product", "callback_data": f"ap:delete:{product_id}"}],
                [{"text": "Back", "callback_data": "adm:products"}],
            ]
        )
        self.page(chat_id, "\n".join(lines), {"inline_keyboard": rows}, message_id)

    def handle_variant_admin(self, chat_id: int, admin_id: int, data: str) -> None:
        parts = data.split(":")
        action = parts[1]
        variant_id = int(parts[2])
        if action == "view":
            self.admin_variant_detail(chat_id, variant_id)
        elif action == "stockadd":
            self.store.set_state(admin_id, "add_stock", {"variant_id": variant_id})
            self.api.send_message(chat_id, "Send stock lines. One stock item per line.\n/cancel to stop.")
        elif action == "stocklist":
            self.admin_stock_list(chat_id, variant_id)
        elif action == "stockdel":
            self.store.set_state(admin_id, "delete_stock", {"variant_id": variant_id})
            self.api.send_message(chat_id, "Send stock IDs to delete, comma separated.\nExample: 12,13,14")
        elif action == "toggle":
            self.store.toggle_variant(variant_id)
            self.admin_variant_detail(chat_id, variant_id)
        elif action == "delete":
            variant = self.store.variant(variant_id)
            product_id = int(variant["product_id"]) if variant else 0
            self.store.delete_variant(variant_id)
            if product_id:
                self.admin_product_detail(chat_id, product_id)
            else:
                self.admin_products(chat_id)

    def admin_variant_detail(self, chat_id: int, variant_id: int) -> None:
        v = self.store.variant(variant_id)
        if not v:
            self.api.send_message(chat_id, "Variant not found.", self.admin_keyboard())
            return
        text = (
            f"Variant #{v['id']}\n"
            f"Product: {v['product_title']}\n"
            f"Title: {v['title']}\n"
            f"Days: {v['days']}\n"
            f"Price: {money(v['price_cents'], self.currency())}\n"
            f"Status: {'active' if int(v['active']) else 'hidden'}\n"
            f"Stock: {v['stock_count']} available / {v['total_stock_count']} total"
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "Add Stock", "callback_data": f"av:stockadd:{variant_id}"}, {"text": "List Stock", "callback_data": f"av:stocklist:{variant_id}"}],
                [{"text": "Delete Stock", "callback_data": f"av:stockdel:{variant_id}"}, {"text": "Toggle Active", "callback_data": f"av:toggle:{variant_id}"}],
                [{"text": "Delete Variant", "callback_data": f"av:delete:{variant_id}"}],
                [{"text": "Back to Product", "callback_data": f"ap:view:{v['product_id']}"}],
            ]
        }
        self.api.send_message(chat_id, text, keyboard)

    def admin_stock_list(self, chat_id: int, variant_id: int) -> None:
        items = self.store.stock_items(variant_id, limit=20)
        if not items:
            self.api.send_message(chat_id, "No available stock for this variant.")
            return
        lines = ["Available stock, first 20:"]
        for item in items:
            content = str(item["content"])
            if len(content) > 120:
                content = content[:117] + "..."
            lines.append(f"#{item['id']}: {content}")
        self.api.send_message(chat_id, "\n".join(lines))

    def admin_users(self, chat_id: int, page: int, balance_only: bool, message_id: int | None = None) -> None:
        limit = 10
        offset = page * limit
        users = self.store.users(limit=limit, offset=offset, balance_only=balance_only)
        total = self.store.user_count(balance_only=balance_only)
        title = "💰 USERS WITH BALANCE" if balance_only else "👥 USERS"
        lines = [f"{title}: {total}", "━━━━━━━━━━━━━━━━━━━━", ""]
        rows = []
        for user in users:
            username = f"@{user['username']}" if user["username"] else "-"
            status = "🚫 banned" if int(user["is_banned"]) else "✅ active"
            lines.append(
                f"🆔 {user['user_id']}\n"
                f"👤 {username} | {user['first_name'] or '-'}\n"
                f"💰 {money(user['balance_cents'], self.currency())} | {status}\n"
            )
            rows.append([{"text": f"👤 {user['user_id']} {username}", "callback_data": f"au:view:{user['user_id']}"}])
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ Prev", "callback_data": f"adm:{'balances' if balance_only else 'users'}:{page - 1}"})
        if offset + limit < total:
            nav.append({"text": "Next ➡️", "callback_data": f"adm:{'balances' if balance_only else 'users'}:{page + 1}"})
        if nav:
            rows.append(nav)
        rows.append([{"text": "⬅️ Back", "callback_data": "adm:home"}])
        self.page(chat_id, "\n".join(lines), {"inline_keyboard": rows}, message_id)

    def handle_user_admin(self, chat_id: int, admin_id: int, data: str) -> None:
        parts = data.split(":")
        action = parts[1]
        target_id = int(parts[2])
        if action == "view":
            self.admin_user_detail(chat_id, target_id)
        elif action in {"addbal", "deduct"}:
            self.store.set_state(admin_id, action, {"user_id": target_id})
            self.api.send_message(chat_id, "Send amount. Example: 10.00\n/cancel to stop.")
        elif action == "ban":
            self.store.set_ban(target_id, True, f"admin:{admin_id}")
            self.api.send_message(target_id, self.store.setting("banned_text"))
            self.admin_user_detail(chat_id, target_id)
        elif action == "unban":
            self.store.set_ban(target_id, False, f"admin:{admin_id}")
            self.api.send_message(target_id, "Your account has been unbanned.")
            self.admin_user_detail(chat_id, target_id)
        elif action == "dm":
            self.store.set_state(admin_id, "dm_message", {"user_id": target_id})
            self.api.send_message(chat_id, "Send the message/media for this user.\n/cancel to stop.")
        elif action == "orders":
            self.admin_orders(chat_id, user_id=target_id)
        elif action == "customprice":
            self.store.set_state(admin_id, "custom_price", {"user_id": target_id})
            self.api.send_message(chat_id, "Send custom price:\nvariant_id price\n\nExample:\n5 9.99\nUse price 'default' to remove custom price.")

    def admin_user_detail(self, chat_id: int, user_id: int) -> None:
        user = self.store.user(user_id)
        if not user:
            self.api.send_message(chat_id, "User not found.", self.admin_keyboard())
            return
        username = f"@{user['username']}" if user["username"] else "-"
        text = (
            "👤 USER DETAILS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 User ID: {user['user_id']}\n"
            f"🔗 Username: {username}\n"
            f"👤 Name: {user['first_name']} {user['last_name'] or ''}\n"
            f"💰 Balance: {money(user['balance_cents'], self.currency())}\n"
            f"📌 Status: {'🚫 banned' if int(user['is_banned']) else '✅ active'}\n"
            f"📅 Joined Bot: {user['created_at'][:19]}"
        )
        ban_button = {"text": "🔓 Unban", "callback_data": f"au:unban:{user_id}"} if int(user["is_banned"]) else {"text": "🔒 Ban", "callback_data": f"au:ban:{user_id}"}
        keyboard = {
            "inline_keyboard": [
                [{"text": "➕ Add Balance", "callback_data": f"au:addbal:{user_id}"}, {"text": "➖ Deduct", "callback_data": f"au:deduct:{user_id}"}],
                [{"text": "💬 Direct Message", "callback_data": f"au:dm:{user_id}"}, ban_button],
                [{"text": "💎 Custom Price", "callback_data": f"au:customprice:{user_id}"}, {"text": "🛒 Orders", "callback_data": f"au:orders:{user_id}"}],
                [{"text": "⬅️ Back to Users", "callback_data": "adm:users:0"}],
            ]
        }
        self.api.send_message(chat_id, text, keyboard)

    def admin_channels(self, chat_id: int) -> None:
        channels = self.store.channels(enabled_only=False)
        lines = ["Join channels:"]
        rows = []
        for ch in channels:
            lines.append(f"#{ch['id']} {ch['title']} - {'enabled' if int(ch['enabled']) else 'disabled'}\n{ch['link']}\nchat: {ch['chat_id'] or '-'}")
            rows.append(
                [
                    {"text": f"Toggle #{ch['id']}", "callback_data": f"adm:toggle_channel:{ch['id']}"},
                    {"text": f"Delete #{ch['id']}", "callback_data": f"adm:del_channel:{ch['id']}"},
                ]
            )
        rows.append([{"text": "Add Channel", "callback_data": "adm:add_channel"}, {"text": "Back", "callback_data": "adm:home"}])
        self.api.send_message(chat_id, "\n\n".join(lines), {"inline_keyboard": rows})

    def admin_settings(self, chat_id: int, message_id: int | None = None) -> None:
        s = self.store.settings_map()
        text = (
            "⚙️ SETTINGS\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🤖 Bot name: {s.get('bot_name')}\n"
            f"🔗 Bot username: @{s.get('bot_username') or 'not_set'}\n"
            f"💵 Currency: {s.get('currency')}\n"
            f"✅ Join required: {s.get('join_required')}\n\n"
            f"👥 Referral: {s.get('referral_enabled')}\n"
            f"💸 Referral reward: {money(parse_cents(s.get('referral_reward', '0.01')), self.currency())}\n"
            f"👨‍💻 Owner URL: {s.get('owner_url')}\n"
            f"💬 Channel URL: {s.get('channel_url')}\n\n"
            "👇 Tap a setting to update."
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "✅ Toggle Join", "callback_data": "adm:toggle_join"}, {"text": "👥 Toggle Referral", "callback_data": "adm:toggle_referral"}],
                [{"text": "🤖 Bot Name", "callback_data": "adm:set:bot_name"}, {"text": "💵 Currency", "callback_data": "adm:set:currency"}],
                [{"text": "🔗 Bot Username", "callback_data": "adm:set:bot_username"}, {"text": "💸 Referral Reward", "callback_data": "adm:set:referral_reward"}],
                [{"text": "👨‍💻 Owner URL", "callback_data": "adm:set:owner_url"}, {"text": "💬 Channel URL", "callback_data": "adm:set:channel_url"}],
                [{"text": "🎉 Welcome", "callback_data": "adm:set:welcome_text"}, {"text": "🚪 Join Text", "callback_data": "adm:set:join_text"}],
                [{"text": "🆘 Help", "callback_data": "adm:set:help_text"}, {"text": "☎️ Contact", "callback_data": "adm:set:contact_text"}],
                [{"text": "ℹ️ Info Text", "callback_data": "adm:set:info_text"}, {"text": "🎁 Redeem Text", "callback_data": "adm:set:redeem_text"}],
                [{"text": "💳 Payment", "callback_data": "adm:set:payment_methods"}],
                [{"text": "⬅️ Back", "callback_data": "adm:home"}],
            ]
        }
        self.page(chat_id, text, keyboard, message_id)

    def admin_orders(self, chat_id: int, user_id: int | None = None) -> None:
        orders = self.store.orders(limit=20, user_id=user_id)
        if not orders:
            self.api.send_message(chat_id, "No orders found.", self.admin_keyboard())
            return
        lines = ["Orders:"]
        for o in orders:
            lines.append(
                f"#{o['id']} user {o['user_id']} @{o['username'] or '-'}\n"
                f"{o['product_title']} / {o['variant_title']} ({o['days']} days)\n"
                f"{money(o['price_cents'], self.currency())} - {o['created_at'][:19]}"
            )
        self.api.send_message(chat_id, "\n\n".join(lines), self.admin_keyboard())

    def admin_topups(self, chat_id: int) -> None:
        topups = self.store.topups(pending_only=False, limit=20)
        if not topups:
            self.api.send_message(chat_id, "No top-ups found.", self.admin_keyboard())
            return
        lines = ["Top-ups:"]
        rows = []
        for t in topups:
            lines.append(
                f"#{t['id']} user {t['user_id']} @{t['username'] or '-'}\n"
                f"{money(t['amount_cents'], self.currency())} - {t['status']}\n"
                f"Ref: {t['txn_ref']}"
            )
            if t["status"] == "pending":
                rows.append(
                    [
                        {"text": f"Approve #{t['id']}", "callback_data": f"at:approve:{t['id']}"},
                        {"text": f"Reject #{t['id']}", "callback_data": f"at:reject:{t['id']}"},
                    ]
                )
        rows.append([{"text": "Back", "callback_data": "adm:home"}])
        self.api.send_message(chat_id, "\n\n".join(lines), {"inline_keyboard": rows})

    def handle_topup_admin(self, chat_id: int, data: str) -> None:
        _, action, raw_id = data.split(":")
        status = "approved" if action == "approve" else "rejected"
        topup = self.store.update_topup(int(raw_id), status)
        if not topup:
            self.api.send_message(chat_id, "Top-up not found.", self.admin_keyboard())
            return
        self.api.send_message(chat_id, f"Top-up #{topup['id']} {status}.", self.admin_keyboard())
        self.api.send_message(
            int(topup["user_id"]),
            f"Top-up #{topup['id']} {status}: {money(topup['amount_cents'], self.currency())}",
        )

    def handle_admin_state(self, msg: dict[str, Any], state: str, data: dict[str, Any]) -> None:
        admin_id = int(msg["from"]["id"])
        chat_id = int(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()

        try:
            if state == "add_product":
                lines = [line.strip() for line in text.splitlines()]
                if not lines or not lines[0]:
                    self.api.send_message(chat_id, "Product name is required.")
                    return
                product_id = self.store.add_product(lines[0], "\n".join(lines[1:]))
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, f"Product #{product_id} added.", self.admin_keyboard())

            elif state == "set_product_emoji":
                emoji = text.strip().split()[0] if text.strip() else "📦"
                self.store.set_product_emoji(int(data["product_id"]), emoji)
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, "Product emoji updated.", self.admin_keyboard())

            elif state == "add_variant":
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if len(lines) < 3:
                    self.api.send_message(chat_id, "Send:\nVariant title\nDays\nPrice")
                    return
                variant_id = self.store.add_variant(
                    int(data["product_id"]),
                    lines[0],
                    int(lines[1]),
                    parse_cents(lines[2]),
                )
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, f"Variant #{variant_id} added.", self.admin_keyboard())

            elif state == "add_stock":
                count = self.store.add_stock(int(data["variant_id"]), text.splitlines())
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, f"{count} stock items added.", self.admin_keyboard())

            elif state == "delete_stock":
                ids = [int(x.strip()) for x in text.replace("\n", ",").split(",") if x.strip().isdigit()]
                count = self.store.delete_stock_ids(int(data["variant_id"]), ids)
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, f"{count} stock items deleted.", self.admin_keyboard())

            elif state == "add_channel":
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if len(lines) < 3:
                    self.api.send_message(chat_id, "Send:\nTitle\nJoin link\nChat ID or @username")
                    return
                self.store.add_channel(lines[0], lines[1], lines[2])
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, "Channel added.", self.admin_keyboard())

            elif state == "set_setting":
                self.store.set_setting(str(data["key"]), text)
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, "Setting updated.", self.admin_keyboard())

            elif state == "dm_target":
                if not text.lstrip("-").isdigit():
                    self.api.send_message(chat_id, "Send a numeric user ID.")
                    return
                self.store.set_state(admin_id, "dm_message", {"user_id": int(text)})
                self.api.send_message(chat_id, "Now send the message/media for this user.")

            elif state == "dm_message":
                target_id = int(data["user_id"])
                result = self.api.copy_message(target_id, chat_id, int(msg["message_id"]))
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, "Message sent." if result.get("ok") else f"Failed: {result.get('description')}", self.admin_keyboard())

            elif state == "broadcast":
                sent = 0
                failed = 0
                for user_id in self.store.all_user_ids():
                    result = self.api.copy_message(user_id, chat_id, int(msg["message_id"]))
                    if result.get("ok"):
                        sent += 1
                    else:
                        failed += 1
                    if self.broadcast_delay > 0:
                        time.sleep(self.broadcast_delay)
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, f"Broadcast finished.\nSent: {sent}\nFailed: {failed}", self.admin_keyboard())

            elif state in {"addbal", "deduct"}:
                amount = parse_cents(text, 0)
                if amount <= 0:
                    self.api.send_message(chat_id, "Amount must be greater than zero.")
                    return
                if state == "deduct":
                    amount = -amount
                user_id = int(data["user_id"])
                self.store.adjust_balance(user_id, amount, f"admin:{admin_id}")
                user = self.store.user(user_id)
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, "Balance updated.", self.admin_keyboard())
                self.api.send_message(user_id, f"Balance updated. Current balance: {money(user['balance_cents'], self.currency())}")

            elif state == "custom_price":
                parts = text.split()
                if len(parts) != 2 or not parts[0].isdigit():
                    self.api.send_message(chat_id, "Send: variant_id price\nExample: 5 9.99")
                    return
                variant_id = int(parts[0])
                price = None if parts[1].lower() == "default" else parse_cents(parts[1], -1)
                if price == -1:
                    self.api.send_message(chat_id, "Invalid price.")
                    return
                self.store.set_custom_price(int(data["user_id"]), variant_id, price)
                self.store.clear_state(admin_id)
                self.api.send_message(chat_id, "Custom price updated.", self.admin_keyboard())

        except Exception as exc:
            traceback.print_exc()
            self.api.send_message(chat_id, f"Error: {exc}\nUse /cancel and try again.")

    def notify_admins(self, text: str) -> None:
        for admin_id in self.admins:
            self.api.send_message(admin_id, text)


def smoke_test() -> None:
    db = ROOT / "data" / "smoke_test.sqlite3"
    if db.exists():
        db.unlink()
    store = Store(db_path=db)
    store.upsert_user({"id": 101, "username": "demo", "first_name": "Demo"})
    store.upsert_user(
        {"id": 102, "username": "friend", "first_name": "Friend"},
        referrer_id=101,
        referral_reward_cents=parse_cents("0.01"),
    )
    assert store.referral_count(101) == 1
    assert int(store.user(101)["balance_cents"]) == 1
    pid = store.add_product("Fluorite Product", "Demo product")
    vid = store.add_variant(pid, "7 Day", 7, parse_cents("12.50"))
    store.add_stock(vid, ["KEY-001"])
    store.adjust_balance(101, parse_cents("20"), "smoke")
    result = store.purchase(101, vid)
    assert result["ok"], result
    store.adjust_balance(101, parse_cents("20"), "smoke")
    assert store.purchase(101, vid)["reason"] == "empty_stock"
    topup = store.create_topup(101, parse_cents("5"), "manual", "TXN")
    store.update_topup(topup, "approved")
    print("smoke-ok")


def main() -> int:
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Telegram-only selling bot with Telegram admin panel.")
    parser.add_argument("--init-db", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()

    if args.smoke_test:
        smoke_test()
        return 0

    store = Store(
        db_path=Path(os.getenv("DB_PATH", str(DEFAULT_DB))),
        database_url=os.getenv("DATABASE_URL", ""),
    )
    if args.init_db:
        print("Database initialized.")
        return 0

    token = os.getenv("BOT_TOKEN", "")
    admin_ids = parse_admin_ids(os.getenv("ADMIN_IDS", os.getenv("ADMIN_CHAT_IDS", "")))
    if not admin_ids:
        print("Warning: ADMIN_IDS is empty. No one can open /admin.")
    app = BotApp(store, TelegramAPI(token), admin_ids)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
