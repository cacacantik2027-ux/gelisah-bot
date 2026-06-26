"""
GELISAH VCS Talent Bot
"""

import logging
import os
import sys
import urllib.parse
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
from telegram.constants import ParseMode
from config import BOT_TOKEN, LIVECHAT_BOT, ADMIN_IDS
from data.talents import load_talents, save_talents

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ConversationHandler states
(
    ADMIN_MENU,
    ADD_NAME,
    ADD_PHOTO,
    ADD_DESC,
    ADD_PRICE,
    EDIT_CHOOSE,
    EDIT_FIELD,
    EDIT_VALUE,
    DEL_CONFIRM,
) = range(9)


# ─── Helpers ───────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_menu_keyboard():
    talents = load_talents()
    buttons = []
    row = []
    for i, t in enumerate(talents):
        row.append(InlineKeyboardButton(f"👤 {t['name']}", callback_data=f"talent_{i}"))
        if len(row) == 2:         
            buttons.append(row)
            row = []
    if row:                        
        buttons.append(row)
    buttons.append([InlineKeyboardButton("📋 Lihat Semua Talent", callback_data="list_all")])
    return InlineKeyboardMarkup(buttons)


def talent_keyboard(idx: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Pricelist", callback_data=f"price_{idx}"),
            InlineKeyboardButton("🛒 Order Sekarang", callback_data=f"order_{idx}"),
        ],
        [InlineKeyboardButton("« Kembali ke Daftar", callback_data="back_main")],
    ])


def price_keyboard(idx: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛒 Order Sekarang", callback_data=f"order_{idx}"),
            InlineKeyboardButton("« Kembali", callback_data=f"talent_{idx}"),
        ],
    ])


def order_keyboard(idx: int, talent_name: str):
    order_msg = f"Halo admin saya ingin order talent {talent_name} dari GELISAH 🔥"
    encoded = urllib.parse.quote(order_msg)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"💬 Chat Admin — Order {talent_name}",
            url=f"https://t.me/{LIVECHAT_BOT}?start={encoded}",
        )],
        [InlineKeyboardButton("« Kembali ke Talent", callback_data=f"talent_{idx}")],
    ])


# ─── User Commands ──────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    talents = load_talents()
    text = (
        "✨ *Selamat datang di GELISAH VCS Talent Agency!*\n\n"
        "Kami menyediakan talent VCS terpercaya dan siap melayani kamu.\n\n"
        f"📊 Total talent tersedia: *{len(talents)} talent*\n\n"
        "Pilih talent di bawah untuk melihat foto, deskripsi, dan pricelist:"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Cara pakai bot GELISAH:*\n\n"
        "1. Ketik /start untuk melihat daftar talent\n"
        "2. Pilih talent yang kamu suka\n"
        "3. Tekan *💰 Pricelist* untuk melihat harga\n"
        "4. Tekan *🛒 Order Sekarang* untuk langsung order ke admin\n\n"
        "❓ Ada pertanyaan? Hubungi admin via /contact",
        parse_mode=ParseMode.MARKDOWN,
    )


async def contact_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📞 *Hubungi Admin GELISAH:*\n\n"
        f"💬 Live Chat: [Klik disini](https://t.me/{LIVECHAT_BOT})\n"
        "🌐 Group: @gelisahidpub\n"
        "📢 Channel: @ttalengelisah",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


# ─── Callback Handler ───────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    talents = load_talents()

    # ── Tampilkan kartu talent ──
    if data.startswith("talent_"):
        idx = int(data.split("_")[1])
        if idx >= len(talents):
            await query.edit_message_text("❌ Talent tidak ditemukan.")
            return
        t = talents[idx]
        caption = (
            f"👤 *{t['name']}*\n\n"
            f"📝 *Deskripsi:*\n{t['description']}\n\n"
            "Tekan tombol di bawah untuk melihat pricelist atau langsung order!"
        )
        if t.get("photo"):
            try:
                await query.message.delete()
                await ctx.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=t["photo"],
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=talent_keyboard(idx),
                )
            except Exception as e:
                logger.warning(f"send_photo failed: {e}")
                await ctx.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=talent_keyboard(idx),
                )
        else:
            try:
                await query.edit_message_text(
                    caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=talent_keyboard(idx),
                )
            except Exception:
                await ctx.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=talent_keyboard(idx),
                )

    # ── Pricelist (slide ke-2) ──
    elif data.startswith("price_"):
        idx = int(data.split("_")[1])
        if idx >= len(talents):
            await query.answer("Talent tidak ditemukan.", show_alert=True)
            return
        t = talents[idx]
        price_text = t.get("pricelist", "_Pricelist belum tersedia. Hubungi admin._")
        text = (
            f"💰 *Pricelist — {t['name']}*\n\n"
            f"{price_text}\n\n"
            "───────────────────\n"
            "Tertarik? Langsung order sekarang! 👇"
        )
        if t.get("photo"):
            try:
                await query.message.delete()
                await ctx.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=t["photo"],
                    caption=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=price_keyboard(idx),
                )
            except Exception as e:
                logger.warning(f"send_photo failed: {e}")
                await ctx.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=price_keyboard(idx),
                )
        else:
            try:
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=price_keyboard(idx),
                )
            except Exception:
                await ctx.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=price_keyboard(idx),
                )

    # ── Order ──
    elif data.startswith("order_"):
        idx = int(data.split("_")[1])
        if idx >= len(talents):
            await query.answer("Talent tidak ditemukan.", show_alert=True)
            return
        t = talents[idx]
        order_msg = f"Halo admin saya ingin order talent {t['name']} dari GELISAH 🔥"
        await ctx.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"✅ *Siap order {t['name']}!*\n\n"
                f"Pesan yang akan terkirim ke admin:\n"
                f"_\"{order_msg}\"_\n\n"
                "Klik tombol di bawah untuk lanjut:"
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=order_keyboard(idx, t["name"]),
        )

    # ── List semua ──
    elif data == "list_all":
        if not talents:
            await query.edit_message_text("Belum ada talent.", reply_markup=main_menu_keyboard())
            return
        lines = ["🌟 *Semua Talent GELISAH:*\n"]
        for i, t in enumerate(talents, 1):
            desc_short = t["description"][:60] + ("…" if len(t["description"]) > 60 else "")
            lines.append(f"{i}. *{t['name']}*\n   _{desc_short}_")
        lines.append("\n\nPilih talent untuk detail & order:")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(),
        )

    # ── Kembali ke menu ──
    elif data == "back_main":
        try:
            await query.message.delete()
        except Exception:
            pass
        await ctx.bot.send_message(
            chat_id=query.message.chat_id,
            text="🏠 *Menu Utama GELISAH*\n\nPilih talent VCS favorit kamu:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(),
        )


# ─── Admin Conversation ─────────────────────────────

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return ConversationHandler.END

    await update.message.reply_text(
        "⚙️ *Panel Admin GELISAH*\n\nPilih aksi:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Tambah Talent", callback_data="adm_add")],
            [InlineKeyboardButton("✏️ Edit Talent", callback_data="adm_edit")],
            [InlineKeyboardButton("🗑 Hapus Talent", callback_data="adm_del")],
            [InlineKeyboardButton("📋 Lihat Semua", callback_data="adm_list")],
            [InlineKeyboardButton("❌ Tutup", callback_data="adm_close")],
        ]),
    )
    return ADMIN_MENU


def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Talent", callback_data="adm_add")],
        [InlineKeyboardButton("✏️ Edit Talent", callback_data="adm_edit")],
        [InlineKeyboardButton("🗑 Hapus Talent", callback_data="adm_del")],
        [InlineKeyboardButton("📋 Lihat Semua", callback_data="adm_list")],
        [InlineKeyboardButton("❌ Tutup", callback_data="adm_close")],
    ])


async def admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    talents = load_talents()

    if data == "adm_close":
        await query.edit_message_text("Panel admin ditutup.")
        return ConversationHandler.END

    elif data == "adm_back":
        await query.edit_message_text(
            "⚙️ *Panel Admin GELISAH*\n\nPilih aksi:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_main_keyboard(),
        )
        return ADMIN_MENU

    elif data == "adm_list":
        if not talents:
            await query.edit_message_text("Belum ada talent.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali", callback_data="adm_back")]]))
            return ADMIN_MENU
        lines = ["📋 *Daftar Talent:*\n"]
        for i, t in enumerate(talents):
            lines.append(
                f"{i+1}. *{t['name']}*\n"
                f"   Desc: {t['description'][:50]}…\n"
                f"   Foto: {'✅' if t.get('photo') else '❌'}"
            )
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali", callback_data="adm_back")]]),
        )
        return ADMIN_MENU

    elif data == "adm_add":
        await query.edit_message_text("➕ *Tambah Talent Baru*\n\nKirim *nama* talent:", parse_mode=ParseMode.MARKDOWN)
        return ADD_NAME

    elif data == "adm_edit":
        if not talents:
            await query.answer("Belum ada talent.", show_alert=True)
            return ADMIN_MENU
        buttons = [[InlineKeyboardButton(f"{i+1}. {t['name']}", callback_data=f"edit_pick_{i}")] for i, t in enumerate(talents)]
        buttons.append([InlineKeyboardButton("« Kembali", callback_data="adm_back")])
        await query.edit_message_text("✏️ Pilih talent yang akan diedit:", reply_markup=InlineKeyboardMarkup(buttons))
        return EDIT_CHOOSE

    elif data == "adm_del":
        if not talents:
            await query.answer("Belum ada talent.", show_alert=True)
            return ADMIN_MENU
        buttons = [[InlineKeyboardButton(f"🗑 {t['name']}", callback_data=f"del_pick_{i}")] for i, t in enumerate(talents)]
        buttons.append([InlineKeyboardButton("« Kembali", callback_data="adm_back")])
        await query.edit_message_text("🗑 Pilih talent yang akan dihapus:", reply_markup=InlineKeyboardMarkup(buttons))
        return DEL_CONFIRM

    elif data.startswith("edit_pick_"):
        idx = int(data.split("_")[-1])
        ctx.user_data["edit_idx"] = idx
        t = talents[idx]
        await query.edit_message_text(
            f"✏️ Edit *{t['name']}* — pilih field yang mau diubah:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Nama", callback_data="ef_name"), InlineKeyboardButton("📷 Foto", callback_data="ef_photo")],
                [InlineKeyboardButton("📄 Deskripsi", callback_data="ef_desc"), InlineKeyboardButton("💰 Pricelist", callback_data="ef_price")],
                [InlineKeyboardButton("« Kembali", callback_data="adm_edit")],
            ]),
        )
        return EDIT_FIELD

    elif data.startswith("ef_"):
        prompts = {
            "ef_name": ("name", "nama baru"),
            "ef_photo": ("photo", "foto baru (kirim foto langsung)"),
            "ef_desc": ("description", "deskripsi baru"),
            "ef_price": ("pricelist", "pricelist baru"),
        }
        field, prompt = prompts.get(data, ("name", "nilai baru"))
        ctx.user_data["edit_field"] = field
        await query.edit_message_text(f"✏️ Kirim {prompt}:")
        return EDIT_VALUE

    elif data.startswith("del_pick_"):
        idx = int(data.split("_")[-1])
        if idx < len(talents):
            name = talents[idx]["name"]
            talents.pop(idx)
            save_talents(talents)
            await query.edit_message_text(f"✅ Talent *{name}* berhasil dihapus!", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    return ADMIN_MENU


async def admin_get_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_talent"] = {"name": update.message.text.strip(), "photo": "", "description": "", "pricelist": ""}
    await update.message.reply_text("📷 Kirim *foto* talent (atau ketik `skip`):", parse_mode=ParseMode.MARKDOWN)
    return ADD_PHOTO


async def admin_get_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        ctx.user_data["new_talent"]["photo"] = update.message.photo[-1].file_id
    elif update.message.text and update.message.text.strip().lower() == "skip":
        ctx.user_data["new_talent"]["photo"] = ""
    else:
        ctx.user_data["new_talent"]["photo"] = update.message.text.strip()
    await update.message.reply_text("📄 Kirim *deskripsi* talent:", parse_mode=ParseMode.MARKDOWN)
    return ADD_DESC


async def admin_get_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_talent"]["description"] = update.message.text.strip()
    await update.message.reply_text(
        "💰 Kirim *pricelist* talent:\n\nContoh:\n• VCS 15 menit → Rp 25.000\n• VCS 30 menit → Rp 45.000",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADD_PRICE


async def admin_get_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_talent"]["pricelist"] = update.message.text.strip()
    talent = ctx.user_data.pop("new_talent")
    talents = load_talents()
    talents.append(talent)
    save_talents(talents)
    await update.message.reply_text(
        f"✅ Talent *{talent['name']}* berhasil ditambahkan!\n\nKetik /start untuk melihat hasilnya.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


async def admin_edit_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    idx = ctx.user_data.get("edit_idx", 0)
    field = ctx.user_data.get("edit_field", "name")
    talents = load_talents()
    if idx >= len(talents):
        await update.message.reply_text("❌ Talent tidak ditemukan.")
        return ConversationHandler.END

    if field == "photo" and update.message.photo:
        value = update.message.photo[-1].file_id
    else:
        value = update.message.text.strip()

    talents[idx][field] = value
    save_talents(talents)
    await update.message.reply_text(
        f"✅ *{talents[idx]['name']}* berhasil diupdate!",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operasi dibatalkan. Ketik /start untuk mulai lagi.")
    return ConversationHandler.END


# ─── Main ───────────────────────────────────────────

def main():
    logger.info("Starting GELISAH Bot...")
    app = Application.builder().token(BOT_TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_cmd)],
        states={
            ADMIN_MENU:   [CallbackQueryHandler(admin_callback)],
            ADD_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_name)],
            ADD_PHOTO:    [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), admin_get_photo)],
            ADD_DESC:     [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_desc)],
            ADD_PRICE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_price)],
            EDIT_CHOOSE:  [CallbackQueryHandler(admin_callback)],
            EDIT_FIELD:   [CallbackQueryHandler(admin_callback)],
            EDIT_VALUE:   [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), admin_edit_value)],
            DEL_CONFIRM:  [CallbackQueryHandler(admin_callback)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("contact", contact_cmd))
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
