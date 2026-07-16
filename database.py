import sqlite3
from contextlib import contextmanager

from config import DB_PATH


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS talents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                portfolio_url TEXT,
                pricelist TEXT NOT NULL,
                photo_file_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                talent_id INTEGER,
                talent_name TEXT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                needs TEXT,
                date_needed TEXT,
                budget TEXT,
                status TEXT DEFAULT 'baru',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        _migrate_schema(conn)


def _migrate_schema(conn):
    """Tambahkan kolom baru ke tabel yang sudah ada di database lama (Railway),
    supaya deploy baru tidak crash gara-gara skema database ketinggalan versi kode."""
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(talents)")}
    if "portfolio_url" not in existing_cols:
        conn.execute("ALTER TABLE talents ADD COLUMN portfolio_url TEXT")
    conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------- Talents ----------

def add_talent(name, description, pricelist, portfolio_url=None, photo_file_id=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO talents (name, description, portfolio_url, pricelist, photo_file_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, description, portfolio_url, pricelist, photo_file_id),
        )
        conn.commit()
        return cur.lastrowid


def get_talent(talent_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM talents WHERE id = ?", (talent_id,)).fetchone()
        return dict(row) if row else None


def list_talents():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM talents ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def delete_talent(talent_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM talents WHERE id = ?", (talent_id,))
        conn.commit()


# ---------- Settings ----------

def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


# ---------- Bookings ----------

def add_booking(talent_id, talent_name, user_id, username, full_name, needs, date_needed, budget):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bookings (talent_id, talent_name, user_id, username, full_name, needs, date_needed, budget) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (talent_id, talent_name, user_id, username, full_name, needs, date_needed, budget),
        )
        conn.commit()
        return cur.lastrowid


def list_bookings(limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
