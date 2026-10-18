# 📚 Panduan Belajar: Cara Kerja context-hygiene

Dokumen ini buat kamu yang mau **membaca kodenya**, paham alurnya, dan
semoga terinspirasi fork + memperbaiki. Ditulis untuk pembaca pemula-menengah
Python. Kode totalnya cuma ~2 file inti, jadi benar-benar bisa dibaca habis.

---

## 1. Masalah yang diselesaikan (40 detik)

AI agent (Hermes, Claude Code, Codex, dll) nyimpen percakapan sebagai "sesi".
Sesi yang jalan berhari-hari mengalami:

```
sesi panjang → konteks penuh → kompresi (peringkas lama) → ringkasan lossy
             → agent "mengingat" hal yang gak pernah dibicarakan → HALU
```

Solusi: **reset sesi secara rutin**. Script ini ngasih tahu *kapan*.

---

## 2. Peta file

```
context_hygiene.py   ← DETEKSI: cari sesi, ukur umur & token, kasih verdict
reset_modes.py       ← AKSI: kirim notifikasi ATAU reset langsung
install.py           ← SETUP: wizard interaktif → tulis crontab + wrapper
skill-hermes/        ← SKILL: aturan perilaku buat agent Hermes
docs/ARCHITECTURE.md ← file yang sedang kamu baca
assets/              ← ilustrasi untuk README
```

Alur data:

```
context_hygiene.py --json          reset_modes.py
┌─────────────────────────┐       ┌──────────────────────┐
│ 1. DETECT (per tool)    │       │ 4. FILTER            │
│    scan state.db /      │ stdout│    ambil verdict==   │
│    *.jsonl / *.json     │──────▶│    "warning"         │
│ 2. MEASURE              │  JSON │ 5. ACT               │
│    age_hours, tokens    │       │    notify → Telegram/│
│ 3. EVALUATE → verdict   │       │      session/webhook │
│    healthy|warning|idle │       │    auto → reset      │
└─────────────────────────┘       └──────────────────────┘
```

Kenapa dipisah dua script? **Unix philosophy**: deteksi gak butuh akses
token Telegram; aksi gak butuh tahu cara scanning. Keduanya komunikasi lewat
JSON sederhana → gampang dites terpisah, gampang diganti salah satunya.

---

## 3. Anatomi `context_hygiene.py`

### 3.1 Pattern "backend registry"

```python
BACKENDS = {
    "hermes": lambda: check_hermes(),
    "claude-code": check_claude_code,
    "codex": check_codex,
    "opencode": check_opencode,
}
```

Setiap tool AI disimpan sesi di tempat berbeda → satu fungsi `check_*` per
tool, semuanya mengembalikan format sama:

```python
{"tool": str, "session_id": str, "started_at": float,
 "last_active": float, "tokens": int|None, "detail": str}
```

**Pelajaran desain #1:** kalau mau nambah dukungan Cursor/Aider, kamu cuma
tulis satu fungsi `check_cursor()` lalu daftar di dict — gak nyentuh kode lain.
Ini pola *plugin registry*, dipakai di mana-mana (pytest, flask blueprint).

### 3.2 Cara baca database tool lain dengan aman

```python
con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)   # READ-ONLY!
```

- `mode=ro` = read-only. **Jangan pernah** buka DB milik aplikasi lain
  mode tulis dari script eksternal — risiko korup kalau aplikasinya
  sedang nulis (SQLite pakai WAL; dua penulis = bencana).
- Semua `check_*` dibungkus `try/except` di `main()` → kalau Claude Code
  gak terpasang, dia diam saja return `[]`. Script satu ini jalan di
  mesin mana pun tanpa error meski cuma ada 1 dari 4 tool.

**Pelajaran desain #2:** *fail silently per backend, fail loudly overall.*
Satu backend gagal = skip. Semua backend gagal = exit code 2.

### 3.3 Estimasi token

```python
def _estimate_tokens_from_chars(n_chars): return n_chars // 4
```

Aturan praktis bahasa Inggris: ~4 karakter ≈ 1 token. Bahasa Indonesia
sedikit lebih boros (~3–3.5 char/token) — jadi estimasi kita *konservatif*
(mengarah ke under-estimate, aman: warning muncul lebih lambat, bukan spam).
Kalau tool menyediakan angka asli (Hermes: `last_prompt_tokens`), pakai yang
asli. Estimasi cuma fallback.

### 3.4 Evaluasi & verdict

```python
if idle_h > 48:      verdict = "idle"      # lama gak dipakai = bukan masalah
elif stale or overloaded: verdict = "warning"  # perlu reset
else:                verdict = "healthy"   # aman
```

Urutan penting: cek idle DULULAN. Sesi 2 minggu lalu yang gak pernah
dipakai lagi bukan ancaman — jangan bikinin alarm noise.

**Pelajaran desain #3:** filter noise sebelum alarm. Alert yang sering
salah bikin user mengabaikan alert (efek "serigala datang").

---

## 4. Anatomi `reset_modes.py`

### 4.1 Dua mode, satu prinsip: TIDAK ADA YANG DIHAPUS

| mode | perilaku | hermes | file-based tool |
|---|---|---|---|
| `notify` | kasih tahu user, user yang ketik `/new` | — | — |
| `auto` | reset langsung | `UPDATE sessions SET ended_at=...` + hapus baris routing | `shutil.move()` ke `archived/` |

Reset Hermes = menutup sesi (stamp `ended_at`) + menghapus entri routing
sehingga gateway membuat sesi BARU pada pesan berikutnya. **Transkrip &
memori tetap utuh** — ini recovery path resmi Hermes (`gateway/session.py`),
bukan hack.

**Pelajaran desain #4:** operasi destruktif harus *reversible*.
Arsip ≠ hapus. End-session ≠ delete-row. Kalau salah, tinggal dipulihkan.

### 4.2 Idle guard — pengaman paling penting

```python
if rrow and (now - rrow[0]) < min_idle_min * 60:
    return "SKIP: ... refusing mid-conversation reset"
```

Mode auto menolak reset kalau sesi aktif < 15 menit lalu. Bayangkan bot
nge-reset sesi *persis saat kamu sedang mengetik* — pesanmu nyasar ke
kepala yang baru. Guard ini yang bikin mode auto aman dijalankan cron jam 9
tanpa pengawasan.

**Pelajaran desain #5:** otomasi tanpa guard = bom waktu. Selalu tentukan
"kondisi kapan aksi INI TIDAK boleh jalan" sebelum menulis aksinya.

### 4.3 Notification adapter (sama polanya dengan backend registry)

```python
def send_notice(text, target):
    if target.startswith("telegram:"): return notify_telegram(...)
    if target.startswith("http"):      return notify_webhook(...)
    return notify_stdout(text)          # fallback = session
```

Telegram cuma butuh: 1 HTTP POST ke `api.telegram.org` dengan
`TELEGRAM_BOT_TOKEN` dari environment. Webhook (ntfy/Slack/Discord) malah
lebih simpel: POST body plain text. Nambah kanal = nambah 5 baris.

### 4.4 Kontrak antar-script (ini kunci fork-ability)

`context_hygiene.py --json` menghasilkan array; `reset_modes.py` hanya
memproses baris `"verdict": "warning"`. Itu SELURUH kontraknya. Kamu bisa:

- ganti deteksi dengan tool lain (misal baca dari API cloud) → asal keluarkan JSON format sama
- ganti aksi dengan integrasi kamu (misal auto-chat ke bot) → asal terima JSON format sama

---

## 5. Anatomi `install.py`

Wizard 4 langkah → tulis 2 artefak:

1. `~/.context-hygiene/run_check.sh` — wrapper yang menjalankan
   `context_hygiene.py --json` lalu menyalurkannya ke `reset_modes.py`
2. entri crontab bertanda `# context-hygiene` — installer menghapus
   entri lamanya dulu (cari baris yang mengandung MARKER) sebelum menambah
   yang baru → **aman dijalankan ulang**, gak numpuk cron duplikat.

**Pelajaran desain #6:** installer yang idempotent (boleh dijalankan
berkali-kali dengan hasil akhir sama) itu wajib. User pasti lupa setting
pertamanya.

---

## 6. Ide perbaikan (buat yang mau fork!)

Diurutkan dari gampang:

1. **Backend baru** (paling gampang): Cursor (`~/Library/Application Support/Cursor/User/workspaceStorage/*/state.vscdb`), Aider (`.aider.chat.history.md`), Gemini CLI. Cukup satu fungsi + daftar di `BACKENDS`.
2. **Flag `--notify ntfy`** dengan topic: sebenarnya sudah jalan via `--notify https://ntfy.sh/<topic>` (webhook) — tinggal didokumentasikan + ditambah di installer sebagai pilihan 4.
3. **Estimasi token per-bahasa**: deteksi rasio char/token dari isi transkrip sendiri, lebih akurat dari konstanta 4.
4. **Windows support**: path di `detect_tools()` masih POSIX. Ganti `Path.home()/...` dengan logika `%APPDATA%`.
5. **Test suite**: setiap `check_*` mudah dites dengan membuat fake `state.db`/`*.jsonl` di `tmp_path` (pytest). Skema Hermes yang diperlukan cuma `gateway_routing(entry_json)` + `sessions(id, started_at, ended_at, input_tokens)`.
6. **Dashboard kecil**: satu halaman HTML yang menjalankan `--json` via fetch dan menampilkan verdict per tool.

## 7. Gaya kode yang dipakai (konvensi repo ini)

- Python stdlib only — **zero dependencies** = bisa jalan di mana saja tanpa venv
- Type hints di signature publik, `from __future__ import annotations` biar kompatibel 3.9
- Docstring di atas file = "apa ini & kapan dipakai", komentar = "kenapa", bukan "apa"
- Fungsi kecil satu tanggung jawab; CLI parsing di `main()` saja
- Exit code bermakna: 0=ok, 1=ada warning, 2=error konfigurasi

Selamat berfork! 🍴 Jangan lupa PR balik kalau backend barumu jadi. 😸
