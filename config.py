import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ID Telegram admin, pisahkan dengan koma jika lebih dari satu
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
]

# Ke mana notifikasi booking dikirim.
# Isi salah satu:
# - BOOKING_NOTIFY_CHAT_ID: ID chat/grup admin (angka, boleh negatif untuk grup)
# - kalau dikosongkan, notifikasi dikirim ke semua ADMIN_IDS satu per satu (private message)
BOOKING_NOTIFY_CHAT_ID = os.getenv("BOOKING_NOTIFY_CHAT_ID", "")

BOT_NAME = os.getenv("BOT_NAME", "Talent Booking Bot")

DEFAULT_GREETING = (
    "Selamat datang di {bot_name}!\n\n"
    "Silakan pilih talent di bawah untuk melihat profil, portofolio, dan pricelist."
)

DEFAULT_HOW_TO_ORDER = (
    "Cara order:\n"
    "1. Pilih talent yang diinginkan\n"
    "2. Lihat profil, portofolio, dan pricelist\n"
    "3. Tekan tombol \"Ajukan Booking\" dan isi form singkat\n"
    "4. Admin akan menghubungi Anda untuk konfirmasi jadwal dan pembayaran"
)

# Lokasi file database, persisten kalau di-deploy dengan volume (mis. Railway)
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DATA_DIR, "bot.db")
