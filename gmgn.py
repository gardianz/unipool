"""Scanner token trending lewat GMGN OpenAPI.

Port dari lp-scanner (Node) ke dalam bot ini supaya jadi satu repo, satu proses,
satu deploy — tanpa file jembatan dan tanpa mesin cooldown/Telegram kembar.

**Lima aturan di bawah bukan preferensi gaya.** Melanggarnya membuat bot memberi
jawaban yang salah soal uang, dan semuanya berasal dari kejadian nyata di proyek
asalnya:

1. **Nilai kosong BUKAN nol.** `null`, `""`, dan `-1` berarti "belum diketahui".
   Menyamakannya dengan 0 membuat "belum diuji" tampil sebagai "pajak 0% • bukan
   honeypot • wewenang sudah dilepas" — kekosongan dibungkus jadi kabar baik.
   `_num()` dan `_tri()` yang menjaganya.
2. **Field keamanan beda per chain.** Solana pakai `renounced_mint` /
   `renounced_freeze_account`; EVM pakai `is_renounced` / `is_open_source`.
   Membaca silang membuat token normal terlihat berisiko, atau sebaliknya.
3. **Jangan mengarang angka yang tidak diberikan GMGN.** `pool.fee_ratio` terukur
   `0.1` sementara `trade_fee / volume_24h` pada token yang sama 0,0074% — beda
   ~13× dan unitnya tidak terdokumentasi. Tidak dipakai.
4. **Label GMGN diteruskan apa adanya.** `insider`, `bundler`, `entrapment`,
   `sniper` itu klasifikasi GMGN yang metodenya tidak dipublikasi — ditampilkan
   dengan sumbernya, tidak diubah jadi vonis "aman"/"scam".
5. **Rate limit nyata dan bannya per-IP.** Tiap retry MENAMBAH ban 5 detik sampai
   5 menit, jadi retry cepat memperburuk keadaan. Hormati `reset_at`.

**API key TIDAK boleh lewat proxy.** `chain._cf_request` sengaja tidak dipakai di
sini: jalur itu meneruskan request ke operator proxy pihak ketiga, dan header
`X-APIKEY` ikut terbawa. Untuk data pool publik itu tidak masalah; untuk
kredensial, itu membocorkannya.
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

import requests

log = logging.getLogger(__name__)

HOST = "https://openapi.gmgn.ai"
USER_AGENT = "unipool-scanner/1.0"
# Demo key publik dari GMGNAI/gmgn-skills — read-only dan rate limit-nya jauh
# lebih ketat. Untuk smoke test saja; key pribadi gratis di https://gmgn.ai/ai.
DEMO_KEY = "gmgn_solbscbaseethmonadtron"

CHAINS = ("sol", "bsc", "eth", "base", "arbitrum", "hyperevm", "robinhood", "arc", "stable")
INTERVALS = ("1m", "5m", "1h", "6h", "24h")
EVM_CHAINS = frozenset({"bsc", "eth", "base", "arbitrum", "hyperevm"})


class RateLimit(RuntimeError):
    """429 / RATE_LIMIT_BANNED. `reset_at` = epoch detik, boleh None."""

    def __init__(self, reset_at=None):
        jam = time.strftime("%H:%M:%S", time.localtime(reset_at)) if reset_at else "tidak diketahui"
        super().__init__(f"GMGN rate limit (RATE_LIMIT_BANNED), reset ~{jam}. "
                         f"Jangan retry sebelum itu — tiap retry menambah ban 5 detik.")
        self.reset_at = reset_at


class ApiError(RuntimeError):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body

    @property
    def fatal(self) -> bool:
        """Kredensial salah tidak akan sembuh dengan menunggu — jangan di-backoff."""
        if self.status in (401, 403):
            return True
        code = str((self.body or {}).get("error", "")) if isinstance(self.body, dict) else ""
        return bool(re.search(r"AUTH|KEY_INVALID|FORBIDDEN|SIGNATURE", code + " " + str(self), re.I))


def api_key(demo: bool = False) -> str | None:
    """`GMGN_API_KEY` dari env, lalu `~/.config/gmgn/.env` sebagai cadangan.

    Dari file gmgn-cli itu **hanya** `GMGN_API_KEY` yang diambil —
    `GMGN_PRIVATE_KEY` di sana milik jalur swap bertanda tangan dan tidak boleh
    masuk ke jalur baca-saja ini."""
    k = (os.environ.get("GMGN_API_KEY") or "").strip()
    if k:
        return k
    try:
        for ln in (Path.home() / ".config/gmgn/.env").read_text().splitlines():
            if ln.strip().startswith("GMGN_API_KEY="):
                v = ln.split("=", 1)[1].strip().strip("\"'")
                if v:
                    return v
    except OSError:
        pass
    return DEMO_KEY if demo else None


def _unwrap(body):
    """Respons terbungkus dua lapis: {code, data:{code, data:{...}}}."""
    node = body
    while isinstance(node, dict) and "code" in node and "data" in node:
        if node["code"] not in (0, 200):
            raise ApiError(node.get("msg") or node.get("error") or f"GMGN error code {node['code']}",
                           None, node)
        node = node["data"]
    return node


class Client:
    """Klien GMGN dengan throttle. Aman dipakai dari beberapa thread."""

    def __init__(self, key: str, timeout: float = 20.0, min_interval: float = 1.0):
        self.key = key
        self.timeout = timeout
        # Dokumen menyebut 20 request/detik, tapi terukur ledakan beruntun memicu
        # ban jauh sebelum angka itu — dan ban memakan MENIT sementara jeda ini
        # hitungan detik.
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()
        self._s = requests.Session()
        self._s.headers.update({"X-APIKEY": key, "Content-Type": "application/json",
                                "User-Agent": USER_AGENT})

    def get(self, path: str, **query):
        with self._lock:
            wait = self._last + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        q = {k: v for k, v in query.items() if v not in (None, "")}
        # Server memvalidasi timestamp dalam ±5 detik dan menolak client_id yang
        # diulang dalam 7 detik.
        q["timestamp"] = int(time.time())
        q["client_id"] = str(uuid.uuid4())
        r = self._s.get(HOST + path, params=q, timeout=self.timeout)
        try:
            body = r.json() if r.text else None
        except ValueError:
            raise ApiError(f"Respons bukan JSON (HTTP {r.status_code}): {r.text[:200]}",
                           r.status_code, r.text)
        if r.status_code == 429 or (isinstance(body, dict) and body.get("error") == "RATE_LIMIT_BANNED"):
            hdr = r.headers.get("x-ratelimit-reset")
            reset = (body or {}).get("reset_at") if isinstance(body, dict) else None
            if reset is None and hdr and hdr.isdigit():
                reset = int(hdr)
            raise RateLimit(reset)
        if not r.ok:
            raise ApiError((body or {}).get("msg") or (body or {}).get("error")
                           or f"HTTP {r.status_code}", r.status_code, body)
        return _unwrap(body)

    def rank(self, chain: str, interval: str, extra: dict | None = None) -> list:
        d = self.get("/v1/market/rank", chain=chain, interval=interval, **(extra or {}))
        return (d or {}).get("rank") or []

    def token_info(self, chain: str, address: str) -> dict:
        return self.get("/v1/token/info", chain=chain, address=address) or {}


# ---------- Normalisasi field mentah ----------
def _num(v):
    """Angka, atau None kalau BELUM DIKETAHUI. Jangan pernah mengembalikan 0."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _tri(v):
    """True / False / None. `-1` dari GMGN berarti BELUM DIUJI, bukan aman."""
    if v is True:
        return True
    if v is False:
        return False
    n = _num(v)
    if n is None or n == -1:
        return None
    return n == 1


_IV_KEY = {"1m": "price_change_percent1m", "5m": "price_change_percent5m",
           "1h": "price_change_percent1h"}


def normalize(raw: dict, chain: str, interval: str) -> dict:
    created = _num(raw.get("creation_timestamp")) or _num(raw.get("open_timestamp")) or 0
    buys, sells = _num(raw.get("buys")), _num(raw.get("sells"))
    total = buys + sells if buys is not None and sells is not None else None
    volume, liq = _num(raw.get("volume")), _num(raw.get("liquidity"))
    evm = chain in EVM_CHAINS
    return {
        "chain": chain, "interval": interval,
        "address": raw.get("address"),
        "symbol": raw.get("symbol") or raw.get("name") or "?",
        "name": raw.get("name") or "",
        "volume": volume,
        "marketCap": _num(raw.get("market_cap")),
        "liquidity": liq,                       # jumlah DUA sisi cadangan
        "turnover": (volume / liq) if volume is not None and liq else None,
        # 0 dari GMGN berarti TIDAK DIKETAHUI, bukan 1 Januari 1970 — menghitung
        # umur dari nilai itu menghasilkan "token berumur 56 tahun".
        "ageSeconds": max(0, int(time.time() - created)) if created > 0 else None,
        "priceChangePercent": _num(raw.get(_IV_KEY.get(interval, ""))) if _IV_KEY.get(interval) else None,
        "price": _num(raw.get("price")),
        "swaps": _num(raw.get("swaps")) if _num(raw.get("swaps")) is not None else total,
        "buys": buys, "sells": sells,
        "sellShare": (sells / total) if total else None,
        "sellShareSource": "jumlah swap",
        "holderCount": _num(raw.get("holder_count")),
        "top10HolderRate": _num(raw.get("top_10_holder_rate")),
        "rugRatio": _num(raw.get("rug_ratio")),
        "insiderRate": _num(raw.get("insider_rate")),
        "bundlerRate": _num(raw.get("bundler_rate")),
        "botDegenRate": _num(raw.get("bot_degen_rate")),
        "botDegenCount": _num(raw.get("bot_degen_count")),
        "entrapmentRatio": _num(raw.get("entrapment_ratio")),
        "sniperCount": _num(raw.get("sniper_count")),
        "top70SniperHoldRate": _num(raw.get("top70_sniper_hold_rate")),
        "devTeamHoldRate": _num(raw.get("dev_team_hold_rate")),
        "smartDegenCount": _num(raw.get("smart_degen_count")),
        "renownedCount": _num(raw.get("renowned_count")),
        "isWashTrading": raw.get("is_wash_trading") if isinstance(raw.get("is_wash_trading"), bool) else None,
        "isHoneypot": _tri(raw.get("is_honeypot")),
        # Field keamanan BEDA per chain — membaca silang menghasilkan vonis palsu.
        "renouncedMint": None if evm else _tri(raw.get("renounced_mint")),
        "renouncedFreeze": None if evm else _tri(raw.get("renounced_freeze_account")),
        "isRenounced": _tri(raw.get("is_renounced")) if evm else None,
        "isOpenSource": _tri(raw.get("is_open_source")) if evm else None,
        "buyTax": _num(raw.get("buy_tax")),
        "sellTax": _num(raw.get("sell_tax")),
        "creatorTokenStatus": raw.get("creator_token_status") or None,
        "creatorClose": raw.get("creator_close") is True or raw.get("creator_token_status") == "creator_close",
        "launchpad": raw.get("launchpad_platform") or raw.get("launchpad") or None,
        "exchange": raw.get("exchange") or None,
        "twitterRenameCount": _num(raw.get("twitter_rename_count")),
    }


# ---------- Filter: satu tabel untuk query server DAN cek ulang klien ----------
# Server GMGN dianggap saringan KASAR dan hasilnya SELALU dicek ulang di sini —
# tidak semua filter punya padanan di server, dan perilaku server bisa berubah.
#   api  : nama parameter server (None = hanya bisa dicek lokal)
#   cmp  : "gte" ambang minimum, "lte" ambang maksimum
FILTER_SPEC = [
    ("minVolume", "min_volume", "volume", "gte", "usd"),
    ("maxVolume", "max_volume", "volume", "lte", "usd"),
    ("minMcap", "min_marketcap", "marketCap", "gte", "usd"),
    ("maxMcap", "max_marketcap", "marketCap", "lte", "usd"),
    ("minLiquidity", "min_liquidity", "liquidity", "gte", "usd"),
    ("maxLiquidity", "max_liquidity", "liquidity", "lte", "usd"),
    ("minHolder", "min_holder_count", "holderCount", "gte", "count"),
    ("maxHolder", "max_holder_count", "holderCount", "lte", "count"),
    ("minSwaps", "min_swaps", "swaps", "gte", "count"),
    ("maxSwaps", "max_swaps", "swaps", "lte", "count"),
    ("minSmartDegen", "min_smart_degen_count", "smartDegenCount", "gte", "count"),
    ("minRenowned", "min_renowned_count", "renownedCount", "gte", "count"),
    ("maxBotDegen", "max_bot_degen_count", "botDegenCount", "lte", "count"),
    ("minTurnover", None, "turnover", "gte", "count"),
    ("maxTurnover", None, "turnover", "lte", "count"),
    ("maxRugRatio", None, "rugRatio", "lte", "ratio"),
    ("maxTop10Rate", "max_top10_holder_rate", "top10HolderRate", "lte", "ratio"),
    ("maxInsiderRate", "max_insider_rate", "insiderRate", "lte", "ratio"),
    ("maxBundlerRate", "max_bundler_rate", "bundlerRate", "lte", "ratio"),
    ("maxEntrapment", "max_entrapment_ratio", "entrapmentRatio", "lte", "ratio"),
    ("maxSniperHold", "max_top70_sniper_hold_rate", "top70SniperHoldRate", "lte", "ratio"),
    ("maxDevHold", "max_dev_team_hold_rate", "devTeamHoldRate", "lte", "ratio"),
    ("minPriceChange", None, "priceChangePercent", "gte", "percent"),
    ("maxPriceChange", None, "priceChangePercent", "lte", "percent"),
    ("minAge", "min_created", "ageSeconds", "gte", "duration"),
    ("maxAge", "max_created", "ageSeconds", "lte", "duration"),
]
_BY_KEY = {f[0]: f for f in FILTER_SPEC}

# Penjelasan + pengelompokan untuk UI. Dipisah dari FILTER_SPEC supaya menambah
# filter tetap cukup satu baris di sana; yang belum punya deskripsi tetap tampil
# memakai nama kuncinya.
FILTER_DESC = {
    "minVolume": "Volume pada interval", "maxVolume": "Volume pada interval",
    "minMcap": "Market cap", "maxMcap": "Market cap",
    "minLiquidity": "Likuiditas (dua sisi)", "maxLiquidity": "Likuiditas (dua sisi)",
    "minHolder": "Jumlah holder", "maxHolder": "Jumlah holder",
    "minSwaps": "Jumlah swap pada interval", "maxSwaps": "Jumlah swap pada interval",
    "minSmartDegen": "Wallet smart-money", "minRenowned": "Wallet KOL/ternama",
    "maxBotDegen": "Wallet bot/degen",
    "minTurnover": "Putaran = volume ÷ likuiditas", "maxTurnover": "Putaran = volume ÷ likuiditas",
    "maxRugRatio": "Skor indikasi rug GMGN",
    "maxTop10Rate": "Porsi 10 holder terbesar",
    "maxInsiderRate": "Aktivitas berlabel insider",
    "maxBundlerRate": "Aktivitas berlabel bundler",
    "maxEntrapment": "Rasio entrapment/phishing",
    "maxSniperHold": "Porsi sniper top-70",
    "maxDevHold": "Porsi tim dev",
    "minPriceChange": "Perubahan harga pada interval",
    "maxPriceChange": "Perubahan harga pada interval",
    "minAge": "Umur token minimum", "maxAge": "Umur token maksimum",
}

FILTER_GROUPS = [
    ("Ukuran pasar", ["minVolume", "maxVolume", "minMcap", "maxMcap",
                      "minLiquidity", "maxLiquidity"]),
    ("Aktivitas", ["minSwaps", "maxSwaps", "minHolder", "maxHolder",
                   "minTurnover", "maxTurnover", "minPriceChange", "maxPriceChange"]),
    ("Wallet", ["minSmartDegen", "minRenowned", "maxBotDegen"]),
    ("Risiko", ["maxRugRatio", "maxTop10Rate", "maxInsiderRate", "maxBundlerRate",
                "maxEntrapment", "maxSniperHold", "maxDevHold"]),
    ("Umur token", ["minAge", "maxAge"]),
]

# Pilihan cepat per satuan. Angka bulat yang lazim, bukan hasil ukur apa pun —
# nilai bebas tetap bisa diketik lewat "Nilai lain".
FILTER_CHOICES = {
    "usd": [10_000, 30_000, 50_000, 100_000, 300_000, 500_000, 1_000_000],
    "count": [0, 1, 3, 5, 10, 50, 100, 200, 500],
    "ratio": [0.05, 0.1, 0.15, 0.2, 0.3, 0.5],
    "percent": [-50, -20, 0, 20, 50, 100],
    "duration": ["1h", "6h", "24h", "48h", "7d", "30d"],
}


def filter_spec(key: str):
    """(key, api, field, cmp, unit) atau None."""
    return _BY_KEY.get(key)
_AGE_UNIT = {"m": 60, "h": 3600, "d": 86400}


def duration_secs(s):
    """`30m` / `6h` / `7d` → detik. None kalau bukan bentuk itu."""
    if not isinstance(s, str) or not re.fullmatch(r"\d+[mhd]", s):
        return None
    return int(s[:-1]) * _AGE_UNIT[s[-1]]


def to_query(values: dict) -> dict:
    """Filter yang dimengerti server. Sisanya dicek lokal."""
    q = {}
    for k, v in (values or {}).items():
        spec = _BY_KEY.get(k)
        if spec and spec[1] and v is not None:
            q[spec[1]] = v
    return q


def passes(token: dict, values: dict) -> bool:
    """Nilai yang BELUM DIKETAHUI tidak lolos ambang apa pun.

    Sengaja: sama seperti perilaku server GMGN, dan supaya "belum diuji" tidak
    lolos seolah-olah "aman"."""
    for k, v in (values or {}).items():
        if v is None:
            continue
        spec = _BY_KEY.get(k)
        if not spec:
            continue
        _, _, field, cmp_, unit = spec
        thr = duration_secs(v) if unit == "duration" else _num(v)
        if thr is None:
            continue
        actual = token.get(field)
        if actual is None:
            return False
        if cmp_ == "gte" and actual < thr:
            return False
        if cmp_ == "lte" and actual > thr:
            return False
    return True


# ---------- Tier ----------
def classify(token: dict) -> dict:
    """Ambang mengikuti panduan screening GMGN sendiri, bukan karangan.

    Angka karangan akan terlihat berwibawa tanpa dasar apa pun; kalau diubah,
    catat alasannya."""
    skips, warns, goods = [], [], []
    p = lambda v, d=1: f"{v * 100:.{d}f}%"

    if token["isHoneypot"] is True:
        skips.append("terdeteksi honeypot (is_honeypot=1)")
    if token["isWashTrading"] is True:
        skips.append("ditandai wash trading oleh GMGN")
    if token["rugRatio"] is not None and token["rugRatio"] > 0.3:
        skips.append(f"skor indikasi rug {p(token['rugRatio'])} di atas 30%")

    if token["rugRatio"] is not None and 0.1 <= token["rugRatio"] <= 0.3:
        warns.append(f"skor indikasi rug {p(token['rugRatio'])} (zona 10–30%)")
    if token["botDegenRate"] is not None and token["botDegenRate"] > 0.3:
        warns.append(f"wallet berlabel bot/degen {p(token['botDegenRate'], 0)}")
    if token["bundlerRate"] is not None and token["bundlerRate"] > 0.2:
        warns.append(f"aktivitas berlabel bundler {p(token['bundlerRate'])}")
    if token["entrapmentRatio"] is not None and token["entrapmentRatio"] > 0.2:
        warns.append(f"entrapment/phishing {p(token['entrapmentRatio'], 0)} — metode GMGN tidak dipublikasi")
    if token["top10HolderRate"] is not None and token["top10HolderRate"] > 0.3:
        warns.append(f"10 holder terbesar pegang {p(token['top10HolderRate'], 0)} supply")
    if token["top70SniperHoldRate"] is not None and token["top70SniperHoldRate"] > 0.1:
        warns.append(f"sniper top-70 masih pegang {p(token['top70SniperHoldRate'])}")
    if token["devTeamHoldRate"] is not None and token["devTeamHoldRate"] > 0.1:
        warns.append(f"tim dev pegang {p(token['devTeamHoldRate'])}")
    if token["ageSeconds"] is not None and token["ageSeconds"] < 3600:
        warns.append("umur token di bawah 1 jam — data on-chain masih tipis")
    if token["ageSeconds"] is None:
        warns.append("umur token tidak diketahui (creation_timestamp = 0)")
    if token["twitterRenameCount"]:
        warns.append(f"akun X pernah ganti nama {token['twitterRenameCount']:.0f}x")
    if token["chain"] == "sol":
        if token["renouncedMint"] is False:
            warns.append("wewenang mint belum dilepas")
        if token["renouncedFreeze"] is False:
            warns.append("wewenang freeze account belum dilepas")
    else:
        if token["isRenounced"] is False:
            warns.append("ownership kontrak belum di-renounce")
        if token["isOpenSource"] is False:
            warns.append("kontrak tidak open source")

    if token["smartDegenCount"] is not None and token["smartDegenCount"] >= 3:
        goods.append(f"{token['smartDegenCount']:.0f} wallet smart-money terdeteksi")
    if token["renownedCount"]:
        goods.append(f"{token['renownedCount']:.0f} wallet KOL/ternama")
    if token["creatorClose"]:
        goods.append("dev sudah clear posisi (creator_close)")
    if token["rugRatio"] is not None and token["rugRatio"] < 0.1:
        goods.append(f"skor indikasi rug rendah {p(token['rugRatio'])}")

    # Kosong bukan berarti aman — kelengkapan data ikut dilaporkan.
    missing = []
    if token["rugRatio"] is None:
        missing.append("rug_ratio")
    if token["top10HolderRate"] is None:
        missing.append("top_10_holder_rate")
    if token["isHoneypot"] is None:
        missing.append("is_honeypot belum diuji")
    if token["chain"] in EVM_CHAINS and (token["buyTax"] is None or token["sellTax"] is None):
        missing.append("pajak beli/jual belum diuji")

    tier = "WATCH"
    if skips:
        tier = "SKIP"
    elif (token["smartDegenCount"] is not None and token["smartDegenCount"] >= 3
          and token["rugRatio"] is not None and token["rugRatio"] < 0.1
          and token["creatorClose"] and token["isWashTrading"] is False):
        tier = "PASS"
    return {"tier": tier, "skips": skips, "warns": warns, "goods": goods, "missing": missing}


# ---------- State polling + cooldown ----------
_STATE_VERSION = 1


class PollState:
    """Jendela konfirmasi + cooldown, tahan restart.

    Tanpa disimpan ke disk, tiap `systemctl restart` mengirim ulang semua kartu
    yang baru saja dikirim — jendela polling dan cooldown ikut hilang.

    Kunci selalu `chain:address`: alamat yang sama bisa ada di beberapa chain EVM.
    """

    def __init__(self, path: str | None):
        self.path = path
        self.seen: dict[str, list] = {}
        self.last: dict[str, float] = {}
        self._warned = False
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text())
            if raw.get("version") == _STATE_VERSION:
                self.seen = {k: list(v) for k, v in (raw.get("seen") or {}).items()}
                self.last = {k: float(v) for k, v in (raw.get("last") or {}).items()}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("state scanner %s tidak terbaca (%s) — mulai dari kosong", path, e)

    def record(self, keys, window: int, scope: str | None = None):
        """`scope` membatasi pembaruan ke satu chain.

        Tanpa itu, scan chain A dihitung sebagai "tidak muncul" bagi token chain B
        yang belum discan pada siklus itu — dan konfirmasi polling jadi tidak
        pernah tercapai."""
        present = set(keys)
        in_scope = (lambda k: True) if scope is None else (lambda k: k.startswith(f"{scope}:"))
        for k in set([x for x in self.seen if in_scope(x)]) | present:
            arr = self.seen.get(k, [])
            arr.append(k in present)
            if window > 0 and len(arr) > window:
                del arr[:-window]
            if any(arr):
                self.seen[k] = arr
            else:
                self.seen.pop(k, None)

    def hits(self, k: str) -> int:
        return sum(1 for x in self.seen.get(k, []) if x)

    def should_alert(self, k: str, confirm: int, cooldown_min: float) -> bool:
        if self.hits(k) < confirm:
            return False
        if time.time() - self.last.get(k, 0) < cooldown_min * 60:
            return False
        self.last[k] = time.time()
        return True

    def persist(self, cooldown_min: float):
        if not self.path:
            return
        # Buang jejak lama supaya file tidak tumbuh tanpa batas di proses 24 jam.
        horizon = time.time() - max(cooldown_min * 60 * 3, 86400)
        self.last = {k: v for k, v in self.last.items() if v >= horizon}
        try:
            tmp = f"{self.path}.tmp"
            Path(tmp).write_text(json.dumps({
                "version": _STATE_VERSION, "updated": int(time.time()),
                "seen": self.seen, "last": self.last}))
            os.replace(tmp, self.path)   # rename atomik: tidak pernah setengah tertulis
        except Exception as e:
            if not self._warned:         # sekali saja, jangan tiap siklus
                self._warned = True
                log.error("gagal menyimpan state scanner ke %s: %s — cooldown tidak "
                          "akan bertahan setelah restart", self.path, e)


def scan_chain(client: Client, chain: str, interval: str, filters: dict, limit: int = 100) -> list[dict]:
    """Satu chain → daftar token ternormalisasi yang lolos filter.

    Filter server dipakai sebagai saringan kasar, lalu SEMUANYA dicek ulang di
    sini — perilaku server bisa berubah dan tidak semua filter punya padanan."""
    raw = client.rank(chain, interval, {**to_query(filters), "limit": min(int(limit or 100), 100)})
    out = []
    for r in raw:
        try:
            t = normalize(r, chain, interval)
        except Exception:
            continue
        if t.get("address") and passes(t, filters):
            out.append(t)
    return out
