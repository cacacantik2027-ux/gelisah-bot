"""
bot.py
======
Bot Telegram TALENT GELISAH -- booking talent/influencer/model untuk
keperluan konten/endorsement/event.

Fitur:
1. Menu /start: "Pilih Talent" sendiri di baris atas, "Live Chat" &
   "Cara Order" sejajar di baris kedua.
2. Live Chat: user tekan tombol "Live Chat" -> semua pesan (teks/foto)
   yang dikirim user setelahnya otomatis diteruskan ke grup admin
   (LIVECHAT_GROUP_ID). Admin membalas cukup dengan me-reply pesan yang
   dikirim bot di grup itu -> otomatis diteruskan balik ke user yang tepat.
3. Pilih Talent: setiap talent punya "halaman" sendiri (foto + deskripsi)
   dengan tombol "Pricelist" dan "Tanyakan Ready". Tombol "Tanyakan Ready"
   otomatis mengirim pertanyaan ke grup admin (menyebutkan nama talent +
   identitas user) -- admin cukup me-reply pesan itu untuk menjawab
   langsung ke user, sama seperti mekanisme live chat.

Jalankan dengan: python bot.py
"""

import json
import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters,
)

import config
import database as db
import keyboards as kb
import api_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Conversation states ─────────────────────────────────────────────────
(
    ADD_NAME, ADD_DESC, ADD_PHOTO, ADD_PRICELIST,
    EDIT_PICK, EDIT_NAME, EDIT_DESC, EDIT_PHOTO, EDIT_PRICELIST,
) = range(9)

DEFAULT_GREETING = (
    "✨ <b>Selamat datang di TALENT GELISAH!</b> ✨\n\n"
    "Kami membantu kamu booking talent/influencer/model untuk kebutuhan "
    "konten, endorsement, maupun event.\n\n"
    "Silakan pilih menu di bawah ini 👇"
)

DEFAULT_HOW_TO_ORDER = (
    "📖 <b>Cara Order</b>\n\n"
    "1️⃣ Tekan <b>Pilih Talent</b> untuk melihat daftar talent yang tersedia.\n"
    "2️⃣ Buka halaman talent yang kamu minati untuk lihat foto & deskripsinya.\n"
    "3️⃣ Cek <b>Pricelist</b> untuk lihat rincian harga paket konten/endorsement/event.\n"
    "4️⃣ Tekan <b>Tanyakan Ready</b> untuk cek ketersediaan talent di tanggal kamu -- "
    "pertanyaan kamu akan diteruskan langsung ke admin.\n"
    "5️⃣ Atau tekan <b>Live Chat</b> kapan saja untuk ngobrol langsung dengan admin."
)


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def user_display(update_user) -> str:
    name = update_user.full_name or update_user.username or str(update_user.id)
    return name


# ── /start & menu utama ──────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    greeting = db.get_setting("greeting_text", DEFAULT_GREETING)
    await update.message.reply_text(greeting, reply_markup=kb.main_menu_keyboard(), parse_mode=ParseMode.HTML)

    # Tombol Mini App "Pilih Talent" (katalog visual) HARUS reply keyboard
    # (bukan inline), lihat catatan di keyboards.py::webapp_launch_keyboard().
    # Tidak bisa dipasang bareng di pesan yang sama dengan inline keyboard di
    # atas, jadi dikirim sebagai pesan kecil terpisah. Kembalikan None kalau
    # WEBAPP_URL belum di-setup -> skip, bot tetap jalan normal.
    webapp_kb = kb.webapp_launch_keyboard()
    if webapp_kb:
        await update.message.reply_text(
            "Atau tekan tombol di bawah untuk lihat katalog talent dalam tampilan galeri foto (Mini App):",
            reply_markup=webapp_kb,
        )


async def back_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    greeting = db.get_setting("greeting_text", DEFAULT_GREETING)
    await query.edit_message_text(greeting, reply_markup=kb.main_menu_keyboard(), parse_mode=ParseMode.HTML)


async def how_to_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = db.get_setting("how_to_order_text", DEFAULT_HOW_TO_ORDER)
    await query.edit_message_text(text, reply_markup=kb.how_to_order_keyboard(), parse_mode=ParseMode.HTML)


# ── Pilih Talent ─────────────────────────────────────────────────────────
async def show_talents_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talents = db.list_talents()
    if not talents:
        await query.edit_message_text(
            "<i>Belum ada talent yang tersedia saat ini. Coba lagi nanti ya.</i>",
            reply_markup=kb.back_main_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    await query.edit_message_text(
        "💃 <b>Pilih Talent</b>\n\nTekan salah satu talent di bawah untuk lihat detailnya.",
        reply_markup=kb.talent_list_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def send_talent_detail(chat_id: int, context: ContextTypes.DEFAULT_TYPE, talent_id: int):
    """Kirim halaman detail talent (foto + deskripsi + tombol Pricelist/
    Tanyakan Ready) sebagai pesan baru ke chat_id. Dipakai dari 3 sumber:
    1. Tombol daftar talent biasa (callback query, ada query.message).
    2. Tombol hasil answerWebAppQuery (Mini App dibuka lewat Menu Button,
       lihat api_server.py::handle_select_talent()) -- callback query tanpa
       query.message.
    3. Mini App dibuka lewat reply keyboard, kirim balik lewat
       Telegram.WebApp.sendData() -- lihat handle_webapp_data() di bawah."""
    talent = db.get_talent(talent_id)
    if not talent:
        await context.bot.send_message(
            chat_id, "<i>Talent tidak ditemukan / sudah dihapus.</i>",
            reply_markup=kb.back_main_keyboard(), parse_mode=ParseMode.HTML,
        )
        return

    caption = f"👤 <b>{talent['name']}</b>\n\n{talent['description']}"
    keyboard = kb.talent_detail_keyboard(talent_id)

    if talent["photo_file_id"]:
        await context.bot.send_photo(
            chat_id=chat_id, photo=talent["photo_file_id"],
            caption=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )


async def talent_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talent_id = int(query.data.split("_", 1)[1])

    if query.message is not None:
        # Tombol biasa dari pesan normal (mis. daftar talent di chat) ->
        # hapus pesan lama dulu (teks daftar talent) supaya chat tetap rapi,
        # baru kirim halaman detail sebagai pesan baru.
        chat_id = query.message.chat_id
        try:
            await query.message.delete()
        except Exception:
            pass
    else:
        # Tombol hasil answerWebAppQuery (Mini App lewat Menu Button) tidak
        # punya query.message sama sekali -- chat_id diambil dari
        # query.from_user.id (aman, alur ini selalu di private chat 1-on-1).
        chat_id = query.from_user.id

    await send_talent_detail(chat_id, context, talent_id)


async def pricelist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talent_id = int(query.data.split("_", 1)[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await query.answer("Talent tidak ditemukan.", show_alert=True)
        return
    text = talent["pricelist_text"] or "<i>Pricelist belum diisi admin.</i>"
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"💰 <b>Pricelist -- {talent['name']}</b>\n\n{text}",
        reply_markup=kb.pricelist_back_keyboard(talent_id),
        parse_mode=ParseMode.HTML,
    )


async def ready_inquiry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol 'Tanyakan Ready' -- kirim pertanyaan ketersediaan talent
    langsung ke grup live chat, tercatat di relay_messages supaya balasan
    admin (reply ke pesan ini) otomatis diteruskan ke user yang tepat."""
    query = update.callback_query
    user = update.effective_user
    talent_id = int(query.data.split("_", 1)[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await query.answer("Talent tidak ditemukan.", show_alert=True)
        return

    if not config.LIVECHAT_GROUP_ID:
        await query.answer("Fitur tanya-ready belum aktif, hubungi admin langsung ya.", show_alert=True)
        return

    await query.answer("Pertanyaan kamu sedang dikirim ke admin...")

    username_part = f"@{user.username}" if user.username else "(tidak ada username)"
    group_text = (
        "❓ <b>TANYA READY</b>\n"
        f"Talent: <b>{talent['name']}</b>\n"
        f"Dari: {user_display(user)} {username_part}\n"
        f"User ID: <code>{user.id}</code>\n\n"
        "↩️ Balas (reply) pesan ini untuk menjawab langsung ke user."
    )
    sent = await context.bot.send_message(
        chat_id=config.LIVECHAT_GROUP_ID,
        text=group_text,
        parse_mode=ParseMode.HTML,
    )
    db.add_relay(sent.message_id, user.id, user_display(user), kind="ready_inquiry", talent_name=talent["name"])

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            f"✅ Pertanyaan tentang ketersediaan <b>{talent['name']}</b> sudah dikirim ke admin.\n"
            "Admin akan membalas langsung di sini secepatnya, mohon ditunggu ya 🙏"
        ),
        reply_markup=kb.after_ready_keyboard(talent_id),
        parse_mode=ParseMode.HTML,
    )


# ── Live Chat ─────────────────────────────────────────────────────────────
async def start_livechat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not config.LIVECHAT_GROUP_ID:
        await query.answer("Live chat belum diaktifkan admin.", show_alert=True)
        return

    await query.answer()
    db.set_live_chat(user.id, user_display(user), active=True)
    await query.edit_message_text(
        "💬 <b>Live Chat aktif</b>\n\n"
        "Ketik pesan (teks atau foto) kapan saja, akan langsung diteruskan ke admin. "
        "Admin akan membalas di sini juga.\n\n"
        "Tekan tombol di bawah kalau sudah selesai.",
        reply_markup=kb.livechat_active_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def end_livechat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    await query.answer()
    db.set_live_chat(user.id, user_display(user), active=False)
    greeting = db.get_setting("greeting_text", DEFAULT_GREETING)
    await query.edit_message_text(
        "❌ Live chat diakhiri. Kamu bisa mulai lagi kapan saja lewat menu utama.\n\n" + greeting,
        reply_markup=kb.main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def relay_user_message_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Kalau user sedang live chat aktif, teruskan pesannya (teks/foto) ke
    grup admin & catat relay-nya. Return True kalau pesan sudah ditangani
    (supaya handler pemanggil tidak perlu proses lebih lanjut)."""
    user = update.effective_user
    message = update.effective_message

    if not db.is_live_chat_active(user.id):
        return False
    if not config.LIVECHAT_GROUP_ID:
        await message.reply_text("Live chat belum diaktifkan admin, hubungi admin langsung ya.")
        return True

    username_part = f"@{user.username}" if user.username else "(tidak ada username)"
    header = (
        f"💬 <b>Live Chat</b> dari {user_display(user)} {username_part}\n"
        f"User ID: <code>{user.id}</code>\n"
        "↩️ Balas (reply) pesan ini untuk menjawab user."
    )

    if message.photo:
        sent = await context.bot.send_photo(
            chat_id=config.LIVECHAT_GROUP_ID,
            photo=message.photo[-1].file_id,
            caption=header + (f"\n\n{message.caption}" if message.caption else ""),
            parse_mode=ParseMode.HTML,
        )
    else:
        text = message.text or "(pesan tanpa teks)"
        sent = await context.bot.send_message(
            chat_id=config.LIVECHAT_GROUP_ID,
            text=header + f"\n\n{text}",
            parse_mode=ParseMode.HTML,
        )

    db.add_relay(sent.message_id, user.id, user_display(user), kind="livechat")
    return True


async def relay_group_reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pesan di grup admin yang me-reply pesan hasil relay bot -> diteruskan
    balik ke user yang bersangkutan."""
    message = update.effective_message
    if not message.reply_to_message:
        return

    relay = db.get_relay(message.reply_to_message.message_id)
    if not relay:
        return

    sender = update.effective_user
    if config.LIVECHAT_ADMIN_ONLY_REPLY and not is_admin(sender.id):
        return

    target_user_id = relay["user_id"]

    if relay["kind"] == "ready_inquiry":
        prefix = f"💌 <b>Admin menjawab pertanyaan ketersediaan {relay['talent_name']}:</b>\n\n"
    else:
        prefix = "👨‍💼 <b>Admin:</b>\n\n"

    try:
        if message.photo:
            await context.bot.send_photo(
                chat_id=target_user_id,
                photo=message.photo[-1].file_id,
                caption=prefix + (message.caption or ""),
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=prefix + (message.text or ""),
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.warning("Gagal meneruskan balasan admin ke user %s: %s", target_user_id, e)
        await message.reply_text(f"⚠️ Gagal meneruskan pesan ke user (mungkin user memblokir bot). Detail: {e}")


# ── Handler pesan private (dispatcher) ───────────────────────────────────
async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # 1) Admin sedang mengisi teks setting (greeting / cara order)?
    awaiting = context.user_data.get("awaiting_setting")
    if awaiting and is_admin(user.id):
        text_html = update.effective_message.text_html or update.effective_message.text or ""
        db.set_setting(awaiting, text_html)
        context.user_data.pop("awaiting_setting", None)
        await update.effective_message.reply_text(f"✅ Teks '{awaiting}' berhasil disimpan.")
        return

    # 2) Live chat aktif -> teruskan ke grup admin
    handled = await relay_user_message_to_group(update, context)
    if handled:
        return

    # 3) Default -> arahkan ke menu utama
    await update.effective_message.reply_text(
        "Gunakan tombol menu di bawah ya 🙂",
        reply_markup=kb.main_menu_keyboard(),
    )


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != config.LIVECHAT_GROUP_ID:
        return
    await relay_group_reply_to_user(update, context)


# ── Admin: kelola talent ─────────────────────────────────────────────────
async def admin_only_guard(update: Update) -> bool:
    user = update.effective_user
    if not is_admin(user.id):
        if update.message:
            await update.message.reply_text("Perintah ini khusus admin.")
        return False
    return True


async def addtalent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return ConversationHandler.END
    context.user_data["new_talent"] = {}
    await update.message.reply_text("Masukkan <b>nama talent</b> baru:", parse_mode=ParseMode.HTML)
    return ADD_NAME


async def addtalent_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_talent"]["name"] = update.message.text.strip()
    await update.message.reply_text("Masukkan <b>deskripsi</b> talent ini:", parse_mode=ParseMode.HTML)
    return ADD_DESC


async def addtalent_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_talent"]["description"] = update.effective_message.text_html or update.message.text.strip()
    await update.message.reply_text(
        "Kirim <b>foto</b> talent ini (atau tekan Lewati kalau belum ada foto):",
        reply_markup=kb.skip_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ADD_PHOTO


async def addtalent_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.photo:
        context.user_data["new_talent"]["photo_file_id"] = update.message.photo[-1].file_id
    await update.message.reply_text(
        "Masukkan <b>pricelist</b> (boleh multi-baris, mis. rincian paket & harga), "
        "atau tekan Lewati:",
        reply_markup=kb.skip_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ADD_PRICELIST


async def addtalent_photo_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Masukkan <b>pricelist</b> (boleh multi-baris, mis. rincian paket & harga), "
        "atau tekan Lewati:",
        reply_markup=kb.skip_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return ADD_PRICELIST


async def addtalent_pricelist_finish(update: Update, context: ContextTypes.DEFAULT_TYPE, pricelist_text: str):
    data = context.user_data.pop("new_talent")
    talent_id = db.add_talent(
        name=data["name"],
        description=data.get("description", ""),
        photo_file_id=data.get("photo_file_id", ""),
        pricelist_text=pricelist_text,
    )
    return talent_id, data["name"]


async def addtalent_pricelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pricelist_text = update.effective_message.text_html or update.message.text.strip()
    talent_id, name = await addtalent_pricelist_finish(update, context, pricelist_text)
    await update.message.reply_text(f"✅ Talent <b>{name}</b> berhasil ditambahkan (ID {talent_id}).",
                                     parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def addtalent_pricelist_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talent_id, name = await addtalent_pricelist_finish(update, context, "")
    await query.edit_message_text(f"✅ Talent <b>{name}</b> berhasil ditambahkan (ID {talent_id}).",
                                   parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def addtalent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_talent", None)
    await update.message.reply_text("Dibatalkan.")
    return ConversationHandler.END


async def talents_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    talents = db.list_talents(active_only=False)
    if not talents:
        await update.message.reply_text("Belum ada talent. Tambah dengan /addtalent.")
        return
    lines = [f"{t['id']}. {t['name']}" for t in talents]
    await update.message.reply_text(
        "📋 <b>Daftar Talent</b>\n\n" + "\n".join(lines) +
        "\n\nGunakan /deltalent untuk menghapus talent.",
        parse_mode=ParseMode.HTML,
    )


async def deltalent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    talents = db.list_talents(active_only=False)
    if not talents:
        await update.message.reply_text("Belum ada talent.")
        return
    await update.message.reply_text("Pilih talent yang mau dihapus:",
                                     reply_markup=kb.talent_admin_list_keyboard("deltalent"))


async def deltalent_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talent_id = int(query.data.split("_", 1)[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await query.edit_message_text("Talent tidak ditemukan.")
        return
    await query.edit_message_text(
        f"Hapus talent <b>{talent['name']}</b>?",
        reply_markup=kb.confirm_delete_keyboard(talent_id),
        parse_mode=ParseMode.HTML,
    )


async def deltalent_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talent_id = int(query.data.split("_", 2)[2])
    talent = db.get_talent(talent_id)
    db.delete_talent(talent_id)
    name = talent["name"] if talent else f"ID {talent_id}"
    await query.edit_message_text(f"🗑️ Talent <b>{name}</b> berhasil dihapus.", parse_mode=ParseMode.HTML)


async def admin_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Ditutup.")


async def setgreeting_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    context.user_data["awaiting_setting"] = "greeting_text"
    await update.message.reply_text("Kirim teks sapaan baru (mendukung HTML/format Telegram):")


async def sethowtoorder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    context.user_data["awaiting_setting"] = "how_to_order_text"
    await update.message.reply_text("Kirim teks 'Cara Order' baru (mendukung HTML/format Telegram):")


async def groupid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Utility kecil untuk admin: cek chat id grup ini, buat diisi ke
    LIVECHAT_GROUP_ID di .env."""
    await update.message.reply_text(f"Chat ID grup ini: <code>{update.effective_chat.id}</code>",
                                     parse_mode=ParseMode.HTML)


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ditrigger saat user pilih talent dari Mini App "Pilih Talent" (halaman
    HTML statis, mis. di-hosting GitHub Pages) yang dibuka lewat reply
    keyboard -- Mini App mengirim data lewat Telegram.WebApp.sendData(...)
    di sisi JS, sampai ke bot sebagai pesan biasa berisi web_app_data (BUKAN
    callback query)."""
    try:
        payload = json.loads(update.effective_message.web_app_data.data)
        talent_id = int(payload["talent_id"])
    except Exception:
        await update.effective_message.reply_text(
            "Data dari halaman katalog tidak terbaca. Coba buka lagi & pilih talentnya."
        )
        return
    await send_talent_detail(update.effective_chat.id, context, talent_id)


async def on_startup(app_: Application):
    """post_init: jalan sekali setelah bot siap tapi sebelum polling mulai.
    Kalau admin sudah setup WEBAPP_URL, nyalakan server API kecil (lihat
    api_server.py) di event loop yang SAMA -- supaya cuma perlu 1 proses/
    service di Railway, bukan 2."""
    if config.WEBAPP_URL:
        await api_server.start_api_server(app_.bot, config.PORT)
    else:
        logger.info("WEBAPP_URL belum diisi -> Mini App 'Pilih Talent' nonaktif, fallback ke daftar talent di chat.")


def main():
    db.init_db()

    app = Application.builder().token(config.BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(CommandHandler("groupid", groupid_cmd))
    app.add_handler(CommandHandler("talents", talents_list_cmd))
    app.add_handler(CommandHandler("deltalent", deltalent_cmd))
    app.add_handler(CommandHandler("setgreeting", setgreeting_cmd))
    app.add_handler(CommandHandler("sethowtoorder", sethowtoorder_cmd))

    app.add_handler(CallbackQueryHandler(back_main_callback, pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(how_to_order_callback, pattern="^how_to_order$"))
    app.add_handler(CallbackQueryHandler(show_talents_callback, pattern="^show_talents$"))
    app.add_handler(CallbackQueryHandler(talent_detail_callback, pattern=r"^talent_\d+$"))
    app.add_handler(CallbackQueryHandler(pricelist_callback, pattern=r"^pricelist_\d+$"))
    app.add_handler(CallbackQueryHandler(ready_inquiry_callback, pattern=r"^ready_\d+$"))
    app.add_handler(CallbackQueryHandler(start_livechat_callback, pattern="^start_livechat$"))
    app.add_handler(CallbackQueryHandler(end_livechat_callback, pattern="^end_livechat$"))
    app.add_handler(CallbackQueryHandler(deltalent_pick_callback, pattern=r"^deltalent_\d+$"))
    app.add_handler(CallbackQueryHandler(deltalent_confirm_callback, pattern=r"^deltalent_confirm_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_close_callback, pattern="^admin_close$"))

    addtalent_conv = ConversationHandler(
        entry_points=[CommandHandler("addtalent", addtalent_start)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addtalent_name)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, addtalent_desc)],
            ADD_PHOTO: [
                MessageHandler(filters.PHOTO, addtalent_photo),
                CallbackQueryHandler(addtalent_photo_skip, pattern="^skip_step$"),
            ],
            ADD_PRICELIST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addtalent_pricelist),
                CallbackQueryHandler(addtalent_pricelist_skip, pattern="^skip_step$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", addtalent_cancel)],
    )
    app.add_handler(addtalent_conv)

    # Pesan di grup live chat (reply admin -> diteruskan ke user). Chat id
    # asli Telegram tidak pernah 0, jadi kalau LIVECHAT_GROUP_ID belum
    # di-setup (masih 0) filter ini otomatis tidak pernah match apa pun --
    # aman, tidak akan "mencuri" pesan reply di chat private user.
    app.add_handler(MessageHandler(
        filters.Chat(chat_id=config.LIVECHAT_GROUP_ID) & filters.REPLY & ~filters.COMMAND,
        group_message_handler,
    ))

    # Pesan private (default + relay live chat)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_message_handler))

    logger.info("Bot TALENT GELISAH berjalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
