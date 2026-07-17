import asyncio
import json
import logging

from telegram import Update, BotCommand, BotCommandScopeChat
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.ext.filters import MessageFilter

import config
import database as db
import keyboards as kb
from api_server import run_api_server

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- States untuk ConversationHandler ----------
(
    ADD_NAME, ADD_DESC, ADD_PRICELIST, ADD_PORTFOLIO, ADD_PHOTO,
    EDIT_GREETING, EDIT_HOWTOORDER, EDIT_WEBAPP_BG,
    EDIT_CHANNEL_PHOTO, EDIT_CHANNEL_DESC, EDIT_CHANNEL_URL,
    ADD_SPONSOR_PHOTO, ADD_SPONSOR_NAME, ADD_SPONSOR_DESC, ADD_SPONSOR_URL,
    EDIT_TALENT_VALUE, EDIT_SPONSOR_VALUE,
    EDIT_CHANNEL2_PHOTO, EDIT_CHANNEL2_DESC, EDIT_CHANNEL2_URL,
) = range(20)

# Label ramah-manusia untuk tiap field talent/sponsor yang bisa diedit,
# dipakai di pesan konfirmasi setelah admin berhasil mengubah suatu field.
TALENT_FIELD_LABELS = {
    "name": "Nama",
    "description": "Deskripsi",
    "pricelist": "Pricelist",
    "portfolio_url": "Link channel",
    "photo_file_id": "Foto",
}
SPONSOR_FIELD_LABELS = {
    "name": "Nama",
    "description": "Deskripsi",
    "url": "Link",
    "photo_file_id": "Foto",
}


def is_admin(user_id):
    return user_id in config.ADMIN_IDS


# Jeda singkat (detik) sebelum bot mengirim balasan, dipakai bareng
# send_chat_action(TYPING) supaya user melihat indikator "sedang mengetik..."
# sekilas -- mirip animasi "thinking" pada chat AI -- sebelum jawaban muncul.
TYPING_DELAY = 0.6


async def send_typing(context: ContextTypes.DEFAULT_TYPE, chat_id, delay: float = TYPING_DELAY):
    """Kirim indikator 'sedang mengetik...' ke `chat_id`, lalu jeda sebentar
    sebelum kode pemanggil mengirim pesan/foto balasannya. Dibungkus try/except
    supaya kalau gagal (mis. user memblokir bot) alur utama tidak ikut gagal."""
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        logger.debug("Gagal mengirim chat action 'typing' (diabaikan).")
    if delay:
        await asyncio.sleep(delay)


class WebAppActionFilter(MessageFilter):
    """Filter pesan `web_app_data` berdasarkan isi field `action` di payload JSON-nya,
    supaya aksi 'lihat talent' dan 'chat sekarang' dari Mini App bisa ditangani
    oleh handler yang berbeda tanpa saling rebutan update."""

    def __init__(self, action):
        super().__init__(name=f"WebAppAction({action})")
        self.action = action

    def filter(self, message):
        try:
            payload = json.loads(message.web_app_data.data)
        except (ValueError, AttributeError, TypeError):
            return False
        return payload.get("action") == self.action


webapp_view_talent_filter = WebAppActionFilter("view_talent")
webapp_chat_talent_filter = WebAppActionFilter("start_chat")


class AdminReplyFilter(MessageFilter):
    """Cocok untuk pesan APA SAJA dari admin yang merupakan reply (balasan)
    ke pesan lain -- dipakai untuk mendeteksi balasan admin ke pesan live chat
    yang diteruskan bot, baik itu terjadi di grup live chat maupun di private
    chat masing-masing admin."""

    def __init__(self):
        super().__init__(name="AdminReply")

    def filter(self, message):
        return bool(message.reply_to_message) and bool(message.from_user) and is_admin(message.from_user.id)


admin_reply_filter = AdminReplyFilter()


async def delete_prev_message(query, context=None):
    """Hapus pesan sebelumnya (yang berisi tombol) setiap kali user menekan tombol,
    supaya histori chat tetap bersih dan tidak menumpuk pesan lama.

    Beberapa alur (mis. pricelist dengan teks panjang) mengirim foto sebagai
    pesan TERPISAH tanpa tombol. Pesan foto "tambahan" itu dicatat di
    context.user_data["extra_msg_to_delete"] supaya ikut dihapus di sini,
    alih-alih tertinggal/nyangkut selamanya di chat setiap kali user
    menekan tombol lain.
    """
    if context is not None:
        extra = context.user_data.pop("extra_msg_to_delete", None)
        if extra:
            try:
                await context.bot.delete_message(chat_id=extra[0], message_id=extra[1])
            except Exception:
                logger.warning("Gagal menghapus pesan foto tambahan (mungkin sudah dihapus / terlalu lama).")
    try:
        await query.message.delete()
    except Exception:
        logger.warning("Gagal menghapus pesan sebelumnya (mungkin sudah dihapus / terlalu lama).")


async def replace_message(query, context, text, reply_markup=None, parse_mode=None, photo=None):
    """Pengganti pola `query.edit_message_text(...)`: hapus pesan lama lalu kirim
    pesan baru sebagai gantinya, supaya perilakunya konsisten dengan tombol lain.
    Kalau `photo` diisi (file_id), pesan baru dikirim sebagai foto dengan `text`
    sebagai caption-nya."""
    await delete_prev_message(query, context)
    chat_id = query.message.chat_id
    await send_typing(context, chat_id)
    if photo:
        return await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    return await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )


def build_greeting_text():
    """Ambil teks sapaan tersimpan lalu isi placeholder {bot_name} dan
    {total_talent} (jumlah talent yang ada saat ini di daftar talent)."""
    total_talent = len(db.list_talents())
    template = db.get_setting("greeting", config.DEFAULT_GREETING)
    return template.format(bot_name=config.BOT_NAME, total_talent=total_talent)


# ==================== BANTUAN & TENTANG ====================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help -- daftar perintah & cara pakai bot. Isinya menyesuaikan
    otomatis: admin melihat daftar perintah admin tambahan, user biasa tidak."""
    await send_typing(context, update.effective_chat.id)

    lines = [
        f"🆘 *Bantuan {config.BOT_NAME}*",
        "",
        "*Perintah:*",
        "/start — Buka menu utama & sapaan",
        "/help — Tampilkan pesan bantuan ini",
        "/about — Info tentang bot ini",
        "",
        "*Cara pakai:*",
        "1️⃣ Tekan /start untuk membuka menu utama.",
        "2️⃣ Tekan *💃 Pilih Talent* untuk melihat daftar talent (atau buka tampilan "
        "Mini App kalau tersedia, untuk pengalaman yang lebih lengkap).",
        "3️⃣ Pilih salah satu talent untuk melihat profil, pricelist, dan link channelnya.",
        "4️⃣ Tekan *💬 Chat Sekarang* untuk terhubung langsung dengan admin.",
        "5️⃣ Ketik kebutuhanmu di chat itu juga — admin akan membalas langsung, "
        "dan sesi akan diakhiri otomatis oleh admin setelah topik selesai.",
    ]

    if is_admin(update.effective_user.id):
        lines += [
            "",
            "*Perintah khusus admin:*",
            "/settings — Buka menu pengaturan bot (kelola talent, sponsor, sapaan, "
            "cara order, background, channel, dll)",
            "/groupid — Tampilkan ID chat/grup ini (dipakai untuk setting `LIVECHAT_GROUP_ID`)",
            "/cancel — Batalkan proses input yang sedang berjalan di menu /settings",
        ]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/about -- info singkat tentang bot ini: apa fungsinya, statistik singkat,
    dan kontak developer kalau ada kendala."""
    await send_typing(context, update.effective_chat.id)

    total_talent = len(db.list_talents())
    lines = [
        f"ℹ️ *Tentang {config.BOT_NAME}*",
        "",
        "Bot order talent yang membantu kamu menemukan, melihat profil, dan "
        "langsung terhubung lewat live chat dengan talent pilihanmu — semua lewat Telegram.",
        "",
        f"✦ Saat ini tersedia *{total_talent}* talent",
        "✦ Live chat langsung dengan admin, tanpa perlu pindah aplikasi",
    ]
    if config.WEBAPP_URL:
        lines.append("✦ Tersedia tampilan Mini App untuk pengalaman yang lebih lengkap")
    lines += [
        "",
        f"Ada pertanyaan atau kendala? Hubungi developer: {config.DEVELOPER_CHAT_URL}",
    ]

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True,
    )


# ==================== START & MENU ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_typing(context, update.effective_chat.id)
    greeting_text = build_greeting_text()
    greeting_photo = db.get_setting("greeting_photo")
    if greeting_photo:
        await update.message.reply_photo(
            photo=greeting_photo,
            caption=greeting_text,
            reply_markup=kb.main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(greeting_text, reply_markup=kb.main_menu_keyboard())

    if config.WEBAPP_URL:
        await update.message.reply_text(
            "Atau lihat katalog dalam tampilan app:",
            reply_markup=kb.webapp_launch_keyboard(config.WEBAPP_URL),
        )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "menu_noop":
        # Tombol indikator halaman (mis. "2/3"), tidak melakukan apa-apa.
        return

    if query.data == "menu_talents" or query.data.startswith("menu_talents_i"):
        index = 0
        if query.data.startswith("menu_talents_i"):
            try:
                index = int(query.data[len("menu_talents_i"):])
            except ValueError:
                index = 0
        talents = db.list_talents()
        if not talents:
            await replace_message(
                query, context,
                "Belum ada talent yang ditambahkan.",
                reply_markup=kb.main_menu_keyboard(),
            )
            return
        await show_talent_card(query, context, talents, index)

    elif query.data == "menu_howtoorder":
        text = db.get_setting("how_to_order", config.DEFAULT_HOW_TO_ORDER)
        await replace_message(query, context, text, reply_markup=kb.main_menu_keyboard())

    elif query.data == "menu_back":
        greeting_text = build_greeting_text()
        greeting_photo = db.get_setting("greeting_photo")
        await replace_message(query, context, greeting_text, reply_markup=kb.main_menu_keyboard(), photo=greeting_photo)


async def show_talent_card(query, context, talents, index):
    """Tampilkan 1 kartu talent (foto + tombol nama) pada satu waktu, dengan
    tombol Sebelumnya/Selanjutnya untuk pindah antar talent satu-satu -- jadi
    berasa seperti "geser halaman" alih-alih daftar tombol nama yang panjang."""
    total = len(talents)
    index = max(0, min(index, total - 1))
    talent = talents[index]

    caption = f"*{talent['name']}*"
    reply_markup = kb.talent_carousel_keyboard(talent, index, total)

    await delete_prev_message(query, context)
    chat_id = query.message.chat_id
    await send_typing(context, chat_id)
    if talent.get("photo_file_id"):
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=talent["photo_file_id"],
            caption=caption,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )


async def show_talent_detail(context: ContextTypes.DEFAULT_TYPE, chat_id, talent):
    """Kirim halaman detail talent (foto+deskripsi+tombol) ke chat_id tertentu.
    Dipakai baik dari tombol chat biasa maupun dari data yang dikirim Mini App."""
    caption = f"*{talent['name']}*\n\n{talent['description']}"
    await send_typing(context, chat_id)
    if talent.get("photo_file_id"):
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=talent["photo_file_id"],
            caption=caption,
            parse_mode="Markdown",
            reply_markup=kb.talent_detail_keyboard(talent),
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="Markdown",
            reply_markup=kb.talent_detail_keyboard(talent),
        )


async def talent_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talent_id = int(query.data.split("_")[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await replace_message(query, context, "Talent tidak ditemukan.", reply_markup=kb.main_menu_keyboard())
        return

    chat_id = query.message.chat_id
    await delete_prev_message(query, context)
    await show_talent_detail(context, chat_id, talent)


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima data dari Mini App (index.html) saat user tap kartu talent."""
    try:
        payload = json.loads(update.effective_message.web_app_data.data)
    except (ValueError, AttributeError):
        return

    if payload.get("action") == "view_talent":
        talent = db.get_talent(int(payload["talent_id"]))
        if talent:
            await show_talent_detail(context, update.effective_chat.id, talent)


async def chat_start_from_webapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point live chat saat user menekan tombol 'Chat Sekarang' di Mini App.
    Mini App akan menutup diri (tg.close()) dan mengirim data ini, lalu bot
    langsung membuka sesi live chat dengan admin di chat seperti biasa."""
    try:
        payload = json.loads(update.effective_message.web_app_data.data)
        talent_id = int(payload["talent_id"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return

    talent = db.get_talent(talent_id)
    if not talent:
        await update.effective_message.reply_text(
            "Talent tidak ditemukan.", reply_markup=kb.main_menu_keyboard()
        )
        return

    await start_chat_session(context, update.effective_chat.id, update.effective_user, talent)


async def pricelist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talent_id = int(query.data.split("_")[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await query.answer("Talent tidak ditemukan.", show_alert=True)
        return
    text = f"💰 *Pricelist - {talent['name']}*\n\n{talent['pricelist']}"
    chat_id = query.message.chat_id
    await delete_prev_message(query, context)
    await send_typing(context, chat_id)
    if talent.get("photo_file_id") and len(text) <= 1024:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=talent["photo_file_id"],
            caption=text,
            parse_mode="Markdown",
            reply_markup=kb.back_to_talent_keyboard(talent_id),
        )
    elif talent.get("photo_file_id"):
        # Caption Telegram maksimal 1024 karakter -> kirim foto polos,
        # lalu teks pricelist lengkap sebagai pesan terpisah.
        # Foto ini tidak punya tombol sendiri, jadi ID-nya dicatat supaya
        # ikut terhapus otomatis saat user menekan tombol lain nanti
        # (mis. "Kembali"/"Chat Sekarang" di pesan teks) -- tanpa ini foto
        # akan nyangkut/tertinggal selamanya di chat.
        photo_msg = await context.bot.send_photo(chat_id=chat_id, photo=talent["photo_file_id"])
        context.user_data["extra_msg_to_delete"] = (chat_id, photo_msg.message_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb.back_to_talent_keyboard(talent_id),
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb.back_to_talent_keyboard(talent_id),
        )


# ==================== LIVE CHAT (chat sekarang, dua arah, relay ke admin) ====================
#
# Alur:
# 1. User menekan "Chat Sekarang" -> sesi baru dibuat di DB (status 'active'),
#    header sesi dikirim ke grup live chat (atau ke tiap admin secara private
#    kalau grup tidak dikonfigurasi) lengkap dengan tombol "Akhiri Sesi".
# 2. Setiap pesan yang dikirim user selama sesi aktif diteruskan (copy_message)
#    ke tujuan admin tsb, dan message_id hasil copy-nya dipetakan ke sesi ini
#    (tabel chat_relay) supaya admin bisa reply pesan spesifik itu.
# 3. Ketika admin me-reply pesan mana pun yang sudah dipetakan ke sebuah sesi
#    (baik reply ke header maupun ke pesan user yang diteruskan), balasannya
#    di-copy balik ke user tsb.
# 4. Admin mengakhiri sesi lewat tombol "Akhiri Sesi" -> status sesi jadi
#    'ended', user diberi tahu, dan pesan header diupdate.

async def broadcast_to_admin_targets(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """Kirim `text` ke grup live chat (kalau LIVECHAT_GROUP_ID diisi), atau ke
    masing-masing admin secara private kalau tidak. Balikin daftar
    (chat_id, message_id) yang berhasil terkirim, supaya balasan admin bisa
    dipetakan kembali ke sesi live chat yang benar."""
    sent = []
    if config.LIVECHAT_GROUP_ID:
        try:
            msg = await context.bot.send_message(
                chat_id=int(config.LIVECHAT_GROUP_ID), text=text,
                parse_mode="Markdown", reply_markup=reply_markup,
            )
            sent.append((msg.chat_id, msg.message_id))
            return sent
        except Exception:
            logger.exception("Gagal kirim live chat ke LIVECHAT_GROUP_ID, fallback ke admin satu-satu.")

    for admin_id in config.ADMIN_IDS:
        try:
            msg = await context.bot.send_message(
                chat_id=admin_id, text=text, parse_mode="Markdown", reply_markup=reply_markup,
            )
            sent.append((msg.chat_id, msg.message_id))
        except Exception:
            logger.exception(f"Gagal kirim live chat ke admin {admin_id}")
    return sent


async def broadcast_copy_to_admin_targets(context: ContextTypes.DEFAULT_TYPE, from_chat_id: int, message_id: int):
    """Teruskan (copy_message) pesan user apa adanya -- teks/foto/video/voice/dsb --
    ke tujuan admin. Balikin daftar (chat_id, message_id) hasil salinannya."""
    sent = []
    if config.LIVECHAT_GROUP_ID:
        try:
            group_id = int(config.LIVECHAT_GROUP_ID)
            copied = await context.bot.copy_message(
                chat_id=group_id, from_chat_id=from_chat_id, message_id=message_id,
            )
            sent.append((group_id, copied.message_id))
            return sent
        except Exception:
            logger.exception("Gagal teruskan pesan user ke LIVECHAT_GROUP_ID, fallback ke admin satu-satu.")

    for admin_id in config.ADMIN_IDS:
        try:
            copied = await context.bot.copy_message(
                chat_id=admin_id, from_chat_id=from_chat_id, message_id=message_id,
            )
            sent.append((admin_id, copied.message_id))
        except Exception:
            logger.exception(f"Gagal teruskan pesan user ke admin {admin_id}")
    return sent


async def start_chat_session(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, talent=None):
    """Mulai (atau lanjutkan) sesi live chat untuk `user` terkait `talent`."""
    existing = db.get_active_session_for_user(user.id)
    if existing:
        await send_typing(context, chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"💬 Anda masih memiliki sesi live chat aktif (mengenai *{existing['talent_name']}*).\n\n"
                 f"Silakan lanjutkan ketik pesan Anda di sini, admin akan membalas langsung.",
            parse_mode="Markdown",
        )
        return

    talent_name = talent["name"] if talent else "-"
    talent_id = talent["id"] if talent else None

    session_id = db.create_chat_session(
        user_id=user.id,
        username=user.username or "-",
        full_name=user.full_name,
        talent_id=talent_id,
        talent_name=talent_name,
    )

    header = (
        f"💬 *Live Chat Baru #{session_id}*\n\n"
        f"Talent: {talent_name}\n"
        f"Dari: {user.full_name} (@{user.username or '-'}, ID: `{user.id}`)\n\n"
        f"_Balas (reply) pesan yang diteruskan dari user ini untuk membalas langsung. "
        f"Tekan tombol di bawah untuk mengakhiri sesi kalau topik sudah selesai._"
    )
    sent_targets = await broadcast_to_admin_targets(context, header, reply_markup=kb.end_chat_keyboard(session_id))
    for admin_chat_id, message_id in sent_targets:
        db.add_relay_mapping(message_id, admin_chat_id, session_id)

    await send_typing(context, chat_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Anda terhubung langsung dengan admin mengenai *{talent_name}*.\n\n"
             f"Silakan ketik pesan Anda sekarang, admin akan membalas langsung di chat ini.\n"
             f"_Sesi ini akan diakhiri oleh admin setelah topik selesai dibahas._",
        parse_mode="Markdown",
    )


async def chat_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point saat user menekan tombol 'Chat Sekarang' di chat biasa."""
    query = update.callback_query
    await query.answer()
    talent_id = int(query.data.split("_")[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await query.answer("Talent tidak ditemukan.", show_alert=True)
        return

    chat_id = query.message.chat_id
    await delete_prev_message(query, context)
    await start_chat_session(context, chat_id, query.from_user, talent)


async def relay_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teruskan pesan dari user (yang punya sesi live chat aktif) ke admin."""
    user = update.effective_user
    if is_admin(user.id):
        return

    session = db.get_active_session_for_user(user.id)
    if not session:
        # Tidak ada sesi live chat aktif -> abaikan, tidak ada yang perlu diteruskan.
        return

    targets = await broadcast_copy_to_admin_targets(
        context, from_chat_id=update.effective_chat.id, message_id=update.effective_message.message_id,
    )
    for admin_chat_id, message_id in targets:
        db.add_relay_mapping(message_id, admin_chat_id, session["id"])

    # Tampilkan indikator "sedang mengetik..." sekilas ke user setelah pesannya
    # diteruskan, supaya terasa ada respons langsung selagi menunggu balasan admin
    # (animasi "thinking" khas chat AI), meski balasan sesungguhnya baru datang
    # setelah admin membalas.
    await send_typing(context, update.effective_chat.id)


async def relay_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teruskan balasan admin (reply ke pesan live chat yang diteruskan) ke user terkait."""
    message = update.effective_message
    session_id = db.get_session_id_by_relay(message.reply_to_message.message_id, update.effective_chat.id)
    if not session_id:
        return

    session = db.get_session(session_id)
    if not session or session["status"] != "active":
        await message.reply_text("⚠️ Sesi live chat ini sudah diakhiri, balasan tidak diteruskan.")
        return

    try:
        await send_typing(context, session["user_id"])
        await context.bot.copy_message(
            chat_id=session["user_id"], from_chat_id=update.effective_chat.id, message_id=message.message_id,
        )
    except Exception:
        logger.exception("Gagal meneruskan balasan admin ke user")
        await message.reply_text("❌ Gagal mengirim balasan ke user (mungkin user memblokir bot).")


async def end_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin menekan tombol 'Akhiri Sesi' -> tutup sesi live chat."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Khusus admin.", show_alert=True)
        return
    await query.answer()

    session_id = int(query.data.split("_")[1])
    session = db.get_session(session_id)
    if not session:
        await query.answer("Sesi tidak ditemukan.", show_alert=True)
        return

    if session["status"] == "active":
        db.end_session(session_id)
        try:
            await context.bot.send_message(
                chat_id=session["user_id"],
                text="✅ Sesi live chat ini telah *diakhiri oleh admin*. Terima kasih!\n\n"
                     "Kalau ada pertanyaan lain, silakan tekan tombol \"💬 Chat Sekarang\" lagi.",
                parse_mode="Markdown",
                reply_markup=kb.main_menu_keyboard(),
            )
        except Exception:
            logger.exception("Gagal memberi tahu user bahwa sesi live chat diakhiri")

    try:
        base_text = query.message.text or ""
        await query.edit_message_text(
            text=base_text + "\n\n🔴 *Sesi telah diakhiri.*",
            parse_mode="Markdown",
            reply_markup=None,
        )
    except Exception:
        logger.warning("Gagal update pesan header sesi live chat (mungkin sudah diedit sebelumnya).")


# ==================== /settings ====================

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⚙️ Menu Pengaturan", reply_markup=kb.settings_menu_keyboard())


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Khusus admin.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    if query.data == "settings_back":
        await replace_message(query, context, "⚙️ Menu Pengaturan", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    if query.data == "settings_listtalent":
        talents = db.list_talents()
        if not talents:
            text = "Belum ada talent."
        else:
            text = "📋 *Daftar Talent:*\n\n" + "\n".join(
                f"• {t['name']} (ID: {t['id']})" for t in talents
            )
        await replace_message(query, context, text, parse_mode="Markdown", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    if query.data == "settings_sessions":
        sessions = db.list_active_sessions()
        if not sessions:
            text = "Tidak ada sesi live chat yang aktif saat ini."
        else:
            lines = ["💬 *Sesi Live Chat Aktif:*\n"]
            for s in sessions:
                lines.append(
                    f"#{s['id']} - {s['talent_name']} - {s['full_name']} (@{s['username']})"
                )
            text = "\n".join(lines)
        await replace_message(query, context, text, parse_mode="Markdown", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    if query.data == "settings_deltalent":
        talents = db.list_talents()
        if not talents:
            await replace_message(query, context, "Belum ada talent untuk dihapus.", reply_markup=kb.settings_menu_keyboard())
            return ConversationHandler.END
        await replace_message(query, context, "Pilih talent yang ingin dihapus:", reply_markup=kb.delete_talent_keyboard(talents))
        return ConversationHandler.END

    if query.data.startswith("delconfirm_"):
        talent_id = int(query.data.split("_")[1])
        db.delete_talent(talent_id)
        await replace_message(query, context, "✅ Talent dihapus.", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    if query.data == "settings_edittalent":
        talents = db.list_talents()
        if not talents:
            await replace_message(query, context, "Belum ada talent untuk diedit.", reply_markup=kb.settings_menu_keyboard())
            return ConversationHandler.END
        await replace_message(query, context, "Pilih talent yang ingin diedit:", reply_markup=kb.edit_talent_list_keyboard(talents))
        return ConversationHandler.END

    if query.data.startswith("edittalentfield_"):
        _, talent_id_str, field = query.data.split("_", 2)
        talent_id = int(talent_id_str)
        talent = db.get_talent(talent_id)
        if not talent:
            await replace_message(query, context, "Talent tidak ditemukan (mungkin sudah dihapus).", reply_markup=kb.settings_menu_keyboard())
            return ConversationHandler.END
        context.user_data["edit_talent"] = {"id": talent_id, "field": field}
        prompts = {
            "name": "Kirim *nama* baru untuk talent ini:",
            "description": "Kirim *deskripsi* baru untuk talent ini:",
            "pricelist": "Kirim *pricelist* baru (boleh multi-baris):",
            "portfolio_url": "Kirim *link channel Telegram* baru\n"
                              "(contoh: `https://t.me/namachannel` atau `@namachannel`, atau ketik `-` untuk mengosongkan):",
            "photo_file_id": "Kirim *foto* baru untuk talent ini:",
        }
        await replace_message(
            query, context, prompts[field], parse_mode="Markdown",
            reply_markup=kb.addtalent_step_keyboard(back_callback=f"edittalent_{talent_id}"),
        )
        return EDIT_TALENT_VALUE

    if query.data.startswith("edittalent_"):
        talent_id = int(query.data.split("_")[1])
        talent = db.get_talent(talent_id)
        if not talent:
            await replace_message(query, context, "Talent tidak ditemukan (mungkin sudah dihapus).", reply_markup=kb.settings_menu_keyboard())
            return ConversationHandler.END
        text = (
            f"✏️ *Edit Talent: {talent['name']}*\n\n"
            f"Nama: {talent['name']}\n"
            f"Deskripsi: {talent['description']}\n"
            f"Pricelist: {talent['pricelist']}\n"
            f"Link Channel: {talent.get('portfolio_url') or '-'}\n\n"
            "Pilih bagian yang ingin diubah:"
        )
        await replace_message(query, context, text, parse_mode="Markdown", reply_markup=kb.edit_talent_field_keyboard(talent))
        return ConversationHandler.END

    if query.data == "settings_addtalent":
        context.user_data["new_talent"] = {}
        await replace_message(
            query, context,
            "Masukkan *nama* talent:",
            parse_mode="Markdown",
            reply_markup=kb.addtalent_step_keyboard(),
        )
        return ADD_NAME

    if query.data == "settings_greeting":
        current_text = db.get_setting("greeting", config.DEFAULT_GREETING)
        has_photo = bool(db.get_setting("greeting_photo"))
        text = (
            "✏️ *Sapaan (/start) saat ini:*\n\n"
            f"{current_text}\n\n"
            f"🖼️ Foto sapaan: {'terpasang' if has_photo else 'tidak ada'}\n\n"
            "Tekan *Edit* untuk mengubahnya."
        )
        await replace_message(
            query, context, text, parse_mode="Markdown",
            reply_markup=kb.preview_edit_keyboard("settings_greeting_edit"),
        )
        return ConversationHandler.END

    if query.data == "settings_greeting_edit":
        await replace_message(
            query, context,
            "Kirim *teks* sapaan baru untuk /start.\n"
            "Gunakan `{bot_name}` untuk nama bot dan `{total_talent}` untuk menampilkan "
            "total talent yang ada di daftar talent.\n\n"
            "Kamu juga bisa kirim *foto* (boleh disertai caption sebagai teks sapaan sekaligus) "
            "untuk memasang foto pada pesan sapaan.\n"
            "Ketik `hapus foto` untuk menghapus foto sapaan yang sudah terpasang.",
            parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard(),
        )
        return EDIT_GREETING

    if query.data == "settings_howtoorder":
        current_text = db.get_setting("how_to_order", config.DEFAULT_HOW_TO_ORDER)
        text = (
            "✏️ *Teks \"Cara Order\" saat ini:*\n\n"
            f"{current_text}\n\n"
            "Tekan *Edit* untuk mengubahnya."
        )
        await replace_message(
            query, context, text, parse_mode="Markdown",
            reply_markup=kb.preview_edit_keyboard("settings_howtoorder_edit"),
        )
        return ConversationHandler.END

    if query.data == "settings_howtoorder_edit":
        await replace_message(
            query, context,
            "Kirim teks baru untuk halaman \"Cara Order\":",
            reply_markup=kb.back_to_settings_keyboard(),
        )
        return EDIT_HOWTOORDER

    if query.data == "settings_webappbg":
        await replace_message(
            query, context,
            "Kirim *foto* untuk dijadikan background Mini App.\n"
            "Ketik `hapus background` untuk menghapus background yang sudah terpasang "
            "(Mini App kembali pakai warna polos bawaan).",
            parse_mode="Markdown",
        )
        return EDIT_WEBAPP_BG

    if query.data == "settings_channel":
        await replace_message(
            query, context,
            "*Ubah Info Channel* (tampil di menu utama Mini App)\n\n"
            "Langkah 1/3 — Kirim *foto* channel, ketik `-` untuk lewati (biarkan seperti sekarang), "
            "atau ketik `hapus` untuk menghapus foto yang sudah ada.",
            parse_mode="Markdown",
            reply_markup=kb.addtalent_step_keyboard(),
        )
        return EDIT_CHANNEL_PHOTO

    if query.data == "settings_sponsor":
        await replace_message(query, context, "🎗️ Kelola Sponsor", reply_markup=kb.sponsor_menu_keyboard())
        return ConversationHandler.END

    if query.data == "sponsor_list":
        sponsors = db.list_sponsors()
        if not sponsors:
            text = "Belum ada sponsor."
        else:
            lines = [f"• {s['name'] or ('Sponsor #' + str(s['id']))} (ID: {s['id']})" for s in sponsors]
            text = "📋 *Daftar Sponsor:*\n\n" + "\n".join(lines)
        await replace_message(query, context, text, parse_mode="Markdown", reply_markup=kb.sponsor_menu_keyboard())
        return ConversationHandler.END

    if query.data == "sponsor_add":
        context.user_data["new_sponsor"] = {}
        await replace_message(
            query, context,
            "Kirim *foto* logo/banner sponsor:",
            parse_mode="Markdown",
            reply_markup=kb.addtalent_step_keyboard(),
        )
        return ADD_SPONSOR_PHOTO

    if query.data == "sponsor_del":
        sponsors = db.list_sponsors()
        if not sponsors:
            await replace_message(query, context, "Belum ada sponsor untuk dihapus.", reply_markup=kb.sponsor_menu_keyboard())
            return ConversationHandler.END
        await replace_message(query, context, "Pilih sponsor yang ingin dihapus:", reply_markup=kb.delete_sponsor_keyboard(sponsors))
        return ConversationHandler.END

    if query.data.startswith("sponsordelconfirm_"):
        sponsor_id = int(query.data.split("_")[1])
        db.delete_sponsor(sponsor_id)
        await replace_message(query, context, "✅ Sponsor dihapus.", reply_markup=kb.sponsor_menu_keyboard())
        return ConversationHandler.END

    if query.data == "settings_editsponsor":
        sponsors = db.list_sponsors()
        if not sponsors:
            await replace_message(query, context, "Belum ada sponsor untuk diedit.", reply_markup=kb.sponsor_menu_keyboard())
            return ConversationHandler.END
        await replace_message(query, context, "Pilih sponsor yang ingin diedit:", reply_markup=kb.edit_sponsor_list_keyboard(sponsors))
        return ConversationHandler.END

    if query.data.startswith("editsponsorfield_"):
        _, sponsor_id_str, field = query.data.split("_", 2)
        sponsor_id = int(sponsor_id_str)
        sponsor = db.get_sponsor(sponsor_id)
        if not sponsor:
            await replace_message(query, context, "Sponsor tidak ditemukan (mungkin sudah dihapus).", reply_markup=kb.sponsor_menu_keyboard())
            return ConversationHandler.END
        context.user_data["edit_sponsor"] = {"id": sponsor_id, "field": field}
        prompts = {
            "name": "Kirim *nama* baru untuk sponsor ini (ketik `-` untuk mengosongkan):",
            "description": "Kirim *deskripsi* baru untuk sponsor ini (ketik `-` untuk mengosongkan):",
            "url": "Kirim *link* baru untuk sponsor ini (ketik `-` untuk mengosongkan):",
            "photo_file_id": "Kirim *foto* baru untuk sponsor ini:",
        }
        await replace_message(
            query, context, prompts[field], parse_mode="Markdown",
            reply_markup=kb.addtalent_step_keyboard(back_callback=f"editsponsor_{sponsor_id}"),
        )
        return EDIT_SPONSOR_VALUE

    if query.data.startswith("editsponsor_"):
        sponsor_id = int(query.data.split("_")[1])
        sponsor = db.get_sponsor(sponsor_id)
        if not sponsor:
            await replace_message(query, context, "Sponsor tidak ditemukan (mungkin sudah dihapus).", reply_markup=kb.sponsor_menu_keyboard())
            return ConversationHandler.END
        text = (
            f"✏️ *Edit Sponsor: {sponsor['name'] or ('Sponsor #' + str(sponsor['id']))}*\n\n"
            f"Nama: {sponsor.get('name') or '-'}\n"
            f"Deskripsi: {sponsor.get('description') or '-'}\n"
            f"Link: {sponsor.get('url') or '-'}\n\n"
            "Pilih bagian yang ingin diubah:"
        )
        await replace_message(query, context, text, parse_mode="Markdown", reply_markup=kb.edit_sponsor_field_keyboard(sponsor))
        return ConversationHandler.END

    if query.data == "settings_channel2":
        await replace_message(
            query, context,
            "*Ubah Info Channel 2* (slot channel/grup kedua di menu utama Mini App)\n\n"
            "Langkah 1/3 — Kirim *foto* channel, ketik `-` untuk lewati (biarkan seperti sekarang), "
            "atau ketik `hapus` untuk menghapus foto yang sudah ada.",
            parse_mode="Markdown",
            reply_markup=kb.addtalent_step_keyboard(),
        )
        return EDIT_CHANNEL2_PHOTO

    if query.data == "settings_togglefloatingsponsor":
        current = db.get_setting("floating_sponsor_enabled", "1")
        new_value = "0" if current == "1" else "1"
        db.set_setting("floating_sponsor_enabled", new_value)
        status = (
            "*diaktifkan* ✅ (tampil di halaman utama Mini App)"
            if new_value == "1"
            else "*dinonaktifkan* ❌ (disembunyikan dari halaman utama Mini App)"
        )
        await replace_message(
            query, context,
            f"🎪 Sponsor Melayang telah {status}.",
            parse_mode="Markdown",
            reply_markup=kb.settings_menu_keyboard(),
        )
        return ConversationHandler.END

    return ConversationHandler.END


async def add_talent_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_talent"]["name"] = update.message.text
    await update.message.reply_text(
        "Masukkan *deskripsi* talent:",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(back_callback="back_to_addname"),
    )
    return ADD_DESC


async def add_talent_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_talent"]["description"] = update.message.text
    await update.message.reply_text(
        "Masukkan *pricelist* (boleh multi-baris):",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(back_callback="back_to_adddesc"),
    )
    return ADD_PRICELIST


async def add_talent_pricelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_talent"]["pricelist"] = update.message.text
    await update.message.reply_text(
        "Masukkan *link channel Telegram* talent\n"
        "(contoh: `https://t.me/namachannel` atau `@namachannel`, atau ketik `-` untuk lewati):",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(back_callback="back_to_addpricelist"),
    )
    return ADD_PORTFOLIO


def _normalize_telegram_link(text):
    """Terima format @username atau t.me/username, balikin URL t.me lengkap."""
    text = text.strip()
    if text.startswith("@"):
        return f"https://t.me/{text[1:]}"
    if text.startswith("t.me/"):
        return f"https://{text}"
    return text


async def add_talent_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["new_talent"]["portfolio_url"] = (
        None if text == "-" else _normalize_telegram_link(text)
    )
    await update.message.reply_text(
        "Kirim *foto* talent (atau ketik `-` untuk lewati):",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(back_callback="back_to_addportfolio"),
    )
    return ADD_PHOTO


async def add_talent_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nt = context.user_data["new_talent"]
    photo_file_id = None
    if update.message.photo:
        # Foto terkompresi (cara normal kirim foto di Telegram)
        photo_file_id = update.message.photo[-1].file_id
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        # Foto dikirim sebagai file/dokumen (mis. opsi "Kirim tanpa kompresi")
        photo_file_id = update.message.document.file_id
    elif update.message.text and update.message.text.strip() != "-":
        # Bukan foto, bukan dokumen gambar, dan bukan "-" (skip) -> minta ulang.
        await update.message.reply_text(
            "Itu bukan foto. Kirim *foto* talent, atau ketik `-` untuk lewati.",
            parse_mode="Markdown",
        )
        return ADD_PHOTO
    nt["photo_file_id"] = photo_file_id

    try:
        talent_id = db.add_talent(
            name=nt["name"],
            description=nt["description"],
            pricelist=nt["pricelist"],
            portfolio_url=nt.get("portfolio_url"),
            photo_file_id=nt.get("photo_file_id"),
        )
    except Exception:
        logger.exception("Gagal menyimpan talent baru (add_talent_photo)")
        await update.message.reply_text(
            "❌ Gagal menyimpan talent ke database. Data yang sudah kamu isi *tidak hilang*, "
            "coba kirim ulang foto ini (atau ketik `-` untuk lewati foto). "
            "Kalau masih gagal, hubungi admin bot.",
            parse_mode="Markdown",
        )
        return ADD_PHOTO

    await update.message.reply_text(
        f"✅ Talent *{nt['name']}* berhasil ditambahkan (ID: {talent_id}).",
        parse_mode="Markdown",
        reply_markup=kb.settings_menu_keyboard(),
    )
    context.user_data.pop("new_talent", None)
    return ConversationHandler.END


async def edit_talent_value_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima nilai baru untuk SATU field talent yang sedang diedit (disimpan
    di context.user_data['edit_talent']) lalu simpan ke database dan tampilkan
    lagi menu field talent tersebut supaya admin bisa lanjut edit field lain."""
    info = context.user_data.get("edit_talent")
    message = update.message
    if not info:
        await message.reply_text("Sesi edit sudah tidak valid, silakan buka lagi dari /settings.")
        return ConversationHandler.END

    talent_id, field = info["id"], info["field"]

    if field == "photo_file_id":
        photo_file_id = None
        if message.photo:
            photo_file_id = message.photo[-1].file_id
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            photo_file_id = message.document.file_id
        if not photo_file_id:
            await message.reply_text(
                "Itu bukan foto. Kirim *foto* baru untuk talent ini.", parse_mode="Markdown",
            )
            return EDIT_TALENT_VALUE
        value = photo_file_id
    else:
        text = (message.text or "").strip()
        if not text:
            await message.reply_text("Kirim teks yang valid.")
            return EDIT_TALENT_VALUE
        if field == "portfolio_url":
            value = None if text == "-" else _normalize_telegram_link(text)
        else:
            value = message.text

    db.update_talent_field(talent_id, field, value)
    context.user_data.pop("edit_talent", None)

    talent = db.get_talent(talent_id)
    await message.reply_text(
        f"✅ {TALENT_FIELD_LABELS.get(field, field)} talent *{talent['name']}* berhasil diubah.",
        parse_mode="Markdown",
        reply_markup=kb.edit_talent_field_keyboard(talent),
    )
    return ConversationHandler.END


async def edit_sponsor_value_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima nilai baru untuk SATU field sponsor yang sedang diedit (disimpan
    di context.user_data['edit_sponsor']) lalu simpan ke database dan tampilkan
    lagi menu field sponsor tersebut supaya admin bisa lanjut edit field lain."""
    info = context.user_data.get("edit_sponsor")
    message = update.message
    if not info:
        await message.reply_text("Sesi edit sudah tidak valid, silakan buka lagi dari /settings.")
        return ConversationHandler.END

    sponsor_id, field = info["id"], info["field"]

    if field == "photo_file_id":
        photo_file_id = None
        if message.photo:
            photo_file_id = message.photo[-1].file_id
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            photo_file_id = message.document.file_id
        if not photo_file_id:
            await message.reply_text(
                "Itu bukan foto. Kirim *foto* baru untuk sponsor ini.", parse_mode="Markdown",
            )
            return EDIT_SPONSOR_VALUE
        value = photo_file_id
    else:
        text = (message.text or "").strip()
        if not text:
            await message.reply_text("Kirim teks yang valid.")
            return EDIT_SPONSOR_VALUE
        if text == "-":
            value = None
        elif field == "url":
            value = _normalize_telegram_link(text)
        else:
            value = message.text

    db.update_sponsor_field(sponsor_id, field, value)
    context.user_data.pop("edit_sponsor", None)

    sponsor = db.get_sponsor(sponsor_id)
    label = sponsor.get("name") or f"Sponsor #{sponsor['id']}"
    await message.reply_text(
        f"✅ {SPONSOR_FIELD_LABELS.get(field, field)} sponsor *{label}* berhasil diubah.",
        parse_mode="Markdown",
        reply_markup=kb.edit_sponsor_field_keyboard(sponsor),
    )
    return ConversationHandler.END


async def edit_channel_photo_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        photo_file_id = message.document.file_id

    if photo_file_id:
        db.set_setting("channel_photo", photo_file_id)
    else:
        text = (message.text or "").strip()
        if text == "-":
            pass  # lewati, biarkan foto lama (kalau ada)
        elif text.lower() == "hapus":
            db.delete_setting("channel_photo")
        else:
            await message.reply_text(
                "Itu bukan foto. Kirim *foto* channel, ketik `-` untuk lewati, "
                "atau `hapus` untuk menghapus foto yang sudah ada.",
                parse_mode="Markdown",
            )
            return EDIT_CHANNEL_PHOTO

    await message.reply_text(
        "Langkah 2/3 — Kirim *deskripsi* channel, ketik `-` untuk lewati, "
        "atau `hapus` untuk menghapus deskripsi yang sudah ada.",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(),
    )
    return EDIT_CHANNEL_DESC


async def edit_channel_desc_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "-":
        pass  # lewati, biarkan deskripsi lama
    elif text.lower() == "hapus":
        db.delete_setting("channel_description")
    else:
        db.set_setting("channel_description", update.message.text)

    await update.message.reply_text(
        "Langkah 3/3 — Kirim *link channel Telegram* talent/bisnis kamu\n"
        "(contoh: `https://t.me/namachannel` atau `@namachannel`), ketik `-` untuk lewati, "
        "atau `hapus` untuk menghapus link yang sudah ada.",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(),
    )
    return EDIT_CHANNEL_URL


async def edit_channel_url_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "-":
        pass  # lewati, biarkan link lama
    elif text.lower() == "hapus":
        db.delete_setting("channel_url")
    else:
        db.set_setting("channel_url", _normalize_telegram_link(text))

    await update.message.reply_text(
        "✅ Info channel berhasil diperbarui. Cek Mini App untuk melihat hasilnya.",
        reply_markup=kb.settings_menu_keyboard(),
    )
    return ConversationHandler.END


async def edit_channel2_photo_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        photo_file_id = message.document.file_id

    if photo_file_id:
        db.set_setting("channel2_photo", photo_file_id)
    else:
        text = (message.text or "").strip()
        if text == "-":
            pass  # lewati, biarkan foto lama (kalau ada)
        elif text.lower() == "hapus":
            db.delete_setting("channel2_photo")
        else:
            await message.reply_text(
                "Itu bukan foto. Kirim *foto* channel, ketik `-` untuk lewati, "
                "atau `hapus` untuk menghapus foto yang sudah ada.",
                parse_mode="Markdown",
            )
            return EDIT_CHANNEL2_PHOTO

    await message.reply_text(
        "Langkah 2/3 — Kirim *deskripsi* channel, ketik `-` untuk lewati, "
        "atau `hapus` untuk menghapus deskripsi yang sudah ada.",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(),
    )
    return EDIT_CHANNEL2_DESC


async def edit_channel2_desc_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "-":
        pass  # lewati, biarkan deskripsi lama
    elif text.lower() == "hapus":
        db.delete_setting("channel2_description")
    else:
        db.set_setting("channel2_description", update.message.text)

    await update.message.reply_text(
        "Langkah 3/3 — Kirim *link channel/grup Telegram*\n"
        "(contoh: `https://t.me/namachannel` atau `@namachannel`), ketik `-` untuk lewati, "
        "atau ketik `hapus` untuk menghapus link yang sudah ada.",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(),
    )
    return EDIT_CHANNEL2_URL


async def edit_channel2_url_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "-":
        pass  # lewati, biarkan link lama
    elif text.lower() == "hapus":
        db.delete_setting("channel2_url")
    else:
        db.set_setting("channel2_url", _normalize_telegram_link(text))

    await update.message.reply_text(
        "✅ Info channel 2 berhasil diperbarui. Cek Mini App untuk melihat hasilnya.",
        reply_markup=kb.settings_menu_keyboard(),
    )
    return ConversationHandler.END


async def add_sponsor_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        photo_file_id = message.document.file_id

    if not photo_file_id:
        await message.reply_text(
            "Itu bukan foto. Kirim *foto* logo/banner sponsor.",
            parse_mode="Markdown",
        )
        return ADD_SPONSOR_PHOTO

    context.user_data["new_sponsor"] = {"photo_file_id": photo_file_id}
    await message.reply_text(
        "Masukkan *nama* sponsor (ketik `-` untuk lewati):",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(),
    )
    return ADD_SPONSOR_NAME


async def add_sponsor_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["new_sponsor"]["name"] = None if text == "-" else update.message.text
    await update.message.reply_text(
        "Masukkan *deskripsi* sponsor (akan tampil saat foto sponsor di Mini App di-tap), "
        "atau ketik `-` untuk lewati:",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(),
    )
    return ADD_SPONSOR_DESC


async def add_sponsor_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["new_sponsor"]["description"] = None if text == "-" else update.message.text
    await update.message.reply_text(
        "Masukkan *link* sponsor (situs web, `https://t.me/namachannel`, dsb), "
        "atau ketik `-` untuk lewati:",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(),
    )
    return ADD_SPONSOR_URL


async def add_sponsor_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ns = context.user_data["new_sponsor"]
    ns["url"] = None if text == "-" else _normalize_telegram_link(text)

    sponsor_id = db.add_sponsor(
        photo_file_id=ns["photo_file_id"],
        name=ns.get("name"),
        description=ns.get("description"),
        url=ns.get("url"),
    )
    context.user_data.pop("new_sponsor", None)
    await update.message.reply_text(
        f"✅ Sponsor berhasil ditambahkan (ID: {sponsor_id}).",
        reply_markup=kb.settings_menu_keyboard(),
    )
    return ConversationHandler.END


async def addtalent_back_to_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await replace_message(
        query, context,
        "Masukkan *nama* talent:", parse_mode="Markdown", reply_markup=kb.addtalent_step_keyboard()
    )
    return ADD_NAME


async def addtalent_back_to_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await replace_message(
        query, context,
        "Masukkan *deskripsi* talent:",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(back_callback="back_to_addname"),
    )
    return ADD_DESC


async def addtalent_back_to_pricelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await replace_message(
        query, context,
        "Masukkan *pricelist* (boleh multi-baris):",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(back_callback="back_to_adddesc"),
    )
    return ADD_PRICELIST


async def addtalent_back_to_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await replace_message(
        query, context,
        "Masukkan *link channel Telegram* talent\n"
        "(contoh: `https://t.me/namachannel` atau `@namachannel`, atau ketik `-` untuk lewati):",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(back_callback="back_to_addpricelist"),
    )
    return ADD_PORTFOLIO


async def addtalent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("new_talent", None)
    context.user_data.pop("new_sponsor", None)
    await replace_message(query, context, "Dibatalkan.", reply_markup=kb.settings_menu_keyboard())
    return ConversationHandler.END


async def edit_greeting_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    photo_file_id = None
    if message.photo:
        # Foto terkompresi (cara normal kirim foto di Telegram)
        photo_file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        # Foto dikirim sebagai file/dokumen (mis. opsi "Kirim tanpa kompresi")
        photo_file_id = message.document.file_id

    if photo_file_id:
        db.set_setting("greeting_photo", photo_file_id)
        if message.caption:
            db.set_setting("greeting", message.caption)
        await message.reply_text("✅ Foto sapaan berhasil diperbarui.", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    text = (message.text or "").strip()
    if text.lower() in ("hapus foto", "hapus foto sapaan"):
        db.delete_setting("greeting_photo")
        await message.reply_text("✅ Foto sapaan berhasil dihapus.", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    db.set_setting("greeting", message.text)
    await message.reply_text("✅ Teks sapaan berhasil diubah.", reply_markup=kb.settings_menu_keyboard())
    return ConversationHandler.END


async def edit_howtoorder_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_setting("how_to_order", update.message.text)
    await update.message.reply_text("✅ Teks Cara Order berhasil diubah.", reply_markup=kb.settings_menu_keyboard())
    return ConversationHandler.END


async def edit_webapp_bg_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    photo_file_id = None
    if message.photo:
        # Foto terkompresi (cara normal kirim foto di Telegram)
        photo_file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        # Foto dikirim sebagai file/dokumen (mis. opsi "Kirim tanpa kompresi")
        photo_file_id = message.document.file_id

    if photo_file_id:
        db.set_setting("webapp_bg_photo", photo_file_id)
        await message.reply_text(
            "✅ Background Mini App berhasil diperbarui.",
            reply_markup=kb.settings_menu_keyboard(),
        )
        return ConversationHandler.END

    text = (message.text or "").strip()
    if text.lower() in ("hapus background", "hapus bg"):
        db.delete_setting("webapp_bg_photo")
        await message.reply_text(
            "✅ Background Mini App berhasil dihapus.",
            reply_markup=kb.settings_menu_keyboard(),
        )
        return ConversationHandler.END

    await message.reply_text(
        "Itu bukan foto. Kirim *foto* untuk background Mini App, "
        "atau ketik `hapus background` untuk menghapus background yang ada.",
        parse_mode="Markdown",
    )
    return EDIT_WEBAPP_BG


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Dibatalkan.")
    return ConversationHandler.END


async def groupid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: `{update.effective_chat.id}`", parse_mode="Markdown")


async def on_startup(application: Application):
    if config.WEBAPP_URL:
        asyncio.create_task(run_api_server())
        logger.info("api_server.py dijalankan karena WEBAPP_URL diisi.")

    # Daftarkan perintah supaya muncul di menu "/" Telegram. Semua user melihat
    # perintah dasar; masing-masing admin (lewat scope per-chat) melihat
    # tambahan perintah khusus admin juga.
    public_commands = [
        BotCommand("start", "Buka menu utama"),
        BotCommand("help", "Bantuan & cara pakai bot"),
        BotCommand("about", "Tentang bot ini"),
    ]
    try:
        await application.bot.set_my_commands(public_commands)
    except Exception:
        logger.warning("Gagal mengatur daftar perintah publik.")

    admin_commands = public_commands + [
        BotCommand("settings", "Menu pengaturan (admin)"),
        BotCommand("groupid", "Lihat ID chat/grup ini"),
        BotCommand("cancel", "Batalkan proses yang sedang berjalan"),
    ]
    for admin_id in config.ADMIN_IDS:
        try:
            await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            logger.warning("Gagal mengatur daftar perintah admin untuk chat_id=%s.", admin_id)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Tangkap semua error yang tidak ke-handle supaya bot tidak diam saja,
    dan supaya errornya kelihatan jelas di log Railway."""
    logger.error("Unhandled exception saat proses update", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Terjadi error saat memproses permintaan ini. "
                "Admin sudah diberi tahu, silakan coba lagi atau ketik /cancel untuk mulai ulang."
            )
        except Exception:
            logger.exception("Gagal mengirim pesan error ke user")


def main():
    db.init_db()
    app = Application.builder().token(config.BOT_TOKEN).post_init(on_startup).build()
    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("groupid", groupid_command))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.WEB_APP_DATA & webapp_view_talent_filter, handle_webapp_data
    ))

    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(talent_detail_callback, pattern="^talent_"))
    app.add_handler(CallbackQueryHandler(pricelist_callback, pattern="^price_"))

    # ---- Live chat: mulai sesi ----
    app.add_handler(CallbackQueryHandler(chat_start_callback, pattern="^chat_"))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.WEB_APP_DATA & webapp_chat_talent_filter, chat_start_from_webapp
    ))

    # ---- Live chat: admin mengakhiri sesi ----
    app.add_handler(CallbackQueryHandler(end_chat_callback, pattern="^endchat_"))

    settings_conv = ConversationHandler(
        entry_points=[
            CommandHandler("settings", settings_command),
            CallbackQueryHandler(
                settings_callback,
                pattern="^settings_|^delconfirm_|^sponsor|^edittalent|^editsponsor",
            ),
        ],
        states={
            ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_talent_name),
            ],
            ADD_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_talent_desc),
                CallbackQueryHandler(addtalent_back_to_name, pattern="^back_to_addname$"),
            ],
            ADD_PRICELIST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_talent_pricelist),
                CallbackQueryHandler(addtalent_back_to_desc, pattern="^back_to_adddesc$"),
            ],
            ADD_PORTFOLIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_talent_portfolio),
                CallbackQueryHandler(addtalent_back_to_pricelist, pattern="^back_to_addpricelist$"),
            ],
            ADD_PHOTO: [
                MessageHandler(
                    (filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND,
                    add_talent_photo,
                ),
                CallbackQueryHandler(addtalent_back_to_portfolio, pattern="^back_to_addportfolio$"),
            ],
            EDIT_GREETING: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
                    edit_greeting_receive,
                ),
            ],
            EDIT_HOWTOORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_howtoorder_receive)],
            EDIT_WEBAPP_BG: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
                    edit_webapp_bg_receive,
                ),
            ],
            EDIT_CHANNEL_PHOTO: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
                    edit_channel_photo_receive,
                ),
            ],
            EDIT_CHANNEL_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_channel_desc_receive)],
            EDIT_CHANNEL_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_channel_url_receive)],
            ADD_SPONSOR_PHOTO: [
                MessageHandler(
                    (filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND,
                    add_sponsor_photo,
                ),
            ],
            ADD_SPONSOR_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sponsor_name)],
            ADD_SPONSOR_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sponsor_desc)],
            ADD_SPONSOR_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sponsor_url)],
            EDIT_TALENT_VALUE: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
                    edit_talent_value_receive,
                ),
            ],
            EDIT_SPONSOR_VALUE: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
                    edit_sponsor_value_receive,
                ),
            ],
            EDIT_CHANNEL2_PHOTO: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
                    edit_channel2_photo_receive,
                ),
            ],
            EDIT_CHANNEL2_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_channel2_desc_receive)],
            EDIT_CHANNEL2_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_channel2_url_receive)],
        },
        fallbacks=[
            CallbackQueryHandler(addtalent_cancel, pattern="^addtalent_cancel$"),
            CallbackQueryHandler(
                settings_callback,
                pattern="^settings_|^delconfirm_|^sponsor|^edittalent|^editsponsor",
            ),
            CommandHandler("settings", settings_command),
            CommandHandler("cancel", cancel_conversation),
        ],
        per_message=False,
        allow_reentry=True,
    )
    app.add_handler(settings_conv)

    # ---- Live chat: relay dua arah ----
    # Balasan admin (reply ke pesan live chat yang diteruskan), baik di grup
    # live chat maupun di private chat masing-masing admin.
    app.add_handler(MessageHandler(filters.ALL & admin_reply_filter & ~filters.COMMAND, relay_admin_reply))
    # Pesan biasa dari user (bukan admin) di private chat, diteruskan kalau
    # user tsb sedang punya sesi live chat aktif. Handler ini sengaja
    # didaftarkan PALING TERAKHIR supaya tidak "merebut" update yang harusnya
    # ditangani flow lain (mis. langkah-langkah /settings admin).
    app.add_handler(MessageHandler(
        filters.ALL
        & filters.ChatType.PRIVATE
        & ~filters.COMMAND
        & ~filters.StatusUpdate.WEB_APP_DATA,
        relay_user_message,
    ))

    logger.info("Bot berjalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
