"""
api_server.py
==============
Server HTTP kecil (aiohttp) yang berjalan BERBARENGAN dengan bot Telegram, di
event loop yang sama (lewat post_init Application di bot.py) -- bukan proses
terpisah, jadi tidak butuh container/service kedua di Railway.

Kenapa perlu ini? Mini App "Pilih Talent" (katalog visual, index.html) adalah
HALAMAN WEB biasa yang dibuka di WebView Telegram -- ia butuh sumber data
lewat HTTP fetch() biasa, bukan lewat Bot API. Jadi bot ini sekalian jadi
backend kecil untuk Mini App-nya.

Endpoint:
- GET  /api/talents        -> daftar talent (publik, read-only): id, name,
  description, photo_url (link ke /photo/<id>, BUKAN link file Telegram
  langsung -- lihat handle_photo() kenapa).
- GET  /photo/<talent_id>  -> stream gambar talent. Kita PROXY di server
  sendiri (bukan kasih link https://api.telegram.org/file/bot<TOKEN>/...
  langsung ke browser) supaya BOT_TOKEN tidak pernah bocor ke network tab
  browser/klien Mini App.
- POST /api/select-talent  -> dipakai KHUSUS kalau Mini App dibuka lewat
  Menu Button (bukan reply keyboard) -- lihat catatan panjang di
  keyboards.py::webapp_launch_keyboard() & index.html::selectTalent().
  Jalur ini tidak bisa pakai Telegram.WebApp.sendData(), jadi index.html
  kirim pilihan talent ke endpoint ini, lalu KITA yang memanggil Bot API
  answerWebAppQuery ke Telegram supaya muncul tombol "Lihat {talent}"
  (callback_data=f"talent_{id}") -- pola yang SUDAH ditangani
  talent_detail_callback() di bot.py, jadi tidak perlu kode baru.
- GET  /health -> healthcheck Railway (opsional).
"""

import logging

import aiohttp
from aiohttp import web

import config
import database as db

logger = logging.getLogger(__name__)

# Diisi start_api_server() -- dipakai handle_photo() untuk resolve file_id
# jadi URL file Telegram (butuh akses Bot API, jadi butuh instance bot).
_bot = None


def _with_cors(resp: web.Response) -> web.Response:
    """Izinkan diakses dari domain manapun (mis. GitHub Pages) -- aman karena
    endpoint ini read-only & isinya memang sudah publik (katalog talent)."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def handle_talents(request: web.Request) -> web.Response:
    talents = db.list_talents()
    base = str(request.url.origin())
    data = [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "photo_url": f"{base}/photo/{t['id']}" if t["photo_file_id"] else None,
        }
        for t in talents
    ]
    return _with_cors(web.json_response({"talents": data}))


async def handle_photo(request: web.Request) -> web.Response:
    """Proxy foto talent dari Telegram -> browser, tanpa pernah menyingkap
    BOT_TOKEN ke klien (lihat docstring modul)."""
    try:
        talent_id = int(request.match_info["talent_id"])
    except (KeyError, ValueError):
        return web.Response(status=400, text="ID tidak valid")

    talent = db.get_talent(talent_id)
    if not talent or not talent["photo_file_id"] or _bot is None:
        return web.Response(status=404, text="Foto tidak ditemukan")

    try:
        tg_file = await _bot.get_file(talent["photo_file_id"])
        file_url = f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{tg_file.file_path}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    return web.Response(status=502, text="Gagal mengambil foto dari Telegram")
                body = await resp.read()
                content_type = resp.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        logger.error(f"Gagal proxy foto talent {talent_id}: {e}")
        return web.Response(status=502, text="Gagal mengambil foto")

    resp = web.Response(body=body, content_type=content_type)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return _with_cors(resp)


async def handle_select_talent(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        query_id = str(body.get("query_id") or "").strip()
        talent_id = int(body.get("talent_id"))
    except Exception:
        return _with_cors(web.json_response({"ok": False, "error": "Payload tidak valid"}, status=400))

    if not query_id:
        return _with_cors(web.json_response(
            {"ok": False, "error": "query_id kosong (Mini App tidak dibuka lewat menu/inline)"}, status=400
        ))

    talent = db.get_talent(talent_id)
    if not talent:
        return _with_cors(web.json_response({"ok": False, "error": "Talent tidak ditemukan"}, status=404))

    result = {
        "type": "article",
        "id": f"talent{talent_id}"[:64],
        "title": f"Talent {talent['name']}",
        "description": "Tekan tombol di bawah untuk lihat detail & pricelist.",
        "input_message_content": {
            "message_text": f"👤 Kamu memilih talent <b>{talent['name']}</b>.\n\nTekan tombol di bawah untuk lihat detailnya.",
            "parse_mode": "HTML",
        },
        "reply_markup": {
            "inline_keyboard": [[
                {"text": f"➡️ Lihat {talent['name']}", "callback_data": f"talent_{talent_id}"}
            ]]
        },
    }

    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/answerWebAppQuery"
    payload = {"web_app_query_id": query_id, "result": result}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
    except Exception as e:
        logger.error(f"answerWebAppQuery error: {e}")
        return _with_cors(web.json_response({"ok": False, "error": "Gagal menghubungi Telegram"}, status=502))

    if not data.get("ok"):
        logger.error(f"answerWebAppQuery ditolak Telegram: {data}")
        return _with_cors(web.json_response(
            {"ok": False, "error": data.get("description", "Telegram menolak permintaan")}, status=502
        ))

    return _with_cors(web.json_response({"ok": True}))


async def handle_options(request: web.Request) -> web.Response:
    return _with_cors(web.Response())


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/talents", handle_talents)
    app.router.add_route("OPTIONS", "/api/talents", handle_options)
    app.router.add_get("/photo/{talent_id}", handle_photo)
    app.router.add_post("/api/select-talent", handle_select_talent)
    app.router.add_route("OPTIONS", "/api/select-talent", handle_options)
    app.router.add_get("/health", handle_health)
    return app


async def start_api_server(bot, port: int) -> web.AppRunner:
    """Jalankan server sebagai background task di event loop yang sedang
    berjalan (dipanggil dari post_init Application PTB). `bot` dibutuhkan
    untuk resolve file_id foto talent lewat get_file()."""
    global _bot
    _bot = bot
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"API server Mini App aktif di port {port} (GET /api/talents)")
    return runner
