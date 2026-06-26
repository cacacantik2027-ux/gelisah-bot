"""
Manajemen data talent — Railway-safe (path absolut)
"""
import json
import os

# Path absolut supaya Railway tidak kebingungan cwd
_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(_DIR, "talents.json")

DEFAULT_TALENTS = [
    {
        "name": "Talent 1",
        "photo": "",
        "description": "Talent cantik, ramah, dan profesional. Siap menemani VCS kamu kapan saja. Pelayanan eksklusif dan memuaskan dijamin!",
        "pricelist": "💎 *Pricelist Talent 1:*\n\n• VCS 15 menit  → Rp 25.000\n• VCS 30 menit  → Rp 45.000\n• VCS 60 menit  → Rp 80.000\n\n✅ Pembayaran: OVO / GoPay / Dana / BCA\n📌 Booking minimal 1 jam sebelumnya."
    },
    {
        "name": "Talent 2",
        "photo": "",
        "description": "Talent berpengalaman, suara merdu, dan ekspresi liar. Siap memberikan pengalaman tak terlupakan!",
        "pricelist": "💎 *Pricelist Talent 2:*\n\n• VCS 15 menit  → Rp 30.000\n• VCS 30 menit  → Rp 55.000\n• VCS 60 menit  → Rp 95.000\n\n✅ Pembayaran: OVO / GoPay / Dana / BCA\n📌 Slot terbatas, booking sekarang!"
    }
]


def load_talents() -> list:
    if not os.path.exists(DATA_FILE):
        save_talents(DEFAULT_TALENTS)
        return list(DEFAULT_TALENTS)
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return list(DEFAULT_TALENTS)


def save_talents(talents: list) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(talents, f, ensure_ascii=False, indent=2)
