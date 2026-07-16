import asyncio
import json
import logging

from telegram import Update
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
    BOOK_NEEDS, BOOK_DATE, BOOK_BUDGET,
    ADD_NAME, ADD_DESC, ADD_PRICELIST, ADD_PORTFOLIO, ADD_PHOTO,
    EDIT_GREETING, EDIT_HOWTOORDER,
) = range(10)


def is_admin(user_id):
    return user_id in config.ADMIN_IDS


class WebAppActionFilter(MessageFilter):
    """Filter pesan `web_app_data` berdasarkan isi field `action` di payload JSON-nya,
    supaya aksi 'lihat talent' dan 'ajukan booking' dari Mini App bisa ditangani
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
webapp_book_talent_filter = WebAppActionFilter("book_talent")


async def delete_prev_message(query):
    """Hapus pesan sebelumnya (yang berisi tombol) setiap kali user menekan tombol,
    supaya histori chat tetap bersih dan tidak menumpuk pesan lama."""
    try:
        await query.message.delete()
    except Exception:
        logger.warning("Gagal menghapus pesan sebelumnya (mungkin sudah dihapus / terlalu lama).")


async def replace_message(query, context, text, reply_markup=None, parse_mode=None):
    """Pengganti pola `query.edit_message_text(...)`: hapus pesan lama lalu kirim
    pesan baru sebagai gantinya, supaya perilakunya konsisten dengan tombol lain."""
    await delete_prev_message(query)
    return await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )


# ==================== START & MENU ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    greeting = db.get_setting("greeting", config.DEFAULT_GREETING).format(bot_name=config.BOT_NAME)
    await update.message.reply_text(greeting, reply_markup=kb.main_menu_keyboard())

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

    if query.data == "menu_talents" or query.data.startswith("menu_talents_p"):
        page = 1
        if query.data.startswith("menu_talents_p"):
            try:
                page = int(query.data[len("menu_talents_p"):])
            except ValueError:
                page = 1
        talents = db.list_talents()
        if not talents:
            await replace_message(
                query, context,
                "Belum ada talent yang ditambahkan.",
                reply_markup=kb.main_menu_keyboard(),
            )
            return
        await replace_message(
            query, context,
            "Pilih talent:", reply_markup=kb.talent_list_keyboard(talents, page=page)
        )

    elif query.data == "menu_howtoorder":
        text = db.get_setting("how_to_order", config.DEFAULT_HOW_TO_ORDER)
        await replace_message(query, context, text, reply_markup=kb.main_menu_keyboard())

    elif query.data == "menu_back":
        greeting = db.get_setting("greeting", config.DEFAULT_GREETING).format(bot_name=config.BOT_NAME)
        await replace_message(query, context, greeting, reply_markup=kb.main_menu_keyboard())


async def show_talent_detail(context: ContextTypes.DEFAULT_TYPE, chat_id, talent):
    """Kirim halaman detail talent (foto+deskripsi+tombol) ke chat_id tertentu.
    Dipakai baik dari tombol chat biasa maupun dari data yang dikirim Mini App."""
    caption = f"*{talent['name']}*\n\n{talent['description']}"
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
    await delete_prev_message(query)
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


async def booking_start_from_webapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point booking saat user menekan tombol 'Ajukan Booking' di Mini App.
    Mini App akan menutup diri (tg.close()) dan mengirim data ini, lalu bot
    melanjutkan alur booking di chat seperti biasa."""
    try:
        payload = json.loads(update.effective_message.web_app_data.data)
        talent_id = int(payload["talent_id"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return ConversationHandler.END

    talent = db.get_talent(talent_id)
    if not talent:
        await update.effective_message.reply_text(
            "Talent tidak ditemukan.", reply_markup=kb.main_menu_keyboard()
        )
        return ConversationHandler.END

    context.user_data["booking"] = {"talent_id": talent_id, "talent_name": talent["name"]}
    await update.effective_message.reply_text(
        f"📝 Ajukan booking untuk *{talent['name']}*.\n\n"
        f"Ceritakan kebutuhan Anda (jenis konten/endorsement/event):",
        parse_mode="Markdown",
        reply_markup=kb.booking_step_keyboard(),
    )
    return BOOK_NEEDS


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
    await delete_prev_message(query)
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
        await context.bot.send_photo(chat_id=chat_id, photo=talent["photo_file_id"])
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


# ==================== BOOKING (satu arah: notifikasi ke admin) ====================

async def booking_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talent_id = int(query.data.split("_")[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await query.answer("Talent tidak ditemukan.", show_alert=True)
        return ConversationHandler.END

    context.user_data["booking"] = {"talent_id": talent_id, "talent_name": talent["name"]}
    chat_id = query.message.chat_id
    await delete_prev_message(query)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📝 Ajukan booking untuk *{talent['name']}*.\n\n"
             f"Ceritakan kebutuhan Anda (jenis konten/endorsement/event):",
        parse_mode="Markdown",
        reply_markup=kb.booking_step_keyboard(),
    )
    return BOOK_NEEDS


async def booking_needs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["booking"]["needs"] = update.message.text
    await update.message.reply_text(
        "Tanggal/periode dibutuhkan kapan?",
        reply_markup=kb.booking_step_keyboard(back_callback="back_to_needs"),
    )
    return BOOK_DATE


async def booking_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["booking"]["date_needed"] = update.message.text
    await update.message.reply_text(
        "Berapa perkiraan budget Anda?",
        reply_markup=kb.booking_step_keyboard(back_callback="back_to_date"),
    )
    return BOOK_BUDGET


async def booking_back_to_needs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await replace_message(
        query, context,
        "Ceritakan kebutuhan Anda (jenis konten/endorsement/event):",
        reply_markup=kb.booking_step_keyboard(),
    )
    return BOOK_NEEDS


async def booking_back_to_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await replace_message(
        query, context,
        "Tanggal/periode dibutuhkan kapan?",
        reply_markup=kb.booking_step_keyboard(back_callback="back_to_needs"),
    )
    return BOOK_DATE


async def booking_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    booking = context.user_data["booking"]
    booking["budget"] = update.message.text
    user = update.effective_user

    booking_id = db.add_booking(
        talent_id=booking["talent_id"],
        talent_name=booking["talent_name"],
        user_id=user.id,
        username=user.username or "-",
        full_name=user.full_name,
        needs=booking["needs"],
        date_needed=booking["date_needed"],
        budget=booking["budget"],
    )

    summary = (
        f"📥 *Booking Baru #{booking_id}*\n\n"
        f"Talent: {booking['talent_name']}\n"
        f"Dari: {user.full_name} (@{user.username or '-'}, ID: {user.id})\n"
        f"Kebutuhan: {booking['needs']}\n"
        f"Tanggal: {booking['date_needed']}\n"
        f"Budget: {booking['budget']}\n\n"
        f"_Hubungi user di atas secara langsung untuk konfirmasi._"
    )
    await notify_admins(context, summary)

    await update.message.reply_text(
        "Terima kasih! Booking Anda sudah kami terima. Admin akan menghubungi Anda "
        "untuk konfirmasi jadwal dan pembayaran.",
        reply_markup=kb.main_menu_keyboard(),
    )
    context.user_data.pop("booking", None)
    return ConversationHandler.END


async def booking_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("booking", None)
    await replace_message(query, context, "Booking dibatalkan.", reply_markup=kb.main_menu_keyboard())
    return ConversationHandler.END


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Kirim notifikasi satu arah. Tidak membuka kanal chat dua arah dengan user."""
    if config.BOOKING_NOTIFY_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=int(config.BOOKING_NOTIFY_CHAT_ID), text=text, parse_mode="Markdown"
            )
            return
        except Exception:
            logger.exception("Gagal kirim notifikasi ke BOOKING_NOTIFY_CHAT_ID")

    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
        except Exception:
            logger.exception(f"Gagal kirim notifikasi ke admin {admin_id}")


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

    if query.data == "settings_bookings":
        bookings = db.list_bookings()
        if not bookings:
            text = "Belum ada booking masuk."
        else:
            lines = ["📥 *Booking Terbaru:*\n"]
            for b in bookings:
                lines.append(
                    f"#{b['id']} - {b['talent_name']} - {b['full_name']} "
                    f"(@{b['username']}) - {b['date_needed']} - {b['budget']}"
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
        await replace_message(
            query, context,
            "Kirim teks sapaan baru untuk /start.\n"
            "Gunakan {bot_name} kalau ingin menyisipkan nama bot."
        )
        return EDIT_GREETING

    if query.data == "settings_howtoorder":
        await replace_message(query, context, "Kirim teks baru untuk halaman \"Cara Order\":")
        return EDIT_HOWTOORDER

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
    await replace_message(query, context, "Dibatalkan.", reply_markup=kb.settings_menu_keyboard())
    return ConversationHandler.END


async def edit_greeting_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_setting("greeting", update.message.text)
    await update.message.reply_text("✅ Teks sapaan berhasil diubah.", reply_markup=kb.settings_menu_keyboard())
    return ConversationHandler.END


async def edit_howtoorder_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_setting("how_to_order", update.message.text)
    await update.message.reply_text("✅ Teks Cara Order berhasil diubah.", reply_markup=kb.settings_menu_keyboard())
    return ConversationHandler.END


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
    app.add_handler(CommandHandler("groupid", groupid_command))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.WEB_APP_DATA & webapp_view_talent_filter, handle_webapp_data
    ))

    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(talent_detail_callback, pattern="^talent_"))
    app.add_handler(CallbackQueryHandler(pricelist_callback, pattern="^price_"))

    booking_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(booking_start, pattern="^book_"),
            MessageHandler(
                filters.StatusUpdate.WEB_APP_DATA & webapp_book_talent_filter,
                booking_start_from_webapp,
            ),
        ],
        states={
            BOOK_NEEDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, booking_needs),
                CallbackQueryHandler(booking_back_to_needs, pattern="^back_to_needs$"),
            ],
            BOOK_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, booking_date),
                CallbackQueryHandler(booking_back_to_needs, pattern="^back_to_needs$"),
            ],
            BOOK_BUDGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, booking_budget),
                CallbackQueryHandler(booking_back_to_date, pattern="^back_to_date$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(booking_cancel, pattern="^booking_cancel$"),
            CommandHandler("cancel", cancel_conversation),
        ],
    )
    app.add_handler(booking_conv)

    settings_conv = ConversationHandler(
        entry_points=[
            CommandHandler("settings", settings_command),
            CallbackQueryHandler(settings_callback, pattern="^settings_|^delconfirm_"),
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
            EDIT_GREETING: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_greeting_receive)],
            EDIT_HOWTOORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_howtoorder_receive)],
        },
        fallbacks=[
            CallbackQueryHandler(addtalent_cancel, pattern="^addtalent_cancel$"),
            CallbackQueryHandler(settings_callback, pattern="^settings_|^delconfirm_"),
            CommandHandler("cancel", cancel_conversation),
        ],
        per_message=False,
    )
    app.add_handler(settings_conv)

    logger.info("Bot berjalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
