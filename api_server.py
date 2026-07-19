"""
Server HTTP kecil untuk Mini App (index.html).
Hanya menyediakan data baca (GET) -- tidak ada endpoint yang menulis/mengubah data,
dan tidak ada endpoint live-chat atau pembayaran di sini.
"""
import logging
import pathlib

from aiohttp import web, ClientSession

import config
import database as db

logger = logging.getLogger(__name__)

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
    """Proxy foto talent dari Telegram, supaya BOT_TOKEN tidak pernah dikirim ke browser."""
    talent_id = int(request.match_info["talent_id"])
    talent = db.get_talent(talent_id)
    if not talent or not talent.get("photo_file_id"):
        return web.Response(status=404, text="Foto tidak ditemukan")
    return await _proxy_telegram_photo(talent["photo_file_id"])


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


@routes.get("/loading.mp4")
async def get_loading_video(request):
    """Sajikan loading.mp4 lokal -- video splash screen yang diputar di
    halaman loading Mini App sebelum menu utama tampil (lihat #loadingVideo
    di index.html). Tanpa route ini videonya 404 & yang kelihatan cuma
    background hitam polos di belakang teks 'Loading...'."""
    file_path = STATIC_DIR / "loading.mp4"
    if not file_path.exists():
        return web.Response(status=404, text="loading.mp4 tidak ditemukan")
    return web.FileResponse(file_path)


def create_app():
    app = web.Application()
    app.add_routes(routes)
    # SENGAJA tidak memasang add_static ke seluruh STATIC_DIR: folder itu juga
    # berisi source code (bot.py, database.py, api_server.py, dll) yang tidak
    # boleh bisa diunduh publik lewat browser. Hanya file yang benar-benar
    # perlu publik (index.html, bgm.mp3, loading.mp4) yang disajikan lewat
    # route eksplisit di atas.
    return app


async def run_api_server():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    logger.info(f"API server Mini App jalan di port {config.PORT}")
