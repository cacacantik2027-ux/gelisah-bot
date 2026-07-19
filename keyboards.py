from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# Bot API 9.4 (9 Februari 2026) menambahkan field `style` di InlineKeyboardButton
# & KeyboardButton, jadi tombol DM bot (bukan Mini App) sekarang bisa punya warna
# native dari Telegram sendiri -- bukan lagi cuma abu-abu/putih polos.
# Nilai yang diterima Bot API HANYA berupa string: "primary" (biru), "success"
# (hijau), "danger" (merah) -- lihat https://core.telegram.org/bots/api#inlinekeyboardbutton
# Sengaja pakai string literal langsung (bukan import enum semacam
# `telegram.constants.KeyboardButtonStyle`) karena kelas enum itu TIDAK ada di
# python-telegram-bot -- kalau diimport bakal langsung ImportError dan bikin
# seluruh bot gagal start. String literal ini valid di python-telegram-bot >= 22.7
# (versi yang sudah expose parameter `style`) maupun versi lain yang menerima
# raw string, dan aman dikirim ke client Telegram versi berapa pun -- di client
# lama (sebelum 9 Feb 2026) tombol cuma tampil normal tanpa warna, tidak error.
#
# Semua tombol di file ini sekarang diberi salah satu dari 3 warna (tidak ada
# lagi tombol polos tanpa style), dengan konvensi:
#   - STYLE_SUCCESS (hijau) -> aksi menambah / konfirmasi positif
#   - STYLE_DANGER  (merah) -> aksi merusak / menghapus / mengakhiri / batal permanen
#   - STYLE_PRIMARY (biru)  -> semua aksi lain (buka, pilih, edit, navigasi, kembali, dst)
STYLE_PRIMARY = "primary"   # biru -- aksi utama / navigasi
STYLE_DANGER = "danger"     # merah -- aksi merusak/mengakhiri/menghapus
STYLE_SUCCESS = "success"   # hijau -- aksi menambah/konfirmasi positif


def webapp_launch_keyboard(webapp_url):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(
            "💃 Buka Katalog Talent (Tampilan App)",
            web_app=WebAppInfo(url=webapp_url),
            style=STYLE_PRIMARY,
        )]],
        resize_keyboard=True,
    )


def webapp_channel_keyboard(bot_username, app_short_name="katalog", label="💃 Buka Katalog Talent", icon_custom_emoji_id=None):
    """Tombol Mini App khusus untuk dipasang di pesan channel.

    Field `web_app=` (dipakai di `webapp_launch_keyboard` di atas) hanya bisa
    tampil/berfungsi di private chat antara user & bot, jadi tidak bisa dipakai
    di channel. Untuk channel, Mini App harus dibuka lewat link langsung
    `https://t.me/<bot_username>/<app_short_name>` (short name didaftarkan
    lewat @BotFather -> /newapp), dipasang sebagai tombol `url=` biasa.

    `icon_custom_emoji_id` (opsional) menampilkan emoji custom di depan teks
    tombol. CATATAN: khusus tombol di CHANNEL, ini hanya benar-benar tampil
    kalau bot sudah beli username tambahan di Fragment -- status Premium
    pemilik bot TIDAK berlaku untuk tombol yang diposting ke channel.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            label,
            url=f"https://t.me/{bot_username}/{app_short_name}",
            style=STYLE_PRIMARY,
        )]
    ])


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💃 Pilih Talent", callback_data="menu_talents", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("📖 Cara Order", callback_data="menu_howtoorder", style=STYLE_PRIMARY)],
    ])


def talent_carousel_keyboard(talent, index, total):
    """Kartu 1 talent per tampilan: tombol nama talent (buka detail lengkap)
    + navigasi Sebelumnya/Selanjutnya untuk pindah ke talent lain satu-satu,
    seperti "geser halaman" bukan daftar tombol nama yang panjang."""
    rows = [
        [InlineKeyboardButton(talent["name"], callback_data=f"talent_{talent['id']}", style=STYLE_PRIMARY)],
    ]

    nav_row = []
    if index > 0:
        nav_row.append(
            InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"menu_talents_i{index - 1}", style=STYLE_PRIMARY)
        )
    if index < total - 1:
        nav_row.append(
            InlineKeyboardButton("Selanjutnya ➡️", callback_data=f"menu_talents_i{index + 1}", style=STYLE_PRIMARY)
        )
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="menu_back", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def talent_detail_keyboard(talent):
    rows = []
    if talent.get("portfolio_url"):
        rows.append([InlineKeyboardButton("📢 Channel Telegram", url=talent["portfolio_url"], style=STYLE_PRIMARY)])
    rows.append([InlineKeyboardButton("💰 Pricelist", callback_data=f"price_{talent['id']}", style=STYLE_PRIMARY)])
    rows.append([InlineKeyboardButton("💬 Chat Sekarang", callback_data=f"chat_{talent['id']}", style=STYLE_PRIMARY)])
    rows.append([InlineKeyboardButton("⬅️ Kembali ke Daftar Talent", callback_data="menu_talents", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def back_to_talent_keyboard(talent_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Chat Sekarang", callback_data=f"chat_{talent_id}", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("⬅️ Kembali", callback_data=f"talent_{talent_id}", style=STYLE_PRIMARY)],
    ])


def end_chat_keyboard(session_id):
    """Tombol yang tampil di pesan header sesi live chat (di grup/private admin),
    dipakai admin untuk mengakhiri sesi kalau topik obrolan sudah selesai."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Akhiri Sesi", callback_data=f"endchat_{session_id}", style=STYLE_DANGER)],
    ])


def bgm_list_keyboard(tracks):
    """Daftar BGM terupload, tiap baris = 1 lagu dengan tombol hapus di sampingnya."""
    rows = [
        [InlineKeyboardButton(f"🗑 {t['title']}", callback_data=f"delbgm_{t['id']}", style=STYLE_DANGER)]
        for t in tracks
    ]
    return InlineKeyboardMarkup(rows)


def addtalent_step_keyboard(back_callback=None):
    rows = []
    if back_callback:
        rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data=back_callback, style=STYLE_PRIMARY)])
    rows.append([InlineKeyboardButton("❌ Batalkan", callback_data="addtalent_cancel", style=STYLE_DANGER)])
    return InlineKeyboardMarkup(rows)


def settings_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Talent", callback_data="settings_addtalent", style=STYLE_SUCCESS)],
        [InlineKeyboardButton("📋 Daftar Talent", callback_data="settings_listtalent", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("✏️ Edit Talent", callback_data="settings_edittalent", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🗑️ Hapus Talent", callback_data="settings_deltalent", style=STYLE_DANGER)],
        [InlineKeyboardButton("✏️ Ubah Sapaan (/start)", callback_data="settings_greeting", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("✏️ Ubah Cara Order", callback_data="settings_howtoorder", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🖼️ Ubah Background Mini App", callback_data="settings_webappbg", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("📢 Ubah Info Channel 1", callback_data="settings_channel", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("📢 Ubah Info Channel 2", callback_data="settings_channel2", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🎗️ Kelola Sponsor", callback_data="settings_sponsor", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🎪 Aktif/Nonaktifkan Sponsor Melayang", callback_data="settings_togglefloatingsponsor", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("💬 Sesi Live Chat Aktif", callback_data="settings_sessions", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("👥 Kelola Admin Grup", callback_data="settings_admins", style=STYLE_PRIMARY)],
    ])


def group_admins_menu_keyboard():
    """Submenu 'Kelola Admin Grup' -- tambah admin baru atau lihat/hapus
    yang sudah ada. Kartu-kartu ini yang tampil di halaman 'Admin Grup' Mini App."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Admin", callback_data="settings_addadmin", style=STYLE_SUCCESS)],
        [InlineKeyboardButton("📋 Daftar & Hapus Admin", callback_data="settings_listadmins", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style=STYLE_PRIMARY)],
    ])


def group_admins_list_keyboard(admins):
    """Daftar admin grup, tiap baris punya tombol hapus sendiri (mirip pola
    delete_talent_keyboard/delete_sponsor_keyboard)."""
    rows = [
        [InlineKeyboardButton(f"🗑 {a['full_name'] or a['username'] or a['user_id']}", callback_data=f"deladmin_{a['id']}", style=STYLE_DANGER)]
        for a in admins
    ]
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="settings_admins", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def back_to_settings_keyboard():
    """Tombol 'Kembali' generik yang membatalkan langkah saat ini dan
    kembali ke Menu Pengaturan (dipakai di prompt ubah sapaan & cara order)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style=STYLE_PRIMARY)],
    ])


def preview_edit_keyboard(edit_callback):
    """Tombol '✏️ Edit' + 'Kembali', dipakai di halaman pratinjau (menampilkan
    isi yang sedang tersimpan) sebelum admin masuk ke mode kirim teks/foto baru."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit", callback_data=edit_callback, style=STYLE_PRIMARY)],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style=STYLE_PRIMARY)],
    ])


def sponsor_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Sponsor", callback_data="sponsor_add", style=STYLE_SUCCESS)],
        [InlineKeyboardButton("📋 Daftar Sponsor", callback_data="sponsor_list", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("✏️ Edit Sponsor", callback_data="settings_editsponsor", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🗑️ Hapus Sponsor", callback_data="sponsor_del", style=STYLE_DANGER)],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style=STYLE_PRIMARY)],
    ])


def edit_talent_list_keyboard(talents):
    rows = [
        [InlineKeyboardButton(f"✏️ {t['name']}", callback_data=f"edittalent_{t['id']}", style=STYLE_PRIMARY)]
        for t in talents
    ]
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def edit_talent_field_keyboard(talent):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Nama", callback_data=f"edittalentfield_{talent['id']}_name", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("📝 Deskripsi", callback_data=f"edittalentfield_{talent['id']}_description", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("💰 Pricelist", callback_data=f"edittalentfield_{talent['id']}_pricelist", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🔗 Link Channel", callback_data=f"edittalentfield_{talent['id']}_portfolio_url", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🖼️ Foto", callback_data=f"edittalentfield_{talent['id']}_photo_file_id", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_edittalent", style=STYLE_PRIMARY)],
    ])


def edit_sponsor_list_keyboard(sponsors):
    rows = []
    for s in sponsors:
        label = s["name"] or f"Sponsor #{s['id']}"
        rows.append([InlineKeyboardButton(f"✏️ {label}", callback_data=f"editsponsor_{s['id']}", style=STYLE_PRIMARY)])
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="settings_sponsor", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def edit_sponsor_field_keyboard(sponsor):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Nama", callback_data=f"editsponsorfield_{sponsor['id']}_name", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("📝 Deskripsi", callback_data=f"editsponsorfield_{sponsor['id']}_description", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🎪 Deskripsi Melayang", callback_data=f"editsponsorfield_{sponsor['id']}_marquee_desc", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🔗 Link", callback_data=f"editsponsorfield_{sponsor['id']}_url", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🖼️ Foto", callback_data=f"editsponsorfield_{sponsor['id']}_photo_file_id", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_editsponsor", style=STYLE_PRIMARY)],
    ])


def delete_sponsor_keyboard(sponsors):
    rows = []
    for s in sponsors:
        label = s["name"] or f"Sponsor #{s['id']}"
        rows.append([InlineKeyboardButton(f"🗑️ {label}", callback_data=f"sponsordelconfirm_{s['id']}", style=STYLE_DANGER)])
    rows.append([InlineKeyboardButton("⬅️ Batal", callback_data="settings_sponsor", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def delete_talent_keyboard(talents):
    rows = [
        [InlineKeyboardButton(f"🗑️ {t['name']}", callback_data=f"delconfirm_{t['id']}", style=STYLE_DANGER)]
        for t in talents
    ]
    rows.append([InlineKeyboardButton("⬅️ Batal", callback_data="settings_back", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)
