# unipool

Bot Telegram untuk farming fee LP **Uniswap V3** di **Robinhood Chain** (chain id 4663) dan **BSC** (chain id 56).

Paste alamat token → bot cari pool → pilih strategi → mint posisi LP. Pantau lewat `/list` (PnL, fee, chart live), tutup posisi satu tombol dengan auto-swap. Semua data dibaca langsung dari blockchain — tidak bergantung UI Uniswap yang sering gagal fetch harga.

## Fitur

- 🔍 **Auto pool discovery** — scan semua fee tier (0.01%–1%) × semua quote (WETH/USDG di Robinhood, WBNB/USDT/USDC di BSC), urut TVL
- 🎯 **4 strategi range**: Stable (±6%), Wide (dua sisi), Lower (setor quote saja, nampung kalau harga turun), Upper (setor token saja, jual bertahap kalau naik) + rekomendasi otomatis
- ✏️ **Custom range** via persen atau **market cap** (`mc 300k 800k`), custom amount (persen saldo / nilai pasti)
- 🔁 **Auto-wrap** ETH→WETH, **auto-swap** komposisi dua sisi (token existing di wallet dipakai duluan)
- 📊 **/list** — nilai posisi, fee unclaimed, PnL per posisi & portfolio, status IN/OUT range
- 📈 **Chart live** — grafik market cap + kotak range posisi, refresh 3 detik, link dexscreener
- 🔔 **Alert** otomatis saat posisi keluar/masuk range
- 🧭 **Menu navigasi** — `/start` membuka dashboard (saldo + tombol Posisi/Dompet/Pengaturan/Chain); semua setting bisa diubah lewat tombol tanpa hafal perintah
- 🛡️ **Failover RPC** multi-endpoint + bypass blokir DNS ISP Indonesia (DoH + koneksi IP langsung, sertifikat tetap diverifikasi)
- 💰 Harga token berlapis: pool v3 → pair v2 → API dexscreener

## Instalasi

### 1. Prasyarat

- Python 3.10 atau lebih baru (`python3 --version`)
- Akun Telegram

### 2. Clone & install dependency

```bash
git clone https://github.com/gardianz/unipool.git
cd unipool
pip install -r requirements.txt
```

### 3. Buat bot Telegram

1. Chat [@BotFather](https://t.me/BotFather) di Telegram → kirim `/newbot`
2. Kasih nama & username bot → BotFather membalas dengan **token** (format `123456:ABC-DEF...`). Simpan.

### 4. Ambil chat ID kamu

Chat [@userinfobot](https://t.me/userinfobot) → dia membalas dengan angka **Id** kamu. Simpan.

> Chat ID wajib diisi. Bot hanya merespons chat ID ini — siapa pun yang bisa chat bot = kontrol penuh wallet.

### 5. (Disarankan) API key Alchemy

Daftar gratis di [dashboard.alchemy.com](https://dashboard.alchemy.com) → buat app → enable network **Robinhood Mainnet** (dan **BNB Mainnet** kalau perlu) → salin API key.

Tanpa Alchemy bot tetap jalan lewat RPC publik, tapi fitur **chart** butuh archive node (Alchemy menyediakannya) dan RPC-nya lebih stabil.

### 6. Konfigurasi `.env`

```bash
cp .env.example .env
nano .env   # atau editor apa pun
```

Isi:

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...      # dari BotFather (langkah 3)
TELEGRAM_CHAT_ID=123456789                # dari userinfobot (langkah 4)
PRIVATE_KEY=0x...                         # private key hot wallet
ALCHEMY_API_KEY=...                       # opsional tapi disarankan (langkah 5)
```

> ⚠️ **Pakai hot wallet khusus** berisi dana secukupnya — jangan wallet utama. File `.env` jangan pernah di-commit/dibagikan.

### 7. Jalankan

```bash
python3 bot.py
```

Log `LP bot jalan. Wallet: 0x...` = siap. Buka chat bot kamu di Telegram, kirim `/start`.

## Cara pakai singkat

1. **Paste alamat token** (`0x...`) → bot tampilkan daftar pool + TVL
2. Pilih pool → muncul **kartu konfirmasi**: strategi, range (dalam market cap), komposisi deposit, rencana wrap/swap
3. Atur pakai tombol (strategi / preset range / amount) atau **✏️ Custom** — balas dengan `40 120` (persen) atau `mc 300k 800k`
4. **✅ Confirm mint** → bot eksekusi wrap → approve → mint, kirim semua link transaksi
5. `/list` → pantau; tombol **📈 Chart** (grafik live + box range) dan **🗑 Close**
6. Close → pilih **swap semua → WETH** atau **tahan token**

## Perintah

| Perintah | Fungsi |
|---|---|
| `/start` | menu utama: dashboard saldo + tombol navigasi |
| paste `0x...` | cari pool token di chain aktif |
| `/list` | posisi + PnL + tombol chart/close |
| `/wallet` | saldo semua token + nilai USD + CA |
| `/settings` | lihat semua setting |
| `/set width 30` | default lebar range % |
| `/set amount_pct 50` | deposit % saldo quote |
| `/set amount 0.05` | deposit fix (override %) |
| `/set slippage 5` | slippage % |
| `/set gap 0` | range nempel harga (default 1 tick-spacing) |
| `/set alert 60` | interval cek alert range (detik, `off` = mati) |
| `/set autoswap off` | matikan auto-swap |
| `/chain 56` | ganti chain (4663 Robinhood / 56 BSC) |

## Troubleshooting

**SSL "Hostname mismatch" ke `rpc.mainnet.chain.robinhood.com`** — DNS ISP Indonesia memblokir domain robinhood.com (redirect internetpositif.id). Bot otomatis bypass: resolve IP asli via DNS-over-HTTPS lalu konek langsung (sertifikat tetap diverifikasi). Alternatif permanen: ganti DNS Windows/router ke `1.1.1.1`.

**Chart gagal / "riwayat tidak tersedia"** — RPC yang dipakai bukan archive node. Isi `ALCHEMY_API_KEY`.

**Mint revert saat token lagi ramai** — harga bergerak melewati range saat transaksi disiapkan. Bot sudah retry 3× otomatis; kalau tetap gagal, perlebar range atau naikkan `/set gap`.

**Token di /wallet "harga ?"** — token belum punya pool v3/v2 dan tidak terindeks dexscreener.

## Catatan risiko

- Bot ini memindahkan **dana sungguhan** di chain live. Uji dengan nominal kecil dulu.
- LP memecoin berisiko tinggi: fee tidak menutup rugi kalau harga token jatuh permanen (impermanent loss). Anggap sebagai beli token diskon sambil dibayar menunggu — bukan mesin uang pasif.
- Private key tersimpan plaintext di `.env` pada mesin yang menjalankan bot. Amankan mesinnya.

## Struktur kode

- `bot.py` — UI Telegram (handler, kartu konfirmasi, chart server lokal, monitor alert)
- `chain.py` — inti web3: discovery, mint, list, close, swap, riwayat harga
- `store.py` — settings + riwayat PnL (`settings.json`, `history.json`)
