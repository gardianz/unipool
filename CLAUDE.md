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
- **`store.py`** — state JSON: settings, event PnL, registry posisi v2/v4, order TP/SL,
  brankas wallet (`wallets.json`, ditulis mode 0600 lewat `_write_secret()`).
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

### Chain yang didukung

| chain | DEX | v4 | indexer Uniswap | quote |
|---|---|---|---|---|
| Robinhood 4663 | Uniswap | ya | ya | WETH, USDG |
| BSC 56 | PancakeSwap + Uniswap | tidak | tidak | WBNB, USDT, USDC |
| Base 8453 | Uniswap | ya | ya | WETH, USDC |
| HyperEVM 999 | HyperSwap | tidak | tidak | WHYPE, USDC |

Semua alamat di dua entri baru diverifikasi on-chain sebelum dipakai:
`npm.factory()`, `npm.WETH9()`, `router.factory()`, `router.WETH9()`,
`v2_router.factory()`, `v2_router.WETH()`, dan untuk Base
`v4_posm/stateview/quoter.poolManager() == v4_pm` + `posm.permit2()` canonical.
`fee_tiers` dibaca dari `factory.feeAmountTickSpacing()`, bukan ditebak.

**HyperEVM ramai fork Solidly/Ramses** — nest, kittenswap, ramses, hybra memakai fee
bebas (terukur 858, 602, 1105, 22222) dan antarmuka factory yang beda, jadi TIDAK
didukung dan pool-nya otomatis terbuang oleh verifikasi factory di
`discover_dex_pools`. Yang didukung HyperSwap (fork Uniswap v3 lurus, tier standar).
**prjx** TVL-nya terbesar (terukur $18,5jt vs HyperSwap $743k) dan fee-nya standar —
layak jadi DEX kedua di `CHAINS[999]["dexes"]`, tapi alamat NPM/router-nya belum
diverifikasi on-chain jadi sengaja belum dimasukkan.

Alchemy mendukung kedua chain (`base-mainnet`, `hyperliquid-mainnet`) tapi tiap
network harus **di-enable per app** di dashboard — kalau tidak, jawabannya 403
"not enabled for this app" dan `get_w3` jatuh ke RPC publik.

### Dispatch versi pool

Satu dict "pool_info" dipakai lintas seluruh kode dengan field `ver` (2/3/4) plus
`pool`, `fee`, `quote_addr`, `quote_sym`, `quote_is_token1`, `token0/1`, `tick_spacing`.
Pool v4 juga membawa `pool_id` dan `key` (PoolKey tuple).

`pool_info["pool"]` itu **alamat kontrak** untuk v2/v3 tapi **poolId 32-byte** untuk v4.
Jangan pernah men-`to_checksum_address()` nilai itu tanpa cek `ver` — pernah bikin mode
Upper mati di semua pool v4 (`Unknown format '0x…'`). Untuk v4 pakai `pool_id` +
`v4_slot0()`; desimal currency pakai `_v4_currency_info()` karena sisi ETH native
(address(0)) tidak punya kontrak ERC20.

Posisi diidentifikasi oleh **`pid`**: `"183469"` = v3, `"v4:12"` = v4, `"v2:0xpair"` = v2
(`parse_pid()`). Aksi generik lewat `add_any` / `reduce_any` / `collect_any` / `close_any` /
`rebalance_position` — jangan panggil varian per-versi langsung dari UI.

### minOut swap: dari quoter/fee, JANGAN dari harga spot

Harga spot tidak memotong fee pool. Di pool ber-fee besar, slippage user habis
dimakan fee sehingga swap **pasti** revert: pool fee 5% + slippage 5% → minOut
mendarat ~0,16% di atas hasil nyata (terbukti `V4TooLittleReceived`: minta
1.851,17 BULL, dapat 1.848,24).

- v4: `v4_swap()` memakai `quoteExactInputSingle` dari v4 quoter — hasilnya sudah
  memperhitungkan fee DAN price impact. Fee dinamis (`fee >= 0x800000`) tidak punya
  nilai statis, jadi kalau quoter gagal fee dianggap 0 dan slippage user yang menahan.
- v3: `swap_to_token()` mengalikan estimasi spot dengan `(1 − fee/1e6)`.

### Pemilihan rute swap: bukan fee terendah, tapi biaya terendah

`find_pool_dex(..., amount_in_wei)` memberi skor tiap pool
`(1 − fee) × kedalaman/(kedalaman + jumlah)`. Fee terendah saja SALAH sebagai
patokan: pool 0,01% ber-kedalaman 0,18 WETH kalah telak dari pool 0,30%
ber-kedalaman 221 begitu swap-nya ≥0,1 WETH — price impact menelan selisih fee.
Tanpa `amount_in_wei` (mis. saat cuma membaca harga) fungsi ini tetap memilih pool
terdalam seperti sebelumnya.

Skor ini pendekatan constant-product, jadi perkiraan — bukan hasil quoter. Cukup
untuk memilih rute, jangan dipakai sebagai angka yang ditampilkan ke user.

**Saldo bukan bukti pool hidup.** Kandidat wajib lolos `liquidity() > 0`, bukan cuma
`balanceOf > 0`. Pool berdebu (terukur **53 wei**) dengan likuiditas aktif 0 tetap
lolos filter saldo, menang sebagai rute *langsung*, lalu setiap swap revert `0x`
tanpa alasan — dan `swap_route` tidak pernah mencoba 2-hop yang sebenarnya jalan.
Kasus nyata: WETH/MSFT fee 3000 di Robinhood mematikan mint pool ber-quote MSFT,
padahal WETH→USDG→MSFT hidup. Efek sampingnya juga menyeret `wrapped_per_quote_wei()`
membaca harga dari pool mati itu.

### Modal gabungan wajib bisa diambil, bukan cuma dihitung

`compute_amount` menghitung ETH + WETH + quote lain sebagai modal, jadi kedua jalur
pengambilannya harus ikut:

- `ensure_quote_balance()` menjual quote lain **langsung** ke quote target dulu
  (USDG→MSFT = 1 hop) sebelum jalur wrapped (wrap + 2 hop, fee/slippage dobel dan
  gagal kalau ETH-nya kurang padahal USDG menumpuk).
- `ensure_native_balance()` mengunwrap **lebih** dari kekurangan sebesar cadangan gas:
  tx unwrap/swap-nya sendiri membakar native, jadi unwrap pas-pasan selalu mendarat
  kurang persis sebesar gas itu (terukur: "punya 0.130946, butuh 0.130789 + gas").
  Sisa kekurangan selalu dihitung ulang dari saldo NYATA, bukan dikurangi angka rencana.

### 'STF' pada swap = jumlah melebihi saldo, bukan pool bermasalah

`TransferHelper.safeTransferFrom` di router v3 balas `'STF'` — revert yang tidak
menyebut token, jumlah, maupun sebabnya. Terbukti di BSC dengan allowance MAX:
swap sebesar saldo LOLOS simulasi, swap 10× saldo memberi `execution reverted: STF`
yang persis sama. Jadi jangan cari-cari masalah di pool.

`swap_to_token()` karena itu memangkas `amount_in_wei` ke saldo NYATA (dan menurunkan
`min_out` proporsional — kalau tidak, swap revert karena minOut kekinggian), lalu
kalau tetap STF ia menyetel ulang approval sekali dan menyimulasikan lagi. Selisih
tipis lazim: jumlahnya dihitung dari saldo yang dibaca sepersekian detik lebih awal,
atau tokennya fee-on-transfer.

### Auto-swap saat close cuma menjual HASIL close

`close_position`/`close_v4`/`reduce_v2` memotret saldo kedua sisi sebelum eksekusi,
lalu auto-swap hanya menjual selisihnya. Dulu ketiganya menjual **seluruh saldo**
token itu di wallet — token yang user pegang untuk keperluan lain ikut terjual.
Kalau selisihnya tak terbaca (RPC lag), lebih baik lewati daripada menebak: saldo
lama tidak boleh disentuh.

### Add v4 memakai fee unclaimed sebagai modal

`INCREASE_LIQUIDITY` v4 mengkreditkan `feesAccrued` terhadap tagihan `SETTLE_PAIR`:
wallet cuma membayar **selisihnya**, tapi likuiditas bertambah sebesar penuh. Terukur
di tx `0x443a4846…`: bot melaporkan 412,523 USDG + 185.967 POOLS masuk, yang benar-benar
keluar dari wallet 398,769 USDG + 167.222 POOLS — selisihnya persis fee unclaimed.

`added_usd` menghitung yang penuh (v4 dari `amounts_from_liquidity`, v3 dari event
`IncreaseLiquidity`), jadi jalur add **wajib** mencatat event `fees` penyeimbang
(`_reinvested_fee_usd()` di bot, `ver == 4` di `api_action`). Tanpa itu fee tercatat
sebagai setoran baru dan PnL rugi palsu sebesar fee tersebut.

Beda per versi, jangan disamaratakan:

- **v4 add** — fee terpakai jadi modal, unclaimed reset ke $0. Perlu event `fees`.
- **v3 add** — `increaseLiquidity` membiarkan fee mengendap di `tokensOwed`, tetap
  unclaimed. **Jangan** catat event `fees` (nanti dobel saat benar-benar diklaim).
- **v2** — tidak punya fee unclaimed (auto-compound).
- **reduce/close** (v3 `decrease+collect`, v4 `DECREASE`+`TAKE_PAIR`) — fee ditarik
  **penuh ke wallet** berapa pun pct-nya, jadi event `fees` dicatat 100%, bukan pro-rata.

### Pembukuan rebalance tidak boleh bolong

`do_rebalance` (bot) dan `api_action` (web) memotret posisi lama SEBELUM eksekusi
untuk mencatat event `close` + `fees`. Kalau snapshot gagal dibaca (RPC lag), dulu
tidak ada event close sama sekali padahal event `mint` posisi baru tetap dicatat —
deposit lama menggantung "masih terbuka" dan PnL portfolio menggelembung palsu.

Sekarang `rebalance_position()` mengembalikan `closed_usd` (nilai yang benar-benar
keluar dari posisi, dihitung dari delta saldo SEBELUM swap komposisi) dan dipakai
sebagai cadangan. `closed_usd` sudah termasuk fee — jadi di jalur cadangan itu
**jangan** menambah event `fees` lagi, nanti dobel.

### Keterangan pool di kartu posisi

`pool_stats(w3, cid, pos)` memberi TVL, volume 24 jam, fee tier, dan tick spacing
satu pool. Dipakai `_pool_info_line()` di kartu detail **satu** posisi saja —
JANGAN dipanggil dari `/list`: TVL v4 butuh StateView dan volume butuh dexscreener,
jadi biayanya per-posisi. Cache 60 detik.

Sumber TVL beda per versi, sengaja:

- v2/v3 — saldo NYATA kedua sisi di kontrak pool (`balanceOf` × harga USD).
- v4 — dexscreener dulu, baru reserve virtual dari StateView (`liquidity` × harga
  × 2) yang ditandai *(perkiraan)* di UI, karena saldo per-pool v4 tidak bisa dibaca.

Semua angkanya **tampilan saja**, jangan dipakai membangun transaksi.

`_v4_position_detail` wajib mengembalikan `tick_spacing` (`key[3]`): fee v4 bebas
(58200, 39966, …) sehingga tabel `TICK_SPACING` tidak memuatnya dan `box_pct()`
jatuh ke default 60 — presisi kisi yang ditampilkan jadi salah (terukur: kisi asli
5,99% dilaporkan 0,60%).

### Posisi v3 yang sudah di-decrease tapi belum di-collect

Uniswap v3 memindahkan **pokok** ke `tokensOwed` saat `decreaseLiquidity` — field
yang SAMA dengan fee. Kalau `collect` gagal/tidak jalan, posisi tertinggal dengan
`liquidity == 0` dan `tokensOwed > 0`, dan angka "unclaimed" itu **pokok + fee**,
bukan fee saja.

`_position_detail` menandainya `pending_claim`. UI wajib memakainya: kartu lama
menulis "Nilai $0,00 / Fee unclaimed $472,32" dan user mengira modalnya lenyap
(kejadian nyata #757291 — $472 aman di dalam posisi selama berjam-jam). Kartu
konfirmasi close juga menyebut `value_usd + unclaimed_usd`, bukan `value_usd` saja.

`close_position` sudah benar: `if liq > 0` melewati decrease dan langsung collect.
Uniswap UI **menyembunyikan** posisi seperti ini (likuiditasnya nol), jadi jangan
menyarankan user mengambilnya dari sana.

### NFT posisi kosong menumpuk

Close tidak mem-burn NFT-nya. Terukur di satu wallet: **108 NFT v3 untuk 1 posisi
hidup**, dan tiap refresh daftar membayar satu `positions()` per NFT (10,5 detik
sekali pindai penuh). `_is_active()` sudah menyaringnya dari tampilan; yang tersisa
ongkos enumerasinya.

`/cleanup` → `burn_empty()` membakarnya lewat `multicall`, 25 per tx (terukur 2,12
juta gas ≈ 0,000045 ETH). AMAN tanpa syarat: `burn` di NPM me-require liquidity DAN
tokensOwed dua-duanya nol — posisi berisi ditolak kontraknya sendiri dengan
`execution reverted: Not cleared` (sudah diuji terhadap posisi hidup).

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
- **v4 lewat `_v4_quote_side(..., w3)`** — argumen `w3` itu yang menyalakan fallback
  ke `resolve_quote_side`. Tanpa `w3` fungsi ini cuma mengenal quote tetap + native,
  dan posisi v4 ber-quote asing rusak bertiga sekaligus: nilai **$0,00**, range tampil
  sebagai harga mentah bukan market cap (blok `if qsym:` yang menghitung `mc_*` ikut
  dilewati), dan setiap aksi ditolak *"Pair tanpa quote yang dikenal bot"* (kasus
  nyata: PACK/NVDA #645408). Jalur **discovery sengaja tidak mengirim `w3`** —
  `resolve_quote_side` membaca sokongan likuiditas kedua sisi (terukur ~20 detik),
  terlalu mahal per pool hasil indexer; itu tugas `discover_foreign_pools()`.
  Quote runtime yang sudah terdaftar dicek dari `_EXTRA_QUOTES` dulu supaya refresh
  daftar posisi tidak membayar ongkos itu berulang (20s → 1,6s).
- **Quote asing jangan dihargai sebagai wrapped.** Dua tempat di jalur v4 dulu memakai
  `qsym if qsym in cfg["quotes"] … else cfg["wrapped_symbol"]` — quote asing pun
  dihargai memakai harga ETH. `quote_usd_price()` sudah menangani quote runtime lewat
  `_EXTRA_QUOTES`, jadi cukup `quote_usd_price(w3, chain_id, qsym)` polos.
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
- v4: saldo per-pool TIDAK bisa dibaca (semua currency ditahan satu PoolManager).
  Urutan sumbernya: **Krystal** (`krystal_pools()`, endpoint yang dipakai web mereka
  sendiri) → dexscreener → `_fill_v4_tvl()` yang menghitung reserve virtual dari
  `liquidity` × harga lewat StateView. Angka indexer tidak dipakai lagi: terukur
  $43,9k untuk pool yang nyatanya ~$3k.

**Endpoint Krystal**: `GET api.krystal.app/all/v2/lp_explorer/top_pools?chainId=&tokenAddress=`
— **v2**, yang v1 cuma melayani Solana (menjawab "chain id 56 not supported"), dan
paramnya `tokenAddress` bukan `search`. Tidak terdokumentasi di swagger publik;
ditemukan dari bundel JS defi.krystal.app. Fragile — semua pemanggilnya harus tetap
jalan kalau API-nya mati, dan angkanya tidak pernah jadi dasar membangun transaksi.

### Krystal sebagai sumber utama daftar pool

`discover_any()` mencoba `discover_krystal()` DULU (<1 detik, angkanya sama dengan yang
dilihat user di web Krystal). Kalau token itu ada di Krystal, daftar yang ditampilkan =
daftar Krystal, titik — tidak disaring ulang oleh `_drop_dead_pools`/`_drop_offprice_pools`
(daftar mereka sudah tersaring ≥$1K TVL). Harga menyimpang cuma DITANDAI
(`p["deviation"]`), tidak dibuang.

Kalau Krystal tidak punya token itu (pair aneh: RTX/NVDAB, HOUSE/BTCB) barulah discovery
sendiri jalan — dengan seluruh saringannya. `res["source"]` menyebut jalur mana yang dipakai.

Dua jebakan yang sudah kena di jalur ini:

- **Sentinel ETH native beda.** Krystal memakai `0xEeee…Eeee`, Uniswap v4 memakai
  `address(0)`. Tanpa `_norm_currency()`, PoolKey tak pernah menghasilkan poolId yang
  sama dan **semua** pool ber-quote ETH native terbuang — di FRONG itu berarti pool
  $618k hilang dari daftar.
- **Jangan menebak fee/spacing kalau ada sumber eksak.** Urutannya
  `_v4_key_from_indexer()` (indexer Uniswap mengirim fee DAN tickSpacing) →
  `_v4_key_from_krystal()` (tebak spacing dari pola fee/50) → `_v4_key_from_init()`
  (log, sering ditolak RPC publik). Semuanya tetap dibuktikan lewat hash.

Tetap berlaku: data Krystal **tidak pernah** otoritatif untuk transaksi. Tiap pool
diverifikasi on-chain di `discover_krystal()` — v2/v3 dicek ke factory DEX-nya,
v4 lewat `_v4_key_from_krystal()` yang menyusun PoolKey lalu membuktikannya dengan
hash (`v4_pool_id(key) == poolId`). Krystal tidak mengirim `tickSpacing`, jadi nilainya
dicoba dari pola **spacing ≈ fee/50** (fee 40000→800, 18888→378) + tier klasik;
yang diterima hanya yang hash-nya cocok.

### Saringan pool yang ditampilkan (jalur discovery sendiri)

Berlapis, urutannya penting (semua di ujung `discover_any()`):

1. `_drop_dead_pools()` — wajib punya TVL **dan** volume 24 jam. Pool ber-quote aneh
   TIDAK dibuang selama ada volume (justru itu yang dicari); yang hilang adalah ekor
   mati dari indexer (token memes: 78 pool → 16). **Katup pengaman**: volume kosong
   sering cuma tidak terindeks, jadi pool tanpa volume tetap ditampilkan kalau TVL-nya
   ≥5% pool terdalam — tanpa itu pool Uniswap v4 CAKE/USDT ber-TVL $922k ikut terbuang.
2. `_drop_offprice_pools()` — dijalankan SESUDAH (1) supaya patokan harganya diambil
   dari pool yang benar-benar diperdagangkan.
3. Urut TVL menurun.

Jumlah yang disembunyikan selalu disebut di UI (bot & web), jangan sampai pool hilang
diam-diam.

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

### Tx hilang dari mempool, bukan kurang gas

Terukur di kedua chain, tx yang dikirim bot **tidak** underpriced:

| | base fee | floor tx termine | dikirim `send_tx` |
|---|---|---|---|
| Robinhood 4663 | 0,02 gwei (rata) | — | cap 0,14 gwei, tip 0,1 |
| BSC 56 | 0 | min 0,05 · median 0,066 gwei | cap 0,20 gwei, tip 0,1 |

Jadi kalau tx tidak masuk blok, sebabnya tx dibuang / tidak dipropagasikan node.
Menaikkan gas tidak menolong; yang menolong **siar ulang berkala + lewat endpoint
lain**.

`wait_ok()` menunggu dalam potongan ~20 detik dan menyiarkan ulang raw tx yang sama
tiap potongan (nonce & tanda tangan identik → mustahil dobel), total 180 detik.
Sebelumnya siar ulangnya cuma sekali di detik ke-90.

Tx disebar ke endpoint lain **sejak dikirim**, bukan menunggu ronde siar ulang
pertama: `send_tx()` memanggil `_fanout_async()` (thread daemon, balik dalam ~2 ms).
Terbukti perlu di BSC — tx wrap `0xdde477e4…` diterima `bsc-dataseed` dengan hash
normal lalu tidak pernah dipropagasikan; 180 detik kemudian tx itu tidak dikenal
node mana pun, bukan sekadar belum di-mine.

Daftar RPC BSC diperluas ke 5 endpoint yang semuanya diverifikasi `eth_chainId == 56`
**dan** mendukung `eth_sendRawTransaction`. `rpc.48.club` ditaruh pertama: dioperasikan
operator validator BSC, jadi tx masuk jalur langsung ke pemilih blok. `1rpc.io/bnb`
dibuang — jawabannya bukan JSON yang sah.

`_rebroadcast()` menyebar ke node aktif **dan** semua endpoint lain di `CHAINS`.
Dua hal yang membuat ini murah, jangan dibalik:

- **Sesi peer tanpa retry** (`_peer_session`, timeout 4 detik). `_rpc_session()`
  memakai `Retry(total=6, backoff 0.6→9.6s)` — untuk endpoint mati itu ~40 detik per
  request, dan sempat terukur **116 detik** hanya untuk membangun daftar peer 4663.
  Dengan sesi cepat, satu ronde siar ulang ke semua endpoint = 4–6 detik.
- **Peer tidak diprobe.** Blockscout eth-rpc Robinhood menjawab **429** untuk
  `eth_chainId` (rate limit, bukan mati) — probe apa pun akan membuangnya, padahal
  satu tx per 20 detik masih lolos. Endpoint yang benar-benar mati gagal murah
  (0,06 detik untuk host yang diblokir DNS ISP).

Kalau `wait_ok` menyerah, `_NONCE_NEXT`/`_LAST_TX` WAJIB di-reset — tanpa itu tx
berikutnya lahir dengan lubang nonce dan ikut mati satu per satu.

### Baca hasil tx: tunggu sisi yang benar-benar berubah

`rebalance_position` dulu cuma mem-`poll_balance` sisi **meme**. Posisi single-sided
(mode Lower) pulang 100% **quote**, jadi `got_m == 0` dan tidak ada penungguan sama
sekali — replika RPC yang telat menjawab saldo pra-close bikin kedua delta 0 dan
rebalance batal *"Hasil close terbaca 0 (RPC lag)"* padahal close-nya sukses.
Sekarang kedua sisi ditunggu (`_poll_wallet` sadar-native untuk currency v4
`address(0)`) dan pembacaan delta diulang 8× sebelum menyerah.

### Laporan langkah alur tx

Satu mint/close/rebalance itu 3–5 tx berurutan; dengan `wait_ok` menunggu sampai 180
detik per tx, totalnya bisa menit-menit. `chain.set_progress(fn)` memasang sink dan
`_step()` melaporkan tiap tahap dari `wait_ok` (terkirim / disiarkan ulang + detik
berjalan / beres), jadi otomatis mencakup SEMUA alur tanpa menyentuh tiap fungsi.

Sink itu global — aman karena semua alur tx diserialisasi `TX_LOCK` per proses.
`_step()` dipanggil dari thread kerja, jadi ia hanya boleh menumpuk teks; di bot,
`with_progress()` yang mengedit pesan Telegram dari sisi async (ticker 5 detik, 5
baris terakhir) dan WAJIB melepas sink di `finally`.

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

### PoA: `get_block` di BSC

BSC memakai extraData 280 byte, jauh di atas 32 byte yang divalidasi web3, jadi
`eth_getBlock` **selalu** melempar `ExtraDataLengthError` tanpa middleware PoA —
`price_history()` (chart) dan pembacaan timestamp blok di web.py mati diam-diam di
chain itu. `_poa()` dipasang di `get_w3()` dan `_forced_ip_w3()`; aman untuk chain
non-PoA karena middleware-nya cuma memangkas extraData yang kepanjangan.

### Jaringan

`get_w3()` melakukan failover multi-endpoint (Alchemy → RPC publik) dengan retry+backoff.
Ada bypass khusus blokir DNS ISP Indonesia: resolve via DNS-over-HTTPS lalu konek ke IP
langsung dengan SNI dipertahankan (`_SNIAdapter`, `_forced_ip_w3`) — sertifikat tetap
diverifikasi, jangan dilonggarkan.

### Wallet: .env + brankas

`all_pks()` = wallet `.env` (urutannya TETAP, supaya arti "W1" tidak bergeser) lalu
wallet dari `store.wallets()`. Sengaja TIDAK di-cache — brankas berubah saat runtime.
`env_pks()` yang di-cache. Wallet `.env` tidak bisa dihapus lewat bot (`is_env_pk()`).

Private key lewat chat itu permanen di riwayat Telegram, jadi: pesan impor dihapus
begitu dibaca, hasil ekspor dihapus otomatis 60 detik (`_autodelete()`), dan ekspor
maupun hapus selalu dua langkah dengan peringatan. Jangan hilangkan penjagaan itu.

## Batasan yang disengaja

- Pool v4 **ber-hooks dilewati** (hook = kode arbitrer, risiko rug). Jumlahnya
  DISEBUTKAN di UI lewat `count_hook_pools()` — dulu pool semacam itu hilang diam-diam
  dan dikira bug (kasus nyata: RUBY/RDDT ber-TVL $40k, hook `0x778b0c4e…`).
- **v4 mati di BSC**: PancakeSwap tidak punya kontrak kompatibel-v4; padanannya
  "Infinity" (Vault + CLPoolManager) arsitekturnya beda total dan belum didukung.
  `has_v4(56)` False, dan `verify_v4`/`discover_v4_pools` fail-closed tanpa key `v4_*`.
- **Modal = semua quote yang bisa ditukar**, bukan cuma quote pool. `compute_amount`
  menambahkan `other_quote_capital()` (saldo quote lain dikonversi lewat harga USD,
  dipotong margin 3% untuk fee+slippage). Eksekusinya WAJIB ikut: untuk pool
  ber-quote native, `ensure_native_balance()` meng-unwrap WETH lalu menjual quote
  lain (USDG) seperlunya. Kalau modal dihitung tanpa jalur pengambilannya, mint
  gagal di tengah setelah beberapa tx terlanjur jalan.
- Token fee-on-transfer tidak didukung di jalur v2.
- Fee v2 auto-compound → tidak ada aksi "collect" untuk v2.
- `WEB_HOST` non-localhost menolak start tanpa `WEB_TOKEN`; jangan sarankan `0.0.0.0`.
