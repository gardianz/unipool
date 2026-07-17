#!/usr/bin/env python3
"""
bot.py — Telegram LP bot: paste alamat token → pilih pool → mint LP single-sided.
/list untuk posisi + PnL + close (dengan auto-swap hasil close → WETH/WBNB).

Jalankan:  python3 bot.py
Env (.env): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PRIVATE_KEY, [RPC_4663, RPC_56]
"""
import asyncio
import functools
import html
import logging
import os
import re
import sys
import time
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
def all_pks() -> tuple[str, ...]:
    """Semua private key dari .env: PRIVATE_KEY (W1), PRIVATE_KEY_2 (W2), dst."""
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


def wallet_address() -> str:
    return _addr_of(pk())


async def reply(update: Update, text: str, kb: InlineKeyboardMarkup | None = None):
    return await update.effective_chat.send_message(
        text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


async def edit(msg, text: str, kb: InlineKeyboardMarkup | None = None):
    """Edit pesan status in-place; fallback kirim baru kalau gagal."""
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                            disable_web_page_preview=True)
    except Exception as e:
        if "not modified" in str(e).lower():
            return  # konten sama — biarkan
        await msg.get_bot().send_message(msg.chat_id, text, parse_mode=ParseMode.HTML,
                                         reply_markup=kb, disable_web_page_preview=True)


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
    "<b>unipool — LP Uniswap V3 (Robinhood + BSC)</b>\n\n"
    "Paste alamat token (0x...) → bot cari pool → pilih → atur strategi → mint.\n"
    "/start membuka menu utama (dashboard saldo + tombol navigasi).\n\n"
    "<b>Perintah:</b>\n"
    "/start — menu utama\n"
    "/list — posisi + PnL + chart/add/reduce/close\n"
    "/wallet — saldo semua token + nilai USD\n"
    "/settings — pengaturan via tombol\n"
    "/set <code>key value</code> — set manual (width, amount, amount_pct, slippage, gap, alert, autoswap)\n"
    "/chain — ganti chain aktif\n\n"
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
         InlineKeyboardButton("👛 Dompet", callback_data="menu|wallet")],
        [InlineKeyboardButton("⚙️ Pengaturan", callback_data="menu|settings"),
         InlineKeyboardButton("⛓ Chain", callback_data="menu|chain")],
        [InlineKeyboardButton("❓ Bantuan", callback_data="menu|help"),
         InlineKeyboardButton("🔄 Segarkan", callback_data="menu|main")],
    ]
    return InlineKeyboardMarkup(rows)


BACK_ROW = [InlineKeyboardButton("⬅️ Menu", callback_data="menu|main")]
NAV_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Posisi", callback_data="go|list"),
     InlineKeyboardButton("🏠 Menu", callback_data="go|main")],
])


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
        f"🦄 <b>unipool</b> — LP Uniswap V3\n"
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
    cfg = ch.CHAINS[cid]
    amount_desc = f"amount {s['amount_fixed']} fix" if s["amount_fixed"] else f"amount {s['amount_pct']}%"
    status = await reply(update, (
        f"⏳ Fetching Uniswap v3 pools on {esc(cfg['name'])}...\n"
        f"(width {s['width_pct']:g}% · {esc(amount_desc)} · deposit auto)"))

    import time as _t
    t0 = _t.time()
    try:
        res = await asyncio.to_thread(ch.discover_pools, cid, token)
    except Exception as e:
        await edit(status, f"❌ Gagal fetch: {esc(e)}")
        return

    pools = res["pools"]
    if not pools:
        await edit(status, f"❌ Tidak ada pool v3 untuk {esc(res['token']['symbol'])} di {esc(cfg['name'])}.")
        return

    buttons = []
    for i, p in enumerate(pools[:8], 1):
        key = uuid.uuid4().hex[:10]
        PENDING[key] = {"chain": cid, "token": res["token"], "pool_info": p,
                        "mode": "lower", "low_pct": s["width_pct"], "up_pct": 100.0,
                        "amount_pct": s["amount_pct"], "amount_fixed": s["amount_fixed"],
                        "gap": int(s.get("gap", 1)), "vol": None, "rec": None}
        label = f"{i}. [v3] {p['quote_sym']} {p['fee'] / 10000:.2f}% · {ch.fmt_usd(p['tvl_usd'])}"
        if p.get("apr_pct"):
            label += f" · APR {p['apr_pct']:,.0f}%"
        buttons.append([InlineKeyboardButton(label, callback_data=f"pool|{key}")])
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="cancel")])
    await edit(status,
               f"Found {len(pools)} pool(s) untuk <b>{esc(res['token']['symbol'])}</b> ({_t.time() - t0:.1f}s). Pilih:",
               InlineKeyboardMarkup(buttons))


# ---------- Mint flow ----------
STRAT_LABEL = {"stable": "Stable", "wide": "Wide", "lower": "Lower", "upper": "Upper"}
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
    q = ch.erc20(w3, p["quote_addr"])
    bal = q.functions.balanceOf(addr).call()
    gas_reserve = int(0.0005 * 1e18)
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


def build_preview(ctx_data: dict) -> str:
    """Kartu konfirmasi mint (dipanggil di thread)."""
    cid = ctx_data["chain"]
    cfg = ch.CHAINS[cid]
    p = ctx_data["pool_info"]
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

    pool = w3.eth.contract(address=ch.Web3.to_checksum_address(p["pool"]), abi=ch.POOL_ABI)
    slot0 = pool.functions.slot0().call()
    sqrtp, cur_tick = slot0[0], slot0[1]
    lo_t, hi_t = ch.calc_strategy_range(cur_tick, p["fee"], p["quote_is_token1"],
                                        mode, ctx_data["low_pct"], ctx_data["up_pct"],
                                        ctx_data.get("gap", 1))
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
    if mode != "upper":
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
        f"<b>Confirm mint · {esc(cfg['name'])} · v3</b>\n"
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


def confirm_kb(key: str, ctx_data: dict) -> InlineKeyboardMarkup:
    mode = ctx_data["mode"]
    rec = ctx_data["rec"]

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
        tid = int(st["key"])
        desc = f"{val:g}% saldo" if is_pct else f"{val:g} quote"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Tambah {desc} ke #{tid}",
                                  callback_data=f"addok|{tid}|{val}|{'p' if is_pct else 'f'}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ])
        await reply(update, f"Konfirmasi tambah dana ke posisi #{tid}:", kb)
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
    tsym = ctx_data["token"]["symbol"]
    mode = ctx_data["mode"]
    strategy = {"mode": mode, "low_pct": ctx_data["low_pct"], "up_pct": ctx_data["up_pct"],
                "gap": ctx_data.get("gap", 1)}

    amount = await asyncio.to_thread(compute_amount, ctx_data)
    dep_sym = tsym if mode == "upper" else p["quote_sym"]
    if amount <= 0:
        await reply(update, f"❌ Saldo {esc(dep_sym)} kosong.")
        return

    status = await reply(update, (
        f"⏳ Minting position ({STRAT_LABEL[mode]})...\n"
        f"<i>{esc(tsym)}/{esc(p['quote_sym'])} fee {p['fee'] / 10000:.2f}% · "
        f"deposit {ch.fmt_amount(amount)} {esc(dep_sym)} "
        f"(wrap/swap otomatis kalau perlu)</i>"))

    async with TX_LOCK:
        try:
            r = await asyncio.to_thread(
                ch.mint_position, cid, pk(), p, amount, strategy, s["slippage_pct"])
        except Exception as e:
            await edit(status, f"❌ Mint gagal: {esc(e)}")
            return

    store.record_event(cid, "mint", r["token_id"], r["deposited_usd"],
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

    lines = [f"✅ <b>{esc(tsym)} #{r['token_id']}</b> [v3] · {STRAT_LABEL[mode]}"]
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
        lines.append(ch.pos_link(cid, r["token_id"]))
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
    try:
        positions = await asyncio.to_thread(ch.list_positions, cid, pk())
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
    pnl_pct = (pnl / deposits * 100) if deposits else 0.0

    lines = []
    if len(all_pks()) > 1:
        waddr = wallet_address()
        lines.append(f"👛 {esc(wallet_label())} <code>{esc(waddr[:6])}…{esc(waddr[-4:])}</code>")
    lines += [
        f"<b>Portfolio PnL {ch.fmt_usd(pnl)} ({pnl_pct:+.2f}%)</b>",
        (f"deposits {ch.fmt_usd(deposits)} | withdrawals {ch.fmt_usd(summary['withdrawals'])} | "
         f"fees claimed {ch.fmt_usd(summary['fees_claimed'])}"),
        f"open value {ch.fmt_usd(open_value)} | unclaimed fees {ch.fmt_usd(unclaimed)}",
        "",
    ]
    buttons = []
    if not positions:
        lines.append("Tidak ada posisi aktif.")
    else:
        lines.append("Klik posisi untuk detail + aksi:")
    for p in positions:
        m = _pos_metrics(cid, p)
        mark = "🟢" if p["in_range"] else "🔴"
        label = f"{mark} {m['meme_sym']} #{p['token_id']} · {ch.fmt_usd(m['cur_total'])}"
        if m["pnl_pct"] is not None:
            label += f" · {m['pnl_pct']:+.0f}%"
        label += f" · {m['age']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"pos|{p['token_id']}")])
    buttons.insert(0, [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])
    buttons.append(BACK_ROW)
    await edit(status, "\n".join(lines), InlineKeyboardMarkup(buttons))


def _pos_metrics(cid: int, p: dict) -> dict:
    """Angka turunan posisi untuk label ringkasan + kartu detail."""
    tid = p["token_id"]
    dep = store.mint_usd(cid, tid)
    claimed = store.fees_claimed_usd(cid, tid)
    withdrawn = store.withdrawn_usd(cid, tid)  # hasil reduce yang sudah masuk wallet
    cur_total = p["value_usd"] + p["unclaimed_usd"]
    mts = store.mint_ts(cid, tid)
    pnl = pnl_pct = apr = None
    if dep:
        pnl = cur_total + claimed + withdrawn - dep
        pnl_pct = pnl / dep * 100
        if mts:
            age_days = max((int(time.time()) - mts) / 86400, 0.01)
            apr = (p["unclaimed_usd"] + claimed) / dep / age_days * 365 * 100
    return {
        "meme_sym": p["sym0"] if p["quote_is_token1"] else p["sym1"],
        "dep": dep, "claimed": claimed, "withdrawn": withdrawn, "cur_total": cur_total,
        "pnl": pnl, "pnl_pct": pnl_pct, "apr": apr,
        "age": store.fmt_age(mts),
    }


def position_card(cid: int, p: dict) -> str:
    """Kartu detail satu posisi (ala BasedBot)."""
    m = _pos_metrics(cid, p)
    tid = p["token_id"]
    in_out = "🟢 IN range" if p["in_range"] else "🔴 OUT of range"
    meme_ca = p["token0"] if p["quote_is_token1"] else p["token1"]
    pct0 = p["usd0"] / p["value_usd"] * 100 if p["value_usd"] else 0
    if m["pnl"] is not None:
        pnl_line = (f"{'🟩 Untung' if m['pnl'] >= 0 else '🟥 Rugi'}: "
                    f"{'+' if m['pnl'] >= 0 else '−'}${abs(m['pnl']):.2f} ({m['pnl_pct']:+.1f}%)")
    else:
        pnl_line = "PnL: ? (mint di luar bot)"
    L = [
        f"<b>{esc(m['meme_sym'])} #{tid}</b> [v3] · {in_out} · Age {m['age']}",
        f"CA: <code>{esc(meme_ca)}</code>",
        "",
        f"📊 Range: {esc(range_str(p))}",
        f"💼 <b>Nilai {ch.fmt_usd(p['value_usd'])}</b>",
        f"· {ch.fmt_amount(p['amount0'])} {esc(p['sym0'])} ({ch.fmt_usd(p['usd0'])} · {pct0:.0f}%)",
        f"· {ch.fmt_amount(p['amount1'])} {esc(p['sym1'])} ({ch.fmt_usd(p['usd1'])} · {100 - pct0:.0f}%)",
        f"💰 <b>Fee unclaimed {ch.fmt_usd(p['unclaimed_usd'])}</b>",
        f"· {ch.fmt_amount(p['fees0'])} {esc(p['sym0'])} ({ch.fmt_usd(p['fees_usd0'])}) + "
        f"{ch.fmt_amount(p['fees1'])} {esc(p['sym1'])} ({ch.fmt_usd(p['fees_usd1'])})",
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
    L.append(ch.pos_link(cid, tid))
    return "\n".join(L)


def position_kb(cid: int, p: dict) -> InlineKeyboardMarkup:
    tid = p["token_id"]
    meme_ca = p["token0"] if p["quote_is_token1"] else p["token1"]
    return InlineKeyboardMarkup([
        chart_buttons(cid, p["pool"], meme_ca) + [InlineKeyboardButton("🔄", callback_data=f"pos|{tid}")],
        [InlineKeyboardButton("➕ Add", callback_data=f"add|{tid}"),
         InlineKeyboardButton("➖ Reduce", callback_data=f"red|{tid}"),
         InlineKeyboardButton("💰 Fee", callback_data=f"fee|{tid}"),
         InlineKeyboardButton("🗑 Close", callback_data=f"close|{tid}")],
        [InlineKeyboardButton("⬅️ Posisi", callback_data="menu|list"),
         InlineKeyboardButton("🏠 Menu", callback_data="menu|main")],
    ])


async def show_position(update: Update, msg, token_id: int):
    s = store.load_settings()
    cid = s["chain"]
    await edit(msg, f"⏳ Memuat posisi #{token_id}...")

    def work():
        return next((p for p in ch.list_positions(cid, pk()) if p["token_id"] == token_id), None)

    try:
        p = await asyncio.to_thread(work)
    except Exception as e:
        await edit(msg, f"❌ Gagal load posisi: {esc(e)}", InlineKeyboardMarkup([BACK_ROW]))
        return
    if not p:
        await edit(msg, f"❌ Posisi #{token_id} tidak ditemukan (sudah ditutup?).",
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
async def ask_add(update: Update, token_id: int):
    s = store.load_settings()
    qsym = ch.CHAINS[s["chain"]]["wrapped_symbol"]
    await update.effective_chat.send_message(
        (f"➕ <b>Balas pesan ini</b> dengan jumlah dana untuk ditambah ke #{token_id}:\n"
         f"· nilai pasti: <code>0.005</code> (satuan quote posisi, umumnya {esc(qsym)})\n"
         f"· persen saldo: <code>30%</code>\n\n"
         f"<i>Komposisi quote/meme dihitung otomatis mengikuti range posisi; "
         f"meme existing di wallet dipakai duluan.</i>"),
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder="contoh: 0.005 atau 30%"))
    AWAITING[update.effective_chat.id] = {"kind": "addamt", "key": str(token_id)}


async def do_add_exec(update: Update, token_id: int, val: float, is_pct: bool):
    s = store.load_settings()
    cid = s["chain"]
    status = await reply(update, f"⏳ Menambah dana ke #{token_id}...")

    def work():
        budget = val
        if is_pct:
            w3 = ch.get_w3(cid)
            cfg = ch.CHAINS[cid]
            npm = w3.eth.contract(address=ch.Web3.to_checksum_address(cfg["npm"]), abi=ch.NPM_ABI)
            pos = npm.functions.positions(token_id).call()
            quotes_lc = {a.lower() for a in cfg["quotes"].values()}
            quote = pos[3] if pos[3].lower() in quotes_lc else pos[2]
            qc = ch.erc20(w3, quote)
            bal = qc.functions.balanceOf(wallet_address()).call()
            if quote.lower() == cfg["wrapped"].lower():
                bal += max(0, w3.eth.get_balance(wallet_address()) - int(0.0005e18))
            else:
                try:
                    wbal = ch.erc20(w3, cfg["wrapped"]).functions.balanceOf(wallet_address()).call()
                    wtotal = wbal + max(0, w3.eth.get_balance(wallet_address()) - int(0.0005e18))
                    rate = ch.wrapped_per_quote_wei(w3, cid, quote)
                    if wtotal > 0 and rate > 0:
                        bal += int(wtotal / rate * 0.98)
                except Exception:
                    pass
            budget = (bal * val / 100) / 10 ** qc.functions.decimals().call()
        return ch.increase_position(cid, pk(), token_id, budget, s["slippage_pct"])

    async with TX_LOCK:
        try:
            r = await asyncio.to_thread(work)
        except Exception as e:
            await edit(status, f"❌ Add gagal: {esc(e)}")
            return
    store.record_event(cid, "mint", token_id, r["added_usd"], "add", wallet=wallet_address())
    lines = [f"✅ <b>Added #{token_id}</b> (~{ch.fmt_usd(r['added_usd'])})"]
    if r.get("quote_in") is not None:
        lines.append(f"Masuk: {ch.fmt_amount(r['quote_in'])} {r['quote_sym']}"
                     f" + {ch.fmt_amount(r['meme_in'])} {r['meme_sym']}"
                     f" <i>(meme dari wallet dipakai duluan)</i>")
    for label, h in r["steps"]:
        lines.append(f"{label}: {ch.tx_link(cid, h)}")
    lines.append(ch.pos_link(cid, token_id))
    await edit(status, "\n".join(lines), NAV_KB)


async def ask_reduce(update: Update, token_id: int):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➖ {pct}%", callback_data=f"redok|{token_id}|{pct}")
         for pct in (25, 50, 75)],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await reply(update, (
        f"➖ <b>Kurangi posisi #{token_id}?</b>\n"
        f"Pilih persentase yang ditarik. Fee unclaimed ikut terambil.\n"
        f"<i>Token hasil penarikan tetap di wallet (tanpa auto-swap). "
        f"Untuk 100% pakai tombol Close.</i>"), kb)


async def do_reduce_exec(update: Update, token_id: int, pct: int):
    s = store.load_settings()
    cid = s["chain"]

    def snapshot():
        return next((p for p in ch.list_positions(cid, pk()) if p["token_id"] == token_id), None)

    pos = await asyncio.to_thread(snapshot)
    status = await reply(update, f"⏳ Menarik {pct}% dari #{token_id}...")
    async with TX_LOCK:
        try:
            r = await asyncio.to_thread(ch.decrease_position, cid, pk(), token_id, pct)
        except Exception as e:
            await edit(status, f"❌ Reduce gagal: {esc(e)}")
            return
    if pos:
        store.record_event(cid, "close", token_id, pos["value_usd"] * pct / 100,
                           f"reduce {pct}%", wallet=wallet_address())
        if pos["unclaimed_usd"] > 0:
            store.record_event(cid, "fees", token_id, pos["unclaimed_usd"], wallet=wallet_address())
    lines = [f"✅ <b>Reduced #{token_id} −{pct}%</b>",
             f"Received ~{ch.fmt_amount(r['got0'])} {esc(r['sym0'])} + "
             f"{ch.fmt_amount(r['got1'])} {esc(r['sym1'])} (termasuk fee)"]
    for label, h in r["steps"]:
        lines.append(f"{label}: {ch.tx_link(cid, h)}")
    lines.append(ch.pos_link(cid, token_id))
    await edit(status, "\n".join(lines), NAV_KB)


# ---------- Collect fee ----------
async def do_collect(update: Update, token_id: int):
    s = store.load_settings()
    cid = s["chain"]

    def find_pos():
        return next((p for p in ch.list_positions(cid, pk()) if p["token_id"] == token_id), None)

    pos = await asyncio.to_thread(find_pos)
    status = await reply(update, f"⏳ Collect fee #{token_id}...")
    async with TX_LOCK:
        try:
            r = await asyncio.to_thread(ch.collect_fees, cid, pk(), token_id)
        except Exception as e:
            await edit(status, f"❌ Collect gagal: {esc(e)}")
            return
    usd_txt = ""
    if pos and pos["unclaimed_usd"] > 0:
        store.record_event(cid, "fees", token_id, pos["unclaimed_usd"], wallet=wallet_address())
        usd_txt = f" (~{ch.fmt_usd(pos['unclaimed_usd'])})"
    lines = [f"✅ <b>Fee terklaim #{token_id}</b>{usd_txt}",
             f"Received {ch.fmt_amount(r['got0'])} {esc(r['sym0'])} + "
             f"{ch.fmt_amount(r['got1'])} {esc(r['sym1'])}",
             "<i>Posisi tetap jalan — liquidity tidak berubah.</i>"]
    for label, h in r["steps"]:
        lines.append(f"{label}: {ch.tx_link(cid, h)}")
    await edit(status, "\n".join(lines), NAV_KB)


# ---------- Close flow ----------
async def ask_close(update: Update, token_id: int):
    s = store.load_settings()
    cid = s["chain"]

    def work():
        for p in ch.list_positions(cid, pk()):
            if p["token_id"] == token_id:
                return p
        return None

    p = await asyncio.to_thread(work)
    if not p:
        await reply(update, f"❌ Posisi #{token_id} tidak ditemukan.")
        return
    status = "🟢 IN" if p["in_range"] else "🔴 OUT"
    wsym = ch.CHAINS[cid]["wrapped_symbol"]
    meme_sym = p["sym0"] if p["quote_is_token1"] else p["sym1"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Close + swap semua {meme_sym} → {wsym}",
                              callback_data=f"closeok|{token_id}|1")],
        [InlineKeyboardButton(f"✅ Close, tahan {meme_sym}", callback_data=f"closeok|{token_id}|0")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await reply(update, (
        f"⚠️ <b>Close position?</b>\n\n"
        f"#{token_id} [v3] {esc(p['sym1'])}/{esc(p['sym0'])}\n"
        f"Val ~{ch.fmt_usd(p['value_usd'])} · {status}\n\n"
        f"Full exit LP (decrease + collect).\n"
        f"<i>Opsi swap menjual SELURUH saldo {esc(meme_sym)} di wallet "
        f"(termasuk sisa dari close/mint sebelumnya), bukan cuma hasil posisi ini.</i>"), kb)


async def do_close(update: Update, token_id: int, autoswap: bool):
    s = store.load_settings()
    cid = s["chain"]

    def find_pos():
        for p in ch.list_positions(cid, pk()):
            if p["token_id"] == token_id:
                return p
        return None

    pos = await asyncio.to_thread(find_pos)
    usd = (pos["value_usd"] + pos["unclaimed_usd"]) if pos else 0.0
    status = await reply(update, f"⏳ Closing #{token_id} (v3)...")
    async with TX_LOCK:
        try:
            r = await asyncio.to_thread(ch.close_position, cid, pk(), token_id, s["slippage_pct"], autoswap)
        except Exception as e:
            await edit(status, f"❌ Close gagal: {esc(e)}")
            return

    store.record_event(cid, "close", token_id, pos["value_usd"] if pos else usd, wallet=wallet_address())
    if pos and pos["unclaimed_usd"] > 0:
        store.record_event(cid, "fees", token_id, pos["unclaimed_usd"], wallet=wallet_address())
    lines = [f"✅ <b>Closed #{token_id}</b>",
             f"Received ~{ch.fmt_amount(r['got0'])} {esc(r['sym0'])} + {ch.fmt_amount(r['got1'])} {esc(r['sym1'])}"]
    if pos:
        lines.append(f"💰 Fee terklaim: {ch.fmt_amount(pos['fees0'])} {esc(pos['sym0'])} + "
                     f"{ch.fmt_amount(pos['fees1'])} {esc(pos['sym1'])} (~{ch.fmt_usd(pos['unclaimed_usd'])})")
    lines.append(f"Withdrawal value ~{ch.fmt_usd(usd)}")
    for label, h in r["steps"]:
        lines.append(f"{label}: {ch.tx_link(cid, h)}")
    await edit(status, "\n".join(lines), NAV_KB)

    if r["swaps"]:
        lines = ["🔄 Auto-swap hasil close:"]
        for sym, h in r["swaps"]:
            if str(h).startswith("0x"):
                lines.append(f"swapped {esc(sym)} → {esc(ch.CHAINS[cid]['wrapped_symbol'])}: {ch.tx_link(cid, h)}")
            else:
                lines.append(f"{esc(sym)}: {esc(h)}")
        await reply(update, "\n".join(lines))


# ---------- Callback router ----------
async def on_callback(update: Update, _):
    if not authorized(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "cancel":
        await q.edit_message_reply_markup(None)
        await reply(update, "❌ Cancelled.")
        return
    if data == "refresh":
        await cmd_list(update, None, status_msg=q.message)
        return
    if data == "noop":
        return
    # --- navigasi menu (edit in-place) ---
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
    if data == "menu|settings":
        await edit(q.message, settings_text(), settings_kb())
        return
    if data == "menu|chain":
        await edit(q.message, "⛓ <b>Pilih chain aktif:</b>", chain_kb())
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
        await show_position(update, q.message, int(data.split("|", 1)[1]))
        return
    if data.startswith("pool|"):
        # pilih pool → kartu konfirmasi (belum mint)
        await show_confirm(q.message, data.split("|", 1)[1])
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
        await show_position(update, q.message, int(data.split("|", 1)[1]))
        return
    if data.startswith("add|"):
        await ask_add(update, int(data.split("|", 1)[1]))
        return
    if data.startswith("addok|"):
        _, tid, val, kind = data.split("|")
        await q.edit_message_reply_markup(None)
        await do_add_exec(update, int(tid), float(val), kind == "p")
        return
    if data.startswith("fee|"):
        await do_collect(update, int(data.split("|", 1)[1]))
        return
    if data.startswith("red|"):
        await ask_reduce(update, int(data.split("|", 1)[1]))
        return
    if data.startswith("redok|"):
        _, tid, pct = data.split("|")
        await q.edit_message_reply_markup(None)
        await do_reduce_exec(update, int(tid), int(pct))
        return
    if data.startswith("close|"):
        await ask_close(update, int(data.split("|", 1)[1]))
        return
    if data.startswith("closeok|"):
        parts = data.split("|")
        await q.edit_message_reply_markup(None)
        await do_close(update, int(parts[1]), autoswap=(len(parts) > 2 and parts[2] == "1"))
        return


# ---------- Monitor alert in/out range ----------
async def monitor_loop(app):
    """Cek berkala semua posisi; kirim alert saat posisi keluar/masuk range."""
    await asyncio.sleep(15)  # kasih waktu bot siap
    while True:
        s = store.load_settings()
        interval = int(s.get("alert_secs", 60) or 0)
        if interval <= 0:
            await asyncio.sleep(60)
            continue
        cid = s["chain"]
        try:
            positions = []
            for key in all_pks():
                positions += await asyncio.to_thread(ch.list_positions, cid, key)
            for p in positions:
                key = (cid, p["token_id"])
                now_in = p["in_range"]
                prev = RANGE_STATE.get(key)
                RANGE_STATE[key] = now_in
                if prev is None or prev == now_in:
                    continue  # baseline pertama / tidak berubah
                meme_sym = p["sym0"] if p["quote_is_token1"] else p["sym1"]
                if now_in:
                    head = f"🟢 <b>{esc(meme_sym)} #{p['token_id']} MASUK range</b> — fee mulai mengalir."
                else:
                    if p.get("mc_now") and p.get("mc_lower") and p["mc_now"] < p["mc_lower"]:
                        arah = f"tembus ke BAWAH — posisi jadi penuh {esc(meme_sym)}"
                    else:
                        arah = f"keluar ke ATAS — posisi jadi penuh {esc(p['quote_sym'] or 'quote')}"
                    head = f"🔴 <b>{esc(meme_sym)} #{p['token_id']} KELUAR range</b> — {arah}. Fee berhenti."
                body = (f"{head}\n"
                        f"Val {ch.fmt_usd(p['value_usd'])} · Unclaimed {ch.fmt_usd(p['unclaimed_usd'])}\n"
                        f"Range: {esc(range_str(p))}")
                meme_ca = p["token0"] if p["quote_is_token1"] else p["token1"]
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Detail", callback_data=f"pos|{p['token_id']}"),
                     InlineKeyboardButton("🗑 Close", callback_data=f"close|{p['token_id']}")],
                    chart_buttons(cid, p["pool"], meme_ca),
                ])
                for chat_id in allowed_chat_ids():
                    try:
                        await app.bot.send_message(chat_id, body, parse_mode=ParseMode.HTML,
                                                   reply_markup=kb, disable_web_page_preview=True)
                    except Exception:
                        pass
            # posisi yang sudah ditutup → buang dari state
            live = {(cid, p["token_id"]) for p in positions}
            for k in [k for k in RANGE_STATE if k[0] == cid and k not in live]:
                RANGE_STATE.pop(k, None)
        except Exception as e:
            log.warning("monitor alert: %s", e)
        await asyncio.sleep(max(30, interval))


async def post_init(app):
    # daftar command → muncul di menu Telegram saat user ketik "/"
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Menu utama (dashboard saldo)"),
            BotCommand("list", "Posisi LP + PnL + chart/close"),
            BotCommand("wallet", "Saldo semua token + nilai USD"),
            BotCommand("settings", "Pengaturan via tombol"),
            BotCommand("chain", "Ganti chain aktif"),
            BotCommand("help", "Bantuan & daftar perintah"),
        ])
    except Exception as e:
        log.warning("set_my_commands gagal: %s", e)
    app.create_task(monitor_loop(app))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import NetworkError
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

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("chain", cmd_chain))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_address))
    app.add_error_handler(on_error)
    log.info("LP bot jalan. Wallet: %s",
             ", ".join(f"W{i + 1} {_addr_of(k)}" for i, k in enumerate(all_pks())))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
