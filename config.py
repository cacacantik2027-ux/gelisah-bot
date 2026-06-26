import os
from dotenv import load_dotenv

# Memuat file .env jika dijalankan di komputer lokal (local development)
# Di Railway, library ini akan otomatis dilewati karena Railway menggunakan sistem environment langsung.
load_dotenv()

# Token Telegram Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Username Bot Livechat (tanpa karakter '@')
LIVECHAT_BOT = os.getenv("LIVECHAT_BOT", "gelisahlivechat_bot")

# ADMIN_IDS diisi di Railway sebagai string pisah koma (Contoh: "12345,67890")
admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [
    int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()
]

# ID Grup Log Telegram (wajib bertipe data integer negatif)
try:
    LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))
except ValueError:
    LOG_GROUP_ID = 0

# Link Gambar QRIS
QRIS_IMAGE_URL = os.getenv("QRIS_IMAGE_URL", "https://placeholder.co/qris.jpg")

# Informasi Rekening Transfer Bank / E-Wallet
# Karakter '\n' di Railway variable otomatis terbaca sebagai baris baru
BANK_INFO = os.getenv(
    "BANK_INFO", 
    "BCA: 1234567890 a.n GELISAH\nDANA: 081234567890"
).replace("\\n", "\n")