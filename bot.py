import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, time as dtime, timedelta
from html import escape as html_escape
from zoneinfo import ZoneInfo

WIB_TZ = ZoneInfo("Asia/Jakarta")
UTC_TZ = ZoneInfo("UTC")

from PIL import Image, ImageDraw
try:
    import pytesseract
except ImportError:  # pip package tidak terpasang -- fitur sensor otomatis akan dilewati.
    pytesseract = None
try:
    # Alternatif ke Tesseract yang murni `pip install` (tidak butuh binary
    # sistem/apt sama sekali) -- dipakai kalau tesseract-ocr tidak terpasang
    # di server. Dependensinya lebih berat (menarik PyTorch) & butuh koneksi
    # internet sekali di awal untuk unduh model deteksi teksnya.
    import easyocr
    import numpy as np
except ImportError:
    easyocr = None
    np = None

from telegram import (
    Update, BotCommand, BotCommandScopeChat, MessageEntity, InputMediaPhoto,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.constants import ChatAction, ChatType
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
import watermark
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
    ADD_BGM_FILE, ADD_BGM_TITLE,
    ADD_SPONSOR_MARQUEE_DESC,
    ADD_GROUP_ADMIN,
    EDIT_GROUP_START_MEDIA,
    EDIT_TESTIMONI_CHANNEL,
    EDIT_GROUP_ADMIN_VALUE,
    EDIT_TESTIMONI_WATERMARK,
    EDIT_PROMO_TEXT,
    EDIT_PROMO_INTERVAL,
) = range(30)

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
    "marquee_desc": "Deskripsi Melayang",
    "url": "Link",
    "photo_file_id": "Foto",
}


def is_admin(user_id):
    """True kalau user_id adalah admin utama (config.ADMIN_IDS, dari server)
    ATAU admin tambahan yang didaftarkan lewat /addadmin (tabel bot_admins)."""
    return user_id in config.ADMIN_IDS or db.is_bot_admin(user_id)


_MARKDOWN_V1_SPECIAL_RE = re.compile(r"([_*`\[])")


def _escape_markdown_v1(text):
    """Escape 4 karakter reserved di parse_mode="Markdown" (legacy, BUKAN
    MarkdownV2) Telegram: `_` `*` `` ` `` `[`. WAJIB dipakai untuk teks yang
    BUKAN ditulis admin sendiri (mis. nama talent, nama/username user) yang
    ditempel ke dalam pesan ber-parse_mode="Markdown" -- kalau tidak,
    Telegram akan menolak SELURUH pesan/foto itu dengan error "can't parse
    entities" begitu teksnya kebetulan mengandung salah satu karakter tsb
    (mis. nama talent "Sarah_23" atau "M.Rizki_"), bukan cuma formatnya yang
    berantakan tapi pesan/foto GAGAL TOTAL terkirim."""
    if not text:
        return text
    return _MARKDOWN_V1_SPECIAL_RE.sub(r"\\\1", str(text))


def _protect_content_enabled():
    """True kalau admin mengaktifkan 'Proteksi Konten' lewat /settings.
    Dibaca ulang dari database setiap kali dipanggil (bukan cache) supaya
    toggle di /settings langsung berlaku real-time tanpa perlu restart bot."""
    return db.get_setting("protect_content_enabled", "0") == "1"


def _install_protect_content_wrapper(bot):
    """Bungkus method pengiriman utama milik `bot` (send_message, send_photo,
    copy_message) SEKALI SAJA di sini, supaya toggle 'Proteksi Konten' di
    /settings otomatis berlaku ke SEMUA pengiriman pesan/foto di seluruh
    bot.py (profil talent, live chat, sapaan, dst) -- tanpa perlu menambahkan
    parameter protect_content secara manual di puluhan titik kirim pesan yang
    tersebar di file ini. Kalau pemanggil sudah menentukan protect_content
    sendiri secara eksplisit, nilai itu tetap dihormati (tidak ditimpa)."""
    original_send_message = bot.send_message
    original_send_photo = bot.send_photo
    original_copy_message = bot.copy_message

    async def send_message(*args, **kwargs):
        kwargs.setdefault("protect_content", _protect_content_enabled())
        return await original_send_message(*args, **kwargs)

    async def send_photo(*args, **kwargs):
        kwargs.setdefault("protect_content", _protect_content_enabled())
        return await original_send_photo(*args, **kwargs)

    async def copy_message(*args, **kwargs):
        kwargs.setdefault("protect_content", _protect_content_enabled())
        return await original_copy_message(*args, **kwargs)

    # `Bot`/`ExtBot` dari python-telegram-bot bersifat "frozen" (attribute-nya
    # dikunci lewat __setattr__ custom setelah objek selesai dibuat), jadi
    # `bot.send_message = ...` biasa akan ditolak dengan AttributeError.
    # object.__setattr__ melewati pengecekan frozen itu tanpa mengubah
    # perilaku lain dari objek bot.
    object.__setattr__(bot, "send_message", send_message)
    object.__setattr__(bot, "send_photo", send_photo)
    object.__setattr__(bot, "copy_message", copy_message)


# Cache watermark foto talent DI MEMORI (bukan database) -- key: photo_file_id
# Telegram asli, value: bytes JPEG yang SUDAH ditempel watermark. Menghindari
# download+proses ulang tiap kali foto talent yang sama dikirim/ditampilkan
# lagi (mis. carousel bolak-balik). Otomatis kosong lagi tiap bot restart --
# itu wajar, foto akan diproses ulang sekali lalu ke-cache lagi.
_talent_photo_watermark_cache = {}


async def _get_watermarked_talent_photo(context, talent):
    """Balikin foto talent yang SUDAH ditempel watermark logo, siap dipakai
    langsung sebagai argumen `photo=`/`media=` di send_photo atau
    InputMediaPhoto. Kalau talent tidak punya foto, balikin None. Kalau
    proses watermark gagal (mis. Telegram lagi bermasalah), fallback ke
    photo_file_id ASLI (tanpa watermark) supaya foto tetap tampil ke user
    alih-alih bikin seluruh alur error."""
    file_id = talent.get("photo_file_id")
    if not file_id:
        return None
    cached = _talent_photo_watermark_cache.get(file_id)
    if cached is not None:
        buf = io.BytesIO(cached)
        buf.name = "talent.jpg"
        return buf
    try:
        tg_file = await context.bot.get_file(file_id)
        raw = bytes(await tg_file.download_as_bytearray())
        watermarked = watermark.apply_watermark(raw)
        _talent_photo_watermark_cache[file_id] = watermarked
        buf = io.BytesIO(watermarked)
        buf.name = "talent.jpg"
        return buf
    except Exception:
        logger.exception("Gagal ambil/watermark foto talent (file_id=%s), fallback ke foto asli.", file_id)
        return file_id


def get_all_admin_ids():
    """Gabungan admin utama (config.ADMIN_IDS) + admin tambahan (/addadmin),
    tanpa duplikat, dipakai tiap kali bot perlu kirim pesan ke SEMUA admin
    (live chat, backup database, daftar perintah, dst)."""
    ids = list(config.ADMIN_IDS)
    for row in db.list_bot_admins():
        if row["user_id"] not in ids:
            ids.append(row["user_id"])
    return ids


def _admin_commands_list(public_commands):
    """Daftar perintah khusus admin, dipakai saat startup DAN saat admin baru
    ditambahkan lewat /addadmin supaya menu perintahnya langsung lengkap."""
    return public_commands + [
        BotCommand("settings", "Menu pengaturan (admin)"),
        BotCommand("groupid", "Lihat ID chat/grup ini"),
        BotCommand("postkatalog", "Posting tombol Mini App ke channel"),
        BotCommand("settestimoni", "Atur channel tujuan autopost testimoni"),
        BotCommand("posttestimoni", "Posting foto bukti tf sendiri ke channel testimoni: reply foto lalu /posttestimoni Nama Talent"),
        BotCommand("setabsentopik", "Atur topik Absen Talent, maks 5 grup (jalankan di topiknya)"),
        BotCommand("linktalent", "Tautkan akun talent (atau /linktalent daftar buat cek daftarnya)"),
        BotCommand("setpromogrup", "Atur grup Auto Promo Talent Ready, maks 5 (jalankan di grupnya)"),
        BotCommand("setpromojadwal", "Atur jadwal berulang Auto Promo (menit / off)"),
        BotCommand("postpromo", "Posting promo talent ready sekarang juga"),
        BotCommand("hapuspromo", "Hapus pesan promo terakhir sekarang juga"),
        BotCommand("exportdb", "Backup database sekarang (kirim ke DM)"),
        BotCommand("addbgm", "Upload musik BGM baru buat Mini App"),
        BotCommand("listbgm", "Lihat/hapus daftar BGM"),
        BotCommand("resetlc", "Lihat & reset sesi live chat yang macet/stuck"),
        BotCommand("addadmin", "Tambah admin baru (bisa balas live chat)"),
        BotCommand("listadmin", "Lihat daftar admin bot"),
        BotCommand("removeadmin", "Hapus admin tambahan"),
        BotCommand("cancel", "Batalkan proses yang sedang berjalan"),
    ]


def _command_mentions_bot(message, bot_username: str) -> bool:
    """True kalau command yang diketik user eksplisit menyebut username bot
    (mis. "/start@nama_bot"), BUKAN cuma "/start" polos -- dipakai untuk
    membatasi semua command di grup, lihat _group_command_allowed()."""
    if not message or not message.text or not bot_username:
        return False
    first_word = message.text.split()[0]
    return first_word.lower().endswith(f"@{bot_username}".lower())


async def _group_command_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Guard dipanggil di baris pertama SETIAP command handler ("/start",
    "/help", "/settings", dst): di private chat selalu lolos (tidak ada
    pembatasan), tapi di GRUP hanya lolos kalau user menuliskan command-nya
    dengan eksplisit menyebut username bot (mis. "/start@nama_bot"), bukan
    cuma "/start" polos -- supaya command bot tidak ikut bereaksi ke command
    bot lain / obrolan grup yang sebetulnya tidak ditujukan ke bot ini."""
    chat = update.effective_chat
    if not chat or chat.type == ChatType.PRIVATE:
        return True
    return _command_mentions_bot(update.effective_message, context.bot.username)


_CARD_OWNER_SUFFIX_RE = re.compile(r"_o(\d+)$")


def _with_owner_suffix(callback_data: str, owner_id) -> str:
    """Tempelkan suffix "_o<user_id>" di akhir `callback_data` -- dipakai saat
    membangun tombol kartu talent yang dikirim di GRUP, supaya tombolnya
    tahu siapa pemiliknya (lihat _split_owner_suffix & _enforce_card_owner).
    Kalau owner_id None (mis. kartu yang tampil di private chat, tidak perlu
    dikunci ke siapa pun), callback_data dibalikin apa adanya tanpa suffix."""
    if owner_id is None:
        return callback_data
    return f"{callback_data}_o{owner_id}"


def _split_owner_suffix(data: str):
    """Kebalikan dari _with_owner_suffix: pisahkan suffix "_o<user_id>" (kalau
    ada) dari callback_data yang diterima, balikin (data_asli_tanpa_suffix,
    owner_id). owner_id None kalau memang tidak ada suffix (kartu ini tidak
    dikunci ke siapa pun, mis. tombol menu private chat biasa) -- SEMUA
    parsing/pencocokan callback_data selanjutnya di tiap handler harus pakai
    `data` hasil fungsi ini, bukan `query.data` mentah."""
    m = _CARD_OWNER_SUFFIX_RE.search(data)
    if not m:
        return data, None
    return data[:m.start()], int(m.group(1))


def _parse_id_and_index(data: str):
    """Parse callback_data berformat `<prefix>_<id>` ATAU `<prefix>_<id>_i<index>`
    (mis. "talent_5" atau "talent_5_i2", "price_5" atau "price_5_i2") --
    dipakai supaya tombol "Kembali" dari halaman detail/pricelist talent bisa
    balik ke POSISI carousel yang sama persis (index) tempat talent itu
    dilihat, bukan selalu balik ke talent pertama/urutan awal daftar.

    Balikin (id: int, index: int|None) -- index None kalau callback_data-nya
    memang tidak membawa info index (mis. dipicu dari luar carousel, seperti
    deep link "Chat Sekarang" atau Mini App), supaya pemanggil bisa fallback
    ke perilaku lama (balik ke daftar/urutan awal) dengan aman."""
    parts = data.split("_")
    item_id = int(parts[1])
    index = None
    if len(parts) >= 3 and parts[2].startswith("i"):
        try:
            index = int(parts[2][1:])
        except ValueError:
            index = None
    return item_id, index


async def _enforce_card_owner(query, owner_id) -> bool:
    """Dipanggil di baris pertama tiap handler tombol kartu talent (nama
    talent, navigasi, tutup, pricelist, kembali). Kalau kartu ini dikunci ke
    user tertentu (owner_id bukan None, artinya kartu ini tampil di GRUP)
    DAN yang menekan tombol BUKAN user itu, balas dengan alert peringatan
    dan balikin False -- pemanggil WAJIB langsung `return` tanpa memproses
    aksi tombolnya. Kalau owner_id None (kartu tidak dikunci, mis. private
    chat) atau penekannya memang si pemilik, balikin True seperti biasa."""
    if owner_id is not None and query.from_user.id != owner_id:
        await query.answer(
            "🙅 Tombol ini cuma buat yang manggil kartu ini. Mention aku sendiri "
            "di grup kalau kamu juga mau lihat katalog talent, ya!",
            show_alert=True,
        )
        return False
    return True


async def _group_admin_command_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Sama seperti _group_command_allowed, TAPI khusus command yang memang
    ditujukan untuk dipakai admin di GRUP LIVE CHAT dengan cara diketik polos
    (mis. reply pesan user lalu ketik "/addadmin" tanpa embel-embel apa pun).

    _group_command_allowed mewajibkan "/perintah@nama_bot" di grup supaya bot
    tidak ikut bereaksi ke command bot lain / obrolan grup yang tak
    ditujukan ke bot ini -- tapi aturan itu terlalu ketat untuk command admin
    seperti /addadmin: cara pakainya sendiri (reply + ketik command polos)
    jadi tidak pernah bisa lolos di grup mana pun, termasuk grup live chat
    resmi bot.

    Di sini: private chat selalu lolos (tidak ada pembatasan). Di grup,
    kalau pengirimnya admin (is_admin) -- lolos walau command ditulis polos,
    karena seorang admin yang sengaja reply pesan user lalu mengetik command
    admin memang bermaksud memanggil bot ini, bukan bot lain. Kalau
    pengirimnya BUKAN admin, tetap pakai aturan mention lama, supaya orang
    lain di grup yang bukan admin tidak bisa memicu command admin ini sama
    sekali walau menyebut @nama_bot -- pengecekan hak akses sebenarnya tetap
    dilakukan lagi di dalam masing-masing command handler."""
    chat = update.effective_chat
    if not chat or chat.type == ChatType.PRIVATE:
        return True
    user = update.effective_user
    if user and is_admin(user.id):
        return True
    return _command_mentions_bot(update.effective_message, context.bot.username)


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


# ==================== Animasi "thinking dots" (ala Claude) ====================
# Dipakai sebagai pengganti send_message/send_photo biasa di alur-alur utama
# bot, supaya balasan bot terasa "hidup" seperti chat AI: pesan sementara
# berisi titik yang bertambah ("." -> ".." -> "...") di-edit beberapa kali
# dengan cepat (meniru animasi "sedang berpikir" ala claude.ai/DM Claude)
# sebelum berhenti di edit TERAKHIR berisi teks FINAL yang sudah diformat
# lengkap (Markdown/entities + tombol) -- teks final ini dikirim langsung
# apa adanya, TANPA efek "diketik"/typewriter bertahap, persis seperti
# balasan bot pada umumnya.
THINKING_DOTS_FRAMES = [".", "..", "..."]
THINKING_DOTS_FRAME_DELAY = 0.22
THINKING_DOTS_TOTAL_SECONDS = 0.9


async def _run_thinking_dots(edit_fn):
    """Jalankan animasi '.' -> '..' -> '...' berulang selama kurang lebih
    THINKING_DOTS_TOTAL_SECONDS detik, mengedit pesan/caption lewat `edit_fn(text)`
    (async callable) di tiap frame-nya."""
    elapsed = 0.0
    i = 0
    while elapsed < THINKING_DOTS_TOTAL_SECONDS:
        frame = THINKING_DOTS_FRAMES[i % len(THINKING_DOTS_FRAMES)]
        await edit_fn(frame)
        await asyncio.sleep(THINKING_DOTS_FRAME_DELAY)
        elapsed += THINKING_DOTS_FRAME_DELAY
        i += 1


async def _safe_edit_text(context, chat_id, message_id, text):
    """Edit isi pesan jadi teks polos, meredam error apa pun (mis. "message is
    not modified" kalau isinya kebetulan sama persis dengan sebelumnya --
    ini normal & aman diabaikan, animasi lanjut ke frame berikutnya)."""
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, disable_web_page_preview=True,
        )
    except Exception:
        pass


async def _safe_edit_caption(context, chat_id, message_id, caption):
    try:
        await context.bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption)
    except Exception:
        pass


async def send_thinking_reply(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id,
    text: str,
    reply_markup=None,
    parse_mode=None,
    entities=None,
    disable_web_page_preview=None,
    reply_to_message_id=None,
):
    """Kirim balasan TEKS dengan animasi thinking dots ala Claude, berhenti di
    teks FINAL yang sudah diformat lengkap (dikirim langsung apa adanya,
    tanpa efek "diketik" bertahap). Kalau `reply_to_message_id` diisi, pesan
    (dan animasi thinking dots-nya) tetap tampil sebagai balasan (reply) ke
    pesan tsb -- dipakai mis. saat bot di-mention di grup, supaya balasannya
    jelas ditujukan ke pesan user yang me-mention, bukan cuma dikirim lepas
    ke grup."""
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    msg = await context.bot.send_message(
        chat_id=chat_id, text=THINKING_DOTS_FRAMES[0], reply_to_message_id=reply_to_message_id,
    )
    clean = text or ""
    edit_fn = lambda t: _safe_edit_text(context, chat_id, msg.message_id, t)
    try:
        await _run_thinking_dots(edit_fn)
    except Exception:
        logger.debug("Animasi thinking dots berhenti di tengah jalan (diabaikan).")

    try:
        return await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg.message_id,
            text=clean,
            parse_mode=parse_mode,
            entities=entities,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except Exception:
        logger.exception("Gagal edit final pesan thinking dots, fallback hapus+kirim ulang.")
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
        except Exception:
            pass
        return await context.bot.send_message(
            chat_id=chat_id, text=clean, parse_mode=parse_mode, entities=entities,
            reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview,
        )


async def send_thinking_photo(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id,
    photo,
    caption: str,
    reply_markup=None,
    parse_mode=None,
    caption_entities=None,
    reply_to_message_id=None,
):
    """Versi foto dari send_thinking_reply(): fotonya langsung tampil (tidak
    bisa 'di-streaming'), CAPTION-nya dianimasikan thinking dots ala Claude
    sebelum berhenti di caption FINAL yang sudah diformat lengkap (dikirim
    langsung apa adanya, tanpa efek "diketik" bertahap). `reply_to_message_id`
    (kalau diisi) membuat foto ini tampil sebagai balasan (reply) ke pesan
    tsb -- lihat catatan di send_thinking_reply()."""
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    msg = await context.bot.send_photo(
        chat_id=chat_id, photo=photo, caption=THINKING_DOTS_FRAMES[0],
        reply_to_message_id=reply_to_message_id,
    )
    clean = caption or ""
    edit_fn = lambda t: _safe_edit_caption(context, chat_id, msg.message_id, t)
    try:
        await _run_thinking_dots(edit_fn)
    except Exception:
        logger.debug("Animasi thinking dots (foto) berhenti di tengah jalan (diabaikan).")

    try:
        return await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=msg.message_id,
            caption=clean,
            parse_mode=parse_mode,
            caption_entities=caption_entities,
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("Gagal edit final caption thinking dots, fallback hapus+kirim ulang.")
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
        except Exception:
            pass
        return await context.bot.send_photo(
            chat_id=chat_id, photo=photo, caption=clean,
            parse_mode=parse_mode, caption_entities=caption_entities, reply_markup=reply_markup,
        )


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


async def _edit_card_in_place(context, chat_id, message, has_new_photo, photo, caption, parse_mode, reply_markup):
    """Coba EDIT `message` yang sudah ada supaya jadi kartu talent yang baru
    (dipakai khusus di GRUP -- lihat show_talent_card/talent_detail_callback),
    alih-alih hapus lalu kirim ulang. Ini yang membuat kartu talent TIDAK
    hilang/tertutup sekilas setiap kali user menekan Sebelumnya/Selanjutnya
    atau memilih talent -- kartunya cuma "berganti isi" di tempat, benar-benar
    hilang hanya kalau tombol '❌ Tutup' yang ditekan.

    Hanya bisa dipakai kalau tipe pesan LAMA & BARU sama-sama foto atau
    sama-sama teks polos (Telegram tidak bisa edit foto jadi teks atau
    sebaliknya). Balikin True kalau berhasil diedit, False kalau perlu
    fallback hapus+kirim ulang (mis. tipe pesan beda, atau pesannya sudah
    terlalu lama/dihapus)."""
    old_has_photo = bool(message.photo)
    if old_has_photo != has_new_photo:
        return False
    try:
        if has_new_photo:
            await context.bot.edit_message_media(
                chat_id=chat_id,
                message_id=message.message_id,
                media=InputMediaPhoto(media=photo, caption=caption, parse_mode=parse_mode),
                reply_markup=reply_markup,
            )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message.message_id,
                text=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        return True
    except Exception:
        logger.debug("Gagal edit kartu talent di tempat, fallback hapus+kirim ulang.")
        return False


async def replace_message(query, context, text, reply_markup=None, parse_mode=None, photo=None, entities=None):
    """Pengganti pola `query.edit_message_text(...)`: hapus pesan lama lalu kirim
    pesan baru sebagai gantinya, dengan animasi 'thinking dots' ala Claude
    sebelum teks final (utuh, tanpa efek "diketik" bertahap) muncul -- supaya
    perilakunya konsisten & terasa hidup di semua tombol. Kalau `photo` diisi
    (file_id), pesan baru dikirim sebagai foto dengan `text` sebagai
    caption-nya (caption yang dianimasikan dengan cara yang sama). `entities`
    (kalau diisi) dipakai untuk emoji custom -- tidak bisa dipakai bersamaan
    dengan `parse_mode`."""
    await delete_prev_message(query, context)
    chat_id = query.message.chat_id
    if photo:
        return await send_thinking_photo(
            context, chat_id, photo, text,
            parse_mode=parse_mode, caption_entities=entities, reply_markup=reply_markup,
        )
    return await send_thinking_reply(
        context, chat_id, text,
        parse_mode=parse_mode, entities=entities, reply_markup=reply_markup,
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
    if not await _group_command_allowed(update, context):
        return
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
            "/resetlc — Lihat & reset sesi live chat yang macet/stuck (tanpa perlu buka /settings)",
            "/cancel — Batalkan proses input yang sedang berjalan di menu /settings",
        ]

    await send_thinking_reply(context, update.effective_chat.id, "\n".join(lines), parse_mode="Markdown")


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/about -- info singkat tentang bot ini: apa fungsinya, statistik singkat,
    dan kontak developer kalau ada kendala."""
    if not await _group_command_allowed(update, context):
        return
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

    await send_thinking_reply(
        context, update.effective_chat.id, "\n".join(lines),
        parse_mode="Markdown", disable_web_page_preview=True,
    )


# ==================== START & MENU ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _group_command_allowed(update, context):
        return
    chat_id = update.effective_chat.id
    greeting_text, greeting_entities = build_greeting_text()
    greeting_photo = db.get_setting("greeting_photo")
    if greeting_photo:
        await send_thinking_photo(
            context, chat_id, greeting_photo, greeting_text,
            caption_entities=greeting_entities, reply_markup=kb.main_menu_keyboard(),
        )
    else:
        await send_thinking_reply(
            context, chat_id, greeting_text,
            entities=greeting_entities, reply_markup=kb.main_menu_keyboard(),
        )

    if config.WEBAPP_URL:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Atau lihat katalog dalam tampilan app:",
            reply_markup=kb.webapp_launch_keyboard(config.WEBAPP_URL),
        )

    # Deep link "?start=chat_<talent_id>" -- dipakai tombol redirect di grup
    # (lihat chat_start_callback) supaya user yang tadinya menekan "Chat
    # Sekarang" di grup langsung tersambung ke live chat begitu private chat
    # ini terbuka, tanpa perlu memilih ulang talent-nya dari awal.
    payload = context.args[0] if context.args else None
    if payload and payload.startswith("chat_"):
        try:
            talent_id = int(payload[len("chat_"):])
        except ValueError:
            talent_id = None
        talent = db.get_talent(talent_id) if talent_id else None
        if talent:
            await start_chat_session(context, chat_id, update.effective_user, talent)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data, owner_id = _split_owner_suffix(query.data)
    if not await _enforce_card_owner(query, owner_id):
        return
    await query.answer()

    if data == "menu_noop":
        # Tombol indikator halaman (mis. "2/3"), tidak melakukan apa-apa.
        return

    if data == "menu_talents" or data.startswith("menu_talents_i"):
        index = 0
        if data.startswith("menu_talents_i"):
            try:
                index = int(data[len("menu_talents_i"):])
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
        await show_talent_card(query, context, talents, index, owner_id=owner_id)

    elif data == "menu_howtoorder":
        text, entities = get_rendered_setting("how_to_order", config.DEFAULT_HOW_TO_ORDER)
        await replace_message(query, context, text, entities=entities, reply_markup=kb.main_menu_keyboard())

    elif data == "menu_back":
        greeting_text, greeting_entities = build_greeting_text()
        greeting_photo = db.get_setting("greeting_photo")
        await replace_message(
            query, context, greeting_text, entities=greeting_entities,
            reply_markup=kb.main_menu_keyboard(), photo=greeting_photo,
        )

    elif data == "menu_close":
        # Tombol "❌ Tutup" (dipakai di grup sebagai ganti "⬅️ Kembali") --
        # cukup hapus kartunya, tidak perlu kirim/ganti dengan pesan apa pun.
        try:
            await query.message.delete()
        except Exception:
            logger.warning("Gagal menghapus pesan saat tombol 'Tutup' ditekan (mungkin sudah dihapus).")


async def send_talent_card_to_chat(context, chat_id, talents, index, close_button=False, reply_to_message_id=None, owner_id=None):
    """Kirim 1 kartu talent (foto + tombol nama + navigasi) ke `chat_id`
    langsung -- inti dari show_talent_card() di atas, tapi dilepas dari
    objek `query` supaya bisa dipanggil juga dari luar callback tombol
    (mis. dari smart-reply grup saat bot di-mention). `reply_to_message_id`
    (kalau diisi) membuat kartu ini tampil sebagai balasan (reply) ke pesan
    tsb -- kartu jadi milik pesan/user itu sendiri, bukan kartu bersama yang
    dipakai bergantian oleh semua orang di grup.

    `owner_id` (diisi kalau kartu ini dikirim ke GRUP) mengunci semua tombol
    kartu ke user tsb -- lihat catatan owner_id di kb.talent_carousel_keyboard."""
    total = len(talents)
    index = max(0, min(index, total - 1))
    talent = talents[index]

    caption = f"*{talent['name']}*"
    reply_markup = kb.talent_carousel_keyboard(talent, index, total, close_button=close_button, owner_id=owner_id)

    if talent.get("photo_file_id"):
        photo = await _get_watermarked_talent_photo(context, talent)
        await send_thinking_photo(
            context, chat_id, photo, caption,
            parse_mode="Markdown", reply_markup=reply_markup, reply_to_message_id=reply_to_message_id,
        )
    else:
        await send_thinking_reply(
            context, chat_id, caption,
            parse_mode="Markdown", reply_markup=reply_markup, reply_to_message_id=reply_to_message_id,
        )


async def show_talent_card(query, context, talents, index, owner_id=None):
    """Tampilkan 1 kartu talent (foto + tombol nama) pada satu waktu, dengan
    tombol Sebelumnya/Selanjutnya untuk pindah antar talent satu-satu -- jadi
    berasa seperti "geser halaman" alih-alih daftar tombol nama yang panjang.

    Tombol terakhir otomatis menyesuaikan tipe chat: "❌ Tutup" kalau ini
    grup, "⬅️ Kembali" kalau di private chat -- konsisten dengan kartu awal
    yang dikirim send_talent_card_to_chat dari smart-reply grup.

    Di GRUP, navigasi Sebelumnya/Selanjutnya (dan kembali dari halaman detail
    ke daftar) TIDAK menghapus/menutup kartu yang sedang tampil -- kartunya
    di-EDIT di tempat (lihat _edit_card_in_place) supaya tidak sekilas
    hilang lalu muncul lagi. Kartu di grup hanya benar-benar hilang kalau
    user menekan tombol '❌ Tutup'. Di private chat perilakunya tetap seperti
    biasa (hapus lalu kirim ulang).

    `owner_id` (dibawa dari suffix callback_data tombol yang ditekan, lihat
    _split_owner_suffix di menu_callback) diteruskan lagi ke kartu berikutnya
    supaya kartunya TETAP terkunci ke user yang sama sepanjang dia geser
    Sebelumnya/Selanjutnya, bukan cuma di kartu pertama saja."""
    chat = query.message.chat
    is_group = chat.type != ChatType.PRIVATE
    close_button = is_group

    total = len(talents)
    index = max(0, min(index, total - 1))
    talent = talents[index]
    caption = f"*{talent['name']}*"
    reply_markup = kb.talent_carousel_keyboard(talent, index, total, close_button=close_button, owner_id=owner_id)
    photo = await _get_watermarked_talent_photo(context, talent)

    if is_group:
        edited = await _edit_card_in_place(
            context, chat.id, query.message,
            has_new_photo=bool(photo), photo=photo, caption=caption,
            parse_mode="Markdown", reply_markup=reply_markup,
        )
        if edited:
            return

    await delete_prev_message(query, context)
    await send_talent_card_to_chat(context, chat.id, talents, index, close_button=close_button, owner_id=owner_id)


# ==================== FITUR ABSEN TALENT ====================
# Talent absen di salah satu topik "Absen Talent" pada grup private talent
# (diatur lewat /setabsentopik -- bisa didaftarkan di sampai 5 grup/topik
# berbeda sekaligus) dengan pesan bebas semacam "Hansel ready sampe jam 2"
# atau "Hansel off". Fitur ini KHUSUS untuk talent yang akunnya sudah
# ditautkan admin lewat /linktalent -- pengirim yang belum tertaut akan
# dapat peringatan tegas dan statusnya TIDAK dicatat (lihat
# absen_message_handler). Status tersimpan di tabel talent_status, otomatis
# balik jadi TIDAK READY kalau jam ready-nya sudah lewat (lihat
# _check_talent_status_expiry), dan di-reset total tiap jam 06:00 WIB
# lengkap dengan notifikasi pengingat ke semua topik terdaftar (lihat
# _daily_absen_reset).

_NOT_READY_KEYWORDS = (
    "tidak ready", "gak ready", "ga ready", "nggak ready", "not ready",
    "off", "libur", "istirahat", "cuti", "sakit", "unavailable", "kosong dulu",
)
_READY_KEYWORDS = ("ready", "standby", "available", "on")
_ABSEN_TIME_RE = re.compile(
    r"jam\s*(\d{1,2})(?:[.:](\d{2}))?\s*(pagi|siang|sore|malam)?", re.IGNORECASE,
)
_MAX_ABSEN_TOPICS = 5


def _parse_absen_status(text: str):
    """Baca satu pesan absen, balikin `(is_ready: bool, ready_until_wib: datetime|None)`,
    atau `None` kalau pesan ini TIDAK dikenali sebagai absen sama sekali
    (supaya obrolan santai lain di topik yang sama tidak ikut kepicu/dibalas bot).

    Aturan jam: "jam 2" tanpa keterangan pagi/siang/sore/malam DIASUMSIKAN sore
    (14:00) kalau angkanya 1-7 -- karena di konteks kerja talent, "jam 2" hampir
    selalu berarti siang/sore. Kalau maksudnya dini hari, tulis "jam 2 pagi" biar
    jelas. Contoh: "Hansel ready sampe jam 2" atau "Hansel off"."""
    low = text.lower()

    is_ready = None
    if any(kw in low for kw in _NOT_READY_KEYWORDS):
        is_ready = False
    elif any(kw in low for kw in _READY_KEYWORDS):
        is_ready = True
    if is_ready is None:
        return None

    ready_until = None
    if is_ready:
        m = _ABSEN_TIME_RE.search(low)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            qualifier = m.group(3)
            if qualifier in ("siang", "sore", "malam") and hour < 12:
                hour += 12
            elif qualifier is None and 1 <= hour <= 7:
                hour += 12  # heuristik: "jam 2" polos -> asumsi 14:00, bukan dini hari
            hour %= 24
            ready_until = datetime.now(WIB_TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)

    return is_ready, ready_until


def _match_talent_from_text(text_lower: str, talents):
    """Cari talent yang namanya disebut di teks absen (word-boundary, supaya
    nama pendek mis. 'hansel' tidak nyangkut ke kata lain yang kebetulan
    mengandungnya). Dipakai untuk PESAN PERINGATAN saja (bukan untuk
    menentukan status siapa yang dicatat -- itu murni dari tautan
    /linktalent, lihat absen_message_handler)."""
    for t in talents:
        name = (t["name"] or "").strip().lower()
        if name and re.search(rf"\b{re.escape(name)}\b", text_lower):
            return t
    return None


def _get_absen_topics():
    """Balikin daftar topik Absen Talent yang terdaftar, tiap item berupa
    {"group_id": str, "topic_id": str}, maksimal _MAX_ABSEN_TOPICS. Otomatis
    migrasi dari setting versi lama (single topik: absen_group_id +
    absen_topic_id) kalau ketemu, supaya upgrade dari versi sebelumnya tidak
    kehilangan pengaturan yang sudah ada."""
    raw = db.get_setting("absen_topics")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, TypeError):
            logger.warning("Gagal parse setting absen_topics, dianggap kosong: %r", raw)

    old_group = db.get_setting("absen_group_id")
    old_topic = db.get_setting("absen_topic_id")
    if old_group and old_topic:
        migrated = [{"group_id": str(old_group), "topic_id": str(old_topic)}]
        _save_absen_topics(migrated)
        db.delete_setting("absen_group_id")
        db.delete_setting("absen_topic_id")
        return migrated
    return []


def _save_absen_topics(topics):
    db.set_setting("absen_topics", json.dumps(topics))


def _find_absen_topic(chat_id, thread_id):
    """Cek apakah pasangan grup+topik ini ada di daftar topik Absen Talent
    yang terdaftar. Balikin entrinya kalau ada, `None` kalau tidak."""
    chat_id = str(chat_id)
    thread_id = str(thread_id or "")
    for t in _get_absen_topics():
        if t.get("group_id") == chat_id and t.get("topic_id") == thread_id:
            return t
    return None


def _get_linked_talent_id(user_id):
    """Ambil talent_id yang ditautkan ke akun Telegram user_id ini lewat
    /linktalent, atau None kalau akun ini belum ditautkan ke talent manapun."""
    val = db.get_setting(f"absen_link_{user_id}")
    try:
        return int(val) if val else None
    except (TypeError, ValueError):
        return None


def _get_talent_link_registry():
    """Balikin `[{"user_id": int, "talent_id": int, "label": str}, ...]` --
    catatan SEMUA tautan akun-talent yang pernah dibuat lewat /linktalent,
    dipakai khusus untuk menampilkan daftarnya (`/linktalent daftar`).
    Sumber kebenaran UTAMA untuk cek tautan tetap key `absen_link_{user_id}`
    (dibaca _get_linked_talent_id) -- registry ini cuma index tambahan
    supaya bisa di-list, jadi kalau keduanya sampai tidak sinkron (mis. data
    lama dari sebelum fitur ini ada), tetap _get_linked_talent_id yang jadi
    acuan valid/tidaknya sebuah tautan."""
    raw = db.get_setting("absen_link_registry")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Gagal parse setting absen_link_registry, dianggap kosong: %r", raw)
        return []


def _save_talent_link_registry(registry):
    db.set_setting("absen_link_registry", json.dumps(registry))


def _link_talent(user_id, talent_id, label=None):
    db.set_setting(f"absen_link_{user_id}", str(talent_id))
    registry = [r for r in _get_talent_link_registry() if r.get("user_id") != user_id]
    registry.append({"user_id": user_id, "talent_id": talent_id, "label": label})
    _save_talent_link_registry(registry)


def _unlink_talent(user_id):
    db.delete_setting(f"absen_link_{user_id}")
    registry = [r for r in _get_talent_link_registry() if r.get("user_id") != user_id]
    _save_talent_link_registry(registry)


def _get_effective_talent_status(talent_id):
    """Ambil status talent, tapi kalau ready_until-nya sudah lewat, langsung
    di-flip ke TIDAK READY dulu (lazy expiry) sebelum dikembalikan. Ini
    jaring pengaman kalau job_queue background (_check_talent_status_expiry)
    tidak jalan, mis. karena APScheduler/`python-telegram-bot[job-queue]`
    belum terpasang di server -- jadi status yang DIBACA user selalu akurat
    walau job periodiknya tidak aktif."""
    status = db.get_talent_status(talent_id)
    if not status or not status["is_ready"] or not status["ready_until"]:
        return status
    ready_until = datetime.strptime(status["ready_until"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC_TZ)
    if datetime.now(UTC_TZ) >= ready_until:
        db.expire_talent_statuses(datetime.now(UTC_TZ).strftime("%Y-%m-%d %H:%M:%S"))
        return db.get_talent_status(talent_id)
    return status


def _talent_status_badge(talent_id) -> str:
    """Baris status ready/tidak-ready untuk ditempel di caption kartu talent
    (lihat _talent_caption). String kosong kalau talent belum pernah absen
    sama sekali."""
    status = _get_effective_talent_status(talent_id)
    if not status:
        return ""
    if status["is_ready"]:
        until = status.get("ready_until")
        if until:
            local = datetime.strptime(until, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC_TZ).astimezone(WIB_TZ)
            return f"\n\n🟢 *READY* sampai {local.strftime('%H:%M')} WIB"
        return "\n\n🟢 *READY*"
    return "\n\n🔴 *TIDAK READY*"


def _talent_caption(talent) -> str:
    """Caption standar kartu detail talent (nama + deskripsi + badge status
    ready/tidak-ready) -- dipakai bareng oleh show_talent_detail &
    talent_detail_callback supaya statusnya konsisten di mana pun kartu
    talent ditampilkan ke user."""
    return f"*{talent['name']}*\n\n{talent['description']}{_talent_status_badge(talent['id'])}"


async def absen_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deteksi pesan absen di salah satu topik "Absen Talent" yang terdaftar
    (lihat /setabsentopik -- bisa sampai 5 grup/topik). Kalau chat+topik ini
    tidak terdaftar, ATAU teksnya tidak dikenali sebagai format absen -- bot
    diam saja (tidak balas apa pun), supaya obrolan santai lain tidak ikut
    kepicu.

    Kalau teksnya DIKENALI sebagai format absen tapi pengirimnya BUKAN
    talent yang sudah ditautkan admin lewat /linktalent -- statusnya TIDAK
    dicatat sama sekali, dan bot membalas dengan peringatan tegas (fitur ini
    memang dikunci khusus untuk talent terdaftar, bukan siapa saja yang
    kebetulan chat di topik itu)."""
    message = update.effective_message
    if not _find_absen_topic(update.effective_chat.id, message.message_thread_id):
        return

    text = (message.text or "").strip()
    if not text:
        return

    parsed = _parse_absen_status(text)
    if parsed is None:
        return
    is_ready, ready_until_wib = parsed

    sender = update.effective_user
    talent_id = _get_linked_talent_id(sender.id) if sender else None
    talent = db.get_talent(talent_id) if talent_id else None

    if not talent:
        uname = f"@{sender.username}" if sender and sender.username else (sender.first_name if sender else "Pengguna")
        name_hit = _match_talent_from_text(text.lower(), db.list_talents())
        if name_hit:
            alasan = (
                f"Nama *{name_hit['name']}* terdeteksi di pesanmu, tapi akun Telegram kamu "
                f"({uname}, id `{sender.id if sender else '-'}`) *belum ditautkan* ke talent tersebut."
            )
        else:
            alasan = (
                f"Baik nama talent maupun akun Telegram kamu ({uname}, id `{sender.id if sender else '-'}`) "
                "*tidak ditemukan sama sekali* di daftar talent yang terdaftar."
            )
        await message.reply_text(
            "🚫 *ABSEN DITOLAK -- BUKAN TALENT TERDAFTAR*\n\n"
            f"{alasan}\n\n"
            "Fitur absen ini KHUSUS untuk talent resmi yang akunnya sudah ditautkan admin lewat "
            "`/linktalent`. Status *TIDAK* dicatat sampai akun kamu ditautkan.\n\n"
            "Kalau kamu talent resmi, segera hubungi admin.",
            parse_mode="Markdown",
            message_thread_id=message.message_thread_id,
        )
        return

    ready_until_utc_str = None
    if ready_until_wib:
        ready_until_utc_str = ready_until_wib.astimezone(UTC_TZ).strftime("%Y-%m-%d %H:%M:%S")

    db.set_talent_status(talent["id"], is_ready, ready_until_utc_str, text, sender.id)

    if is_ready:
        confirm = f"🟢 Absen tercatat: *{talent['name']}* READY"
        if ready_until_wib:
            confirm += f" sampai {ready_until_wib.strftime('%H:%M')} WIB"
        confirm += "."
    else:
        confirm = f"🔴 Absen tercatat: *{talent['name']}* TIDAK READY."

    await message.reply_text(confirm, parse_mode="Markdown", message_thread_id=message.message_thread_id)


async def _check_talent_status_expiry(context: ContextTypes.DEFAULT_TYPE):
    """Job periodik (JobQueue): cek talent yang jam ready-nya sudah lewat,
    otomatis flip ke TIDAK READY, lalu kabari di SEMUA topik Absen Talent
    yang terdaftar (bisa lebih dari satu grup, lihat /setabsentopik) supaya
    admin/talent lain tahu. Kalau JobQueue tidak aktif di server (APScheduler
    belum terpasang), status tetap akurat saat DIBACA berkat lazy expiry di
    _get_effective_talent_status -- job ini cuma soal notifikasi proaktifnya."""
    now_utc_str = datetime.now(UTC_TZ).strftime("%Y-%m-%d %H:%M:%S")
    expired_ids = db.expire_talent_statuses(now_utc_str)
    if not expired_ids:
        return

    topics = _get_absen_topics()
    if not topics:
        return

    for talent_id in expired_ids:
        talent = db.get_talent(talent_id)
        name = talent["name"] if talent else f"#{talent_id}"
        for topic in topics:
            try:
                await context.bot.send_message(
                    chat_id=int(topic["group_id"]),
                    text=f"⏰ *{name}* otomatis jadi TIDAK READY (jam ready sudah lewat).",
                    parse_mode="Markdown",
                    message_thread_id=int(topic["topic_id"]) if topic.get("topic_id") else None,
                )
            except Exception:
                logger.exception(
                    "Gagal kirim notifikasi auto-expire status talent #%s ke grup %s",
                    talent_id, topic.get("group_id"),
                )


async def _daily_absen_reset(context: ContextTypes.DEFAULT_TYPE):
    """Job harian (JobQueue, jalan tiap jam 06:00 WIB): reset status SEMUA
    talent balik ke TIDAK READY/belum absen untuk hari itu, lalu kirim
    notifikasi pengingat ke SEMUA topik Absen Talent yang terdaftar supaya
    talent absen ulang. Kalau JobQueue tidak aktif, job ini tidak akan
    berjalan otomatis -- lihat warning saat startup bot."""
    talents = db.list_talents()
    for t in talents:
        try:
            db.set_talent_status(t["id"], False, None, "[auto-reset harian 06:00 WIB]", 0)
        except Exception:
            logger.exception("Gagal reset status absen harian untuk talent #%s", t["id"])

    topics = _get_absen_topics()
    if not topics:
        return

    reminder = (
        "🌅 *Selamat pagi!*\n\n"
        "Status ready semua talent sudah di-reset untuk hari ini. Talent, tolong segera "
        "absen ya -- kirim pesan seperti \"Hansel ready sampe jam 5\" atau \"Hansel off\" "
        "di topik ini."
    )
    for topic in topics:
        try:
            await context.bot.send_message(
                chat_id=int(topic["group_id"]),
                text=reminder,
                parse_mode="Markdown",
                message_thread_id=int(topic["topic_id"]) if topic.get("topic_id") else None,
            )
        except Exception:
            logger.exception("Gagal kirim reminder absen harian ke grup %s", topic.get("group_id"))


async def setabsentopik_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Jalankan DI DALAM topik berjudul "Absen Talent" pada grup private
    talent (khusus admin) untuk mendaftarkan topik itu sebagai sumber absen.
    Bisa dijalankan di BEBERAPA grup/topik berbeda sekaligus -- maksimal 5
    topik terdaftar bersamaan. (Catatan: bot tidak bisa memverifikasi judul
    topik lewat Telegram Bot API, jadi pastikan sendiri topiknya memang
    dibuat dengan judul "Absen Talent" sebelum menjalankan command ini.)

    Pakai:
    - `/setabsentopik` (di dalam topiknya) -- daftarkan topik ini.
    - `/setabsentopik hapus` (di dalam topiknya) -- lepas topik ini saja.
    - `/setabsentopik daftar` -- lihat semua topik yang sedang terdaftar."""
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "Command ini harus dijalankan DI DALAM topik \"Absen Talent\" pada grup, bukan di private chat."
        )
        return
    if not is_admin(update.effective_user.id):
        return

    if context.args and context.args[0].lower() in ("daftar", "list"):
        topics = _get_absen_topics()
        if not topics:
            await update.message.reply_text("Belum ada topik Absen Talent yang terdaftar.")
            return
        lines = [f"📋 *Topik Absen Talent terdaftar* ({len(topics)}/{_MAX_ABSEN_TOPICS}):", ""]
        for i, t in enumerate(topics, 1):
            lines.append(f"{i}. Grup `{t.get('group_id')}` / topik `{t.get('topic_id')}`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if context.args and context.args[0].lower() == "hapus":
        topics = _get_absen_topics()
        thread_id = update.effective_message.message_thread_id
        new_topics = [
            t for t in topics
            if not (t.get("group_id") == str(update.effective_chat.id) and t.get("topic_id") == str(thread_id or ""))
        ]
        if len(new_topics) == len(topics):
            await update.message.reply_text("Topik ini memang belum terdaftar sebagai Absen Talent.")
            return
        _save_absen_topics(new_topics)
        await update.message.reply_text(
            f"✅ Topik ini sudah dilepas dari daftar Absen Talent ({len(new_topics)}/{_MAX_ABSEN_TOPICS} tersisa)."
        )
        return

    thread_id = update.effective_message.message_thread_id
    if not thread_id:
        await update.message.reply_text(
            "⚠️ Sepertinya ini bukan topik terpisah (mungkin grup belum mengaktifkan Topics, atau ini "
            "topik \"General\"). Aktifkan Topics di pengaturan grup, buat topik berjudul \"Absen Talent\", "
            "lalu jalankan command ini DI DALAM topik itu."
        )
        return

    topics = _get_absen_topics()
    if _find_absen_topic(update.effective_chat.id, thread_id):
        await update.message.reply_text("Topik ini sudah terdaftar sebagai Absen Talent sebelumnya.")
        return
    if len(topics) >= _MAX_ABSEN_TOPICS:
        await update.message.reply_text(
            f"⚠️ Sudah ada {_MAX_ABSEN_TOPICS} grup/topik Absen Talent terdaftar (batas maksimal). "
            "Lepas salah satu dulu dengan `/setabsentopik hapus` (dijalankan DI DALAM topik yang mau "
            "dilepas) sebelum menambah yang baru.",
            parse_mode="Markdown",
        )
        return

    topics.append({"group_id": str(update.effective_chat.id), "topic_id": str(thread_id)})
    _save_absen_topics(topics)
    await update.message.reply_text(
        f"✅ Topik ini diatur sebagai topik *Absen Talent* ({len(topics)}/{_MAX_ABSEN_TOPICS} grup terdaftar).\n\n"
        "Mulai sekarang, pesan absen di sini (mis. \"Hansel ready sampe jam 2\" atau \"Hansel off\") "
        "akan otomatis dideteksi & status talent-nya diperbarui -- KHUSUS untuk talent yang akunnya "
        "sudah ditautkan admin lewat `/linktalent`.",
        parse_mode="Markdown",
    )


def _get_seen_users_cache():
    """Balikin dict `{"username_lower": {"user_id": int, "label": str}}` --
    cache ringan (JSON via settings, TANPA skema DB baru, sama polanya
    seperti _get_talent_link_registry) berisi user yang PERNAH 'terlihat'
    bot ini (lewat /start, pesan private, pesan grup, atau tombol yang
    ditekan -- lihat _track_seen_user). Dipakai sebagai fallback oleh
    _resolve_telegram_username, lihat catatan di sana KENAPA fallback ini
    perlu ada sama sekali."""
    raw = db.get_setting("seen_users_by_username")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        logger.warning("Gagal parse setting seen_users_by_username, dianggap kosong: %r", raw)
        return {}


def _remember_seen_user(user):
    """Simpan/perbarui satu entri di _get_seen_users_cache() untuk `user`
    (objek `telegram.User`). No-op kalau user None, bot, atau memang tidak
    punya username publik (tidak ada apa pun yang bisa dicocokkan nanti)."""
    if not user or user.is_bot or not user.username:
        return
    cache = _get_seen_users_cache()
    cache[user.username.lower()] = {"user_id": user.id, "label": f"@{user.username}"}
    db.set_setting("seen_users_by_username", json.dumps(cache))


async def _track_seen_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler generik (didaftarkan di GROUP DISPATCH TERSENDIRI, lihat
    main()) yang cuma mencatat _remember_seen_user() untuk SETIAP update
    yang punya effective_user -- pesan private, pesan grup, maupun tombol
    yang ditekan. Sengaja dipasang terpisah dari alur command/menu manapun
    supaya tidak pernah "merebut" update dari handler lain (lihat catatan
    per-group dispatch PTB di absen_message_handler)."""
    _remember_seen_user(update.effective_user)


async def _resolve_telegram_username(context: ContextTypes.DEFAULT_TYPE, username: str):
    """Resolve @username Telegram jadi (user_id, label_tampilan).

    CATATAN PENTING soal keterbatasan Bot API: `getChat` Telegram SECARA
    RESMI cuma didokumentasikan bekerja untuk channel/supergroup publik lewat
    @username -- BUKAN untuk private chat/akun user biasa. Untuk user biasa,
    getChat by @username hanya kadang berhasil KALAU bot itu kebetulan sudah
    "mengenal" user tsb sebelumnya (japri sekali, atau nongol di grup yang
    sama), dan GAGAL untuk user yang belum pernah berinteraksi APAPUN dengan
    bot ini -- inilah kenapa dulu `/linktalent @username Nama Talent`
    kelihatan seperti "tidak berfungsi" dan admin jadi wajib reply pesan
    talent-nya setiap kali.

    Supaya mode langsung (`@username`) benar-benar berguna, coba DUA cara,
    urut dari yang paling akurat:
    1. `getChat` langsung -- kalau berhasil, datanya paling fresh (ambil
       username/nama terbaru dari Telegram saat ini juga).
    2. Fallback ke cache `_get_seen_users_cache()` -- user_id yang PERNAH
       tercatat bot ini punya username itu (lewat /start, chat, pesan grup,
       atau tombol -- lihat _track_seen_user). Ini yang bikin mode langsung
       akhirnya bisa dipakai untuk talent yang sudah pernah /start bot ini
       atau pernah kelihatan di grup mana pun tempat bot ada, TANPA admin
       perlu reply pesannya lagi.

    Balikin (None, None) kalau KEDUA cara gagal -- di titik ini memang tidak
    ada info user_id yang bisa dipakai sama sekali, satu-satunya jalan
    tersisa adalah reply pesan asli dari talent tsb."""
    uname = username[1:] if username.startswith("@") else username
    try:
        chat = await context.bot.get_chat(f"@{uname}")
        if chat.type == ChatType.PRIVATE:
            label = f"@{chat.username}" if chat.username else (chat.first_name or uname)
            return chat.id, label
    except Exception:
        logger.warning("Gagal resolve username Telegram %r lewat getChat, coba cache lokal.", uname)

    cached = _get_seen_users_cache().get(uname.lower())
    if cached and cached.get("user_id"):
        return cached["user_id"], cached.get("label", f"@{uname}")

    return None, None


async def linktalent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Khusus admin. Tautkan akun Telegram seorang talent ke data talent-nya
    di database, supaya HANYA akun itu yang boleh mengubah status absen
    talent tsb (lihat FITUR ABSEN TALENT -- absen_message_handler menolak
    siapa pun yang belum tertaut dengan peringatan tegas).

    DUA cara pakai (pilih salah satu):
    1. Langsung sebut usernamenya, TANPA perlu reply pesan apa pun:
         `/linktalent @username Nama Talent`
       (resolve lewat Bot API getChat, dengan fallback ke cache "user pernah
       kelihatan" -- lihat _resolve_telegram_username -- SELAMA akun itu
       pernah /start bot ini, chat langsung, atau muncul di grup mana pun
       tempat bot ada. Kalau belum pernah sama sekali, pakai cara 2.)
    2. Reply pesan APAPUN dari talent yang bersangkutan, lalu:
         `/linktalent Nama Talent`
       (SELALU berhasil, tidak bergantung apakah bot sudah "kenal" akunnya
       -- pakai ini kalau cara 1 gagal resolve.)

    Untuk melepas tautan (mis. talent resign / ganti akun):
      `/linktalent hapus @username`  ATAU  reply pesan talent lalu `/linktalent hapus`

    Untuk cek daftar tautan yang sudah diinput (siapa saja yang sudah
    ditautkan ke talent mana, lengkap status absen terkininya):
      `/linktalent daftar`"""
    if not is_admin(update.effective_user.id):
        return

    args = context.args or []
    replied = update.message.reply_to_message

    # ---- Mode daftar (cek semua tautan yang sudah diinput) ----
    if args and args[0].lower() in ("daftar", "list"):
        registry = _get_talent_link_registry()
        # Cross-check ke sumber kebenaran (_get_linked_talent_id) supaya
        # entry yang sudah tidak valid (mis. data lama sebelum ada registry,
        # atau sempat tidak sinkron) tidak ikut ditampilkan sebagai aktif.
        entries = []
        for r in registry:
            user_id = r.get("user_id")
            active_talent_id = _get_linked_talent_id(user_id) if user_id is not None else None
            if active_talent_id is None or active_talent_id != r.get("talent_id"):
                continue
            talent = db.get_talent(active_talent_id)
            if not talent:
                continue
            entries.append((r, talent))

        if not entries:
            await update.message.reply_text(
                "Belum ada akun talent yang ditautkan lewat /linktalent."
            )
            return

        lines = [f"📋 *Daftar Tautan Absen Talent* ({len(entries)}):", ""]
        for r, talent in entries:
            label = r.get("label") or f"id `{r.get('user_id')}`"
            status = _get_effective_talent_status(talent["id"])
            if status and status["is_ready"]:
                until = status.get("ready_until")
                if until:
                    local = datetime.strptime(until, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC_TZ).astimezone(WIB_TZ)
                    status_label = f"🟢 ready sampai {local.strftime('%H:%M')} WIB"
                else:
                    status_label = "🟢 ready"
            elif status:
                status_label = "🔴 tidak ready"
            else:
                status_label = "⚪ belum pernah absen"
            lines.append(f"• *{talent['name']}* -- {label} (id `{r.get('user_id')}`) -- {status_label}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    # ---- Mode hapus (lepas tautan) ----
    if args and args[0].lower() == "hapus":
        if len(args) >= 2 and args[1].startswith("@"):
            target_id, target_label = await _resolve_telegram_username(context, args[1])
            if target_id is None:
                await update.message.reply_text(
                    f"⚠️ Tidak bisa menemukan user dengan username {args[1]}. Pastikan usernamenya "
                    "benar, atau lepas lewat reply: reply pesan talent itu lalu `/linktalent hapus`.",
                    parse_mode="Markdown",
                )
                return
            _unlink_talent(target_id)
            await update.message.reply_text(
                f"✅ Tautan absen untuk {target_label} (id `{target_id}`) sudah dilepas.", parse_mode="Markdown",
            )
            return
        if replied and replied.from_user:
            _unlink_talent(replied.from_user.id)
            await update.message.reply_text(
                f"✅ Tautan absen untuk {replied.from_user.first_name} (id `{replied.from_user.id}`) sudah dilepas.",
                parse_mode="Markdown",
            )
            return
        await update.message.reply_text(
            "⚠️ Sertakan usernamenya (`/linktalent hapus @username`), atau reply pesan talent-nya "
            "lalu jalankan `/linktalent hapus`.",
            parse_mode="Markdown",
        )
        return

    # ---- Mode langsung: /linktalent @username Nama Talent ----
    if args and args[0].startswith("@"):
        username = args[0]
        name_query = " ".join(args[1:]).strip()
        if not name_query:
            await update.message.reply_text(
                "⚠️ Sertakan juga nama talent-nya. Contoh: `/linktalent @username Hansel`",
                parse_mode="Markdown",
            )
            return

        target_id, target_label = await _resolve_telegram_username(context, username)
        if target_id is None:
            await update.message.reply_text(
                f"⚠️ Tidak bisa menemukan user dengan username {username}.\n\n"
                "Ini terjadi kalau: (a) usernamenya salah ketik/sudah ganti, ATAU (b) Telegram "
                "belum pernah 'mengenalkan' akun itu ke bot ini -- akun tsb perlu PERNAH "
                "/start bot ini, chat langsung, atau setidaknya muncul sekali di grup mana pun "
                "tempat bot ada, baru bisa dicari lewat @username.\n\n"
                "Alternatif tercepat: reply salah satu pesan DARI talent tsb (di grup atau private "
                "chat), lalu jalankan "
                f"`/linktalent {name_query}` (tanpa @username) -- cara ini SELALU berhasil karena "
                "tidak bergantung pada apakah bot sudah 'kenal' akunnya atau belum.",
                parse_mode="Markdown",
            )
            return

        talents = db.list_talents()
        talent = next((t for t in talents if (t["name"] or "").strip().lower() == name_query.lower()), None)
        if not talent:
            talent = _match_talent_from_text(name_query.lower(), talents)
        if not talent:
            await update.message.reply_text(
                f"⚠️ Talent dengan nama \"{name_query}\" tidak ditemukan di daftar talent. "
                "Cek ejaan namanya, atau tambahkan talent-nya dulu lewat menu /settings."
            )
            return

        _link_talent(target_id, talent["id"], label=target_label)
        await update.message.reply_text(
            f"✅ Akun Telegram {target_label} (id `{target_id}`) berhasil ditautkan sebagai talent *{talent['name']}*.\n\n"
            f"Mulai sekarang, HANYA akun ini yang bisa mengubah status absen untuk *{talent['name']}*.",
            parse_mode="Markdown",
        )
        return

    # ---- Mode reply (cara lama, dipertahankan sebagai fallback) ----
    if not replied or not replied.from_user:
        await update.message.reply_text(
            "⚠️ Pakai salah satu cara:\n"
            "1. `/linktalent @username Nama Talent` -- langsung, tidak perlu reply.\n"
            "2. Reply pesan dari talent-nya, lalu `/linktalent Nama Talent`.\n\n"
            "Untuk melepas tautan: `/linktalent hapus @username` atau reply pesan talent lalu "
            "`/linktalent hapus`.\n\n"
            "Untuk cek daftar tautan yang sudah diinput: `/linktalent daftar`.",
            parse_mode="Markdown",
        )
        return

    target = replied.from_user
    if target.is_bot:
        await update.message.reply_text("⚠️ Tidak bisa menautkan akun bot.")
        return

    name_query = " ".join(args).strip()
    if not name_query:
        await update.message.reply_text(
            "⚠️ Sertakan nama talent-nya. Contoh: `/linktalent Hansel`", parse_mode="Markdown",
        )
        return

    talents = db.list_talents()
    talent = next((t for t in talents if (t["name"] or "").strip().lower() == name_query.lower()), None)
    if not talent:
        talent = _match_talent_from_text(name_query.lower(), talents)
    if not talent:
        await update.message.reply_text(
            f"⚠️ Talent dengan nama \"{name_query}\" tidak ditemukan di daftar talent. "
            "Cek ejaan namanya, atau tambahkan talent-nya dulu lewat menu /settings."
        )
        return

    uname = f"@{target.username}" if target.username else target.first_name
    _link_talent(target.id, talent["id"], label=uname)
    await update.message.reply_text(
        f"✅ Akun Telegram {uname} (id `{target.id}`) berhasil ditautkan sebagai talent *{talent['name']}*.\n\n"
        f"Mulai sekarang, HANYA akun ini yang bisa mengubah status absen untuk *{talent['name']}*.",
        parse_mode="Markdown",
    )


# ==================== FITUR AUTO PROMO TALENT READY ====================
# Posting otomatis (bisa dijadwalkan berulang lewat /setpromojadwal) ke
# grup/topik yang berisi daftar talent yang lagi READY (dari fitur Absen
# Talent di atas), lengkap dengan tombol deep link "?start=chat_<id>" ke
# chat pribadi bot per talent (sama seperti pola yang sudah dipakai
# chat_start_callback/private_deeplink_keyboard). Arsitekturnya sengaja
# dibuat mirip fitur Absen Talent: bisa didaftarkan ke BEBERAPA grup/topik
# sekaligus (maks 5), semuanya disimpan generik lewat get_setting/
# set_setting (JSON) -- TIDAK butuh skema database baru.
#
# "Hapus pesan terakhir" ditangani DUA cara:
# 1. OTOMATIS -- tiap kali _post_promo_job jalan (baik lewat jadwal maupun
#    /postpromo manual), pesan promo LAMA di grup itu (kalau ada & masih
#    tercatat) dihapus dulu SEBELUM posting yang baru, supaya grup tidak
#    menumpuk banyak pesan promo basi.
# 2. MANUAL -- command /hapuspromo, buat admin yang mau bersihkan grup
#    SEKARANG JUGA tanpa menunggu siklus posting berikutnya (mis. semua
#    talent baru saja jadi tidak-ready).

_MAX_PROMO_TOPICS = 5


def _get_promo_topics():
    """Balikin daftar grup/topik tujuan Auto Promo yang terdaftar, tiap item
    {"group_id": str, "topic_id": str} (topic_id boleh string kosong kalau
    grupnya tidak pakai fitur Topics sama sekali -- BEDA dengan Absen Talent
    yang mewajibkan topik, promo boleh diposting ke grup biasa)."""
    raw = db.get_setting("promo_topics")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, TypeError):
            logger.warning("Gagal parse setting promo_topics, dianggap kosong: %r", raw)
    return []


def _save_promo_topics(topics):
    db.set_setting("promo_topics", json.dumps(topics))


def _find_promo_topic(chat_id, thread_id):
    chat_id = str(chat_id)
    thread_id = str(thread_id or "")
    for t in _get_promo_topics():
        if t.get("group_id") == chat_id and t.get("topic_id") == thread_id:
            return t
    return None


def _promo_topic_key(group_id, topic_id):
    return f"{group_id}:{topic_id or ''}"


def _get_promo_last_messages():
    """Balikin dict {"<group_id>:<topic_id>": message_id} -- jejak pesan
    promo TERAKHIR yang diposting per grup/topik. Dipakai untuk (a) hapus
    otomatis sebelum posting ulang, dan (b) command /hapuspromo."""
    raw = db.get_setting("promo_last_messages")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            logger.warning("Gagal parse setting promo_last_messages, dianggap kosong: %r", raw)
    return {}


def _save_promo_last_messages(data):
    db.set_setting("promo_last_messages", json.dumps(data))


def _list_ready_talents():
    """Balikin `[(talent, status), ...]` untuk semua talent yang statusnya
    READY saat ini (lazy-expiry lewat _get_effective_talent_status, sama
    seperti yang dipakai badge status di kartu detail talent)."""
    ready = []
    for t in db.list_talents():
        status = _get_effective_talent_status(t["id"])
        if status and status["is_ready"]:
            ready.append((t, status))
    return ready


# Template teks isi pesan Auto Promo, bisa diubah admin lewat /settings >
# "✏️ Ubah Teks Promo Talent" (lihat settings_promotext & edit_promotext_receive).
# Placeholder `{daftar_talent}` WAJIB ada di teksnya -- akan digantikan
# otomatis dengan daftar talent yang lagi ready (satu baris per talent,
# lihat _build_promo_message), sisanya (header/footer) bebas diedit admin.
_DEFAULT_PROMO_TEXT = (
    "🔥 *Talent Ready Sekarang!*\n\n"
    "{daftar_talent}\n\n"
    "Tekan tombol nama talent di bawah untuk langsung chat lewat bot 👇"
)


def _build_promo_message(bot_username):
    """Bangun `(text, reply_markup)` pesan Auto Promo, atau `(None, None)`
    kalau TIDAK ADA talent yang ready sekarang -- pemanggil (_post_promo_job)
    harus SKIP posting kalau begitu, supaya grup tidak dapat promo kosong/basi."""
    ready = _list_ready_talents()
    if not ready:
        return None, None

    talent_lines = []
    for talent, status in ready:
        until = status.get("ready_until")
        if until:
            local = datetime.strptime(until, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC_TZ).astimezone(WIB_TZ)
            talent_lines.append(f"🟢 *{talent['name']}* -- ready sampai {local.strftime('%H:%M')} WIB")
        else:
            talent_lines.append(f"🟢 *{talent['name']}*  -- ready")
    daftar_talent = "\n".join(talent_lines)

    template = db.get_setting("promo_text", _DEFAULT_PROMO_TEXT)
    if "{daftar_talent}" in template:
        text = template.format(daftar_talent=daftar_talent)
    else:
        # Jaga-jaga kalau admin sampai menghapus placeholder-nya secara tidak
        # sengaja -- tetap tempel daftar talent di bawah teks admin supaya
        # promo tidak pernah terkirim tanpa daftar talent sama sekali.
        text = f"{template}\n\n{daftar_talent}"

    markup = kb.promo_keyboard([t for t, _s in ready], bot_username)
    return text, markup


async def _delete_promo_message(context: ContextTypes.DEFAULT_TYPE, group_id, topic_id):
    """Hapus pesan promo TERAKHIR yang tercatat untuk grup/topik ini (kalau
    ada), lalu lepas jejaknya dari promo_last_messages -- baik berhasil
    dihapus maupun ternyata sudah tidak ada (mis. admin hapus manual dari
    Telegram, atau lebih dari 48 jam jadi Telegram menolak menghapusnya).
    Balikin True kalau memang ada entri yang diproses (ada jejak sebelumnya)."""
    key = _promo_topic_key(group_id, topic_id)
    last_messages = _get_promo_last_messages()
    message_id = last_messages.get(key)
    if not message_id:
        return False
    try:
        await context.bot.delete_message(chat_id=int(group_id), message_id=int(message_id))
    except Exception:
        logger.warning(
            "Gagal hapus pesan promo lama (grup=%s, message_id=%s) -- mungkin sudah dihapus "
            "manual sebelumnya atau lebih dari 48 jam, lanjut saja.", group_id, message_id,
        )
    last_messages.pop(key, None)
    _save_promo_last_messages(last_messages)
    return True


async def _post_promo_job(context: ContextTypes.DEFAULT_TYPE):
    """Job auto-promo -- dipanggil baik terjadwal berulang (lihat
    /setpromojadwal & _reschedule_promo_job) MAUPUN manual lewat /postpromo.

    Untuk SETIAP grup/topik promo terdaftar (lihat /setpromogrup, maks 5):
    1. Hapus pesan promo LAMA di grup itu (kalau ada) -- lihat catatan
       "hapus pesan terakhir" di banner komentar atas.
    2. Kalau ADA talent yang ready: posting pesan baru + coba PIN (gagal pin
       -- mis. bot tidak punya izin "Pin Messages" di grup itu -- TIDAK
       membatalkan posting, cuma dicatat di log).
    3. Kalau TIDAK ADA talent yang ready sama sekali: cukup berhenti di
       langkah 1 (bersihkan pesan lama), TIDAK posting apa-apa -- supaya
       grup tidak menampilkan info basi seolah masih ada yang ready."""
    topics = _get_promo_topics()
    if not topics:
        return

    bot_username = context.bot.username
    text, markup = _build_promo_message(bot_username)
    last_messages = _get_promo_last_messages()

    for topic in topics:
        group_id = topic.get("group_id")
        topic_id = topic.get("topic_id")
        await _delete_promo_message(context, group_id, topic_id)

        if text is None:
            continue

        try:
            sent = await context.bot.send_message(
                chat_id=int(group_id),
                text=text,
                parse_mode="Markdown",
                reply_markup=markup,
                message_thread_id=int(topic_id) if topic_id else None,
            )
        except Exception:
            logger.exception("Gagal posting auto-promo talent ready ke grup %s", group_id)
            continue

        last_messages[_promo_topic_key(group_id, topic_id)] = sent.message_id
        _save_promo_last_messages(last_messages)

        try:
            await context.bot.pin_chat_message(
                chat_id=int(group_id), message_id=sent.message_id, disable_notification=True,
            )
        except Exception:
            logger.warning(
                "Gagal pin pesan promo di grup %s (kemungkinan bot tidak punya izin admin "
                "'Pin Messages' di grup ini) -- pesan tetap terposting, cuma tidak ke-pin.", group_id,
            )


def _reschedule_promo_job(job_queue, interval_minutes):
    """Hapus job auto-promo LAMA (kalau ada, dicari lewat nama job
    "promo_autopost") lalu jadwalkan ulang sesuai interval baru (menit).
    `interval_minutes` None/0 -- cuma hapus job-nya (auto-promo jadi OFF,
    posting manual lewat /postpromo tetap selalu bisa dipakai kapan saja).
    Dipanggil baik dari /setpromojadwal MAUPUN saat startup bot (supaya
    jadwal yang sudah diatur sebelumnya tetap aktif lagi setelah restart)."""
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name("promo_autopost"):
        job.schedule_removal()
    if interval_minutes:
        job_queue.run_repeating(
            _post_promo_job,
            interval=interval_minutes * 60,
            first=interval_minutes * 60,
            name="promo_autopost",
        )


async def setpromogrup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Jalankan DI DALAM grup/topik tujuan Auto Promo (khusus admin) untuk
    mendaftarkannya. Bisa didaftarkan di BEBERAPA grup/topik sekaligus --
    maksimal 5. Pakai:
    - `/setpromogrup` -- daftarkan grup/topik ini.
    - `/setpromogrup hapus` -- lepas grup/topik ini saja (dijalankan di dalamnya).
    - `/setpromogrup daftar` -- lihat semua grup/topik yang terdaftar."""
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("Command ini harus dijalankan di dalam GRUP tujuan promo, bukan private chat.")
        return
    if not is_admin(update.effective_user.id):
        return

    if context.args and context.args[0].lower() in ("daftar", "list"):
        topics = _get_promo_topics()
        if not topics:
            await update.message.reply_text("Belum ada grup/topik promo yang terdaftar.")
            return
        lines = [f"📋 *Grup/topik Auto Promo terdaftar* ({len(topics)}/{_MAX_PROMO_TOPICS}):", ""]
        for i, t in enumerate(topics, 1):
            lines.append(f"{i}. Grup `{t.get('group_id')}` / topik `{t.get('topic_id') or '-'}`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    thread_id = update.effective_message.message_thread_id

    if context.args and context.args[0].lower() == "hapus":
        topics = _get_promo_topics()
        new_topics = [
            t for t in topics
            if not (t.get("group_id") == str(update.effective_chat.id) and t.get("topic_id") == str(thread_id or ""))
        ]
        if len(new_topics) == len(topics):
            await update.message.reply_text("Grup/topik ini memang belum terdaftar sebagai tujuan promo.")
            return
        _save_promo_topics(new_topics)
        await _delete_promo_message(context, str(update.effective_chat.id), thread_id)
        await update.message.reply_text(
            f"✅ Grup/topik ini sudah dilepas dari daftar Auto Promo ({len(new_topics)}/{_MAX_PROMO_TOPICS} tersisa)."
        )
        return

    topics = _get_promo_topics()
    if _find_promo_topic(update.effective_chat.id, thread_id):
        await update.message.reply_text("Grup/topik ini sudah terdaftar sebagai tujuan promo sebelumnya.")
        return
    if len(topics) >= _MAX_PROMO_TOPICS:
        await update.message.reply_text(
            f"⚠️ Sudah ada {_MAX_PROMO_TOPICS} grup/topik promo terdaftar (batas maksimal). "
            "Lepas salah satu dulu dengan `/setpromogrup hapus` (dijalankan di dalamnya) sebelum "
            "menambah yang baru.",
            parse_mode="Markdown",
        )
        return

    topics.append({"group_id": str(update.effective_chat.id), "topic_id": str(thread_id or "")})
    _save_promo_topics(topics)
    await update.message.reply_text(
        f"✅ Grup/topik ini diatur sebagai tujuan *Auto Promo Talent Ready* ({len(topics)}/{_MAX_PROMO_TOPICS} terdaftar).\n\n"
        "Atur jadwal berulangnya lewat `/setpromojadwal <menit>` (mis. `/setpromojadwal 120` = "
        "tiap 2 jam), atau posting manual sekarang lewat `/postpromo`.",
        parse_mode="Markdown",
    )


async def setpromojadwal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Atur jadwal berulang Auto Promo (khusus admin). Pakai:
    - `/setpromojadwal <menit>` -- posting otomatis tiap N menit (mis. `120` = tiap 2 jam).
    - `/setpromojadwal off` -- matikan posting otomatis (posting manual lewat /postpromo tetap bisa).
    - `/setpromojadwal` (tanpa argumen) -- lihat jadwal yang sedang aktif."""
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        current = db.get_setting("promo_interval_minutes")
        if current:
            await update.message.reply_text(
                f"⏰ Jadwal Auto Promo saat ini: tiap *{current} menit*.\n\n"
                "Ubah dengan `/setpromojadwal <menit>`, atau matikan dengan `/setpromojadwal off`.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "⏰ Auto Promo terjadwal sedang OFF.\n\n"
                "Aktifkan dengan `/setpromojadwal <menit>` (mis. `/setpromojadwal 120` = tiap 2 jam).",
                parse_mode="Markdown",
            )
        return

    arg = context.args[0].lower()
    if arg == "off":
        db.delete_setting("promo_interval_minutes")
        _reschedule_promo_job(context.job_queue, None)
        await update.message.reply_text(
            "✅ Auto Promo terjadwal dimatikan. Posting manual lewat /postpromo tetap bisa dipakai kapan saja."
        )
        return

    try:
        minutes = int(arg)
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Format salah. Pakai angka menit (mis. `/setpromojadwal 120`) atau `/setpromojadwal off`.",
            parse_mode="Markdown",
        )
        return

    if not _get_promo_topics():
        await update.message.reply_text(
            "⚠️ Belum ada grup/topik tujuan promo yang terdaftar. Daftarkan dulu lewat "
            "`/setpromogrup` (dijalankan di dalam grup tujuannya) sebelum atur jadwal.",
            parse_mode="Markdown",
        )
        return

    if context.job_queue is None:
        await update.message.reply_text(
            "⚠️ JobQueue tidak aktif di server ini (`python-telegram-bot[job-queue]` belum "
            "terpasang), jadi jadwal berulang TIDAK akan jalan otomatis walau pengaturan ini "
            "tersimpan. Posting manual lewat /postpromo tetap selalu bisa dipakai."
        )
        return

    db.set_setting("promo_interval_minutes", str(minutes))
    _reschedule_promo_job(context.job_queue, minutes)
    await update.message.reply_text(
        f"✅ Auto Promo dijadwalkan tiap *{minutes} menit*. Posting otomatis pertama dalam "
        f"{minutes} menit dari sekarang (atau posting manual sekarang lewat /postpromo).",
        parse_mode="Markdown",
    )


async def postpromo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Posting promo talent ready SEKARANG JUGA ke semua grup/topik promo
    terdaftar, tanpa menunggu jadwal (khusus admin). Berguna buat testing
    atau dorongan promo dadakan."""
    if not is_admin(update.effective_user.id):
        return
    if not _get_promo_topics():
        await update.message.reply_text(
            "⚠️ Belum ada grup/topik tujuan promo yang terdaftar. Daftarkan dulu lewat `/setpromogrup`.",
            parse_mode="Markdown",
        )
        return
    if not _list_ready_talents():
        await update.message.reply_text("ℹ️ Tidak ada talent yang READY sekarang, promo tidak diposting.")
        return

    await update.message.reply_text("⏳ Memposting promo ke semua grup/topik terdaftar...")
    await _post_promo_job(context)
    await update.message.reply_text("✅ Selesai.")


async def _delete_all_promo_messages(context: ContextTypes.DEFAULT_TYPE):
    """Hapus pesan promo TERAKHIR di SEMUA grup/topik promo terdaftar sekarang
    juga, balikin jumlah grup/topik yang berhasil diproses. Dipakai bersama
    oleh /hapuspromo DAN tombol "🗑️ Hapus Pesan Promo Sekarang" di /settings
    (lihat settings_callback -> settings_autopromo_delnow) supaya logikanya
    tidak dobel."""
    deleted_count = 0
    for topic in _get_promo_topics():
        if await _delete_promo_message(context, topic.get("group_id"), topic.get("topic_id")):
            deleted_count += 1
    return deleted_count


async def hapuspromo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hapus pesan promo TERAKHIR di semua grup/topik promo terdaftar,
    sekarang juga (khusus admin) -- tanpa menunggu siklus posting berikutnya
    menghapusnya otomatis. Berguna untuk menghentikan promo secara manual."""
    if not is_admin(update.effective_user.id):
        return
    if not _get_promo_topics():
        await update.message.reply_text("Belum ada grup/topik tujuan promo yang terdaftar.")
        return

    deleted_count = await _delete_all_promo_messages(context)

    if deleted_count:
        await update.message.reply_text(f"✅ Pesan promo terakhir sudah dihapus dari {deleted_count} grup/topik.")
    else:
        await update.message.reply_text("ℹ️ Tidak ada pesan promo yang tercatat untuk dihapus (mungkin belum pernah posting).")


async def statustalent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ringkasan status ready/tidak-ready semua talent saat ini (hasil fitur
    absen). Bisa dipakai siapa saja -- talent, admin, atau calon customer
    yang mau cek talent mana yang lagi ready."""
    talents = db.list_talents()
    if not talents:
        await update.message.reply_text("Belum ada talent yang terdaftar.")
        return

    lines = ["📋 *Status Talent Saat Ini*", ""]
    for t in talents:
        status = _get_effective_talent_status(t["id"])
        if status and status["is_ready"]:
            until = status.get("ready_until")
            if until:
                local = datetime.strptime(until, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC_TZ).astimezone(WIB_TZ)
                lines.append(f"🟢 {t['name']} -- ready sampai {local.strftime('%H:%M')} WIB")
            else:
                lines.append(f"🟢 {t['name']} -- ready")
        elif status:
            lines.append(f"🔴 {t['name']} -- tidak ready")
        else:
            lines.append(f"⚪ {t['name']} -- belum absen")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def show_talent_detail(context: ContextTypes.DEFAULT_TYPE, chat_id, talent, owner_id=None, index=None):
    """Kirim halaman detail talent (foto+deskripsi+tombol) ke chat_id tertentu.
    Dipakai baik dari tombol chat biasa maupun dari data yang dikirim Mini App.
    `owner_id` diteruskan ke talent_detail_keyboard kalau halaman ini tampil
    di GRUP, supaya tombolnya tetap terkunci ke pemilik kartu. `index`
    (posisi talent ini di carousel, kalau diketahui) diteruskan juga supaya
    tombol "Kembali ke Daftar Talent" balik ke POSISI yang sama, bukan reset
    ke urutan talent pertama (lihat _parse_id_and_index)."""
    caption = _talent_caption(talent)
    if talent.get("photo_file_id"):
        photo = await _get_watermarked_talent_photo(context, talent)
        await send_thinking_photo(
            context, chat_id, photo, caption,
            parse_mode="Markdown", reply_markup=kb.talent_detail_keyboard(talent, owner_id=owner_id, index=index),
        )
    else:
        await send_thinking_reply(
            context, chat_id, caption,
            parse_mode="Markdown", reply_markup=kb.talent_detail_keyboard(talent, owner_id=owner_id, index=index),
        )


async def talent_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dipicu saat user memilih (menekan tombol nama) salah satu talent dari
    kartu carousel. Di GRUP, kartu carousel yang sedang tampil TIDAK
    dihapus/ditutup -- langsung di-EDIT di tempat jadi halaman detailnya
    (lihat _edit_card_in_place & catatan di show_talent_card di atas).

    callback_data membawa POSISI (index) talent ini di carousel (lihat
    talent_carousel_keyboard/back_to_talent_keyboard di keyboards.py) --
    index ini diteruskan terus ke talent_detail_keyboard supaya tombol
    "Kembali ke Daftar Talent" nanti balik ke posisi yang sama persis, bukan
    reset ke talent pertama/urutan awal (lihat _parse_id_and_index).

    Kartu ini bisa saja terkunci ke satu user (lihat _split_owner_suffix) --
    kalau iya dan yang menekan BUKAN pemiliknya, tolak lebih dulu sebelum
    memproses apa pun."""
    query = update.callback_query
    data, owner_id = _split_owner_suffix(query.data)
    if not await _enforce_card_owner(query, owner_id):
        return
    await query.answer()
    talent_id, index = _parse_id_and_index(data)
    talent = db.get_talent(talent_id)
    if not talent:
        await replace_message(query, context, "Talent tidak ditemukan.", reply_markup=kb.main_menu_keyboard())
        return

    chat = query.message.chat
    caption = _talent_caption(talent)
    reply_markup = kb.talent_detail_keyboard(talent, owner_id=owner_id, index=index)
    photo = await _get_watermarked_talent_photo(context, talent)

    if chat.type != ChatType.PRIVATE:
        edited = await _edit_card_in_place(
            context, chat.id, query.message,
            has_new_photo=bool(photo), photo=photo, caption=caption,
            parse_mode="Markdown", reply_markup=reply_markup,
        )
        if edited:
            return

    await delete_prev_message(query, context)
    await show_talent_detail(context, chat.id, talent, owner_id=owner_id, index=index)


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
    """Entry point live chat lewat web_app_data dengan action 'start_chat'.

    CATATAN (per perbaikan tombol "Chat Sekarang" di index.html): frontend
    SEKARANG TIDAK LAGI memanggil tg.sendData() untuk aksi ini -- tombol
    "Chat Sekarang" sudah diganti ke pendekatan deep link
    (tg.openTelegramLink()/window.location.href ke "?start=chat_<id>")
    supaya berfungsi konsisten di semua cara Mini App dibuka, termasuk
    Direct Link (t.me/<bot>/<short_name> dari /postkatalog) yang TIDAK
    mendukung sendData() sama sekali. Handler ini sengaja TETAP dipertahankan
    (bukan dihapus) untuk kompatibilitas ke belakang, seandainya ada versi
    index.html lama/custom yang masih mengirim web_app_data 'start_chat'.
    Mini App akan menutup diri (tg.close()) dan mengirim data ini, lalu bot
    langsung membuka sesi live chat dengan admin di chat seperti biasa.

    Sengaja DIBATASI hanya untuk private chat -- SAMA seperti chat_start_callback()
    (versi tombol inline biasa) -- karena live chat butuh sesi 1-ke-1 antara
    user & admin. Kalau Mini App-nya dibuka dari dalam GRUP (mis. lewat tombol
    Mini App yang diposting ke grup pakai /postkatalog), update web_app_data
    ini akan datang dengan effective_chat = grup tsb, BUKAN DM user -- jadi
    tanpa pengecekan ini, sesi live chat malah kebuka & pesan konfirmasinya
    keliru terkirim ke grup (dan tidak akan pernah bisa dibalas, karena
    relay_user_message cuma didaftarkan untuk private chat). Makanya di sini
    user diarahkan lewat deep link ke private chat bot dulu, sama seperti
    versi tombol inline."""
    try:
        payload = json.loads(update.effective_message.web_app_data.data)
        talent_id = int(payload["talent_id"])
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.warning("Payload web_app_data 'start_chat' tidak valid: %r", getattr(update.effective_message, "web_app_data", None))
        return

    talent = db.get_talent(talent_id)
    if not talent:
        await update.effective_message.reply_text(
            "Talent tidak ditemukan.", reply_markup=kb.main_menu_keyboard()
        )
        return

    if update.effective_chat.type != ChatType.PRIVATE:
        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start=chat_{talent_id}"
        await update.effective_message.reply_text(
            "💬 *Chat Sekarang* cuma bisa dipakai lewat chat pribadi denganku, "
            "biar obrolanmu sama admin tetap privat 🙏\n\n"
            "Klik tombol di bawah ini, nanti begitu terbuka aku langsung "
            f"sambungkan kamu dengan admin soal *{talent['name']}*.",
            parse_mode="Markdown",
            reply_markup=kb.private_deeplink_keyboard(deep_link),
        )
        return

    try:
        await start_chat_session(context, update.effective_chat.id, update.effective_user, talent)
    except Exception:
        logger.exception("Gagal memulai sesi live chat dari tombol Mini App 'Chat Sekarang'")
        try:
            await update.effective_message.reply_text(
                "⚠️ Gagal membuka live chat, silakan coba tekan tombol \"💬 Chat Sekarang\" lagi."
            )
        except Exception:
            logger.exception("Gagal mengirim pesan error fallback untuk chat_start_from_webapp")


async def pricelist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback_data membawa POSISI (index) talent ini di carousel (lihat
    talent_detail_keyboard di keyboards.py) -- diteruskan ke
    back_to_talent_keyboard supaya tombol "⬅️ Kembali" balik ke halaman
    detail talent yang sama DENGAN index aslinya tetap menempel, bukan
    kehilangan posisi carousel-nya (lihat _parse_id_and_index).

    Kartu ini bisa saja terkunci ke satu user (lihat _split_owner_suffix)
    -- kalau iya dan yang menekan BUKAN pemiliknya, tolak lebih dulu sebelum
    memproses apa pun."""
    query = update.callback_query
    data, owner_id = _split_owner_suffix(query.data)
    if not await _enforce_card_owner(query, owner_id):
        return
    await query.answer()
    talent_id, index = _parse_id_and_index(data)
    talent = db.get_talent(talent_id)
    if not talent:
        await query.answer("Talent tidak ditemukan.", show_alert=True)
        return
    text = f"💰 *Pricelist - {talent['name']}*\n\n{talent['pricelist']}"
    chat_id = query.message.chat_id
    back_markup = kb.back_to_talent_keyboard(talent_id, owner_id=owner_id, index=index)
    await delete_prev_message(query, context)
    await send_typing(context, chat_id)
    if talent.get("photo_file_id") and len(text) <= 1024:
        photo = await _get_watermarked_talent_photo(context, talent)
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=text,
            parse_mode="Markdown",
            reply_markup=back_markup,
        )
    elif talent.get("photo_file_id"):
        # Caption Telegram maksimal 1024 karakter -> kirim foto polos,
        # lalu teks pricelist lengkap sebagai pesan terpisah.
        # Foto ini tidak punya tombol sendiri, jadi ID-nya dicatat supaya
        # ikut terhapus otomatis saat user menekan tombol lain nanti
        # (mis. "Kembali"/"Chat Sekarang" di pesan teks) -- tanpa ini foto
        # akan nyangkut/tertinggal selamanya di chat.
        photo = await _get_watermarked_talent_photo(context, talent)
        photo_msg = await context.bot.send_photo(chat_id=chat_id, photo=photo)
        context.user_data["extra_msg_to_delete"] = (chat_id, photo_msg.message_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=back_markup,
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=back_markup,
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

    for admin_id in get_all_admin_ids():
        try:
            msg = await context.bot.send_message(
                chat_id=admin_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup,
            )
            sent.append((msg.chat_id, msg.message_id))
        except Exception:
            logger.exception(f"Gagal kirim live chat ke admin {admin_id}")
    return sent


async def broadcast_copy_to_admin_targets(context: ContextTypes.DEFAULT_TYPE, from_chat_id: int, message_id: int, reply_markup=None):
    """Teruskan (copy_message) pesan user apa adanya -- teks/foto/video/voice/dsb --
    ke tujuan admin. Balikin daftar (chat_id, message_id) hasil salinannya.

    `reply_markup` opsional -- dipakai untuk menempelkan tombol "✅ Approve ke
    Channel Testimoni" pada foto yang di-relay (lihat relay_user_message)."""
    sent = []
    if config.LIVECHAT_GROUP_ID:
        try:
            group_id = int(config.LIVECHAT_GROUP_ID)
            copied = await context.bot.copy_message(
                chat_id=group_id, from_chat_id=from_chat_id, message_id=message_id,
                reply_markup=reply_markup,
            )
            sent.append((group_id, copied.message_id))
            return sent
        except Exception:
            logger.exception("Gagal teruskan pesan user ke LIVECHAT_GROUP_ID, fallback ke admin satu-satu.")

    for admin_id in get_all_admin_ids():
        try:
            copied = await context.bot.copy_message(
                chat_id=admin_id, from_chat_id=from_chat_id, message_id=message_id,
                reply_markup=reply_markup,
            )
            sent.append((admin_id, copied.message_id))
        except Exception:
            logger.exception(f"Gagal teruskan pesan user ke admin {admin_id}")
    return sent


async def broadcast_photo_to_admin_targets(context: ContextTypes.DEFAULT_TYPE, photo, caption, parse_mode=None, reply_markup=None):
    """Kirim `photo` (file_id) + `caption` ke grup live chat (kalau LIVECHAT_GROUP_ID
    diisi), atau ke masing-masing admin secara private kalau tidak. Balikin daftar
    (chat_id, message_id) yang berhasil terkirim, sama seperti broadcast_to_admin_targets
    tapi versi foto -- dipakai untuk format relay "ala livechatgram" (foto profil user
    kecil + caption di bawahnya)."""
    sent = []
    if config.LIVECHAT_GROUP_ID:
        try:
            msg = await context.bot.send_photo(
                chat_id=int(config.LIVECHAT_GROUP_ID), photo=photo, caption=caption,
                parse_mode=parse_mode, reply_markup=reply_markup,
            )
            sent.append((msg.chat_id, msg.message_id))
            return sent
        except Exception:
            logger.exception("Gagal kirim foto live chat ke LIVECHAT_GROUP_ID, fallback ke admin satu-satu.")

    for admin_id in get_all_admin_ids():
        try:
            msg = await context.bot.send_photo(
                chat_id=admin_id, photo=photo, caption=caption,
                parse_mode=parse_mode, reply_markup=reply_markup,
            )
            sent.append((msg.chat_id, msg.message_id))
        except Exception:
            logger.exception(f"Gagal kirim foto live chat ke admin {admin_id}")
    return sent


async def _get_profile_photo_file_id(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Ambil file_id foto profil user, ukuran TERKECIL yang tersedia supaya
    tampil sebagai thumbnail kecil (ala livechatgram) di chat admin, bukan
    foto besar penuh layar. Balikin None kalau user tidak punya foto profil
    atau gagal diambil (mis. privasi foto profil disembunyikan dari bot)."""
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos and photos.total_count > 0 and photos.photos:
            return photos.photos[0][0].file_id  # [0][0] = ukuran terkecil
    except Exception:
        logger.debug("Gagal mengambil foto profil user_id=%s (mungkin privasi disembunyikan).", user_id)
    return None


def _mention_html(full_name: str, user_id: int) -> str:
    """Bikin mention HTML yang bisa diklik admin untuk langsung membuka
    profil user (pakai tg://user?id=...), jadi tetap berfungsi walau user
    tidak/belum punya username Telegram."""
    return f'<a href="tg://user?id={user_id}">{html_escape(full_name)}</a>'


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
    """Entry point saat user menekan tombol 'Chat Sekarang' di chat biasa.

    Sengaja DIBATASI hanya untuk private chat -- live chat butuh sesi 1-ke-1
    antara user & admin, jadi tidak masuk akal (dan bisa bocor ke orang lain)
    kalau dibuka langsung di dalam grup. Kalau tombol ini kepencet di grup
    (mis. dari kartu talent yang tampil gara-gara fitur smart-reply mention),
    user diarahkan lewat deep link ke private chat bot -- begitu dibuka,
    /start otomatis melanjutkan ke sesi live chat untuk talent yang sama,
    tanpa perlu klak-klik ulang dari awal."""
    query = update.callback_query
    talent_id = int(query.data.split("_")[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await query.answer("Talent tidak ditemukan.", show_alert=True)
        return

    if query.message.chat.type != ChatType.PRIVATE:
        await query.answer()
        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start=chat_{talent_id}"
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "💬 *Chat Sekarang* cuma bisa dipakai lewat chat pribadi denganku, "
                "biar obrolanmu sama admin tetap privat 🙏\n\n"
                "Klik tombol di bawah ini, nanti begitu terbuka aku langsung "
                f"sambungkan kamu dengan admin soal *{talent['name']}*."
            ),
            parse_mode="Markdown",
            reply_to_message_id=query.message.message_id,
            reply_markup=kb.private_deeplink_keyboard(deep_link),
        )
        return

    await query.answer()
    chat_id = query.message.chat_id
    await delete_prev_message(query, context)
    try:
        await start_chat_session(context, chat_id, query.from_user, talent)
    except Exception:
        logger.exception("Gagal memulai sesi live chat dari tombol 'Chat Sekarang'")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Gagal membuka live chat, silakan coba lagi.",
            )
        except Exception:
            logger.exception("Gagal mengirim pesan error fallback untuk chat_start_callback")


# ==================== SMART REPLY SAAT DI-MENTION DI GRUP ====================
# Kalau bot di-mention (@username_bot) atau di-reply di dalam grup, bot
# LANGSUNG membalas pesan itu (reply_to_message_id) dengan tombol "🚀 Mulai".
# Begitu user yang di-mention menekan tombol itu, bot mengirim kartu katalog
# talent -- juga sebagai REPLY ke pesan mention ASLI user tsb, jadi kartunya
# tetap "nyambung" ke pesan yang memicunya. Bot TIDAK bereaksi pada kata
# kunci apa pun (mis. "order talent"/"katalog") yang diketik bebas di grup --
# satu-satunya cara memicu bot di grup adalah mention username-nya atau
# me-reply pesan bot secara langsung.
#
# PENTING (perbaikan bug "kartu talent hilang saat dipakai bersamaan"):
# desain SEBELUMNYA menyimpan status "siapa yang sudah klik Mulai" di
# context.chat_data (memori bersama SATU grup, dipakai semua orang). Kalau
# beberapa user mention bot & klik Mulai hampir bersamaan, status/kartu di
# memori bersama itu bisa saling timpa, sehingga kartu salah satu user
# hilang dan tidak muncul lagi.
#
# Desain SEKARANG tidak menyimpan status apa pun di memori bersama untuk
# fitur ini. Setiap pesan "🚀 Mulai" membawa SEMUA informasi yang
# dibutuhkan langsung di callback_data tombolnya sendiri (id user yang
# berhak menekan + id pesan mention asli yang harus dibalas) -- lihat
# kb.group_start_keyboard(). Karena itu, setiap mention & setiap tombol
# sepenuhnya independen satu sama lain (tidak ada objek/memori bersama yang
# bisa saling tabrakan), jadi bot aman dipakai banyak orang di grup yang
# sama secara bersamaan.
#
# Satu-satunya aksi yang TETAP dibatasi khusus chat pribadi adalah
# "💬 Chat Sekarang" (live chat 1-ke-1 dengan admin) -- lihat guard di
# chat_start_callback() di atas -- karena aksi itu perlu obrolan privat,
# bukan hal yang cocok ditampilkan ke seluruh isi grup.


def _text_mentions_bot(message, bot_username: str) -> bool:
    """True kalau `message` mengandung mention "@bot_username" (entity type
    'mention', dari user mengetik manual -- BUKAN 'text_mention' yang dipakai
    untuk mention tanpa username karena itu menunjuk ke akun lain, bukan bot)."""
    if not message or not message.text or not bot_username:
        return False
    target = f"@{bot_username}".lower()
    try:
        mentions = message.parse_entities(types=[MessageEntity.MENTION])
    except Exception:
        return False
    return any(text.lower() == target for text in mentions.values())


def _is_reply_to_bot(message, bot_id: int) -> bool:
    """True kalau `message` adalah reply langsung ke salah satu pesan bot --
    diperlakukan sama seperti mention, karena user jelas sedang menyapa bot."""
    reply = message.reply_to_message if message else None
    return bool(reply and reply.from_user and reply.from_user.id == bot_id)


async def group_smart_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler utama fitur smart-reply di grup. HANYA bereaksi kalau bot
    di-mention (@username_bot) atau di-reply langsung -- tidak bereaksi pada
    kata kunci apa pun (mis. "order talent"/"katalog") yang diketik bebas di
    grup, supaya bot tidak ikut membalas obrolan grup yang tidak ada
    urusannya dengan bot sama sekali.

    Begitu terpicu, bot membalas (reply_to_message_id) pesan user tsb dengan
    tombol "🚀 Mulai" -- id user & id pesan mention aslinya disisipkan di
    callback_data tombolnya sendiri (lihat kb.group_start_keyboard), BUKAN
    disimpan di memori bersama, supaya banyak user bisa mention bot
    bersamaan di grup yang sama tanpa saling mengganggu."""
    message = update.effective_message
    user = update.effective_user
    if not message or not message.text or not user:
        return

    chat = update.effective_chat
    bot = context.bot

    if not (_text_mentions_bot(message, bot.username) or _is_reply_to_bot(message, bot.id)):
        return

    mention = _mention_html(user.full_name, user.id)
    await send_typing(context, chat.id)
    await bot.send_message(
        chat_id=chat.id,
        text=(
            f"Halo {mention}! 👋\n\n"
            "Klik tombol <b>Mulai</b> di bawah ini untuk lihat katalog talent kami 👇"
        ),
        parse_mode="HTML",
        reply_to_message_id=message.message_id,
        reply_markup=kb.group_start_keyboard(user.id, message.message_id),
    )


async def group_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ditekan dari tombol "🚀 Mulai" yang bot kirim di grup setelah
    di-mention/di-reply. callback_data membawa id user yang di-tuju (supaya
    tombol tidak bisa "dibajak"/dipakai orang lain di grup itu) dan id pesan
    mention ASLI-nya (supaya kartu talent bisa dikirim sebagai balasan
    langsung ke pesan itu) -- semua tersimpan di tombolnya sendiri, tanpa
    memori bersama, jadi aman dipakai banyak user sekaligus."""
    query = update.callback_query
    try:
        _, target_user_id_str, orig_message_id_str = query.data.split("_", 2)
        target_user_id = int(target_user_id_str)
        orig_message_id = int(orig_message_id_str)
    except (IndexError, ValueError):
        await query.answer()
        return

    if query.from_user.id != target_user_id:
        await query.answer("Tombol ini bukan untukmu 😅 Mention aku sendiri, ya!", show_alert=True)
        return

    await query.answer()
    chat = query.message.chat

    # Hapus pesan "Klik Mulai" -- sudah tidak diperlukan lagi begitu ditekan.
    try:
        await query.message.delete()
    except Exception:
        logger.warning("Gagal menghapus pesan 'Klik Mulai' (mungkin sudah dihapus).")

    # Animasi sapaan (mis. Pikachu melambai tanpa latar belakang) yang
    # diatur admin lewat Menu Pengaturan > "Animasi Sapaan Grup (Mulai)" --
    # dikirim dulu sebagai pesan tersendiri kalau sudah diatur (tidak wajib).
    await send_group_start_greeting_media(context, chat.id)

    talents = db.list_talents()
    if not talents:
        await send_typing(context, chat.id)
        await context.bot.send_message(
            chat_id=chat.id,
            text="Waduh, belum ada talent yang ditambahkan nih 🙏",
            reply_to_message_id=orig_message_id,
        )
        return

    mention = _mention_html(query.from_user.full_name, query.from_user.id)
    await send_typing(context, chat.id)
    await context.bot.send_message(
        chat_id=chat.id,
        text=f"💃 Halo {mention}! Ini dia katalog talent kami:",
        parse_mode="HTML",
        reply_to_message_id=orig_message_id,
    )
    await send_talent_card_to_chat(
        context, chat.id, talents, 0, close_button=True, reply_to_message_id=orig_message_id,
        owner_id=target_user_id,
    )




async def send_group_start_greeting_media(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Kirim animasi sapaan (mis. Pikachu melambai tanpa latar belakang) yang
    diatur admin lewat "🐹 Animasi Sapaan Grup (Mulai)" di Menu Pengaturan,
    kalau memang sudah diatur. Mendukung 3 jenis file: STICKER (video/animated
    sticker -- format PALING pas untuk animasi "tanpa latar belakang" karena
    stiker video (.webm VP9) di Telegram memang mendukung transparansi asli),
    ANIMATION (GIF/MP4 tanpa suara), atau VIDEO biasa. Aman diabaikan kalau
    belum diatur atau gagal kirim (mis. file_id kedaluwarsa)."""
    file_id = db.get_setting("group_start_media_file_id")
    if not file_id:
        return
    kind = db.get_setting("group_start_media_kind", "sticker")
    try:
        if kind == "animation":
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
            await context.bot.send_animation(chat_id=chat_id, animation=file_id)
        elif kind == "video":
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
            await context.bot.send_video(chat_id=chat_id, video=file_id)
        else:
            await context.bot.send_sticker(chat_id=chat_id, sticker=file_id)
    except Exception:
        logger.exception("Gagal mengirim animasi sapaan grup (group_start_media), lanjut tanpa animasi.")


# Pola nominal transfer (Rp 100.000 / IDR 100,000 / 100.000 dst) dan
# kata kunci baris "penerima" -- dipakai _redact_sensitive_regions untuk
# menentukan baris teks mana yang TETAP terlihat (sisanya dihitamkan).
_NOMINAL_RE = re.compile(r"(rp\.?\s?\d[\d.,]*|idr\s?\d[\d.,]*|\b\d{1,3}(?:[.,]\d{3}){1,}\b)", re.IGNORECASE)
_RECIPIENT_LABEL_RE = re.compile(r"\b(kepada|penerima|tujuan|beneficiary|recipient)\b", re.IGNORECASE)


def _testimoni_censor_backend():
    """Balikin nama mesin OCR yang siap dipakai: 'tesseract' atau 'easyocr',
    atau None kalau tidak ada satupun tersedia. Sengaja TIDAK menginisialisasi
    reader EasyOCR di sini (itu lambat, bisa unduh model di percobaan
    pertama) -- cuma cek pip package-nya kepasang, supaya fungsi ini aman
    dipanggil langsung dari settings_callback tanpa nge-block event loop."""
    if pytesseract is not None:
        try:
            pytesseract.get_tesseract_version()
            return "tesseract"
        except Exception:
            pass
    if easyocr is not None:
        return "easyocr"
    return None


def _testimoni_censor_available() -> bool:
    """True kalau ada mesin OCR yang siap dipakai (tesseract ATAU easyocr).
    Dipanggil sebelum _redact_sensitive_regions supaya kalau tidak ada
    satupun tersedia, bot fallback kirim foto APA ADANYA (tanpa sensor)
    alih-alih error/gagal total."""
    return _testimoni_censor_backend() is not None


_easyocr_reader = None  # lazy-init sekali & dipakai ulang (init-nya lambat).


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        # gpu=False -- asumsi server tanpa GPU (VPS/hosting biasa).
        _easyocr_reader = easyocr.Reader(["en"], gpu=False)
    return _easyocr_reader


def _classify_and_redact_lines(img, lines):
    """Bagian yang SAMA dipakai oleh kedua backend OCR: tentukan baris mana
    yang tetap terlihat (nominal / label penerima+baris setelahnya), lalu
    hitamkan sisanya. `lines` = list of dict {text, x0, y0, x1, y1}."""
    if not lines:
        # Tidak ada teks kebaca sama sekali -- kemungkinan besar foto bukan
        # screenshot teks (atau OCR gagal baca semua) -- lebih aman balikin
        # apa adanya daripada menghitamkan seluruh foto tanpa alasan jelas.
        return None

    ordered = sorted(lines, key=lambda l: (l["y0"], l["x0"]))
    for line in ordered:
        line["keep"] = bool(_NOMINAL_RE.search(line["text"]) or _RECIPIENT_LABEL_RE.search(line["text"]))
    for idx, line in enumerate(ordered):
        if _RECIPIENT_LABEL_RE.search(line["text"]) and idx + 1 < len(ordered):
            ordered[idx + 1]["keep"] = True

    draw = ImageDraw.Draw(img)
    pad = 4
    for line in ordered:
        if line["keep"]:
            continue
        draw.rectangle([line["x0"] - pad, line["y0"] - pad, line["x1"] + pad, line["y1"] + pad], fill=(0, 0, 0))

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _redact_sensitive_regions_tesseract(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    grouped = {}
    for i in range(len(data["text"])):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        line = grouped.setdefault(key, {"words": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h})
        line["words"].append(word)
        line["x0"] = min(line["x0"], x)
        line["y0"] = min(line["y0"], y)
        line["x1"] = max(line["x1"], x + w)
        line["y1"] = max(line["y1"], y + h)

    lines = [{"text": " ".join(l["words"]), **l} for l in grouped.values()]
    result = _classify_and_redact_lines(img, lines)
    return result if result is not None else image_bytes


def _redact_sensitive_regions_easyocr(image_bytes: bytes) -> bytes:
    reader = _get_easyocr_reader()
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    results = reader.readtext(np.array(img))  # [(box[4 titik], text, confidence), ...]

    lines = []
    for box, text, _conf in results:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append({"text": text, "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)})

    result = _classify_and_redact_lines(img, lines)
    return result if result is not None else image_bytes


def _redact_sensitive_regions(image_bytes: bytes) -> bytes:
    """Best-effort: hitamkan SEMUA baris teks hasil OCR, KECUALI baris yang
    cocok pola nominal (Rp/IDR + angka) atau baris label penerima ('kepada',
    'penerima', 'tujuan', dst) beserta baris tepat SETELAHNYA (diasumsikan
    itu nilai/nama dari label tsb). Jadi nama pengirim, no. rekening
    pengirim, tanggal, ID transaksi, dll ikut tertutup -- sesuai permintaan
    "sensor seluruh teks sensitif kecuali nominal dan penerima".

    Otomatis pakai backend OCR mana pun yang tersedia (tesseract kalau ada,
    kalau tidak fallback ke EasyOCR yang murni pip install -- lihat
    _testimoni_censor_backend). Ini heuristik OCR, BUKAN 100% akurat -- tata
    letak tiap bank/e-wallet beda-beda. Kalau OCR gagal baca apa pun/error,
    balikin bytes ASLI apa adanya (mode Approve Manual jadi default, supaya
    admin sempat melihat hasil foto ASLI sebelum diproses & diposting)."""
    backend = _testimoni_censor_backend()
    if backend == "tesseract":
        return _redact_sensitive_regions_tesseract(image_bytes)
    if backend == "easyocr":
        return _redact_sensitive_regions_easyocr(image_bytes)
    return image_bytes

def _apply_testimoni_watermark(image_bytes: bytes, watermark_bytes: bytes) -> bytes:
    """Tempel watermark (stiker/gambar yang diupload admin lewat /settings >
    Channel Testimoni > Set Watermark) di pojok kanan-bawah foto, diskalakan
    proporsional (maks ~25% lebar foto) dengan sedikit transparansi supaya
    tidak menutupi info penting di foto."""
    base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    mark = Image.open(io.BytesIO(watermark_bytes)).convert("RGBA")

    target_w = max(int(base.width * 0.25), 1)
    ratio = target_w / mark.width
    mark = mark.resize((target_w, max(int(mark.height * ratio), 1)))

    alpha = mark.split()[3].point(lambda p: int(p * 0.85))
    mark.putalpha(alpha)

    pos = (base.width - mark.width - 16, base.height - mark.height - 16)
    base.alpha_composite(mark, pos)

    out = io.BytesIO()
    base.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue()


def _run_censor(image_bytes: bytes):
    """Jalankan SELURUH proses sensor -- termasuk cek ketersediaan backend
    OCR (`_testimoni_censor_available`, yang di dalamnya ada subprocess call
    `tesseract --version`) -- di dalam SATU thread worker, supaya tidak ada
    bagian manapun yang sempat memblok event loop, dan supaya semuanya bisa
    dibungkus SATU timeout yang sama di _prepare_testimoni_photo. Balikin
    `(image_bytes, applied: bool)` -- `applied=False` kalau memang tidak ada
    mesin OCR yang tersedia sama sekali (bukan gagal/timeout)."""
    if not _testimoni_censor_available():
        return image_bytes, False
    return _redact_sensitive_regions(image_bytes), True


_TESTIMONI_CENSOR_TIMEOUT_SECONDS = 25


async def _prepare_testimoni_photo(context: ContextTypes.DEFAULT_TYPE, file_id: str):
    """Siapkan foto bukti tf sebelum diposting ke channel testimoni: sensor
    info sensitif (kalau diaktifkan & tesseract tersedia) lalu tempel
    watermark (kalau sudah diatur admin). Balikin `file_id` ASLI apa adanya
    kalau tidak ada pemrosesan yang perlu dilakukan, ATAU kalau proses
    gagal di tengah jalan -- supaya testimoni tetap terkirim daripada gagal
    total gara-gara error pemrosesan gambar."""
    censor_on = db.get_setting("testimoni_censor_enabled") == "1"
    watermark_file_id = db.get_setting("testimoni_watermark_file_id")
    if not censor_on and not watermark_file_id:
        return file_id

    try:
        tg_file = await context.bot.get_file(file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception:
        logger.exception("Gagal download foto testimoni (file_id=%s) untuk diproses, kirim apa adanya.", file_id)
        return file_id

    if censor_on:
        try:
            # Timeout eksplisit -- SANGAT PENTING: kalau backend easyocr lagi
            # unduh model pertama kalinya dan server tidak ada akses internet
            # keluar (atau lambat), proses ini bisa menggantung TANPA BATAS
            # tanpa timeout ini -- bikin tombol "Approve ke Channel Testimoni"
            # kelihatan macet/stuck selamanya (lihat testimoni_approve_callback).
            image_bytes, applied = await asyncio.wait_for(
                asyncio.to_thread(_run_censor, image_bytes),
                timeout=_TESTIMONI_CENSOR_TIMEOUT_SECONDS,
            )
            if not applied:
                logger.warning(
                    "Sensor info sensitif testimoni AKTIF tapi tidak ada mesin OCR yang tersedia di "
                    "server (tesseract-ocr / easyocr) -- foto dikirim TANPA sensor. Install salah "
                    "satunya (`pip install easyocr` cukup pip, tidak perlu apt) untuk mengaktifkan."
                )
        except asyncio.TimeoutError:
            logger.warning(
                "Sensor foto testimoni (file_id=%s) timeout setelah %ss -- kemungkinan OCR macet "
                "(mis. easyocr gagal unduh model karena server tanpa akses internet keluar, atau "
                "koneksinya lambat). Foto dikirim TANPA sensor supaya approve tidak ikut macet.",
                file_id, _TESTIMONI_CENSOR_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Gagal sensor foto testimoni (file_id=%s), lanjut tanpa sensor.", file_id)


    if watermark_file_id:
        try:
            wm_file = await context.bot.get_file(watermark_file_id)
            watermark_bytes = bytes(await wm_file.download_as_bytearray())
            image_bytes = await asyncio.to_thread(_apply_testimoni_watermark, image_bytes, watermark_bytes)
        except Exception:
            logger.exception("Gagal tempel watermark testimoni (file_id=%s), lanjut tanpa watermark.", file_id)

    buf = io.BytesIO(image_bytes)
    buf.name = "testimoni.jpg"
    return buf


async def _post_testimoni_photo(context: ContextTypes.DEFAULT_TYPE, channel_raw: str, file_id: str, talent_name: str):
    """Proses (sensor+watermark sesuai pengaturan) lalu kirim foto ke channel
    testimoni (`channel_raw`) dengan caption otomatis menyebut nama talent +
    tag #testimoni. Balikin objek `Message` hasil kirim (dipakai buat bangun
    link postingan, lihat _testimoni_channel_link) -- dipakai baik oleh
    approve manual (testimoni_approve_callback) maupun mode Autoapprove
    (_handle_testimoni_photo).

    PENTING: `talent_name` di-escape dulu lewat _escape_markdown_v1() sebelum
    ditempel ke caption ber-parse_mode="Markdown" -- BUKAN sekadar kosmetik.
    Kalau nama talent mengandung salah satu karakter reserved Markdown (mis.
    underscore, sangat umum di nama/username seperti "Sarah_23"), Telegram
    akan MENOLAK SELURUH send_photo dengan error "can't parse entities" tanpa
    ini -- efeknya foto GAGAL TOTAL terposting ke channel testimoni (bukan
    cuma captionnya yang salah format)."""
    channel_target = int(channel_raw) if channel_raw.lstrip("-").isdigit() else channel_raw
    caption = f"✅ Testimoni order untuk *{_escape_markdown_v1(talent_name)}*\n\n#testimoni"
    photo = await _prepare_testimoni_photo(context, file_id)
    return await context.bot.send_photo(chat_id=channel_target, photo=photo, caption=caption, parse_mode="Markdown")


async def _testimoni_channel_link(context: ContextTypes.DEFAULT_TYPE, channel_raw: str, message_id: int):
    """Bangun link postingan channel testimoni. Channel publik (punya
    @username) -> link biasa yang bisa dibuka siapa saja. Channel privat
    (cuma chat_id) -> link internal t.me/c/... yang cuma bisa dibuka member
    channel tsb. Balikin None kalau gagal (mis. bot bukan admin channel)."""
    try:
        chat = await context.bot.get_chat(channel_raw)
        if chat.username:
            return f"https://t.me/{chat.username}/{message_id}"
        cid = str(chat.id)
        internal_id = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
        return f"https://t.me/c/{internal_id}/{message_id}"
    except Exception:
        logger.exception("Gagal bangun link postingan channel testimoni")
        return None


async def _notify_user_testimoni_pending(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reply_to_message_id: int):
    """Kirim notifikasi 'menunggu konfirmasi' ke user pengirim bukti tf,
    sebagai balasan ke foto yang baru dia kirim. Balikin message_id
    notifikasi ini (nanti DIEDIT jadi status final oleh
    _notify_user_testimoni_approved), atau None kalau gagal kirim
    (mis. user memblokir bot)."""
    try:
        msg = await send_thinking_reply(
            context, chat_id,
            "⏳ Bukti transfer kamu sudah diterima, menunggu konfirmasi admin ya...",
            reply_to_message_id=reply_to_message_id,
        )
        return msg.message_id if msg else None
    except Exception:
        logger.exception("Gagal kirim notifikasi 'menunggu konfirmasi' testimoni ke user (chat_id=%s)", chat_id)
        return None


async def _notify_user_testimoni_approved(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id, channel_link):
    """Ubah notifikasi 'menunggu konfirmasi' di chat user jadi status final
    'sudah disetujui admin', lengkap dengan link postingan di channel
    testimoni (kalau berhasil dibuat) sebagai bukti approve-nya. Kalau edit
    gagal (mis. pesan sudah dihapus user, atau memang belum ada pesan
    pending), fallback kirim pesan baru supaya user tetap dapat kabar."""
    text = "✅ Bukti transfer kamu sudah *dikonfirmasi admin*!"
    if channel_link:
        text += f"\n\n🔗 Lihat postingan testimoninya di sini:\n{channel_link}"
    if message_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="Markdown")
            return
        except Exception:
            logger.warning("Gagal edit notifikasi testimoni user (chat_id=%s, message_id=%s), kirim pesan baru.", chat_id, message_id)
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("Gagal kirim notifikasi approve testimoni ke user (chat_id=%s)", chat_id)


async def _handle_testimoni_photo(context: ContextTypes.DEFAULT_TYPE, session, message):
    """Dipanggil setiap kali user mengirim FOTO selama sesi live chat aktif
    (mis. bukti transfer). Balikin `InlineKeyboardMarkup` untuk ditempel ke
    foto yang di-relay ke grup live chat, atau `None`.

    - Channel testimoni belum diatur -> tidak melakukan apa-apa, balikin None.
    - Channel SUDAH diatur -> user langsung dikirimi notifikasi "menunggu
      konfirmasi admin" (jadi user tahu fotonya diproses, bukan diam saja).
    - Mode Autoapprove AKTIF -> foto LANGSUNG diproses (sensor+watermark) &
      diposting ke channel testimoni saat itu juga, notifikasi user di atas
      langsung diedit jadi "dikonfirmasi" + link postingan -- balikin None
      (tidak ada tombol, karena sudah otomatis terkirim).
    - Mode manual (default/OFF) -> balikin tombol "Approve ke Channel
      Testimoni"; notifikasi "menunggu konfirmasi" baru diedit jadi
      "dikonfirmasi" + link setelah admin menekan tombol itu (lihat
      testimoni_approve_callback)."""
    channel_raw = db.get_setting("testimoni_channel_id")
    if not channel_raw:
        return None

    pending_msg_id = await _notify_user_testimoni_pending(context, session["user_id"], message.message_id)

    if db.get_setting("testimoni_autoapprove") == "1":
        try:
            sent = await _post_testimoni_photo(context, channel_raw, message.photo[-1].file_id, session["talent_name"])
            link = await _testimoni_channel_link(context, channel_raw, sent.message_id) if sent else None
            await _notify_user_testimoni_approved(context, session["user_id"], pending_msg_id, link)
        except Exception:
            logger.exception("Gagal autopost foto testimoni (session #%s)", session["id"])
        return None

    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Approve ke Channel Testimoni",
            callback_data=f"testiapprove_{session['id']}_{pending_msg_id or 0}",
        ),
    ]])


def _testimoni_settings_text():
    """Teks status untuk submenu /settings > "Channel Testimoni" --
    dipakai ulang di semua aksi (buka menu, toggle autoapprove/sensor,
    set/hapus channel & watermark) supaya statusnya selalu konsisten & terkini."""
    channel_raw = db.get_setting("testimoni_channel_id")
    autoapprove = db.get_setting("testimoni_autoapprove") == "1"
    censor = db.get_setting("testimoni_censor_enabled") == "1"
    watermark_file_id = db.get_setting("testimoni_watermark_file_id")

    status_line = f"Channel: `{channel_raw}`" if channel_raw else "Channel: _belum diatur_"
    mode_line = (
        "Mode: 🤖 *Autoapprove* -- foto langsung diposting tanpa approve manual"
        if autoapprove
        else "Mode: ✅ *Approve manual* -- admin klik tombol dulu di grup live chat"
    )
    watermark_line = "Watermark: ✅ terpasang" if watermark_file_id else "Watermark: _belum diatur_"
    backend = _testimoni_censor_backend()
    backend_label = {"tesseract": "Tesseract", "easyocr": "EasyOCR"}.get(backend)
    censor_line = f"Sensor info sensitif: {'✅ ON' if censor else '❌ OFF'}" + (f" (mesin: {backend_label})" if censor and backend_label else "")

    lines = ["📸 *Channel Testimoni*", "", status_line, mode_line, watermark_line, censor_line, ""]
    if censor and not backend:
        lines.append(
            "⚠️ Sensor sedang ON tapi belum ada mesin OCR yang tersedia di server -- foto akan "
            "tetap terkirim TANPA sensor. Install `easyocr` (`pip install easyocr`, tidak perlu "
            "apt/system package) atau `tesseract-ocr` untuk mengaktifkan.\n"
        )
    lines.append(
        "Foto yang dikirim user selama sesi live chat aktif (mis. bukti transfer) akan diproses "
        "(disensor & ditempel watermark sesuai pengaturan di atas) lalu diposting ke channel ini "
        "dengan caption otomatis menyebut nama talent + tag #testimoni. User yang mengirim juga "
        "otomatis dapat notifikasi \"menunggu konfirmasi\" lalu \"dikonfirmasi\" + link postingannya."
    )
    lines.append(
        "\nPunya foto bukti tf sendiri (bukan dari sesi live chat customer) yang mau langsung "
        "diposting ke sini? Reply foto itu lalu ketik `/posttestimoni Nama Talent`."
    )
    return "\n".join(lines)


def _testimoni_menu_kwargs():
    """Kumpulan kwargs terkini untuk kb.testimoni_menu_keyboard(), dibaca
    ulang dari database supaya SELALU mencerminkan state paling baru --
    dipanggil setelah db.set_setting/delete_setting, bukan sebelum."""
    return dict(
        channel_configured=bool(db.get_setting("testimoni_channel_id")),
        autoapprove_enabled=db.get_setting("testimoni_autoapprove") == "1",
        watermark_configured=bool(db.get_setting("testimoni_watermark_file_id")),
        censor_enabled=db.get_setting("testimoni_censor_enabled") == "1",
    )


def _first_message_header(talent_name, full_name, username, user_id):
    return (
        f"Talent : {talent_name}\n"
        f"Dari : {full_name}\n"
        f"Usn : @{username or '-'}\n"
        f"ID : {user_id}"
    )


async def relay_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teruskan pesan dari user (yang punya sesi live chat aktif) ke admin.
    Pesan PERTAMA dalam sesi disertai header lengkap (Talent/Dari/Usn/ID).
    Pesan KEDUA dan seterusnya pakai format ala livechatgram: foto profil user
    (kecil) di kiri dengan nama yang bisa diklik untuk langsung membuka profil
    user, lalu isi pesan di bawahnya.

    CATATAN: sengaja TIDAK ada pengecualian untuk admin/owner di sini. Kalau
    admin/owner menekan "Chat Sekarang" sendiri (mis. untuk testing), sesi
    live chat-nya harus tetap jalan normal seperti user biasa. Ini aman
    karena handler ini cuma jalan kalau memang ADA sesi aktif untuk user_id
    tsb (harus sengaja ditekan tombolnya dulu), dan balasan admin yang
    berupa reply ke pesan relay sudah ditangkap duluan oleh relay_admin_reply
    (didaftarkan lebih dulu di main()), jadi tidak akan bentrok."""
    user = update.effective_user

    session = db.get_active_session_for_user(user.id)
    if not session:
        # Tidak ada sesi live chat aktif -> abaikan, tidak ada yang perlu diteruskan.
        return

    message = update.effective_message

    try:
        # Peringatan "salah menu": kalau user yang sedang chat dengan SATU
        # talent (session["talent_id"]) menyebut nama TALENT LAIN di
        # pesannya, kasih tahu supaya dia tidak salah kira sedang ngobrol
        # dengan talent yang disebut itu -- mis. lagi chat sama Hansel tapi
        # nulis "halo Rina masih ready?", padahal ini bukan menu/chat Rina.
        # Sifatnya cuma HEADS-UP: pesan tetap diteruskan ke admin seperti
        # biasa di bawah, tidak diblokir.
        text_to_check = message.text or message.caption
        if text_to_check and session.get("talent_id"):
            mentioned_talent = _match_talent_from_text(text_to_check.lower(), db.list_talents())
            if mentioned_talent and mentioned_talent["id"] != session["talent_id"]:
                await message.reply_text(
                    f"⚠️ Kamu menyebut *{mentioned_talent['name']}*, tapi obrolan ini sedang "
                    f"berlangsung dengan *{session['talent_name']}*, BUKAN menu talent tersebut.\n\n"
                    f"Kalau kamu mau chat dengan *{mentioned_talent['name']}*, buka dulu halaman "
                    f"detail talent-nya dari daftar talent lalu tekan \"💬 Chat Sekarang\" di sana. "
                    f"Pesanmu barusan tetap kami teruskan ke admin *{session['talent_name']}* seperti biasa.",
                    parse_mode="Markdown",
                )

        is_first = db.count_relay_for_session(session["id"]) == 0
        # Kirim tombol "Akhiri Sesi" hanya sekali, menempel di pesan pertama sesi ini.
        reply_markup = kb.end_chat_keyboard(session["id"]) if is_first else None

        # Peringatan tidak-ada-username: dicek tiap kali user kirim pesan di live
        # chat, tapi pesan peringatannya sendiri hanya dikirim SEKALI (menempel di
        # pesan pertama sesi) supaya user tahu sejak awal tanpa bikin chat berisik
        # kalau diulang-ulang di tiap pesan berikutnya.
        if is_first and not user.username:
            await message.reply_text(
                "⚠️ Akun Telegram Anda belum memiliki *username*.\n\n"
                "Tanpa username, admin akan kesulitan menghubungi atau memverifikasi "
                "ulang identitas Anda di luar sesi chat ini (mis. kalau koneksi terputus). "
                "Mohon atur username terlebih dahulu lewat *Pengaturan > Username* di "
                "aplikasi Telegram Anda.\n\n"
                "Pesan Anda tetap akan kami teruskan ke admin seperti biasa.",
                parse_mode="Markdown",
            )

        targets = []

        # Dihitung SEKALI (bukan di tiap titik copy foto di bawah) supaya di
        # mode Autoapprove foto tidak ke-post dua kali ke channel testimoni.
        testimoni_markup = await _handle_testimoni_photo(context, session, message) if message.photo else None

        if is_first:
            header = _first_message_header(session["talent_name"], user.full_name, user.username, user.id)
            # Pesan teks murni (bukan media) -> gabungkan header + isi pesan jadi satu
            # pesan saja. Sengaja TANPA parse_mode supaya karakter markdown (_, *, dll)
            # yang diketik user tidak bikin pengiriman gagal.
            if message.text and not message.caption:
                body = f"{header}\n\nPesan : {message.text}"
                targets = await broadcast_to_admin_targets(context, body, reply_markup=reply_markup, parse_mode=None)
            else:
                # Pesan berupa media (foto/video/voice/stiker/dsb) -> kirim header dulu
                # sebagai pesan teks terpisah, lalu teruskan media aslinya apa adanya.
                header_targets = await broadcast_to_admin_targets(context, header, reply_markup=reply_markup, parse_mode=None)
                for admin_chat_id, message_id in header_targets:
                    db.add_relay_mapping(message_id, admin_chat_id, session["id"])

                targets = await broadcast_copy_to_admin_targets(
                    context, from_chat_id=update.effective_chat.id, message_id=message.message_id,
                    reply_markup=testimoni_markup,
                )
        else:
            # Format sederhana: nama user (bisa diklik, langsung buka profil
            # user lewat tg://user?id=...) di baris pertama, isi pesan di
            # bawahnya -- TANPA mengirim foto profil (dulu dikirim sebagai
            # foto kecil, sekarang cukup mention teks saja).
            mention = _mention_html(user.full_name, user.id)

            if message.text and not message.caption:
                # HTML-escape isi pesan (bukan mention-nya) supaya karakter
                # spesial ('<', '&', dll) yang diketik user tidak merusak
                # parsing HTML atau membatalkan pengiriman.
                caption_body = f"{mention}\n\n{html_escape(message.text)}"
                targets = await broadcast_to_admin_targets(context, caption_body, parse_mode="HTML")
            else:
                # Pesan berupa media -> kirim "header" (mention nama, bisa
                # diklik) dulu sebagai bubble teks terpisah, baru teruskan
                # media aslinya apa adanya (mengikuti pola yang sama seperti
                # pesan pertama).
                header_targets = await broadcast_to_admin_targets(context, mention, parse_mode="HTML")
                for admin_chat_id, message_id in header_targets:
                    db.add_relay_mapping(message_id, admin_chat_id, session["id"])

                targets = await broadcast_copy_to_admin_targets(
                    context, from_chat_id=update.effective_chat.id, message_id=message.message_id,
                    reply_markup=testimoni_markup,
                )

        for admin_chat_id, message_id in targets:
            db.add_relay_mapping(message_id, admin_chat_id, session["id"])

        if not targets:
            # broadcast_to_admin_targets/broadcast_copy_to_admin_targets sudah
            # mencatat error detail di log -- di sini user WAJIB diberi tahu
            # bahwa pesannya GAGAL terkirim, supaya tidak salah kira admin
            # sedang membaca padahal sebenarnya tidak ada satupun target yang
            # berhasil menerima pesannya (mis. LIVECHAT_GROUP_ID salah/bot
            # bukan admin grup, atau semua admin tidak bisa dihubungi).
            await message.reply_text(
                "⚠️ Pesan Anda gagal diteruskan ke admin. Silakan coba lagi sesaat lagi, "
                "atau hubungi developer kalau masalah ini terus berulang."
            )
            logger.error(
                "Live chat sesi #%s: SEMUA target admin gagal menerima pesan dari user_id=%s.",
                session["id"], user.id,
            )
            return

        # Tampilkan indikator "sedang mengetik..." sekilas ke user setelah pesannya
        # diteruskan, supaya terasa ada respons langsung selagi menunggu balasan admin
        # (animasi "thinking" khas chat AI), meski balasan sesungguhnya baru datang
        # setelah admin membalas.
        await send_typing(context, update.effective_chat.id)
    except Exception:
        logger.exception("Gagal meneruskan pesan live chat dari user_id=%s", user.id)
        try:
            await message.reply_text(
                "⚠️ Terjadi kesalahan saat mengirim pesan Anda ke admin. Silakan coba lagi."
            )
        except Exception:
            logger.exception("Gagal mengirim pesan error fallback di relay_user_message")


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


async def testimoni_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin menekan tombol "✅ Approve ke Channel Testimoni" yang menempel di
    foto yang di-relay user (mis. bukti transfer) di grup live chat. Bot
    mengambil foto itu APA ADANYA dari pesan yang ditempeli tombol (jadi
    tidak perlu akses ulang ke chat user), lalu repost ke channel testimoni
    yang sudah diatur admin lewat /settings > Channel Testimoni (atau
    /settestimoni), dengan caption otomatis menyebut nama talent + tag
    #testimoni. Tombol ini HANYA muncul kalau mode Autoapprove sedang OFF --
    kalau Autoapprove ON, foto sudah otomatis diposting duluan tanpa tombol
    ini sama sekali (lihat _handle_testimoni_photo).

    PENTING soal timing `query.answer()`: dijawab SEGERA (early ack) sebelum
    proses berat (download+sensor+watermark+upload channel) dimulai --
    BUKAN di akhir setelah semuanya selesai. Kalau dijawab di akhir, tombol
    "Approve" akan kelihatan berputar-putar/STUCK selama proses berat itu
    berjalan (apalagi sensor OCR bisa lambat, lihat _TESTIMONI_CENSOR_TIMEOUT_SECONDS
    di _prepare_testimoni_photo). Karena satu callback query cuma boleh
    dijawab SEKALI oleh Telegram, hasil sukses/gagal dari proses berat ini
    dikabari lewat edit caption foto ini, bukan lewat query.answer() lagi.

    Setelah berhasil posting, notifikasi "⏳ menunggu konfirmasi" yang tadi
    dikirim ke user pengirim foto (lihat _notify_user_testimoni_pending)
    diedit jadi "✅ dikonfirmasi admin" lengkap dengan link postingannya."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Khusus admin.", show_alert=True)
        return

    try:
        _, session_id_str, pending_msg_id_str = query.data.split("_")
        session_id = int(session_id_str)
        pending_msg_id = int(pending_msg_id_str) or None
    except (IndexError, ValueError):
        await query.answer()
        return

    channel_raw = db.get_setting("testimoni_channel_id")
    if not channel_raw:
        await query.answer("⚠️ Channel testimoni belum diatur. Atur dulu lewat /settings > Channel Testimoni.", show_alert=True)
        return

    photo = query.message.photo
    if not photo:
        await query.answer("⚠️ Pesan ini bukan foto, tidak bisa diposting ke testimoni.", show_alert=True)
        return

    # Matikan animasi loading tombol SEKARANG, sebelum proses berat di bawah
    # -- lihat catatan timing di docstring atas.
    await query.answer("⏳ Memproses & memposting ke channel testimoni...")

    session = db.get_session(session_id)
    talent_name = session["talent_name"] if session else "-"
    file_id = photo[-1].file_id

    try:
        sent = await _post_testimoni_photo(context, channel_raw, file_id, talent_name)
    except Exception as e:
        logger.exception("Gagal posting foto testimoni ke channel testimoni (session #%s)", session_id)
        try:
            existing_caption = query.message.caption or ""
            note = f"❌ Gagal diposting ke channel testimoni: {e}\n(Tombol masih aktif, coba tekan Approve lagi.)"
            await query.edit_message_caption(caption=(existing_caption + "\n\n" if existing_caption else "") + note)
        except Exception:
            logger.warning("Gagal update caption setelah approve testimoni GAGAL (mungkin sudah diedit sebelumnya).")
        return

    if session:
        link = await _testimoni_channel_link(context, channel_raw, sent.message_id) if sent else None
        await _notify_user_testimoni_approved(context, session["user_id"], pending_msg_id, link)

    try:
        existing_caption = query.message.caption or ""
        note = f"✅ Sudah diposting ke channel testimoni oleh {query.from_user.full_name}."
        new_caption = (existing_caption + "\n\n" if existing_caption else "") + note
        await query.edit_message_caption(caption=new_caption, reply_markup=None)
    except Exception:
        logger.warning("Gagal update caption foto setelah approve testimoni (mungkin sudah diedit sebelumnya).")


async def posttestimoni_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/posttestimoni <Nama Talent> -- balas (reply) foto BUKTI TRANSFER yang
    mau diposting ke channel testimoni, lalu ketik `/posttestimoni Nama Talent`.
    Khusus admin/owner.

    KENAPA FITUR INI PERLU (terpisah dari tombol "✅ Approve ke Channel
    Testimoni" yang sudah ada di testimoni_approve_callback): tombol Approve
    itu HANYA pernah menempel pada foto yang di-relay dari SESI LIVE CHAT
    customer (lihat _handle_testimoni_photo, dipanggil dari relay_user_message
    yang cuma jalan untuk pesan di PRIVATE CHAT dengan sesi live chat aktif).
    Kalau admin/owner PUNYA SENDIRI foto bukti tf (mis. dari luar Telegram,
    atau transfer manual di luar alur live chat) dan mau langsung
    memostingnya ke channel testimoni, foto itu TIDAK PERNAH datang dari sesi
    live chat manapun -- jadi tombol Approve tidak akan pernah muncul untuk
    foto itu, dan sebelum command ini ada, owner sama sekali tidak punya
    cara untuk memposting foto tsb ke channel testimoni. Command ini
    menyediakan jalur langsung: admin reply foto itu sendiri (dari mana pun
    asalnya), lalu ketik command ini -- tanpa perlu foto itu pernah melewati
    sesi live chat sama sekali."""
    if not await _group_admin_command_allowed(update, context):
        return
    if not is_admin(update.effective_user.id):
        return

    replied = update.message.reply_to_message
    photo_file_id = None
    if replied:
        if replied.photo:
            photo_file_id = replied.photo[-1].file_id
        elif replied.document and (replied.document.mime_type or "").startswith("image/"):
            photo_file_id = replied.document.file_id

    if not photo_file_id:
        await update.message.reply_text(
            "⚠️ *Cara pakai:* balas (reply) foto bukti transfer yang mau diposting ke channel "
            "testimoni, lalu ketik `/posttestimoni Nama Talent`.\n\n"
            "Contoh: reply foto bukti tf, lalu ketik `/posttestimoni Hansel`.",
            parse_mode="Markdown",
        )
        return

    name_query = " ".join(context.args or []).strip()
    if not name_query:
        await update.message.reply_text(
            "⚠️ Sertakan nama talent-nya. Contoh: `/posttestimoni Hansel`", parse_mode="Markdown",
        )
        return

    talents = db.list_talents()
    talent = next((t for t in talents if (t["name"] or "").strip().lower() == name_query.lower()), None)
    if not talent:
        talent = _match_talent_from_text(name_query.lower(), talents)
    if not talent:
        await update.message.reply_text(
            f"⚠️ Talent dengan nama \"{name_query}\" tidak ditemukan di daftar talent. "
            "Cek ejaan namanya, atau tambahkan talent-nya dulu lewat menu /settings."
        )
        return

    channel_raw = db.get_setting("testimoni_channel_id")
    if not channel_raw:
        await update.message.reply_text(
            "⚠️ Channel testimoni belum diatur. Atur dulu lewat /settings > Channel Testimoni."
        )
        return

    processing_msg = await update.message.reply_text("⏳ Memproses & memposting ke channel testimoni...")

    try:
        sent = await _post_testimoni_photo(context, channel_raw, photo_file_id, talent["name"])
    except Exception as e:
        logger.exception("Gagal posting foto testimoni manual (owner) untuk talent #%s", talent["id"])
        await processing_msg.edit_text(f"❌ Gagal diposting ke channel testimoni: {e}")
        return

    link = await _testimoni_channel_link(context, channel_raw, sent.message_id) if sent else None
    result_text = f"✅ Foto bukti tf berhasil diposting ke channel testimoni untuk *{_escape_markdown_v1(talent['name'])}*."
    if link:
        result_text += f"\n\n🔗 {link}"
    await processing_msg.edit_text(result_text, parse_mode="Markdown")


def _build_active_sessions_text(sessions, prefix=None):
    """Bangun teks daftar "Sesi Live Chat Aktif" -- dipakai bareng oleh tombol
    /settings > "💬 Sesi Live Chat Aktif", command /resetlc, dan fallback
    tampilan di reset_chat_callback(), supaya ketiganya konsisten & sama-sama
    kebal dari bug yang sama.

    SENGAJA TIDAK memakai parse_mode="Markdown": talent_name/full_name/
    username di sini berasal dari data user Telegram & talent yang bisa saja
    mengandung karakter spesial Markdown (mis. underscore di username/nama
    orang itu wajar & umum). Kalau dipaksa parse_mode="Markdown", Telegram
    akan menolak pesannya ("can't parse entities") begitu ada sesi dengan
    karakter seperti itu -- inilah penyebab tombol "Sesi Live Chat Aktif"
    sebelumnya bisa error. Baris judul dibuat mencolok pakai emoji, bukan
    sintaks markdown, supaya tetap aman dikirim apa adanya (parse_mode=None)."""
    lines = [prefix] if prefix else []
    lines.append("💬 Sesi Live Chat Aktif:\n")
    for s in sessions:
        username = s.get("username") or "-"
        lines.append(f"#{s['id']} - {s['talent_name']} - {s['full_name']} (@{username})")
    lines.append(
        "\nKalau ada user yang mengeluh terjebak/tidak dibalas, tekan tombol "
        "♻️ Reset di bawah untuk sesi terkait -- sesi itu akan ditutup & "
        "dibersihkan supaya user bisa langsung memulai live chat yang baru."
    )
    return "\n".join(lines)


async def reset_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin menekan tombol '♻️ Reset Sesi (Stuck)' -- dipakai khusus untuk
    membersihkan data sesi live chat seorang user yang MACET/nyangkut di
    database (mis. relay-nya kacau, admin ganti device, atau user komplain
    tidak kunjung dibalas padahal tidak ada pesan masuk ke admin). Beda dari
    "Akhiri Sesi" biasa: status sesi ditandai 'reset' + seluruh pemetaan
    relay pesannya ikut dihapus, supaya user bisa LANGSUNG menekan
    "💬 Chat Sekarang" lagi untuk memulai sesi live chat yang baru, bersih
    dari sisa data sesi lama. Bisa dipicu dari tombol di header sesi (grup/
    private admin) MAUPUN dari daftar "Sesi Live Chat Aktif" di /settings."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Khusus admin.", show_alert=True)
        return
    await query.answer()

    session_id = int(query.data.split("_", 1)[1])
    session = db.get_session(session_id)
    if not session:
        await query.answer("Sesi tidak ditemukan (mungkin sudah pernah direset).", show_alert=True)
        return

    was_active = session["status"] == "active"
    db.reset_session(session_id)

    if was_active:
        try:
            await context.bot.send_message(
                chat_id=session["user_id"],
                text="🔄 Sesi live chat Anda telah *direset oleh admin* karena mengalami kendala teknis.\n\n"
                     "Mohon maaf atas ketidaknyamanannya -- silakan tekan tombol \"💬 Chat Sekarang\" lagi "
                     "untuk memulai sesi live chat yang baru.",
                parse_mode="Markdown",
                reply_markup=kb.main_menu_keyboard(),
            )
        except Exception:
            logger.exception("Gagal memberi tahu user bahwa sesi live chat direset")

    # Kalau dipicu dari header sesi live chat (formatnya selalu diawali "Talent :"
    # -- lihat _first_message_header) -> update pesan itu di tempat. Kalau dipicu
    # dari daftar "Sesi Live Chat Aktif" di /settings -> refresh daftarnya.
    if query.message and query.message.text and query.message.text.startswith("Talent :"):
        try:
            base_text = query.message.text or ""
            await query.edit_message_text(text=base_text + "\n\n♻️ Sesi telah direset oleh admin.", reply_markup=None)
        except Exception:
            logger.warning("Gagal update pesan header sesi live chat setelah direset.")
        return

    sessions = db.list_active_sessions()
    if not sessions:
        await replace_message(
            query, context, "✅ Sesi direset. Tidak ada lagi sesi live chat yang aktif saat ini.",
            reply_markup=kb.settings_menu_keyboard(),
        )
    else:
        text = _build_active_sessions_text(sessions, prefix="✅ Sesi direset.\n")
        await replace_message(
            query, context, text,
            reply_markup=kb.active_sessions_keyboard(sessions),
        )


async def resetlc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resetlc -- shortcut khusus admin: langsung tampilkan daftar "Sesi Live
    Chat Aktif" beserta tombol ♻️ Reset per-sesi, PERSIS seperti tombol
    "💬 Sesi Live Chat Aktif" di /settings -- tanpa perlu buka menu /settings
    dulu, supaya admin bisa langsung reset sesi yang macet/stuck begitu ada
    laporan dari user."""
    if not await _group_command_allowed(update, context):
        return
    if not is_admin(update.effective_user.id):
        return

    sessions = db.list_active_sessions()
    if not sessions:
        await update.message.reply_text("Tidak ada sesi live chat yang aktif saat ini.")
        return

    text = _build_active_sessions_text(sessions)
    await update.message.reply_text(text, reply_markup=kb.active_sessions_keyboard(sessions))


# ==================== /addadmin, /listadmin, /removeadmin ====================
# HANYA owner (config.ADMIN_IDS, dari environment variable server) yang boleh
# menambah/menghapus admin -- BUKAN semua admin (is_admin() juga true untuk
# admin tambahan hasil /addadmin), supaya cuma pemilik bot yang bisa
# mengangkat/mencabut admin baru. /listadmin tetap boleh dipakai semua admin
# (cuma menampilkan info, tidak mengubah apa pun).
#
# /addadmin & /removeadmin dipakai dengan REPLY ke pesan user yang dituju
# (BUKAN lagi ketik user_id manual) -- paling praktis dipakai langsung di
# GRUP LIVE CHAT: owner reply salah satu pesan yang diteruskan bot dari user
# tsb, lalu ketik /addadmin, dan user itu langsung jadi admin yang bisa ikut
# mengelola & membalas live chat (lewat is_admin() & AdminReplyFilter).

async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addadmin -- balas (reply) pesan user yang mau dijadikan admin, lalu
    ketik /addadmin tanpa argumen. Khusus owner."""
    if not await _group_admin_command_allowed(update, context):
        return
    if update.effective_user.id not in config.ADMIN_IDS:
        return

    replied = update.message.reply_to_message
    if not replied or not replied.from_user:
        await update.message.reply_text(
            "⚠️ *Cara pakai:* balas (reply) salah satu pesan dari user yang mau "
            "dijadikan admin, lalu ketik `/addadmin` (tanpa argumen apa pun).\n\n"
            "Contoh: di grup live chat, reply pesan yang diteruskan bot dari "
            "user tsb, lalu ketik /addadmin.",
            parse_mode="Markdown",
        )
        return

    target = replied.from_user
    if target.is_bot:
        await update.message.reply_text("⚠️ Tidak bisa menjadikan akun bot sebagai admin.")
        return

    if is_admin(target.id):
        await update.message.reply_text("ℹ️ User ini sudah menjadi admin.")
        return

    try:
        db.add_bot_admin(target.id, target.username, target.full_name, update.effective_user.id)
    except Exception:
        logger.exception("Gagal simpan admin baru %s ke database (mungkin tabel bot_admins belum ada -- pastikan database.py sudah di-upload versi terbaru & server sudah di-restart).", target.id)
        await update.message.reply_text(
            "⚠️ Gagal menyimpan admin baru ke database. Kemungkinan server belum "
            "di-restart setelah update terakhir, atau ada masalah lain di database. "
            "Coba restart server lalu ulangi lagi."
        )
        return

    # Perbarui menu perintah "/" khusus admin buat user yang baru ditambahkan.
    try:
        public_commands = [
            BotCommand("start", "Buka menu utama"),
            BotCommand("help", "Bantuan & cara pakai bot"),
            BotCommand("about", "Tentang bot ini"),
            BotCommand("statustalent", "Lihat status ready/tidak semua talent"),
        ]
        await context.bot.set_my_commands(
            _admin_commands_list(public_commands),
            scope=BotCommandScopeChat(chat_id=target.id),
        )
    except Exception:
        logger.warning("Gagal mengatur daftar perintah admin untuk admin baru %s.", target.id)

    display_name = f"@{target.username}" if target.username else target.full_name
    await update.message.reply_text(
        f"✅ Berhasil menambahkan {display_name} (`{target.id}`) sebagai admin.\n\n"
        "Admin ini sekarang bisa membuka /settings dan membalas chat pengguna "
        "saat live chat aktif.",
        parse_mode="Markdown",
    )


async def listadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/listadmin -- lihat daftar admin utama & admin tambahan. Boleh dipakai
    semua admin (bukan cuma owner), cuma menampilkan info."""
    if not await _group_admin_command_allowed(update, context):
        return
    if not is_admin(update.effective_user.id):
        return

    lines = ["👮 *Daftar Admin Bot*", "", "_Admin utama (dari konfigurasi server):_"]
    for admin_id in config.ADMIN_IDS:
        lines.append(f"• `{admin_id}`")

    extra_admins = db.list_bot_admins()
    lines.append("")
    lines.append("_Admin tambahan (via /addadmin):_")
    if not extra_admins:
        lines.append("_(belum ada)_")
    else:
        for a in extra_admins:
            display = f"@{a['username']}" if a["username"] else (a["full_name"] or "-")
            lines.append(f"• {display} — `{a['user_id']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/removeadmin -- balas (reply) pesan admin tambahan yang mau dicabut,
    ATAU ketik /removeadmin <user_id> kalau tidak ada pesannya untuk di-reply.
    Khusus owner. Tidak bisa dipakai untuk mencabut admin utama (config)."""
    if not await _group_admin_command_allowed(update, context):
        return
    if update.effective_user.id not in config.ADMIN_IDS:
        return

    replied = update.message.reply_to_message
    if replied and replied.from_user:
        target_id = replied.from_user.id
    elif context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("⚠️ user_id harus berupa angka.")
            return
    else:
        await update.message.reply_text(
            "⚠️ *Cara pakai:* balas (reply) pesan dari admin yang mau dicabut lalu "
            "ketik `/removeadmin`, atau ketik `/removeadmin <user_id>` langsung.",
            parse_mode="Markdown",
        )
        return

    if target_id in config.ADMIN_IDS:
        await update.message.reply_text(
            "⚠️ User ini adalah admin utama (dikonfigurasi lewat server), "
            "tidak bisa dihapus lewat perintah ini."
        )
        return

    if not db.is_bot_admin(target_id):
        await update.message.reply_text("ℹ️ User ini bukan admin tambahan.")
        return

    db.remove_bot_admin(target_id)
    await update.message.reply_text(f"✅ Admin `{target_id}` berhasil dihapus.", parse_mode="Markdown")


# ==================== /settings ====================

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _group_command_allowed(update, context):
        return
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
            await replace_message(query, context, text, reply_markup=kb.settings_menu_keyboard())
            return ConversationHandler.END

        text = _build_active_sessions_text(sessions)
        await replace_message(query, context, text, reply_markup=kb.active_sessions_keyboard(sessions))
        return ConversationHandler.END

    if query.data == "settings_testimoni":
        await replace_message(
            query, context, _testimoni_settings_text(), parse_mode="Markdown",
            reply_markup=kb.testimoni_menu_keyboard(**_testimoni_menu_kwargs()),
        )
        return ConversationHandler.END

    if query.data == "settings_testimoni_toggleautoapprove":
        current = db.get_setting("testimoni_autoapprove") == "1"
        db.set_setting("testimoni_autoapprove", "0" if current else "1")
        await replace_message(
            query, context, _testimoni_settings_text(), parse_mode="Markdown",
            reply_markup=kb.testimoni_menu_keyboard(**_testimoni_menu_kwargs()),
        )
        return ConversationHandler.END

    if query.data == "settings_testimoni_togglecensor":
        current = db.get_setting("testimoni_censor_enabled") == "1"
        db.set_setting("testimoni_censor_enabled", "0" if current else "1")
        await replace_message(
            query, context, _testimoni_settings_text(), parse_mode="Markdown",
            reply_markup=kb.testimoni_menu_keyboard(**_testimoni_menu_kwargs()),
        )
        return ConversationHandler.END

    if query.data == "settings_testimoni_removechannel":
        db.delete_setting("testimoni_channel_id")
        db.delete_setting("testimoni_autoapprove")
        await replace_message(
            query, context, _testimoni_settings_text(), parse_mode="Markdown",
            reply_markup=kb.testimoni_menu_keyboard(**_testimoni_menu_kwargs()),
        )
        return ConversationHandler.END

    if query.data == "settings_testimoni_removewatermark":
        db.delete_setting("testimoni_watermark_file_id")
        await replace_message(
            query, context, _testimoni_settings_text(), parse_mode="Markdown",
            reply_markup=kb.testimoni_menu_keyboard(**_testimoni_menu_kwargs()),
        )
        return ConversationHandler.END

    if query.data == "settings_testimoni_setchannel":
        await replace_message(
            query, context,
            "Kirim *@username channel* atau *chat_id* channel testimoni tujuan.\n\n"
            "Pastikan bot sudah jadi admin channel tsb (dengan izin kirim pesan) -- "
            "akan langsung dicek begitu Anda kirim.",
            parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard(),
        )
        return EDIT_TESTIMONI_CHANNEL

    if query.data == "settings_testimoni_setwatermark":
        await replace_message(
            query, context,
            "Kirim *stiker statis* atau *gambar* (foto/PNG/JPG) untuk dijadikan watermark.\n\n"
            "Watermark akan ditempel otomatis di pojok kanan-bawah tiap foto yang diposting "
            "ke channel testimoni. Stiker *animasi/video* tidak didukung.",
            parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard(),
        )
        return EDIT_TESTIMONI_WATERMARK

    if query.data == "settings_groupstartmedia":
        file_id = db.get_setting("group_start_media_file_id")
        kind = db.get_setting("group_start_media_kind", "sticker")
        status = f"terpasang (jenis: {kind})" if file_id else "belum diatur"
        text = (
            "🐹 *Animasi Sapaan Grup (tombol \"Mulai\")*\n\n"
            "Animasi ini otomatis dikirim di grup begitu user menekan tombol "
            "🚀 *Mulai*, sebelum pesan panduan teks muncul menyusul.\n\n"
            f"Status saat ini: {status}\n\n"
            "Tekan *Edit* untuk mengatur/mengganti."
        )
        await replace_message(
            query, context, text, parse_mode="Markdown",
            reply_markup=kb.preview_edit_keyboard("settings_groupstartmedia_edit"),
        )
        return ConversationHandler.END

    if query.data == "settings_groupstartmedia_edit":
        await replace_message(
            query, context,
            "Kirim *stiker* (disarankan -- termasuk stiker video/animasi yang "
            "latar belakangnya transparan, mis. karakter yang sedang melambai), "
            "atau kirim *GIF/animasi* atau *video pendek tanpa suara* sebagai "
            "gantinya.\n\n"
            "Ketik `hapus` untuk menghapus animasi sapaan yang sudah terpasang "
            "(kembali ke sapaan teks biasa saja).",
            parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard(),
        )
        return EDIT_GROUP_START_MEDIA

    if query.data == "settings_admins":
        await replace_message(
            query, context,
            "👥 *Kelola Admin Grup*\n\n"
            "Kartu admin di sini ditampilkan di halaman \"Admin Grup\" pada Mini App "
            "(foto profil, nama, username, jabatan, dan tombol Chat).",
            parse_mode="Markdown",
            reply_markup=kb.group_admins_menu_keyboard(),
        )
        return ConversationHandler.END

    if query.data == "settings_listadmins":
        admins = db.list_group_admins()
        if not admins:
            await replace_message(
                query, context, "Belum ada admin yang ditambahkan.",
                reply_markup=kb.group_admins_menu_keyboard(),
            )
            return ConversationHandler.END
        await replace_message(
            query, context,
            "Pilih admin: tap ✏️ untuk edit datanya, atau 🗑 untuk hapus langsung.",
            reply_markup=kb.group_admins_list_keyboard(admins),
        )
        return ConversationHandler.END

    if query.data.startswith("editadminfield_"):
        _, admin_id_str, field = query.data.split("_", 2)
        admin_id = int(admin_id_str)
        admin = db.get_group_admin(admin_id)
        if not admin:
            await replace_message(query, context, "Admin tidak ditemukan (mungkin sudah dihapus).", reply_markup=kb.group_admins_menu_keyboard())
            return ConversationHandler.END
        context.user_data["edit_group_admin"] = {"id": admin_id, "field": field}
        prompts = {
            "username": "Kirim *username* baru (tanpa @, atau ketik `-` untuk mengosongkan):",
            "full_name": "Kirim *nama lengkap* baru:",
            "jabatan": "Kirim *jabatan* baru (atau ketik `-` untuk mengosongkan):",
            "photo_file_id": "Kirim *foto* baru untuk admin ini:",
        }
        await replace_message(
            query, context, prompts[field], parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard(),
        )
        return EDIT_GROUP_ADMIN_VALUE

    if query.data.startswith("editadmin_"):
        admin_id = int(query.data.split("_")[1])
        admin = db.get_group_admin(admin_id)
        if not admin:
            await replace_message(query, context, "Admin tidak ditemukan (mungkin sudah dihapus).", reply_markup=kb.group_admins_menu_keyboard())
            return ConversationHandler.END
        text = (
            f"✏️ *Edit Admin: {admin['full_name'] or admin['username'] or admin['user_id']}*\n\n"
            f"Username: {('@' + admin['username']) if admin.get('username') else '-'}\n"
            f"Nama Lengkap: {admin.get('full_name') or '-'}\n"
            f"Jabatan: {admin.get('jabatan') or '-'}\n"
            f"ID Telegram: {admin['user_id']}\n\n"
            "Pilih bagian yang ingin diubah:"
        )
        await replace_message(query, context, text, parse_mode="Markdown", reply_markup=kb.edit_group_admin_field_keyboard(admin))
        return ConversationHandler.END

    if query.data == "settings_addadmin":
        await replace_message(
            query, context,
            "➕ *Tambah Admin Grup*\n\n"
            "Kirim dalam *satu pesan*, format:\n"
            "`@username id_telegram Jabatan`\n\n"
            "Contoh:\n"
            "`@johndoe 123456789 Admin Order`\n\n"
            "Foto profil, nama, akan diambil otomatis dari Telegram user tsb kalau bisa didapat "
            "(user harus pernah start bot ini dulu / punya foto profil publik).",
            parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard(),
        )
        return ADD_GROUP_ADMIN

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
            f"Link Channel: {talent.get('portfolio_url') or '-'}\n"
            f"Status: {_talent_status_badge(talent['id']).replace(chr(10)+chr(10), '') or '_belum absen_'}\n\n"
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

    if query.data == "settings_promotext":
        current_text = db.get_setting("promo_text", _DEFAULT_PROMO_TEXT)
        text = (
            "✏️ *Teks Isi Promo Talent saat ini:*\n\n"
            f"{current_text}\n\n"
            "Tekan *Edit* untuk mengubahnya."
        )
        await replace_message(
            query, context, text, parse_mode="Markdown",
            reply_markup=kb.preview_edit_keyboard("settings_promotext_edit", back_callback="settings_autopromo"),
        )
        return ConversationHandler.END

    if query.data == "settings_promotext_edit":
        await replace_message(
            query, context,
            "Kirim teks baru untuk isi pesan *Auto Promo Talent Ready*.\n\n"
            "Wajib sertakan placeholder `{daftar_talent}` di bagian yang mau diisi "
            "daftar talent yang lagi ready (satu baris per talent, terisi otomatis "
            "tiap promo diposting) -- sisanya (header/footer/emoji) bebas kamu atur.\n\n"
            "Contoh default:\n"
            f"`{_DEFAULT_PROMO_TEXT}`",
            parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard("settings_autopromo"),
        )
        return EDIT_PROMO_TEXT

    if query.data == "settings_autopromo":
        interval = db.get_setting("promo_interval_minutes")
        topics = _get_promo_topics()
        text = (
            "📢 *Auto Promo Talent*\n\n"
            "Semua pengaturan posting promo talent ready ada di sini: teks pesan, "
            "jadwal berulang, grup/topik tujuan, dan posting/hapus manual.\n\n"
            f"⏰ Jadwal saat ini: {'tiap ' + interval + ' menit' if interval else 'OFF'}\n"
            f"📋 Grup/topik terdaftar: {len(topics)}/{_MAX_PROMO_TOPICS}"
        )
        await replace_message(
            query, context, text, parse_mode="Markdown",
            reply_markup=kb.autopromo_menu_keyboard(interval, len(topics), _MAX_PROMO_TOPICS),
        )
        return ConversationHandler.END

    if query.data == "settings_autopromo_jadwal":
        current = db.get_setting("promo_interval_minutes")
        text = (
            "⏰ *Atur Jadwal Auto Promo*\n\n"
            f"Jadwal saat ini: {'tiap *' + current + ' menit*' if current else '*OFF*'}\n\n"
            "Kirim angka *menit* untuk jadwal posting berulang (mis. `120` = tiap 2 jam), "
            "atau ketik `off` untuk mematikan jadwal berulang (posting manual lewat tombol "
            "\"🚀 Posting Sekarang\" tetap selalu bisa dipakai)."
        )
        await replace_message(
            query, context, text, parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard("settings_autopromo"),
        )
        return EDIT_PROMO_INTERVAL

    if query.data == "settings_autopromo_topics":
        topics = _get_promo_topics()
        if not topics:
            text = (
                "Belum ada grup/topik tujuan promo yang terdaftar.\n\n"
                "Untuk mendaftarkan grup/topik baru, jalankan `/setpromogrup` DI DALAM grup "
                "tujuannya (Telegram tidak mengizinkan bot mendaftarkan grup dari luar grup "
                "itu sendiri, jadi langkah ini tidak bisa lewat /settings)."
            )
        else:
            lines = [f"📋 *Grup/topik Auto Promo terdaftar* ({len(topics)}/{_MAX_PROMO_TOPICS}):", ""]
            for i, t in enumerate(topics, 1):
                lines.append(f"{i}. Grup `{t.get('group_id')}` / topik `{t.get('topic_id') or '-'}`")
            lines.append("")
            lines.append(
                "Tekan salah satu di bawah untuk melepasnya. Untuk mendaftarkan yang baru, "
                "jalankan `/setpromogrup` DI DALAM grup tujuannya."
            )
            text = "\n".join(lines)
        await replace_message(
            query, context, text, parse_mode="Markdown",
            reply_markup=kb.autopromo_topics_keyboard(topics),
        )
        return ConversationHandler.END

    if query.data.startswith("autopromo_deltopic_"):
        topics = _get_promo_topics()
        try:
            index = int(query.data.split("_")[-1])
            topic = topics[index]
        except (ValueError, IndexError):
            await replace_message(
                query, context, "Grup/topik ini sudah tidak ada di daftar (mungkin baru saja dilepas).",
                reply_markup=kb.autopromo_topics_keyboard(_get_promo_topics()),
            )
            return ConversationHandler.END
        new_topics = topics[:index] + topics[index + 1:]
        _save_promo_topics(new_topics)
        await _delete_promo_message(context, topic.get("group_id"), topic.get("topic_id"))
        await replace_message(
            query, context,
            f"✅ Grup `{topic.get('group_id')}` / topik `{topic.get('topic_id') or '-'}` sudah dilepas dari daftar Auto Promo.",
            parse_mode="Markdown",
            reply_markup=kb.autopromo_topics_keyboard(new_topics),
        )
        return ConversationHandler.END

    if query.data == "settings_autopromo_postnow":
        interval = db.get_setting("promo_interval_minutes")
        topics = _get_promo_topics()
        if not topics:
            text = "⚠️ Belum ada grup/topik tujuan promo yang terdaftar."
        elif not _list_ready_talents():
            text = "ℹ️ Tidak ada talent yang READY sekarang, promo tidak diposting."
        else:
            await _post_promo_job(context)
            text = "✅ Promo sudah diposting ke semua grup/topik terdaftar."
        await replace_message(
            query, context, text,
            reply_markup=kb.autopromo_menu_keyboard(interval, len(topics), _MAX_PROMO_TOPICS),
        )
        return ConversationHandler.END

    if query.data == "settings_autopromo_delnow":
        interval = db.get_setting("promo_interval_minutes")
        topics = _get_promo_topics()
        if not topics:
            text = "⚠️ Belum ada grup/topik tujuan promo yang terdaftar."
        else:
            deleted_count = await _delete_all_promo_messages(context)
            text = (
                f"✅ Pesan promo terakhir sudah dihapus dari {deleted_count} grup/topik."
                if deleted_count
                else "ℹ️ Tidak ada pesan promo yang tercatat untuk dihapus (mungkin belum pernah posting)."
            )
        await replace_message(
            query, context, text,
            reply_markup=kb.autopromo_menu_keyboard(interval, len(topics), _MAX_PROMO_TOPICS),
        )
        return ConversationHandler.END

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
            "marquee_desc": "Kirim *deskripsi melayang* baru untuk sponsor ini -- teks ini yang akan "
                "tampil vertikal di samping logo pada sponsor melayang (ketik `-` untuk mengosongkan):",
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
            f"Deskripsi Melayang: {sponsor.get('marquee_desc') or '-'}\n"
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

    if query.data == "settings_toggleprotectcontent":
        current = db.get_setting("protect_content_enabled", "0")
        new_value = "0" if current == "1" else "1"
        db.set_setting("protect_content_enabled", new_value)
        if new_value == "1":
            status_text = (
                "🛡️ Proteksi Konten *diaktifkan* ✅\n\n"
                "Mulai sekarang, foto & pesan yang dikirim bot ini (profil talent, "
                "live chat, dll) *tidak bisa di-forward/diteruskan* dan tombol simpan "
                "medianya disembunyikan -- ini berlaku di aplikasi Telegram resmi "
                "(Android/iOS/Desktop).\n\n"
                "⚠️ *Penting, biar tidak salah ekspektasi:* proteksi ini TIDAK bisa "
                "mencegah orang memotret layar HP-nya pakai kamera/HP lain, dan juga "
                "tidak bisa mem-blokir screenshot di halaman web/Mini App -- itu di "
                "luar kemampuan bot atau aplikasi web manapun, karena kamera fisik "
                "menangkap cahaya layar langsung, bukan lewat sistem yang bisa "
                "dikendalikan software."
            )
        else:
            status_text = "🛡️ Proteksi Konten *dinonaktifkan* ❌ (forward/simpan media kembali seperti biasa)."
        await replace_message(
            query, context, status_text,
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


async def edit_testimoni_channel_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima @username channel atau chat_id yang dikirim admin setelah
    menekan "➕ Set Channel ID"/"✏️ Ganti Channel ID" di submenu Channel
    Testimoni, verifikasi bot benar-benar bisa kirim pesan ke sana (kirim
    lalu langsung hapus pesan uji), baru simpan."""
    text = update.message.text.strip()
    target = int(text) if text.lstrip("-").isdigit() else text

    try:
        chat = await context.bot.get_chat(target)
        test_msg = await context.bot.send_message(chat_id=target, text="✅ Channel testimoni berhasil terhubung.")
        await test_msg.delete()
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Gagal terhubung ke `{text}`.\n"
            "Pastikan bot sudah jadi admin channel tsb (dengan izin kirim pesan) dan "
            "chat_id/username-nya benar. Coba kirim lagi, atau tekan Kembali untuk membatalkan.",
            parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard(),
        )
        return EDIT_TESTIMONI_CHANNEL

    db.set_setting("testimoni_channel_id", text)
    await update.message.reply_text(
        f"✅ Channel testimoni diatur ke *{chat.title or text}*.",
        parse_mode="Markdown",
        reply_markup=kb.testimoni_menu_keyboard(**_testimoni_menu_kwargs()),
    )
    return ConversationHandler.END


async def edit_testimoni_watermark_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima stiker statis atau gambar yang dikirim admin setelah menekan
    "🖼️ Set Watermark"/"✏️ Ganti Watermark", validasi bisa dibaca sebagai
    gambar oleh Pillow (supaya langsung ketahuan kalau formatnya tidak
    didukung, bukan baru gagal nanti pas ada foto testimoni yang diproses),
    baru simpan file_id-nya."""
    message = update.message

    if message.sticker and (message.sticker.is_animated or message.sticker.is_video):
        await message.reply_text(
            "⚠️ Watermark harus berupa gambar *statis* -- stiker animasi/video tidak didukung. "
            "Kirim stiker statis, atau kirim sebagai foto/gambar biasa.",
            parse_mode="Markdown",
        )
        return EDIT_TESTIMONI_WATERMARK

    file_id = None
    if message.sticker:
        file_id = message.sticker.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id
    else:
        await message.reply_text(
            "⚠️ Kirim *stiker statis* atau *gambar* (foto/PNG/JPG) untuk dijadikan watermark.",
            parse_mode="Markdown",
        )
        return EDIT_TESTIMONI_WATERMARK

    try:
        tg_file = await context.bot.get_file(file_id)
        raw = bytes(await tg_file.download_as_bytearray())
        Image.open(io.BytesIO(raw)).convert("RGBA")  # validasi format kebaca Pillow
    except Exception:
        logger.exception("Gagal validasi file watermark testimoni")
        await message.reply_text(
            "⚠️ Gagal membaca file ini sebagai gambar. Coba kirim dalam format PNG/JPG "
            "(sebagai foto biasa), atau stiker statis lain.",
            parse_mode="Markdown",
        )
        return EDIT_TESTIMONI_WATERMARK

    db.set_setting("testimoni_watermark_file_id", file_id)
    await message.reply_text(
        "✅ Watermark testimoni berhasil diatur. Mulai sekarang foto yang diposting ke "
        "channel testimoni akan otomatis ditempeli watermark ini.",
        reply_markup=kb.testimoni_menu_keyboard(**_testimoni_menu_kwargs()),
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


# ---------- Kelola Admin Grup (kartu di Mini App) ----------

async def _resolve_admin_profile(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Coba ambil nama & foto profil user dari Telegram secara otomatis.
    Ini best-effort: hanya berhasil kalau bot pernah "kenal" user tsb
    (mis. user sudah pernah start bot ini), karena Bot API tidak bisa
    mengintip data user sembarangan demi privasi. Kalau gagal, kembalikan
    None -- admin tetap tersimpan pakai data yang diketik manual."""
    full_name = None
    photo_file_id = None
    try:
        chat = await context.bot.get_chat(user_id)
        full_name = " ".join(filter(None, [chat.first_name, chat.last_name])) or None
    except Exception:
        logger.info("Tidak bisa get_chat untuk user_id=%s (mungkin belum pernah start bot).", user_id)

    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos and photos.photos:
            # Ambil resolusi terbesar dari foto utama (foto pertama).
            photo_file_id = photos.photos[0][-1].file_id
    except Exception:
        logger.info("Tidak bisa ambil foto profil untuk user_id=%s.", user_id)

    return full_name, photo_file_id


async def add_group_admin_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima 1 pesan berformat '@username id_telegram Jabatan' lalu simpan
    sebagai kartu admin grup (foto & nama diambil otomatis kalau bisa)."""
    text = (update.message.text or "").strip()
    parts = text.split(None, 2)
    if len(parts) < 2:
        await update.message.reply_text(
            "⚠️ Format belum sesuai. Kirim: `@username id_telegram Jabatan`\n"
            "Contoh: `@johndoe 123456789 Admin Order`",
            parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard(),
        )
        return ADD_GROUP_ADMIN

    username_raw, id_raw = parts[0], parts[1]
    jabatan = parts[2].strip() if len(parts) > 2 else None
    username = username_raw.lstrip("@").strip() or None

    if not id_raw.isdigit():
        await update.message.reply_text(
            "⚠️ ID Telegram harus berupa angka. Kirim ulang: `@username id_telegram Jabatan`",
            parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard(),
        )
        return ADD_GROUP_ADMIN

    user_id = int(id_raw)
    full_name, photo_file_id = await _resolve_admin_profile(context, user_id)

    db.add_group_admin(
        user_id=user_id,
        username=username,
        full_name=full_name or username or str(user_id),
        jabatan=jabatan,
        photo_file_id=photo_file_id,
    )

    note = "" if photo_file_id else (
        "\n\n⚠️ Foto profil tidak berhasil diambil otomatis (user mungkin belum "
        "pernah start bot ini atau foto profilnya privat). Kartu tetap tersimpan "
        "tanpa foto, minta user itu /start bot ini lalu kirim ulang datanya kalau "
        "mau fotonya muncul."
    )
    await update.message.reply_text(
        f"✅ Admin *{full_name or username or user_id}* berhasil ditambahkan/diperbarui."
        f"{note}",
        parse_mode="Markdown",
        reply_markup=kb.group_admins_menu_keyboard(),
    )
    return ConversationHandler.END


async def edit_group_admin_value_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima nilai baru untuk SATU field kartu admin grup yang sedang diedit
    (disimpan di context.user_data['edit_group_admin']), simpan ke database,
    lalu tampilkan lagi menu field admin tsb supaya admin bisa lanjut edit
    field lain. Pola yang sama seperti edit_talent_value_receive."""
    info = context.user_data.get("edit_group_admin")
    message = update.message
    if not info:
        await message.reply_text("Sesi edit sudah tidak valid, silakan buka lagi dari /settings.")
        return ConversationHandler.END

    admin_id, field = info["id"], info["field"]

    if field == "photo_file_id":
        photo_file_id = None
        if message.photo:
            photo_file_id = message.photo[-1].file_id
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            photo_file_id = message.document.file_id
        if not photo_file_id:
            await message.reply_text("Itu bukan foto. Kirim *foto* baru untuk admin ini.", parse_mode="Markdown")
            return EDIT_GROUP_ADMIN_VALUE
        value = photo_file_id
    else:
        text = (message.text or "").strip()
        if not text:
            await message.reply_text("Kirim teks yang valid.")
            return EDIT_GROUP_ADMIN_VALUE
        if field == "username":
            value = None if text == "-" else text.lstrip("@").strip()
        elif field == "jabatan":
            value = None if text == "-" else text
        else:
            value = text

    db.update_group_admin_field(admin_id, field, value)
    context.user_data.pop("edit_group_admin", None)

    admin = db.get_group_admin(admin_id)
    await message.reply_text(
        "✅ Data admin berhasil diperbarui.",
        reply_markup=kb.edit_group_admin_field_keyboard(admin) if admin else kb.group_admins_menu_keyboard(),
    )
    return ConversationHandler.END


async def delete_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin menekan tombol hapus di daftar 'Kelola Admin Grup'."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Khusus admin.", show_alert=True)
        return
    await query.answer()

    admin_id = int(query.data.split("_")[1])
    db.delete_group_admin(admin_id)

    admins = db.list_group_admins()
    if not admins:
        await replace_message(query, context, "Belum ada admin yang ditambahkan.", reply_markup=kb.group_admins_menu_keyboard())
        return
    await replace_message(
        query, context,
        "Admin dihapus. Pilih admin lain yang ingin dihapus (tap = hapus):",
        reply_markup=kb.group_admins_list_keyboard(admins),
    )


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
        "Masukkan *deskripsi melayang* sponsor -- teks ini yang akan tampil vertikal di "
        "samping logo pada sponsor melayang (isi manual, boleh beda dari deskripsi di atas), "
        "atau ketik `-` untuk lewati:",
        parse_mode="Markdown",
        reply_markup=kb.addtalent_step_keyboard(),
    )
    return ADD_SPONSOR_MARQUEE_DESC


async def add_sponsor_marquee_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["new_sponsor"]["marquee_desc"] = None if text == "-" else update.message.text
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
        marquee_desc=ns.get("marquee_desc"),
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


async def edit_group_start_media_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simpan file_id animasi sapaan grup (stiker/animasi/video) yang dikirim
    admin, atau hapus kalau admin mengetik "hapus"."""
    message = update.message
    text = (message.text or "").strip().lower()
    if text in ("hapus", "hapus animasi", "hapus sapaan"):
        db.delete_setting("group_start_media_file_id")
        db.delete_setting("group_start_media_kind")
        await message.reply_text("✅ Animasi sapaan grup berhasil dihapus.", reply_markup=kb.settings_menu_keyboard())
        return ConversationHandler.END

    if message.sticker:
        db.set_setting("group_start_media_file_id", message.sticker.file_id)
        db.set_setting("group_start_media_kind", "sticker")
    elif message.animation:
        db.set_setting("group_start_media_file_id", message.animation.file_id)
        db.set_setting("group_start_media_kind", "animation")
    elif message.video:
        db.set_setting("group_start_media_file_id", message.video.file_id)
        db.set_setting("group_start_media_kind", "video")
    else:
        await message.reply_text(
            "⚠️ Kirim *stiker*, *GIF/animasi*, atau *video pendek*, ya -- "
            "atau ketik `hapus` untuk menghapus animasi yang sudah ada.",
            parse_mode="Markdown",
        )
        return EDIT_GROUP_START_MEDIA

    await message.reply_text(
        "✅ Animasi sapaan grup berhasil diperbarui. Coba tekan tombol 🚀 Mulai "
        "di grup untuk melihat hasilnya.",
        reply_markup=kb.settings_menu_keyboard(),
    )
    return ConversationHandler.END


async def edit_howtoorder_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_setting_with_emoji("how_to_order", update.message)
    await update.message.reply_text("✅ Teks Cara Order berhasil diubah.", reply_markup=kb.settings_menu_keyboard())
    return ConversationHandler.END


async def edit_promotext_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if "{daftar_talent}" not in text:
        await update.message.reply_text(
            "⚠️ Teksnya harus tetap mengandung placeholder `{daftar_talent}` supaya daftar talent "
            "yang ready bisa otomatis diisi di sana. Kirim ulang teksnya dengan placeholder itu "
            "disertakan, atau /cancel untuk batalkan.",
            parse_mode="Markdown",
        )
        return EDIT_PROMO_TEXT
    db.set_setting("promo_text", text)
    interval = db.get_setting("promo_interval_minutes")
    topics = _get_promo_topics()
    await update.message.reply_text(
        "✅ Teks isi Promo Talent berhasil diubah.",
        reply_markup=kb.autopromo_menu_keyboard(interval, len(topics), _MAX_PROMO_TOPICS),
    )
    return ConversationHandler.END


async def edit_promointerval_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima input teks angka menit / `off` dari langkah "⏰ Atur Jadwal" di
    submenu Auto Promo (/settings), lalu terapkan dengan logika yang PERSIS
    sama seperti /setpromojadwal (reschedule job "promo_autopost")."""
    text = (update.message.text or "").strip().lower()
    interval_for_menu = db.get_setting("promo_interval_minutes")
    topics = _get_promo_topics()

    if text == "off":
        db.delete_setting("promo_interval_minutes")
        _reschedule_promo_job(context.job_queue, None)
        await update.message.reply_text(
            "✅ Auto Promo terjadwal dimatikan. Posting manual lewat tombol \"🚀 Posting Sekarang\" tetap bisa dipakai kapan saja.",
            reply_markup=kb.autopromo_menu_keyboard(None, len(topics), _MAX_PROMO_TOPICS),
        )
        return ConversationHandler.END

    try:
        minutes = int(text)
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Format salah. Kirim angka menit (mis. `120`) atau ketik `off`.",
            parse_mode="Markdown",
            reply_markup=kb.back_to_settings_keyboard("settings_autopromo"),
        )
        return EDIT_PROMO_INTERVAL

    if not topics:
        await update.message.reply_text(
            "⚠️ Belum ada grup/topik tujuan promo yang terdaftar. Daftarkan dulu lewat "
            "`/setpromogrup` (dijalankan di dalam grup tujuannya) sebelum atur jadwal.",
            parse_mode="Markdown",
            reply_markup=kb.autopromo_menu_keyboard(interval_for_menu, len(topics), _MAX_PROMO_TOPICS),
        )
        return ConversationHandler.END

    if context.job_queue is None:
        await update.message.reply_text(
            "⚠️ JobQueue tidak aktif di server ini (`python-telegram-bot[job-queue]` belum "
            "terpasang), jadi jadwal berulang TIDAK akan jalan otomatis walau pengaturan ini "
            "tersimpan. Posting manual lewat tombol \"🚀 Posting Sekarang\" tetap selalu bisa dipakai.",
            reply_markup=kb.autopromo_menu_keyboard(str(minutes), len(topics), _MAX_PROMO_TOPICS),
        )
        db.set_setting("promo_interval_minutes", str(minutes))
        return ConversationHandler.END

    db.set_setting("promo_interval_minutes", str(minutes))
    _reschedule_promo_job(context.job_queue, minutes)
    await update.message.reply_text(
        f"✅ Auto Promo dijadwalkan tiap *{minutes} menit*. Posting otomatis pertama dalam "
        f"{minutes} menit dari sekarang (atau posting manual sekarang lewat tombol \"🚀 Posting Sekarang\").",
        parse_mode="Markdown",
        reply_markup=kb.autopromo_menu_keyboard(str(minutes), len(topics), _MAX_PROMO_TOPICS),
    )
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
    if not await _group_command_allowed(update, context):
        return
    context.user_data.clear()
    await update.message.reply_text("Dibatalkan.")
    return ConversationHandler.END


# ==================== MULTI-BGM (upload musik lewat bot) ====================
async def addbgm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai alur upload BGM baru (khusus admin): admin kirim file audio dulu,
    lalu bot minta judul lagunya."""
    if not await _group_command_allowed(update, context):
        return ConversationHandler.END
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "🎵 Kirim file *audio/musik*-nya sekarang (mp3, dll -- lewat menu lampiran > Musik/Audio "
        "di Telegram, JANGAN dikirim sebagai foto/video).\n\nKetik /cancel untuk batal.",
        parse_mode="Markdown",
    )
    return ADD_BGM_FILE


async def addbgm_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    audio = message.audio or (message.document if message.document and (message.document.mime_type or "").startswith("audio/") else None)

    if not audio:
        await message.reply_text(
            "Itu bukan file audio. Kirim file musik lewat menu lampiran > Musik/Audio di Telegram "
            "(bukan foto/video/voice note), atau /cancel untuk batal."
        )
        return ADD_BGM_FILE

    default_title = getattr(audio, "title", None) or getattr(audio, "file_name", None) or "BGM tanpa judul"
    context.user_data["new_bgm"] = {
        "file_id": audio.file_id,
        "mime_type": getattr(audio, "mime_type", None) or "audio/mpeg",
        "default_title": default_title,
    }
    await message.reply_text(
        f"Judul lagu ini apa? (ketik `-` untuk pakai judul bawaan: *{default_title}*)",
        parse_mode="Markdown",
    )
    return ADD_BGM_TITLE


async def addbgm_receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    nb = context.user_data.get("new_bgm")
    if not nb:
        await update.message.reply_text("Sesi upload BGM sudah tidak berlaku, coba /addbgm lagi.")
        return ConversationHandler.END

    title = nb["default_title"] if text == "-" else text
    track_id = db.add_bgm_track(file_id=nb["file_id"], title=title, mime_type=nb["mime_type"])
    context.user_data.pop("new_bgm", None)

    await update.message.reply_text(
        f"✅ BGM \"{title}\" berhasil ditambahkan (ID: {track_id}). "
        f"Sekarang otomatis muncul jadi pilihan lagu di Mini App.\n\n"
        f"Lihat semua BGM: /listbgm",
    )
    return ConversationHandler.END


async def listbgm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan semua BGM yang sudah diupload, tiap baris ada tombol hapus."""
    if not await _group_command_allowed(update, context):
        return
    if not is_admin(update.effective_user.id):
        return
    tracks = db.list_bgm_tracks()
    if not tracks:
        await update.message.reply_text(
            "Belum ada BGM yang diupload. Pakai /addbgm untuk menambahkan."
        )
        return
    await update.message.reply_text(
        f"🎵 Ada {len(tracks)} BGM terpasang:",
        reply_markup=kb.bgm_list_keyboard(tracks),
    )


async def delbgm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    track_id = int(query.data.split("_", 1)[1])
    db.delete_bgm_track(track_id)

    tracks = db.list_bgm_tracks()
    if not tracks:
        await query.edit_message_text("Semua BGM sudah dihapus. Pakai /addbgm untuk menambahkan lagi.")
        return
    await query.edit_message_text(
        f"🎵 Ada {len(tracks)} BGM terpasang:",
        reply_markup=kb.bgm_list_keyboard(tracks),
    )


async def groupid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _group_command_allowed(update, context):
        return
    await update.message.reply_text(f"Chat ID: `{update.effective_chat.id}`", parse_mode="Markdown")


async def postkatalog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Posting pesan (teks, atau foto+teks) berisi tombol Mini App ke channel
    (khusus admin). Pakai lewat private chat dengan bot:
      - Teks saja : /postkatalog <@username_channel atau chat_id> [teks pesan]
      - Dengan foto: REPLY ke sebuah pesan foto, lalu ketik
                     /postkatalog <@username_channel atau chat_id> [teks pesan]
        (Telegram tidak membaca command dari caption foto, makanya harus lewat reply.)
    Tombol pakai link t.me langsung (bukan field web_app=) karena web_app= hanya
    berfungsi di private chat, tidak tampil/berfungsi kalau dipasang di channel."""
    if not await _group_command_allowed(update, context):
        return
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Pakai: `/postkatalog <@username_channel atau chat_id> [teks pesan]`\n\n"
            "Contoh teks saja:\n"
            "`/postkatalog @channel_saya Cek katalog talent kami di sini 👇`\n\n"
            "Contoh dengan foto: reply ke pesan foto, lalu ketik command yang sama "
            "di atas -- fotonya akan ikut diposting bersama tombol Mini App.\n\n"
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

    # Foto (opsional): command HARUS di-reply-kan ke pesan foto, karena
    # Telegram/PTB hanya mendeteksi command dari message.text, bukan dari
    # caption foto -- jadi caption-foto-berisi-command tidak akan pernah
    # sampai ke handler ini.
    photo_file_id = None
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        photo_file_id = update.message.reply_to_message.photo[-1].file_id

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

    # Caption foto Telegram dibatasi 1024 karakter (jauh lebih pendek dari
    # limit teks biasa 4096) -- kalau kepanjangan, mending gagal cepat dengan
    # pesan yang jelas daripada dilempar exception mentah dari Telegram.
    if photo_file_id and len(text) > 1024:
        await update.message.reply_text(
            "⚠️ Teks pesan kepanjangan untuk dijadikan caption foto (maks 1024 karakter, "
            f"punya Anda {len(text)} karakter). Persingkat teksnya, atau kirim tanpa foto.",
            parse_mode="Markdown",
        )
        return

    bot_username = (await context.bot.get_me()).username
    reply_markup = kb.webapp_channel_keyboard(
        bot_username, config.WEBAPP_SHORT_NAME,
        icon_custom_emoji_id=config.CHANNEL_BUTTON_ICON_EMOJI_ID,
    )
    try:
        if photo_file_id:
            await context.bot.send_photo(
                chat_id=target,
                photo=photo_file_id,
                caption=text,
                caption_entities=entities,
                reply_markup=reply_markup,
            )
        else:
            await context.bot.send_message(
                chat_id=target,
                text=text,
                entities=entities,
                reply_markup=reply_markup,
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


async def settestimoni_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Atur channel tujuan autopost testimoni (khusus admin), dipakai lewat
    private chat dengan bot -- alternatif command-line dari /settings >
    "📸 Channel Testimoni" (yang juga punya toggle mode Autoapprove):
      /settestimoni <@username_channel atau chat_id>  -- atur/ganti channel
      /settestimoni hapus                             -- lepas pengaturan
      /settestimoni                                    -- lihat status saat ini
    Mode approve (manual/Autoapprove) diatur lewat tombol di /settings, bukan
    dari command ini."""
    if not await _group_command_allowed(update, context):
        return
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(_testimoni_settings_text(), parse_mode="Markdown")
        return

    if context.args[0].lower() == "hapus":
        db.delete_setting("testimoni_channel_id")
        db.delete_setting("testimoni_autoapprove")
        db.delete_setting("testimoni_watermark_file_id")
        db.delete_setting("testimoni_censor_enabled")
        await update.message.reply_text("✅ Channel testimoni sudah dilepas (beserta watermark & pengaturan sensor). Tombol approve tidak akan muncul lagi.")
        return

    target_raw = context.args[0]
    target = int(target_raw) if target_raw.lstrip("-").isdigit() else target_raw

    try:
        chat = await context.bot.get_chat(target)
        # Kirim & langsung hapus pesan uji untuk memastikan bot benar-benar
        # bisa kirim pesan ke channel tsb (get_chat saja tidak menjamin itu).
        test_msg = await context.bot.send_message(chat_id=target, text="✅ Channel testimoni berhasil terhubung.")
        await test_msg.delete()
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Gagal terhubung ke `{target_raw}`.\n"
            "Pastikan bot sudah jadi admin channel tsb (dengan izin kirim pesan) dan chat_id/username-nya benar.\n\n"
            f"Detail error: {e}",
            parse_mode="Markdown",
        )
        return

    db.set_setting("testimoni_channel_id", target_raw)
    await update.message.reply_text(
        f"✅ Channel testimoni diatur ke *{chat.title or target_raw}*.\n\n"
        "Mulai sekarang, tombol \"✅ Approve ke Channel Testimoni\" akan muncul di grup live chat "
        "setiap kali user mengirim foto (mis. bukti transfer) selama sesi live chat aktif.",
        parse_mode="Markdown",
    )


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
    for admin_id in get_all_admin_ids():
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
    if not await _group_command_allowed(update, context):
        return
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⏳ Membuat backup database...")
    await send_database_backup(context.bot)


async def restoredb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pulihkan database dari file .db yang dikirim admin (biasanya hasil
    /exportdb dari deployment lama), dengan cara REPLY ke file itu memakai
    perintah /restoredb. HANYA untuk dipakai sekali saat migrasi ke Volume baru
    yang masih kosong -- setelah dipakai, HAPUS command ini dari kode dan
    deploy ulang, karena siapa pun yang lolos is_admin() bisa menimpa seluruh
    database produksi lewat command ini."""
    if not await _group_command_allowed(update, context):
        return
    if not is_admin(update.effective_user.id):
        return

    reply = update.message.reply_to_message
    document = reply.document if reply else None
    if not document:
        await update.message.reply_text(
            "⚠️ Reply ke file .db yang mau dipulihkan, baru kirim /restoredb.\n"
            "Contoh: kirim file bot_backup_xxxx.db ke chat ini, lalu reply file itu dengan /restoredb."
        )
        return
    if not document.file_name or not document.file_name.lower().endswith(".db"):
        await update.message.reply_text("⚠️ File yang di-reply harus berekstensi .db.")
        return

    await update.message.reply_text(
        f"⏳ Menimpa `{config.DB_PATH}` dengan `{document.file_name}`...",
        parse_mode="Markdown",
    )
    try:
        tg_file = await context.bot.get_file(document.file_id)
        # Tulis ke file sementara dulu, baru rename -- supaya kalau proses
        # download gagal di tengah jalan, database lama yang masih aktif
        # tidak ikut rusak/setengah tertulis.
        tmp_path = config.DB_PATH + ".restoring"
        await tg_file.download_to_drive(tmp_path)
        os.replace(tmp_path, config.DB_PATH)
    except Exception as e:
        logger.error("Gagal restore database dari %s: %s", document.file_name, e)
        await update.message.reply_text(
            f"❌ Gagal restore: {e}\n\n"
            "Catatan: Bot Telegram API tidak bisa download file di atas 20MB. "
            "Kalau file backup-nya sudah lebih besar dari itu, restore harus lewat "
            "Railway CLI, bukan lewat command ini."
        )
        return

    await update.message.reply_text(
        "✅ Database berhasil dipulihkan. RESTART service sekarang juga supaya "
        "bot memakai koneksi baru ke file ini (data lama di memori/koneksi lama "
        "masih yang sebelumnya)."
    )


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
        BotCommand("statustalent", "Lihat status ready/tidak semua talent"),
    ]
    try:
        await application.bot.set_my_commands(public_commands)
    except Exception:
        logger.warning("Gagal mengatur daftar perintah publik.")

    admin_commands = _admin_commands_list(public_commands)
    for admin_id in get_all_admin_ids():
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
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(on_startup)
        # Default python-telegram-bot memproses update SATU-SATU (sequential).
        # Kalau ada 10+ pengguna live chat bersamaan, user ke-2 dst harus
        # menunggu proses user pertama selesai total (termasuk jeda 0.6 detik
        # indikator "mengetik") sebelum update mereka mulai diproses -- inilah
        # salah satu penyebab bot terasa tidak merespon saat dipakai banyak
        # orang sekaligus. `concurrent_updates` mengizinkan banyak update
        # diproses paralel (di sini dibatasi 32 sekaligus, jauh lebih dari
        # cukup untuk 10 pengguna bersamaan, sambil tetap membatasi resource).
        .concurrent_updates(32)
        .build()
    )
    app.add_error_handler(global_error_handler)
    _install_protect_content_wrapper(app.bot)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("groupid", groupid_command))
    app.add_handler(CommandHandler("postkatalog", postkatalog_command))
    app.add_handler(CommandHandler("settestimoni", settestimoni_command))
    app.add_handler(CommandHandler("posttestimoni", posttestimoni_command))
    app.add_handler(CommandHandler("setabsentopik", setabsentopik_command))
    app.add_handler(CommandHandler("linktalent", linktalent_command))
    app.add_handler(CommandHandler("setpromogrup", setpromogrup_command))
    app.add_handler(CommandHandler("setpromojadwal", setpromojadwal_command))
    app.add_handler(CommandHandler("postpromo", postpromo_command))
    app.add_handler(CommandHandler("hapuspromo", hapuspromo_command))
    app.add_handler(CommandHandler("statustalent", statustalent_command))
    app.add_handler(CommandHandler("exportdb", exportdb_command))
    app.add_handler(CommandHandler("restoredb", restoredb_command))
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

    # ---- Live chat: admin mengakhiri / mereset sesi ----
    app.add_handler(CallbackQueryHandler(end_chat_callback, pattern="^endchat_"))
    app.add_handler(CallbackQueryHandler(reset_chat_callback, pattern="^resetchat_"))
    app.add_handler(CallbackQueryHandler(testimoni_approve_callback, pattern="^testiapprove_"))
    app.add_handler(CommandHandler("resetlc", resetlc_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("listadmin", listadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))

    settings_conv = ConversationHandler(
        entry_points=[
            CommandHandler("settings", settings_command),
            CallbackQueryHandler(
                settings_callback,
                pattern="^settings_|^delconfirm_|^sponsor|^edittalent|^editsponsor|^editadmin|^autopromo_",
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
            EDIT_PROMO_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_promotext_receive)],
            EDIT_PROMO_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_promointerval_receive)],
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
            EDIT_TESTIMONI_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_testimoni_channel_receive)],
            EDIT_TESTIMONI_WATERMARK: [MessageHandler((filters.Sticker.ALL | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, edit_testimoni_watermark_receive)],
            EDIT_GROUP_ADMIN_VALUE: [MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, edit_group_admin_value_receive)],
            ADD_SPONSOR_PHOTO: [
                MessageHandler(
                    (filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND,
                    add_sponsor_photo,
                ),
            ],
            ADD_SPONSOR_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sponsor_name)],
            ADD_SPONSOR_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sponsor_desc)],
            ADD_SPONSOR_MARQUEE_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sponsor_marquee_desc)],
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
            ADD_GROUP_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_group_admin_receive)],
            EDIT_GROUP_START_MEDIA: [
                MessageHandler(
                    (filters.Sticker.ALL | filters.ANIMATION | filters.VIDEO | filters.TEXT) & ~filters.COMMAND,
                    edit_group_start_media_receive,
                ),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(addtalent_cancel, pattern="^addtalent_cancel$"),
            CallbackQueryHandler(
                settings_callback,
                pattern="^settings_|^delconfirm_|^sponsor|^edittalent|^editsponsor|^editadmin|^autopromo_",
            ),
            CommandHandler("settings", settings_command),
            CommandHandler("cancel", cancel_conversation),
        ],
        per_message=False,
        allow_reentry=True,
    )
    app.add_handler(settings_conv)

    # ---- Multi-BGM: upload musik lewat bot (terpisah dari settings_conv
    # supaya tidak perlu mengubah menu settings yang sudah ada) ----
    bgm_conv = ConversationHandler(
        entry_points=[CommandHandler("addbgm", addbgm_command)],
        states={
            ADD_BGM_FILE: [MessageHandler((filters.AUDIO | filters.Document.AUDIO) & ~filters.COMMAND, addbgm_receive_file)],
            ADD_BGM_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addbgm_receive_title)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_message=False,
        allow_reentry=True,
    )
    app.add_handler(bgm_conv)
    app.add_handler(CommandHandler("listbgm", listbgm_command))
    app.add_handler(CallbackQueryHandler(delbgm_callback, pattern="^delbgm_"))
    app.add_handler(CallbackQueryHandler(delete_admin_callback, pattern="^deladmin_"))

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

    # ---- Smart reply saat bot di-mention/di-reply di dalam grup ----
    # Didaftarkan PALING TERAKHIR (setelah relay admin & live chat di atas)
    # supaya tidak "merebut" balasan admin di grup live chat -- handler ini
    # sendiri juga sudah aman karena hanya bereaksi pada mention/reply-ke-bot,
    # bukan sembarang pesan grup ataupun kata kunci.
    app.add_handler(CallbackQueryHandler(group_start_callback, pattern="^groupstart_"))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        group_smart_reply_handler,
    ))

    # Handler absen talent -- didaftarkan di GROUP DISPATCH TERPISAH (group=1)
    # dari group_smart_reply_handler di atas (group=0 default), supaya PTB
    # tetap memanggil KEDUANYA untuk pesan grup yang sama (per-group dispatch
    # PTB cuma stop di handler pertama yang cocok DALAM SATU grup, bukan
    # lintas grup). absen_message_handler sendiri sudah aman/no-op kecuali
    # persis di grup+topik yang diatur lewat /setabsentopik.
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        absen_message_handler,
    ), group=1)

    # ---- Cache ringan "user pernah kelihatan" (lihat _track_seen_user &
    # _resolve_telegram_username) -- didaftarkan di GROUP DISPATCH TERSENDIRI
    # (group=2) SUPAYA SELALU JALAN untuk SETIAP update yang punya
    # effective_user (pesan private, pesan grup APAPUN termasuk yang bukan
    # untuk bot ini, maupun tombol yang ditekan), tanpa pernah "merebut"
    # update dari handler manapun di group=0/1 di atas. Ini yang bikin
    # `/linktalent @username Nama Talent` (mode langsung, tanpa reply) bisa
    # berfungsi untuk siapa pun yang PERNAH /start bot ini atau kelihatan di
    # grup mana pun tempat bot ada -- bukan cuma yang baru saja getChat-able.
    app.add_handler(MessageHandler(filters.ALL, _track_seen_user), group=2)
    app.add_handler(CallbackQueryHandler(_track_seen_user, pattern=".*"), group=2)

    if app.job_queue:
        app.job_queue.run_repeating(_check_talent_status_expiry, interval=60, first=30)
        app.job_queue.run_daily(_daily_absen_reset, time=dtime(hour=6, minute=0, tzinfo=WIB_TZ))
        saved_promo_interval = db.get_setting("promo_interval_minutes")
        if saved_promo_interval:
            try:
                _reschedule_promo_job(app.job_queue, int(saved_promo_interval))
            except ValueError:
                logger.warning("Setting promo_interval_minutes rusak (%r), auto-promo tidak dijadwalkan ulang.", saved_promo_interval)
    else:
        logger.warning(
            "JobQueue tidak aktif (kemungkinan 'python-telegram-bot[job-queue]' belum terpasang) "
            "-- status talent TETAP akurat saat dibaca user (lazy expiry), tapi notifikasi "
            "auto-off proaktif, reset+reminder harian jam 06:00 WIB di topik Absen Talent, DAN "
            "jadwal berulang Auto Promo Talent Ready tidak akan jalan (posting manual lewat "
            "/postpromo tetap bisa). Install dengan: `pip install \"python-telegram-bot[job-queue]\"` "
            "kalau mau mengaktifkan semuanya."
        )


    logger.info("Bot berjalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
