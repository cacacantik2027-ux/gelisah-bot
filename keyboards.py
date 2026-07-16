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


TALENTS_PER_PAGE = 8


def talent_list_keyboard(talents, page=1):
    """Tampilkan daftar talent dengan pagination (per halaman), supaya kalau
    talent-nya banyak, keyboard-nya tidak kepanjangan / gagal ditampilkan Telegram."""
    total = len(talents)
    total_pages = max(1, (total + TALENTS_PER_PAGE - 1) // TALENTS_PER_PAGE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * TALENTS_PER_PAGE
    page_talents = talents[start:start + TALENTS_PER_PAGE]

    rows = [
        [InlineKeyboardButton(t["name"], callback_data=f"talent_{t['id']}", style="primary")]
        for t in page_talents
    ]

    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(
                InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"menu_talents_p{page - 1}", style="primary")
            )
        nav_row.append(
            InlineKeyboardButton(f"{page}/{total_pages}", callback_data="menu_noop", style="primary")
        )
        if page < total_pages:
            nav_row.append(
                InlineKeyboardButton("Selanjutnya ➡️", callback_data=f"menu_talents_p{page + 1}", style="primary")
            )
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="menu_back", style="danger")])
    return InlineKeyboardMarkup(rows)


def talent_detail_keyboard(talent):
    rows = []
    if talent.get("portfolio_url"):
        rows.append([InlineKeyboardButton("📢 Channel Telegram", url=talent["portfolio_url"], style="primary")])
    rows.append([InlineKeyboardButton("💰 Pricelist", callback_data=f"price_{talent['id']}", style="success")])
    rows.append([InlineKeyboardButton("💬 Chat Sekarang", callback_data=f"chat_{talent['id']}", style="success")])
    rows.append([InlineKeyboardButton("⬅️ Kembali ke Daftar Talent", callback_data="menu_talents", style="danger")])
    return InlineKeyboardMarkup(rows)


def back_to_talent_keyboard(talent_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Chat Sekarang", callback_data=f"chat_{talent_id}", style="success")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data=f"talent_{talent_id}", style="danger")],
    ])


def end_chat_keyboard(session_id):
    """Tombol yang tampil di pesan header sesi live chat (di grup/private admin),
    dipakai admin untuk mengakhiri sesi kalau topik obrolan sudah selesai."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Akhiri Sesi", callback_data=f"endchat_{session_id}", style="danger")],
    ])


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
        [InlineKeyboardButton("💬 Sesi Live Chat Aktif", callback_data="settings_sessions", style="primary")],
    ])


def delete_talent_keyboard(talents):
    rows = [
        [InlineKeyboardButton(f"🗑️ {t['name']}", callback_data=f"delconfirm_{t['id']}", style="danger")]
        for t in talents
    ]
    rows.append([InlineKeyboardButton("⬅️ Batal", callback_data="settings_back", style="primary")])
    return InlineKeyboardMarkup(rows)
