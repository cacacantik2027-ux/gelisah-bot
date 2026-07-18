import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, BotCommand, BotCommandScopeChat, MessageEntity
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


async def replace_message(query, context, text, reply_markup=None, parse_mode=None, photo=None, entities=None):
    """Pengganti pola `query.edit_message_text(...)`: hapus pesan lama lalu kirim
    pesan baru sebagai gantinya, supaya perilakunya konsisten dengan tombol lain.
    Kalau `photo` diisi (file_id), pesan baru dikirim sebagai foto dengan `text`
    sebagai caption-nya. `entities` (kalau diisi) dipakai untuk emoji custom --
    tidak bisa dipakai bersamaan dengan `parse_mode`."""
    await delete_prev_message(query, context)
    chat_id = query.message.chat_id
    await send_typing(context, chat_id)
    if photo:
        return await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=text,
            parse_mode=parse_mode,
            caption_entities=entities,
            reply_markup=reply_markup,
        )
    return await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        entities=entities,
        reply_markup=reply_markup,
    )


# ==================== EMOJI PREMIUM KUSTOM ====================
# Admin bisa menyisipkan emoji premium custom di TEKS APA PUN yang disetting &
# disimpan lewat bot (sapaan, cara pesan, deskripsi channel, teks /postkatalog,
# dst) dengan menaruh placeholder berformat `{emoji:<custom_emoji_id>}` di
# tempat yang diinginkan. Bot otomatis mengubahnya jadi emoji custom asli saat
# dikirim. Cara dapat custom_emoji_id: forward pesan yang berisi emoji custom
# itu ke @userinfobot atau @RawDataBot, lihat field `custom_emoji_id`.
#
# CATATAN PENTING (batasan dari Telegram, bukan dari kode ini):
# Emoji custom dari bot HANYA tampil kalau (a) bot sudah beli username
# tambahan di Fragment, ATAU (b) dikirim ke private chat/grup/supergrup DAN
# pemilik bot masih Telegram Premium aktif. Untuk pesan yang dikirim ke
# CHANNEL (termasuk /postkatalog), opsi (b) TIDAK berlaku -- wajib opsi (a).
# Kalau syaratnya belum terpenuhi, placeholder akan tetap tampil sebagai
# emoji "⭐" biasa (bukan error), bukan versi custom-nya.

CUSTOM_EMOJI_PATTERN = re.compile(r"\{emoji:(\d+)\}")
CUSTOM_EMOJI_PLACEHOLDER = "⭐"  # karakter fallback yang dipakai untuk tiap placeholder


def _utf16_len(s):
    """Panjang string dalam satuan UTF-16 code unit -- ini satuan yang dipakai
    Telegram untuk offset/length MessageEntity, BUKAN jumlah karakter Python biasa."""
    return len(s.encode("utf-16-le")) // 2


def render_custom_emoji(text):
    """Ubah semua placeholder `{emoji:<id>}` di `text` jadi karakter emoji
    fallback + MessageEntity custom_emoji yang menempel di karakter itu.
    Balikin (text_bersih, list_entities). Kalau tidak ada placeholder sama
    sekali, balikin (text, []) apa adanya."""
    if not text or "{emoji:" not in text:
        return text, []

    entities = []
    parts = []
    last_end = 0
    pos = 0  # posisi berjalan dalam UTF-16 code unit

    for m in CUSTOM_EMOJI_PATTERN.finditer(text):
        before = text[last_end:m.start()]
        parts.append(before)
        pos += _utf16_len(before)

        parts.append(CUSTOM_EMOJI_PLACEHOLDER)
        entities.append(MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=pos,
            length=_utf16_len(CUSTOM_EMOJI_PLACEHOLDER),
            custom_emoji_id=m.group(1),
        ))
        pos += _utf16_len(CUSTOM_EMOJI_PLACEHOLDER)
        last_end = m.end()

    parts.append(text[last_end:])
    return "".join(parts), entities


def _build_utf16_offset_map(text):
    """Balikin (offsets, total): offsets[i] = posisi UTF-16 code unit sebelum
    karakter text[i]. Dipakai untuk konversi offset MessageEntity dari Telegram
    (satuan UTF-16) ke indeks string Python biasa (satuan code point)."""
    offsets = []
    total = 0
    for ch in text:
        offsets.append(total)
        total += _utf16_len(ch)
    offsets.append(total)
    return offsets, total


def extract_custom_emoji_placeholders(text, entities):
    """Kebalikan dari render_custom_emoji(): dipakai saat MENERIMA pesan dari
    admin. Kalau admin beneran kirim/tempel emoji premium custom (dari
    keyboard emoji Telegram, BUKAN ngetik ID manual), Telegram otomatis
    menyertakan entity bertipe 'custom_emoji' lengkap dengan custom_emoji_id-
    nya di pesan itu. Fungsi ini mendeteksi entity tsb dan mengubah emoji
    custom itu jadi placeholder `{emoji:<id>}` di dalam teks, supaya bisa
    disimpan sebagai teks biasa dan direnderkan ulang oleh render_custom_emoji()
    kapan pun teks itu ditampilkan lagi -- admin tidak perlu tahu/ketik ID
    emoji-nya sama sekali."""
    if not text or not entities:
        return text

    custom_entities = [e for e in entities if e.type == MessageEntity.CUSTOM_EMOJI]
    if not custom_entities:
        return text

    offsets, _ = _build_utf16_offset_map(text)
    u16_to_py = {u: i for i, u in enumerate(offsets)}

    result = text
    # Proses dari belakang ke depan supaya indeks penggantian sebelumnya
    # tidak bergeser oleh penggantian berikutnya.
    for e in sorted(custom_entities, key=lambda e: e.offset, reverse=True):
        start_py = u16_to_py.get(e.offset)
        end_py = u16_to_py.get(e.offset + e.length)
        if start_py is None or end_py is None:
            continue  # offset tidak pas di batas karakter -> lewati demi aman
        result = result[:start_py] + f"{{emoji:{e.custom_emoji_id}}}" + result[end_py:]
    return result


def entities_to_json(entities):
    """Serialize entity custom_emoji (yang beneran ditempel user lewat emoji
    keyboard Telegram, BUKAN lewat placeholder manual) ke JSON buat disimpan
    di DB. Balikin None kalau tidak ada entity custom_emoji sama sekali."""
    custom = [e for e in (entities or []) if e.type == MessageEntity.CUSTOM_EMOJI]
    if not custom:
        return None
    return json.dumps([
        {"offset": e.offset, "length": e.length, "custom_emoji_id": e.custom_emoji_id}
        for e in custom
    ])


def entities_from_json(raw):
    """Deserialize balik JSON hasil entities_to_json() jadi list MessageEntity."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [
        MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=d["offset"], length=d["length"], custom_emoji_id=d["custom_emoji_id"],
        )
        for d in data
    ]


def save_setting_with_emoji(key, message):
    """Simpan teks (message.text atau message.caption) SEKALIGUS entity emoji
    premium aslinya kalau admin menempel emoji itu langsung dari emoji
    keyboard Telegram -- jadi admin tidak perlu tahu/ketik custom_emoji_id
    sama sekali, tinggal pilih emoji-nya seperti biasa lalu kirim."""
    text = message.text or message.caption or ""
    entities = list(message.entities or message.caption_entities or [])
    db.set_setting(key, text)
    serialized = entities_to_json(entities)
    if serialized:
        db.set_setting(f"{key}_entities", serialized)
    else:
        db.delete_setting(f"{key}_entities")


def get_rendered_setting(key, default=""):
    """Ambil teks sebuah setting siap kirim (text, entities). Prioritas:
    1) entity custom_emoji ASLI tersimpan (dari emoji yang ditempel langsung),
    2) fallback ke placeholder manual `{emoji:ID}` di teksnya (lihat
    render_custom_emoji), 3) teks polos kalau tidak ada emoji sama sekali."""
    text = db.get_setting(key, default)
    stored_entities = entities_from_json(db.get_setting(f"{key}_entities"))
    if stored_entities:
        return text, stored_entities
    return render_custom_emoji(text)


def format_with_entities(template, entities, **kwargs):
    """Seperti `template.format(**kwargs)`, tapi ikut menggeser posisi entity
    custom_emoji yang tersimpan supaya tidak geser/rusak kalau ada placeholder
    seperti {bot_name}/{total_talent} yang panjang teksnya beda dari nilai
    penggantinya (dipakai khusus untuk sapaan/build_greeting_text)."""
    if not entities:
        return template.format(**kwargs), []

    pattern = re.compile(r"\{(\w+)\}")
    adjusted = [
        {"offset": e.offset, "length": e.length, "custom_emoji_id": e.custom_emoji_id}
        for e in entities
    ]
    out_parts = []
    idx = 0
    pos = 0  # posisi berjalan di teks HASIL, dalam UTF-16 code unit

    for m in pattern.finditer(template):
        key = m.group(1)
        before = template[idx:m.start()]
        out_parts.append(before)
        pos += _utf16_len(before)

        if key in kwargs:
            value = str(kwargs[key])
            delta = _utf16_len(value) - _utf16_len(m.group(0))
            for ent in adjusted:
                if ent["offset"] >= pos:
                    ent["offset"] += delta
            out_parts.append(value)
            pos += _utf16_len(value)
        else:
            out_parts.append(m.group(0))
            pos += _utf16_len(m.group(0))

        idx = m.end()

    out_parts.append(template[idx:])
    final_entities = [
        MessageEntity(type=MessageEntity.CUSTOM_EMOJI, offset=e["offset"], length=e["length"], custom_emoji_id=e["custom_emoji_id"])
        for e in adjusted
    ]
    return "".join(out_parts), final_entities


def build_greeting_text():
    """Ambil teks sapaan tersimpan (+ entity emoji premium asli kalau ada)
    lalu isi placeholder {bot_name} dan {total_talent} (jumlah talent yang ada
    saat ini di daftar talent). Balikin (text, entities)."""
    total_talent = len(db.list_talents())
    template = db.get_setting("greeting", config.DEFAULT_GREETING)
    stored_entities = entities_from_json(db.get_setting("greeting_entities"))
    if stored_entities:
        return format_with_entities(template, stored_entities, bot_name=config.BOT_NAME, total_talent=total_talent)
    text = template.format(bot_name=config.BOT_NAME, total_talent=total_talent)
    return render_custom_emoji(text)


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
    greeting_text, greeting_entities = build_greeting_text()
    greeting_photo = db.get_setting("greeting_photo")
    if greeting_photo:
        await update.message.reply_photo(
            photo=greeting_photo,
            caption=greeting_text,
            caption_entities=greeting_entities,
            reply_markup=kb.main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            greeting_text, entities=greeting_entities, reply_markup=kb.main_menu_keyboard(),
        )

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
        text, entities = get_rendered_setting("how_to_order", config.DEFAULT_HOW_TO_ORDER)
        await replace_message(query, context, text, entities=entities, reply_markup=kb.main_menu_keyboard())

    elif query.data == "menu_back":
        greeting_text, greeting_entities = build_greeting_text()
        greeting_photo = db.get_setting("greeting_photo")
        await replace_message(
            query, context, greeting_text, entities=greeting_entities,
            reply_markup=kb.main_menu_keyboard(), photo=greeting_photo,
        )


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

async def broadcast_to_admin_targets(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode="Markdown"):
    """Kirim `text` ke grup live chat (kalau LIVECHAT_GROUP_ID diisi), atau ke
    masing-masing admin secara private kalau tidak. Balikin daftar
    (chat_id, message_id) yang berhasil terkirim, supaya balasan admin bisa
    dipetakan kembali ke sesi live chat yang benar."""
    sent = []
    if config.LIVECHAT_GROUP_ID:
        try:
            msg = await context.bot.send_message(
                chat_id=int(config.LIVECHAT_GROUP_ID), text=text,
                parse_mode=parse_mode, reply_markup=reply_markup,
            )
            sent.append((msg.chat_id, msg.message_id))
            return sent
        except Exception:
            logger.exception("Gagal kirim live chat ke LIVECHAT_GROUP_ID, fallback ke admin satu-satu.")

    for admin_id in config.ADMIN_IDS:
        try:
            msg = await context.bot.send_message(
                chat_id=admin_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup,
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

    # Catatan: header "Talent / Dari / Usn / ID" TIDAK dikirim ke admin di sini.
    # Header baru dikirim (digabung dengan isi pesan) begitu user benar-benar
    # mengetik pesan pertamanya -- lihat relay_user_message().

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


def _first_message_header(talent_name, full_name, username, user_id):
    return (
        f"Talent : {talent_name}\n"
        f"Dari : {full_name}\n"
        f"Usn : @{username or '-'}\n"
        f"ID : {user_id}"
    )


async def relay_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teruskan pesan dari user (yang punya sesi live chat aktif) ke admin.
    Pesan PERTAMA dalam sesi disertai header lengkap (Talent/Dari/Usn/ID),
    pesan-pesan berikutnya cukup header ringkas (Dari saja)."""
    user = update.effective_user
    if is_admin(user.id):
        return

    session = db.get_active_session_for_user(user.id)
    if not session:
        # Tidak ada sesi live chat aktif -> abaikan, tidak ada yang perlu diteruskan.
        return

    message = update.effective_message
    is_first = db.count_relay_for_session(session["id"]) == 0
    # Kirim tombol "Akhiri Sesi" hanya sekali, menempel di pesan pertama sesi ini.
    reply_markup = kb.end_chat_keyboard(session["id"]) if is_first else None

    if is_first:
        header = _first_message_header(session["talent_name"], user.full_name, user.username, user.id)
    else:
        header = f"Dari : {user.full_name}"

    targets = []
    # Pesan teks murni (bukan media) -> gabungkan header + isi pesan jadi satu
    # pesan saja. Sengaja TANPA parse_mode supaya karakter markdown (_, *, dll)
    # yang diketik user tidak bikin pengiriman gagal.
    if message.text and not message.caption:
        body = f"{header}\n\nPesan : {message.text}" if is_first else f"{header}\nPesan :\n{message.text}"
        targets = await broadcast_to_admin_targets(context, body, reply_markup=reply_markup, parse_mode=None)
    else:
        # Pesan berupa media (foto/video/voice/stiker/dsb) -> kirim header dulu
        # sebagai pesan teks terpisah, lalu teruskan media aslinya apa adanya.
        header_targets = await broadcast_to_admin_targets(context, header, reply_markup=reply_markup, parse_mode=None)
        for admin_chat_id, message_id in header_targets:
            db.add_relay_mapping(message_id, admin_chat_id, session["id"])

        targets = await broadcast_copy_to_admin_targets(
            context, from_chat_id=update.effective_chat.id, message_id=message.message_id,
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
            text=base_text + "\n\n🔴 Sesi telah diakhiri.",
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
            save_setting_with_emoji("greeting", message)
        await message.reply_text("✅ Foto sapaan berhasil diperbarui.", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    text = (message.text or "").strip()
    if text.lower() in ("hapus foto", "hapus foto sapaan"):
        db.delete_setting("greeting_photo")
        await message.reply_text("✅ Foto sapaan berhasil dihapus.", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    save_setting_with_emoji("greeting", message)
    await message.reply_text("✅ Teks sapaan berhasil diubah.", reply_markup=kb.settings_menu_keyboard())
    return ConversationHandler.END


async def edit_howtoorder_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_setting_with_emoji("how_to_order", update.message)
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


async def postkatalog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Posting pesan berisi tombol Mini App ke channel (khusus admin).
    Pakai lewat private chat dengan bot: /postkatalog <@username_channel atau chat_id> [teks pesan]
    Tombol pakai link t.me langsung (bukan field web_app=) karena web_app= hanya
    berfungsi di private chat, tidak tampil/berfungsi kalau dipasang di channel."""
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Pakai: `/postkatalog <@username_channel atau chat_id> [teks pesan]`\n\n"
            "Contoh:\n"
            "`/postkatalog @channel_saya Cek katalog talent kami di sini 👇`\n\n"
            "Catatan: bot harus sudah jadi admin channel tsb (dengan izin kirim pesan).",
            parse_mode="Markdown",
        )
        return

    if not config.WEBAPP_URL:
        await update.message.reply_text(
            "⚠️ `WEBAPP_URL` belum diisi di environment variable, jadi belum ada Mini App "
            "untuk dipasang tombolnya.",
            parse_mode="Markdown",
        )
        return

    target_raw = context.args[0]
    target = int(target_raw) if target_raw.lstrip("-").isdigit() else target_raw

    # Ambil bagian "teks pesan" apa adanya dari pesan ASLI (bukan dari
    # context.args yang sudah kepotong per-spasi), supaya emoji premium yang
    # ditempel admin langsung dari emoji keyboard Telegram ikut kebawa utuh.
    full_text = update.message.text or ""
    parts = full_text.split(None, 2)  # ["/postkatalog", "@channel", "sisa pesan..."]
    raw_pesan = parts[2] if len(parts) > 2 else ""

    if raw_pesan:
        char_start = len(full_text) - len(raw_pesan)
        cutoff_utf16 = _utf16_len(full_text[:char_start])
        pesan_entities = [
            MessageEntity(
                type=MessageEntity.CUSTOM_EMOJI,
                offset=e.offset - cutoff_utf16,
                length=e.length,
                custom_emoji_id=e.custom_emoji_id,
            )
            for e in (update.message.entities or [])
            if e.type == MessageEntity.CUSTOM_EMOJI and e.offset >= cutoff_utf16
        ]
        if pesan_entities:
            text, entities = raw_pesan, pesan_entities
        else:
            # Tidak ada emoji asli -> tetap dukung placeholder manual {emoji:ID}
            text, entities = render_custom_emoji(raw_pesan)
    else:
        text, entities = "Yuk lihat katalog talent kami 👇", []

    bot_username = (await context.bot.get_me()).username
    try:
        await context.bot.send_message(
            chat_id=target,
            text=text,
            entities=entities,
            reply_markup=kb.webapp_channel_keyboard(
                bot_username, config.WEBAPP_SHORT_NAME,
                icon_custom_emoji_id=config.CHANNEL_BUTTON_ICON_EMOJI_ID,
            ),
        )
    except Exception as e:
        logger.warning("Gagal posting tombol Mini App ke channel %s: %s", target, e)
        await update.message.reply_text(
            f"⚠️ Gagal mengirim ke `{target_raw}`.\n"
            "Pastikan bot sudah jadi admin channel tsb (dengan izin kirim pesan) dan "
            "chat_id/username-nya benar.\n\n"
            f"Detail error: {e}",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(f"✅ Tombol Mini App berhasil diposting ke `{target_raw}`.", parse_mode="Markdown")


EXPORT_TZ = ZoneInfo("Asia/Jakarta")  # WIB (UTC+7)
EXPORT_HOUR = 6  # jam 06:00 WIB


async def send_database_backup(bot):
    """Kirim file database (bot.db) sebagai dokumen ke DM masing-masing admin.
    Dipakai baik oleh export otomatis harian maupun command /exportdb manual,
    supaya data tidak hilang kalau pindah/redeploy tanpa Volume aktif."""
    if not os.path.exists(config.DB_PATH):
        logger.warning("File database tidak ditemukan di %s, backup dilewati.", config.DB_PATH)
        return

    timestamp = datetime.now(EXPORT_TZ).strftime("%Y-%m-%d_%H-%M")
    for admin_id in config.ADMIN_IDS:
        try:
            with open(config.DB_PATH, "rb") as f:
                await bot.send_document(
                    chat_id=admin_id,
                    document=f,
                    filename=f"bot_backup_{timestamp}.db",
                    caption=f"🗄️ Backup database otomatis — {timestamp} WIB",
                )
        except Exception as e:
            logger.warning("Gagal kirim backup database ke admin %s: %s", admin_id, e)


async def daily_export_loop(bot):
    """Task background yang jalan selama bot hidup: kirim backup database ke
    DM tiap admin setiap hari jam 06:00 WIB (bukan 24 jam sejak startup)."""
    while True:
        now = datetime.now(EXPORT_TZ)
        next_run = now.replace(hour=EXPORT_HOUR, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()
        logger.info(
            "Export database otomatis berikutnya: %s WIB (dalam %.0f menit).",
            next_run.strftime("%Y-%m-%d %H:%M"), wait_seconds / 60,
        )
        await asyncio.sleep(wait_seconds)
        await send_database_backup(bot)


async def exportdb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backup database manual (khusus admin), dikirim ke DM masing-masing admin
    kapan pun dibutuhkan, tidak perlu menunggu jadwal harian."""
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⏳ Membuat backup database...")
    await send_database_backup(context.bot)


async def on_startup(application: Application):
    if config.WEBAPP_URL:
        asyncio.create_task(run_api_server())
        logger.info("api_server.py dijalankan karena WEBAPP_URL diisi.")

    asyncio.create_task(daily_export_loop(application.bot))
    logger.info("Export database otomatis aktif, terjadwal tiap hari jam %02d:00 WIB.", EXPORT_HOUR)

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
        BotCommand("postkatalog", "Posting tombol Mini App ke channel"),
        BotCommand("exportdb", "Backup database sekarang (kirim ke DM)"),
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
    app.add_handler(CommandHandler("postkatalog", postkatalog_command))
    app.add_handler(CommandHandler("exportdb", exportdb_command))
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
