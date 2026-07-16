import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ID Telegram admin, pisahkan dengan koma jika lebih dari satu
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
]

# Ke mana sesi live chat (chat langsung dengan user) dikirim/diteruskan.
# Isi salah satu:
# - LIVECHAT_GROUP_ID: ID grup live chat admin (angka, boleh negatif untuk grup).
#   Semua pesan dari/ke user akan diteruskan (relay) dari & ke grup ini.
# - kalau dikosongkan, pesan diteruskan ke semua ADMIN_IDS satu per satu (private message)
# LIVECHAT_GROUP_ID menggantikan BOOKING_NOTIFY_CHAT_ID lama; nama env var lama
# tetap dibaca sebagai fallback supaya deployment lama tidak perlu diubah confignya.
LIVECHAT_GROUP_ID = os.getenv("LIVECHAT_GROUP_ID", os.getenv("BOOKING_NOTIFY_CHAT_ID", ""))

BOT_NAME = os.getenv("BOT_NAME", "Talent Booking Bot")

# Link chat developer, dipakai untuk tombol logo Telegram mengambang di Mini App.
DEVELOPER_CHAT_URL = os.getenv("DEVELOPER_CHAT_URL", "https://t.me/gosahsoknal")

# Mini App (opsional). Kalau WEBAPP_URL kosong, bot tetap jalan normal tanpa tombol Mini App.
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
# Port untuk api_server.py (server katalog untuk Mini App). Railway isi otomatis lewat PORT.
PORT = int(os.getenv("PORT", "8080"))

DEFAULT_GREETING = (
    "Selamat datang di {bot_name}!\n\n"
    "Saat ini tersedia {total_talent} talent untuk kamu pilih.\n"
    "Silakan pilih talent di bawah untuk melihat profil, portofolio, dan pricelist."
)

DEFAULT_HOW_TO_ORDER = (
    "Cara order:\n"
    "1. Pilih talent yang diinginkan\n"
    "2. Lihat profil, portofolio, dan pricelist\n"
    "3. Tekan tombol \"Chat Sekarang\" untuk langsung terhubung dengan admin\n"
    "4. Ketik kebutuhan Anda, admin akan membalas langsung di chat ini\n"
    "5. Admin akan mengakhiri sesi live chat setelah topik selesai dibahas"
)

# Lokasi file database, persisten kalau di-deploy dengan volume (mis. Railway)
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DATA_DIR, "bot.db")
