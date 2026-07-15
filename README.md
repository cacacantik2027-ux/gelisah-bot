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

## Deploy ke Railway

- Pasang **Volume** (Settings → Volumes) supaya database (`bot.db`) tidak hilang
  setiap redeploy. `config.py` otomatis pakai `RAILWAY_VOLUME_MOUNT_PATH` kalau ada.
- Jalankan sebagai **worker** (bot pakai `run_polling()`, bukan server HTTP),
  jadi tidak perlu Networking/Public Domain.
- Isi environment variables sesuai `.env.example` di Settings → Variables.

## Catatan

Bot ini sengaja **tidak** menyertakan:
- Kanal live-chat real-time yang menyambungkan percakapan user ke grup admin.
- Verifikasi pembayaran otomatis (OCR bukti transfer) atau auto-posting bukti
  transfer ke channel.

Kalau kamu butuh sistem pembayaran untuk bisnis lain (toko online, penjualan
produk/jasa dengan alur DP yang jelas), sebaiknya dibangun sebagai proyek
terpisah sesuai konteks bisnisnya.
