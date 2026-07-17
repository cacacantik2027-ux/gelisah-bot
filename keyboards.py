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


def webapp_channel_keyboard(bot_username, app_short_name="katalog", label="💃 Buka Katalog Talent"):
    """Tombol Mini App khusus untuk dipasang di pesan channel.

    Field `web_app=` (dipakai di `webapp_launch_keyboard` di atas) hanya bisa
    tampil/berfungsi di private chat antara user & bot, jadi tidak bisa dipakai
    di channel. Untuk channel, Mini App harus dibuka lewat link langsung
    `https://t.me/<bot_username>/<app_short_name>` (short name didaftarkan
    lewat @BotFather -> /newapp), dipasang sebagai tombol `url=` biasa.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            label,
            url=f"https://t.me/{bot_username}/{app_short_name}",
            style="success",
        )]
    ])


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💃 Pilih Talent", callback_data="menu_talents", style="success")],
        [InlineKeyboardButton("📖 Cara Order", callback_data="menu_howtoorder", style="primary")],
    ])


def talent_carousel_keyboard(talent, index, total):
    """Kartu 1 talent per tampilan: tombol nama talent (buka detail lengkap)
    + navigasi Sebelumnya/Selanjutnya untuk pindah ke talent lain satu-satu,
    seperti "geser halaman" bukan daftar tombol nama yang panjang."""
    rows = [
        [InlineKeyboardButton(talent["name"], callback_data=f"talent_{talent['id']}", style="success")],
    ]

    nav_row = []
    if index > 0:
        nav_row.append(
            InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"menu_talents_i{index - 1}", style="primary")
        )
    nav_row.append(
        InlineKeyboardButton(f"{index + 1}/{total}", callback_data="menu_noop", style="primary")
    )
    if index < total - 1:
        nav_row.append(
            InlineKeyboardButton("Selanjutnya ➡️", callback_data=f"menu_talents_i{index + 1}", style="primary")
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
        [InlineKeyboardButton("✏️ Edit Talent", callback_data="settings_edittalent", style="primary")],
        [InlineKeyboardButton("🗑️ Hapus Talent", callback_data="settings_deltalent", style="danger")],
        [InlineKeyboardButton("✏️ Ubah Sapaan (/start)", callback_data="settings_greeting", style="primary")],
        [InlineKeyboardButton("✏️ Ubah Cara Order", callback_data="settings_howtoorder", style="primary")],
        [InlineKeyboardButton("🖼️ Ubah Background Mini App", callback_data="settings_webappbg", style="primary")],
        [InlineKeyboardButton("📢 Ubah Info Channel 1", callback_data="settings_channel", style="primary")],
        [InlineKeyboardButton("📢 Ubah Info Channel 2", callback_data="settings_channel2", style="primary")],
        [InlineKeyboardButton("🎗️ Kelola Sponsor", callback_data="settings_sponsor", style="primary")],
        [InlineKeyboardButton("🎪 Aktif/Nonaktifkan Sponsor Melayang", callback_data="settings_togglefloatingsponsor", style="primary")],
        [InlineKeyboardButton("💬 Sesi Live Chat Aktif", callback_data="settings_sessions", style="primary")],
    ])


def back_to_settings_keyboard():
    """Tombol 'Kembali' generik yang membatalkan langkah saat ini dan
    kembali ke Menu Pengaturan (dipakai di prompt ubah sapaan & cara order)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style="primary")],
    ])


def preview_edit_keyboard(edit_callback):
    """Tombol '✏️ Edit' + 'Kembali', dipakai di halaman pratinjau (menampilkan
    isi yang sedang tersimpan) sebelum admin masuk ke mode kirim teks/foto baru."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit", callback_data=edit_callback, style="primary")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style="primary")],
    ])


def sponsor_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Sponsor", callback_data="sponsor_add", style="success")],
        [InlineKeyboardButton("📋 Daftar Sponsor", callback_data="sponsor_list", style="primary")],
        [InlineKeyboardButton("✏️ Edit Sponsor", callback_data="settings_editsponsor", style="primary")],
        [InlineKeyboardButton("🗑️ Hapus Sponsor", callback_data="sponsor_del", style="danger")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style="primary")],
    ])


def edit_talent_list_keyboard(talents):
    rows = [
        [InlineKeyboardButton(f"✏️ {t['name']}", callback_data=f"edittalent_{t['id']}", style="primary")]
        for t in talents
    ]
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style="primary")])
    return InlineKeyboardMarkup(rows)


def edit_talent_field_keyboard(talent):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Nama", callback_data=f"edittalentfield_{talent['id']}_name", style="primary")],
        [InlineKeyboardButton("📝 Deskripsi", callback_data=f"edittalentfield_{talent['id']}_description", style="primary")],
        [InlineKeyboardButton("💰 Pricelist", callback_data=f"edittalentfield_{talent['id']}_pricelist", style="primary")],
        [InlineKeyboardButton("🔗 Link Channel", callback_data=f"edittalentfield_{talent['id']}_portfolio_url", style="primary")],
        [InlineKeyboardButton("🖼️ Foto", callback_data=f"edittalentfield_{talent['id']}_photo_file_id", style="primary")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_edittalent", style="danger")],
    ])


def edit_sponsor_list_keyboard(sponsors):
    rows = []
    for s in sponsors:
        label = s["name"] or f"Sponsor #{s['id']}"
        rows.append([InlineKeyboardButton(f"✏️ {label}", callback_data=f"editsponsor_{s['id']}", style="primary")])
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="settings_sponsor", style="primary")])
    return InlineKeyboardMarkup(rows)


def edit_sponsor_field_keyboard(sponsor):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Nama", callback_data=f"editsponsorfield_{sponsor['id']}_name", style="primary")],
        [InlineKeyboardButton("📝 Deskripsi", callback_data=f"editsponsorfield_{sponsor['id']}_description", style="primary")],
        [InlineKeyboardButton("🔗 Link", callback_data=f"editsponsorfield_{sponsor['id']}_url", style="primary")],
        [InlineKeyboardButton("🖼️ Foto", callback_data=f"editsponsorfield_{sponsor['id']}_photo_file_id", style="primary")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_editsponsor", style="danger")],
    ])


def delete_sponsor_keyboard(sponsors):
    rows = []
    for s in sponsors:
        label = s["name"] or f"Sponsor #{s['id']}"
        rows.append([InlineKeyboardButton(f"🗑️ {label}", callback_data=f"sponsordelconfirm_{s['id']}", style="danger")])
    rows.append([InlineKeyboardButton("⬅️ Batal", callback_data="settings_sponsor", style="primary")])
    return InlineKeyboardMarkup(rows)


def delete_talent_keyboard(talents):
    rows = [
        [InlineKeyboardButton(f"🗑️ {t['name']}", callback_data=f"delconfirm_{t['id']}", style="danger")]
        for t in talents
    ]
    rows.append([InlineKeyboardButton("⬅️ Batal", callback_data="settings_back", style="primary")])
    return InlineKeyboardMarkup(rows)
