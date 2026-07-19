"""Utilitas watermark: tempel logo sebagai watermark transparan berulang
(tiled, diagonal) di atas foto talent. Dipakai bersama oleh bot.py (kirim
foto ke DM/live chat Telegram) dan api_server.py (proxy foto ke Mini App),
supaya SATU logika watermark dipakai konsisten di kedua tempat -- foto
talent yang beredar lewat jalur mana pun (chat bot ATAU Mini App) selalu
sudah bertanda, jadi lebih sulit dipakai ulang pihak yang tidak berwenang
kalau sampai foto itu disimpan/di-screenshot.

Modul ini juga menyediakan generate_watermark_only() -- gambar TRANSPARAN
tanpa foto sama sekali, cuma pola watermark-nya -- dipakai sebagai lapisan
"umpan" di index.html: elemen <img> yang ditumpuk pas di atas foto (yang
tampil sebagai CSS background, bukan <img>), supaya kalau ada yang klik-kanan
> Simpan Gambar tepat kena elemen itu, yang tersimpan cuma watermark-nya,
BUKAN foto aslinya.

CATATAN JUJUR: watermark & umpan ini menandai kepemilikan foto & mempersulit
penyalahgunaan -- BUKAN mencegah foto disimpan sama sekali dengan cara apa
pun (mis. screenshot, DevTools, lihat langsung URL foto). Itu di luar
jangkauan software apa pun, lihat catatan proteksi konten di index.html/bot.py.
"""
import io
import logging
import pathlib

from PIL import Image

logger = logging.getLogger(__name__)

_LOGO_PATH = pathlib.Path(__file__).parent / "logo.png"
_logo_cache = None


def _load_logo():
    global _logo_cache
    if _logo_cache is None:
        _logo_cache = Image.open(_LOGO_PATH).convert("RGBA")
    return _logo_cache


def _build_watermark_layer(width: int, height: int, opacity: float) -> "Image.Image":
    """Bangun layer RGBA transparan berukuran width x height, berisi pola
    watermark logo berulang (diagonal, mirip watermark foto stok). Dipakai
    sebagai layer bersama oleh apply_watermark() (ditempel di atas foto asli)
    maupun generate_watermark_only() (dipakai berdiri sendiri, tanpa foto)."""
    logo = _load_logo()

    target_w = max(40, int(width * 0.16))
    ratio = target_w / logo.width
    logo_resized = logo.resize((target_w, max(1, int(logo.height * ratio))), Image.LANCZOS)

    # Turunkan opacity logo TANPA merusak transparansi asli logo (kalau
    # logo.png sendiri sudah punya area transparan di sekitar bentuknya).
    alpha = logo_resized.split()[3].point(lambda a: int(a * opacity))
    logo_resized.putalpha(alpha)
    logo_rot = logo_resized.rotate(30, expand=True)

    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    step_x = logo_rot.width + 60
    step_y = logo_rot.height + 60
    row = 0
    y = -step_y
    while y < height + step_y:
        offset = step_x // 2 if row % 2 else 0
        x = -step_x + offset
        while x < width + step_x:
            layer.paste(logo_rot, (x, y), logo_rot)
            x += step_x
        y += step_y
        row += 1
    return layer


def apply_watermark(image_bytes: bytes, opacity: float = 0.50) -> bytes:
    """Tempel logo.png sebagai watermark transparan berulang di atas foto,
    lalu kembalikan bytes JPEG hasil akhirnya. Kalau proses gagal karena
    alasan apa pun (mis. bytes bukan gambar valid), foto ASLI dikembalikan
    apa adanya -- supaya fitur watermark tidak pernah membuat foto gagal
    tampil sama sekali."""
    try:
        base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        overlay = _build_watermark_layer(base.width, base.height, opacity)
        watermarked = Image.alpha_composite(base, overlay).convert("RGB")
        out = io.BytesIO()
        watermarked.save(out, format="JPEG", quality=90)
        return out.getvalue()
    except Exception:
        logger.exception("Gagal menerapkan watermark, kirim/tampilkan foto asli tanpa watermark.")
        return image_bytes


def generate_watermark_only(width: int, height: int, opacity: float = 0.50) -> bytes:
    """Buat gambar PNG TRANSPARAN berukuran width x height, berisi HANYA pola
    watermark (tanpa foto apa pun di baliknya). Opacity default 0.50 supaya
    kalau ini yang tersimpan sendirian, jelas kelihatan sebagai watermark,
    bukan cuma gambar kosong/blur yang membingungkan. Kalau proses gagal,
    balikin PNG transparan kosong 1x1 sebagai fallback paling aman."""
    try:
        layer = _build_watermark_layer(width, height, opacity)
        out = io.BytesIO()
        layer.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        logger.exception("Gagal membuat gambar umpan watermark.")
        blank = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        out = io.BytesIO()
        blank.save(out, format="PNG")
        return out.getvalue()
