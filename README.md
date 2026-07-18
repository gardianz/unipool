# unipool

Bot Telegram untuk farming fee LP **Uniswap V2 + V3 + V4** di **Robinhood Chain** (chain id 4663) dan **BSC** (chain id 56).

Paste alamat token → bot cari pool (v2/v3/v4 sekaligus) → pilih strategi → mint posisi LP. Pantau lewat `/list` (PnL, fee, chart), tutup posisi satu tombol dengan auto-swap. Semua data dibaca langsung dari blockchain — tidak bergantung UI Uniswap yang sering gagal fetch harga.

## Fitur

- 🔍 **Auto pool discovery v2/v3/v4** — scan semua fee tier (0.01%–1%) × semua quote (WETH/USDG di Robinhood, WBNB/USDT/USDC di BSC), termasuk pool v4 ber-quote **ETH native**, urut TVL dengan label [v2]/[v3]/[v4]
- 🧬 **Uniswap V4** — mint/add/reduce/collect/close via PositionManager + Permit2 (approval dibatasi: jumlah pas + kedaluwarsa 1 jam), swap via UniversalRouter; pool ber-hook dilewati (vanilla saja)
- 💧 **Uniswap V2** — add liquidity full-range 50/50 (swap otomatis setengah budget), reduce/close via router; fee 0.3% auto-compound ke posisi
- 🛡️ **Anti pool beracun** — probe swap bolak-balik ~$100 (Quoter v4 / matematika reserves v2): pool dust atau harga dimanipulasi dibuang dari daftar; semua alamat kontrak v2/v4 diverifikasi silang on-chain sebelum tx pertama (fail-closed)
- 🎯 **4 strategi range**: Stable (±6%), Wide (dua sisi), Lower (setor quote saja, nampung kalau harga turun), Upper (setor token saja, jual bertahap kalau naik) + rekomendasi otomatis
- ✏️ **Custom range** via persen atau **market cap** (`mc 300k 800k`), custom amount (persen saldo / nilai pasti)
- 🔁 **Auto-wrap** ETH→WETH, **auto-swap** komposisi dua sisi (token existing di wallet dipakai duluan); pair non-WETH (mis. USDG) otomatis dibeli dari saldo WETH/ETH saat mint
- 📊 **/list** — nilai posisi, fee unclaimed, PnL per posisi & portfolio, status IN/OUT range
- 📈 **Chart** — tombol langsung ke GMGN & DexScreener per posisi/pool
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

Tanpa Alchemy bot tetap jalan lewat RPC publik, tapi RPC Alchemy lebih stabil dan tidak kena blokir DNS ISP.

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
5. `/list` → klik posisi untuk kartu detail; tombol **📈 GMGN** / **📊 DexScreener** (chart), **➕ Add**, **➖ Reduce**, **💰 Fee** (collect fee tanpa close; tidak ada di v2 karena auto-compound), **🗑 Close**
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

## Jalankan 24/7 di VPS (systemd)

Supaya bot hidup terus dan auto-restart kalau crash/VPS reboot:

```bash
sudo tee /etc/systemd/system/unipool.service > /dev/null <<'UNIT'
[Unit]
Description=unipool LP bot
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/unipool
ExecStart=/home/ubuntu/unipool/.venv/bin/python3 bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now unipool
```

Cek log: `journalctl -u unipool -f` · restart: `sudo systemctl restart unipool` (perlu setiap `git pull`).

## Troubleshooting

**SSL "Hostname mismatch" ke `rpc.mainnet.chain.robinhood.com`** — DNS ISP Indonesia memblokir domain robinhood.com (redirect internetpositif.id). Bot otomatis bypass: resolve IP asli via DNS-over-HTTPS lalu konek langsung (sertifikat tetap diverifikasi). Alternatif permanen: ganti DNS Windows/router ke `1.1.1.1`.

**Mint revert saat token lagi ramai** — harga bergerak melewati range saat transaksi disiapkan. Bot sudah retry 3× otomatis; kalau tetap gagal, perlebar range atau naikkan `/set gap`.

**Token di /wallet "harga ?"** — token belum punya pool v3/v2 dan tidak terindeks dexscreener.

**`telegram.error.NetworkError: Bad Gateway` di log** — server Telegram lagi gangguan sesaat (HTTP 502). Bot retry otomatis, tidak perlu diapa-apakan.

**`429 Too Many Requests` dari Alchemy** — kena limit compute-unit free tier. Bot sekarang retry otomatis dengan backoff (hormati Retry-After) dan meng-cache `eth_chainId`, jadi error ini harusnya hilang sendiri. Kalau masih sering: naikkan interval alert (`/set alert 120` atau `300`), atau upgrade plan Alchemy.

## Catatan risiko

- Bot ini memindahkan **dana sungguhan** di chain live. Uji dengan nominal kecil dulu.
- Token **fee-on-transfer** tidak didukung di jalur v2 (router standar) dan berisiko di v3/v4.
- Pool v4 dengan **hooks** sengaja tidak didukung — hook bisa berisi kode arbitrer (risiko rug).
- Posisi v4 dan LP v2 dicatat di `history.json` lokal (registry) — jangan hapus file itu selama masih ada posisi terbuka; posisi tetap aman on-chain, tapi bot tidak bisa menampilkannya lagi tanpa registry (v4 PositionManager tidak bisa di-enumerate).
- LP memecoin berisiko tinggi: fee tidak menutup rugi kalau harga token jatuh permanen (impermanent loss). Anggap sebagai beli token diskon sambil dibayar menunggu — bukan mesin uang pasif.
- Private key tersimpan plaintext di `.env` pada mesin yang menjalankan bot. Amankan mesinnya.

## Struktur kode

- `bot.py` — UI Telegram (handler, kartu konfirmasi, monitor alert)
- `chain.py` — inti web3: discovery + aksi v2/v3/v4, swap, verifikasi kontrak, riwayat harga
- `store.py` — settings + riwayat PnL (`settings.json`, `history.json`)
