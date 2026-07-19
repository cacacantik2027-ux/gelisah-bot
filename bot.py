import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta
from html import escape as html_escape
from zoneinfo import ZoneInfo

from telegram import Update, BotCommand, BotCommandScopeChat, MessageEntity, InputMediaPhoto
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
) = range(25)

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


async def show_talent_detail(context: ContextTypes.DEFAULT_TYPE, chat_id, talent, owner_id=None):
    """Kirim halaman detail talent (foto+deskripsi+tombol) ke chat_id tertentu.
    Dipakai baik dari tombol chat biasa maupun dari data yang dikirim Mini App.
    `owner_id` diteruskan ke talent_detail_keyboard kalau halaman ini tampil
    di GRUP, supaya tombolnya tetap terkunci ke pemilik kartu."""
    caption = f"*{talent['name']}*\n\n{talent['description']}"
    if talent.get("photo_file_id"):
        photo = await _get_watermarked_talent_photo(context, talent)
        await send_thinking_photo(
            context, chat_id, photo, caption,
            parse_mode="Markdown", reply_markup=kb.talent_detail_keyboard(talent, owner_id=owner_id),
        )
    else:
        await send_thinking_reply(
            context, chat_id, caption,
            parse_mode="Markdown", reply_markup=kb.talent_detail_keyboard(talent, owner_id=owner_id),
        )


async def talent_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dipicu saat user memilih (menekan tombol nama) salah satu talent dari
    kartu carousel. Di GRUP, kartu carousel yang sedang tampil TIDAK
    dihapus/ditutup -- langsung di-EDIT di tempat jadi halaman detailnya
    (lihat _edit_card_in_place & catatan di show_talent_card di atas).

    Kartu ini bisa saja terkunci ke satu user (lihat _split_owner_suffix) --
    kalau iya dan yang menekan BUKAN pemiliknya, tolak lebih dulu sebelum
    memproses apa pun."""
    query = update.callback_query
    data, owner_id = _split_owner_suffix(query.data)
    if not await _enforce_card_owner(query, owner_id):
        return
    await query.answer()
    talent_id = int(data.split("_")[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await replace_message(query, context, "Talent tidak ditemukan.", reply_markup=kb.main_menu_keyboard())
        return

    chat = query.message.chat
    caption = f"*{talent['name']}*\n\n{talent['description']}"
    reply_markup = kb.talent_detail_keyboard(talent, owner_id=owner_id)
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
    await show_talent_detail(context, chat.id, talent, owner_id=owner_id)


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
    """Kartu ini bisa saja terkunci ke satu user (lihat _split_owner_suffix)
    -- kalau iya dan yang menekan BUKAN pemiliknya, tolak lebih dulu sebelum
    memproses apa pun."""
    query = update.callback_query
    data, owner_id = _split_owner_suffix(query.data)
    if not await _enforce_card_owner(query, owner_id):
        return
    await query.answer()
    talent_id = int(data.split("_")[1])
    talent = db.get_talent(talent_id)
    if not talent:
        await query.answer("Talent tidak ditemukan.", show_alert=True)
        return
    text = f"💰 *Pricelist - {talent['name']}*\n\n{talent['pricelist']}"
    chat_id = query.message.chat_id
    back_markup = kb.back_to_talent_keyboard(talent_id, owner_id=owner_id)
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

    for admin_id in get_all_admin_ids():
        try:
            copied = await context.bot.copy_message(
                chat_id=admin_id, from_chat_id=from_chat_id, message_id=message_id,
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
            "Pilih admin yang ingin dihapus dari daftar (tap = hapus):",
            reply_markup=kb.group_admins_list_keyboard(admins),
        )
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

    # ---- Live chat: admin mengakhiri / mereset sesi ----
    app.add_handler(CallbackQueryHandler(end_chat_callback, pattern="^endchat_"))
    app.add_handler(CallbackQueryHandler(reset_chat_callback, pattern="^resetchat_"))
    app.add_handler(CommandHandler("resetlc", resetlc_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("listadmin", listadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))

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
                pattern="^settings_|^delconfirm_|^sponsor|^edittalent|^editsponsor",
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


    logger.info("Bot berjalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
