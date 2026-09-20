/*
 * Sidecar Meteora DLMM — dipanggil `solana.py` lewat stdin/stdout JSON.
 *
 * Kenapa proses Node dan bukan Python: SDK resmi Meteora (@meteora-ag/dlmm)
 * cuma ada di TypeScript/Rust, dan seluruh bagian yang berbahaya — encode
 * instruksi Anchor, turunan PDA bin array, matematika bin, Token-2022 transfer
 * hook, compute budget — ada di dalamnya. Menulis ulang itu di Python berarti
 * menulis sendiri jalur uang yang tidak bisa diuji tanpa biaya.
 *
 * CommonJS, bukan ESM: build ESM SDK-nya rusak — `import` dari dist/index.mjs
 * gagal dengan "Directory import .../@coral-xyz/anchor/dist/cjs/utils/bytes is
 * not supported". Jangan ditambahkan "type": "module" ke package.json.
 *
 * Protokol: SATU request JSON di stdin, SATU response JSON di stdout.
 *   { "cmd": "...", "rpc": "https://...", "secret": "<base58|json array>", ... }
 *   -> { "ok": true, ... }  |  { "ok": false, "error": "..." }
 *
 * `secret` HANYA lewat stdin, tidak pernah argv: argv terlihat di `ps` oleh
 * setiap user di mesin itu.
 */
"use strict";

const web3 = require("@solana/web3.js");
const { Connection, Keypair, PublicKey, ComputeBudgetProgram } = web3;
const BN = require("bn.js");
const bs58 = require("bs58").default || require("bs58");
const dlmmPkg = require("@meteora-ag/dlmm");
const DLMM = dlmmPkg.default || dlmmPkg;
const { StrategyType } = dlmmPkg;

const SOL_MINT = "So11111111111111111111111111111111111111112";

// ---------- util ----------
const bn = (v) => new BN(String(v ?? "0"));
const s = (v) => (v === null || v === undefined ? null : String(v));

function keypairFrom(secret) {
  if (!secret) throw new Error("SOLANA_PRIVATE_KEY kosong");
  const t = String(secret).trim();
  if (t.startsWith("[")) return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(t)));
  return Keypair.fromSecretKey(bs58.decode(t));
}

function strategyOf(name) {
  const k = String(name || "Spot").toLowerCase();
  if (k === "curve") return StrategyType.Curve;
  if (k === "bidask" || k === "bid-ask") return StrategyType.BidAsk;
  return StrategyType.Spot;
}

/* Jumlah tx yang SUDAH disiarkan pada proses ini. Dipakai main() untuk
 * memutuskan boleh-tidaknya mengulang perintah di endpoint lain: selama nol,
 * mengulang dari awal mustahil menyetor/menarik dua kali. Dinaikkan SEBELUM
 * `await`, karena tx yang timeout pun bisa tetap mendarat. */
let SENT = 0;

/* Semua tx dikirim lewat sini supaya priority fee + compute limit seragam.
 * Tanpa priority fee, tx DLMM rutin tertinggal saat jaringan ramai — dan yang
 * dilihat user cuma "timeout", bukan sebab yang bisa ditindaklanjuti. */
const CONFIRM_TIMEOUT_MS = 90_000;
const REBROADCAST_MS = 4_000;

/* Kirim SATU tx lalu tunggu sampai benar-benar pasti.
 *
 * `sendAndConfirmTransaction` TIDAK dipakai, dan alasannya kejadian sungguhan:
 * ia melempar *"Signature … has expired: block height exceeded"* untuk tx yang
 * ternyata **sukses dan finalized**. Alur rebalance lalu berhenti di tengah —
 * satu potong `removeLiquidity` sudah mendarat sementara sisanya tidak pernah
 * dikirim, dan posisinya tertinggal setengah terkuras. Kelas kegagalan yang
 * sama persis dengan "mint sukses tapi dilaporkan gagal" di jalur EVM.
 *
 * Karena itu: tanda tangan dipegang SENDIRI, status diperiksa berkala, dan
 * begitu kedaluwarsa statusnya diperiksa SEKALI LAGI sebelum menyerah. Raw tx
 * yang sama disiarkan ulang tiap beberapa detik — tanda tangannya identik jadi
 * mustahil dobel, aturan yang sama dengan `_rebroadcast` di EVM. */
async function sendOne(conn, tx, signers, microLamports) {
  if (microLamports > 0) {
    tx.instructions.unshift(
      ComputeBudgetProgram.setComputeUnitPrice({ microLamports: Number(microLamports) }));
  }
  const bh = await conn.getLatestBlockhash("confirmed");
  tx.recentBlockhash = bh.blockhash;
  tx.lastValidBlockHeight = bh.lastValidBlockHeight;
  tx.feePayer = signers[0].publicKey;
  tx.sign(...signers);
  const raw = tx.serialize();
  SENT += 1;                       // sebelum kirim: tx yang timeout pun bisa mendarat
  const sig = await conn.sendRawTransaction(raw, { skipPreflight: false, maxRetries: 5 });

  const check = async () => {
    const st = await conn.getSignatureStatuses([sig], { searchTransactionHistory: true });
    const v = (st && st.value && st.value[0]) || null;
    if (!v) return null;
    if (v.err) throw new Error(`Tx ${sig} gagal di chain: ${JSON.stringify(v.err)}`);
    return (v.confirmationStatus === "confirmed" || v.confirmationStatus === "finalized")
      ? sig : null;
  };

  const t0 = Date.now();
  let lastSend = t0;
  while (Date.now() - t0 < CONFIRM_TIMEOUT_MS) {
    await new Promise((r) => setTimeout(r, 1200));
    const ok = await check();
    if (ok) return ok;
    if (Date.now() - lastSend > REBROADCAST_MS) {
      lastSend = Date.now();
      try { await conn.sendRawTransaction(raw, { skipPreflight: true }); } catch (_) {}
    }
    let h = 0;
    try { h = await conn.getBlockHeight("confirmed"); } catch (_) {}
    if (h && h > bh.lastValidBlockHeight) {
      // Kedaluwarsa BUKAN bukti gagal — periksa sekali lagi sebelum menyerah.
      const last = await check();
      if (last) return last;
      throw new Error(`Tx ${sig} kedaluwarsa tanpa masuk chain (blockhash lewat).`);
    }
  }
  const last = await check();
  if (last) return last;
  throw new Error(`Tx ${sig} belum terkonfirmasi setelah ${CONFIRM_TIMEOUT_MS / 1000}s — `
                  + "JANGAN langsung mengulang, cek dulu di solscan.");
}

async function sendAll(conn, txs, kp, microLamports) {
  const list = Array.isArray(txs) ? txs : [txs];
  const out = [];
  for (const tx of list) {
    if (!tx) continue;
    out.push(await sendOne(conn, tx, [kp], microLamports));
  }
  return out;
}

/* `rebalancePosition` mengembalikan INSTRUKSI, bukan Transaction — beda dengan
 * jalur add/remove. Instruksi init bin array dipisah jadi tx sendiri: ia harus
 * sudah masuk chain sebelum instruksi rebalance-nya jalan, dan menggabungnya
 * bisa melewati batas ukuran tx. Kelompok kosong dilewati. */
function buildTxs(payer, groups) {
  const out = [];
  for (const ixs of groups) {
    if (!ixs || !ixs.length) continue;
    const tx = new web3.Transaction();
    tx.feePayer = payer;
    for (const ix of ixs) tx.add(ix);
    out.push(tx);
  }
  return out;
}

/* positionData SDK berisi BN + Decimal campur; JSON.stringify apa adanya
 * menghasilkan objek BN mentah yang tak terpakai di Python. */
function posOut(p, pool, binStep, dx, dy) {
  const d = p.positionData;
  return {
    address: p.publicKey.toBase58(),
    pool,
    bin_step: binStep,
    lower_bin: d.lowerBinId,
    upper_bin: d.upperBinId,
    // totalXAmount/totalYAmount SDK sudah string desimal RAW (lamport), bukan
    // jumlah manusia — pembagian desimalnya dilakukan di Python.
    amount_x_raw: s(d.totalXAmount),
    amount_y_raw: s(d.totalYAmount),
    fee_x_raw: s(d.feeX),
    fee_y_raw: s(d.feeY),
    reward_one_raw: s(d.rewardOne),
    reward_two_raw: s(d.rewardTwo),
    dec_x: dx,
    dec_y: dy,
    last_updated: s(d.lastUpdatedAt),
    bins: (d.positionBinData || []).map((b) => ({
      bin_id: b.binId,
      price: b.pricePerToken,
      x_raw: s(b.positionXAmount),
      y_raw: s(b.positionYAmount),
      liq: s(b.positionLiquidity),
    })),
  };
}

async function poolState(conn, poolAddr) {
  const pool = new PublicKey(poolAddr);
  const inst = await DLMM.create(conn, pool);
  const active = await inst.getActiveBin();
  const dx = inst.tokenX.mint.decimals;
  const dy = inst.tokenY.mint.decimals;
  return { inst, active, dx, dy, pool };
}

function poolOut(inst, active, dx, dy, poolAddr) {
  return {
    pool: poolAddr,
    bin_step: inst.lbPair.binStep,
    active_bin: active.binId,
    // pricePerToken sudah memperhitungkan selisih desimal kedua mint.
    price: Number(active.pricePerToken),
    mint_x: inst.tokenX.publicKey.toBase58(),
    mint_y: inst.tokenY.publicKey.toBase58(),
    dec_x: dx,
    dec_y: dy,
    reserve_x_raw: s(inst.tokenX.amount),
    reserve_y_raw: s(inst.tokenY.amount),
  };
}

// ---------- perintah ----------
const CMDS = {
  /* Dipakai `solana.py` untuk memastikan sidecar-nya benar-benar bisa jalan
   * SEBELUM ada tx — node ada, node_modules terpasang, SDK bisa di-require.
   * Versinya dibaca dari node_modules langsung: `require("@meteora-ag/dlmm/
   * package.json")` ditolak ("Package subpath './package.json' is not defined
   * by exports"). */
  async ping() {
    let ver = null;
    try {
      ver = JSON.parse(require("fs").readFileSync(
        require("path").join(__dirname, "node_modules/@meteora-ag/dlmm/package.json"),
        "utf8")).version;
    } catch (_) { /* versi itu informasi, bukan syarat */ }
    return { sdk: ver, node: process.version, program: dlmmPkg.LBCLMM_PROGRAM_IDS["mainnet-beta"] };
  },

  async pool(req, conn) {
    const { inst, active, dx, dy } = await poolState(conn, req.pool);
    const out = poolOut(inst, active, dx, dy, req.pool);
    const fee = inst.getFeeInfo();
    out.base_fee_pct = Number(fee.baseFeeRatePercentage);
    out.max_fee_pct = Number(fee.maxFeeRatePercentage);
    out.protocol_fee_pct = Number(fee.protocolFeePercentage);
    out.dynamic_fee_pct = Number(inst.getDynamicFee());
    return out;
  },

  /* Harga <-> bin id. Dipakai kartu range: user berpikir dalam harga/market cap,
   * program berpikir dalam bin id, dan pembulatannya harus sama persis dengan
   * yang dipakai saat deposit. */
  async bins(req, conn) {
    const { inst, active, dx, dy } = await poolState(conn, req.pool);
    const out = poolOut(inst, active, dx, dy, req.pool);
    out.query = (req.prices || []).map((p) => {
      const perLamport = inst.toPricePerLamport(Number(p));
      const id = DLMM.getBinIdFromPrice(perLamport, inst.lbPair.binStep, false);
      return { price: Number(p), bin_id: id, bin_price: Number(inst.fromPricePerLamport(
        dlmmPkg.getPriceOfBinByBinId(id, inst.lbPair.binStep).toString())) };
    });
    out.bin_prices = (req.bins || []).map((id) => ({
      bin_id: Number(id),
      price: Number(inst.fromPricePerLamport(
        dlmmPkg.getPriceOfBinByBinId(Number(id), inst.lbPair.binStep).toString())),
    }));
    return out;
  },

  /* Pool pemilik sebuah posisi. Dipakai dispatcher generik: `pid` bot cuma
   * membawa alamat posisi (`dlmm:<pubkey>`), sedangkan tiap aksi SDK butuh
   * pool-nya. Sengaja lewat `wrapPosition` dan bukan membaca offset byte
   * sendiri — layout Position, PositionV2, dan extended position berbeda, dan
   * offset yang ditebak akan mengembalikan pubkey yang salah tanpa gejala. */
  async position_pool(req, conn) {
    const key = new PublicKey(req.position);
    const info = await conn.getAccountInfo(key);
    if (!info) throw new Error("Akun posisi tidak ada di chain");
    const program = dlmmPkg.createProgram(conn);
    const p = dlmmPkg.wrapPosition(program, key, info);
    return {
      position: req.position,
      pool: p.lbPair().toBase58(),
      owner: p.owner().toBase58(),
      lower_bin: p.lowerBinId().toNumber(),
      upper_bin: p.upperBinId().toNumber(),
    };
  },

  /* Komposisi dua sisi untuk sebuah range + shape, TANPA mengirim tx.
   *
   * Dipakai kartu konfirmasi: user menyebut jumlah di SATU sisi (lazimnya
   * quote), dan sisi lawannya ditentukan geometri range terhadap bin aktif —
   * persis seperti `plan_two_sided` di jalur EVM. Rumusnya tidak ditulis ulang
   * di Python: `autoFillXByStrategy`/`autoFillYByStrategy` adalah fungsi yang
   * SAMA yang nanti menentukan berapa yang benar-benar tertarik saat deposit,
   * jadi menyalinnya berarti kartu dan eksekusi bisa berbeda. */
  async quote_add(req, conn) {
    const { inst, active, dx, dy } = await poolState(conn, req.pool);
    const lower = Number(req.lower_bin), upper = Number(req.upper_bin);
    const st = strategyOf(req.strategy);
    const binStep = inst.lbPair.binStep;
    const xInActive = bn(active.xAmount);
    const yInActive = bn(active.yAmount);
    const out = { ...poolOut(inst, active, dx, dy, req.pool),
                  lower_bin: lower, upper_bin: upper, n_bins: upper - lower + 1 };
    if (req.amount_x_raw !== undefined && req.amount_x_raw !== null) {
      out.amount_x_raw = s(bn(req.amount_x_raw));
      out.amount_y_raw = s(dlmmPkg.autoFillYByStrategy(
        active.binId, binStep, bn(req.amount_x_raw), xInActive, yInActive,
        lower, upper, st));
    } else {
      out.amount_y_raw = s(bn(req.amount_y_raw));
      out.amount_x_raw = s(dlmmPkg.autoFillXByStrategy(
        active.binId, binStep, bn(req.amount_y_raw), xInActive, yInActive,
        lower, upper, st));
    }
    // Range yang seluruhnya di ATAS bin aktif cuma menampung X, yang seluruhnya
    // di BAWAH cuma menampung Y. UI wajib menyebutnya: itu yang menentukan token
    // mana yang harus dipegang user sebelum menekan tombol.
    out.side = lower > active.binId ? "x_only"
             : upper < active.binId ? "y_only" : "both";
    // Sisi yang tidak tertampung DINOLKAN di sini, bukan dibiarkan apa adanya.
    // autoFill mengembalikan 0 untuk sisi yang dihitungnya, tapi jumlah yang
    // DIKIRIM pemanggil untuk sisi yang salah tetap lewat — dan kartu lalu
    // menjanjikan "100 USDC masuk" untuk range yang tidak bisa menerima USDC
    // sama sekali.
    if (out.side === "x_only" && bn(out.amount_y_raw).gtn(0)) {
      out.unusable_y_raw = out.amount_y_raw;
      out.amount_y_raw = "0";
    }
    if (out.side === "y_only" && bn(out.amount_x_raw).gtn(0)) {
      out.unusable_x_raw = out.amount_x_raw;
      out.amount_x_raw = "0";
    }
    return out;
  },

  /* Beberapa posisi yang alamatnya SUDAH diketahui, dibaca satu per satu.
   *
   * `getPositionsByUserAndLbPair` menyapu `getProgramAccounts` dan Alchemy
   * menolaknya 429 ("exceeded its compute units per second") bahkan untuk satu
   * wallet di satu pool — terukur gagal di KEEMPAT key yang sehat. `getPosition`
   * membaca akun yang ditunjuk saja, jadi ongkosnya tetap walau wallet-nya
   * punya ratusan posisi. Alamatnya datang dari indeks Data API. */
  async positions_by_key(req, conn) {
    const { inst, active, dx, dy } = await poolState(conn, req.pool);
    const out = [];
    for (const key of req.positions || []) {
      const p = await inst.getPosition(new PublicKey(String(key)));
      out.push(posOut(p, req.pool, inst.lbPair.binStep, dx, dy));
    }
    return { active_bin: active.binId,
             pool: poolOut(inst, active, dx, dy, req.pool), positions: out };
  },

  async positions(req, conn) {
    const owner = new PublicKey(req.owner);
    if (req.pool) {
      const { inst, active, dx, dy } = await poolState(conn, req.pool);
      const r = await inst.getPositionsByUserAndLbPair(owner);
      return {
        active_bin: active.binId,
        pool: poolOut(inst, active, dx, dy, req.pool),
        positions: (r.userPositions || []).map((p) =>
          posOut(p, req.pool, inst.lbPair.binStep, dx, dy)),
      };
    }
    // Tanpa `pool`: sapu SEMUA pool. Satu panggilan RPC berat, jadi dipakai
    // hanya oleh /list dan /recover, bukan jalur klik per-posisi.
    const all = await DLMM.getAllLbPairPositionsByUser(conn, owner);
    const out = [];
    for (const [addr, v] of all.entries()) {
      const dx = v.tokenX.mint.decimals, dy = v.tokenY.mint.decimals;
      for (const p of v.lbPairPositionsData || []) {
        const o = posOut(p, addr, v.lbPair.binStep, dx, dy);
        o.mint_x = v.tokenX.publicKey.toBase58();
        o.mint_y = v.tokenY.publicKey.toBase58();
        o.active_bin = v.lbPair.activeId;
        out.push(o);
      }
    }
    return { positions: out };
  },

  async swap_quote(req, conn) {
    const { inst } = await poolState(conn, req.pool);
    const swapForY = !!req.swap_for_y;
    const binArrays = await inst.getBinArrayForSwap(swapForY);
    const q = inst.swapQuote(bn(req.amount_in_raw), swapForY,
      bn(req.slippage_bps || 100), binArrays, !!req.is_partial_fill);
    return {
      amount_out_raw: s(q.outAmount),
      min_amount_out_raw: s(q.minOutAmount),
      fee_raw: s(q.fee),
      price_impact: Number(q.priceImpact),
      amount_in_raw: s(q.consumedInAmount),
      end_bin: q.endPrice ? undefined : undefined,
    };
  },

  async swap(req, conn, kp) {
    const { inst } = await poolState(conn, req.pool);
    const swapForY = !!req.swap_for_y;
    const binArrays = await inst.getBinArrayForSwap(swapForY);
    const q = inst.swapQuote(bn(req.amount_in_raw), swapForY,
      bn(req.slippage_bps || 100), binArrays, false);
    const tx = await inst.swap({
      inToken: swapForY ? inst.tokenX.publicKey : inst.tokenY.publicKey,
      outToken: swapForY ? inst.tokenY.publicKey : inst.tokenX.publicKey,
      inAmount: bn(req.amount_in_raw),
      minOutAmount: q.minOutAmount,
      lbPair: inst.pubkey,
      user: kp.publicKey,
      binArraysPubkey: q.binArraysPubkey,
    });
    const sigs = await sendAll(conn, tx, kp, req.priority_micro_lamports);
    return {
      signatures: sigs,
      amount_out_raw: s(q.outAmount),
      min_amount_out_raw: s(q.minOutAmount),
      fee_raw: s(q.fee),
      price_impact: Number(q.priceImpact),
    };
  },

  /* Buat posisi BARU + setor. Satu tx kalau rangenya muat; SDK memecah sendiri
   * kalau lebih lebar dari MAX_BIN_LENGTH_ALLOWED_IN_ONE_TX (26 bin). */
  async add_new(req, conn, kp) {
    const { inst, active } = await poolState(conn, req.pool);
    const posKp = Keypair.generate();
    const lower = Number(req.lower_bin), upper = Number(req.upper_bin);
    const txs = await inst.initializePositionAndAddLiquidityByStrategy({
      positionPubKey: posKp.publicKey,
      user: kp.publicKey,
      totalXAmount: bn(req.amount_x_raw),
      totalYAmount: bn(req.amount_y_raw),
      strategy: { maxBinId: upper, minBinId: lower, strategyType: strategyOf(req.strategy) },
      slippage: req.slippage_pct === undefined ? undefined : Number(req.slippage_pct),
    });
    const list = Array.isArray(txs) ? txs : [txs];
    const sigs = [];
    for (const tx of list) {
      sigs.push(await sendOne(conn, tx, [kp, posKp],
                              req.priority_micro_lamports));
    }
    return { signatures: sigs, position: posKp.publicKey.toBase58(),
             active_bin: active.binId, lower_bin: lower, upper_bin: upper };
  },

  async add_existing(req, conn, kp) {
    const { inst } = await poolState(conn, req.pool);
    const txs = await inst.addLiquidityByStrategy({
      positionPubKey: new PublicKey(req.position),
      user: kp.publicKey,
      totalXAmount: bn(req.amount_x_raw),
      totalYAmount: bn(req.amount_y_raw),
      strategy: {
        maxBinId: Number(req.upper_bin), minBinId: Number(req.lower_bin),
        strategyType: strategyOf(req.strategy),
      },
      slippage: req.slippage_pct === undefined ? undefined : Number(req.slippage_pct),
    });
    return { signatures: await sendAll(conn, txs, kp, req.priority_micro_lamports),
             position: req.position };
  },

  /* bps_to_remove 10000 = 100%. `shouldClaimAndClose` menutup akun posisi dan
   * MENGEMBALIKAN sewa rent-nya — kalau tidak, SOL-nya terkunci di akun kosong. */
  async remove(req, conn, kp) {
    const { inst } = await poolState(conn, req.pool);
    const owner = kp.publicKey;
    // `getPosition` membaca akun yang ditunjuk saja. Jangan diganti
    // `getPositionsByUserAndLbPair`: itu memindai seluruh akun program DLMM dan
    // Alchemy menolaknya 429 — terukur menggagalkan rebalance SEBELUM satu pun
    // tx sempat jalan.
    const p = await inst.getPosition(new PublicKey(String(req.position)));
    if (p.positionData.owner && p.positionData.owner.toBase58
        && p.positionData.owner.toBase58() !== owner.toBase58()) {
      throw new Error("Posisi bukan milik wallet ini");
    }
    const d = p.positionData;
    const bps = Number(req.bps_to_remove || 10000);
    const txs = await inst.removeLiquidity({
      position: p.publicKey,
      user: owner,
      fromBinId: d.lowerBinId,
      toBinId: d.upperBinId,
      bps: bn(bps),
      shouldClaimAndClose: bps >= 10000 ? req.close !== false : false,
    });
    return {
      signatures: await sendAll(conn, txs, kp, req.priority_micro_lamports),
      removed_bps: bps,
      closed: bps >= 10000 && req.close !== false,
      before: posOut(p, req.pool, inst.lbPair.binStep,
                     inst.tokenX.mint.decimals, inst.tokenY.mint.decimals),
    };
  },

  async claim(req, conn, kp) {
    const { inst } = await poolState(conn, req.pool);
    let list;
    if (req.position) {
      list = [await inst.getPosition(new PublicKey(String(req.position)))];
    } else {
      const r = await inst.getPositionsByUserAndLbPair(kp.publicKey);
      list = r.userPositions || [];
    }
    if (!list.length) throw new Error("Tidak ada posisi untuk diklaim");
    const txs = await inst.claimAllSwapFee({ owner: kp.publicKey, positions: list });
    return {
      signatures: await sendAll(conn, txs, kp, req.priority_micro_lamports),
      claimed: list.map((p) => posOut(p, req.pool, inst.lbPair.binStep,
        inst.tokenX.mint.decimals, inst.tokenY.mint.decimals)),
    };
  },

  /* ── Compound: klaim fee lalu setor kembali ke RANGE YANG SAMA ────────────
   *
   * Beda dari `rebalance`: rangenya TIDAK digeser. Deposit dinyatakan sebagai
   * delta terhadap bin aktif (`lowerBinId - activeId` … `upperBinId - activeId`),
   * jadi bin batasnya persis sama dengan sebelumnya.
   *
   * Fee DLMM tidak mengendap di posisi seperti v3 dan tidak bisa dikreditkan
   * terhadap tagihan seperti v4 — ia DIKLAIM (`shouldClaimFee: true`) lalu
   * dipakai sebagai jumlah setoran dalam simulasi yang sama, sehingga tidak ada
   * jeda di mana dana itu menganggur di wallet. */
  async compound(req, conn, kp) {
    const { inst } = await poolState(conn, req.pool);
    const owner = kp ? kp.publicKey : new PublicKey(req.owner);
    const key = new PublicKey(String(req.position));
    const p = await inst.getPosition(key);
    const d = p.positionData;
    const dx = inst.tokenX.mint.decimals, dy = inst.tokenY.mint.decimals;
    const before = posOut(p, req.pool, inst.lbPair.binStep, dx, dy);
    const feeX = bn(d.feeX), feeY = bn(d.feeY);
    if (feeX.isZero() && feeY.isZero()) {
      throw new Error("Tidak ada fee yang bisa di-compound (kedua sisi 0)");
    }
    const activeId = inst.lbPair.activeId;
    const minDeltaId = bn(d.lowerBinId - activeId);
    const maxDeltaId = bn(d.upperBinId - activeId);
    const sp = dlmmPkg.buildLiquidityStrategyParameters(
      feeX, feeY, minDeltaId, maxDeltaId, bn(inst.lbPair.binStep), false,
      bn(activeId), dlmmPkg.getLiquidityStrategyParameterBuilder(strategyOf(req.strategy)));
    const rp = await dlmmPkg.RebalancePosition.create({
      program: inst.program, positionAddress: key, positionData: d,
      shouldClaimFee: true, shouldClaimReward: false, pairAddress: inst.pubkey,
    });
    const sim = await inst.simulateRebalancePositionWithStrategy(rp, {
      buildRebalanceStrategyParameters: () => ({
        // withdraws kosong = pokok TIDAK disentuh; yang masuk cuma fee.
        withdraws: [],
        deposits: [{ minDeltaId, maxDeltaId, x0: sp.x0, y0: sp.y0,
                     deltaX: sp.deltaX, deltaY: sp.deltaY,
                     favorXInActiveBin: false }],
      }),
    });
    const r = sim.simulationResult;
    const out = {
      position: req.position, before,
      lower_bin: d.lowerBinId, upper_bin: d.upperBinId, active_bin: activeId,
      fee_x_raw: s(feeX), fee_y_raw: s(feeY),
      in_position_x_raw: s(r.amountXDeposited),
      in_position_y_raw: s(r.amountYDeposited),
      from_wallet_x_raw: s(r.actualAmountXDeposited),
      from_wallet_y_raw: s(r.actualAmountYDeposited),
      to_wallet_x_raw: s(r.actualAmountXWithdrawn),
      to_wallet_y_raw: s(r.actualAmountYWithdrawn),
      rental_lamports: s(r.rentalCostLamports),
      dec_x: dx, dec_y: dy,
    };
    if (req.dry) return out;
    const ixs = await inst.rebalancePosition(
      sim, bn(req.max_active_bin_slippage || dlmmPkg.MAX_ACTIVE_BIN_SLIPPAGE),
      owner, req.slippage_pct === undefined ? undefined : Number(req.slippage_pct));
    out.signatures = await sendAll(
      conn, buildTxs(owner, [ixs.initBinArrayInstructions,
                             ixs.rebalancePositionInstruction]),
      kp, req.priority_micro_lamports);
    return out;
  },

  async close(req, conn, kp) {
    const { inst } = await poolState(conn, req.pool);
    const p = await inst.getPosition(new PublicKey(String(req.position)));
    const before = posOut(p, req.pool, inst.lbPair.binStep,
                          inst.tokenX.mint.decimals, inst.tokenY.mint.decimals);
    const txs = await inst.removeLiquidity({
      position: p.publicKey, user: kp.publicKey,
      fromBinId: p.positionData.lowerBinId, toBinId: p.positionData.upperBinId,
      bps: bn(10000), shouldClaimAndClose: true,
    });
    return { signatures: await sendAll(conn, txs, kp, req.priority_micro_lamports),
             before };
  },
};

// ---------- main ----------
(async () => {
  let req = {};
  try {
    const chunks = [];
    for await (const c of process.stdin) chunks.push(c);
    req = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    const fn = CMDS[req.cmd];
    if (!fn) throw new Error(`cmd tidak dikenal: ${req.cmd}`);
    // Keypair hanya dibuat untuk perintah yang memang menandatangani.
    const kp = req.secret ? keypairFrom(req.secret) : null;
    // Beberapa endpoint, dicoba berurutan. Perintah baca DLMM memakai
    // getProgramAccounts (memindai seluruh akun program), dan Alchemy
    // menjawabnya 429 "exceeded its compute units per second" bahkan untuk satu
    // wallet — jadi satu RPC saja membuat /list gagal total, bukan sekadar
    // lambat. Perintah yang MENANDATANGANI tidak diulang ke endpoint lain:
    // mengirim ulang tx yang dibangun ulang bisa menyetor dua kali.
    const urls = (req.rpcs && req.rpcs.length ? req.rpcs : [req.rpc]).filter(Boolean);
    let out, lastErr;
    for (const url of urls) {
      const conn = new Connection(url, { commitment: "confirmed" });
      try {
        out = await fn(req, conn, kp);
        lastErr = null;
        break;
      } catch (e) {
        lastErr = e;
        const m = String((e && e.message) || e);
        const retryable = m.includes("429") || m.includes("Too Many Requests")
          || m.includes("capacity") || m.includes("not enabled")
          || m.includes("fetch failed") || m.includes("ETIMEDOUT");
        // Perintah bertanda tangan BOLEH diulang di endpoint lain selama belum
        // ada satu pun tx yang disiarkan — kegagalannya di situ selalu pembacaan
        // (DLMM.create / getPosition), dan 429 di pembacaan itulah yang paling
        // sering terjadi. Begitu ada tx terkirim, mengulang bisa menyetor dua
        // kali, jadi berhenti apa pun sebabnya.
        if (SENT > 0 || !retryable) break;
      }
    }
    if (lastErr) throw lastErr;
    process.stdout.write(JSON.stringify({ ok: true, ...out }));
  } catch (e) {
    const msg = (e && (e.message || e.toString())) || "error tanpa pesan";
    // Log program Solana ada di e.logs dan itu SATU-SATUNYA yang menyebut error
    // Anchor sebenarnya — tanpa ini user cuma melihat "Transaction failed".
    const logs = (e && e.logs) || (e && e.transactionLogs) || null;
    process.stdout.write(JSON.stringify({
      ok: false, error: msg, logs: logs ? logs.slice(-12) : null,
    }));
    process.exitCode = 1;
  }
})();
