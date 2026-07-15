"""
config.py
=========
Konfigurasi bot TALENT GELISAH (booking talent/influencer/model untuk
konten/endorsement/event). Isi nilai-nilai di bawah lewat file .env
(lihat .env.example) sebelum menjalankan bot.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "ISI_TOKEN_BOT_TELEGRAM_KAMU")
BOT_NAME = os.getenv("BOT_NAME", "TALENT GELISAH")

# ID Telegram admin (bisa lebih dari satu, pisahkan dengan koma di .env).
# Admin adalah orang yang boleh mengelola daftar talent (/addtalent,
# /talents, /edittalent, /deltalent, /setgreeting, /sethowtoorder).
# Cara cek ID Telegram: chat ke @userinfobot
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

# ── Mini App "Pilih Talent" (katalog visual, halaman HTML asli) ────────────
# Isi URL halaman statis yang sudah kamu hosting (mis. GitHub Pages, format
# https://<username>.github.io/<repo>/index.html). Kalau dikosongkan, bot
# otomatis FALLBACK ke daftar talent versi tombol chat biasa (perilaku
# lama) -- jadi bot tetap jalan normal walau Mini App ini belum disetup.
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

# Port HTTP internal untuk endpoint publik GET /api/talents & GET /photo/<id>
# (dipakai halaman Mini App di atas buat ambil data talent & foto lewat
# fetch() biasa). Railway otomatis meng-inject env var PORT begitu
# Networking > Public Domain diaktifkan di dashboard service ini.
PORT = int(os.getenv("PORT", "8080"))

# ── Live Chat ────────────────────────────────────────────────────────────
# ID grup Telegram tempat admin menjawab live chat & pertanyaan "ready atau
# tidak". Bot HARUS sudah jadi member di grup ini. Cara cek Chat ID grup:
# forward salah satu pesan dari grup itu ke @userinfobot, atau pakai
# /groupid (lihat catatan di bawah kalau mau tambahkan command debug sendiri).
LIVECHAT_GROUP_ID = int(os.getenv("LIVECHAT_GROUP_ID", "0"))

# Kalau True: hanya pesan dari ADMIN_IDS di dalam LIVECHAT_GROUP_ID yang
# diteruskan balik ke user. Kalau False: siapa pun yang membalas (reply) ke
# pesan bot di grup itu akan diteruskan (cocok kalau grupnya memang privat
# khusus admin/CS saja, tidak perlu re-check identitas satu-satu).
LIVECHAT_ADMIN_ONLY_REPLY = os.getenv("LIVECHAT_ADMIN_ONLY_REPLY", "false").lower() == "true"

# Username Telegram admin/kontak yang ditampilkan sebagai fallback (mis. di
# pesan error atau kalau LIVECHAT_GROUP_ID belum di-setup). Isi TANPA @.
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "admin")

# ── Penyimpanan data ─────────────────────────────────────────────────────
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR", "data")
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "bot.db"))

os.makedirs(DATA_DIR, exist_ok=True)
