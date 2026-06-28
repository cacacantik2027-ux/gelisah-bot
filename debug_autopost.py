#!/usr/bin/env python3
"""
Debug script - jalankan di server untuk cek settings dan autopost
Usage: python3 debug_autopost.py
"""
import json, os, sys

SETTINGS_FILE = "data/settings.json"

if not os.path.exists(SETTINGS_FILE):
    print("❌ File data/settings.json TIDAK DITEMUKAN!")
    print("   Pastikan Anda menjalankan skrip ini dari folder yang sama dengan bot.py")
    sys.exit(1)

with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
    s = json.load(f)

print("=" * 50)
print("📋 ISI SETTINGS.JSON SAAT INI:")
print("=" * 50)
for k, v in s.items():
    print(f"  {k}: {repr(v)}")

print()
print("=" * 50)
print("🔍 DIAGNOSA AUTOPOST:")
print("=" * 50)

channel_id = s.get("channel_id", "FIELD TIDAK ADA")
log_group_id = s.get("log_group_id", "FIELD TIDAK ADA")
qris_url = s.get("qris_url", "FIELD TIDAK ADA")

print(f"  channel_id    : {channel_id}")
print(f"  log_group_id  : {log_group_id}")
print(f"  qris_url      : {qris_url}")

print()
if channel_id == "FIELD TIDAK ADA":
    print("❌ MASALAH: Field 'channel_id' tidak ada di settings.json!")
    print("   → Jalankan /settings di bot lalu isi Channel ID")
elif channel_id == 0 or channel_id == "0":
    print("❌ MASALAH: channel_id = 0, autopost TIDAK AKTIF!")
    print("   → Jalankan /settings di bot lalu isi Channel ID channel tujuan")
    print("   → Format: -100xxxxxxxxxx (angka negatif)")
else:
    print(f"✅ channel_id sudah diisi: {channel_id}")

if log_group_id == 0 or log_group_id == "FIELD TIDAK ADA":
    print("⚠️  log_group_id = 0, bukti transfer tidak dikirim ke grup log!")
else:
    print(f"✅ log_group_id sudah diisi: {log_group_id}")

# Cek apakah ada field yang hilang
required_fields = ["channel_id", "log_group_id", "qris_url", "watermark_file_id"]
missing = [f for f in required_fields if f not in s]
if missing:
    print()
    print(f"⚠️  Field berikut TIDAK ADA di settings.json: {missing}")
    print("   → Bot akan otomatis menambahkan saat restart, tapi sebaiknya tambahkan manual")
    # Tambahkan field yang hilang
    if "channel_id" not in s:
        s["channel_id"] = 0
    if "watermark_file_id" not in s:
        s["watermark_file_id"] = ""
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=4, ensure_ascii=False)
    print("   → Field yang hilang sudah ditambahkan otomatis dengan nilai default")

print()
print("=" * 50)
print("Jika channel_id sudah benar tapi autopost masih gagal,")
print("cek log bot untuk baris yang mengandung [AUTOPOST]")
print("=" * 50)
