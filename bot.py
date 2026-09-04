#!/usr/bin/env python3
"""
bot.py — Telegram LP bot: paste alamat token → pilih pool → mint LP single-sided.
/list untuk posisi + PnL + close (dengan auto-swap hasil close → WETH/WBNB).

Jalankan:  python3 bot.py
Env (.env): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PRIVATE_KEY, [RPC_4663, RPC_56, RPC_8453, RPC_999]
"""
import asyncio
from contextlib import asynccontextmanager
import functools
import html
import logging
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import uuid

from dotenv import load_dotenv
from telegram import (BotCommand, ForceReply, InlineKeyboardButton,
                      InlineKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import chain as ch
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


def list_positions_all(cid: int, key: str | None = None,
                       errors: list | None = None) -> list[dict]:
    """Posisi v3 + v4 + v2 wallet (v4/v2 dari registry yang dicatat saat mint).

    `errors`: ref yang GAGAL dibaca ditampung di sini. WAJIB disebut ke user kalau
    terisi — posisi yang gagal dibaca beda dari posisi yang tidak ada, dan kalau
    dibuang diam-diam RPC sibuk terlihat seperti dana hilang."""
    key = key or pk()
    w = _addr_of(key)
    return ch.list_all_positions(cid, key,
                                 store.refs(cid, w, "v2"), store.refs(cid, w, "v4"),
                                 errors=errors)


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


def gas_line(cid: int) -> str:
    """Baris '⛽ gas' untuk kartu hasil. Kosong kalau tidak ada tx (mis. aksi batal)."""
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
    bal_lines = [f"· {esc(cfg['native_symbol'])}: {ch.fmt_amount(native)} ({ch.fmt_usd(native * eth_usd)})"]
    for sym, a in cfg["quotes"].items():
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
SET_KEYS = "width, amount, amount_pct, slippage, gap, alert, autoswap"
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
        elif key == "alert":
            s["alert_secs"] = 0 if val in ("off", "0", "no") else max(30, int(float(val)))
        elif key == "autoswap":
            s["autoswap"] = val in ("on", "true", "1", "yes")
        else:
            return f"Key tidak dikenal: {key}"
    except ValueError:
        return "Value tidak valid."
    return None


def _next_step(steps: list, cur):
    try:
        return steps[(steps.index(cur) + 1) % len(steps)]
    except ValueError:
        return steps[0]


def cycle_setting(key: str):
    s = store.load_settings()
    if key == "slippage":
        s["slippage_pct"] = _next_step(SLIP_STEPS, s["slippage_pct"])
    elif key == "gap":
        s["gap"] = (int(s.get("gap", 1)) + 1) % 6
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


def settings_text() -> str:
    s = store.load_settings()
    cfg = ch.CHAINS[s["chain"]]
    return (
        "⚙️ <b>Pengaturan</b>\n"
        f"Chain aktif: {s['chain']} ({esc(cfg['name'])})\n\n"
        "Klik tombol untuk ganti nilai (▸ = putar preset).\n"
        "· <b>Slippage</b> — toleransi harga saat mint/swap\n"
        "· <b>Gap</b> — jarak range single-sided dari harga (tick-spacing; 0 = nempel)\n"
        "· <b>Alert</b> — interval cek posisi keluar/masuk range\n"
        "· <b>Autoswap</b> — hasil close otomatis di-swap ke wrapped native\n"
        "· <b>Amount</b> — default besaran deposit\n"
        "· <b>Width</b> — default lebar range %"
    )


def settings_kb() -> InlineKeyboardMarkup:
    s = store.load_settings()
    alert = f"{int(s.get('alert_secs', 60))}s" if s.get("alert_secs") else "off"
    amount = f"{s['amount_fixed']:g} fix" if s["amount_fixed"] else f"{s['amount_pct']:g}%"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Slippage: {s['slippage_pct']:g}% ▸", callback_data="cyc|slippage"),
         InlineKeyboardButton(f"Gap: {s.get('gap', 1)} ▸", callback_data="cyc|gap")],
        [InlineKeyboardButton(f"Alert: {alert} ▸", callback_data="cyc|alert"),
         InlineKeyboardButton(f"Autoswap: {'✅ ON' if s['autoswap'] else '🚫 OFF'}", callback_data="cyc|autoswap")],
        [InlineKeyboardButton(f"Amount: {amount} ▸", callback_data="cyc|amount"),
         InlineKeyboardButton(f"Width: {s['width_pct']:g}% ▸", callback_data="cyc|width")],
        [InlineKeyboardButton("✏️ Set nilai manual…", callback_data="askset")],
        BACK_ROW,
    ])


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
        s = store.load_settings()
        s["chain"] = int(args[0])
        store.save_settings(s)
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
    lines.append(f"{esc(cfg['native_symbol'])}: {ch.fmt_amount(native)} ({ch.fmt_usd(native * eth_usd)})")
    for sym, a in cfg["quotes"].items():
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
        s = store.load_settings()          # jangan tinggalkan wallet_idx menunjuk entah ke mana
        s["wallet_idx"] = 0
        store.save_settings(s)
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
            s["chain"] = cid
            store.save_settings(s)
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


async def show_pools_for(status, cid: int, token: str):
    """Discovery + daftar pool untuk (chain, token). Dipisah dari on_address supaya
    tombol pilih-chain bisa memakai jalur yang sama persis."""
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
            f"· – = belum terindeks</i>\n<i>{src_line}</i>{off_line}\n\nPilih pool:")
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


def compute_amount(ctx_data: dict) -> float:
    """Budget deposit. lower/wide/stable = satuan quote; upper = satuan meme."""
    cid = ctx_data["chain"]
    cfg = ch.CHAINS[cid]
    p = ctx_data["pool_info"]
    if ctx_data["amount_fixed"]:
        return float(ctx_data["amount_fixed"])
    w3 = ch.get_w3(cid)
    addr = wallet_address()
    if ctx_data["mode"] == "upper":
        meme = _meme_addr(p)
        mdec = ch.token_info(w3, meme)["decimals"]
        bal = ch.erc20(w3, meme).functions.balanceOf(addr).call()
        return (bal * ctx_data["amount_pct"] / 100) / 10 ** mdec
    gas_reserve = ch.gas_reserve_wei(cid, w3)
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
            wtotal = wbal + max(0, w3.eth.get_balance(addr) - gas_reserve)
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
        raise RuntimeError(f"Saldo {p['quote_sym']} kosong.")
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
        f"📈 <a href=\"https://gmgn.ai/{cfg['gmgn']}/token/{ctx_data['token']['address']}\">GMGN</a> · "
        f"<a href=\"https://dexscreener.com/{cfg['dexscreener']}/{p['pool']}\">DexScreener</a>\n\n"
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

    amount = compute_amount(ctx_data)
    dep_sym = tsym if mode == "upper" else p["quote_sym"]
    if amount <= 0:
        raise RuntimeError(f"Saldo {dep_sym} kosong."
                           + (" Upper butuh pegang token meme." if mode == "upper" else ""))

    if p.get("ver") == 4:
        sqrtp, cur_tick = ch.v4_slot0(w3, cid, p["pool_id"])
    else:
        pool = w3.eth.contract(address=ch.Web3.to_checksum_address(p["pool"]), abi=ch.POOL_ABI)
        slot0 = pool.functions.slot0().call()
        sqrtp, cur_tick = slot0[0], slot0[1]
    lo_t, hi_t = ch.calc_strategy_range(cur_tick, p["fee"], p["quote_is_token1"],
                                        mode, ctx_data["low_pct"], ctx_data["up_pct"],
                                        ctx_data.get("gap", 1), spacing=p.get("tick_spacing"))
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
    amount_desc = "fix" if ctx_data["amount_fixed"] else f"{ctx_data['amount_pct']:g}%"
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
        f"📈 <a href=\"https://gmgn.ai/{cfg['gmgn']}/token/{ctx_data['token']['address']}\">GMGN</a> · "
        f"<a href=\"https://dexscreener.com/{cfg['dexscreener']}/{p['pool']}\">DexScreener</a>\n\n"
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


def box_pct(pool_info: dict) -> float:
    """Lebar satu kotak tick-spacing dalam persen — presisi terbaik pool ini.
    Pool fee 5% biasanya spacing 1000 (≈10,5%), fee 0,05% spacing 10 (≈0,1%)."""
    sp = int(pool_info.get("tick_spacing") or ch.TICK_SPACING.get(pool_info.get("fee"), 60) or 60)
    return round((math.exp(0.0001 * sp) - 1) * 100, 4)


def confirm_kb(key: str, ctx_data: dict) -> InlineKeyboardMarkup:
    mode = ctx_data["mode"]
    rec = ctx_data["rec"]
    if ctx_data["pool_info"].get("ver") == 2:
        def abtn2(a):
            mark = "✓ " if (not ctx_data["amount_fixed"] and ctx_data["amount_pct"] == a) else ""
            return InlineKeyboardButton(f"{mark}A {a:g}%", callback_data=f"amt|{key}|{a}")
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm add", callback_data=f"mint|{key}"),
             InlineKeyboardButton("❌ Cancel", callback_data=f"cancelp|{key}")],
            [abtn2(a) for a in (25, 50, 75, 100)],
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

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm mint", callback_data=f"mint|{key}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"cancelp|{key}")],
        [sbtn(m) for m in ("stable", "wide", "lower", "upper")],
        [wbtn(lo, up) for lo, up in STRAT_PRESETS[mode]],
        [InlineKeyboardButton("🎯 Rapat — langsung aktif (2 sisi)", callback_data=f"tight|{key}")],
        [abtn(a) for a in (25, 50, 75, 100)],
        [InlineKeyboardButton("✏️ Custom Range…", callback_data=f"askrng|{key}"),
         InlineKeyboardButton("✏️ Custom Amount…", callback_data=f"askamt|{key}")],
    ])


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
    text += pool_warnings(ctx_data["chain"], ctx_data["pool_info"])
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
        _, tick = ch.v4_slot0(w3, ctx_data["chain"], p["pool_id"])
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
    strategy = {"mode": mode, "low_pct": ctx_data["low_pct"], "up_pct": ctx_data["up_pct"],
                "gap": ctx_data.get("gap", 1)}

    amount = await asyncio.to_thread(compute_amount, ctx_data)
    dep_sym = tsym if mode == "upper" else p["quote_sym"]
    if amount <= 0:
        await reply(update, f"❌ Saldo {esc(dep_sym)} kosong.")
        return

    mode_label = "V2 50/50" if ver == 2 else STRAT_LABEL[mode]
    head = (f"⏳ Minting position ({mode_label}) [v{ver}]...\n"
            f"<i>{esc(tsym)}/{esc(p['quote_sym'])} fee {p['fee'] / 10000:.2f}% · "
            f"deposit {ch.fmt_amount(amount)} {esc(dep_sym)} "
            f"(wrap/swap otomatis kalau perlu)</i>")
    status = await reply(update, head)

    def work():
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
        g = gas_line(cid)
        if g:
            lines.append(g)
        await edit(status, "\n".join(lines), NAV_KB)
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
    lines.insert(2, (f"Deposited ~{ch.fmt_amount(r['deposited'])} {esc(r['deposit_sym'])} "
                     f"({ch.fmt_usd(r['deposited_usd'])})"))
    if r["token_id"]:
        lines.append(ch.pos_link_any(cid, pid))
    g = gas_line(cid)
    if g:
        lines.append(g)
    await edit(status, "\n".join(lines), NAV_KB)


# ---------- /list ----------
async def cmd_list(update: Update, _, status_msg=None):
    if not authorized(update):
        return
    s = store.load_settings()
    cid = s["chain"]
    if status_msg is None:
        status = await reply(update, f"⏳ Loading positions on {esc(ch.CHAINS[cid]['name'])}...")
    else:
        # refresh: pakai pesan /list yang sudah ada, jangan kirim baru
        status = status_msg
        await edit(status, f"⏳ Refreshing positions on {esc(ch.CHAINS[cid]['name'])}...")
    read_errors: list = []
    try:
        positions = await asyncio.to_thread(list_positions_all, cid, None, read_errors)
    except Exception as e:
        await edit(status, f"❌ Gagal load posisi: {esc(e)}")
        return

    # klaim event riwayat lama (tanpa tag wallet) yang posisinya milik wallet ini
    store.adopt_orphans(cid, wallet_address(), [p["token_id"] for p in positions])
    summary = store.portfolio_summary(cid, wallet_address())
    open_value = sum(p["value_usd"] for p in positions)
    unclaimed = sum(p["unclaimed_usd"] for p in positions)
    deposits = summary["deposits"]
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
    churn = store.churn_count(cid, wallet_address())

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
        lines.append(f"⚠️ {len(read_errors)} posisi GAGAL dibaca (RPC sibuk) — "
                     f"belum tentu tertutup. Klik Refresh.")
        lines.append("")
    if not positions:
        lines.append("Tidak ada posisi aktif." if not read_errors
                     else "Tidak ada posisi yang berhasil dibaca.")
    else:
        lines.append("Klik posisi untuk detail + aksi:")
    for p in positions:
        m = _pos_metrics(cid, p)
        mark = "🟢" if p["in_range"] else "🔴"
        label = f"{mark} {m['meme_sym']} {_pos_disp(p)} · {ch.fmt_usd(m['cur_total'])}"
        if m["pnl_pct"] is not None:
            label += f" · {m['pnl_pct']:+.0f}%"
        label += f" · {m['age']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"pos|{p['pid']}")])
    # Posisi tanpa event mint (mis. hasil /recover, atau mint yang sempat dilaporkan
    # gagal) menambah open_value TANPA deposit pembanding — PnL jadi terlalu bagus.
    # Sebut jumlahnya, jangan diam-diam.
    tanpa_deposit = [p for p in positions if store.mint_usd(cid, p["token_id"]) is None]
    if tanpa_deposit:
        nilai = sum(p["value_usd"] for p in tanpa_deposit)
        lines.insert(len(lines) - 1,
                     f"<i>⚠️ {len(tanpa_deposit)} posisi ({ch.fmt_usd(nilai)}) tidak punya "
                     f"catatan deposit — PnL di atas terlalu bagus sebesar itu.</i>")
    buttons.insert(0, [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])
    if unclaimed > 0:
        buttons.insert(1, [InlineKeyboardButton(
            f"💰 Claim semua fee ({ch.fmt_usd(unclaimed)})", callback_data="claimall")])
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
    await edit(msg, position_card(cid, p), position_kb(cid, p))


# ---------- Chart (link eksternal) ----------
def chart_buttons(cid: int, pool: str, meme_ca: str) -> list[InlineKeyboardButton]:
    cfg = ch.CHAINS[cid]
    return [
        InlineKeyboardButton("📈 GMGN", url=f"https://gmgn.ai/{cfg['gmgn']}/token/{meme_ca}"),
        InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/{cfg['dexscreener']}/{pool}"),
    ]


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
        g = gas_line(cid)
        if g:
            lines.append(g)
        await edit(status, "\n".join(lines), NAV_KB)


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
        g = gas_line(cid)
        if g:
            lines.append(g)
        await edit(status, "\n".join(lines), NAV_KB)


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
        g = gas_line(cid)
        if g:
            lines.append(g)
        await edit(status, "\n".join(lines), NAV_KB)


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
    for label, h in r["steps"]:
        lines.append(f"{label}: {ch.tx_link(cid, h)}")
    if r["token_id"]:
        lines.append(ch.pos_link_any(cid, new_pid))
    g = gas_line(cid)
    if g:
        lines.append(g)
    await edit(status, "\n".join(lines), NAV_KB)


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
        g = gas_line(cid)
        if g:
            lines.append(g)
        await edit(status, "\n".join(lines), NAV_KB)

        if r["swaps"]:
            lines = ["🔄 Auto-swap hasil close:"]
            for sym, h in r["swaps"]:
                if str(h).startswith("0x"):
                    lines.append(f"swapped {esc(sym)} → {esc(ch.CHAINS[cid]['wrapped_symbol'])}: {ch.tx_link(cid, h)}")
                else:
                    lines.append(f"{esc(sym)}: {esc(h)}")
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
        s = store.load_settings()
        s["wallet_idx"] = int(data.split("|")[1])
        store.save_settings(s)
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
        s = store.load_settings()
        s["chain"] = int(data.split("|")[1])
        store.save_settings(s)
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
        st = store.load_settings()
        st["chain"] = cid2
        store.save_settings(st)
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
    if data.startswith(("wd|", "amt|", "st|")):
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
async def _notify(app, body: str):
    for chat_id in allowed_chat_ids():
        try:
            await app.bot.send_message(chat_id, body, parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
        except Exception:
            pass


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
            pk_pos = await asyncio.to_thread(list_positions_all, cid, key)
        except Exception as e:
            log.warning("monitor posisi %s/%s: %s", cid, waddr, e)
            by_wallet[waddr] = None  # fetch gagal → JANGAN anggap posisi hilang
            continue
        by_wallet[waddr] = {p["pid"]: p for p in pk_pos}
        positions += pk_pos
    return positions, by_wallet


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
        interval = int(s.get("alert_secs", 60) or 0)
        alert_on = interval > 0
        order_chains = [c for c in ch.CHAINS if _orders_for_chain(c, "active")]
        chains = set(order_chains)
        if alert_on:
            chains.add(active_cid)
        if not chains:
            await asyncio.sleep(60)
            continue
        for cid in chains:
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
        gap = int(s.get("order_secs", 120) or 120) if order_chains else 0
        await asyncio.sleep(max(30, gap, interval) if (order_chains or alert_on) else 60)


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
    g = gas_line(cid)
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
    g = gas_line(cid)
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
        return next((x for x in list_positions_all(cid) if x["pid"] == str(src_pid)), None)

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
        return next((x for x in list_positions_all(cid) if x["pid"] == str(src_pid)), None)

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
    await edit(msg, (
        f"♻️ <b>Compound {_pos_disp(p)}?</b>\n\n"
        f"{_pool_info_line(cid, p, ver)}\n"
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
        g = gas_line(cid)
        if g:
            lines.append(g)
        await edit(status, "\n".join(lines), NAV_KB)


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
    g = gas_line(cid)
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
