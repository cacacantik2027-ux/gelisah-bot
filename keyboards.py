from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)


def webapp_launch_keyboard(webapp_url):
    return ReplyKeyboardMarkup(
        [[KeyboardButton("💃 Buka Katalog Talent (Tampilan App)", web_app=WebAppInfo(url=webapp_url))]],
        resize_keyboard=True,
    )


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💃 Pilih Talent", callback_data="menu_talents", style="success")],
        [InlineKeyboardButton("📖 Cara Order", callback_data="menu_howtoorder", style="primary")],
    ])


def talent_list_keyboard(talents):
    rows = [
        [InlineKeyboardButton(t["name"], callback_data=f"talent_{t['id']}", style="primary")]
        for t in talents
    ]
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="menu_back", style="danger")])
    return InlineKeyboardMarkup(rows)


def talent_detail_keyboard(talent):
    rows = []
    if talent.get("portfolio_url"):
        rows.append([InlineKeyboardButton("📢 Channel Telegram", url=talent["portfolio_url"], style="primary")])
    rows.append([InlineKeyboardButton("💰 Pricelist", callback_data=f"price_{talent['id']}", style="success")])
    rows.append([InlineKeyboardButton("📝 Ajukan Booking", callback_data=f"book_{talent['id']}", style="success")])
    rows.append([InlineKeyboardButton("⬅️ Kembali ke Daftar Talent", callback_data="menu_talents", style="danger")])
    return InlineKeyboardMarkup(rows)


def back_to_talent_keyboard(talent_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Ajukan Booking", callback_data=f"book_{talent_id}", style="success")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data=f"talent_{talent_id}", style="danger")],
    ])


def cancel_booking_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Batalkan", callback_data="booking_cancel", style="danger")],
    ])


def booking_step_keyboard(back_callback=None):
    rows = []
    if back_callback:
        rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data=back_callback, style="primary")])
    rows.append([InlineKeyboardButton("❌ Batalkan", callback_data="booking_cancel", style="danger")])
    return InlineKeyboardMarkup(rows)


def addtalent_step_keyboard(back_callback=None):
    rows = []
    if back_callback:
        rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data=back_callback, style="primary")])
    rows.append([InlineKeyboardButton("❌ Batalkan", callback_data="addtalent_cancel", style="danger")])
    return InlineKeyboardMarkup(rows)


def settings_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Talent", callback_data="settings_addtalent", style="success")],
        [InlineKeyboardButton("📋 Daftar Talent", callback_data="settings_listtalent", style="primary")],
        [InlineKeyboardButton("🗑️ Hapus Talent", callback_data="settings_deltalent", style="danger")],
        [InlineKeyboardButton("✏️ Ubah Sapaan (/start)", callback_data="settings_greeting", style="primary")],
        [InlineKeyboardButton("✏️ Ubah Cara Order", callback_data="settings_howtoorder", style="primary")],
        [InlineKeyboardButton("📥 Lihat Booking Masuk", callback_data="settings_bookings", style="primary")],
    ])


def delete_talent_keyboard(talents):
    rows = [
        [InlineKeyboardButton(f"🗑️ {t['name']}", callback_data=f"delconfirm_{t['id']}", style="danger")]
        for t in talents
    ]
    rows.append([InlineKeyboardButton("⬅️ Batal", callback_data="settings_back", style="primary")])
    return InlineKeyboardMarkup(rows)
