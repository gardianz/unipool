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
`ALCHEMY_API_KEYS` (dipisah koma/spasi), dan `ALCHEMY_API_KEY_2..10`, dedupe dengan
urutan dipertahankan. Tiap key jadi **endpoint tersendiri** di `get_w3`, jadi tidak
ada mekanisme baru yang perlu ditulis — rotasi `_RPC_BAD` yang sudah ada langsung
bekerja: key yang kena 429 ditandai, dilewati 120 detik, dan panggilan berikutnya
jalan lewat key berikutnya. Kuota Alchemy dihitung per-app, jadi N key = N jatah.
Terukur: dengan key `aaa` ditandai kena limit, endpoint terpilih berikutnya adalah
key `bbb` — bukan RPC publik yang 10x lebih lambat.

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

Kalau Krystal tidak kenal tokennya, `token_chains_onchain()` mengecek `eth_getCode`
per chain sebagai petunjuk terakhir. Itu satu request per chain, jadi HANYA dipakai
di jalur "tidak ada pool", tidak pernah di jalur normal.

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
menukar satuan. `compute_amount()` menghitung nilai meme dalam quote lalu
membaginya dengan porsi meme range: `budget = nilai_meme / (1 − keep_frac)`, dengan
`keep_frac` dari `plan_two_sided()`. Terukur di GME/WETH: 75% dari 226.545,62 GME
menghasilkan sisi meme 169.909,22 = **75,0%** persis.

Dua syarat yang gampang terlewat:

- **Range harus dihitung SEBELUM amount.** `build_preview` dan `do_mint` sama-sama
  menghitung tick lebih dulu lalu mengoper `sqrtp` + `ticks` ke `compute_amount()`.
  Kalau salah satu lupa, jumlah yang dieksekusi beda dari yang ditampilkan.
- **Kartu wajib menyebut satuan persennya** (`75% GME` vs `75% USDG`) — tanpa itu
  "75%" ambigu dan user salah memperkirakan berapa yang dipakai.

Mode `lower` (100% quote) memakai nilai meme apa adanya: seluruh meme dijual.

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

Kalau perlu memangkas lebih jauh: monitor sebenarnya cuma memakai `in_range`,
`mc_now`, dan `mc_lower`. Itu bisa dihitung dari **satu** panggilan slot0 per pool
(sisanya statis atau sudah di-cache: `token_supply`, `quote_usd_price`), bukan 12,4
panggilan per posisi. Belum dikerjakan — perlu jalur "ringan" di
`_position_detail`/`_v4_position_detail` dan sentuh jalur eksekutor TP/SL.

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
