# 🤖 GELISAH VCS Talent Bot

Bot Telegram resmi **GELISAH** untuk showcase talent, pricelist, dan order via live chat.

---

## ✨ Fitur

- 👤 **Kartu Talent** — foto + nama + deskripsi
- 💰 **Pricelist** — slide ke-2 dengan detail harga per paket
- 🛒 **Tombol Order** — langsung ke `@livechatgs_bot` dengan pesan otomatis nama talent
- ⚙️ **Panel Admin** — tambah / edit / hapus talent via chat

---

## 🚀 Deploy ke Railway (Gratis)

### 1. Upload repo ke GitHub

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/USERNAME/gelisah-bot.git
git push -u origin main
```

### 2. Buat project di Railway

1. Buka [railway.app](https://railway.app) → login GitHub
2. **New Project** → **Deploy from GitHub repo** → pilih repo ini
3. Buka tab **Settings** → **Service** → pastikan **Start Command** kosong (pakai Procfile)

### 3. Set Environment Variables di Railway

Buka tab **Variables** → tambahkan satu per satu:

| Name | Value |
|---|---|
| `BOT_TOKEN` | Token dari @BotFather |
| `LIVECHAT_BOT` | `livechatgs_bot` |
| `ADMIN_IDS` | User ID kamu (dari @userinfobot) |

### 4. Deploy!

Klik **Deploy** atau Railway otomatis deploy setelah variables diisi.

Cek tab **Logs** — jika muncul `Bot is running.` berarti sukses ✅

---

## 🔧 Jalankan Lokal

```bash
# Clone & masuk folder
git clone https://github.com/USERNAME/gelisah-bot.git
cd gelisah-bot

# Buat virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Setup .env
cp .env.example .env
# Edit .env — isi BOT_TOKEN dan ADMIN_IDS

# Jalankan
python bot.py
```

---

## 📋 Perintah Bot

| Perintah | Siapa | Fungsi |
|---|---|---|
| `/start` | Semua | Tampilkan daftar talent |
| `/help` | Semua | Panduan penggunaan |
| `/contact` | Semua | Info kontak admin |
| `/admin` | Admin | Panel kelola talent |
| `/cancel` | Admin | Batalkan operasi admin |

---

## 📁 Struktur Folder

```
gelisah-bot/
├── bot.py              # File utama
├── config.py           # Konfigurasi & env vars
├── requirements.txt    # Dependencies
├── Procfile            # Start command untuk Railway
├── runtime.txt         # Versi Python
├── .env.example        # Template env vars
├── .gitignore
├── README.md
└── data/
    ├── __init__.py
    ├── talents.py      # Load/save talent
    └── talents.json    # Database talent
```

---

## ❗ Troubleshooting Railway

| Error | Solusi |
|---|---|
| `BOT_TOKEN belum diisi` | Set variable `BOT_TOKEN` di Railway → Variables |
| `ModuleNotFoundError` | Pastikan `requirements.txt` sudah benar, redeploy |
| `No module named 'dotenv'` | Pastikan `python-dotenv` ada di requirements.txt |
| Bot tidak respond | Cek Logs, pastikan tidak ada error Python |
| Deploy stuck | Pastikan Procfile berisi `worker: python -u bot.py` |
