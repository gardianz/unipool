# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Bahasa

Komentar kode, string yang dilihat user (Telegram/web), dan pesan commit di repo ini
memakai **bahasa Indonesia**. Ikuti gaya itu untuk perubahan baru.

## Menjalankan

```bash
pip install -r requirements.txt
cp .env.example .env      # isi TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PRIVATE_KEY

python3 bot.py            # bot Telegram (long polling)
python3 web.py            # UI web → http://127.0.0.1:8899
```

Tidak ada test suite, linter, atau build step di repo ini — verifikasi dilakukan manual
terhadap chain live. Pemeriksaan termurah setelah edit: `python3 -m py_compile chain.py bot.py web.py`.

Kedua proses bisa jalan bersamaan dan berbagi state file (`settings.json`, `history.json`).
Deploy di VPS pakai systemd (unit lengkap ada di README) — setiap `git pull` perlu
`systemctl restart unipool` / `unipool-web`.

⚠️ Kode ini memindahkan dana sungguhan di chain live dengan private key plaintext di `.env`.
Perubahan pada jalur transaksi (mint/close/swap/approval) tidak bisa "dicoba dulu" tanpa biaya.

## Arsitektur

Tiga file Python + satu folder static. **`chain.py` adalah satu-satunya mesin**; `bot.py`
dan `web.py` cuma UI di atasnya, jadi perubahan logika trading masuk ke `chain.py` supaya
kedua UI ikut berubah.

- **`chain.py`** (~3400 baris) — semua web3: konfigurasi chain & DEX, discovery pool,
  matematika tick/range, mint/add/reduce/collect/close/rebalance untuk v2/v3/v4, swap,
  verifikasi kontrak, harga token.
- **`bot.py`** — UI Telegram: handler, kartu konfirmasi, router callback, `monitor_loop`
  (alert range + eksekutor order TP/SL).
- **`web.py`** — server `http.server` stdlib (tanpa framework) + API JSON. Meng-`import bot`
  untuk `pk()`, `_addr_of()`, `compute_amount()` — jadi `bot.py` harus tetap aman di-import
  tanpa efek samping (jangan taruh kode jalan di luar `main()`).
- **`store.py`** — state JSON: settings, event PnL, registry posisi v2/v4, order TP/SL.
- **`static/`** — `index.html` + `app.js` + `lightweight-charts.js` (di-vendor, offline).

### `CHAINS` di chain.py:26 adalah sumber kebenaran per-chain

Semua alamat kontrak (factory, npm, router, v4_pm/posm/stateview/quoter/router, permit2,
wrapped, daftar quote), RPC, dan quirk chain tinggal di dict itu. Menambah chain =
menambah satu entri, bukan menyebar `if chain_id ==` di kode. Contoh quirk yang sudah
ada: `v4_swap_hop_field` (UniversalRouter Robinhood punya field ekstra di struct swap).

**Satu chain bisa punya beberapa DEX.** Robinhood = Uniswap saja; BSC = PancakeSwap
(utama) **+ Uniswap** di `CHAINS[56]["dexes"]`. Kunci di sub-dict itu menimpa kunci
chain untuk pool asal DEX tersebut.

Konsekuensi paling penting: **alamat kontrak milik POOL, bukan chain**. Di jalur
transaksi selalu `pool_cfg(chain_id, pool_info)` / `dex_cfg(chain_id, dex)`, jangan
`CHAINS[chain_id]` — NPM PancakeSwap dipakai untuk pool Uniswap tidak akan error,
dana justru mendarat di pool DEX lain dengan token+fee yang sama.

Helper: `dex_names()`, `dex_cfg()`, `pool_cfg()`, `has_v4(cid, dex)`, `any_has_v4()`,
`fee_tiers(cid, dex)`, `v4_dex()`/`v4_cfg()`, `uni_api_dex()`,
`which_dex_v2()`/`which_dex_v3()` (menentukan pemilik pool dari factory on-chain).

**`pid` v3 bernamespace** di DEX non-utama: `"uniswap:99"` (tokenId dua NPM bisa
bertabrakan). DEX utama tetap `"99"` supaya `history.json` lama terbaca. Lihat
`make_pid()`/`pid_dex()`/`parse_pid()`. v2 tidak dinamespace — alamat pair unik,
pemiliknya dicari lewat `which_dex_v2()`.

`assert_pool_orientation(w3, pool_info, chain_id)` ikut memverifikasi pool memang
milik factory DEX yang tertulis di dict.

Jebakan yang sudah terbukti saat menambah PancakeSwap — jangan diulang:

- **Fee tier tidak sama.** Uniswap punya 3000 (spacing 60), PancakeSwap punya 2500
  (spacing 50) dan tidak punya 3000. `TICK_SPACING` sengaja jadi gabungan keduanya
  (aman karena tidak bentrok), tapi tier yang di-*scan* harus dari `fee_tiers(cid)`.
- **`slot0.feeProtocol` beda tipe**: `uint8` di Uniswap, `uint32` di PancakeSwap
  (nilainya ratusan juta). `POOL_ABI` memakai `uint32` supaya mendekode keduanya —
  dengan `uint8`, eth-abi menolak padding tidak nol dan **semua** pool Pancake gagal
  dibaca. Jangan "dirapikan" balik ke uint8.
- **Router v3 Pancake ada dua.** Yang dipakai SmartRouter `0x13f4EA83…` karena
  `ExactInputSingleParams`-nya bentuk SwapRouter02 (tanpa `deadline`), cocok dengan
  `ROUTER_ABI`. SwapRouter `0x1b81D678…` memakai struct lama (+`deadline`,
  selector `0x414bf389`) → calldata tidak cocok.
- **Fee v2 beda**: Uniswap 0.3% (997/1000), Pancake 0.25% (9975/10000). Dipakai di
  probe round-trip `discover_v2_pools` dan label fee di UI.

Verifikasi alamat baru **on-chain** sebelum dipakai (semua alamat di dict itu sudah:
`npm.factory()`, `router.factory()`, `v2_router.factory()/WETH()` saling cocok).

### Dispatch versi pool

Satu dict "pool_info" dipakai lintas seluruh kode dengan field `ver` (2/3/4) plus
`pool`, `fee`, `quote_addr`, `quote_sym`, `quote_is_token1`, `token0/1`, `tick_spacing`.
Pool v4 juga membawa `pool_id` dan `key` (PoolKey tuple).

Posisi diidentifikasi oleh **`pid`**: `"183469"` = v3, `"v4:12"` = v4, `"v2:0xpair"` = v2
(`parse_pid()`). Aksi generik lewat `add_any` / `reduce_any` / `collect_any` / `close_any` /
`rebalance_position` — jangan panggil varian per-versi langsung dari UI.

### Registry posisi (kenapa `history.json` penting)

Posisi v3 bisa dienumerasi on-chain (ERC721Enumerable), tapi **PositionManager v4 tidak
bisa** dan posisi v2 cuma saldo LP token. Karena itu setiap mint v2/v4 menulis
`store.add_ref()`; kalau `history.json` hilang, posisi tetap aman on-chain tapi tidak
akan pernah muncul lagi di UI. `list_all_positions()` = enumerasi v3 + registry v2/v4.

### Discovery & sumber data: indexer untuk kecepatan, on-chain untuk kebenaran

`discover_any()` mencoba API resmi Uniswap (`ListPools`) dulu, fallback ke scan RPC
(`discover_pools`, semua quote × fee tier + `discover_dex_pools` via DexScreener).
Daftar posisi v3 pakai `ListPositions` untuk *daftar kandidat* saja.

Indexer Uniswap itu **cuma untuk chain ber-DEX Uniswap** (`uni_api: True`). Di BSC
kedua fungsi itu langsung `None`, jadi discovery selalu scan RPC + DexScreener dan
daftar posisi murni enumerasi NFT on-chain. Konsekuensinya di BSC: discovery beberapa
detik lebih lambat, dan enumerasi hanya memindai NFT terbaru — posisi ber-indeks lama
baru tertangkap lewat scan penuh (`full=True`, tombol Refresh).

Daftar DexScreener memuat pool dari **semua** DEX di chain itu. Yang menyaringnya
adalah verifikasi `factory.getPool(t0,t1,fee) == addr` di `discover_dex_pools` — itulah
alasan pool Uniswap tidak bisa nyasar masuk daftar BSC. Jangan lemahkan cek itu.

### Quote di luar daftar tetap (auto-deteksi)

Banyak memecoin sama sekali tidak punya pool ke WBNB/USDT/USDC — pasangannya token
lain (kasus nyata: RTX cuma ada di pair RTX/NVDAB). `discover_foreign_pools()`
menangkap pool semacam itu: kandidat dari DexScreener, pool diverifikasi ke factory
on-chain, lalu token lawannya didaftarkan sebagai quote runtime via `register_quote()`
sehingga seluruh kode lama yang memanggil `quote_usd_price(quote_sym)` tetap jalan.

Aturan yang gampang dilanggar kalau tidak hati-hati:

- **Syarat jadi quote bukan "punya harga"** melainkan `quote_backing_usd() > 0` —
  likuiditas on-chain terhadap quote tetap. `token_usd_price()` punya fallback
  DexScreener sehingga token sampah pun "punya harga"; kalau dipakai sebagai syarat,
  sisi quote bisa salah pilih (pernah kejadian: RTX terpilih jadi quote atas NVDAB).
- **`register_quote()` tidak boleh menimpa simbol quote resmi.** Token bisa mengaku
  bernama "USDT"; simbol bentrok disambiguasi jadi `SYM~abcd`.
- **Sisi quote posisi ditentukan `resolve_quote_side()`**, dipakai `_position_detail`
  (v3) dan `_v2_position_detail` (v2). Sebelum ada ini, posisi ber-quote asing tampil
  bernilai 0 di v3 dan **hilang sama sekali** dari `/list` di v2.
- **Rute swap harus lewat `swap_any()`/`swap_route()`**, bukan `swap_to_token()`
  langsung: quote auto-deteksi lazimnya tidak punya pool langsung ke wrapped (NVDAB
  cuma berpasangan dengan USDT), jadi perlu 2-hop. `reduce_v2` bahkan butuh tiga
  lapis (v2 langsung → rute v3 → jual ke sisi lawan) dan **urutan sisi** yang diproses
  penting: sisi tanpa rute dikonversi dulu ke sisi lawannya.
- `discover_foreign_pools` dijalankan **berurutan** setelah scan utama. Pernah dicoba
  paralel: di RPC publik keduanya berebut dan malah kena rate-limit (19s vs 10s).

### Filter harga menyimpang

`_drop_offprice_pools()` membuang pool yang harganya lewat `PRICE_DEVIATION_MAX`
(25%) dari pool **terdalam** token itu — patokannya TVL terbesar, bukan angka
mutlak. Pool debu bisa berharga 2× pasar justru karena tak terarbitrase (untungnya
lebih kecil dari gas); LP di situ = modal user yang dipakai menyeret harganya balik.

Dua jebakan yang sudah kena sekali:

- **Harga harus milik token yang DICARI.** Sisi non-quote tidak selalu token itu:
  kalau yang dicari justru jadi sisi quote (mis. mencari USDG), pool itu menghargai
  token lain dan angkanya tak sebanding. `_pool_price_usd` mengembalikan `None`
  untuk kasus itu. Tanpa penjagaan ini, pool v4 USDG ber-TVL $1,6jt ikut terbuang.
- **Jalur indexer Uniswap tidak mengirim `sqrtPrice`**, jadi harganya tak terhitung
  dan pool lolos tanpa dicek. `_fill_missing_sqrtp()` mengisinya on-chain, tapi
  **dibatasi 12 pool ber-TVL teratas** — membaca slot0 untuk ratusan pool terlalu mahal.

### Sumber angka TVL

Urutan daftar pool DAN patokan filter harga sama-sama bergantung TVL, jadi angkanya
harus sedekat mungkin ke kenyataan:

- v2 & v3 hasil scan RPC: dari saldo nyata kedua sisi di kontrak pool.
- v3 dari indexer Uniswap: `totalLiquidityUsd` bisa meleset jauh (terukur $24,4k
  untuk pool yang saldonya $40,7k), jadi `_fill_onchain_tvl()` menghitung ulang dari
  `balanceOf` — dibatasi 12 pool teratas, ditandai `tvl_src="chain"`.
- v4: saldo per-pool TIDAK bisa dibaca (semua currency ditahan satu PoolManager),
  jadi dipakai likuiditas dexscreener kalau ada; sisanya angka indexer apa adanya.

### Ambang TVL

Tidak ada lagi lantai TVL dolar di discovery — pool kecil tetap ditampilkan dan
ditandai `thin` (<$50) supaya UI memperingatkan. Yang membuang pool tetap ada dan
jangan dilemahkan: probe round-trip (pool dust/harga dimanipulasi), verifikasi
factory, dan syarat reserve/sisi quote tidak nol. Pool dengan reserve benar-benar nol
tetap dibuang: harganya belum ada, deposit pertama yang menentukannya.

Invarian yang berkali-kali jadi sumber bug (lihat riwayat commit): **indexer Uniswap bisa
telat berjam-jam**, jadi jalur indexer selalu di-union dengan enumerasi NFT terbaru
on-chain, dan detail posisi selalu dibaca on-chain. Jangan pernah menjadikan angka
indexer otoritatif untuk membangun transaksi — `assert_pool_orientation()` dan
`verify_router` / `verify_v2_router` / `verify_v4` mem-verifikasi silang alamat kontrak
on-chain sebelum dana bergerak (fail-closed).

### Range selalu dihitung di server

Browser hanya mengirim *persen* lebar range; tick final tetap dari `calc_strategy_range()`
— fungsi yang sama dipakai bot Telegram. Demikian pula **alamat pool tidak pernah datang
dari browser**: hasil discovery disimpan di `_POOLS` (web.py) dan klien cuma memegang
key-nya. Pertahankan properti ini saat menambah endpoint.

### Serialisasi transaksi & eksekutor tunggal

Masing-masing proses punya lock nonce sendiri (`TX_LOCK`: `asyncio.Lock` di bot.py,
`threading.Lock` di web.py). Untuk order TP/SL, **hanya `monitor_loop` di bot.py yang
mengeksekusi close**; web cuma membuat/membatalkan order di `history.json`. Menambahkan
eksekusi di web akan menciptakan dua penulis nonce untuk satu wallet.

### Performa: request HTTP tidak boleh memblokir pada RPC

`api_positions` menyajikan cache pendek dan me-refresh di latar (single-flight,
`_POS_REFRESHING`), dua tahap saat cold (nilai/range dulu, PnL yang butuh `getLogs`
menyusul). `chain.py` menyimpan cache berumur di `_cache={}` default-arg untuk hal
immutable (`pool_addr_of`, `token_supply`, hasil verifikasi kontrak) dan `.active_positions.json`
untuk set tokenId aktif. RPC free-tier gampang kena 429 — hindari menambah panggilan
per-posisi di jalur refresh.

### Jaringan

`get_w3()` melakukan failover multi-endpoint (Alchemy → RPC publik) dengan retry+backoff.
Ada bypass khusus blokir DNS ISP Indonesia: resolve via DNS-over-HTTPS lalu konek ke IP
langsung dengan SNI dipertahankan (`_SNIAdapter`, `_forced_ip_w3`) — sertifikat tetap
diverifikasi, jangan dilonggarkan.

## Batasan yang disengaja

- Pool v4 **ber-hooks dilewati** (hook = kode arbitrer, risiko rug).
- **v4 mati di BSC**: PancakeSwap tidak punya kontrak kompatibel-v4; padanannya
  "Infinity" (Vault + CLPoolManager) arsitekturnya beda total dan belum didukung.
  `has_v4(56)` False, dan `verify_v4`/`discover_v4_pools` fail-closed tanpa key `v4_*`.
- Token fee-on-transfer tidak didukung di jalur v2.
- Fee v2 auto-compound → tidak ada aksi "collect" untuk v2.
- `WEB_HOST` non-localhost menolak start tanpa `WEB_TOKEN`; jangan sarankan `0.0.0.0`.
