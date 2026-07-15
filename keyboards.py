"""
keyboards.py
============
Kumpulan fungsi pembuat inline keyboard untuk bot TALENT GELISAH.
"""

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton,
)

import config
import database as db


def webapp_launch_keyboard():
    """Reply keyboard (BUKAN inline) berisi 1 tombol yang membuka Mini App
    "Pilih Talent" (katalog visual). Tombol ini HARUS reply keyboard (bukan
    inline) supaya Telegram.WebApp.sendData() di halamannya benar-benar
    sampai ke handle_webapp_data() di bot.py -- lihat
    https://core.telegram.org/bots/webapps#keyboard-button-web-apps

    Kembalikan None kalau WEBAPP_URL belum di-setup admin (supaya pemanggil
    bisa skip mengirim keyboard ini, dan bot tetap jalan normal lewat
    daftar talent versi tombol chat biasa)."""
    if not config.WEBAPP_URL:
        return None
    return ReplyKeyboardMarkup(
        [[KeyboardButton("💃 Buka Katalog Talent (Tampilan App)", web_app=WebAppInfo(url=config.WEBAPP_URL))]],
        resize_keyboard=True,
    )


def remove_webapp_keyboard():
    return ReplyKeyboardRemove()


def main_menu_keyboard():
    """Menu /start:
    - "Pilih Talent" sendirian di baris paling atas (paling menonjol).
    - "Live Chat" & "Cara Order" sejajar di baris kedua.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💃 Pilih Talent", callback_data="show_talents", style="success")],
        [
            InlineKeyboardButton("💬 Live Chat", callback_data="start_livechat", style="primary"),
            InlineKeyboardButton("📖 Cara Order", callback_data="how_to_order", style="primary"),
        ],
    ])


def how_to_order_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="back_main", style="danger")],
    ])


def talent_list_keyboard():
    """Daftar talent, 2 tombol per baris, tiap tombol buka halaman detail
    talent (foto + deskripsi + tombol pricelist/tanyakan ready)."""
    talents = db.list_talents()
    talent_buttons = [
        InlineKeyboardButton(f"👤 {t['name']}", callback_data=f"talent_{t['id']}", style="success")
        for t in talents
    ]
    buttons = [talent_buttons[i:i + 2] for i in range(0, len(talent_buttons), 2)]
    buttons.append([InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="back_main", style="danger")])
    return InlineKeyboardMarkup(buttons)


def talent_detail_keyboard(talent_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Pricelist", callback_data=f"pricelist_{talent_id}", style="primary"),
            InlineKeyboardButton("❓ Tanyakan Ready", callback_data=f"ready_{talent_id}", style="success"),
        ],
        [InlineKeyboardButton("⬅️ Kembali ke Daftar Talent", callback_data="show_talents", style="danger")],
    ])


def pricelist_back_keyboard(talent_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Kembali ke Halaman Talent", callback_data=f"talent_{talent_id}", style="danger")],
    ])


def after_ready_keyboard(talent_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Kembali ke Halaman Talent", callback_data=f"talent_{talent_id}", style="danger")],
    ])


def livechat_active_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Akhiri Live Chat", callback_data="end_livechat", style="danger")],
    ])


def back_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Kembali ke Menu Utama", callback_data="back_main", style="danger")],
    ])


# ── Admin: kelola talent ──────────────────────────────────────────────
def talent_admin_list_keyboard(prefix: str):
    """prefix: 'edittalent' atau 'deltalent'."""
    style = "danger" if prefix == "deltalent" else "primary"
    talents = db.list_talents(active_only=False)
    buttons = [
        [InlineKeyboardButton(t["name"], callback_data=f"{prefix}_{t['id']}", style=style)]
        for t in talents
    ]
    buttons.append([InlineKeyboardButton("❎ Tutup", callback_data="admin_close", style="danger")])
    return InlineKeyboardMarkup(buttons)


def confirm_delete_keyboard(talent_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Ya, Hapus", callback_data=f"deltalent_confirm_{talent_id}", style="danger"),
            InlineKeyboardButton("↩️ Batal", callback_data="admin_close", style="primary"),
        ]
    ])


def skip_keyboard(callback_data: str = "skip_step"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Lewati", callback_data=callback_data, style="primary")]])
