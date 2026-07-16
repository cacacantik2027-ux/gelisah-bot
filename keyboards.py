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
        [InlineKeyboardButton("💃 Pilih Talent", callback_data="menu_talents")],
        [InlineKeyboardButton("📖 Cara Order", callback_data="menu_howtoorder")],
    ])


def talent_list_keyboard(talents):
    rows = [
        [InlineKeyboardButton(t["name"], callback_data=f"talent_{t['id']}")]
        for t in talents
    ]
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="menu_back")])
    return InlineKeyboardMarkup(rows)


def talent_detail_keyboard(talent):
    rows = []
    if talent.get("portfolio_url"):
        rows.append([InlineKeyboardButton("🔗 Lihat Portofolio", url=talent["portfolio_url"])])
    rows.append([InlineKeyboardButton("💰 Pricelist", callback_data=f"price_{talent['id']}")])
    rows.append([InlineKeyboardButton("📝 Ajukan Booking", callback_data=f"book_{talent['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Kembali ke Daftar Talent", callback_data="menu_talents")])
    return InlineKeyboardMarkup(rows)


def back_to_talent_keyboard(talent_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Ajukan Booking", callback_data=f"book_{talent_id}")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data=f"talent_{talent_id}")],
    ])


def cancel_booking_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Batalkan", callback_data="booking_cancel")],
    ])


def settings_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Talent", callback_data="settings_addtalent")],
        [InlineKeyboardButton("📋 Daftar Talent", callback_data="settings_listtalent")],
        [InlineKeyboardButton("🗑️ Hapus Talent", callback_data="settings_deltalent")],
        [InlineKeyboardButton("✏️ Ubah Sapaan (/start)", callback_data="settings_greeting")],
        [InlineKeyboardButton("✏️ Ubah Cara Order", callback_data="settings_howtoorder")],
        [InlineKeyboardButton("📥 Lihat Booking Masuk", callback_data="settings_bookings")],
    ])


def delete_talent_keyboard(talents):
    rows = [
        [InlineKeyboardButton(f"🗑️ {t['name']}", callback_data=f"delconfirm_{t['id']}")]
        for t in talents
    ]
    rows.append([InlineKeyboardButton("⬅️ Batal", callback_data="settings_back")])
    return InlineKeyboardMarkup(rows)
