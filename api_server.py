"""
Server HTTP kecil untuk Mini App (index.html).
Hanya menyediakan data baca (GET) -- tidak ada endpoint yang menulis/mengubah data,
dan tidak ada endpoint live-chat atau pembayaran di sini.
"""
import io
import logging
import pathlib

from aiohttp import web, ClientSession
from PIL import Image

import config
import database as db
import watermark

logger = logging.getLogger(__name__)

# Cache watermark foto talent DI MEMORI (bukan database) -- key: photo_file_id
# Telegram asli, value: bytes JPEG yang SUDAH ditempel watermark. Menghindari
# download+proses ulang tiap kali Mini App minta foto talent yang sama.
# Otomatis kosong lagi tiap proses restart -- itu wajar.
_talent_photo_watermark_cache = {}

# Cache gambar "umpan" (PNG transparan, HANYA watermark tanpa foto sama
# sekali) -- key: photo_file_id, value: bytes PNG. Dipakai index.html sebagai
# elemen <img> yang ditumpuk di atas foto asli (yang tampil sebagai CSS
# background), supaya kalau ada yang klik-kanan > Simpan Gambar tepat kena
# elemen itu, yang tersimpan cuma watermark-nya -- bukan foto aslinya.
_talent_photo_decoy_cache = {}

routes = web.RouteTableDef()

# Folder tempat index.html (dan bgm.mp3 kalau ada) berada -- SAMA DENGAN folder
# tempat api_server.py ini berada (bukan subfolder "webapp/"), supaya cocok
# dengan struktur file hasil deploy (bot.py, database.py, api_server.py,
# keyboards.py, index.html semuanya sejajar di folder yang sama).
STATIC_DIR = pathlib.Path(__file__).parent


def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@routes.get("/")
@routes.get("/index.html")
async def get_index(request):
    """Sajikan index.html langsung di root domain ATAU di /index.html, supaya
    URL Railway ini bisa dibuka langsung lewat browser biasa MAUPUN lewat
    tombol Mini App di Telegram, terlepas dari WEBAPP_URL disetel ke domain
    polos atau ke '.../index.html'. Tanpa route eksplisit ini, aiohttp static
    TIDAK otomatis menyajikan index.html -- cuma akan 403/404."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return web.Response(
            status=404,
            text="index.html tidak ditemukan di server. Pastikan file index.html "
                 "sudah ikut di-deploy sejajar dengan api_server.py.",
        )
    return web.FileResponse(index_path)


_bot_username_cache = None


async def _get_bot_username():
    """Ambil @username bot lewat Telegram Bot API (getMe), lalu simpan di memori
    (sekali saja per proses server) -- dipakai frontend buat bikin deep link
    `https://t.me/<username>?start=chat_<id>` waktu Mini App dibuka lewat
    browser biasa (BUKAN lewat Telegram), jadi tombol "Chat Sekarang" tetap
    bisa mengarahkan user ke DM bot walau tg.sendData() tidak berfungsi
    di luar konteks Telegram."""
    global _bot_username_cache
    if _bot_username_cache is not None:
        return _bot_username_cache
    try:
        async with ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/getMe"
            ) as resp:
                info = await resp.json()
                if info.get("ok"):
                    _bot_username_cache = info["result"]["username"]
    except Exception:
        logger.exception("Gagal ambil username bot lewat getMe")
    return _bot_username_cache


@routes.get("/api/home")
async def get_home(request):
    """Data untuk halaman utama Mini App: sapaan (sudah diisi placeholder),
    total talent saat ini, dan foto sapaan (kalau admin sudah memasangnya)."""
    total_talent = len(db.list_talents())
    template = db.get_setting("greeting", config.DEFAULT_GREETING)
    try:
        greeting_text = template.format(bot_name=config.BOT_NAME, total_talent=total_talent)
    except (KeyError, IndexError):
        # Kalau template custom admin punya placeholder yang tidak dikenali,
        # tampilkan apa adanya daripada bikin endpoint ini error.
        greeting_text = template

    data = {
        "bot_name": config.BOT_NAME,
        "bot_username": await _get_bot_username(),
        "greeting": greeting_text,
        "total_talent": total_talent,
        "greeting_photo_url": "/photo/greeting" if db.get_setting("greeting_photo") else None,
        "background_photo_url": "/photo/background" if db.get_setting("webapp_bg_photo") else None,
        "channel_photo_url": "/photo/channel" if db.get_setting("channel_photo") else None,
        "channel_description": db.get_setting("channel_description"),
        "channel_url": db.get_setting("channel_url"),
        "channel2_photo_url": "/photo/channel2" if db.get_setting("channel2_photo") else None,
        "channel2_description": db.get_setting("channel2_description"),
        "channel2_url": db.get_setting("channel2_url"),
        "floating_sponsor_enabled": db.get_setting("floating_sponsor_enabled", "1") == "1",
        "protect_content_enabled": db.get_setting("protect_content_enabled", "0") == "1",
        "developer_chat_url": config.DEVELOPER_CHAT_URL,
    }
    return _cors(web.json_response(data))


@routes.get("/api/talents")
async def get_talents(request):
    talents = db.list_talents()
    data = [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "pricelist": t.get("pricelist"),
            "portfolio_url": t.get("portfolio_url"),
            "photo_url": f"/photo/{t['id']}" if t.get("photo_file_id") else None,
        }
        for t in talents
    ]
    return _cors(web.json_response(data))


@routes.get("/api/sponsors")
async def get_sponsors(request):
    sponsors = db.list_sponsors()
    data = [
        {
            "id": s["id"],
            "name": s.get("name"),
            "description": s.get("description"),
            "marquee_desc": s.get("marquee_desc"),
            "url": s.get("url"),
            "photo_url": f"/photo/sponsor/{s['id']}" if s.get("photo_file_id") else None,
        }
        for s in sponsors
    ]
    return _cors(web.json_response(data))


@routes.get("/api/admins")
async def get_admins(request):
    """Daftar kartu admin grup untuk halaman 'Admin Grup' di Mini App."""
    admins = db.list_group_admins()
    data = [
        {
            "id": a["id"],
            "user_id": a["user_id"],
            "username": a.get("username"),
            "full_name": a.get("full_name"),
            "jabatan": a.get("jabatan"),
            "photo_url": f"/photo/admin/{a['id']}" if a.get("photo_file_id") else None,
        }
        for a in admins
    ]
    return _cors(web.json_response(data))


async def _proxy_telegram_file(file_id, default_content_type="application/octet-stream"):
    """Ambil isi file APA PUN dari Telegram lewat file_id lalu kirim balik ke
    browser, supaya BOT_TOKEN tidak pernah dikirim ke browser. Dipakai untuk
    foto maupun audio BGM."""
    bot_token = config.BOT_TOKEN
    async with ClientSession() as session:
        async with session.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": file_id},
        ) as resp:
            file_info = await resp.json()
            if not file_info.get("ok"):
                return web.Response(status=502, text="Gagal ambil info file")
            file_path = file_info["result"]["file_path"]

        file_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        async with session.get(file_url) as file_resp:
            content = await file_resp.read()
            return _cors(web.Response(body=content, content_type=default_content_type))


async def _proxy_telegram_photo(file_id):
    """Proxy khusus foto (dipertahankan terpisah untuk kompatibilitas kode lama)."""
    return await _proxy_telegram_file(file_id, default_content_type="image/jpeg")


async def _proxy_talent_photo_watermarked(file_id):
    """Proxy KHUSUS foto talent: sama seperti _proxy_telegram_photo, tapi
    hasilnya ditempel watermark logo dulu sebelum dikirim ke Mini App --
    supaya foto talent yang tampil/di-download lewat Mini App SELALU sudah
    bertanda, konsisten dengan foto yang dikirim lewat chat bot Telegram.
    Hasil watermark di-cache di memori per file_id supaya tidak perlu
    download+proses ulang di setiap request."""
    cached = _talent_photo_watermark_cache.get(file_id)
    if cached is not None:
        return _cors(web.Response(body=cached, content_type="image/jpeg"))
    bot_token = config.BOT_TOKEN
    async with ClientSession() as session:
        async with session.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": file_id},
        ) as resp:
            file_info = await resp.json()
            if not file_info.get("ok"):
                return web.Response(status=502, text="Gagal ambil info file")
            file_path = file_info["result"]["file_path"]

        file_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        async with session.get(file_url) as file_resp:
            raw = await file_resp.read()

    watermarked = watermark.apply_watermark(raw)
    _talent_photo_watermark_cache[file_id] = watermarked
    return _cors(web.Response(body=watermarked, content_type="image/jpeg"))


async def _proxy_talent_photo_decoy(file_id):
    """Kirim gambar 'umpan': PNG transparan berisi HANYA watermark, tanpa
    foto talent sama sekali -- dipakai index.html sebagai lapisan <img> yang
    ditumpuk pas di atas foto asli (yang tampil sebagai CSS background,
    bukan <img>, jadi tidak muncul di menu 'Simpan Gambar Sebagai...').
    Kalau ada yang klik-kanan tepat kena lapisan umpan ini, yang tersimpan
    cuma watermark-nya. Ukurannya disamakan dengan foto watermark asli
    (diambil dari cache-nya, memicu proses watermark dulu kalau belum ada)
    supaya pas menutupi foto tanpa distorsi."""
    cached = _talent_photo_decoy_cache.get(file_id)
    if cached is not None:
        return _cors(web.Response(body=cached, content_type="image/png"))

    watermarked = _talent_photo_watermark_cache.get(file_id)
    if watermarked is None:
        # Foto watermark-nya belum pernah diproses -- proses dulu (ini juga
        # otomatis mengisi _talent_photo_watermark_cache di atas).
        resp = await _proxy_talent_photo_watermarked(file_id)
        if resp.status != 200:
            return resp
        watermarked = _talent_photo_watermark_cache.get(file_id)
        if watermarked is None:
            return web.Response(status=502, text="Gagal menyiapkan gambar umpan")

    with Image.open(io.BytesIO(watermarked)) as im:
        width, height = im.size
    decoy = watermark.generate_watermark_only(width, height)
    _talent_photo_decoy_cache[file_id] = decoy
    return _cors(web.Response(body=decoy, content_type="image/png"))


@routes.get("/api/bgm")
async def get_bgm_list(request):
    """Daftar semua musik BGM yang sudah diupload admin, buat ditampilkan
    sebagai pilihan lagu di Mini App (user pilih sendiri mau dengar yang mana)."""
    tracks = db.list_bgm_tracks()
    data = [{"id": t["id"], "title": t["title"], "url": f"/bgm/{t['id']}"} for t in tracks]
    return _cors(web.json_response(data))


@routes.get("/bgm/{track_id:\\d+}")
async def get_bgm_file(request):
    """Proxy/stream file audio BGM tertentu dari Telegram."""
    track_id = int(request.match_info["track_id"])
    track = db.get_bgm_track(track_id)
    if not track:
        return web.Response(status=404, text="BGM tidak ditemukan")
    return await _proxy_telegram_file(track["file_id"], default_content_type=track.get("mime_type") or "audio/mpeg")


@routes.get("/photo/greeting")
async def get_greeting_photo(request):
    """Proxy foto sapaan (halaman utama Mini App)."""
    photo_file_id = db.get_setting("greeting_photo")
    if not photo_file_id:
        return web.Response(status=404, text="Foto sapaan tidak ditemukan")
    return await _proxy_telegram_photo(photo_file_id)


@routes.get("/photo/background")
async def get_background_photo(request):
    """Proxy foto background Mini App."""
    photo_file_id = db.get_setting("webapp_bg_photo")
    if not photo_file_id:
        return web.Response(status=404, text="Background tidak ditemukan")
    return await _proxy_telegram_photo(photo_file_id)


@routes.get("/photo/channel")
async def get_channel_photo(request):
    """Proxy foto channel yang tampil di halaman utama Mini App."""
    photo_file_id = db.get_setting("channel_photo")
    if not photo_file_id:
        return web.Response(status=404, text="Foto channel tidak ditemukan")
    return await _proxy_telegram_photo(photo_file_id)


@routes.get("/photo/channel2")
async def get_channel2_photo(request):
    """Proxy foto channel/grup kedua yang tampil di halaman utama Mini App."""
    photo_file_id = db.get_setting("channel2_photo")
    if not photo_file_id:
        return web.Response(status=404, text="Foto channel 2 tidak ditemukan")
    return await _proxy_telegram_photo(photo_file_id)


@routes.get("/photo/sponsor/{sponsor_id:\\d+}")
async def get_sponsor_photo(request):
    """Proxy foto sponsor yang tampil di halaman utama Mini App."""
    sponsor_id = int(request.match_info["sponsor_id"])
    sponsor = db.get_sponsor(sponsor_id)
    if not sponsor or not sponsor.get("photo_file_id"):
        return web.Response(status=404, text="Foto sponsor tidak ditemukan")
    return await _proxy_telegram_photo(sponsor["photo_file_id"])


@routes.get("/photo/admin/{admin_id:\\d+}")
async def get_admin_photo(request):
    """Proxy foto profil admin grup (halaman 'Admin Grup' di Mini App)."""
    admin_id = int(request.match_info["admin_id"])
    admin = db.get_group_admin(admin_id)
    if not admin or not admin.get("photo_file_id"):
        return web.Response(status=404, text="Foto admin tidak ditemukan")
    return await _proxy_telegram_photo(admin["photo_file_id"])


@routes.get("/photo/{talent_id:\\d+}")
async def get_photo(request):
    """Proxy foto talent dari Telegram (SUDAH ditempel watermark logo), supaya
    BOT_TOKEN tidak pernah dikirim ke browser."""
    talent_id = int(request.match_info["talent_id"])
    talent = db.get_talent(talent_id)
    if not talent or not talent.get("photo_file_id"):
        return web.Response(status=404, text="Foto tidak ditemukan")
    return await _proxy_talent_photo_watermarked(talent["photo_file_id"])


@routes.get("/photo/{talent_id:\\d+}/decoy")
async def get_photo_decoy(request):
    """Gambar umpan (PNG transparan, cuma watermark) untuk foto talent
    tertentu -- lihat _proxy_talent_photo_decoy()."""
    talent_id = int(request.match_info["talent_id"])
    talent = db.get_talent(talent_id)
    if not talent or not talent.get("photo_file_id"):
        return web.Response(status=404, text="Foto tidak ditemukan")
    return await _proxy_talent_photo_decoy(talent["photo_file_id"])


@routes.options("/api/{tail:.*}")
@routes.options("/photo/{tail:.*}")
@routes.options("/bgm/{tail:.*}")
async def preflight(request):
    return _cors(web.Response())


@routes.get("/bgm.mp3")
async def get_local_bgm(request):
    """Sajikan bgm.mp3 lokal (kalau ada) -- direferensikan langsung sebagai
    fallback audio di index.html. File audio BGM utama tetap dilayani lewat
    /bgm/{track_id} (proxy dari Telegram, lihat get_bgm_file di atas)."""
    file_path = STATIC_DIR / "bgm.mp3"
    if not file_path.exists():
        return web.Response(status=404, text="bgm.mp3 tidak ditemukan")
    return web.FileResponse(file_path)


@routes.get("/logo.png")
async def get_logo(request):
    """Sajikan logo.png lokal -- ditampilkan di tengah layar loading screen
    (lihat #pageLoading di index.html). Menggantikan loading.mp4 yang
    sebelumnya dipakai sebagai video splash screen -- fitur video sudah
    dihapus, sekarang loading screen cukup logo statis + teks."""
    file_path = STATIC_DIR / "logo.png"
    if not file_path.exists():
        return web.Response(status=404, text="logo.png tidak ditemukan")
    return web.FileResponse(file_path)


def create_app():
    app = web.Application()
    app.add_routes(routes)
    # SENGAJA tidak memasang add_static ke seluruh STATIC_DIR: folder itu juga
    # berisi source code (bot.py, database.py, api_server.py, dll) yang tidak
    # boleh bisa diunduh publik lewat browser. Hanya file yang benar-benar
    # perlu publik (index.html, bgm.mp3, logo.png) yang disajikan lewat
    # route eksplisit di atas.
    return app


async def run_api_server():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    logger.info(f"API server Mini App jalan di port {config.PORT}")
