# TALENT GELISAH — Bot Telegram Booking Talent

Bot Telegram untuk booking talent/influencer/model untuk keperluan
konten, endorsement, dan event.

## Fitur

1. **Menu /start** — tombol "💃 Pilih Talent" sendiri di baris atas,
   "💬 Live Chat" dan "📖 Cara Order" sejajar di baris kedua.
2. **Live Chat** — user tekan "Live Chat", lalu semua pesan (teks/foto)
   yang dikirim otomatis diteruskan ke grup admin. Admin cukup **reply**
   pesan yang dikirim bot di grup itu untuk membalas langsung ke user
   yang bersangkutan.
3. **Halaman Talent** — setiap talent punya halaman sendiri (foto +
   deskripsi) dengan tombol:
   - **💰 Pricelist** — tampilkan rincian harga.
   - **❓ Tanyakan Ready** — otomatis mengirim pertanyaan ketersediaan
     ke grup admin (menyebut nama talent + identitas user). Admin
     tinggal reply pesan tersebut untuk menjawab langsung ke user,
     memakai mekanisme relay yang sama dengan Live Chat.

## Mini App "Katalog Talent" (opsional)

Selain daftar talent versi tombol chat biasa, tersedia juga **Mini App**
(`index.html`) berupa katalog visual (grid foto talent) yang dibuka dari
tombol khusus di bawah kolom chat. Ketuk salah satu talent di Mini App
langsung membuka halaman detailnya di chat (foto + deskripsi + tombol
Pricelist/Tanyakan Ready), sama seperti lewat menu biasa.

Cara aktifkan:
1. Aktifkan **Networking > Public Domain** di dashboard service Railway
   kamu (supaya `PORT` ter-inject otomatis & backend Mini App bisa diakses
   publik) — bot tetap jalan pakai polling, fitur ini cuma butuh domain
   publik untuk endpoint `/api/talents` & `/photo/<id>`.
2. Buka `index.html`, ganti `API_BASE_URL` dengan domain Railway kamu
   (mis. `https://nama-service-kamu.up.railway.app`).
3. Hosting `index.html` di layanan statis mana pun (mis. GitHub Pages).
4. Isi `WEBAPP_URL` di `.env`/Railway dengan URL hasil hosting itu, lalu
   redeploy bot.

Kalau `WEBAPP_URL` dikosongkan, Mini App otomatis nonaktif dan bot tetap
berjalan normal lewat daftar talent versi chat biasa.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` menjadi `.env`, isi `BOT_TOKEN`, `ADMIN_IDS`.
3. Tambahkan bot ke grup Telegram khusus admin/CS (grup ini yang akan
   dipakai untuk live chat & tanya-ready), lalu jalankan bot dan ketik
   `/groupid` di grup itu untuk mendapatkan chat id-nya. Isi ke
   `LIVECHAT_GROUP_ID` di `.env`, lalu restart bot.
4. Jalankan bot: `python bot.py`

## Kelola talent (khusus admin, sesuai `ADMIN_IDS`)

- `/addtalent` — tambah talent baru (nama → deskripsi → foto → pricelist,
  foto & pricelist boleh dilewati).
- `/talents` — lihat daftar semua talent beserta ID-nya.
- `/deltalent` — hapus talent (pilih dari daftar tombol).
- `/setgreeting` — ubah teks sapaan di `/start`.
- `/sethowtoorder` — ubah teks halaman "Cara Order".

## Catatan teknis

- Relay live chat & tanya-ready memakai tabel `relay_messages`: setiap
  pesan yang bot kirim ke grup admin dicatat `message_id`-nya, dipetakan
  ke `user_id` pengirim asli. Saat admin **reply** pesan itu di grup,
  bot mencari petanya di tabel ini untuk tahu balasan harus diteruskan
  ke user mana — jadi admin tidak perlu mengetik ID user manual.
- Set `LIVECHAT_ADMIN_ONLY_REPLY=true` di `.env` kalau grup live chat
  kamu bukan grup privat khusus admin (supaya hanya user di `ADMIN_IDS`
  yang balasannya diteruskan ke customer).
