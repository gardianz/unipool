#!/usr/bin/env python3
"""
bot.py — Telegram LP bot: paste alamat token → pilih pool → mint LP single-sided.
/list untuk posisi + PnL + close (dengan auto-swap hasil close → WETH/WBNB).

Jalankan:  python3 bot.py
Env (.env): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PRIVATE_KEY, [RPC_4663, RPC_56, RPC_8453, RPC_999, RPC_5042]
"""
import asyncio
from contextlib import asynccontextmanager
import functools
import html
import logging
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import uuid
from pathlib import Path

from dotenv import load_dotenv
from telegram import (BotCommand, ForceReply, InlineKeyboardButton,
                      InlineKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import chain as ch
import gmgn
import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lp-bot")
# long-polling getUpdates tiap ~10 detik itu normal — jangan banjiri log
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

ADDR_RE = re.compile(r"\b(0x[0-9a-fA-F]{40})\b")
CUSTOM_RANGE_RE = re.compile(r"^r(?:ange)?\s+(\d+(?:\.\d+)?)(?:\s+(\d+(?:\.\d+)?))?$", re.I)
CUSTOM_AMT_RE = re.compile(r"^a(?:mount)?\s+(\d*\.?\d+)\s*(%?)$", re.I)
TX_LOCK = asyncio.Lock()   # serialisasi tx (nonce)
PENDING: dict[str, dict] = {}  # konteks tombol pilih pool
LAST_CONFIRM: dict[int, tuple] = {}  # chat_id → (key, message kartu konfirmasi aktif)
AWAITING: dict[int, dict] = {}  # chat_id → {"kind": "range"|"amount", "key": ...} nunggu balasan user
RANGE_STATE: dict[tuple, bool] = {}  # (chain_id, token_id) → in_range terakhir (untuk alert)

# ---------- Auth ----------
def allowed_chat_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return {int(x) for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}


def authorized(update: Update) -> bool:
    ids = allowed_chat_ids()
    cid = update.effective_chat.id if update.effective_chat else None
    return bool(ids) and cid in ids


# ---------- Util ----------
def esc(s) -> str:
    return html.escape(str(s))


@functools.lru_cache(maxsize=1)
def env_pks() -> tuple[str, ...]:
    """Private key dari .env: PRIVATE_KEY (W1), PRIVATE_KEY_2 (W2), dst.
    Ini milik operator mesin — tidak bisa dihapus lewat bot."""
    keys = []
    raw = os.environ.get("PRIVATE_KEY", "").strip()
    if raw:
        keys.append(raw)
    i = 2
    while True:
        raw = os.environ.get(f"PRIVATE_KEY_{i}", "").strip()
        if not raw:
            break
        keys.append(raw)
        i += 1
    return tuple(k if k.startswith("0x") else "0x" + k for k in keys)


def all_pks() -> tuple[str, ...]:
    """Wallet .env DULU (urutannya tetap, supaya W1 tidak berubah arti), lalu
    wallet yang ditambahkan lewat bot. TIDAK di-cache: brankas bisa berubah
    saat runtime lewat menu tambah/hapus."""
    keys = list(env_pks())
    seen = {k.lower() for k in keys}
    for w in store.wallets():
        k = w["pk"] if w["pk"].startswith("0x") else "0x" + w["pk"]
        if k.lower() not in seen:
            keys.append(k)
            seen.add(k.lower())
    return tuple(keys)


def is_env_pk(key: str) -> bool:
    return any(k.lower() == str(key).lower() for k in env_pks())


def wallet_name(key: str) -> str:
    for w in store.wallets():
        if w["pk"].lower() == str(key).lower():
            return w.get("name") or ""
    return ""


def active_wallet_idx() -> int:
    n = max(1, len(all_pks()))
    try:
        return min(max(0, int(store.load_settings().get("wallet_idx", 0))), n - 1)
    except (TypeError, ValueError):
        return 0


def pk() -> str:
    return all_pks()[active_wallet_idx()]


def wallet_label(idx: int | None = None) -> str:
    return f"W{(active_wallet_idx() if idx is None else idx) + 1}"


@functools.lru_cache(maxsize=16)
def _addr_of(key: str) -> str:
    from web3 import Web3
    return Web3().eth.account.from_key(key).address


def disp_pid(pid) -> str:
    """'183469' → '#183469' · 'v4:12'/'v2:0x..' apa adanya."""
    s = str(pid)
    return f"#{s}" if s.isdigit() else s


def fmt_short(v) -> str:
    """Angka USD ringkas tanpa '$' untuk kolom tabel: 4.8M · 100.2k · 189.6 · –."""
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.1f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e5:
        return f"{v / 1e3:.0f}k"      # 128.8k → 129k, biar kolom tetap ≤5 karakter
    if a >= 1e3:
        return f"{v / 1e3:.1f}k"
    if a >= 10:
        return f"{v:.0f}"
    return f"{v:.1f}"


def fmt_ratio(vol, tvl) -> str:
    """Rasio volume 24 jam terhadap TVL: 2.8k% · 660% · 47% · –."""
    if not vol or not tvl:
        return "–"
    return fmt_pct_short(vol / tvl * 100)


def fmt_pct_short(v) -> str:
    """Persen ringkas: 52k% · 3.0k% · 55% · –."""
    if not v:
        return "–"
    a = abs(v)
    if a >= 1e4:
        return f"{v / 1e3:.0f}k%"
    if a >= 1e3:
        return f"{v / 1e3:.1f}k%"
    return f"{v:.0f}%"


def pk_for(addr: str) -> str | None:
    """Private key untuk alamat wallet tertentu (buat eksekusi order milik wallet itu)."""
    al = str(addr).lower()
    for k in all_pks():
        if _addr_of(k).lower() == al:
            return k
    return None


# ---------- Cache daftar posisi (stale-while-revalidate) ----------
# Membaca daftar itu N posisi x ~12 panggilan RPC. Dulu SETIAP klik /list, Refresh,
# dan tiap putaran monitor_loop membayarnya lagi dari nol, jadi dua pemakai yang
# sebenarnya butuh data yang SAMA saling menggandakan tagihan CU — lalu 429 mengenai
# keduanya dan kliknya jadi belasan detik (terukur `menu|list makan 16.9s`).
#
# Sekarang satu pembaca (monitor_loop, `fresh=True`) mengisi cache dan semua klik UI
# membacanya gratis. Volume RPC-nya JADI LEBIH KECIL, bukan cuma dipindah.
_POS_CACHE: dict[tuple, tuple] = {}   # (cid, addr) -> (positions, errors, ts)
_POS_BUSY: set = set()
_POS_CLOCK = threading.Lock()
_POS_FRESH_SECS = 45                  # dianggap segar: disajikan apa adanya
_POS_MAX_STALE = 900                  # masih boleh disajikan sambil disegarkan di latar


def _pos_read(cid: int, key: str, addr: str) -> tuple[list, list]:
    errs: list = []
    pos = ch.list_all_positions(cid, key, store.refs(cid, addr, "v2"),
                                store.refs(cid, addr, "v4"), errors=errs)
    _POS_CACHE[(cid, addr)] = (pos, errs, time.time())
    return pos, errs


def _pos_refresh_bg(cid: int, key: str, addr: str) -> None:
    """Segarkan cache di thread latar, satu per (chain, wallet)."""
    ck = (cid, addr)
    with _POS_CLOCK:
        if ck in _POS_BUSY:
            return
        _POS_BUSY.add(ck)

    def work():
        try:
            _pos_read(cid, key, addr)
        except Exception as e:
            log.warning("refresh posisi latar %s/%s: %s", cid, addr, e)
        finally:
            with _POS_CLOCK:
                _POS_BUSY.discard(ck)

    threading.Thread(target=work, daemon=True, name="pos-refresh").start()


def pos_cache_drop(cid: int | None = None) -> None:
    """Buang cache sesudah aksi yang MENGUBAH posisi (mint/close/reduce/…).

    Tanpa ini user menutup posisi lalu `/list` masih menampilkannya — jauh lebih
    buruk daripada lambat. Dipanggil di `position_busy` (6 alur aksi) dan do_mint."""
    for k in [k for k in _POS_CACHE if cid is None or k[0] == cid]:
        _POS_CACHE.pop(k, None)


def list_positions_all(cid: int, key: str | None = None, errors: list | None = None,
                       fresh: bool = False, light: bool = False) -> list[dict]:
    """Posisi v3 + v4 + v2 wallet (v4/v2 dari registry yang dicatat saat mint).

    `errors`: ref yang GAGAL dibaca ditampung di sini. WAJIB disebut ke user kalau
    terisi — posisi yang gagal dibaca beda dari posisi yang tidak ada, dan kalau
    dibuang diam-diam RPC sibuk terlihat seperti dana hilang.

    `fresh=True` melewati cache — WAJIB untuk jalur yang memutuskan aksi dana
    (monitor_loop/eksekutor TP-SL, snapshot sebelum migrate). Jalur tampilan boleh
    basi; jalur yang memindahkan uang tidak.

    `light=True` (monitor) membaca versi murah: `value_usd`/`unclaimed_usd`-nya
    **0**. Hasilnya karena itu TIDAK boleh masuk `_POS_CACHE` — `/list` akan
    menampilkan semua posisi bernilai $0. Mode ini melewati cache dua arah."""
    key = key or pk()
    w = _addr_of(key)
    if light:
        errs: list = []
        pos = ch.list_all_positions(cid, key, store.refs(cid, w, "v2"),
                                    store.refs(cid, w, "v4"), errors=errs, light=True)
        if errors is not None:
            errors.extend(errs)
        return pos
    ck = (cid, w)
    hit = _POS_CACHE.get(ck)
    age = (time.time() - hit[2]) if hit else None
    if not fresh and hit and age < _POS_MAX_STALE:
        if age >= _POS_FRESH_SECS:
            _pos_refresh_bg(cid, key, w)   # sajikan yang lama, segarkan di latar
        if errors is not None:
            errors.extend(hit[1])
        # Salinan dangkal: pemanggil yang menyaring/mengurutkan tidak boleh mengubah
        # daftar milik cache. Dict posisinya sendiri TETAP dipakai bersama — baca
        # saja, jangan dimutasi (jebakan yang sama dengan `store._hist()`).
        return list(hit[0])
    pos, errs = _pos_read(cid, key, w)
    if errors is not None:
        errors.extend(errs)
    return pos


def position_one(cid: int, pid, key: str | None = None) -> dict | None:
    """Satu posisi, dibaca LANGSUNG dari pid-nya. `None` = memang tidak ada.

    Dulu tiap tombol mencari posisinya dengan memindai seluruh `list_positions_all`
    lalu menyaring pid. Mahal (terukur `reb|v4:1277501` 24,7 detik untuk 8 posisi
    padahal yang dibutuhkan satu) dan RAPUH: daftar itu sengaja menelan kegagalan
    per-posisi supaya tetap tampil, jadi satu 429 dari RPC membuat posisi yang
    dicari lenyap dan UI melapor "tidak ditemukan (sudah ditutup?)" — pesan yang
    membuat user mengira dananya hilang, lalu mengklik ulang dan beraksi dua kali.
    Sekarang gagal baca dilempar sebagai error yang menyebut sebabnya."""
    try:
        return ch.position_by_pid(cid, key or pk(), pid)
    except Exception as e:
        raise RuntimeError(f"Gagal membaca posisi {disp_pid(pid)}: {e}") from e


def wallet_address() -> str:
    return _addr_of(pk())


TG_MAX_CHARS = 4096          # batas keras Telegram untuk satu pesan


def _fit(text: str) -> str:
    """Potong ke batas Telegram.

    Pesan >4096 karakter ditolak dengan BadRequest "Message is too long" — dan di
    PTB `BadRequest` itu TURUNAN `NetworkError`, jadi on_error dulu menganggapnya
    gangguan jaringan lalu menelannya diam-diam. Akibatnya operasi yang sebenarnya
    SUDAH SELESAI tidak pernah menampilkan hasil, dan bot kelihatan menggantung di
    pesan "Closing…" selamanya. Tag HTML yang terbelah ikut dibuang supaya potongan
    tetap bisa di-parse."""
    if len(text) <= TG_MAX_CHARS:
        return text
    cut = text[:TG_MAX_CHARS - 48]
    if cut.rfind("<") > cut.rfind(">"):      # jangan tinggalkan tag setengah jadi
        cut = cut[:cut.rfind("<")]
    return cut.rstrip() + "\n… <i>(dipotong — terlalu panjang)</i>"


async def reply(update: Update, text: str, kb: InlineKeyboardMarkup | None = None):
    return await update.effective_chat.send_message(
        _fit(text), parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


async def edit(msg, text: str, kb: InlineKeyboardMarkup | None = None):
    """Edit pesan status in-place; fallback kirim baru kalau gagal."""
    text = _fit(text)
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                            disable_web_page_preview=True)
    except Exception as e:
        if "not modified" in str(e).lower():
            return  # konten sama — biarkan
        await msg.get_bot().send_message(msg.chat_id, text, parse_mode=ParseMode.HTML,
                                         reply_markup=kb, disable_web_page_preview=True)


def swap_cost_lines(d: dict, qi: dict) -> list[str]:
    """Uraian biaya satu swap: fee pool DAN price impact, terpisah.

    Dulu satu angka berlabel "(fee pool + price impact)" — dan labelnya KELIRU:
    pembandingnya sudah memotong fee, jadi angka itu sebenarnya impact saja.
    Akibatnya kartu justru MENGECILKAN biaya sebenarnya, dan user membaca fee
    pool yang wajar sebagai kegagalan routing. Terukur pada close BLAST/ETH:
    kartu menulis 3,8% padahal biaya sesungguhnya 6,1% — fee pool 4,00% (memang
    segitu tarif pool-nya) + price impact 2,19%.

    Memisahkannya penting karena tindakannya beda: fee pool cuma bisa dihindari
    dengan pindah pool (dan routing sudah memilih yang termurah), sedangkan
    impact dikecilkan dengan memperkecil jumlah swap."""
    got, ideal = d.get("got") or 0, d.get("ideal") or 0
    exp = d.get("expect") or 0
    dec = qi["decimals"]
    if got <= 0 or (ideal <= 0 and exp <= 0):
        return []
    if ideal <= 0:                       # data lama tanpa `ideal`
        return [f"   └ biaya swap {(1 - got / exp) * 100:.1f}% (price impact) — "
                f"nilai wajar sebelum swap ~{ch.fmt_amount(exp / 10 ** dec)} {esc(qi['symbol'])}"]
    fee_pct = (d.get("fee_ppm") or 0) / 1e4
    impact = (1 - got / exp) if exp > 0 else 0
    total = 1 - got / ideal
    return [f"   └ biaya swap <b>{total * 100:.1f}%</b> = fee pool {fee_pct:.2f}% "
            f"+ price impact {max(0.0, impact) * 100:.2f}%",
            f"   └ tanpa fee &amp; impact dapat ~{ch.fmt_amount(ideal / 10 ** dec)} "
            f"{esc(qi['symbol'])}, diterima {ch.fmt_amount(got / 10 ** dec)}"]


def gas_line(cid: int) -> str:
    """Baris '⛽ gas' untuk kartu hasil. Kosong kalau tidak ada tx (mis. aksi batal).

    **Melakukan RPC** (`fmt_gas` → `quote_usd_price` untuk kurs native), jadi
    pemanggilnya WAJIB `await asyncio.to_thread(gas_line, cid)`. Dipanggil langsung
    di event loop, ia menahan SELURUH bot selama panggilan itu — terukur di log VPS
    `event loop tertahan 12,8 detik` tepat sebelum kartu hasil mint muncul, dan
    selama itu tidak ada klik lain yang bisa dijawab (query callback keburu
    kedaluwarsa)."""
    wei = ch.gas_spent_wei()
    return f"⛽ gas terpakai: {ch.fmt_gas(cid, wei)}" if wei else ""


async def with_progress(status, head: str, work):
    """Jalankan `work` (fungsi sinkron, di thread) sambil menyiarkan langkahnya.

    Alur mint/close/rebalance itu 3–5 tx berurutan; tanpa ini UI diam berpuluh detik
    dan user tidak tahu langkah mana yang menggantung. chain._step() dipanggil dari
    thread kerja, jadi ia cuma menumpuk teks ke list — pengeditan pesan dilakukan
    ticker di sisi async supaya tidak menyentuh Telegram dari thread lain."""
    log: list[str] = []
    ch.set_progress(log.append)

    async def ticker():
        seen = 0
        while True:
            await asyncio.sleep(5)
            if len(log) == seen:
                continue
            seen = len(log)
            body = "\n".join(f"<i>{esc(x)}</i>" for x in log[-5:])
            await edit(status, f"{head}\n\n{body}")

    tick = asyncio.create_task(ticker())
    try:
        return await asyncio.to_thread(work)
    finally:
        tick.cancel()
        ch.set_progress(None)


def range_str(p: dict) -> str:
    # tampil market cap kalau ada (lebih gampang dibaca daripada harga 0.0₆xx)
    if p.get("mc_now"):
        return (f"MC {ch.fmt_usd(p['mc_lower'])}–{ch.fmt_usd(p['mc_upper'])} "
                f"(now {ch.fmt_usd(p['mc_now'])})")
    def tick_price(t):
        raw = ch.tick_to_price(t)
        if p["quote_is_token1"]:
            return raw * 10 ** (p["dec0"] - p["dec1"])
        v = 1 / raw if raw else 0
        return v * 10 ** (p["dec1"] - p["dec0"])
    lo, hi = tick_price(p["tick_lower"]), tick_price(p["tick_upper"])
    now = tick_price(p["cur_tick"])
    if lo > hi:
        lo, hi = hi, lo
    return f"{ch.fmt_price(lo)}–{ch.fmt_price(hi)} (now {ch.fmt_price(now)})"


# ---------- Commands & menu utama ----------
HELP = (
    "<b>unipool — LP concentrated liquidity</b>\n"
    "<i>Uniswap v2/v3/v4 di Robinhood &amp; Base · PancakeSwap+Uniswap di BSC · "
    "HyperSwap di HyperEVM</i>\n\n"
    "Paste alamat token (0x...) → bot cari pool → pilih → atur strategi → mint.\n"
    "/start membuka menu utama (dashboard saldo + tombol navigasi).\n\n"
    "<b>Perintah:</b>\n"
    "/start — menu utama\n"
    "/list — posisi + PnL + chart/add/reduce/close\n"
    "/orders — pesanan TP/SL (auto-close posisi saat market cap sentuh batas)\n"
    "/wallet — saldo semua token + nilai USD\n"
    "/settings — pengaturan via tombol\n"
    "/set <code>key value</code> — set manual (width, amount, amount_pct, slippage, gap, alert, autoswap)\n"
    "/chain — ganti chain aktif\n"
    "/wallets — kelola wallet: impor/buat/ekspor/hapus\n"
    "/revoke — cabut approval token yang menganggur (keamanan)\n"
    "  <code>/revoke 0xKontrak</code> — periksa kontrak di luar daftar bot\n"
    "/cleanup — burn NFT posisi kosong (mempercepat /list)\n"
    "/recover — pulihkan posisi v4 yang ada on-chain tapi hilang dari /list\n"
    "/all — ringkasan posisi di semua chain sekaligus\n\n"
    "<b>Custom saat kartu konfirmasi aktif:</b>\n"
    "<code>r 40 120</code> — range −40%/+120%\n"
    "<code>a 30%</code> / <code>a 0.005</code> — amount"
)

def menu_kb() -> InlineKeyboardMarkup:
    rows = []
    n = len(all_pks())
    if n > 1:
        cur = active_wallet_idx()
        rows.append([InlineKeyboardButton(("✓ " if i == cur else "") + f"W{i + 1}",
                                          callback_data=f"wsel|{i}")
                     for i in range(min(n, 8))])
    rows += [
        [InlineKeyboardButton("📊 Posisi LP", callback_data="menu|list"),
         InlineKeyboardButton("🌐 Semua chain", callback_data="menu|all"),
         InlineKeyboardButton("🎯 Pesanan", callback_data="menu|orders")],
        [InlineKeyboardButton("👛 Dompet", callback_data="menu|wallet"),
         InlineKeyboardButton("🔑 Wallet", callback_data="menu|wallets"),
         InlineKeyboardButton("⚙️ Pengaturan", callback_data="menu|settings")],
        # Perawatan: dua-duanya sebelumnya cuma bisa lewat perintah ketik dan
        # praktis tak terlihat dari menu.
        [InlineKeyboardButton("🔐 Revoke", callback_data="menu|revoke"),
         InlineKeyboardButton("🧹 Cleanup NFT", callback_data="menu|cleanup"),
         InlineKeyboardButton("🩹 Recover", callback_data="menu|recover")],
        [InlineKeyboardButton("⛓ Chain", callback_data="menu|chain"),
         InlineKeyboardButton("❓ Bantuan", callback_data="menu|help")],
        [InlineKeyboardButton("🔄 Segarkan", callback_data="menu|main")],
    ]
    return InlineKeyboardMarkup(rows)


BACK_ROW = [InlineKeyboardButton("⬅️ Menu", callback_data="menu|main")]
DEL_BTN = InlineKeyboardButton("✖", callback_data="del")  # hapus pesan (anti spam chat)
NAV_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Posisi", callback_data="go|list"),
     InlineKeyboardButton("🏠 Menu", callback_data="go|main"),
     DEL_BTN],
])
DEL_KB = InlineKeyboardMarkup([[DEL_BTN]])


def build_main_menu() -> str:
    """Dashboard: saldo inti + ringkasan setting (dipanggil di thread)."""
    s = store.load_settings()
    cid = s["chain"]
    cfg = ch.CHAINS[cid]
    w3 = ch.get_w3(cid)
    addr = wallet_address()
    eth_usd = ch.quote_usd_price(w3, cid, cfg["wrapped_symbol"])
    native = w3.eth.get_balance(addr) / 1e18
    total = native * eth_usd
    # Di chain ber-native_erc20 (Arc), saldo native DAN saldo ERC20-nya itu kantong
    # yang sama — menampilkan keduanya membuat dashboard menulis USDC dua kali dan
    # Total-nya dua kali lipat dari uang yang benar-benar ada.
    ne = ch.native_erc20(cid)
    ne_note = " (native = ERC20)" if ne else ""
    bal_lines = [f"· {esc(cfg['native_symbol'])}{ne_note}: {ch.fmt_amount(native)} "
                 f"({ch.fmt_usd(native * eth_usd)})"]
    for sym, a in cfg["quotes"].items():
        if ne and str(a).lower() == ne:
            continue
        c = ch.erc20(w3, a)
        bal = c.functions.balanceOf(addr).call() / 10 ** c.functions.decimals().call()
        usd = bal * (1.0 if sym in cfg["stable_syms"] else eth_usd)
        total += usd
        bal_lines.append(f"· {esc(sym)}: {ch.fmt_amount(bal)} ({ch.fmt_usd(usd)})")
    amount = f"{s['amount_fixed']:g} fix" if s["amount_fixed"] else f"{s['amount_pct']:g}%"
    alert = f"{int(s.get('alert_secs', 60))}s" if s.get("alert_secs") else "off"
    pks = all_pks()
    wallets_line = ""
    if len(pks) > 1:
        cur = active_wallet_idx()
        parts = []
        for i, k in enumerate(pks):
            bal = w3.eth.get_balance(_addr_of(k)) / 1e18
            mark = "▸" if i == cur else ""
            parts.append(f"{mark}W{i + 1} {ch.fmt_amount(bal)}")
        wallets_line = f"👛 {' · '.join(parts)} {esc(cfg['native_symbol'])}\n"
    return (
        f"🦄 <b>unipool</b> — LP {esc(ch.dex_name(cid))} {esc(ch.versions_label(cid))}\n"
        f"⛓ {esc(cfg['name'])} (chain {cid})\n"
        f"{wallets_line}"
        f"{esc(wallet_label())}: <code>{esc(addr)}</code>\n\n"
        f"💰 <b>Saldo:</b>\n" + "\n".join(bal_lines) + "\n"
        f"<b>Total: {ch.fmt_usd(total)}</b> · 1 {esc(cfg['wrapped_symbol'])} = ${eth_usd:,.0f}\n\n"
        f"⚙️ amount {esc(amount)} · slippage {s['slippage_pct']:g}% · gap {s.get('gap', 1)} · "
        f"alert {alert} · autoswap {'ON' if s['autoswap'] else 'OFF'}\n\n"
        f"📥 Paste alamat token (<code>0x...</code>) untuk buka posisi baru."
    )


async def show_main_menu(update: Update, msg=None):
    if msg is None:
        msg = await reply(update, "⏳ Memuat menu...")
    else:
        await edit(msg, "⏳ Memuat menu...")
    try:
        text = await asyncio.to_thread(build_main_menu)
    except Exception as e:
        text = (f"🦄 <b>unipool</b>\n❌ Gagal baca saldo: {esc(e)}\n\n"
                f"Paste alamat token (<code>0x...</code>) untuk mulai.")
    await edit(msg, text, menu_kb())


# ---------- Settings via tombol ----------
SET_KEYS = ("width, amount, amount_pct, slippage, impact, gap, alert, order, "
            "autoswap, allchains")
IMPACT_STEPS = [5.0, 10.0, 15.0, 25.0, 50.0, 100.0]
ORDER_STEPS = [60, 120, 300, 600]
SLIP_STEPS = [0.5, 1.0, 3.0, 5.0, 10.0]
ALERT_STEPS = [0, 30, 60, 120, 300, 600]
AMT_STEPS = [25.0, 50.0, 75.0, 100.0]
WIDTH_STEPS = [10.0, 20.0, 30.0, 50.0, 100.0]


def apply_setting(s: dict, key: str, val: str) -> str | None:
    """Mutasi s; return pesan error atau None kalau sukses."""
    try:
        if key == "width":
            s["width_pct"] = max(0.1, float(val))
        elif key == "amount":
            s["amount_fixed"] = max(0.0, float(val)) or None
        elif key == "amount_pct":
            s["amount_pct"] = min(100.0, max(1.0, float(val)))
            s["amount_fixed"] = None
        elif key == "slippage":
            s["slippage_pct"] = min(50.0, max(0.1, float(val)))
        elif key == "gap":
            s["gap"] = min(5, max(0, int(float(val))))
        elif key == "impact":
            # Batas price impact swap. 100% = penjagaannya mati — biarkan bisa,
            # tapi kartu tetap MENAMPILKAN angkanya supaya tidak jadi diam-diam.
            s["impact_max_pct"] = min(100.0, max(1.0, float(val)))
        elif key == "alert":
            s["alert_secs"] = 0 if val in ("off", "0", "no") else max(30, int(float(val)))
        elif key == "order":
            # Interval pindai order. Lantainya 30 detik: tiap pindai membaca posisi,
            # dan itu pemakai kuota RPC terbesar.
            s["order_secs"] = max(30, int(float(val)))
        elif key == "autoswap":
            s["autoswap"] = val in ("on", "true", "1", "yes")
        elif key == "allchains":
            s["list_all_chains"] = val in ("on", "true", "1", "yes")
        else:
            return f"Key tidak dikenal: {key}"
    except ValueError:
        return "Value tidak valid."
    return None


def impact_limit() -> float:
    """Batas price impact swap sebagai pecahan 0..1, dari setelan user.

    `ch._SWAP_IMPACT_MAX` tetap jadi cadangan supaya mesin tetap punya batas
    kalau setelannya belum ada — penjagaan ini tidak boleh pernah hilang cuma
    karena settings.json lama."""
    try:
        v = float(store.load_settings().get("impact_max_pct") or 0) / 100
    except (TypeError, ValueError):
        v = 0
    return v if 0 < v <= 1 else ch._SWAP_IMPACT_MAX


def _next_step(steps: list, cur):
    try:
        return steps[(steps.index(cur) + 1) % len(steps)]
    except ValueError:
        return steps[0]


def cycle_setting(key: str):
    s = store.load_settings()
    if key == "allchains":
        store.set_global("list_all_chains", not s.get("list_all_chains", True))
        return
    if key == "slippage":
        s["slippage_pct"] = _next_step(SLIP_STEPS, s["slippage_pct"])
    elif key == "gap":
        s["gap"] = (int(s.get("gap", 1)) + 1) % 6
    elif key == "impact":
        s["impact_max_pct"] = _next_step(
            IMPACT_STEPS, float(s.get("impact_max_pct") or ch._SWAP_IMPACT_MAX * 100))
    elif key == "order":
        s["order_secs"] = _next_step(ORDER_STEPS, int(s.get("order_secs", 120) or 120))
    elif key == "alert":
        s["alert_secs"] = _next_step(ALERT_STEPS, int(s.get("alert_secs", 60) or 0))
    elif key == "autoswap":
        s["autoswap"] = not s["autoswap"]
    elif key == "amount":
        s["amount_pct"] = _next_step(AMT_STEPS, 0 if s["amount_fixed"] else s["amount_pct"])
        s["amount_fixed"] = None
    elif key == "width":
        s["width_pct"] = _next_step(WIDTH_STEPS, s["width_pct"])
    store.save_settings(s)


# Spesifikasi tiap setelan yang punya layar sendiri. Satu tabel, tiga layar
# (daftar → pilih jaringan → editor) dibangun otomatis darinya — menambah setelan
# baru cukup menambah satu entri, bukan tiga potong UI.
SETTING_SPEC: dict = {
    "slippage": {
        "emoji": "📉", "label": "Slippage", "field": "slippage_pct", "sec": "Trading",
        "desc": "Toleransi selisih harga saat mint & swap. Terlalu kecil = transaksi "
                "sering ditolak; terlalu besar = rugi lebih banyak saat pasar bergerak.",
        "fmt": lambda v: f"{v:g}%", "choices": [0.5, 1, 2, 3, 5, 10, 15, 20],
    },
    "impact": {
        "emoji": "💥", "label": "Dampak Harga", "field": "impact_max_pct", "sec": "Trading",
        "desc": "Batas price impact swap. Di atas angka ini kartu konfirmasi minta izin "
                "dulu, bukan menolak diam-diam. minOut TIDAK melindungi dari impact — "
                "quoter sudah memasukkannya, jadi ini penjagaan terpisah.",
        "fmt": lambda v: f"{v:g}%", "choices": [5, 10, 15, 25, 50, 100],
    },
    "gap": {
        "emoji": "🎯", "label": "Gap Range", "field": "gap", "sec": "Trading",
        "desc": "Jarak aman range single-sided dari harga sekarang, dalam satuan "
                "tick-spacing. 0 = menempel harga.",
        "fmt": lambda v: f"{int(v)}", "choices": [0, 1, 2, 3, 4, 5],
    },
    "autoswap": {
        "emoji": "🔁", "label": "Autoswap", "field": "autoswap", "sec": "Trading",
        "desc": "Hasil close otomatis ditukar ke quote pool. Matikan kalau kamu mau "
                "menahan token memenya — swap selalu kena fee pool + price impact.",
        "fmt": lambda v: "ON" if v else "OFF", "choices": ["on", "off"],
        "clabel": lambda v: "✅ ON" if v == "on" else "🚫 OFF",
    },
    "amount": {
        "emoji": "🔢", "label": "Jumlah Default", "field": "amount_pct", "sec": "Trading",
        "desc": "Besaran deposit default di kartu mint, sebagai persen modal. "
                "Jumlah TETAP (mis. 10 USDG) diatur di menu Tombol Jumlah.",
        "fmt": lambda v: f"{v:g}%", "choices": [10, 25, 50, 75, 100],
    },
    "width": {
        "emoji": "📐", "label": "Lebar Range", "field": "width_pct", "sec": "Trading",
        "desc": "Lebar range default dalam persen.",
        "fmt": lambda v: f"{v:g}%", "choices": [10, 25, 50, 75, 100, 150, 300],
    },
    "alert": {
        "emoji": "🔔", "label": "Alert Range", "field": "alert_secs", "sec": "Otomatisasi",
        "desc": "Interval cek posisi keluar/masuk range. <b>Ini pemakai kuota RPC "
                "terbesar</b> — makin rapat makin boros. 0 = mati.",
        "fmt": lambda v: f"{int(v)}s" if v else "OFF",
        "choices": ["off", 30, 60, 120, 300, 600],
    },
    "order": {
        "emoji": "⏱", "label": "Order TP/SL", "field": "order_secs", "sec": "Otomatisasi",
        "desc": "Interval cek pesanan TP/SL. Sama seperti Alert, tiap pindai membaca "
                "posisi — jadi angka ini langsung menentukan tagihan RPC.",
        "fmt": lambda v: f"{int(v)}s", "choices": [30, 60, 120, 300, 600],
    },
}


def _sval(cid: int, key: str):
    spec = SETTING_SPEC[key]
    return store.load_settings(cid).get(spec["field"])


def setkey_text(key: str, cid: int) -> str:
    spec = SETTING_SPEC[key]
    return (f"{spec['emoji']} <b>{esc(spec['label'])}</b> · {esc(ch.CHAINS[cid]['name'])}\n\n"
            f"{spec['desc']}\n\n"
            f"Nilai sekarang: <b>{esc(spec['fmt'](_sval(cid, key)))}</b>\n"
            f"<i>Setelan ini berlaku untuk {esc(ch.CHAINS[cid]['name'])} saja.</i>")


def setkey_kb(key: str, cid: int) -> InlineKeyboardMarkup:
    spec = SETTING_SPEC[key]
    now = spec["fmt"](_sval(cid, key))
    rows, baris = [], []
    for c in spec["choices"]:
        lbl = spec["clabel"](c) if spec.get("clabel") else \
            ("OFF" if c == "off" else spec["fmt"](float(c)))
        baris.append(InlineKeyboardButton(("✓ " if lbl.lstrip("✅🚫 ") == now else "") + lbl,
                                          callback_data=f"setv|{key}|{cid}|{c}"))
        if len(baris) == 3:
            rows.append(baris); baris = []
    if baris:
        rows.append(baris)
    if not spec.get("clabel"):
        rows.append([InlineKeyboardButton("✏️ Nilai lain…", callback_data=f"setx|{key}|{cid}")])
    rows.append([InlineKeyboardButton("⬅️ Pilih Jaringan", callback_data=f"setk|{key}"),
                 InlineKeyboardButton("⚙️ Pengaturan", callback_data="menu|settings")])
    return InlineKeyboardMarkup(rows)


def setnet_kb(key: str) -> InlineKeyboardMarkup:
    """Pilih jaringan untuk sebuah setelan. Nilai tiap chain ikut ditampilkan supaya
    user tidak perlu membuka satu per satu untuk membandingkan."""
    aktif = store.load_settings()["chain"]
    spec = SETTING_SPEC[key]
    rows, baris = [], []
    for cid, cfg in ch.CHAINS.items():
        baris.append(InlineKeyboardButton(
            ("✓ " if cid == aktif else "") + f"{cfg['name']} · {spec['fmt'](_sval(cid, key))}",
            callback_data=f"setkc|{key}|{cid}"))
        if len(baris) == 2:
            rows.append(baris); baris = []
    if baris:
        rows.append(baris)
    rows.append([InlineKeyboardButton("⬅️ Pengaturan", callback_data="menu|settings")])
    return InlineKeyboardMarkup(rows)


def _sec(judul: str) -> list:
    """Baris judul seksi. `noop` = tombol yang sengaja tidak melakukan apa-apa."""
    return [InlineKeyboardButton(f"— {judul} —", callback_data="noop")]


def settings_text() -> str:
    s = store.load_settings()
    cfg = ch.CHAINS[s["chain"]]
    w = wallet_address()
    return (
        "⚙️ <b>Pengaturan</b>\n"
        f"⛓ {esc(cfg['name'])} ({s['chain']}) · 👛 <code>{esc(w[:6])}…{esc(w[-4:])}</code>\n\n"
        "▸ = putar preset. Angka apa pun bisa diketik lewat "
        "<b>Set nilai manual</b>.\n\n"
        "· <b>Slippage</b> — toleransi harga saat mint/swap\n"
        "· <b>Impact</b> — batas price impact swap; di atasnya kartu minta izin dulu\n"
        "· <b>Gap</b> — jarak range single-sided dari harga (satuan tick-spacing)\n"
        "· <b>Autoswap</b> — hasil close otomatis ditukar ke quote pool\n"
        "· <b>Amount / Width</b> — default besaran deposit &amp; lebar range\n"
        "· <b>Alert / Order</b> — interval pindai monitor. <b>Ini yang paling "
        "menentukan pemakaian kuota RPC</b> — makin rapat makin boros."
    )


def settings_kb() -> InlineKeyboardMarkup:
    s = store.load_settings()
    cid = s["chain"]
    cfg = ch.CHAINS[cid]
    alert = f"{int(s.get('alert_secs', 60))}s" if s.get("alert_secs") else "off"
    order = f"{int(s.get('order_secs', 120))}s"
    amount = f"{s['amount_fixed']:g} fix" if s["amount_fixed"] else f"{s['amount_pct']:g}%"
    imp = float(s.get("impact_max_pct") or ch._SWAP_IMPACT_MAX * 100)
    allc = bool(s.get("list_all_chains", True))

    def kbtn(key):
        sp = SETTING_SPEC[key]
        return InlineKeyboardButton(f"{sp['emoji']} {sp['label']}", callback_data=f"setk|{key}")

    rows = [_sec("Trading")]
    trading = ["slippage", "impact", "gap", "autoswap", "amount", "width"]
    for i in range(0, len(trading), 2):
        rows.append([kbtn(k) for k in trading[i:i + 2]])
    rows += [
        _sec("Tombol & Tampilan"),
        [InlineKeyboardButton("💰 Tombol Jumlah", callback_data="setbtn"),
         InlineKeyboardButton(f"🌐 Lintas Chain · {'ON' if allc else 'OFF'}",
                              callback_data="cyc|allchains")],
        _sec("Otomatisasi"),
        [kbtn("alert"), kbtn("order")],
        _sec("Umum"),
        [InlineKeyboardButton(f"⛓ Rantai · {esc(cfg['name'])}", callback_data="menu|chain"),
         InlineKeyboardButton("👛 Dompet", callback_data="menu|wallets")],
        [InlineKeyboardButton(f"🔭 Scanner · {'ON' if (store.load_settings().get('scanner') or {}).get('on') else 'OFF'}",
                              callback_data="menu|scanner"),
         InlineKeyboardButton("🔌 Status RPC", callback_data="menu|rpc")],
        [InlineKeyboardButton("✏️ Set Manual…", callback_data="askset")],
        [InlineKeyboardButton("🗑 Reset Pengaturan", callback_data="setrst")],
        BACK_ROW,
    ]
    return InlineKeyboardMarkup(rows)


def btnnet_kb() -> InlineKeyboardMarkup:
    """Pilih jaringan yang mau dikonfigurasi — SENGAJA tidak memindah chain aktif.

    Mengatur tombol Base sambil bekerja di Robinhood harus bisa; memindahkan chain
    aktif hanya untuk mengedit tombol justru membatalkan tujuan 'tidak perlu ganti
    chain'."""
    aktif = store.load_settings()["chain"]
    rows, baris = [], []
    for cid, cfg in ch.CHAINS.items():
        baris.append(InlineKeyboardButton(("✓ " if cid == aktif else "") + cfg["name"],
                                          callback_data=f"setbtnc|{cid}"))
        if len(baris) == 2:
            rows.append(baris); baris = []
    if baris:
        rows.append(baris)
    rows.append([InlineKeyboardButton("⬅️ Pengaturan", callback_data="menu|settings")])
    return InlineKeyboardMarkup(rows)


def btn_text(cid: int) -> str:
    cfg = ch.CHAINS[cid]
    return (f"💰 <b>Tombol Jumlah</b> · {esc(cfg['name'])}\n\n"
            "Atur nilai tombol jumlah tetap yang muncul di kartu mint, per simbol. "
            "Angkanya jumlah token — <b>bukan persen</b>.\n\n"
            "<i>Satuannya mengikuti budget kartu: quote pool, atau token meme di "
            "mode Upper. Simbol tanpa tombol memakai tebakan default; tombol A% "
            "tetap ada apa pun isinya. Setelan ini per-jaringan.</i>\n"
            "Simbol lain (mis. token meme) bisa ditambah lewat "
            "<code>/presets SIMBOL 100000 500000</code>.")


def btn_kb(cid: int) -> InlineKeyboardMarkup:
    rows = []
    for sym in preset_syms(cid):
        vals = presets_get(cid, sym)
        tampil = vals or amount_presets(sym, cid)
        rows.append(_sec(sym if vals else f"{sym} · default"))
        for v in tampil:
            rows.append([
                InlineKeyboardButton(f"💰 {v:g} {sym}", callback_data="noop"),
                InlineKeyboardButton("❌ Hapus", callback_data=f"btndel|{cid}|{sym}|{v:g}"),
            ])
        if len(tampil) < 4:
            rows.append([InlineKeyboardButton(f"➕ Tambah Tombol {sym}",
                                              callback_data=f"btnadd|{cid}|{sym}")])
        if vals:
            rows.append([InlineKeyboardButton(f"↩️ Balikkan {sym} ke default",
                                              callback_data=f"btnrst|{cid}|{sym}")])
    rows.append([InlineKeyboardButton("⬅️ Pilih Jaringan", callback_data="setbtn"),
                 InlineKeyboardButton("⚙️ Pengaturan", callback_data="menu|settings")])
    return InlineKeyboardMarkup(rows)


def chain_kb() -> InlineKeyboardMarkup:
    cur = store.load_settings()["chain"]
    rows = [[InlineKeyboardButton(("✓ " if cid == cur else "") + f"{cfg['name']} ({cid})",
                                  callback_data=f"chsel|{cid}")]
            for cid, cfg in ch.CHAINS.items()]
    rows.append(BACK_ROW)
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, _):
    if not authorized(update):
        return
    await show_main_menu(update)


async def cmd_help(update: Update, _):
    if not authorized(update):
        return
    await reply(update, HELP, InlineKeyboardMarkup([BACK_ROW]))


async def cmd_settings(update: Update, _):
    if not authorized(update):
        return
    await reply(update, settings_text(), settings_kb())


_RPC_ICON = {"ok": "✅", "quota": "🛑", "burst": "⏳", "error": "❌"}


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    """/scan — jalankan satu siklus scanner SEKARANG, tanpa menunggu giliran.

    Saklar on/off dilewati, tapi konfirmasi polling dan cooldown TIDAK —
    melewatinya akan membuat perintah ini jadi tombol spam."""
    if not authorized(update):
        return
    c = scanner_cfg()
    status = await reply(update, f"🔎 Memindai {esc(', '.join(c.get('chains') or []))} "
                                 f"({esc(c.get('interval') or '5m')})…")
    try:
        app = getattr(context, "application", None) or _APP[0]
        total, kirim, catatan = await scanner_cycle(app, force=True)
    except gmgn.RateLimit as e:
        await edit(status, f"⏳ {esc(e)}")
        return
    except Exception as e:
        await edit(status, f"❌ Scan gagal: {esc(e)}")
        return
    if catatan:
        await edit(status, f"⚠️ {esc(catatan)}")
        return
    await edit(status, f"✅ {total} token lolos filter · {kirim} kartu dikirim.\n"
                       f"<i>Yang tidak dikirim: belum lolos konfirmasi "
                       f"{c.get('confirm')}/{c.get('window')} polling, atau masih dalam "
                       f"cooldown {c.get('cooldown')} menit.</i>")


def scanner_filters_line(f: dict) -> str:
    """Ringkasan filter aktif. Kunci yang TIDAK ADA berarti filternya mati."""
    out = []
    for key, _api, _field, cmp_, unit in gmgn.FILTER_SPEC:
        v = (f or {}).get(key)
        if v is None:
            continue
        tanda = "≥" if cmp_ == "gte" else "≤"
        if unit == "usd":
            nilai = ch.fmt_usd(float(v))
        elif unit == "ratio":
            nilai = f"{float(v) * 100:g}%"
        elif unit == "percent":
            nilai = f"{float(v):g}%"
        elif unit == "duration":
            nilai = str(v)
        else:
            nilai = f"{float(v):,.0f}"
        out.append(f"{key} {tanda} {nilai}")
    return " · ".join(out)


def _filt_fmt(unit: str, v) -> str:
    """Tampilan satu nilai filter menurut satuannya."""
    if v is None:
        return "—"
    if unit == "usd":
        return ch.fmt_usd(float(v))
    if unit == "ratio":
        return f"{float(v) * 100:g}%"
    if unit == "percent":
        return f"{float(v):+g}%"
    if unit == "duration":
        return str(v)
    return f"{float(v):,.0f}"


def scanfilt_text() -> str:
    f = scanner_cfg().get("filters") or {}
    aktif = sum(1 for v in f.values() if v is not None)
    return ("🎚 <b>Filter scanner</b>\n\n"
            f"{aktif} filter aktif. Klik untuk mengubah atau mematikan.\n\n"
            "<i>Filter yang MATI tidak menyaring apa pun. Yang hidup menolak token "
            "yang datanya BELUM DIKETAHUI — sama seperti perilaku server GMGN, "
            "supaya 'belum diuji' tidak lolos seolah-olah 'aman'.</i>\n"
            "<i>Filter ber-awalan min/max dikirim ke server GMGN sebagai saringan "
            "kasar lalu DICEK ULANG di sini; beberapa (turnover, rug, perubahan "
            "harga) memang cuma bisa dihitung lokal.</i>")


def scanfilt_kb() -> InlineKeyboardMarkup:
    f = scanner_cfg().get("filters") or {}
    rows = []
    for judul, keys in gmgn.FILTER_GROUPS:
        rows.append(_sec(judul))
        baris = []
        for k in keys:
            spec = gmgn.filter_spec(k)
            if not spec:
                continue
            nilai = _filt_fmt(spec[4], f.get(k))
            tanda = "≥" if spec[3] == "gte" else "≤"
            aktif = "" if f.get(k) is None else "✓ "
            baris.append(InlineKeyboardButton(f"{aktif}{k} {tanda} {nilai}",
                                              callback_data=f"sf|{k}"))
            if len(baris) == 2:
                rows.append(baris); baris = []
        if baris:
            rows.append(baris)
    rows.append([InlineKeyboardButton("↩️ Balikkan semua ke default", callback_data="sf|__reset"),
                 InlineKeyboardButton("⬅️ Scanner", callback_data="menu|scanner")])
    return InlineKeyboardMarkup(rows)


def scanfilt_one_text(key: str) -> str:
    spec = gmgn.filter_spec(key)
    f = scanner_cfg().get("filters") or {}
    tanda = "minimum" if spec[3] == "gte" else "maksimum"
    lokal = "" if spec[1] else ("\n<i>Hanya dihitung lokal — GMGN tidak punya "
                                "parameter server untuk filter ini.</i>")
    return (f"🎚 <b>{esc(key)}</b>\n"
            f"{esc(gmgn.FILTER_DESC.get(key, key))} — ambang <b>{tanda}</b>.\n\n"
            f"Sekarang: <b>{esc(_filt_fmt(spec[4], f.get(key)))}</b>"
            f"{lokal}\n\n"
            f"<i>Token yang nilainya belum diketahui TIDAK lolos selama filter ini "
            f"hidup.</i>")


def scanfilt_one_kb(key: str) -> InlineKeyboardMarkup:
    spec = gmgn.filter_spec(key)
    f = scanner_cfg().get("filters") or {}
    now = f.get(key)
    rows, baris = [], []
    for c in gmgn.FILTER_CHOICES.get(spec[4], []):
        sama = str(now) == str(c) or (isinstance(now, (int, float))
                                      and isinstance(c, (int, float)) and float(now) == float(c))
        baris.append(InlineKeyboardButton(("✓ " if sama else "") + _filt_fmt(spec[4], c),
                                          callback_data=f"sfv|{key}|{c}"))
        if len(baris) == 3:
            rows.append(baris); baris = []
    if baris:
        rows.append(baris)
    rows.append([InlineKeyboardButton("✏️ Nilai lain…", callback_data=f"sfx|{key}"),
                 InlineKeyboardButton("🚫 Matikan filter", callback_data=f"sfv|{key}|off")])
    rows.append([InlineKeyboardButton("⬅️ Daftar filter", callback_data="sf|__list"),
                 InlineKeyboardButton("🔭 Scanner", callback_data="menu|scanner")])
    return InlineKeyboardMarkup(rows)


def scanfilt_set(key: str, raw) -> str | None:
    """Setel satu filter. Return pesan error, atau None kalau sukses.

    Validasinya sama dengan `/scanner set` — satu tempat, supaya tombol dan
    perintah teks tidak bisa berbeda aturan."""
    spec = gmgn.filter_spec(key)
    if not spec:
        return f"Filter tidak dikenal: {key}"
    f = dict(scanner_cfg().get("filters") or {})
    v = str(raw).strip()
    if v.lower() in ("off", "-", "none", "mati"):
        f.pop(key, None)
    elif spec[4] == "duration":
        if gmgn.duration_secs(v) is None:
            return "Umur harus bentuk 30m / 6h / 7d."
        f[key] = v
    else:
        try:
            num = float(v.replace(",", "."))
        except ValueError:
            return f"'{v}' bukan angka."
        if spec[4] == "ratio" and not (0 <= num <= 1):
            return "Rasio harus 0–1 (mis. 0.2 untuk 20%)."
        if spec[4] in ("usd", "count") and num < 0:
            return "Nilai tidak boleh negatif."
        f[key] = num
    scanner_save({"filters": f})
    return None


def scanner_text() -> str:
    c = scanner_cfg()
    f = c.get("filters") or {}
    key = "✅ ada" if gmgn.api_key() else "❌ <b>belum diisi</b> (GMGN_API_KEY)"
    aktif = [x for x in (c.get("chains") or []) if _lp_cid(x)]
    lain = [x for x in (c.get("chains") or []) if not _lp_cid(x)]
    L = [f"🔭 <b>Scanner token trending</b> · {'🟢 ON' if c.get('on') else '🔴 OFF'}",
         f"Kunci GMGN: {key}",
         "",
         f"⛓ Chain: {esc(', '.join(aktif) or '—')}"
         + (f" · <i>{esc(', '.join(lain))} (kartu info saja, bot tidak bisa LP di sana)</i>" if lain else ""),
         f"⏱ Interval data {esc(c.get('interval'))} · pindai tiap {c.get('watch')} detik · "
         f"jeda antar request {c.get('pace')}s",
         f"🎯 Kirim maks {c.get('top')} kartu per chain tiap siklus",
         f"🔁 Konfirmasi {c.get('confirm')}/{c.get('window')}: token harus muncul di "
         f"{c.get('confirm')} dari {c.get('window')} scan terakhir sebelum dikirim — "
         f"menyaring lonjakan satu-tick.",
         f"❄️ Cooldown {c.get('cooldown')} menit: <b>per TOKEN</b>, bukan per scan. "
         f"Token yang sudah dikirim tidak dikirim lagi selama itu; scan tetap jalan "
         f"tiap {c.get('watch')} detik dan token LAIN tetap masuk.",
         "",
         "<b>Filter</b> " + (f"({len([1 for k in f if f[k] is not None])} aktif)"
                             if any(v is not None for v in f.values()) else "(tidak ada)"),
         # Dibangun dari FILTER_SPEC, bukan ditulis satu per satu: filter yang
         # DIMATIKAN tidak ada kuncinya, dan menuliskannya manual bikin
         # fmt_usd(None) meledak — pernah kejadian persis begitu.
         esc(scanner_filters_line(f)) or "—",
         "",
         "<i>Ambang risiko menolak token yang datanya BELUM DIKETAHUI — sama seperti "
         "perilaku server GMGN, supaya 'belum diuji' tidak lolos seolah-olah 'aman'.</i>",
         "<i>Ubah nilai: </i><code>/scanner set minVolume 500000</code><i>, "
         "chain: </i><code>/scanner chains robinhood,base</code>"]
    return "\n".join(L)


def scanner_kb() -> InlineKeyboardMarkup:
    c = scanner_cfg()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'🔴 Matikan' if c.get('on') else '🟢 Nyalakan'}",
                              callback_data="scan|toggle"),
         InlineKeyboardButton("🔎 Pindai sekarang", callback_data="scan|now")],
        [InlineKeyboardButton(f"⏱ Interval · {c.get('interval')}", callback_data="scan|iv"),
         InlineKeyboardButton(f"🎯 Top · {c.get('top')}", callback_data="scan|top")],
        [InlineKeyboardButton(f"🕐 Tiap {c.get('watch')}s", callback_data="scan|watch"),
         InlineKeyboardButton(f"❄️ Cooldown {c.get('cooldown')}m", callback_data="scan|cool")],
        [InlineKeyboardButton(f"🎚 Filter ({sum(1 for v in (c.get('filters') or {}).values() if v is not None)} aktif)",
                              callback_data="sf|__list"),
         InlineKeyboardButton("⛓ Chain", callback_data="scan|chains")],
        BACK_ROW,
    ])


async def cmd_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scanner — status + setelan. `set <kunci> <nilai>` / `chains <a,b>`."""
    if not authorized(update):
        return
    args = getattr(context, "args", None) or []
    if args and args[0].lower() == "chains":
        pilih = [x.strip().lower() for x in " ".join(args[1:]).replace(",", " ").split() if x.strip()]
        tak = [x for x in pilih if x not in gmgn.CHAINS]
        if tak:
            await reply(update, f"❌ Chain tidak dikenal: {esc(', '.join(tak))}\n"
                                f"Pilihan: {esc(', '.join(gmgn.CHAINS))}")
            return
        scanner_save({"chains": pilih})
        await reply(update, scanner_text(), scanner_kb())
        return
    if args and args[0].lower() == "set" and len(args) >= 3:
        k, v = args[1], args[2]
        c = scanner_cfg()
        if k in ("on", "include_skip"):
            scanner_save({k: v.lower() in ("on", "true", "1", "yes")})
        elif k in ("interval",):
            if v not in gmgn.INTERVALS:
                await reply(update, f"❌ Interval harus salah satu: {esc(', '.join(gmgn.INTERVALS))}")
                return
            scanner_save({"interval": v})
        elif k in ("watch", "top", "confirm", "window", "limit"):
            scanner_save({k: max(1, int(float(v)))})
        elif k in ("cooldown", "pace"):
            scanner_save({k: max(0.2, float(v))})
        elif gmgn.filter_spec(k):
            # Validasi filter hidup di SATU tempat (`scanfilt_set`) supaya tombol
            # dan perintah teks tidak bisa punya aturan yang berbeda.
            err = scanfilt_set(k, v)
            if err:
                await reply(update, f"❌ {esc(err)}")
                return
        else:
            await reply(update, f"❌ Kunci tidak dikenal: <code>{esc(k)}</code>\n"
                                f"Filter: {esc(', '.join(f[0] for f in gmgn.FILTER_SPEC))}\n"
                                f"Lain: on, interval, watch, top, confirm, window, limit, "
                                f"cooldown, pace, include_skip")
            return
        await reply(update, scanner_text(), scanner_kb())
        return
    await reply(update, scanner_text(), scanner_kb())


async def cmd_rpc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rpc — status tiap endpoint RPC. /rpc all untuk semua chain.

    Kuota TERSISA tidak bisa dibaca (Alchemy tidak mengeksposnya lewat API key),
    tapi key yang SUDAH habis bisa: 429-nya menyebut "Monthly capacity limit
    exceeded". Tanpa perintah ini gejalanya cuma "bot lambat", dan tidak ada
    perubahan kode yang bisa memperbaikinya."""
    if not authorized(update):
        return
    args = getattr(context, "args", None) or []      # context None saat dipanggil dari tombol
    semua = bool(args) and str(args[0]).lower() in ("all", "semua")
    cids = list(ch.CHAINS) if semua else [store.load_settings()["chain"]]
    status = await reply(update, "🔌 Mengecek RPC…")
    rows = await asyncio.to_thread(lambda: [(c, ch.rpc_health(c)) for c in cids])
    lines, mati = [], []
    for cid, hs in rows:
        lines.append(f"\n<b>{esc(ch.CHAINS[cid]['name'])}</b>")
        for h in hs:
            ms = f"{h['ms']} ms" if h["ms"] is not None else "—"
            lines.append(f"{_RPC_ICON.get(h['kind'], '❔')} <code>{esc(h['short'])}</code>"
                         f"\n    {ms} · {esc(h['why'][:90])}")
            if h["kind"] == "quota":
                mati.append(h["short"])
    if mati:
        lines.append("\n🛑 <b>Key di atas habis jatah BULANAN</b> — tidak akan pulih "
                     "sampai siklus billing berganti. Tambah key baru di "
                     "<code>alchemy_keys.txt</code> (satu per baris) atau naikkan paket. "
                     "Bot melewatinya 6 jam lalu mencoba lagi.")
    lines.append("\n<i>Sisa kuota tidak bisa dibaca lewat API key — Alchemy tidak "
                 "mengeksposnya. Yang terbaca cuma status per endpoint di atas.</i>")
    await edit(status, "🔌 <b>Status RPC</b>\n" + "\n".join(lines))


async def cmd_presets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/presets — atur tombol jumlah TETAP di kartu mint, per simbol.

    /presets                  lihat semua
    /presets USDG 10 20 30    ganti daftar USDG
    /presets USDG -           hapus (kembali ke tebakan default)

    Angkanya jumlah token, bukan persen: "10" untuk USDG berarti 10 USDG.
    Satuannya mengikuti budget kartu — quote pool, atau token meme di mode Upper."""
    if not authorized(update):
        return
    args = context.args or []
    cid = store.load_settings()["chain"]
    nama = ch.CHAINS[cid]["name"]
    if not args:
        baris = []
        for sym in preset_syms(cid):
            v = presets_get(cid, sym)
            baris.append(f"· <code>{esc(sym)}</code>: "
                         + (", ".join(f"{x:g}" for x in v) if v
                            else "<i>tebakan default (" +
                                 ", ".join(f"{x:g}" for x in amount_presets(sym, cid)) + ")</i>"))
        await reply(update, f"🔢 <b>Tombol jumlah tetap</b> · {esc(nama)}\n"
                    + "\n".join(baris)
                    + "\n\nUbah: <code>/presets USDG 10 20 30</code>"
                      "\nHapus: <code>/presets USDG -</code>"
                      "\n\n<i>Angka = jumlah token, bukan persen. Satuannya mengikuti "
                      "budget kartu: quote pool, atau token meme di mode Upper. "
                      "Setelan ini per-chain — mengubahnya di sini tidak menyentuh "
                      "chain lain. Lewat tombol: /settings → 💰 Tombol jumlah.</i>")
        return
    sym = args[0].upper()
    if len(args) == 1 or args[1] == "-":
        presets_set(cid, sym, [])
        await reply(update, f"🗑 Preset <b>{esc(sym)}</b> di {esc(nama)} dihapus — "
                            f"kembali ke tebakan default.")
        return
    vals = []
    for a in args[1:5]:
        try:
            v = float(a.replace(",", "."))
        except ValueError:
            await reply(update, f"❌ <code>{esc(a)}</code> bukan angka.")
            return
        if v <= 0:
            await reply(update, "❌ Jumlah harus lebih dari 0.")
            return
        vals.append(v)
    vals = presets_set(cid, sym, vals)
    await reply(update, f"✅ Preset <b>{esc(sym)}</b> di {esc(nama)}: "
                        + ", ".join(f"{v:g}" for v in vals)
                        + "\n<i>Maksimal 4 tombol; sisanya diabaikan.</i>")


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    args = context.args or []
    if len(args) != 2:
        await reply(update, f"Format: /set key value ({SET_KEYS})")
        return
    s = store.load_settings()
    err = apply_setting(s, args[0].lower(), args[1].lower())
    if err:
        await reply(update, f"❌ {esc(err)}")
        return
    store.save_settings(s)
    await reply(update, settings_text(), settings_kb())


async def cmd_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    args = context.args or []
    if args and args[0].isdigit() and int(args[0]) in ch.CHAINS:
        store.set_chain(int(args[0]))
        await reply(update, f"✅ Chain aktif: {s['chain']} ({esc(ch.CHAINS[s['chain']]['name'])})")
        return
    await reply(update, "⛓ <b>Pilih chain aktif:</b>", chain_kb())


WAL_PAGE = 6  # token ERC20 per halaman


def wallet_text(page: int = 0) -> tuple[str, int, int]:
    """Saldo semua token + USD, token ERC20 dipaginasi.
    Return (text, page, pages). Dipanggil di thread."""
    s = store.load_settings()
    cid = s["chain"]
    cfg = ch.CHAINS[cid]
    w3 = ch.get_w3(cid)
    addr = wallet_address()
    eth_usd = ch.quote_usd_price(w3, cid, cfg["wrapped_symbol"])
    lines = [f"<b>Wallet {esc(wallet_label())}</b> <code>{esc(addr)}</code> — {esc(cfg['name'])}"]
    total = 0.0
    native = w3.eth.get_balance(addr) / 1e18
    total += native * eth_usd
    # native_erc20 (Arc): saldo native dan ERC20-nya satu kantong — sekali saja
    ne = ch.native_erc20(cid)
    lines.append(f"{esc(cfg['native_symbol'])}{' (= ERC20)' if ne else ''}: "
                 f"{ch.fmt_amount(native)} ({ch.fmt_usd(native * eth_usd)})")
    for sym, a in cfg["quotes"].items():
        if ne and str(a).lower() == ne:
            continue
        c = ch.erc20(w3, a)
        bal = c.functions.balanceOf(addr).call() / 10 ** c.functions.decimals().call()
        usd = bal * (1.0 if sym in cfg["stable_syms"] else eth_usd)
        total += usd
        lines.append(f"{esc(sym)}: {ch.fmt_amount(bal)} ({ch.fmt_usd(usd)})")
    # token ERC20 lain (meme hasil close, dll) — via Alchemy, urut nilai USD
    quote_addrs = {a.lower() for a in cfg["quotes"].values()}
    toks = []
    for t in ch.wallet_tokens(cid, addr):
        if t["address"].lower() in quote_addrs:
            continue
        bal = t["raw"] / 10 ** t["decimals"]
        price = ch.token_usd_price(w3, cid, t["address"])
        usd = bal * price
        total += usd
        toks.append((usd, price, bal, t["symbol"], t["address"]))
    toks.sort(key=lambda x: -x[0])
    pages = max(1, -(-len(toks) // WAL_PAGE))
    page = min(max(0, page), pages - 1)
    if toks:
        lines.append(f"\n🪙 <b>Token ({len(toks)})</b> — halaman {page + 1}/{pages}:")
    for usd, price, bal, sym, address in toks[page * WAL_PAGE:(page + 1) * WAL_PAGE]:
        usd_txt = f" ({ch.fmt_usd(usd)})" if price else " (harga ?)"
        lines.append(f"{esc(sym)}: {ch.fmt_amount(bal)}{usd_txt}")
        lines.append(f"<code>{esc(address)}</code>")
    lines.append(f"\n<b>Total: {ch.fmt_usd(total)}</b> · 1 {esc(cfg['wrapped_symbol'])} = ${eth_usd:,.0f}")
    return "\n".join(lines), page, pages


def wallet_kb(page: int = 0, pages: int = 1) -> InlineKeyboardMarkup:
    rows = []
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"wal|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"wal|{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔄 Segarkan", callback_data=f"wal|{page}")])
    rows.append(BACK_ROW)
    return InlineKeyboardMarkup(rows)


# ---------- Kelola wallet (tambah / buat / ekspor / hapus) ----------
def wallets_text() -> str:
    cur = active_wallet_idx()
    lines = ["👛 <b>Kelola wallet</b>\n"]
    for i, k in enumerate(all_pks()):
        src = ".env" if is_env_pk(k) else "brankas bot"
        nm = wallet_name(k)
        lines.append(f"{'▸ ' if i == cur else '   '}<b>W{i + 1}</b>{' ' + esc(nm) if nm else ''} "
                     f"<code>{esc(_addr_of(k))}</code>\n     <i>{src}</i>")
    lines.append(
        "\n⚠️ Wallet dari <code>.env</code> tidak bisa dihapus lewat bot — ubah filenya "
        "lalu restart. Wallet tambahan disimpan di <code>wallets.json</code> "
        "(permission 600, tidak ikut git).")
    return "\n".join(lines)


def wallets_kb() -> InlineKeyboardMarkup:
    rows = []
    pks = all_pks()
    cur = active_wallet_idx()
    for i in range(0, min(len(pks), 8), 4):
        rows.append([InlineKeyboardButton(("✓ " if j == cur else "") + f"W{j + 1}",
                                          callback_data=f"wsel|{j}")
                     for j in range(i, min(i + 4, len(pks), 8))])
    rows.append([InlineKeyboardButton("➕ Impor key", callback_data="wal2|import"),
                 InlineKeyboardButton("🆕 Buat baru", callback_data="wal2|new")])
    rows.append([InlineKeyboardButton("🔑 Ekspor key", callback_data="wal2|exportmenu"),
                 InlineKeyboardButton("🗑 Hapus", callback_data="wal2|delmenu")])
    rows.append([InlineKeyboardButton("‹ Menu", callback_data="menu|home")])
    return InlineKeyboardMarkup(rows)


def wallet_pick_kb(action: str) -> InlineKeyboardMarkup:
    """Daftar wallet untuk dipilih (ekspor/hapus). Wallet .env tidak bisa dihapus."""
    rows = []
    for i, k in enumerate(all_pks()):
        if action == "del" and is_env_pk(k):
            continue
        rows.append([InlineKeyboardButton(f"W{i + 1} · {_addr_of(k)[:8]}…{_addr_of(k)[-4:]}",
                                          callback_data=f"wal2|{action}|{i}")])
    rows.append([InlineKeyboardButton("‹ Batal", callback_data="wal2|back")])
    return InlineKeyboardMarkup(rows)


async def _autodelete(msg, secs: int = 60):
    """Hapus pesan berisi rahasia setelah beberapa detik."""
    try:
        await asyncio.sleep(secs)
        await msg.delete()
    except Exception:
        pass


async def handle_wallets_cb(update: Update, q, data: str):
    """Router menu wallet. Setiap aksi yang menyentuh private key butuh konfirmasi."""
    parts = data.split("|")
    act = parts[1]
    if act == "import":
        AWAITING[update.effective_chat.id] = {"kind": "wallet_import", "key": ""}
        await edit(q.message,
                   "🔑 <b>Impor wallet</b>\n\nBalas pesan ini dengan private key "
                   "(64 hex, boleh pakai awalan <code>0x</code>).\n\n"
                   "⚠️ Pesanmu akan otomatis dihapus setelah dibaca, tapi key tetap "
                   "sempat melewati server Telegram. Jangan impor wallet utama.",
                   InlineKeyboardMarkup([[InlineKeyboardButton("‹ Batal", callback_data="wal2|back")]]))
        return
    if act == "new":
        from web3 import Web3
        acct = Web3().eth.account.create()
        key = acct.key.hex()
        key = key if key.startswith("0x") else "0x" + key
        store.add_wallet(key, "baru")
        await edit(q.message,
                   f"✅ <b>Wallet baru dibuat</b>\n<code>{esc(acct.address)}</code>\n\n"
                   f"<i>Private key TIDAK ditampilkan di sini. Pakai tombol Ekspor kalau "
                   f"benar-benar perlu mencadangkannya.</i>", wallets_kb())
        return
    if act in ("exportmenu", "delmenu"):
        kind = "export" if act == "exportmenu" else "del"
        pks = all_pks()
        if kind == "del" and all(is_env_pk(k) for k in pks):
            await edit(q.message, "Tidak ada wallet yang bisa dihapus — semuanya dari "
                                  "<code>.env</code>.", wallets_kb())
            return
        title = ("🔑 Pilih wallet yang mau <b>diekspor</b>:" if kind == "export"
                 else "🗑 Pilih wallet yang mau <b>dihapus</b>:")
        await edit(q.message, title, wallet_pick_kb(kind))
        return
    if act in ("export", "del") and len(parts) == 3:
        i = int(parts[2])
        pks = all_pks()
        if i >= len(pks):
            await edit(q.message, "⚠️ Wallet sudah berubah. Buka menu lagi.", wallets_kb())
            return
        addr = _addr_of(pks[i])
        if act == "export":
            await edit(q.message,
                       f"🔑 <b>Ekspor W{i + 1}</b>\n<code>{esc(addr)}</code>\n\n"
                       f"⚠️ Private key akan dikirim sebagai pesan chat. Siapa pun yang "
                       f"bisa membuka Telegram-mu (atau backup-nya) bisa mengambil seluruh "
                       f"dana wallet ini. Pesannya dihapus otomatis 60 detik.",
                       InlineKeyboardMarkup([[
                           InlineKeyboardButton("Ya, tampilkan", callback_data=f"wal2|export2|{i}"),
                           InlineKeyboardButton("‹ Batal", callback_data="wal2|back")]]))
        else:
            await edit(q.message,
                       f"🗑 <b>Hapus W{i + 1}?</b>\n<code>{esc(addr)}</code>\n\n"
                       f"⚠️ Key-nya dibuang dari <code>wallets.json</code>. Kalau belum "
                       f"kamu cadangkan, dana di wallet ini TIDAK BISA diakses lagi. "
                       f"Ekspor dulu kalau ragu.",
                       InlineKeyboardMarkup([[
                           InlineKeyboardButton("Ya, hapus", callback_data=f"wal2|del2|{i}"),
                           InlineKeyboardButton("‹ Batal", callback_data="wal2|back")]]))
        return
    if act == "export2" and len(parts) == 3:
        i = int(parts[2])
        pks = all_pks()
        if i >= len(pks):
            await edit(q.message, "⚠️ Wallet sudah berubah.", wallets_kb())
            return
        m = await q.message.reply_text(
            f"<code>{esc(pks[i])}</code>\n\n⏳ dihapus 60 detik lagi",
            parse_mode=ParseMode.HTML)
        asyncio.create_task(_autodelete(m, 60))
        await edit(q.message, wallets_text(), wallets_kb())
        return
    if act == "del2" and len(parts) == 3:
        i = int(parts[2])
        pks = all_pks()
        if i >= len(pks) or is_env_pk(pks[i]):
            await edit(q.message, "⚠️ Wallet itu dari .env — tidak bisa dihapus lewat bot.",
                       wallets_kb())
            return
        store.remove_wallet(pks[i])
        store.set_global("wallet_idx", 0)   # jangan menunjuk wallet yang sudah hilang
        await edit(q.message, "✅ Wallet dihapus.", wallets_kb())
        return
    await edit(q.message, wallets_text(), wallets_kb())


async def cmd_wallets(update: Update, _):
    if not authorized(update):
        return
    await reply(update, wallets_text(), wallets_kb())


async def cmd_wallet(update: Update, _, status_msg=None, page: int = 0):
    if not authorized(update):
        return
    if status_msg is None:
        msg = await reply(update, "⏳ Memuat wallet...")
    else:
        msg = status_msg
        await edit(msg, "⏳ Memuat wallet...")
    try:
        text, page, pages = await asyncio.to_thread(wallet_text, page)
    except Exception as e:
        text, pages = f"❌ Gagal baca wallet: {esc(e)}", 1
    await edit(msg, text, wallet_kb(page, pages))


# ---------- Discovery: paste alamat ----------
async def on_address(update: Update, _):
    if not authorized(update):
        return
    text = (update.message.text or "").strip()
    # paste alamat baru membatalkan mode nunggu-balasan
    if ADDR_RE.search(text):
        AWAITING.pop(update.effective_chat.id, None)
    elif await handle_awaiting(update):
        return
    # input custom untuk kartu konfirmasi aktif: `r 40 120` (range %), `a 0.005` / `a 30%`
    mc = CUSTOM_RANGE_RE.match(text)
    if mc:
        await apply_custom(update, rng=(float(mc.group(1)), float(mc.group(2)) if mc.group(2) else None))
        return
    mc = CUSTOM_AMT_RE.match(text)
    if mc:
        await apply_custom(update, amt=(float(mc.group(1)), mc.group(2) == "%"))
        return
    m = ADDR_RE.search(text)
    if not m:
        return
    token = m.group(1)
    s = store.load_settings()
    cid = s["chain"]
    # Paste alamat = mulai dari nol. Status alur lain (mis. pindah pool yang
    # ditinggalkan) tidak boleh ikut terbawa.
    MIGRATE.pop(update.effective_chat.id, None)
    status = await reply(update, "🔎 Mencari chain untuk token ini…")
    # Token yang ditempel belum tentu di chain aktif. Krystal memetakan token→chain
    # dalam SATU request: endpoint top_pools jalan tanpa `chainId` dan tiap entri
    # membawa chainId sendiri — itu juga cara defi.krystal.app/pools bekerja.
    try:
        hits = await asyncio.to_thread(ch.token_chains, token)
    except Exception:
        hits = []
    found = [c for c, _ in hits]
    if found and cid not in found:
        if len(found) == 1:
            cid = found[0]
            store.set_chain(cid)
            s = store.load_settings()
            await edit(status, f"⛓ Token ini ada di <b>{esc(ch.CHAINS[cid]['name'])}</b> — "
                               f"chain aktif dipindah ke sana.")
        else:
            # Beberapa chain → biar user yang pilih; jangan menebak pakai uang orang.
            rows = [[InlineKeyboardButton(
                f"{ch.CHAINS[c]['name']} · TVL {fmt_short(v)}", callback_data=f"chtok|{c}|{token}")]
                for c, v in hits]
            rows.append([InlineKeyboardButton("✖ Cancel", callback_data="cancel")])
            await edit(status, (f"⛓ Token ini punya pool di <b>{len(found)} chain</b>. "
                                f"Pilih yang mana:"), InlineKeyboardMarkup(rows))
            return
    await show_pools_for(status, cid, token)


async def show_pools_for(status, cid: int, token: str, extra: dict | None = None):
    """Discovery + daftar pool untuk (chain, token). Dipisah dari on_address supaya
    tombol pilih-chain bisa memakai jalur yang sama persis.

    `extra` = entri alert token trending (lihat `_lp_alert`). Kalau ada, saran
    posisi ikut ditempel di kartu. Jalur tombolnya TIDAK berubah sama sekali —
    alert masuk ke alur konfirmasi mint yang sudah ada, jadi tidak ada jalur
    transaksi baru yang perlu diuji ulang."""
    s = store.load_settings()
    cfg = ch.CHAINS[cid]
    amount_desc = f"amount {s['amount_fixed']} fix" if s["amount_fixed"] else f"amount {s['amount_pct']}%"
    await edit(status, (
        f"⏳ Fetching {esc(ch.dex_name(cid))} {esc(ch.versions_label(cid))} pools "
        f"on {esc(cfg['name'])}...\n"
        f"(width {s['width_pct']:g}% · {esc(amount_desc)} · deposit auto)"))

    import time as _t
    t0 = _t.time()
    try:
        res = await asyncio.to_thread(ch.discover_any, cid, token)
    except Exception as e:
        await edit(status, f"❌ Gagal fetch: {esc(e)}")
        return

    pools = res["pools"]
    if not pools:
        # Krystal tidak tahu token ini (kalau tahu, chain-nya sudah dipindah di
        # on_address). Cek kontraknya benar-benar ada di chain lain — satu
        # eth_getCode per chain, cuma dibayar di jalur gagal ini.
        others = [c for c in await asyncio.to_thread(ch.token_chains_onchain, token) if c != cid]
        extra = ""
        if others:
            extra = ("\n\n<i>Kontrak ini juga ada di: "
                     + ", ".join(esc(ch.CHAINS[c]["name"]) for c in others)
                     + " — pindah dengan /chain lalu tempel lagi.</i>")
        await edit(status, f"❌ Tidak ada pool {esc(ch.versions_label(cid))} untuk "
                           f"{esc(res['token']['symbol'])} di {esc(cfg['name'])}.{extra}")
        return

    top = pools[:10]
    tsym = res["token"]["symbol"]
    # Tabel monospace (<pre>) — 42 kolom, muat di layar HP tanpa wrap.
    # V/TVL = volume 24 jam ÷ TVL. Ini yang menunjukkan pool benar-benar dipakai:
    # TVL besar tapi rasio kecil = modal nganggur, fee-nya tipis.
    rows = [f"{'#':>2} {'pool':<7} {'fee':>6} {'TVL':>6} {'APR':>5} {'1D':>5} {'V/TVL':>6}"]
    buttons = []
    for i, p in enumerate(top, 1):
        key = uuid.uuid4().hex[:10]
        PENDING[key] = {"chain": cid, "token": res["token"], "pool_info": p,
                        "mode": "v2" if p.get("ver") == 2 else "lower",
                        "low_pct": s["width_pct"], "up_pct": 100.0,
                        "amount_pct": s["amount_pct"], "amount_fixed": s["amount_fixed"],
                        "amount_src": "quote",   # saldo yang dipersenkan tombol A%
                        "gap": int(s.get("gap", 1)), "vol": None, "rec": None}
        ver = p.get("ver", 3)
        # tanda DEX cuma muncul di chain ber-DEX ganda (BSC: P=PancakeSwap, U=Uniswap)
        dtag = (p.get("dex") or "")[:1] if len(ch.dex_names(cid)) > 1 else ""
        warn = "!" if p.get("deviation") else ""
        rows.append(
            f"{i:>2} {f'v{ver}{dtag}{warn} ' + p['quote_sym'][:5]:<7} {p['fee'] / 10000:>5.2f}% "
            f"{fmt_short(p['tvl_usd']):>6} {fmt_pct_short(p.get('apr_pct')):>5} "
            f"{fmt_short(p.get('vol24_usd')):>5} {fmt_ratio(p.get('vol24_usd'), p.get('tvl_usd')):>6}")
        buttons.append([InlineKeyboardButton(
            f"{i}. [{esc(p.get('dex') or '')} v{ver}] {p['quote_sym']} "
            f"{p['fee'] / 10000:.2f}% · {ch.fmt_usd(p['tvl_usd'])}",
            callback_data=f"pool|{key}")])
    # Buat pool v4 ber-fee/spacing custom. Cuma muncul kalau chain ini punya v4 DAN
    # sudah ada pool rujukan — harga awal pool baru disalin dari pool terdalam yang
    # pasangan currency-nya sama, tidak pernah ditebak (lihat v4_ref_sqrt_price).
    if ch.has_v4(cid) and pools:
        nk = uuid.uuid4().hex[:10]
        NEWPOOL[nk] = {"chain": cid, "token": res["token"], "pools": pools,
                       "quote_addr": None, "fee": 10000, "spacing": None}
        buttons.append([InlineKeyboardButton("➕ Buat pool baru (fee/kisi custom)",
                                             callback_data=f"np|{nk}")])
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="cancel")])
    # pool yang disaring — sebutkan, jangan hilang diam-diam
    hooks_n = res.get("hook_pools") or 0
    dead = res.get("dropped_dead") or []
    off = res.get("dropped_offprice") or []
    off_line = ""
    if hooks_n:
        off_line += (f"\n🪝 {hooks_n} pool v4 <b>ber-hooks</b> tidak ditampilkan — hook itu "
                     f"kontrak arbitrer yang ikut jalan tiap swap/mint/burn dan bisa "
                     f"menahan dana. Sengaja tidak didukung.")
    if dead:
        off_line += (f"\n🔇 {len(dead)} pool disembunyikan — tanpa TVL/volume 24 jam "
                     f"(pool mati; yang TVL-nya masih ≥5% pool terdalam tetap ditampilkan).")
    if off:
        det = ", ".join(f"[v{d.get('ver', 3)}] {esc(d['quote_sym'])} {d['deviation'] * 100:+.0f}%"
                        for d in off[:3])
        off_line = (f"\n⚠️ {len(off)} pool disembunyikan — harganya menyimpang jauh dari pool "
                    f"terdalam ({det}). Pool begitu tak terarbitrase; LP di situ = modalmu "
                    f"yang dipakai menyeret harganya balik ke pasar.")
    # Sumber daftar WAJIB disebut: jalur Krystal menampilkan daftar mereka apa adanya,
    # sedangkan jalur "scan sendiri" melewati seluruh saringan (pool mati, harga
    # menyimpang) sehingga daftarnya bisa jauh lebih pendek. Tanpa keterangan ini,
    # Krystal yang gagal sesaat terlihat seperti bot kehilangan pool (kejadian nyata:
    # BNBCAT 20 pool jadi 4).
    # `source` sekarang bisa gabungan ("krystal+uniswap+gecko"), jadi JANGAN
    # dicocokkan persis — dulu nilai gabungan jatuh ke cabang terakhir dan kartunya
    # menulis "scan sendiri … Krystal tidak punya token ini" padahal Krystal yang
    # menyumbang mayoritas daftar.
    _SRC_NAMA = {"krystal": "Krystal", "uniswap": "indexer Uniswap",
                 "gecko": "GeckoTerminal", "scan": "scan sendiri"}
    parts = [p for p in str(res.get("source") or "").split("+") if p]
    if parts:
        nm = " + ".join(_SRC_NAMA.get(p, p) for p in parts)
        src_line = f"\U0001F4DA sumber: {esc(nm)}"
        if "krystal" not in parts:
            src_line += (f" (Krystal gagal: {esc(ch.krystal_last_error())})"
                         if ch.krystal_last_error() else " (Krystal tidak punya token ini)")
    else:
        src_line = ("\U0001F526 sumber: scan sendiri \u2014 daftarnya lewat saringan pool mati "
                    "& harga menyimpang"
                    + (f" (Krystal gagal: {esc(ch.krystal_last_error())})"
                       if ch.krystal_last_error() else " (Krystal tidak punya token ini)"))
    text = (f"Found {len(pools)} pool(s) untuk <b>{esc(tsym)}</b> ({_t.time() - t0:.1f}s):\n"
            f"<pre>{esc(chr(10).join(rows))}</pre>\n"
            f"<i>P=PancakeSwap · U=Uniswap · ! = harga menyimpang · TVL/volume USD · "
            f"APR estimasi · V/TVL = volume 24j ÷ TVL (makin tinggi makin produktif) "
            f"· – = belum terindeks</i>\n<i>{src_line}</i>{off_line}")
    if extra:
        try:
            text += "\n" + "\n".join(lp_suggestion(cid, res, extra))
        except Exception as err:
            log.warning("saran posisi gagal dirakit: %s", err)
    text += "\n\nPilih pool:"
    await edit(status, text, InlineKeyboardMarkup(buttons))


# ---------- Mint flow ----------
STRAT_LABEL = {"stable": "Stable", "wide": "Wide", "lower": "Lower", "upper": "Upper",
               "same": "Sama (MC range dipertahankan)"}
STRAT_PRESETS = {  # baris tombol lebar range per mode: (low_pct, up_pct)
    "stable": [(2, 2), (5, 5), (6.18, 6.18), (10, 10)],
    "wide": [(25, 50), (50, 100), (60, 150), (75, 300)],
    "lower": [(10, 100), (25, 100), (50, 100), (75, 100)],
    "upper": [(50, 25), (50, 50), (50, 100), (50, 200)],
}


def _meme_addr(p: dict) -> str:
    return p["token0"] if p["quote_is_token1"] else p["token1"]


def compute_amount(ctx_data: dict, sqrtp: int | None = None,
                   ticks: tuple[int, int] | None = None) -> float:
    """Budget deposit. lower/wide/stable = satuan quote; upper = satuan meme.

    `amount_src` memilih saldo mana yang dipersenkan:

    - `"quote"` (default) — % dari modal sisi quote (quote + WETH/native + quote
      lain yang bisa ditukar). Perilaku lama.
    - `"meme"` — % dari saldo token meme. Sisi quote-nya MENYESUAIKAN mengikuti
      rasio range: budget quote = nilai meme ÷ (1 − porsi_quote), sehingga meme
      sebanyak itulah yang benar-benar masuk posisi. `sqrtp`+`ticks` perlu untuk
      menghitung rasionya; tanpa itu dipakai perkiraan nilai meme apa adanya.
    """
    cid = ctx_data["chain"]
    cfg = ch.CHAINS[cid]
    p = ctx_data["pool_info"]
    if ctx_data["amount_fixed"]:
        return float(ctx_data["amount_fixed"])
    w3 = ch.get_w3(cid)
    addr = wallet_address()
    mode = ctx_data["mode"]
    if ctx_data.get("amount_src") == "meme" and mode != "upper":
        meme = _meme_addr(p)
        mdec = ch.token_info(w3, meme)["decimals"]
        use = ch.erc20(w3, meme).functions.balanceOf(addr).call() * ctx_data["amount_pct"] / 100
        if use <= 0:
            return 0.0
        s = sqrtp
        if s is None:
            s = (ch.v4_slot0(w3, cid, p["pool_id"])[0] if p.get("ver") == 4 else
                 w3.eth.contract(address=ch.Web3.to_checksum_address(p["pool"]),
                                 abi=ch.POOL_ABI).functions.slot0().call()[0])
        raw = (s / ch.Q96) ** 2
        mprice_q = raw if p["quote_is_token1"] else (1 / raw if raw else 0)  # quote-wei per meme-wei
        val_q = use * mprice_q                       # nilai meme dalam quote-wei
        if ticks and mode in ("wide", "stable"):
            keep, _ = ch.plan_two_sided(s, ticks[0], ticks[1], 10 ** 18, p["quote_is_token1"])
            keep_frac = keep / 10 ** 18
            # Budget yang diminta mesin mint adalah sisi QUOTE saja — meme di wallet
            # DITAMBAHKAN di atasnya (`quote_dep = (budget + meme_val) * keep_frac`).
            # Jadi yang dikembalikan bukan totalnya, melainkan porsi quote-nya:
            #   quote = nilai_meme × keep_frac / (1 − keep_frac)
            # Mengembalikan total (nilai_meme / (1 − keep_frac)) membuat posisi jadi
            # 1/keep_frac kali lebih besar, dan bot lalu MEMBELI meme tambahan lewat
            # swap padahal user memilih memakai saldo meme yang ada.
            if keep_frac >= 0.999:
                pass                                 # praktis 100% quote
            elif keep_frac <= 0:
                return 0.0                           # 100% meme → tidak butuh quote
            else:
                val_q = val_q * keep_frac / (1 - keep_frac)
        # mode "lower" = 100% quote: seluruh meme dijual, jadi nilainya apa adanya.
        return val_q / 10 ** p["quote_decimals"]
    if mode == "upper":
        meme = _meme_addr(p)
        mdec = ch.token_info(w3, meme)["decimals"]
        bal = ch.erc20(w3, meme).functions.balanceOf(addr).call()
        return (bal * ctx_data["amount_pct"] / 100) / 10 ** mdec
    gas_reserve = ch.gas_reserve_wei(cid, w3)
    ne = ch.native_erc20(cid)
    if ne and p["quote_addr"].lower() == ne:
        # Quote-nya ADALAH native dengan wajah ERC20 (Arc: USDC). Saldo native TIDAK
        # boleh ditambahkan lagi — `balanceOf` sudah membaca kantong yang sama, dan
        # menjumlahkannya membuat "50% saldo" jadi ~99% lalu mint gagal di tengah.
        # Cadangan gas tetap dipotong: gas dibayar dari kantong ini juga.
        bal = ch.erc20(w3, p["quote_addr"]).functions.balanceOf(addr).call()
        keep = gas_reserve // 10 ** (18 - p["quote_decimals"])
        bal = max(0, bal - keep)
        bal += ch.other_quote_capital(w3, cid, addr, p["quote_addr"])
        return (bal * ctx_data["amount_pct"] / 100) / 10 ** p["quote_decimals"]
    if p["quote_addr"].lower() == ch.V4_NATIVE:
        # Pool v4 ber-quote ETH native. Modal = native + WETH (1:1, tinggal unwrap)
        # + quote lain seperti USDG (dijual otomatis saat mint lewat
        # ensure_native_balance). Semuanya benar-benar bisa diambil — kalau cuma
        # dihitung tanpa jalur eksekusi, mint-nya gagal di tengah.
        bal = max(0, w3.eth.get_balance(addr) - gas_reserve)
        try:
            bal += ch.erc20(w3, cfg["wrapped"]).functions.balanceOf(addr).call()
        except Exception:
            pass
        bal += ch.other_quote_capital(w3, cid, addr, p["quote_addr"])
        return (bal * ctx_data["amount_pct"] / 100) / 10 ** p["quote_decimals"]
    q = ch.erc20(w3, p["quote_addr"])
    bal = q.functions.balanceOf(addr).call()
    bal += ch.other_quote_capital(w3, cid, addr, p["quote_addr"])
    if p["quote_addr"].lower() == cfg["wrapped"].lower():
        bal += max(0, w3.eth.get_balance(addr) - gas_reserve)
    else:
        # quote bukan wrapped (mis. USDG): saldo WETH+native ikut jadi modal,
        # nanti di-swap otomatis ke quote saat mint
        try:
            wbal = ch.erc20(w3, cfg["wrapped"]).functions.balanceOf(addr).call()
            # Chain ber-native_erc20 (Arc): saldo native SUDAH ikut terhitung di
            # other_quote_capital lewat wajah ERC20-nya, jadi di sini cuma wrapped.
            wtotal = wbal if ne else wbal + max(0, w3.eth.get_balance(addr) - gas_reserve)
            if wtotal > 0:
                rate = ch.wrapped_per_quote_wei(w3, cid, p["quote_addr"])  # wei wrapped per wei quote
                if rate > 0:
                    bal += int(wtotal / rate * 0.98)  # margin biaya swap
        except Exception:
            pass  # tidak ada pool wrapped/quote — pakai saldo quote apa adanya
    return (bal * ctx_data["amount_pct"] / 100) / 10 ** p["quote_decimals"]


def recommend_strategy(ctx_data: dict) -> tuple[str, float | None]:
    """(mode rekomendasi, vol harian %). Aturan: pair stabil → stable;
    vol <8% → stable; 8–40% → wide; >40%/tidak diketahui → lower."""
    cid = ctx_data["chain"]
    cfg = ch.CHAINS[cid]
    p = ctx_data["pool_info"]
    tsym = ctx_data["token"]["symbol"]
    w3 = ch.get_w3(cid)
    if tsym.upper() in cfg["stable_syms"] and p["quote_sym"] in cfg["stable_syms"]:
        return "stable", None
    if p.get("ver", 3) != 3:
        return "lower", None  # oracle TWAP cuma ada di pool v3
    vol = ch.pool_volatility_daily(w3, p["pool"])
    if vol is None:
        return "lower", None
    if vol < 8:
        return "stable", vol
    if vol < 40:
        return "wide", vol
    return "lower", vol


def _meme_price(p: dict, tdec: int, tick: int) -> float:
    """Harga meme dalam quote pada tick tertentu."""
    raw = ch.tick_to_price(tick)
    if p["quote_is_token1"]:
        return raw * 10 ** (tdec - p["quote_decimals"])
    return (1 / raw if raw else 0) * 10 ** (tdec - p["quote_decimals"])


def newpool_note(ctx_data: dict) -> str:
    """Baris di kartu konfirmasi kalau pool-nya BELUM ada dan akan dibuat oleh
    tombol Confirm — angka range & deposit di atasnya dihitung dari harga rujukan,
    bukan dari pool yang sudah berjalan."""
    if not ctx_data.get("init_sqrtp"):
        return ""
    return ("\n\n🆕 <b>Pool ini belum ada.</b> Tombol Confirm membuatnya lalu langsung "
            "mint di transaksi berikutnya — keduanya berurutan tanpa jeda, jadi harga "
            "awal tidak sempat basi. Range & jumlah di atas dihitung dari harga rujukan.")


def pool_warnings(cid: int, p: dict) -> str:
    """Peringatan kartu konfirmasi untuk pool yang bukan profil normal."""
    cfg = ch.CHAINS[cid]
    lines = []
    if p.get("foreign_quote"):
        lines.append(
            f"⚠️ Quote pool ini <b>{esc(p['quote_sym'])}</b>, bukan "
            f"{esc(cfg['wrapped_symbol'])}/stable. Nilai posisi &amp; PnL USD ikut "
            f"naik-turun harga {esc(p['quote_sym'])}, dan modal masuk/keluar lewat "
            f"swap 2 langkah (fee &amp; slippage dobel).")
    if p.get("thin"):
        lines.append("⚠️ TVL pool sangat kecil — slippage besar dan harga gampang digeser.")
    # Pool ber-fee besar punya tick spacing lebar; tepi range WAJIB kelipatan spacing,
    # jadi range tidak bisa dipasang rapat ke harga. Sebutkan supaya tidak dikira bug.
    bp = box_pct(p)
    if p.get("ver") in (3, 4) and bp >= 1:
        lines.append(
            f"📐 Kisi pool ini <b>{bp:g}%</b> — tepi range wajib kelipatan tick spacing, "
            f"jadi tidak bisa lebih rapat dari itu. Tombol 🎯 Rapat memakai satu kotak "
            f"kisi yang mencakup harga: langsung aktif, dua sisi.")
    if p.get("deviation"):
        lines.append(
            f"⚠️ Harga pool ini <b>{p['deviation'] * 100:+.0f}%</b> dari pool terdalam. "
            f"Pool begitu tidak terarbitrase — LP di situ = modalmu yang dipakai "
            f"menyeret harganya balik ke pasar.")
    return ("\n\n" + "\n".join(lines)) if lines else ""


def ext_links_html(cid: int, token_ca: str, pool) -> str:
    """Baris link luar (GMGN · DexScreener) untuk kartu konfirmasi.

    Slug-nya OPSIONAL per chain dan harus dibaca dengan `.get()`: Arc tidak punya
    `gmgn` sama sekali karena GMGN memang belum melayaninya. Dulu ketiga tempat
    menulis `cfg['gmgn']` langsung, dan akibatnya seluruh kartu konfirmasi mint di
    Arc mati dengan KeyError yang sampai ke user cuma sebagai `❌ 'gmgn'` —
    pesan yang tidak menunjuk apa pun.

    Link yang slug-nya tidak ada dilewati; kalau semuanya tidak ada, baris ini
    hilang (dan pemanggil tidak perlu tahu)."""
    cfg = ch.CHAINS[cid]
    parts = []
    if cfg.get("gmgn"):
        parts.append(f'📈 <a href="https://gmgn.ai/{cfg["gmgn"]}/token/{token_ca}">GMGN</a>')
    if cfg.get("dexscreener") and pool:
        parts.append(f'<a href="https://dexscreener.com/{cfg["dexscreener"]}/{pool}">DexScreener</a>')
    return (" · ".join(parts) + "\n") if parts else ""


def no_funds_msg(ctx_data: dict, dep_sym: str) -> str:
    """Pesan "saldo kosong" yang MENYEBUTKAN apa yang dibaca dan di mana.

    "Saldo USDC kosong." saja tidak bisa dipakai mendiagnosis apa pun: user tidak
    tahu bot sedang di chain mana, memakai wallet yang mana, dan berapa yang
    sebenarnya terbaca. Kejadian nyata — wallet berisi 59,997116 USDC di Arc
    (diverifikasi dari arsip di blok jam kejadian) tapi bot melapor kosong, dan
    dugaannya bisa jatuh ke belasan tempat karena pesannya bisu. Tiga angka di
    bawah langsung menunjuk sebabnya: chain salah, wallet salah, atau memang habis.

    Teks POLOS tanpa tag HTML: jalur `build_preview` melemparnya sebagai
    RuntimeError dan pemanggilnya menulis `f"❌ {esc(e)}"`, jadi tag apa pun akan
    tampil mentah. Prefiks ❌ juga tidak ditaruh di sini, pemanggil yang menambah.

    Pembacaan tambahan ini HANYA jalan di jalur gagal, jadi tidak menambah ongkos
    RPC di jalur normal."""
    cid = ctx_data["chain"]
    cfg = ch.CHAINS[cid]
    p = ctx_data["pool_info"]
    addr = wallet_address()
    out = [f"Saldo {dep_sym} kosong di {cfg['name']}.",
           f"Wallet {wallet_label()} {addr}"]
    try:
        w3 = ch.get_w3(cid)
        qaddr = p.get("quote_addr")
        if qaddr and str(qaddr).lower() != ch.V4_NATIVE:
            qbal = ch.erc20(w3, qaddr).functions.balanceOf(addr).call()
            out.append(f"{p.get('quote_sym', '?')}: "
                       f"{qbal / 10 ** int(p.get('quote_decimals') or 18):.6f}")
        nat = w3.eth.get_balance(addr)
        out.append(f"{cfg['native_symbol']} (native): {nat / 1e18:.6f} "
                   f"— cadangan gas {ch.gas_reserve_wei(cid, w3) / 1e18:.6f}")
    except Exception as e:
        out.append(f"(saldo gagal dibaca: {e})")
    out.append("Kalau angkanya tidak nol, chain atau wallet aktifnya yang salah "
               "— cek /wallet dan /chain.")
    return "\n".join(out)


def build_preview_v2(ctx_data: dict) -> str:
    """Kartu konfirmasi add liquidity V2 (full-range 50/50, tanpa strategi range)."""
    cid = ctx_data["chain"]
    cfg = ch.CHAINS[cid]
    p = ctx_data["pool_info"]
    tsym = ctx_data["token"]["symbol"]
    tdec = ctx_data["token"]["decimals"]
    w3 = ch.get_w3(cid)

    amount = compute_amount(ctx_data)
    if amount <= 0:
        raise RuntimeError(no_funds_msg(ctx_data, p["quote_sym"]))
    rq, rm = ch._v2_pair_reserves(w3, p["pool"], p["quote_addr"])
    price_q = (rq / rm) * 10 ** (tdec - p["quote_decimals"]) if rm else 0
    usd = amount * p["quote_usd"]
    try:
        supply = ch.token_supply(w3, _meme_addr(p))
    except Exception:
        supply = 0
    meme_bal = ch.erc20(w3, _meme_addr(p)).functions.balanceOf(wallet_address()).call()
    meme_val_q = meme_bal * rq // rm if rm else 0
    qwei = int(amount * 10 ** p["quote_decimals"])
    quote_keep = min((qwei + meme_val_q) // 2, qwei)
    swap_in = qwei - quote_keep
    amount_desc = "fix" if ctx_data["amount_fixed"] else f"{ctx_data['amount_pct']:g}%"
    vol_txt = f"vol 24j {ch.fmt_usd(p['vol24_usd'])}" if p.get("vol24_usd") is not None else "vol 24j: ?"
    if p.get("apr_pct"):
        vol_txt += f" · APR pool ~{p['apr_pct']:,.0f}%"
    return (
        f"<b>Confirm add liquidity · {esc(cfg['name'])} · v2</b>\n"
        f"CA: <code>{esc(ctx_data['token']['address'])}</code>\n"
        f"{esc(p.get('dex') or ch.dex_name(cid))} · {esc(tsym)}/{esc(p['quote_sym'])} "
        f"{p['fee'] / 10000:.2f}% · TVL {ch.fmt_usd(p['tvl_usd'])} · {vol_txt}\n"
        + ext_links_html(cid, ctx_data["token"]["address"], p["pool"]) + "\n"
        f"Value deposited: {ch.fmt_amount(amount)} {esc(p['quote_sym'])} ({ch.fmt_usd(usd)} · {esc(amount_desc)})\n"
        f"Current price: {ch.fmt_price(price_q)} {esc(p['quote_sym'])}/{esc(tsym)}"
        + (f" · MC {ch.fmt_usd(price_q * p['quote_usd'] * supply)}" if supply else "") + "\n\n"
        f"📦 <b>Komposisi 50/50 (full range):</b>\n"
        f"· {ch.fmt_amount(quote_keep / 10 ** p['quote_decimals'])} {esc(p['quote_sym'])} masuk pair\n"
        + (f"· swap {ch.fmt_amount(swap_in / 10 ** p['quote_decimals'])} {esc(p['quote_sym'])} → {esc(tsym)}\n"
           if swap_in > qwei // 500 else f"· tanpa swap — {esc(tsym)} existing dipakai\n")
        + f"\n<i>LP v2 = full range, selalu aktif. Fee {p['fee'] / 10000:g}% auto-compound ke posisi "
        f"(tidak ada klaim fee terpisah). Token fee-on-transfer tidak didukung.</i>\n\n"
        f"Custom: <code>a 0.005</code> / <code>a 30%</code> (amount)\n"
        f"Slippage {store.load_settings()['slippage_pct']:g}% · deadline 20 menit"
    )


def build_preview(ctx_data: dict) -> str:
    """Kartu konfirmasi mint (dipanggil di thread)."""
    cid = ctx_data["chain"]
    cfg = ch.CHAINS[cid]
    p = ctx_data["pool_info"]
    if p.get("ver") == 2:
        return build_preview_v2(ctx_data)
    tsym = ctx_data["token"]["symbol"]
    tdec = ctx_data["token"]["decimals"]
    mode = ctx_data["mode"]
    w3 = ch.get_w3(cid)

    if ctx_data["rec"] is None:
        ctx_data["rec"], ctx_data["vol"] = recommend_strategy(ctx_data)

    # Range dihitung DULU: `amount_src="meme"` butuh rasio sisi range untuk
    # menskalakan nilai meme jadi budget quote. Tick tidak bergantung pada amount,
    # jadi urutan ini aman.
    if p.get("ver") == 4:
        sqrtp, cur_tick = ctx_slot0(ctx_data)
    else:
        pool = w3.eth.contract(address=ch.Web3.to_checksum_address(p["pool"]), abi=ch.POOL_ABI)
        slot0 = pool.functions.slot0().call()
        sqrtp, cur_tick = slot0[0], slot0[1]
    lo_t, hi_t = ch.calc_strategy_range(cur_tick, p["fee"], p["quote_is_token1"],
                                        mode, ctx_data["low_pct"], ctx_data["up_pct"],
                                        ctx_data.get("gap", 1), spacing=p.get("tick_spacing"))

    amount = compute_amount(ctx_data, sqrtp, (lo_t, hi_t))
    src_meme = ctx_data.get("amount_src") == "meme"
    dep_sym = tsym if mode == "upper" else p["quote_sym"]
    if amount <= 0:
        raise RuntimeError(
            no_funds_msg(ctx_data, tsym if (src_meme or mode == "upper") else dep_sym)
            + ("\n<i>Mode Upper butuh pegang token meme.</i>" if mode == "upper" else ""))
    lo, hi = sorted([_meme_price(p, tdec, lo_t), _meme_price(p, tdec, hi_t)])
    now = _meme_price(p, tdec, cur_tick)
    try:
        supply = ch.token_supply(w3, _meme_addr(p))
    except Exception:
        supply = 0

    # deskripsi range + rencana aksi per mode
    if mode == "lower":
        side_line = "range BELOW market · aktif kalau harga turun masuk range"
    elif mode == "upper":
        side_line = "range ABOVE market · aktif kalau harga naik masuk range"
    else:
        side_line = "range dua sisi · langsung aktif (🟢 IN range)"

    extra = ""
    if mode in ("wide", "stable"):
        qwei = int(amount * 10 ** p["quote_decimals"])
        keep, swap = ch.plan_two_sided(sqrtp, lo_t, hi_t, qwei, p["quote_is_token1"])
        # meme yang sudah dipegang dihitung duluan; swap cuma nutup kekurangan
        meme_bal = ch.erc20(w3, _meme_addr(p)).functions.balanceOf(wallet_address()).call()
        raw = (sqrtp / ch.Q96) ** 2
        meme_price_q = raw if p["quote_is_token1"] else (1 / raw if raw else 0)  # quote-wei per meme-wei
        meme_val_q = int(meme_bal * meme_price_q)
        keep_frac = keep / qwei if qwei else 0
        quote_dep = min(int((qwei + meme_val_q) * keep_frac), qwei)
        swap = max(0, qwei - quote_dep)
        if swap <= qwei // 500:
            swap = 0
        # sisi meme yang benar2 masuk posisi (jaga rasio range)
        meme_need_q = int(quote_dep * (1 - keep_frac) / keep_frac) if keep_frac > 0 else meme_val_q
        from_wallet_q = min(meme_val_q, max(0, meme_need_q - swap))
        excess_q = max(0, meme_val_q - from_wallet_q)
        qd, qs = p["quote_decimals"], p["quote_sym"]

        def in_meme(qv):
            return qv / meme_price_q / 10 ** tdec if meme_price_q else 0

        L = [f"\n📦 <b>Komposisi deposit (dua sisi):</b>",
             f"· Sisi bawah: {ch.fmt_amount(quote_dep / 10 ** qd)} {esc(qs)} masuk posisi",
             f"· Sisi atas : ~{ch.fmt_amount(in_meme(meme_need_q))} {esc(tsym)} "
             f"(≈{ch.fmt_amount(meme_need_q / 10 ** qd)} {esc(qs)})"]
        if from_wallet_q > 0:
            L.append(f"   └ dari wallet: ~{ch.fmt_amount(in_meme(from_wallet_q))} {esc(tsym)} ✓")
        if swap > 0:
            L.append(f"   └ swap baru : {ch.fmt_amount(swap / 10 ** qd)} {esc(qs)} → {esc(tsym)}")
            # Price impact swap komposisi WAJIB ditampilkan: minOut tidak
            # melindunginya (quoter sudah memasukkannya), jadi tanpa angka ini user
            # tidak punya cara tahu berapa yang menguap. Terukur di BODKIN: $36
            # keluar wallet, $19,87 mendarat di posisi.
            imp = (ch.swap_impact_v4(cid, p["key"], p["quote_addr"], swap)
                   if p.get("ver") == 4 and p.get("key") else None)
            ctx_data["_impact"] = imp
            if imp is not None:
                lim = ctx_data.get("max_impact") or impact_limit()
                tag = "  ⚠️ TINGGI" if imp > lim else ""
                L.append(f"   └ price impact swap: {imp * 100:.1f}%{tag}")
                if imp > lim:
                    L.append(f"   └ <b>swap ini membakar ~{ch.fmt_usd(swap / 10 ** qd * p['quote_usd'] * imp)}</b> "
                             f"— pool terlalu tipis untuk jumlah segini. Kecilkan jumlah, "
                             f"pilih pool lebih dalam, atau pakai mode Lower (tanpa swap).")
        else:
            L.append(f"   └ tanpa swap — {esc(tsym)} existing sudah cukup")
        if excess_q > qwei // 100:
            L.append(f"· Sisa ~{ch.fmt_amount(in_meme(excess_q))} {esc(tsym)} "
                     f"tidak terpakai, tetap di wallet")
        extra = "\n".join(L)
    if mode != "upper" and p["quote_addr"].lower() == ch.V4_NATIVE:
        extra += "\nDeposit pakai ETH native langsung (tanpa wrap)."
    elif mode != "upper":
        bal = ch.erc20(w3, p["quote_addr"]).functions.balanceOf(wallet_address()).call()
        deficit = max(0, int(amount * 10 ** p["quote_decimals"]) - bal)
        if deficit and p["quote_addr"].lower() == cfg["wrapped"].lower():
            extra += (f"\nAuto-wrap: {ch.fmt_amount(deficit / 10 ** p['quote_decimals'])} "
                      f"native → {esc(p['quote_sym'])}")
        elif deficit:
            try:
                rate = ch.wrapped_per_quote_wei(w3, cid, p["quote_addr"])
                weth_in = deficit * rate / 1e18
                extra += (f"\nAuto-swap: ~{ch.fmt_amount(weth_in)} {esc(cfg['wrapped_symbol'])} → "
                          f"{ch.fmt_amount(deficit / 10 ** p['quote_decimals'])} {esc(p['quote_sym'])} "
                          f"(wrap otomatis kalau perlu)")
            except Exception:
                extra += (f"\n⚠️ Saldo {esc(p['quote_sym'])} kurang dan pool "
                          f"{esc(cfg['wrapped_symbol'])}/{esc(p['quote_sym'])} tidak ditemukan — mint bakal gagal.")

    usd = amount * (ch._meme_usd(w3, cid, p) if mode == "upper" else p["quote_usd"])
    # Persennya bisa merujuk saldo quote ATAU saldo meme — wajib disebut, kalau
    # tidak "25%" jadi ambigu dan user salah memperkirakan berapa yang dipakai.
    amount_desc = ("fix" if ctx_data["amount_fixed"] else
                   f"{ctx_data['amount_pct']:g}% "
                   f"{tsym if (src_meme or mode == 'upper') else p['quote_sym']}")
    if mode == "stable":
        strat_desc = f"±{ctx_data['low_pct']:g}%"
    elif mode == "wide":
        strat_desc = f"−{ctx_data['low_pct']:g}% / +{ctx_data['up_pct']:g}%"
    elif mode == "lower":
        strat_desc = f"−{ctx_data['low_pct']:g}%"
    else:
        strat_desc = f"+{ctx_data['up_pct']:g}%"

    vol = ctx_data["vol"]
    if p.get("vol24_usd") is not None:
        vol_txt = f"vol 24j {ch.fmt_usd(p['vol24_usd'])}"
        if p.get("apr_pct"):
            vol_txt += f" · APR pool ~{p['apr_pct']:,.0f}%"
    else:
        vol_txt = f"vol 24j ≈ {vol:.0f}%" if vol is not None else "vol 24j: ?"
    rec = ctx_data["rec"]

    return (
        f"<b>Confirm mint · {esc(cfg['name'])} · v{p.get('ver', 3)}</b>\n"
        f"CA: <code>{esc(ctx_data['token']['address'])}</code>\n"
        f"{esc(tsym)}/{esc(p['quote_sym'])} {p['fee'] / 10000:.2f}% · TVL {ch.fmt_usd(p['tvl_usd'])} · {vol_txt}\n"
        + ext_links_html(cid, ctx_data["token"]["address"], p["pool"]) + "\n"
        f"<b>Strategi: {STRAT_LABEL[mode]} {strat_desc}</b>"
        f"{' ⭐' if mode == rec else f' (rekomendasi: ⭐ {STRAT_LABEL[rec]})'}\n"
        f"Value deposited: {ch.fmt_amount(amount)} {esc(dep_sym)} ({ch.fmt_usd(usd)} · {esc(amount_desc)})\n"
        + (f"Range: MC {ch.fmt_usd(lo * p['quote_usd'] * supply)}–{ch.fmt_usd(hi * p['quote_usd'] * supply)} "
           f"(now {ch.fmt_usd(now * p['quote_usd'] * supply)})\n" if supply else
           f"Range: {ch.fmt_price(lo)}–{ch.fmt_price(hi)} (now {ch.fmt_price(now)})\n")
        + f"Current price: {ch.fmt_price(now)} {esc(p['quote_sym'])}/{esc(tsym)}"
        + (f" · MC {ch.fmt_usd(now * p['quote_usd'] * supply)}" if supply else "") + "\n"
        f"{side_line}{extra}\n\n"
        f"<i>Price strategies:\n"
        f"· Stable ±6% — pair stabil / volatilitas rendah\n"
        f"· Wide −50%/+100% — pair volatil, dua sisi, langsung makan fee\n"
        f"· Lower −50% — setor {esc(p['quote_sym'])} saja, nampung kalau harga turun\n"
        f"· Upper +100% — setor {esc(tsym)} saja, jual bertahap kalau naik</i>\n\n"
        f"Custom: ketik <code>r 40 120</code> (range %) · <code>a 0.005</code> / <code>a 30%</code> (amount)\n"
        f"Slippage {store.load_settings()['slippage_pct']:g}% · deadline 20 menit"
    )


# Lebar minta-sekecil-mungkin. calc_strategy_range membulatkan tepi KE LUAR ke
# kelipatan tick spacing, jadi meminta lebar ~1 tick selalu menghasilkan tepat SATU
# kotak kisi — range terapat yang legal di pool mana pun. Meminta selebar satu kotak
# justru meluber jadi dua, karena harga sekarang ada di tengah kotak.
TIGHT_PCT = 0.01


# ---------- Buat pool v4 baru (fee & tick spacing custom) ----------
# v4 tidak punya whitelist fee tier seperti v3: PoolKey membawa fee dan tickSpacing
# apa adanya. Batasnya DIUKUR ke PoolManager (chain.V4_FEE_MAX / V4_SPACING_*).
NEWPOOL: dict[str, dict] = {}

NP_FEES = [500, 3000, 10000, 20000, 30000, 50000]        # 0,05% … 5%

# Preset kisi IKUT fee, bukan daftar tetap. Diukur dari 188 pool v4 vanilla
# (Robinhood + Arc, indexer Uniswap): pembagi yang dipakai pembuat pool
# didominasi fee/100 (132 pool) lalu fee/50 (48 pool), dan bobot TVL-nya justru
# terbalik — fee/50 memegang 57,3% TVL, fee/100 41,2%. Yang lebih rapat dari
# fee/200 ada tapi semuanya debu (fee 2% kisi 10 = TVL $458; fee 3,5% kisi 10 =
# $3.290; fee 4,5% kisi 60 = $0), sedangkan fee/200 masih hidup (fee 4% kisi 200
# = $109k + $86k di Arc). Jadi rentang yang terbukti dipakai: fee/200 … fee/50.
NP_SPACING_DIVS = [(200, "rapat"), (100, "standar"), (50, "longgar")]

# Deviasi harga rujukan terhadap median pool bervolume yang masih boleh dibuat.
# Di atas ini pembuatan ditolak: pool DOT/USDC 5% lahir 27% di atas pasar dan
# langsung diseret turun, dan rujukannya meleset 125% dari venue sebenarnya.
NP_DEV_BLOCK = 0.25


def np_spacing_presets(fee: int) -> list[tuple[int, str]]:
    """[(spacing, label)] untuk fee ini — plus beberapa nilai mutlak yang lazim."""
    out, seen = [], set()
    for d, lbl in NP_SPACING_DIVS:
        v = max(ch.V4_SPACING_MIN, min(ch.V4_SPACING_MAX, int(fee) // d or 1))
        if v not in seen:
            seen.add(v)
            out.append((v, lbl))
    for v in (1, 10, 60, 200):
        if v not in seen and ch.V4_SPACING_MIN <= v <= ch.V4_SPACING_MAX:
            seen.add(v)
            out.append((v, ""))
    return out


def np_spacing(ctx: dict) -> int:
    """Tick spacing efektif. Default mengikuti pola fee/100 yang dominan terukur di
    pool v4 chain ini (fee 50000 → 500, 86000 → 860); dibatasi ke rentang yang sah."""
    sp = ctx.get("spacing")
    if sp:
        return int(sp)
    return max(ch.V4_SPACING_MIN, min(ch.V4_SPACING_MAX, int(ctx["fee"]) // 100 or 1))


def np_quote(ctx: dict) -> str:
    """Quote yang dipakai. Default = quote pool TERDALAM token ini, karena dari
    pasangan itulah harga awal bisa disalin."""
    if ctx.get("quote_addr"):
        return ctx["quote_addr"]
    best = max(ctx["pools"], key=lambda p: float(p.get("tvl_usd") or 0), default=None)
    return best["quote_addr"] if best else ""


def np_quotes(ctx: dict) -> list[tuple[str, str]]:
    """[(simbol, alamat)] quote yang PUNYA pool rujukan — hanya itu yang harga
    awalnya bisa ditentukan tanpa menebak."""
    out, seen = [], set()
    for p in sorted(ctx["pools"], key=lambda x: -float(x.get("tvl_usd") or 0)):
        a = str(p.get("quote_addr") or "").lower()
        if not a or a in seen:
            continue
        seen.add(a)
        out.append((p.get("quote_sym") or "?", p["quote_addr"]))
    return out


def ctx_slot0(ctx_data: dict) -> tuple[int, int]:
    """(sqrtPriceX96, tick) pool di ctx — memakai harga rujukan kalau pool-nya
    BELUM di-initialize.

    Alur "buat pool + mint" menyiapkan kartu konfirmasi SEBELUM pool-nya ada, supaya
    user melihat range dan jumlah deposit yang sebenarnya lalu menekan SATU tombol:
    pembuatan dan mint jalan berurutan di dalam satu `TX_LOCK`, tanpa jeda manusia
    di antaranya. Tanpa ini `v4_slot0` mengembalikan 0 dan seluruh matematika range
    runtuh."""
    cid = ctx_data["chain"]
    p = ctx_data["pool_info"]
    w3 = ch.get_w3(cid)
    if p.get("ver") == 4:
        sq, tick = ch.v4_slot0(w3, cid, p["pool_id"])
        if sq <= 0 and ctx_data.get("init_sqrtp"):
            sq = int(ctx_data["init_sqrtp"])
            tick = int(round(math.log((sq / ch.Q96) ** 2) / math.log(1.0001)))
        return sq, tick
    s0 = w3.eth.contract(address=ch.Web3.to_checksum_address(p["pool"]),
                         abi=ch.POOL_ABI).functions.slot0().call()
    return s0[0], s0[1]


def np_median(px: list[float]) -> float:
    """Median harga — satu implementasi saja, di `ch.geo_median`. Jumlah GENAP
    memakai rata-rata GEOMETRIK dua nilai tengah, bukan elemen ke-n//2 (indeks
    polos selalu mengambil yang lebih tinggi; pada 6 pool DOT ia memilih
    0,000144989 padahal dua tengahnya 0,000125074 dan 0,000144989)."""
    return ch.geo_median(px)


def np_price(p: dict, tdec: int, sq: int) -> float:
    """Harga meme dalam quote dari sqrtPriceX96, lewat `_meme_price` yang SAMA
    dengan kartu mint. Versi pertama menuliskan rumusnya ulang dan tandanya
    terbalik saat quote jadi currency0 — pool DOT/USDC yang harganya 0,00027480
    tampil sebagai "0.0₂₀0" (meleset 1e24)."""
    raw = (sq / ch.Q96) ** 2
    if raw <= 0:
        return 0.0
    return _meme_price(p, tdec, int(round(math.log(raw) / math.log(1.0001))))


# Di luar rasio ini terhadap anchor, kandidat dibuang SEBELUM median dihitung.
NP_ANCHOR_DROP = 3.0
# Kalau median pool sepasang sendiri sejauh ini dari anchor, tidak ada pool
# sepasang yang layak jadi rujukan — pembuatan DITOLAK.
NP_ANCHOR_BLOCK = 3.0


def gmgn_price_usd(cid: int, token: str, _cache={}) -> float:
    """Harga USD token dari GMGN, 0 kalau tidak tersedia. Dipanggil di thread.

    Dipakai sebagai salah satu sumber `ch.token_anchor_price`. Sengaja berdiri di
    `bot.py`, bukan di `chain.py`: kunci GMGN tidak boleh pernah lewat
    `ch._cf_request` — jalur itu meneruskan header ke operator proxy pihak ketiga.
    Klien scanner dipakai ulang supaya throttle per-IP-nya tetap satu penghitung."""
    if not ch.CHAINS[cid].get("gmgn"):
        return 0.0
    key = (cid, str(token).lower())
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < 120:
        return hit[0]
    px = 0.0
    try:
        cl = _scan_client(float((scanner_cfg().get("pace") or 1.0)))
        if cl is not None:
            d = cl.token_info(ch.CHAINS[cid]["gmgn"], str(token).lower()) or {}
            px = float(((d.get("price") or {}).get("price")) or 0)
    except Exception:
        px = 0.0
    _cache[key] = (px, time.time())
    return px


def np_anchor(ctx: dict, p: dict) -> dict:
    """Patokan harga INDEPENDEN untuk kartu pembuatan pool. Dipanggil di thread.

    Menggabungkan GMGN, pool terdalam GeckoTerminal (pasangan apa pun, termasuk
    ber-hooks), dan `token_usd_price` bot sendiri — persis dua sumber yang disebut
    user saat harga rujukan meleset: "tinggal ambil dari pool terdalam dengan hook
    atau gmgn bisa"."""
    cid = ctx["chain"]
    tok = ctx["token"]["address"]
    extra = [("GMGN", gmgn_price_usd(cid, tok))]
    return ch.token_anchor_price(cid, tok, p.get("quote_sym"), extra=extra)


def np_build(ctx: dict) -> tuple[dict, int, dict | None, str | None, float, list, dict]:
    """(pool_info, sqrtPrice rujukan, pool rujukan, alasan ditolak, deviasi,
    kandidat, anchor). Dipanggil di thread."""
    cid = ctx["chain"]
    w3 = ch.get_w3(cid)
    fee, sp = int(ctx["fee"]), np_spacing(ctx)
    p = ch.v4_new_pool_info(w3, cid, ctx["token"]["address"], np_quote(ctx), fee, sp)
    sq, ref, cands = ch.v4_ref_sqrt_price(w3, cid, p["key"][0], p["key"][1], ctx["pools"])
    td = int(ctx["token"].get("decimals") or 18)
    for c in cands:
        c["price"] = np_price(p, td, c["sq"])
        c["hooked"] = False

    # Patokan INDEPENDEN dulu, sebelum menyentuh median pool sepasang. Median pool
    # sepasang saja pernah dihitung dari tiga pool debu (volume $8,04 / $0,76 /
    # $0,06) yang harganya berselisih 100× — lihat `ch.token_anchor_price`.
    try:
        anchor = np_anchor(ctx, p)
    except Exception:
        anchor = {"usd": 0.0, "per_quote": 0.0, "srcs": [], "deep": "", "deep_vol": 0.0}
    ap = float(anchor.get("per_quote") or 0)

    live = [c for c in cands if c["price"] > 0 and c["vol"] > 0]

    # Pool BER-HOOKS ikut jadi pembanding harga (bukan tempat menaruh dana). Di
    # token launchpad justru di situlah seluruh volumenya: terukur DOT/USDC Arc,
    # pool ber-hook $441.817/24 jam sementara semua pool tanpa hook digabung ~$800.
    if live or ap > 0:
        try:
            hooked = ch.v4_hook_price_refs(
                cid, ctx["token"]["address"], p["quote_addr"],
                [c["price"] for c in live],
                skip_ids={str(c["pool"].get("pool")) for c in cands},
                anchor=ap)
        except Exception:
            hooked = []
        live += [h for h in hooked if h["vol"] > 0]
        cands = cands + hooked

    # Buang kandidat yang jauh dari patokan independen SEBELUM median dihitung.
    # Tanpa ini, pool yang harganya mustahil ikut menentukan mediannya sendiri dan
    # saringan dua-lintasan di bawah tidak menolong sama sekali kalau yang tersisa
    # memang semuanya debu.
    if ap > 0:
        keep = [c for c in live if abs(math.log(c["price"] / ap)) <= math.log(NP_ANCHOR_DROP)]
        live = keep
    # Kalau tidak ada satu pun pool BERVOLUME yang masuk akal, pool tanpa volume
    # masih lebih baik daripada tidak ada rujukan — asalkan ia dekat patokan.
    no_ref = False
    if not live and ap > 0:
        live = [c for c in cands
                if c["price"] > 0
                and abs(math.log(c["price"] / ap)) <= math.log(NP_ANCHOR_DROP)]
        # Masih kosong = harga pasarnya DIKETAHUI tapi tidak ada satu pun pool
        # sepasang yang mendekatinya. Jangan jatuh balik ke pilihan `v4_ref_sqrt_price`
        # (pool bervolume terbesar): di BEORN/USDG itu justru pool ber-tick mentok
        # berharga 2,9e-27 dengan volume terlapor $1.473.
        no_ref = not live

    # Pool ber-tick mentok (harga ~1e-20) tetap punya volume kecil, dan satu saja
    # cukup menyeret median. Dua lintasan: median kasar dulu, lalu buang yang lebih
    # dari 10x dari situ.
    if len(live) > 2:
        m0 = np_median([c["price"] for c in live])
        if m0 > 0:
            keep = [c for c in live if abs(math.log(c["price"] / m0)) <= math.log(10)]
            if keep:
                live = keep
    # Tandai kandidat yang BENAR-BENAR dipakai menghitung median, supaya daftar di
    # kartu dan persen deviasinya berasal dari himpunan yang sama. Sebelum ini kartu
    # menampilkan 5 teratas per volume sedangkan median dihitung dari semuanya, dan
    # angkanya tidak bisa direkonsiliasi user.
    for c in live:
        c["used"] = True
    dev = 0.0
    med = 0.0
    pick_px = 0.0
    if live:
        med = np_median([c["price"] for c in live])
        # Rujukan sqrtPrice HARUS dari pool tanpa hook: hanya di situ urutan currency
        # dan desimalnya pasti (PoolKey-nya kita yang susun). Harga pool ber-hook
        # cuma dipakai menggeser MEDIAN-nya, dan itu justru intinya.
        # Pool ber-hook boleh jadi sumber harga awal HANYA kalau `sq_ok`: urutan
        # currency dan desimal kedua sisinya sama dengan PoolKey target, sehingga
        # sqrtPriceX96-nya berlaku apa adanya. Untuk pool tanpa hook itu selalu
        # benar — PoolKey-nya kita sendiri yang susun.
        vanilla = [c for c in live if not c.get("hooked") or c.get("sq_ok")]
        if vanilla and med:
            # Di antara pool yang harganya SUDAH rapat dengan harga pasar,
            # yang paling ramai diperdagangkan adalah sumber terbaik: harganya yang
            # paling sering diarbitrase, jadi paling kecil kemungkinan basi.
            # "Terdekat median" saja bisa memilih pool mati — terukur pada LONG/USDC
            # Arc, ia memilih pool fee 3,36% bervolume $0,11 padahal pool fee 1%
            # bervolume $109.5k harganya sama-sama rapat.
            near = ([c for c in vanilla
                     if abs(math.log(c["price"] / ap)) <= math.log(1.10)] if ap > 0 else [])
            pick = (max(near, key=lambda c: c["vol"]) if near
                    else min(vanilla, key=lambda c: abs(math.log(c["price"] / med))))
            sq, ref = pick["sq"], pick["pool"]
            pick_px = pick["price"]
            dev = pick_px / med - 1
    bad = ch.v4_check_new_pool(w3, cid, p["key"], sq)

    # Di atas ambang ini pembuatan DITOLAK, bukan sekadar diperingatkan — harga
    # awal yang meleset sejauh itu dijamin diambil arbitraser dari deposit pertama.
    # Deviasi terhadap patokan independen — dipakai untuk MEMBATALKAN blokir di
    # atas. Kalau pool sepasang saling tidak sepakat (lazim saat semuanya debu)
    # tapi yang dipilih justru rapat dengan harga pasar, mediannya yang tidak
    # bermakna, bukan pilihannya. Terukur di BEORN/WETH Robinhood: dua pool
    # sepasang berselisih 3,6x (volume $0,40 dan $0,63) sementara pilihannya cuma
    # 24,9% dari harga pasar tiga sumber.
    dev_ap = (pick_px / ap - 1) if (ap > 0 and pick_px > 0) else None
    if not bad and abs(dev) > NP_DEV_BLOCK and (dev_ap is None or abs(dev_ap) > NP_DEV_BLOCK):
        lain = "" if dev_ap is None else f" dan {dev_ap * 100:+.0f}% dari harga pasar"
        bad = (f"Harga rujukan meleset {dev * 100:+.0f}% dari median pool yang "
               f"benar-benar diperdagangkan (termasuk pool ber-hooks){lain}. Pool baru "
               f"yang lahir di harga itu langsung diarbitrase dari deposit Anda — "
               f"pilih quote lain atau tunggu harganya rapat.")
    # Anchor-nya sendiri BISA TELAT beberapa persen (GeckoTerminal terukur jauh
    # tertinggal di Arc), jadi ambangnya sengaja kasar: yang dikejar keadaan di mana
    # SELURUH pool sepasang memang tidak layak jadi rujukan, bukan selisih puluhan
    # persen. Blok ini yang seharusnya berbunyi untuk BEORN/USDG, bukan "+859%"
    # terhadap median debu.
    if not bad and ap > 0 and med > 0 and abs(math.log(med / ap)) > math.log(NP_ANCHOR_BLOCK):
        bad = (f"Harga pool sepasang ({ch.fmt_price(med)}) meleset "
               f"{med / ap:.1f}x dari harga pasar {ch.fmt_price(ap)} "
               f"{p['quote_sym']}/{ctx['token']['symbol']} "
               f"({', '.join(n for n, _ in anchor.get('srcs') or [])}). "
               f"Tidak ada pool {ctx['token']['symbol']}/{p['quote_sym']} yang layak "
               f"jadi rujukan — pilih quote lain (mis. pasangan pool terdalamnya).")
    if not bad and no_ref:
        bad = (f"Harga pasar {ctx['token']['symbol']} diketahui ({ch.fmt_price(ap)} "
               f"{p['quote_sym']} — {', '.join(n for n, _ in anchor.get('srcs') or [])}) "
               f"tapi tidak ada pool {ctx['token']['symbol']}/{p['quote_sym']} yang "
               f"harganya mendekati itu. Menyalin harga dari pool yang ada = pool baru "
               f"lahir salah harga. Pilih quote lain.")
    if not bad and sq <= 0 and ap <= 0:
        bad = "Harga awal tidak diketahui — tidak ada pool rujukan untuk pasangan ini."
    return p, sq, ref, bad, dev, cands, anchor


def np_text(ctx: dict, p: dict, sq: int, ref: dict | None, bad: str | None,
            dev: float = 0.0, cands: list | None = None,
            anchor: dict | None = None) -> str:
    cid = ctx["chain"]
    tsym = ctx["token"]["symbol"]
    fee, sp = int(ctx["fee"]), np_spacing(ctx)
    ada = ch.v4_pool_exists(ch.get_w3(cid), cid, p["pool_id"])
    L = [f"<b>➕ Buat pool v4 baru · {esc(ch.CHAINS[cid]['name'])}</b>",
         f"{esc(tsym)}/{esc(p['quote_sym'])} · fee <b>{fee / 1e4:g}%</b> · "
         f"tick spacing <b>{sp}</b> (kisi {box_pct(p):.4f}%)",
         f"poolId: <code>{esc(p['pool'])}</code>"]
    if ref is not None and sq > 0:
        # Harga dihitung lewat `_meme_price` yang SAMA dengan kartu mint, bukan
        # rumus tersendiri. Versi pertama menuliskannya ulang dan tandanya terbalik
        # saat quote jadi currency0 (`10**(qd-td)` bukan `10**(td-qd)`): pool
        # DOT/USDC 5% yang harga awalnya 0,00027480 USDC tampil sebagai "0.0₂₀0",
        # meleset 1e24 — dan itu justru angka yang paling harus dipercaya user.
        harga = np_price(p, int(ctx["token"].get("decimals") or 18), sq)
        vol = ref.get("vol24_usd")
        # Rujukan bisa berupa pool ber-hook (fee-nya tidak diketahui dari poolId —
        # itu hash). Menulis `fee/1e4` apa adanya akan meledak di None.
        rfee = ref.get("fee")
        lbl = f"fee {rfee / 1e4:g}%" if rfee else f"🪝 ber-hook {esc(ref.get('name') or '')}"
        L.append(f"\nHarga awal: <b>{ch.fmt_price(harga)} {esc(p['quote_sym'])}</b>/{esc(tsym)}\n"
                 f"<i>disalin dari pool v{ref.get('ver')} {lbl} "
                 f"(TVL {ch.fmt_usd(ref.get('tvl_usd'))}, vol 24j "
                 f"{ch.fmt_usd(vol) if vol else '—'}) — bukan tebakan.</i>")
        # Pool yang TIDAK pernah ditransaksikan tidak tahu harga apa pun. Kejadian
        # nyata: DOT/USDC 5% dibuat dari pool fee 20% ber-TVL $89 yang harganya belum
        # pernah bergerak — pool barunya lahir 27% di atas pasar lalu diseret turun,
        # dan biayanya keluar dari deposit pertama.
        if not vol:
            L.append("⚠️ <b>Pool rujukan ini tidak punya volume 24 jam</b> — harganya bisa "
                     "basi. Bandingkan dulu dengan harga di luar (GMGN/DexScreener) "
                     "sebelum membuat pool.")
        live = [x for x in (cands or []) if x.get("used")]
        if live:
            lain = ", ".join(
                (("🪝 " if c.get("hooked") else "")
                 + (f"{c['pool'].get('fee', 0) / 1e4:g}%" if c["pool"].get("fee") else "hook")
                 + f" ({ch.fmt_usd(c['vol'])}) → {ch.fmt_price(c.get('price') or 0)}")
                for c in sorted(live, key=lambda x: -x["vol"])[:6])
            L.append(f"<i>Pool sepasang yang ada volumenya: {esc(lain)}</i>")
        if abs(dev) > 0.10 and live:
            L.append(f"⚠️ Harga rujukan <b>{dev * 100:+.0f}%</b> dari median pool di atas.")
    # Patokan independen selalu disebut, juga saat semuanya rapat: user perlu bisa
    # membandingkan harga awal pool dengan harga yang ia lihat di GMGN sendiri.
    ap = float((anchor or {}).get("per_quote") or 0)
    if ap > 0:
        # Sumbernya dalam DOLAR, sedangkan `per_quote` sudah dibagi harga quote.
        # Menuliskannya tanpa satuan membuat "GMGN 0,000196" terbaca sebagai
        # WETH/BEORN padahal itu USD — meleset 2.600x di quote ETH.
        src = ", ".join(f"{esc(n)} ${ch.fmt_price(v)}" for n, v in (anchor.get("srcs") or []))
        deep = anchor.get("deep")
        L.append(f"\n<b>Harga pasar (di luar pool sepasang):</b> {ch.fmt_price(ap)} "
                 f"{esc(p['quote_sym'])}/{esc(tsym)}\n<i>{src}"
                 + (f" · pool terdalam {esc(deep)} (vol {ch.fmt_usd(anchor.get('deep_vol'))})"
                    if deep else "") + "</i>")
        if sq > 0 and ref is not None:
            h = np_price(p, int(ctx["token"].get("decimals") or 18), sq)
            if h > 0:
                L.append(f"<i>Harga awal di atas {h / ap - 1:+.1%} dari itu.</i>")
                # Diperingatkan, tidak diblokir: patokan ini BISA telat beberapa
                # puluh persen (terukur di Arc GeckoTerminal melaporkan CRCL $67,77
                # saat pool terdalam on-chain $202,56). Yang memblokir tetap dua
                # ambang yang dihitung dari pembacaan on-chain serentak.
                if abs(math.log(h / ap)) > math.log(1.25):
                    L.append(f"⚠️ Selisihnya di atas 25%. Kalau harga pasar di atas yang "
                             f"benar, arbitraser mengambil {abs(h / ap - 1) * 100:.0f}% "
                             f"dari deposit pertama Anda. Bandingkan dulu di GMGN.")
    if ada:
        L.append("\n✅ Pool ini <b>sudah ada</b> — tidak ada tx pembuatan, "
                 "tombol di bawah langsung ke kartu mint.")
    elif bad:
        L.append(f"\n❌ {esc(bad)}")
    else:
        L.append("\n⚠️ <b>Anda yang menentukan harga pool ini.</b> Pool baru kosong: tidak ada "
                 "LP lain, tidak ada volume, dan harga awal di atas jadi harga pool. Kalau "
                 "meleset dari pasar, arbitraser mengambil selisihnya dari deposit pertama — "
                 "yaitu milik Anda.")
        L.append("<i>Setor DUA SISI di sekitar harga itu (Wide/Stable/Rapat). Satu sisi "
                 "(Lower/Upper) di pool kosong berarti tick aktif tanpa likuiditas: swap "
                 "pertama menyapu seluruh range Anda sekaligus di harga tepi.</i>")
        L.append(f"<i>Tick spacing menentukan lebar kotak minimum — {sp} = {box_pct(p):.4f}% "
                 f"per kotak. Itu knop yang menentukan serapat apa range bisa disetel.</i>")
    return "\n".join(L)


def np_kb(key: str, ctx: dict, p: dict, bad: str | None, ada: bool) -> InlineKeyboardMarkup:
    fee, sp = int(ctx["fee"]), np_spacing(ctx)
    rows = []
    if ada:
        rows.append([InlineKeyboardButton("➡️ Ke kartu mint", callback_data=f"npgo|{key}")])
    elif not bad:
        rows.append([InlineKeyboardButton("➡️ Siapkan mint (pool dibuat saat Confirm)",
                                          callback_data=f"npok|{key}")])
    qs = np_quotes(ctx)
    if len(qs) > 1:
        cur = str(np_quote(ctx)).lower()
        rows.append([InlineKeyboardButton(("✓ " if a.lower() == cur else "") + sym,
                                          callback_data=f"npq|{key}|{a}") for sym, a in qs[:4]])
    rows.append([InlineKeyboardButton(("✓ " if f == fee else "") + f"{f / 1e4:g}%",
                                      callback_data=f"npf|{key}|{f}") for f in NP_FEES[:3]])
    rows.append([InlineKeyboardButton(("✓ " if f == fee else "") + f"{f / 1e4:g}%",
                                      callback_data=f"npf|{key}|{f}") for f in NP_FEES[3:]])
    pres = np_spacing_presets(fee)
    for i in (0, 3):
        chunk = pres[i:i + 3]
        if chunk:
            rows.append([InlineKeyboardButton(
                ("✓ " if v == sp else "") + (f"{lbl} {v}" if lbl else f"kisi {v}"),
                callback_data=f"nps|{key}|{v}") for v, lbl in chunk])
    rows.append([InlineKeyboardButton("✏️ Fee lain…", callback_data=f"npxf|{key}"),
                 InlineKeyboardButton("✏️ Kisi lain…", callback_data=f"npxs|{key}")])
    rows.append([InlineKeyboardButton("⬅️ Pool lain", callback_data=f"npback|{key}"),
                 InlineKeyboardButton("✖ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def np_refresh_sqrt(ctx_data: dict) -> tuple[int, str]:
    """(sqrtPriceX96 terbaru, alasan gagal) untuk pool yang belum ada.

    Dihitung ulang dari discovery SEGAR memakai jalur yang sama dengan kartu
    pembuatan — termasuk pool ber-hooks sebagai pembanding — lalu ditolak kalau
    devisinya sudah lewat `NP_DEV_BLOCK`. Dipanggil di thread."""
    cid = ctx_data["chain"]
    p0 = ctx_data["pool_info"]
    if ch.v4_pool_exists(ch.get_w3(cid), cid, p0["pool_id"]):
        return 0, "pool sudah dibuat pihak lain — buka ulang kartunya"
    res = ch.discover_any(cid, ctx_data["token"]["address"])
    ctx = {"chain": cid, "token": ctx_data["token"], "pools": res.get("pools") or [],
           "quote_addr": p0["quote_addr"], "fee": p0["fee"],
           "spacing": p0["tick_spacing"]}
    p, sq, ref, bad, dev, _c, _a = np_build(ctx)
    if bad:
        return 0, bad
    if sq <= 0:
        return 0, "tidak ada pool rujukan"
    if p["pool"] != p0["pool"]:
        return 0, "PoolKey berubah — buka ulang kartunya"
    return int(sq), ""


async def do_newpool(update: Update, key: str):
    """Siapkan kartu konfirmasi mint untuk pool baru — TANPA membuat pool dulu.

    Pembuatan pool dipindah ke dalam `do_mint`, di `TX_LOCK` yang sama dengan
    mint-nya. Versi pertama membuat pool lebih dulu lalu menampilkan kartu dan
    MENUNGGU user menekan Confirm; harga pool baru beku sampai ada likuiditas,
    jadi jeda itu — berapa detik pun — berarti menyetor ke harga yang sudah basi,
    dan selisihnya diambil arbitraser dari deposit pertama.

    Digabung jadi satu tx lewat `posm.multicall` memang mungkin (selectornya ada di
    ketiga chain v4), tapi itu berarti menyentuh isi `mint_v4` — jalur dana yang
    tidak bisa diuji ulang tanpa biaya. Dua tx berurutan dalam satu lock sudah
    menutup celah yang nyata, yaitu jeda manusianya."""
    ctx = NEWPOOL.get(key)
    if not ctx:
        await reply(update, "⚠️ Tombol kadaluarsa (bot sempat restart). Paste alamat lagi.")
        return
    cid = ctx["chain"]
    try:
        p, sq, ref, bad, dev, cands, anchor = await asyncio.to_thread(np_build, ctx)
    except Exception as e:
        await reply(update, f"❌ {esc(e)}")
        return
    ada = await asyncio.to_thread(ch.v4_pool_exists, ch.get_w3(cid), cid, p["pool_id"])
    if bad and not ada:
        await reply(update, f"❌ {esc(bad)}")
        return
    s_set = store.load_settings(cid)
    mk = uuid.uuid4().hex[:10]
    PENDING[mk] = {"chain": cid, "token": ctx["token"], "pool_info": p,
                   # DUA SISI untuk pool baru: di pool kosong, posisi satu sisi
                   # meninggalkan tick aktif tanpa likuiditas sama sekali.
                   "mode": "wide",
                   "low_pct": s_set["width_pct"], "up_pct": 100.0,
                   "amount_pct": s_set["amount_pct"], "amount_fixed": s_set["amount_fixed"],
                   "amount_src": "quote", "gap": int(s_set.get("gap", 1)),
                   "vol": None, "rec": None,
                   # Dipakai ctx_slot0() selama pool belum ada, dan jadi harga
                   # `initialize` saat Confirm ditekan.
                   "init_sqrtp": (0 if ada else int(sq))}
    msg = await reply(update, "⏳ Menyiapkan kartu mint…")
    await show_confirm(msg, mk)


async def show_newpool(msg, key: str):
    ctx = NEWPOOL.get(key)
    if not ctx:
        await edit(msg, "⚠️ Tombol kadaluarsa (bot sempat restart). Paste alamat lagi.")
        return
    try:
        p, sq, ref, bad, dev, cands, anchor = await asyncio.to_thread(np_build, ctx)
        ada = await asyncio.to_thread(ch.v4_pool_exists, ch.get_w3(ctx["chain"]),
                                      ctx["chain"], p["pool_id"])
        text = await asyncio.to_thread(np_text, ctx, p, sq, ref, bad, dev, cands, anchor)
    except Exception as e:
        await edit(msg, f"❌ {esc(e)}")
        return
    ctx["_pool"], ctx["_sq"] = p, sq
    await edit(msg, text, np_kb(key, ctx, p, bad, ada))


def box_pct(pool_info: dict) -> float:
    """Lebar satu kotak tick-spacing dalam persen — presisi terbaik pool ini.
    Pool fee 5% biasanya spacing 1000 (≈10,5%), fee 0,05% spacing 10 (≈0,1%)."""
    sp = int(pool_info.get("tick_spacing") or ch.TICK_SPACING.get(pool_info.get("fee"), 60) or 60)
    return round((math.exp(0.0001 * sp) - 1) * 100, 4)


def budget_sym(ctx_data: dict) -> str:
    """Simbol satuan budget kartu ini. `amount_fixed` selalu dalam satuan INI.

    lower/wide/stable = quote pool; upper = token meme (lihat compute_amount).
    `amount_src` tidak ikut menentukan: jumlah tetap men-short-circuit persen."""
    if ctx_data.get("mode") == "upper":
        return ctx_data["token"]["symbol"]
    return ctx_data["pool_info"]["quote_sym"]


def _presets_norm(vals) -> list[float]:
    out = []
    for x in vals or []:
        try:
            f = float(x)
        except (TypeError, ValueError):
            continue
        if f > 0 and f not in out:
            out.append(f)
    return sorted(out)[:4]


def presets_get(cid: int, sym: str) -> list[float]:
    """Preset EKSPLISIT simbol ini di chain ini (tanpa tebakan).

    Disimpan per-chain (`{"4663": {"USDG": [...]}}`) karena simbol yang sama bisa
    ada di beberapa chain dengan besaran yang wajar berbeda — 0,01 ETH di
    Robinhood belum tentu sama maunya dengan 0,01 ETH di Base. Bentuk LAMA yang
    datar (`{"USDG": [...]}`) tetap dibaca sebagai default lintas-chain supaya
    settings.json yang sudah ada tidak perlu diutak-atik."""
    per = (store.load_settings().get("amount_presets") or {}).get(str(cid)) or {}
    return _presets_norm(per.get(sym) or per.get(sym.upper()))


def presets_set(cid: int, sym: str, vals) -> list[float]:
    s = store.load_settings()
    tbl = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in (s.get("amount_presets") or {}).items()}
    per = dict(tbl.get(str(cid)) or {})
    vals = _presets_norm(vals)
    if vals:
        per[sym.upper()] = vals
    else:
        per.pop(sym.upper(), None)
    tbl[str(cid)] = per
    store.set_global("amount_presets", tbl)
    return vals


def preset_syms(cid: int) -> list[str]:
    """Simbol yang layak punya tombol jumlah di chain ini, urut stabil."""
    cfg = ch.CHAINS.get(cid) or {}
    out = list((cfg.get("quotes") or {}).keys())
    for extra in (cfg.get("wrapped_symbol"), cfg.get("native_symbol")):
        if extra and extra not in out:
            out.append(extra)
    tbl = (store.load_settings().get("amount_presets") or {}).get(str(cid)) or {}
    for k in tbl:
        if k not in out:
            out.append(k)     # simbol yang ditambahkan user sendiri (mis. token meme)
    return out


def amount_presets(sym: str, cid: int | None = None) -> list[float]:
    """Jumlah tetap yang ditawarkan untuk simbol ini (dari /presets atau menu).

    Tanpa entri, hanya simbol QUOTE yang ditebak — stable pakai satuan puluhan,
    wrapped/native pakai pecahan. Token meme TIDAK pernah ditebak: satuannya bisa
    ribuan atau miliaran tergantung supply, jadi "0,01 microduck" cuma tombol yang
    tidak pernah masuk akal. Untuk meme, tombol A% memang sudah jawabannya.
    Menebak lewat harga USD akan lebih tepat tapi itu panggilan RPC di jalur render
    keyboard — tidak sepadan untuk angka yang cuma saran."""
    v = presets_get(cid, sym) if cid is not None else []
    if not v:
        tbl = store.load_settings().get("amount_presets") or {}
        legacy = tbl.get(sym) or tbl.get(sym.upper())
        v = _presets_norm(legacy) if isinstance(legacy, list) else []
    if not v:
        known = {"USD" in sym.upper()}
        if cid in ch.CHAINS:
            cfg = ch.CHAINS[cid]
            known.add(sym in (cfg.get("quotes") or {}))
            known.add(sym == cfg.get("wrapped_symbol") or sym == cfg.get("native_symbol"))
        if not any(known):
            return []
        v = [10, 25, 50] if "USD" in sym.upper() else [0.01, 0.025, 0.05]
    return _presets_norm(v)


def confirm_kb(key: str, ctx_data: dict) -> InlineKeyboardMarkup:
    mode = ctx_data["mode"]
    rec = ctx_data["rec"]
    if ctx_data["pool_info"].get("ver") == 2:
        def abtn2(a):
            mark = "✓ " if (not ctx_data["amount_fixed"] and ctx_data["amount_pct"] == a) else ""
            return InlineKeyboardButton(f"{mark}A {a:g}%", callback_data=f"amt|{key}|{a}")
        bsym2 = ctx_data["pool_info"]["quote_sym"]

        def fbtn2(v):
            mark = "✓ " if ctx_data["amount_fixed"] and float(ctx_data["amount_fixed"]) == v else ""
            return InlineKeyboardButton(f"{mark}{v:g} {bsym2}", callback_data=f"amtf|{key}|{v:g}")

        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm add", callback_data=f"mint|{key}"),
             InlineKeyboardButton("❌ Cancel", callback_data=f"cancelp|{key}")],
            [InlineKeyboardButton("⬅️ Pool lain", callback_data=f"pools|{key}")],
            [abtn2(a) for a in (25, 50, 75, 100)],
            *([[fbtn2(v) for v in amount_presets(bsym2, ctx_data["chain"])]]
              if amount_presets(bsym2, ctx_data["chain"]) else []),
            [InlineKeyboardButton("✏️ Custom Amount…", callback_data=f"askamt|{key}")],
        ])

    def sbtn(m):
        mark = "✓ " if m == mode else ("⭐ " if m == rec else "")
        return InlineKeyboardButton(f"{mark}{STRAT_LABEL[m]}", callback_data=f"st|{key}|{m}")

    def wbtn(low, up):
        cur = (ctx_data["low_pct"], ctx_data["up_pct"])
        mark = "✓ " if cur == (low, up) else ""
        if mode == "stable":
            lbl = f"±{low:g}%"
        elif mode == "wide":
            lbl = f"−{low:g}/+{up:g}"
        elif mode == "lower":
            lbl = f"−{low:g}%"
        else:
            lbl = f"+{up:g}%"
        return InlineKeyboardButton(f"{mark}{lbl}", callback_data=f"wd|{key}|{low}|{up}")

    def abtn(a):
        mark = "✓ " if (not ctx_data["amount_fixed"] and ctx_data["amount_pct"] == a) else ""
        return InlineKeyboardButton(f"{mark}A {a:g}%", callback_data=f"amt|{key}|{a}")

    # Baris pemilih SUMBER persen: tombol A% mempersenkan saldo quote (lama) atau
    # saldo token meme. Tidak ditampilkan di mode "upper" — mode itu memang selalu
    # memakai meme, jadi pilihannya cuma menyesatkan.
    src_sel = ctx_data.get("amount_src", "quote")

    def srcbtn(val, label):
        return InlineKeyboardButton(("✓ " if src_sel == val else "") + label,
                                    callback_data=f"amtsrc|{key}|{val}")

    rows = [
        [InlineKeyboardButton(
            "✅ Buat pool + mint" if ctx_data.get("init_sqrtp") else "✅ Confirm mint",
            callback_data=f"mint|{key}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"cancelp|{key}")],
        # Kembali ke DAFTAR POOL token yang sama. Tanpa ini satu-satunya jalan
        # membandingkan pool lain adalah Cancel lalu menempel ulang CA-nya.
        [InlineKeyboardButton("⬅️ Pool lain", callback_data=f"pools|{key}")],
        [sbtn(m) for m in ("stable", "wide", "lower", "upper")],
        [wbtn(lo, up) for lo, up in STRAT_PRESETS[mode]],
        [InlineKeyboardButton("🎯 Rapat — langsung aktif (2 sisi)", callback_data=f"tight|{key}")],
    ]
    if mode != "upper":
        rows.append([srcbtn("quote", f"💰 {ctx_data['pool_info']['quote_sym']}"),
                     srcbtn("meme", f"🪙 {ctx_data['token']['symbol']}")])
    # Tombol izin impact tinggi hanya muncul kalau memang terlewati — kalau selalu
    # ada, user terbiasa menekannya dan penjagaannya jadi percuma.
    imp = ctx_data.get("_impact")
    lim = ctx_data.get("max_impact") or impact_limit()
    if imp is not None and imp > lim:
        rows.append([InlineKeyboardButton(
            f"⚠️ Saya paham, lanjut walau impact {imp * 100:.0f}%",
            callback_data=f"okimp|{key}")])
    bsym = budget_sym(ctx_data)

    def fbtn(v):
        mark = "✓ " if ctx_data["amount_fixed"] and float(ctx_data["amount_fixed"]) == v else ""
        return InlineKeyboardButton(f"{mark}{v:g} {bsym}", callback_data=f"amtf|{key}|{v:g}")

    rows.append([abtn(a) for a in (25, 50, 75, 100)])
    fixed = [fbtn(v) for v in amount_presets(bsym, ctx_data["chain"])]
    if fixed:
        rows.append(fixed)
    rows.append([InlineKeyboardButton("✏️ Custom Range…", callback_data=f"askrng|{key}"),
                 InlineKeyboardButton("✏️ Custom Amount…", callback_data=f"askamt|{key}")])
    return InlineKeyboardMarkup(rows)


async def show_confirm(msg, key: str):
    ctx_data = PENDING.get(key)
    if not ctx_data:
        await edit(msg, "⚠️ Tombol kadaluarsa (bot sempat restart). Paste alamat lagi.")
        return
    try:
        text = await asyncio.to_thread(build_preview, ctx_data)
    except Exception as e:
        await edit(msg, f"❌ {esc(e)}")
        return
    text += pool_warnings(ctx_data["chain"], ctx_data["pool_info"]) + newpool_note(ctx_data)
    await edit(msg, text, confirm_kb(key, ctx_data))
    LAST_CONFIRM[msg.chat_id] = (key, msg)


def _num_usd(s: str) -> float:
    """'300k' → 300000, '1.2m' → 1200000, '0.5b' → 5e8."""
    s = s.strip().rstrip(",")
    mult = 1.0
    if s and s[-1] in "kmb":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}[s[-1]]
        s = s[:-1]
    return float(s) * mult


def parse_range_input(text: str, mode: str, mc_now: float) -> tuple[float, float]:
    """Parse balasan range: persen ('40', '40 120') atau market cap ('mc 300k 800k',
    '300k 800k'). Return (low_pct, up_pct)."""
    t = text.lower().replace("$", "").replace("%", "").replace("–", " ").replace("-", " ").strip()
    # "r 40 120" / "range 40 120" — bentuk yang dipakai kalau diketik sebagai pesan
    # biasa. Diterima juga di sini supaya user tidak perlu ingat dua format.
    for pfx in ("range ", "r "):
        if t.startswith(pfx):
            t = t[len(pfx):].strip()
            break
    is_mc = t.startswith("mc")
    if is_mc:
        t = t[2:].strip()
    parts = [p for p in t.split() if p]
    if not parts or len(parts) > 2:
        raise ValueError("format tidak dikenal")
    has_suffix = any(p[-1] in "kmb" for p in parts)
    vals = [_num_usd(p) for p in parts]
    if not is_mc and not has_suffix and all(v <= 500 for v in vals):
        # persen
        if len(vals) == 2:
            return vals[0], vals[1]
        return vals[0], vals[0]
    # market cap absolut → konversi ke persen relatif MC sekarang
    if mc_now <= 0:
        raise ValueError("MC sekarang tidak tersedia")
    if len(vals) == 2:
        lo_mc, hi_mc = sorted(vals)
        if not (lo_mc < mc_now < hi_mc) and mode in ("wide", "stable"):
            raise ValueError(f"MC sekarang {ch.fmt_usd(mc_now)} harus di antara batas range")
        return max(0.5, (1 - lo_mc / mc_now) * 100), max(0.5, (hi_mc / mc_now - 1) * 100)
    v = vals[0]
    if mode == "lower":
        if v >= mc_now:
            raise ValueError(f"batas bawah harus < MC sekarang ({ch.fmt_usd(mc_now)})")
        return (1 - v / mc_now) * 100, 100.0
    if mode == "upper":
        if v <= mc_now:
            raise ValueError(f"batas atas harus > MC sekarang ({ch.fmt_usd(mc_now)})")
        return 50.0, (v / mc_now - 1) * 100
    # stable/wide satu nilai MC → jarak simetris
    d = abs(v / mc_now - 1) * 100
    return max(0.5, d), max(0.5, d)


def current_mc(ctx_data: dict) -> float:
    """MC token sekarang (untuk prompt & konversi input MC)."""
    p = ctx_data["pool_info"]
    w3 = ch.get_w3(ctx_data["chain"])
    if p.get("ver") == 4:
        _, tick = ctx_slot0(ctx_data)
    elif p.get("ver") == 2:
        raise RuntimeError("Range tidak berlaku untuk pool v2.")
    else:
        pool = w3.eth.contract(address=ch.Web3.to_checksum_address(p["pool"]), abi=ch.POOL_ABI)
        tick = pool.functions.slot0().call()[1]
    supply = ch.token_supply(w3, _meme_addr(p))
    return _meme_price(p, ctx_data["token"]["decimals"], tick) * p["quote_usd"] * supply


async def ask_custom(update: Update, key: str, kind: str):
    ctx = PENDING.get(key)
    if not ctx:
        await reply(update, "⚠️ Kartu kadaluarsa. Paste alamat token lagi.")
        return
    tsym = ctx["token"]["symbol"]
    if kind == "range":
        try:
            mc_now = await asyncio.to_thread(current_mc, ctx)
            mc_txt = f"\nMC {esc(tsym)} sekarang: <b>{ch.fmt_usd(mc_now)}</b>"
        except Exception:
            mc_txt = ""
        txt = (f"✏️ <b>Balas pesan ini</b> dengan range untuk {esc(tsym)}:\n"
               f"· persen: <code>40</code> (satu sisi) atau <code>40 120</code> (−40%/+120%)\n"
               f"· market cap: <code>mc 300k 800k</code> atau <code>250k</code> (batas sesuai mode)"
               f"{mc_txt}")
    else:
        txt = (f"✏️ <b>Balas pesan ini</b> dengan amount:\n"
               f"· persen saldo: <code>30%</code>\n"
               f"· nilai pasti: <code>0.005</code> (satuan {esc(ctx['pool_info']['quote_sym'] if ctx['mode'] != 'upper' else tsym)})")
    await update.effective_chat.send_message(
        txt, parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder="contoh: 40 120 / mc 300k 800k"))
    AWAITING[update.effective_chat.id] = {"kind": kind, "key": key}


async def handle_awaiting(update: Update) -> bool:
    """Proses balasan untuk prompt custom. Return True kalau pesan dikonsumsi."""
    chat_id = update.effective_chat.id
    st = AWAITING.get(chat_id)
    if not st:
        return False
    if st["kind"] in ("npfee", "npspacing"):
        ctx = NEWPOOL.get(st["key"])
        if not ctx:
            AWAITING.pop(chat_id, None)
            await reply(update, "⚠️ Tombol kadaluarsa. Paste alamat lagi.")
            return True
        raw = (update.message.text or "").strip().replace("%", "").replace(",", ".")
        try:
            if st["kind"] == "npfee":
                # diketik dalam PERSEN (yang dilihat user), disimpan ppm
                v = int(round(float(raw) * 10_000))
                if not 0 <= v <= ch.V4_FEE_MAX:
                    raise ValueError
                ctx["fee"], ctx["spacing"] = v, None
            else:
                v = int(float(raw))
                if not ch.V4_SPACING_MIN <= v <= ch.V4_SPACING_MAX:
                    raise ValueError
                ctx["spacing"] = v
        except ValueError:
            lim = (f"0–{ch.V4_FEE_MAX / 1e4:g}%" if st["kind"] == "npfee"
                   else f"{ch.V4_SPACING_MIN}–{ch.V4_SPACING_MAX}")
            await reply(update, f"❌ Di luar rentang yang diterima PoolManager ({lim}).")
            return True
        AWAITING.pop(chat_id, None)
        msg = await reply(update, "⏳ Menghitung…")
        await show_newpool(msg, st["key"])
        return True
    if st["kind"] == "reducepct":
        raw = (update.message.text or "").strip().replace("%", "").replace(",", ".")
        try:
            pct = int(round(float(raw)))
        except ValueError:
            await reply(update, "❌ Isi angka 1–99. Contoh: <code>15</code>")
            return True
        if not 1 <= pct <= 99:
            await reply(update, "❌ Rentang 1–99. Untuk 100% pakai tombol Close.")
            return True
        AWAITING.pop(chat_id, None)
        pid = st["key"]
        await reply(update, f"➖ Tarik <b>{pct}%</b> dari {disp_pid(pid)}?",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton(f"✅ Ya, tarik {pct}%", callback_data=f"redok|{pid}|{pct}"),
                        InlineKeyboardButton("❌ Batal", callback_data="cancel")]]))
        return True
    if st["kind"] == "wallet_import":
        raw = (update.message.text or "").strip()
        # Hapus pesan berisi private key SECEPATNYA — kalau tidak, key-nya mengendap
        # di riwayat chat Telegram selamanya.
        try:
            await update.message.delete()
        except Exception:
            pass
        AWAITING.pop(chat_id, None)
        key = raw if raw.startswith("0x") else "0x" + raw
        try:
            addr = _addr_of(key)
        except Exception:
            await reply(update, "❌ Private key tidak valid. Harus 64 karakter hex.")
            return True
        if not store.add_wallet(key):
            await reply(update, f"⚠️ Wallet <code>{esc(addr)}</code> sudah ada.", wallets_kb())
            return True
        await reply(update,
                    f"✅ Wallet ditambahkan: <code>{esc(addr)}</code>\n"
                    f"<i>Pesan berisi key sudah dihapus dari chat. Key tersimpan di "
                    f"wallets.json (permission 600).</i>", wallets_kb())
        return True
    if st["kind"] == "btnadd":
        c, sym = st["key"].split("|", 1)
        cid = int(c)
        try:
            v = float((update.message.text or "").strip().replace(",", "."))
        except ValueError:
            await reply(update, "❌ Bukan angka. Contoh: <code>25</code> atau <code>0.05</code>.")
            return True
        if v <= 0:
            await reply(update, "❌ Jumlah harus lebih dari 0.")
            return True
        # Menambah ke daftar yang TAMPIL: kalau simbol ini masih memakai tebakan,
        # user mengharapkan tombol BERTAMBAH, bukan tiga tebakan hilang diganti satu.
        vals = presets_set(cid, sym, (presets_get(cid, sym) or amount_presets(sym, cid)) + [v])
        await reply(update, f"✅ Tombol <b>{esc(sym)}</b>: "
                            + ", ".join(f"{x:g}" for x in vals)
                            + ("\n<i>Maksimal 4 tombol per simbol.</i>" if len(vals) >= 4 else ""))
        await update.effective_chat.send_message(btn_text(cid), parse_mode=ParseMode.HTML,
                                                 reply_markup=btn_kb(cid))
        return True
    if st["kind"] == "scanfilt":
        AWAITING.pop(chat_id, None)
        err = scanfilt_set(st["key"], (update.message.text or "").strip())
        if err:
            await reply(update, f"❌ {esc(err)}")
            return True
        await update.effective_chat.send_message(
            scanfilt_one_text(st["key"]), parse_mode=ParseMode.HTML,
            reply_markup=scanfilt_one_kb(st["key"]))
        return True
    if st["kind"] == "scanchains":
        pilih = [x.strip().lower() for x in
                 (update.message.text or "").replace(",", " ").split() if x.strip()]
        tak = [x for x in pilih if x not in gmgn.CHAINS]
        if tak:
            await reply(update, f"❌ Chain tidak dikenal: {esc(', '.join(tak))}\n"
                                f"Pilihan: {esc(', '.join(gmgn.CHAINS))}")
            return True
        AWAITING.pop(chat_id, None)
        scanner_save({"chains": pilih})
        await update.effective_chat.send_message(scanner_text(), parse_mode=ParseMode.HTML,
                                                 reply_markup=scanner_kb())
        return True
    if st["kind"] == "setkey":
        key, c = st["key"].split("|", 1)
        cid2 = int(c)
        cur = store.load_settings(cid2)
        err = apply_setting(cur, key, (update.message.text or "").strip().lower())
        if err:
            await reply(update, f"❌ {esc(err)}")
            return True
        store.save_settings(cur, cid=cid2)
        AWAITING.pop(chat_id, None)
        await update.effective_chat.send_message(
            setkey_text(key, cid2), parse_mode=ParseMode.HTML,
            reply_markup=setkey_kb(key, cid2))
        return True
    if st["kind"] == "setval":
        parts = (update.message.text or "").strip().lower().split()
        if len(parts) != 2:
            await reply(update, f"❌ Format: <code>key value</code>\nkey: {SET_KEYS}")
            return True
        s = store.load_settings()
        err = apply_setting(s, parts[0], parts[1])
        if err:
            await reply(update, f"❌ {esc(err)}")
            return True
        store.save_settings(s)
        AWAITING.pop(chat_id, None)
        await reply(update, settings_text(), settings_kb())
        return True
    if st["kind"] == "addamt":
        text = (update.message.text or "").strip()
        try:
            t = text.replace("%", " %").split()
            val = float(t[0].replace(",", "."))
            is_pct = "%" in text
        except (ValueError, IndexError):
            await reply(update, "❌ Format tidak valid. Contoh: <code>0.005</code> atau <code>30%</code>")
            return True
        AWAITING.pop(chat_id, None)
        tid = st["key"]
        desc = f"{val:g}% saldo" if is_pct else f"{val:g} quote"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Tambah {desc} ke {disp_pid(tid)}",
                                  callback_data=f"addok|{tid}|{val:g}|{'p' if is_pct else 'f'}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ])
        msg = await reply(update, "⏳ Menghitung detail…")
        cid = store.load_settings()["chain"]
        try:
            txt = await asyncio.to_thread(add_confirm_text, cid, tid, val, is_pct)
        except Exception:
            txt = f"Konfirmasi tambah dana ke posisi {disp_pid(tid)}:"
        await edit(msg, txt, kb)
        return True
    if st["kind"] == "order":
        pid = st["key"]
        text = (update.message.text or "").strip()
        cid = store.load_settings()["chain"]

        def snap():
            return position_one(cid, pid)

        p = await asyncio.to_thread(snap)
        mc_now = (p.get("mc_now") or 0.0) if p else 0.0
        try:
            tp, sl = parse_tpsl(text, mc_now)
        except ValueError as e:
            await reply(update, f"❌ {esc(e)}\nContoh: <code>tp 800k</code> · "
                                f"<code>sl 200k</code> · <code>800k 200k</code>")
            return True  # tetap nunggu balasan berikutnya
        AWAITING.pop(chat_id, None)
        meme_sym = (p["sym0"] if p["quote_is_token1"] else p["sym1"]) if p else ""
        tp_s = str(int(round(tp))) if tp is not None else "x"
        sl_s = str(int(round(sl))) if sl is not None else "x"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Buat + auto-swap saat trigger",
                                  callback_data=f"orderok|{pid}|{tp_s}|{sl_s}|1")],
            [InlineKeyboardButton("✅ Buat, tahan token saat trigger",
                                  callback_data=f"orderok|{pid}|{tp_s}|{sl_s}|0")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ])
        await reply(update, (
            f"🎯 <b>Konfirmasi pesanan {esc(meme_sym)} {disp_pid(pid)}</b>\n"
            f"· TP (close saat MC ≥): <b>{ch.fmt_usd(tp) if tp is not None else '—'}</b>\n"
            f"· SL (close saat MC ≤): <b>{ch.fmt_usd(sl) if sl is not None else '—'}</b>\n"
            f"MC sekarang: {ch.fmt_usd(mc_now) if mc_now else '?'}\n\n"
            f"Saat trigger → posisi di-close otomatis (full exit)."), kb)
        return True
    key = st["key"]
    ctx = PENDING.get(key)
    if not ctx:
        AWAITING.pop(chat_id, None)
        return False
    text = (update.message.text or "").strip()
    try:
        if st["kind"] == "range":
            mc_now = 0.0
            try:
                mc_now = await asyncio.to_thread(current_mc, ctx)
            except Exception:
                pass
            low, up = parse_range_input(text, ctx["mode"], mc_now)
            if ctx["mode"] == "lower":
                ctx["low_pct"] = low
            elif ctx["mode"] == "upper":
                ctx["up_pct"] = up
            else:
                ctx["low_pct"], ctx["up_pct"] = low, up
        else:
            t = text.replace("%", " %").split()
            val = float(t[0].replace(",", "."))
            if "%" in text:
                ctx["amount_pct"] = min(100.0, max(1.0, val))
                ctx["amount_fixed"] = None
            else:
                ctx["amount_fixed"] = val
    except (ValueError, IndexError) as e:
        await reply(update, f"❌ Input tidak valid: {esc(e)}\nContoh: <code>40 120</code> · <code>mc 300k 800k</code> · <code>30%</code> · <code>0.005</code>")
        return True  # tetap nunggu balasan berikutnya
    AWAITING.pop(chat_id, None)
    ent = LAST_CONFIRM.get(chat_id)
    if ent and ent[0] == key:
        await show_confirm(ent[1], key)
    return True


async def apply_custom(update: Update, rng=None, amt=None):
    """Terapkan input custom (ketikan `r ...` / `a ...`) ke kartu konfirmasi aktif."""
    ent = LAST_CONFIRM.get(update.effective_chat.id)
    if not ent:
        await reply(update, "Tidak ada kartu konfirmasi aktif. Paste alamat token dulu.")
        return
    key, msg = ent
    ctx = PENDING.get(key)
    if not ctx:
        await reply(update, "⚠️ Kartu kadaluarsa. Paste alamat token lagi.")
        return
    if rng:
        v1, v2 = rng
        mode = ctx["mode"]
        if mode == "lower":
            ctx["low_pct"] = v1
        elif mode == "upper":
            ctx["up_pct"] = v1
        elif mode == "stable":
            ctx["low_pct"] = ctx["up_pct"] = v1
        else:  # wide
            ctx["low_pct"] = v1
            ctx["up_pct"] = v2 if v2 else v1
    if amt:
        val, is_pct = amt
        if is_pct:
            ctx["amount_pct"] = min(100.0, max(1.0, val))
            ctx["amount_fixed"] = None
        else:
            ctx["amount_fixed"] = val
    await show_confirm(msg, key)


async def do_mint(update: Update, ctx_data: dict):
    s = store.load_settings()
    cid = ctx_data["chain"]
    p = ctx_data["pool_info"]
    ver = p.get("ver", 3)
    tsym = ctx_data["token"]["symbol"]
    mode = ctx_data["mode"]
    strategy = {"max_impact": ctx_data.get("max_impact") or impact_limit(),
                "mode": mode, "low_pct": ctx_data["low_pct"], "up_pct": ctx_data["up_pct"],
                "gap": ctx_data.get("gap", 1)}

    # Pool BELUM ada: harga rujukan DISEGARKAN tepat sebelum eksekusi. Kartu bisa
    # saja didiamkan menit-menit sebelum Confirm ditekan, dan harga pool baru
    # ditentukan saat `initialize` — bukan saat kartunya dirender. Kalau tidak
    # disegarkan, jeda itu persis kembali jadi celah yang mau ditutup.
    if ver == 4 and ctx_data.get("init_sqrtp"):
        try:
            fresh, why = await asyncio.to_thread(np_refresh_sqrt, ctx_data)
        except Exception as e:
            fresh, why = 0, str(e)
        if not fresh:
            await reply(update, f"❌ Harga rujukan tidak bisa disegarkan: {esc(why)}")
            return
        ctx_data["init_sqrtp"] = fresh

    # Sama persis dengan kartu konfirmasi: untuk `amount_src="meme"` budget-nya
    # bergantung rasio range, jadi tick harus ikut dihitung — kalau tidak, jumlah
    # yang dieksekusi beda dari yang ditampilkan.
    def _amt():
        p_ = ctx_data["pool_info"]
        sq, ct = ctx_slot0(ctx_data)
        tk = ch.calc_strategy_range(ct, p_["fee"], p_["quote_is_token1"], mode,
                                    ctx_data["low_pct"], ctx_data["up_pct"],
                                    ctx_data.get("gap", 1), spacing=p_.get("tick_spacing"))
        return compute_amount(ctx_data, sq, tk)

    amount = await asyncio.to_thread(_amt)
    dep_sym = tsym if mode == "upper" else p["quote_sym"]
    if amount <= 0:
        await reply(update, "❌ " + esc(await asyncio.to_thread(no_funds_msg, ctx_data, dep_sym)))
        return

    mode_label = "V2 50/50" if ver == 2 else STRAT_LABEL[mode]
    head = (f"⏳ Minting position ({mode_label}) [v{ver}]...\n"
            f"<i>{esc(tsym)}/{esc(p['quote_sym'])} fee {p['fee'] / 10000:.2f}% · "
            f"deposit {ch.fmt_amount(amount)} {esc(dep_sym)} "
            f"(wrap/swap otomatis kalau perlu)</i>")
    status = await reply(update, head)

    def work():
        # Pool BELUM ada (alur "buat pool + mint"): initialize dulu, di dalam
        # `work` yang sama supaya keduanya duduk dalam SATU `TX_LOCK` dan tidak ada
        # jeda manusia di antaranya. Harga pool baru beku sampai ada likuiditas,
        # jadi jeda apa pun = menyetor ke harga yang sudah basi.
        if ver == 4 and ctx_data.get("init_sqrtp"):
            ch.v4_init_pool(cid, pk(), p["key"], int(ctx_data["init_sqrtp"]))
        if ver == 2:
            return ch.mint_v2(cid, pk(), p, amount, s["slippage_pct"])
        if ver == 4:
            return ch.mint_v4(cid, pk(), p, amount, strategy, s["slippage_pct"])
        return ch.mint_position(cid, pk(), p, amount, strategy, s["slippage_pct"])

    async with TX_LOCK:
        try:
            r = await with_progress(status, head, work)
        except Exception as e:
            await edit(status, f"❌ Mint gagal: {esc(e)}")
            return

    pos_cache_drop(cid)            # posisi baru lahir — daftar lama sudah salah
    if ver == 2:
        pid = f"v2:{r['pair'].lower()}"
        store.add_ref(cid, wallet_address(), "v2", r["pair"])
        store.set_v2_basis(cid, wallet_address(), r["pair"], r.get("k_per_lp") or 0,
                           r.get("lp_before", 0), r.get("lp_after", 0))
        store.record_event(cid, "mint", pid, r["deposited_usd"],
                           f"{tsym}/{p['quote_sym']} v2", wallet=wallet_address())
        lines = [f"✅ <b>{esc(tsym)} LP</b> [v2] · full range ({ch.fmt_usd(r['deposited_usd'])})",
                 f"Masuk: {ch.fmt_amount(r['quote_in'])} {esc(r['quote_sym'])} + "
                 f"{ch.fmt_amount(r['meme_in'])} {esc(r['meme_sym'])}"]
        for label, h in r["steps"]:
            lines.append(f"{label}: {ch.tx_link(cid, h)}")
        lines.append(ch.pos_link_any(cid, pid))
        g = await asyncio.to_thread(gas_line, cid)
        if g:
            lines.append(g)
        sesudah, kb = await after_action(cid, pid)
        await edit(status, "\n".join(lines + sesudah), kb)
        return

    pid = f"v4:{r['token_id']}" if ver == 4 else r["token_id"]
    if ver == 4 and r["token_id"]:
        store.add_ref(cid, wallet_address(), "v4", str(r["token_id"]))
    store.record_event(cid, "mint", pid, r["deposited_usd"],
                       f"{tsym}/{p['quote_sym']} {mode}", wallet=wallet_address())

    tdec = ctx_data["token"]["decimals"]
    lo, hi = sorted([_meme_price(p, tdec, r["tick_lower"]), _meme_price(p, tdec, r["tick_upper"])])
    now = _meme_price(p, tdec, r["cur_tick"])

    def mc_supply():
        try:
            return ch.token_supply(ch.get_w3(cid), _meme_addr(p))
        except Exception:
            return 0
    supply = await asyncio.to_thread(mc_supply)

    lines = [f"✅ <b>{esc(tsym)} #{r['token_id']}</b> [v{ver}] · {STRAT_LABEL[mode]}"]
    for label, h in r["steps"]:
        lines.append(f"{label}: {ch.tx_link(cid, h)}")
    if supply:
        qu = p["quote_usd"]
        lines.insert(1, (f"Range: MC {ch.fmt_usd(lo * qu * supply)}–{ch.fmt_usd(hi * qu * supply)} "
                         f"(now {ch.fmt_usd(now * qu * supply)})"))
    else:
        lines.insert(1, f"Range: {ch.fmt_price(lo)}–{ch.fmt_price(hi)} (now {ch.fmt_price(now)})")
    # Sebut yang NYATA masuk posisi, bukan budget. "Deposited ~246,093 USDG
    # ($235,71)" menaruh rencana dan realisasi di satu baris, dan user membaca
    # selisihnya sebagai kerugian — padahal sebagian besar cuma budget yang tidak
    # terpakai dan masih ada di wallet.
    if r.get("in_quote") is not None and r.get("in_meme") is not None:
        sisi = f"{ch.fmt_amount(r['in_quote'])} {esc(r.get('quote_sym') or '')}"
        if r["in_meme"] > 0:
            sisi += f" + {ch.fmt_amount(r['in_meme'])} {esc(r.get('meme_sym') or '')}"
        lines.insert(2, f"Masuk posisi: {sisi} ({ch.fmt_usd(r['deposited_usd'])})")
        sisa = (r.get("deposited") or 0) - (r["in_quote"] or 0)
        if r.get("deposit_sym") == r.get("quote_sym") and sisa > (r.get("deposited") or 0) * 0.01:
            lines.insert(3, (f"<i>· dari budget {ch.fmt_amount(r['deposited'])} "
                             f"{esc(r['deposit_sym'])} — sisanya dipakai beli "
                             f"{esc(r.get('meme_sym') or 'meme')} / tetap di wallet</i>"))
    else:
        lines.insert(2, (f"Deposited ~{ch.fmt_amount(r['deposited'])} {esc(r['deposit_sym'])} "
                         f"({ch.fmt_usd(r['deposited_usd'])})"))
    if r["token_id"]:
        lines.append(ch.pos_link_any(cid, pid))
    g = await asyncio.to_thread(gas_line, cid)
    if g:
        lines.append(g)
    sesudah, kb = await after_action(cid, pid, "Nilai posisi")
    await edit(status, "\n".join(lines + sesudah), kb)


# ---------- /list ----------
_LIST_BUDGET = 5      # detik maks untuk SELURUH pemindaian lintas chain
_SCAN_TASKS: dict = {}   # chain -> task pindai yang sedang jalan (single-flight)


async def _chain_positions(cid: int) -> tuple[int, list, list]:
    """(cid, posisi, error) satu chain — dipakai pemindaian lintas chain."""
    errs: list = []
    try:
        pos = await asyncio.to_thread(list_positions_all, cid, None, errs)
    except Exception as e:
        return cid, [], [f"{ch.CHAINS[cid]['name']}: {type(e).__name__}"]
    return cid, pos, errs


async def _scan_chains(cids: list[int]) -> list[tuple[int, list, list]]:
    """Pindai beberapa chain paralel dengan SATU anggaran waktu total.

    Anggarannya TOTAL, bukan per-chain: batas per-chain masih bisa menumpuk kalau
    beberapa chain sama-sama lambat, dan yang dirasakan user cuma "daftar lama
    muncul". Terukur 4 chain dingin **13,9 detik** — hampir seluruhnya menunggu
    HyperEVM yang jatuh ke RPC publik.

    Chain yang belum selesai TIDAK dibatalkan: `asyncio.shield` membiarkan
    task-nya jalan terus dan mengisi `_POS_CACHE`, jadi klik berikutnya sudah
    lengkap tanpa menunggu lagi. Membatalkannya justru membuat daftar tidak pernah
    lengkap — tiap klik memulai pembacaan dari nol lalu dibatalkan lagi."""
    # Single-flight per chain: tanpa ini tiap klik /list memulai pembacaan BARU
    # untuk chain yang pembacaannya masih jalan — dua kali ongkos RPC, dan
    # daftarnya tetap tidak pernah lengkap karena yang baru sama lambatnya.
    tasks = {}
    for c in cids:
        t = _SCAN_TASKS.get(c)
        if t is None or t.done():
            t = asyncio.ensure_future(_chain_positions(c))
            _SCAN_TASKS[c] = t
        tasks[c] = t
    try:
        await asyncio.wait(tasks.values(), timeout=_LIST_BUDGET)
    except Exception:
        pass
    out = []
    for c, t in tasks.items():
        if t.done() and not t.cancelled():
            try:
                out.append(t.result())
            except Exception as e:
                out.append((c, [], [f"{ch.CHAINS[c]['name']}: {type(e).__name__}"]))
            continue
        # Belum selesai: JANGAN dibatalkan — dibiarkan mengisi `_POS_CACHE` supaya
        # klik berikutnya langsung lengkap. Membatalkannya membuat daftar tidak
        # pernah lengkap: tiap klik memulai dari nol lalu dibatalkan lagi.
        out.append((c, [], [f"{ch.CHAINS[c]['name']}: masih dimuat"]))
    return out


async def cmd_list(update: Update, _, status_msg=None):
    if not authorized(update):
        return
    s = store.load_settings()
    cid = s["chain"]
    # Semua chain sekaligus: user tidak perlu ganti chain hanya untuk melihat
    # posisinya. Aksi dana TETAP per-chain, jadi mengklik posisi chain lain
    # memindahkan chain aktif dulu (`posc|`) — dengan begitu seluruh alur di
    # belakangnya (add/close/rebalance/order) tetap menunjuk chain yang benar
    # tanpa perlu mengoper chain_id ke belasan tempat.
    cids = list(ch.CHAINS) if s.get("list_all_chains", True) else [cid]
    judul = "semua chain" if len(cids) > 1 else ch.CHAINS[cid]["name"]
    if status_msg is None:
        status = await reply(update, f"⏳ Loading positions · {esc(judul)}…")
    else:
        # refresh: pakai pesan /list yang sudah ada, jangan kirim baru
        status = status_msg
        await edit(status, f"⏳ Refreshing positions · {esc(judul)}…")
    hasil = await _scan_chains(cids)
    per_chain = {c: (pos, errs) for c, pos, errs in hasil}
    read_errors = [e for _, _, errs in hasil for e in errs]
    positions = [p for _, pos, _ in hasil for p in pos]
    if not positions and all(errs for _, _, errs in hasil) and read_errors:
        await edit(status, "❌ Gagal load posisi: " + esc(" | ".join(read_errors[:3])))
        return

    open_value = unclaimed = deposits = 0.0
    withdrawals = fees_claimed = 0.0
    churn = 0
    for c, (pos, _errs) in per_chain.items():
        # klaim event riwayat lama (tanpa tag wallet) yang posisinya milik wallet ini
        store.adopt_orphans(c, wallet_address(), [p["token_id"] for p in pos])
        sm = store.portfolio_summary(c, wallet_address())
        deposits += sm["deposits"]
        withdrawals += sm["withdrawals"]
        fees_claimed += sm["fees_claimed"]
        churn += store.churn_count(c, wallet_address())
        open_value += sum(p["value_usd"] for p in pos)
        unclaimed += sum(p["unclaimed_usd"] for p in pos)
    summary = {"deposits": deposits, "withdrawals": withdrawals, "fees_claimed": fees_claimed}
    pnl = summary["withdrawals"] + summary["fees_claimed"] + open_value + unclaimed - deposits
    # Persennya HARUS terhadap modal bersih (deposits − withdrawals), bukan deposits
    # kumulatif. Tiap rebalance/pindah pool/compound mencatat close + mint baru,
    # jadi deposits menggelembung oleh dana yang sama didaur ulang berkali-kali dan
    # persentasenya jadi terlihat jauh lebih kecil dari yang benar-benar dirasakan
    # (terukur: −3,19% terhadap deposit kumulatif $67,4k vs −26,48% terhadap modal
    # bersih $8,1k, dari 541 siklus).
    net_in = max(0.0, deposits - summary["withdrawals"])
    base = net_in or deposits
    pnl_pct = (pnl / base * 100) if base else 0.0

    lines = []
    if len(all_pks()) > 1:
        waddr = wallet_address()
        lines.append(f"👛 {esc(wallet_label())} <code>{esc(waddr[:6])}…{esc(waddr[-4:])}</code>")
    lines += [
        f"<b>Portfolio PnL {ch.fmt_usd(pnl)} ({pnl_pct:+.2f}% dari modal bersih "
        f"{ch.fmt_usd(net_in)})</b>",
        (f"deposits {ch.fmt_usd(deposits)} | withdrawals {ch.fmt_usd(summary['withdrawals'])} | "
         f"fees claimed {ch.fmt_usd(summary['fees_claimed'])}"),
        (f"<i>deposits/withdrawals termasuk {churn} siklus rebalance — dana yang sama "
         f"didaur ulang, bukan modal segar.</i>" if churn else ""),
        f"open value {ch.fmt_usd(open_value)} | unclaimed fees {ch.fmt_usd(unclaimed)}",
        "",
    ]
    buttons = []
    # Posisi yang GAGAL dibaca wajib disebut. Kalau tidak, RPC sibuk terlihat sama
    # persis dengan dana yang hilang — dan nilai portfolio di atas ikut kelihatan
    # menyusut padahal posisinya utuh on-chain.
    if read_errors:
        pending = [e for e in read_errors if e.endswith("masih dimuat")]
        gagal = [e for e in read_errors if not e.endswith("masih dimuat")]
        if gagal:
            lines.append(f"⚠️ {len(gagal)} posisi GAGAL dibaca (RPC sibuk) — "
                         f"belum tentu tertutup. Klik Refresh.")
        if pending:
            lines.append("⏳ " + esc(", ".join(e.split(":")[0] for e in pending))
                         + " masih dimuat di latar — klik Refresh sebentar lagi.")
        lines.append("")
    if not positions:
        lines.append("Tidak ada posisi aktif." if not read_errors
                     else "Tidak ada posisi yang berhasil dibaca.")
    else:
        lines.append("Klik posisi untuk detail + aksi:")
    banyak = len([c for c in per_chain if per_chain[c][0]]) > 1
    for c in cids:
        pos = per_chain.get(c, ([], []))[0]
        if not pos:
            continue
        if banyak:
            nilai = sum(p["value_usd"] + p["unclaimed_usd"] for p in pos)
            buttons.append([InlineKeyboardButton(
                f"— {ch.CHAINS[c]['name']} · {ch.fmt_usd(nilai)} —"
                + ("" if c == cid else " ↗"), callback_data="noop")])
        for p in pos:
            m = _pos_metrics(c, p)
            mark = "🟢" if p["in_range"] else "🔴"
            label = f"{mark} {m['meme_sym']} {_pos_disp(p)} · {ch.fmt_usd(m['cur_total'])}"
            if m["pnl_pct"] is not None:
                label += f" · {m['pnl_pct']:+.0f}%"
            label += f" · {m['age']}"
            # posc| memindahkan chain aktif dulu; pos| tetap ada untuk chain aktif
            cb = f"pos|{p['pid']}" if c == cid else f"posc|{c}|{p['pid']}"
            buttons.append([InlineKeyboardButton(label, callback_data=cb)])
    # Posisi tanpa event mint (mis. hasil /recover, atau mint yang sempat dilaporkan
    # gagal) menambah open_value TANPA deposit pembanding — PnL jadi terlalu bagus.
    # Sebut jumlahnya, jangan diam-diam.
    tanpa_deposit = [p for c, (pos, _e) in per_chain.items() for p in pos
                     if store.mint_usd(c, p["token_id"]) is None]
    if tanpa_deposit:
        nilai = sum(p["value_usd"] for p in tanpa_deposit)
        lines.insert(len(lines) - 1,
                     f"<i>⚠️ {len(tanpa_deposit)} posisi ({ch.fmt_usd(nilai)}) tidak punya "
                     f"catatan deposit — PnL di atas terlalu bagus sebesar itu.</i>")
    buttons.insert(0, [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])
    # Claim mengirim tx, jadi cakupannya HANYA chain aktif — labelnya wajib
    # menyebut chainnya, kalau tidak angka portfolio lintas-chain di atas bikin
    # user mengira semua chain ikut terklaim.
    unc_aktif = sum(p["unclaimed_usd"] for p in per_chain.get(cid, ([], []))[0])
    if unc_aktif > 0:
        buttons.insert(1, [InlineKeyboardButton(
            f"💰 Claim fee {ch.CHAINS[cid]['name']} ({ch.fmt_usd(unc_aktif)})",
            callback_data="claimall")])
    buttons.append(BACK_ROW)
    await edit(status, "\n".join(lines), InlineKeyboardMarkup(buttons))


def _pos_disp(p: dict) -> str:
    """Label pendek posisi: '#183469' (v3) · '#12 [v4]' · '[v2]'."""
    ver = p.get("ver", 3)
    if ver == 2:
        return "[v2]"
    if ver == 4:
        return f"#{p['v4_tid']} [v4]"
    return f"#{p['token_id']}"


def v2_earned_usd(cid: int, p: dict, wallet: str = "") -> float:
    """Fee yang sudah mengendap ke dalam posisi v2 — tidak pernah muncul sebagai
    'unclaimed' karena langsung jadi bagian reserve. Dihitung dari pertumbuhan
    √k per LP sejak masuk (kebal pergerakan harga, hanya naik oleh fee).

    Posisi lama yang belum punya patokan diinisialisasi saat PERTAMA terlihat, jadi
    fee-nya terhitung sejak saat itu — bukan sejak mint (k saat mint tidak bisa
    dibaca lagi: node publik memangkas state lama)."""
    if p.get("ver") != 2 or not p.get("k_per_lp") or not p.get("value_usd"):
        return 0.0
    w = wallet or wallet_address()
    basis = store.v2_basis(cid, w, p["pool"])
    if not basis:
        store.set_v2_basis(cid, w, p["pool"], p["k_per_lp"])
        return 0.0
    return p["value_usd"] * (1 - basis / p["k_per_lp"]) if p["k_per_lp"] > basis else 0.0


def _pos_metrics(cid: int, p: dict) -> dict:
    """Angka turunan posisi untuk label ringkasan + kartu detail."""
    tid = p["token_id"]
    dep = store.mint_usd(cid, tid)
    claimed = store.fees_claimed_usd(cid, tid)
    withdrawn = store.withdrawn_usd(cid, tid)  # hasil reduce yang sudah masuk wallet
    cur_total = p["value_usd"] + p["unclaimed_usd"]
    mts = store.mint_ts(cid, tid)
    pnl = pnl_pct = apr = None
    # Fee v2 tidak pernah muncul di unclaimed_usd (mengendap ke dalam posisi), jadi
    # dihitung dari pertumbuhan √k per LP sejak masuk. Tanpa ini APR posisi v2
    # dijamin selalu 0% — rumus di bawah berbentuk v3.
    earned = p["unclaimed_usd"] + claimed
    if p.get("ver") == 2:
        earned = v2_earned_usd(cid, p) + claimed
    if dep:
        pnl = cur_total + claimed + withdrawn - dep
        pnl_pct = pnl / dep * 100
        if mts:
            age_days = max((int(time.time()) - mts) / 86400, 0.01)
            apr = earned / dep / age_days * 365 * 100
    return {
        "meme_sym": p["sym0"] if p["quote_is_token1"] else p["sym1"],
        "dep": dep, "claimed": claimed, "withdrawn": withdrawn, "cur_total": cur_total,
        "pnl": pnl, "pnl_pct": pnl_pct, "apr": apr, "earned": earned,
        "age": store.fmt_age(mts),
    }


def add_confirm_text(cid: int, pid: str, val: float, is_pct: bool) -> str:
    """Kartu konfirmasi ADD yang benar-benar memberi tahu apa yang akan terjadi.

    Dipanggil di thread — semua isinya baca on-chain. Sebelumnya kartu ini cuma
    menulis "Konfirmasi tambah dana ke posisi X", jadi user menyetujui tanpa tahu
    pool mana, berapa yang benar-benar masuk, dan komposisinya jadi apa."""
    s = store.load_settings()
    p = position_one(cid, pid)
    if not p:
        return f"Konfirmasi tambah dana ke posisi {disp_pid(pid)}:"
    ver = p.get("ver", 3)
    w3 = ch.get_w3(cid)
    quote = p["token1"] if p["quote_is_token1"] else p["token0"]
    qsym = p.get("quote_sym") or "quote"
    try:
        qdec = ch._v4_currency_info(w3, cid, quote)["decimals"] if ver == 4 \
            else ch.token_info(w3, quote)["decimals"]
        qusd = ch.quote_usd_price(w3, cid, qsym)
    except Exception:
        qdec, qusd = 18, 0.0

    # jumlah yang benar-benar akan dipakai: persen dihitung dari modal yang bisa diambil
    amt = val
    if is_pct:
        try:
            # compute_amount butuh bentuk pool_info discovery (quote_addr/quote_decimals),
            # sedangkan dict posisi memakai token0/token1 + quote_is_token1
            amt = compute_amount({"chain": cid, "mode": "lower",
                                  "amount_pct": val, "amount_fixed": 0,
                                  "pool_info": {"quote_addr": quote, "quote_sym": qsym,
                                                "quote_decimals": qdec, "ver": ver,
                                                "pool": p.get("pool")}})
        except Exception:
            amt = None
    lines = [f"➕ <b>Konfirmasi tambah dana</b> — {esc(p.get('sym0') if p['quote_is_token1'] else p.get('sym1'))} "
             f"{_pos_disp(p)}", "", _pool_info_line(cid, p, ver)]
    if ver != 2:
        lines.append(f"📊 Range: {esc(range_str(p))} · "
                     f"{'🟢 IN range' if p['in_range'] else '🔴 OUT of range'}")
    lines.append(f"💼 Posisi sekarang <b>{ch.fmt_usd(p['value_usd'])}</b>")
    lines.append("")
    if amt is None:
        lines.append(f"Akan ditambah: <b>{val:g}% saldo</b> <i>(jumlah dihitung saat eksekusi)</i>")
    else:
        lines.append(f"Akan ditambah: <b>{ch.fmt_amount(amt)} {esc(qsym)}</b>"
                     + (f" (~{ch.fmt_usd(amt * qusd)})" if qusd else ""))
    # komposisi: berapa yang ditahan sebagai quote vs ditukar jadi meme
    try:
        if ver != 2 and amt:
            sqrtp = (ch.v4_slot0(w3, cid, p["pool_id"])[0] if ver == 4
                     else w3.eth.contract(address=ch.Web3.to_checksum_address(p["pool"]),
                                          abi=ch.POOL_ABI).functions.slot0().call()[0])
            keep, swap = ch.plan_two_sided(sqrtp, p["tick_lower"], p["tick_upper"],
                                           int(amt * 10 ** qdec), p["quote_is_token1"])
            tot = keep + swap
            if tot > 0:
                msym = p["sym0"] if p["quote_is_token1"] else p["sym1"]
                lines.append(f"Komposisi otomatis: ~{keep / tot * 100:.0f}% {esc(qsym)} + "
                             f"~{swap / tot * 100:.0f}% {esc(msym)} "
                             f"<i>(meme dibeli otomatis; yang sudah di wallet dipakai duluan)</i>")
    except Exception:
        pass
    if amt and qusd:
        lines.append(f"Perkiraan sesudahnya: <b>{ch.fmt_usd(p['value_usd'] + amt * qusd)}</b>")
    if ver == 4 and p.get("unclaimed_usd"):
        # v4 mengkreditkan feesAccrued ke tagihan increase — lihat CLAUDE.md.
        # Dengan CLOSE_CURRENCY per sisi, fee yang TIDAK terpakai (lazim kalau
        # komposisinya ~100% satu sisi) mendarat di wallet, bukan tetap unclaimed.
        lines.append(f"♻️ Fee unclaimed {ch.fmt_usd(p['unclaimed_usd'])} ikut jadi modal "
                     f"(sisa yang tidak terpakai masuk wallet)")
    lines.append("")
    lines.append(f"<i>Slippage {s['slippage_pct']:g}% · deadline 20 menit</i>")
    return "\n".join(lines)


def _pool_info_line(cid: int, p: dict, ver: int) -> str:
    """Baris keterangan POOL di kartu posisi: fee tier, TVL, volume, porsi kita.

    Sengaja cuma di kartu detail (satu posisi), tidak di //list — pool_stats
    memanggil StateView + dexscreener, jadi biayanya per-posisi."""
    try:
        s = ch.pool_stats(ch.get_w3(cid), cid, p)
    except Exception:
        s = {}
    dex = s.get("dex") or p.get("dex") or ""
    fee_pct = s.get("fee_pct")
    bits = [f"v{ver}" + (f" {esc(dex)}" if dex else "")]
    if fee_pct is not None:
        bits.append(f"fee {fee_pct:g}%")
    tvl, vol = s.get("tvl_usd"), s.get("vol24_usd")
    if tvl:
        src = " <i>(perkiraan)</i>" if s.get("tvl_src") == "chain" and ver == 4 else ""
        bits.append(f"TVL ${fmt_short(tvl)}{src}")
    if vol:
        bits.append(f"vol 24j ${fmt_short(vol)}")
    if tvl and vol:
        bits.append(f"V/TVL {fmt_ratio(vol, tvl)}")
    line = "🏊 " + " · ".join(bits)
    extra = []
    if tvl and p.get("value_usd"):
        # Porsi kita di pool: penentu seberapa besar dampak masuk/keluar kita sendiri
        extra.append(f"porsi kita {p['value_usd'] / tvl * 100:.1f}%")
    if ver != 2:
        extra.append(f"kisi {box_pct(p):.2f}%")
    if extra:
        line += "\n<i>" + " · ".join(extra) + "</i>"
    return line


def position_card(cid: int, p: dict) -> str:
    """Kartu detail satu posisi (ala BasedBot)."""
    m = _pos_metrics(cid, p)
    ver = p.get("ver", 3)
    in_out = "🟢 IN range" if p["in_range"] else "🔴 OUT of range"
    meme_ca = p["token0"] if p["quote_is_token1"] else p["token1"]
    pct0 = p["usd0"] / p["value_usd"] * 100 if p["value_usd"] else 0
    if m["pnl"] is not None:
        pnl_line = (f"{'🟩 Untung' if m['pnl'] >= 0 else '🟥 Rugi'}: "
                    f"{'+' if m['pnl'] >= 0 else '−'}${abs(m['pnl']):.2f} ({m['pnl_pct']:+.1f}%)")
    else:
        pnl_line = "PnL: ? (mint di luar bot)"
    range_line = ("📊 Full range (v2, selalu aktif)" if ver == 2
                  else f"📊 Range: {esc(range_str(p))}")
    pool_line = _pool_info_line(cid, p, ver)
    fee_line = ((f"💰 Fee terkumpul ~{ch.fmt_usd(m['earned'])} "
                 f"<i>(fee {p.get('fee', 3000) / 10000:g}% auto-compound — sudah termasuk "
                 f"di nilai posisi, tak perlu diklaim)</i>"
                 if m.get("earned") else
                 f"💰 Fee {p.get('fee', 3000) / 10000:g}% auto-compound ke posisi (v2)") if ver == 2 else
                # liq==0 + tokensOwed>0: decrease sudah jalan, collect belum. Angka itu
                # POKOK + fee, bukan fee saja — menyebutnya "fee" bikin user mengira
                # modalnya hilang karena "Nilai" di atasnya $0,00.
                (f"📦 <b>Menunggu diklaim {ch.fmt_usd(p['unclaimed_usd'])}</b> "
                 f"<i>(pokok + fee — posisi sudah ditarik, tinggal Fee/Close untuk "
                 f"memindahkannya ke wallet)</i>\n"
                 if p.get("pending_claim") else
                 f"💰 <b>Fee unclaimed {ch.fmt_usd(p['unclaimed_usd'])}</b>\n") +
                f"· {ch.fmt_amount(p['fees0'])} {esc(p['sym0'])} ({ch.fmt_usd(p['fees_usd0'])}) + "
                f"{ch.fmt_amount(p['fees1'])} {esc(p['sym1'])} ({ch.fmt_usd(p['fees_usd1'])})")
    L = [
        f"<b>{esc(m['meme_sym'])} {_pos_disp(p)}</b> · {in_out} · Age {m['age']}",
        f"CA: <code>{esc(meme_ca)}</code>",
        "",
        pool_line,
        range_line,
        f"💼 <b>Nilai {ch.fmt_usd(p['value_usd'])}</b>",
        f"· {ch.fmt_amount(p['amount0'])} {esc(p['sym0'])} ({ch.fmt_usd(p['usd0'])} · {pct0:.0f}%)",
        f"· {ch.fmt_amount(p['amount1'])} {esc(p['sym1'])} ({ch.fmt_usd(p['usd1'])} · {100 - pct0:.0f}%)",
        fee_line,
        "",
        pnl_line,
    ]
    stat = []
    if m["dep"]:
        stat.append(f"Deposit {ch.fmt_usd(m['dep'])}")
    if m["withdrawn"]:
        stat.append(f"Ditarik {ch.fmt_usd(m['withdrawn'])}")
    if m["claimed"]:
        stat.append(f"Fee terklaim {ch.fmt_usd(m['claimed'])}")
    if m["apr"] is not None:
        stat.append(f"APR ~{m['apr']:,.0f}%")
    if stat:
        L.append(" · ".join(stat))
    L.append(ch.pos_link_any(cid, p["pid"]))
    return "\n".join(L)


def position_kb(cid: int, p: dict) -> InlineKeyboardMarkup:
    pid = p["pid"]
    ver = p.get("ver", 3)
    meme_ca = p["token0"] if p["quote_is_token1"] else p["token1"]
    actions = [InlineKeyboardButton("➕ Add", callback_data=f"add|{pid}"),
               InlineKeyboardButton("➖ Reduce", callback_data=f"red|{pid}")]
    if ver != 2:  # fee v2 auto-compound — tidak ada klaim terpisah
        actions.append(InlineKeyboardButton("💰 Fee", callback_data=f"fee|{pid}"))
        actions.append(InlineKeyboardButton("♻️ Compound", callback_data=f"cmp|{pid}"))
    actions.append(InlineKeyboardButton("🗑 Close", callback_data=f"close|{pid}"))
    rows = [chart_buttons(cid, p["pool"], meme_ca) + [InlineKeyboardButton("🔄", callback_data=f"pos|{pid}")],
            actions]
    if ver != 2:
        rows.append([InlineKeyboardButton("🎯 TP/SL (auto-close di market cap)",
                                          callback_data=f"tpsl|{pid}")])
        rows.append([InlineKeyboardButton("⚖️ Rebalance (mint ulang di harga sekarang)",
                                          callback_data=f"reb|{pid}")])
        rows.append([InlineKeyboardButton("🔀 Pindah pool (fee tier lain)",
                                          callback_data=f"mig|{pid}")])
    rows.append([InlineKeyboardButton("⬅️ Posisi", callback_data="menu|list"),
                 InlineKeyboardButton("🏠 Menu", callback_data="menu|main")])
    return InlineKeyboardMarkup(rows)


async def show_position(update: Update, msg, pid: str):
    s = store.load_settings()
    cid = s["chain"]
    await edit(msg, f"⏳ Memuat posisi {disp_pid(pid)}...")

    def work():
        return position_one(cid, pid)

    try:
        p = await asyncio.to_thread(work)
    except Exception as e:
        await edit(msg, f"❌ Gagal load posisi: {esc(e)}", InlineKeyboardMarkup([BACK_ROW]))
        return
    if not p:
        await edit(msg, f"❌ Posisi {disp_pid(pid)} tidak ditemukan (sudah ditutup?).",
                   InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Posisi", callback_data="menu|list")], BACK_ROW]))
        return
    # position_card → _pool_info_line → pool_stats (StateView + dexscreener), jadi
    # dirakit di thread. Di event loop ia menahan semua klik lain selama RPC-nya.
    card = await asyncio.to_thread(position_card, cid, p)
    await edit(msg, card, position_kb(cid, p))


# ---------- Chart (link eksternal) ----------
def chart_buttons(cid: int, pool: str, meme_ca: str) -> list[InlineKeyboardButton]:
    cfg = ch.CHAINS[cid]
    out = []
    # Slug opsional per chain — Arc tidak punya `gmgn`. Tombol yang slug-nya tidak
    # ada dilewati, bukan dibuat dengan URL rusak.
    if cfg.get("gmgn"):
        out.append(InlineKeyboardButton("📈 GMGN", url=f"https://gmgn.ai/{cfg['gmgn']}/token/{meme_ca}"))
    if cfg.get("dexscreener") and pool:
        out.append(InlineKeyboardButton(
            "📊 DexScreener", url=f"https://dexscreener.com/{cfg['dexscreener']}/{pool}"))
    return out


# ---------- Add / Reduce flow ----------
async def ask_add(update: Update, pid: str):
    if str(pid).startswith("v2:"):
        await reply(update, ("➕ Add posisi v2: paste alamat token lagi lalu pilih pool "
                             "<b>[v2]</b> yang sama — deposit baru otomatis menambah LP existing."))
        return
    s = store.load_settings()
    cid = s["chain"]

    def info():
        """Quote posisi + modal yang benar-benar tersedia — bukan tebakan 'umumnya WETH'."""
        pos = position_one(cid, pid)
        if not pos:
            return None
        w3 = ch.get_w3(cid)
        cfg = ch.CHAINS[cid]
        quote = pos["token1"] if pos["quote_is_token1"] else pos["token0"]
        qsym = pos.get("quote_sym") or "?"
        addr = wallet_address()
        gas_reserve = ch.gas_reserve_wei(cid, w3)
        if str(quote).lower() == ch.V4_NATIVE:
            qdec, bal = 18, max(0, w3.eth.get_balance(addr) - gas_reserve)
            try:
                bal += ch.erc20(w3, cfg["wrapped"]).functions.balanceOf(addr).call()
            except Exception:
                pass
        else:
            qc = ch.erc20(w3, quote)
            qdec = qc.functions.decimals().call()
            bal = qc.functions.balanceOf(addr).call()
            if str(quote).lower() == cfg["wrapped"].lower():
                bal += max(0, w3.eth.get_balance(addr) - gas_reserve)
        bal += ch.other_quote_capital(w3, cid, addr, quote)
        qusd = ch.quote_usd_price(w3, cid, qsym) if qsym in cfg["quotes"] or qsym in (
            cfg["wrapped_symbol"], cfg["native_symbol"]) else ch.token_usd_price(w3, cid, quote)
        return pos, qsym, bal / 10 ** qdec, qusd

    try:
        pos, qsym, avail, qusd = await asyncio.to_thread(info)
        head = (f"Posisi sekarang: <b>{ch.fmt_usd(pos['value_usd'])}</b>"
                f"{' · 🟢 IN range' if pos['in_range'] else ' · 🔴 OUT of range'}\n"
                f"Modal tersedia: <b>{ch.fmt_amount(avail)} {esc(qsym)}</b> "
                f"({ch.fmt_usd(avail * qusd)}) — termasuk saldo quote lain yang bisa ditukar\n"
                f"Contoh: <code>30%</code> = {ch.fmt_amount(avail * 0.3)} {esc(qsym)} "
                f"({ch.fmt_usd(avail * 0.3 * qusd)})\n\n")
    except Exception:
        head, qsym = "", "quote posisi"
    await update.effective_chat.send_message(
        (f"➕ <b>Balas pesan ini</b> dengan jumlah dana untuk ditambah ke {disp_pid(pid)}:\n\n"
         f"{head}"
         f"· nilai pasti: <code>0.005</code> (satuan {esc(qsym)})\n"
         f"· persen saldo: <code>30%</code>\n\n"
         f"<i>Komposisi quote/meme dihitung otomatis mengikuti range posisi; "
         f"meme existing di wallet dipakai duluan.</i>"),
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder="contoh: 0.005 atau 30%"))
    AWAITING[update.effective_chat.id] = {"kind": "addamt", "key": str(pid)}


def _reinvested_fee_usd(cid: int, pid: str) -> float:
    """Fee unclaimed yang akan IKUT TERPAKAI sebagai modal saat add — v4 saja.

    v4 `INCREASE_LIQUIDITY` mengkreditkan feesAccrued terhadap tagihan `SETTLE_PAIR`,
    jadi wallet cuma membayar selisihnya tapi likuiditas bertambah sebesar penuh
    (terbukti di tx: dilaporkan 412,523 USDG, keluar dari wallet 398,769 — selisihnya
    persis fee unclaimed). `added_usd` menghitung yang penuh, jadi tanpa event `fees`
    penyeimbang, fee itu tercatat sebagai setoran baru dan PnL kelihatan rugi
    sebesar fee tersebut.

    v3 tidak kena: `increaseLiquidity` membiarkan fee mengendap di `tokensOwed`
    (tetap unclaimed). v2 tidak punya fee unclaimed sama sekali.
    Gagal baca = 0 (lebih baik tidak mencatat daripada menebak angka).
    """
    if ch.parse_pid(str(pid))[0] != 4:
        return 0.0
    try:
        pos = position_one(cid, pid)
        return max(0.0, float(pos["unclaimed_usd"])) if pos else 0.0
    except Exception:
        return 0.0


# Satu posisi = satu aksi pada satu waktu.
#
# `concurrent_updates` membuat dua klik diproses PARALEL. TX_LOCK menyerialkan
# transaksinya, tapi kedua alur sudah membaca posisi SEBELUM lock — jadi keduanya
# memakai snapshot yang sama dan menghitung jumlah dari angka yang sudah basi.
# Terbukti di v4:1300787: "Reduce 50%" diklik dua kali, tiap alur menghapus
# 3.760.957.351.020.571 likuiditas (setengah dari NILAI AWAL), sehingga yang kedua
# menghabiskan seluruh sisa dan posisi tinggal liquidity=1. Dananya utuh
# (2 x 49,99 USDG kembali), tapi user melihat "posisi jadi $0".
_BUSY_PIDS: set = set()
_BUSY_LOCK = asyncio.Lock()


@asynccontextmanager
async def position_busy(update: Update, pid) -> "AsyncIterator[bool]":
    """Klaim posisi untuk satu aksi. Yield False kalau sedang dipakai alur lain."""
    key = str(pid)
    async with _BUSY_LOCK:
        taken = key in _BUSY_PIDS
        if not taken:
            _BUSY_PIDS.add(key)
    if taken:
        await reply(update, f"⏳ Aksi untuk {disp_pid(pid)} masih berjalan — "
                            f"tunggu sampai selesai, jangan klik dua kali.")
        yield False
        return
    try:
        yield True
    finally:
        # Aksi apa pun di sini (add/reduce/collect/rebalance/close/compound) mengubah
        # posisi, jadi cache daftar WAJIB dibuang — menampilkan posisi yang sudah
        # ditutup jauh lebih buruk daripada menunggu satu pembacaan.
        pos_cache_drop()
        async with _BUSY_LOCK:
            _BUSY_PIDS.discard(key)


async def do_add_exec(update: Update, pid: str, val: float, is_pct: bool):
    async with position_busy(update, pid) as _ok:
        if not _ok:
            return
        s = store.load_settings()
        cid = s["chain"]
        status = await reply(update, f"⏳ Menambah dana ke {disp_pid(pid)}...")

        def work():
            budget = val
            pre_fee = _reinvested_fee_usd(cid, pid)
            if is_pct:
                w3 = ch.get_w3(cid)
                cfg = ch.CHAINS[cid]
                pos = position_one(cid, pid)
                if not pos:
                    raise RuntimeError("Posisi tidak ditemukan.")
                quote = pos["token1"] if pos["quote_is_token1"] else pos["token0"]
                gas_reserve = ch.gas_reserve_wei(cid, w3)
                if quote.lower() == ch.V4_NATIVE:
                    bal = max(0, w3.eth.get_balance(wallet_address()) - gas_reserve)
                    try:    # WETH 1:1, di-unwrap otomatis saat eksekusi
                        bal += ch.erc20(w3, cfg["wrapped"]).functions.balanceOf(wallet_address()).call()
                    except Exception:
                        pass
                    qdec = 18
                else:
                    qc = ch.erc20(w3, quote)
                    bal = qc.functions.balanceOf(wallet_address()).call()
                    qdec = qc.functions.decimals().call()
                    if quote.lower() == cfg["wrapped"].lower():
                        bal += max(0, w3.eth.get_balance(wallet_address()) - gas_reserve)
                    else:
                        try:
                            wbal = ch.erc20(w3, cfg["wrapped"]).functions.balanceOf(wallet_address()).call()
                            wtotal = wbal + max(0, w3.eth.get_balance(wallet_address()) - gas_reserve)
                            rate = ch.wrapped_per_quote_wei(w3, cid, quote)
                            if wtotal > 0 and rate > 0:
                                bal += int(wtotal / rate * 0.98)
                        except Exception:
                            pass
                budget = (bal * val / 100) / 10 ** qdec
            return ch.add_any(cid, pk(), pid, budget, s["slippage_pct"]), pre_fee

        head = f"⏳ Menambah dana ke {disp_pid(pid)}..."
        async with TX_LOCK:
            try:
                r, pre_fee = await with_progress(status, head, work)
            except Exception as e:
                await edit(status, f"❌ Add gagal: {esc(e)}")
                return
        ev_tid = ch.parse_pid(pid)[1] if str(pid).isdigit() else str(pid)
        store.record_event(cid, "mint", ev_tid, r["added_usd"], "add", wallet=wallet_address())
        if pre_fee > 0:
            store.record_event(cid, "fees", ev_tid, pre_fee, "reinvest saat add",
                               wallet=wallet_address())
        lines = [f"✅ <b>Added {disp_pid(pid)}</b> (~{ch.fmt_usd(r['added_usd'])})"]
        if r.get("quote_in") is not None:
            lines.append(f"Masuk: {ch.fmt_amount(r['quote_in'])} {r['quote_sym']}"
                         f" + {ch.fmt_amount(r['meme_in'])} {r['meme_sym']}"
                         f" <i>(meme dari wallet dipakai duluan)</i>")
        if pre_fee > 0:
            lines.append(f"♻️ Fee unclaimed {ch.fmt_usd(pre_fee)} ikut jadi modal "
                         f"(sisa yang tidak terpakai masuk wallet) — dihitung sebagai fee, "
                         f"bukan setoran baru.")
        for label, h in r["steps"]:
            lines.append(f"{label}: {ch.tx_link(cid, h)}")
        lines.append(ch.pos_link_any(cid, pid))
        g = await asyncio.to_thread(gas_line, cid)
        if g:
            lines.append(g)
        sesudah, kb = await after_action(cid, pid)
        await edit(status, "\n".join(lines + sesudah), kb)


async def ask_reduce(update: Update, pid: str):
    note = ("<i>Token hasil penarikan tetap di wallet (tanpa auto-swap). "
            "Untuk 100% pakai tombol Close.</i>")
    if str(pid).startswith("v2:"):
        note = "<i>Fee v2 sudah auto-compound di dalam nilai LP. Untuk 100% pakai Close.</i>"
    s = store.load_settings()
    cid = s["chain"]

    def snap():
        return position_one(cid, pid)

    head = ""
    try:
        pos = await asyncio.to_thread(snap)
        if pos:
            msym = pos["sym0"] if pos["quote_is_token1"] else pos["sym1"]
            qsym = pos.get("quote_sym") or "?"
            head = (f"Nilai posisi: <b>{ch.fmt_usd(pos['value_usd'])}</b> · "
                    f"fee unclaimed {ch.fmt_usd(pos['unclaimed_usd'])}\n"
                    f"Isi: {ch.fmt_amount(pos['amount0'] if pos['quote_is_token1'] else pos['amount1'])} "
                    f"{esc(msym)} + "
                    f"{ch.fmt_amount(pos['amount1'] if pos['quote_is_token1'] else pos['amount0'])} "
                    f"{esc(qsym)}\n"
                    f"Tiap 10% ≈ {ch.fmt_usd(pos['value_usd'] / 10)}\n\n")
    except Exception:
        head = ""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➖ {pct}%", callback_data=f"redok|{pid}|{pct}")
         for pct in (10, 25, 50, 75)],
        [InlineKeyboardButton("✏️ Custom %…", callback_data=f"askred|{pid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await reply(update, (
        f"➖ <b>Kurangi posisi {disp_pid(pid)}?</b>\n\n{head}"
        f"Pilih persentase yang ditarik. Fee unclaimed ikut terambil.\n{note}"), kb)


async def ask_reduce_custom(update: Update, pid: str):
    await update.effective_chat.send_message(
        (f"✏️ <b>Balas pesan ini</b> dengan persen yang mau ditarik dari {disp_pid(pid)}:\n"
         f"· contoh: <code>15</code> atau <code>15%</code>\n"
         f"· rentang 1–99 — untuk 100% pakai tombol Close"),
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder="contoh: 15"))
    AWAITING[update.effective_chat.id] = {"kind": "reducepct", "key": str(pid)}


async def do_reduce_exec(update: Update, pid: str, pct: int):
    async with position_busy(update, pid) as _ok:
        if not _ok:
            return
        s = store.load_settings()
        cid = s["chain"]

        def snapshot():
            return position_one(cid, pid)

        pos = await asyncio.to_thread(snapshot)
        head = f"⏳ Menarik {pct}% dari {disp_pid(pid)}..."
        status = await reply(update, head)
        async with TX_LOCK:
            try:
                r = await with_progress(status, head,
                                        lambda: ch.reduce_any(cid, pk(), pid, pct, s["slippage_pct"]))
            except Exception as e:
                await edit(status, f"❌ Reduce gagal: {esc(e)}")
                return
        ev_tid = ch.parse_pid(pid)[1] if str(pid).isdigit() else str(pid)
        if pos:
            store.record_event(cid, "close", ev_tid, pos["value_usd"] * pct / 100,
                               f"reduce {pct}%", wallet=wallet_address())
            if pos["unclaimed_usd"] > 0:
                store.record_event(cid, "fees", ev_tid, pos["unclaimed_usd"], wallet=wallet_address())
        lines = [f"✅ <b>Reduced {disp_pid(pid)} −{pct}%</b>",
                 f"Received ~{ch.fmt_amount(r['got0'])} {esc(r['sym0'])} + "
                 f"{ch.fmt_amount(r['got1'])} {esc(r['sym1'])} (termasuk fee)"]
        for label, h in r["steps"]:
            lines.append(f"{label}: {ch.tx_link(cid, h)}")
        lines.append(ch.pos_link_any(cid, pid))
        g = await asyncio.to_thread(gas_line, cid)
        if g:
            lines.append(g)
        sesudah, kb = await after_action(cid, pid)
        await edit(status, "\n".join(lines + sesudah), kb)


# ---------- Collect fee ----------
async def do_collect(update: Update, pid: str):
    async with position_busy(update, pid) as _ok:
        if not _ok:
            return
        s = store.load_settings()
        cid = s["chain"]

        def find_pos():
            return position_one(cid, pid)

        pos = await asyncio.to_thread(find_pos)
        status = await reply(update, f"⏳ Collect fee {disp_pid(pid)}...")
        async with TX_LOCK:
            try:
                r = await asyncio.to_thread(ch.collect_any, cid, pk(), pid)
            except Exception as e:
                await edit(status, f"❌ Collect gagal: {esc(e)}")
                return
        ev_tid = ch.parse_pid(pid)[1] if str(pid).isdigit() else str(pid)
        usd_txt = ""
        if pos and pos["unclaimed_usd"] > 0:
            store.record_event(cid, "fees", ev_tid, pos["unclaimed_usd"], wallet=wallet_address())
            usd_txt = f" (~{ch.fmt_usd(pos['unclaimed_usd'])})"
        lines = [f"✅ <b>Fee terklaim {disp_pid(pid)}</b>{usd_txt}",
                 f"Received {ch.fmt_amount(r['got0'])} {esc(r['sym0'])} + "
                 f"{ch.fmt_amount(r['got1'])} {esc(r['sym1'])}",
                 "<i>Posisi tetap jalan — liquidity tidak berubah.</i>"]
        for label, h in r["steps"]:
            lines.append(f"{label}: {ch.tx_link(cid, h)}")
        g = await asyncio.to_thread(gas_line, cid)
        if g:
            lines.append(g)
        sesudah, kb = await after_action(cid, pid)
        await edit(status, "\n".join(lines + sesudah), kb)


# ---------- Rebalance ----------
async def ask_rebalance(update: Update, pid: str):
    s = store.load_settings()
    cid = s["chain"]
    if str(pid).startswith("v2:"):
        await reply(update, "Posisi v2 full-range — tidak perlu rebalance.")
        return

    def work():
        return position_one(cid, pid)

    p = await asyncio.to_thread(work)
    if not p:
        await reply(update, f"❌ Posisi {disp_pid(pid)} tidak ditemukan.")
        return
    meme_sym = p["sym0"] if p["quote_is_token1"] else p["sym1"]
    status = "🟢 IN" if p["in_range"] else "🔴 OUT"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚖️ Wide — dua sisi", callback_data=f"rebok|{pid}|wide")],
        [InlineKeyboardButton(f"Lower — {p['quote_sym'] or 'quote'} saja (nampung turun)",
                              callback_data=f"rebok|{pid}|lower"),
         InlineKeyboardButton(f"Upper — {meme_sym} saja (jual naik)",
                              callback_data=f"rebok|{pid}|upper")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await reply(update, (
        f"⚖️ <b>Rebalance {_pos_disp(p)}?</b>\n"
        f"{esc(p['sym1'])}/{esc(p['sym0'])} · Val ~{ch.fmt_usd(p['value_usd'])} · {status}\n"
        f"Range: {esc(range_str(p))}\n\n"
        f"Close (fee ikut terambil) → swap komposisi → mint ulang dengan "
        f"<b>lebar range sama</b> dipusatkan di harga sekarang.\n"
        f"<i>Hanya dana hasil posisi ini yang dipakai. 3–5 transaksi.</i>"), kb)


async def do_rebalance(update: Update, pid: str, mode: str):
    async with position_busy(update, pid) as _ok:
        if not _ok:
            return
        s = store.load_settings()
        cid = s["chain"]

        def snapshot():
            return position_one(cid, pid)

        pos = await asyncio.to_thread(snapshot)
        head = f"⏳ Rebalance {disp_pid(pid)} → {mode}... (close → swap → mint)"
        status = await reply(update, head)
        async with TX_LOCK:
            try:
                r = await with_progress(status, head, lambda: ch.rebalance_position(
                    cid, pk(), pid, mode, s["slippage_pct"], int(s.get("gap", 1))))
            except Exception as e:
                if isinstance(e, ch.AlreadyClosed):
                    await edit(status, f"✅ {esc(e)}", NAV_KB)
                    return
                await edit(status, f"❌ Rebalance gagal: {esc(e)}\n"
                                   f"<i>Kalau close sudah jalan, dananya aman di wallet — "
                                   f"cek /wallet lalu mint manual.</i>")
                return

        await finish_rebalance(update, status, cid, pid, pos, r, mode=mode)


async def finish_rebalance(update, status, cid: int, pid: str, pos, r: dict,
                           mode: str | None = None, label: str = "Rebalanced"):
    """Pembukuan + kartu hasil untuk rebalance DAN pindah pool — dua-duanya
    close-lalu-mint, jadi pencatatannya harus persis sama."""
    ver, old_ref = ch.parse_pid(pid)
    ev_old = old_ref if ver == 3 else str(pid)
    # Pembukuan tidak boleh bolong: kalau snapshot posisi lama gagal dibaca (RPC lag),
    # event close tetap dicatat dari nilai hasil close yang sebenarnya. Tanpa ini,
    # deposit posisi lama menggantung sebagai "masih terbuka" sementara posisi baru
    # terhitung modal segar — PnL portfolio menggelembung palsu.
    if pos:
        store.record_event(cid, "close", ev_old, pos["value_usd"], "rebalance out",
                           wallet=wallet_address())
        if pos["unclaimed_usd"] > 0:
            store.record_event(cid, "fees", ev_old, pos["unclaimed_usd"], wallet=wallet_address())
    elif r.get("closed_usd"):
        # closed_usd sudah mencakup principal + fee, jadi TIDAK ditambah event fees
        # terpisah — kalau tidak, fee-nya terhitung dua kali.
        store.record_event(cid, "close", ev_old, r["closed_usd"],
                           "rebalance out (snapshot gagal)", wallet=wallet_address())
    new_pid = f"v4:{r['token_id']}" if ver == 4 else r["token_id"]
    if ver == 4:
        store.drop_ref(cid, wallet_address(), "v4", str(old_ref))
        if r["token_id"]:
            store.add_ref(cid, wallet_address(), "v4", str(r["token_id"]))
    store.record_event(cid, "mint", new_pid, r["deposited_usd"], "rebalance in", wallet=wallet_address())

    lines = [f"✅ <b>{label} {disp_pid(pid)} → #{r['token_id']}</b> [v{ver}]"
             + (f" · {STRAT_LABEL[mode]}" if mode else ""),
             f"Closed: {ch.fmt_amount(r['closed_got0'])} {esc(r['closed_sym0'])} + "
             f"{ch.fmt_amount(r['closed_got1'])} {esc(r['closed_sym1'])} (termasuk fee)",
             f"Minted: ~{ch.fmt_amount(r['deposited'])} {esc(r['deposit_sym'])} "
             f"({ch.fmt_usd(r['deposited_usd'])})"]
    # Range SESUDAH rebalance wajib disebut: seluruh gunanya rebalance adalah
    # memindahkan range, jadi kartu tanpa angka barunya memaksa user membuka
    # kartu posisi hanya untuk tahu apakah hasilnya sesuai harapan.
    for lbl, h in r["steps"]:
        lines.append(f"{lbl}: {ch.tx_link(cid, h)}")
    if r["token_id"]:
        lines.append(ch.pos_link_any(cid, new_pid))
    g = await asyncio.to_thread(gas_line, cid)
    if g:
        lines.append(g)
    sesudah, kb = await after_action(cid, new_pid, "Posisi baru") if r["token_id"] else ([], NAV_KB)
    await edit(status, "\n".join(lines + sesudah), kb)


# ---------- Close flow ----------
async def ask_close(update: Update, pid: str):
    s = store.load_settings()
    cid = s["chain"]

    def work():
        return position_one(cid, pid)

    p = await asyncio.to_thread(work)
    if not p:
        await reply(update, f"❌ Posisi {disp_pid(pid)} tidak ditemukan.")
        return
    ver = p.get("ver", 3)
    status = "🟢 IN" if p["in_range"] else "🔴 OUT"
    wsym = ch.CHAINS[cid]["wrapped_symbol"]
    meme_sym = p["sym0"] if p["quote_is_token1"] else p["sym1"]
    if ver == 4:
        swap_note = (f"<i>Opsi swap menjual hasil {esc(meme_sym)} → quote pool via "
                     f"UniversalRouter v4.</i>")
        detail = "Full exit LP (burn posisi, principal + fee sekaligus)."
    elif ver == 2:
        swap_note = f"<i>Opsi swap menjual {esc(meme_sym)} hasil penarikan via router v2.</i>"
        detail = "Full exit LP (removeLiquidity 100%, fee sudah auto-compound)."
    else:
        # close_position memotret saldo sebelum eksekusi dan hanya menjual SELISIHNYA —
        # saldo lama user tidak disentuh. Teks lama menyatakan sebaliknya.
        swap_note = (f"<i>Opsi swap menjual hasil {esc(meme_sym)} dari posisi ini saja; "
                     f"saldo {esc(meme_sym)} yang sudah ada di wallet tidak disentuh.</i>")
        detail = ("Posisi SUDAH ditarik, tinggal dipindahkan ke wallet (collect)."
                  if p.get("pending_claim") else "Full exit LP (decrease + collect).")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Close + swap {meme_sym} → {wsym if ver != 4 else 'quote'}",
                              callback_data=f"closeok|{pid}|1")],
        [InlineKeyboardButton(f"✅ Close, tahan {meme_sym}", callback_data=f"closeok|{pid}|0")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await reply(update, (
        f"⚠️ <b>Close position?</b>\n\n"
        f"{_pos_disp(p)} {esc(p['sym1'])}/{esc(p['sym0'])}\n"
        # yang keluar ke wallet = nilai posisi DITAMBAH yang belum diklaim. Menampilkan
        # value_usd saja bikin posisi pending-claim tertulis "Val ~$0.00" padahal ada
        # ratusan dolar menunggu (kejadian nyata #757291).
        f"Keluar ke wallet ~{ch.fmt_usd(p['value_usd'] + p['unclaimed_usd'])} · {status}\n"
        f"<i>posisi {ch.fmt_usd(p['value_usd'])} + belum diklaim "
        f"{ch.fmt_usd(p['unclaimed_usd'])}</i>\n\n"
        f"{detail}\n{swap_note}"), kb)


async def do_close(update: Update, pid: str, autoswap: bool):
    async with position_busy(update, pid) as _ok:
        if not _ok:
            return
        s = store.load_settings()
        cid = s["chain"]

        def find_pos():
            return position_one(cid, pid)

        pos = await asyncio.to_thread(find_pos)
        usd = (pos["value_usd"] + pos["unclaimed_usd"]) if pos else 0.0
        ver, ref = ch.parse_pid(pid)
        head = f"⏳ Closing {disp_pid(pid)} (v{ver})..."
        status = await reply(update, head)
        async with TX_LOCK:
            try:
                r = await with_progress(status, head, lambda: ch.close_any(
                    cid, pk(), pid, s["slippage_pct"], autoswap))
            except Exception as e:
                if isinstance(e, ch.AlreadyClosed):
                    # Bukan kegagalan: tx susulan ditolak, tapi close-nya sendiri
                    # sudah berhasil dan dananya sudah di wallet.
                    await edit(status, f"✅ {esc(e)}", NAV_KB)
                    return
                await edit(status, f"❌ Close gagal: {esc(e)}")
                return

        ev_tid = ref if ver == 3 else str(pid)
        if ver == 4:
            store.drop_ref(cid, wallet_address(), "v4", str(ref))
        elif ver == 2:   # dulu tidak pernah dibersihkan — registry & patokan fee jadi basi
            store.drop_ref(cid, wallet_address(), "v2", str(ref))
            store.drop_v2_basis(cid, wallet_address(), str(ref))
        store.record_event(cid, "close", ev_tid, pos["value_usd"] if pos else usd, wallet=wallet_address())
        if pos and pos["unclaimed_usd"] > 0:
            store.record_event(cid, "fees", ev_tid, pos["unclaimed_usd"], wallet=wallet_address())
        lines = [f"✅ <b>Closed {disp_pid(pid)}</b>",
                 f"Received ~{ch.fmt_amount(r['got0'])} {esc(r['sym0'])} + {ch.fmt_amount(r['got1'])} {esc(r['sym1'])}"]
        if pos:
            lines.append(f"💰 Fee terklaim: {ch.fmt_amount(pos['fees0'])} {esc(pos['sym0'])} + "
                         f"{ch.fmt_amount(pos['fees1'])} {esc(pos['sym1'])} (~{ch.fmt_usd(pos['unclaimed_usd'])})")
        lines.append(f"Withdrawal value ~{ch.fmt_usd(usd)}")
        for label, h in r["steps"]:
            lines.append(f"{label}: {ch.tx_link(cid, h)}")
        g = await asyncio.to_thread(gas_line, cid)
        if g:
            lines.append(g)
        # Close: posisinya memang sudah tidak ada, jadi TIDAK ada keadaan "sesudah"
        # untuk dibaca — memanggil after_action() di sini cuma membuang satu
        # pembacaan RPC untuk hasil yang pasti kosong.
        await edit(status, "\n".join(lines), NAV_KB)

        if r["swaps"]:
            lines = ["🔄 Auto-swap hasil close:"]
            # Info per-swap: jumlah yang benar-benar diterima + biayanya. Dulu label
            # tujuannya di-hardcode ke wrapped_symbol padahal close v4 menjual meme
            # ke QUOTE POOL (mis. USDG) — jadi kartunya menyebut token yang salah dan
            # tidak pernah menyebut berapa yang termakan fee + price impact.
            info = {d["sym"]: d for d in (r.get("swap_info") or [])}
            for sym, h in r["swaps"]:
                if not str(h).startswith("0x"):
                    lines.append(f"{esc(sym)}: {esc(h)}")
                    continue
                d = info.get(sym)
                if not d:
                    lines.append(f"swapped {esc(sym)}: {ch.tx_link(cid, h)}")
                    continue
                def _qinfo(q=d["quote"]):
                    if str(q).lower() == ch.V4_NATIVE:
                        return {"symbol": ch.CHAINS[cid]["native_symbol"], "decimals": 18}
                    return ch.token_info(ch.get_w3(cid), ch.Web3.to_checksum_address(q))

                try:
                    qi = await asyncio.to_thread(_qinfo)   # decimals+symbol = 2 RPC
                except Exception:
                    qi = {"symbol": "?", "decimals": 18}
                got = d["got"] / 10 ** qi["decimals"]
                lines.append(f"swapped {esc(sym)} → {ch.fmt_amount(got)} {esc(qi['symbol'])}: "
                             f"{ch.tx_link(cid, h)}")
                lines += swap_cost_lines(d, qi)
            await reply(update, "\n".join(lines), DEL_KB)


# ---------- Callback router ----------
async def on_callback(update: Update, _):
    if not authorized(update):
        return
    t0 = time.monotonic()
    try:
        return await _route_callback(update)
    finally:
        dt = time.monotonic() - t0
        # Klik yang lama dicatat bersama lag event loop saat itu, supaya "lambat"
        # bisa dibedakan: kerja RPC yang memang berat vs event loop yang tertahan
        # panggilan blocking (lag tinggi = ada yang lupa dibungkus to_thread).
        if dt > 3:
            log.warning("callback %s makan %.1fs (lag loop %.1fs)",
                        (update.callback_query.data or "?")[:40], dt, _LOOP_LAG[0])


async def _route_callback(update: Update):
    q = update.callback_query
    # Query callback punya masa berlaku pendek. Kalau sudah lewat, answer() melempar
    # BadRequest "Query is too old" — dan dulu itu membatalkan SELURUH handler
    # sebelum aksinya sempat jalan, lalu on_error mengirim "aksinya kemungkinan sudah
    # jalan" yang justru terbalik. Gagal menghentikan spinner bukan alasan untuk
    # tidak mengerjakan permintaan user.
    try:
        await q.answer()
    except Exception as e:
        log.warning("answer callback gagal (%s) — aksi tetap dijalankan", e)
    data = q.data or ""

    if data == "del":
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_reply_markup(None)  # >48 jam tidak bisa dihapus — copot tombol saja
        return
    if data == "cancel":
        # WAJIB dibersihkan: kalau tidak, status "sedang pindah pool" menempel dan
        # pemilihan pool BERIKUTNYA (mis. setelah paste token lain) diperlakukan
        # sebagai tujuan pindah — muncul "Pool tujuan bukan untuk token yang sama".
        MIGRATE.pop(update.effective_chat.id, None)
        await q.edit_message_reply_markup(None)
        await reply(update, "❌ Cancelled.")
        return
    if data == "claimall":
        await q.edit_message_reply_markup(None)
        await do_claim_all(update)
        return
    if data == "refresh":
        pos_cache_drop()           # tombol ini memang untuk memaksa baca ulang
        await cmd_list(update, None, status_msg=q.message)
        return
    if data == "cleanupok":
        await q.edit_message_reply_markup(None)
        await do_cleanup(update)
        return
    if data == "noop":
        return
    # --- navigasi menu (edit in-place) ---
    if data in ("menu|main", "go|main", "menu|list", "go|list"):
        # kembali ke menu/daftar = keluar dari alur pindah pool
        MIGRATE.pop(update.effective_chat.id, None)
    if data == "menu|main":
        await show_main_menu(update, msg=q.message)
        return
    if data.startswith("wsel|"):
        store.set_global("wallet_idx", int(data.split("|")[1]))
        await show_main_menu(update, msg=q.message)
        return
    if data.startswith("wal|"):
        await cmd_wallet(update, None, status_msg=q.message, page=int(data.split("|")[1]))
        return
    if data == "menu|list":
        await cmd_list(update, None, status_msg=q.message)
        return
    if data == "menu|wallet":
        await cmd_wallet(update, None, status_msg=q.message)
        return
    if data == "menu|wallets" or data == "wal2|back":
        await edit(q.message, wallets_text(), wallets_kb())
        return
    if data.startswith("wal2|"):
        await handle_wallets_cb(update, q, data)
        return
    if data == "menu|settings":
        await edit(q.message, settings_text(), settings_kb())
        return
    if data == "menu|chain":
        await edit(q.message, "⛓ <b>Pilih chain aktif:</b>", chain_kb())
        return
    # --- editor filter scanner ---
    if data.startswith("sf|"):
        key = data.split("|", 1)[1]
        if key == "__list":
            await edit(q.message, scanfilt_text(), scanfilt_kb())
            return
        if key == "__reset":
            scanner_save({"filters": dict(store.DEFAULT_SETTINGS["scanner"]["filters"])})
            await edit(q.message, scanfilt_text(), scanfilt_kb())
            return
        if not gmgn.filter_spec(key):
            return
        await edit(q.message, scanfilt_one_text(key), scanfilt_one_kb(key))
        return
    if data.startswith("sfv|"):
        _, key, val = data.split("|", 2)
        err = scanfilt_set(key, val)
        if err:
            await reply(update, f"❌ {esc(err)}")
            return
        await edit(q.message, scanfilt_one_text(key), scanfilt_one_kb(key))
        return
    if data.startswith("sfx|"):
        key = data.split("|", 1)[1]
        spec = gmgn.filter_spec(key)
        contoh = {"usd": "250000", "count": "150", "ratio": "0.25",
                  "percent": "30", "duration": "12h"}.get(spec[4], "1")
        await update.effective_chat.send_message(
            f"✏️ <b>Balas pesan ini</b> dengan nilai untuk <b>{esc(key)}</b>.\n"
            f"{esc(gmgn.FILTER_DESC.get(key, key))} · satuan <b>{esc(spec[4])}</b>"
            + ("\n<i>Rasio ditulis 0–1, mis. 0.25 untuk 25%.</i>" if spec[4] == "ratio" else "")
            + ("\n<i>Umur ditulis 30m / 6h / 7d.</i>" if spec[4] == "duration" else "")
            + f"\nContoh: <code>{esc(contoh)}</code> · <code>off</code> untuk mematikan",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True, input_field_placeholder=contoh))
        AWAITING[update.effective_chat.id] = {"kind": "scanfilt", "key": key}
        return
    if data.startswith("scan|"):
        act = data.split("|", 1)[1]
        c = scanner_cfg()
        if act == "toggle":
            scanner_save({"on": not c.get("on")})
        elif act == "now":
            await q.edit_message_reply_markup(None)
            await cmd_scan(update, None)
            return
        elif act == "iv":
            iv = list(gmgn.INTERVALS)
            scanner_save({"interval": iv[(iv.index(c.get("interval") or "5m") + 1) % len(iv)]})
        elif act == "top":
            scanner_save({"top": _next_step([1, 3, 5, 10], int(c.get("top") or 3))})
        elif act == "watch":
            scanner_save({"watch": _next_step([60, 120, 300, 600], int(c.get("watch") or 60))})
        elif act == "cool":
            scanner_save({"cooldown": _next_step([15, 30, 60, 180], int(c.get("cooldown") or 30))})
        elif act == "chains":
            await update.effective_chat.send_message(
                "⛓ <b>Balas pesan ini</b> dengan daftar chain, dipisah koma.\n"
                f"Pilihan: <code>{esc(', '.join(gmgn.CHAINS))}</code>\n"
                f"Sekarang: <code>{esc(', '.join(c.get('chains') or []))}</code>\n"
                "<i>Chain di luar bsc/base/hyperevm/robinhood tetap dapat kartu info, "
                "tapi bot tidak bisa membuka LP di sana.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=ForceReply(selective=True,
                                        input_field_placeholder="robinhood, base"))
            AWAITING[update.effective_chat.id] = {"kind": "scanchains", "key": ""}
            return
        await edit(q.message, scanner_text(), scanner_kb())
        return
    if data == "menu|rpc":
        await cmd_rpc(update, None)
        return
    if data == "menu|scanner":
        await edit(q.message, scanner_text(), scanner_kb())
        return
    # --- setelan per-jaringan: daftar → pilih jaringan → editor ---
    if data.startswith("setk|"):
        key = data.split("|", 1)[1]
        sp = SETTING_SPEC[key]
        await edit(q.message, f"{sp['emoji']} <b>{esc(sp['label'])}</b>\n\n"
                              f"Pilih jaringan untuk dikonfigurasi.\n"
                              f"<i>{sp['desc']}</i>", setnet_kb(key))
        return
    if data.startswith("setkc|"):
        _, key, c = data.split("|", 2)
        await edit(q.message, setkey_text(key, int(c)), setkey_kb(key, int(c)))
        return
    if data.startswith("setv|"):
        _, key, c, val = data.split("|", 3)
        cid2 = int(c)
        st = store.load_settings(cid2)
        err = apply_setting(st, key, str(val).lower())
        if err:
            await reply(update, f"❌ {esc(err)}")
            return
        # cid eksplisit: setelan chain LAIN tidak boleh mendarat di chain aktif
        store.save_settings(st, cid=cid2)
        await edit(q.message, setkey_text(key, cid2), setkey_kb(key, cid2))
        return
    if data.startswith("setx|"):
        _, key, c = data.split("|", 2)
        sp = SETTING_SPEC[key]
        await update.effective_chat.send_message(
            f"✏️ <b>Balas pesan ini</b> dengan nilai {esc(sp['label'])} untuk "
            f"{esc(ch.CHAINS[int(c)]['name'])}.\n"
            f"Sekarang: <b>{esc(sp['fmt'](_sval(int(c), key)))}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True, input_field_placeholder="3"))
        AWAITING[update.effective_chat.id] = {"kind": "setkey", "key": f"{key}|{c}"}
        return
    # --- editor tombol jumlah (per jaringan, TANPA memindah chain aktif) ---
    if data == "setbtn":
        await edit(q.message, "💰 <b>Tombol Jumlah</b>\n\nPilih jaringan untuk dikonfigurasi.",
                   btnnet_kb())
        return
    if data.startswith("setbtnc|"):
        cid = int(data.split("|", 1)[1])
        await edit(q.message, btn_text(cid), btn_kb(cid))
        return
    if data.startswith("btndel|"):
        _, c, sym, val = data.split("|", 3)
        cid = int(c)
        skrg = presets_get(cid, sym) or amount_presets(sym, cid)
        sisa = [v for v in skrg if f"{v:g}" != val]
        if not sisa:
            sisa = [0]      # penanda "sengaja kosong" dibuang _presets_norm -> default lagi
        # Daftar kosong = kembali ke tebakan default, BUKAN "tanpa tombol". Kalau
        # user memang mau kosong, dia menghapusnya satu per satu dan tebakannya
        # muncul lagi — itu perilaku yang sama dengan simbol yang belum pernah diatur.
        presets_set(cid, sym, sisa)
        await edit(q.message, btn_text(cid), btn_kb(cid))
        return
    if data.startswith("btnrst|"):
        _, c, sym = data.split("|", 2)
        presets_set(int(c), sym, [])
        await edit(q.message, btn_text(int(c)), btn_kb(int(c)))
        return
    if data.startswith("btnadd|"):
        _, c, sym = data.split("|", 2)
        await update.effective_chat.send_message(
            f"➕ <b>Balas pesan ini</b> dengan jumlah untuk tombol <b>{esc(sym)}</b> "
            f"di {esc(ch.CHAINS[int(c)]['name'])}.\n"
            f"Contoh: <code>25</code> atau <code>0.05</code> — jumlah token, bukan persen.",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True, input_field_placeholder="25"))
        AWAITING[update.effective_chat.id] = {"kind": "btnadd", "key": f"{c}|{sym}"}
        return
    if data == "setrst":
        await edit(q.message,
                   "🗑 <b>Reset semua pengaturan?</b>\n\n"
                   "Slippage, impact, gap, autoswap, interval monitor, lebar/jumlah "
                   "default, dan semua tombol jumlah kembali ke bawaan.\n\n"
                   "<i>Wallet, posisi, dan riwayat PnL TIDAK tersentuh — yang direset "
                   "cuma settings.json.</i>",
                   InlineKeyboardMarkup([
                       [InlineKeyboardButton("✅ Ya, reset", callback_data="setrst2"),
                        InlineKeyboardButton("❌ Batal", callback_data="menu|settings")]]))
        return
    if data == "setrst2":
        lama = store.load_settings()
        baru = dict(store.DEFAULT_SETTINGS)
        # chain & wallet aktif dipertahankan: keduanya "di mana saya sekarang",
        # bukan preferensi — meresetnya bikin user tiba-tiba pindah chain.
        baru["chain"] = lama.get("chain", baru["chain"])
        baru["wallet_idx"] = lama.get("wallet_idx", 0)
        store.save_settings(baru, raw=True)   # reset: buang juga override per-chain
        await edit(q.message, settings_text(), settings_kb())
        return
    if data == "menu|revoke":
        await cmd_revoke(update, None)
        return
    if data == "menu|cleanup":
        await cmd_cleanup(update, None)
        return
    if data == "menu|recover":
        await cmd_recover(update, None)
        return
    if data == "menu|all":
        await cmd_all(update, None)
        return
    if data == "menu|help":
        await edit(q.message, HELP, InlineKeyboardMarkup([BACK_ROW]))
        return
    # --- navigasi pesan baru (dipakai dari receipt tx, biar receipt tetap ada) ---
    if data == "go|main":
        await show_main_menu(update)
        return
    if data == "go|list":
        await cmd_list(update, None)
        return
    if data.startswith("chsel|"):
        store.set_chain(int(data.split("|")[1]))
        await show_main_menu(update, msg=q.message)
        return
    if data.startswith("cyc|"):
        cycle_setting(data.split("|")[1])
        await edit(q.message, settings_text(), settings_kb())
        return
    if data == "askset":
        await update.effective_chat.send_message(
            ("✏️ <b>Balas pesan ini</b> dengan <code>key value</code>\n"
             f"key: {SET_KEYS}\n"
             "contoh: <code>slippage 3</code> · <code>amount 0.05</code> · <code>alert off</code>"),
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True, input_field_placeholder="slippage 3"))
        AWAITING[update.effective_chat.id] = {"kind": "setval", "key": ""}
        return
    if data.startswith("posc|"):
        # Posisi di chain LAIN: pindahkan chain aktif dulu, baru buka kartunya.
        # Semua alur aksi (add/close/rebalance/order) membaca chain aktif, jadi ini
        # yang membuat daftar lintas-chain aman tanpa mengoper chain_id ke mana-mana.
        _, c, pid = data.split("|", 2)
        if int(c) != store.load_settings()["chain"]:
            store.set_chain(int(c))
            await reply(update, f"⛓ Chain aktif dipindah ke <b>{esc(ch.CHAINS[int(c)]['name'])}</b> "
                                f"— posisi ini ada di sana.")
        await show_position(update, q.message, pid)
        return
    if data.startswith("pos|"):
        await show_position(update, q.message, data.split("|", 1)[1])
        return
    if data.startswith("mig|"):
        await ask_migrate(update, data.split("|", 1)[1])
        return
    if data.startswith("cmpok|"):
        await q.edit_message_reply_markup(None)
        await do_compound(update, data.split("|", 1)[1])
        return
    if data.startswith("cmp|"):
        await ask_compound(update, data.split("|", 1)[1])
        return
    if data.startswith("rvk|"):
        _, k, i = data.split("|", 2)
        await q.edit_message_reply_markup(None)
        await do_revoke(update, k, int(i))
        return
    if data.startswith("rvkall|"):
        await q.edit_message_reply_markup(None)
        await do_revoke(update, data.split("|", 1)[1], None)
        return
    if data.startswith("chtok|"):
        # user memilih chain untuk token yang punya pool di beberapa chain
        _, c, tok = data.split("|", 2)
        cid2 = int(c)
        store.set_chain(cid2)
        await q.edit_message_reply_markup(None)
        await show_pools_for(q.message, cid2, tok)
        return
    if data.startswith("pool|"):
        key = data.split("|", 1)[1]
        src_pid = MIGRATE.get(update.effective_chat.id)
        if src_pid:
            await show_migrate_confirm(q.message, key, src_pid)
        else:
            # pilih pool → kartu konfirmasi (belum mint)
            await show_confirm(q.message, key)
        return
    if data.startswith("np|"):
        await show_newpool(q.message, data.split("|", 1)[1])
        return
    if data.startswith(("npq|", "npf|", "nps|")):
        pre, k, val = data.split("|", 2)
        ctx = NEWPOOL.get(k)
        if not ctx:
            await edit(q.message, "⚠️ Tombol kadaluarsa (bot sempat restart). Paste alamat lagi.")
            return
        ctx["quote_addr" if pre == "npq" else "fee" if pre == "npf" else "spacing"] = (
            val if pre == "npq" else int(val))
        if pre == "npf":
            ctx["spacing"] = None        # kisi ikut fee kecuali user memilih sendiri
        await show_newpool(q.message, k)
        return
    if data.startswith(("npxf|", "npxs|")):
        k = data.split("|", 1)[1]
        fee = data.startswith("npxf|")
        # prompt sendiri, bukan ask_custom(): fungsi itu membaca PENDING sedangkan
        # konteks pembuatan pool ada di NEWPOOL.
        txt = (f"✏️ <b>Balas pesan ini</b> dengan fee dalam PERSEN "
               f"(0–{ch.V4_FEE_MAX / 1e4:g}). Contoh: <code>0.75</code>"
               if fee else
               f"✏️ <b>Balas pesan ini</b> dengan tick spacing "
               f"({ch.V4_SPACING_MIN}–{ch.V4_SPACING_MAX}). Contoh: <code>25</code>\n"
               f"<i>Makin kecil = range bisa makin rapat.</i>")
        await update.effective_chat.send_message(
            txt, parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True,
                                    input_field_placeholder="0.75" if fee else "25"))
        AWAITING[update.effective_chat.id] = {"kind": "npfee" if fee else "npspacing", "key": k}
        return
    if data.startswith("npback|"):
        ctx = NEWPOOL.get(data.split("|", 1)[1])
        if not ctx:
            await edit(q.message, "⚠️ Tombol kadaluarsa. Paste alamat lagi.")
            return
        await show_pools_for(q.message, ctx["chain"], ctx["token"]["address"])
        return
    if data.startswith(("npok|", "npgo|")):
        await q.edit_message_reply_markup(None)
        await do_newpool(update, data.split("|", 1)[1])
        return
    if data.startswith("pools|"):
        # Balik ke daftar pool token yang sama, di pesan yang SAMA. ctx lama
        # dibiarkan: show_pools_for membuat key baru untuk tiap pool, dan
        # discovery-nya sudah di-cache jadi klik ini murah.
        ctx = PENDING.get(data.split("|", 1)[1])
        if not ctx:
            await edit(q.message, "⚠️ Tombol kadaluarsa (bot sempat restart). Paste alamat lagi.")
            return
        await show_pools_for(q.message, ctx["chain"], ctx["token"]["address"])
        return
    if data.startswith("migok|"):
        _, key, mode = data.split("|")
        await q.edit_message_reply_markup(None)
        await do_migrate(update, key, mode)
        return
    if data.startswith("tight|"):
        key = data.split("|", 1)[1]
        ctx = PENDING.get(key)
        if not ctx:
            await edit(q.message, "⚠️ Tombol kadaluarsa (bot sempat restart). Paste alamat lagi.")
            return
        # Rapat = buat range MENCAKUP harga sekarang supaya posisi langsung aktif,
        # TAPI bentuk mode yang dipilih dipertahankan:
        #   Lower  → lebar bawah tetap, tepi atas cuma 1 kotak di atas harga
        #            (mayoritas quote, sedikit meme dibeli otomatis)
        #   Upper  → kebalikannya
        #   Stable/Wide → satu kotak di kedua sisi
        # Mesin mint memakai jalur dua-sisi ("wide"), jadi mode disetel ke situ.
        m = ctx["mode"]
        if m == "lower":
            ctx["up_pct"] = TIGHT_PCT          # low_pct dibiarkan apa adanya
        elif m == "upper":
            ctx["low_pct"] = TIGHT_PCT
        else:
            ctx["low_pct"] = ctx["up_pct"] = TIGHT_PCT
        ctx["mode"] = "stable" if m in ("stable", "wide") else "wide"
        ctx["gap"] = 0
        await show_confirm(q.message, key)
        return
    if data.startswith("okimp|"):
        ctx = PENDING.get(data.split("|", 1)[1])
        if ctx is not None:
            # Ambang dinaikkan HANYA untuk kartu ini, bukan setelan global.
            ctx["max_impact"] = 1.0
        await show_confirm(q.message, data.split("|", 1)[1])
        return
    if data.startswith("amtf|"):
        _, key, val = data.split("|", 2)
        ctx = PENDING.get(key)
        if not ctx:
            await edit(q.message, "⚠️ Tombol kadaluarsa (bot sempat restart). Paste alamat lagi.")
            return
        ctx["amount_fixed"] = float(val)     # satuan budget kartu ini, bukan persen
        await show_confirm(q.message, key)
        return
    if data.startswith(("wd|", "amt|", "st|", "amtsrc|")):
        parts = data.split("|")
        kind, key = parts[0], parts[1]
        ctx = PENDING.get(key)
        if not ctx:
            await edit(q.message, "⚠️ Tombol kadaluarsa (bot sempat restart). Paste alamat lagi.")
            return
        if kind == "wd":
            ctx["low_pct"], ctx["up_pct"] = float(parts[2]), float(parts[3])
        elif kind == "st":
            ctx["mode"] = parts[2]
            # default lebar per mode
            defaults = {"stable": (6.18, 6.18), "wide": (50, 100), "lower": (50, 100), "upper": (50, 100)}
            ctx["low_pct"], ctx["up_pct"] = defaults[ctx["mode"]]
        elif kind == "amtsrc":
            # Saldo mana yang dipersenkan: sisi quote (lama) atau token meme.
            ctx["amount_src"] = parts[2]
            ctx["amount_fixed"] = None       # kembali ke persen, bukan jumlah tetap
        else:
            ctx["amount_pct"] = float(parts[2])
            ctx["amount_fixed"] = None
        await show_confirm(q.message, key)
        return
    if data.startswith(("askrng|", "askamt|")):
        kind = "range" if data.startswith("askrng|") else "amount"
        await ask_custom(update, data.split("|", 1)[1], kind)
        return
    if data.startswith("cancelp|"):
        PENDING.pop(data.split("|", 1)[1], None)
        await edit(q.message, "❌ Cancelled.")
        return
    if data.startswith("mint|"):
        key = data.split("|", 1)[1]
        ctx = PENDING.pop(key, None)
        if not ctx:
            await edit(q.message, "⚠️ Tombol kadaluarsa (bot sempat restart). Paste alamat lagi.")
            return
        await q.edit_message_reply_markup(None)
        await do_mint(update, ctx)
        return
    if data.startswith("chart|"):
        # tombol lama (pra-link eksternal) — arahkan ke kartu detail
        await show_position(update, q.message, data.split("|", 1)[1])
        return
    if data.startswith("add|"):
        await ask_add(update, data.split("|", 1)[1])
        return
    if data.startswith("addok|"):
        _, tid, val, kind = data.split("|")
        await q.edit_message_reply_markup(None)
        await do_add_exec(update, tid, float(val), kind == "p")
        return
    if data.startswith("fee|"):
        await do_collect(update, data.split("|", 1)[1])
        return
    if data.startswith("reb|"):
        await ask_rebalance(update, data.split("|", 1)[1])
        return
    if data.startswith("rebok|"):
        _, tid, mode = data.split("|")
        await q.edit_message_reply_markup(None)
        await do_rebalance(update, tid, mode)
        return
    if data.startswith("askred|"):
        await ask_reduce_custom(update, data.split("|", 1)[1])
        return
    if data.startswith("red|"):
        await ask_reduce(update, data.split("|", 1)[1])
        return
    if data.startswith("redok|"):
        _, tid, pct = data.split("|")
        await q.edit_message_reply_markup(None)
        await do_reduce_exec(update, tid, int(pct))
        return
    if data.startswith("close|"):
        await ask_close(update, data.split("|", 1)[1])
        return
    if data.startswith("closeok|"):
        parts = data.split("|")
        await q.edit_message_reply_markup(None)
        await do_close(update, parts[1], autoswap=(len(parts) > 2 and parts[2] == "1"))
        return
    if data == "menu|orders":
        await show_orders(update, q.message)
        return
    if data.startswith("tpsl|"):
        await ask_tpsl(update, data.split("|", 1)[1])
        return
    if data.startswith("orderok|"):
        _, pid, tp_s, sl_s, sw = data.split("|")
        await q.edit_message_reply_markup(None)
        await do_create_order(update, pid, tp_s, sl_s, sw == "1")
        return
    if data.startswith("ordcancel|"):
        oid = data.split("|", 1)[1]
        cid = store.load_settings()["chain"]
        store.update_order(cid, oid, status="cancelled", reason="dibatalkan user")
        await show_orders(update, q.message)
        return


# ---------- Order TP/SL (auto-close posisi LP saat market cap sentuh batas) ----------
def parse_tpsl(text: str, mc_now: float) -> tuple[float | None, float | None]:
    """Parse balasan TP/SL → (tp_mc, sl_mc) dalam USD. Format:
    'tp 800k' · 'sl 200k' · 'tp 800k sl 200k' · '800k 200k' (TP lalu SL) ·
    '800k -' / '- 200k' (lewati satu sisi). Raise ValueError kalau invalid."""
    t = text.lower().replace("$", "").strip()
    toks = [x for x in t.replace(",", " ").split() if x]

    def num(x):
        if x in ("-", "x", "skip", "none", "n"):
            return None
        return _num_usd(x)

    tp = sl = None
    if any(x in ("tp", "sl") for x in toks):     # bentuk berlabel
        i = 0
        while i < len(toks):
            if toks[i] in ("tp", "sl") and i + 1 < len(toks):
                v = num(toks[i + 1])
                if toks[i] == "tp":
                    tp = v
                else:
                    sl = v
                i += 2
            else:
                i += 1
    else:                                        # posisional: [TP] [SL]
        if len(toks) >= 1:
            tp = num(toks[0])
        if len(toks) >= 2:
            sl = num(toks[1])
    if tp is None and sl is None:
        raise ValueError("isi minimal satu batas TP atau SL")
    if mc_now > 0:
        if tp is not None and tp <= mc_now:
            raise ValueError(f"TP harus > MC sekarang ({ch.fmt_usd(mc_now)})")
        if sl is not None and sl >= mc_now:
            raise ValueError(f"SL harus < MC sekarang ({ch.fmt_usd(mc_now)})")
    if tp is not None and sl is not None and sl >= tp:
        raise ValueError("SL harus < TP")
    return tp, sl


async def ask_tpsl(update: Update, pid: str):
    s = store.load_settings()
    cid = s["chain"]

    def work():
        return position_one(cid, pid)

    p = await asyncio.to_thread(work)
    if not p:
        await reply(update, f"❌ Posisi {disp_pid(pid)} tidak ditemukan.")
        return
    if p.get("ver") == 2:
        await reply(update, "⚠️ Posisi v2 full-range — TP/SL berbasis market cap tidak berlaku.")
        return
    meme_sym = p["sym0"] if p["quote_is_token1"] else p["sym1"]
    mc = p.get("mc_now")
    mc_txt = f"MC {esc(meme_sym)} sekarang: <b>{ch.fmt_usd(mc)}</b>\n" if mc else ""
    await update.effective_chat.send_message(
        (f"🎯 <b>TP/SL untuk {esc(meme_sym)} {disp_pid(pid)}</b>\n{mc_txt}\n"
         f"<b>Balas pesan ini</b> dengan batas market cap:\n"
         f"· <code>tp 800k</code> — take profit di MC 800k\n"
         f"· <code>sl 200k</code> — stop loss di MC 200k\n"
         f"· <code>800k 200k</code> — TP lalu SL sekaligus\n"
         f"· <code>800k -</code> / <code>- 200k</code> — lewati satu sisi\n\n"
         f"Saat MC sentuh batas → posisi auto-close."),
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder="tp 800k · sl 200k · 800k 200k"))
    AWAITING[update.effective_chat.id] = {"kind": "order", "key": str(pid)}


async def do_create_order(update: Update, pid: str, tp_s: str, sl_s: str, autoswap: bool):
    s = store.load_settings()
    cid = s["chain"]

    def snap():
        return position_one(cid, pid)

    p = await asyncio.to_thread(snap)
    if not p:
        await reply(update, f"❌ Posisi {disp_pid(pid)} tidak ditemukan (mungkin sudah ditutup).")
        return
    meme_sym = p["sym0"] if p["quote_is_token1"] else p["sym1"]
    tp = None if tp_s == "x" else float(tp_s)
    sl = None if sl_s == "x" else float(sl_s)
    oid = await asyncio.to_thread(store.add_order, cid, {
        "wallet": wallet_address(), "pid": str(pid), "meme_sym": meme_sym,
        "tp_mc": tp, "sl_mc": sl, "autoswap": bool(autoswap), "slippage": s["slippage_pct"],
    })
    interval = int(s.get("alert_secs", 60) or 0)
    warn = "" if interval > 0 else ("\n<i>ℹ️ Alert OFF — cek TP/SL tetap jalan tiap ~30s "
                                    "selama ada pesanan aktif.</i>")
    await reply(update, (
        f"✅ <b>Pesanan dibuat</b> <code>{oid}</code>\n"
        f"{esc(meme_sym)} {disp_pid(pid)} · TP {ch.fmt_usd(tp) if tp else '—'} · "
        f"SL {ch.fmt_usd(sl) if sl else '—'} · {'auto-swap' if autoswap else 'tahan token'}{warn}"),
        InlineKeyboardMarkup([[InlineKeyboardButton("🎯 Pesanan", callback_data="menu|orders"),
                               InlineKeyboardButton("🏠 Menu", callback_data="menu|main"), DEL_BTN]]))


def _orders_for_chain(cid: int, status: str = "") -> list[dict]:
    out = []
    for k in all_pks():
        out += store.orders(cid, _addr_of(k), status=status)
    return out


def orders_text(cid: int) -> str:
    cfg = ch.CHAINS[cid]
    lines = [f"🎯 <b>Pesanan TP/SL</b> — {esc(cfg['name'])}",
             "Auto-close posisi LP saat market cap sentuh batas.\n"]
    active = _orders_for_chain(cid, "active")
    if not active:
        lines.append("Belum ada pesanan aktif.\nBuka 📊 Posisi → tombol 🎯 TP/SL untuk buat.")
    else:
        for o in active:
            tp = ch.fmt_usd(o["tp_mc"]) if o.get("tp_mc") else "—"
            sl = ch.fmt_usd(o["sl_mc"]) if o.get("sl_mc") else "—"
            sw = "swap" if o.get("autoswap") else "tahan"
            lines.append(f"• <code>{o['id']}</code> {esc(o.get('meme_sym', ''))} "
                         f"{disp_pid(o['pid'])} · TP {tp} · SL {sl} · {sw}")
    hist = [o for o in _orders_for_chain(cid)
            if o.get("status") in ("done", "error", "cancelled")]
    hist.sort(key=lambda o: o.get("triggered") or o.get("created") or 0, reverse=True)
    if hist:
        lines.append("\n<b>Riwayat terakhir:</b>")
        for o in hist[:5]:
            icon = {"done": "✅", "error": "⚠️", "cancelled": "🚫"}.get(o["status"], "•")
            lines.append(f"{icon} <code>{o['id']}</code> {disp_pid(o['pid'])} · "
                         f"{esc(o.get('reason', '') or o['status'])}")
    return "\n".join(lines)


def orders_kb(cid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"✖ Batal {o['id']} ({disp_pid(o['pid'])})",
                                  callback_data=f"ordcancel|{o['id']}")]
            for o in _orders_for_chain(cid, "active")]
    rows.append([InlineKeyboardButton("📊 Posisi (buat baru)", callback_data="menu|list"),
                 InlineKeyboardButton("🏠 Menu", callback_data="menu|main")])
    return InlineKeyboardMarkup(rows)


async def show_orders(update: Update, msg=None):
    cid = store.load_settings()["chain"]
    if msg is None:
        msg = await reply(update, "⏳ Memuat pesanan...")
    else:
        await edit(msg, "⏳ Memuat pesanan...")
    try:
        text = await asyncio.to_thread(orders_text, cid)
        kb = await asyncio.to_thread(orders_kb, cid)
    except Exception as e:
        await edit(msg, f"❌ Gagal load pesanan: {esc(e)}", InlineKeyboardMarkup([BACK_ROW]))
        return
    await edit(msg, text, kb)


async def cmd_orders(update: Update, _, status_msg=None):
    if not authorized(update):
        return
    await show_orders(update, status_msg)


# ---------- Monitor: alert in/out range + eksekusi order TP/SL ----------
# ---------- Jembatan alert token trending (lp-scanner / GMGN) ----------
# Scanner GMGN jalan sebagai proses TERPISAH (Node) dan menulis kandidat yang lolos
# saringannya ke file JSONL. Bot ini yang menyambungnya ke dunia LP: cari pool,
# hitung saran posisi dari data ON-CHAIN sendiri, lalu kirim kartu bertombol yang
# masuk ke alur mint biasa. **Mint tetap manual** — bot tidak pernah mengirim tx
# dari jalur ini.
#
# File, bukan HTTP: dua proses bisa restart sendiri-sendiri, tidak perlu port,
# tidak perlu token, dan tidak ada yang gagal kalau salah satunya sedang mati.
LP_INBOX = os.environ.get("LP_ALERT_INBOX", "").strip() or None
_LP_INBOX_TICK = 10          # detik antar pemeriksaan inbox
_LP_SEEN: dict = {}          # "cid:token" -> ts, redaman ganda di sisi bot ini
_LP_SEEN_TTL = 3600
_LP_APR_MIN_SHARE = 0.2      # pool baru layak ditunjuk kalau TVL-nya >= 20% pool terdalam


def _lp_take() -> list[dict]:
    """Ambil semua entri inbox lalu kosongkan, atomik lewat rename.

    Rename dulu baru baca: penulis (proses scanner) membuka file lewat PATH tiap
    kali append, jadi sesudah rename ia membuat file baru dan tidak ada baris yang
    tertimpa. Sisa `.taking` dari proses yang mati di tengah ikut dibaca duluan,
    supaya kandidat tidak hilang gara-gara restart."""
    if not LP_INBOX:
        return []
    out: list[dict] = []
    tmp = LP_INBOX + ".taking"

    def _slurp(path):
        try:
            with open(path) as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        log.warning("baris inbox LP tidak bisa di-parse, dilewati")
            os.unlink(path)
        except FileNotFoundError:
            pass

    _slurp(tmp)                      # sisa dari crash sebelumnya
    try:
        os.replace(LP_INBOX, tmp)
    except (FileNotFoundError, OSError):
        return out
    _slurp(tmp)
    return out


def _lp_cid(slug: str) -> int | None:
    """Slug chain GMGN → chain id bot. None = chain itu tidak didukung di sini."""
    for cid, cfg in ch.CHAINS.items():
        if cfg.get("gmgn") == slug:
            return cid
    return None


def fmt_age_short(sec) -> str:
    """Umur token dalam satuan yang masih punya arti pada skalanya.

    Dulu selalu dibulatkan ke 0,1 jam — token berumur 11 menit terbaca
    "0,2 jam", dan itu justru menghilangkan informasi yang paling penting untuk
    token sebaru itu. Di bawah satu jam yang relevan MENIT, bukan pecahan jam."""
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return "belum diketahui"
    if s < 0:
        return "belum diketahui"
    if s < 60:
        inti = f"{s} detik"
    elif s < 3600:
        inti = f"{s // 60} menit"
    elif s < 86400:
        j, m = s // 3600, (s % 3600) // 60
        inti = f"{j} jam" + (f" {m} menit" if m else "")
    else:
        h, j = s // 86400, (s % 86400) // 3600
        inti = f"{h} hari" + (f" {j} jam" if j else "")
    if s < 1800:
        return inti + " — sangat baru"
    if s < 86400:
        return inti + " — baru"
    return inti


def _lp_ctx_lines(e: dict) -> list[str]:
    """Konteks dari GMGN — angka MENTAH apa adanya, tanpa turunan baru.

    `None` berarti belum diketahui dan ditulis begitu; menyulapnya jadi 0 akan
    membuat 'belum diuji' terbaca sebagai 'aman' (aturan yang sama dipegang
    scanner-nya)."""
    iv = e.get("interval") or "?"

    def u(v):
        return ch.fmt_usd(v) if isinstance(v, (int, float)) else "belum diketahui"

    def p(v, d=0):
        return f"{v * 100:.{d}f}%" if isinstance(v, (int, float)) else "belum diketahui"

    L = [f"📡 <b>Alert token trending</b> · {esc(e.get('tier') or '?')}",
         f"🌑 <b>{esc(e.get('symbol') or '?')}</b>"
         + (f" ({esc(e['name'])})" if e.get("name") and e["name"] != e.get("symbol") else ""),
         f"📋 <code>{esc(e.get('address') or '')}</code>",
         f"💸 Volume {esc(iv)} {u(e.get('volume'))} · 📦 MC {u(e.get('marketCap'))}",
         f"💧 Likuiditas GMGN {u(e.get('liquidity'))} <i>(dua sisi)</i>"
         + (f" · putaran {e['turnover']:.2f}×" if isinstance(e.get("turnover"), (int, float)) else ""),
         ]
    if isinstance(e.get("ageSeconds"), (int, float)):
        L.append(f"🕐 Umur token {esc(fmt_age_short(e['ageSeconds']))}")
    if isinstance(e.get("priceChangePercent"), (int, float)):
        L.append(f"📈 Gerak harga {esc(iv)}: {e['priceChangePercent']:+.2f}%")
    def n(v):
        # _num() mengembalikan float, jadi jumlah wallet harus dibulatkan untuk
        # ditampilkan — "Holder 3288.0" terbaca seperti angka pecahan yang salah.
        return f"{v:,.0f}" if isinstance(v, (int, float)) else "?"

    L.append(f"👥 Holder {n(e.get('holderCount'))} · smart-money "
             f"{n(e.get('smartDegenCount'))} · KOL {n(e.get('renownedCount'))}")
    if isinstance(e.get("swaps"), (int, float)):
        L.append(f"🔁 Swap {esc(iv)}: {n(e['swaps'])}"
                 + (f" · porsi jual {e['sellShare'] * 100:.0f}% ({esc(e.get('sellShareSource') or '')})"
                    if isinstance(e.get("sellShare"), (int, float)) else ""))
    L.append(f"🛡 Skor rug GMGN {p(e.get('rugRatio'), 1)} · 10 holder teratas "
             f"{p(e.get('top10HolderRate'))}")
    if e.get("pollHits") and e.get("pollWindow"):
        L.append(f"🕐 Terkonfirmasi {e['pollHits']}/{e['pollWindow']} polling")
    for r in (e.get("skips") or [])[:4]:
        L.append(f"🔴 {esc(r)}")
    for r in (e.get("warns") or [])[:5]:
        L.append(f"🟡 {esc(r)}")
    if e.get("missing"):
        L.append(f"📦 Data belum lengkap: {esc(', '.join(e['missing'][:4]))} — "
                 f"<i>kosong bukan berarti aman</i>")
    L.append("<i>Semua angka di atas dari GMGN, bukan hasil pemeriksaan on-chain bot ini.</i>")
    return L


def lp_suggestion(cid: int, res: dict, e: dict) -> list[str]:
    """Saran posisi. Hanya dari angka yang benar-benar diukur — bukan karangan.

    Tiga hal yang disebut, dan tiap-tiapnya menyebut DASARNYA:

    - **Pool mana.** Daftar dari discovery bot sendiri (sudah diverifikasi on-chain),
      bukan dari GMGN. Yang ditunjuk dua: APR tertinggi dan TVL terdalam — dua
      tujuan berbeda, dan menyembunyikan salah satunya berarti memilihkan untuk
      user. Kalau pool ber-APR tertinggi jauh lebih tipis, itu disebut.
    - **Mode range.** `recommend_strategy()` yang sudah ada, yang mengukur
      volatilitas pool dari oracle TWAP. Pool v4 tidak punya oracle itu, jadi kalau
      tidak terukur dikatakan tidak terukur — tidak diganti tebakan.
    - **Apakah lebar range default masuk akal.** Ini aritmetika lurus dari gerak
      harga yang DILAPORKAN GMGN pada interval alert, bukan model apa pun:
      berapa kali gerakan sebesar itu, searah, sampai harga keluar range.

    Silang-cek likuiditas GMGN vs TVL hitungan sendiri juga disebut kalau jauh
    beda — dua sumber yang tidak sepakat adalah informasi, bukan gangguan.
    """
    pools = res.get("pools") or []
    if not pools:
        return []
    s = store.load_settings(cid)
    L = ["", "📐 <b>SARAN POSISI</b>"]

    dalam = max(pools, key=lambda p: p.get("tvl_usd") or 0)
    tv_d = dalam.get("tvl_usd") or 0

    def tag(p):
        return (f"[v{p.get('ver', 3)}] {esc(p['quote_sym'])} {p['fee'] / 10000:.2f}% · "
                f"TVL {ch.fmt_usd(p.get('tvl_usd'))}"
                + (f" · APR ~{p['apr_pct']:,.0f}%" if p.get("apr_pct") else ""))

    # APR pool debu SELALU terlihat menang — rumusnya membagi dengan TVL, jadi
    # pool $800 dengan sedikit volume mengalahkan pool $150k. Menunjuknya tetap
    # salah walau diberi peringatan, karena angka terbesar yang menarik mata.
    # Kandidat karena itu dibatasi ke pool yang TVL-nya >= _LP_APR_MIN_SHARE dari
    # pool terdalam; yang terbuang tetap DISEBUT, bukan dihilangkan diam-diam.
    ber_apr = [p for p in pools if p.get("apr_pct")]
    layak = [p for p in ber_apr if (p.get("tvl_usd") or 0) >= tv_d * _LP_APR_MIN_SHARE]
    terbaik = max(layak, key=lambda p: p["apr_pct"]) if layak else None
    tertinggi = max(ber_apr, key=lambda p: p["apr_pct"]) if ber_apr else None

    L.append(f"🌊 Terdalam: {tag(dalam)}")
    if terbaik and terbaik is not dalam:
        L.append(f"💰 APR terbaik yang cukup dalam: {tag(terbaik)}")
    if tertinggi and tertinggi not in (terbaik, dalam):
        L.append(f"   ⏭ APR tertinggi sebenarnya {tertinggi['apr_pct']:,.0f}% di "
                 f"{tag(tertinggi)} — <b>tidak ditunjuk</b>, TVL-nya di bawah "
                 f"{_LP_APR_MIN_SHARE * 100:.0f}% pool terdalam. APR dihitung ÷TVL, jadi "
                 f"pool debu selalu terlihat menang padahal masuk-keluarnya mahal.")
    pilih = terbaik or dalam

    # Mode: dari pengukuran yang sudah ada, dan sebutkan kalau tidak terukur
    try:
        rec, vol = recommend_strategy({"chain": cid, "pool_info": pilih,
                                       "token": res["token"]})
        if vol is not None:
            L.append(f"🎯 Mode: <b>{STRAT_LABEL[rec]}</b> — volatilitas pool 24 jam "
                     f"terukur {vol:.0f}% (oracle TWAP pool)")
        else:
            L.append(f"🎯 Mode: <b>{STRAT_LABEL[rec]}</b> — <i>volatilitas tidak terukur "
                     f"(pool v4 tidak punya oracle TWAP), jadi ini default, bukan hasil ukur</i>")
    except Exception:
        pass

    # Apakah lebar default masuk akal terhadap gerak harga yang dilaporkan alert
    gerak = e.get("priceChangePercent")
    lebar = float(s.get("width_pct") or 0)
    if isinstance(gerak, (int, float)) and abs(gerak) > 0.01 and lebar > 0:
        n = lebar / abs(gerak)
        iv = esc(e.get("interval") or "?")
        L.append(f"📏 Lebar default kamu ±{lebar:g}%. Harga bergerak {gerak:+.2f}% dalam "
                 f"{iv} terakhir — gerakan sebesar itu <b>{n:.1f}×</b> berturut searah sudah "
                 f"menembus batas range.")
        if n < 3:
            L.append("   ⚠️ artinya range itu gampang keluar di token seaktif ini; "
                     "range lebih lebar = fee lebih tipis tapi lebih jarang rebalance")

    # Dua sumber likuiditas yang tidak sepakat itu informasi, bukan gangguan
    gl, tv = e.get("liquidity"), dalam.get("tvl_usd")
    if isinstance(gl, (int, float)) and gl > 0 and tv:
        r = tv / gl
        if r < 0.4 or r > 2.5:
            L.append(f"🔎 Likuiditas GMGN {ch.fmt_usd(gl)} vs TVL pool terdalam hitungan "
                     f"bot {ch.fmt_usd(tv)} ({r:.1f}×) — GMGN menjumlah SEMUA pool token ini, "
                     f"bot menghitung per-pool. Pakai angka per-pool untuk menilai kedalaman.")
    L.append("<i>Saran, bukan perintah. Mint tetap kamu yang menekan tombolnya.</i>")
    return L


async def _lp_alert(app, e: dict):
    """Satu kandidat dari scanner → kartu + daftar pool bertombol."""
    slug = str(e.get("chain") or "")
    addr = str(e.get("address") or "")
    cid = _lp_cid(slug)
    if not cid or not ch.Web3.is_address(addr):
        log.info("alert LP dilewati: chain %s tidak didukung / alamat tidak valid", slug)
        return
    # Redaman ganda: scanner punya cooldown sendiri, tapi restart-nya mengosongkan
    # state. Ini menjaga chat tidak dibanjiri token yang sama kalau itu terjadi.
    k = f"{cid}:{addr.lower()}"
    now = time.time()
    for old_k, ts in [(x, t) for x, t in _LP_SEEN.items() if now - t > _LP_SEEN_TTL]:
        _LP_SEEN.pop(old_k, None)
    if now - _LP_SEEN.get(k, 0) < _LP_SEEN_TTL:
        return
    _LP_SEEN[k] = now

    chats = list(allowed_chat_ids())
    if not chats:
        return
    body = "\n".join(_lp_ctx_lines(e))
    msg = None
    for chat_id in chats:
        try:
            m = await app.bot.send_message(chat_id, body, parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=True)
            msg = msg or m
        except Exception:
            pass
    if msg is None:
        return
    # Kartu pool memakai jalur yang SAMA PERSIS dengan tempel-CA manual, jadi
    # tombolnya masuk ke alur konfirmasi mint yang sudah ada — tidak ada jalur
    # transaksi baru yang perlu diuji ulang.
    status = await app.bot.send_message(msg.chat_id, "🔎 Mencari pool…",
                                        parse_mode=ParseMode.HTML)
    try:
        await show_pools_for(status, cid, ch.Web3.to_checksum_address(addr), extra=e)
    except Exception as err:
        log.warning("alert LP %s: %s", addr, err)
        await edit(status, f"❌ Gagal mencari pool: {esc(err)}")


# ---------- Scanner token trending bawaan (GMGN) ----------
# Dulu proses Node terpisah yang menulis file JSONL; sekarang di dalam bot ini
# supaya satu repo, satu proses, satu deploy. Jembatan file tetap ada dan tetap
# jalan (`LP_ALERT_INBOX`) kalau suatu saat mau memberi makan dari luar.
_SCAN_STATE_FILE = str(Path(__file__).with_name(".scanner_state.json"))
_SCAN = {"client": None, "pace": None, "state": None, "warned": False}
_APP = [None]        # Application, diisi _start_background — dipakai jalur yang
                     # dipanggil dari tombol (context-nya tidak selalu ada)


def scanner_cfg() -> dict:
    """Setelan scanner: default ditimpa yang tersimpan.

    **`filters` TIDAK di-merge** — dipakai apa adanya kalau user pernah
    menyimpannya. Kalau di-merge, `/scanner set maxRugRatio off` tidak akan
    pernah berfungsi: kunci yang baru dihapus langsung diisi ulang oleh default
    di siklus berikutnya. Konsekuensinya filter default baru tidak menyusul ke
    user lama — itu memang yang diinginkan untuk setelan yang sudah disentuh."""
    saved = store.load_settings().get("scanner") or {}
    c = {**store.DEFAULT_SETTINGS["scanner"], **saved}
    c["filters"] = dict(saved["filters"]) if isinstance(saved.get("filters"), dict) \
        else dict(store.DEFAULT_SETTINGS["scanner"]["filters"])
    return c


def scanner_save(patch: dict):
    s = store.load_settings()
    cur = {**store.DEFAULT_SETTINGS["scanner"], **(s.get("scanner") or {})}
    cur.update(patch)
    store.set_global("scanner", cur)


def _scan_client(pace: float):
    """Klien GMGN, dibuat ulang HANYA kalau pace berubah — sesi HTTP dan
    penghitung throttle-nya harus bertahan antar siklus, kalau tidak jeda
    antar-request-nya hilang dan ban per-IP tinggal menunggu waktu."""
    if _SCAN["client"] is None or _SCAN["pace"] != pace:
        key = gmgn.api_key()
        if not key:
            return None
        _SCAN["client"] = gmgn.Client(key, min_interval=pace)
        _SCAN["pace"] = pace
    return _SCAN["client"]


def _scan_state() -> "gmgn.PollState":
    if _SCAN["state"] is None:
        _SCAN["state"] = gmgn.PollState(_SCAN_STATE_FILE)
    return _SCAN["state"]


async def scanner_cycle(app, force: bool = False) -> tuple[int, int, str]:
    """Satu siklus pindai semua chain. Return (kandidat, kartu, catatan).

    `force` melewati saklar on/off (dipakai /scan), tapi TIDAK melewati
    konfirmasi polling dan cooldown — dua hal itu yang mencegah chat dibanjiri,
    dan melewatinya berarti /scan jadi tombol spam."""
    c = scanner_cfg()
    if not force and not c.get("on"):
        return 0, 0, "scanner mati"
    client = _scan_client(float(c.get("pace") or 1.0))
    if client is None:
        return 0, 0, "GMGN_API_KEY belum diisi"
    st = _scan_state()
    total = kirim = 0
    for chain in c.get("chains") or []:
        if chain not in gmgn.CHAINS:
            continue
        tokens = await asyncio.to_thread(gmgn.scan_chain, client, chain,
                                         c.get("interval") or "5m",
                                         c.get("filters") or {}, c.get("limit") or 100)
        total += len(tokens)
        st.record([f"{chain}:{t['address']}" for t in tokens], int(c.get("window") or 3), chain)
        rows = []
        for t in tokens:
            v = gmgn.classify(t)
            if v["tier"] == "SKIP" and not c.get("include_skip"):
                continue
            rows.append((t, v))
        for t, v in rows[: int(c.get("top") or 3)]:
            k = f"{chain}:{t['address']}"
            if not st.should_alert(k, int(c.get("confirm") or 1), float(c.get("cooldown") or 30)):
                continue
            entry = {**t, **v, "pollHits": st.hits(k), "pollWindow": int(c.get("window") or 3)}
            try:
                await _lp_alert(app, entry)
                kirim += 1
            except Exception as e:
                log.warning("kartu scanner %s gagal: %s", t.get("symbol"), e)
    await asyncio.to_thread(st.persist, float(c.get("cooldown") or 30))
    return total, kirim, ""


async def _scanner_loop(app):
    """Loop scanner. Penanganan error mengikuti sifat GMGN:

    - **429** → tunggu sampai `reset_at`, JANGAN retry. Bannya per-IP dan tiap
      retry menambahnya 5 detik sampai 5 menit.
    - **Kredensial ditolak** → matikan scanner dan beri tahu user. Menunggu tidak
      menyembuhkan key yang salah, dan proses yang terus mencoba cuma
      menyembunyikan masalahnya di log.
    - **Sisanya** → backoff eksponensial."""
    await asyncio.sleep(20)
    gagal = 0
    while True:
        c = scanner_cfg()
        jeda = max(30, int(c.get("watch") or 60))
        if not c.get("on"):
            await asyncio.sleep(30)
            continue
        try:
            total, kirim, catatan = await scanner_cycle(app)
            if catatan:
                if not _SCAN["warned"]:
                    _SCAN["warned"] = True
                    log.warning("scanner: %s", catatan)
                await asyncio.sleep(120)
                continue
            _SCAN["warned"] = False
            gagal = 0
            log.info("scanner: %d token lolos filter, %d kartu dikirim", total, kirim)
        except gmgn.RateLimit as e:
            tunggu = max(0, (e.reset_at or 0) - time.time()) + 1 if e.reset_at else jeda
            log.warning("scanner kena rate limit GMGN — menunggu %.0f detik tanpa retry", tunggu)
            await asyncio.sleep(tunggu)
            continue
        except gmgn.ApiError as e:
            if e.fatal:
                scanner_save({"on": False})
                log.error("scanner dimatikan: kredensial GMGN ditolak (%s)", e)
                await _notify(app, f"🛑 <b>Scanner dimatikan</b> — GMGN menolak kredensial: "
                                   f"{esc(e)}\nPerbaiki <code>GMGN_API_KEY</code> lalu "
                                   f"nyalakan lagi lewat /scanner.")
                continue
            gagal += 1
        except Exception as e:
            gagal += 1
            log.warning("siklus scanner gagal (%dx): %s", gagal, e)
        if gagal:
            await asyncio.sleep(min(jeda * 2 ** min(gagal, 5), 900))
            continue
        await asyncio.sleep(jeda)


async def _lp_inbox_loop(app):
    """Pantau inbox scanner. Tick pendek: kandidat trending cepat basi."""
    if not LP_INBOX:
        return
    log.info("jembatan alert LP aktif — memantau %s", LP_INBOX)
    await asyncio.sleep(5)
    while True:
        try:
            for e in await asyncio.to_thread(_lp_take):
                try:
                    await _lp_alert(app, e)
                except Exception as err:
                    log.warning("proses alert LP gagal: %s", err)
        except Exception as err:
            log.warning("baca inbox LP gagal: %s", err)
        await asyncio.sleep(_LP_INBOX_TICK)


async def _notify(app, body: str):
    for chat_id in allowed_chat_ids():
        try:
            await app.bot.send_message(chat_id, body, parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
        except Exception:
            pass


async def after_action(cid: int, pid: str, judul: str = "Sesudah") -> tuple[list, InlineKeyboardMarkup]:
    """(baris keadaan posisi SESUDAH aksi, keyboard dengan tombol buka kartunya).

    Kartu hasil mint/add/reduce/collect/compound/rebalance dulu berhenti di daftar
    tx — user harus membuka /list lalu mencari posisinya lagi hanya untuk tahu
    hasil aksinya seperti apa. Padahal itu justru pertanyaan pertamanya: nilainya
    jadi berapa, masih in-range atau tidak, range-nya di mana.

    Dibaca lewat `position_one` (langsung on-chain, BUKAN `_POS_CACHE` yang isinya
    masih keadaan SEBELUM aksi). Gagal baca dilewati diam-diam — kartu hasil
    transaksi tidak boleh batal cuma karena satu pembacaan tambahan."""
    def work():
        p = position_one(cid, pid)
        if not p:
            return None, ""
        try:
            return p, _pool_info_line(cid, p, p.get("ver", 3))
        except Exception:
            return p, ""

    p, pool_line = None, ""
    try:
        p, pool_line = await asyncio.to_thread(work)
    except Exception as e:
        log.warning("baca posisi sesudah aksi %s: %s", pid, e)
    if not p:
        return [], NAV_KB
    lines = [""]
    if pool_line:
        lines.append(pool_line)
    lines.append(f"{judul}: <b>{ch.fmt_usd(p['value_usd'])}</b> · fee unclaimed "
                 f"{ch.fmt_usd(p['unclaimed_usd'])} · "
                 + ("🟢 IN range" if p["in_range"] else "🔴 OUT of range"))
    lines.append(f"Range: {esc(range_str(p))}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📋 Buka {disp_pid(pid)}", callback_data=f"pos|{pid}")],
        *NAV_KB.inline_keyboard,
    ])
    return lines, kb


async def _full_pos(cid: int, p: dict) -> dict:
    """Detail PENUH sebuah posisi hasil pindai ringan.

    Pindai monitor sengaja tidak membaca fee/jumlah (`value_usd` &
    `unclaimed_usd` = 0), jadi angka itu WAJIB dibaca ulang sebelum dipakai —
    kartu alert dan terutama `record_event`, karena event PnL bernilai 0 merusak
    riwayat secara permanen. Dipanggil hanya saat alert BERBUNYI atau order
    TERPICU, dan dua-duanya jarang, jadi ongkosnya tidak masuk ke tiap putaran.

    Gagal baca mengembalikan `p` apa adanya — lebih baik satu kartu tanpa angka
    daripada alert/eksekusi yang batal."""
    if not p.get("light"):
        return p
    try:
        key = pk_for(p.get("_wallet") or "") or pk()
        full = await asyncio.to_thread(ch.position_by_pid, cid, key, p["pid"])
        if full:
            full["_wallet"] = p.get("_wallet")
            return full
    except Exception as e:
        log.warning("detail penuh %s: %s", p.get("pid"), e)
    return p


async def _emit_range_alerts(app, cid: int, positions: list[dict]):
    for p in positions:
        if p.get("ver") == 2:
            continue  # v2 full-range, tidak pernah out of range
        key = (cid, p["pid"])
        now_in = p["in_range"]
        prev = RANGE_STATE.get(key)
        RANGE_STATE[key] = now_in
        if prev is None or prev == now_in:
            continue  # baseline pertama / tidak berubah
        p = await _full_pos(cid, p)   # transisi = jarang, di sini baru bayar penuh
        meme_sym = p["sym0"] if p["quote_is_token1"] else p["sym1"]
        if now_in:
            head = f"🟢 <b>{esc(meme_sym)} {_pos_disp(p)} MASUK range</b> — fee mulai mengalir."
        else:
            if p.get("mc_now") and p.get("mc_lower") and p["mc_now"] < p["mc_lower"]:
                arah = f"tembus ke BAWAH — posisi jadi penuh {esc(meme_sym)}"
            else:
                arah = f"keluar ke ATAS — posisi jadi penuh {esc(p['quote_sym'] or 'quote')}"
            head = f"🔴 <b>{esc(meme_sym)} {_pos_disp(p)} KELUAR range</b> — {arah}. Fee berhenti."
        body = (f"{head}\n"
                f"Val {ch.fmt_usd(p['value_usd'])} · Unclaimed {ch.fmt_usd(p['unclaimed_usd'])}\n"
                f"Range: {esc(range_str(p))}")
        meme_ca = p["token0"] if p["quote_is_token1"] else p["token1"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Detail", callback_data=f"pos|{p['pid']}"),
             InlineKeyboardButton("⚖️ Rebalance", callback_data=f"reb|{p['pid']}"),
             InlineKeyboardButton("🗑 Close", callback_data=f"close|{p['pid']}"), DEL_BTN],
            chart_buttons(cid, p["pool"], meme_ca),
        ])
        for chat_id in allowed_chat_ids():
            try:
                await app.bot.send_message(chat_id, body, parse_mode=ParseMode.HTML,
                                           reply_markup=kb, disable_web_page_preview=True)
            except Exception:
                pass


async def _check_orders(app, cid: int, active_orders: list[dict], by_wallet: dict):
    """Cek tiap order aktif vs MC posisi. by_wallet[addr] = {pid: pos} atau None (fetch gagal)."""
    for o in active_orders:
        live = by_wallet.get(o.get("wallet", "").lower())
        if live is None:
            continue  # fetch wallet ini gagal / wallet tak ada → skip aman, coba lagi nanti
        p = live.get(str(o["pid"]))
        if p is None:
            # Wallet kosong TOTAL = ambigu (mungkin fetch transient) → biarkan aktif, coba lagi.
            # Cap "done" hanya kalau wallet masih punya posisi LAIN → bukti fetch sukses &
            # posisi order ini memang sudah ditutup manual. Cegah order valid tak terlindungi.
            if not live:
                continue
            store.update_order(cid, o["id"], status="done",
                               reason="posisi sudah tidak ada", triggered=int(time.time()))
            await _notify(app, f"🎯 Pesanan <code>{o['id']}</code> {disp_pid(o['pid'])} "
                               f"dihapus otomatis — posisi sudah tidak ada.")
            continue
        mc = p.get("mc_now")
        if not mc:
            continue
        hit = None
        if o.get("tp_mc") and mc >= o["tp_mc"]:
            hit = ("TP", o["tp_mc"], "≥")
        elif o.get("sl_mc") and mc <= o["sl_mc"]:
            hit = ("SL", o["sl_mc"], "≤")
        if hit:
            await _trigger_order(app, cid, o, p, hit, mc)


async def _trigger_order(app, cid: int, o: dict, p: dict, hit: tuple, mc: float):
    kind, level, op = hit
    # Keputusan trigger memakai `mc` dari pindai ringan (rumusnya identik dengan
    # jalur penuh), tapi `value_usd`/`unclaimed_usd` di bawah masuk ke
    # `record_event` — dan event bernilai 0 merusak riwayat PnL permanen.
    p = await _full_pos(cid, p)
    # KUNCI ANTI DOUBLE-TRIGGER: tandai done SEBELUM eksekusi. Kalau close lambat,
    # iterasi loop berikutnya tidak akan melihat order ini sebagai active lagi.
    store.update_order(cid, o["id"], status="done",
                       reason=f"{kind} @ MC {ch.fmt_usd(mc)}", triggered=int(time.time()))
    waddr = o.get("wallet", "")
    key = pk_for(waddr)
    if not key:
        store.update_order(cid, o["id"], status="error", reason="wallet tidak tersedia di .env")
        await _notify(app, f"⚠️ Pesanan <code>{o['id']}</code> gagal: wallet "
                           f"<code>{esc(waddr)}</code> tidak ada di .env.")
        return
    slip = float(o.get("slippage") or store.load_settings()["slippage_pct"])
    autoswap = bool(o.get("autoswap"))
    meme_sym = o.get("meme_sym", "")
    await _notify(app, (f"🎯 <b>TRIGGER {kind}</b> {esc(meme_sym)} {disp_pid(o['pid'])} — "
                        f"MC {ch.fmt_usd(mc)} {op} {ch.fmt_usd(level)}\n⏳ Auto-close posisi..."))
    ver, ref = ch.parse_pid(o["pid"])
    async with TX_LOCK:
        try:
            r = await asyncio.to_thread(ch.close_any, cid, key, o["pid"], slip, autoswap)
        except Exception as e:
            store.update_order(cid, o["id"], status="error", reason=str(e)[:200])
            await _notify(app, f"⚠️ <b>Order {o['id']} close GAGAL</b>: {esc(str(e)[:300])}\n"
                               f"Posisi TIDAK ditutup — cek manual di 📊 Posisi.")
            return
    # catat event PnL (mirror do_close) supaya riwayat konsisten
    ev_tid = ref if ver == 3 else str(o["pid"])
    if ver == 4:
        pos_cache_drop(cid)
        store.drop_ref(cid, waddr, "v4", str(ref))
    elif ver == 2:
        store.drop_ref(cid, waddr, "v2", str(ref))
        store.drop_v2_basis(cid, waddr, str(ref))
    store.record_event(cid, "close", ev_tid, p.get("value_usd", 0.0), wallet=waddr)
    if p.get("unclaimed_usd", 0) > 0:
        store.record_event(cid, "fees", ev_tid, p["unclaimed_usd"], wallet=waddr)
    if r.get("steps"):
        store.update_order(cid, o["id"], tx=r["steps"][0][1])
    lines = [f"✅ <b>Order {o['id']} eksekusi</b> — {kind} {esc(meme_sym)} {disp_pid(o['pid'])}",
             f"Close pada MC {ch.fmt_usd(mc)} · withdraw "
             f"~{ch.fmt_usd(p.get('value_usd', 0) + p.get('unclaimed_usd', 0))}"]
    for label, h in r.get("steps", []):
        lines.append(f"{label}: {ch.tx_link(cid, h)}")
    for sym, h in r.get("swaps", []):
        if str(h).startswith("0x"):
            lines.append(f"swap {esc(sym)} → {esc(ch.CHAINS[cid]['wrapped_symbol'])}: "
                         f"{ch.tx_link(cid, h)}")
    await _notify(app, "\n".join(lines))


async def _gather_positions(cid: int, only_wallets: set | None = None):
    """Ambil posisi wallet di satu chain. Return (positions, by_wallet).
    by_wallet[addr] = {pid: pos} atau None kalau fetch wallet itu gagal.

    `only_wallets`: batasi ke alamat tertentu (lowercase). Satu pindai wallet
    terukur **199 request RPC** untuk 16 posisi (12,4 per posisi), jadi memindai
    wallet yang tidak punya kepentingan di chain itu langsung menggandakan tagihan.
    """
    positions = []
    by_wallet = {}
    for key in all_pks():
        waddr = _addr_of(key).lower()
        if only_wallets is not None and waddr not in only_wallets:
            continue
        try:
            # Pindai RINGAN: monitor cuma butuh in_range/mc_now/mc_lower, dan
            # versi ringan menghitungnya dengan rumus yang persis sama (terukur
            # mc_now identik sampai desimal terakhir) dengan 11 request jadi 5.
            # value_usd/unclaimed_usd-nya 0 — siapa pun yang butuh angka itu
            # membaca detail penuh lewat `_full_pos()`.
            pk_pos = await asyncio.to_thread(list_positions_all, cid, key, None, True, True)
            for _p in pk_pos:
                _p["_wallet"] = waddr
        except Exception as e:
            log.warning("monitor posisi %s/%s: %s", cid, waddr, e)
            by_wallet[waddr] = None  # fetch gagal → JANGAN anggap posisi hilang
            continue
        by_wallet[waddr] = {p["pid"]: p for p in pk_pos}
        positions += pk_pos
    return positions, by_wallet


_MONITOR_TICK = 15          # detik antar pemeriksaan "chain mana yang jatuh tempo"
_LAST_SCAN: dict = {}       # chain -> kapan terakhir dipindai monitor
_LOOP_LAG = [0.0]   # lag event loop terakhir (detik), diisi _loop_watchdog


async def _loop_watchdog():
    """Ukur seberapa telat event loop bangun dari sleep 1 detik.

    Lag mendekati 0 = loop sehat, lambatnya murni dari kerja RPC. Lag beberapa
    detik = ada panggilan blocking yang tidak dibungkus `asyncio.to_thread`, dan
    itu menahan SEMUA hal termasuk menjawab query callback (gejalanya "Query is
    too old"). Tanpa angka ini keduanya terlihat sama dari luar."""
    while True:
        t = time.monotonic()
        await asyncio.sleep(1)
        lag = time.monotonic() - t - 1
        _LOOP_LAG[0] = max(0.0, lag)
        if lag > 2:
            log.warning("event loop tertahan %.1f detik", lag)


async def monitor_loop(app):
    """Cek berkala: alert in/out range (chain aktif) + eksekusi order TP/SL.
    Order dicek di SEMUA chain yang punya pesanan aktif — jadi TP/SL tetap jalan
    walau bot lagi di chain lain. Iramanya `max(alert_secs, order_secs)`, bukan 30
    detik mati: tiap pindai wallet terukur 199 request RPC, jadi interval loop ini
    yang paling menentukan tagihan CU."""
    await asyncio.sleep(15)  # kasih waktu bot siap
    while True:
        s = store.load_settings()
        active_cid = s["chain"]
        alert_on = int(s.get("alert_secs", 60) or 0) > 0
        order_chains = [c for c in ch.CHAINS if _orders_for_chain(c, "active")]
        chains = set(order_chains)
        if alert_on:
            chains.add(active_cid)
        if not chains:
            await asyncio.sleep(60)
            continue
        # Interval sekarang PER CHAIN, jadi tiap chain dipindai sesuai setelannya
        # sendiri — kalau tidak, setelan "600 detik" di satu chain diam-diam tidak
        # berlaku karena loop memakai angka chain aktif untuk semuanya.
        now = time.time()
        due = []
        for cid in chains:
            sc = store.load_settings(cid)
            iv = max(30, int(sc.get("order_secs", 120) or 120) if cid in order_chains else 0,
                     int(sc.get("alert_secs", 60) or 0) if (alert_on and cid == active_cid) else 0)
            if now - _LAST_SCAN.get(cid, 0) >= iv:
                _LAST_SCAN[cid] = now
                due.append(cid)
        for cid in due:
            try:
                # Wallet yang dipindai dibatasi: alert cuma untuk chain aktif (semua
                # wallet), sedangkan pengecekan order cuma butuh wallet pemilik order.
                need = None
                if not (alert_on and cid == active_cid):
                    need = {str(o.get("wallet", "")).lower()
                            for o in _orders_for_chain(cid, "active")}
                    need.discard("")
                    if not need:
                        continue
                positions, by_wallet = await _gather_positions(cid, need)
                if alert_on and cid == active_cid:
                    await _emit_range_alerts(app, cid, positions)
                active_orders = _orders_for_chain(cid, "active")
                if active_orders:
                    await _check_orders(app, cid, active_orders, by_wallet)
                # posisi yang sudah ditutup → buang dari state alert. HANYA saat
                # pindai penuh: kalau cuma sebagian wallet dibaca, `live` tidak
                # lengkap dan entri wallet lain ikut terbuang (transisi range
                # berikutnya jadi hilang karena dianggap baseline baru).
                if need is None:
                    live = {(cid, p["pid"]) for p in positions}
                    for k in [k for k in RANGE_STATE if k[0] == cid and k not in live]:
                        RANGE_STATE.pop(k, None)
            except Exception as e:
                log.warning("monitor %s: %s", cid, e)
        # Dulu `30 if order_chains else ...` — adanya SATU order aktif memaksa pindai
        # tiap 30 detik selamanya, mengabaikan setelan user. Terukur: 2 wallet tiap
        # 30 detik = 1,15 juta request/hari (~30M CU), yaitu seluruh kuota bulanan
        # Alchemy dalam satu hari, dan throughput-nya menembus batas sehingga muncul
        # 429 yang membuat posisi hilang dari /list.
        # Tick pendek + jatuh tempo per chain di atas: yang menentukan tagihan RPC
        # tetap interval tiap chain, bukan panjang tidur ini. Tick yang tidak ada
        # chain jatuh tempo tidak menembak satu pun request.
        await asyncio.sleep(_MONITOR_TICK)


async def post_init(app):
    # daftar command → muncul di menu Telegram saat user ketik "/"
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Menu utama (dashboard saldo)"),
            BotCommand("list", "Posisi LP + PnL + chart/close"),
            BotCommand("orders", "Pesanan TP/SL (auto-close di market cap)"),
            BotCommand("wallet", "Saldo semua token + nilai USD"),
            BotCommand("wallets", "Kelola wallet: impor/buat/ekspor/hapus"),
            BotCommand("settings", "Pengaturan via tombol"),
            BotCommand("presets", "Tombol jumlah tetap di kartu mint"),
            BotCommand("rpc", "Status RPC + key Alchemy yang habis jatah"),
            BotCommand("scan", "Pindai token trending sekarang"),
            BotCommand("scanner", "Setelan scanner token trending"),
            BotCommand("chain", "Ganti chain aktif"),
            BotCommand("revoke", "Cabut approval token yang menganggur"),
            BotCommand("cleanup", "Burn NFT posisi kosong (mempercepat /list)"),
            BotCommand("recover", "Pulihkan posisi v4 yang hilang dari daftar"),
            BotCommand("all", "Ringkasan posisi di semua chain"),
            BotCommand("help", "Bantuan & daftar perintah"),
        ])
    except Exception as e:
        log.warning("set_my_commands gagal: %s", e)
    # Dijadwalkan lewat job queue, bukan create_task langsung: task yang dibuat
    # saat aplikasi BELUM jalan tidak ikut di-await PTB (PTBUserWarning), jadi
    # error di dalamnya bisa hilang diam-diam.
    # Executor default `asyncio.to_thread` = min(32, cpu+4); di VPS 2 core cuma 6
    # worker, jadi pembacaan posisi milik monitor_loop dan klik user berebut slot
    # dan yang kalah MENUNGGU giliran — terlihat persis seperti RPC lambat.
    # Semuanya kerja I/O (nunggu jaringan), jadi jumlah worker tidak perlu ikut core.
    try:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=32, thread_name_prefix="unipool"))
    except Exception as e:
        log.warning("set executor gagal: %s", e)
    _BG.append(asyncio.create_task(_start_background(app)))


_BG: list = []   # pegang referensi task bootstrap — asyncio tidak menahannya sendiri


async def _start_background(app):
    """Daftarkan task latar SETELAH aplikasi jalan.

    `app.create_task()` di dalam `post_init` memberi PTBUserWarning "Tasks created
    while the application is not running won't be automatically awaited" — task-nya
    tetap jalan, tapi tidak ikut di-await sehingga error di dalamnya hilang
    diam-diam. Job queue akan menyelesaikannya juga, tapi butuh extra
    `python-telegram-bot[job-queue]` (APScheduler) yang belum tentu terpasang; di
    VPS memang tidak ada, dan cabang cadangannya memunculkan warning yang sama.
    Menunggu `app.running` tidak butuh dependensi apa pun."""
    for _ in range(600):                 # maks ~60 detik, lalu jalan apa adanya
        if getattr(app, "running", False):
            break
        await asyncio.sleep(0.1)
    app.create_task(monitor_loop(app))
    _APP[0] = app
    app.create_task(_lp_inbox_loop(app))
    app.create_task(_scanner_loop(app))
    app.create_task(_loop_watchdog())


async def cmd_cleanup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Burn NFT posisi v3 yang benar-benar kosong.

    Close tidak mem-burn NFT-nya, jadi sisanya menumpuk dan setiap refresh daftar
    posisi membayar satu `positions()` per NFT (terukur 127 NFT untuk 1 posisi hidup).
    Aman: `burn` di NPM me-require liquidity DAN tokensOwed dua-duanya 0 — posisi
    yang masih berisi ditolak kontraknya sendiri ("Not cleared")."""
    if not authorized(update):
        return
    cid = store.load_settings()["chain"]
    status = await reply(update, "🔎 Menghitung NFT posisi kosong…")
    try:
        ids = await asyncio.to_thread(ch.empty_position_ids, cid, pk())
    except Exception as e:
        await edit(status, f"❌ Gagal membaca: {esc(e)}")
        return
    if not ids:
        await edit(status, "✅ Tidak ada NFT kosong — sudah bersih.", NAV_KB)
        return
    await edit(status, (
        f"🧹 <b>{len(ids)} NFT posisi kosong</b> ditemukan (likuiditas 0, fee 0).\n"
        f"<i>Membakarnya mempercepat semua refresh daftar posisi. Posisi yang masih "
        f"berisi tidak bisa ikut terbakar — kontraknya menolak.</i>"),
        InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔥 Burn {min(len(ids), 200)} NFT", callback_data="cleanupok")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]))


async def do_cleanup(update: Update):
    cid = store.load_settings()["chain"]
    head = "🔥 Membakar NFT kosong…"
    status = await reply(update, head)
    async with TX_LOCK:
        try:
            r = await with_progress(status, head, lambda: ch.burn_empty(cid, pk()))
        except Exception as e:
            await edit(status, f"❌ Cleanup gagal: {esc(e)}")
            return
    lines = [f"✅ <b>{r['burned']} NFT kosong dibakar</b> (dari {r['total']})"]
    if r["sisa"]:
        lines.append(f"<i>Sisa {r['sisa']} — jalankan /cleanup lagi.</i>")
    for label, h in r["steps"]:
        lines.append(f"{label}: {ch.tx_link(cid, h)}")
    g = await asyncio.to_thread(gas_line, cid)
    if g:
        lines.append(g)
    await edit(status, "\n".join(lines), NAV_KB)


# Hasil scan approval per chat, supaya tombol tidak perlu membawa alamat panjang
# (callback_data Telegram dibatasi 64 byte).
REVOKES: dict[str, dict] = {}


def _revoke_line(i: int, r: dict) -> str:
    amt = ("<b>TAK TERBATAS</b>" if r["unlimited"]
           else f"{r['amount'] / 10 ** r['decimals']:,.4f}")
    tag = "🔑 Permit2" if r["kind"] == "permit2" else "📝 ERC20"
    return f"{i}. {tag} · <b>{esc(r['symbol'])}</b> → {esc(r['spender_label'])}\n     jumlah {amt}"


async def do_claim_all(update: Update):
    """Klaim fee SEMUA posisi yang punya unclaimed. Satu tx per posisi (fee ada di
    kontrak masing-masing, tidak bisa dibatch)."""
    s = store.load_settings()
    cid = s["chain"]
    status = await reply(update, "🔎 Mencari posisi ber-fee…")

    def snap():
        return [p for p in list_positions_all(cid)
                if p.get("unclaimed_usd", 0) > 0 and p.get("ver") != 2]

    try:
        target = await asyncio.to_thread(snap)
    except Exception as e:
        await edit(status, f"❌ Gagal membaca posisi: {esc(e)}")
        return
    if not target:
        await edit(status, "ℹ️ Tidak ada fee yang bisa diklaim.", NAV_KB)
        return
    total = sum(p["unclaimed_usd"] for p in target)
    head = f"⏳ Klaim fee {len(target)} posisi ({ch.fmt_usd(total)})…"
    await edit(status, head)

    def work():
        ok, gagal = [], []
        for p in target:
            try:
                ok.append((p, ch.collect_any(cid, pk(), p["pid"])))
            except Exception as e:
                gagal.append((p, str(e)[:80]))
        return ok, gagal

    async with TX_LOCK:
        try:
            ok, gagal = await with_progress(status, head, work)
        except Exception as e:
            await edit(status, f"❌ Claim gagal: {esc(e)}")
            return
    klaim = 0.0
    for p, _r in ok:
        klaim += p["unclaimed_usd"]
        ev = ch.parse_pid(p["pid"])[1] if str(p["pid"]).isdigit() else str(p["pid"])
        store.record_event(cid, "fees", ev, p["unclaimed_usd"], "claim all",
                           wallet=wallet_address())
    lines = [f"✅ <b>Fee {ch.fmt_usd(klaim)} diklaim</b> dari {len(ok)} posisi"]
    for p, r in ok:
        h = (r.get("steps") or [("collect", "")])[-1][1]
        lines.append(f"· {esc(p['sym0'])}/{esc(p['sym1'])} {_pos_disp(p)}: "
                     + (ch.tx_link(cid, h) if h else "ok"))
    for p, err in gagal:
        lines.append(f"❌ {_pos_disp(p)}: {esc(err)}")
    g = await asyncio.to_thread(gas_line, cid)
    if g:
        lines.append(g)
    await edit(status, "\n".join(lines), NAV_KB)


async def cmd_all(update: Update, _=None):
    """Ringkasan posisi di SEMUA chain, bukan cuma yang aktif.

    /list sengaja tetap per-chain (detail + tombol aksi butuh chain aktif); ini
    pelengkapnya supaya tidak perlu ganti chain satu per satu untuk tahu di mana
    dana tersebar."""
    if not authorized(update):
        return
    status = await reply(update, "🌐 Membaca posisi di semua chain…")

    def scan():
        out = []
        for cid in ch.CHAINS:
            try:
                out.append((cid, list_positions_all(cid), None))
            except Exception as e:
                out.append((cid, None, str(e)[:60]))
        return out

    rows = await asyncio.to_thread(scan)
    lines, total_v, total_f = [], 0.0, 0.0
    for cid, pos, err in rows:
        nama = esc(ch.CHAINS[cid]["name"])
        if err is not None:
            lines.append(f"· <b>{nama}</b> — gagal dibaca: {esc(err)}")
            continue
        v = sum(p["value_usd"] for p in pos)
        f = sum(p["unclaimed_usd"] for p in pos)
        total_v += v
        total_f += f
        if not pos:
            lines.append(f"· <b>{nama}</b> — tidak ada posisi")
            continue
        lines.append(f"· <b>{nama}</b> — {len(pos)} posisi · {ch.fmt_usd(v)} "
                     f"(fee {ch.fmt_usd(f)})")
        for p in sorted(pos, key=lambda x: -x["value_usd"])[:5]:
            m = "🟢" if p["in_range"] else "🔴"
            sym = p["sym0"] if p["quote_is_token1"] else p["sym1"]
            lines.append(f"    {m} {esc(sym)} {_pos_disp(p)} · {ch.fmt_usd(p['value_usd'])}")
        if len(pos) > 5:
            lines.append(f"    <i>… +{len(pos) - 5} lagi</i>")
    head = (f"🌐 <b>Semua chain</b> · {wallet_label()}\n"
            f"Total posisi <b>{ch.fmt_usd(total_v)}</b> · fee belum diklaim "
            f"{ch.fmt_usd(total_f)}\n"
            f"<i>Ganti chain lewat ⛓ Chain untuk aksi (add/close/rebalance).</i>\n")
    await edit(status, head + "\n".join(lines), NAV_KB)


async def cmd_recover(update: Update, _=None):
    """Pulihkan posisi v4 yang ada on-chain tapi hilang dari registry bot.

    v4 tidak bisa dienumerasi, jadi bot bergantung `history.json`. Kalau mint sukses
    tapi dilaporkan gagal, ref-nya tidak pernah tercatat dan posisinya lenyap dari
    /list padahal dananya utuh. Ini membacanya kembali dari event Transfer on-chain."""
    if not authorized(update):
        return
    cid = store.load_settings()["chain"]
    status = await reply(update, f"🔎 Memindai posisi v4 on-chain di "
                                 f"{esc(ch.CHAINS[cid]['name'])}…")
    try:
        tids = await asyncio.to_thread(ch.find_v4_positions, cid, pk())
    except Exception as e:
        await edit(status, f"❌ Gagal memindai: {esc(e)}")
        return
    w = wallet_address()
    known = {str(x).lower() for x in store.refs(cid, w, "v4")}

    def scan():
        out = []
        for t in tids:
            try:
                out.append((t, ch._v4_position_detail(ch.get_w3(cid), cid, int(t), w)))
            except Exception:
                out.append((t, None))
        return out

    rows = await asyncio.to_thread(scan)
    baru, kosong = [], 0
    for t, d in rows:
        if not d:
            kosong += 1
            continue
        if str(t).lower() in known:
            continue
        store.add_ref(cid, w, "v4", str(t))
        pos_cache_drop(cid)
        baru.append((t, d))
    if not baru:
        await edit(status, (f"✅ Tidak ada posisi yang hilang — {len(tids)} NFT v4 "
                            f"diperiksa ({kosong} sudah kosong)."), NAV_KB)
        return
    lines = [f"🩹 <b>{len(baru)} posisi dipulihkan</b> ke daftar:"]
    for t, d in baru:
        lines.append(f"· v4:{t} {esc(d['sym0'])}/{esc(d['sym1'])} — "
                     f"{ch.fmt_usd(d['value_usd'])} (fee {ch.fmt_usd(d['unclaimed_usd'])})")
    lines.append("<i>Buka /list untuk melihatnya.</i>")
    await edit(status, "\n".join(lines), NAV_KB)


async def cmd_revoke(update: Update, ctx=None):
    """Daftar approval aktif + tombol mencabutnya.

    Bot memberi approval TAK TERBATAS ke router/NPM saat mint & swap (standar, biar
    tidak bayar gas approve tiap transaksi). Selama approval itu hidup, kontrak
    tersebut bisa memindahkan token itu kapan saja — jadi mencabutnya setelah
    selesai LP itu kebersihan yang wajar."""
    if not authorized(update):
        return
    s = store.load_settings()
    cid = s["chain"]
    status = await reply(update, f"🔎 Memindai approval di {esc(ch.CHAINS[cid]['name'])}…")
    # /revoke <alamat> — periksa kontrak di luar daftar bot (mis. yang kamu lihat
    # di Rabby tapi tidak pernah dipakai bot ini)
    extra = [a for a in (getattr(ctx, "args", None) or []) if ADDR_RE.fullmatch(a.strip())]
    try:
        rows = await asyncio.to_thread(ch.scan_approvals, cid, pk(), None, extra)
    except Exception as e:
        await edit(status, f"❌ Gagal memindai: {esc(e)}")
        return
    if not rows:
        await edit(status, (f"✅ Tidak ada approval aktif di "
                            f"{esc(ch.CHAINS[cid]['name'])} untuk {wallet_label()}."), NAV_KB)
        return
    # Approval yang dikunci kontrak tokennya tidak bisa dicabut — pisahkan supaya
    # tidak ada tombol yang dijamin gagal.
    fixed = [r for r in rows if r.get("fixed")]
    rows = [r for r in rows if not r.get("fixed")]
    if not rows:
        note = (f"\n\n<i>{len(fixed)} allowance Permit2 ({', '.join(esc(r['symbol']) for r in fixed[:6])}) "
                f"dikunci di tak terhingga oleh kontrak tokennya sendiri — bukan approval "
                f"yang kamu berikan, dan tidak bisa dicabut.</i>" if fixed else "")
        await edit(status, (f"✅ Tidak ada approval yang bisa dicabut di "
                            f"{esc(ch.CHAINS[cid]['name'])} untuk {wallet_label()}.{note}"), NAV_KB)
        return
    key = uuid.uuid4().hex[:8]
    REVOKES[key] = {"chain": cid, "rows": rows}
    body = "\n".join(_revoke_line(i, r) for i, r in enumerate(rows[:10], 1))
    btns = [[InlineKeyboardButton(f"{i}. {r['symbol']} → {r['spender_label'][:22]}",
                                  callback_data=f"rvk|{key}|{i - 1}")]
            for i, r in enumerate(rows[:10], 1)]
    btns.append([InlineKeyboardButton(f"🧹 Cabut SEMUA ({len(rows)})", callback_data=f"rvkall|{key}")])
    btns.append([InlineKeyboardButton("✖ Cancel", callback_data="cancel")])
    await edit(status, (
        f"🔐 <b>{len(rows)} approval aktif</b> · {wallet_label()} · "
        f"{esc(ch.CHAINS[cid]['name'])}\n\n{body}\n\n"
        f"<i>Tip: <code>/revoke 0xKontrak</code> untuk memeriksa kontrak di luar daftar bot "
        f"(mis. yang muncul di Rabby).</i>\n"
        f"<i>Approval TAK TERBATAS artinya kontrak itu boleh memindahkan token tersebut "
        f"kapan saja tanpa persetujuan lagi. Mencabutnya aman — bot akan minta approval "
        f"lagi sendiri saat kamu mint/swap berikutnya.</i>"
        + (f"\n<i>🔒 {len(fixed)} allowance Permit2 dilewati "
           f"({', '.join(esc(r['symbol']) for r in fixed[:6])}) — dikunci di tak terhingga "
           f"oleh kontrak tokennya, bukan approval yang kamu berikan, dan mustahil dicabut.</i>"
           if fixed else "")), InlineKeyboardMarkup(btns))


# Posisi asal saat alur "pindah pool" berjalan, per chat. Dipakai show_confirm untuk
# tahu bahwa pool yang dipilih adalah TUJUAN pindah, bukan mint baru.
MIGRATE: dict[int, str] = {}


async def ask_migrate(update: Update, pid: str):
    """Mulai alur pindah pool: tampilkan daftar pool token yang sama."""
    s = store.load_settings()
    cid = s["chain"]
    if ch.parse_pid(pid)[0] == 2:
        await reply(update, "ℹ️ Posisi v2 full-range — tidak ada fee tier lain untuk "
                            "dipindahi.", NAV_KB)
        return
    status = await reply(update, "⏳ Membaca posisi…")

    def snap():
        return position_one(cid, pid)

    p = await asyncio.to_thread(snap)
    if not p:
        await edit(status, "❌ Posisi tidak ditemukan.")
        return
    meme = p["token0"] if p["quote_is_token1"] else p["token1"]
    MIGRATE[update.effective_chat.id] = str(pid)
    await edit(status, (
        f"🔀 <b>Pindah pool</b> — {_pos_disp(p)} ({ch.fmt_usd(p['value_usd'])})\n"
        f"<i>Dari {esc(p.get('quote_sym') or '')} fee {p.get('fee', 0) / 10000:g}%. "
        f"Pilih pool tujuan di bawah — harus ber-quote sama. Alurnya: close posisi "
        f"lama → swap komposisi → mint di pool baru.</i>"))
    await show_pools_for(status, cid, meme)


async def show_migrate_confirm(msg, key: str, src_pid: str):
    """Kartu konfirmasi pindah pool: pool asal vs tujuan + pilihan mode range."""
    ctx = PENDING.get(key)
    if not ctx:
        await edit(msg, "⚠️ Tombol kadaluarsa (bot sempat restart). Ulangi dari posisi.")
        return
    cid = ctx["chain"]
    dest = ctx["pool_info"]

    def snap():
        # fresh: snapshot ini jadi dasar pindah dana, bukan sekadar tampilan
        return next((x for x in list_positions_all(cid, fresh=True) if x["pid"] == str(src_pid)), None)

    p = await asyncio.to_thread(snap)
    if not p:
        await edit(msg, "❌ Posisi asal tidak ditemukan.")
        return
    src_q = (p["token1"] if p["quote_is_token1"] else p["token0"]).lower()
    cross = str(dest.get("quote_addr", "")).lower() != src_q
    # Token meme HARUS sama — kalau tidak, ini bukan pindah pool melainkan tukar aset.
    src_meme = (p["token0"] if p["quote_is_token1"] else p["token1"]).lower()
    # quote_is_token1 True  -> quote = token1, jadi MEME = token0
    # quote_is_token1 False -> quote = token0, jadi MEME = token1
    dest_meme = str(dest.get("token0") if dest.get("quote_is_token1")
                    else dest.get("token1")).lower()
    if dest_meme and dest_meme != src_meme:
        await edit(msg, "❌ Pool tujuan bukan untuk token yang sama.", NAV_KB)
        return
    # "Sama" = pertahankan rentang MARKET CAP posisi lama. Tick tidak bisa disalin
    # mentah antar pool (skala harga beda kalau quote beda, kisi beda kalau fee beda),
    # jadi batasnya dikonversi lewat harga USD lalu dibulatkan KE LUAR ke kisi tujuan.
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Sama (pertahankan MC range)", callback_data=f"migok|{key}|same")],
        [InlineKeyboardButton("↔️ Wide (dua sisi)", callback_data=f"migok|{key}|wide")],
        [InlineKeyboardButton("⬇️ Lower (quote saja)", callback_data=f"migok|{key}|lower"),
         InlineKeyboardButton("⬆️ Upper (meme saja)", callback_data=f"migok|{key}|upper")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    await edit(msg, (
        f"🔀 <b>Pindah pool?</b>\n\n"
        f"<b>Dari</b> {_pos_disp(p)} · {esc(p.get('quote_sym'))} "
        f"fee {p.get('fee', 0) / 10000:g}% · {ch.fmt_usd(p['value_usd'])}\n"
        f"<b>Ke</b> [v{dest.get('ver', 3)}] {esc(dest.get('quote_sym'))} "
        f"fee {dest['fee'] / 10000:g}% · TVL {ch.fmt_usd(dest.get('tvl_usd') or 0)}\n\n"
        f"Close posisi lama (fee ikut terambil) → swap komposisi"
        + (f" → tukar {esc(p.get('quote_sym'))} ke {esc(dest.get('quote_sym'))}" if cross else "")
        + f" → mint di pool baru.\n"
        + (f"<i>⚠️ Quote BEDA — hasil close ditukar dulu, jadi ada fee &amp; slippage "
           f"swap tambahan dan totalnya 4–6 transaksi.</i>\n" if cross else "")
        + f"<i>Hanya dana hasil posisi ini yang dipakai.</i>\n\n"
        + (f"🎯 <b>Sama</b> — pertahankan rentang MC {ch.fmt_usd(p.get('mc_lower') or 0)}–"
           f"{ch.fmt_usd(p.get('mc_upper') or 0)}. Batasnya menempel kisi pool tujuan "
           f"({(dest.get('tick_spacing') or 60)} tick), jadi bisa melebar sedikit — "
           f"tidak pernah menyempit.\n" if p.get("mc_lower") and p.get("mc_upper") else "")
        + f"<i>Tiga pilihan lain memakai LEBAR range lama tapi dipusatkan di harga "
          f"sekarang. Pilih:</i>"), kb)


async def do_migrate(update: Update, key: str, mode: str):
    ctx = PENDING.get(key)
    src_pid = MIGRATE.pop(update.effective_chat.id, None)
    if not ctx or not src_pid:
        await reply(update, "⚠️ Konteks hilang. Ulangi dari kartu posisi.")
        return
    s = store.load_settings()
    cid = ctx["chain"]
    dest = ctx["pool_info"]
    head = f"⏳ Pindah {disp_pid(src_pid)} → [v{dest.get('ver', 3)}] fee {dest['fee'] / 10000:g}%…"
    status = await reply(update, head)

    def snap():
        # fresh: snapshot ini jadi dasar pindah dana, bukan sekadar tampilan
        return next((x for x in list_positions_all(cid, fresh=True) if x["pid"] == str(src_pid)), None)

    pos = await asyncio.to_thread(snap)
    async with TX_LOCK:
        try:
            r = await with_progress(status, head, lambda: ch.rebalance_position(
                cid, pk(), src_pid, mode, s["slippage_pct"], int(s.get("gap", 1)),
                target_pool=dest))
        except Exception as e:
            if isinstance(e, ch.AlreadyClosed):
                await edit(status, f"✅ {esc(e)}", NAV_KB)
                return
            await edit(status, f"❌ Pindah pool gagal: {esc(e)}\n"
                               f"<i>Kalau close sudah jalan, dananya aman di wallet — "
                               f"cek /wallet lalu mint manual.</i>")
            return
    await finish_rebalance(update, status, cid, src_pid, pos, r, label="Pindah pool")


async def ask_compound(update: Update, pid: str):
    """Konfirmasi compound: reinvestasi fee unclaimed ke posisi yang sama."""
    s = store.load_settings()
    cid = s["chain"]
    ver = ch.parse_pid(pid)[0]
    if ver == 2:
        await reply(update, "ℹ️ Fee LP v2 sudah auto-compound ke dalam posisi — "
                            "tidak ada yang perlu di-compound.", NAV_KB)
        return
    msg = await reply(update, "⏳ Menghitung fee…")

    def snap():
        return position_one(cid, pid)

    p = await asyncio.to_thread(snap)
    if not p or p["unclaimed_usd"] <= 0:
        await edit(msg, "ℹ️ Tidak ada fee unclaimed untuk di-compound.", NAV_KB)
        return
    # v3 vs v4 beda jumlah tx dan beda sumber dana — sebutkan supaya user tahu
    detail = ("Fee dipakai langsung sebagai modal (v4 mengkreditkannya ke tagihan "
              "settle), jadi wallet praktis tidak membayar apa-apa. 1–2 transaksi."
              if ver == 4 else
              "Fee di-collect ke wallet dulu, lalu ditambahkan kembali ke posisi. "
              "2–4 transaksi.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"♻️ Compound {ch.fmt_usd(p['unclaimed_usd'])}",
                              callback_data=f"cmpok|{pid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    # pool_stats di dalamnya menembak StateView + dexscreener — jangan di event loop.
    pool_line = await asyncio.to_thread(_pool_info_line, cid, p, ver)
    await edit(msg, (
        f"♻️ <b>Compound {_pos_disp(p)}?</b>\n\n"
        f"{pool_line}\n"
        f"💼 Posisi sekarang <b>{ch.fmt_usd(p['value_usd'])}</b> · "
        f"{'🟢 IN range' if p['in_range'] else '🔴 OUT of range'}\n"
        f"💰 Fee unclaimed <b>{ch.fmt_usd(p['unclaimed_usd'])}</b>\n"
        f"· {ch.fmt_amount(p['fees0'])} {esc(p['sym0'])} + "
        f"{ch.fmt_amount(p['fees1'])} {esc(p['sym1'])}\n\n"
        f"Perkiraan sesudahnya: <b>{ch.fmt_usd(p['value_usd'] + p['unclaimed_usd'])}</b>\n"
        f"<i>{detail} Hanya fee posisi ini yang dipakai — saldo wallet lain tidak "
        f"disentuh dan tidak ada swap. Rasio dua sisi ditentukan range, jadi lazimnya "
        f"ada sisa yang tidak muat; sisa itu dikirim ke wallet, bukan hilang — nilai "
        f"posisi sesudahnya bisa lebih kecil dari perkiraan sebesar sisa itu.</i>"), kb)


async def do_compound(update: Update, pid: str):
    async with position_busy(update, pid) as _ok:
        if not _ok:
            return
        s = store.load_settings()
        cid = s["chain"]
        head = f"⏳ Compound {disp_pid(pid)}…"
        status = await reply(update, head)
        pre_fee = _reinvested_fee_usd(cid, pid)
        async with TX_LOCK:
            try:
                r = await with_progress(status, head, lambda: ch.compound_any(
                    cid, pk(), pid, s["slippage_pct"]))
            except Exception as e:
                await edit(status, f"❌ Compound gagal: {esc(e)}")
                return
        ev_tid = ch.parse_pid(pid)[1] if str(pid).isdigit() else str(pid)
        # added_usd menghitung likuiditas penuh; fee yang jadi modalnya diimbangi event
        # `fees` supaya tidak tercatat sebagai setoran baru (lihat CLAUDE.md).
        store.record_event(cid, "mint", ev_tid, r["added_usd"], "compound", wallet=wallet_address())
        claimed = r.get("compounded_usd") or pre_fee
        if claimed > 0:
            store.record_event(cid, "fees", ev_tid, claimed, "compound", wallet=wallet_address())
        lines = [f"✅ <b>Compound {disp_pid(pid)}</b> — fee masuk kembali jadi likuiditas "
                 f"(~{ch.fmt_usd(r['added_usd'])})"]
        if r.get("used0") is not None:
            lines.append(f"Dipakai: {ch.fmt_amount(r['used0'])} {esc(r['sym0'])} + "
                         f"{ch.fmt_amount(r['used1'])} {esc(r['sym1'])}")
            if (r.get("left0") or 0) > 0 or (r.get("left1") or 0) > 0:
                lines.append(f"<i>Sisa {ch.fmt_amount(r['left0'])} {esc(r['sym0'])} + "
                             f"{ch.fmt_amount(r['left1'])} {esc(r['sym1'])} dikirim ke "
                             f"WALLET (bukan hilang) — rasio dua sisi ditentukan range, "
                             f"jadi lazim ada yang tidak muat.</i>")
        for label, h in r["steps"]:
            lines.append(f"{label}: {ch.tx_link(cid, h)}")
        lines.append(ch.pos_link_any(cid, pid))
        g = await asyncio.to_thread(gas_line, cid)
        if g:
            lines.append(g)
        sesudah, kb = await after_action(cid, pid)
        await edit(status, "\n".join(lines + sesudah), kb)


async def do_revoke(update: Update, key: str, idx: int | None):
    ctx = REVOKES.get(key)
    if not ctx:
        await reply(update, "⚠️ Daftar kadaluarsa (bot sempat restart). Jalankan /revoke lagi.")
        return
    cid = ctx["chain"]
    items = ctx["rows"] if idx is None else [ctx["rows"][idx]]
    head = f"⏳ Mencabut {len(items)} approval…"
    status = await reply(update, head)

    def work():
        done, fail = [], []
        for it in items:
            try:
                done.append((it, ch.revoke_approval(cid, pk(), it)))
            except Exception as e:
                fail.append((it, str(e)[:90]))
        return done, fail

    async with TX_LOCK:
        try:
            done, fail = await with_progress(status, head, work)
        except Exception as e:
            await edit(status, f"❌ Revoke gagal: {esc(e)}")
            return
    lines = [f"✅ <b>{len(done)} approval dicabut</b>"]
    for it, h in done:
        lines.append(f"· {esc(it['symbol'])} → {esc(it['spender_label'])}: {ch.tx_link(cid, h)}")
    for it, err in fail:
        lines.append(f"❌ {esc(it['symbol'])} → {esc(it['spender_label'])}: {esc(err)}")
    g = await asyncio.to_thread(gas_line, cid)
    if g:
        lines.append(g)
    await edit(status, "\n".join(lines), NAV_KB)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import BadRequest, NetworkError
    # BadRequest HARUS dicek duluan: di PTB ia turunan NetworkError, jadi cabang di
    # bawah akan menelannya sebagai "gangguan jaringan, retry otomatis" padahal
    # Telegram menolak pesannya secara permanen dan tidak ada retry yang menolong.
    if isinstance(context.error, BadRequest):
        log.error("Telegram menolak pesan: %s", context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await update.effective_chat.send_message(
                    f"⚠️ Telegram menolak pesan hasil: {esc(str(context.error)[:200])}\n"
                    f"<i>Aksinya sendiri kemungkinan sudah jalan — cek /list.</i>",
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return
    if isinstance(context.error, NetworkError):
        # 502/timeout dari server Telegram — PTB retry sendiri, cukup 1 baris warning
        log.warning("Jaringan Telegram: %s (retry otomatis)", context.error)
        return
    log.error("Handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        msg = str(context.error)[:500]
        try:
            await update.effective_chat.send_message(f"❌ Error: {msg}")
        except Exception:
            pass


def main():
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("❌ TELEGRAM_BOT_TOKEN belum diset (.env).")
    if not allowed_chat_ids():
        sys.exit("❌ TELEGRAM_CHAT_ID belum diset (.env) — wajib, ini kontrol wallet!")
    if not os.environ.get("PRIVATE_KEY", "").strip():
        sys.exit("❌ PRIVATE_KEY belum diset (.env).")

    # Timeout HTTP Telegram dinaikkan dari default (5 detik): VPS ini berkali-kali
    # kena ReadTimeout ke api.telegram.org, dan long polling memang menahan koneksi.
    #
    # `concurrent_updates` WAJIB: default PTB memproses update SATU PER SATU, jadi
    # satu /list yang lama menahan semua klik berikutnya di antrean. Query callback
    # punya masa berlaku pendek, sehingga yang mengantre mati sebelum sempat dijawab
    # dan bot melempar "Query is too old and response timeout expired or query id is
    # invalid" — tombolnya berputar terus di sisi user. Aman untuk jalur dana karena
    # TX_LOCK tetap menyerialkan tiap alur tx (nonce tidak bisa dobel) dan
    # assert_position_open() menolak aksi ke posisi yang sudah tertutup.
    app = (Application.builder().token(token)
           .connect_timeout(20).read_timeout(40).write_timeout(40).pool_timeout(20)
           .get_updates_connect_timeout(20).get_updates_read_timeout(40)
           .concurrent_updates(True)
           .post_init(post_init).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("presets", cmd_presets))
    app.add_handler(CommandHandler("rpc", cmd_rpc))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("scanner", cmd_scanner))
    app.add_handler(CommandHandler("chain", cmd_chain))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("recover", cmd_recover))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_address))
    app.add_error_handler(on_error)
    log.info("LP bot jalan. Wallet: %s",
             ", ".join(f"W{i + 1} {_addr_of(k)}" for i, k in enumerate(all_pks())))
    # bootstrap_retries=-1: coba selamanya. Default 0 berarti SATU kegagalan jaringan
    # saat start (get_me timeout) langsung mematikan proses — "Failed run number 0 of
    # 0. Aborting." — dan bot tidak pernah hidup sampai dijalankan manual lagi.
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
