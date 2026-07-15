"""
database.py
============
Layer database SQLite untuk bot TALENT GELISAH.

Tabel:
- settings          : teks sapaan, teks "cara order", dll (key-value).
- talents           : daftar talent (nama, deskripsi, foto, pricelist).
- live_chat_sessions: status live chat tiap user (aktif/tidak) supaya bot
                       tahu kapan harus meneruskan pesan user ke grup admin.
- relay_messages    : "buku alamat" pesan yang diteruskan bot ke grup admin
                       -> menyimpan message_id pesan bot di grup, dipetakan
                       ke user_id aslinya. Saat admin me-reply pesan itu di
                       grup, bot cari message_id-nya di sini untuk tahu balas
                       ke user mana. Dipakai baik untuk live chat maupun
                       untuk pertanyaan "tanyakan ready" per talent.
"""

import sqlite3
import datetime
import threading

import config

_local = threading.local()


def _connect():
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_conn():
    if not hasattr(_local, "conn"):
        _local.conn = _connect()
    return _local.conn


def init_db():
    conn = get_conn()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS talents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                photo_file_id TEXT NOT NULL DEFAULT '',
                pricelist_text TEXT NOT NULL DEFAULT '',
                order_index INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_chat_sessions (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS relay_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'livechat',
                talent_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_relay_group_msg ON relay_messages(group_message_id)"
        )


# ── settings ────────────────────────────────────────────────────────────
def get_setting(key: str, default: str = "") -> str:
    row = get_conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ── talents ─────────────────────────────────────────────────────────────
def add_talent(name: str, description: str, photo_file_id: str, pricelist_text: str) -> int:
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO talents (name, description, photo_file_id, pricelist_text, order_index, created_at) "
            "VALUES (?, ?, ?, ?, (SELECT COALESCE(MAX(order_index), 0) + 1 FROM talents), ?)",
            (name, description, photo_file_id, pricelist_text, datetime.datetime.now().isoformat()),
        )
        return cur.lastrowid


def edit_talent(talent_id: int, name: str = None, description: str = None,
                 photo_file_id: str = None, pricelist_text: str = None):
    fields, values = [], []
    for col, val in (("name", name), ("description", description),
                      ("photo_file_id", photo_file_id), ("pricelist_text", pricelist_text)):
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(val)
    if not fields:
        return
    values.append(talent_id)
    conn = get_conn()
    with conn:
        conn.execute(f"UPDATE talents SET {', '.join(fields)} WHERE id = ?", values)


def delete_talent(talent_id: int):
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM talents WHERE id = ?", (talent_id,))


def list_talents(active_only: bool = True):
    conn = get_conn()
    if active_only:
        rows = conn.execute("SELECT * FROM talents WHERE active = 1 ORDER BY order_index, id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM talents ORDER BY order_index, id").fetchall()
    return [dict(r) for r in rows]


def get_talent(talent_id: int):
    row = get_conn().execute("SELECT * FROM talents WHERE id = ?", (talent_id,)).fetchone()
    return dict(row) if row else None


# ── live chat sessions ─────────────────────────────────────────────────
def set_live_chat(user_id: int, user_name: str, active: bool):
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT INTO live_chat_sessions (user_id, user_name, active, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET user_name = excluded.user_name, "
            "active = excluded.active, updated_at = excluded.updated_at",
            (user_id, user_name, 1 if active else 0, datetime.datetime.now().isoformat()),
        )


def is_live_chat_active(user_id: int) -> bool:
    row = get_conn().execute(
        "SELECT active FROM live_chat_sessions WHERE user_id = ?", (user_id,)
    ).fetchone()
    return bool(row and row["active"])


# ── relay messages (jembatan bot <-> grup admin) ─────────────────────────
def add_relay(group_message_id: int, user_id: int, user_name: str, kind: str, talent_name: str = ""):
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT INTO relay_messages (group_message_id, user_id, user_name, kind, talent_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_message_id, user_id, user_name, kind, talent_name, datetime.datetime.now().isoformat()),
        )


def get_relay(group_message_id: int):
    row = get_conn().execute(
        "SELECT * FROM relay_messages WHERE group_message_id = ? ORDER BY id DESC LIMIT 1",
        (group_message_id,),
    ).fetchone()
    return dict(row) if row else None
