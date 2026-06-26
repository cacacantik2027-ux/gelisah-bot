"""
Konfigurasi Bot GELISAH
"""
import os
from dotenv import load_dotenv

# Load .env file jika ada (lokal), Railway pakai env vars langsung
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
LIVECHAT_BOT = os.getenv("LIVECHAT_BOT", "livechatgs_bot")

_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in _raw.split(",") if x.strip().isdigit()]

# Validasi saat startup
if not BOT_TOKEN or BOT_TOKEN == "MASUKKAN_TOKEN_BOT_DISINI":
    raise ValueError(
        "❌ BOT_TOKEN belum diisi!\n"
        "Set environment variable BOT_TOKEN di Railway:\n"
        "  Settings → Variables → New Variable\n"
        "  Name: BOT_TOKEN  |  Value: token_dari_botfather"
    )
