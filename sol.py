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
        # Harga KEDUA sisi ikut dibawa. Data API sudah mengirimnya di payload
        # pool yang sama, jadi mengambilnya lagi lewat `token_usd_price()` berarti
        # satu request per TOKEN per posisi — terukur 10 request tambahan untuk
        # 5 posisi, dan `/list` lintas chain cuma punya anggaran 5 detik total.
        "px0": float(tx.get("price") or 0), "px1": float(ty.get("price") or 0),
        "supply0": float(tx.get("total_supply") or 0),
        "supply1": float(ty.get("total_supply") or 0),
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

    # Harga dari payload pool dulu (sudah ada, nol request); `token_usd_price`
    # hanya untuk pool yang belum diindeks Data API.
    px0 = float(pinfo.get("px0") or 0) or token_usd_price(pinfo["token0"])
    px1 = float(pinfo.get("px1") or 0) or token_usd_price(pinfo["token1"])
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


def _safe_pool_info(pool: str) -> dict | None:
    try:
        return pool_info(pool)
    except Exception:
        return None


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
    idx = portfolio_index(address)
    raws = []
    if idx:
        # SATU panggilan untuk semua pool: tiap panggilan sidecar itu proses Node
        # baru (~0,7–1 detik hanya untuk start + require SDK), dan `/list` lintas
        # chain punya anggaran 5 detik TOTAL. Lima pool = lima kali ongkos itu.
        try:
            d = sidecar("positions_by_key",
                        groups=[{"pool": pool, "positions": keys}
                                for pool, keys in idx])
            raws = d.get("positions") or []
        except SolanaError:
            raws = []
    if not raws:
        d = sidecar("positions", owner=address)
        raws = d.get("positions") or []
    # `pool_info` satu request Data API per POOL. Berurutan itu terukur mendominasi
    # pembacaan dingin (8,0 detik untuk 5 posisi), dan `/list` lintas chain cuma
    # punya anggaran 5 detik TOTAL — jadi Solana selalu tertulis "masih dimuat"
    # pada klik pertama. Diambil paralel; hasilnya di-cache `_api` jadi klik
    # berikutnya tidak membayar lagi.
    pools = list(dict.fromkeys(r["pool"] for r in raws))
    infos: dict = {}
    if pools:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=min(8, len(pools))) as ex:
            for pool, info in zip(pools, ex.map(
                    lambda x: (lambda: _safe_pool_info(x))(), pools)):
                if info:
                    infos[pool] = info
    out = []
    for raw in raws:
        try:
            pinfo = infos.get(raw["pool"]) or pool_info(raw["pool"])
            out.append(_position_detail(raw, pinfo, raw.get("active_bin")))
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
    # `positions_by_key` membaca akun yang ditunjuk saja. Jangan diganti
    # `positions` (tanpa alamat): itu memindai seluruh akun program DLMM dan
    # Alchemy menolaknya 429.
    d = sidecar("positions_by_key", pool=pool, positions=[position])
    cur = next(iter(d.get("positions") or []), None)
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


def jupiter_key() -> str | None:
    return (os.environ.get("JUPITER_API_KEY") or "").strip() or None


def best_quote(p: dict, amount_in: float, swap_for_y: bool,
               slippage_pct: float = 1.0) -> dict:
    """Quote swap TERBAIK: Jupiter (agregator) dulu, pool posisi cadangan.

    Swap komposisi TIDAK boleh jalan di pool posisi sendiri — itu satu venue
    tipis. Terukur pada WOJAK/SOL untuk jumlah yang sama persis:

    | WOJAK | pool posisi | Jupiter | selisih |
    |---|---|---|---|
    | 5.000 | 2,96% impact | 0,00% | +6,1% SOL |
    | 20.000 | 7,18% | 0,00% | +9,3% |
    | 33.290 | 9,97% | 0,24% | +12,3% |
    | 52.026 | 13,28% | 0,26% | **+16,5%** |

    Bahkan saat Jupiter tetap lewat Meteora DLMM ia menang, karena ia memilih
    pool DLMM yang lebih DALAM — bukan pool posisi. Pelajaran yang sama persis
    dengan "swap v4 dirutekan ke pool TERBAIK, bukan pool posisi" di EVM.

    Kegagalan Jupiter TIDAK membatalkan swap: jalur pool selalu jadi cadangan,
    sama seperti "kegagalan routing tidak boleh membatalkan swap" di `v4_swap`."""
    mint_in = p["token0"] if swap_for_y else p["token1"]
    mint_out = p["token1"] if swap_for_y else p["token0"]
    dec_in = p["dec0"] if swap_for_y else p["dec1"]
    dec_out = p["dec1"] if swap_for_y else p["dec0"]
    try:
        d = sidecar("jup_quote", input_mint=mint_in, output_mint=mint_out,
                    amount_in_raw=_amt_raw(amount_in, dec_in),
                    slippage_bps=int(round(float(slippage_pct) * 100)),
                    max_accounts=50, jupiter_api_key=jupiter_key())
        q = d.get("quote") or {}
        out = int(q.get("outAmount") or 0)
        if out > 0:
            jup = {"venue": "jupiter", "quote": q,
                   "amount_out": out / 10 ** dec_out,
                   "min_out": int(q.get("otherAmountThreshold") or 0) / 10 ** dec_out,
                   # `priceImpactPct` Jupiter itu PECAHAN ("0.0264" = 2,64%),
                   # bukan persen — dikalikan 100 lagi akan menampilkan 264%.
                   "impact": float(q.get("priceImpactPct") or 0),
                   "route": "+".join(dict.fromkeys(
                       (x.get("swapInfo") or {}).get("label", "?")
                       for x in q.get("routePlan") or []))}
            # Jupiter mengoptimalkan HASIL, bukan impact, dan untuk jumlah kecil
            # ia kadang memilih satu rute sederhana yang justru tipis. Pool
            # posisi baru dibandingkan kalau impact-nya masih di atas 1% —
            # supaya kartu tidak membayar dua quote untuk kasus yang lazim.
            if jup["impact"] <= 0.01:
                return jup
            try:
                pq = swap_quote(p["pool"], amount_in, swap_for_y, slippage_pct)
                pool_out = int(pq.get("amount_out_raw") or 0) / 10 ** dec_out
                if pool_out > jup["amount_out"]:
                    return {"venue": "pool", "quote": None,
                            "amount_out": pool_out,
                            "min_out": int(pq.get("min_amount_out_raw") or 0) / 10 ** dec_out,
                            "impact": float(pq.get("price_impact") or 0) / 100.0,
                            "route": "Meteora DLMM (pool posisi)"}
            except Exception:
                pass
            return jup
    except Exception:
        pass
    q = swap_quote(p["pool"], amount_in, swap_for_y, slippage_pct)
    return {"venue": "pool", "quote": None,
            "amount_out": int(q.get("amount_out_raw") or 0) / 10 ** dec_out,
            "min_out": int(q.get("min_amount_out_raw") or 0) / 10 ** dec_out,
            "impact": float(q.get("price_impact") or 0) / 100.0,
            "route": "Meteora DLMM (pool posisi)"}


def best_swap(secret: str, p: dict, amount_in: float, swap_for_y: bool,
              slippage_pct: float, q: dict | None = None) -> dict:
    """Eksekusi swap lewat venue terbaik. `q` = hasil `best_quote` yang sudah
    dihitung, supaya rutenya sama dengan yang ditampilkan di kartu."""
    q = q or best_quote(p, amount_in, swap_for_y, slippage_pct)
    dec_out = p["dec1"] if swap_for_y else p["dec0"]
    if q["venue"] == "jupiter":
        d = sidecar("jup_swap", secret=secret, quote=q["quote"],
                    priority_micro_lamports=priority_fee(),
                    jupiter_api_key=jupiter_key())
        return {"signatures": d.get("signatures"),
                "got": int(d.get("amount_out_raw") or 0) / 10 ** dec_out,
                "impact": float(d.get("price_impact") or 0) / 100.0,
                "route": d.get("route") or "Jupiter"}
    d = swap(secret, p["pool"], amount_in, swap_for_y, slippage_pct, priority_fee())
    return {"signatures": d.get("signatures"),
            "got": int(d.get("amount_out_raw") or 0) / 10 ** dec_out,
            "impact": float(d.get("price_impact") or 0) / 100.0,
            "route": "Meteora DLMM (pool posisi)"}


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
_PRIORITY_DEFAULT = 20_000      # µlamport per compute unit (~0,000004 SOL/tx)
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

    Default `_PRIORITY_DEFAULT`, bukan 0. Tanpa priority fee, tx DLMM
    tertinggal dan blockhash-nya kedaluwarsa sebelum masuk chain — terukur
    menggagalkan rebalance di tengah alur. Ongkosnya dapat diabaikan: 20.000
    µlamport/CU × ~200k CU = 4.000 lamport = **0,000004 SOL** per tx.
    `SOLANA_PRIORITY_FEE=0` mematikannya."""
    raw = (os.environ.get("SOLANA_PRIORITY_FEE") or "").strip()
    if not raw:
        return _PRIORITY_DEFAULT
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return _PRIORITY_DEFAULT


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
    cur = sidecar("positions_by_key", pool=pool, positions=[position])
    raw = next(iter(cur.get("positions") or []), None)
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


# ══════════════════════ Rebalance & compound (in-place) ══════════════════════
# Keduanya memakai jalur `rebalancePosition` SDK, yang bekerja DI TEMPAT: akun
# posisinya tidak ditutup, jadi sewa ~0,057 SOL tidak dilepas lalu dibayar lagi,
# tidak ada posisi baru yang lahir, dan `pid`-nya tetap sehingga riwayat PnL
# tidak terputus. Ini beda mendasar dari jalur EVM yang harus close lalu mint.


def _rb_amounts(d: dict) -> dict:
    """Angka manusia dari hasil simulasi/eksekusi rebalance.

    TIGA besaran yang berbeda dan gampang tertukar — menukarnya membuat kartu
    melapor "0 masuk" untuk rebalance yang sebenarnya menyetor ulang penuh:

    - `in_position` — yang MENDARAT di bin (pokok lama + fee + tambahan)
    - `from_wallet` — kekurangan yang ditambal DARI wallet
    - `to_wallet`   — sisa yang KEMBALI ke wallet

    Diturunkan dari sumber SDK: `actualAmount*Deposited = max(0, deposit −
    withdrawn)` dan `actualLiquidityAndFee*Withdrawn = max(0, withdrawn −
    deposit)`."""
    dx, dy = int(d.get("dec_x") or 0), int(d.get("dec_y") or 0)

    def g(k, dec):
        return int(d.get(k) or 0) / 10 ** dec

    return {
        "in0": g("in_position_x_raw", dx), "in1": g("in_position_y_raw", dy),
        "from0": g("from_wallet_x_raw", dx), "from1": g("from_wallet_y_raw", dy),
        "to0": g("to_wallet_x_raw", dx), "to1": g("to_wallet_y_raw", dy),
        "rent_sol": int(d.get("rental_lamports") or 0) / 1e9,
    }


def compound_plan(address: str, position: str, shape: str = "Spot") -> dict:
    """Simulasi compound TANPA mengirim tx."""
    pool = pool_of_position(position)
    d = sidecar("compound", pool=pool, position=position, owner=address,
                strategy=shape, dry=True)
    return {**d, "pool_info": pool_info(pool), "amt": _rb_amounts(d)}


def compound(secret: str, position: str, shape: str = "Spot",
             slippage_pct: float = 5.0) -> dict:
    """Klaim fee lalu setor kembali ke RANGE YANG SAMA.

    Beda dari `rebalance`: bin batasnya TIDAK bergeser — deposit dinyatakan
    sebagai delta terhadap bin aktif (`lower−active … upper−active`), jadi
    rangenya persis sama. Yang masuk cuma fee; pokoknya tidak disentuh."""
    pool = pool_of_position(position)
    d = sidecar("compound", secret=secret, pool=pool, position=position,
                strategy=shape, slippage_pct=float(slippage_pct),
                priority_micro_lamports=priority_fee())
    return {**d, "pool_info": pool_info(pool), "amt": _rb_amounts(d)}


def compound_bridge(secret: str, position: str, shape: str = "Spot",
                    slippage_pct: float = 5.0) -> dict:
    """Jembatan `chain.compound_any` untuk DLMM."""
    r = compound(secret, position, shape=shape, slippage_pct=slippage_pct)
    a, p = r["amt"], r["pool_info"]
    px0, px1 = token_usd_price(p["token0"]), token_usd_price(p["token1"])
    dx, dy = p["dec0"], p["dec1"]
    f0 = int(r.get("fee_x_raw") or 0) / 10 ** dx
    f1 = int(r.get("fee_y_raw") or 0) / 10 ** dy
    return {"steps": _steps(r.get("signatures"), "Compound DLMM"),
            "pid": f"dlmm:{position}", "position": position,
            "lower_bin": r.get("lower_bin"), "upper_bin": r.get("upper_bin"),
            "fees0": f0, "fees1": f1, "fee_usd": f0 * px0 + f1 * px1,
            "added_usd": a["in0"] * px0 + a["in1"] * px1,
            "amt": a, "pool_info": p, "signatures": r.get("signatures")}


# ══════════════════ Rebalance: close → swap komposisi → mint ══════════════════
# Alurnya SENGAJA sama dengan jalur EVM (`chain.rebalance_position`), bukan
# `rebalancePosition` bawaan SDK. SDK itu mempertahankan lebar range dan selalu
# memusatkannya di bin aktif — jadi ia cuma bisa meniru mode `wide`, dan mode
# Lower/Upper (satu sisi) mustahil dinyatakan di sana. Mode itu justru yang
# paling sering dipakai: Lower menampung harga turun dengan 100% quote, Upper
# menjual naik dengan 100% meme.


def rebalance_bins(width: int, active: int, mode: str, quote_is_y: bool
                   ) -> tuple[int, int]:
    """Bin batas baru: LEBAR SAMA, diletakkan sesuai mode terhadap bin aktif.

    Sisi mana yang memegang quote TIDAK tetap. Di DLMM bin **di bawah** bin
    aktif memegang token Y dan bin **di atas** memegang token X. Jadi "Lower =
    100% quote" berarti di bawah bin aktif kalau quote itu token_y, tapi di
    ATAS bin aktif kalau quote itu token_x. Menebaknya dari nama mode saja akan
    menghasilkan posisi 100% MEME untuk user yang meminta 100% quote."""
    w = max(1, int(width))
    quote_below = bool(quote_is_y)       # quote ada di bin bawah?
    if mode == "lower":                  # 100% quote
        below = quote_below
    elif mode == "upper":                # 100% meme
        below = not quote_below
    else:                                # wide/stable/same → dua sisi, terpusat
        lo = int(active) - w // 2
        return lo, lo + w - 1
    if below:
        hi = int(active) - 1
        return hi - w + 1, hi
    lo = int(active) + 1
    return lo, lo + w - 1


def _wallet_pair(address: str, p: dict) -> tuple[float, float]:
    """(saldo token_x, saldo token_y) yang benar-benar bisa dipakai.

    Sisi yang kebetulan SOL dipotong cadangan gas: fee tx dan sewa akun posisi
    baru dibayar dari kantong yang sama, jadi memakai saldo penuh membuat mint
    gagal justru di langkah terakhir."""
    import chain as ch
    res = float(ch.CHAINS[SOL_CHAIN].get("gas_reserve") or 0.08)
    b0 = token_balance(address, p["token0"])
    b1 = token_balance(address, p["token1"])
    if p["token0"] == SOL_MINT:
        b0 = max(0.0, b0 - res)
    if p["token1"] == SOL_MINT:
        b1 = max(0.0, b1 - res)
    return b0, b1


def _plan_pair(pool: str, lower: int, upper: int, shape: str,
               p: dict, x: float | None = None, y: float | None = None) -> dict:
    """quote_add dengan salah satu sisi diberikan; kembalikan (x, y) manusia."""
    kw = ({"amount_x_raw": _amt_raw(x, p["dec0"])} if x is not None
          else {"amount_y_raw": _amt_raw(y, p["dec1"])})
    d = sidecar("quote_add", pool=pool, lower_bin=lower, upper_bin=upper,
                strategy=shape, **kw)
    return {"x": int(d["amount_x_raw"]) / 10 ** p["dec0"],
            "y": int(d["amount_y_raw"]) / 10 ** p["dec1"],
            "side": d.get("side"), "active_bin": int(d["active_bin"])}


def _swap_guarded(secret: str, p: dict, amount: float, swap_for_y: bool,
                  slippage_pct: float, max_impact: float, steps: list,
                  out: dict) -> dict:
    """Swap komposisi dengan penjagaan price impact.

    `minOut` TIDAK melindungi dari price impact — quoter sudah memasukkannya,
    jadi swap yang menggerakkan harga pool berapa pun tetap "sesuai quote" dan
    tidak pernah gagal. Aturan yang sama dengan `v4_swap` di jalur EVM, dan di
    sini justru lebih perlu: mode Lower/Upper menjual SELURUH satu sisi, dan di
    pool tipis itu bisa puluhan persen. Terukur pada WOJAK/SOL: menjual 52.026
    WOJAK memberi impact **21,99%** — ~$10 dari $46."""
    q = best_quote(p, amount, swap_for_y, slippage_pct)
    imp = float(q["impact"])
    if max_impact is not None and imp > max_impact:
        raise SolanaError(
            f"Swap komposisi price impact {imp * 100:.1f}% lewat {q['route']} "
            f"(batas {max_impact * 100:.0f}%) — likuiditasnya terlalu tipis untuk "
            f"menukar sebanyak itu sekaligus. Pilih mode Wide (tidak perlu menjual "
            f"habis satu sisi), atau naikkan batas impact di /settings.")
    r = best_swap(secret, p, amount, swap_for_y, slippage_pct, q)
    steps += _steps(r.get("signatures"),
                    f"Swap komposisi lewat {r['route']} ({r['impact'] * 100:.2f}% impact)")
    out["impact"] = max(float(out.get("impact") or 0), r["impact"])
    out["route"] = r["route"]
    return {"amount_out_raw": _amt_raw(
        r["got"], p["dec1"] if swap_for_y else p["dec0"])}


def _compose(secret: str, pool: str, p: dict, lower: int, upper: int,
             shape: str, have_x: float, have_y: float, slippage_pct: float,
             steps: list, max_impact: float | None = None,
             out: dict | None = None) -> tuple[float, float]:
    """Tukar sebagian sisi supaya komposisinya pas untuk range ini.

    Swapnya DI POOL POSISI ITU SENDIRI, sama seperti swap komposisi jalur EVM.
    Rasio deposit untuk sebuah range+shape itu TETAP, dan `autoFill*` linear
    terhadap jumlah — jadi satu probe sudah cukup: dari (y0, x0) hasil probe,
    faktor skalanya `k = nilai_total / nilai_probe`, dan target tiap sisi
    `k × sisi_probe`. Tanpa penskalaan ini, sisa yang tidak terpakai bisa
    puluhan persen dari modal."""
    pr = float(pool_state(pool)["price"])          # token_y per token_x
    if pr <= 0:
        return have_x, have_y

    def val(x, y):                                  # nilai total dalam satuan Y
        return x * pr + y

    probe = _plan_pair(pool, lower, upper, shape, p,
                       y=have_y if have_y > 0 else None,
                       x=None if have_y > 0 else have_x)
    out = out if out is not None else {}
    if probe["side"] == "y_only":
        # Range butuh Y saja: seluruh X ditukar.
        if have_x > 0:
            r = _swap_guarded(secret, p, have_x, True, slippage_pct,
                              max_impact, steps, out)
            have_y += int(r.get("amount_out_raw") or 0) / 10 ** p["dec1"]
            have_x = 0.0
        return have_x, have_y
    if probe["side"] == "x_only":
        if have_y > 0:
            r = _swap_guarded(secret, p, have_y, False, slippage_pct,
                              max_impact, steps, out)
            have_x += int(r.get("amount_out_raw") or 0) / 10 ** p["dec0"]
            have_y = 0.0
        return have_x, have_y

    vp = val(probe["x"], probe["y"])
    if vp <= 0:
        return have_x, have_y
    k = val(have_x, have_y) / vp
    want_x, want_y = probe["x"] * k, probe["y"] * k
    if want_x > have_x:
        need_y = (want_x - have_x) * pr
        amt = min(need_y, have_y)
        if amt > 0:
            r = _swap_guarded(secret, p, amt, False, slippage_pct,
                              max_impact, steps, out)
            have_x += int(r.get("amount_out_raw") or 0) / 10 ** p["dec0"]
            have_y -= amt
    elif want_y > have_y:
        need_x = (want_y - have_y) / pr
        amt = min(need_x, have_x)
        if amt > 0:
            r = _swap_guarded(secret, p, amt, True, slippage_pct,
                              max_impact, steps, out)
            have_y += int(r.get("amount_out_raw") or 0) / 10 ** p["dec1"]
            have_x -= amt
    return have_x, have_y


def rebalance_impact(address: str, position: str, mode: str) -> float | None:
    """Perkiraan price impact swap komposisi untuk mode ini — TANPA tx.

    Ditampilkan di kartu SEBELUM user menekan tombol, sama seperti
    `swap_impact_v4()` di jalur EVM. Mode Wide tidak menjual habis satu sisi,
    jadi perkiraannya cuma dihitung untuk Lower/Upper."""
    if mode not in ("lower", "upper"):
        return None
    try:
        pool = pool_of_position(position)
        p = pool_info(pool)
        d = sidecar("positions_by_key", pool=pool, positions=[position])
        raw = next(iter(d.get("positions") or []), None)
        if not raw:
            return None
        q_is_y = bool(p["quote_is_token1"])
        # Lower = 100% quote → sisi MEME yang dijual; Upper sebaliknya.
        sell_x = (mode == "lower") == q_is_y
        dec = p["dec0"] if sell_x else p["dec1"]
        amt = ((int(raw["amount_x_raw"]) + int(raw["fee_x_raw"])) if sell_x
               else (int(raw["amount_y_raw"]) + int(raw["fee_y_raw"]))) / 10 ** dec
        if amt <= 0:
            return None
        return float(best_quote(p, amt, sell_x, 5.0)["impact"])
    except Exception:
        return None


def width_choices(bin_step: int, old_width: int) -> list[tuple[int, str]]:
    """[(lebar_bin, label)] untuk kartu rebalance DLMM.

    Lebar dinyatakan dalam BIN, dan lebar yang sama berarti rentang harga yang
    sangat berbeda tergantung `bin_step`: satu bin = `bin_step/100` persen. 125
    bin di pool bin step 100 itu rentang **3,4×** — likuiditasnya tersebar
    setipis 0,0076 SOL per bin dan praktis tidak menghasilkan apa-apa sampai
    harga bergerak jauh. Itu yang terjadi pada rebalance WOJAK/SOL pertama."""
    step = max(1, int(bin_step)) / 100.0          # persen per bin
    out, seen = [], set()
    for n, tag in ((1, "🎯 1 kotak"), (5, "rapat"), (20, "sedang"),
                   (int(old_width), "lebar lama")):
        n = max(1, min(MAX_BINS_PER_POSITION, int(n)))
        if n in seen:
            continue
        seen.add(n)
        span = (1.0 + step / 100.0) ** n - 1.0
        out.append((n, f"{tag} · {n} bin (~{span * 100:.1f}%)"))
    return out


def rebalance_any(secret: str, position: str, mode: str = "wide",
                  shape: str = "Spot", slippage_pct: float = 5.0,
                  max_impact: float | None = 0.25,
                  width_bins: int | None = None) -> dict:
    """Close posisi → swap komposisi sesuai mode → mint ulang dengan LEBAR range
    yang sama, diletakkan menurut mode terhadap harga sekarang.

    Alur dan artinya sama persis dengan `chain.rebalance_position` di EVM:

    - `wide`/`stable` — dua sisi, dipusatkan di harga sekarang
    - `lower` — 100% quote, seluruh range di sisi yang memegang quote
    - `upper` — 100% meme, seluruh range di sisi yang memegang meme

    **Hanya dana HASIL posisi ini yang dipakai**, bukan seluruh saldo wallet —
    aturan yang sama dengan jalur EVM. Jumlahnya diambil dari snapshot posisi
    tepat sebelum close (pokok + fee), lalu dijepit ke saldo NYATA supaya
    selisih pembulatan tidak membuat mint meminta lebih dari yang ada."""
    addr = address_of(secret)
    pool = pool_of_position(position)
    p = pool_info(pool)
    q_is_y = bool(p["quote_is_token1"])

    cur = sidecar("positions_by_key", pool=pool, positions=[position])
    raws = cur.get("positions") or []
    if not raws:
        raise SolanaError("Posisi tidak ditemukan (sudah ditutup?).")
    old = raws[0]
    old_lo, old_hi = int(old["lower_bin"]), int(old["upper_bin"])
    old_width = old_hi - old_lo + 1
    # Lebar range BOLEH diganti di sini, dan itu perbedaan nyata dari jalur EVM.
    # Di Uniswap lebar range = rentang harga; di DLMM ia jumlah BIN, dan
    # mempertahankan jumlah bin yang sama saat memindahkan seluruh range ke satu
    # sisi menghasilkan tangga yang jauh lebih dalam daripada posisi semula
    # (terukur WOJAK/SOL: 125 bin dua sisi jadi 125 bin satu sisi = rentang
    # 3,4× dengan 0,0076 SOL per bin).
    width = max(1, min(MAX_BINS_PER_POSITION,
                       int(width_bins) if width_bins else old_width))

    steps: list = []
    before_x, before_y = _wallet_pair(addr, p)
    cl = close(secret, pool, position, priority=priority_fee())
    steps += _steps(cl.get("signatures"), "Close DLMM")
    snap = cl.get("before") or {}
    # Yang keluar dari posisi = pokok + fee. Dipakai sebagai BATAS ATAS, lalu
    # dijepit ke selisih saldo nyata: pembacaan snapshot dan hasil close bisa
    # berbeda beberapa wei, dan meminta lebih dari yang ada = mint gagal.
    out_x = (int(snap.get("amount_x_raw") or 0) + int(snap.get("fee_x_raw") or 0)) / 10 ** p["dec0"]
    out_y = (int(snap.get("amount_y_raw") or 0) + int(snap.get("fee_y_raw") or 0)) / 10 ** p["dec1"]
    after_x, after_y = _wallet_pair(addr, p)
    got_x = min(out_x, max(0.0, after_x - before_x)) if after_x > before_x else out_x
    got_y = min(out_y, max(0.0, after_y - before_y)) if after_y > before_y else out_y
    got_x, got_y = min(got_x, after_x), min(got_y, after_y)
    if got_x <= 0 and got_y <= 0:
        raise SolanaError(
            "Hasil close terbaca 0 — dananya aman di wallet, cek /wallet lalu "
            "buat posisi baru manual.")

    st = pool_state(pool)
    active = int(st["active_bin"])
    lo, hi = rebalance_bins(width, active, mode, q_is_y)
    swp: dict = {}
    got_x, got_y = _compose(secret, pool, p, lo, hi, shape, got_x, got_y,
                            slippage_pct, steps, max_impact, swp)

    # Sisi PROBE dipilih dari sisi mana yang BERISI, bukan dari nama mode.
    # Setelah swap komposisi, mode satu sisi menyisakan dana hanya di satu sisi
    # — dan sisi mana itu bergantung orientasi quote. Memilihnya dari mode
    # membuat "Lower" pada pool ber-quote token_x memprobe sisi yang kosong,
    # autoFill mengembalikan 0, dan posisi barunya lahir TANPA dana.
    def _probe(lo_, hi_):
        return (_plan_pair(pool, lo_, hi_, shape, p, y=got_y) if got_y > 0
                else _plan_pair(pool, lo_, hi_, shape, p, x=got_x))

    # Bin aktif bisa bergeser oleh swap komposisi itu sendiri — mode satu sisi
    # harus tetap tidak menyentuh bin aktif, jadi letaknya dihitung ULANG.
    plan = _probe(lo, hi)
    if plan["active_bin"] != active:
        active = plan["active_bin"]
        lo, hi = rebalance_bins(width, active, mode, q_is_y)
        plan = _probe(lo, hi)
    # Kalau sisi lawan yang diminta melebihi yang ada, rencananya disusun ULANG
    # dari sisi yang membatasi. Memotongnya begitu saja (`min`) merusak RASIO
    # kedua sisi, dan rasio itulah yang menentukan bentuk posisinya.
    if plan["x"] > got_x + 1e-12:
        plan = _plan_pair(pool, lo, hi, shape, p, x=got_x)
    elif plan["y"] > got_y + 1e-12:
        plan = _plan_pair(pool, lo, hi, shape, p, y=got_y)
    dep_x, dep_y = min(plan["x"], got_x), min(plan["y"], got_y)
    if dep_x <= 0 and dep_y <= 0:
        raise SolanaError(
            "Komposisi hasil close tidak muat di range baru — dananya aman di "
            "wallet, cek /wallet lalu buat posisi baru manual.")

    d = sidecar("add_new", secret=secret, pool=pool, lower_bin=lo, upper_bin=hi,
                amount_x_raw=_amt_raw(dep_x, p["dec0"]),
                amount_y_raw=_amt_raw(dep_y, p["dec1"]),
                strategy=shape, slippage_pct=float(slippage_pct),
                priority_micro_lamports=priority_fee())
    steps += _steps(d.get("signatures"), "Mint DLMM")
    px0, px1 = token_usd_price(p["token0"]), token_usd_price(p["token1"])
    return {"steps": steps, "mode": mode, "shape": shape,
            "pid": f"dlmm:{d.get('position')}", "position": d.get("position"),
            "old_position": position, "old_lower": old_lo, "old_upper": old_hi,
            "lower_bin": lo, "upper_bin": hi, "active_bin": active,
            "n_bins": hi - lo + 1, "width": width, "old_width": old_width,
            "closed_usd": (out_x * px0) + (out_y * px1),
            "in0": dep_x, "in1": dep_y,
            "added_usd": dep_x * px0 + dep_y * px1,
            "left0": max(0.0, got_x - dep_x), "left1": max(0.0, got_y - dep_y),
            "swap_impact": swp.get("impact"),
            "pool_info": p, "signatures": d.get("signatures")}
