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

import config
import database as db
import keyboards as kb

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


# ==================== START & MENU ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    greeting = db.get_setting("greeting", config.DEFAULT_GREETING).format(bot_name=config.BOT_NAME)
    await update.message.reply_text(greeting, reply_markup=kb.main_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "menu_talents":
        talents = db.list_talents()
        if not talents:
            await query.edit_message_text(
                "Belum ada talent yang ditambahkan.",
                reply_markup=kb.main_menu_keyboard(),
            )
            return
        await query.edit_message_text(
            "Pilih talent:", reply_markup=kb.talent_list_keyboard(talents)
        )

    elif query.data == "menu_howtoorder":
        text = db.get_setting("how_to_order", config.DEFAULT_HOW_TO_ORDER)
        await query.edit_message_text(text, reply_markup=kb.main_menu_keyboard())

    elif query.data == "menu_back":
        greeting = db.get_setting("greeting", config.DEFAULT_GREETING).format(bot_name=config.BOT_NAME)
        await query.edit_message_text(greeting, reply_markup=kb.main_menu_keyboard())


async def talent_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talent_id = int(query.data.split("_")[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await query.edit_message_text("Talent tidak ditemukan.", reply_markup=kb.main_menu_keyboard())
        return

    caption = f"*{talent['name']}*\n\n{talent['description']}"
    if talent.get("photo_file_id"):
        await query.message.delete()
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=talent["photo_file_id"],
            caption=caption,
            parse_mode="Markdown",
            reply_markup=kb.talent_detail_keyboard(talent),
        )
    else:
        await query.edit_message_text(
            caption, parse_mode="Markdown", reply_markup=kb.talent_detail_keyboard(talent)
        )


async def pricelist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    talent_id = int(query.data.split("_")[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await query.answer("Talent tidak ditemukan.", show_alert=True)
        return
    text = f"💰 *Pricelist - {talent['name']}*\n\n{talent['pricelist']}"
    await context.bot.send_message(
        chat_id=query.message.chat_id,
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
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"📝 Ajukan booking untuk *{talent['name']}*.\n\n"
             f"Ceritakan kebutuhan Anda (jenis konten/endorsement/event):",
        parse_mode="Markdown",
        reply_markup=kb.cancel_booking_keyboard(),
    )
    return BOOK_NEEDS


async def booking_needs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["booking"]["needs"] = update.message.text
    await update.message.reply_text(
        "Tanggal/periode dibutuhkan kapan?", reply_markup=kb.cancel_booking_keyboard()
    )
    return BOOK_DATE


async def booking_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["booking"]["date_needed"] = update.message.text
    await update.message.reply_text(
        "Berapa perkiraan budget Anda?", reply_markup=kb.cancel_booking_keyboard()
    )
    return BOOK_BUDGET


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
    await query.edit_message_text("Booking dibatalkan.", reply_markup=kb.main_menu_keyboard())
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
        await query.edit_message_text("⚙️ Menu Pengaturan", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    if query.data == "settings_listtalent":
        talents = db.list_talents()
        if not talents:
            text = "Belum ada talent."
        else:
            text = "📋 *Daftar Talent:*\n\n" + "\n".join(
                f"• {t['name']} (ID: {t['id']})" for t in talents
            )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb.settings_menu_keyboard())
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
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    if query.data == "settings_deltalent":
        talents = db.list_talents()
        if not talents:
            await query.edit_message_text("Belum ada talent untuk dihapus.", reply_markup=kb.settings_menu_keyboard())
            return ConversationHandler.END
        await query.edit_message_text("Pilih talent yang ingin dihapus:", reply_markup=kb.delete_talent_keyboard(talents))
        return ConversationHandler.END

    if query.data.startswith("delconfirm_"):
        talent_id = int(query.data.split("_")[1])
        db.delete_talent(talent_id)
        await query.edit_message_text("✅ Talent dihapus.", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    if query.data == "settings_addtalent":
        context.user_data["new_talent"] = {}
        await query.edit_message_text("Masukkan *nama* talent:", parse_mode="Markdown")
        return ADD_NAME

    if query.data == "settings_greeting":
        await query.edit_message_text(
            "Kirim teks sapaan baru untuk /start.\n"
            "Gunakan {bot_name} kalau ingin menyisipkan nama bot."
        )
        return EDIT_GREETING

    if query.data == "settings_howtoorder":
        await query.edit_message_text("Kirim teks baru untuk halaman \"Cara Order\":")
        return EDIT_HOWTOORDER

    return ConversationHandler.END


async def add_talent_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_talent"]["name"] = update.message.text
    await update.message.reply_text("Masukkan *deskripsi* talent:", parse_mode="Markdown")
    return ADD_DESC


async def add_talent_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_talent"]["description"] = update.message.text
    await update.message.reply_text("Masukkan *pricelist* (boleh multi-baris):", parse_mode="Markdown")
    return ADD_PRICELIST


async def add_talent_pricelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_talent"]["pricelist"] = update.message.text
    await update.message.reply_text(
        "Masukkan *link portofolio* (atau ketik `-` untuk lewati):", parse_mode="Markdown"
    )
    return ADD_PORTFOLIO


async def add_talent_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["new_talent"]["portfolio_url"] = None if text == "-" else text
    await update.message.reply_text("Kirim *foto* talent (atau ketik `-` untuk lewati):", parse_mode="Markdown")
    return ADD_PHOTO


async def add_talent_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nt = context.user_data["new_talent"]
    if update.message.photo:
        nt["photo_file_id"] = update.message.photo[-1].file_id
    else:
        nt["photo_file_id"] = None

    talent_id = db.add_talent(
        name=nt["name"],
        description=nt["description"],
        pricelist=nt["pricelist"],
        portfolio_url=nt.get("portfolio_url"),
        photo_file_id=nt.get("photo_file_id"),
    )
    await update.message.reply_text(
        f"✅ Talent *{nt['name']}* berhasil ditambahkan (ID: {talent_id}).",
        parse_mode="Markdown",
        reply_markup=kb.settings_menu_keyboard(),
    )
    context.user_data.pop("new_talent", None)
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


def main():
    db.init_db()
    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("groupid", groupid_command))

    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(talent_detail_callback, pattern="^talent_"))
    app.add_handler(CallbackQueryHandler(pricelist_callback, pattern="^price_"))

    booking_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(booking_start, pattern="^book_")],
        states={
            BOOK_NEEDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, booking_needs)],
            BOOK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, booking_date)],
            BOOK_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, booking_budget)],
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
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_talent_name)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_talent_desc)],
            ADD_PRICELIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_talent_pricelist)],
            ADD_PORTFOLIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_talent_portfolio)],
            ADD_PHOTO: [MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND, add_talent_photo)],
            EDIT_GREETING: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_greeting_receive)],
            EDIT_HOWTOORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_howtoorder_receive)],
        },
        fallbacks=[
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
