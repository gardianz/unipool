"""
store.py — Penyimpanan JSON sederhana: settings bot + riwayat deposit/withdraw
untuk hitung PnL portfolio ala /list.
"""
import fcntl
import json
import logging
import math
import threading
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

BASE = Path(__file__).parent
SETTINGS_FILE = BASE / "settings.json"
HISTORY_FILE = BASE / "history.json"

log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "chain": 4663,
    "width_pct": 50.0,      # lebar range %
    "amount_pct": 50.0,     # % saldo quote yang dipakai (kalau amount_fixed None)
    "amount_fixed": None,   # jumlah quote fix, override amount_pct
    "slippage_pct": 5.0,
    "autoswap": True,       # swap hasil close → wrapped native
    "gap": 1,               # jarak pengaman range single-sided dari harga (satuan tick-spacing; 0 = nempel)
    "alert_secs": 60,       # interval cek alert in/out range (detik; 0 = off)
    "wallet_idx": 0,        # wallet aktif (index di daftar PRIVATE_KEY, PRIVATE_KEY_2, ...)
}


def _read(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def _write(path: Path, data):
    """Tulis atomik. Nama sementara HARUS unik per penulis.

    Dulu `path.with_suffix(".tmp")` — satu nama untuk semua penulis. Dua penulis
    bersamaan (bot.py multi-thread, atau bot.py + web.py yang memang berbagi file
    ini) menulis ke tmp yang SAMA lalu sama-sama rename, jadi yang mendarat bisa
    potongan dua JSON yang disambung. File rusak = `_read` gagal = registry posisi
    v2/v4 dianggap kosong, dan posisi lenyap dari UI walau dananya utuh on-chain.
    Dengan nama unik, rename POSIX tetap atomik: penulis terakhir menang, tapi
    file tidak pernah setengah jadi."""
    blob = json.dumps(data, indent=2)      # serialisasi DULU: kalau gagal, file lama utuh
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident():x}.tmp")
    try:
        tmp.write_text(blob)
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()                   # kalau replace sudah jalan, ini no-op
        except OSError:
            pass


# ---------- Brankas wallet (di luar .env) ----------
# Wallet yang ditambahkan lewat bot disimpan di sini, TERPISAH dari .env supaya
# .env tetap jadi milik operator mesin. File ini berisi private key polos: mode
# 0600 dan wajib ada di .gitignore. Siapa pun yang bisa membaca file ini bisa
# memindahkan seluruh dana wallet-wallet itu.
WALLETS_FILE = BASE / "wallets.json"


def _write_secret(path: Path, data):
    """Tulis file rahasia: permission 0600 dipasang SEBELUM isi terlihat di FS."""
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def wallets() -> list[dict]:
    """[{name, pk}] — wallet tambahan di luar .env."""
    d = _read(WALLETS_FILE, {"wallets": []})
    out = []
    for w in d.get("wallets", []):
        if isinstance(w, dict) and w.get("pk"):
            out.append({"name": str(w.get("name") or ""), "pk": str(w["pk"])})
    return out


def add_wallet(pk: str, name: str = "") -> bool:
    """False kalau private key itu sudah ada (tidak digandakan)."""
    pk = pk if pk.startswith("0x") else "0x" + pk
    ws = wallets()
    if any(w["pk"].lower() == pk.lower() for w in ws):
        return False
    ws.append({"name": name, "pk": pk})
    _write_secret(WALLETS_FILE, {"wallets": ws})
    return True


def remove_wallet(pk: str) -> bool:
    ws = wallets()
    keep = [w for w in ws if w["pk"].lower() != str(pk).lower()]
    if len(keep) == len(ws):
        return False
    _write_secret(WALLETS_FILE, {"wallets": keep})
    return True


def rename_wallet(pk: str, name: str) -> bool:
    ws = wallets()
    for w in ws:
        if w["pk"].lower() == str(pk).lower():
            w["name"] = name
            _write_secret(WALLETS_FILE, {"wallets": ws})
            return True
    return False


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(_read(SETTINGS_FILE, {}))
    return s


def save_settings(s: dict):
    _write(SETTINGS_FILE, s)


# ---------- Riwayat PnL ----------
_HIST_CACHE: dict = {"key": None, "val": None}   # (mtime, size) -> isi file
_HIST_LOCK = threading.Lock()


def _hist(fresh: bool = False) -> dict:
    """Isi history.json, di-cache selama file-nya belum berubah.

    Kartu /list memanggil mint_usd/fees_claimed_usd/withdrawn_usd/mint_ts per posisi,
    jadi tanpa cache satu refresh mem-parse file yang sama puluhan kali. Kunci
    cache-nya (mtime, ukuran) supaya perubahan dari proses lain (web.py) tetap
    terbaca — file ditulis atomik lewat rename, jadi mtime pasti berubah.

    `fresh=True` WAJIB untuk setiap pemanggil yang akan MEMUTASI hasilnya lalu
    `_write`. Objek yang di-cache dipakai bersama semua pembaca: memutasinya berarti
    mengubah apa yang dilihat pemanggil lain, dan menulis dari salinan yang sudah
    basi bisa menghapus perubahan proses lain (web.py menulis file yang sama).

    Baca yang GAGAL tidak pernah di-cache. Kalau file sempat tak terbaca, hasil
    default `{"events": {}}` dulu ikut tersimpan dan menempel sampai mtime berubah —
    registry posisi v2/v4 terlihat kosong padahal isinya ada."""
    if not fresh:
        try:
            st = HISTORY_FILE.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if key is not None:
            with _HIST_LOCK:
                if _HIST_CACHE["key"] == key and _HIST_CACHE["val"] is not None:
                    return _HIST_CACHE["val"]
    if not HISTORY_FILE.exists():
        return {"events": {}}
    try:
        val = json.loads(HISTORY_FILE.read_text())
    except Exception:
        return {"events": {}}          # rusak/kepotong → JANGAN di-cache
    if not fresh:
        try:
            st = HISTORY_FILE.stat()
            with _HIST_LOCK:
                _HIST_CACHE["key"] = (st.st_mtime_ns, st.st_size)
                _HIST_CACHE["val"] = val
        except OSError:
            pass
    return val


_HIST_WLOCK = threading.RLock()
_HIST_LOCKFILE = BASE / ".history.lock"


@contextmanager
def _hist_write():
    """Kunci baca-ubah-tulis history.json — antar-thread DAN antar-proses.

    Semua mutator polanya sama: baca file, ubah di memori, tulis ulang. Tanpa kunci,
    dua penulis membaca isi yang sama lalu saling menimpa: terukur pada 8 thread x 40
    event, hanya 47 dari 320 event yang tersimpan. `bot.py` kini memproses update
    secara paralel dan `web.py` proses TERPISAH yang menulis file yang sama, jadi
    kunci thread saja tidak cukup — perlu flock."""
    with _HIST_WLOCK:
        try:
            f = open(_HIST_LOCKFILE, "w")
        except OSError:
            yield                       # tanpa flock masih lebih baik daripada gagal
            return
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            finally:
                f.close()


# Batas kewarasan nilai event (USD). Posisi LP terbesar yang masuk akal di bot ini
# jauh di bawah ini; angka di atasnya selalu bug pembacaan, bukan dana sungguhan.
_USD_SANITY_MAX = 1e9


def drop_bad_events(chain_id: int, limit: float = _USD_SANITY_MAX) -> list[dict]:
    """Buang event ber-USD mustahil yang terlanjur tercatat. Kembalikan yang dibuang."""
    with _hist_write():
        h = _hist(fresh=True)
        key = str(chain_id)
        ev = h.get("events", {}).get(key, [])
        bad = [e for e in ev
               if not isinstance(e.get("usd"), (int, float))
               or not math.isfinite(float(e["usd"])) or abs(float(e["usd"])) > limit]
        if bad:
            h["events"][key] = [e for e in ev if e not in bad]
            _write(HISTORY_FILE, h)
        return bad


def record_event(chain_id: int, kind: str, token_id, usd: float,
                 detail: str = "", wallet: str = ""):
    """kind: mint | close | fees"""
    # Satu nilai mustahil merusak SELURUH PnL portfolio selamanya: jumlahnya bukan
    # rata-rata, jadi tidak ada yang meredamnya. Terjadi sungguhan — satu event
    # `fees` senilai $5,9e53 (v4:1239107) membuat PnL terbaca
    # "$593805893216973777495023055208279841552788881408.0M" dan persentasenya ikut
    # ngawur. Nilai sebesar itu selalu bug pembacaan (raw token dianggap sudah
    # berdesimal, delta uint256 yang underflow, harga dari pool debu), bukan dana
    # sungguhan. Lebih baik satu event hilang daripada seluruh riwayat tidak terpakai.
    try:
        usd = float(usd)
    except (TypeError, ValueError):
        log.error("record_event %s %s: usd tidak valid (%r) — dilewati", kind, token_id, usd)
        return
    if not math.isfinite(usd) or abs(usd) > _USD_SANITY_MAX:
        log.error("record_event %s %s: usd mustahil ($%.3g) — dilewati", kind, token_id, usd)
        return
    with _hist_write():
        h = _hist(fresh=True)
        h["events"].setdefault(str(chain_id), []).append({
            "ts": int(time.time()), "kind": kind, "token_id": token_id,
            "usd": usd, "detail": detail, "wallet": wallet.lower(),
        })
        _write(HISTORY_FILE, h)


def adopt_orphans(chain_id: int, wallet: str, token_ids: list[int]):
    """Event lama tanpa tag wallet: klaim ke wallet ini kalau posisinya memang miliknya."""
    with _hist_write():
        h = _hist(fresh=True)
        ids = set(token_ids)
        changed = False
        for e in h["events"].get(str(chain_id), []):
            if not e.get("wallet") and e.get("token_id") in ids:
                e["wallet"] = wallet.lower()
                changed = True
        if changed:
            _write(HISTORY_FILE, h)


# ---------- Registry posisi V2/V4 ----------
# NPM v3 bisa di-enumerate on-chain (ERC721Enumerable), tapi PositionManager v4
# tidak, dan posisi v2 cuma saldo LP token — keduanya dicatat di sini saat mint.
def add_ref(chain_id: int, wallet: str, kind: str, ref: str):
    """kind: 'v2' (ref = alamat pair) | 'v4' (ref = tokenId str)."""
    with _hist_write():
        h = _hist(fresh=True)
        lst = (h.setdefault("refs", {}).setdefault(str(chain_id), {})
                .setdefault(wallet.lower(), {}).setdefault(kind, []))
        ref = str(ref).lower()
        if ref not in lst:
            lst.append(ref)
            _write(HISTORY_FILE, h)


def refs(chain_id: int, wallet: str, kind: str) -> list[str]:
    return list(_hist().get("refs", {}).get(str(chain_id), {})
                .get(wallet.lower(), {}).get(kind, []))


def drop_ref(chain_id: int, wallet: str, kind: str, ref: str):
    with _hist_write():
        h = _hist(fresh=True)
        lst = h.get("refs", {}).get(str(chain_id), {}).get(wallet.lower(), {}).get(kind, [])
        ref = str(ref).lower()
        if ref in lst:
            lst.remove(ref)
            _write(HISTORY_FILE, h)


# ---------- Patokan fee posisi V2 ----------
# LP v2 tidak punya "fee unclaimed" — fee mengendap ke reserve, jadi jumlah LP token
# tetap tapi nilainya naik. Patokannya √k per LP saat masuk (k = reserve0×reserve1):
# angka itu KEBAL pergerakan harga dan hanya naik oleh fee, jadi
#   fee = nilai_sekarang × (1 − k_saat_masuk / k_sekarang).
def v2_basis(chain_id: int, wallet: str, pair: str) -> float | None:
    return (_hist().get("v2_basis", {}).get(str(chain_id), {})
            .get(wallet.lower(), {}).get(str(pair).lower()))


def set_v2_basis(chain_id: int, wallet: str, pair: str, k_per_lp: float,
                 lp_before: int = 0, lp_after: int = 0):
    """Simpan patokan. Kalau sudah ada posisi (add liquidity berikutnya), patokan
    lama dan baru dirata-rata berbobot jumlah LP — kalau tidak, LP yang baru masuk
    ikut diklaim sudah mengumpulkan fee sejak mint pertama."""
    with _hist_write():
        if not k_per_lp:
            return
        h = _hist(fresh=True)
        d = (h.setdefault("v2_basis", {}).setdefault(str(chain_id), {})
              .setdefault(wallet.lower(), {}))
        key = str(pair).lower()
        old = d.get(key)
        if old and lp_after > lp_before > 0:
            added = lp_after - lp_before
            d[key] = (old * lp_before + k_per_lp * added) / lp_after
        else:
            d[key] = k_per_lp
        _write(HISTORY_FILE, h)


def drop_v2_basis(chain_id: int, wallet: str, pair: str):
    with _hist_write():
        h = _hist(fresh=True)
        d = (h.get("v2_basis", {}).get(str(chain_id), {}).get(wallet.lower(), {}))
        if d.pop(str(pair).lower(), None) is not None:
            _write(HISTORY_FILE, h)


def mint_ts(chain_id: int, token_id) -> int | None:
    for e in _hist()["events"].get(str(chain_id), []):
        if e["kind"] == "mint" and e["token_id"] == token_id:
            return e["ts"]
    return None


def mint_usd(chain_id: int, token_id) -> float | None:
    """Total deposit posisi (mint awal + semua add)."""
    total = sum(e["usd"] for e in _hist()["events"].get(str(chain_id), [])
                if e["kind"] == "mint" and e["token_id"] == token_id)
    return total or None


def fees_claimed_usd(chain_id: int, token_id) -> float:
    return sum(e["usd"] for e in _hist()["events"].get(str(chain_id), [])
               if e["kind"] == "fees" and e["token_id"] == token_id)


def withdrawn_usd(chain_id: int, token_id) -> float:
    """Total dana yang sudah ditarik dari posisi (reduce/close)."""
    return sum(e["usd"] for e in _hist()["events"].get(str(chain_id), [])
               if e["kind"] == "close" and e["token_id"] == token_id)


def churn_count(chain_id: int, wallet: str = "") -> int:
    """Berapa kali dana yang SAMA didaur ulang (rebalance / pindah pool / compound).

    Tiap siklus mencatat close + mint baru, jadi `deposits` dan `withdrawals`
    menggelembung tanpa ada modal segar yang masuk. Angka ini dipakai UI untuk
    menjelaskan kenapa dua angka itu jauh lebih besar dari modal sebenarnya."""
    ev = [e for e in _hist()["events"].get(str(chain_id), [])
          if e.get("wallet", "") == wallet.lower()]
    return sum(1 for e in ev if e["kind"] == "mint"
               and str(e.get("detail", "")).startswith(("rebalance", "compound")))


def portfolio_summary(chain_id: int, wallet: str = "") -> dict:
    """PnL per wallet — event lama tanpa field wallet tidak ikut dihitung."""
    ev = [e for e in _hist()["events"].get(str(chain_id), [])
          if e.get("wallet", "") == wallet.lower()]
    deposits = sum(e["usd"] for e in ev if e["kind"] == "mint")
    withdrawals = sum(e["usd"] for e in ev if e["kind"] == "close")
    fees = sum(e["usd"] for e in ev if e["kind"] == "fees")
    return {"deposits": deposits, "withdrawals": withdrawals, "fees_claimed": fees}


# ---------- Order TP/SL (auto-close posisi LP saat market cap sentuh batas) ----------
# Disimpan di history.json → "orders": {"<chain>": [order, ...]}.
# Satu order = satu posisi LP + batas TP (MC atas) dan/atau SL (MC bawah).
# EKSEKUTOR TUNGGAL: monitor_loop di bot.py. Web hanya membuat/membatalkan order
# (menulis file ini); bot yang menjalankan close saat trigger — jadi tidak ada
# risiko dua proses menutup posisi yang sama (nonce/double-spend).
# Field order:
#   id, wallet (lowercase), pid (str), meme_sym,
#   tp_mc (float|None), sl_mc (float|None), autoswap (bool),
#   status ("active"|"done"|"error"|"cancelled"),
#   created (ts), triggered (ts|None), reason (str), tx (str|None)
def add_order(chain_id: int, order: dict) -> str:
    with _hist_write():
        h = _hist(fresh=True)
        o = dict(order)
        o["id"] = uuid.uuid4().hex[:6]
        o.setdefault("created", int(time.time()))
        o.setdefault("status", "active")
        o.setdefault("triggered", None)
        o.setdefault("reason", "")
        o.setdefault("tx", None)
        o["wallet"] = str(o.get("wallet", "")).lower()
        o["pid"] = str(o.get("pid", ""))
        (h.setdefault("orders", {}).setdefault(str(chain_id), [])).append(o)
        _write(HISTORY_FILE, h)
        return o["id"]


def orders(chain_id: int, wallet: str = "", status: str = "") -> list[dict]:
    lst = _hist().get("orders", {}).get(str(chain_id), [])
    out = []
    for o in lst:
        if wallet and o.get("wallet", "").lower() != wallet.lower():
            continue
        if status and o.get("status") != status:
            continue
        out.append(o)
    return out


def get_order(chain_id: int, oid: str) -> dict | None:
    for o in _hist().get("orders", {}).get(str(chain_id), []):
        if o.get("id") == oid:
            return o
    return None


def update_order(chain_id: int, oid: str, **fields) -> bool:
    with _hist_write():
        h = _hist(fresh=True)
        for o in h.get("orders", {}).get(str(chain_id), []):
            if o.get("id") == oid:
                o.update(fields)
                _write(HISTORY_FILE, h)
                return True
        return False


def drop_order(chain_id: int, oid: str) -> bool:
    with _hist_write():
        h = _hist(fresh=True)
        lst = h.get("orders", {}).get(str(chain_id), [])
        n = len(lst)
        lst[:] = [o for o in lst if o.get("id") != oid]
        if len(lst) != n:
            _write(HISTORY_FILE, h)
            return True
        return False


def fmt_age(ts: int | None) -> str:
    if not ts:
        return "?"
    d = int(time.time()) - ts
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"
