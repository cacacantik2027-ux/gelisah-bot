# Bot Order Talent 

Bot Telegram untuk katalog talent/influencer/model, dengan fitur **live chat
langsung** yang menghubungkan user ke admin untuk keperluan booking
konten/endorsement/event.

## Fitur

1. **Katalog Talent** — foto, deskripsi, dan link portofolio per talent.
2. **Chat Sekarang** — user tekan tombol "💬 Chat Sekarang" di halaman detail
   talent, lalu langsung terhubung ke **grup live chat admin** (atau ke
   masing-masing admin secara private kalau grup tidak dikonfigurasi):
   - Setiap pesan yang diketik user diteruskan (relay) apa adanya ke admin.
   - Admin membalas dengan cara **reply** pesan yang diteruskan tsb, dan
     balasannya otomatis diteruskan balik ke user.
   - Admin bisa **mengakhiri sesi** kapan saja lewat tombol "🔴 Akhiri Sesi"
     begitu topik pembicaraan selesai. User akan diberi tahu sesi sudah ditutup.
3. **/settings** — kelola konten bot: tambah/hapus talent, ubah teks sapaan,
   ubah teks "Cara Order", dan lihat sesi live chat yang sedang aktif.

## Setup

1. Install dependency:
   ```
   pip install -r requirements.txt
   ```
2. Salin `.env.example` menjadi `.env` dan isi:
   - `BOT_TOKEN` — token dari @BotFather
   - `ADMIN_IDS` — ID Telegram kamu (cek lewat @userinfobot)
   - `LIVECHAT_GROUP_ID` — opsional, ID grup live chat admin. Buat grup Telegram,
     tambahkan bot ke grup tsb, lalu jalankan `/groupid` di grup itu untuk
     mendapatkan ID-nya. Kalau dikosongkan, pesan live chat dikirim ke
     masing-masing `ADMIN_IDS` secara private.
3. Jalankan:
   ```
   python bot.py
   ```

## Command

| Command      | Keterangan                                        |
|--------------|----------------------------------------------------|
| `/start`     | Tampilkan menu utama (Pilih Talent, Cara Order)     |
| `/settings`  | Menu kelola konten (khusus admin)                   |
| `/groupid`   | Tampilkan chat ID (untuk isi `LIVECHAT_GROUP_ID`)   |
| `/cancel`    | Batalkan proses yang sedang berjalan (mis. tambah talent) |

## Cara Kerja Live Chat

1. User menekan "💬 Chat Sekarang" di halaman detail talent (atau dari Mini App).
2. Bot membuat sesi live chat baru, lalu mengirim pesan header ke grup live
   chat (kalau `LIVECHAT_GROUP_ID` diisi) atau ke tiap admin secara private,
   lengkap dengan tombol "🔴 Akhiri Sesi".
3. Setiap pesan yang dikirim user selanjutnya (teks, foto, voice note, dsb)
   diteruskan otomatis ke tujuan admin tsb selama sesi masih aktif.
4. Admin membalas dengan **me-reply** pesan yang diteruskan itu (baik reply ke
   pesan header maupun ke pesan user tertentu) — balasannya akan diteruskan
   otomatis ke user yang bersangkutan.
5. Kalau topik sudah selesai dibahas, admin tekan tombol "🔴 Akhiri Sesi" pada
   pesan header. Sesi ditutup, user diberi tahu, dan tombol pada pesan header
   hilang supaya tidak dipakai lagi.

Catatan: satu user hanya bisa punya **satu sesi live chat aktif** di satu waktu.
Kalau user menekan "Chat Sekarang" lagi saat sesi sebelumnya masih aktif, bot
hanya mengingatkan supaya lanjut mengetik di sesi yang sudah ada.

## Mini App (opsional)

Folder `webapp/` berisi katalog talent versi tampilan-app (`index.html` + musik
background `bgm.mp3`, volume 50%). Ini murni tampilan (browse-only): begitu
user tap talent tertentu, data dikirim balik ke bot (`Telegram.WebApp.sendData()`)
dan halaman detail (foto+deskripsi+Pricelist+Chat Sekarang) muncul di chat
seperti biasa lewat menu chat biasa. Menekan "💬 Chat Sekarang" di Mini App akan
langsung membuka sesi live chat yang sama seperti dari menu chat biasa.

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

Catatan: Mini App ini sendiri (`index.html` + `api_server.py`) tetap
read-only untuk data katalog — sesi live chat sepenuhnya ditangani oleh bot
lewat Telegram, bukan lewat endpoint Mini App.

## Deploy ke Railway

- Pasang **Volume** (Settings → Volumes) supaya database (`bot.db`) tidak hilang
  setiap redeploy. `config.py` otomatis pakai `RAILWAY_VOLUME_MOUNT_PATH` kalau ada.
- Jalankan sebagai **worker** (bot pakai `run_polling()`, bukan server HTTP),
  jadi tidak perlu Networking/Public Domain.
- Isi environment variables sesuai `.env.example` di Settings → Variables.

## Warna Tombol

Bot ini pakai fitur `style` bawaan Telegram Bot API 9.4 (Februari 2026) untuk
mewarnai tombol inline secara native — bukan emoji atau Mini App:
- `success` → hijau (aksi utama, mis. Pilih Talent, Pricelist, Chat Sekarang)
- `primary` → biru (navigasi/opsi netral)
- `danger` → merah (batal/kembali/hapus/akhiri sesi)

Catatan: warna hanya tampil di aplikasi Telegram yang sudah update setelah
9 Februari 2026 — versi Telegram lama akan menampilkan tombol tanpa warna
(fallback normal, tidak error). Membutuhkan `python-telegram-bot>=22.7`.

## Catatan

- Verifikasi pembayaran otomatis (OCR bukti transfer) atau auto-posting bukti
  transfer ke channel **tidak** disertakan.
- Kalau kamu butuh sistem pembayaran untuk bisnis lain (toko online, penjualan
  produk/jasa dengan alur DP yang jelas), sebaiknya dibangun sebagai proyek
  terpisah sesuai konteks bisnisnya.
