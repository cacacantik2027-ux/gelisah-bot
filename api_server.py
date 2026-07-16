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


@routes.get("/api/talents")
async def get_talents(request):
    talents = db.list_talents()
    data = [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "portfolio_url": t.get("portfolio_url"),
            "photo_url": f"/photo/{t['id']}" if t.get("photo_file_id") else None,
        }
        for t in talents
    ]
    return _cors(web.json_response(data))


@routes.get("/photo/{talent_id}")
async def get_photo(request):
    """Proxy foto talent dari Telegram, supaya BOT_TOKEN tidak pernah dikirim ke browser."""
    talent_id = int(request.match_info["talent_id"])
    talent = db.get_talent(talent_id)
    if not talent or not talent.get("photo_file_id"):
        return web.Response(status=404, text="Foto tidak ditemukan")

    bot_token = config.BOT_TOKEN
    async with ClientSession() as session:
        async with session.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": talent["photo_file_id"]},
        ) as resp:
            file_info = await resp.json()
            if not file_info.get("ok"):
                return web.Response(status=502, text="Gagal ambil info file")
            file_path = file_info["result"]["file_path"]

        file_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        async with session.get(file_url) as file_resp:
            content = await file_resp.read()
            return _cors(web.Response(body=content, content_type="image/jpeg"))


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
