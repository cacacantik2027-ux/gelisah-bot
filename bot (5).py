"""
GELISAH VCS Talent Bot with Autopayment, Group Log, In-Chat Settings, 
Dynamic Admin Management, and Session Cleanup System.
"""

import logging
import os
import sys
import json
import html  # Digunakan untuk membersihkan teks input agar anti-crash pada format HTML Telegram
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

# Mengatur logging sistem
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Memuat konfigurasi dinamis atau fallback dari environment variables
try:
    from config import BOT_TOKEN, LIVECHAT_BOT, ADMIN_IDS, LOG_GROUP_ID, QRIS_IMAGE_URL, BANK_INFO
except ImportError:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    LIVECHAT_BOT = os.getenv("LIVECHAT_BOT", "gelisahlivechat_bot")
    admin_ids_raw = os.getenv("ADMIN_IDS", "")
    ADMIN_IDS = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()]
    try:
        LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))
    except ValueError:
        LOG_GROUP_ID = 0
    QRIS_IMAGE_URL = os.getenv("QRIS_IMAGE_URL", "https://placeholder.co/qris.jpg")
    BANK_INFO = os.getenv("BANK_INFO", "BCA: 1234567890 a.n GELISAH\nDANA: 081234567890").replace("\\n", "\n")

# Penanganan database talent lokal (Fallback otomatis jika file data/talents.py tidak ada)
try:
    from data.talents import load_talents, save_talents
except ImportError:
    def load_talents():
        if os.path.exists("data/talents.json"):
            with open("data/talents.json", "r", encoding="utf-8") as f:
                return json.load(f)
        return []
    def save_talents(talents):
        if not os.path.exists("data"):
            os.makedirs("data")
        with open("data/talents.json", "w", encoding="utf-8") as f:
            json.dump(talents, f, indent=4, ensure_ascii=False)

# ConversationHandler states untuk Admin (Talent Management)
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

# ConversationHandler states untuk Pengaturan Bot (Settings Panel)
(
    SETTINGS_MENU,
    SETTINGS_INPUT,
) = range(9, 11)

SETTINGS_FILE = "data/settings.json"

def load_settings() -> dict:
    """Memuat konfigurasi global dari settings.json dengan fallback default dari config.py"""
    defaults = {
        "welcome_text": (
            "✨ *Selamat datang di GELISAH VCS Talent Agency!*\n\n"
            "Kami menyediakan talent VCS terpercaya dan siap melayani kamu.\n\n"
            "📊 Total talent tersedia: *{total_talents} talent*\n\n"
            "developer: @gosahsoknal"
        ),
        "bank_info": BANK_INFO,
        "qris_url": QRIS_IMAGE_URL,
        "livechat_bot": LIVECHAT_BOT,
        "log_group_id": LOG_GROUP_ID,
        "additional_admins": []  # List ID Admin tambahan yang diinput manual
    }
    if not os.path.exists("data"):
        os.makedirs("data")
    if not os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(defaults, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Gagal membuat settings.json: {e}")
        return defaults
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            updated = False
            for k, v in defaults.items():
                if k not in data:
                    data[k] = v
                    updated = True
            if updated:
                save_settings(data)
            return data
    except Exception as e:
        logger.error(f"Gagal membaca settings.json, menggunakan default: {e}")
        # ─── SISTEM SELF-HEALING (PERBAIKAN OTOMATIS) ───
        # Jika file JSON rusak/corrupt, tulis ulang dengan data default agar error tidak berulang terus-menerus
        try:
            save_settings(defaults)
            logger.info("🛡️ Berhasil melakukan self-healing: Berkas settings.json yang rusak telah ditulis ulang dengan default.")
        except Exception as he:
            logger.error(f"Gagal memulihkan file settings.json yang rusak: {he}")
        return defaults

def save_settings(settings: dict):
    """Menyimpan konfigurasi baru ke dalam settings.json"""
    if not os.path.exists("data"):
        os.makedirs("data")
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Gagal menyimpan settings.json: {e}")

def is_admin(user_id: int) -> bool:
    """Memeriksa apakah user adalah admin utama (config) atau admin tambahan (settings.json)"""
    if user_id in ADMIN_IDS:
        return True
    settings = load_settings()
    additional = settings.get("additional_admins", [])
    return user_id in additional

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

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Bersihkan sesi pembayaran agar steril saat kembali ke start
    if "awaiting_payment_for" in ctx.user_data:
        ctx.user_data.pop("awaiting_payment_for", None)
        
    talents = load_talents()
    settings = load_settings()
    welcome_tpl = settings.get("welcome_text")
    
    text = welcome_tpl.replace("{total_talents}", str(len(talents)))
    
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
    settings = load_settings()
    livechat = settings.get("livechat_bot", LIVECHAT_BOT)
    await update.message.reply_text(
        "📞 *Hubungi Admin GELISAH:*\n\n"
        f"💬 Live Chat: [Klik disini](https://t.me/{livechat})\n"
        "🌐 Group: @gelisahidpub\n"
        "📢 Channel: @ttalengelisah",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )

async def reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Menghapus total semua data sesi user/admin agar terhindar dari kondisi hang"""
    ctx.user_data.clear()
    await update.message.reply_text(
        "🔄 *Sesi Anda berhasil di-reset!*\n\n"
        "Semua proses yang sedang berjalan telah dihentikan secara paksa. "
        "Silakan ketik /start untuk mengulang menu utama.",
        parse_mode=ParseMode.MARKDOWN
    )

async def add_admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    if not ctx.args:
        await update.message.reply_text("⚠️ Format salah! Gunakan: `/addadmin <User_ID>`", parse_mode=ParseMode.MARKDOWN)
        return

    new_id_str = ctx.args[0]
    if not new_id_str.isdigit():
        await update.message.reply_text("❌ ID harus berupa angka bulat positif!")
        return

    new_id = int(new_id_str)
    settings = load_settings()
    additional = settings.get("additional_admins", [])

    if new_id in ADMIN_IDS or new_id in additional:
        await update.message.reply_text(f"ℹ️ User ID `{new_id}` sudah terdaftar sebagai admin.", parse_mode=ParseMode.MARKDOWN)
        return

    additional.append(new_id)
    settings["additional_admins"] = additional
    save_settings(settings)

    await update.message.reply_text(f"✅ Berhasil menambahkan `{new_id}` sebagai admin tambahan bot.", parse_mode=ParseMode.MARKDOWN)

async def del_admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    if not ctx.args:
        await update.message.reply_text("⚠️ Format salah! Gunakan: `/deladmin <User_ID>`", parse_mode=ParseMode.MARKDOWN)
        return

    target_id_str = ctx.args[0]
    if not target_id_str.isdigit():
        await update.message.reply_text("❌ ID harus berupa angka bulat positif!")
        return

    target_id = int(target_id_str)
    
    if target_id in ADMIN_IDS:
        await update.message.reply_text("❌ Tidak dapat menghapus Admin Utama yang terdaftar di berkas config sistem!")
        return

    settings = load_settings()
    additional = settings.get("additional_admins", [])

    if target_id not in additional:
        await update.message.reply_text(f"❌ User ID `{target_id}` tidak ditemukan di daftar admin tambahan.", parse_mode=ParseMode.MARKDOWN)
        return

    additional.remove(target_id)
    settings["additional_admins"] = additional
    save_settings(settings)

    await update.message.reply_text(f"✅ Berhasil menghapus `{target_id}` dari daftar admin tambahan.", parse_mode=ParseMode.MARKDOWN)

async def list_admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    settings = load_settings()
    additional = settings.get("additional_admins", [])

    msg = "👥 *DAFTAR ADMIN GELISAH BOT*\n\n"
    msg += "⭐ *Admin Utama (Config/Railway):*\n"
    for aid in ADMIN_IDS:
        msg += f"• `{aid}`\n"
    
    msg += "\n👤 *Admin Tambahan (Dynamic Database):*\n"
    if additional:
        for aid in additional:
            msg += f"• `{aid}`\n"
    else:
        msg += "_(Belum ada admin tambahan)_\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def handle_payment_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "awaiting_payment_for" not in ctx.user_data:
        return
        
    idx = ctx.user_data["awaiting_payment_for"]
    talents = load_talents()
    settings = load_settings()
    log_group_id = int(settings.get("log_group_id", LOG_GROUP_ID))
    livechat = settings.get("livechat_bot", LIVECHAT_BOT)
    
    if idx >= len(talents):
        await update.message.reply_text("❌ Terjadi kesalahan. Sesi order kedaluwarsa.")
        ctx.user_data.clear() # Reset sesi karena data tidak valid
        return

    talent = talents[idx]
    user = update.message.from_user
    photo_id = update.message.photo[-1].file_id
    
    await update.message.reply_text(
        "⏳ *Bukti transfer berhasil dikirim!*\n\n"
        "Mohon tunggu sebentar, tim admin sedang mengecek pembayaran kamu. Notifikasi akan masuk ke sini.",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Bersihkan sesi pembayaran agar user tidak mengirim foto ganda
    ctx.user_data.pop("awaiting_payment_for", None)

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
            chat_id=log_group_id,
            photo=photo_id,
            caption=admin_caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Gagal mengirim log ke grup: {e}")
        await update.message.reply_text(f"Terjadi kesalahan sistem saat mengirim bukti ke admin. Harap lapor via Livechat (@{livechat}).")

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    talents = load_talents()
    settings = load_settings()
    livechat = settings.get("livechat_bot", LIVECHAT_BOT)

    # Tombol settings yang muncul setelah conversation END (konfirmasi berhasil simpan)
    if data == "set_close":
        await query.edit_message_text("Panel pengaturan bot ditutup.")
        return

    if data == "set_back_to_menu":
        if not is_admin(query.from_user.id):
            await query.answer("⛔ Akses ditolak.", show_alert=True)
            return
        new_settings = load_settings()
        import html as _html
        welcome_esc = _html.escape(new_settings.get("welcome_text", ""))
        bank_esc    = _html.escape(new_settings.get("bank_info", ""))
        qris_esc    = _html.escape(str(new_settings.get("qris_url", "")))
        lc_esc      = _html.escape(new_settings.get("livechat_bot", ""))
        lg_esc      = _html.escape(str(new_settings.get("log_group_id", "")))
        msg = (
            "⚙️ <b>PENGATURAN BOT GELISAH</b>\n\n"
            f"✍️ <b>Welcome Text:</b>\n<code>{welcome_esc}</code>\n\n"
            f"🏦 <b>Info Bank:</b>\n<code>{bank_esc}</code>\n\n"
            f"📸 <b>QRIS URL/ID:</b>\n<code>{qris_esc}</code>\n\n"
            f"💬 <b>Livechat Bot:</b> @{lc_esc}\n"
            f"📁 <b>Log Group ID:</b> <code>{lg_esc}</code>\n\n"
            "Pilih konfigurasi yang ingin Anda ubah di bawah ini:"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ Welcome Text", callback_data="set_welcome"),
             InlineKeyboardButton("🏦 Info Bank",    callback_data="set_bank")],
            [InlineKeyboardButton("📸 QRIS Image",   callback_data="set_qris"),
             InlineKeyboardButton("💬 Livechat Bot", callback_data="set_livechat")],
            [InlineKeyboardButton("📁 Log Group ID", callback_data="set_log_group")],
            [InlineKeyboardButton("❌ Tutup Panel",  callback_data="set_close")],
        ])
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

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
            new_caption = query.message.caption + f"\n\n✅ *STATUS:* DITERIMA oleh {admin_name}"
            await query.edit_message_caption(caption=new_caption, parse_mode=ParseMode.MARKDOWN)
            
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
                    [InlineKeyboardButton("💬 Hubungi Livechat", url=f"https://t.me/{livechat}")]
                ])
            )
            
        elif action == "rej":
            new_caption = query.message.caption + f"\n\n❌ *STATUS:* DITOLAK oleh {admin_name}"
            await query.edit_message_caption(caption=new_caption, parse_mode=ParseMode.MARKDOWN)
            
            reject_msg = (
                f"❌ *PEMBAYARAN DITOLAK*\n\n"
                f"Maaf, bukti pembayaran kamu untuk talent *{talent_name}* ditolak oleh admin (dana belum masuk / bukti tidak valid).\n\n"
                f"Jika ada kendala, silakan klik tombol di bawah untuk kembali atau lapor admin."
            )
            # Menambahkan tombol kembali ke menu utama pada saat pembayaran ditolak
            await ctx.bot.send_message(
                chat_id=user_id, 
                text=reject_msg, 
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Hubungi Livechat", url=f"https://t.me/{livechat}")],
                    [InlineKeyboardButton("« Kembali ke Menu Utama", callback_data="back_main")]
                ])
            )
        return

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

    elif data.startswith("confirm_order_"):
        idx = int(data.split("_")[-1])
        if idx >= len(talents):
            await query.answer("Talent tidak ditemukan.", show_alert=True)
            return
        t = talents[idx]
        
        ctx.user_data["awaiting_payment_for"] = idx
        
        bank_info = settings.get("bank_info", BANK_INFO)
        qris_url = settings.get("qris_url", QRIS_IMAGE_URL)

        payment_text = (
            f"💳 *PEMBAYARAN*\n\n"
            f"Silakan lakukan pembayaran untuk talent *{t['name']}*.\n\n"
            f"🏦 *Informasi Rekening & E-Wallet:*\n"
            f"`{bank_info}`\n\n"
            f"📸 *Atau Scan QRIS di atas/bawah*\n\n"
            f"⚠️ *PENTING:*\n"
            f"Setelah transfer berhasil, *KIRIMKAN FOTO BUKTI TRANSFER* (Screenshot/Resi) langsung ke bot ini sekarang."
        )

        photo_to_send = qris_url
        is_local_file = False
        
        if isinstance(qris_url, str):
            if not qris_url.startswith("http") and os.path.exists(qris_url):
                try:
                    photo_to_send = open(qris_url, "rb")
                    is_local_file = True
                except Exception as e:
                    logger.error(f"Gagal membuka file lokal QRIS: {e}")

        try:
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
            await ctx.bot.send_message(
                chat_id=query.message.chat_id,
                text=payment_text + "\n\n*(Gambar QRIS gagal dimuat, silakan gunakan rekening di atas)*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="cancel_order")]])
            )
        finally:
            if is_local_file and hasattr(photo_to_send, "close"):
                photo_to_send.close()

    # ── Tombol Batalkan Pesanan Sesi Selesai (Bekerja Sempurna) ──
    elif data == "cancel_order":
        # Menghapus total seluruh status sesi transaksi user secara steril
        ctx.user_data.clear()
        
        try:
            await query.message.delete()
        except Exception:
            pass

        await ctx.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ *Pesanan Anda telah dibatalkan.*\n\nSemua riwayat transaksi sementara telah dihapus. Silakan pilih kembali talent favorit Anda pada menu utama.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali ke Menu Utama", callback_data="back_main")]])
        )

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

    elif data == "show_help":
        await query.edit_message_text(
            "📖 *Cara pakai bot GELISAH:*\n\n"
            "1. Ketik /start untuk melihat daftar talent\n"
            "2. Pilih talent yang kamu suka\n"
            "3. Tekan *💰 Pricelist* untuk melihat harga\n"
            "4. Tekan *🛒 Order Sekarang* dan ikuti instruksi\n"
            "5. Kirim bukti transfer dan tunggu konfirmasi\n\n"
            "❓ Ada pertanyaan? Hubungi admin via tombol Kontak.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali ke Menu", callback_data="back_main")]]),
        )

    elif data == "show_contact":
        await query.edit_message_text(
            "📞 *Hubungi Admin GELISAH:*\n\n"
            f"💬 Live Chat: [Klik disini](https://t.me/{livechat})\n"
            "🌐 Group: @gelisahidpub\n"
            "📢 Channel: @ttalengelisah",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali ke Menu", callback_data="back_main")]]),
        )

    elif data == "back_main":
        ctx.user_data.clear()
        welcome_tpl = settings.get("welcome_text", "✨ *Selamat datang!*\n\nPilih talent:")
        welcome_text = welcome_tpl.replace("{total_talents}", str(len(talents)))
        try:
            # Kalau pesan saat ini berupa foto, hapus dulu lalu kirim teks baru
            if query.message.photo:
                await query.message.delete()
                await ctx.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=welcome_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=main_menu_keyboard(),
                )
            else:
                # Edit pesan teks biasa langsung (tidak menambah pesan baru)
                await query.edit_message_text(
                    welcome_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=main_menu_keyboard(),
                )
        except Exception:
            await ctx.bot.send_message(
                chat_id=query.message.chat_id,
                text=welcome_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard(),
            )

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
        ctx.user_data.clear() # Sesi admin ditutup, hapus state
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
        await query.edit_message_text(
            "➕ *Tambah Talent Baru*\n\nKirim *nama* talent:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Batal", callback_data="adm_back")]]),
        )
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
        await query.edit_message_text(
            f"✏️ Kirim {prompt}:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Batal", callback_data="adm_back")]]),
        )
        return EDIT_VALUE

    elif data.startswith("del_pick_"):
        idx = int(data.split("_")[-1])
        if idx < len(talents):
            name = talents[idx]["name"]
            talents.pop(idx)
            save_talents(talents)
            await query.edit_message_text(
                f"✅ Talent *{name}* berhasil dihapus!",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Kembali ke Menu Admin", callback_data="adm_back")],
                    [InlineKeyboardButton("❌ Tutup", callback_data="adm_close")],
                ]),
            )
        ctx.user_data.clear()
        return ADMIN_MENU

    return ADMIN_MENU

async def admin_get_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_talent"] = {"name": update.message.text.strip(), "photo": "", "description": "", "pricelist": ""}
    await update.message.reply_text(
        "📷 Kirim *foto* talent (atau ketik `skip`):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Batal", callback_data="adm_back")]]),
    )
    return ADD_PHOTO

async def admin_get_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        ctx.user_data["new_talent"]["photo"] = update.message.photo[-1].file_id
    elif update.message.text and update.message.text.strip().lower() == "skip":
        ctx.user_data["new_talent"]["photo"] = ""
    else:
        ctx.user_data["new_talent"]["photo"] = update.message.text.strip()
    await update.message.reply_text(
        "📄 Kirim *deskripsi* talent:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Batal", callback_data="adm_back")]]),
    )
    return ADD_DESC

async def admin_get_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_talent"]["description"] = update.message.text.strip()
    await update.message.reply_text(
        "💰 Kirim *pricelist* talent:\n\nContoh:\n• VCS 15 menit → Rp 25.000\n• VCS 30 menit → Rp 45.000",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Batal", callback_data="adm_back")]]),
    )
    return ADD_PRICE

async def admin_get_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Menyimpan talent baru dan mengembalikan kontrol ke panel admin utama dengan callback tombol"""
    ctx.user_data["new_talent"]["pricelist"] = update.message.text.strip()
    talent = ctx.user_data.pop("new_talent")
    talents = load_talents()
    talents.append(talent)
    save_talents(talents)
    
    # Bersihkan state input agar bersih
    ctx.user_data.pop("new_talent", None)
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("« Kembali ke Menu Admin", callback_data="adm_back")],
        [InlineKeyboardButton("❌ Tutup", callback_data="adm_close")]
    ])
    
    await update.message.reply_text(
        f"✅ Talent *{talent['name']}* berhasil ditambahkan!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard
    )
    return ADMIN_MENU

async def admin_edit_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Mengupdate nilai field talent dan menyajikan tombol navigasi interaktif untuk kembali ke menu admin"""
    idx = ctx.user_data.get("edit_idx", 0)
    field = ctx.user_data.get("edit_field", "name")
    talents = load_talents()
    if idx >= len(talents):
        await update.message.reply_text("❌ Talent tidak ditemukan.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if field == "photo" and update.message.photo:
        value = update.message.photo[-1].file_id
    else:
        value = update.message.text.strip()

    talents[idx][field] = value
    save_talents(talents)
    
    # Menyiapkan tombol callback terpadu agar admin bisa langsung kembali ke menu utama panel
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("« Kembali ke Menu Admin", callback_data="adm_back")],
        [InlineKeyboardButton("❌ Tutup", callback_data="adm_close")]
    ])

    await update.message.reply_text(
        f"✅ *{talents[idx]['name']}* (field `{field}`) berhasil diperbarui!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard
    )
    
    # Bersihkan state edit sementara tapi pertahankan status ADMIN_MENU agar tombol callback bekerja
    ctx.user_data.pop("edit_idx", None)
    ctx.user_data.pop("edit_field", None)
    
    return ADMIN_MENU

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear() # Bersihkan total jika perintah dibatalkan manual
    await update.message.reply_text("❌ Operasi dibatalkan. Ketik /start untuk mulai lagi.")
    return ConversationHandler.END

async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Membuka menu utama pengaturan bot chat menggunakan ParseMode.HTML (Anti-Crash)"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak.")
        return ConversationHandler.END

    settings = load_settings()
    
    # HTML escape mencegah kegagalan parser Telegram akibat karakter khusus (*, _, etc)
    welcome_esc = html.escape(settings.get("welcome_text", ""))
    bank_esc = html.escape(settings.get("bank_info", ""))
    qris_esc = html.escape(str(settings.get("qris_url", "")))
    livechat_esc = html.escape(settings.get("livechat_bot", ""))
    log_group_esc = html.escape(str(settings.get("log_group_id", "")))

    msg = (
        "⚙️ <b>PENGATURAN BOT GELISAH</b>\n\n"
        f"✍️ <b>Welcome Text:</b>\n<code>{welcome_esc}</code>\n\n"
        f"🏦 <b>Info Bank:</b>\n<code>{bank_esc}</code>\n\n"
        f"📸 <b>QRIS URL/ID:</b>\n<code>{qris_esc}</code>\n\n"
        f"💬 <b>Livechat Bot:</b> @{livechat_esc}\n"
        f"📁 <b>Log Group ID:</b> <code>{log_group_esc}</code>\n\n"
        "Pilih konfigurasi yang ingin Anda ubah di bawah ini:"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Welcome Text", callback_data="set_welcome"),
         InlineKeyboardButton("🏦 Info Bank", callback_data="set_bank")],
        [InlineKeyboardButton("📸 QRIS Image", callback_data="set_qris"),
         InlineKeyboardButton("💬 Livechat Bot", callback_data="set_livechat")],
        [InlineKeyboardButton("📁 Log Group ID", callback_data="set_log_group")],
        [InlineKeyboardButton("❌ Tutup Panel", callback_data="set_close")]
    ])
    
    try:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Gagal memparsing HTML menu settings: {e}. Mengirim format teks biasa (Fallback).")
        # Fallback teks biasa jika parser mengalami kendala tak terduga
        fallback_msg = (
            "⚙️ PENGATURAN BOT GELISAH (FALLBACK TEXT)\n\n"
            f"Welcome Text: {settings.get('welcome_text')}\n"
            f"Info Bank: {settings.get('bank_info')}\n"
            f"QRIS: {settings.get('qris_url')}\n"
            f"Livechat Bot: @{settings.get('livechat_bot')}\n"
            f"Log Group ID: {settings.get('log_group_id')}"
        )
        await update.message.reply_text(fallback_msg, reply_markup=keyboard)

    return SETTINGS_MENU

async def settings_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Menangani pemilihan menu konfigurasi"""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "set_close":
        ctx.user_data.clear() # Tutup panel setting, bersihkan sesi
        await query.edit_message_text("Panel pengaturan bot ditutup.")
        return ConversationHandler.END

    fields_map = {
        "set_welcome": ("welcome_text", "Kirimkan *Teks Sambutan (Welcome)* yang baru.\n\nTips: Masukkan `{total_talents}` di dalam teks jika ingin bot menampilkan jumlah talent secara otomatis."),
        "set_bank": ("bank_info", "Kirimkan informasi *Bank & E-Wallet* baru (Gunakan baris baru sesuka Anda):"),
        "set_qris": ("qris_url", "Kirimkan *Gambar QRIS* baru.\n\nAnda bisa mengirimkan:\n1. Tautan langsung internet (Direct Link)\n2. Nama file lokal yang ada di server (misal: `qris.jpg`)\n3. Atau langsung kirim *Foto* QRIS ke sini agar bot menangkap File ID-nya."),
        "set_livechat": ("livechat_bot", "Kirimkan *Username Bot Livechat* yang baru (Ketik username saja, tanpa tanda '@'):"),
        "set_log_group": ("log_group_id", "Kirimkan *ID Grup Log* Telegram baru (Pastikan berawalan tanda minus `-100`, contoh: `-100123456789`):"),
    }

    if data in fields_map:
        field, prompt = fields_map[data]
        ctx.user_data["editing_field"] = field
        # Menambahkan tombol kembali saat input data pengaturan
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("« Kembali / Batal", callback_data="set_back_to_menu")]
        ])
        await query.edit_message_text(prompt, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return SETTINGS_INPUT

    return SETTINGS_MENU


async def settings_back_to_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Menangani pembatalan input pengaturan dan kembali ke menu utama settings"""
    query = update.callback_query
    await query.answer("Kembali ke menu pengaturan.")
    
    # Bersihkan field yang sedang di-edit agar tidak bentrok
    ctx.user_data.pop("editing_field", None)
    
    settings = load_settings()
    welcome_esc = html.escape(settings.get("welcome_text", ""))
    bank_esc = html.escape(settings.get("bank_info", ""))
    qris_esc = html.escape(str(settings.get("qris_url", "")))
    livechat_esc = html.escape(settings.get("livechat_bot", ""))
    log_group_esc = html.escape(str(settings.get("log_group_id", "")))

    msg = (
        "⚙️ <b>PENGATURAN BOT GELISAH</b>\n\n"
        f"✍️ <b>Welcome Text:</b>\n<code>{welcome_esc}</code>\n\n"
        f"🏦 <b>Info Bank:</b>\n<code>{bank_esc}</code>\n\n"
        f"📸 <b>QRIS URL/ID:</b>\n<code>{qris_esc}</code>\n\n"
        f"💬 <b>Livechat Bot:</b> @{livechat_esc}\n"
        f"📁 <b>Log Group ID:</b> <code>{log_group_esc}</code>\n\n"
        "Pilih konfigurasi yang ingin Anda ubah di bawah ini:"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Welcome Text", callback_data="set_welcome"),
         InlineKeyboardButton("🏦 Info Bank", callback_data="set_bank")],
        [InlineKeyboardButton("📸 QRIS Image", callback_data="set_qris"),
         InlineKeyboardButton("💬 Livechat Bot", callback_data="set_livechat")],
        [InlineKeyboardButton("📁 Log Group ID", callback_data="set_log_group")],
        [InlineKeyboardButton("❌ Tutup Panel", callback_data="set_close")]
    ])
    
    await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return SETTINGS_MENU


async def settings_get_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    field = ctx.user_data.get("editing_field")
    settings = load_settings()

    if not field:
        await update.message.reply_text("❌ Terjadi kesalahan. Silakan ketik /settings ulang.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if field == "qris_url" and update.message.photo:
        val = update.message.photo[-1].file_id
    else:
        if not update.message.text:
            await update.message.reply_text("❌ Kiriman tidak valid. Kirim teks atau foto yang sesuai:")
            return SETTINGS_INPUT
        val = update.message.text.strip()

    if field == "log_group_id":
        try:
            val = int(val)
        except ValueError:
            await update.message.reply_text("❌ Format ID salah. Harus berupa angka bulat negatif (contoh: `-100123456789`). Coba kirim ulang:")
            return SETTINGS_INPUT

    settings[field] = val
    save_settings(settings)
    ctx.user_data.pop("editing_field", None)

    field_labels = {
        "welcome_text": "Welcome Text",
        "bank_info": "Info Bank",
        "qris_url": "QRIS Image",
        "livechat_bot": "Livechat Bot",
        "log_group_id": "Log Group ID",
    }
    label = field_labels.get(field, field)

    # Tampilkan konfirmasi berhasil + tombol kembali ke panel settings
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Kembali ke Panel Settings", callback_data="set_back_to_menu")],
        [InlineKeyboardButton("❌ Tutup", callback_data="set_close")],
    ])
    await update.message.reply_text(
        f"✅ *{label}* berhasil diperbarui!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )

    ctx.user_data.clear()
    return ConversationHandler.END

def main():
    logger.info("Starting GELISAH Bot...")
    app = Application.builder().token(BOT_TOKEN).build()

    # Form Percakapan Pengelolaan Talent
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
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("reset", cancel),   # ← /reset paksa keluar dari state stuck
            CommandHandler("batal", cancel),   # ← /batal juga bisa
            CommandHandler("admin", admin_cmd),
        ],
        per_message=False,
        allow_reentry=True,
        conversation_timeout=300,  # ← auto-reset state setelah 5 menit tidak ada aktivitas
    )

    # Form Percakapan Setting Konfigurasi Bot
    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_cmd)],
        states={
            SETTINGS_MENU:  [
                CallbackQueryHandler(settings_back_to_menu, pattern="^set_back_to_menu$"),
                CallbackQueryHandler(settings_callback),
            ],
            SETTINGS_INPUT: [
                CallbackQueryHandler(settings_back_to_menu, pattern="^set_back_to_menu$"),
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), settings_get_value),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("reset", cancel),    # ← /reset paksa keluar dari state stuck
            CommandHandler("batal", cancel),    # ← /batal juga bisa
            CommandHandler("settings", settings_cmd),
        ],
        per_message=False,
        allow_reentry=True,
        conversation_timeout=300,  # ← auto-reset state setelah 5 menit tidak ada aktivitas
    )

    # Pendaftaran Perintah Pelanggan
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("contact", contact_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd)) # Tambahan perintah reset sesi untuk memulihkan hang
    app.add_handler(CommandHandler("batal", reset_cmd)) # Shortcut perintah /reset
    
    # Pendaftaran Perintah Admin Dinamis
    app.add_handler(CommandHandler("addadmin", add_admin_cmd))
    app.add_handler(CommandHandler("deladmin", del_admin_cmd))
    app.add_handler(CommandHandler("listadmin", list_admin_cmd))
    
    # Mengaktifkan Form Alur Logika Percakapan
    app.add_handler(admin_conv)
    app.add_handler(settings_conv) 
    
    # Filter Penangkap Gambar Screenshot Bukti Transfer
    app.add_handler(MessageHandler(filters.PHOTO, handle_payment_proof))
    
    # Filter Callback Tombol Dinamis Inline
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
