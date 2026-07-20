"""
web.py — UI web unipool: chart real-time + atur range min/max lewat drag di chart.

Jalankan:  python3 web.py          → http://127.0.0.1:8899
Env (.env, sama dengan bot.py): PRIVATE_KEY, [RPC_4663, RPC_56]
Env tambahan opsional:
    WEB_HOST   default 127.0.0.1  — JANGAN dibuka ke publik tanpa WEB_TOKEN
    WEB_PORT   default 8899
    WEB_TOKEN  password akses; wajib kalau WEB_HOST bukan localhost

Semua aksi on-chain memakai fungsi yang sama persis dengan bot Telegram
(chain.py), termasuk perhitungan range — chart cuma mengirim persen lebar
range, tick finalnya tetap dihitung calc_strategy_range di server.
"""
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv
from web3 import Web3

load_dotenv(Path(__file__).parent / ".env")

import chain as ch          # noqa: E402
import store                # noqa: E402
import bot                  # noqa: E402  (dipakai untuk key wallet + compute_amount)

BASE = Path(__file__).parent
STATIC = BASE / "static"
TX_LOCK = threading.Lock()   # serialisasi tx (nonce) — sama seperti bot

# ABI read-only tambahan untuk peta likuiditas (tidak ada di chain.py)
POOL_TICKS_ABI = [
    {"inputs": [{"name": "tick", "type": "int24"}], "name": "ticks", "outputs": [
        {"name": "liquidityGross", "type": "uint128"}, {"name": "liquidityNet", "type": "int128"},
        {"name": "feeGrowthOutside0X128", "type": "uint256"}, {"name": "feeGrowthOutside1X128", "type": "uint256"},
        {"name": "tickCumulativeOutside", "type": "int56"}, {"name": "secondsPerLiquidityOutsideX128", "type": "uint160"},
        {"name": "secondsOutside", "type": "uint32"}, {"name": "initialized", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "wordPosition", "type": "int16"}], "name": "tickBitmap",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]   # liquidity() sudah ada di ch.POOL_ABI — jangan diulang (selector bentrok)
STATEVIEW_TICKS_ABI = [
    {"inputs": [{"name": "poolId", "type": "bytes32"}, {"name": "tick", "type": "int16"}],
     "name": "getTickBitmap", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "poolId", "type": "bytes32"}, {"name": "tick", "type": "int24"}],
     "name": "getTickLiquidity", "outputs": [
        {"name": "liquidityGross", "type": "uint128"}, {"name": "liquidityNet", "type": "int128"}],
     "stateMutability": "view", "type": "function"},
]

# Pool hasil discovery disimpan di server; klien cuma memegang key-nya.
# Data untuk membangun transaksi TIDAK BOLEH datang dari browser.
_POOLS: dict[str, dict] = {}
_TOKENS: dict[str, dict] = {}


def pool_key(chain_id: int, p: dict) -> str:
    return f"{chain_id}:{p.get('ver', 3)}:{str(p['pool']).lower()}:{p['fee']}"


# ──────────────────────────── helper harga/chart ────────────────────────────
def meme_addr(p: dict) -> str:
    return p["token0"] if p["quote_is_token1"] else p["token1"]


def meme_price_at(p: dict, tdec: int, tick: int) -> float:
    """Harga meme (dalam quote) pada tick tertentu."""
    raw = ch.tick_to_price(tick)
    if p["quote_is_token1"]:
        return raw * 10 ** (tdec - p["quote_decimals"])
    return (1 / raw if raw else 0) * 10 ** (tdec - p["quote_decimals"])


def cur_tick_of(chain_id: int, p: dict) -> int:
    w3 = ch.get_w3(chain_id)
    ver = p.get("ver", 3)
    if ver == 4:
        return ch.v4_slot0(w3, chain_id, bytes.fromhex(str(p["pool"]).removeprefix("0x")))[1]
    if ver == 2:
        rq, rm = ch._v2_pair_reserves(w3, p["pool"], p["quote_addr"])
        raw = (rq / rm) if rm else 0
        # tick semu buat referensi harga saja
        return ch.price_to_tick(raw if p["quote_is_token1"] else (1 / raw if raw else 1))
    return w3.eth.contract(address=Web3.to_checksum_address(p["pool"]),
                           abi=ch.POOL_ABI).functions.slot0().call()[1]


GT_TF = {  # timeframe UI → (path GeckoTerminal, aggregate, detik per candle)
    "1m": ("minute", 1, 60), "5m": ("minute", 5, 300), "15m": ("minute", 15, 900),
    "1h": ("hour", 1, 3600), "4h": ("hour", 4, 14400), "1d": ("day", 1, 86400),
}


def gecko_candles(chain_id: int, pool_addr: str, tf: str, limit: int = 300) -> list[dict]:
    """OHLCV dari GeckoTerminal (gratis, tanpa API key). Harga dalam token lawan."""
    slug = ch.CHAINS[chain_id].get("gecko")
    if not slug:
        return []
    path, agg, _ = GT_TF.get(tf, GT_TF["15m"])
    r = requests.get(
        f"https://api.geckoterminal.com/api/v2/networks/{slug}/pools/{pool_addr}/ohlcv/{path}",
        params={"aggregate": agg, "limit": min(limit, 1000), "currency": "token"},
        timeout=20, headers={"Accept": "application/json"})
    if r.status_code != 200:
        return []
    rows = r.json().get("data", {}).get("attributes", {}).get("ohlcv_list", []) or []
    out = []
    for ts, o, h, l, c, v in rows:
        try:
            out.append({"time": int(ts), "open": float(o), "high": float(h),
                        "low": float(l), "close": float(c), "volume": float(v)})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["time"])
    return out


def onchain_candles(chain_id: int, p: dict, tdec: int, tf: str, points: int = 96) -> list[dict]:
    """Cadangan kalau GeckoTerminal belum meng-index pool: sampling slot0 di blok
    lampau (butuh RPC archive). Tiap titik jadi satu candle datar."""
    if p.get("ver", 3) != 3:
        return []
    span = GT_TF.get(tf, GT_TF["15m"])[2] * points
    hist = ch.price_history(chain_id, p["pool"], span, points)
    out = []
    for ts, tick in hist:
        v = meme_price_at(p, tdec, tick)
        out.append({"time": int(ts), "open": v, "high": v, "low": v, "close": v, "volume": 0})
    return out


def orient_candles(cands: list[dict], now_price: float) -> list[dict]:
    """GeckoTerminal bisa memberi harga base→quote atau kebalikannya. Pilih
    orientasi yang cocok dengan harga on-chain sekarang (pembanding rasio log)."""
    if not cands or now_price <= 0:
        return cands
    last = cands[-1]["close"]
    if last <= 0:
        return cands
    if abs(last / now_price - 1) <= abs((1 / last) / now_price - 1):
        return cands
    inv = []
    for c in cands:
        if min(c["open"], c["high"], c["low"], c["close"]) <= 0:
            continue
        inv.append({"time": c["time"], "open": 1 / c["open"], "high": 1 / c["low"],
                    "low": 1 / c["high"], "close": 1 / c["close"], "volume": c["volume"]})
    return inv


def liquidity_profile(chain_id: int, p: dict, tdec: int, span_ticks: int = 12000) -> list[dict]:
    """Distribusi likuiditas di sekitar harga (histogram ala UI Uniswap).
    Cari tick ter-inisialisasi lewat tickBitmap, lalu akumulasi liquidityNet."""
    ver = p.get("ver", 3)
    if ver == 2:
        return []
    w3 = ch.get_w3(chain_id)
    sp = int(p.get("tick_spacing") or ch.TICK_SPACING.get(p["fee"], 60))
    cur = cur_tick_of(chain_id, p)

    if ver == 4:
        cfg = ch.CHAINS[chain_id]
        sv = w3.eth.contract(address=Web3.to_checksum_address(cfg["v4_stateview"]),
                             abi=ch.V4_STATEVIEW_ABI + STATEVIEW_TICKS_ABI)
        pid = bytes.fromhex(str(p["pool"]).removeprefix("0x"))
        bitmap = lambda w: sv.functions.getTickBitmap(pid, w).call()
        tick_net = lambda t: sv.functions.getTickLiquidity(pid, t).call()[1]
        active = sv.functions.getLiquidity(pid).call()
    else:
        pool = w3.eth.contract(address=Web3.to_checksum_address(p["pool"]),
                               abi=ch.POOL_ABI + POOL_TICKS_ABI)
        bitmap = lambda w: pool.functions.tickBitmap(w).call()
        tick_net = lambda t: pool.functions.ticks(t).call()[1]
        active = pool.functions.liquidity().call()

    lo_t = max(ch.MIN_TICK, cur - span_ticks)
    hi_t = min(ch.MAX_TICK, cur + span_ticks)
    w_lo, w_hi = (lo_t // sp) >> 8, (hi_t // sp) >> 8
    if w_hi - w_lo > 40:            # jaga jumlah RPC call tetap wajar
        w_hi = w_lo + 40

    from concurrent.futures import ThreadPoolExecutor
    words = list(range(w_lo, w_hi + 1))
    with ThreadPoolExecutor(max_workers=5) as ex:
        maps = list(ex.map(lambda w: (w, bitmap(w)), words))
    init_ticks = []
    for w, bm in maps:
        if not bm:
            continue
        for bit in range(256):
            if bm >> bit & 1:
                t = ((w << 8) + bit) * sp
                if lo_t <= t <= hi_t:
                    init_ticks.append(t)
    if not init_ticks:
        return []
    init_ticks.sort()
    with ThreadPoolExecutor(max_workers=5) as ex:
        nets = dict(zip(init_ticks, ex.map(tick_net, init_ticks)))

    # bangun bar: dari tick aktif jalan ke kanan (tambah net) dan ke kiri (kurangi net)
    bars, liq = [], active
    right = [t for t in init_ticks if t > cur]
    prev = ch.round_down(cur, sp)
    for t in right:
        if liq > 0:
            bars.append((prev, t, liq))
        liq += nets[t]
        prev = t
    liq = active
    left = [t for t in reversed(init_ticks) if t <= cur]
    prev = ch.round_down(cur, sp) + sp
    for t in left:
        if liq > 0:
            bars.append((t, prev, liq))
        liq -= nets[t]
        prev = t

    out = []
    for a, b, l in bars:
        pa, pb = sorted([meme_price_at(p, tdec, a), meme_price_at(p, tdec, b)])
        if pa > 0:
            out.append({"p0": pa, "p1": pb, "liq": float(l)})
    out.sort(key=lambda x: x["p0"])
    return out[:400]


# ──────────────────────────── endpoint API ────────────────────────────
def api_state(_q, _b) -> dict:
    s = store.load_settings()
    cid = int(s.get("chain", 4663))
    pks = bot.all_pks()
    idx = bot.active_wallet_idx()
    addr = bot._addr_of(pks[idx])
    w3 = ch.get_w3(cid)
    cfg = ch.CHAINS[cid]
    native = w3.eth.get_balance(addr) / 1e18
    try:
        wrapped = ch.erc20(w3, cfg["wrapped"]).functions.balanceOf(addr).call() / 1e18
    except Exception:
        wrapped = 0.0
    return {
        "chain": cid, "chain_name": cfg["name"], "explorer": cfg["explorer"],
        "wallets": [{"idx": i, "label": f"W{i + 1}", "address": bot._addr_of(k)}
                    for i, k in enumerate(pks)],
        "wallet_idx": idx, "address": addr,
        "native": native, "native_sym": cfg["native_symbol"],
        "wrapped_sym": cfg["wrapped_symbol"], "wrapped": wrapped,
        "settings": s,
        "chains": [{"id": c, "name": v["name"]} for c, v in ch.CHAINS.items()],
    }


def api_settings(_q, b) -> dict:
    s = store.load_settings()
    for k in ("chain", "slippage_pct", "amount_pct", "gap", "wallet_idx", "autoswap"):
        if k in b:
            s[k] = b[k]
    store.save_settings(s)
    return api_state(None, None)


def api_discover(_q, b) -> dict:
    cid = int(b["chain"])
    token = Web3.to_checksum_address(str(b["token"]).strip())
    res = ch.discover_pools(cid, token)
    if not res["pools"]:
        raise RuntimeError("Tidak ada pool Uniswap (v2/v3/v4) untuk token ini.")
    _TOKENS[f"{cid}:{token.lower()}"] = res["token"]
    out = []
    for p in res["pools"][:12]:
        k = pool_key(cid, p)
        _POOLS[k] = p
        out.append({
            "key": k, "ver": p.get("ver", 3), "pool": str(p["pool"]),
            "fee": p["fee"], "fee_pct": p["fee"] / 10000,
            "quote_sym": p["quote_sym"], "tvl_usd": p["tvl_usd"],
            "vol24_usd": p.get("vol24_usd"), "apr_pct": p.get("apr_pct"),
            "quote_usd": p["quote_usd"],
        })
    return {"token": res["token"], "pools": out}


def _pool(b) -> tuple[int, dict, dict]:
    k = str(b["key"])
    p = _POOLS.get(k)
    if not p:
        raise RuntimeError("Pool tidak dikenal — cari ulang tokennya.")
    cid = int(k.split(":")[0])
    tk = _TOKENS.get(f"{cid}:{meme_addr(p).lower()}")
    if not tk:
        tk = ch.token_info(ch.get_w3(cid), meme_addr(p))
    return cid, p, tk


def api_pool(_q, b) -> dict:
    cid, p, tk = _pool(b)
    cur = cur_tick_of(cid, p)
    price = meme_price_at(p, tk["decimals"], cur)
    try:
        supply = ch.token_supply(ch.get_w3(cid), meme_addr(p))
    except Exception:
        supply = 0
    return {
        "key": b["key"], "ver": p.get("ver", 3), "pool": str(p["pool"]),
        "fee": p["fee"], "fee_pct": p["fee"] / 10000,
        "tick": cur, "tick_spacing": int(p.get("tick_spacing") or ch.TICK_SPACING.get(p["fee"], 60)),
        "price": price, "price_usd": price * p["quote_usd"],
        "quote_sym": p["quote_sym"], "quote_usd": p["quote_usd"],
        "token_sym": tk["symbol"], "token_dec": tk["decimals"], "token_addr": meme_addr(p),
        "tvl_usd": p["tvl_usd"], "vol24_usd": p.get("vol24_usd"), "apr_pct": p.get("apr_pct"),
        "mc_usd": price * p["quote_usd"] * supply if supply else None,
        "supply": supply,
    }


def api_candles(q, _b) -> dict:
    k = q.get("key", [""])[0]
    tf = q.get("tf", ["15m"])[0]
    p = _POOLS.get(k)
    if not p:
        raise RuntimeError("Pool tidak dikenal.")
    cid = int(k.split(":")[0])
    tk = _TOKENS.get(f"{cid}:{meme_addr(p).lower()}") or ch.token_info(ch.get_w3(cid), meme_addr(p))
    cur = cur_tick_of(cid, p)
    now = meme_price_at(p, tk["decimals"], cur)
    src = "geckoterminal"
    cands = []
    try:
        cands = orient_candles(gecko_candles(cid, str(p["pool"]), tf), now)
    except Exception:
        cands = []
    if len(cands) < 3:
        try:
            cands = onchain_candles(cid, p, tk["decimals"], tf)
            src = "onchain"
        except Exception:
            cands = []
            src = "none"
    try:
        supply = ch.token_supply(ch.get_w3(cid), meme_addr(p))
    except Exception:
        supply = 0
    # volume GeckoTerminal (currency=token) satuannya token quote → ubah ke USD
    return {"candles": cands, "price": now, "tick": cur, "source": src,
            "quote_usd": p["quote_usd"], "supply": supply}


def api_liquidity(_q, b) -> dict:
    cid, p, tk = _pool(b)
    try:
        bars = liquidity_profile(cid, p, tk["decimals"])
    except Exception:
        bars = []
    return {"bars": bars, "price": meme_price_at(p, tk["decimals"], cur_tick_of(cid, p))}


def _budget(cid: int, p: dict, tk: dict, mode: str, b: dict) -> float:
    ctx = {"chain": cid, "pool_info": p, "token": tk, "mode": mode,
           "amount_pct": float(b.get("amount_pct") or 0),
           "amount_fixed": b.get("amount_fixed") or None}
    return bot.compute_amount(ctx)


def _strategy(b, p, tk, cur, sp) -> tuple[dict, int, int, str]:
    """Strategi + (tick_lower, tick_upper, mode efektif).
    Kalau klien mengirim price_lower/price_upper, range dipakai apa adanya —
    letaknya bebas, termasuk seluruhnya di bawah/atas harga sekarang. Mode
    (sisi token yang disetor) diturunkan dari letak range, bukan dipilih user."""
    q1 = p["quote_is_token1"]
    if b.get("price_lower") and b.get("price_upper"):
        lo, hi = ch.ticks_from_prices(float(b["price_lower"]), float(b["price_upper"]),
                                      p["fee"], q1, tk["decimals"], p["quote_decimals"], sp)
        return {"mode": "custom", "ticks": (lo, hi)}, lo, hi, ch.effective_mode(lo, hi, cur, q1)
    mode = str(b.get("mode", "lower"))
    st = {"mode": mode,
          "low_pct": max(0.01, min(float(b.get("low_pct", 20)), 99.0)),
          "up_pct": max(0.01, float(b.get("up_pct", 20))),
          "gap": int(b.get("gap", store.load_settings().get("gap", 1)))}
    lo, hi = ch.calc_strategy_range(cur, p["fee"], q1, mode, st["low_pct"], st["up_pct"],
                                    st["gap"], spacing=sp)
    return st, lo, hi, mode


def api_preview(_q, b) -> dict:
    """Range final + komposisi deposit. Tick selalu dihitung server (dibulatkan
    ke tick spacing) — klien cuma mengirim batas yang diinginkan."""
    cid, p, tk = _pool(b)
    ver = p.get("ver", 3)
    tdec = tk["decimals"]
    cur = cur_tick_of(cid, p)
    now = meme_price_at(p, tdec, cur)

    if ver == 2:
        amount = _budget(cid, p, tk, "lower", b)
        return {"ver": 2, "price": now, "amount": amount, "dep_sym": p["quote_sym"],
                "usd": amount * p["quote_usd"], "full_range": True}

    sp = int(p.get("tick_spacing") or ch.TICK_SPACING.get(p["fee"], 60))
    _st, lo, hi, mode = _strategy(b, p, tk, cur, sp)
    amount = _budget(cid, p, tk, mode, b)
    dep_sym = tk["symbol"] if mode == "upper" else p["quote_sym"]
    p_lo, p_hi = sorted([meme_price_at(p, tdec, lo), meme_price_at(p, tdec, hi)])
    usd = amount * (p["quote_usd"] if mode != "upper" else now * p["quote_usd"])

    comp = None
    if mode in ("wide", "stable"):
        try:
            w3 = ch.get_w3(cid)
            sqrtp = (ch.v4_slot0(w3, cid, bytes.fromhex(str(p["pool"]).removeprefix("0x")))[0]
                     if ver == 4 else
                     w3.eth.contract(address=Web3.to_checksum_address(p["pool"]),
                                     abi=ch.POOL_ABI).functions.slot0().call()[0])
            bw = int(amount * 10 ** p["quote_decimals"])
            keep, swap = ch.plan_two_sided(sqrtp, lo, hi, bw, p["quote_is_token1"])
            comp = {"quote": keep / 10 ** p["quote_decimals"],
                    "swap": swap / 10 ** p["quote_decimals"]}
        except Exception:
            comp = None

    return {
        "ver": ver, "mode": mode, "price": now,
        "tick_lower": lo, "tick_upper": hi, "cur_tick": cur, "tick_spacing": sp,
        "price_lower": p_lo, "price_upper": p_hi,
        "pct_lower": (p_lo / now - 1) * 100 if now else 0,
        "pct_upper": (p_hi / now - 1) * 100 if now else 0,
        "in_range": p_lo <= now <= p_hi,
        "amount": amount, "dep_sym": dep_sym, "usd": usd, "comp": comp,
        "custom": bool(b.get("price_lower") and b.get("price_upper")),
        "mc_lower": p_lo * p["quote_usd"] * b.get("supply", 0) if b.get("supply") else None,
        "mc_upper": p_hi * p["quote_usd"] * b.get("supply", 0) if b.get("supply") else None,
    }


def api_mint(_q, b) -> dict:
    cid, p, tk = _pool(b)
    ver = p.get("ver", 3)
    s = store.load_settings()
    if ver == 2:
        strategy, mode = {"mode": "lower"}, "lower"
    else:
        sp = int(p.get("tick_spacing") or ch.TICK_SPACING.get(p["fee"], 60))
        strategy, _lo, _hi, mode = _strategy(b, p, tk, cur_tick_of(cid, p), sp)
    slip = float(b.get("slippage_pct", s["slippage_pct"]))
    amount = _budget(cid, p, tk, mode, b)
    if amount <= 0:
        raise RuntimeError(f"Saldo {tk['symbol'] if mode == 'upper' else p['quote_sym']} kosong.")
    key = bot.pk()
    addr = bot._addr_of(key)

    with TX_LOCK:
        if ver == 2:
            r = ch.mint_v2(cid, key, p, amount, slip)
        elif ver == 4:
            r = ch.mint_v4(cid, key, p, amount, strategy, slip)
        else:
            r = ch.mint_position(cid, key, p, amount, strategy, slip)

    if ver == 2:
        pid = f"v2:{r['pair'].lower()}"
        store.add_ref(cid, addr, "v2", r["pair"])
    else:
        pid = f"v4:{r['token_id']}" if ver == 4 else r["token_id"]
        if ver == 4 and r["token_id"]:
            store.add_ref(cid, addr, "v4", str(r["token_id"]))
    store.record_event(cid, "mint", ev_id(pid), r["deposited_usd"],
                       f"{tk['symbol']}/{p['quote_sym']} {mode}", wallet=addr)
    return {"pid": str(pid), "steps": [{"label": l, "tx": h, "url": ch.tx_link(cid, h)}
                                       for l, h in r["steps"]],
            "deposited_usd": r["deposited_usd"], "link": ch.pos_link_any(cid, pid),
            "tick_lower": r.get("tick_lower"), "tick_upper": r.get("tick_upper")}


NPM_TOPICS = {
    "inc": "0x" + Web3.keccak(text="IncreaseLiquidity(uint256,uint128,uint256,uint256)").hex().removeprefix("0x"),
    "dec": "0x" + Web3.keccak(text="DecreaseLiquidity(uint256,uint128,uint256,uint256)").hex().removeprefix("0x"),
    "col": "0x" + Web3.keccak(text="Collect(uint256,address,uint256,uint256)").hex().removeprefix("0x"),
}


def _logs(chain_id: int, addr: str, topic0: str, topic1: str) -> list[dict]:
    """Log terfilter tokenId. fromBlock harus hex — Blockstock/Blockscout menolak
    integer 0 dengan 'invalid address' yang menyesatkan."""
    w3 = ch.get_w3(chain_id)
    try:
        r = w3.provider.make_request("eth_getLogs", [{
            "address": Web3.to_checksum_address(addr), "fromBlock": "0x0",
            "toBlock": "latest", "topics": [topic0, topic1]}])
        raw = r.get("result")
        if not isinstance(raw, list):
            raise RuntimeError(str(r.get("error")))
    except Exception:
        # RPC membatasi rentang getLogs → lewat API explorer
        try:
            resp = requests.get(f"{ch.CHAINS[chain_id]['explorer']}/api", timeout=30, params={
                "module": "logs", "action": "getLogs", "fromBlock": 0, "toBlock": "latest",
                "address": addr, "topic0": topic0, "topic1": topic1, "topic0_1_opr": "and"})
            raw = resp.json().get("result") or []
        except Exception:
            return []
    out = []
    for lg in raw:
        d = str(lg.get("data") or "")
        b = lg.get("blockNumber")
        try:
            bn = int(b, 16) if isinstance(b, str) else int(b)
        except (TypeError, ValueError):
            continue
        out.append({"data": d.removeprefix("0x"), "block": bn})
    return out


def _words(hexdata: str) -> list[int]:
    return [int(hexdata[i:i + 64], 16) for i in range(0, len(hexdata) - 63, 64)]


def cost_basis(chain_id: int, tid: int, _cache={}, ttl: int = 900) -> dict | None:
    """Modal & hasil tarikan posisi v3 langsung dari event NPM (bukan dari
    history.json lokal) — supaya PnL tetap benar walau posisinya di-mint di
    luar bot ini. None kalau RPC tidak mendukung getLogs rentang penuh."""
    ck = (chain_id, int(tid))
    hit = _cache.get(ck)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    npm = ch.CHAINS[chain_id]["npm"]
    t1 = "0x" + f"{int(tid):064x}"
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:   # 3 query sekaligus, bukan berurutan
        inc, dec, col = ex.map(lambda k: _logs(chain_id, npm, NPM_TOPICS[k], t1),
                               ("inc", "dec", "col"))
    if not inc:
        _cache[ck] = (None, time.time())
        return None

    d0 = d1 = w0 = w1 = c0 = c1 = 0
    for lg in inc:                     # (liquidity, amount0, amount1)
        v = _words(lg["data"])
        if len(v) >= 3:
            d0 += v[1]; d1 += v[2]
    for lg in dec:
        v = _words(lg["data"])
        if len(v) >= 3:
            w0 += v[1]; w1 += v[2]
    for lg in col:                     # (recipient, amount0, amount1)
        v = _words(lg["data"])
        if len(v) >= 3:
            c0 += v[1]; c1 += v[2]
    # Collect membawa principal hasil decrease + fee; selisihnya = fee terealisasi
    res = {"dep0": d0, "dep1": d1, "wd0": w0, "wd1": w1,
           "fee0": max(0, c0 - w0), "fee1": max(0, c1 - w1),
           "first_block": min(lg["block"] for lg in inc)}
    _cache[ck] = (res, time.time())
    return res


def _block_ts(chain_id: int, n: int, _cache={}) -> int | None:
    if n in _cache:
        return _cache[n]
    try:
        _cache[n] = int(ch.get_w3(chain_id).eth.get_block(n)["timestamp"])
    except Exception:
        _cache[n] = None
    return _cache[n]


_POS_CACHE: dict = {}
_POS_LOCK = threading.Lock()


def api_positions(q, _b) -> dict:
    """Cache pendek + refresh di latar: buka tab Positions langsung tampil,
    data baru menyusul. Tanpa ini tiap buka tab menunggu puluhan panggilan RPC."""
    cid = int(q.get("chain", [store.load_settings()["chain"]])[0])
    fresh = q.get("fresh", ["0"])[0] == "1"
    ck = (cid, bot.active_wallet_idx())
    hit = _POS_CACHE.get(ck)
    if hit and not fresh and time.time() - hit[1] < 20:
        return {**hit[0], "cached": True}
    if hit and not fresh and not _POS_LOCK.locked():
        # sajikan yang lama, muat ulang di belakang layar
        threading.Thread(target=_positions_refresh, args=(cid, ck), daemon=True).start()
        return {**hit[0], "cached": True, "stale": True}
    res = _positions_build(cid)
    _POS_CACHE[ck] = (res, time.time())
    return res


def _positions_refresh(cid: int, ck):
    with _POS_LOCK:
        try:
            _POS_CACHE[ck] = (_positions_build(cid), time.time())
        except Exception:
            pass


def _positions_build(cid: int) -> dict:
    key = bot.pk()
    addr = bot._addr_of(key)
    pos = ch.list_all_positions(cid, key, store.refs(cid, addr, "v2"), store.refs(cid, addr, "v4"))

    from concurrent.futures import ThreadPoolExecutor
    v3ids = [str(p.get("pid") or p.get("token_id")) for p in pos
             if p.get("ver", 3) == 3 and str(p.get("pid") or p.get("token_id")).isdigit()]
    cbs = {}
    if v3ids:
        def safe(pid):
            try:
                return cost_basis(cid, int(pid))
            except Exception:
                return None
        with ThreadPoolExecutor(max_workers=5) as ex:
            cbs = {pid: cb for pid, cb in zip(v3ids, ex.map(safe, v3ids)) if cb}

    out = []
    for p in pos:
        pid = str(p.get("pid") or p.get("token_id"))
        tid = ev_id(pid)
        dep = store.mint_usd(cid, tid) or 0
        fees = store.fees_claimed_usd(cid, tid)
        wd = store.withdrawn_usd(cid, tid)
        cur = p.get("value_usd", 0) + p.get("unclaimed_usd", 0)
        d = {k: v for k, v in p.items() if isinstance(v, (int, float, str, bool, type(None)))}

        # sisi meme vs quote + harga batas range (buat kartu ala UI LP)
        q_is_t1 = p.get("quote_is_token1", True)
        d["meme_sym"] = p["sym0"] if q_is_t1 else p["sym1"]
        d["meme_amount"] = p["amount0"] if q_is_t1 else p["amount1"]
        d["quote_amount"] = p["amount1"] if q_is_t1 else p["amount0"]
        d["meme_fees"] = p["fees0"] if q_is_t1 else p["fees1"]
        d["quote_fees"] = p["fees1"] if q_is_t1 else p["fees0"]
        if p.get("ver") != 2 and p.get("tick_lower") is not None:
            mdec = p["dec0"] if q_is_t1 else p["dec1"]
            qdec = p["dec1"] if q_is_t1 else p["dec0"]

            def pq(t, _m=mdec, _q=qdec, _t1=q_is_t1):
                raw = ch.tick_to_price(t)
                return (raw if _t1 else (1 / raw if raw else 0)) * 10 ** (_m - _q)
            lo, hi = sorted([pq(p["tick_lower"]), pq(p["tick_upper"])])
            now = pq(p["cur_tick"])
            d.update(price_lower=lo, price_upper=hi, price_now=now,
                     to_min_pct=(now / lo - 1) * 100 if lo else None,
                     to_max_pct=(hi / now - 1) * 100 if now else None)
        d.update(pid=pid, deposit_usd=dep, fees_claimed_usd=fees, withdrawn_usd=wd,
                 pnl_usd=(cur + fees + wd - dep) if dep else None,
                 basis="local" if dep else None,
                 age=store.fmt_age(store.mint_ts(cid, tid)),
                 link=ch.pos_link_any(cid, pid))
        cb = cbs.get(pid)
        if cb and d.get("price_now") and p.get("quote_sym"):
            # semua dinilai pada harga SEKARANG → PnL sudah termasuk impermanent loss
            try:
                qusd = ch.quote_usd_price(ch.get_w3(cid), cid, p["quote_sym"])
            except Exception:
                qusd = 0.0
            musd = d["price_now"] * qusd                    # USD per 1 meme
            u0, u1 = (musd, qusd) if q_is_t1 else (qusd, musd)
            e0, e1 = 10 ** p["dec0"], 10 ** p["dec1"]
            dep_usd = cb["dep0"] / e0 * u0 + cb["dep1"] / e1 * u1
            wd_usd = cb["wd0"] / e0 * u0 + cb["wd1"] / e1 * u1
            fee_usd = cb["fee0"] / e0 * u0 + cb["fee1"] / e1 * u1
            if dep_usd > 0:
                d.update(deposit_usd=dep_usd, withdrawn_usd=wd_usd,
                         fees_claimed_usd=fee_usd, basis="onchain",
                         pnl_usd=cur + wd_usd + fee_usd - dep_usd)
                ts = _block_ts(cid, cb["first_block"])
                if ts:
                    d["age"] = store.fmt_age(ts)
        out.append(d)
    summ = store.portfolio_summary(cid, addr)
    onchain = [p for p in out if p.get("basis") == "onchain"]
    if onchain:   # ringkasan pakai data on-chain kalau tersedia (lebih lengkap)
        summ["deposits"] = sum(p["deposit_usd"] for p in onchain)
        summ["withdrawals"] = sum(p["withdrawn_usd"] for p in onchain)
        summ["fees_claimed"] = sum(p["fees_claimed_usd"] for p in onchain)
    summ["total_value"] = sum(p.get("value_usd", 0) for p in out)
    summ["unclaimed"] = sum(p.get("unclaimed_usd", 0) for p in out)
    summ["open"] = len(out)
    summ["in_range"] = sum(1 for p in out if p.get("in_range"))
    known = [p for p in out if p.get("pnl_usd") is not None]
    summ["pnl"] = sum(p["pnl_usd"] for p in known) if known else None
    return {"positions": out, "summary": summ}


def ev_id(pid):
    """ID event PnL — harus sama persis dengan bot: int untuk v3, string untuk v2/v4."""
    return ch.parse_pid(pid)[1] if str(pid).isdigit() else str(pid)


def _snapshot(cid: int, pid: str) -> dict | None:
    key = bot.pk()
    addr = bot._addr_of(key)
    pos = ch.list_all_positions(cid, key, store.refs(cid, addr, "v2"), store.refs(cid, addr, "v4"))
    return next((p for p in pos if str(p.get("pid") or p.get("token_id")) == str(pid)), None)


def api_action(_q, b) -> dict:
    """add | reduce | collect | close | rebalance untuk pid apa pun (v2/v3/v4).
    Nilai USD yang dicatat diambil dari snapshot posisi SEBELUM tx — sama
    seperti bot, supaya riwayat PnL dua UI ini konsisten."""
    s = store.load_settings()
    cid = int(b.get("chain", s["chain"]))
    pid = str(b["pid"])
    act = str(b["action"])
    slip = float(b.get("slippage_pct", s["slippage_pct"]))
    addr = bot._addr_of(bot.pk())
    ver, ref = ch.parse_pid(pid)
    tid = ev_id(pid)
    pos = None if act == "add" else _snapshot(cid, pid)
    extra = {}

    with TX_LOCK:
        if act == "add":
            r = ch.add_any(cid, bot.pk(), pid, float(b["amount"]), slip)
        elif act == "reduce":
            r = ch.reduce_any(cid, bot.pk(), pid, int(b["pct"]), slip)
        elif act == "collect":
            r = ch.collect_any(cid, bot.pk(), pid)
        elif act == "close":
            r = ch.close_any(cid, bot.pk(), pid, slip, bool(b.get("autoswap", s["autoswap"])))
        elif act == "rebalance":
            r = ch.rebalance_position(cid, bot.pk(), pid, str(b.get("mode", "wide")),
                                      slip, int(s.get("gap", 1)))
        else:
            raise RuntimeError(f"Aksi tidak dikenal: {act}")

    if act == "add":
        store.record_event(cid, "mint", tid, r["added_usd"], "add", wallet=addr)
    elif act == "reduce":
        pct = int(b["pct"])
        if pos:
            store.record_event(cid, "close", tid, pos["value_usd"] * pct / 100,
                               f"reduce {pct}%", wallet=addr)
            if pos["unclaimed_usd"] > 0:
                store.record_event(cid, "fees", tid, pos["unclaimed_usd"], wallet=addr)
    elif act == "collect":
        if pos and pos["unclaimed_usd"] > 0:
            store.record_event(cid, "fees", tid, pos["unclaimed_usd"], wallet=addr)
            extra["fees_usd"] = pos["unclaimed_usd"]
    elif act == "close":
        if ver == 4:
            store.drop_ref(cid, addr, "v4", str(ref))
        elif ver == 2:
            store.drop_ref(cid, addr, "v2", str(ref))
        store.record_event(cid, "close", tid, pos["value_usd"] if pos else 0.0, wallet=addr)
        if pos and pos["unclaimed_usd"] > 0:
            store.record_event(cid, "fees", tid, pos["unclaimed_usd"], wallet=addr)
        extra["withdrawn_usd"] = (pos["value_usd"] + pos["unclaimed_usd"]) if pos else 0.0
        extra["swaps"] = [{"sym": sy, "tx": h, "url": ch.tx_link(cid, h)}
                          for sy, h in r.get("swaps", []) if str(h).startswith("0x")]
    elif act == "rebalance":
        if pos:
            store.record_event(cid, "close", tid, pos["value_usd"], "rebalance out", wallet=addr)
            if pos["unclaimed_usd"] > 0:
                store.record_event(cid, "fees", tid, pos["unclaimed_usd"], wallet=addr)
        new_pid = f"v4:{r['token_id']}" if ver == 4 else r["token_id"]
        if ver == 4:
            store.drop_ref(cid, addr, "v4", str(ref))
            if r["token_id"]:
                store.add_ref(cid, addr, "v4", str(r["token_id"]))
        store.record_event(cid, "mint", new_pid, r["deposited_usd"], "rebalance in", wallet=addr)
        extra["new_pid"] = str(new_pid)
        extra["link"] = ch.pos_link_any(cid, new_pid) if r["token_id"] else None

    steps = [{"label": l, "tx": h, "url": ch.tx_link(cid, h)} for l, h in r.get("steps", [])]
    return {"ok": True, "steps": steps, **extra,
            **{k: v for k, v in r.items()
               if k != "steps" and isinstance(v, (int, float, str, bool, type(None)))}}


ROUTES_GET = {"/api/state": api_state, "/api/candles": api_candles, "/api/positions": api_positions}
ROUTES_POST = {"/api/settings": api_settings, "/api/discover": api_discover,
               "/api/pool": api_pool, "/api/preview": api_preview,
               "/api/liquidity": api_liquidity, "/api/mint": api_mint,
               "/api/action": api_action}


# ──────────────────────────── HTTP ────────────────────────────
WEB_TOKEN = os.environ.get("WEB_TOKEN", "").strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "unipool"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/" in str(args[0] if args else ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _authed(self, q: dict) -> bool:
        if not WEB_TOKEN:
            return True
        return (self.headers.get("X-Token") == WEB_TOKEN
                or q.get("t", [""])[0] == WEB_TOKEN)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            self._send(401, b"unauthorized", "text/plain")
            return
        if u.path in ROUTES_GET:
            self._run(ROUTES_GET[u.path], q, {})
            return
        name = "index.html" if u.path in ("/", "/index.html") else u.path.lstrip("/")
        f = (STATIC / name).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            self._json(401, {"error": "unauthorized"})
            return
        fn = ROUTES_POST.get(u.path)
        if not fn:
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            self._json(400, {"error": "body bukan JSON"})
            return
        self._run(fn, q, body)

    def _run(self, fn, q, body):
        t0 = time.time()
        try:
            res = fn(q, body)
            res["_ms"] = int((time.time() - t0) * 1000)
            self._json(200, res)
        except Exception as e:
            traceback.print_exc()
            self._json(400, {"error": str(e) or e.__class__.__name__})


def main():
    host = os.environ.get("WEB_HOST", "127.0.0.1").strip()
    port = int(os.environ.get("WEB_PORT", "8899"))
    if host not in ("127.0.0.1", "localhost", "::1") and not WEB_TOKEN:
        sys.exit("❌ WEB_HOST bukan localhost tapi WEB_TOKEN kosong.\n"
                 "   UI ini memegang private key — jangan pernah dibuka ke internet tanpa token.\n"
                 "   Set WEB_TOKEN di .env, atau akses lewat SSH tunnel:\n"
                 "   ssh -L 8899:127.0.0.1:8899 user@vps")
    if not os.environ.get("PRIVATE_KEY", "").strip():
        sys.exit("❌ PRIVATE_KEY belum diset (.env).")
    if not STATIC.is_dir():
        sys.exit(f"❌ Folder static/ tidak ada di {STATIC}")

    keys = bot.all_pks()
    print(f"unipool web · http://{host}:{port}" + ("?t=<WEB_TOKEN>" if WEB_TOKEN else ""))
    print("wallet: " + ", ".join(f"W{i + 1} {bot._addr_of(k)}" for i, k in enumerate(keys)))
    if not WEB_TOKEN:
        print("⚠️  WEB_TOKEN kosong — server hanya menerima koneksi dari localhost.")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
