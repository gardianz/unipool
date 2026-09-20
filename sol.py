"""Mesin Solana — HANYA pool Meteora DLMM.

Kedudukannya sama dengan `chain.py` untuk chain EVM: seluruh logika Solana
tinggal di sini, `bot.py`/`web.py` cuma UI di atasnya. `chain.py` merutekan ke
modul ini lewat `CHAINS[SOL_CHAIN]["kind"] == "solana"`.

Tiga sumber, dan pembagiannya disengaja:

- **Data API Meteora** (`dlmm.datapi.meteora.ag`) — daftar pool, TVL, volume,
  APR, fee, harga USD token. Read-only dan cepat; dipakai untuk TAMPILAN dan
  untuk memilih pool. Tidak pernah jadi dasar membangun transaksi.
- **RPC Solana** (Alchemy) — keadaan pool & posisi yang sebenarnya.
- **Sidecar Node** (`meteora/dlmm.cjs`) — SDK resmi `@meteora-ag/dlmm` untuk
  membaca posisi dan MENANDATANGANI transaksi. Lihat komentar di file itu untuk
  alasan kenapa bukan Python.

Yang TIDAK ada di sini dan memang tidak akan ada: approval/allowance (Solana
tidak punya), wrap/unwrap manual (SDK yang mengurus wSOL), dan tick spacing —
padanannya `bin_step`.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

import requests

# ══════════════════════════ Konstanta ══════════════════════════
# Solana tidak punya EVM chainId. 1399811149 adalah id numerik yang dipakai
# registry lintas-chain (mis. Wormhole/chainlist) untuk mainnet-beta, jadi ia
# tidak mungkin bentrok dengan chain EVM mana pun di CHAINS.
SOL_CHAIN = 1399811149

DLMM_PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"   # diverifikasi on-chain
SOL_MINT = "So11111111111111111111111111111111111111112"        # wrapped SOL
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

DATAPI = "https://dlmm.datapi.meteora.ag"
_UA = {"accept": "application/json", "user-agent": "Mozilla/5.0"}

# Rate limit Data API 30 req/detik. Yang kita kirim jauh di bawah itu, tapi
# cache tetap perlu: satu kartu pool bisa memanggil endpoint yang sama beberapa
# kali lewat jalur berbeda.
_API_TTL = 45
_api_cache: dict = {}
_api_lock = threading.Lock()

# Sewa akun posisi DLMM. Dikembalikan saat posisi ditutup, tapi selama posisi
# hidup ia TERKUNCI — dan itu harus disebut UI, kalau tidak user mengira
# SOL-nya hilang. Terukur dari `getMinimumBalanceForRentExemption` PositionV2.
POSITION_RENT_SOL = 0.05724

# Shape likuiditas DLMM. Ini padanan asli Meteora — di EVM tidak ada (Uniswap
# cuma punya rentang datar), jadi jangan dipetakan paksa ke mode EVM.
SHAPES = (
    ("Spot", "▬ Spot", "rata di seluruh range — serbaguna, fee stabil"),
    ("Curve", "▲ Curve", "menumpuk di tengah range — untuk harga yang diperkirakan diam"),
    ("BidAsk", "▼ Bid-Ask", "menumpuk di kedua tepi — untuk volatil / DCA keluar-masuk"),
)

MAX_BINS_PER_POSITION = 1400     # PositionV2; default 70
_SIDECAR = Path(__file__).resolve().parent / "meteora" / "dlmm.cjs"
_SIDECAR_TIMEOUT = 180


class SolanaError(RuntimeError):
    """Kegagalan jalur Solana. Turunan RuntimeError supaya semua `except
    RuntimeError` yang sudah ada di bot ikut menangkapnya."""


# ══════════════════════════ base58 ══════════════════════════
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_MAP = {c: i for i, c in enumerate(_B58)}


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + (out or "1")


def b58decode(txt: str) -> bytes:
    n = 0
    for c in txt:
        if c not in _B58_MAP:
            raise ValueError(f"bukan base58: {c!r}")
        n = n * 58 + _B58_MAP[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(txt) - len(txt.lstrip("1"))) + body


def is_sol_address(txt: str) -> bool:
    """True untuk alamat/mint Solana. Dipakai `on_address` untuk memisahkan
    tempelan CA Solana dari CA EVM (`0x…`) TANPA menebak dari panjang saja —
    base58 harus benar-benar mendekode ke 32 byte."""
    t = str(txt or "").strip()
    if not (32 <= len(t) <= 44) or t.startswith("0x"):
        return False
    try:
        return len(b58decode(t)) == 32
    except ValueError:
        return False


# ══════════════════════════ Wallet ══════════════════════════
def secret_keys() -> list[str]:
    """Isi `SOLANA_PRIVATE_KEY` / `SOLANA_PRIVATE_KEYS` (dipisah koma/spasi/baris).

    Formatnya base58 (ekspor Phantom/Solflare) atau array JSON 64 byte
    (`solana-keygen`). SENGAJA terpisah dari `PRIVATE_KEY` EVM: key EVM itu
    secp256k1 dan Solana ed25519 — dipakai silang bukan cuma gagal, tapi
    menghasilkan alamat yang bukan milik siapa pun."""
    out, seen = [], set()
    for env in ("SOLANA_PRIVATE_KEY", "SOLANA_PRIVATE_KEYS"):
        raw = (os.environ.get(env) or "").strip()
        if not raw:
            continue
        # Array JSON boleh memuat koma di dalamnya, jadi ia diambil utuh.
        parts = [raw] if raw.startswith("[") else raw.replace(",", " ").split()
        for p in parts:
            p = p.strip().strip("'\"")
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def address_of(secret: str) -> str:
    """Alamat publik dari secret key, TANPA kriptografi.

    Format secret Solana 64 byte = 32 byte seed + 32 byte public key, jadi
    alamatnya tinggal 32 byte terakhir. Untuk seed 32 byte saja, turunannya
    butuh ed25519 dan itu diserahkan ke sidecar."""
    t = str(secret).strip()
    raw = bytes(json.loads(t)) if t.startswith("[") else b58decode(t)
    if len(raw) == 64:
        return b58encode(raw[32:])
    raise SolanaError(
        "Secret key Solana harus 64 byte (ekspor Phantom/Solflare atau "
        f"solana-keygen); yang diberikan {len(raw)} byte.")


# ══════════════════════════ RPC ══════════════════════════
_rpc_bad: dict[str, float] = {}
_RPC_BAD_COOLDOWN = 900.0        # kuota Alchemy per-app, tidak pulih dalam detik


def _alchemy_keys() -> list[str]:
    # Diimpor di dalam fungsi: chain.py meng-import modul ini, jadi import di
    # level atas akan melingkar.
    import chain as ch
    return ch.alchemy_keys()


def rpc_urls() -> list[str]:
    """Endpoint Solana, yang sedang dihukum ditaruh di belakang — bukan dibuang.

    Aturan yang sama dengan `chain.get_w3`: membuang endpoint bisa menghabiskan
    kandidat sampai tidak ada RPC sama sekali. Terukur pada key user: 4 dari 6
    key sehat di Solana, satu kuotanya habis, satu lagi belum di-enable
    (`SOLANA_MAINNET is not enabled for this app` — harus dinyalakan per app di
    dashboard Alchemy, sama seperti base-mainnet dan arc-mainnet)."""
    urls = [f"https://solana-mainnet.g.alchemy.com/v2/{k}" for k in _alchemy_keys()]
    extra = (os.environ.get("RPC_SOLANA") or "").replace(",", " ").split()
    urls += [u for u in extra if u.startswith("http")]
    if not urls:
        urls = ["https://api.mainnet-beta.solana.com"]
    now = time.time()
    return sorted(urls, key=lambda u: max(0.0, _rpc_bad.get(u, 0) - now))


_HEALTH_TTL = 600.0
_health: dict = {}        # url -> (kind, waktu)


def healthy_rpc_urls() -> list[str]:
    """Endpoint yang benar-benar bisa melayani Solana, yang sehat di depan.

    Endpoint ber-`not enabled` DIBUANG, bukan cuma diurutkan ke belakang: tidak
    seperti 429 yang sesaat, "SOLANA_MAINNET is not enabled for this app" adalah
    keadaan permanen sampai network-nya dinyalakan di dashboard Alchemy — dan
    mencobanya berarti satu request gagal di tiap panggilan. Terukur pada key
    user: 4 dari 6 key sehat, satu kuota bulanan habis, satu belum di-enable.

    Yang kena 429 TETAP dipakai (ditaruh terakhir): itu batas throughput yang
    pulih dalam hitungan detik, dan membuangnya bisa menghabiskan kandidat."""
    urls = rpc_urls()
    now = time.time()
    stale = [u for u in urls if now - _health.get(u, ("", 0))[1] > _HEALTH_TTL]
    if stale:
        import concurrent.futures as _cf

        def probe(u):
            try:
                r = requests.post(u, json={"jsonrpc": "2.0", "id": 1,
                                           "method": "getSlot", "params": []}, timeout=8)
                msg = str(((r.json() or {}).get("error") or {}).get("message") or "")
                return u, ("off" if "not enabled" in msg
                           else "quota" if "capacity limit" in msg
                           else "burst" if ("429" in msg or "compute units per second" in msg)
                           else "error" if msg else "ok")
            except Exception:
                return u, "error"

        with _cf.ThreadPoolExecutor(max_workers=8) as ex:
            for u, kind in ex.map(probe, stale):
                _health[u] = (kind, now)
    rank = {"ok": 0, "burst": 1, "error": 2, "quota": 3}
    live = [u for u in urls if _health.get(u, ("ok", 0))[0] != "off"]
    live.sort(key=lambda u: rank.get(_health.get(u, ("ok", 0))[0], 2))
    # Katup pengaman: kalau semuanya tersaring, pakai daftar apa adanya —
    # chain tanpa satu pun endpoint jauh lebih buruk daripada endpoint buruk.
    return live or urls


def rpc_url() -> str:
    return healthy_rpc_urls()[0]


def _short_rpc(url: str) -> str:
    """URL yang aman ditulis di log — API key disensor jadi 4 karakter terakhir."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return url.replace(tail, "…" + tail[-4:]) if len(tail) > 8 else url


def rpc(method: str, params: list, timeout: float = 20.0):
    """Satu panggilan JSON-RPC, dengan rotasi endpoint. Melempar kalau semua gagal."""
    last = ""
    for url in healthy_rpc_urls():
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                         "method": method, "params": params},
                              timeout=timeout, headers={"content-type": "application/json"})
            body = r.json() if r.text else {}
            err = body.get("error") or {}
            if err:
                msg = str(err.get("message") or err)
                # Kuota habis / network belum di-enable = endpoint ini memang
                # mati untuk Solana; jangan dicoba lagi tiap panggilan.
                if "capacity limit" in msg or "not enabled" in msg:
                    _rpc_bad[url] = time.time() + _RPC_BAD_COOLDOWN
                last = f"{_short_rpc(url)}: {msg[:160]}"
                continue
            return body.get("result")
        except Exception as e:
            last = f"{_short_rpc(url)}: {type(e).__name__} {e}"
    raise SolanaError(f"Semua RPC Solana gagal — {last}")


def rpc_health() -> list[dict]:
    """Status tiap endpoint Solana untuk `/rpc`. Satu `getSlot` murah per URL."""
    out = []
    for url in rpc_urls():
        t0 = time.time()
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getSlot",
                                         "params": []}, timeout=10)
            body = r.json() if r.text else {}
            msg = str(((body.get("error") or {}).get("message")) or "")
            if msg:
                kind = ("quota" if "capacity limit" in msg
                        else "off" if "not enabled" in msg else "error")
            else:
                kind = "ok"
            out.append({"url": _short_rpc(url), "kind": kind,
                        "ms": int((time.time() - t0) * 1000), "why": msg[:120]})
        except Exception as e:
            out.append({"url": _short_rpc(url), "kind": "error",
                        "ms": int((time.time() - t0) * 1000), "why": f"{type(e).__name__}"})
    return out


def sol_balance(address: str) -> float:
    """Saldo SOL native (bukan wSOL)."""
    v = rpc("getBalance", [address, {"commitment": "confirmed"}]) or {}
    return int(v.get("value") or 0) / 1e9


def token_balance(address: str, mint: str) -> float:
    """Saldo SPL token. 0 kalau wallet belum punya akun token itu.

    wSOL SENGAJA dilaporkan sebagai saldo SOL native: akun wSOL hampir selalu
    kosong di luar transaksi (SDK membungkus & membuka sendiri di dalam satu tx),
    jadi melaporkan saldo akun wSOL akan selalu menulis 0 untuk wallet yang
    sebenarnya punya SOL."""
    if str(mint) == SOL_MINT:
        return sol_balance(address)
    v = rpc("getTokenAccountsByOwner",
            [address, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
    total = 0.0
    for acc in v.get("value") or []:
        info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        total += float(((info.get("tokenAmount") or {}).get("uiAmount")) or 0)
    return total


# ══════════════════════════ Data API Meteora ══════════════════════════
def _api(path: str, params: dict | None = None, ttl: int = _API_TTL) -> dict | list:
    """GET Data API dengan cache pendek. Kegagalan IKUT di-cache sebentar.

    Aturan yang sama dengan `chain._dex_pairs()`: tanpa itu, host yang sedang
    tidak bisa menjangkau Meteora membayar timeout penuh pada SETIAP klik.
    Hasil lama dikembalikan kalau ada — ini angka tampilan, basi jauh lebih baik
    daripada hilang."""
    key = (path, tuple(sorted((params or {}).items())))
    now = time.time()
    with _api_lock:
        hit = _api_cache.get(key)
    if hit and now - hit[1] < (ttl if hit[2] else 20):
        return hit[0]
    try:
        r = requests.get(f"{DATAPI}{path}", params=params or {}, headers=_UA, timeout=20)
        r.raise_for_status()
        data = r.json()
        with _api_lock:
            _api_cache[key] = (data, now, True)
        return data
    except Exception:
        if hit:
            with _api_lock:
                _api_cache[key] = (hit[0], now, False)
            return hit[0]
        with _api_lock:
            _api_cache[key] = ({}, now, False)
        return {}


def api_pools(query: str = "", page_size: int = 100, sort_by: str = "") -> list[dict]:
    p = {"page_size": int(page_size)}
    if query:
        p["query"] = query
    if sort_by:
        p["sort_by"] = sort_by
    d = _api("/pools", p)
    rows = d.get("data") if isinstance(d, dict) else d
    return rows if isinstance(rows, list) else []


def api_pool(pool: str) -> dict:
    d = _api(f"/pools/{pool}")
    if isinstance(d, dict):
        return d.get("data") if isinstance(d.get("data"), dict) else d
    return {}


# ══════════════════════════ Sidecar ══════════════════════════
def sidecar(cmd: str, secret: str | None = None, timeout: float = _SIDECAR_TIMEOUT,
            **kw) -> dict:
    """Panggil `meteora/dlmm.cjs`. Melempar `SolanaError` kalau gagal.

    `secret` dikirim lewat STDIN, tidak pernah argv — argv terlihat di `ps` oleh
    semua user di mesin itu."""
    if not _SIDECAR.exists():
        raise SolanaError(
            "Sidecar Meteora belum terpasang. Jalankan: "
            "npm install --prefix meteora")
    req = {"cmd": cmd, **kw}
    if cmd != "ping":
        # SELURUH daftar dikirim, bukan satu URL: perintah baca DLMM memakai
        # getProgramAccounts dan Alchemy menjawabnya 429 bahkan untuk satu
        # wallet, jadi tanpa rotasi `/list` gagal total. Sidecar TIDAK merotasi
        # perintah yang menandatangani.
        urls = healthy_rpc_urls()
        req.setdefault("rpcs", urls)
        req.setdefault("rpc", urls[0])
    if secret:
        req["secret"] = secret
    try:
        p = subprocess.run(["node", str(_SIDECAR)], input=json.dumps(req),
                           capture_output=True, text=True, timeout=timeout,
                           cwd=str(_SIDECAR.parent.parent))
    except FileNotFoundError:
        raise SolanaError("`node` tidak ditemukan — sidecar Meteora butuh Node.js 18+.")
    except subprocess.TimeoutExpired:
        raise SolanaError(f"Sidecar Meteora timeout {timeout:.0f}s pada '{cmd}'.")
    try:
        out = json.loads(p.stdout or "{}")
    except ValueError:
        tail = (p.stderr or p.stdout or "")[-300:].strip()
        raise SolanaError(f"Sidecar Meteora balas bukan JSON: {tail}")
    if not out.get("ok"):
        msg = str(out.get("error") or "gagal tanpa pesan")
        logs = out.get("logs") or []
        if logs:
            # Log program itu SATU-SATUNYA yang menyebut error Anchor yang
            # sebenarnya; tanpa ini user cuma melihat "Transaction failed".
            msg += " · log: " + " | ".join(str(x)[:120] for x in logs[-3:])
        raise SolanaError(msg)
    out.pop("ok", None)
    return out


def sidecar_ready() -> tuple[bool, str]:
    """(siap, keterangan) — dipakai `/rpc` dan pemeriksaan sebelum tx pertama."""
    try:
        d = sidecar("ping", timeout=30)
        return True, f"SDK {d.get('sdk')} · {d.get('node')}"
    except SolanaError as e:
        return False, str(e)


# ══════════════════════════ Harga & token ══════════════════════════
_QUOTE_MINTS = {SOL_MINT: "SOL", USDC_MINT: "USDC", USDT_MINT: "USDT"}


def quote_side(mint_x: str, mint_y: str) -> tuple[str | None, bool]:
    """(simbol quote, quote_adalah_token_y). (None, False) kalau tak satu pun
    sisi berupa quote yang dikenal.

    USDC/USDT menang atas SOL: untuk pair SOL/USDC, yang dianggap "meme" harus
    SOL, bukan sebaliknya — kalau terbalik, seluruh kartu menampilkan harga USDC
    dalam satuan SOL."""
    for pref in (USDC_MINT, USDT_MINT, SOL_MINT):
        if mint_y == pref:
            return _QUOTE_MINTS[pref], True
        if mint_x == pref:
            return _QUOTE_MINTS[pref], False
    return None, False


def token_meta(mint: str) -> dict:
    """{'symbol','decimals','price'} dari Data API Meteora, dengan GMGN sebagai
    cadangan harga. Meteora hanya mengenal token yang punya pool DLMM."""
    for row in api_pools(query=str(mint), page_size=10):
        for side in ("token_x", "token_y"):
            t = row.get(side) or {}
            if str(t.get("address")) == str(mint):
                return {"address": mint, "symbol": t.get("symbol") or "?",
                        "name": t.get("name") or "", "decimals": int(t.get("decimals") or 0),
                        "price": float(t.get("price") or 0),
                        "supply": float(t.get("total_supply") or 0),
                        "verified": bool(t.get("is_verified"))}
    return {"address": mint, "symbol": "?", "name": "", "decimals": 0,
            "price": 0.0, "supply": 0.0, "verified": False}


def token_usd_price(mint: str) -> float:
    """Harga USD 1 token. Meteora dulu (harganya per-pool, langsung dari pool
    terdalam token itu), GMGN sebagai pembanding kalau Meteora tidak punya."""
    px = float(token_meta(mint).get("price") or 0)
    if px > 0:
        return px
    try:
        import gmgn
        key = gmgn.api_key()
        if key:
            d = gmgn.Client(key).token_info("sol", str(mint)) or {}
            return float(((d.get("price") or {}).get("price")) or 0)
    except Exception:
        pass
    return 0.0


# ══════════════════════════ Pool ══════════════════════════
def _pool_from_api(row: dict) -> dict | None:
    """Satu entri Data API → dict pool_info berbentuk SAMA dengan pool EVM.

    Bentuknya sengaja dibuat cocok supaya kartu pool, pengurutan TVL, penanda
    `thin`, dan kolom APR di `bot.py`/`web.py` tidak butuh cabang khusus."""
    tx, ty = row.get("token_x") or {}, row.get("token_y") or {}
    mx, my = str(tx.get("address") or ""), str(ty.get("address") or "")
    if not mx or not my:
        return None
    qsym, q_is_y = quote_side(mx, my)
    if qsym is None:
        return None            # pair tanpa quote yang dikenal — tidak bisa dinilai
    cfgp = row.get("pool_config") or {}
    bin_step = int(cfgp.get("bin_step") or 0)
    qaddr = my if q_is_y else mx
    qinfo = ty if q_is_y else tx
    vol = row.get("volume") or {}
    return {
        "ver": 5, "dex": "Meteora DLMM", "pool": str(row.get("address") or ""),
        "name": row.get("name") or "",
        "bin_step": bin_step,
        # `fee` dipakai UI sebagai ppm, sama seperti fee tier Uniswap, supaya
        # label "%"-nya dihitung dengan rumus yang sama (fee/1e4).
        "fee": int(round(float(cfgp.get("base_fee_pct") or 0) * 10_000)),
        "base_fee_pct": float(cfgp.get("base_fee_pct") or 0),
        "max_fee_pct": float(cfgp.get("max_fee_pct") or 0),
        "dynamic_fee_pct": float(row.get("dynamic_fee_pct") or 0),
        # Padanan tick spacing: bin_step yang menentukan lebar satu kotak.
        "tick_spacing": bin_step,
        "quote_sym": qsym, "quote_addr": qaddr,
        "quote_decimals": int(qinfo.get("decimals") or 0),
        "quote_usd": float(qinfo.get("price") or 0),
        "quote_is_token1": q_is_y,
        "token0": mx, "token1": my,
        "sym0": tx.get("symbol") or "?", "sym1": ty.get("symbol") or "?",
        "dec0": int(tx.get("decimals") or 0), "dec1": int(ty.get("decimals") or 0),
        "basis": "meteora",
        "tvl_usd": float(row.get("tvl") or 0),
        "vol24_usd": float(vol.get("24h") or 0),
        "apr_pct": float(row.get("apr") or 0) * 100.0,
        "price": float(row.get("current_price") or 0),
        "launchpad": row.get("launchpad") or None,
        "blacklisted": bool(row.get("is_blacklisted")),
        "has_farm": bool(row.get("has_farm")),
    }


def pool_info(pool: str) -> dict:
    """pool_info satu pool DLMM dari alamatnya. Dipakai jalur posisi, yang cuma
    tahu alamat pool-nya."""
    row = api_pool(pool)
    p = _pool_from_api(row) if row else None
    if p:
        return p
    # Data API belum mengindeks pool ini (pool yang baru lahir) — susun dari
    # keadaan on-chain lewat sidecar supaya posisinya tetap bisa dibaca.
    st = sidecar("pool", pool=pool)
    qsym, q_is_y = quote_side(st["mint_x"], st["mint_y"])
    return {
        "ver": 5, "dex": "Meteora DLMM", "pool": pool, "name": "",
        "bin_step": int(st["bin_step"]), "tick_spacing": int(st["bin_step"]),
        "fee": int(round(float(st.get("base_fee_pct") or 0) * 10_000)),
        "base_fee_pct": float(st.get("base_fee_pct") or 0),
        "dynamic_fee_pct": float(st.get("dynamic_fee_pct") or 0),
        "quote_sym": qsym, "quote_addr": (st["mint_y"] if q_is_y else st["mint_x"]),
        "quote_decimals": int(st["dec_y"] if q_is_y else st["dec_x"]),
        "quote_usd": 0.0, "quote_is_token1": q_is_y,
        "token0": st["mint_x"], "token1": st["mint_y"],
        "dec0": int(st["dec_x"]), "dec1": int(st["dec_y"]),
        "basis": "meteora", "tvl_usd": 0.0, "vol24_usd": 0.0, "apr_pct": None,
        "price": float(st.get("price") or 0),
    }


def discover(token: str) -> dict:
    """Semua pool DLMM yang memuat token ini. Bentuk hasilnya sama persis dengan
    `chain.discover_any()` supaya UI tidak butuh cabang."""
    rows = api_pools(query=str(token), page_size=100)
    pools, seen = [], set()
    for row in rows:
        tx, ty = row.get("token_x") or {}, row.get("token_y") or {}
        if str(token) not in (str(tx.get("address")), str(ty.get("address"))):
            continue          # query juga mengembalikan pool token bernama serupa
        p = _pool_from_api(row)
        if not p or p["pool"] in seen:
            continue
        # Pool blacklist Meteora TIDAK ditampilkan: mereka menandainya sendiri,
        # dan itu satu-satunya sinyal rug yang datang dari pihak yang mengindeks
        # seluruh pool DLMM.
        if p["blacklisted"]:
            continue
        seen.add(p["pool"])
        p["thin"] = p["tvl_usd"] < 50
        pools.append(p)
    pools.sort(key=lambda p: p.get("tvl_usd") or 0, reverse=True)
    meta = token_meta(token)
    return {"token": meta, "pools": pools, "source": "meteora",
            "dropped_dead": [], "dropped_offprice": [], "hook_pools": 0}


def pool_state(pool: str) -> dict:
    """Keadaan pool LANGSUNG dari chain (bukan Data API): bin aktif, harga, fee
    dinamis. Dipakai jalur yang butuh angka SEKARANG, bukan angka terindeks."""
    return sidecar("pool", pool=pool)


def bin_prices(pool: str, bins=(), prices=()) -> dict:
    """Konversi bin id ↔ harga lewat SDK.

    Tidak dihitung di Python walau rumusnya cuma `(1 + bin_step/10000)**id`:
    pembulatan bin id saat deposit ditentukan SDK, dan kartu yang memakai
    pembulatan berbeda akan menjanjikan range yang bukan range yang terjadi."""
    return sidecar("bins", pool=pool, bins=list(bins), prices=list(prices))


def pool_stats(p: dict) -> dict:
    """TVL / volume / fee satu pool untuk kartu posisi — bentuk sama dengan
    `chain.pool_stats()`."""
    row = api_pool(p.get("pool") or "")
    fresh = _pool_from_api(row) if row else None
    src = fresh or p
    return {"tvl_usd": src.get("tvl_usd"), "vol24_usd": src.get("vol24_usd"),
            "fee": src.get("fee"), "tick_spacing": src.get("bin_step"),
            "apr_pct": src.get("apr_pct"), "tvl_src": "meteora"}


# ══════════════════════════ Posisi ══════════════════════════
def _price_of_bin(bin_id: int, bin_step: int, dec_x: int, dec_y: int) -> float:
    """Harga token_y per token_x pada satu bin, sudah disesuaikan desimal.

    Dipakai HANYA untuk menampilkan batas range. Angka yang dipakai transaksi
    selalu datang dari SDK (`bin_prices`) — rumus lokal ini tidak menentukan
    pembulatan bin apa pun."""
    return (1.0 + bin_step / 10_000.0) ** bin_id * 10 ** (dec_x - dec_y)


def _q_price(bin_id: int, bin_step: int, dx: int, dy: int, q_is_t1: bool) -> float:
    """Harga 1 MEME dalam satuan QUOTE pada sebuah bin."""
    r = _price_of_bin(bin_id, bin_step, dx, dy)
    return r if q_is_t1 else (1 / r if r else 0.0)


def _position_detail(raw: dict, pinfo: dict, active_bin: int | None = None) -> dict:
    """Satu posisi sidecar → dict posisi berbentuk SAMA dengan posisi v3/v4.

    `tick_lower`/`tick_upper`/`cur_tick` diisi BIN ID. Keduanya peran yang sama
    (koordinat harga diskret yang membatasi range), jadi seluruh UI range,
    penanda IN/OUT, dan konversi market cap jalan tanpa cabang."""
    dx, dy = int(raw.get("dec_x") or 0), int(raw.get("dec_y") or 0)
    bin_step = int(raw.get("bin_step") or pinfo.get("bin_step") or 0)
    lo, hi = int(raw["lower_bin"]), int(raw["upper_bin"])
    cur = int(active_bin if active_bin is not None else (raw.get("active_bin") or 0))

    a0 = int(raw.get("amount_x_raw") or 0) / 10 ** dx
    a1 = int(raw.get("amount_y_raw") or 0) / 10 ** dy
    f0 = int(raw.get("fee_x_raw") or 0) / 10 ** dx
    f1 = int(raw.get("fee_y_raw") or 0) / 10 ** dy

    px0 = token_usd_price(pinfo["token0"])
    px1 = token_usd_price(pinfo["token1"])
    usd0, usd1 = a0 * px0, a1 * px1
    fu0, fu1 = f0 * px0, f1 * px1

    qsym = pinfo.get("quote_sym")
    q_is_t1 = bool(pinfo.get("quote_is_token1"))
    mdec, qdec = (dx, dy) if q_is_t1 else (dy, dx)

    def _mc(b):
        pr = _price_of_bin(b, bin_step, dx, dy)
        if not q_is_t1:
            pr = 1 / pr if pr else 0.0
        qusd = pinfo.get("quote_usd") or (px1 if q_is_t1 else px0)
        supply = float((pinfo.get("supply") or 0)) or 0.0
        return pr * qusd * supply if supply else None

    return {
        "ver": 5, "pid": f"dlmm:{raw['address']}", "token_id": f"dlmm:{raw['address']}",
        "dex": "Meteora DLMM", "pool": pinfo["pool"], "position": raw["address"],
        "token0": pinfo["token0"], "token1": pinfo["token1"],
        "sym0": pinfo.get("sym0") or "?", "sym1": pinfo.get("sym1") or "?",
        "dec0": dx, "dec1": dy,
        "fee": pinfo.get("fee"), "bin_step": bin_step, "tick_spacing": bin_step,
        "tick_lower": lo, "tick_upper": hi, "cur_tick": cur,
        "bin_lower": lo, "bin_upper": hi, "active_bin": cur,
        "n_bins": hi - lo + 1,
        # Harga batas SELALU dalam satuan quote/meme, sama seperti kartu EVM —
        # `_price_of_bin` memberi y-per-x, jadi ia dibalik kalau quote = token_x.
        "price_lower": _q_price(lo, bin_step, dx, dy, q_is_t1),
        "price_upper": _q_price(hi + 1, bin_step, dx, dy, q_is_t1),
        "price_now": _q_price(cur, bin_step, dx, dy, q_is_t1),
        "amount0": a0, "amount1": a1, "fees0": f0, "fees1": f1,
        "in_range": lo <= cur <= hi,
        "value_usd": usd0 + usd1, "unclaimed_usd": fu0 + fu1,
        "usd0": usd0, "usd1": usd1, "fees_usd0": fu0, "fees_usd1": fu1,
        "quote_sym": qsym, "quote_is_token1": q_is_t1,
        "mc_lower": _mc(lo), "mc_upper": _mc(hi + 1), "mc_now": _mc(cur),
        # Sewa akun posisi TERKUNCI selama posisi hidup dan kembali saat close.
        # Wajib disebut UI, kalau tidak user mengira SOL-nya hilang.
        "rent_sol": POSITION_RENT_SOL,
    }


def portfolio_index(address: str) -> list[tuple[str, list[str]]]:
    """[(pool, [alamat posisi])] dari Data API — indeks, bukan sumber kebenaran.

    Alamat posisinya yang penting: dengan itu tiap posisi bisa dibaca lewat
    `getPosition` (satu akun) dan bukan `getProgramAccounts` (memindai seluruh
    akun program DLMM). Terukur, jalur pindai dijawab **429** oleh KEEMPAT key
    Alchemy yang sehat, jadi ini bukan optimasi — tanpa ini `/list` tidak jalan
    sama sekali di RPC free tier."""
    d = _api("/portfolio/open", {"user": str(address)}, ttl=30)
    out = []
    for pl in (d.get("pools") if isinstance(d, dict) else None) or []:
        a = pl.get("address") or pl.get("poolAddress")
        keys = [str(k) for k in (pl.get("listPositions") or []) if k]
        if a and keys:
            out.append((str(a), keys))
    return out


def portfolio_pools(address: str) -> list[str]:
    """Alamat pool tempat wallet ini PUNYA posisi terbuka, dari Data API.

    Dipakai sebagai INDEKS, dan itu yang membuat `/list` murah. Menyapu semua
    posisi lewat SDK berarti `getProgramAccounts` tanpa filter pool — terukur
    dijawab **429 Too Many Requests** oleh Alchemy bahkan untuk satu wallet,
    karena ia memindai seluruh akun program DLMM. Dengan indeks ini, RPC-nya
    cuma dipanggil per pool yang memang berisi posisi."""
    d = _api("/portfolio/open", {"user": str(address)}, ttl=30)
    out = []
    for pl in (d.get("pools") if isinstance(d, dict) else None) or []:
        a = pl.get("address") or pl.get("poolAddress")
        if a and a not in out:
            out.append(str(a))
    return out


def list_positions(address: str) -> list[dict]:
    """Semua posisi DLMM wallet ini.

    Dua jalur, dan urutannya menentukan ongkosnya:

    1. **Indeks Data API** (`/portfolio/open`) memberi daftar POOL-nya, lalu
       tiap pool dibaca terpisah lewat SDK — `getProgramAccounts` yang difilter
       pool jauh lebih murah dan tidak kena 429.
    2. Kalau Data API kosong/gagal, barulah sapuan penuh. Ia benar tapi mahal,
       jadi ia cadangan — bukan jalur utama.

    Posisi yang belum diindeks Data API (baru dibuat beberapa detik lalu) tetap
    ketemu lewat jalur 2, jadi indeks yang telat tidak pernah MENGHILANGKAN
    posisi — aturan yang sama dengan indexer Uniswap di jalur EVM."""
    raws = []
    for pool, keys in portfolio_index(address):
        try:
            d = sidecar("positions_by_key", pool=pool, positions=keys)
            for raw in d.get("positions") or []:
                raw.setdefault("active_bin", d.get("active_bin"))
                raws.append(raw)
        except SolanaError:
            continue
    if not raws:
        d = sidecar("positions", owner=address)
        raws = d.get("positions") or []
    out = []
    for raw in raws:
        try:
            out.append(_position_detail(raw, pool_info(raw["pool"]),
                                        raw.get("active_bin")))
        except Exception:
            # Sama seperti `list_all_positions` EVM: satu posisi yang gagal
            # dibaca tidak boleh menjatuhkan seluruh daftar.
            continue
    return out


def position_one(address: str, position: str, pool: str | None = None) -> dict | None:
    """Satu posisi dari alamatnya. `None` = benar-benar tidak ada.

    Aturan yang sama dengan `chain.position_by_pid`: kegagalan BACA dilempar,
    hanya ketiadaan yang mengembalikan None — kalau disamakan, RPC sibuk terbaca
    sebagai "posisi hilang" dan user mengira dananya lenyap."""
    if pool:
        d = sidecar("positions", owner=address, pool=pool)
        pinfo = pool_info(pool)
        for raw in d.get("positions") or []:
            if raw["address"] == position:
                return _position_detail(raw, pinfo, d.get("active_bin"))
        return None
    # Tanpa `pool`, pool-nya dicari dari akun posisinya sendiri (satu
    # `getAccountInfo`) — BUKAN dengan menyapu semua posisi wallet.
    try:
        return position_one(address, position, pool_of_position(position))
    except SolanaError:
        for p in list_positions(address):
            if p["position"] == position:
                return p
        return None


# ══════════════════════════ Aksi (butuh secret) ══════════════════════════
def _amt_raw(x: float, dec: int) -> str:
    return str(int(round(float(x) * 10 ** int(dec))))


def add_new(secret: str, pool: str, lower_bin: int, upper_bin: int,
            amount_x: float, amount_y: float, shape: str = "Spot",
            slippage_pct: float = 5.0, priority: int = 0) -> dict:
    """Buat posisi BARU + setor. Satu tekan, sama seperti jalur mint EVM."""
    p = pool_info(pool)
    n = int(upper_bin) - int(lower_bin) + 1
    if n < 1:
        raise SolanaError("Range kosong: bin atas harus ≥ bin bawah.")
    if n > MAX_BINS_PER_POSITION:
        raise SolanaError(
            f"Range {n} bin melebihi batas satu posisi ({MAX_BINS_PER_POSITION}). "
            f"Perkecil range atau pakai bin step yang lebih besar.")
    return sidecar("add_new", secret=secret, pool=pool,
                   lower_bin=int(lower_bin), upper_bin=int(upper_bin),
                   amount_x_raw=_amt_raw(amount_x, p["dec0"]),
                   amount_y_raw=_amt_raw(amount_y, p["dec1"]),
                   strategy=shape, slippage_pct=float(slippage_pct),
                   priority_micro_lamports=int(priority))


def add_existing(secret: str, pool: str, position: str, amount_x: float,
                 amount_y: float, shape: str = "Spot", slippage_pct: float = 5.0,
                 priority: int = 0) -> dict:
    """Tambah dana ke posisi yang sudah ada — rangenya TIDAK berubah, jadi bin
    batasnya dibaca dari posisi itu sendiri, bukan dari pemanggil."""
    p = pool_info(pool)
    cur = None
    d = sidecar("positions", owner=address_of(secret), pool=pool)
    for raw in d.get("positions") or []:
        if raw["address"] == position:
            cur = raw
            break
    if cur is None:
        raise SolanaError("Posisi tidak ditemukan di pool itu.")
    return sidecar("add_existing", secret=secret, pool=pool, position=position,
                   lower_bin=int(cur["lower_bin"]), upper_bin=int(cur["upper_bin"]),
                   amount_x_raw=_amt_raw(amount_x, p["dec0"]),
                   amount_y_raw=_amt_raw(amount_y, p["dec1"]),
                   strategy=shape, slippage_pct=float(slippage_pct),
                   priority_micro_lamports=int(priority))


def reduce(secret: str, pool: str, position: str, pct: int,
           priority: int = 0) -> dict:
    """Tarik sebagian likuiditas. 100% ikut menutup akun posisi dan mengembalikan
    sewanya — kalau tidak, SOL sewa itu terkunci di akun kosong selamanya."""
    bps = max(1, min(10_000, int(round(float(pct) * 100))))
    return sidecar("remove", secret=secret, pool=pool, position=position,
                   bps_to_remove=bps, close=bps >= 10_000,
                   priority_micro_lamports=int(priority))


def collect(secret: str, pool: str, position: str | None = None,
            priority: int = 0) -> dict:
    """Klaim fee swap. Tanpa `position`, semua posisi wallet di pool itu."""
    return sidecar("claim", secret=secret, pool=pool, position=position,
                   priority_micro_lamports=int(priority))


def close(secret: str, pool: str, position: str, priority: int = 0) -> dict:
    """Tarik 100% + klaim fee + tutup akun posisi (sewa kembali) dalam satu alur."""
    return sidecar("close", secret=secret, pool=pool, position=position,
                   priority_micro_lamports=int(priority))


def swap_quote(pool: str, amount_in: float, swap_for_y: bool,
               slippage_pct: float = 1.0) -> dict:
    p = pool_info(pool)
    dec = p["dec0"] if swap_for_y else p["dec1"]
    return sidecar("swap_quote", pool=pool, swap_for_y=bool(swap_for_y),
                   amount_in_raw=_amt_raw(amount_in, dec),
                   slippage_bps=int(round(float(slippage_pct) * 100)))


def swap(secret: str, pool: str, amount_in: float, swap_for_y: bool,
         slippage_pct: float = 1.0, priority: int = 0) -> dict:
    p = pool_info(pool)
    dec = p["dec0"] if swap_for_y else p["dec1"]
    return sidecar("swap", secret=secret, pool=pool, swap_for_y=bool(swap_for_y),
                   amount_in_raw=_amt_raw(amount_in, dec),
                   slippage_bps=int(round(float(slippage_pct) * 100)),
                   priority_micro_lamports=int(priority))


def explorer_tx(sig: str) -> str:
    return f"https://solscan.io/tx/{sig}"


def explorer_addr(addr: str) -> str:
    return f"https://solscan.io/account/{addr}"


# ══════════════════ Jembatan dispatcher generik chain.py ══════════════════
# `pid` bot untuk DLMM cuma membawa alamat POSISI (`dlmm:<pubkey>`), sedangkan
# tiap aksi SDK butuh alamat POOL-nya juga. Empat fungsi di bawah yang
# menjembatani, supaya `chain.add_any/reduce_any/collect_any/close_any` tidak
# perlu tahu apa pun tentang Meteora.
_pool_of: dict[str, tuple[str, float]] = {}
_POOL_OF_TTL = 3600.0      # pool sebuah posisi TIDAK pernah berubah


def pool_of_position(position: str) -> str:
    """Alamat pool pemilik posisi ini.

    Dibaca lewat SDK (`wrapPosition`), bukan dengan mengambil offset byte
    sendiri: layout Position, PositionV2, dan extended position berbeda, dan
    offset yang ditebak mengembalikan pubkey yang salah TANPA gejala — aksi
    berikutnya lalu dikirim ke pool lain."""
    hit = _pool_of.get(position)
    if hit and time.time() - hit[1] < _POOL_OF_TTL:
        return hit[0]
    d = sidecar("position_pool", position=position)
    _pool_of[position] = (d["pool"], time.time())
    return d["pool"]


def priority_fee() -> int:
    """Priority fee (micro-lamport per CU) dari `SOLANA_PRIORITY_FEE`.

    Default 0 = tanpa priority fee. Saat jaringan ramai, tx DLMM tanpa priority
    fee tertinggal dan yang dilihat user cuma "timeout" — angka yang bisa
    ditindaklanjuti harus bisa disetel tanpa menyentuh kode."""
    try:
        return max(0, int(float(os.environ.get("SOLANA_PRIORITY_FEE") or 0)))
    except ValueError:
        return 0


def _steps(sigs, what: str) -> list[str]:
    """Daftar langkah berbentuk sama dengan jalur EVM (`{"steps": [...]}`),
    supaya kartu hasil `bot.py` tidak butuh cabang."""
    return [f"{what}: {explorer_tx(s_)}" for s_ in (sigs or [])]


def add_any(secret: str, position: str, budget_quote: float,
            slippage_pct: float) -> dict:
    """Tambah dana ke posisi DLMM yang sudah ada. Range TIDAK berubah.

    `budget_quote` dalam satuan QUOTE pool itu — sama artinya dengan jalur EVM.
    Sisi lawannya dihitung sidecar lewat `autoFill*ByStrategy`, fungsi yang SAMA
    yang menentukan jumlah tertarik saat deposit."""
    pool = pool_of_position(position)
    p = pool_info(pool)
    q_is_y = bool(p["quote_is_token1"])
    qdec = p["dec1"] if q_is_y else p["dec0"]
    cur = sidecar("positions", owner=address_of(secret), pool=pool)
    raw = next((x for x in cur.get("positions") or []
                if x["address"] == position), None)
    if raw is None:
        raise SolanaError("Posisi tidak ditemukan di pool itu (sudah ditutup?).")
    kw = {("amount_y_raw" if q_is_y else "amount_x_raw"): _amt_raw(budget_quote, qdec)}
    plan = sidecar("quote_add", pool=pool, lower_bin=raw["lower_bin"],
                   upper_bin=raw["upper_bin"], strategy="Spot", **kw)
    d = sidecar("add_existing", secret=secret, pool=pool, position=position,
                lower_bin=raw["lower_bin"], upper_bin=raw["upper_bin"],
                amount_x_raw=plan["amount_x_raw"], amount_y_raw=plan["amount_y_raw"],
                strategy="Spot", slippage_pct=float(slippage_pct),
                priority_micro_lamports=priority_fee())
    x = int(plan["amount_x_raw"]) / 10 ** p["dec0"]
    y = int(plan["amount_y_raw"]) / 10 ** p["dec1"]
    qsym, msym = ((p.get("sym1"), p.get("sym0")) if q_is_y else (p.get("sym0"), p.get("sym1")))
    return {"steps": _steps(d.get("signatures"), "Add DLMM"),
            "added_usd": x * token_usd_price(p["token0"]) + y * token_usd_price(p["token1"]),
            "quote_sym": qsym, "meme_sym": msym,
            "quote_in": (y if q_is_y else x), "meme_in": (x if q_is_y else y),
            "quote_dep": (y if q_is_y else x), "signatures": d.get("signatures")}


def reduce_any(secret: str, position: str, pct: int) -> dict:
    """Tarik sebagian. 100% ikut MENUTUP akun posisi supaya sewanya kembali —
    tanpa itu ~0,057 SOL terkunci selamanya di akun kosong."""
    pool = pool_of_position(position)
    d = reduce(secret, pool, position, pct, priority=priority_fee())
    return {"steps": _steps(d.get("signatures"), f"Reduce {pct}% DLMM"),
            "closed": bool(d.get("closed")), "signatures": d.get("signatures"),
            "rent_back_sol": POSITION_RENT_SOL if d.get("closed") else 0.0}


def collect_any(secret: str, position: str) -> dict:
    pool = pool_of_position(position)
    before = position_one(address_of(secret), position, pool)
    d = collect(secret, pool, position, priority=priority_fee())
    return {"steps": _steps(d.get("signatures"), "Claim fee DLMM"),
            "fees_usd": (before or {}).get("unclaimed_usd") or 0.0,
            "signatures": d.get("signatures")}


def close_any(secret: str, position: str, autoswap: bool = False) -> dict:
    """Tarik 100% + klaim fee + tutup akun posisi, satu alur.

    `autoswap` DIABAIKAN dan itu disengaja: di jalur EVM ia menjual sisi meme
    hasil close lewat pool yang sama, dan di DLMM sisi mana yang keluar
    ditentukan letak bin aktif terhadap range. Menjual otomatis di sini berarti
    menebak niat user pada posisi yang baru saja ditutup — lebih baik disebut
    di kartu daripada dilakukan diam-diam."""
    pool = pool_of_position(position)
    d = close(secret, pool, position, priority=priority_fee())
    before = d.get("before") or {}
    p = pool_info(pool)
    a0 = int(before.get("amount_x_raw") or 0) / 10 ** p["dec0"]
    a1 = int(before.get("amount_y_raw") or 0) / 10 ** p["dec1"]
    f0 = int(before.get("fee_x_raw") or 0) / 10 ** p["dec0"]
    f1 = int(before.get("fee_y_raw") or 0) / 10 ** p["dec1"]
    px0, px1 = token_usd_price(p["token0"]), token_usd_price(p["token1"])
    return {"steps": _steps(d.get("signatures"), "Close DLMM"),
            "closed_usd": (a0 + f0) * px0 + (a1 + f1) * px1,
            "fees_usd": f0 * px0 + f1 * px1,
            "amount0": a0, "amount1": a1, "fees0": f0, "fees1": f1,
            "rent_back_sol": POSITION_RENT_SOL,
            "signatures": d.get("signatures"),
            "note": "Sisa token TIDAK dijual otomatis — cek /wallet.",
            }


# ══════════════════════════ Range & modal ══════════════════════════
def range_bins(pool: str, low_pct: float, up_pct: float, mode: str = "wide") -> dict:
    """Bin batas untuk range ±persen di sekitar harga SEKARANG.

    Persennya dipakai pada harga MEME dalam satuan quote — sama artinya dengan
    `low_pct`/`up_pct` di jalur EVM, jadi tombol preset lebar range yang sudah
    ada berlaku apa adanya.

    Konversi harga→bin dilakukan SDK, bukan `log(1+bin_step/1e4)` di Python:
    pembulatan bin saat deposit ditentukan SDK, dan kartu yang memakai
    pembulatan berbeda akan menjanjikan range yang bukan range yang terjadi."""
    st = pool_state(pool)
    qsym, q_is_y = quote_side(st["mint_x"], st["mint_y"])
    px = float(st["price"])                     # harga token_y per token_x
    meme_price = px if q_is_y else (1.0 / px if px else 0.0)
    if meme_price <= 0:
        raise SolanaError("Harga pool tidak terbaca — tidak bisa menghitung range.")
    lo_p = meme_price * (1 - min(99.0, float(low_pct)) / 100.0)
    hi_p = meme_price * (1 + float(up_pct) / 100.0)
    if mode == "lower":        # 100% quote: seluruh range DI BAWAH harga sekarang
        hi_p = meme_price
    elif mode == "upper":      # 100% meme: seluruh range DI ATAS harga sekarang
        lo_p = meme_price
    # Dalam satuan pool (y per x) urutannya terbalik kalau quote = token_x.
    a = lo_p if q_is_y else (1.0 / hi_p)
    b = hi_p if q_is_y else (1.0 / lo_p)
    q = bin_prices(pool, prices=[a, b, px])
    ids = sorted(x["bin_id"] for x in q["query"][:2])
    lower, upper = ids[0], ids[1]
    active = int(q["active_bin"])
    # Mode satu sisi harus benar-benar tidak menyentuh bin aktif; kalau tidak,
    # posisi "100% quote" tetap menarik sisi meme dari wallet.
    if mode == "lower":
        upper = min(upper, active - 1)
    elif mode == "upper":
        lower = max(lower, active + 1)
    if upper < lower:
        lower = upper = active
    n = upper - lower + 1
    if n > MAX_BINS_PER_POSITION:
        # Dipangkas SIMETRIS di sekitar bin aktif, bukan dipotong di satu ujung —
        # memotong satu sisi diam-diam menggeser pusat range yang user pilih.
        half = MAX_BINS_PER_POSITION // 2
        lower, upper = max(lower, active - half), min(upper, active + half - 1)
    return {"pool": pool, "lower_bin": lower, "upper_bin": upper,
            "active_bin": active, "n_bins": upper - lower + 1,
            "bin_step": int(q["bin_step"]), "price": px,
            "price_lower": _price_of_bin(lower, int(q["bin_step"]),
                                         int(q["dec_x"]), int(q["dec_y"])),
            "price_upper": _price_of_bin(upper + 1, int(q["bin_step"]),
                                         int(q["dec_x"]), int(q["dec_y"])),
            "quote_sym": qsym, "quote_is_token1": q_is_y}


def capital(address: str, p: dict) -> float:
    """Modal sisi QUOTE yang benar-benar bisa dipakai, dalam satuan quote.

    Untuk quote SOL, cadangan `gas_reserve` DIPOTONG: biaya tx DAN sewa akun
    posisi (~0,057 SOL, kembali saat close) dibayar dari kantong yang sama, jadi
    "100% saldo" tanpa potongan selalu gagal di langkah terakhir."""
    q_is_y = bool(p["quote_is_token1"])
    qaddr = p["quote_addr"]
    bal = token_balance(address, qaddr)
    if qaddr == SOL_MINT:
        import chain as ch
        bal -= float(ch.CHAINS[SOL_CHAIN].get("gas_reserve") or 0.08)
    return max(0.0, bal)


def meme_balance(address: str, p: dict) -> float:
    """Saldo sisi MEME pool ini, dalam satuan manusia."""
    meme = p["token0"] if p["quote_is_token1"] else p["token1"]
    return token_balance(address, meme)


def plan_mint(pool: str, low_pct: float, up_pct: float, mode: str,
              shape: str, budget: float) -> dict:
    """Rencana deposit LENGKAP tanpa mengirim tx — dipakai kartu konfirmasi.

    `budget` satuannya QUOTE, KECUALI mode `upper` yang satuannya MEME — aturan
    yang sama persis dengan jalur EVM (`budget_sym()`), karena range di atas
    harga sekarang memang tidak bisa menerima quote sama sekali. Mengirimnya
    sebagai quote membuat sisi meme dihitung dari angka yang salah satuan, dan
    terukur menghasilkan setoran 0,0000057 SOL untuk budget "100".

    Komposisi dua sisinya dihitung sidecar lewat `autoFill*ByStrategy`, fungsi
    yang SAMA yang menentukan jumlah tertarik saat deposit — jadi kartu dan
    eksekusi tidak bisa berbeda."""
    rng = range_bins(pool, low_pct, up_pct, mode)
    p = pool_info(pool)
    q_is_y = bool(p["quote_is_token1"])
    qdec = p["dec1"] if q_is_y else p["dec0"]
    mdec = p["dec0"] if q_is_y else p["dec1"]
    if mode == "upper":
        kw = {("amount_x_raw" if q_is_y else "amount_y_raw"): _amt_raw(budget, mdec)}
    else:
        kw = {("amount_y_raw" if q_is_y else "amount_x_raw"): _amt_raw(budget, qdec)}
    lower, upper = rng["lower_bin"], rng["upper_bin"]
    plan = sidecar("quote_add", pool=pool, lower_bin=lower, upper_bin=upper,
                   strategy=shape, **kw)
    # `range_bins` dan `quote_add` membaca bin aktif lewat panggilan RPC yang
    # BERBEDA, dan bin aktif bergerak tiap swap. Kalau ia bergeser di antara
    # keduanya, range mode satu-sisi ikut menyentuh bin aktif lagi — dan kartu
    # "Lower (100% quote)" diam-diam menarik sisi meme dari wallet. Terukur pada
    # SOL/USDC bin step 4 (0,04% per bin): satu blok saja cukup.
    want = "y_only" if mode == "lower" else "x_only" if mode == "upper" else None
    if want and plan.get("side") != want:
        act = int(plan["active_bin"])
        if mode == "lower":
            upper = min(upper, act - 1)
            lower = min(lower, upper)
        else:
            lower = max(lower, act + 1)
            upper = max(upper, lower)
        rng["lower_bin"], rng["upper_bin"] = lower, upper
        rng["active_bin"], rng["n_bins"] = act, upper - lower + 1
        plan = sidecar("quote_add", pool=pool, lower_bin=lower, upper_bin=upper,
                       strategy=shape, **kw)
    x = int(plan["amount_x_raw"]) / 10 ** p["dec0"]
    y = int(plan["amount_y_raw"]) / 10 ** p["dec1"]
    return {**rng, "shape": shape, "side": plan.get("side"),
            "amount_x": x, "amount_y": y,
            "quote_in": (y if q_is_y else x), "meme_in": (x if q_is_y else y),
            "unusable_quote": (int(plan.get("unusable_y_raw") or 0) / 10 ** p["dec1"]
                               if q_is_y else
                               int(plan.get("unusable_x_raw") or 0) / 10 ** p["dec0"]),
            "usd": x * token_usd_price(p["token0"]) + y * token_usd_price(p["token1"]),
            "pool_info": p}


def mint_new(secret: str, pool: str, budget: float, low_pct: float,
             up_pct: float, mode: str = "wide", shape: str = "Spot",
             slippage_pct: float = 5.0) -> dict:
    """Buat posisi DLMM baru. Range + komposisi DIHITUNG ULANG di sini.

    Sengaja tidak memakai angka dari kartu: bin aktif bergerak tiap swap, jadi
    rencana yang dibuat beberapa detik lalu bisa menunjuk bin yang sudah bukan
    bin aktif — dan komposisi dua sisinya ikut salah."""
    plan = plan_mint(pool, low_pct, up_pct, mode, shape, budget)
    p = plan["pool_info"]
    addr = address_of(secret)
    have_q = capital(addr, p)
    if plan["quote_in"] > have_q + 1e-12:
        raise SolanaError(
            f"Saldo {p['quote_sym']} kurang: butuh {plan['quote_in']:.6f}, "
            f"ada {have_q:.6f} di {addr[:6]}…{addr[-4:]} (sudah dipotong cadangan gas).")
    have_m = meme_balance(addr, p)
    msym = p.get("sym0") if plan["quote_is_token1"] else p.get("sym1")
    if plan["meme_in"] > have_m + 1e-12:
        # Tidak ada swap otomatis di jalur ini — lihat catatan di `close_any`.
        raise SolanaError(
            f"Saldo {msym} kurang: range ini butuh {plan['meme_in']:.6f} {msym} "
            f"tapi wallet cuma punya {have_m:.6f}. Beli dulu sisi itu, atau "
            f"pakai mode Lower (100% {p['quote_sym']}).")
    d = sidecar("add_new", secret=secret, pool=pool,
                lower_bin=plan["lower_bin"], upper_bin=plan["upper_bin"],
                amount_x_raw=_amt_raw(plan["amount_x"], p["dec0"]),
                amount_y_raw=_amt_raw(plan["amount_y"], p["dec1"]),
                strategy=shape, slippage_pct=float(slippage_pct),
                priority_micro_lamports=priority_fee())
    return {"steps": _steps(d.get("signatures"), "Mint DLMM"),
            "position": d.get("position"), "pid": f"dlmm:{d.get('position')}",
            "deposited": budget, "deposited_usd": plan["usd"],
            "in_quote": plan["quote_in"], "in_meme": plan["meme_in"],
            "quote_sym": p["quote_sym"], "meme_sym": msym,
            "lower_bin": plan["lower_bin"], "upper_bin": plan["upper_bin"],
            "n_bins": plan["n_bins"], "shape": shape,
            "rent_sol": POSITION_RENT_SOL, "signatures": d.get("signatures")}
