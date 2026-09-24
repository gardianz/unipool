# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Bahasa

Komentar kode, string yang dilihat user (Telegram/web), dan pesan commit di repo ini
memakai **bahasa Indonesia**. Ikuti gaya itu untuk perubahan baru.

## Menjalankan

```bash
pip install -r requirements.txt
cp .env.example .env      # isi TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PRIVATE_KEY

npm install --prefix meteora   # sidecar Meteora DLMM (hanya perlu untuk Solana)

python3 bot.py            # bot Telegram (long polling)
python3 web.py            # UI web → http://127.0.0.1:8899
```

Tidak ada test suite, linter, atau build step di repo ini — verifikasi dilakukan manual
terhadap chain live. Pemeriksaan termurah setelah edit:
`python3 -m py_compile chain.py bot.py web.py sol.py && node --check meteora/dlmm.cjs`.

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
- **`sol.py`** — mesin **Solana** (hanya Meteora DLMM). Kedudukannya sama dengan
  `chain.py` untuk chain EVM. `chain.py` merutekan ke sini lewat
  `is_solana(chain_id)`; `get_w3()` MENOLAK chain itu.
- **`meteora/dlmm.cjs`** — sidecar Node (SDK resmi `@meteora-ag/dlmm`), satu
  request JSON di stdin → satu response di stdout. Dipasang dengan
  `npm install --prefix meteora`.
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

| chain | DEX | v4 | indexer Uniswap | quote | gas |
|---|---|---|---|---|---|
| Robinhood 4663 | Uniswap | ya | ya | WETH, USDG | ETH |
| BSC 56 | PancakeSwap + Uniswap | tidak | tidak | WBNB, USDT, USDC | BNB |
| Base 8453 | Uniswap | ya | ya | WETH, USDC | ETH |
| HyperEVM 999 | HyperSwap | tidak | tidak | WHYPE, USDC | HYPE |
| Arc 5042 | Uniswap | ya | ya | USDC | **USDC** |
| Solana 1399811149 | **Meteora DLMM** | — | tidak | SOL, USDC, USDT | SOL |

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

Alchemy mendukung ketiganya (`base-mainnet`, `hyperliquid-mainnet`, `arc-mainnet`)
tapi tiap network harus **di-enable per app** di dashboard — kalau tidak, jawabannya
403 "not enabled for this app" dan `get_w3` jatuh ke RPC publik. Terukur saat Arc
ditambahkan: dari dua key user, `…ei1f` sehat di Arc dan `…Ev8C` menjawab 403.
`/rpc all` memperlihatkannya per endpoint.

### Arc 5042: gas token-nya USDC, dan USDC itu ERC20 yang SAMA

Ini satu-satunya chain di repo yang native-nya bukan coin terpisah, dan salah
paham di sini langsung berarti salah hitung uang.

- **Native = USDC, 18 desimal** di level EVM, sekaligus punya wajah **ERC20 6
  desimal** di `0x3600…0000`. Satu kantong, dua tampilan — terverifikasi di blok
  terpin: `eth_getBalance(addr) // 1e12 == USDC.balanceOf(addr)` PERSIS, untuk
  EOA maupun kontrak. (Awalnya terlihat meleset 42,6 USDC di PoolManager; itu cuma
  karena dua panggilan mengenai blok berbeda — chain-nya 0,52 detik per blok.)
- **`native_erc20` di CHAINS menyatakannya**, dan `native_family()` mengelompokkan
  `address(0)` + wrapped + native_erc20 jadi satu kantong. Aturannya: **saldo
  native TIDAK PERNAH dihitung lagi sebagai modal terpisah** — ia selalu masuk
  lewat wajah ERC20-nya.

  Tanpa itu `compute_amount` menghitung uang yang sama dua kali: untuk pool
  ber-quote USDC, `balanceOf` memberi 11,83 lalu cabang wrapped menambah saldo
  native yang sama lagi lewat `wrapped_per_quote_wei` (WUSDC $1 ÷ USDC $1 ×
  10^12) — "50% saldo" jadi ~99% dan mint gagal di tengah. Sesudah diperbaiki,
  terukur di wallet berisi 11,827631 USDC: 100% → **11,767631** (= saldo −
  cadangan gas 0,06) dan 50% → **5,883815**. Cadangan gas WAJIB dipotong di sini:
  gas dibayar dari kantong yang sama.

  Tiga jalur lain ikut disadarkan, dan semuanya soal tx bukan tampilan:
  `other_quote_capital` (jangan jumlahkan sesama keluarga native),
  `ensure_native_balance` (menjual USDC untuk "menambah native" itu menjual native
  itu sendiri — nol hasilnya, gasnya tetap terbakar), dan `ensure_quote_balance`
  (quote == native_erc20 → tidak ada yang bisa di-unwrap/ditukar, jadi menolak
  dengan pesan jelas, bukan membeli WUSDC dari uang yang sama lalu gagal).
- **WETH9 yang tertanam di periphery Uniswap Arc SELALU REVERT.**
  `npm.WETH9()`, `router.WETH9()`, `v2_router.WETH()` dan immutable UniversalRouter
  semuanya menunjuk `0x8bceaa40…` — kontrak **53 byte** yang isinya cuma
  `revert(0xea3559ef)`. Jadi seluruh jalur ETH-native periphery mati di chain ini.
  Alamat itu tetap ditulis di CHAINS sebagai `v2_weth` supaya
  `verify_v2_router` tetap **fail-closed** (ia membandingkan
  `cfg.get("v2_weth", cfg["wrapped"])`), bukan dilonggarkan.
- **`wrapped` = WUSDC `0xe6b0a06c…`** — wrapped native yang benar-benar jalan
  (`deposit`/`withdraw` ada; saldo native kontraknya == totalSupply persis).
  Praktis tidak terpakai (supply terukur 0,267) karena native sudah punya wajah
  ERC20 sendiri. `wrapped_symbol` "WUSDC" dimasukkan ke `stable_syms` supaya
  harganya $1 tanpa perlu pool — dan karena itu `quote_usd_price` sekarang memakai
  `cfg["quotes"].get(sym)`: stable_syms boleh memuat simbol yang BUKAN quote, dan
  tanpa `.get()` loop-nya KeyError lalu menjatuhkan seluruh pembacaan harga.

**Cara alamat Uniswap Arc ditemukan** (docs.uniswap.org & sdk-core belum memuat
chain 5042; docs.arc.io masih testnet-only) — dipakai sekali dan berhasil:

1. Alamat dari GeckoTerminal (`networks/arc/pools`) → `pool.factory()` on-chain
   memberi factory v3.
2. `alchemy_getAssetTransfers` kategori `erc721` di SELURUH rentang blok (satu
   panggilan; `eth_getLogs` free tier cuma 10 blok) memberi daftar kontrak NFT →
   yang `name()`-nya "Uniswap V3 Positions NFT-V1" dan "Uniswap v4 Positions NFT"
   adalah NPM dan posm. `posm.poolManager()` memberi v4_pm.
3. Deployer ditemukan dengan **bisect `eth_getCode` per blok** (archive Alchemy)
   untuk mencari blok pembuatan, lalu membaca tx di blok itu. Alamat CREATE
   diturunkan dari `keccak(rlp([deployer, nonce]))`: nonce 1 = v2 factory,
   2 = v2 router, 3 = v3 factory, 12 = NPM, 19 = SwapRouter02.
4. Kontrak v4 lahir lewat **CREATE2 deployer kanonik `0x4e59b448…`**, jadi
   alamatnya dihitung langsung dari calldata tx: `keccak(0xff ++ deployer ++ salt
   ++ keccak(initcode))`. Karena argumen konstruktor StateView/V4Quoter cuma
   PoolManager, **alamatnya IDENTIK dengan Robinhood** — dan memang terbukti punya
   code di Arc dengan `poolManager()` yang benar.
5. UniversalRouter tidak lahir dari deployer itu; ia ditemukan dengan mendaftar
   `to` dari tx yang menyentuh PoolManager, lalu menyaring yang `poolManager()`-nya
   cocok DAN ukurannya 24.546 byte.

**UR Arc = build yang SAMA dengan UR Robinhood.** Bytecode-nya dibandingkan
byte-per-byte: 24.546 byte di kedua chain, beda cuma 19 rentang — seluruhnya
immutable (WETH9, NPM, posm, v3/v2 factory, alamat diri) plus chainId
(`0x13b2` vs `0x1237`). Karena itu `v4_swap_hop_field: True`: struct swap-nya
ikut punya field ekstra `minHopPriceX36`.

**Sumber data luar di Arc tipis.** Krystal tidak melayani chain ini dan
DexScreener menjawab `pairs: null` (belum diindeks), jadi yang tersisa indexer
Uniswap (mendukung 5042, terukur 121 entri untuk satu token) + GeckoTerminal
(`gecko: "arc"`). **GMGN sudah melayani Arc** (`gmgn: "arc"`, diverifikasi:
`rank("arc")` menjawab dan field keamanannya bentuk EVM sama seperti Base) —
sebelumnya belum, jadi kunci itu sempat sengaja dikosongkan.

**Dashboard sempat menghitung USDC DUA KALI.** `build_main_menu()` dan
`wallet_text()` menulis baris native lalu satu baris per entri `quotes`; di Arc
keduanya USDC yang sama, jadi dashboard menampilkan dua baris identik dan
**Total-nya dua kali lipat** dari uang yang benar-benar ada (terukur: 226,765
USDC dilaporkan $453,53). Keduanya sekarang melewati entri quote yang alamatnya
== `native_erc20` dan menandai barisnya "(native = ERC20)".

Konsekuensi yang perlu diingat: pembanding harga independen untuk
`assert_pool_price_sane` jadi lebih lemah di sini. Jangan simpulkan bot salah baca
dari selisih terhadap GeckoTerminal — terukur GT melaporkan CRCL $67,77 sementara
pool v4 terdalam on-chain $202,56, dan lima menit kemudian pool yang sama $116,09.
Tokennya memang bergerak sebesar itu; GT-nya yang telat. Patokan yang benar
`quoteExactInputSingle` (terukur 1 USDC → 0,00775229 CRCL, cocok dengan spot +
fee 10%).

**Explorer `arc-scan.org`** (pihak ketiga). `arcscan.app` hanya melayani testnet —
apex-nya tidak punya A record — dan `explorer.arc.io` ada di balik Cloudflare
Access milik Circle.

### Solana 1399811149: Meteora DLMM, dan kenapa ada proses Node

**Bukan chain EVM.** `get_w3()` MENOLAK id ini dengan pesan eksplisit, dan
`is_solana(cid)` adalah penjagaan di tiap jalur EVM — bukan cuma penanda
tampilan. Tanpa itu satu `CHAINS[cid]["factory"]` yang lolos menghasilkan
KeyError samar yang tidak menunjuk apa pun, pola kegagalan yang sama dengan
`cfg['gmgn']` di Arc yang dulu mematikan seluruh kartu mint.

Id `1399811149` dipakai registry lintas-chain (Wormhole/chainlist) untuk
mainnet-beta — dipilih justru karena mustahil bentrok dengan chainId EVM.

Mesinnya **`sol.py`** (bukan `solana.py`: nama itu akan menaungi paket PyPI
`solana`, dan `meteora.py` bentrok dengan direktori `meteora/`).

**Tiga sumber, pembagiannya disengaja:**

| sumber | untuk apa |
|---|---|
| Data API Meteora (`dlmm.datapi.meteora.ag`) | daftar pool, TVL, volume, APR, fee, harga USD, INDEKS posisi |
| RPC Solana (Alchemy) | keadaan pool & posisi yang sebenarnya |
| sidecar Node `meteora/dlmm.cjs` | SDK resmi `@meteora-ag/dlmm` — baca posisi + TANDA TANGAN tx |

#### Sidecar Node: alasannya, dan dua jebakan yang sudah menggigit

SDK resmi Meteora **cuma ada di TypeScript/Rust**, dan seluruh bagian yang
berbahaya ada di dalamnya: encode instruksi Anchor, turunan PDA bin array,
matematika bin, Token-2022 transfer hook, compute budget. Menulis ulang itu di
Python berarti menulis sendiri jalur uang yang tidak bisa diuji tanpa biaya —
dan tidak ada SDK Python sama sekali (`solana`/`solders`/`anchorpy` tidak
terpasang, dan memasangnya pun tidak memberi satu pun fungsi DLMM).

- **CommonJS, bukan ESM.** Build ESM SDK-nya RUSAK: `import` dari
  `dist/index.mjs` gagal *"Directory import …/@coral-xyz/anchor/dist/cjs/utils/
  bytes is not supported"*. Jangan menambahkan `"type": "module"` ke
  `meteora/package.json`.
- **`require("@meteora-ag/dlmm/package.json")` ditolak** ("Package subpath
  './package.json' is not defined by exports") — versinya dibaca dari
  `node_modules` lewat `fs` kalau perlu, dan itu informasi, bukan syarat.

Protokolnya SATU request JSON di stdin, SATU response di stdout. **`secret`
HANYA lewat stdin, tidak pernah argv** — argv terlihat di `ps` oleh setiap user
di mesin itu.

`sidecar_ready()` memeriksa node + node_modules + SDK SEBELUM ada tx, dan
`/rpc` menampilkannya.

#### `getProgramAccounts` ditolak 429 — indeks Data API yang menyelamatkannya

Ini bukan optimasi, tanpa ini `/list` **tidak jalan sama sekali** di RPC free
tier. `DLMM.getAllLbPairPositionsByUser` dan bahkan
`getPositionsByUserAndLbPair` (yang sudah difilter satu pool) memakai
`getProgramAccounts` — memindai seluruh akun program DLMM. Terukur: Alchemy
menjawab **429 "exceeded its compute units per second"** di KEEMPAT key yang
sehat, untuk SATU wallet di SATU pool.

Urutan yang dipakai sekarang:

1. `GET /portfolio/open?user=<wallet>` memberi daftar pool **dan alamat tiap
   posisi** (`listPositions`) — terindeks, nol RPC, terukur 0,19 detik.
2. `positions_by_key` membaca tiap alamat lewat `inst.getPosition(pubkey)` —
   akun yang ditunjuk saja, jadi ongkosnya tetap walau wallet punya ratusan
   posisi. Terukur 1 posisi dalam 4,6 detik dari dingin.
3. Sapuan penuh hanya kalau indeks kosong/gagal. Ia benar tapi mahal, jadi
   **cadangan** — dan karena ia ada, posisi yang belum terindeks (baru dibuat
   beberapa detik lalu) tetap ketemu. Indeks yang telat tidak pernah
   MENGHILANGKAN posisi, aturan yang sama dengan indexer Uniswap di jalur EVM.

Konsekuensinya: **Solana tidak butuh registry `history.json`.** Posisi DLMM bisa
dienumerasi dari owner-nya, jadi `v2_refs`/`v4_refs` diabaikan dan registry yang
bolong tidak pernah menghilangkan posisi — berbeda dari v4 EVM.

#### Rotasi RPC: dua jenis kegagalan, dua perlakuan

`healthy_rpc_urls()` memprobe tiap endpoint sekali per 600 detik dan
mengelompokkannya. **Yang `not enabled` DIBUANG, yang 429 TIDAK.** Bedanya
menentukan: *"SOLANA_MAINNET is not enabled for this app"* permanen sampai
network-nya dinyalakan per app di dashboard Alchemy (sama seperti
`base-mainnet` dan `arc-mainnet`), sedangkan 429 throughput pulih dalam
hitungan detik dan membuangnya bisa menghabiskan kandidat.

Terukur pada key user: **4 dari 6 sehat**, `…Ev8C` kuota bulanan habis,
`…s8dK` belum di-enable.

**Sidecar menerima SELURUH daftar** (`rpcs`) dan mencoba berurutan — tapi
**perintah yang MENANDATANGANI tidak pernah diulang ke endpoint lain**:
mengirim ulang tx yang dibangun ulang bisa menyetor dua kali. Aturan yang sama
dengan `_recover_sent()` di jalur EVM.

#### Bin, bukan tick — dan jangan pernah menghitungnya sendiri

Harga bin: `P_i = (1 + bin_step/10.000)^i`. `bin_step` dalam basis point, jadi
satu kotak = **`bin_step/100` persen PERSIS** — bukan `exp(0,0001 × spacing)`
seperti tick Uniswap. Kebetulan hampir sama untuk bin step kecil (4 → 0,040%),
tapi melenceng makin besar (400 → 4,00% vs 4,08%). `box_pct()` karena itu punya
cabang sendiri untuk `ver == 5`.

Lebih penting: **konversi harga↔bin untuk RANGE selalu lewat SDK**
(`bins`/`quote_add`), tidak pernah `log()` di Python. Pembulatan bin saat
deposit ditentukan SDK, dan kartu yang memakai pembulatan berbeda akan
menjanjikan range yang bukan range yang terjadi.

Kelalaian yang sudah kejadian: `range_str()` menjalankan bin id lewat
matematika tick (`1,0001^tick`) dan melaporkan posisi SOL/USDC bin step 4 yang
rangenya **104,44–110,98** sebagai **568,43–577,08**.

`tick_lower`/`tick_upper`/`cur_tick` di dict posisi DIISI BIN ID — perannya
sama (koordinat harga diskret yang membatasi range), jadi seluruh UI range dan
penanda IN/OUT jalan tanpa cabang.

**Batas 1.400 bin per posisi** (`MAX_BINS_PER_POSITION`; default layout 70).
Range yang lebih lebar dipangkas **SIMETRIS di sekitar bin aktif** — memotong
satu ujung diam-diam menggeser pusat range yang user pilih — dan kartu
MENYEBUTKAN pemangkasannya. Terukur: ±25/50% di pool bin step 4 butuh lebih
dari 1.400 bin, jadi lebar segitu memang menuntut bin step lebih besar.

#### Shape Spot/Curve/Bid-Ask: knop yang tidak ada di Uniswap

`StrategyType` SDK: Spot=0, Curve=1, BidAsk=2. Ini **bukan** mode range
(wide/lower/upper) melainkan bentuk SEBARAN likuiditas di dalam range, jadi ia
baris tombol tersendiri di kartu konfirmasi. Terukur pada SOL/USDC ±25/50%
dengan 100 USDC: Spot butuh 0,8756 SOL, Curve 0,9112, BidAsk 0,8423 — Curve
menumpuk dekat bin aktif sehingga butuh sisi lawan lebih banyak.

#### Rebalance DLMM = close → swap komposisi → mint, SAMA seperti EVM

**`rebalancePosition` bawaan SDK sengaja TIDAK dipakai untuk rebalance.** Ia
selalu memusatkan range di bin aktif, jadi ia cuma bisa meniru mode `wide` —
mode **Lower** (100% quote, menampung harga turun) dan **Upper** (100% meme,
menjual naik) mustahil dinyatakan di sana, padahal justru itu yang paling
sering dipakai. Jadi alurnya sama persis dengan `chain.rebalance_position`:
close (fee ikut terambil) → swap komposisi di pool itu sendiri → mint ulang
dengan **lebar range yang sama**, diletakkan menurut mode.

UI-nya **dua langkah**, dan urutannya penting: mode dulu (`rebsh|<pid>|<mode>`,
tombolnya sama dengan EVM), baru layar kedua yang memuat LEBAR + shape
(`rebok|<pid>|<mode>:<shape>:<width>`). Menggabung semuanya jadi satu layar
berarti belasan tombol yang artinya tidak bisa ditebak dari labelnya. Tombol
lebar (`rebw|`) merender ulang pesan yang SAMA dan tidak mengeksekusi apa pun —
pola yang sama dengan tombol lebar range di kartu mint.

**Lebar range DIPERTAHANKAN, dan yang dipindahkan cuma LETAKNYA** — sama
seperti EVM. Rebalance artinya menempelkan range ke harga sekarang, bukan
mengubah ukuran yang user pilih saat mint.

Ini sempat salah dibaca dan hasilnya merugikan. Keluhan "posisinya kurang
rapat" ternyata soal LETAK (range lama tertinggal jauh di bawah harga), bukan
soal lebar — dan `width_choices()` sempat dibuat berbawaan **RAPAT 5 bin**.
Akibatnya rebalance posisi PAID/SOL **70 bin** pulang jadi **5 bin (~4,1%)**:
cakupan yang user pilih sendiri hilang tanpa ia meminta. Yang benar-benar
memperbaiki keluhan aslinya adalah `rebalance_bins()` yang menempelkan range
ke bin aktif, dan itu sudah jalan.

`width_choices()` tetap menawarkan preset lebih rapat karena lebar di DLMM itu
jumlah BIN dan satu bin = `bin_step/100` persen: 125 bin di pool bin step 100
adalah rentang **2,38e-6 … 8,18e-6 SOL, 3,4×** dengan **0,0076 SOL per bin** —
tersebar setipis itu dan praktis tidak menghasilkan apa-apa sampai harga
bergerak jauh (terukur pada rebalance WOJAK/SOL pertama). Tapi itu PILIHAN.
Urutannya lebar lama / sedang 20 / rapat 5 / 1 kotak, **yang pertama = bawaan**,
dan persennya dihitung dari `bin_step` pool itu sehingga "5 bin" di pool bin
step 4 (~0,2%) dan bin step 100 (~5,1%) tidak tertukar artinya.

**Sisi mana yang memegang quote TIDAK tetap, dan menebaknya dari nama mode
memberi user kebalikan dari yang ia minta.** Di DLMM bin **di bawah** bin aktif
memegang token Y, bin **di atas** memegang token X. Jadi "Lower = 100% quote"
berarti di bawah bin aktif kalau quote itu `token_y`, tapi di **ATAS** bin aktif
kalau quote itu `token_x`. `rebalance_bins()` yang menanganinya; diuji untuk
kedua orientasi.

Tiga hal lain yang gampang salah di jalur ini:

- **Sisi PROBE dipilih dari sisi yang BERISI, bukan dari nama mode.** Sesudah
  swap komposisi, mode satu sisi menyisakan dana hanya di satu sisi — dan sisi
  mana itu bergantung orientasi quote. Memilihnya dari mode membuat "Lower"
  pada pool ber-quote `token_x` memprobe sisi yang kosong, `autoFill`
  mengembalikan 0, dan posisi barunya lahir **tanpa dana**.
- **Kalau sisi lawan yang diminta melebihi yang ada, rencananya disusun ULANG
  dari sisi yang membatasi** — bukan dipotong dengan `min()`. Memotong merusak
  RASIO kedua sisi, dan rasio itulah yang menentukan bentuk posisinya.
- **Bin aktif bisa bergeser oleh swap komposisi itu sendiri**, jadi letak range
  dihitung ulang sesudah swap. Tanpa itu mode satu sisi bisa menyentuh bin aktif
  lagi dan menarik sisi lawan yang tidak diminta.

**Swap komposisi dirutekan lewat JUPITER, bukan pool posisi.** Ini bukan
optimasi — pool DLMM satu pasangan itu SATU venue tipis, dan menjual habis satu
sisi di sana (yang memang arti mode Lower/Upper) membayar puluhan persen.
Terukur pada WOJAK/SOL, jumlah yang sama persis pada saat yang sama:

| WOJAK | pool posisi | Jupiter | selisih hasil |
|---|---|---|---|
| 5.000 | 0,040135 SOL · 2,90% | 0,042800 SOL · 1,91% | **+6,6%** |
| 20.000 | 0,152736 · 6,84% | 0,171036 · 1,99% | **+12,0%** |
| 33.290 | 0,246236 · 9,77% | 0,284821 · 1,96% | **+15,7%** |
| 52.026 | 0,370336 · 13,17% | 0,444963 · 2,00% | **+20,2%** |

Bahkan saat Jupiter tetap lewat Meteora DLMM ia menang, karena ia memilih pool
DLMM yang lebih DALAM — bukan pool posisi. Pelajaran yang sama persis dengan
"swap v4 dirutekan ke pool TERBAIK, dan patokannya quoter" di jalur EVM, dan
alasannya sama: venue yang dipakai posisi belum tentu venue terbaik.

`best_quote()` mencoba Jupiter dulu. **Kalau impact Jupiter masih di atas 1%,
pool posisi ikut di-quote dan yang HASILNYA lebih besar yang menang** — Jupiter
mengoptimalkan hasil, bukan impact, dan untuk jumlah kecil ia kadang memilih
satu rute sederhana yang justru tipis. Kegagalan Jupiter TIDAK membatalkan
swap; jalur pool selalu jadi cadangan, aturan yang sama dengan "kegagalan
routing tidak boleh membatalkan swap" di `v4_swap`.

**`priceImpactPct` Jupiter itu PECAHAN** (`"0.0264"` = 2,64%), bukan persen —
mengalikannya 100 lagi menampilkan 264%.

Selain venue, **rasio deposit untuk sebuah range+shape itu TETAP** sedangkan
`autoFill*` linear terhadap jumlah — jadi satu probe cukup: dari `(y0, x0)`
hasil probe, faktor skalanya `k = nilai_total / nilai_probe` dan target tiap
sisi `k × sisi_probe`. Tanpa penskalaan itu sisa yang tidak terpakai bisa
puluhan persen dari modal.

**Price impact dijaga terpisah, karena `minOut` tidak menahannya** — quoter
sudah memasukkan impact, jadi swap sebesar apa pun tetap "sesuai quote".
`_swap_guarded()` menolak di atas `impact_limit()`, dan `rebalance_impact()`
menghitungnya TANPA tx sehingga kartu langkah-2 menyebut angkanya SEBELUM
tombol ditekan — aturan yang sama dengan `swap_impact_v4()` di EVM.

**Hanya dana HASIL posisi ini yang dipakai**, sama seperti EVM. Jumlahnya dari
snapshot tepat sebelum close (pokok + fee), lalu dijepit ke saldo NYATA —
pembacaan snapshot dan hasil close bisa beda beberapa wei, dan meminta lebih
dari yang ada = mint gagal.

Pid-nya BERGANTI (akun lama ditutup, yang baru lahir), jadi pembukuannya sama
dengan EVM: event `close` + `fees` untuk yang lama, `mint` untuk yang baru.

#### Compound DLMM justru TETAP di tempat

Di sini `rebalancePosition` SDK memang alat yang benar, karena yang diinginkan
justru **range yang tidak bergeser**:
`simulateRebalancePositionWithStrategy` dengan `withdraws: []` (pokok tidak
disentuh) dan satu deposit yang range-nya dinyatakan sebagai **delta terhadap
bin aktif** (`lowerBinId − activeId … upperBinId − activeId`), sehingga bin
batasnya persis sama. `shouldClaimFee: true`, jadi fee diklaim dan dipakai
sebagai jumlah setoran **di dalam simulasi yang sama** — tidak ada jeda di mana
dana itu menganggur di wallet. Akun posisinya tidak ditutup, jadi sewa ~0,057
SOL tidak dilepas lalu dibayar lagi dan `pid`-nya tetap.

**Pindah pool ditolak**: akun posisi DLMM terikat ke satu `lbPair`.

#### Zap Meteora: TIDAK dipakai, dan batasnya bukan angka tetap

`@meteora-ag/zap-sdk` (v1.3.2) bisa zap-in satu token, zap-out lewat Jupiter,
dan `rebalanceDlmmPosition`. Sengaja tidak dipakai untuk rebalance: range-nya
dinyatakan `minDeltaId`/`maxDeltaId` terhadap bin aktif dan alurnya milik SDK,
sedangkan jalur ini harus mengikuti logika EVM. Nilai tambahnya yang nyata
adalah ATOMISITAS (close/swap/mint sekarang tx terpisah).

**Zap TIDAK bisa dipakai untuk close + auto-swap — programnya memang tidak
punya sisi itu.** Dipasang dan diperiksa sekali (v1.3.2), lalu dicopot lagi;
jangan diulang. Daftar instruksi program Zap LENGKAPnya:

```
close_ledger_account, initialize_ledger_account, set_ledger_balance,
update_ledger_balance_after_swap, zap_in_damm_v2,
zap_in_dlmm_for_initialized_position, zap_in_dlmm_for_uninitialized_position,
zap_out
```

Zap-**in** punya instruksi POSISI DLMM; zap-**out** tidak. `zap_out` cuma
menerima dua akun (`user_token_in_account`, `amm_program`) — ia pembungkus CPI
generik untuk sebuah **swap** yang berangkat dari token yang SUDAH ada di ATA
user, memakai "ledger" untuk menghitung persentasenya di on-chain.
`zapOutThroughJupiter/Dlmm/DammV2` seluruhnya kaki SWAP itu, bukan penutup
posisi. "Zap out of your positions" di README-nya merujuk zap-in yang dibalik,
bukan satu instruksi close.

Membuatnya atomik berarti merakit sendiri payload CPI `zap_out` berisi
instruksi `removeLiquidity` DLMM — SDK-nya tidak mengirim satu pun helper untuk
itu (`getDlmmRemainingAccounts`/`createDlmmSwapPayload`/`AMOUNT_IN_DLMM_OFFSET`
semuanya untuk SWAP), `docs.md` yang dirujuk README tidak ikut dipaketkan, dan
jalur itu tidak bisa diuji tanpa mengeluarkan uang sungguhan.

**Dan atomisitasnya sendiri ternyata tidak ada.** `rebalanceDlmmPosition` —
satu-satunya alur DLMM tingkat-tinggi di SDK itu — mengembalikan **TUJUH
transaksi** (`setupTransaction`, `initBinArrayTransaction`,
`rebalancePositionTransaction`, `swapTransaction`, `ledgerTransaction`,
`zapInTransaction`, `cleanUpTransaction`). Jadi alasan utama memakai Zap
(close + jual dalam satu tx) tidak pernah ada sejak awal.

Catatan operasional kalau suatu saat tetap dipakai: `jupiterApiUrl` SDK-nya
menempel `/swap/v1/quote` dan `/swap/v1/swap-instructions`, bentuk yang sama
dengan `lite-api.jup.ag/swap/v1` yang sudah dipakai sidecar — jadi ia bisa
diarahkan ke sana dan tidak butuh `JUPITER_API_KEY` (bawaannya `api.jup.ag`
yang sejak 31 Januari 2026 mewajibkan key).

**Batas bin Zap tidak ada sebagai konstanta.** Dicari di dokumen dan di
`dist/` SDK-nya: satu-satunya angka yang ada `BINARY_SEARCH_MAX_ITERATIONS = 20`
dan `SWAP_BIN_ARRAY_COUNT = 4`; tidak ada `MAX_BIN*` apa pun. Yang mengikat
adalah **ukuran transaksi dan jumlah akun**: satu tx Zap memuat rute swap
Jupiter DAN bin array DLMM (satu bin array = 70 bin), jadi makin lebar range
makin banyak akun sampai tx tidak muat. Itulah guna parameter `maxAccounts`
(contoh resminya memakai 50) — batasnya bergeser mengikuti panjang rute swap,
bukan angka tetap yang bisa dihafal.

**TIGA besaran hasil compound yang WAJIB dipisah — menukarnya membuat kartu melapor
"0 masuk" untuk rebalance yang sebenarnya menyetor ulang penuh.** Dibaca dari
sumber SDK (`_simulateDeposit`):

| field SDK | artinya |
|---|---|
| `amountXDeposited` | yang MENDARAT di bin (pokok lama + fee + tambahan) |
| `actualAmountXDeposited` = `max(0, deposit − withdrawn)` | yang keluar dari **wallet** |
| `actualLiquidityAndFeeXWithdrawn` = `max(0, withdrawn − deposit)` | yang kembali ke **wallet** |

Percobaan pertama memakai `actualAmountXDeposited` sebagai "yang disetor" dan
kartunya menulis **0** untuk operasi yang sebenarnya menyetor ulang 71.724 FLEX
+ 1,0956 SOL. `_rb_amounts()` menamainya `in_*` / `from_*` / `to_*`.

**Range >70 bin TIDAK bisa lewat `initializePositionAndAddLiquidityByStrategy`.**
Akun posisi lahir seukuran `DEFAULT_BIN_PER_POSITION` (70) dan harus di-realloc
untuk sisanya, sedangkan Solana membatasi realloc **10.240 byte per inner
instruction**. Gejalanya `Failed to reallocate account data` dengan log
*"Account data size realloc limited to 10240 in inner instructions"* — terukur
pada range 125 bin. `add_new` karena itu memakai
`initializeMultiplePositionAndAddLiquidityByStrategy2` di atas 70 bin (ia
memecah jadi beberapa posisi/tx sendiri dan mengembalikan instruksi per
posisi), dan jalur lama tetap dipakai untuk range biasa supaya yang sudah
terbukti tidak ikut berubah.

`rebalancePosition` mengembalikan **instruksi**, bukan `Transaction` (beda dengan
jalur add/remove). `buildTxs()` memecahnya jadi dua tx: init bin array harus
sudah masuk chain sebelum instruksi rebalance jalan, dan menggabungnya bisa
melewati batas ukuran tx. Bin array baru punya sewa sendiri
(`rentalCostLamports`, terukur 0,000569 SOL) — disebut di kartu supaya user
tidak kaget SOL-nya berkurang di luar fee tx.

Pembukuan compound sama artinya dengan v4: `added_usd` menghitung likuiditas
penuh, jadi fee yang jadi modalnya WAJIB diimbangi event `fees` — tanpa itu fee
tercatat sebagai setoran baru dan PnL rugi palsu sebesar fee tersebut.

#### Komposisi dua sisi dihitung SDK, dan sisinya ditentukan letak range

Bin **di bawah** bin aktif hanya menampung token Y (quote), bin **di atas**
hanya token X. Jadi:

- range seluruhnya di bawah harga → **100% quote**, persis limit buy bertingkat;
- range seluruhnya di atas → **100% meme**, persis limit sell bertingkat;
- straddle → dua sisi.

`quote_add` memanggil `autoFillXByStrategy`/`autoFillYByStrategy` — fungsi yang
SAMA yang menentukan jumlah tertarik saat deposit, jadi kartu dan eksekusi tidak
bisa berbeda. Sisi yang tidak tertampung **DINOLKAN dan dilaporkan**
(`unusable_*`): autoFill menolkan sisi yang dihitungnya, tapi jumlah yang
dikirim pemanggil untuk sisi yang salah tetap lewat — dan kartu lalu menjanjikan
"100 USDC masuk" untuk range yang tidak bisa menerima USDC sama sekali.

**Mode `upper` budgetnya satuan MEME**, sama seperti jalur EVM (`budget_sym()`).
Mengirimnya sebagai quote membuat sisi meme dihitung dari angka yang salah
satuan — terukur menghasilkan setoran **0,0000057 SOL** untuk budget "100".

**Balapan bin aktif.** `range_bins` dan `quote_add` membaca bin aktif lewat
panggilan RPC yang BERBEDA, dan bin aktif bergerak tiap swap. Kalau bergeser di
antara keduanya, range mode satu-sisi menyentuh bin aktif lagi dan kartu
"Lower (100% quote)" diam-diam menarik sisi meme. `plan_mint` memeriksa
`side` hasilnya dan menjepit ulang sekali kalau tidak sesuai — terukur terjadi
pada SOL/USDC bin step 4 (0,04% per bin): satu blok saja cukup.

**Mint TIDAK menukar otomatis.** Kalau sisi meme kurang, kartu MENGATAKANNYA
dan menyarankan mode Lower — bukan menukar diam-diam.

**Close BISA**, dan rutenya JUPITER — bukan pool posisi. Close menjual SELURUH
sisi meme sekaligus, dan pool DLMM satu pasangan itu venue tipis; terukur pada
posisi hidup, Jupiter memilih venue lain untuk 5 dari 9 pool (Pump.fun Amm,
Manifest, Raydium CP+Denali+Kipseli). Alasan yang sama persis dengan swap
komposisi rebalance.

Tiga aturan disalin dari jalur EVM, dan ketiganya soal uang:

- **Hanya hasil posisi INI yang dijual.** Jumlahnya dari snapshot `before`
  (pokok + fee), lalu DIJEPIT ke delta saldo nyata dan ke saldo nyata itu
  sendiri. Saldo meme yang user pegang untuk keperluan lain tidak boleh ikut
  terjual. Diuji: snapshot 100 sementara yang mendarat 60 → yang dijual 60.
- **Delta tak terbaca = swap DILEWATI**, bukan ditebak. RPC yang telat menjawab
  saldo pra-close membuat deltanya 0; menjual "sebanyak snapshot" di situ
  berarti menjual saldo lama.
- **Kegagalan swap TIDAK boleh melempar.** Close-nya sudah masuk chain dan
  dananya sudah di wallet — melempar membuat kartu melapor gagal untuk posisi
  yang sebenarnya sudah tertutup, dan user mengulang close yang sudah jalan.
  `_close_autoswap()` mengembalikan `(info, swap_error)` dan kartu menulis
  "⚠️ Auto-swap dilewati: …" di bawah kartu ✅.

Price impact dijaga terpisah (`minOut` tidak menahannya) dan **membatalkan
SWAP-nya saja**, bukan close-nya. Untuk sisi meme yang mint-nya SOL, cadangan
gas dipotong dulu — menjual habis SOL berarti tidak ada ongkos tx tersisa.

Arahnya (`swap_for_y`) diturunkan dari `quote_is_token1`, tidak pernah ditebak:
di DLMM sisi mana yang keluar saat close ditentukan letak bin aktif terhadap
range, jadi menebaknya dari nama apa pun akan menjual sisi yang salah.

Jalan ke sini lewat dua bug yang perlu diingat:

- `ask_close` memberi tombol "Close + swap MEME → quote" sementara
  `sol.close_any` MENGABAIKAN `autoswap`. Tombolnya menjanjikan swap yang tidak
  pernah terjadi: user menekannya, hasilnya utuh di wallet, dan ia menyimpulkan
  swapnya gagal.
- Kartu hasilnya membaca `r["swaps"]` POLOS, dan mesin tanpa auto-swap tidak
  mengisi kunci itu — `KeyError: 'swaps'` muncul SESUDAH kartu ✅ terkirim,
  jadi user melihat "❌ Error: 'swaps'" tepat di bawah close yang berhasil.
  Kelas yang SAMA dengan `got0`/`steps`. Aturannya tetap satu: **mesin memenuhi
  kontrak, kartu tetap `.get()`**.

#### Wallet & sewa akun

**`SOLANA_PRIVATE_KEY` WAJIB terpisah dari `PRIVATE_KEY`.** Key EVM secp256k1,
Solana ed25519 — dipakai silang bukan cuma gagal, ia menghasilkan alamat yang
bukan milik siapa pun. `pks_for(cid)` memilih daftar yang berlaku, dan
`wallet_idx` dijepit ke panjang daftar chain itu (jumlah wallet EVM dan Solana
tidak harus sama).

`_addr_of()` membedakannya dari BENTUK key, bukan dari chain aktif — ia dipakai
untuk daftar wallet lintas chain. Alamat diturunkan **tanpa kriptografi**:
secret Solana 64 byte = 32 seed + 32 public key, jadi alamatnya 32 byte
terakhir. Seed 32 byte saja ditolak dengan pesan jelas.

**Sewa akun posisi ~0,05724 SOL TERKUNCI selama posisi hidup** dan kembali saat
Close. Wajib disebut UI — kalau tidak user mengira SOL-nya hilang. `gas_reserve`
Solana 0,08 menutup fee tx DAN sewa itu; tanpa potongan sebesar ini "100% saldo"
gagal justru di langkah terakhir.

**Reduce 100% WAJIB `shouldClaimAndClose`** — tanpa itu sewanya terkunci
selamanya di akun kosong.

#### ADDR_RE sekarang dua bentuk, dan base58 WAJIB diverifikasi

Regexnya menangkap `0x…40hex` ATAU base58 32–44 karakter. Cabang base58 juga
cocok dengan kata biasa sepanjang itu, jadi kecocokannya WAJIB diverifikasi
`sol.is_sol_address()` — dekode base58 harus menghasilkan tepat 32 byte. Tanpa
cek itu, kalimat biasa memicu discovery.

`token_chains()` punya cabang sendiri untuk alamat Solana: Krystal maupun
GeckoTerminal tidak akan pernah menemukannya lewat query alamat EVM, dan tanpa
cabang ini user menempel mint Solana lalu bot memindai chain EVM aktif —
gejalanya terlihat seperti salah deteksi.

#### pid `dlmm:<pubkey>` = ver 5, dan tiap dispatcher WAJIB punya cabangnya

`reduce_any`/`close_any` jatuh ke cabang v2 untuk apa pun yang bukan 3/4, jadi
TANPA cabang eksplisit sebuah posisi DLMM dikirim ke `reduce_v2()` dan berakhir
di jalur EVM yang salah. `close_any` juga memanggil `get_w3()` di baris PERTAMA
untuk `assert_position_open` — cabang ver 5 harus di ATAS panggilan itu, kalau
tidak setiap close DLMM ditolak sebelum sempat sampai ke cabangnya.

`pid` cuma membawa alamat POSISI, sedangkan tiap aksi SDK butuh POOL-nya.
`pool_of_position()` membacanya lewat `wrapPosition` SDK (di-cache selamanya —
pool sebuah posisi tidak pernah berubah), **bukan** dengan mengambil offset byte
sendiri: layout Position, PositionV2, dan extended position berbeda, dan offset
yang ditebak mengembalikan pubkey yang salah TANPA gejala.

#### Wallet PER CHAIN, dan `pk()` tanpa argumen itu jebakan

Daftar wallet EVM dan Solana BERBEDA (secp256k1 vs ed25519), jadi `pk()` tanpa
argumen — yang membaca chain AKTIF — salah di setiap jalur lintas-chain.
Terjadi sungguhan: dengan chain aktif Solana, `/list` mengirim secret Solana ke
jalur EVM dan **Robinhood/Base/Arc gagal dibaca seluruhnya** ("masih dimuat"),
sementara posisi Solana-nya sendiri tidak pernah muncul. Gejalanya terbaca
sebagai "wallet Solana tidak terdeteksi" padahal dashboard-nya justru benar —
dashboard memang memakai chain aktif.

Aturannya: **apa pun yang memilih wallet WAJIB menyebut chain-nya** —
`pk(cid)`, `wallet_address(cid)`, `pks_for(cid)`, `wallet_label(cid=…)` —
bukan versi tanpa argumen. Yang sudah dipindah: `list_positions_all`,
`position_by_pid`, pembukuan per-chain di `cmd_list`
(`adopt_orphans`/`portfolio_summary`/`churn_count` — alamatnya beda per chain,
jadi riwayat bisa menempel ke wallet yang salah), layar **Kelola wallet**
(`wallets_text`/`wallets_kb`/`wallet_pick_kb`), tombol W di `menu_kb`, header
PnL, `_orders_for_chain`, dan `_gather_positions` milik monitor.

Dua yang TIDAK ikut, dan sengaja:

- **`pk_for(addr)` menyapu KEDUA keluarga** (`all_pks() + sol_pks()`): ia
  mencari kunci dari ALAMAT, dan alamat itu bisa milik chain mana pun karena
  monitor dan eksekutor order jalan lintas chain. Kalau daftar Solana tidak
  ikut, alert/order posisi DLMM tidak akan pernah menemukan key-nya.
- **Brankas bot (`wallets.json`) menyimpan KEDUA keluarga**, dengan field
  `kind` (`"evm"` / `"sol"`). Entri lama tidak punya field itu; semuanya EVM
  karena brankas dulu memaksa awalan `0x`, jadi ketiadaannya disimpulkan dari
  BENTUK key-nya, bukan diasumsikan buta.

**Impor MENDETEKSI keluarga kuncinya sendiri** (`detect_key_kind`), tidak
bertanya: menebak salah berarti memasang awalan `0x` pada key Solana (merusaknya)
atau menurunkan alamat dengan kurva yang salah — dua-duanya gagal SENYAP. Yang
diterima: 64 hex (dengan/tanpa `0x`) = EVM; base58 64 byte atau array JSON 64
angka = Solana. **Seed Solana 32 byte DITOLAK** dengan pesan jelas — alamatnya
tidak bisa diturunkan tanpa public key-nya, dan menyimpannya berarti key yang
nanti gagal dipakai.

**Key WAJIB dikanonikkan sebelum masuk brankas** (`canon_key`). Satu key Solana
punya dua ejaan — base58 dan array JSON — dan `add_wallet` mendedupe dengan
membandingkan TEKS. Tanpa normalisasi, key yang SAMA diimpor dua kali tersimpan
dua kali lalu muncul sebagai dua wallet beralamat identik (terukur:
`FY2Y2k518XXX…` tampil dobel di `sol_pks()`). Base58 yang dipilih karena itu
bentuk ekspor Phantom/Solflare, jadi hasil tombol Ekspor bisa ditempel balik ke
wallet biasa.

**Keypair Solana baru dibuat di SIDECAR** (`keygen`), bukan Python: ed25519
tidak ada di sana — `address_of()` cuma bisa MEMBACA 32 byte terakhir dari
secret 64 byte, tidak menurunkan alamat dari seed. Tombol "Buat baru" karena itu
bertanya keluarganya dulu (`wal2|new|evm` / `wal2|new|sol`).

**Ekspor/Hapus menyebut KELUARGA di callback** (`wal2|<aksi>|<fam>|<i>`) —
indeks saja ambigu begitu dua daftar tampil berdampingan, dan salah keluarga
berarti menghapus wallet yang bukan dimaksud. Wallet `.env` (keluarga mana pun)
tidak pernah muncul di daftar Hapus dan ditolak `is_env_pk_any()` kalau tetap
dicoba.

**Layar Kelola wallet menampilkan KEDUA keluarga sekaligus** (⟠ EVM / ◎ Solana)
— itu yang dicari user ("wallet saya ada di mana") — tapi **dinomori
TERPISAH**: `W1…Wn` untuk EVM, `S1…Sn` untuk Solana. Penomoran bersama membuat
"W2" ambigu begitu dua daftar tampil berdampingan.

**Indeks wallet juga DIPISAH** (`wallet_idx` vs `sol_wallet_idx`). Dengan satu
indeks bersama, memilih wallet Solana ikut memindahkan wallet EVM aktif tanpa
user memintanya — dan kalau daftarnya beda panjang, indeksnya terpotong diam-
diam. `idx_key(cid)` memilih setelan yang benar; tombol `wselx|<fam>|<i>` di
layar gabungan menyebut keluarganya eksplisit, sedangkan `wsel|<i>` di menu
utama selalu mengikuti chain aktif.

#### callback_data Telegram: BATAS KERAS 64 BYTE, dan seluruh pesan yang ditolak

Telegram membalas **`Button_data_invalid`** kalau ada SATU tombol yang
`callback_data`-nya lewat 64 byte — dan yang gagal bukan tombolnya saja,
melainkan **seluruh pesan**. Terjadi begitu Solana masuk: pid DLMM itu
`dlmm:` + pubkey base58 44 karakter = **49 byte**, sehingga

| callback | byte | |
|---|---|---|
| `pos\|dlmm:<44>` | 53 | ok |
| `rebw\|dlmm:<44>\|lower\|125` | 64 | mepet |
| `posc\|1399811149\|dlmm:<44>` | **65** | ✗ lewat SATU byte |
| `rebok\|dlmm:<44>\|lower:BidAsk:125` | **72** | ✗ |

Akibatnya `/list` dan `/start` **sama sekali tidak bisa dirender** — user cuma
melihat "Telegram menolak pesan hasil: Button_data_invalid" berulang, dan
tombolnya tidak pernah muncul.

Karena itu pid panjang TIDAK PERNAH masuk callback apa adanya. `cb(pid)`
menukarnya jadi **handle 13 byte** (`~` + blake2s 6 byte), dan `cb_restore()`
di BARIS PERTAMA router menukarnya balik sebelum handler mana pun melihat
datanya — jadi tidak ada satu pun handler yang perlu tahu soal batas ini. Pid
pendek (EVM) dikirim apa adanya supaya callback di pesan lama tetap terbaca.

Tabel handle ada di memori, jadi `cb_restore` memanggil `_cb_rebuild()` saat
handle tidak dikenal: tombol di pesan LAMA tetap hidup sesudah restart.
Pembacaannya lewat `list_positions_all` yang sudah ber-cache, jadi lazimnya nol
RPC.

**Penjagaan terakhir ada di JALUR KIRIM, bukan di situs pembuatnya.**
`_fit_kb()` di `reply()`/`edit()` memeriksa tiap tombol, menukar yang
kepanjangan jadi handle, dan mencatat WARNING. Itu perlu karena situs pembuat
callback ada puluhan dan satu yang terlewat sudah cukup mematikan `/list` dan
`/start` — dan itu memang terjadi DUA KALI. Yang kedua lolos dari dua lapis
pemeriksaan sekaligus:

```python
cb = f"pos|{p['pid']}" if c == cid else f"posc|{c}|{p['pid']}"   # SALAH
buttons.append([InlineKeyboardButton(label, callback_data=cb)])
```

Callback-nya dibangun ke VARIABEL dulu, jadi pencarian `callback_data=f"…"`
(grep maupun scan AST atas keyword `callback_data`) tidak menemukannya. Lebih
buruk: variabelnya dinamai **`cb`**, yang menaungi fungsi `cb()` di scope itu —
jadi seandainya pun dibungkus, hasilnya `TypeError`. Jangan pernah memakai nama
`cb` untuk variabel lokal.

Pemeriksaan murah sebelum menambah callback baru: render keyboard-nya untuk
posisi DLMM **dengan chain aktif yang BERBEDA** (supaya cabang `posc|` ikut
terpakai) lalu pastikan `len(callback_data.encode()) <= 64`. Terukur sesudah
diperbaiki: `posc|` 65 → **29 byte**.

**`menu|home` tidak pernah ada di router** — tombol "‹ Menu" di layar Kelola
wallet karena itu diam sejak commit `74e9574`. Yang benar `menu|main`.
Pemeriksaan murah untuk mencegah terulang: kumpulkan semua literal
`callback_data=` lalu cocokkan ke literal `data ==` / `data.startswith(` di
router (terukur 105 tombol unik, nol tanpa handler).

`wallet_idx` dipakai bersama semua chain, jadi indeksnya **dijepit** ke panjang
daftar chain itu — tanpa itu chain ber-1 wallet menampilkan wallet pertamanya
dengan label "W3" hanya karena chain lain punya 3. Chain yang belum punya
wallet sama sekali (Solana sebelum `SOLANA_PRIVATE_KEY` diisi) **dilewati**,
bukan dilaporkan sebagai kegagalan baca.

#### Pembacaan posisi Solana: satu proses Node, dan pool diambil paralel

Tiap panggilan sidecar itu **proses Node baru** (~0,7–1 detik hanya untuk start
+ `require` SDK). Lima pool berarti lima kali ongkos itu, sedangkan `/list`
lintas chain punya anggaran **5 detik TOTAL**. Dua hal yang memperbaikinya:

- `positions_by_key` menerima `groups` — semua pool dalam SATU panggilan.
- **`position_one()` WAJIB memakai `positions_by_key`, bukan `positions`.**
  Ini satu-satunya tempat yang terlewat saat jalur lain dipindah ke pembacaan
  per-alamat, dan ia duduk persis di jalur klik `pos|`/`reb|`/`fee|`/`cmp|`:
  terukur **43,7 detik lalu 429** untuk SATU posisi, karena `positions` menyapu
  `getProgramAccounts` seluruh program DLMM dan sidecar mencobanya di tiap
  endpoint. Sesudah diperbaiki **0,67 detik**, dan `position_by_pid` 47,8 → 0,48
  detik.
- `pool_info` per pool diambil **paralel**, dan harga kedua sisi dibaca dari
  payload pool yang sudah ada (`px0`/`px1`) alih-alih `token_usd_price()` per
  token — itu satu request per TOKEN per posisi, 10 request tambahan untuk 5
  posisi.

Terukur untuk 5 posisi: **6,5 → 1,4 detik hangat**, dingin 6,8 detik. Dingin
masih di atas anggaran, jadi klik pertama menandai Solana "masih dimuat" dan
klik kedua lengkap — perilaku yang sama dengan chain EVM yang lambat, dan
task-nya sengaja tidak dibatalkan supaya cache-nya terisi.

**Dua hal lagi yang membuat posisi Solana KADANG tidak muncul di `/list`**, dan
keduanya soal WAKTU, bukan gagal baca — gejalanya identik dengan dana hilang:

- **`positions_by_key` dulu loop BERURUTAN per pool.** `DLMM.create` itu
  beberapa round-trip RPC sendiri, dijalankan satu per satu: terukur **14,1
  detik untuk 8 pool**. Sekarang `DLMM.createMultiple` + `Promise.all`.
  Instansinya dipetakan lewat `inst.pubkey`, **bukan lewat URUTAN** yang
  dikembalikan `createMultiple` — urutannya tidak dijanjikan di mana pun, dan
  salah pasang berarti posisi dibaca dengan desimal + bin step pool LAIN, gagal
  SENYAP karena angkanya tetap terlihat wajar.
- **`Connection` web3.js MENUNGGU pada 429** secara default dan menghormati
  `Retry-After` sebelum mencoba lagi di endpoint yang SAMA — persis jebakan
  yang sudah tercatat di jalur EVM ("429 jangan pernah ditunggu, rotasi
  endpoint"). `disableRetryOnRateLimit: true` membuatnya gagal SEKETIKA
  sehingga loop endpoint di sidecar yang mengambil alih.

Terukur pada 8 posisi, enam pembacaan berturut-turut: **median 8,92 → 4,35
detik, terburuk 12,54 → 5,68 detik**, jumlah posisi tetap 8. Sisa ayunannya
memang latensi RPC, jadi klik pertama yang dingin masih bisa kalah dari
anggaran — tapi sekarang lazimnya tidak.

#### PnL Solana dibaca dari METEORA, bukan `history.json`

Posisi DLMM bisa dienumerasi dari owner-nya, jadi posisi yang dibuat di
**meteora.ag** ikut muncul di `/list` — dan bot tidak punya satu pun event
mint untuknya. Akibatnya `_pos_metrics` tidak punya deposit pembanding dan
SELURUH baris Solana berakhir "?" (terukur: 5 dari 5 posisi), sementara
`open_value`-nya tetap masuk ke header portfolio tanpa deposit lawan sehingga
PnL keseluruhan menggelembung palsu.

`/portfolio/open` — endpoint yang SAMA yang sudah dipakai sebagai indeks
posisi — ternyata sudah membawa pembukuannya: `pnl`, `pnlPctChange`,
`totalDeposit`, `balances`, `unclaimedFees` (plus varian `*Sol`). Terukur
cocok dengan meteora.ag/portfolio sampai sen terakhir: 6 pool, deposit
$2.160,23, **PnL +$18,63 (+0,86%)** — identik dengan blok `total` API-nya.

`sol.portfolio_stats()` membacanya dan `_attach_pnl()` menempelkannya ke tiap
posisi (`pnl_usd`/`pnl_pct`/`deposit_usd`/`pnl_shared`/`pnl_src`).
**Ongkosnya nol di jalur daftar**: `portfolio_index()` sudah memanggil URL
yang sama dengan param yang sama, jadi `_api` (ttl 30 detik) menjawab dari
cache — terukur 0,206 detik dingin, **0,000 detik** berikutnya.

Empat hal yang WAJIB dipegang, semuanya sudah diukur:

- **Angkanya PER POOL, bukan per posisi.** Sepuluh varian endpoint dicoba
  (`/positions/<addr>`, `/portfolio/<user>`, `/portfolio/position`,
  `/pools/<pool>/positions`, …) — **semuanya 404**. Dua posisi di pool yang
  sama karena itu berbagi satu angka; `pnl_shared` menyebutkan jumlahnya dan
  UI mengatakannya. Penjumlahan (header `/list`, ringkasan web) karena itu
  **dedupe per POOL** — menjumlah per posisi menghitungnya dua kali.
- **`pnl` TIDAK bisa dihitung ulang dari `balances + fee − totalDeposit`.**
  Ia sudah memuat penarikan dan fee terklaim, dan keduanya tidak dirinci.
  Terbukti di EMBER/SOL: deposit $407,02, nilai $167,66, fee unclaimed
  $18,48, tapi `pnl` **+$40,84** — rumus naif memberi **−$220,88**. Karena
  itu `claimed`/`withdrawn` dari `history.json` DINOLKAN di jalur ini, bukan
  ditampilkan di sebelahnya seolah-olah komponennya.
- **`pnl` (USD) dan `pnlSol` bisa BERLAWANAN TANDA** dan itu bukan
  ketidakcocokan — terukur PAID/SOL `pnl` +$0,43 sementara `pnlSol`
  −0,0056 SOL. Bot memakai yang USD, sama seperti seluruh angka lain.
- **Nilainya STRING** (`"0.8788…"`), jadi `_f()` yang mengonversinya.

Header `/list` karena itu menjumlah **PnL dan penyebutnya PER CHAIN**, bukan
sekali dari total: Solana memakai `pnl` + `totalDeposit` Meteora (penyebut
yang sama dengan persen di meteora.ag, jadi kedua layar tidak saling
membantah), chain EVM tetap `withdrawals + fees_claimed + open + unclaimed −
deposits` terhadap modal bersih. Memaksakan Solana ke rumus global berarti
menaruh selisihnya di `withdrawals`, dan itu merusak modal bersih chain EVM
yang dihitung dari angka yang sama. Posisi ber-PnL Meteora juga dikecualikan
dari peringatan "tidak punya catatan deposit" — deposit-nya justru diketahui,
cuma bukan dari `history.json`.

Umur posisi tetap "?": payload ini tidak memuat waktu mint (`updatedAt` itu
perubahan terakhir), dan `lastUpdatedAt` dari SDK juga bukan waktu lahirnya.
Jadi APR posisi Solana juga tidak dihitung — bukan diisi tebakan.

#### Market cap DLMM: satu kunci salah nama yang mematikan tiga hal

`_mc()` di `_position_detail` membaca `pinfo.get("supply")` — kunci itu **tidak
pernah ada**. `pool_info()` menamainya per sisi (`supply0`/`supply1`), jadi
`mc_lower`/`mc_upper`/`mc_now` SELURUH posisi DLMM bernilai `None`, diam-diam,
tanpa satu pun error.

Akibatnya tiga, dan yang kedua yang paling berbahaya:

- **Range tampil sebagai harga mentah** (`0.0₅199–0.0₅797`) padahal cabang
  market cap di `range_str()` sudah ada — ia cuma tidak pernah kebagian data.
- **Arah alert SELALU "keluar ke ATAS".** Alert memakai
  `mc_now and mc_lower and mc_now < mc_lower` untuk memutuskan arah, dan `None`
  membuat syarat itu permanen False. Terukur pada PONDER/SOL: harga
  **0.0₅191 di bawah** batas bawah 0.0₅199, tapi alertnya menulis naik DAN
  "posisi jadi penuh SOL" padahal isinya justru penuh PONDER — kebalikan
  keduanya, untuk keputusan yang user pakai memilih rebalance.
- **Order TP/SL Solana mati senyap.** Tidak ada penolakan di `ask_tpsl` (dan
  tidak pernah ada) — kartunya tampil, order tersimpan, lalu `_check_orders`
  melewatinya selamanya karena `if not mc: continue`.

Supply-nya milik sisi **MEME**, jadi `supply0` kalau quote token1, `supply1`
kalau sebaliknya. Sesudah diperbaiki, 6 posisi hidup semuanya punya MC —
PAID $9,2jt–$16,1jt (now $16,4jt), PONDER $215,3k–$861,0k — dan
**TP/SL Solana jadi benar-benar hidup**: `close_any` sudah punya cabang ver 5,
`_check_orders` generik, jadi tidak ada kode baru yang perlu ditulis. Itu
perubahan jalur DANA yang lahir dari perbaikan pembacaan, bukan dari fitur
baru — sebut ke user, jangan biarkan ia menemukannya sendiri saat posisi
tertutup otomatis.

**Arah out-of-range dihitung dari TICK/BIN, bukan dari market cap**
(`out_of_range_side()`), supaya ia tetap benar walau MC tak terbaca. Aturan
sisinya SAMA untuk Uniswap dan DLMM:

- `cur < lower` → posisi 100% **token0**. v3/v4: harga di bawah range. DLMM:
  seluruh bin posisi ada DI ATAS bin aktif, dan bin di atas memegang token X.
- `cur > upper` → posisi 100% **token1**.

Yang berbeda cuma KATA arahnya: user membaca harga quote-per-meme, jadi saat
quote = token0 hubungannya terbalik terhadap tick/bin. Diuji untuk keempat
kombinasi (dua orientasi × dua sisi).

**Batas MC harus DIURUTKAN sebelum ditulis.** Untuk quote = token0 harganya
dibalik, jadi `mc_lower` (dari batas bawah tick/bin) justru yang lebih BESAR.

**Kartu MINT ikut, lewat `sol.plan_view()`.** Kartu posisi memakai MC sementara
kartu konfirmasi mint menulis harga mentah (`0.0₅545 … 0.0₄108`) — dua satuan
berbeda untuk keputusan yang sama, dan MC juga satuan yang dipakai TP/SL. Helper
itu sekaligus membetulkan orientasinya: `quote_add` mengembalikan `price_*`
sebagai harga MENTAH y-per-x (`_price_of_bin`), TIDAK dibalik seperti `_q_price`
di `_position_detail`, sedangkan kartunya menulis label `<quote>/<meme>` — jadi
di pool ber-quote `token_x` angkanya kebalikan dari labelnya. Terukur pada
FAMILY/SOL: bin −663…−578 = **MC $577,0k … $1,1M**. Supply tak terbaca → `mc_*`
None dan kartu jatuh ke harga, aturan yang sama dengan `range_str()`.

#### Kartu hasil punya KONTRAK, dan mesin Solana sempat tidak memenuhinya

`close_any`/`reduce_any`/`collect_any` adalah dispatcher generik: `do_close`,
`do_reduce_exec`, dan `do_collect` merakit kartunya dengan SATU kode untuk semua
versi. Kodenya membaca `r['got0']`/`r['sym0']`/`r['got1']`/`r['sym1']` langsung,
dan `sol.py` tidak mengisi satu pun dari keempatnya.

**Gagalnya SESUDAH uang bergerak.** Urutan di `do_close`: tx → `record_event` →
kartu. Jadi close DLMM yang SUKSES di chain berakhir `KeyError: 'got0'` dan user
membaca **❌ Error: 'got0'** untuk posisi yang sudah tertutup dan dananya sudah
di wallet — terverifikasi: posisi `Ey8C…HkiG` memang hilang dari daftar posisi
hidup sesudah error itu. Kerugian nyatanya bukan dana melainkan kepercayaan:
langkah berikutnya yang wajar adalah mengulang aksi yang sudah berhasil.

Yang diisi sekarang, dan dari mana:

| | isi `got0`/`got1` |
|---|---|
| `close_any` | pokok + fee dari snapshot `before` (close DLMM menarik keduanya) |
| `reduce_any` | pokok × pct, **fee HANYA kalau 100%** |
| `collect_any` | `fees0`/`fees1` posisi tepat sebelum klaim |

**Reduce sebagian TIDAK menarik fee di DLMM** — `shouldClaimAndClose` cuma
dinyalakan di 100%, sedangkan v3/v4 EVM selalu menarik fee penuh berapa pun
pct-nya. `fee_included` menyatakannya dan kartu menulis "(fee TETAP di posisi)"
alih-alih "(termasuk fee)"; tanpa itu kartu berbohong tentang uang.

Bedanya dari EVM: di sana `got*` diukur dari **delta saldo** sesudah tx, di sini
dari **snapshot sebelum** eksekusi. Praktis sama karena tidak ada swap yang
memotongnya, tapi jangan diperlakukan sebagai angka terukur.

**`steps` juga dua bentuk, dan `for label, h in …` meledak untuk salah satunya.**
Mesin EVM mengembalikan `(label, txhash)` yang UI-nya jadikan link lewat
`ch.tx_link`; `sol._steps()` sudah mengembalikan STRING jadi, karena signature
Solana bukan hash 0x-hex. `step_lines(cid, steps)` menerima keduanya dan dipakai
di SEMUA situs yang bisa menerima hasil DLMM (add/reduce/collect/close/rebalance/
eksekutor order). `step_tx(steps)` untuk `store.update_order(tx=…)` — `steps[0][1]`
polos mengambil **karakter kedua** dari string dan menyimpannya sebagai tx order.

**`with_progress(status, head, work)` butuh TIGA argumen.** Jalur mint DLMM
memanggilnya dengan dua, dan mint gagal SEBELUM satu tx pun dikirim dengan pesan
yang tidak menunjuk apa pun: *"Mint DLMM gagal: with_progress() missing 1
required positional argument: 'work'"*. Pemeriksaan murahnya scan AST — cari
`Call` bernama `with_progress` yang `len(args) != 3`.

Aturan umumnya: **mesin baru wajib memenuhi kontrak dict yang dibaca kartu
generik**, dan kartu generik membacanya dengan `.get()` + melewati barisnya kalau
kosong. Dua-duanya, bukan salah satu — kontrak menjaga kartunya informatif, dan
`.get()` menjaga aksi yang sudah berhasil tidak dilaporkan gagal.

#### `CollectFeeMode`: pool yang fee-nya cuma menumpuk di SATU sisi

Sebagian pool DLMM membayar fee **hanya dalam satu token**, arah swap apa pun.
Meteora menandainya dengan titik hijau "Fees In Quote Token" di daftar pool
mereka; bot dulu tidak menyebutnya sama sekali, jadi dua pool pasangan yang
sama terlihat identik padahal artinya berbeda untuk LP.

Enum SDK-nya: **0 = `InputOnly`** (fee diambil dari sisi token yang MASUK, jadi
kedua sisi bisa menumpuk) dan **1 = `OnlyY`** (fee SELALU token Y). Terukur
pada TIGRINO/SOL, pasangan yang sama persis: `5TTHzu39…` mode 0, `AGVxQPJk…`
dan `HrqPvukx…` mode 1 — cocok persis dengan titik hijau di UI Meteora.

Dua sumber, dan keduanya dipakai:

- **Data API**: `pool_config.collect_fee_mode` (bukan di akar row — di akar
  tidak ada field fee apa pun selain `dynamic_fee_pct`).
- **Sidecar**: `lbPair.parameters.collectFeeMode` — **bersarang di
  `parameters`**, bukan di akar `lbPair`. Mencarinya di akar mengembalikan
  `undefined` TANPA error, dan pool quote-only lalu terbaca seperti pool biasa.
  Dipakai untuk pool yang belum terindeks Data API. Nilainya diverifikasi
  identik dengan Data API pada kedua pool di atas.

**Mode 1 TIDAK otomatis berarti "fee dalam quote".** Nama Meteora benar untuk
pool mereka karena quote di situ memang selalu token Y — tapi kalau quote
justru `token_x`, mode 1 berarti fee menumpuk di sisi **MEME**. Itu kebalikan
dari yang dibaca user dan justru kasus yang paling perlu ditandai, jadi
`fee_only_sym()` menurunkan simbolnya dari ORIENTASI, bukan dari nama mode.

Kenapa ini bukan kosmetik: **Compound berubah artinya.** Fee sisi lawan selalu
0, jadi `compound_any` cuma menyetor ulang satu sisi — dan untuk posisi
satu-sisi mode `upper` (100% meme) hasilnya tetap quote. Ditandai di tiga
tempat: tabel daftar pool (`¤` di kolom tag + legenda yang menyebut jumlahnya),
label tombol pool (`· fee→SOL`), kartu konfirmasi mint (satu kalimat penuh
dengan akibatnya), dan baris info pool di kartu posisi (`fee hanya SOL`).

#### Yang belum ada untuk Solana

Swap komposisi otomatis, pindah pool, revoke approval (Solana tidak punya
allowance), dan `/cleanup`. Semuanya menolak dengan pesan yang menyebut
alasannya, bukan gagal diam-diam.

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

### minOut TIDAK melindungi dari price impact — itu tugas penjagaan terpisah

`minOut` diambil dari quoter, dan quoter **sudah** memasukkan dampak harga. Jadi
impact seberapa pun akan lolos: swap yang menggerakkan harga pool 2× tetap "sesuai
quote" dan tidak pernah revert.

Terjadi sungguhan di BODKIN #1913813: budget 0,021 ETH, yang benar-benar keluar
wallet 0,014658434 ETH (~$36,04), yang mendarat di posisi cuma **$19,87**. Swap
komposisi 0,010423 ETH dieksekusi saat likuiditas aktif pool cuma **8,59e18**
(sekarang 2,41e22 — 2.800× lebih tebal); harga rata-rata eksekusi 0,000001574
ETH/BODKIN sedangkan harga pool SESUDAHNYA 0,000003722, jadi swap itu menggerakkan
harganya sendiri lebih dari 2×.

`v4_swap()` karena itu membandingkan hasil quoter dengan hasil harga spot dikurangi
fee, dan menolak kalau selisihnya di atas `_SWAP_IMPACT_MAX` (25%).

**Angkanya ditampilkan di kartu SEBELUM user menekan tombol**, bukan ditolak
diam-diam saat eksekusi: `swap_impact_v4()` menghitungnya tanpa mengirim tx, kartu
menulis "price impact swap: X%" plus perkiraan dolar yang terbakar, dan tombol
"⚠️ Saya paham, lanjut walau impact X%" muncul **hanya kalau ambangnya terlewati**
(kalau selalu ada, user terbiasa menekannya dan penjagaannya jadi percuma).
Persetujuan itu menaikkan ambang untuk KARTU ITU saja lewat `ctx["max_impact"]` →
`strategy["max_impact"]` → `v4_swap(..., max_impact)`, bukan setelan global.

Terukur di pool BODKIN saat tebal: 0,0104 ETH → 0,2%, 0,5 → 4,7%, 2,0 → 38,5%,
8,0 → 84,6%. Jadi swap normal tidak terganggu dan yang besar tetap minta izin.

### Swap v4 dirutekan ke pool TERBAIK, dan patokannya quoter

`v4_swap(..., route=True)` (default) memilih pool terbaik untuk jumlah itu, bukan
selalu pool posisi. Dulu swap komposisi (mint) dan auto-swap (close) selalu jalan
di pool posisi sendiri, jadi posisi di pool kecil DIJAMIN membayar price impact
besar saat keluar — terjadi berulang ke user, terukur di close BLAST/ETH: biaya
swap **7,3%**.

**Patokannya hasil `quoteExactInputSingle`, BUKAN TVL.** Quoter memasukkan fee DAN
price impact sekaligus, jadi ia satu-satunya angka yang sebanding antar pool. TVL
sebagai patokan salah, dan salahnya besar — terukur pada microduck di Robinhood
untuk swap 100.000 token:

| pasangan | pool ber-TVL terbesar | pool terbaik menurut quoter | selisih |
|---|---|---|---|
| USDG | fee 5%, TVL $224.388 | fee 0,78%, TVL $175.345 | **+135,8%** |
| ETH native | fee 3,2%, TVL $963 | fee 1,0024%, TVL $71.503 | +72% |

Memilih lewat TVL berarti membuang lebih dari separuh hasil swap.

Empat syarat, jangan dilemahkan:

- **Pasangan currency harus PERSIS sama.** Merutekan ke pool ber-quote lain
  mengubah token yang diterima user, padahal kartu sudah menjanjikan yang satunya
  dan pemanggil membaca delta saldo token itu.
- **Kandidat dari `discover_any()`**, yang tiap pool-nya sudah diverifikasi lewat
  hash PoolKey — jadi aman dipakai membangun transaksi. Pool ber-hooks tidak pernah
  lolos hash, dan quoter-nya juga revert; dua-duanya memang tidak diinginkan.
- **Daftar kandidat di-cache `_V4_ROUTE_TTL` (600 detik).** Discovery terukur ~2,3
  detik bahkan saat hangat, sedangkan quote 12 pool paralel cuma **0,09 detik** —
  jadi yang mahal pencarian kandidatnya, bukan quote-nya. Sesudah cache: 0,000s.
- **Kegagalan routing TIDAK boleh membatalkan swap** — apa pun yang salah,
  kembalikan pool asal.

`swap_impact_v4(..., route=True)` memakai pool yang SAMA dengan yang akan
dieksekusi. Tanpa itu kartu menampilkan impact pool posisi sedangkan swapnya di
pool lain — terukur pada pasangan yang sama: kartu menulis **58,1%** padahal yang
benar-benar terjadi **0,7%**.

`swap_any`/`swap_route`/`find_pool_dex` tetap hanya melayani v3; routing v4 berdiri
sendiri karena pool v4 tidak bisa dienumerasi on-chain.

**Jangan simpulkan "pool-nya beda" dari keadaan pool yang berubah.** poolId swap dan
poolId posisi di kasus itu SAMA; yang beda adalah keadaan pool sesudah swap
(tick 125017) vs sekarang (tick 141958). Bandingkan poolId, bukan harga/likuiditas.

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

### Slug sumber luar itu OPSIONAL — baca dengan `.get()`

`gmgn`, dan pada dasarnya `dexscreener`/`gecko`/`slug` juga, tidak ada di semua
chain: Arc tidak punya `gmgn` sama sekali karena GMGN memang belum melayaninya.
Tiga tempat di `bot.py` dulu menulis `cfg['gmgn']` langsung, dan akibatnya
**seluruh kartu konfirmasi mint di Arc mati** — user cuma melihat `❌ 'gmgn'`,
pesan KeyError yang tidak menunjuk apa pun.

`ext_links_html(cid, token_ca, pool)` dan `chart_buttons()` sekarang melewati link
yang slug-nya tidak ada. Aturannya untuk kunci chain baru: kalau tidak semua chain
punya, pembacanya WAJIB `.get()` — daftar kunci yang hilang per chain gampang
dicek (`set(gabungan) - set(cfg)`), dan saat ditulis yang hilang di Arc cuma
`gmgn` + `dexes`.

### "Saldo X kosong" WAJIB menyebut chain, wallet, dan angkanya

`no_funds_msg()` dipakai ketiga tempat yang menolak karena `compute_amount() <= 0`
(`build_preview`, `build_preview_v2`, `do_mint`).

Pesan lamanya cuma "Saldo USDC kosong." dan itu tidak bisa dipakai mendiagnosis
apa pun — user tidak tahu bot sedang di chain mana, memakai wallet yang mana, dan
berapa yang benar-benar terbaca. Kejadian nyata: wallet berisi **59,997116 USDC
di Arc** (dibuktikan dari arsip Alchemy pada blok 21081086, jam kejadian persis)
sementara bot melapor kosong. Tanpa angka di pesannya, dugaan bisa jatuh ke
belasan tempat — padahal tiga baris sudah cukup memisahkan chain salah / wallet
salah / memang habis.

Dua hal yang gampang salah kalau ditulis ulang:

- **Teksnya POLOS, tanpa tag HTML.** `build_preview` melemparnya sebagai
  `RuntimeError` dan pemanggilnya menulis `f"❌ {esc(e)}"`, jadi tag apa pun
  tampil mentah. Prefiks ❌ juga ditambahkan pemanggil, bukan di dalam.
- **Pembacaan saldonya HANYA di jalur gagal**, jadi tidak menambah ongkos RPC di
  jalur normal.

Ingat juga `.env` ada di `.gitignore`: VPS punya `.env` DAN `settings.json`
sendiri, jadi wallet + chain aktif di sana bisa berbeda dari mesin tempat
diagnosis dijalankan. Jangan menyimpulkan dari saldo yang dibaca di mesin lain.

### 'STF' pada swap = jumlah melebihi saldo, bukan pool bermasalah

`TransferHelper.safeTransferFrom` di router v3 balas `'STF'` — revert yang tidak
menyebut token, jumlah, maupun sebabnya. Terbukti di BSC dengan allowance MAX:
swap sebesar saldo LOLOS simulasi, swap 10× saldo memberi `execution reverted: STF`
yang persis sama. Jadi jangan cari-cari masalah di pool.

**Pemangkasan itu bisa TIDAK jalan, dan STF-nya lolos ke user.** `poll_balance`
berhenti begitu saldo ≥ target, jadi replika RPC yang menjawab lebih tinggi dari
kenyataan membuat `bal_in < amount_in_wei` bernilai False — tidak ada yang dipangkas,
dan router gagal menarik token. Gejalanya di Base: *"Gagal beli USDC dari WETH: Swap
WETH→USDC (fee 100) ditolak pool: execution reverted: STF"* padahal pool-nya sehat
(55,9 WETH + $240k USDC) dan allowance MAX.

Penanganan STF karena itu MEMBACA ULANG saldo dulu dan memangkas, baru meng-approve
ulang kalau masih gagal — bukan langsung approve. Approve tidak menolong kalau
sebabnya saldo, dan ia mengirim tx (bayar gas) untuk dugaan yang belum tentu benar.
Terbukti: minta 0,013452132 WETH dengan saldo 0,012811555 → STF; dipangkas ke saldo
nyata → SUKSES (32,1649 USDC).

`swap_to_token()` karena itu memangkas `amount_in_wei` ke saldo NYATA (dan menurunkan
`min_out` proporsional — kalau tidak, swap revert karena minOut kekinggian), lalu
kalau tetap STF ia menyetel ulang approval sekali dan menyimulasikan lagi. Selisih
tipis lazim: jumlahnya dihitung dari saldo yang dibaca sepersekian detik lebih awal,
atau tokennya fee-on-transfer.

### Auto-swap saat close cuma menjual HASIL close

**Swapnya di POOL POSISI ITU SENDIRI** (`v4_swap(..., key, meme, ...)`), sama seperti
swap komposisi saat mint — bukan dirutekan ke pool lain yang lebih dalam.
`swap_any`/`swap_route`/`find_pool_dex` hanya melayani v3.

**Kartu WAJIB menyebut jumlah yang diterima dan biayanya.** Dulu cuma menulis
"swapped MEME → WETH" — dua-duanya salah: tujuannya sebenarnya QUOTE POOL (mis.
USDG, sedangkan label di-hardcode ke `wrapped_symbol`), dan tidak ada satu pun angka
sehingga user tidak bisa tahu berapa yang termakan. `close_v4` sekarang mengembalikan
`swap_info` berisi jumlah quote yang BENAR-BENAR diterima (delta saldo) plus nilai
wajar sebelum swap (harga pool dikurangi fee), dan kartu menghitung selisihnya.

**Fee pool dan price impact WAJIB dipisah, dan `expect` sudah memotong fee.**
Kartu dulu menulis satu angka berlabel "(fee pool + price impact)" — labelnya
KELIRU, karena `expect = ideal × (1 − fee)` sehingga angkanya impact SAJA.
Akibatnya kartu justru MENGECILKAN biaya sebenarnya, dan user membaca fee pool
yang memang segitu tarifnya sebagai kegagalan routing.

Terukur pada close BLAST/ETH (#2602421), diverifikasi dari event `Swap` on-chain:

| | ETH |
|---|---|
| tanpa fee & impact | 0,00948466 |
| − fee pool **4,00%** (tarif pool itu) | −0,00037939 |
| − price impact **2,19%** (pool bergeser 423 tick) | −0,00019946 |
| diterima | **0,00890581** |

Total **6,1%**, sedangkan kartu menulis **3,8%**. Memisahkannya penting karena
tindakannya beda: fee pool cuma bisa dihindari dengan pindah pool (dan routing
sudah memilih yang termurah), sedangkan impact dikecilkan dengan memperkecil
jumlah swap.

**`key[2]` BUKAN fee yang benar-benar dibayar — pakai `v4_fee_ppm()`.** Dua sebab,
dan yang pertama mengenai SEMUA pool:

- **Protocol fee duduk DI ATAS LP fee** dan tidak ada di PoolKey. Terukur pada dua
  pool Robinhood, dibandingkan dengan field `fee` di event `Swap` on-chain:

  | pool | `key[2]` | `slot0.lpFee` | protocolFee | fee NYATA (event) |
  |---|---|---|---|---|
  | BLAST/USDG | 48900 (4,8900%) | 48900 | 1000 (0,1%) | **49852 (4,9852%)** |
  | BLAST/ETH | 40000 (4,0000%) | 40000 | 1000 (0,1%) | **40960 (4,0960%)** |

  Kartu yang memakai `key[2]` karena itu selalu MENGECILKAN fee, dan selisihnya
  bocor ke angka "price impact" yang jadi terlihat lebih besar dari kenyataan
  (terukur pada close BLAST/USDG: impact dilaporkan 0,19% padahal 0,09%).

- **Fee dinamis** (`key[2] >= 0x800000`) sama sekali tidak punya nilai di PoolKey.
  `slot0.lpFee` selalu berisi yang SEDANG berlaku, jadi membacanya dari sana
  menangani kedua kasus sekaligus. Pool ber-fee dinamis di v4 **wajib punya hook**,
  dan bot melewati semua pool ber-hooks — jadi ini jaring pengaman, bukan jalur
  utama. Jangan simpulkan "pool v4 fee-nya dinamis jadi angkanya tidak bisa
  dipastikan": untuk pool yang bot pakai, fee-nya statis dan terbaca pasti.

Rumus gabungannya persis `ProtocolFeeLibrary.calculateSwapFee` Uniswap:
`p + lp − (p × lp) / 1e6` — **bukan** `p + lp × (1 − p)`. Terbukti:
`1000 + 48900 − 48 = 49852`, cocok dengan event; rumus yang salah memberi 49851.
Protocol fee berbeda per ARAH: 12 bit bawah untuk zeroForOne, 12 bit atas untuk
oneForZero.

Dipakai di EMPAT tempat yang semuanya dulu memakai `key[2]`: penjaga price impact
`v4_swap`, fallback minOut saat quoter gagal, `swap_impact_v4` (kartu), dan
`fee_ppm` yang dilaporkan `close_v4`.

**Quote NATIVE: `got` dari delta saldo ikut terpotong GAS tx swap itu sendiri.**
Terukur 0,0000124 ETH = 0,14% pada swap $22 — dan makin kecil swapnya makin besar
porsinya. `close_v4` menambahkan `gasUsed × effectiveGasPrice` kembali.

**Pembanding dihitung dari pool yang BENAR-BENAR dipakai.** `v4_swap(..., out=dict)`
mengisi `out["key"]`/`out["fee_ppm"]`; routing bisa memindahkan pool dan tiap pool
punya fee sendiri, jadi memakai `key` yang dikirim pemanggil menghasilkan angka
yang salah begitu rute berpindah.

Terukur di #1774674: 5.859,04 MEME → **261,00 USDG**, nilai wajar $269,57 → biaya
**3,2%**, yaitu fee pool 1,51% + price impact 1,7%.

**Cara memastikan routing benar-benar bekerja** (dipakai sekali dan berhasil):
ambil `poolId` dari event `Swap` PoolManager di tx swapnya, susun PoolKey dari
calldata UniversalRouter lalu buktikan dengan hash, terakhir panggil
`quoteExactInputSingle(...).call(block_identifier=<blok−1>)` untuk SEMUA kandidat.
Quote historis itu yang menyelesaikan perdebatan — untuk BLAST hasilnya pool yang
dipakai memang terbaik (+5,04% di atas alternatif terdekat), jadi 3,8% itu tarif
pool, bukan bug. Jangan menilai rute dengan quote SEKARANG: harga sudah bergeser.



`close_position`/`close_v4`/`reduce_v2` memotret saldo kedua sisi sebelum eksekusi,
lalu auto-swap hanya menjual selisihnya. Dulu ketiganya menjual **seluruh saldo**
token itu di wallet — token yang user pegang untuk keperluan lain ikut terjual.
Kalau selisihnya tak terbaca (RPC lag), lebih baik lewati daripada menebak: saldo
lama tidak boleh disentuh.

### Compound: reinvestasi fee ke posisi yang sama

`compound_any()` — tombol ♻️ Compound di kartu posisi. Beda per versi:

- **v4** (`_compound_v4`) — likuiditas dihitung LANGSUNG dari kedua sisi fee
  (`liquidity_for_amounts(sqrtp, lo, hi, f0, f1)`), tanpa collect dan **tanpa swap**.
  Jangan diganti dengan memanggil `add_any()`: jalur itu menghitung komposisi dari
  budget lalu menukar sebagian dari saldo WALLET — padahal fee v4 sudah dua sisi, dan
  wallet belum tentu punya quote sebanyak itu (terukur: fee $45,58 sedangkan wallet
  16,69 USDG).
- **v3** — fee mengendap di `tokensOwed`, jadi WAJIB collect dulu; jumlah yang
  ditambahkan dihitung dari selisih saldo NYATA sebelum/sesudah collect, bukan dari
  angka yang dilaporkan.
- **v2** — sudah auto-compound, ditolak dengan pesan.

**Aksinya `CLOSE_CURRENCY` per sisi, BUKAN `SETTLE_PAIR`.** Fee yang terpakai bisa
lebih kecil dari fee yang tersedia (rasio ditentukan range + harga), sehingga
delta-nya positif — kita yang menerima. `SETTLE_PAIR` menolak keadaan itu dengan
`DeltaNotNegative(address)` (selector `0x3351b260`, terbukti saat disimulasikan).
`CLOSE_CURRENCY` (0x12) menyelesaikan satu currency tanpa perlu tahu arah deltanya.

Sisa yang tidak muat **dikirim ke WALLET**, bukan ditinggalkan sebagai fee —
`CLOSE_CURRENCY` mengambil delta positif. Terbukti di tx `0x268953d5…`: 5.705,57
CHILL masuk wallet dan fee unclaimed turun ke ~$0. UI wajib menyebutnya, kalau tidak
nilai posisi sesudahnya terlihat lebih kecil dari perkiraan dan dikira dana menyusut.

**Konversi USD memakai `raw`, bukan harga per-unit-manusia.** `u0`/`u1` itu raw, jadi
`u0 * raw` sudah menghasilkan raw sisi lawan. Memakai `raw × 10**(mdec-qdec)` menggandakan
faktor desimal dua kali — terukur melaporkan **$9.982.236,6 juta** untuk compound yang
sebenarnya $30,28, dan angka itu ikut tertulis ke `history.json` sebagai deposit.
Terukur di CHILL #1011495: fee 20,3177 USDG + 9.240,35 CHILL, terpakai 20,3136 USDG
+ 3.918,96 CHILL, gas 197.924 (~0,000009 ETH).

### Add v4 juga `CLOSE_CURRENCY`, bukan `SETTLE_PAIR`

Alasannya sama persis dengan compound, dan jalur add sempat terlewat.
`INCREASE_LIQUIDITY` mengkreditkan `feesAccrued` terhadap tagihan; kalau komposisi
yang dibutuhkan tidak memakai habis fee di salah satu sisi — lazim untuk posisi OUT
of range yang butuh ~100% satu sisi — delta sisi itu jadi POSITIF dan `SETTLE_PAIR`
menolak dengan `DeltaNotNegative(address)` (selector `0x3351b260`).

Terbukti di RAM #1308102 (posisi butuh ~100% WETH + 0% RAM, fee RAM $3,99
menganggur): disimulasikan pada posisi hidup, `SETTLE_PAIR` revert
`DeltaNotNegative(0x5173d45a…)` sedangkan `CLOSE_CURRENCY` per sisi SUKSES. Gejala
di UI: *"Add v4 gagal 3×. Simulasi tx gagal (tidak dikirim)"* — jadi tidak ada dana
bergerak, tapi add tidak pernah bisa jalan.

Konsekuensi yang wajib disebut UI: sisa fee yang tidak terpakai mendarat di
**WALLET**, bukan tetap unclaimed.

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

### Kartu hasil mint: sebut yang NYATA masuk, bukan budget

`mint_v4`/`mint_position` mengembalikan `"deposited": budget` — itu **rencana**,
sedangkan `deposited_usd` nilai **nyata** yang masuk posisi. Menaruh keduanya di satu
baris ("Deposited ~246,093 USDG ($235,71)") membuat user membaca selisihnya sebagai
kerugian $10, padahal sebagian besar cuma budget yang tidak terpakai dan masih ada
di wallet.

Terukur di NUDES #2298038 — enam tx, dan biayanya cuma di SATU tempat:

| langkah | masuk | keluar | biaya |
|---|---|---|---|
| wrap | 0,097697018 ETH | 0,097697 WETH | 0 (1:1) |
| swap WETH→USDG | $241,21 | 241,153925 USDG | **$0,06** (0,02%) |
| swap komposisi USDG→NUDES | 122,194841 USDG | 16.103,49 NUDES ($117,20) | **$4,99** (4,08%) |
| mint | 119,435324 USDG + 16.100,27 NUDES | posisi $235,71 | — |

Gas keenam tx 0,000126 ETH (~$0,31), sisa 3,22 NUDES ($0,02) di wallet. Jadi
kerugian nyata **~$5,4**, bukan $10,4 — dan semuanya di swap komposisi (fee pool 4%
+ price impact), bukan di mint.

`mint_v4` karena itu ikut mengembalikan `in_quote`/`in_meme` (jumlah NYATA kedua
sisi) dan kartu menyebutnya; budget disebut terpisah sebagai keterangan.

### Gas dilaporkan otomatis di semua alur

`wait_ok()` menghitung `gasUsed × effectiveGasPrice` tiap tx dan menjumlahkannya di
`_GAS_WEI`. Penghitung direset oleh `set_progress(fn)` — jadi tiap alur melapor
biayanya sendiri tanpa menyentuh fungsi mana pun satu per satu. UI memanggil
`gas_line(cid)` → `ch.fmt_gas()` di kartu hasil (terukur: "0.000010 ETH (~$0.03)"
untuk alur 2 tx).

### Pindah pool = rebalance dengan `target_pool`

Mode `"same"` mempertahankan **rentang harga** posisi lama, bukan cuma lebarnya.
Tiga mode lain (`wide`/`lower`/`upper`) memakai lebar lama tapi **dipusatkan di harga
sekarang** — itu yang selama ini bikin range bergeser saat pindah pool.

Tick TIDAK bisa disalin mentah antar pool: skala harganya beda kalau quote-nya beda
dan kisinya beda kalau fee-nya beda. `ticks_for_same_band()` karena itu mengonversi
lewat harga USD (batas MC — MC = harga × supply, dan supply-nya sama), lalu
membulatkan **KE LUAR** ke kisi pool tujuan: lebih baik sedikit lebih lebar daripada
memotong sisi yang user harapkan tetap tertutup. Terukur RAM #1481226, WETH 2% →
USDG 3% (kisi 300): tick `92800..102400` → `-301200..-291300`, MC
$4.319.631–$11.281.023 → $4.257.653–$11.457.776. Tick tujuan NEGATIF — bukti kenapa
menyalin tick apa adanya akan salah total (USDG 6 desimal vs RAM 18, orientasi quote
juga terbalik).

**`"same"` tidak boleh diteruskan ke mesin mint.** `_range_of()` mengembalikan mode
EFEKTIF dari letak range terhadap harga, dan `mint_v4` menolak kalau tidak cocok
dengan `strategy["mode"]` — jadi `"same"` akan selalu gagal di situ. Alurnya
menurunkan `cmode = effective_mode(lo, hi, tick_tujuan, q_is_t1_tujuan)` lebih dulu,
memakainya untuk komposisi dana DAN sebagai `strategy["mode"]`, lalu menaruh tick
hasil konversi di `strategy["ticks"]`.

Komposisi dihitung dari geometri pool TUJUAN (di situlah dananya mendarat), bukan
pool asal — `plan_two_sided` hanya MEMBAGI total secara proporsional, jadi totalnya
boleh tetap dalam satuan quote lama.

`rebalance_position(..., target_pool=dict)` menutup posisi di pool lama lalu mint di
pool itu (mis. fee tier 5% → 2%). Dua penjagaan WAJIB, jangan dilemahkan:

- **Token meme harus sama.** Kalau beda, itu bukan pindah pool melainkan tukar
  aset — ditolak UI.
- **Quote boleh beda.** Hasil close ditukar lewat `_convert_quote()` sebelum mint.
  Helper itu sadar ETH native di KEDUA sisi (`swap_any` cuma mengerti ERC20, jadi
  native di-wrap dulu / di-unwrap sesudahnya) dan membaca jumlah yang benar-benar
  diterima dari delta saldo, bukan estimasi. Mode `upper` dilewati: budget-nya dalam
  satuan meme, dan meme-nya sama di kedua pool.
- **`assert_pool_orientation(w3, dest, chain_id)`** dipanggil untuk pool tujuan —
  dict-nya berasal dari pilihan UI, jadi tidak boleh dipercaya begitu saja.

Pindah lintas-quote menambah fee + slippage satu swap lagi dan totalnya 4–6 tx —
UI wajib menyebutnya supaya user tahu ongkosnya lebih mahal dari pindah sesama quote.

Pembukuannya sama persis dengan rebalance (`finish_rebalance()` dipakai berdua):
event `close` + `fees` untuk posisi lama, `mint` untuk yang baru, dan `drop_ref`/
`add_ref` untuk v4.

### Persen PnL: terhadap modal BERSIH, bukan deposit kumulatif

Tiap rebalance / pindah pool / compound mencatat `close` + `mint` baru, jadi
`deposits` dan `withdrawals` menggelembung oleh dana yang sama didaur ulang. Memakai
`deposits` sebagai penyebut membuat kerugian terlihat jauh lebih kecil dari yang
dirasakan — terukur di satu wallet: **−3,19% terhadap deposit kumulatif $67,4k**
padahal **−26,48% terhadap modal bersih $8,1k**, dari 541 siklus.

Penyebutnya `deposits − withdrawals`, dan UI menyebut jumlah siklusnya
(`store.churn_count()`) supaya dua angka besar itu tidak disalahartikan sebagai
modal segar.

Angka PnL dolarnya sendiri sudah benar sejak awal:
`withdrawals + fees_claimed + open_value + unclaimed − deposits`.

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

### Harga wrapped: pool TERDALAM, bukan tier pertama

`quote_usd_price()` dulu memakai `for fee in fee_tiers(...)` dan mengembalikan pool
pertama yang ada, apa pun kedalamannya — pool debu ber-fee 0,01% menang atas pool
yang benar-benar diperdagangkan. Terukur di HyperEVM: **WHYPE terbaca $62,50 padahal
pasar $80,22 (−28%)**, dan SELURUH nilai posisi di chain itu ikut salah karena sisi
meme pun dihargai dalam wrapped. Sekarang lewat `find_pool_dex()` yang mensyaratkan
`liquidity() > 0` dan memilih terdalam lintas DEX; sesudahnya WHYPE $80,36 (meleset
0,17% dari GeckoTerminal).

### `/recover`: baca ulang posisi v4 dari chain

`find_v4_positions()` menggabungkan **indexer Uniswap** dan **Krystal**
(`krystal_user_positions()`, endpoint `all/v1/lp/userPositions?addresses=` — sumber
yang sama dengan defi.krystal.app/account/<addr>/positions). Krystal mengindeks lebih
banyak protokol dan terbukti menemukan posisi yang indexer lewatkan: terukur di satu
wallet **indexer 8, gabungan 11**. Keduanya dipakai (`uniswap_v4_token_ids()`,
endpoint yang sama dengan app.uniswap.org/positions): ia tahu SEMUA posisi wallet
berapa pun umurnya. Jalur event `Transfer` PositionManager cuma cadangan — getLogs
dibatasi rentang blok, dan di RPC pelit cakupannya cuma beberapa jam sehingga posisi
lama tidak akan pernah ketemu (terukur di VPS: `/recover` melaporkan **0 NFT**
padahal indexer menyebut **8**). Ini jaring pengaman untuk registry
`history.json` yang bolong — v4 tidak bisa dienumerasi on-chain, jadi tanpa registry
posisi lenyap dari UI walau dananya utuh.

Terukur di satu wallet: **6 posisi hidup ditemukan dalam 8 detik** (BONER $47,77,
CHILL $32,67, VYNEX $116,57, dan tiga lainnya) — semuanya tak terlihat di `/list`
karena mint-nya sempat dilaporkan gagal.

Hasilnya pemulihan, bukan sumber kebenaran: di RPC yang pelit cakupannya parsial.

### `ensure_native_balance` JANGAN keluar saat WETH kosong

Dulu ada `if wbal <= 0: return txs` tepat sebelum loop "jual quote lain". Akibatnya
wallet tanpa WETH tapi ber-USDG banyak tetap gagal mint pool ber-quote ETH:
*"Saldo native+WETH kurang: punya 0.016259, butuh 0.058918 + gas"* — padahal
`compute_amount` sudah menghitung USDG itu sebagai modal. Jalur penjualan quote lain
itulah yang menutup kekurangannya, dan ia tidak pernah tercapai.

Kegagalan penjualan per-quote sekarang dilaporkan lewat `_step()`, tidak lagi
ditelan `except: continue`.

### Satu posisi = satu aksi pada satu waktu

`concurrent_updates` membuat dua klik diproses PARALEL. `TX_LOCK` menyerialkan
**transaksinya**, tapi kedua alur sudah membaca posisi SEBELUM lock — jadi keduanya
memakai snapshot yang sama dan menghitung jumlah dari angka yang sudah basi.

Terbukti di v4:1300787 (NBHOODS): "Reduce 50%" terklik dua kali, dan tiap alur
menghapus **3.760.957.351.020.571** likuiditas — setengah dari nilai AWAL, bukan
setengah dari sisa. Yang kedua karena itu menghabiskan seluruh sisanya dan posisi
tinggal `liquidity = 1`. Dananya utuh (2 × 49,99 USDG kembali ke wallet, total
99,98 dari deposit 99,98), tapi hanya satu penarikan yang tercatat sehingga kartu
melapor "Rugi −$49,99 (−50%)" dan user melihat posisinya jadi $0.

`position_busy(update, pid)` mengklaim posisi di awal `do_add_exec`,
`do_reduce_exec`, `do_collect`, `do_rebalance`, `do_close`, dan `do_compound`.
Klik kedua ditolak dengan pesan, bukan dijalankan. Pid berbeda tidak saling
menghalangi.

`assert_position_open()` TIDAK cukup untuk kasus ini — posisinya memang masih
terbuka saat alur kedua jalan; yang salah jumlahnya, bukan keberadaannya.

### Aksi ke posisi yang sudah tertutup

**Posisi yang sudah tertutup BUKAN error — `AlreadyClosed`.** Kelas turunan
`RuntimeError` (jadi semua `except` lama tetap menangkapnya) yang dipakai
`assert_position_open()` dan `close_v4`. UI menampilkannya sebagai ✅, bukan ❌,
karena tidak ada dana yang bergerak dan hasil close-nya sudah di wallet.

**Jangan mencocokkan teks error untuk mendeteksinya** — bunyinya bervariasi dan dua
varian sudah kejadian: `NOT_MINTED` (tx masuk blok lalu revert) dan
*"Transaction with hash … not found"* (`wait_ok` menyerah sebelum node mengenali
tx-nya, tx menyusul masuk blok lalu revert). Yang menentukan keadaan on-chain:
`close_v4` menangkap kegagalan APA PUN lalu memeriksa `ownerOf` sekali.

Terbukti di #1740532: burn SUKSES di blok **54079998**, tx susulan revert 25 blok
kemudian di **54080023**, dan hasilnya — 71,2994 MEME (event Transfer) + ~0,0416 ETH
**native** (tanpa event, jadi tak terlihat di scan log) — sudah ada di wallet sejak
awal. `rebalance_position()` ikut memanggil `assert_position_open()` supaya jalur
rebalance dan pindah pool memberi sinyal yang sama.

Dua alur yang berjalan berdekatan (tombol Close tertekan dua kali) membuat yang
kedua mengirim tx ke posisi yang sudah di-burn: tx masuk blok lalu revert
`NOT_MINTED`, gas terbakar percuma, dan pesannya bikin user mengira close-nya gagal
padahal yang pertama sukses. Terbukti di #1102018 — burn sukses di blok 48918344,
tx kedua revert 26 blok kemudian.

`close_any()` memanggil `assert_position_open()` lebih dulu dan menolak dengan
pesan "sudah tertutup" tanpa mengirim apa pun.

**`wait_ok()` juga mendekode alasan revert**: kalau receipt status 0, call-nya
diulang di blok sebelumnya untuk mendapat sebabnya. "Tx close v4 FAILED" tanpa
alasan memaksa user menebak; sekarang pesannya menyertakan `NOT_MINTED`, `STF`,
dan sejenisnya.

### Allowance Permit2 DIPOTONG tiap dipakai — jangan approve pas-pasan

`AllowanceTransfer` Permit2 mengurangi allowance setiap kali spender menariknya.
`ensure_permit2()` dulu meng-approve persis `need_wei`, jadi begitu satu tx sukses
sisanya ~0 dan percobaan berikutnya gagal `InsufficientAllowance(uint256)` (selector
`0xf96fb071`; argumennya = sisa allowance, terukur 10000 wei).

Akibatnya jauh lebih buruk dari sekadar gagal: mint/add yang tx pertamanya SUKSES
dilaporkan "gagal 3×" karena dua percobaan sisanya mentok di preflight — dan user
menambah dana dua kali.

Dua penjagaan sekarang:

- approve dengan **margin 2×** jumlah tx itu (tetap terbatas, kedaluwarsa tetap 1 jam);
- kalau preflight tetap balas `0xf96fb071`, allowance disetel ulang sebelum percobaan
  berikutnya, bukan mengulang tiga kali dengan sebab yang sama.

### Retry yang mengirim setoran KEDUA

`mint_v4`/`increase_v4` mencoba 3×. Kalau percobaan pertama sebenarnya SUKSES tapi
`wait_ok`/`_preflight` menyimpulkan gagal, percobaan berikutnya menambah dana LAGI.

Terbukti di RAM #1308102: dua `increase` identik
(**+4.649.204.726.935.655.993** likuiditas) di blok **51026498** dan **51026523**,
selang 25 blok. User mengklik sekali, menyetor dua kali; kartu hasil cuma menyebut
tx yang kedua. Untuk `mint_v4` akibatnya lebih parah — posisi KEDUA lahir dengan
modal baru.

`_recover_sent()` sudah ada tapi dulu cuma dipanggil SESUDAH ketiga percobaan habis
— terlambat. Sekarang dipanggil di AWAL tiap percobaan ulang: kalau ada tx terkirim
yang receipt-nya status 1, alurnya berhenti dan memakai tx itu.

Aturannya: jalur tx apa pun yang punya loop retry WAJIB memeriksa receipt tx yang
sudah terkirim sebelum mengirim yang baru. Mengirim ulang itu aman hanya untuk tx
yang IDENTIK (nonce + tanda tangan sama, seperti `_rebroadcast`), bukan untuk tx
baru yang dibangun ulang.

### Mint yang sukses tapi dilaporkan gagal = dana hilang dari UI

`mint_v4`/`increase_v4` mencoba 3× dan menyerah kalau `wait_ok`/`_preflight` gagal.
Tapi salah satu percobaan bisa SUDAH masuk blok dengan sukses sementara percobaan
berikutnya revert — dan kalau alurnya tetap melempar error, `store.add_ref()` tidak
pernah dipanggil. v4 **tidak bisa dienumerasi on-chain**, jadi posisi itu lenyap dari
UI padahal uangnya di dalamnya.

Terjadi sungguhan: rebalance microduck melapor *"Mint v4 gagal 3×"*, padahal tx
`0x34f14b15…` sukses dan melahirkan NFT **1026584 berisi 171,97 USDG**. Urutannya
terbaca jelas di explorer: close 22:16:25 → swap 22:16:26 → **mint OK 22:16:28** →
mint error 22:16:31.

`_recover_sent()` karena itu memeriksa receipt SEMUA tx yang benar-benar terkirim
sebelum menyerah, dan memakai yang statusnya 1. Jangan hapus: tanpa itu satu
kekeliruan pembacaan sama dengan kehilangan posisi.

### Menulis `history.json`: tiga syarat, semuanya sudah pernah bocor

Tiga hal ini bersama-sama pernah **menghapus seluruh registry posisi** begitu
`concurrent_updates` dinyalakan. Terukur pada 8 thread × 40 event: registry
**0 dari 50 ref** tersisa dan cuma **1 dari 320 event** tersimpan. Gejalanya persis
"posisi saya hilang" — padahal dananya utuh on-chain.

- **Nama file sementara harus unik per penulis.** `_write()` dulu memakai
  `path.with_suffix(".tmp")`, satu nama untuk semua. Dua penulis (bot multi-thread,
  atau bot + web) menulis ke tmp yang sama lalu sama-sama rename, jadi yang mendarat
  bisa sambungan dua JSON. Sekarang `history.json.<pid>.<tid>.tmp`; rename POSIX
  tetap atomik jadi file tidak pernah setengah jadi.
- **Baca yang gagal JANGAN di-cache.** File yang sempat rusak membuat `_hist()`
  balik `{"events": {}}`, dan dulu default itu ikut tersimpan sampai mtime berubah —
  registry terlihat kosong padahal isinya ada. Lebih buruk: mutator berikutnya
  menulis ulang dari isi kosong itu, jadi kerusakan sementara menjadi permanen.
- **Baca-ubah-tulis harus dikunci.** Semua mutator polanya sama, jadi dua penulis
  membaca isi yang sama lalu saling menimpa. `_hist_write()` memegang `RLock`
  (antar-thread) **dan** `fcntl.flock` di `.history.lock` (antar-proses — `web.py`
  proses terpisah yang menulis file yang sama). Sesudahnya: 3 proses × 4 thread ×
  40 event = **480/480 tersimpan, 50/50 ref utuh**.

Konsekuensi untuk kode baru: mutator apa pun WAJIB `with _hist_write():` dan
`_hist(fresh=True)`. Objek dari `_hist()` polos dipakai bersama semua pembaca —
memutasinya mengubah apa yang dilihat pemanggil lain, dan menulis dari salinan basi
menghapus perubahan proses lain.

### Satu tombol = satu posisi, dan gagal baca BUKAN "tidak ditemukan"

`position_by_pid()` membaca satu posisi langsung dari pid-nya. Dulu 17 tempat di
`bot.py` memindai seluruh `list_all_positions()` lalu menyaring pid — dua kerugian:

- **Mahal.** Terukur `reb|v4:1277501` 24,7 detik, dan langsung 3,12s vs 15,14s
  untuk 17 posisi (nilai identik). Ongkos RPC-nya ikut ~5× lebih kecil, jadi ini
  juga yang paling meredakan 429.
- **Rapuh.** `list_all_positions` sengaja menelan kegagalan per-posisi supaya daftar
  tetap tampil. Satu 429 membuat posisi yang dicari lenyap dari hasil dan UI melapor
  **"tidak ditemukan (sudah ditutup?)"** — user mengira dananya hilang, lalu mengklik
  ulang dan beraksi dua kali. `position_by_pid` MELEMPAR kegagalan baca; hanya `None`
  yang berarti benar-benar tidak ada.

Aturannya: jalur satu-posisi pakai `position_one()`/`position_by_pid()`, jalur daftar
pakai `list_all_positions()`. Jangan mencari satu posisi lewat daftar.

### Harga pool rusak = mint harus DITOLAK, bukan cuma ditandai

`assert_pool_price_sane(w3, cid, pool_info)` dipanggil di awal `mint_position()` dan
`mint_v4()`, SEBELUM tx apa pun (termasuk approve/permit2).

Kejadian nyata: pool HOME/USDG fee 3,60% di Robinhood harganya terkunci di tick
**887271** — satu tick dari MAX_TICK. Pool itu menghargai 1 HOME = **2,94e-27 USDG**
sedangkan pasar **$0,00030730**: meleset ~1e23 kali. Kartu konfirmasi menampilkan
"MC $0.00", "Value deposited 119.517,82 HOME ($0.00)", dan "Current price 0.0₂₀0" —
bot SUDAH tahu angkanya omong kosong, lalu tetap mint. Modalnya lenyap.

Pool ini lolos karena datang dari **jalur Krystal**, yang sengaja cuma MENANDAI
deviasi (`p["deviation"]`) tanpa membuang (lihat bagian Krystal). Untuk ditampilkan
itu benar; untuk memasukkan dana, tidak.

Dua penjagaan:

- **Tick pool tidak boleh mepet ±887272.** Harga mentok = bukan harga pasar.
- **Harga pool vs `token_usd_price()`** (sumber independen) maksimal
  `_POOL_PRICE_MAX_RATIO` = 20x. Longgar disengaja: yang dikejar pool rusak, bukan
  pool mahal. Kalau salah satu harga tak terbaca, mint TIDAK dihalangi.

**`priceUsd` DexScreener selalu milik `baseToken` — WAJIB dicocokkan.** API-nya
mengembalikan pair di mana token yang dicari justru jadi QUOTE, dan harganya milik
token lain. Terbukti di FATCOIN: pair terlikuid `LLY/FATCOIN` ($539.592) membawa
`priceUsd` **1147,36** yaitu harga LLY; angka itu jadi "harga pasar FATCOIN" lalu
memblokir mint ke pool yang harganya justru benar ($0,0203) dengan pesan
*"meleset 56.726x"*. Filter `baseToken.address == token` menutupnya — sesudahnya
FATCOIN $0,02026, cocok dengan pool.

Ini juga sempat menyesatkan diagnosis: ROBINVAULT terbaca $1,35 dari pair
mis-orientasi, padahal harga sebenarnya **~$0,0044** (pool ETH 5,3% $0,00445, pool
USDG 4% $0,00438, 5 pair DexScreener ~$0,00425 — semuanya sepakat). Yang rusak justru
pool ETH 5% ber-TVL $41.688 yang tick-nya **887271 = MAX_TICK−1**. Pelajarannya: satu
sumber harga yang menyimpang jauh dari SEMUA sumber lain adalah sumbernya yang salah,
bukan pool-nya — cek beberapa pool on-chain sebelum menyimpulkan.

**Patokan pasarnya sendiri bisa rusak, dan itu memblokir mint yang SAH.**
`token_usd_price()` memilih pool ber-saldo quote terbesar, dan pool yang harganya
mentok di batas kisi tetap lolos filter itu karena masih memegang >$10 quote.
Terukur: RAM di Robinhood dihargai **$3,40257e+50** (= raw 3,402568e38 di MAX_TICK
× 1e12 selisih desimal), lalu angka itu dipakai sebagai pembanding dan membatalkan
rebalance ke pool yang harganya justru wajar ($0,295 vs pasar $0,180 — cuma 1,64x).

Dua penambalan, keduanya perlu:

- `token_usd_price()` membuang pool ber-`raw` di luar `1e-36..1e36`. Batas itu
  HANYA menangkap ujung kisi: pasangan desimal 18/6 dengan token semurah 1e-18 pun
  cuma menghasilkan raw ~1e30.
- `assert_pool_price_sane()` mengabaikan patokan yang di luar `1e-30..1e7` — angka
  mustahil tidak boleh dipakai memblokir apa pun.

Sesudahnya: RAM $0,180, RADIO $0,001206, HOME $0,0003841, WETH $2.465,45 (semuanya
wajar), pool HOME rusak TETAP ditolak, dan 8 pool posisi hidup lolos semua.

**Orientasi desimal gampang terbalik dan gagalnya senyap.** `quote_is_token1` False
berarti quote itu **token0**, jadi `dec0 = desimal quote`. Terbalik sekali saat
ditulis, dan akibatnya SEMUA pool sehat ditolak dengan deviasi ~1e24 (persis faktor
`10**(18-6)` dikuadratkan). Uji apa pun perubahan di sini terhadap pool posisi hidup
dengan kedua orientasi quote — 8 pool nyata dipakai saat ini, termasuk HOOD10/USDG
dan INJOH/USDG yang quote-nya token1.

### Deposit ETH native harus menyisakan gas

Untuk pool v4 ber-`currency0` native, `value` tx = `a0max`. Budgetnya dihitung dari
native + WETH + quote lain, lalu `ensure_native_balance()` menaikkan saldo native
seperlunya — tapi gas tx MINT-nya sendiri belum masuk hitungan, jadi `value` bisa
mendarat persis sebesar saldo. Terukur: punya **150.350.143.094.057.117** wei, butuh
**150.971.630.123.489.580** → simulasi ditolak
`insufficient funds for gas * price + value`, selisihnya persis ongkos gas
(0,000621 ETH), padahal `gas_reserve_wei` 0,001236 ETH seharusnya menutupinya.

`_fit_native_value()` dipanggil di `mint_v4` dan `increase_v4` tepat sebelum
`a0max` dihitung: kalau `a0max > saldo − cadangan gas`, **likuiditasnya** yang
diskalakan, bukan cuma value-nya dipotong — memotong value saja membuat tx revert
karena jumlah yang ditarik posm tidak ikut berubah. Terukur: deposit 0,335109559 →
0,333845815 ETH, menyisakan 0,001263744 untuk gas.

### Rentang yang modalnya tidak bisa kembali (kerugian nyata)

`assert_range_recoverable()` dipanggil di SEMUA jalur yang memasukkan dana ke posisi
(mint v3/v4, add v3/v4) dan membatalkan sebelum tx dikirim.

Kejadian nyata di Robinhood: **119.485,589 HOME** masuk ke posisi v4 #1281406 di tick
**876240..887220** dengan likuiditas cuma **15.373** (tx `0x6c2687cd…`). Empat menit
kemudian posisi dibakar (`0x7d8e839e…`, `ModifyLiquidity −15.373`) dan
mengembalikan **NOL** — tx itu cuma punya 2 log, tanpa satu pun transfer token, dan
sapuan penuh kedua wallet di seluruh rentang blok (56/56 query sukses) menemukan
hanya SATU transfer: 119.485 HOME keluar. Uangnya tidak nyangkut di kontrak, sudah
pindah ke lawan trading.

Sebabnya granularitas wei, bukan bug pembacaan:

```
L=15373, tick 876240..887220
  di batas ATAS : token1 = 1,19e23 wei  (119.485,589 HOME — seluruh modal)
  di batas BAWAH: token0 = 6,1e-16 wei  (di bawah SATU wei → nol)
```

Tick atasnya cuma **52** dari MAX_TICK (887272). Di rasio harga ~1e38, satu wei sisi
lawan bernilai ~1e20 token, jadi begitu harga melintas turun, sisi yang seharusnya
diterima posisi membulat jadi nol dan modal lenyap seluruhnya. Ini TIDAK terlihat
sampai harga benar-benar melintas.

Dua aturannya, keduanya perlu:

- **Rentang tidak boleh mepet batas kisi** (±887272, margin 100 tick).
- **Tiap sisi ≥ `_MIN_SIDE_WEI` (1000 wei)** saat harga ada di batasnya —
  `amounts_from_liquidity` dievaluasi di `_sqrt_at_tick(lo)` dan `_sqrt_at_tick(hi)`.
  `_sqrt_at_tick` memakai `Decimal` presisi 80: di tick ±887k float64 kehabisan digit.

Diuji: kasus HOME ditolak, `L=1` dan `L=1000` di tick normal ditolak, dan **6 posisi
hidup sungguhan lolos semua** (L 1e15–4,6e19). Jangan longgarkan tanpa menguji ulang
terhadap posisi hidup — penjagaan yang salah tuning memblokir mint yang sah.

### Gagal dibaca ≠ tidak ada

`list_all_positions()` dulu membuang exception per-posisi sama seperti hasil `None`.
Padahal artinya beda: `None` = posisi memang kosong, exception = **belum tahu**.
Akibatnya RPC yang kena 429 membuat sebagian posisi lenyap dari `/list` tanpa jejak,
nilai portfolio ikut terlihat menyusut, dan user mengira dananya hilang (terukur:
16 ref di registry, yang muncul 8).

Sekarang tiap ref dicoba **3×** (jeda 0,4/0,8 detik, `get_w3` diambil ulang tiap
percobaan sehingga endpoint yang kena rate limit sudah dirotasi), dan yang tetap
gagal masuk ke parameter `errors`. UI **wajib** menyebutnya — `cmd_list` menulis
"N posisi GAGAL dibaca (RPC sibuk) — belum tentu tertutup".

tokenId yang tidak ada tetap mengembalikan `None` tanpa exception, jadi tidak ikut
terlapor sebagai error (diuji dengan tokenId palsu: 0 error).

### Rate limit RPC: rotasi endpoint, bukan menunggu

**Beberapa API key Alchemy didukung** lewat `alchemy_keys()`: `ALCHEMY_API_KEY`,
`ALCHEMY_API_KEYS` (dipisah koma/spasi), `ALCHEMY_API_KEY_2..10`, **dan
`alchemy_keys.txt` (satu key per baris)** — semuanya di-UNION, dedupe dengan
urutan dipertahankan.

File-nya default di samping `chain.py`, dipindah lewat `ALCHEMY_KEY_FILE`, dan
**WAJIB ada di .gitignore** (kredensial, sama seperti `proxies.txt`; contohnya
`alchemy_keys.txt.example`). Dipakai karena menambah key = menambah baris,
tanpa mengurus nomor urut `_2..10`. Dua detail yang sengaja ada:

- **URL penuh diterima.** Yang paling gampang tersalin dari dashboard Alchemy
  adalah `https://<net>.g.alchemy.com/v2/<key>`, bukan key telanjang — ditempel
  apa adanya, `_alchemy_urls()` membangun URL di dalam URL dan endpoint-nya 404.
  Gejalanya cuma "lambat", karena `get_w3` diam-diam jatuh ke RPC publik.
  `_clean_alchemy_key()` mengambil bagian setelah `/v2/` dan membuang tanda kutip.
- **Cache berkunci `(mtime_ns, ukuran)`.** Key yang ditambahkan saat bot jalan
  langsung terpakai tanpa restart, tapi filenya tidak dibaca ulang tiap panggilan
  — `_chain_rpcs()` memanggilnya di jalur failover. Terukur 2.000 panggilan
  0,018 detik. Tiap key jadi **endpoint tersendiri** di `get_w3`, jadi tidak
ada mekanisme baru yang perlu ditulis — rotasi `_RPC_BAD` yang sudah ada langsung
bekerja: key yang kena 429 ditandai, dilewati, dan panggilan berikutnya jalan
lewat key berikutnya. Terukur: dengan key `aaa` ditandai kena limit, endpoint
terpilih berikutnya adalah key `bbb` — bukan RPC publik yang 10x lebih lambat.

**Jatah TIDAK dihitung per-network.** Terukur dengan `rpc_health()`: key `…Ev8C`
menjawab `Monthly capacity limit exceeded` di KEEMPAT chain sekaligus (Robinhood,
Base, BSC, HyperEVM) sementara key `…ei1f` sehat di semuanya. Jadi menambah
network tidak menambah jatah; menambah KEY hanya menolong kalau key itu milik
app/akun lain.

### `/rpc` — status endpoint, dan kenapa sisa kuota tidak bisa dibaca

`rpc_health(chain_id)` menembak satu `eth_blockNumber` murah ke tiap endpoint
(paralel, tanpa retry) dan mengklasifikasikannya: `ok` / `quota` / `burst` /
`error`. `/rpc` menampilkannya, `/rpc all` untuk semua chain.

**Sisa kuota tidak bisa dibaca dan jangan dicoba lagi.** Sudah diuji: header
jawaban Alchemy cuma memuat `x-alchemy-trace-id` (tidak ada sisa kuota),
`dashboard.alchemy.com/api/team-apps` dan `/api/compute-units` menjawab **404**
baik dengan API key sebagai `X-Alchemy-Token` maupun tanpa (endpoint dashboard
butuh token yang berbeda dari API key RPC), dan tidak ada metode JSON-RPC untuk
itu (`alchemy_getComputeUnits` → `-32600`).

Yang BISA dibaca pasti adalah key yang sudah HABIS — Alchemy menyebutnya
eksplisit di body 429. Satu request per endpoint sudah cukup, dan itu satu-satunya
cara mengetahuinya dari sisi bot.

Terukur saat ditulis, dan gambarannya penting: **Robinhood cuma punya SATU
endpoint sehat** (satu key Alchemy) — `rpc.mainnet.chain.robinhood.com` SSLError
dan blockscout 403 Cloudflare — sedangkan Base/BSC/HyperEVM punya 3–5 RPC publik
yang jalan. Jadi chain itu memang paling rapuh terhadap satu key bermasalah.
`hyperliquid-mainnet` juga menjawab *"not enabled for this app"* untuk key yang
sehat sekalipun: network Alchemy harus di-enable per app di dashboard.

`get_w3` men-cache satu endpoint 5 menit. Failover-nya dulu cuma ada di pemilihan
AWAL, padahal jatah habis di tengah jalan justru yang lazim — begitu endpoint itu
kena 429, semua panggilan gagal sampai cache kedaluwarsa dan user melihat
*"Collect gagal: … too many 429 error responses"*.

`_Provider.make_request` menandai endpoint-nya di `_RPC_BAD` lalu membuang cache
chain, jadi panggilan berikutnya memilih endpoint lain sendiri. `_is_rate_limited()`
mencocokkan teks exception karena urllib3 menghabiskan retry lalu melempar
`MaxRetryError`/`RetryError` — bukan objek HTTP yang status code-nya bisa dibaca.
Endpoint bertanda dilewati `_RPC_BAD_COOLDOWN` (120 detik), **tapi hanya kalau masih
ada pilihan lain** supaya chain ber-RPC tunggal tidak jadi mati total.

#### 429 Alchemy ada DUA jenis, dan bedanya menentukan segalanya

Terukur pada key user: satu key menjawab **`Monthly capacity limit exceeded`**
untuk SETIAP request — jatah bulanan habis, 100% gagal, tidak akan pulih sampai
siklus billing berganti. Key satunya sehat: 40 request berurutan 1,10 detik,
median 24 ms. Jadi "bot lambat" di chain itu sebagian besar adalah bot yang
mencoba key mati berulang kali.

`_rate_limit_kind()` membedakan `"quota"` dari `"burst"`, dan hukumannya beda
jauh: `_RPC_DEAD_COOLDOWN` **6 jam** vs `_RPC_BAD_COOLDOWN` **15 detik**.
Memperlakukan keduanya sama berarti key mati dicoba ulang tiap 2 menit selamanya,
dan tiap percobaan duduk di jalur klik user. Key yang mati **dicatat di log satu
kali** dengan nama (4 karakter terakhir) — dari luar gejalanya cuma "lambat", dan
tidak ada perubahan kode yang bisa memperbaikinya.

Burst 429 (throughput 300 CU/s) pulih dalam milidetik, jadi 15 detik sudah
kebanyakan. Dulu 120 detik, disetel waktu kedua jenis ini belum dibedakan — di
chain yang endpoint sehatnya cuma SATU itu berarti dua menit tanpa RPC layak.

**`raise_on_status=False` WAJIB di `_rpc_retry()`.** Tanpa itu urllib3 melempar
`RetryError` setelah retry status habis, dan objek itu TIDAK membawa response —
body-nya hilang, dan body itulah satu-satunya yang membedakan kedua jenis 429.
Dengan `False`, response 429 terakhir dikembalikan, `raise_for_status()` melempar
`HTTPError` ber-`.response` utuh, dan `_rate_limit_kind()` bisa membacanya.

#### Endpoint bermasalah DIURUTKAN, jangan dibuang

`get_w3` dulu menyaring `fresh_rpcs` dan memakai hanya itu. Itu mematikan chain:
eth-rpc Blockscout Robinhood menjawab **403 Cloudflare** ("Just a moment…"), dan
403 **bukan** rate limit sehingga ia tidak pernah ditandai — jadi selamanya
terhitung "segar", `fresh_rpcs` berisi dia saja, dan katup pengaman "kalau semua
ditandai, pakai semua" TIDAK PERNAH terpicu. Key Alchemy sehat yang cuma kena
burst kalah dari endpoint yang dijamin gagal, dan log penuh
*"Semua RPC Robinhood gagal"* yang cuma menyebut blockscout.

Sekarang `rpcs = sorted(rpcs, key=_bad_left)` — tidak ada yang dibuang, yang
sehat selalu dicoba dulu, yang sedang dihukum tetap jadi cadangan terakhir. Gagal
bukan-rate-limit (Cloudflare 403, SSL, timeout) ikut dihukum `_RPC_HARD_COOLDOWN`
600 detik, jadi ia tenggelam sendiri ke dasar urutan.

**Pesan gagalnya wajib menyebut SEBAB.** "HTTPError" saja memaksa user menebak,
padahal tiga sebab tersering butuh tiga tindakan berbeda: jatah bulanan habis
(tambah key / naikkan paket), Cloudflare menolak IP (pakai proxy/WARP), endpoint
menggantung (tidak ada yang bisa dilakukan selain pindah). `_why()` menarik
`error.message` dari body JSON-RPC dan mengenali halaman tantangan Cloudflare;
`_short_rpc()` menyensor API key jadi 4 karakter terakhir supaya aman di log.

#### 429 JANGAN pernah ditunggu — dua lapis retry sempat melakukannya

Sumber utama "bot lambat sekali" yang terukur: **satu panggilan RPC yang kena 429
memakan 20,01 detik lalu tetap gagal.** Bukan kerja, murni tidur. Cocok persis
dengan log VPS — `callback st|… makan 20.8s (lag loop 0.0s)`, `wd|… 20.2s`, dan
`mint|… 57.5s` (tiga panggilan semacam itu). Lag loop 0,0 karena tidurnya di
thread, bukan di event loop, jadi kelihatan seperti "RPC lambat" padahal endpoint
lain menganggur.

Dua lapis yang menunggu, keduanya harus dijinakkan:

| lapis | setelan lama | biaya terukur |
|---|---|---|
| urllib3 `Retry` | `respect_retry_after_header=True`, `total=4` | **20,01 detik** (Alchemy kirim `Retry-After: 5` × 4 retry) |
| web3 `ExceptionRetryConfiguration` | default memuat `requests.HTTPError` | **1,89 detik** (5 retry, backoff 0,125) |

`respect_retry_after_header` **MENGALAHKAN** `backoff_factor` — jadi backoff
pendek yang sudah disetel di `_rpc_retry()` tidak berpengaruh sama sekali selama
header itu dihormati. Terukur pada server 429 lokal: tanpa `Retry-After` 4,21
detik, `Retry-After: 1` 4,01 detik, `Retry-After: 5` **20,01 detik**.

Sekarang `respect_retry_after_header=False` + `status=2` (jeda 0 + 0,5 detik),
dan `_w3_retry_cfg()` membuang `HTTPError` dari retry web3. Terukur: 429
terus-menerus **20,01 detik → 0,50 detik**, burst 429 sesaat pulih di retry
pertama (**0,00 detik**).

**429 JANGAN dibuang dari `status_forcelist`** — sempat dilakukan dan itu
MEMATIKAN chain. eth-rpc Blockscout Robinhood membalas 429 untuk `eth_chainId`
sebagai rate limit biasa (sudah tercatat di bagian siar-ulang tx); dengan nol
retry ia langsung dianggap endpoint mati, `get_w3` kehabisan kandidat, dan log
VPS penuh *"Semua RPC Robinhood gagal"*. Burst 429 sesaat (Alchemy 300 CU/s)
juga lazim dan hilang dalam ratusan milidetik. Aturannya: **retry PENDEK untuk
burst, rotasi endpoint untuk jatah yang benar-benar habis** — bukan salah
satunya saja. `status=2` membatasi retry berbasis status; kegagalan
koneksi/timeout tetap dapat jatah `total` penuh.

Menunggu LAMA tidak pernah bisa menolong: **kuota Alchemy dihitung per-app**,
jadi jatah tidak pulih dalam hitungan detik. Yang menolong pindah key.

**Menandai endpoint saja TIDAK cukup.** Panggilan yang kena 429 tetap gagal, dan
yang dilihat user adalah *"Collect gagal: too many 429"* walau key lain masih
punya jatah — rotasinya baru berlaku untuk panggilan BERIKUTNYA.
`_Provider.make_request` karena itu mengulang request itu juga lewat endpoint lain
(`get_w3(fresh=True)`, maks `_RPC_FAILOVER_MAX` = 2 hop). Terukur: request yang
dulu 20 detik lalu gagal sekarang **pulih dalam 0,14 detik**.

**Failover JANGAN memakai `get_w3(fresh=True)`, dan menandai endpoint JANGAN
dilakukan di jalur probe.** Dua hal itu bersama-sama sempat mematikan seluruh
chain. `get_w3` memverifikasi tiap kandidat dengan `eth_chainId` lewat
`_Provider` baru, jadi tiap kandidat yang ikut kena 429 saat diprobe ikut
ditandai `_RPC_BAD` — **satu** panggilan gagal menghanguskan SEMUA key Alchemy
sekaligus. Gejalanya di VPS user (2 key Alchemy terpasang): pesan gagal cuma
menyebut SATU endpoint, `https://robinhoodchain.blockscout.com/api/eth-rpc` —
yaitu satu-satunya yang belum ternoda — dan itu pun langsung gagal karena
429-nya tidak lagi di-retry.

Tiga penjagaan sekarang, jangan dihapus:

- **Hop membangun `Web3.HTTPProvider` langsung dari `_chain_rpcs(chain_id)`**,
  tanpa verifikasi `eth_chainId`. Endpoint itu berasal dari `CHAINS`/
  `_alchemy_urls` untuk chain tersebut, jadi chain_id-nya sudah pasti dan
  probe-nya cuma round-trip tambahan. Hop menandai HANYA endpoint yang
  benar-benar ia coba.
- **`_RPC_FAILOVER_TL.busy` mematikan penandaan DAN failover saat bersarang.**
  `get_w3` menyalakannya selama sapuan probe-nya, jadi kandidat yang cuma kena
  burst sesaat tidak ikut hangus.
- **Marking terjadi SESUDAH cek `busy`, bukan sebelum.** Urutan terbalik persis
  itulah yang meloloskan cascade di atas.

### Koneksi RPC dipakai ULANG per endpoint

`w3_for_url(url, chain_id)` menyimpan satu `Web3` per ENDPOINT di `_W3_BY_URL`,
dan `_CHAIN_OK` mengingat endpoint yang chain_id-nya sudah terverifikasi.

Dulu tiap sapuan `get_w3` membangun provider **dan `requests.Session` baru**.
Session baru = pool koneksi baru = **handshake TLS baru** — dan `_mark_bad()`
membuang `_W3_CACHE` pada SETIAP 429, jadi di bawah tekanan rate limit sapuan itu
terjadi terus-menerus. Terukur membaca satu posisi v4 di proses dingin: total
2,12 detik, yang **1,78 detik-nya cuma 2× `eth_chainId`**, sementara 11
`eth_call` isinya 0,32 detik. Ongkosnya hampir seluruhnya koneksi, bukan kerja.

chain_id sebuah endpoint tidak bisa berubah, jadi sekali terverifikasi probenya
dilewati selamanya. Sesudah keduanya: `get_w3` setelah cache dibuang
**1,78 detik → 0,000 detik (0 request)**, dan `position_by_pid` 2,12 → **0,30
detik dingin / 0,16 detik hangat**. Hop failover juga memakai provider hangat itu,
bukan membuka session baru tiap hop.

### Kegagalan sumber luar WAJIB ikut di-cache

`_dex_pairs()` dulu `except: return []` tanpa menyentuh cache. Di host yang
dexscreener-nya diblokir, SETIAP pemanggilan karena itu membayar ULANG seluruh
budget `_cf_request` (jalur langsung + tiap proxy). `pool_stats()` memanggilnya
**dua kali** (`dex_volumes` + `_dexliq_of`) dan duduk persis di jalur klik
tombol — terukur di VPS: `callback pos|v4:2452060 makan 19.9s (lag loop 0.0s)`.

Sekarang kegagalan disimpan dengan TTL sendiri (`_DEX_PAIRS_FAIL_TTL` 60 detik,
sukses 120) dan mengembalikan hasil LAMA kalau ada — angka tampilan, basi jauh
lebih baik daripada hilang. Budget juga dipotong 8 → 4 detik
(`_DEX_PAIRS_BUDGET`); fungsi ini ada di jalur klik, bukan cuma discovery.

Terukur dengan dexscreener dimatikan paksa: panggilan pertama 4,00 detik,
**berikutnya 0,00 detik**, dan `dex_volumes` + `_dexliq_of` bersama-sama 4,00
detik (dulu 2 × 8).

Bedakan dari aturan `krystal_raw()` yang justru **tidak boleh** meng-cache hasil
kosong: di sana yang di-cache adalah JAWABAN SUKSES yang isinya kosong, dan itu
menghapus pool dari daftar. Di sini yang di-cache adalah fakta bahwa request-nya
GAGAL, supaya klik berikutnya tidak membayar timeout yang sama lagi.

### Kartu hasil & kartu posisi WAJIB dirakit di thread

`gas_line()` melakukan RPC (`fmt_gas` → `quote_usd_price` untuk kurs native) dan
dipanggil di **11** tempat; `position_card()` memanggil `_pool_info_line` →
`pool_stats` (StateView + dexscreener). Semuanya dulu jalan langsung di event
loop — terukur di log VPS `event loop tertahan 12,8 detik` tepat sebelum kartu
hasil mint muncul, dan selama itu **tidak ada** klik lain yang bisa dijawab
(query callback keburu kedaluwarsa, gejalanya tombol berputar terus).

`concurrent_updates(True)` tidak menolong sama sekali untuk ini: yang tertahan
loop-nya, bukan antreannya.

Aturannya: apa pun yang menyentuh RPC/HTTP di `bot.py` masuk
`asyncio.to_thread`, termasuk yang "cuma satu panggilan" dan yang di-cache
(cache miss tetap RPC). Yang sudah dipindah: `gas_line` (11 tempat),
`position_card` (`show_position`), `_pool_info_line` (`ask_compound`),
`token_info` (`do_close`). Pemeriksaan ulangnya murah — scan AST `bot.py` untuk
panggilan bernama itu yang ada di badan `async def` tanpa `to_thread`.

### Nilai event mustahil = PnL rusak selamanya

PnL portfolio itu **jumlah**, bukan rata-rata — tidak ada yang meredam satu nilai
ngawur. Terjadi sungguhan: satu event `fees` senilai **$5,9e53** (v4:1239107)
membuat PnL terbaca `$593805893216973777495023055208279841552788881408.0M` dan
persentasenya ikut ngawur, padahal seluruh sisa riwayatnya sehat.

`record_event()` menolak `usd` yang tidak finite atau di atas `_USD_SANITY_MAX`
(1e9) dan **mencatatnya di log** lengkap dengan kind + token_id. Angka sebesar itu
selalu bug pembacaan (raw token dianggap sudah berdesimal, delta uint256 yang
underflow, harga dari pool debu), bukan dana sungguhan — lebih baik satu event
hilang daripada seluruh riwayat tidak terpakai.

Sumbernya hampir selalu `pos["unclaimed_usd"]`: 12 dari 13 pemanggil `record_event`
ber-kind `fees` meneruskan nilai itu apa adanya. Jadi kalau log penjaga muncul,
yang dicari adalah pembacaan fee posisi itu di `chain.py`, bukan jalur pencatatannya.

`drop_bad_events(chain_id)` membuang yang terlanjur tercatat.

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

**Request-nya harus meniru web mereka.** Dibaca dari bundel
`defi-assets.krystal.app/assets/index-*.js`, parameter yang dikirim
`defi.krystal.app/pools` adalah `chainId` (DIHILANGKAN saat "All Networks"),
`tokenAddress` (lowercase), `category`, `protocols`, dan `skipCheckAutomation`.

- `skipCheckAutomation=true` mematikan pengecekan dukungan automation di sisi
  server — itu fitur UI mereka (ikon robot; **bukan** penanda hook). Tanpa itu
  request dingin terukur 4 detik, dengan itu 0,4 detik.
- **Cloudflare menyaring lewat TLS fingerprint (JA3), bukan cuma header.** Terukur
  di VPS: `403` tanpa header, dan DENGAN header lengkap jawabannya tetap bukan JSON.
  `curl_cffi` (`impersonate="chrome"`) meniru handshake TLS Chrome dan tembus —
  dipakai duluan lewat `_krystal_get()`, `requests` jadi cadangan. Paketnya
  **opsional**: tanpa itu bot tetap jalan, discovery-nya saja jatuh ke scan RPC.
- **Blokirnya tidak cuma mengenai Krystal.** Indexer Uniswap (ListPools/ListPositions)
  dan dexscreener duduk di belakang Cloudflare yang sama. Terukur: dengan indexer
  hidup, jalur fallback pun menemukan 19 pool BNBCAT; di VPS yang diblokir cuma 2
  (hasil scan RPC murni). Karena itu SEMUA request ke sumber luar lewat
  `_cf_get()`/`_cf_post()`, bukan `requests` polos.
- Header tetap meniru browser (`user-agent`, `origin`, `referer`). Default
  python-requests gampang dijegal Cloudflare dari IP datacenter, dan gejalanya
  bukan exception melainkan **hasil kosong** — bot lalu diam-diam jatuh ke scan RPC
  penuh. `krystal_last_error()` menyimpan sebabnya dan UI menampilkannya, jadi
  kegagalan tidak lagi tak terlihat.
- `/all/v1/lp_explorer/configs` memberi daftar chain + protokol yang dilayani
  Krystal (9 chain saat ditulis).

**Krystal punya DUA endpoint pool, dan `top_pools` saja tidak cukup.** Halaman
Pools mereka menyaring `>= $1K TVL` dan per-quote; kotak search-nya tidak. Terukur
untuk DINO di Robinhood: `top_pools` 5 entri, `global_search` **10** — dan yang
hilang termasuk pool USDG ber-TVL **$34.938**, sedangkan bot cuma menampilkan
terbesar $4,6k dari jalur gecko. `krystal_raw()` karena itu meng-union
`GET /all/v1/global_search/search?query=<token>` (ditemukan dari bundel
defi.krystal.app), dedupe per `poolAddress`, entri `top_pools` menang karena
statistiknya lebih lengkap. Skema search sudah cocok dengan
`_v4_key_from_krystal` apa adanya — `token0.address`, `feeTier` dalam PERSEN,
`hooks` absen — jadi verifikasi hash PoolKey berjalan sama persis.

**Keduanya saling melewatkan pool — DUA ARAH, jadi union-nya wajib.** Terukur:
untuk DINO kotak search punya 10 entri (termasuk pool $92k) sedangkan `top_pools`
cuma 5; untuk OPTIMUS justru sebaliknya — `top_pools` punya WETH/OPTIMUS 0,9%
ber-TVL **$366.278** yang TIDAK ADA di kotak search sama sekali, padahal pool itu
terverifikasi sehat on-chain (`lpFee 0,9%`, tanpa hooks, likuiditas aktif 1,36e22,
harga pool $0,00874 vs pasar $0,00874693). Jangan pernah menyimpulkan "Krystal
tidak punya pool ini" dari satu endpoint saja.

Pool ber-TVL besar yang tetap tidak muncul biasanya memang **pool hook**: DINO/GOOGL
$92.608 terbukti ber-`hooks = 0xE5e70264…` (hook launchpad PONS) dengan `lpFee = 0%`
— LP tidak dapat apa pun di sana, jadi menyembunyikannya benar.

**Endpoint Krystal**: `GET api.krystal.app/all/v2/lp_explorer/top_pools?chainId=&tokenAddress=`
— **v2**, yang v1 cuma melayani Solana (menjawab "chain id 56 not supported"), dan
paramnya `tokenAddress` bukan `search`. Tidak terdokumentasi di swagger publik;
ditemukan dari bundel JS defi.krystal.app. Fragile — semua pemanggilnya harus tetap
jalan kalau API-nya mati, dan angkanya tidak pernah jadi dasar membangun transaksi.

### Tempel CA token chain mana pun: pemetaan token → chain

`token_chains(token)` mengembalikan `[(chain_id, tvl_total)]` untuk chain yang ADA di
`CHAINS`, urut TVL. Satu request saja: endpoint `top_pools` Krystal jalan **tanpa
`chainId`** dan tiap entri membawa `chainId` sendiri — itu juga cara
`defi.krystal.app/pools` bekerja (satu daftar lintas chain; filter chain di UI cuma
menyempitkan, bukan syarat query).

`on_address` memakainya sebelum discovery: token yang tidak ada di chain aktif
memindahkan chain aktif (satu chain → otomatis, beberapa chain → tombol `chtok|`).
Chain aktif memang ikut dipindah — bukan cuma dipakai untuk flow itu — supaya
`/list`, `/wallet`, dan monitor tidak menunjuk chain lain daripada posisi yang baru
dibuat.

**Krystal tidak melayani semua chain yang didukung bot**, dan kosong di sini
berarti chain aktif TIDAK dipindah — user menempel CA Arc lalu bot memindai
Robinhood ("Fetching Uniswap v2/v3/v4 pools on Robinhood…"), tidak menemukan apa
pun, dan baru di pesan gagal menyebut chain yang benar. Gejalanya terlihat seperti
salah deteksi, padahal jalur deteksinya memang cuma punya satu sumber.

Lapis kedua `_token_chains_gecko()`: `api.geckoterminal.com/api/v2/search/pools
?query=<alamat>` mencari LINTAS network dalam satu request, dan slug chain-nya ada
di depan `id` (`"arc_0x…"`) sehingga bisa dipetakan balik lewat `CHAINS[cid]["gecko"]`.
Dipakai HANYA kalau Krystal kosong, jadi chain yang Krystal kenal tidak berubah
perilakunya sama sekali (terukur: Krystal menjawab 43 entri untuk CAKE, jalur gecko
tidak tersentuh). Sesudahnya: LONG → Arc $951.488, CRCL → Arc $1.049.045, USDC →
Base, CAKE → BSC, alamat ngawur → kosong.

Hasil search **wajib disaring** ke pool yang token itu benar-benar salah satu
sisinya (`base_token`/`quote_token` == `<slug>_<alamat>`) — query alamat juga
mengembalikan pool token bernama serupa. TVL-nya cuma untuk MENGURUTKAN pilihan
chain; pool-nya sendiri tetap dicari ulang dan diverifikasi on-chain oleh
`discover_any` sesudah chain dipindah.

Kalau dua-duanya tidak kenal tokennya, `token_chains_onchain()` mengecek
`eth_getCode` per chain sebagai petunjuk terakhir. Itu satu request per chain, jadi
HANYA dipakai di jalur "tidak ada pool", tidak pernah di jalur normal.

### `PROXY_LIST`: proxy untuk API data pasar, JANGAN untuk RPC

**Cara yang terbukti menembus: WARP mode proxy.** `warp-cli mode proxy` + `proxy
port 40000`, lalu `socks5://127.0.0.1:40000` di baris pertama `proxies.txt`. Exit
IP-nya milik Cloudflare sendiri (terukur 104.28.222.43) dan Krystal menjawab
**200 + 19 pool** dari VPS yang sebelumnya selalu 403. Sengaja **proxy mode, bukan
full-tunnel**: full-tunnel akan menyeret RPC ikut lewat WARP, padahal pemisahan
"proxy hanya untuk data pasar" itu justru jaminannya. Butuh `PySocks` untuk jalur
requests (curl_cffi sudah lewat libcurl).

`_cf_request()` mencoba jalur langsung dulu, lalu proxy dari `proxies.txt` (satu
per baris, `#` = komentar; path bisa diganti lewat `PROXY_FILE`) dan env
`PROXY_LIST` (`ip:port:user:pass` atau URL penuh) kalau jawabannya 4xx/5xx — Cloudflare menolak
dengan **403 + HTML**, bukan exception, jadi status code ikut diperiksa. Proxy yang
berhasil diingat (`_PROXY_GOOD`) supaya percobaan berikutnya mulai dari situ, dan
jalur langsung yang ditolak dilewati selama `_DIRECT_COOLDOWN` (300 detik) — di host
terblokir ia selalu gagal, jadi mencobanya tiap request cuma round-trip percuma.

Batas yang disengaja: proxy **hanya** untuk Krystal / indexer Uniswap / dexscreener
/ GeckoTerminal. Angka dari sumber-sumber itu memang sudah diperlakukan sebagai
tampilan belaka dan tiap pool tetap diverifikasi on-chain, jadi operator proxy tidak
bisa mengarahkan transaksi. Menyalurkan RPC lewat pihak ketiga akan membuang jaminan
itu — jangan dilakukan.

`timeout` yang dikirim pemanggil diperlakukan `_cf_request()` sebagai batas **waktu
total**, bukan per percobaan. Fungsi ini mencoba jalur langsung LALU tiap proxy
berurutan, dan tiap percobaan mencoba curl_cffi dulu baru `requests` — jadi dulu
`timeout=6` dengan 2 proxy terukur **18,0 detik** (dan di host ber-curl_cffi bisa
dua kali lipat lagi). Itu duduk persis di jalur klik tombol: kartu detail memanggil
dexscreener lewat `pool_stats`, `timeout=8` jadi puluhan detik. Jalur yang diblokir
menjawab 403 dengan cepat sehingga proxy tetap kebagian jatah; yang dipotong hanya
kasus benar-benar menggantung.

`proxies.txt` ada di `.gitignore` — isinya kredensial, jangan pernah di-commit.
Contohnya `proxies.txt.example`.

Terukur dengan proxy datacenter: indexer Uniswap **pulih** (0 → 94 entri saat jalur
langsung diblokir), Krystal **tetap 403** — Cloudflare mereka menolak IP datacenter
apa pun, proxy maupun bukan. Jadi proxy menolong indexer, bukan Krystal.

Blokirnya **se-domain**, bukan per-path: `api.krystal.app/all/v2/lp_explorer/top_pools`,
`/all/v1/lp_explorer/configs`, bahkan halaman `defi.krystal.app/pools` semuanya 403
dari IP yang sama. Tidak ada celah host/path — yang bisa menembus cuma IP dengan
reputasi bersih (residensial/mobile).

**`discover_foreign_pools()` hanya untuk jalur Krystal.** Daftar Krystal disaring
per-quote sehingga pool ber-quote aneh bisa hilang; GeckoTerminal memuat semua pool
yang mengandung token itu apa pun quote-nya. Di jalur gecko pencarian itu murni
beban — terukur 32,7 detik untuk 0 pool tambahan (36,1s → 6,9s setelah dilewati).

### Tiga sumber daftar pool di-UNION, bukan berantai

**Bukan union-nya yang mahal.** Terukur untuk DINO: `discover_gecko` 2,1 detik,
`uni_discover` 0,5 detik, sedangkan `discover_krystal` 54,9 detik. Mematikan dua
sumber tambahan hanya menghemat ~2,6 detik dan mengembalikan bug "cuma 1 pool" —
yang mahal selalu verifikasi PoolKey Krystal, bukan jumlah sumbernya.

`discover_krystal`, `uni_discover`, dan `discover_gecko` dijalankan PARALEL lalu
hasilnya di-union (dedupe per alamat/poolId); scan RPC sendiri hanya kalau ketiganya
kosong. Prioritas saat pool sama muncul di beberapa sumber: Krystal (statistik
terlengkap) → indexer Uniswap (fee & tickSpacing eksak) → GeckoTerminal.

**Aturannya: hasil gabungan tidak boleh lebih sedikit dari sumber tunggal mana pun.**
Model berantai melanggar itu dua arah, dan keduanya sudah terjadi:

- Krystal menang duluan → indexer tertutup. RADIO: Krystal 2 pool (terbesar $2.568),
  indexer 53 pool (terbesar $50.173).
- Krystal menjawab 1 pool → GeckoTerminal yang punya 5 ikut dilewati. Terukur di VPS
  user untuk DINO: daftarnya justru MENYUSUT dari 5 jadi 1 setelah jalur Krystal
  "diperbaiki".

`discover_foreign_pools()` dilewati kalau indexer ATAU gecko menyumbang — keduanya
sudah memuat semua quote.

**Verifikasi per-entri tidak boleh menelan kegagalan RPC.** `build()` dulu
`except Exception: return None`, jadi pool yang gagal dibaca karena RPC sibuk tidak
bisa dibedakan dari pool yang memang bukan milik kita. Terukur: 11 entri Krystal
menghasilkan 1 pool di satu host dan 10 di host lain dari entri yang SAMA. Sekarang
tiap entri dicoba 2×, dan yang tetap gagal dihitung + dicatat di log. Batas entri
juga dinaikkan 20 → 60, karena `krystal_raw` kini meng-union dua endpoint.

Dulu Krystal menang begitu hasilnya tidak kosong. Itu **menyembunyikan pool
terdalam**: daftar Krystal disaring ≥$1K TVL dan per-quote. Terukur untuk RADIO di
Robinhood — Krystal **2 pool** (terbesar $2.568), indexer Uniswap **53 pool** dengan
yang terbesar **$50.173**, dan pool itu TIDAK ADA di Krystal sama sekali. 51 pool
hanya ada di indexer.

Angka pool yang dimiliki Krystal tetap dari Krystal; indexer hanya menambah pool
yang tidak ada di sana. `res["source"]` jadi `krystal+uniswap` / `krystal` /
`uniswap` / `gecko`.

**`discover_foreign_pools()` dilewati kalau indexer ikut menyumbang** — indexer
sudah memuat semua pool Uniswap apa pun quote-nya, jadi pencarian itu murni beban
(terukur RADIO 28,7s → 11,9s). Kalau tetap perlu (indexer kosong), ia **dibatasi
`_FOREIGN_POOL_BUDGET` = 12 detik**: terukur pernah **104,9 detik** untuk DINO, dan
itu duduk persis di jalur klik tombol. Batas itu HANYA bekerja kalau executor-nya
tidak dipakai lewat `with` — keluar dari blok `with` memanggil `shutdown(wait=True)`
yang menunggu thread selesai, jadi timeout-nya tidak berpengaruh sama sekali
(terukur tetap 104,8 detik sampai `with`-nya dihapus). Sesudahnya DINO 21,8 detik.

Indexer Uniswap juga dulu dilewati begitu Krystal gagal, dengan alasan keduanya duduk
di belakang Cloudflare yang sama sehingga sama-sama mati di host terblokir. Itu TIDAK
selalu benar: terukur di VPS user, Krystal menjawab `HTTP 200, hasil kosong`
sedangkan indexer Uniswap tembus normal.

Terukur untuk RADIO di Robinhood:

| sumber | pool | waktu |
|---|---|---|
| Krystal | 0 | 2,0s |
| indexer Uniswap | **17** | 13,2s |
| GeckoTerminal | 7 | 11,8s |

Dua alasan indexer didahulukan dari GeckoTerminal:

- **Cakupan** — 17 vs 7 pool untuk token yang sama.
- **`fee` DAN `tickSpacing` eksak.** Nama pool GeckoTerminal fee-nya dibulatkan
  ("BNBCAT / USDT 4.202%" untuk fee 42122), jadi PoolKey harus ditebak lalu
  dibuktikan lewat hash. Indexer mengirim nilai aslinya (terukur spacing 9303,
  19988, 18665 — mustahil ditebak dari tier klasik).

**`ListPools` mewajibkan `token0`**, dan itu bukan filter posisi token — dipakai
sebagai "token ini ada di pool", jadi RADIO (alamat tinggi) tetap mengembalikan 59
entri. Mengirim `token1` saja dijawab `400 Missing required parameter: token0`.
Token yang terlalu baru dijawab **HTTP 200 dengan body `{}`** (terukur: DINO), bukan
error — itu sebabnya `uni_pools` tidak meng-cache hasil kosong, supaya percobaan
berikutnya langsung dapat begitu indexer menyusul.

**Jangan percaya `totalLiquidityUsd` indexer.** Untuk RADIO ia melaporkan $41.686
untuk pool yang setelah dihitung ulang on-chain oleh `_fill_onchain_tvl()` cuma
$661. Angka yang ditampilkan harus selalu yang hasil hitung ulang.

`discover_foreign_pools()` tetap hanya untuk jalur Krystal: daftar indexer sudah
memuat semua pool Uniswap apa pun quote-nya, sama seperti GeckoTerminal.

### GeckoTerminal: satu-satunya sumber yang lolos dari host terblokir

Urutan sumber daftar pool: **Krystal → GeckoTerminal → discovery sendiri**.

Krystal dan indexer Uniswap sama-sama di belakang Cloudflare. Dari VPS yang IP-nya
kena *managed challenge*, keduanya menjawab halaman HTML "Just a moment…" — 403
untuk SEMUA profil impersonasi curl_cffi (chrome, chrome131/124/120, safari17_0,
firefox133). Itu keputusan reputasi IP, bukan TLS: tidak ada perubahan HTTP client
yang bisa menembusnya. `api.geckoterminal.com` tidak di belakang Cloudflare dan
tetap menjawab 200 dari host yang sama.

`discover_gecko()` memakai `/networks/{net}/tokens/{addr}/pools`. Dua hal penting:

- **`address` untuk pool v4 adalah poolId 66-karakter**, bukan alamat kontrak.
- **Fee-nya dibulatkan** di nama pool ("BNBCAT / USDT 4.202%" untuk fee asli 42122),
  jadi `_v4_key_search()` menebak fee di sekitar nilai itu dan menerima yang
  `v4_pool_id(key)`-nya cocok. Hash itu keccak LOKAL — tanpa RPC — jadi ribuan
  kombinasi praktis gratis (16 pool < 1 detik) dan hash cocok = kunci autentik.
  Pool ber-hooks otomatis tidak pernah cocok, dan itu memang yang diinginkan.

Terukur di BNBCAT/BSC dengan Krystal + indexer dimatikan: **16 pool dalam 10,4
detik**, termasuk pool terbesarnya (PancakeSwap v2 $263k) dan 13 pool v4 ber-fee
non-standar. Sebelum ada jalur ini, host terblokir cuma dapat 2 pool dalam 45 detik
(hasil scan tier tetap).

### Krystal sebagai sumber utama daftar pool

`discover_any()` mencoba `discover_krystal()` DULU (<1 detik, angkanya sama dengan yang
dilihat user di web Krystal). Kalau token itu ada di Krystal, daftar yang ditampilkan =
daftar Krystal, titik — tidak disaring ulang oleh `_drop_dead_pools`/`_drop_offprice_pools`
(daftar mereka sudah tersaring ≥$1K TVL). Harga menyimpang cuma DITANDAI
(`p["deviation"]`), tidak dibuang.

**`krystal_raw()` tidak boleh meng-cache hasil kosong.** Krystal bisa menjawab HTTP
200 dengan payload error (`result` hilang) → `out = []`, dan dulu itu ikut di-cache
selama ttl penuh (120 detik). Akibatnya SETIAP discovery dalam 2 menit berikutnya
jatuh ke scan RPC penuh berikut seluruh saringannya — token yang di web Krystal
punya 20 pool cuma muncul 4 di bot, plus "78 pool disembunyikan" (kejadian nyata:
BNBCAT di BSC). Sekarang ada satu retry, dan hasil kosong mengembalikan cache lama
tanpa menimpanya.

Karena dua jalur ini menghasilkan daftar yang panjangnya bisa jauh berbeda, UI
**wajib menyebut `res["source"]`** — tanpa itu Krystal yang gagal sesaat terlihat
seperti bot kehilangan pool.

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
hash (`v4_pool_id(key) == poolId`).

**Krystal tidak mengirim `tickSpacing`, dan menebaknya salah = pool lenyap diam-diam.**
Pola yang dominan terukur adalah **fee/100**, bukan fee/50 seperti dugaan awal: dari
10 pool DINO di Robinhood, fee 50000→500, 86000→860, 87000→870, 46000→460, 44000→440,
94000→940, 49200→492. Dengan hanya fee/50, **9 dari 10 gagal** dan yang lolos cuma
satu karena spacing-nya kebetulan 60 (tier tetap) — itulah sebabnya kartu cuma
menampilkan 1 pool.

**Sapuan penuh 1..32767 sempat ditambahkan, lalu DIBUANG** — jangan diulang. Diukur
pada 14 entri DINO: 4 entri cocok lewat kandidat dalam 0,1–0,4 ms dan 10 sisanya
gagal walau sudah disapu penuh, jadi hasilnya **0 pool tambahan dengan ongkos
10 × 0,85 detik**. Yang gagal itu bukan pool ber-hooks (log `Initialize` menunjukkan
`hooks = 0x0`), melainkan kasus di bawah ini.

**Krystal melaporkan alamat ERC20 WRAPPED untuk pool yang PoolKey-nya memakai ETH
NATIVE.** Terbukti di WETH/DINO Robinhood: dengan alamat WETH tidak ada spacing yang
cocok, dengan `address(0)` langsung cocok di spacing 60. `_v4_key_from_krystal()`
karena itu mencoba kedua varian pasangan currency. Tanpa itu pool-nya jatuh ke
`_v4_key_from_init()` yang memakai `getLogs` — terukur 8,1 detik untuk 9 pool, dan
di RPC pelit sering ditolak sehingga pool-nya hilang sama sekali. Sesudah varian
native ditambahkan: **10 dari 11 entri resolve tanpa getLogs** (sebelumnya 3 dari 13).

Gejala penting: kegagalan ini SENYAP kalau indexer Uniswap sedang tersedia, karena
`_v4_key_from_indexer()` memberi fee+spacing eksak lebih dulu. Bug-nya cuma muncul di
host yang indexer-nya kosong — persis kenapa satu host meloloskan 10 pool dan host
lain 1 dari entri Krystal yang SAMA.

### `res["source"]` sekarang GABUNGAN — jangan dicocokkan persis

Nilainya bisa `krystal+uniswap+gecko`. UI dulu mencocokkan persis `== "krystal"` /
`== "gecko"`, jadi nilai gabungan jatuh ke cabang terakhir dan kartu menulis
*"sumber: scan sendiri … (Krystal tidak punya token ini)"* padahal Krystal yang
menyumbang mayoritas daftar. Pecah dengan `split("+")` dan sebutkan semuanya.

**APR diseragamkan di `discover_any()`**, bukan per-sumber: Krystal mengirim `apr`,
GeckoTerminal tidak. Tanpa penyeragaman, kolom APR kosong justru untuk pool yang
volume dan TVL-nya sudah diketahui — terlihat seperti data hilang padahal cuma tidak
dihitung. Rumusnya `vol24 × fee/1e6 ÷ tvl × 365 × 100`, dan hasilnya cocok dengan
angka Krystal (terukur DINO/USDG 4,6% → 8.573% vs 8.573,17%; 4,4% → 9.353% vs
9.353,48%).

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

### Tombol A% bisa mempersenkan saldo MEME, bukan cuma quote

`ctx["amount_src"]` = `"quote"` (default, perilaku lama) atau `"meme"`. Barisnya
muncul di kartu konfirmasi mint sebagai `💰 <quote>` / `🪙 <meme>`, disembunyikan di
mode `upper` karena mode itu memang selalu memakai meme.

Untuk `"meme"`, sisi quote **menyesuaikan mengikuti rasio range** — bukan sekadar
menukar satuan.

**Yang dikembalikan `compute_amount()` adalah porsi QUOTE, bukan totalnya.** Mesin
mint memakai `budget` sebagai sisi quote saja dan MENAMBAHKAN meme wallet di atasnya
(`quote_dep = (budget + meme_val) × keep_frac`). Jadi rumusnya

    budget_quote = nilai_meme × keep_frac / (1 − keep_frac)

Mengembalikan totalnya (`nilai_meme / (1 − keep_frac)`) membuat posisi jadi
`1/keep_frac` kali lebih besar, dan bot lalu MEMBELI meme tambahan lewat swap padahal
user memilih memakai saldo meme yang sudah ada — persis kebalikan dari yang diminta.
Terukur di BODKIN/USDG dengan saldo 14.915,77: rumus lama memaksa "swap baru 5,7658
USDG → BODKIN"; sesudah diperbaiki **swap 0** dan sisi meme persis 14.915,77.

Diuji untuk 25/50/100%: sisi meme selalu tepat sasaran dan swap selalu 0. Sumber
`"quote"` tidak berubah perilakunya.

Dua syarat yang gampang terlewat:

- **Range harus dihitung SEBELUM amount.** `build_preview` dan `do_mint` sama-sama
  menghitung tick lebih dulu lalu mengoper `sqrtp` + `ticks` ke `compute_amount()`.
  Kalau salah satu lupa, jumlah yang dieksekusi beda dari yang ditampilkan.
- **Kartu wajib menyebut satuan persennya** (`75% GME` vs `75% USDG`) — tanpa itu
  "75%" ambigu dan user salah memperkirakan berapa yang dipakai.

Mode `lower` (100% quote) memakai nilai meme apa adanya: seluruh meme dijual.

### Tombol jumlah TETAP di kartu mint (`/presets` atau menu Pengaturan)

Selain baris `A 25/50/75/100%`, kartu mint punya baris jumlah tetap —
`10 USDG`, `0.025 WETH` — lewat callback `amtf|<key>|<jumlah>` yang menyetel
`ctx["amount_fixed"]`. Diatur lewat `/presets` atau **Pengaturan → 💰 Tombol
jumlah** (editor bertombol: tambah / hapus / balikkan ke default).

**Disimpan PER CHAIN**: `{"4663": {"USDG": [10, 25, 50]}}`. Simbol yang sama ada
di beberapa chain dengan besaran yang wajar berbeda — 0,01 ETH di Robinhood belum
tentu sama maunya dengan 0,01 ETH di Base. Bentuk lama yang datar
(`{"USDG": […]}`) masih dibaca sebagai default lintas-chain.

**`DEFAULT_SETTINGS["amount_presets"]` WAJIB kosong.** Sempat diisi tebakan, dan
itu merusak dua hal sekaligus: tebakan terbaca sebagai pilihan EKSPLISIT user,
sehingga "balikkan ke default" tidak pernah benar-benar mengosongkan dan preset
satu chain terlihat bocor ke chain lain. Tebakannya dihitung `amount_presets()`
saat render, bukan disimpan.

`presets_get()` mengembalikan yang EKSPLISIT saja (kosong = pakai tebakan);
`amount_presets()` yang menambahkan tebakan. Editor membedakan keduanya lewat
label seksi (`USDG` vs `USDG · default`), dan **➕ Tambah** menambah di ATAS
daftar yang tampil — kalau tidak, satu klik "tambah" pada simbol bertebakan
justru menghapus tiga tombol dan menyisakan satu.

**Satuannya satuan BUDGET kartu itu, bukan selalu quote.** `compute_amount()`
mengembalikan `amount_fixed` apa adanya dan men-short-circuit sebelum
`amount_src`, dan budget mode `upper` itu satuan MEME. `budget_sym()` yang
menentukan labelnya — kalau salah, user mengira menyetor 10 USDG padahal 10 meme.

**Token meme TIDAK pernah ditebak.** Tanpa entri eksplisit, hanya simbol quote
(daftar `quotes` chain itu, wrapped/native, atau yang mengandung "USD") yang
dapat tebakan default. Satuan meme bisa ribuan sampai miliaran tergantung supply,
jadi tebakan apa pun menghasilkan tombol yang tidak pernah masuk akal
(`0,01 microduck`). Untuk meme, baris A% memang sudah jawabannya — dan
`/presets MICRODUCK 100000 500000` tetap bisa dipakai kalau user mau.

Baris kosong tidak dikirim ke Telegram; kalau tidak ada preset, barisnya hilang.

### `/list` menampilkan SEMUA chain — user tidak perlu ganti chain

`cmd_list` memindai seluruh `CHAINS` (setelan `list_all_chains`, default ON) dan
mengelompokkan tombolnya per chain. Aksi dana TETAP per-chain, jadi mengklik
posisi chain lain memakai `posc|<cid>|<pid>` yang **memindahkan chain aktif dulu**
lalu membuka kartunya. Itu disengaja: seluruh alur di belakangnya
(add/reduce/close/rebalance/order) membaca chain aktif, jadi cara ini aman tanpa
mengoper `chain_id` ke belasan tempat — dan user tetap tidak pernah menekan
"ganti chain" sendiri.

**Satu anggaran waktu TOTAL, bukan per-chain** (`_LIST_BUDGET` = 5 detik). Batas
per-chain masih bisa menumpuk kalau beberapa chain sama-sama lambat. Terukur
4 chain dingin **13,9 detik**, hampir seluruhnya menunggu HyperEVM yang jatuh ke
RPC publik; dengan anggaran total: **5,00 detik**, sisanya ditandai "masih
dimuat".

Dua hal yang membuat ini tidak jadi bumerang:

- **Chain yang belum selesai JANGAN dibatalkan.** Task-nya dibiarkan jalan dan
  mengisi `_POS_CACHE`. Membatalkannya membuat daftar tidak pernah lengkap: tiap
  klik memulai dari nol lalu dibatalkan lagi di detik yang sama.
- **Single-flight per chain** (`_SCAN_TASKS`). Tanpa itu tiap klik memulai
  pembacaan BARU untuk chain yang masih jalan — dua kali ongkos RPC dan sama
  lambatnya. Terukur: klik 1 = 5,00 detik / 41 request (HyperEVM pending), klik 2
  = 4,40 detik / **6 request** dan sudah LENGKAP, klik 3 = 0,00 detik / 0 request.

Tombol **Claim fee cakupannya HANYA chain aktif** dan labelnya wajib menyebut
nama chain — angka portfolio di atas lintas-chain, jadi tanpa itu user mengira
semua chain ikut terklaim.

### Setelan berlaku PER CHAIN

Slippage yang wajar di Robinhood belum tentu wajar di BSC, dan interval monitor
pantas beda per chain karena ongkos RPC-nya beda. `store.PER_CHAIN_KEYS` =
`slippage_pct`, `impact_max_pct`, `gap`, `autoswap`, `amount_pct`,
`amount_fixed`, `width_pct`, `alert_secs`, `order_secs`. Sisanya global
(`chain`, `wallet_idx`, `list_all_chains`, `amount_presets` — yang terakhir sudah
per-chain di dalamnya).

Caranya sengaja tidak menyentuh pembaca: **`load_settings(cid=None)` menumpuk
nilai per-chain di atas nilai global**, jadi seluruh `load_settings()["slippage_pct"]`
yang sudah ada otomatis mendapat nilai chain aktif — nol perubahan di puluhan
tempat. `save_settings()` merutekan balik: kunci per-chain masuk ke
`per_chain[<cid>]`, sisanya ke global. Nilai global jadi bawaan untuk chain yang
belum pernah diatur.

Tiga jebakan, semuanya sudah ditutup:

- **Memindah chain aktif JANGAN lewat `save_settings()`.** Dict yang dikirim
  pemanggil berisi nilai per-chain milik chain LAMA, dan routing akan
  menyalinnya ke chain baru. `store.set_chain()` menulis `chain` saja; lima
  pemanggil (`/chain`, auto-switch `on_address`, `chsel|`, `posc|`, `chtok|`)
  sudah dipindah ke sana. `set_global()` untuk `wallet_idx`/`amount_presets`/
  `list_all_chains`.
- **Menyimpan setelan chain LAIN wajib `save_settings(st, cid=…)`** — tanpa cid
  eksplisit ia mendarat di chain aktif, dan editor memang bisa mengedit chain
  yang tidak sedang aktif.
- **`monitor_loop` harus menghormatinya.** Dulu satu interval untuk semua chain,
  jadi setelan "600 detik" di satu chain diam-diam tidak berlaku. Sekarang loop
  berdetak `_MONITOR_TICK` (15 detik) dan tiap chain dipindai hanya kalau
  intervalnya SENDIRI sudah jatuh tempo (`_LAST_SCAN`). Tick tanpa chain jatuh
  tempo tidak menembak satu pun request, jadi tagihan RPC tetap ditentukan
  interval tiap chain — bukan panjang tick.

Reset (`setrst`) memakai `save_settings(..., raw=True)` supaya override per-chain
ikut terbuang, tapi mempertahankan `chain` dan `wallet_idx`.

### Menu Pengaturan: daftar → pilih jaringan → editor

Tiga layar, dan semuanya dibangun dari satu tabel `SETTING_SPEC` — menambah
setelan baru cukup satu entri, bukan tiga potong UI. Tiap entri membawa emoji,
label, field, penjelasan, formatter, dan daftar pilihan.

1. `menu|settings` — kategori saja, tanpa nilai (Trading / Tombol & Tampilan /
   Otomatisasi / Umum).
2. `setk|<key>` — pilih jaringan. **Nilai tiap chain ikut ditampilkan di
   tombolnya** supaya user tidak perlu membuka satu per satu untuk membandingkan.
3. `setkc|<key>|<cid>` — editor: tombol pilihan cepat (`setv|<key>|<cid>|<val>`)
   + "Nilai lain…" (`setx|`, balasan teks). Validasinya tetap `apply_setting()`
   yang sama dengan `/set`, jadi tidak ada aturan yang ditulis dua kali.

Editor tombol jumlah mengikuti pola yang sama (`setbtn` → `setbtnc|<cid>`), dan
seperti layar setelan lain ia **tidak memindah chain aktif**: mengatur Base
sambil bekerja di Robinhood harus bisa.

### Menu Pengaturan bersektor

`settings_kb()` dikelompokkan pakai baris judul `_sec()` (tombol ber-callback
`noop`): Trading / Tombol & Tampilan / Otomatisasi / Umum.

**Editor tombol jumlah lewat pemilih JARINGAN** (`setbtn` → `setbtnc|<cid>`), dan
layar itu **sengaja tidak memindah chain aktif**: mengatur tombol Base sambil
bekerja di Robinhood harus bisa. Semua callback-nya membawa cid
(`btnadd|<cid>|<sym>`, `btndel|<cid>|<sym>|<val>`, `btnrst|<cid>|<sym>`) —
membacanya dari chain aktif akan mengedit jaringan yang salah.

`setrst` mereset `settings.json` ke bawaan tapi **mempertahankan `chain` dan
`wallet_idx`**: keduanya "di mana saya sekarang", bukan preferensi. Seksi Monitor menyebut
alert DAN order karena **dua angka itu yang paling menentukan pemakaian kuota
RPC** — teksnya wajib menyebutkan itu, kalau tidak user merapatkan interval tanpa
tahu ongkosnya.

`impact_max_pct` (setelan baru) menggantikan konstanta mati sebagai batas price
impact. `impact_limit()` mengembalikannya sebagai pecahan 0..1 dan **jatuh ke
`ch._SWAP_IMPACT_MAX` kalau setelannya tidak ada** — penjagaan ini tidak boleh
hilang cuma karena `settings.json` lama.

Tombol yang membuka layar lain memakai prefiks `menu|` (edit pesan di tempat),
bukan `go|` (kirim pesan baru — itu khusus dari kartu hasil tx supaya kartunya
tetap ada). `cmd_rpc` dipanggil dari tombol dengan `context=None`, jadi ia membaca
`getattr(context, "args", None)`.

### Buat pool v4 baru: fee & tick spacing CUSTOM

Tombol **➕ Buat pool baru** di daftar pool → `np|` → layar pemilih fee/kisi →
`npok|` (buat + mint) atau `npgo|` (pool sudah ada → langsung mint).

**Cuma v4.** Di v3 `feeAmountTickSpacing` adalah whitelist yang hanya bisa ditambah
`enableFeeAmount` oleh owner factory, dan owner-nya bukan kita (terukur: Arc
`0xbCA30b54…`, Robinhood `0x05C420bC…`, Base `0xaBEA7665…`, tier aktif ketiganya
100/500/3000/10000). v4 tidak punya whitelist — fee dan tickSpacing ada di PoolKey.

**Batasnya DIUKUR ke PoolManager, bukan dibaca dari dokumentasi** (`V4_FEE_MAX`,
`V4_SPACING_MIN/MAX`):

| | hasil |
|---|---|
| tickSpacing 0 | `TickSpacingTooSmall` (0xe9e90588) |
| tickSpacing 1 … 32767 | OK |
| tickSpacing 32768 | `TickSpacingTooLarge` (0xb70024f8) |
| fee 0 … 1.000.000 (100%) | OK |
| fee 1.000.001 | `LPFeeTooLarge` (0x14002113) |
| fee 0x800000 (dinamis) | `HookAddressNotValid` (0xe65af6a0) — wajib hook |

**`posm.initializePool` MENELAN kegagalan — jangan pakai untuk validasi.**
Implementasinya membungkus `poolManager.initialize` dalam try/catch dan
mengembalikan `type(int24).max` (8388607) kalau gagal. Jadi `eth_call` ke posm
TIDAK PERNAH revert: percobaan pertama yang memvalidasi lewat posm melaporkan
tickSpacing 100.000 **dan** fee 100,0001% sama-sama lolos, padahal dua-duanya
ditolak. `v4_check_new_pool()` karena itu menyimulasikan ke **PoolManager**
langsung (`V4_PM_INIT_ABI`), dan `v4_init_pool()` memeriksa `v4_pool_exists()`
SESUDAH tx masuk blok — receipt sukses bukan bukti pool-nya jadi.

**Harga awal disalin, tidak pernah ditebak.** `initialize()` menerima
`sqrtPriceX96` apa adanya, dan yang membuat pool menentukan harganya; kalau
meleset, arbitraser mengambil selisihnya dari deposit pertama — milik si pembuat.
`v4_ref_sqrt_price()` mengumpulkan semua pool yang pasangan currency-nya PERSIS
sama (v3 maupun v4) dan memakai sqrtPrice-nya apa adanya: angka itu rasio
token1-per-token0 dalam WEI, tidak bergantung fee maupun spacing. Kalau tidak ada
pool rujukan yang layak, harga awalnya DIHITUNG dari patokan pasar — dengan syarat
ketat, lihat `np_derive` di bawah.

**TVL terbesar BUKAN patokan harga — itu sudah merugikan sekali.** Pool DOT/USDC
5% di Arc dibuat dengan harga salinan dari pool fee 20% ber-TVL $89 yang harganya
**belum pernah bergerak sama sekali** (0,0002748042 saat pool dibuat DAN berjam-jam
sesudahnya). Pasar sebenarnya ~0,00021, jadi pool baru itu lahir **27% di atas
pasar** dan langsung diseret turun begitu ada likuiditas — tick 358322 → 361476,
diverifikasi dari event `Initialize` di tx pembuatannya vs `getSlot0` sesudahnya.

**Pool ber-HOOKS ikut jadi pembanding HARGA, walau nyaris tidak pernah jadi tempat
LP.** Melewatinya benar untuk menaruh dana; SALAH untuk membaca harga. Di token
launchpad justru di situlah seluruh volumenya — terukur DOT/USDC Arc: pool
ber-hook `0x0d751ec0…` **$441.817/24 jam** sementara SEMUA pool tanpa hook
digabung ~$800. `getSlot0(poolId)` adalah pembacaan murni StateView — hook-nya
tidak dijalankan dan PoolKey-nya tidak perlu diketahui, jadi tidak ada kode asing
yang tersentuh.

### Harga rujukan pool baru: patokan INDEPENDEN dulu, median belakangan

Median pool sepasang saja TIDAK CUKUP, dan itu sudah memblokir pembuatan yang sah
sekaligus meloloskan angka omong kosong. Kartu BEORN/USDG di Robinhood menghitung
mediannya dari tiga pool debu — volume 24 jam **$8,04**, **$0,76**, dan **$0,06** —
yang harganya berselisih **100×** satu sama lain, lalu menolak dengan "+859%".
Empat pool BEORN/USDG yang benar-benar diperdagangkan (0,000232–0,000245) tidak
ada di himpunan itu sama sekali, dan harga sebenarnya ~0,00025 — disepakati GMGN
($0,00024906), pool terdalam GeckoTerminal ($0,00028666), dan `token_usd_price`
($0,0002627).

`ch.token_anchor_price()` karena itu membangun patokan yang **tidak berasal dari
pool sepasang yang sedang dinilai**, dari tiga sumber yang di-median-geometrik:

| sumber | dari mana |
|---|---|
| GMGN | `bot.gmgn_price_usd()` — `token_info()["price"]["price"]` |
| pool terdalam | `ch.gecko_deep_price_usd()` — pool token itu yang volumenya terbesar di GeckoTerminal, **pasangan apa pun, termasuk ber-hooks** |
| bot | `token_usd_price()` yang sudah ada |

Itulah "pool terdalam dengan hook atau GMGN" yang dimaksud saat harga rujukan
terlihat meleset: seluruh perdagangan BEORN terjadi di **BEORN/SHROOM
($493.436/24 jam)**, pasangan yang tidak pernah bisa jadi kandidat sqrtPrice.

**Kunci GMGN diambil di `bot.py`, bukan `chain.py`** — `extra=[("GMGN", …)]`
dioper masuk. `chain._cf_request` meneruskan header ke operator proxy pihak
ketiga, dan `X-APIKEY` tidak boleh ikut ke sana.

**Patokan ini MENYARING dan MEMILIH, tidak pernah DISALIN.** GeckoTerminal bisa
telat jauh (terukur di Arc: GT $67,77 saat pool terdalam on-chain $202,56), jadi
angka yang masuk `initialize()` tetap sqrtPrice yang dibaca on-chain. Urutannya:

1. Kandidat di luar `NP_ANCHOR_DROP` (3×) dari patokan **dibuang sebelum median
   dihitung**. Tanpa ini saringan dua-lintasan tidak menolong sama sekali kalau
   yang tersisa memang semuanya debu.
2. Kalau tidak ada pool bervolume yang masuk akal, pool TANPA volume yang dekat
   patokan masih dipakai. Kalau itu pun kosong, pembuatan **DITOLAK** — jangan
   jatuh balik ke pilihan `v4_ref_sqrt_price` (pool bervolume terbesar): di
   BEORN/USDG itu justru pool ber-tick mentok berharga **2,9e-27** yang volume
   terlapornya $1.473.
3. Yang dipilih adalah pool **paling ramai di antara yang harganya sudah dalam
   10% dari patokan**, bukan yang terdekat median. "Terdekat median" saja memilih
   pool mati — terukur LONG/USDC Arc: ia memilih pool fee 3,36% bervolume **$0,11**
   padahal pool fee 1% bervolume **$109,5k** sama-sama rapat.
4. `NP_DEV_BLOCK` (25% terhadap median) **dibatalkan** kalau pilihannya justru
   rapat dengan patokan. Pool sepasang yang saling tidak sepakat itu lazim saat
   semuanya debu; mediannya yang tidak bermakna, bukan pilihannya. Terukur
   BEORN/WETH: dua pool sepasang berselisih 3,6× (volume $0,40 dan $0,63)
   sementara pilihannya cuma 22,7% dari harga pasar tiga sumber.
5. `NP_ANCHOR_BLOCK` (3×) menolak kalau MEDIAN pool sepasang sendiri sejauh itu
   dari patokan — artinya tidak ada pool sepasang yang layak jadi rujukan.

Hasil terukur sesudahnya: BEORN/USDG **+0,5%** dan LONG/USDC Arc **+0,3%** dari
harga pasar, dari yang sebelumnya diblokir "+859%".

### Tanpa pool rujukan, harga awal DIHITUNG — bukan ditolak

Aturan lama "harga awal disalin, tidak pernah ditebak" menolak pasangan yang tidak
punya pool rujukan layak. Itu **asimetri, bukan penjagaan**: bot memblokir dengan
kalimat yang menyebut harga pasarnya sendiri —

> ❌ Harga pasar PONXWORK diketahui (0,0₃121 USDG — GMGN, pool terdalam, bot) tapi
> tidak ada pool PONXWORK/USDG yang harganya mendekati itu.

— lalu menolak MEMAKAI angka yang baru saja ia sebut. Terukur pada PONXWORK di
Robinhood: quote WETH bisa dibuat (ada pool ber-hook bervolume $141,8k yang
harganya rapat), quote USDG **tidak bisa sama sekali**, karena satu-satunya pool
PONXWORK/USDG yang ada adalah fee **99,12%, TVL $1,94, volume 0** dengan harga
0,0₅296 — **34× meleset**. Menyalin pool debu itu justru pilihan yang paling
merugikan, jadi "tidak ada rujukan" tidak boleh berarti "tidak bisa dibuat".

`np_derive()` menghitungnya, dan **syaratnya tiga, ketiganya perlu**:

- **Minimal `NP_DERIVE_MIN_SRC` (2) sumber independen.** Satu API yang telat
  sendirian tidak boleh menentukan harga pool.
- **Sumbernya SEPAKAT** dalam `NP_DERIVE_SPREAD` (1,25×). Kalau tidak, harga
  pasarnya memang tidak diketahui — aturan yang sama dengan
  `assert_pool_price_sane`.
- **Minimal satu sumber tersambung ke venue NYATA** — pembacaan on-chain bot
  (`anchor["own"]`) atau pool terdalam bervolume ≥ `NP_DERIVE_MIN_VOL` ($1.000).
  Dua API yang sama-sama memantulkan angka yang sama bukan dua sumber.

Angka yang dipakai `per_quote`, yaitu **ANGKA YANG SAMA** yang kartu tampilkan
sebagai "Harga pasar". Memakai satu sumber tertentu akan membuat harga awal
berbeda dari yang user baca di layar.

`ch.price_to_sqrt_x96()` yang mengubahnya jadi sqrtPriceX96 — **tidak lewat tick**:
sqrtPrice itu kontinu, jadi membulatkannya ke tick dulu cuma menambah galat
sebesar setengah kisi. Akarnya `Decimal` presisi 80; di harga 1e-16 (pasangan
desimal 18/6 untuk token semurah 1e-4) float64 sudah kehabisan digit dan galatnya
langsung jadi harga pool. Terukur round-trip harga→sqrt→harga: **galat 2,2e-16**
(epsilon float) untuk kedua orientasi quote, desimal 18/6, 18/18, dan 6/18.

Terukur sesudahnya pada PONXWORK/USDG: harga awal **0,0₃108** vs patokan
0,0₃108 — **−0,003%** — sementara jalur WETH tetap MENYALIN pool ber-hook
bervolume $148,3k seperti sebelumnya. Salin selalu menang kalau ada; hitung
hanya jalur cadangan, dan kartu MENGATAKAN yang mana yang dipakai berikut
sumbernya, plus daftar pool sepasang yang sengaja TIDAK dipakai.

**Patokan DISEGARKAN di jalur eksekusi.** `np_build(ctx, fresh=True)` dari
`np_refresh_sqrt` melewati cache `token_anchor_price` (60 detik). Untuk harga yang
DISALIN itu tidak penting — sqrtPrice pool selalu dibaca on-chain saat itu juga —
tapi harga yang DIHITUNG berasal dari patokan, jadi patokan basi = harga pool
basi. Cache di bawahnya (`token_usd_price` 120 detik, GeckoTerminal 45 detik)
tetap berlaku.

**`np_anchor` mengembalikan SALINAN.** `token_anchor_price` mengembalikan objek
yang ia cache sendiri, dan `np_build` menempelkan `anchor["derived"]` ke dict itu —
tanpa salinan, keterangan jalur harga bocor ke pemanggil lain lewat cache.

### Pool yang DIBUAT dari bot ini tidak ada di daftar mana pun

Pool baru tidak diindeks Krystal (saringan ≥$1K TVL), indexer Uniswap, maupun
GeckoTerminal, **dan** `_drop_dead_pools()` membuangnya karena belum punya volume.
Akibatnya user membuat pool sendiri, menempel CA-nya, dan pool itu tidak ada —
satu-satunya jalan masuk hilang padahal pool-nya sehat on-chain.

`store.add_new_pool()` dicatat **di dalam `v4_init_pool()`**, bukan di UI: itu
satu-satunya tempat sebuah pool benar-benar lahir, jadi bot dan web ikut tanpa
menulis apa pun. Isinya MINIMAL — hanya PoolKey (c0/c1/fee/spacing) — karena
`v4_new_pool_info()` bisa membangun dict pool_info lengkap dari situ secara LOKAL
(poolId = keccak, nol sumber luar). Tidak ada angka pasar yang disimpan; kalau
indexer menyusul, entri indexer yang menang.

`own_new_pools()` mengembalikannya dan `discover_any()` (pembungkus tipis di atas
`_discover_any()`) meng-union-nya **paling akhir, sesudah semua saringan**.
Empat hal yang wajib ikut:

- **Tiap entri diverifikasi `v4_pool_exists()`** — pembuatan yang gagal atau chain
  yang di-reset tidak boleh memunculkan pool hantu. Diuji: entri palsu
  (fee 4242, kisi 777) dilewati, entri asli muncul.
- **Native dan wrapped diterima dua-duanya** saat mencocokkan sisi: PoolKey pool
  ETH memakai `address(0)` sementara user menempel alamat ERC20-nya. Jebakan yang
  sama sudah menggigit di `_v4_key_from_krystal`.
- **Cocok dari KEDUA sisi.** Menempel alamat quote-nya juga menemukan pool itu.
- **`top = pools[:10]` tidak boleh memotongnya.** Pool baru selalu ber-TVL 0
  sehingga selalu di urutan paling buntut — padahal ia satu-satunya yang tidak
  bisa ditemukan lewat jalur lain mana pun. `show_pools_for` menambahkannya di
  luar batas 10, menandainya **★**, dan menyebutkannya di legenda.

`res["source"]` bertambah slug **`own`** (`krystal+uniswap+own`), jadi pembacanya
tetap `split("+")` seperti aturan yang sudah ada — jangan dicocokkan persis.

**`v4_hook_price_refs()` TIDAK BOLEH menebak tafsir.** Versi pertama memakai
setiap pool token itu apa adanya lalu memilih satu dari empat tafsir (quote di
currency0/1 × desimal ERC20/18) yang paling dekat median pool yang sudah
diketahui. Dua-duanya salah:

- **Daftar GeckoTerminal memuat pasangan LAIN** (BEORN/SHROOM, BEORN/WETH).
  Harganya bukan harga dalam quote ini sama sekali, dan yang menahannya cuma
  saringan 10× — bukan pemahaman apa pun.
- **Memilih tafsir "paling dekat median" itu MELINGKAR**: pembandingnya jadi
  menegaskan median yang mau diperiksa. Terukur pada BEORN/USDG — dua pool yang
  diketahui berharga 0,000368 dan 0,00000387 (rata-rata geometrik 0,0000378), dan
  tafsir yang terpilih untuk pool ber-hook keluar **0,0000383**, yaitu 1,4% dari
  rata-rata itu. Terlihat seperti konfirmasi sumber ketiga, padahal cuma pantulan
  angka yang sama.

Sekarang keduanya ditutup dan tidak ada yang ditebak lagi: hanya pool yang KEDUA
sisinya cocok, dan orientasinya diturunkan dari aturan PoolKey — **currency0
selalu alamat yang lebih kecil**, jadi native (`address(0)`) selalu currency0.
Desimalnya dibaca dari kontraknya. Diverifikasi terhadap `base_token_price_usd`
GeckoTerminal untuk pool yang sama: **beda 0,00%** pada pool mati (buktinya
rumusnya eksak) dan 6–12% pada pool yang aktif diperdagangkan (GT-nya yang telat).

**Native vs wrapped WAJIB diterima keduanya.** GeckoTerminal melaporkan pool ETH
native sebagai `address(0)` sementara `CHAINS[...]["quotes"]` menyimpan alamat
WETH — tanpa menerima kedua bentuk, filter pasangan membuang **SEMUA** pool ETH
(terukur: 3 pool BEORN/WETH hilang seluruhnya). Jebakan yang sama sudah pernah
menggigit di `_v4_key_from_krystal`. Orientasi dan desimal karena itu diturunkan
dari sisi quote **BARIS ITU**, bukan dari target: baris native dan target wrapped
adalah PoolKey yang berbeda.

**Pool ber-hook BOLEH jadi sumber harga awal kalau `sq_ok`.** sqrtPriceX96 itu
rasio token1-wei per token0-wei; fee, tick spacing, dan hooks tidak ikut
menentukannya. Jadi ia berlaku apa adanya untuk PoolKey lain asalkan **urutan
currency dan desimal kedua sisinya sama**. Tanpa ini, pasangan yang seluruh
pool-nya ber-hook tidak bisa dibuat sama sekali — terukur BEORN/WETH Robinhood,
di mana ketiga pool sepasangnya native dan tak satu pun masuk discovery.

**Satu request GeckoTerminal, dipakai bertiga.** `_gecko_token_pools()` men-cache
45 detik dan melayani `discover_gecko`, `gecko_deep_price_usd`, dan
`v4_hook_price_refs` — dulu satu kartu menembak URL yang persis sama tiga kali,
dan kegagalannya SENYAP (`[]`) sehingga gejalanya bukan error melainkan "tidak ada
pool rujukan" yang muncul sesekali. Kegagalan tidak menimpa hasil lama, aturan yang
sama dengan `_dex_pairs()`.

**Selector PoolManager didekode jadi kalimat.** `v4_check_new_pool` dulu meneruskan
teks mentah web3 — user melihat `PoolManager menolak PoolKey ini: ('0x7983c051',
'0x7983c051')` untuk keadaan yang artinya cuma "pool ini sudah ada". Kelima
selector yang sudah diukur (`0x7983c051` PoolAlreadyInitialized, `0xe9e90588`/
`0xb70024f8` tick spacing, `0x14002113` fee, `0xe65af6a0` fee dinamis) sekarang
punya kalimatnya sendiri.

Dua jebakan aritmetika di jalur ini, dua-duanya sudah menggigit sekali:

- **Pool ber-harga mustahil menyeret median.** Pool ber-tick mentok (harga ~1e-20)
  tetap punya volume kecil dan ikut terhitung. Median dihitung dua lintasan:
  kasar dulu, lalu buang yang lebih dari 10x dari situ.
- **Median panjang GENAP tidak boleh `v[n//2]`.** Indeks polos selalu mengambil
  yang lebih tinggi — pada 6 pool DOT ia memilih 0,000144989 padahal dua tengahnya
  0,000125074 dan 0,000144989. `ch.geo_median()` memakai rata-rata **geometrik**
  dua nilai tengah; harga itu besaran rasio, jadi geometrik yang benar. Satu
  implementasi saja — `np_median()` cuma meneruskan ke sana.

Daftar pool di kartu dan persen deviasinya berasal dari himpunan yang SAMA
(`c["used"]`). Sebelumnya kartu menampilkan 5 teratas per volume sedangkan median
dihitung dari semuanya, dan angkanya tidak bisa direkonsiliasi user. Kartu juga
WAJIB menampilkan patokan independennya berikut tiap sumbernya — **dalam DOLAR
dengan tanda $**, karena `per_quote` sudah dibagi harga quote dan menuliskannya
tanpa satuan membuat "GMGN 0,000196" terbaca sebagai WETH/BEORN padahal itu USD
(meleset 2.600× di quote ETH).

**Harga di kartu dihitung lewat `np_price()` → `_meme_price()`, helper yang SAMA
dengan kartu mint.** Versi pertama menuliskan rumusnya ulang dan tandanya terbalik
saat quote jadi currency0 (`10**(qd-td)` bukan `10**(td-qd)`): pool DOT/USDC yang
harga awalnya 0,00027480 tampil sebagai **"0.0₂₀0"** — meleset 1e24, dan justru
di angka yang paling harus dipercaya user. Deviasi persennya juga dihitung dari
HARGA, bukan dari sqrtPrice mentah: untuk quote currency0 hubungannya terbalik,
jadi persen dari `sq` akan salah tanda.

**Jalur ini TIDAK lewat discovery.** Pool yang baru lahir belum diindeks
Krystal/indexer/GeckoTerminal, dan `_drop_dead_pools()` juga akan membuangnya
(belum punya volume). `v4_new_pool_info()` membangun dict pool_info lengkap dari
PoolKey secara LOKAL — poolId-nya keccak, tanpa satu pun sumber luar — dengan
bentuk yang sama persis dengan `_uni_v4_pool()` supaya seluruh alur mint
memakainya tanpa cabang khusus. Diuji: kartu mint, `assert_pool_orientation`, dan
`assert_pool_price_sane` semuanya lolos dengan dict buatan lokal itu.

**Pembuatan pool terjadi DI DALAM `do_mint`, bukan sebelum kartunya.** Versi
pertama membuat pool lalu menampilkan kartu konfirmasi dan MENUNGGU user menekan
Confirm. Itu mengembalikan celah yang justru mau ditutup: harga pool baru beku
sampai ada likuiditas, jadi jeda berapa detik pun berarti menyetor ke harga yang
sudah basi. Sekarang `npok|` cuma menyiapkan `PENDING` (dengan `init_sqrtp`), dan
tombol Confirm menjalankan `v4_init_pool` + `mint_v4` berurutan di dalam **satu
`TX_LOCK`** — tanpa jeda manusia di antaranya.

`ctx_slot0(ctx_data)` yang membuat itu mungkin: selama pool belum ada, ia memakai
`init_sqrtp` sebagai ganti `v4_slot0` (yang balik 0), sehingga kartu konfirmasi,
`current_mc`, dan `_amt` di `do_mint` semuanya menghitung range & jumlah deposit
dari harga rujukan. Tanpa itu seluruh matematika range runtuh.

**Harga rujukan DISEGARKAN tepat sebelum eksekusi** (`np_refresh_sqrt`), bukan
dipakai apa adanya dari kartu — kartunya bisa didiamkan menit-menit. Terukur saat
ditulis: dalam hitungan detik saja rujukan DOT/USDC bergeser **−10,03%**. Fungsi
itu juga menolak kalau pool keburu dibuat pihak lain, kalau PoolKey-nya berubah,
atau kalau deviasinya sudah lewat `NP_DEV_BLOCK`.

**Tetap dua tx, bukan `posm.multicall`.** Menggabungnya jadi satu tx memang
mungkin (selectornya ada di ketiga chain v4), tapi itu berarti menyentuh isi
`mint_v4` — jalur dana yang tidak bisa diuji ulang tanpa biaya. Yang berbahaya
bukan "dua tx", melainkan jeda manusia di antaranya, dan itu sudah hilang.

**Default mode pool baru = `wide` (DUA SISI), dan itu bukan selera.** Di pool
kosong, posisi satu sisi (Lower/Upper) meninggalkan tick aktif tanpa likuiditas
sama sekali — swap pertama menyapu seluruh range itu sekaligus di harga tepi,
persis seperti limit order yang langsung tersapu. Dua sisi di sekitar harga awal
juga satu-satunya bentuk yang menghasilkan fee dari kedua arah.

**Preset tick spacing IKUT fee, bukan daftar tetap.** Daftar mutlak (1…1000)
tidak masuk akal untuk pool ber-fee besar. Diukur dari **188 pool v4 vanilla**
(Robinhood + Arc, indexer Uniswap yang mengirim fee DAN tickSpacing eksak),
pembagi yang dipakai pembuat pool:

| pola | jumlah pool | TVL | |
|---|---|---|---|
| `fee/100` — kotak ≈ **1× fee** | **132** | $40,9jt (41,2%) | terbanyak per jumlah |
| `fee/50` — kotak ≈ **2× fee** (kanon Uniswap) | 48 | **$56,8jt (57,3%)** | terbanyak per TVL |
| lain-lain | 8 | $1,5jt | |

Jadi dua-duanya sah, dan yang besar justru memakai kisi lebih longgar. Yang lebih
rapat dari `fee/200` ada tapi di sampel ini **semuanya debu** — fee 2% kisi 10 =
TVL $458, fee 3,5% kisi 10 = $3.290, fee 4,5% kisi 60 = $0 — sedangkan `fee/200`
masih hidup (fee 4% kisi 200 = $109k dan $86k di Arc). Rentang yang terbukti
dipakai karena itu **`fee/200` … `fee/50`**, dan itulah tiga preset
`np_spacing_presets()`: rapat / standar / longgar.

Kenapa ini penting dan bukan detail kosmetik: **kotak = range tersempit yang bisa
disetel selamanya**. Tombol 🎯 Rapat memakai SATU kotak, jadi di fee 5% kisi 1000
posisi "rapat" itu lebarnya 10,5%. Tepi range juga selalu dibulatkan ke kisi, jadi
range ±25% di kisi 1000 cuma muat 2 kotak — galat pembulatannya sebesar setengah
targetnya sendiri.

**Pool yang SUDAH ada bukan kegagalan.** Simulasi `initialize` untuknya revert
`PoolAlreadyInitialized` (0x7983c051); `do_newpool` memeriksa `v4_pool_exists()`
lebih dulu dan mengarahkan ke kartu mint, bukan menolak.

### Kartu konfirmasi mint punya tombol balik ke DAFTAR pool

`⬅️ Pool lain` (`pools|<key>`) merender ulang `show_pools_for()` di pesan yang
SAMA. Tanpa itu satu-satunya jalan membandingkan pool lain dari token yang sama
adalah Cancel lalu menempel ulang CA-nya — padahal memilih fee tier justru
keputusan yang paling sering diulang.

`ctx` lama sengaja tidak dibuang: `show_pools_for` membuat key `PENDING` baru
untuk tiap pool, dan discovery-nya sudah di-cache jadi klik ini murah.

Prefiksnya `pools|`, dan handler `pool|` yang lebih dulu di router TIDAK
menangkapnya (`"pools|…".startswith("pool|")` False) — tapi kalau menambah
callback baru di sekitar sini, periksa hal itu lagi.

### Tiap kartu hasil menyebut keadaan SESUDAH + tombol buka posisi

`after_action(cid, pid, judul)` dipakai SEMUA alur yang menyisakan posisi:
mint (v2 dan v3/v4), add, reduce, collect, compound, rebalance. Ia menambahkan
baris info pool, nilai + fee unclaimed, status IN/OUT, range, dan tombol
`pos|<pid>`.

Tanpa itu kartu berhenti di daftar tx dan user harus membuka `/list` lalu mencari
posisinya lagi — padahal itu justru pertanyaan pertamanya: nilainya jadi berapa,
masih in-range atau tidak, range-nya di mana. Untuk rebalance lebih parah lagi:
seluruh guna aksinya memang memindahkan range.

Tiga hal yang gampang salah di sini:

- **Dibaca lewat `position_one`, bukan `_POS_CACHE`.** Cache masih berisi keadaan
  SEBELUM aksi (`position_busy` baru membuangnya di `finally`, sesudah kartu
  dirakit), jadi memakainya akan menampilkan angka lama sebagai "sesudah".
- **`do_close` sengaja TIDAK memakainya** — posisinya memang sudah tidak ada,
  jadi itu cuma membuang satu pembacaan RPC untuk hasil yang pasti kosong.
- **Gagal baca dilewati diam-diam** dan kembali ke `NAV_KB` biasa. Kartu hasil
  transaksi tidak boleh batal cuma karena satu pembacaan tambahan.

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

**Sebelum menyerah, `wait_ok()` menyapu SEMUA endpoint sekali lagi.** Tx bisa
mendarat persis di detik terakhir, atau mendarat di node yang belum terlihat endpoint
aktif. Terukur di Base: wrap `0x19579ae0…` masuk blok **51084522 pada 19:53:11** —
detik yang SAMA dengan saat bot melapor "tidak masuk chain", dan 3 dari 4 endpoint
sudah punya receipt-nya. Gas bukan sebabnya (maxFee 0,489 gwei vs baseFee 0,193).

Pesan gagalnya juga tidak boleh menjanjikan "tidak ada dana yang berpindah" — untuk
wrap/approve/swap itu KELIRU kalau tx-nya menyusul, dan user yang mengulang akan
menjalankan langkah itu dua kali. Sekarang bunyinya "tx MASIH BISA menyusul — jangan
langsung mengulang; cek /wallet dan explorer".

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

### Update Telegram diproses PARALEL

`Application.builder().concurrent_updates(True)` — tanpa itu PTB memproses update
**satu per satu**, jadi satu `/list` yang lama menahan seluruh klik berikutnya di
antrean. Query callback punya masa berlaku pendek sehingga yang mengantre mati
sebelum sempat dijawab: terukur di VPS `Telegram menolak pesan: Query is too old and
response timeout expired or query id is invalid`, dan di sisi user tombolnya cuma
berputar. `q.answer()` sudah ada di baris pertama router — bukan handler-nya yang
telat, melainkan update-nya belum kebagian giliran.

Aman untuk jalur dana karena penjagaannya tidak bergantung urutan update:
`TX_LOCK` menyerialkan **12 alur** pemindah dana (mint/add/reduce/collect/rebalance/
close/trigger order/cleanup/claim-all/migrate/compound/revoke) sehingga nonce tidak
bisa dobel, dan `assert_position_open()` menolak aksi ke posisi yang sudah tertutup.
Sink `set_progress` + `_GAS_WEI` global juga tetap benar: semua `with_progress`
dipasang DI DALAM `TX_LOCK`. Kalau menambah alur tx baru, dua syarat itu wajib ikut.

`monitor_loop` dan `_loop_watchdog` didaftarkan `_start_background()`, yang menunggu
`app.running` dulu. `app.create_task()` langsung di `post_init` memberi
PTBUserWarning "Tasks created while the application is not running won't be
automatically awaited" — task-nya tetap jalan, tapi tidak ikut di-await sehingga
error di dalamnya hilang diam-diam (terbukti: warning-nya persis dijaga
`app.running`, True → 0 warning). Job queue menyelesaikannya juga, tapi butuh extra
`python-telegram-bot[job-queue]` (APScheduler) yang di VPS tidak terpasang sehingga
cabang cadangannya memunculkan warning yang sama. Menunggu `app.running` tidak butuh
dependensi apa pun.

**`q.answer()` yang gagal TIDAK boleh membatalkan aksinya.** Query kedaluwarsa cuma
berarti spinner tombol tidak bisa dihentikan; dulu BadRequest-nya melempar keluar
sebelum aksinya sempat jalan, dan `on_error` mengirim "aksinya kemungkinan sudah
jalan" yang justru terbalik dari kenyataan.

Dua alat ukur dipasang supaya "lambat" tidak perlu ditebak lagi:
`_loop_watchdog()` mencatat lag event loop (lag ~0 = lambatnya murni kerja RPC; lag
beberapa detik = ada panggilan blocking yang lupa dibungkus `asyncio.to_thread`), dan
router callback mencatat klik yang >3 detik berikut lag saat itu.

Executor default `asyncio.to_thread` disetel eksplisit **32 worker**. Default-nya
`min(32, cpu+4)` — di VPS 2 core cuma 6, sehingga pembacaan posisi milik
`monitor_loop` dan klik user berebut slot dan yang kalah menunggu giliran, persis
terlihat seperti RPC lambat. Semuanya kerja I/O, jadi jumlahnya tidak perlu ikut
jumlah core.

### `monitor_loop` adalah pemakai CU RPC terbesar

Terukur: **satu pindai wallet = 199 request RPC** untuk 16 posisi (12,4 per posisi).
Loop ini jalan terus-menerus, jadi intervalnya yang menentukan tagihan — bukan
pemakaian UI.

| konfigurasi | request/hari | CU/hari (26 CU per `eth_call`) |
|---|---|---|
| 2 wallet tiap 30 detik | 1.146.240 | **~30M** |
| 2 wallet tiap 120 detik | 286.560 | ~7,5M |
| 1 wallet (order saja) tiap 120 detik | 143.280 | ~3,7M |

Kuota Alchemy free 30M CU/bulan — konfigurasi lama menghabiskannya dalam **satu
hari**, dan throughput-nya menembus batas (terukur **487,7 / 300 CU/s**) sehingga
muncul 429 yang membuat posisi hilang dari `/list`.

Dua sebabnya, keduanya sudah diperbaiki:

- **`30 if order_chains else …`** — satu order aktif memaksa pindai tiap 30 detik
  selamanya, mengabaikan setelan user. Sekarang `max(30, order_secs, alert_secs)`
  dengan `order_secs` default 120.
- **Semua wallet dipindai di semua chain.** Alert memang butuh semua wallet, tapi
  hanya di chain aktif; pengecekan order cuma butuh wallet pemilik order.
  `_gather_positions(cid, only_wallets)` membatasinya, dan chain tanpa order
  di-skip total.

Pembersihan `RANGE_STATE` HANYA boleh jalan saat pindai penuh (`need is None`) —
kalau `live` dibangun dari sebagian wallet, entri wallet lain ikut terbuang dan
transisi range berikutnya hilang karena dianggap baseline baru.

#### Pindai monitor memakai jalur RINGAN

Terukur per posisi v4 saat hangat, pembacaan penuh = **6 panggilan**: `ownerOf`,
`getPoolAndPositionInfo`, `getPositionLiquidity`, `getSlot0`, plus DUA
(`getFeeGrowthInside` + `getPositionInfo`) yang semata-mata untuk fee unclaimed.
Monitor sendiri cuma memakai `in_range`, `mc_now`, dan `mc_lower`.

`list_all_positions(..., light=True)` → `_v4_light()` memangkasnya:

- `_v4_static()` men-cache `(PoolKey, tick_lower, tick_upper)` **selamanya** —
  ketiganya imutabel untuk satu tokenId, dan tokenId v4 monoton naik jadi tidak
  pernah dipakai ulang. Cache ini juga dipakai jalur PENUH, jadi semua pembacaan
  ikut hemat satu panggilan.
- `slot0` dibagi antar posisi yang sepool (`slot0_shared`).
- `ownerOf` tidak dibaca: `getPositionLiquidity == 0` sudah cukup untuk menyatakan
  posisi tutup/terbakar.
- Fee unclaimed TIDAK dibaca sama sekali.

Terukur pada 2 posisi hidup: **11 request → 5** (2,2×), dan `mc_now` **identik
sampai desimal terakhir** (Δ0,000000%), begitu juga `in_range` dan `mc_lower` —
rumusnya memang disalin persis. Jadi keputusan TP/SL tidak berubah sedikit pun.
Penghematannya bukan 10× karena `slot0` tetap satu per POOL dan tiap posisi
lazimnya di pool berbeda; yang hilang justru bagian termahal (fee).

Tiga penjagaan, dan dua di antaranya pernah jadi bug kalau dilanggar:

- **`value_usd`/`unclaimed_usd` hasil ringan bernilai 0**, ditandai `p["light"]`.
  `_full_pos()` membacanya ulang SEBELUM dipakai — dipanggil saat alert
  BERBUNYI dan saat order TERPICU, dua-duanya jarang. `record_event` dengan nilai
  0 akan merusak riwayat PnL secara permanen.
- **Hasil ringan TIDAK boleh masuk `_POS_CACHE`.** Kalau masuk, `/list` menampilkan
  seluruh posisi bernilai $0. `list_positions_all(light=True)` karena itu melewati
  cache dua arah.
- **Tiap posisi ditandai `p["_wallet"]`** supaya `_full_pos()` bisa membaca ulang
  dengan key wallet yang BENAR (`pk_for`), bukan wallet aktif.

### Daftar posisi: satu pembaca, banyak pembaca gratis

Membaca daftar itu N posisi x ~12 panggilan RPC. Dulu SETIAP klik `/list`,
`Refresh`, dan SETIAP putaran `monitor_loop` membayarnya lagi dari nol — dua
pemakai yang butuh data SAMA saling menggandakan tagihan CU, lalu 429 mengenai
keduanya. Terukur di VPS: `menu|list makan 16.9s`, `refresh 19.2s`,
`pos|… 10.3s`, semuanya `lag loop 0.0s`.

`list_positions_all(cid, key, errors, fresh=False)` sekarang stale-while-revalidate
(`_POS_CACHE`, segar `_POS_FRESH_SECS` 45 detik, basi masih disajikan sampai
`_POS_MAX_STALE` 900 detik sambil disegarkan di thread latar — satu per
`(chain, wallet)`, dijaga `_POS_BUSY`). Terukur: klik pertama 0,80 detik,
**klik berikutnya 0,00 detik tanpa satu pun pembacaan chain**, dan saat basi
daftarnya keluar SEKARANG sementara penyegarannya jalan di latar.

`monitor_loop` yang MENGISI cache (`fresh=True`), jadi volume RPC total justru
TURUN, bukan cuma berpindah: UI berhenti menembak pembacaannya sendiri.

Dua aturan yang wajib ikut, dan keduanya lebih penting daripada kecepatannya:

- **`fresh=True` untuk jalur yang memindahkan uang.** Eksekutor TP/SL dan
  snapshot sebelum migrate memutuskan aksi dana — angka basi di situ tidak boleh.
  Jalur tampilan boleh basi.
- **Cache DIBUANG sesudah tiap perubahan posisi** (`pos_cache_drop()`): di
  `position_busy` (mencakup 6 alur add/reduce/collect/rebalance/close/compound),
  di `do_mint`, `/recover`, `/cleanup`, dan tombol Refresh. Menampilkan posisi
  yang sudah ditutup jauh lebih buruk daripada menunggu satu pembacaan.

Yang dikembalikan salinan DANGKAL: pemanggil boleh menyaring/mengurutkan, tapi
dict posisinya dipakai bersama semua pembaca — baca saja, jangan dimutasi
(jebakan yang sama dengan `store._hist()`).

### Read timeout RPC 10 detik, bukan 30

Terukur di VPS: `ReadTimeoutError(host='rpc.mainnet.chain.robinhood.com',
read timeout=30)` berulang — endpoint publiknya menerima koneksi lalu diam.
Dengan retry di atasnya, satu panggilan bisa menghabiskan menit. `get_w3`
mencoba endpoint berurutan, jadi menunggu lama di yang menggantung selalu lebih
buruk daripada pindah. `_RPC_READ_TIMEOUT` = 10; timeout konek tetap 5.

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

Empat sumber lambat yang sudah diukur dan diperbaiki — jangan dibalik:

- **Detail posisi v4/v2 dibaca paralel** di `list_all_positions()`. Satu posisi v4 =
  11 panggilan RPC ≈ 3,3 detik di RPC ber-latensi 270 ms; berurutan, 14 posisi butuh
  ~49 detik. Dengan `ThreadPoolExecutor` (maks 8): **8,66 detik**, hasil identik.
  Jalur v3 memang sudah paralel sejak awal — v4/v2 yang tertinggal.
- **Backoff retry RPC dipendekkan** (`total=4, backoff_factor=0.3`, total ~4 detik).
  Sebelumnya `total=6, backoff_factor=0.6`: satu panggilan yang kena 429 tidur
  0,6+1,2+2,4+4,8+9,6+19,2 ≈ **37 detik** sebelum pemanggilnya tahu ada masalah, dan
  satu kartu posisi butuh ~11 panggilan. Endpoint bermasalah ditangani failover
  `get_w3`, bukan dengan menunggu lebih lama di endpoint yang sama.
- **Timeout konek 5 detik** (`request_kwargs={"timeout": (5, 30)}`). `get_w3` mencoba
  endpoint berurutan, jadi 30 detik per endpoint mati berlipat sebelum sampai yang hidup.
- **Daftar kandidat indexer Uniswap pakai stale-while-revalidate** (`_swr()`).
  Terukur dari VPS ber-Alchemy: satu panggilan indexer **2,07 detik**, sedangkan
  membaca detail satu posisi on-chain cuma **0,19 detik** — jadi `/list` didominasi
  menunggu indexer, bukan RPC, dan dengan ttl 20 detik hampir tiap refresh
  membayarnya lagi. Sekarang hasil basi (sampai `_SWR_MAX_STALE` = 600 detik)
  dikembalikan SEKARANG dan penyegaran jalan di thread latar (satu thread per key,
  dijaga `_SWR_BUSY`). Aman dibuat basi karena daftar itu CUMA kandidat:
  `list_positions()` selalu meng-union-kan dengan enumerasi NFT terbaru on-chain
  (indexer memang bisa telat berjam-jam) dan detail tiap posisi selalu dibaca
  on-chain. Basi berarti "kandidat lama ikut diperiksa", bukan "posisi baru tidak
  terlihat". Terukur: 1,52s → 0,000s, hasil identik.
- **`store._hist()` di-cache** dengan kunci `(mtime_ns, ukuran)`. Kartu `/list`
  memanggil `mint_usd`/`fees_claimed_usd`/`withdrawn_usd`/`mint_ts` per posisi, jadi
  satu refresh mem-parse `history.json` puluhan kali. Kunci mtime membuat tulisan dari
  proses lain (web.py) tetap terbaca — file ditulis atomik lewat rename.
  **Cache itu HANYA untuk pembaca** — lihat bagian di bawah.

`provider.cache_allowed_requests = True` **dipertahankan** untuk pembacaan posisi
(terukur 11 vs 18 panggilan RPC, 3,3 vs 5,1 detik), tapi **dimatikan di jalur polling
tx** lewat `_no_req_cache(w3)`. Untuk tiap hasil ber-`blockNumber`, web3 menembak satu
`eth_getBlockByNumber` EKSTRA hanya untuk memutuskan boleh di-cache atau tidak; blok
receipt yang baru masuk sering belum terbaca sehingga panggilan itu balik null dan
web3 mencatat `TypeError: 'NoneType' object is not subscriptable`
(`request_caching_validation.py:121`). Errornya ditangkap web3 jadi tidak merusak
apa pun — yang mahal round-trip terbuangnya, tiap poll, selama `wait_ok` menunggu
sampai 180 detik per tx. Receipt tx pending juga memang tidak layak di-cache.

### PoA: `get_block` di BSC

BSC memakai extraData 280 byte, jauh di atas 32 byte yang divalidasi web3, jadi
`eth_getBlock` **selalu** melempar `ExtraDataLengthError` tanpa middleware PoA —
`price_history()` (chart) dan pembacaan timestamp blok di web.py mati diam-diam di
chain itu. `_poa()` dipasang di `get_w3()` dan `_forced_ip_w3()`; aman untuk chain
non-PoA karena middleware-nya cuma memangkas extraData yang kepanjangan.

### `chain.py` TIDAK memuat `.env`

Hanya `bot.py` (di dalam `main()`) dan `web.py` (saat import) memanggil `load_dotenv`.
Akibatnya script diagnostik yang cuma `import chain` jalan **tanpa**
`ALCHEMY_API_KEY`, jatuh ke RPC publik, dan mengukur latensi yang sama sekali bukan
yang dipakai bot — terukur Robinhood **247 ms tanpa .env vs 17,7 ms dengan Alchemy**,
dan kesimpulan "perlu self-host" yang ditarik dari angka itu salah total. Script
apa pun yang mengukur atau meniru jalur bot WAJIB `load_dotenv(Path(...)/".env")`
dengan path eksplisit — `find_dotenv()` melempar AssertionError kalau dijalankan
dari stdin (`python3 - <<EOF`).

Alchemy per-network: `base-mainnet` menjawab 4xx kalau network itu belum di-enable
di dashboard app-nya, dan `get_w3` diam-diam jatuh ke RPC publik (terukur 279 ms vs
98 ms `base-rpc.publicnode.com`). Gejalanya cuma "lambat", bukan error.

### Jaringan

`get_w3()` melakukan failover multi-endpoint (Alchemy → RPC publik) dengan retry+backoff.
Ada bypass khusus blokir DNS ISP Indonesia: resolve via DNS-over-HTTPS lalu konek ke IP
langsung dengan SNI dipertahankan (`_SNIAdapter`, `_forced_ip_w3`) — sertifikat tetap
diverifikasi, jangan dilonggarkan.

### Revoke approval

Bot memberi approval **tak terbatas** ke router/NPM saat mint & swap (`ensure_approval`
memakai `MAX_UINT256`) supaya tidak membayar gas approve tiap transaksi. Selama
approval itu hidup, kontrak tersebut boleh memindahkan token itu kapan saja.

`/revoke` → `scan_approvals()` → `revoke_approval()`. Dua jenis, cara mencabutnya beda:

- **ERC20** — `approve(spender, 0)`.
- **Permit2** — `permit2.approve(token, spender, 0, 0)`. Jumlah 0 melumpuhkan spender
  walau kedaluwarsanya belum lewat. Permit2 yang SUDAH kedaluwarsa tidak dilaporkan.

`approval_spenders()` menyusun daftar spender dari dict chain (NPM/router v3/router
v2/posm/UniversalRouter tiap DEX + Permit2), jadi DEX atau chain baru otomatis ikut —
jangan menuliskan alamat lagi di situ. `permit2` bisa tinggal di sub-dict DEX
(BSC: hanya Uniswap yang punya v4), jadi dibaca dari level chain DAN dex.

**Cakupannya sengaja sempit dan itu perlu disebut ke user.** Rabby menemukan SEMUA
spender dengan membaca event `Approval`; di sini tidak bisa — getLogs rentang lebar
ditolak hampir semua RPC publik (BSC/Base terukur maks 5.000 blok ≈ 2 jam). Jadi
`scan_approvals()` cuma memeriksa spender yang ada di `CHAINS`, dan `/revoke 0xAlamat`
menerima spender lain yang disodorkan user (`extra_spenders`).

Token yang dipindai: quote tetap + quote runtime + ERC20 di wallet (`wallet_tokens`,
butuh Alchemy). Sengaja tidak menyapu seluruh riwayat transfer — approval yang
berbahaya adalah yang tokennya masih dipegang. Terukur: 4 approval aktif ditemukan
di 1,6 detik, gas cabut 31k–42k (~0,000001 ETH per approval).

**Allowance Permit2 yang dikunci kontrak token tidak boleh dilaporkan.** ERC20 gaya
Solady dengan `_givePermit2InfiniteAllowance()` meng-hardcode
`allowance(owner, Permit2) == type(uint256).max` — itu BUKAN approval yang user
berikan — dan `approve(Permit2, …)` sengaja revert
`Permit2AllowanceIsFixedAtInfinity()` (selector `0x3f68539a`). Terukur di Robinhood:
FRONG/Liluni/POOLS bytecode-nya identik 7154 byte dan `approve` ke Permit2 gagal
untuk jumlah berapa pun, sedangkan ke NPM/router/alamat acak lolos.

`scan_approvals()` mendeteksinya dengan menyimulasikan `approve(permit2, 0)` dan
menandai `fixed=True`; UI memisahkannya dari daftar bertombol supaya tidak ada
tombol yang dijamin gagal, tapi tetap menyebutkannya.

Mencabut aman: bot minta approval lagi sendiri saat mint/swap berikutnya.

### Wallet: .env + brankas

`all_pks()` = wallet `.env` (urutannya TETAP, supaya arti "W1" tidak bergeser) lalu
wallet EVM dari `store.wallets()` — entri ber-`kind == "sol"` DILEWATI, kalau tidak
key ed25519 masuk daftar secp256k1. `sol_pks()` kebalikannya. Sengaja TIDAK di-cache — brankas berubah saat runtime.
`env_pks()` yang di-cache. Wallet `.env` tidak bisa dihapus lewat bot (`is_env_pk()`).

Private key lewat chat itu permanen di riwayat Telegram, jadi: pesan impor dihapus
begitu dibaca, hasil ekspor dihapus otomatis 60 detik (`_autodelete()`), dan ekspor
maupun hapus selalu dua langkah dengan peringatan. Jangan hilangkan penjagaan itu.

### Scanner token trending (GMGN) — di DALAM bot ini

`gmgn.py` adalah port Python dari lp-scanner (dulu proses Node terpisah). Satu
repo, satu proses, satu deploy — tanpa file jembatan dan tanpa mesin
cooldown/Telegram kembar. Dinyalakan lewat `/scanner`, dipaksa jalan sekarang
lewat `/scan`, butuh `GMGN_API_KEY`. Tanpa key, scanner diam dan sisa bot normal.

**Lima aturan berikut ikut diport dan tidak boleh dilanggar** — semuanya lahir
dari kejadian nyata, dan melanggarnya membuat bot memberi jawaban salah soal uang:

1. **Nilai kosong BUKAN nol.** `null`, `""`, `-1` = "belum diketahui". Menyamakan
   dengan 0 membuat "belum diuji" tampil sebagai "pajak 0% • bukan honeypot •
   wewenang sudah dilepas". `_num()`/`_tri()` yang menjaganya.
2. **Field keamanan beda per chain.** Solana `renounced_mint`/
   `renounced_freeze_account`; EVM `is_renounced`/`is_open_source`. Membaca
   silang menghasilkan vonis palsu.
3. **Jangan mengarang angka yang tidak diberikan GMGN.** `pool.fee_ratio` terukur
   `0.1` sementara `trade_fee / volume_24h` pada token yang sama 0,0074% — beda
   ~13×, unitnya tidak terdokumentasi. Tidak dipakai.
4. **Label GMGN diteruskan apa adanya.** `insider`/`bundler`/`entrapment`/`sniper`
   metodenya tidak dipublikasi — ditampilkan dengan sumbernya, bukan jadi vonis.
5. **Rate limit nyata dan bannya per-IP.** Tiap retry MENAMBAH ban 5 detik sampai
   5 menit. `Client` menjaga jeda `pace` (default 1 detik) antar request — dan
   klien-nya dibuat ULANG hanya kalau pace berubah, karena penghitung throttle
   dan sesi HTTP-nya harus bertahan antar siklus. 429 → tunggu `reset_at`, jangan
   retry. Kredensial ditolak → scanner DIMATIKAN + user diberi tahu; menunggu
   tidak menyembuhkan key yang salah.

Tiga hal yang gampang salah dan sudah ditutup:

- **`filters` yang tersimpan TIDAK di-merge dengan default.** Kalau di-merge,
  `/scanner set maxRugRatio off` tidak akan pernah berfungsi — kunci yang baru
  dihapus langsung diisi ulang default di siklus berikutnya.
- **Baris filter dibangun dari `FILTER_SPEC`,** bukan ditulis satu per satu.
  Filter yang dimatikan tidak punya kunci, dan menuliskannya manual membuat
  `fmt_usd(None)` meledak — pernah kejadian persis begitu.
- **Ambang risiko menolak token yang datanya BELUM DIKETAHUI** (`passes()`).
  Sama seperti perilaku server GMGN, supaya "belum diuji" tidak lolos
  seolah-olah "aman".

`PollState` (`.scanner_state.json`, ditulis tmp+`rename()`) menyimpan jendela
konfirmasi dan cooldown. Tanpa itu tiap `systemctl restart` mengirim ulang semua
kartu yang baru dikirim. Kuncinya `chain:address` — alamat yang sama bisa ada di
beberapa chain EVM — dan `record(scope=chain)` mencegah scan chain A dihitung
sebagai "tidak muncul" bagi token chain B yang belum discan siklus itu.

**`/scan` melewati saklar on/off tapi TIDAK melewati konfirmasi + cooldown.**
Melewatinya akan membuat perintah itu jadi tombol spam.

#### `EVM_CHAINS` = semua chain GMGN KECUALI `sol`

Daftar putih manualnya sempat ketinggalan `robinhood`, `arc`, dan `stable`, dan
akibatnya bukan kosmetik: `normalize()` menyaring lewat `EVM_CHAINS` sedangkan
`classify()` menyaring lewat `chain == "sol"`. Untuk ketiga chain itu keduanya
tidak sepakat — `isRenounced`/`isOpenSource` dipaksa `None` di normalize, lalu
cabang peringatan di classify tidak pernah berbunyi. Token yang ownership-nya
**belum di-renounce** dan kontraknya **tidak open source** lolos tanpa satu pun
peringatan (diuji: sesudah diperbaiki keduanya muncul, sebelumnya nol).

Diverifikasi langsung ke API GMGN: `arc`, `robinhood`, `base`, dan `stable`
sama-sama mengirim `is_renounced`/`is_open_source` dengan `renounced_mint`/
`renounced_freeze_account` null; hanya `sol` yang sebaliknya. Karena itu
daftarnya diturunkan dari `CHAINS` (`c != "sol"`), bukan ditulis tangan — chain
baru otomatis ikut, dan kalau suatu hari ada chain non-EVM lain, field EVM-nya
terbaca None = "belum diketahui" yang memang arah amannya.

#### Tiga angka waktu yang berbeda — jangan tertukar

User pernah membacanya sebagai satu hal, jadi kartunya WAJIB menjelaskan:

| | arti |
|---|---|
| `interval` (5m) | jendela data GMGN — "volume 5 menit terakhir". Bukan jadwal. |
| `watch` (60s) | seberapa sering bot memindai. |
| `cooldown` (30m) | **per TOKEN**, bukan per scan. Token yang sudah dikirim tidak dikirim lagi selama itu; scan tetap jalan dan token LAIN tetap masuk. |

`confirm`/`window` (2/3) beda lagi: token harus muncul di 2 dari 3 scan terakhir
sebelum dikirim, untuk menyaring lonjakan satu-tick.

#### Umur token: menit di bawah satu jam, bukan pecahan jam

`fmt_age_short()`. Dulu selalu `{detik/3600:.1f} jam`, jadi token berumur **11
menit** terbaca **"0,2 jam"** — dan itu justru menghilangkan informasi yang paling
penting untuk token sebaru itu. Datanya sendiri benar (`creation_timestamp` GMGN
terverifikasi: 22:34:34, umur 663 detik); yang salah cuma pembulatannya.

Satuan mengikuti skalanya: detik → menit → jam+menit → hari+jam, dengan bagian
nol dibuang ("1 jam", bukan "1 jam 0 menit"). Penanda "sangat baru" (<30 menit)
dan "baru" (<24 jam) ikut, sama seperti versi Node.

#### Editor filter bertombol

`sf|__list` → daftar per kelompok (`gmgn.FILTER_GROUPS`) → `sf|<key>` → editor
nilai (`sfv|<key>|<val>`, `sfx|` untuk ketik bebas, `off` untuk mematikan).
Pilihan cepat per satuan ada di `gmgn.FILTER_CHOICES` — angka bulat yang lazim,
bukan hasil ukur apa pun.

**Validasi hidup di SATU tempat**, `scanfilt_set()`: tombol dan `/scanner set`
memakainya berdua, jadi aturannya tidak bisa berbeda. Rasio ditolak di luar 0–1
(user mengetik "5" untuk 5% itu wajar, dan tanpa cek ini filternya jadi mati
total karena tidak ada rasio yang melebihi 5), umur wajib bentuk `30m`/`6h`/`7d`,
dan nilai negatif ditolak untuk USD/count.

Daftar filter dirender dari `FILTER_SPEC` + `FILTER_DESC`, bukan ditulis satu per
satu — filter yang dimatikan tidak punya kunci, dan menulisnya manual membuat
`fmt_usd(None)` meledak (pernah kejadian).

#### Jembatan file (opsional, untuk umpan dari luar)

`LP_ALERT_INBOX` masih ada dan tetap jalan: proses lain boleh menulis kandidat
sebagai JSONL dan bot ini memprosesnya lewat jalur yang sama. Tidak diperlukan
lagi sejak scanner-nya di dalam — dipertahankan supaya sumber lain (mis. scanner
Node lama di `alert/`, yang di-`.gitignore` dan repo terpisah) tetap bisa dipakai
tanpa menulis ulang apa pun.

**Mint tetap manual.** Jalur ini tidak pernah mengirim transaksi — ia berhenti di
kartu pool yang sama persis dengan hasil tempel-CA manual (`show_pools_for`,
tombol `pool|<key>`). Konsekuensinya tidak ada jalur transaksi baru yang perlu
diuji ulang, dan itu memang tujuannya.

- **File, bukan HTTP.** Dua proses restart sendiri-sendiri; tidak perlu port,
  token, atau salah satunya hidup. `_lp_take()` mengambil isinya lewat
  `os.replace()` lalu mengosongkan — penulis membuka file lewat PATH tiap append,
  jadi sesudah rename ia membuat file baru dan tidak ada baris yang tertimpa. Sisa
  `.taking` dari proses yang mati di tengah dibaca duluan supaya kandidat tidak
  hilang gara-gara restart.
- **Chain dipetakan lewat `CHAINS[cid]["gmgn"]`**, bukan tabel baru. Slug yang
  tidak punya padanan (`sol`, `eth`, `arbitrum`, …) dilewati — scanner sudah
  menyaringnya juga, ini lapis kedua.
- **Redaman ganda `_LP_SEEN`.** Scanner punya cooldown sendiri, tapi restart-nya
  mengosongkan state; tanpa lapis ini satu restart bisa membanjiri chat dengan
  token yang sama.

**Saran posisi hanya dari yang benar-benar diukur.** `lp_suggestion()` menyebut
dasar tiap angkanya:

- **Pool** dari discovery bot sendiri (sudah diverifikasi on-chain), bukan dari
  GMGN. Ditunjuk dua: terdalam dan APR-terbaik.
- **APR pool debu TIDAK pernah ditunjuk.** APR dihitung ÷TVL, jadi pool $800
  dengan sedikit volume selalu mengalahkan pool $150k — terukur 40.899% vs 1.587%
  pada microduck. Kandidat dibatasi ke TVL ≥ `_LP_APR_MIN_SHARE` (20%) pool
  terdalam, dan yang terbuang **tetap disebut berikut alasannya**, bukan
  dihilangkan diam-diam.
- **Mode range** dari `recommend_strategy()` yang mengukur volatilitas pool lewat
  oracle TWAP. Pool v4 tidak punya oracle itu — kalau tidak terukur, kartu
  MENGATAKAN tidak terukur, bukan diganti tebakan.
- **Kelayakan lebar range** itu aritmetika lurus dari gerak harga yang dilaporkan
  GMGN pada interval alert: berapa kali gerakan sebesar itu, searah, sampai harga
  keluar range. Bukan model apa pun.
- **Likuiditas GMGN vs TVL per-pool** disilang-cek dan selisih besar disebutkan —
  GMGN menjumlah semua pool token itu, bot menghitung per-pool. Dua sumber yang
  tidak sepakat adalah informasi, bukan gangguan.

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
