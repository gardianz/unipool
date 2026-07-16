"""
store.py — Penyimpanan JSON sederhana: settings bot + riwayat deposit/withdraw
untuk hitung PnL portfolio ala /list.
"""
import json
import time
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


def record_event(chain_id: int, kind: str, token_id: int | None, usd: float,
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


def mint_ts(chain_id: int, token_id: int) -> int | None:
    for e in _hist()["events"].get(str(chain_id), []):
        if e["kind"] == "mint" and e["token_id"] == token_id:
            return e["ts"]
    return None


def mint_usd(chain_id: int, token_id: int) -> float | None:
    """Total deposit posisi (mint awal + semua add)."""
    total = sum(e["usd"] for e in _hist()["events"].get(str(chain_id), [])
                if e["kind"] == "mint" and e["token_id"] == token_id)
    return total or None


def fees_claimed_usd(chain_id: int, token_id: int) -> float:
    return sum(e["usd"] for e in _hist()["events"].get(str(chain_id), [])
               if e["kind"] == "fees" and e["token_id"] == token_id)


def withdrawn_usd(chain_id: int, token_id: int) -> float:
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


def fmt_age(ts: int | None) -> str:
    if not ts:
        return "?"
    d = int(time.time()) - ts
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"
