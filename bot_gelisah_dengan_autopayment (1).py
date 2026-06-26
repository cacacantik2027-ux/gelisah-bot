"""
GELISAH VCS Talent Bot with Autopayment & Group Log
"""

import logging
import os
import sys
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
from config import BOT_TOKEN, LIVECHAT_BOT, ADMIN_IDS, LOG_GROUP_ID, QRIS_IMAGE_URL, BANK_INFO
from data.talents import load_talents, save_talents

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ConversationHandler states untuk Admin
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
    buttons.append([
        InlineKeyboardButton("📖 Bantuan", callback_data="show_help"),
        InlineKeyboardButton("📞 Kontak", callback_data="show_contact"),
    ])
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


def order_keyboard(idx: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Konfirmasi Order & Bayar", callback_data=f"confirm_order_{idx}"),
            InlineKeyboardButton("« Batal", callback_data=f"talent_{idx}"),
        ],
    ])


# ─── User Commands ──────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Reset state pembayaran jika user mengetik /start
    if "awaiting_payment_for" in ctx.user_data:
        del ctx.user_data["awaiting_payment_for"]
        
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
        "4. Tekan *🛒 Order Sekarang* dan ikuti instruksi pembayaran\n"
        "5. Kirim foto bukti transfer, lalu tunggu admin konfirmasi.\n\n"
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


# ─── Handler Bukti Pembayaran ───────────────────────

async def handle_payment_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Menangkap foto bukti pembayaran dari user dan mengirim ke Grup Log"""
    # Jika user tidak dalam state membayar, abaikan
    if "awaiting_payment_for" not in ctx.user_data:
        return
        
    idx = ctx.user_data["awaiting_payment_for"]
    talents = load_talents()
    
    if idx >= len(talents):
        await update.message.reply_text("❌ Terjadi kesalahan. Talent tidak ditemukan.")
        del ctx.user_data["awaiting_payment_for"]
        return

    talent = talents[idx]
    user = update.message.from_user
    photo_id = update.message.photo[-1].file_id # Ambil resolusi foto tertinggi
    
    # Notif user
    await update.message.reply_text(
        "⏳ *Bukti transfer berhasil dikirim!*\n\n"
        "Mohon tunggu sebentar, tim admin sedang mengecek pembayaran kamu. Notifikasi akan masuk ke sini.",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Hapus state user agar tidak spam
    del ctx.user_data["awaiting_payment_for"]

    # Kirim ke Grup Log Admin
    admin_caption = (
        f"🔔 *BUKTI PEMBAYARAN BARU!*\n\n"
        f"🙋‍♂️ *User:* [{user.full_name}](tg://user?id={user.id})\n"
        f"🆔 *Username:* @{user.username or '-'}\n"
        f"🆔 *ID User:* `{user.id}`\n\n"
        f"👤 *Talent Dipesan:* {talent['name']}\n\n"
        f"Tolong cek mutasi dan konfirmasi di bawah 👇"
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Terima", callback_data=f"acc_{user.id}_{idx}"),
            InlineKeyboardButton("❌ Tolak", callback_data=f"rej_{user.id}_{idx}")
        ]
    ])
    
    try:
        await ctx.bot.send_photo(
            chat_id=LOG_GROUP_ID,
            photo=photo_id,
            caption=admin_caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Gagal mengirim log ke grup: {e}")
        await update.message.reply_text("Terjadi kesalahan sistem saat mengirim bukti ke admin. Harap lapor via Livechat.")


# ─── Callback Handler ───────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    talents = load_talents()

    # ── Fitur Konfirmasi Admin di Grup Log ──
    if data.startswith("acc_") or data.startswith("rej_"):
        if not is_admin(query.from_user.id):
            await query.answer("⛔ Hanya admin yang bisa klik tombol ini!", show_alert=True)
            return

        action, user_id_str, talent_idx_str = data.split("_")
        user_id = int(user_id_str)
        t_idx = int(talent_idx_str)
        talent_name = talents[t_idx]['name'] if t_idx < len(talents) else "Talent Tidak Diketahui"
        admin_name = query.from_user.first_name

        if action == "acc":
            # Edit pesan di grup
            new_caption = query.message.caption + f"\n\n✅ *STATUS:* DITERIMA oleh {admin_name}"
            await query.edit_message_caption(caption=new_caption, parse_mode=ParseMode.MARKDOWN)
            
            # Kirim notif ke user
            success_msg = (
                f"🎉 *PEMBAYARAN DITERIMA!*\n\n"
                f"Pembayaran kamu untuk talent *{talent_name}* telah dikonfirmasi oleh admin.\n\n"
                f"Silakan klik link di bawah untuk menghubungi Livechat dan menjadwalkan sesi kamu 👇"
            )
            await ctx.bot.send_message(
                chat_id=user_id, 
                text=success_msg, 
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Hubungi Livechat", url=f"https://t.me/{LIVECHAT_BOT}")]
                ])
            )
            
        elif action == "rej":
            # Edit pesan di grup
            new_caption = query.message.caption + f"\n\n❌ *STATUS:* DITOLAK oleh {admin_name}"
            await query.edit_message_caption(caption=new_caption, parse_mode=ParseMode.MARKDOWN)
            
            # Kirim notif ke user
            reject_msg = (
                f"❌ *PEMBAYARAN DITOLAK*\n\n"
                f"Maaf, bukti pembayaran kamu untuk talent *{talent_name}* ditolak oleh admin (dana belum masuk / bukti tidak valid).\n\n"
                f"Jika ada kendala, silakan hubungi Livechat."
            )
            await ctx.bot.send_message(
                chat_id=user_id, 
                text=reject_msg, 
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Hubungi Livechat", url=f"https://t.me/{LIVECHAT_BOT}")]
                ])
            )
        return

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

    # ── Order Awal ──
    elif data.startswith("order_"):
        idx = int(data.split("_")[1])
        if idx >= len(talents):
            await query.answer("Talent tidak ditemukan.", show_alert=True)
            return
        t = talents[idx]
        await ctx.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"🛒 *Konfirmasi Order*\n\n"
                f"Talent: *{t['name']}*\n\n"
                f"Siapkan dana pembayaran sesuai pricelist.\n"
                f"Tekan *✅ Konfirmasi Order & Bayar* untuk melihat metode pembayaran (QRIS/Rekening)."
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=order_keyboard(idx),
        )

    # ── Tampilkan Instruksi Bayar (Autopayment) ──
    elif data.startswith("confirm_order_"):
        idx = int(data.split("_")[-1])
        if idx >= len(talents):
            await query.answer("Talent tidak ditemukan.", show_alert=True)
            return
        t = talents[idx]
        
        # Set state user bahwa dia sedang proses membayar
        ctx.user_data["awaiting_payment_for"] = idx
        
        payment_text = (
            f"💳 *PEMBAYARAN*\n\n"
            f"Silakan lakukan pembayaran untuk talent *{t['name']}*.\n\n"
            f"🏦 *Informasi Rekening & E-Wallet:*\n"
            f"`{BANK_INFO}`\n\n"
            f"📸 *Atau Scan QRIS di atas/bawah*\n\n"
            f"⚠️ *PENTING:*\n"
            f"Setelah transfer berhasil, *KIRIMKAN FOTO BUKTI TRANSFER* (Screenshot/Resi) langsung ke bot ini sekarang."
        )

        photo_to_send = QRIS_IMAGE_URL
        is_local_file = False
        
        # Cek jika QRIS_IMAGE_URL adalah file lokal di server
        if isinstance(QRIS_IMAGE_URL, str):
            if not QRIS_IMAGE_URL.startswith("http") and os.path.exists(QRIS_IMAGE_URL):
                try:
                    photo_to_send = open(QRIS_IMAGE_URL, "rb")
                    is_local_file = True
                except Exception as e:
                    logger.error(f"Gagal membuka file lokal QRIS: {e}")

        try:
            # Kirim gambar QRIS + Text
            await query.message.delete()
            await ctx.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=photo_to_send,
                caption=payment_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="cancel_order")]])
            )
        except Exception as e:
            logger.error(f"Gagal mengirim QRIS: {e}")
            # Fallback jika QRIS gagal (hanya kirim teks)
            await ctx.bot.send_message(
                chat_id=query.message.chat_id,
                text=payment_text + "\n\n*(Gambar QRIS gagal dimuat, silakan gunakan rekening di atas)*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="cancel_order")]])
            )
        finally:
            if is_local_file and hasattr(photo_to_send, "close"):
                photo_to_send.close()

    # ── Batal Order ──
    elif data == "cancel_order":
        if "awaiting_payment_for" in ctx.user_data:
            del ctx.user_data["awaiting_payment_for"]
        await query.edit_message_text(
            "❌ Pesanan dibatalkan. Silakan pilih talent lain jika berminat.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali ke Menu", callback_data="back_main")]])
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

    # ── Bantuan ──
    elif data == "show_help":
        await query.answer()
        await ctx.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "📖 *Cara pakai bot GELISAH:*\n\n"
                "1. Ketik /start untuk melihat daftar talent\n"
                "2. Pilih talent yang kamu suka\n"
                "3. Tekan *💰 Pricelist* untuk melihat harga\n"
                "4. Tekan *🛒 Order Sekarang* dan ikuti instruksi\n"
                "5. Kirim bukti transfer dan tunggu konfirmasi\n\n"
                "❓ Ada pertanyaan? Hubungi admin via tombol Kontak."
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali ke Menu", callback_data="back_main")]]),
        )

    # ── Kontak ──
    elif data == "show_contact":
        await query.answer()
        await ctx.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "📞 *Hubungi Admin GELISAH:*\n\n"
                f"💬 Live Chat: [Klik disini](https://t.me/{LIVECHAT_BOT})\n"
                "🌐 Group: @gelisahidpub\n"
                "📢 Channel: @ttalengelisah"
            ),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali ke Menu", callback_data="back_main")]]),
        )

    # ── Kembali ke menu ──
    elif data == "back_main":
        if "awaiting_payment_for" in ctx.user_data:
            del ctx.user_data["awaiting_payment_for"]
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


# ─── Admin Conversation (Kode Admin Sebelumnya Sama) ────────────────

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return ConversationHandler.END

    await update.message.reply_text(
        "⚙️ *Panel Admin GELISAH*\n\nPilih aksi:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Tambah Talent", callback_data="adm_add"),
             InlineKeyboardButton("✏️ Edit Talent", callback_data="adm_edit")],
            [InlineKeyboardButton("🗑 Hapus Talent", callback_data="adm_del"),
             InlineKeyboardButton("📋 Lihat Semua", callback_data="adm_list")],
            [InlineKeyboardButton("❌ Tutup", callback_data="adm_close")],
        ]),
    )
    return ADMIN_MENU

def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Talent", callback_data="adm_add"),
         InlineKeyboardButton("✏️ Edit Talent", callback_data="adm_edit")],
        [InlineKeyboardButton("🗑 Hapus Talent", callback_data="adm_del"),
         InlineKeyboardButton("📋 Lihat Semua", callback_data="adm_list")],
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
        buttons = []
        row = []
        for i, t in enumerate(talents):
            row.append(InlineKeyboardButton(f"{i+1}. {t['name']}", callback_data=f"edit_pick_{i}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("« Kembali", callback_data="adm_back")])
        await query.edit_message_text("✏️ Pilih talent yang akan diedit:", reply_markup=InlineKeyboardMarkup(buttons))
        return EDIT_CHOOSE

    elif data == "adm_del":
        if not talents:
            await query.answer("Belum ada talent.", show_alert=True)
            return ADMIN_MENU
        buttons = []
        row = []
        for i, t in enumerate(talents):
            row.append(InlineKeyboardButton(f"🗑 {t['name']}", callback_data=f"del_pick_{i}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
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
    
    # ── HANDLER BARU UNTUK MENANGKAP FOTO BUKTI PEMBAYARAN ──
    app.add_handler(MessageHandler(filters.PHOTO, handle_payment_proof))
    
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()