import logging
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

logger = logging.getLogger(__name__)


def init_db():
    logger.info("init_db() dipanggil, DB_PATH=%s", DB_PATH)
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
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                talent_id INTEGER,
                talent_name TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                ended_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_relay (
                group_message_id INTEGER NOT NULL,
                admin_chat_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                PRIMARY KEY (group_message_id, admin_chat_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sponsors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_file_id TEXT NOT NULL,
                name TEXT,
                description TEXT,
                marquee_desc TEXT,
                url TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bgm_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                title TEXT NOT NULL,
                mime_type TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                full_name TEXT,
                jabatan TEXT,
                photo_file_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        _migrate_schema(conn)


def _migrate_schema(conn):
    """Tambahkan kolom baru ke tabel yang sudah ada di database lama (Railway),
    supaya deploy baru tidak crash gara-gara skema database ketinggalan versi kode."""
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(talents)")}
    logger.info("Kolom talents saat ini: %s", sorted(existing_cols))
    if "portfolio_url" not in existing_cols:
        logger.info("Kolom portfolio_url belum ada, menjalankan ALTER TABLE...")
        conn.execute("ALTER TABLE talents ADD COLUMN portfolio_url TEXT")
        logger.info("ALTER TABLE selesai, kolom portfolio_url ditambahkan.")
    else:
        logger.info("Kolom portfolio_url sudah ada, migrasi dilewati.")

    sponsor_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sponsors)")}
    if "description" not in sponsor_cols:
        logger.info("Kolom sponsors.description belum ada, menjalankan ALTER TABLE...")
        conn.execute("ALTER TABLE sponsors ADD COLUMN description TEXT")
        logger.info("ALTER TABLE selesai, kolom sponsors.description ditambahkan.")
    else:
        logger.info("Kolom sponsors.description sudah ada, migrasi dilewati.")
    if "marquee_desc" not in sponsor_cols:
        logger.info("Kolom sponsors.marquee_desc belum ada, menjalankan ALTER TABLE...")
        conn.execute("ALTER TABLE sponsors ADD COLUMN marquee_desc TEXT")
        logger.info("ALTER TABLE selesai, kolom sponsors.marquee_desc ditambahkan.")
    else:
        logger.info("Kolom sponsors.marquee_desc sudah ada, migrasi dilewati.")
    conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL (Write-Ahead Logging) mengizinkan banyak pembaca berjalan bersamaan
    # dengan satu penulis, alih-alih saling mengunci seperti mode default
    # ("rollback journal"). Ditambah busy_timeout supaya kalau memang ada
    # tabrakan singkat, koneksi menunggu (maks 30 detik) alih-alih langsung
    # gagal dengan error "database is locked". Ini penting supaya bot tetap
    # jalan lancar saat dipakai 10+ pengguna live chat secara bersamaan.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()


# ---------- Talents ----------

def add_talent(name, description, pricelist, portfolio_url=None, photo_file_id=None):
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO talents (name, description, portfolio_url, pricelist, photo_file_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, description, portfolio_url, pricelist, photo_file_id),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.OperationalError as e:
            # Database lama belum ke-migrate (kolom belum ada). Coba migrasi
            # sekali lagi di sini (bukan cuma saat startup) lalu ulangi insert-nya,
            # supaya proses tambah talent tidak gagal total gara-gara ini.
            logger.error("INSERT talents gagal (%s), mencoba migrasi ulang lalu retry...", e)
            _migrate_schema(conn)
            cur = conn.execute(
                "INSERT INTO talents (name, description, portfolio_url, pricelist, photo_file_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, description, portfolio_url, pricelist, photo_file_id),
            )
            conn.commit()
            logger.info("Retry insert talents berhasil setelah migrasi ulang.")
            return cur.lastrowid


def get_talent(talent_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM talents WHERE id = ?", (talent_id,)).fetchone()
        return dict(row) if row else None


def list_talents():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM talents ORDER BY name COLLATE NOCASE ASC").fetchall()
        return [dict(r) for r in rows]


def delete_talent(talent_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM talents WHERE id = ?", (talent_id,))
        conn.commit()


# Kolom talents yang boleh diedit satu-per-satu lewat menu "Edit Talent".
_TALENT_EDITABLE_FIELDS = {"name", "description", "pricelist", "portfolio_url", "photo_file_id"}


def update_talent_field(talent_id, field, value):
    """Ubah SATU kolom milik talent tertentu. `field` divalidasi terhadap
    whitelist supaya tidak bisa dipakai untuk menyuntik nama kolom sembarangan."""
    if field not in _TALENT_EDITABLE_FIELDS:
        raise ValueError(f"Field talent tidak dikenali: {field}")
    with get_conn() as conn:
        conn.execute(f"UPDATE talents SET {field} = ? WHERE id = ?", (value, talent_id))
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


def delete_setting(key):
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()


# ---------- Sponsors ----------

def add_sponsor(photo_file_id, name=None, description=None, marquee_desc=None, url=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sponsors (photo_file_id, name, description, marquee_desc, url) VALUES (?, ?, ?, ?, ?)",
            (photo_file_id, name, description, marquee_desc, url),
        )
        conn.commit()
        return cur.lastrowid


def list_sponsors():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM sponsors ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]


def get_sponsor(sponsor_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sponsors WHERE id = ?", (sponsor_id,)).fetchone()
        return dict(row) if row else None


def delete_sponsor(sponsor_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM sponsors WHERE id = ?", (sponsor_id,))
        conn.commit()


def add_bgm_track(file_id, title, mime_type=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bgm_tracks (file_id, title, mime_type) VALUES (?, ?, ?)",
            (file_id, title, mime_type),
        )
        conn.commit()
        return cur.lastrowid


def list_bgm_tracks():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM bgm_tracks ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]


def get_bgm_track(track_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bgm_tracks WHERE id = ?", (track_id,)).fetchone()
        return dict(row) if row else None


def delete_bgm_track(track_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM bgm_tracks WHERE id = ?", (track_id,))
        conn.commit()


# Kolom sponsors yang boleh diedit satu-per-satu lewat menu "Edit Sponsor".
_SPONSOR_EDITABLE_FIELDS = {"name", "description", "marquee_desc", "url", "photo_file_id"}


def update_sponsor_field(sponsor_id, field, value):
    """Ubah SATU kolom milik sponsor tertentu. `field` divalidasi terhadap
    whitelist supaya tidak bisa dipakai untuk menyuntik nama kolom sembarangan."""
    if field not in _SPONSOR_EDITABLE_FIELDS:
        raise ValueError(f"Field sponsor tidak dikenali: {field}")
    with get_conn() as conn:
        conn.execute(f"UPDATE sponsors SET {field} = ? WHERE id = ?", (value, sponsor_id))
        conn.commit()


# ---------- Live Chat Sessions ----------

def create_chat_session(user_id, username, full_name, talent_id, talent_name):
    """Buat sesi live chat baru berstatus 'active' untuk seorang user."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_sessions (user_id, username, full_name, talent_id, talent_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, full_name, talent_id, talent_name),
        )
        conn.commit()
        return cur.lastrowid


def get_active_session_for_user(user_id):
    """Ambil sesi live chat yang masih aktif milik user tertentu (kalau ada)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE user_id = ? AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def get_session(session_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None


def end_session(session_id):
    """Tandai sesi live chat sebagai selesai. Dipanggil saat admin menekan
    tombol 'Akhiri Sesi'."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE chat_sessions SET status = 'ended', ended_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()


def reset_session(session_id):
    """Reset sesi live chat yang MACET/STUCK (mis. admin ke-refresh/pindah
    device, sesi kelupaan diakhiri, atau relay-nya rusak) -- beda dari
    end_session() biasa: status diberi label 'reset' (bukan 'ended') supaya
    kelihatan di histori kalau ini reset paksa oleh admin, DAN seluruh
    pemetaan relay (chat_relay) milik sesi ini ikut dihapus supaya tidak ada
    balasan admin yang salah nyasar ke sesi lama. Setelah direset, user bisa
    langsung menekan "Chat Sekarang" lagi untuk membuat sesi baru dari nol,
    karena get_active_session_for_user() hanya mencari status = 'active'."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE chat_sessions SET status = 'reset', ended_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.execute("DELETE FROM chat_relay WHERE session_id = ?", (session_id,))
        conn.commit()


def list_active_sessions(limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_sessions WHERE status = 'active' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- Live Chat Relay Mapping ----------
# Setiap pesan yang diteruskan (di-copy) ke chat admin (grup live chat atau
# private message admin) dicatat di sini, supaya saat admin me-reply pesan
# tersebut, bot tahu balasan itu harus diteruskan ke sesi/user yang mana.

def add_relay_mapping(group_message_id, admin_chat_id, session_id):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_relay (group_message_id, admin_chat_id, session_id) "
            "VALUES (?, ?, ?)",
            (group_message_id, admin_chat_id, session_id),
        )
        conn.commit()


def get_session_id_by_relay(group_message_id, admin_chat_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_id FROM chat_relay WHERE group_message_id = ? AND admin_chat_id = ?",
            (group_message_id, admin_chat_id),
        ).fetchone()
        return row["session_id"] if row else None


# ---------- Kartu Admin Grup (ditampilkan di Mini App) ----------
# Diisi admin lewat DM ke bot (input manual: username + id + jabatan).
# photo_file_id diambil otomatis dari foto profil Telegram user tsb kalau
# bisa didapat -- lihat resolve & simpan di bot.py.

def add_group_admin(user_id, username, full_name, jabatan, photo_file_id):
    """Tambah admin grup baru, atau perbarui datanya kalau user_id sudah
    pernah didaftarkan sebelumnya (supaya admin bisa input ulang buat update
    tanpa perlu hapus dulu)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO group_admins (user_id, username, full_name, jabatan, photo_file_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, full_name=excluded.full_name, "
            "jabatan=excluded.jabatan, photo_file_id=excluded.photo_file_id",
            (user_id, username, full_name, jabatan, photo_file_id),
        )
        conn.commit()


def list_group_admins():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM group_admins ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_group_admin(admin_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM group_admins WHERE id = ?", (admin_id,)).fetchone()
        return dict(row) if row else None


def delete_group_admin(admin_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM group_admins WHERE id = ?", (admin_id,))
        conn.commit()


def count_relay_for_session(session_id):
    """Hitung berapa pesan yang sudah pernah diteruskan untuk sesi ini.
    Dipakai untuk mendeteksi apakah pesan yang masuk adalah pesan PERTAMA
    dalam sesi (0 = pertama) supaya formatnya beda dengan pesan berikutnya."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM chat_relay WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row["c"] if row else 0
