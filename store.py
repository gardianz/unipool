"""
store.py — Penyimpanan JSON sederhana: settings bot + riwayat deposit/withdraw
untuk hitung PnL portfolio ala /list.
"""
import json
import time
import uuid
from pathlib import Path

BASE = Path(__file__).parent
SETTINGS_FILE = BASE / "settings.json"
HISTORY_FILE = BASE / "history.json"

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
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(_read(SETTINGS_FILE, {}))
    return s


def save_settings(s: dict):
    _write(SETTINGS_FILE, s)


# ---------- Riwayat PnL ----------
def _hist() -> dict:
    return _read(HISTORY_FILE, {"events": {}})


def record_event(chain_id: int, kind: str, token_id, usd: float,
                 detail: str = "", wallet: str = ""):
    """kind: mint | close | fees"""
    h = _hist()
    h["events"].setdefault(str(chain_id), []).append({
        "ts": int(time.time()), "kind": kind, "token_id": token_id,
        "usd": usd, "detail": detail, "wallet": wallet.lower(),
    })
    _write(HISTORY_FILE, h)


def adopt_orphans(chain_id: int, wallet: str, token_ids: list[int]):
    """Event lama tanpa tag wallet: klaim ke wallet ini kalau posisinya memang miliknya."""
    h = _hist()
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
    h = _hist()
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
    h = _hist()
    lst = h.get("refs", {}).get(str(chain_id), {}).get(wallet.lower(), {}).get(kind, [])
    ref = str(ref).lower()
    if ref in lst:
        lst.remove(ref)
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
    h = _hist()
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
    h = _hist()
    for o in h.get("orders", {}).get(str(chain_id), []):
        if o.get("id") == oid:
            o.update(fields)
            _write(HISTORY_FILE, h)
            return True
    return False


def drop_order(chain_id: int, oid: str) -> bool:
    h = _hist()
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
