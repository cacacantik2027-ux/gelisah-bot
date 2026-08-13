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


def group_start_keyboard(user_id, message_id):
    """Tombol "🚀 Mulai" yang bot kirim di GRUP setelah di-mention/di-reply.
    callback_data membawa id user yang di-mention (supaya tombol yang sama
    tidak bisa dipakai/dibajak member lain di grup itu) SEKALIGUS id pesan
    mention aslinya (supaya kartu talent nanti bisa dikirim sebagai balasan
    langsung ke pesan user itu, tanpa perlu menyimpan status apa pun di
    memori bersama/chat_data -- semua informasi yang dibutuhkan sudah
    menempel di tombolnya sendiri, jadi aman dipakai banyak orang sekaligus)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Mulai", callback_data=f"groupstart_{user_id}_{message_id}", style=STYLE_SUCCESS)],
    ])


def private_deeplink_keyboard(url, label="🤖 Buka Chat Pribadi"):
    """Tombol link biasa (bukan callback) yang membuka private chat dengan
    bot lewat deep link `https://t.me/<username>?start=<payload>` -- dipakai
    untuk aksi yang memang cuma boleh jalan di private chat (mis. live chat),
    supaya user tidak perlu cari-cari sendiri chat bot-nya."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, url=url, style=STYLE_PRIMARY)],
    ])


def promo_keyboard(talents, bot_username):
    """Tombol deep link "💬 <Nama Talent>" per talent, dua kolom per baris,
    untuk pesan Auto Promo Talent Ready yang diposting bot ke grup (lihat
    _post_promo_job di bot.py). Sengaja pakai URL BUTTON (bukan
    callback_data) -- sama seperti private_deeplink_keyboard di atas --
    karena tombol ini harus membuka PRIVATE CHAT bot dari dalam GRUP,
    lalu /start otomatis melanjutkan ke sesi live chat talent yang sama
    (lihat payload "?start=chat_<id>" di fungsi start() di bot.py)."""
    rows = []
    row = []
    for talent in talents:
        deep_link = f"https://t.me/{bot_username}?start=chat_{talent['id']}"
        row.append(InlineKeyboardButton(f"💬 {talent['name']}", url=deep_link, style=STYLE_PRIMARY))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💃 Pilih Talent", callback_data="menu_talents", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("📖 Cara Order", callback_data="menu_howtoorder", style=STYLE_PRIMARY)],
    ])


def _owner_suffixed(callback_data: str, owner_id) -> str:
    """Tempelkan suffix "_o<user_id>" ke `callback_data` kalau `owner_id`
    diisi -- dipakai supaya tombol kartu talent yang tampil di GRUP terkunci
    ke user yang memicunya (lihat _split_owner_suffix/_enforce_card_owner
    di bot.py). Kalau owner_id None (kartu private chat, tidak perlu
    dikunci), callback_data dibalikin apa adanya."""
    if owner_id is None:
        return callback_data
    return f"{callback_data}_o{owner_id}"


def talent_carousel_keyboard(talent, index, total, close_button=False, owner_id=None):
    """Kartu 1 talent per tampilan: tombol nama talent (buka detail lengkap)
    + navigasi Sebelumnya/Selanjutnya untuk pindah ke talent lain satu-satu,
    seperti "geser halaman" bukan daftar tombol nama yang panjang.

    `close_button=True` dipakai saat kartu ini tampil di GRUP (lihat
    send_talent_card_to_chat/show_talent_card di bot.py): tombol terakhir
    jadi "❌ Tutup" (hapus kartunya) alih-alih "⬅️ Kembali" ke sapaan menu
    utama -- soalnya sapaan itu konsepnya menu DM, ganjil kalau muncul di
    tengah grup.

    `owner_id` (diisi kalau kartu ini tampil di GRUP) mengunci SEMUA tombol
    kartu ini (nama talent, navigasi, tutup) supaya cuma bisa ditekan oleh
    user itu -- member grup lain yang menekannya akan mendapat pesan
    peringatan dari bot (lihat _enforce_card_owner di bot.py), bukan ikut
    "membajak"/mengubah kartu milik orang lain."""
    rows = [
        [InlineKeyboardButton(
            talent["name"],
            callback_data=_owner_suffixed(f"talent_{talent['id']}_i{index}", owner_id),
            style=STYLE_PRIMARY,
        )],
    ]

    nav_row = []
    if index > 0:
        nav_row.append(
            InlineKeyboardButton(
                "⬅️ Sebelumnya",
                callback_data=_owner_suffixed(f"menu_talents_i{index - 1}", owner_id),
                style=STYLE_PRIMARY,
            )
        )
    if index < total - 1:
        nav_row.append(
            InlineKeyboardButton(
                "Selanjutnya ➡️",
                callback_data=_owner_suffixed(f"menu_talents_i{index + 1}", owner_id),
                style=STYLE_PRIMARY,
            )
        )
    if nav_row:
        rows.append(nav_row)

    if close_button:
        rows.append([InlineKeyboardButton(
            "❌ Tutup", callback_data=_owner_suffixed("menu_close", owner_id), style=STYLE_DANGER
        )])
    else:
        rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="menu_back", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def talent_detail_keyboard(talent, owner_id=None, index=None):
    """`owner_id` (diisi kalau halaman detail ini tampil di GRUP) mengunci
    tombol Pricelist & Kembali ke Daftar Talent ke user tsb -- lihat catatan
    owner_id di talent_carousel_keyboard di atas. Tombol "Channel Telegram"
    (url biasa) dan "Chat Sekarang" sengaja TIDAK dikunci: link channel cuma
    membuka URL publik, dan "Chat Sekarang" sudah otomatis diarahkan bot ke
    private chat msing-masing penekan (lihat chat_start_callback), jadi aman
    dipakai siapa pun tanpa perlu dikunci ke satu user.

    `index` = posisi talent ini di carousel (kalau diketahui, lihat
    _parse_id_and_index di bot.py) -- diselipkan ke callback_data tombol
    Pricelist ("price_<id>_i<index>") supaya kalau nanti user menekan
    "Kembali" dari halaman pricelist, dia balik ke halaman detail INI LAGI
    dengan index yang sama, bukan kehilangan jejak posisinya. Tombol
    "Kembali ke Daftar Talent" juga memakai index yang sama
    ("menu_talents_i<index>") supaya balik PERSIS ke posisi talent ini di
    carousel, bukan reset ke talent pertama/urutan awal. Kalau index None
    (mis. halaman ini dibuka dari luar carousel, seperti Mini App atau deep
    link "Chat Sekarang"), fallback ke perilaku lama: tombol Pricelist tanpa
    index, dan "Kembali ke Daftar Talent" ke urutan awal (index 0)."""
    price_suffix = f"_i{index}" if index is not None else ""
    back_target = f"menu_talents_i{index}" if index is not None else "menu_talents"

    rows = []
    if talent.get("portfolio_url"):
        rows.append([InlineKeyboardButton("📢 Channel Telegram", url=talent["portfolio_url"], style=STYLE_PRIMARY)])
    rows.append([InlineKeyboardButton(
        "💰 Pricelist", callback_data=_owner_suffixed(f"price_{talent['id']}{price_suffix}", owner_id), style=STYLE_PRIMARY
    )])
    rows.append([InlineKeyboardButton("💬 Chat Sekarang", callback_data=f"chat_{talent['id']}", style=STYLE_PRIMARY)])
    rows.append([InlineKeyboardButton(
        "⬅️ Kembali ke Daftar Talent",
        callback_data=_owner_suffixed(back_target, owner_id),
        style=STYLE_PRIMARY,
    )])
    return InlineKeyboardMarkup(rows)


def back_to_talent_keyboard(talent_id, owner_id=None, index=None):
    """`owner_id` mengunci tombol "⬅️ Kembali" (balik ke halaman detail talent)
    ke user tsb kalau halaman pricelist ini tampil di GRUP -- lihat catatan
    owner_id di talent_carousel_keyboard di atas.

    `index` = posisi talent ini di carousel (diteruskan dari halaman detail
    yang membuka pricelist ini, lihat pricelist_callback di bot.py) --
    disisipkan ke callback_data ("talent_<id>_i<index>") supaya tombol
    "Kembali" ini membawa balik ke halaman detail talent yang sama LENGKAP
    dengan index aslinya, bukan cuma ke detail talent tanpa jejak posisi
    carousel (yang bikin tombol "Kembali ke Daftar Talent" di halaman detail
    nanti ikut kehilangan jejak juga). Kalau index None, fallback ke
    perilaku lama (tanpa index)."""
    target = f"talent_{talent_id}_i{index}" if index is not None else f"talent_{talent_id}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Chat Sekarang", callback_data=f"chat_{talent_id}", style=STYLE_PRIMARY)],
        [InlineKeyboardButton(
            "⬅️ Kembali", callback_data=_owner_suffixed(target, owner_id), style=STYLE_PRIMARY
        )],
    ])


def end_chat_keyboard(session_id):
    """Tombol yang tampil di pesan header sesi live chat (di grup/private admin).
    "Akhiri Sesi" dipakai kalau topik obrolan memang sudah selesai secara normal.
    "Reset Sesi" dipakai khusus kalau sesi ini MACET/STUCK (mis. user komplain
    tidak dibalas padahal admin tidak melihat pesan apa pun, relay-nya kacau,
    dsb) -- beda dari akhiri sesi biasa, reset juga membersihkan pemetaan
    relay-nya supaya user bisa langsung mulai sesi live chat yang baru."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Akhiri Sesi", callback_data=f"endchat_{session_id}", style=STYLE_DANGER)],
        [InlineKeyboardButton("♻️ Reset Sesi (Stuck)", callback_data=f"resetchat_{session_id}", style=STYLE_DANGER)],
    ])


def active_sessions_keyboard(sessions):
    """Daftar sesi live chat aktif di Menu Pengaturan > "Sesi Live Chat Aktif",
    tiap baris punya tombol reset sendiri -- dipakai admin buat membersihkan
    sesi yang macet/stuck walau pesan header aslinya sudah hilang/ke-scroll."""
    rows = [
        [InlineKeyboardButton(
            f"♻️ Reset #{s['id']} - {s['talent_name']} - {s['full_name']}",
            callback_data=f"resetchat_{s['id']}",
            style=STYLE_DANGER,
        )]
        for s in sessions
    ]
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)


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
        [InlineKeyboardButton("✏️ Ubah Teks Promo Talent", callback_data="settings_promotext", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🖼️ Ubah Background Mini App", callback_data="settings_webappbg", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("📢 Ubah Info Channel 1", callback_data="settings_channel", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("📢 Ubah Info Channel 2", callback_data="settings_channel2", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🎗️ Kelola Sponsor", callback_data="settings_sponsor", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🎪 Aktif/Nonaktifkan Sponsor Melayang", callback_data="settings_togglefloatingsponsor", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("💬 Sesi Live Chat Aktif", callback_data="settings_sessions", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("📸 Channel Testimoni", callback_data="settings_testimoni", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("👥 Kelola Admin Grup", callback_data="settings_admins", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🐹 Animasi Sapaan Grup (Mulai)", callback_data="settings_groupstartmedia", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🛡️ Aktif/Nonaktifkan Proteksi Konten", callback_data="settings_toggleprotectcontent", style=STYLE_PRIMARY)],
    ])


def testimoni_menu_keyboard(channel_configured: bool, autoapprove_enabled: bool, watermark_configured: bool = False, censor_enabled: bool = False):
    """Submenu 'Channel Testimoni' di Menu Pengaturan.

    - Tombol 1: set channel ID (kalau belum ada) atau ganti channel ID
      (kalau sudah ada) -- masuk mode kirim teks lewat settings_testimoni_setchannel.
    - Tombol 2: set/ganti watermark (stiker statis atau gambar) yang ditempel
      otomatis ke tiap foto testimoni -- settings_testimoni_setwatermark.
    - Tombol 3 (hanya kalau watermark sudah ada): hapus watermark.
    - Tombol 4: toggle Sensor Info Sensitif --
        ON  = info di foto (mis. nama pengirim, dll) disensor otomatis,
              KECUALI nama penerima & nominal yang sengaja dipertahankan.
        OFF (default) = foto diposting apa adanya (tanpa disensor).
    - Tombol 5: toggle mode Autoapprove --
        ON  = foto langsung diposting ke channel testimoni tanpa approve admin.
        OFF (default) = admin harus klik "✅ Approve ke Channel Testimoni" dulu
              di grup live chat.
    - Tombol hapus channel hanya muncul kalau channel sudah diatur.
    Lihat testimoni_approve_callback, _prepare_testimoni_photo, &
    _redact_sensitive_regions di bot.py."""
    watermark_label = "✏️ Ganti Watermark" if watermark_configured else "🖼️ Set Watermark"
    censor_label = f"🔒 Sensor Info Sensitif: {'ON' if censor_enabled else 'OFF'}"
    autoapprove_label = f"🤖 Autoapprove: {'ON' if autoapprove_enabled else 'OFF'}"

    rows = [
        [InlineKeyboardButton(
            "✏️ Ganti Channel ID" if channel_configured else "➕ Set Channel ID",
            callback_data="settings_testimoni_setchannel", style=STYLE_PRIMARY,
        )],
        [InlineKeyboardButton(watermark_label, callback_data="settings_testimoni_setwatermark", style=STYLE_PRIMARY)],
    ]
    if watermark_configured:
        rows.append([InlineKeyboardButton("🗑️ Hapus Watermark", callback_data="settings_testimoni_removewatermark", style=STYLE_DANGER)])
    rows.append([InlineKeyboardButton(censor_label, callback_data="settings_testimoni_togglecensor", style=STYLE_SUCCESS if censor_enabled else STYLE_PRIMARY)])
    rows.append([InlineKeyboardButton(autoapprove_label, callback_data="settings_testimoni_toggleautoapprove", style=STYLE_SUCCESS if autoapprove_enabled else STYLE_PRIMARY)])
    if channel_configured:
        rows.append([InlineKeyboardButton("🗑️ Hapus Channel", callback_data="settings_testimoni_removechannel", style=STYLE_DANGER)])
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def group_admins_menu_keyboard():
    """Submenu 'Kelola Admin Grup' -- tambah admin baru atau lihat/hapus
    yang sudah ada. Kartu-kartu ini yang tampil di halaman 'Admin Grup' Mini App."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Admin", callback_data="settings_addadmin", style=STYLE_SUCCESS)],
        [InlineKeyboardButton("📋 Daftar & Hapus Admin", callback_data="settings_listadmins", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_back", style=STYLE_PRIMARY)],
    ])


def group_admins_list_keyboard(admins):
    """Daftar admin grup -- tiap admin punya dua tombol berdampingan: ✏️ Edit
    (buka menu pilih field, lihat edit_group_admin_field_keyboard) dan 🗑
    Hapus langsung (tetap dipertahankan supaya hapus cepat tidak perlu 2 tap)."""
    rows = []
    for a in admins:
        label = a["full_name"] or a["username"] or str(a["user_id"])
        rows.append([
            InlineKeyboardButton(f"✏️ {label}", callback_data=f"editadmin_{a['id']}", style=STYLE_PRIMARY),
            InlineKeyboardButton("🗑", callback_data=f"deladmin_{a['id']}", style=STYLE_DANGER),
        ])
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data="settings_admins", style=STYLE_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def edit_group_admin_field_keyboard(admin):
    """Pilih field kartu admin grup yang ingin diedit satu-per-satu -- pola
    yang sama seperti edit_talent_field_keyboard/edit_sponsor_field_keyboard."""
    admin_id = admin["id"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Username", callback_data=f"editadminfield_{admin_id}_username", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("📝 Nama Lengkap", callback_data=f"editadminfield_{admin_id}_full_name", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("💼 Jabatan", callback_data=f"editadminfield_{admin_id}_jabatan", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🖼️ Foto", callback_data=f"editadminfield_{admin_id}_photo_file_id", style=STYLE_PRIMARY)],
        [InlineKeyboardButton("🗑️ Hapus Admin Ini", callback_data=f"deladmin_{admin_id}", style=STYLE_DANGER)],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="settings_listadmins", style=STYLE_PRIMARY)],
    ])


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
