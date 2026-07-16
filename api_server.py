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


def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


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
            "url": s.get("url"),
            "photo_url": f"/photo/sponsor/{s['id']}" if s.get("photo_file_id") else None,
        }
        for s in sponsors
    ]
    return _cors(web.json_response(data))


async def _proxy_telegram_photo(file_id):
    """Ambil isi foto dari Telegram lewat file_id lalu kirim balik ke browser,
    supaya BOT_TOKEN tidak pernah dikirim ke browser."""
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
            return _cors(web.Response(body=content, content_type="image/jpeg"))


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


@routes.get("/photo/sponsor/{sponsor_id:\\d+}")
async def get_sponsor_photo(request):
    """Proxy foto sponsor yang tampil di halaman utama Mini App."""
    sponsor_id = int(request.match_info["sponsor_id"])
    sponsor = db.get_sponsor(sponsor_id)
    if not sponsor or not sponsor.get("photo_file_id"):
        return web.Response(status=404, text="Foto sponsor tidak ditemukan")
    return await _proxy_telegram_photo(sponsor["photo_file_id"])


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
async def preflight(request):
    return _cors(web.Response())


def create_app():
    app = web.Application()
    app.add_routes(routes)

    # Sajikan folder webapp/ (index.html + bgm.mp3) sebagai file statis biasa,
    # supaya bgm.mp3 bisa di-GET langsung dari domain Railway ini juga.
    webapp_dir = pathlib.Path(__file__).parent / "webapp"
    app.router.add_static("/", webapp_dir, show_index=False, name="webapp_static")

    return app


async def run_api_server():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    logger.info(f"API server Mini App jalan di port {config.PORT}")
