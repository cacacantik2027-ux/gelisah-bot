# Talent Booking Bot

Bot Telegram untuk katalog talent/influencer/model dan pengajuan booking untuk
keperluan konten/endorsement/event.

## Fitur

1. **Katalog Talent** — foto, deskripsi, dan link portofolio per talent.
2. **Ajukan Booking** — user isi form singkat (kebutuhan, tanggal, budget).
   Bot mengirim **satu notifikasi** ke admin (lewat pesan bot/grup). Tidak ada
   kanal chat dua arah otomatis — admin menindaklanjuti secara manual di luar bot
   (WhatsApp, telepon, dsb).
3. **/settings** — kelola konten bot: tambah/hapus talent, ubah teks sapaan,
   ubah teks "Cara Order", dan lihat daftar booking yang masuk.

## Setup

1. Install dependency:
   ```
   pip install -r requirements.txt
   ```
2. Salin `.env.example` menjadi `.env` dan isi:
   - `BOT_TOKEN` — token dari @BotFather
   - `ADMIN_IDS` — ID Telegram kamu (cek lewat @userinfobot)
   - `BOOKING_NOTIFY_CHAT_ID` — opsional, ID grup/chat tujuan notifikasi booking.
     Kalau dikosongkan, notifikasi dikirim ke masing-masing `ADMIN_IDS`.
3. Jalankan:
   ```
   python bot.py
   ```

## Command

| Command      | Keterangan                                        |
|--------------|----------------------------------------------------|
| `/start`     | Tampilkan menu utama (Pilih Talent, Cara Order)     |
| `/settings`  | Menu kelola konten (khusus admin)                   |
| `/groupid`   | Tampilkan chat ID (untuk isi `BOOKING_NOTIFY_CHAT_ID`) |
| `/cancel`    | Batalkan proses yang sedang berjalan (isi form/tambah talent) |

## Mini App (opsional)

Folder `webapp/` berisi katalog talent versi tampilan-app (`index.html` + musik
background `bgm.mp3`, volume 50%). Ini murni tampilan (browse-only): begitu
user tap talent tertentu, data dikirim balik ke bot (`Telegram.WebApp.sendData()`)
dan halaman detail (foto+deskripsi+Pricelist+Ajukan Booking) muncul di chat
seperti biasa lewat menu chat biasa.

Cara aktifkan:
1. Hosting folder `webapp/` (isi `index.html` + `bgm.mp3`) di layanan statis
   apa pun, mis. GitHub Pages, Netlify, atau Cloudflare Pages.
2. Deploy bot ini ke Railway dan aktifkan **Networking → Public Domain**
   supaya `api_server.py` (penyedia data katalog) bisa diakses dari internet.
3. Edit baris `API_BASE_URL` di `webapp/index.html` menjadi domain Railway kamu
   (mis. `https://nama-app.up.railway.app`), lalu upload ulang ke hosting statis.
4. Isi `WEBAPP_URL` di Railway dengan URL hasil hosting `index.html`, redeploy.

Musik:
- Sudah diatur otomatis: coba autoplay dalam kondisi mute (diizinkan browser),
  lalu otomatis unmute + volume 50% begitu user menyentuh layar pertama kali
  (kebijakan autoplay browser mengharuskan ada interaksi user untuk audio bersuara).
- Ada tombol 🔇/🔊 di pojok kanan atas untuk toggle manual.
- Ganti file `webapp/bgm.mp3` kapan saja kalau mau pakai musik lain (pastikan
  kamu punya hak pakai atas file musik tersebut).

Catatan: Mini App ini **tidak** mengirim atau menerima data live-chat/pembayaran
apa pun — endpoint di `api_server.py` semuanya read-only (GET), tidak ada
endpoint yang menulis data.

## Deploy ke Railway

- Pasang **Volume** (Settings → Volumes) supaya database (`bot.db`) tidak hilang
  setiap redeploy. `config.py` otomatis pakai `RAILWAY_VOLUME_MOUNT_PATH` kalau ada.
- Jalankan sebagai **worker** (bot pakai `run_polling()`, bukan server HTTP),
  jadi tidak perlu Networking/Public Domain.
- Isi environment variables sesuai `.env.example` di Settings → Variables.

## Warna Tombol

Bot ini pakai fitur `style` bawaan Telegram Bot API 9.4 (Februari 2026) untuk
mewarnai tombol inline secara native — bukan emoji atau Mini App:
- `success` → hijau (aksi utama, mis. Pilih Talent, Pricelist, Ajukan Booking)
- `primary` → biru (navigasi/opsi netral)
- `danger` → merah (batal/kembali/hapus)

Catatan: warna hanya tampil di aplikasi Telegram yang sudah update setelah
9 Februari 2026 — versi Telegram lama akan menampilkan tombol tanpa warna
(fallback normal, tidak error). Membutuhkan `python-telegram-bot>=22.7`.

## Catatan

Bot ini sengaja **tidak** menyertakan:
- Kanal live-chat real-time yang menyambungkan percakapan user ke grup admin.
- Verifikasi pembayaran otomatis (OCR bukti transfer) atau auto-posting bukti
  transfer ke channel.

Kalau kamu butuh sistem pembayaran untuk bisnis lain (toko online, penjualan
produk/jasa dengan alur DP yang jelas), sebaiknya dibangun sebagai proyek
terpisah sesuai konteks bisnisnya.
