"""
chain.py — Web3 core untuk LP bot: discovery pool, mint single-sided,
listing posisi, close, dan auto-swap.
DEX per chain: Uniswap v2/v3/v4 di Robinhood (4663), PancakeSwap v2/v3 di BSC (56).
"""
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import quote, urlparse

import requests
from eth_abi import encode as abi_encode
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from web3 import Web3
from web3.exceptions import ContractLogicError

try:    # BSC itu PoA: extraData 280 byte, jauh di atas 32 byte yang divalidasi web3.
    from web3.middleware import ExtraDataToPOAMiddleware as _POA   # web3 v7
except ImportError:                                                # pragma: no cover
    try:
        from web3.middleware import geth_poa_middleware as _POA    # web3 v6
    except ImportError:
        _POA = None

Q96 = 2**96
MAX_UINT128 = 2**128 - 1
MAX_UINT256 = 2**256 - 1
# Gabungan fee tier Uniswap (3000→60) dan PancakeSwap (2500→50). Pemetaannya tidak
# pernah bentrok — fee 2500 cuma ada di Pancake, 3000 cuma di Uniswap — jadi satu map
# aman untuk dua DEX. Fee tier yang di-scan per chain: CHAINS[cid]["fee_tiers"].
TICK_SPACING = {100: 1, 500: 10, 2500: 50, 3000: 60, 10000: 200}
MIN_TICK, MAX_TICK = -887272, 887272
DEADLINE_SECS = 1200

CHAINS = {
    4663: {
        "name": "Robinhood",
        "dex": "Uniswap",
        "fee_tiers": (100, 500, 3000, 10000),   # tier v3 yang di-scan discovery
        "uni_api": True,        # boleh pakai API indexer resmi Uniswap (ListPools/ListPositions)
        "v2_fee": 3000,         # fee pair v2 (ppm) — Uniswap V2 = 0.3%
        "v2_swap_num": 997, "v2_swap_den": 1000,   # konstanta getAmountOut router v2
        "gas_reserve": 0.0005,  # fallback cadangan gas kalau harga gas tak terbaca
        "slug": "robinhood",  # slug URL app.uniswap.org
        "dexscreener": "robinhood",
        "gecko": "robinhood",
        "gmgn": "robinhood",
        # Daftar posisi & pool diambil dari API resmi Uniswap (read-only, tanpa key/
        # tx) — sama seperti app.uniswap.org, jadi konsisten & lengkap. Lihat
        # uniswap_v3_token_ids (posisi) dan _uni_discover (pool) di web.py. Dict pool
        # dari indexer tetap diverifikasi on-chain (assert_pool_orientation) sebelum
        # mint. Fallback ke scan RPC kalau API mati. (Indexer alps sudah tidak dipakai.)
        "v2_factory": "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f",
        # rpc.mainnet.chain.robinhood.com sering diblokir DNS ISP Indonesia
        # (redirect ke internetpositif.id) → fallback Blockscout eth-rpc
        "rpcs": [
            "https://rpc.mainnet.chain.robinhood.com",
            "https://robinhoodchain.blockscout.com/api/eth-rpc",
        ],
        "alchemy": "robinhood-mainnet",
        "rpc_env": "RPC_4663",
        "explorer": "https://robinhoodchain.blockscout.com",
        "factory": "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
        "npm": "0x73991a25c818bf1f1128deaab1492d45638de0d3",
        # SwapRouter02 — diverifikasi on-chain: factory() == factory di atas
        "router": "0xCaf681a66D020601342297493863E78C959E5cb2",
        # V2 router — diverifikasi on-chain: factory()==v2_factory, WETH()==wrapped
        "v2_router": "0x89e5db8b5aa49aa85ac63f691524311aeb649eba",
        # Uniswap V4 (developers.uniswap.org/contracts/v4/deployments; semua diverifikasi
        # on-chain: posm/stateview/quoter/UR .poolManager() == v4_pm, posm.permit2() canonical)
        "v4_pm": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "v4_posm": "0x58daec3116aae6d93017baaea7749052e8a04fa7",
        "v4_stateview": "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
        "v4_quoter": "0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
        "v4_router": "0x8876789976decbfcbbbe364623c63652db8c0904",
        # UR Robinhood = build custom: ExactInputSingleParams punya field ekstra
        # uint256 minHopPriceX36 (diverifikasi dari source Blockscout). BSC = standar.
        "v4_swap_hop_field": True,
        "permit2": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
        "wrapped": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        "wrapped_symbol": "WETH",
        "native_symbol": "ETH",
        "quotes": {
            "WETH": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
            "USDG": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        },
        "stable_syms": {"USDG"},
    },
    8453: {
        "name": "Base",
        "dex": "Uniswap",
        # feeAmountTickSpacing() dibaca langsung dari factory: 100→1, 500→10,
        # 3000→60, 10000→200 (tier standar Uniswap, tidak ada kejutan seperti Pancake)
        "fee_tiers": (100, 500, 3000, 10000),
        "uni_api": True,
        "v2_fee": 3000,
        "v2_swap_num": 997, "v2_swap_den": 1000,
        "gas_reserve": 0.00005,   # L2, gas murah
        "slug": "base",
        "dexscreener": "base",
        "gecko": "base",
        "gmgn": "base",
        "rpcs": [
            "https://mainnet.base.org",
            "https://base-rpc.publicnode.com",
            "https://base.drpc.org",
        ],
        "alchemy": "base-mainnet",
        "rpc_env": "RPC_8453",
        "explorer": "https://basescan.org",
        # Semua diverifikasi on-chain: npm.factory()==factory, npm.WETH9()==wrapped,
        # router.factory()==factory, router.WETH9()==wrapped,
        # v2_router.factory()==v2_factory, v2_router.WETH()==wrapped
        "factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        "npm": "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
        "router": "0x2626664c2603336E57B271c5C0b26F421741e481",
        "v2_factory": "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
        "v2_router": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        # v4 — posm/stateview/quoter .poolManager() semuanya == v4_pm (diverifikasi),
        # posm.permit2() == permit2 canonical
        "v4_pm": "0x498581fF718922c3f8e6A244956aF099B2652b2b",
        "v4_posm": "0x7C5f5A4bBd8fD63184577525326123B519429bDc",
        "v4_stateview": "0xA3c0c9b65baD0b08107Aa264b0f3dB444b867A71",
        "v4_quoter": "0x0d5e0F971ED27FBfF6c2837bf31316121532048D",
        "v4_router": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
        "v4_swap_hop_field": False,   # UniversalRouter standar, bukan build Robinhood
        "permit2": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
        "wrapped": "0x4200000000000000000000000000000000000006",
        "wrapped_symbol": "WETH",
        "native_symbol": "ETH",
        "quotes": {
            "WETH": "0x4200000000000000000000000000000000000006",
            "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        },
        "stable_syms": {"USDC"},
    },
    999: {
        "name": "HyperEVM",
        # DEX utama HyperSwap — fork Uniswap v3 lurus (fee tier standar). HyperEVM
        # ramai fork Solidly/Ramses (nest, kittenswap, ramses) yang fee-nya bebas
        # (858, 602, 1105, 22222) dan antarmuka factory-nya beda; itu TIDAK didukung.
        # prjx (TVL terbesar) fee-nya standar dan layak ditambah nanti sebagai DEX
        # kedua — alamat NPM/router-nya belum sempat diverifikasi on-chain.
        "dex": "HyperSwap",
        "fee_tiers": (100, 500, 3000, 10000),   # dibaca dari feeAmountTickSpacing()
        "uni_api": False,       # indexer Uniswap tidak mengindeks HyperEVM
        "v2_fee": 3000,
        "v2_swap_num": 997, "v2_swap_den": 1000,
        "gas_reserve": 0.02,    # gas dibayar HYPE; blok cepat, biaya per tx kecil
        "slug": "hyperevm",
        "dexscreener": "hyperevm",
        "gecko": "hyperevm",
        "gmgn": "hyperevm",
        "rpcs": [
            "https://rpc.hyperliquid.xyz/evm",
            "https://hyperliquid.drpc.org",
            "https://rpc.hyperlend.finance",
            "https://rpc.purroofgroup.com",
        ],
        "alchemy": "hyperliquid-mainnet",
        "rpc_env": "RPC_999",
        "explorer": "https://hyperevmscan.io",
        # HyperSwap, semua diverifikasi on-chain: npm.factory()==factory,
        # npm.WETH9()==wrapped, router.factory()==factory, router.WETH9()==wrapped,
        # v2_router.factory()==v2_factory, v2_router.WETH()==wrapped
        "factory": "0xB1c0fa0B789320044A6F623cFe5eBda9562602E3",
        "npm": "0x6eDA206207c09e5428F281761DdC0D300851fBC8",
        "router": "0x6D99e7f6747AF2cDbB5164b6DD50e40D4fDe1e77",
        "v2_factory": "0x724412C00059bf7d6ee7d4a1d0D5cd4de3ea1C48",
        "v2_router": "0xb4a9C4e6Ea8E2191d2FA5B380452a634Fb21240A",
        # Tidak ada Uniswap v4 di HyperEVM → has_v4() False, verify_v4/discover_v4_pools
        # fail-closed karena kunci v4_* memang tidak ada (sama seperti BSC).
        "permit2": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
        "wrapped": "0x5555555555555555555555555555555555555555",
        "wrapped_symbol": "WHYPE",
        "native_symbol": "HYPE",
        "quotes": {
            "WHYPE": "0x5555555555555555555555555555555555555555",
            "USDC": "0xb88339CB7199b77E23DB6E890353E22632Ba630f",
        },
        "stable_syms": {"USDC"},
    },
    56: {
        "name": "BSC",
        "dex": "PancakeSwap",
        # Pancake tidak punya fee 3000; punya 2500 (spacing 50) yang tidak ada di Uniswap.
        "fee_tiers": (100, 500, 2500, 10000),
        # Indexer resmi Uniswap tidak mengindeks pool PancakeSwap → discovery & daftar
        # posisi di BSC memakai scan RPC + dexscreener dan enumerasi NPM on-chain.
        "uni_api": False,
        "v2_fee": 2500,         # PancakeSwap V2 = 0.25%
        "v2_swap_num": 9975, "v2_swap_den": 10000,  # PancakeV2Pair.getAmountOut
        "gas_reserve": 0.001,   # fallback cadangan gas kalau harga gas tak terbaca
        "slug": "bsc",          # slug URL pancakeswap.finance
        "dexscreener": "bsc",
        "gecko": "bsc",
        "gmgn": "bsc",
        # PancakeSwap V2 (diverifikasi on-chain: v2_router.factory()==v2_factory,
        # v2_router.WETH()==wrapped)
        "v2_factory": "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
        # Semua diverifikasi: eth_chainId == 56 DAN eth_sendRawTransaction didukung.
        # rpc.48.club ditaruh duluan karena dioperasikan operator validator BSC —
        # tx-nya masuk jalur langsung ke pemilih blok. bsc-dataseed pernah terbukti
        # MENERIMA tx wrap lalu tidak pernah mempropagasikannya (tx hilang total dari
        # chain, lihat wait_ok). 1rpc.io dibuang: jawabannya bukan JSON yang sah.
        "rpcs": [
            "https://rpc.48.club",
            "https://bsc-dataseed.bnbchain.org",
            "https://bsc-rpc.publicnode.com",
            "https://bsc-dataseed1.defibit.io",
            "https://bsc-dataseed1.ninicoin.io",
        ],
        "alchemy": "bnb-mainnet",
        "rpc_env": "RPC_56",
        "explorer": "https://bscscan.com",
        # PancakeSwap V3 (docs.pancakeswap.finance; diverifikasi on-chain:
        # npm.factory() == factory, npm.WETH9() == wrapped)
        "factory": "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
        "npm": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
        # SmartRouter — dipakai karena ExactInputSingleParams-nya bentuk SwapRouter02
        # (tanpa deadline), persis ROUTER_ABI. JANGAN ganti ke SwapRouter V3
        # 0x1b81D678ffb9C0263b24A97847620C99d213eB14: strukturnya versi lama (pakai
        # deadline, selector 0x414bf389) sehingga calldata-nya tidak cocok.
        # Diverifikasi on-chain: router.factory() == factory.
        "router": "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
        "v2_router": "0x10ED43C718714eb63d5aA57B78B54704E256024E",
        # Tidak ada key v4_* → jalur v4 mati di chain ini (has_v4() False).
        # Padanan v4 di Pancake adalah "Infinity" (Vault + CLPoolManager), arsitektur
        # berbeda total dari Uniswap V4 dan belum didukung.
        # DEX kedua di chain ini. Kunci di sini MENIMPA kunci chain di atas untuk pool
        # yang berasal dari DEX ini; sisanya (rpc, quotes, wrapped, dst) diwarisi.
        # Semua alamat diverifikasi on-chain — lihat verify_dex().
        "dexes": {
            "Uniswap": {
                # indexer resmi Uniswap dipakai untuk SISI UNISWAP saja — itu satu-
                # satunya cara menemukan pool v4 ber-fee/spacing non-standar (0,35%
                # dsb) yang tak terjangkau scan tier standar
                "uni_api": True,
                "fee_tiers": (100, 500, 3000, 10000),   # Uniswap punya 3000, tidak punya 2500
                "v2_fee": 3000,
                "v2_swap_num": 997, "v2_swap_den": 1000,
                "slug": "bnb",                          # slug URL app.uniswap.org
                "factory": "0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7",
                "npm": "0x7b8A01B39D58278b5DE7e48c8449c9f4F5170613",
                "router": "0xB971eF87ede563556b2ED4b1C0b0019111Dd85d2",   # SwapRouter02
                "v2_factory": "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
                "v2_router": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
                "v4_pm": "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df",
                "v4_posm": "0x7a4a5c919ae2541aed11041a1aeee68f1287f95b",
                "v4_stateview": "0xd13dd3d6e93f276fafc9db9e6bb47c1180aee0c4",
                "v4_quoter": "0x9f75dd27d6664c475b90e105573e550ff69437b0",
                "v4_router": "0x1906c1d672b88cd1b9ac7593301ca990f94eae07",
                "permit2": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
            },
        },
        "wrapped": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "wrapped_symbol": "WBNB",
        "native_symbol": "BNB",
        "quotes": {
            "WBNB": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
            "USDT": "0x55d398326f99059fF775485246999027B3197955",
            "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        },
        "stable_syms": {"USDT", "USDC"},
    },
}


# ---------- Kemampuan per chain & per DEX ----------
# Satu chain bisa punya lebih dari satu DEX (BSC: PancakeSwap + Uniswap). Alamat
# kontrak karena itu milik POOL, bukan chain. dex_cfg() mengembalikan konfigurasi
# efektif: kunci chain, ditimpa kunci DEX yang bersangkutan.
def dex_name(chain_id: int) -> str:
    """DEX utama chain ini (dipakai kalau pool tidak menyebut asalnya)."""
    return CHAINS[chain_id].get("dex", "Uniswap")


def dex_names(chain_id: int) -> tuple[str, ...]:
    """Semua DEX di chain ini, yang utama di depan."""
    return (dex_name(chain_id), *(CHAINS[chain_id].get("dexes") or {}).keys())


def dex_cfg(chain_id: int, dex: str | None = None) -> dict:
    """Konfigurasi efektif untuk satu DEX. Tanpa argumen = DEX utama."""
    cfg = CHAINS[chain_id]
    if not dex or dex == dex_name(chain_id):
        return cfg
    extra = (cfg.get("dexes") or {}).get(dex)
    if extra is None:
        raise RuntimeError(f"DEX '{dex}' tidak dikenal di chain {chain_id}")
    return {**cfg, **extra, "dex": dex}


def pool_cfg(chain_id: int, p: dict) -> dict:
    """Konfigurasi untuk pool/posisi tertentu — SELALU pakai ini di jalur transaksi,
    jangan CHAINS[chain_id] langsung, kalau tidak pool Uniswap akan dieksekusi
    memakai kontrak PancakeSwap (bisa masuk ke pool yang salah, bukan cuma gagal)."""
    return dex_cfg(chain_id, (p or {}).get("dex"))


def has_v4(chain_id: int, dex: str | None = None) -> bool:
    """True kalau DEX ini punya deployment v4 yang didukung. PancakeSwap tidak punya
    kontrak kompatibel-v4 (padanannya 'Infinity', arsitektur beda)."""
    return bool(dex_cfg(chain_id, dex).get("v4_pm"))


def any_has_v4(chain_id: int) -> bool:
    return any(has_v4(chain_id, d) for d in dex_names(chain_id))


def versions_label(chain_id: int) -> str:
    """'v2/v3/v4' atau 'v2/v3' — dipakai di pesan discovery kedua UI."""
    return "v2/v3/v4" if any_has_v4(chain_id) else "v2/v3"


def fee_tiers(chain_id: int, dex: str | None = None) -> tuple:
    return tuple(dex_cfg(chain_id, dex).get("fee_tiers") or (100, 500, 3000, 10000))


GAS_UNITS_FULL_FLOW = 1_500_000   # wrap + approve×3 + swap×3 + addLiquidity, token boros


def sort_tokens(a: str, b: str) -> tuple[str, str]:
    """Urutkan sepasang alamat token seperti kontrak melakukannya: menurut NILAI.

    JANGAN pakai sorted([a, b]) untuk alamat checksum. Itu membandingkan string,
    dan di ASCII huruf besar (A-F = 0x41-0x46) lebih kecil dari huruf kecil
    (a-f = 0x61-0x66), sehingga urutannya bisa terbalik dari urutan numerik.
    Contoh nyata: '0xF74548…' (memes) terbaca lebih kecil dari '0xbb4CdB…' (WBNB)
    padahal 0xbb < 0xf7. Akibatnya quote_is_token1 terbalik, harga jadi kebalikannya,
    dan TVL pool memes/WBNB terbaca $321 TRILIUN.
    Aman untuk alamat yang sudah lowercase semua — tapi jangan bergantung pada itu."""
    return (a, b) if int(a, 16) < int(b, 16) else (b, a)


def gas_reserve_wei(chain_id: int, w3: Web3 | None = None) -> int:
    """Native yang tidak boleh ikut dipakai jadi modal — cadangan gas.

    Dihitung dari harga gas SEKARANG (2× satu alur mint penuh), bukan angka mati:
    di BSC 0,05 gwei satu alur penuh cuma ~0,00006 BNB (~$0,04), jadi cadangan tetap
    sebesar 0,003 BNB akan mengunci sebagian besar saldo wallet kecil tanpa guna —
    sementara kalau harga gas naik ke level lama (3 gwei) justru kurang.
    Di-clamp ke [1/5, 5×] nilai fallback config supaya tidak ekstrem ke dua arah."""
    fallback = int(float(CHAINS[chain_id].get("gas_reserve", 0.001)) * 1e18)
    if w3 is None:
        return fallback
    try:
        est = int(w3.eth.gas_price * GAS_UNITS_FULL_FLOW * 2)
    except Exception:
        return fallback
    return min(max(est, fallback // 5), fallback * 5)


# ---------- ABIs minimal ----------
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "o", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function", "stateMutability": "view"},
    {"constant": False, "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function", "stateMutability": "nonpayable"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "type": "function", "stateMutability": "view"},
]

WETH_ABI = ERC20_ABI + [
    {"constant": False, "inputs": [], "name": "deposit", "outputs": [], "type": "function", "stateMutability": "payable"},
    {"constant": False, "inputs": [{"name": "wad", "type": "uint256"}], "name": "withdraw",
     "outputs": [], "type": "function", "stateMutability": "nonpayable"},
]

FACTORY_ABI = [
    {"constant": True, "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "address"}, {"name": "", "type": "uint24"}], "name": "getPool", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
]

POOL_ABI = [
    {"constant": True, "inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"},
        # feeProtocol: uint8 di Uniswap V3, uint32 di PancakeSwap V3 (fee0 | fee1<<16,
        # nilai nyatanya ratusan juta). uint32 mendekode keduanya — dengan uint8,
        # eth-abi menolak padding tidak nol dan SEMUA pool Pancake gagal dibaca.
        {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint32"},
        {"name": "unlocked", "type": "bool"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [], "name": "liquidity", "outputs": [{"name": "", "type": "uint128"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [], "name": "token1", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [], "name": "fee", "outputs": [{"name": "", "type": "uint24"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [], "name": "tickSpacing", "outputs": [{"name": "", "type": "int24"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [{"name": "secondsAgos", "type": "uint32[]"}], "name": "observe", "outputs": [
        {"name": "tickCumulatives", "type": "int56[]"},
        {"name": "secondsPerLiquidityCumulativeX128s", "type": "uint160[]"}], "type": "function", "stateMutability": "view"},
]

NPM_ABI = [
    # burn + multicall dipakai membersihkan NFT posisi kosong (lihat burn_empty()).
    # burn hanya lolos kalau liquidity DAN tokensOwed dua-duanya 0 — kontraknya
    # sendiri yang menjaga, jadi tidak mungkin membakar posisi yang masih berisi.
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "burn",
     "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "data", "type": "bytes[]"}], "name": "multicall",
     "outputs": [{"name": "results", "type": "bytes[]"}],
     "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "ownerOf",
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"components": [
        {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"}, {"name": "tickUpper", "type": "int24"},
        {"name": "amount0Desired", "type": "uint256"}, {"name": "amount1Desired", "type": "uint256"},
        {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
        {"name": "recipient", "type": "address"}, {"name": "deadline", "type": "uint256"}],
        "name": "params", "type": "tuple"}], "name": "mint",
     "outputs": [{"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"},
                 {"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}],
     "type": "function", "stateMutability": "payable"},
    {"inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "index", "type": "uint256"}], "name": "tokenOfOwnerByIndex", "outputs": [{"name": "", "type": "uint256"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "positions", "outputs": [
        {"name": "nonce", "type": "uint96"}, {"name": "operator", "type": "address"},
        {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"}, {"name": "tickUpper", "type": "int24"},
        {"name": "liquidity", "type": "uint128"},
        {"name": "feeGrowthInside0LastX128", "type": "uint256"}, {"name": "feeGrowthInside1LastX128", "type": "uint256"},
        {"name": "tokensOwed0", "type": "uint128"}, {"name": "tokensOwed1", "type": "uint128"}],
     "type": "function", "stateMutability": "view"},
    {"inputs": [{"components": [
        {"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"},
        {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
        {"name": "deadline", "type": "uint256"}], "name": "params", "type": "tuple"}],
     "name": "decreaseLiquidity", "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}],
     "type": "function", "stateMutability": "payable"},
    {"inputs": [{"components": [
        {"name": "tokenId", "type": "uint256"},
        {"name": "amount0Desired", "type": "uint256"}, {"name": "amount1Desired", "type": "uint256"},
        {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
        {"name": "deadline", "type": "uint256"}], "name": "params", "type": "tuple"}],
     "name": "increaseLiquidity", "outputs": [{"name": "liquidity", "type": "uint128"},
        {"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}],
     "type": "function", "stateMutability": "payable"},
    {"inputs": [{"components": [
        {"name": "tokenId", "type": "uint256"}, {"name": "recipient", "type": "address"},
        {"name": "amount0Max", "type": "uint128"}, {"name": "amount1Max", "type": "uint128"}],
        "name": "params", "type": "tuple"}], "name": "collect",
     "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}],
     "type": "function", "stateMutability": "payable"},
]

V2_FACTORY_ABI = [
    {"constant": True, "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "address"}], "name": "getPair", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
]

V2_PAIR_ABI = [
    {"constant": True, "inputs": [], "name": "getReserves", "outputs": [{"name": "r0", "type": "uint112"}, {"name": "r1", "type": "uint112"}, {"name": "ts", "type": "uint32"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"constant": True, "inputs": [], "name": "token1", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
]

V2_ROUTER_ABI = [
    {"inputs": [], "name": "factory", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"inputs": [], "name": "WETH", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "path", "type": "address[]"}],
     "name": "getAmountsOut", "outputs": [{"name": "amounts", "type": "uint256[]"}],
     "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"}, {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "name": "swapExactTokensForTokens", "outputs": [{"name": "amounts", "type": "uint256[]"}],
     "type": "function", "stateMutability": "nonpayable"},
    # Varian untuk token fee-on-transfer: pair mengecek invarian K dari saldo yang
    # BENAR-BENAR sampai, sedangkan swapExactTokensForTokens memakai hasil
    # getAmountsOut yang menganggap tidak ada pajak → revert "Pancake: K".
    {"inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"}, {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "name": "swapExactTokensForTokensSupportingFeeOnTransferTokens", "outputs": [],
     "type": "function", "stateMutability": "nonpayable"},
    {"inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"},
                {"name": "amountADesired", "type": "uint256"}, {"name": "amountBDesired", "type": "uint256"},
                {"name": "amountAMin", "type": "uint256"}, {"name": "amountBMin", "type": "uint256"},
                {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
     "name": "addLiquidity", "outputs": [{"name": "amountA", "type": "uint256"},
                                         {"name": "amountB", "type": "uint256"}, {"name": "liquidity", "type": "uint256"}],
     "type": "function", "stateMutability": "nonpayable"},
    {"inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"},
                {"name": "liquidity", "type": "uint256"},
                {"name": "amountAMin", "type": "uint256"}, {"name": "amountBMin", "type": "uint256"},
                {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
     "name": "removeLiquidity", "outputs": [{"name": "amountA", "type": "uint256"}, {"name": "amountB", "type": "uint256"}],
     "type": "function", "stateMutability": "nonpayable"},
]

# ---------- Uniswap V4 ABIs ----------
_POOLKEY_COMPONENTS = [
    {"name": "currency0", "type": "address"}, {"name": "currency1", "type": "address"},
    {"name": "fee", "type": "uint24"}, {"name": "tickSpacing", "type": "int24"},
    {"name": "hooks", "type": "address"},
]

V4_POSM_ABI = [
    {"inputs": [], "name": "poolManager", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"inputs": [], "name": "permit2", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"inputs": [], "name": "nextTokenId", "outputs": [{"name": "", "type": "uint256"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "ownerOf", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "getPositionLiquidity",
     "outputs": [{"name": "liquidity", "type": "uint128"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "getPoolAndPositionInfo",
     "outputs": [{"components": _POOLKEY_COMPONENTS, "name": "poolKey", "type": "tuple"},
                 {"name": "info", "type": "uint256"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "unlockData", "type": "bytes"}, {"name": "deadline", "type": "uint256"}],
     "name": "modifyLiquidities", "outputs": [], "type": "function", "stateMutability": "payable"},
]

V4_STATEVIEW_ABI = [
    {"inputs": [], "name": "poolManager", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "poolId", "type": "bytes32"}], "name": "getSlot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
        {"name": "protocolFee", "type": "uint24"}, {"name": "lpFee", "type": "uint24"}],
     "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "poolId", "type": "bytes32"}], "name": "getLiquidity",
     "outputs": [{"name": "", "type": "uint128"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "poolId", "type": "bytes32"}, {"name": "tickLower", "type": "int24"},
                {"name": "tickUpper", "type": "int24"}], "name": "getFeeGrowthInside",
     "outputs": [{"name": "feeGrowthInside0X128", "type": "uint256"}, {"name": "feeGrowthInside1X128", "type": "uint256"}],
     "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "poolId", "type": "bytes32"}, {"name": "owner", "type": "address"},
                {"name": "tickLower", "type": "int24"}, {"name": "tickUpper", "type": "int24"},
                {"name": "salt", "type": "bytes32"}], "name": "getPositionInfo",
     "outputs": [{"name": "liquidity", "type": "uint128"},
                 {"name": "feeGrowthInside0LastX128", "type": "uint256"},
                 {"name": "feeGrowthInside1LastX128", "type": "uint256"}],
     "type": "function", "stateMutability": "view"},
]

PERMIT2_ABI = [
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "token", "type": "address"},
                {"name": "spender", "type": "address"}], "name": "allowance",
     "outputs": [{"name": "amount", "type": "uint160"}, {"name": "expiration", "type": "uint48"},
                 {"name": "nonce", "type": "uint48"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "token", "type": "address"}, {"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint160"}, {"name": "expiration", "type": "uint48"}],
     "name": "approve", "outputs": [], "type": "function", "stateMutability": "nonpayable"},
]

V4_QUOTER_ABI = [
    {"inputs": [], "name": "poolManager", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"components": [
        {"components": _POOLKEY_COMPONENTS, "name": "poolKey", "type": "tuple"},
        {"name": "zeroForOne", "type": "bool"},
        {"name": "exactAmount", "type": "uint128"},
        {"name": "hookData", "type": "bytes"}], "name": "params", "type": "tuple"}],
     "name": "quoteExactInputSingle",
     "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "gasEstimate", "type": "uint256"}],
     "type": "function", "stateMutability": "nonpayable"},
]

V4_UR_ABI = [
    {"inputs": [], "name": "poolManager", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
    {"inputs": [{"name": "commands", "type": "bytes"}, {"name": "inputs", "type": "bytes[]"},
                {"name": "deadline", "type": "uint256"}],
     "name": "execute", "outputs": [], "type": "function", "stateMutability": "payable"},
]

# v4-periphery Actions (github.com/Uniswap/v4-periphery Actions.sol)
V4_INCREASE, V4_DECREASE, V4_MINT, V4_BURN = 0x00, 0x01, 0x02, 0x03
V4_SWAP_IN_SINGLE, V4_SETTLE_ALL, V4_SETTLE_PAIR = 0x06, 0x0C, 0x0D
V4_TAKE_ALL, V4_TAKE_PAIR, V4_SWEEP = 0x0F, 0x11, 0x14
UR_CMD_V4_SWAP = 0x10
V4_NATIVE = "0x0000000000000000000000000000000000000000"
V4_FEE_SPACINGS = ((100, 1), (500, 10), (3000, 60), (10000, 200))

# SwapRouter02: exactInputSingle TANPA field deadline (beda dari SwapRouter v1)
ROUTER_ABI = [
    {"inputs": [{"components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}], "name": "params", "type": "tuple"}],
     "name": "exactInputSingle", "outputs": [{"name": "amountOut", "type": "uint256"}],
     "type": "function", "stateMutability": "payable"},
    {"inputs": [], "name": "factory", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
]

ERC721_TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
INCREASE_LIQ_TOPIC = Web3.keccak(text="IncreaseLiquidity(uint256,uint128,uint256,uint256)").hex()


def _increase_amounts(receipt, npm_addr: str) -> tuple[int, int] | None:
    """(amount0, amount1) aktual yang masuk posisi, dari event IncreaseLiquidity NPM."""
    for log in receipt.logs:
        if (log.address.lower() == npm_addr.lower() and log.topics
                and log.topics[0].hex().removeprefix("0x") == INCREASE_LIQ_TOPIC.removeprefix("0x")):
            d = log.data.hex().removeprefix("0x")
            if len(d) >= 192:
                return int(d[64:128], 16), int(d[128:192], 16)
    return None


# ---------- Helpers matematika tick/price ----------
def tick_to_price(tick: int) -> float:
    return 1.0001 ** tick


def price_to_tick(price: float) -> int:
    return math.floor(math.log(price) / math.log(1.0001))


def round_down(tick: int, spacing: int) -> int:
    return (tick // spacing) * spacing


def round_up(tick: int, spacing: int) -> int:
    return -((-tick) // spacing) * spacing


def amounts_from_liquidity(liquidity: int, sqrtp_x96: int, tick_lower: int, tick_upper: int) -> tuple[float, float]:
    """Jumlah (token0, token1) raw dari liquidity posisi pada harga sekarang."""
    sa = math.sqrt(1.0001 ** tick_lower)
    sb = math.sqrt(1.0001 ** tick_upper)
    sp = sqrtp_x96 / Q96
    if sp <= sa:
        return liquidity * (sb - sa) / (sa * sb), 0.0
    if sp >= sb:
        return 0.0, liquidity * (sb - sa)
    return liquidity * (sb - sp) / (sp * sb), liquidity * (sp - sa)


def liquidity_for_amounts(sqrtp_x96: int, tick_lower: int, tick_upper: int,
                          amount0: int, amount1: int) -> float:
    """Liquidity maksimal dari pasangan amount (kebalikan amounts_from_liquidity)."""
    sa = math.sqrt(1.0001 ** tick_lower)
    sb = math.sqrt(1.0001 ** tick_upper)
    sp = sqrtp_x96 / Q96
    if sp <= sa:
        return amount0 * (sa * sb) / (sb - sa)
    if sp >= sb:
        return amount1 / (sb - sa)
    return min(amount0 * (sp * sb) / (sb - sp), amount1 / (sp - sa))


# ---------- Formatting ----------
_SUB = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def fmt_price(p: float) -> str:
    """0.0000131 → 0.0₄131 (gaya subscript seperti UI trading)."""
    if p == 0:
        return "0"
    if p >= 0.001:
        return f"{p:.6g}"
    s = f"{p:.20f}".split(".")[1]
    zeros = len(s) - len(s.lstrip("0"))
    digits = s[zeros:zeros + 3].rstrip("0") or "0"
    return f"0.0{str(zeros).translate(_SUB)}{digits}"


def fmt_usd(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"${v / 1_000:.1f}k"
    return f"${v:.2f}"


def fmt_amount(v: float) -> str:
    if v == 0:
        return "0"
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 0.0001:
        return f"{v:.6g}"
    return f"{v:.4e}"


def _rpc_retry() -> Retry:
    """Retry otomatis untuk rate limit / gangguan sesaat RPC (Alchemy 429 dst).
    Backoff 0.6→9.6 detik, hormati header Retry-After. Aman untuk JSON-RPC:
    request read idempoten; eth_sendRawTransaction kirim bytes yang sama
    (hash tx sama) jadi re-broadcast tidak dobel."""
    return Retry(total=6, backoff_factor=0.6, status_forcelist=(429, 502, 503, 504),
                 allowed_methods=None, respect_retry_after_header=True)


# Listing posisi menembak banyak read paralel (per posisi: slot0 + collect +
# supply, plus cost_basis nested). Pool default requests = 10 → "pool full,
# discarding connection" lalu koneksi dibuka ulang tiap kali = lambat. Besarkan.
_POOL = 32


def _rpc_session() -> requests.Session:
    s = requests.Session()
    a = HTTPAdapter(max_retries=_rpc_retry(), pool_connections=_POOL, pool_maxsize=_POOL)
    s.mount("https://", a)
    s.mount("http://", a)
    return s


# ---------- Bypass blokir DNS ISP (DNS-over-HTTPS + koneksi langsung ke IP) ----------
class _SNIAdapter(HTTPAdapter):
    """Konek ke IP tapi SNI + verifikasi cert tetap pakai hostname asli."""

    def __init__(self, hostname: str):
        self._hostname = hostname
        super().__init__(max_retries=_rpc_retry(), pool_connections=_POOL, pool_maxsize=_POOL)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["server_hostname"] = self._hostname
        kwargs["assert_hostname"] = self._hostname
        return super().init_poolmanager(*args, **kwargs)


def _doh_resolve(host: str) -> str | None:
    """Resolve A record via DNS-over-HTTPS (lolos dari DNS ISP yang diracuni)."""
    for doh in ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query"):
        try:
            r = requests.get(doh, params={"name": host, "type": "A"},
                             headers={"accept": "application/dns-json"}, timeout=10)
            for a in r.json().get("Answer", []):
                if a.get("type") == 1:
                    return a["data"]
        except Exception:
            continue
    return None


def _forced_ip_w3(rpc_url: str) -> Web3 | None:
    u = urlparse(rpc_url)
    if u.scheme != "https" or not u.hostname:
        return None
    ip = _doh_resolve(u.hostname)
    if not ip:
        return None
    session = requests.Session()
    session.mount(f"https://{ip}", _SNIAdapter(u.hostname))
    session.headers["Host"] = u.hostname
    ip_url = rpc_url.replace(u.hostname, ip, 1)
    provider = Web3.HTTPProvider(ip_url, request_kwargs={"timeout": 30}, session=session)
    provider.cache_allowed_requests = True  # eth_chainId dkk tidak di-query berulang
    return _poa(Web3(provider))


# ---------- Koneksi & util dasar ----------
_W3_CACHE: dict[int, tuple[Web3, float]] = {}
_NONCE_NEXT: dict[str, int] = {}  # alamat → nonce berikutnya (pelacak lokal utk tx beruntun)
_LAST_TX: dict[str, str] = {}     # alamat → hash tx terakhir yang kita siarkan
_LAST_RAW: dict[str, bytes] = {}  # hash → raw signed tx, untuk siar ulang kalau hilang


def _poa(w3: Web3) -> Web3:
    """Pasang middleware PoA. Tanpa ini `eth_getBlock` di BSC SELALU melempar
    ExtraDataLengthError (extraData 280 byte) — chart harga dan pembacaan timestamp
    blok mati diam-diam di chain itu. Aman untuk chain non-PoA: middleware ini cuma
    memangkas extraData yang kepanjangan."""
    if _POA is not None:
        try:
            w3.middleware_onion.inject(_POA, layer=0)
        except Exception:
            pass
    return w3


def get_w3(chain_id: int, fresh: bool = False) -> Web3:
    """Failover multi-RPC: coba tiap endpoint (env override dulu), verifikasi
    chain_id, cache yang jalan 5 menit."""
    hit = _W3_CACHE.get(chain_id)
    if hit and not fresh and time.time() - hit[1] < 300:
        return hit[0]
    cfg = CHAINS[chain_id]
    rpcs = []
    if os.environ.get(cfg["rpc_env"]):
        rpcs.append(os.environ[cfg["rpc_env"]])
    # Alchemy prioritas kalau API key ada (host g.alchemy.com tidak kena blokir DNS ISP)
    akey = os.environ.get("ALCHEMY_API_KEY", "").strip()
    if akey and cfg.get("alchemy"):
        rpcs.append(f"https://{cfg['alchemy']}.g.alchemy.com/v2/{akey}")
    rpcs += cfg["rpcs"]
    errs = []
    for rpc in rpcs:
        provider = Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}, session=_rpc_session())
        provider.cache_allowed_requests = True  # eth_chainId dkk tidak di-query berulang
        candidates = [_poa(Web3(provider))]
        for i, w3 in enumerate(candidates):
            try:
                if w3.eth.chain_id == chain_id:
                    _W3_CACHE[chain_id] = (w3, time.time())
                    return w3
                errs.append(f"{rpc}: chain_id salah")
            except Exception as e:
                errs.append(f"{rpc}{' (via IP)' if i else ''}: {type(e).__name__}")
                # koneksi normal gagal → coba bypass DNS ISP via DoH + IP langsung
                if i == 0:
                    forced = _forced_ip_w3(rpc)
                    if forced is not None:
                        candidates.append(forced)
    raise RuntimeError(f"Semua RPC {cfg['name']} gagal — " + " | ".join(errs))


def calldata(fn) -> bytes:
    """Encode calldata ContractFunction (web3 v6/v7 kompatibel)."""
    return fn._encode_transaction_data()


def erc20(w3: Web3, addr: str):
    return w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)


_TOKEN_CACHE: dict[tuple, dict] = {}


def token_info(w3: Web3, addr: str) -> dict:
    key = (w3.provider.endpoint_uri, addr.lower())
    hit = _TOKEN_CACHE.get(key)
    if hit:
        return hit
    c = erc20(w3, addr)
    info = {"address": Web3.to_checksum_address(addr), "decimals": c.functions.decimals().call(),
            "symbol": c.functions.symbol().call()}
    _TOKEN_CACHE[key] = info
    return info


def _tx_known(w3: Web3, txhash: str | None) -> bool:
    """True kalau node masih mengenal tx ini (pending maupun sudah ter-mine)."""
    if not txhash:
        return False
    try:
        return w3.eth.get_transaction(txhash) is not None
    except Exception:
        return False


def send_tx(w3: Web3, pk: str, tx: dict) -> str:
    account = w3.eth.account.from_key(pk)
    tx["to"] = Web3.to_checksum_address(tx["to"])
    tx["from"] = account.address
    tx["chainId"] = w3.eth.chain_id
    tx.setdefault("value", 0)
    try:
        est = w3.eth.estimate_gas(tx)
        # Buffer 1.3x SAJA tidak aman: sebagian token memecoin punya logika pajak /
        # reflection yang cuma terpicu pada kondisi tertentu (mis. saldo pajak
        # menembus ambang → token menjual sendiri ke BNB). Biaya transfernya lalu
        # melonjak ratusan ribu gas ANTARA estimasi dan eksekusi — terukur di RTX
        # (0x2Ec3…): 51k s/d 357k gas untuk satu transfer, dan satu addLiquidity
        # gagal kehabisan gas di limit 368k padahal estimasinya 283k. Karena gas
        # yang tak terpakai dikembalikan, kelebihan limit praktis gratis.
        tx["gas"] = max(int(est * 1.3), est + 300_000)
    except ContractLogicError:
        raise
    except Exception:
        tx.setdefault("gas", 800_000)
    try:
        base = w3.eth.gas_price
        tip = w3.to_wei("0.1", "gwei")
        tx["maxFeePerGas"] = base * 2 + tip
        tx["maxPriorityFeePerGas"] = tip
    except Exception:
        tx["gasPrice"] = w3.eth.gas_price

    # Nonce: replika RPC sering telat sinkron setelah tx beruntun (close→swap→mint),
    # jadi lacak sendiri nonce berikutnya per alamat dan ambil yang tertinggi.
    # Catatan: web3 v7 melempar Web3RPCError (bukan ValueError) untuk error RPC,
    # makanya except-nya harus generik — dicek dari pesan.
    last_err = None
    addr_lc = account.address.lower()
    for attempt in range(5):
        rpc_n = w3.eth.get_transaction_count(account.address, "pending")
        # Pelacak lokal hanya boleh mendahului RPC kalau tx terakhir kita MEMANG masih
        # dikenal node. Kalau tx itu sudah hilang (di-drop mempool / tidak
        # terpropagasi), mendahului = membuat LUBANG nonce, dan tx ini beserta semua
        # tx sesudahnya tidak akan pernah bisa di-mine.
        n = rpc_n
        tracked = _NONCE_NEXT.get(addr_lc, 0)
        if tracked > rpc_n:
            if _tx_known(w3, _LAST_TX.get(addr_lc)):
                n = min(tracked, rpc_n + 3)
            else:
                _NONCE_NEXT.pop(addr_lc, None)
        tx["nonce"] = n
        signed = w3.eth.account.sign_transaction(tx, pk)
        try:
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            _NONCE_NEXT[addr_lc] = n + 1
            hh = "0x" + h.hex().removeprefix("0x")
            _LAST_TX[addr_lc] = hh
            if len(_LAST_RAW) > 50:
                _LAST_RAW.clear()
            _LAST_RAW[hh] = signed.raw_transaction
            _fanout_async(w3, signed.raw_transaction)
            return hh
        except Exception as e:
            s = str(e).lower()
            if "already known" in s or "already exists" in s or "known transaction" in s:
                _NONCE_NEXT[addr_lc] = n + 1
                return "0x" + signed.hash.hex().removeprefix("0x")
            if "nonce too low" in s or ("nonce" in s and "low" in s):
                # negara chain sudah lewat — sinkronkan cache lalu ulang
                _NONCE_NEXT[addr_lc] = max(_NONCE_NEXT.get(addr_lc, 0), n + 1)
                last_err = e
                time.sleep(2)
                continue
            if "replacement transaction underpriced" in s or "nonce too high" in s:
                _NONCE_NEXT.pop(addr_lc, None)  # cache salah arah — reset, percaya RPC
                last_err = e
                time.sleep(2)
                continue
            raise
    raise RuntimeError(f"Gagal kirim tx setelah 5 percobaan (nonce): {last_err}")


def poll_balance(w3: Web3, token: str, addr: str, min_expected: int,
                 tries: int = 10, delay: float = 0.7) -> int:
    """Baca saldo dengan retry — replika RPC bisa telat sinkron sesaat setelah tx
    (read-after-write). Berhenti begitu saldo >= min_expected atau kehabisan percobaan."""
    bal = 0
    for i in range(tries):
        try:
            bal = erc20(w3, token).functions.balanceOf(Web3.to_checksum_address(addr)).call()
        except Exception:
            bal = 0
        if bal >= min_expected:
            return bal
        time.sleep(delay)
    return bal


# ---------- Laporan langkah ke UI ----------
# Satu alur mint/close/rebalance itu 3–5 tx berurutan yang totalnya bisa memakan
# menit. Tanpa laporan, UI cuma menampilkan satu pesan diam dan user tidak bisa tahu
# langkah mana yang menggantung — keluhannya jadi "stuck lama lalu gagal" tanpa
# petunjuk. Global aman di sini karena SEMUA alur tx diserialisasi TX_LOCK di
# masing-masing proses (asyncio.Lock di bot.py, threading.Lock di web.py).
_PROGRESS = None


def set_progress(fn) -> None:
    """Pasang/lepas (fn=None) sink laporan langkah. fn dipanggil dari thread kerja,
    jadi ia harus murah dan tidak boleh melempar."""
    global _PROGRESS
    _PROGRESS = fn


def _step(msg: str) -> None:
    if _PROGRESS is None:
        return
    try:
        _PROGRESS(msg)
    except Exception:
        pass


def _peer_session() -> requests.Session:
    """Sesi khusus siar ulang: TANPA retry, timeout pendek. `_rpc_session()` memakai
    `Retry(total=6, backoff 0.6→9.6s)` — untuk endpoint mati itu ~40 detik per
    request, dan terukur 116 detik hanya untuk membangun daftar peer chain 4663.
    Di sini kegagalan justru harus murah: fungsi pemakainya jalan di dalam loop
    tunggu tx."""
    s = requests.Session()
    a = HTTPAdapter(max_retries=0, pool_connections=4, pool_maxsize=4)
    s.mount("https://", a)
    s.mount("http://", a)
    return s


def _peer_w3s(chain_id: int, skip_uri: str = "", _cache={}) -> list:
    """Endpoint LAIN yang hidup di chain ini, untuk menyebar siar ulang tx.

    RPC publik BSC lazim menerima tx lalu tidak mempropagasikannya; menyuntikkannya
    ulang lewat endpoint berbeda itu yang menolong.

    Sengaja TIDAK diprobe dulu. Blockscout eth-rpc Robinhood menjawab **429** untuk
    `eth_chainId` (rate limit, bukan mati) — probe apa pun akan membuangnya, padahal
    satu tx per 20 detik masih lolos di sana. Endpoint yang benar-benar mati gagal
    murah (terukur 0,06 detik untuk host yang diblokir DNS ISP) karena sesinya tanpa
    retry, jadi mencoba lebih murah daripada memilah. Tx yang nyasar ke chain lain
    ditolak sendiri oleh tanda tangannya."""
    key = (chain_id, skip_uri)
    hit = _cache.get(key)
    if hit:
        return hit[0]
    cfg = CHAINS[chain_id]
    urls = []
    if os.environ.get(cfg["rpc_env"]):
        urls.append(os.environ[cfg["rpc_env"]])
    akey = os.environ.get("ALCHEMY_API_KEY", "").strip()
    if akey and cfg.get("alchemy"):
        urls.append(f"https://{cfg['alchemy']}.g.alchemy.com/v2/{akey}")
    urls += cfg["rpcs"]
    out = []
    for u in urls:
        if skip_uri and u == skip_uri:
            continue
        try:
            out.append(Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 4},
                                              session=_peer_session())))
        except Exception:
            continue
    _cache[key] = (out, time.time())
    return out


def _fanout_async(w3: Web3, raw) -> None:
    """Sebar raw tx ke endpoint LAIN sekarang juga, di thread terpisah.

    Tanpa ini tx cuma duduk di satu node sampai ronde siar ulang pertama (20 detik).
    Terbukti perlu di BSC: tx wrap 0xdde477e4… diterima bsc-dataseed (hash-nya
    kembali normal) lalu tidak pernah dipropagasikan — 180 detik kemudian tx itu
    tidak dikenal node mana pun, bukan sekadar belum di-mine.

    Dijalankan di thread supaya send_tx tidak ikut menunggu (satu ronde ke semua
    endpoint terukur ~4 detik); kegagalan diabaikan, ini murni usaha tambahan."""
    try:
        cid = w3.eth.chain_id
    except Exception:
        return
    uri = getattr(w3.provider, "endpoint_uri", "") or ""

    def go():
        for p in _peer_w3s(cid, uri):
            try:
                p.eth.send_raw_transaction(raw)
            except Exception:
                pass

    threading.Thread(target=go, daemon=True).start()


def _rebroadcast(w3: Web3, raw) -> None:
    """Siarkan ulang raw tx ke node aktif DAN endpoint lain yang hidup. Nonce dan
    tanda tangannya identik, jadi tidak mungkin jadi tx kedua — paling banter
    dijawab "already known"."""
    if raw is None:
        return
    try:
        w3.eth.send_raw_transaction(raw)
    except Exception:
        pass
    try:
        peers = _peer_w3s(w3.eth.chain_id, getattr(w3.provider, "endpoint_uri", "") or "")
    except Exception:
        return
    for p in peers:
        try:
            p.eth.send_raw_transaction(raw)
        except Exception:
            pass


def wait_ok(w3: Web3, txhash: str, what: str, total_wait: int = 180):
    """Tunggu receipt sambil menyiarkan ulang tx BERKALA (tiap ~20 detik).

    Penyebab gagal di titik ini bukan gas kurang — base fee Robinhood terukur rata
    0,02 gwei dan tx dikirim dengan tip 0,1 gwei (5x) — melainkan tx hilang dari
    mempool: node tidak mempropagasikannya atau meng-evict-nya. Dulu siar ulangnya
    cuma SEKALI di detik ke-90; sekarang ~8 kali dalam jendela yang sama, karena
    blok chain ini sub-detik dan tx yang belum masuk 20 detik memang bukan sekadar
    "masih antre"."""
    raw = _LAST_RAW.get(txhash)
    started = time.time()
    deadline = started + total_wait
    r = None
    _step(f"⏳ {what} terkirim, menunggu masuk blok…")
    while True:
        left = deadline - time.time()
        if left <= 0:
            break
        try:
            r = w3.eth.wait_for_transaction_receipt(txhash, timeout=max(5, min(20, left)))
            break
        except Exception:
            _rebroadcast(w3, raw)
            _step(f"↻ {what} belum masuk blok setelah {int(time.time() - started)}s — disiarkan ulang")
    if r is not None and r.status == 1:
        _step(f"✅ {what} beres ({int(time.time() - started)}s)")
    if r is None:
        # Menyerah. WAJIB reset pelacak nonce: kalau tidak, tx berikutnya lahir
        # dengan lubang nonce dan ikut mati satu per satu.
        _NONCE_NEXT.clear()
        _LAST_TX.clear()
        raise RuntimeError(
            f"Tx {what} tidak masuk chain setelah {total_wait} detik dan sudah disiarkan "
            f"ulang berkali-kali — kemungkinan besar dibuang mempool RPC. "
            f"Tidak ada dana yang berpindah di langkah ini; ulangi saja. ({txhash})")
    if r.status != 1:
        hint = ""
        try:
            # gasUsed mepet limit = kehabisan gas, bukan require() yang gagal.
            # Bedanya penting: yang satu tinggal naikkan limit, yang satu salah angka.
            lim = w3.eth.get_transaction(txhash)["gas"]
            if r.gasUsed >= lim * 0.97:
                hint = (f" — kehabisan gas (pakai {r.gasUsed:,} dari limit {lim:,}). "
                        f"Token ini boros gas dan biayanya berubah-ubah; coba ulang.")
        except Exception:
            pass
        raise RuntimeError(f"Tx {what} FAILED: {txhash}{hint}")
    return r


def tx_link(chain_id: int, h: str) -> str:
    return f"{CHAINS[chain_id]['explorer']}/tx/{h}"


def pos_link(chain_id: int, token_id: int) -> str:
    cfg = CHAINS[chain_id]
    if cfg.get("dex") == "PancakeSwap":
        return f"https://pancakeswap.finance/liquidity/{token_id}?chain={cfg['slug']}"
    return f"https://app.uniswap.org/positions/v3/{cfg['slug']}/{token_id}"


def pos_link_any(chain_id: int, pid) -> str:
    """Link posisi lintas versi. v2 tidak punya halaman posisi → link pair dexscreener."""
    ver, ref = parse_pid(pid)
    cfg = CHAINS[chain_id]
    if ver == 4:
        return f"https://app.uniswap.org/positions/v4/{cfg['slug']}/{ref}"
    if ver == 2:
        return f"https://dexscreener.com/{cfg['dexscreener']}/{ref}"
    return pos_link(chain_id, ref)


# ---------- Harga USD quote ----------
def _pool_price_t1_per_t0(w3: Web3, pool_addr: str) -> float:
    pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
    sp = pool.functions.slot0().call()[0]
    return (sp / Q96) ** 2


# ---------- Quote di luar daftar tetap (hasil auto-deteksi discovery) ----------
# Banyak pool memecoin tidak ber-quote WBNB/USDT/USDC melainkan token lain (mis.
# RTX/NVDAB). Token lawan seperti itu didaftarkan di sini saat discovery supaya
# seluruh kode lama yang memanggil quote_usd_price(quote_sym) tetap bisa menghargainya.
_EXTRA_QUOTES: dict[int, dict[str, str]] = {}   # chain_id -> {symbol: address}


def register_quote(chain_id: int, sym: str, addr: str) -> str:
    """Daftarkan token lawan sebagai quote runtime. Return simbol final yang dipakai
    di dict pool (bisa didisambiguasi kalau bentrok)."""
    cfg = CHAINS[chain_id]
    addr = Web3.to_checksum_address(addr)
    reg = _EXTRA_QUOTES.setdefault(chain_id, {})
    for s, a in reg.items():
        if a.lower() == addr.lower():
            return s
    sym = (str(sym or "").strip() or "?")[:12]
    # JANGAN pernah menimpa simbol quote resmi: token mana pun bisa mengaku bernama
    # "USDT" dan akan meracuni SELURUH perhitungan USD kalau simbolnya dipakai polos.
    if sym in cfg["quotes"] or sym in cfg["stable_syms"] or sym in reg:
        sym = f"{sym}~{addr[-4:]}"
    reg[sym] = addr
    return sym


def quote_addr_of(chain_id: int, quote_sym: str) -> str | None:
    """Alamat quote dari daftar tetap maupun hasil auto-deteksi."""
    return (CHAINS[chain_id]["quotes"].get(quote_sym)
            or _EXTRA_QUOTES.get(chain_id, {}).get(quote_sym))


def is_extra_quote(chain_id: int, quote_sym: str) -> bool:
    return quote_sym in _EXTRA_QUOTES.get(chain_id, {})


def quote_usd_price(w3: Web3, chain_id: int, quote_sym: str, _cache={}) -> float:
    """Harga USD 1 unit quote. Stable = 1. Wrapped native = dari pool wrapped/stable.
    Quote hasil auto-deteksi = harga tokennya sendiri (token_usd_price)."""
    cfg = CHAINS[chain_id]
    if quote_sym in cfg["stable_syms"]:
        return 1.0
    extra = _EXTRA_QUOTES.get(chain_id, {}).get(quote_sym)
    if extra:
        # token_usd_price hanya melihat quote TETAP, jadi tidak ada rekursi balik ke sini
        return token_usd_price(w3, chain_id, extra)
    key = (chain_id, quote_sym)
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)
    wrapped = Web3.to_checksum_address(cfg["wrapped"])
    for stable_sym in cfg["stable_syms"]:
        stable = Web3.to_checksum_address(cfg["quotes"][stable_sym])
        t0, t1 = sort_tokens(wrapped, stable)
        for fee in fee_tiers(chain_id):
            pool = factory.functions.getPool(t0, t1, fee).call()
            if int(pool, 16) == 0:
                continue
            raw = _pool_price_t1_per_t0(w3, pool)
            dec_w = 18
            dec_s = token_info(w3, stable)["decimals"]
            if t0 == wrapped:
                price = raw * 10 ** (dec_w - dec_s)   # stable per wrapped
            else:
                price = (1 / raw) * 10 ** (dec_w - dec_s)
            if price > 0:
                _cache[key] = (price, time.time())
                return price
    return 0.0


def _dex_pairs(chain_id: int, token_addr: str, _cache={}) -> list[dict]:
    """Daftar pair dexscreener utk token di chain ini (cache 2 menit).
    Data eksternal — SELALU verifikasi on-chain sebelum dipakai."""
    cfg = CHAINS[chain_id]
    key = (chain_id, token_addr.lower())
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < 120:
        return hit[0]
    try:
        r = _cf_get(f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}", timeout=8)
        pairs = [p for p in (r.json().get("pairs") or [])
                 if p.get("chainId") == cfg.get("dexscreener")]
    except Exception:
        return []
    _cache[key] = (pairs, time.time())
    return pairs


def dex_volumes(chain_id: int, token_addr: str) -> dict:
    """Volume 24 jam per pool dari dexscreener: {pool_addr_lower: vol_usd}."""
    return {(p.get("pairAddress") or "").lower(): float((p.get("volume") or {}).get("h24") or 0)
            for p in _dex_pairs(chain_id, token_addr)}


# ---------- Discovery pool via API resmi Uniswap (ListPools) ----------
# Sumber yang sama dengan app.uniswap.org & dengan daftar posisi, jadi konsisten.
# Read-only, tanpa API key. Penting: pool v4 di chain ini banyak yang pakai fee &
# tick spacing NON-STANDAR (mis. 34880/698) — scan RPC yang cuma mencoba tier
# standar (100/500/3000/10000) tidak akan pernah menemukannya.
_UNI_POOLS_API = "https://interface.gateway.uniswap.org/v2/data.v1.DataApiService/ListPools"
_UNI_POOLS_CACHE: dict[tuple, tuple] = {}   # (cid, token) -> (ts, pools mentah)


def uni_pools(cid: int, token: str, ttl: int = 30) -> list | None:
    """Semua pool Uniswap yang memuat `token` di chain `cid` (v3 + v4), langsung
    dari API resmi Uniswap. Cache pendek per-token. Read-only — cuma alamat token
    publik, tak pernah untuk tx. None kalau gagal → caller fallback ke scan RPC.
    None juga di chain yang DEX-nya bukan Uniswap (indexer ini tidak mengenal
    pool PancakeSwap; jawabannya akan kosong atau justru pool DEX yang salah)."""
    if not uni_api_dex(cid):
        return None
    ck = (cid, token.lower())
    hit = _UNI_POOLS_CACHE.get(ck)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    body = {"chainId": cid, "token0": Web3.to_checksum_address(token),
            "protocolVersions": ["PROTOCOL_VERSION_V3", "PROTOCOL_VERSION_V4"],
            "pageSize": 100}
    try:
        r = _cf_post(_UNI_POOLS_API, headers=_UNI_HDR, json=body, timeout=10)
        pools = r.json().get("pools")
        if not isinstance(pools, list):
            return hit[1] if hit else None
        _UNI_POOLS_CACHE[ck] = (time.time(), pools)
        return pools
    except Exception:
        return hit[1] if hit else None


def _uni_v3_pool(cid: int, w3: Web3, ap: dict, tl: str, quotes_lc: dict) -> dict | None:
    """Petakan satu entri pool v3 ListPools → dict pool bot, hanya yang sisi
    lawannya quote dikenal (biar bisa deposit single-side). None kalau bukan."""
    a0 = str(ap.get("token0") or "").lower()
    a1 = str(ap.get("token1") or "").lower()
    if tl not in (a0, a1):
        return None
    if a0 == tl and a1 in quotes_lc:
        qaddr_lc, qsym, q_is_t1 = a1, quotes_lc[a1], True
    elif a1 == tl and a0 in quotes_lc:
        qaddr_lc, qsym, q_is_t1 = a0, quotes_lc[a0], False
    else:
        return None
    fee = int(ap.get("fee"))
    qaddr = Web3.to_checksum_address(qaddr_lc)
    return {
        "ver": 3, "dex": uni_api_dex(cid),
        "pool": Web3.to_checksum_address(str(ap.get("poolId"))), "fee": fee,
        "quote_sym": qsym, "quote_addr": qaddr,
        "quote_decimals": token_info(w3, qaddr)["decimals"],
        "quote_usd": quote_usd_price(w3, cid, qsym), "quote_is_token1": q_is_t1,
        "token0": Web3.to_checksum_address(a0), "token1": Web3.to_checksum_address(a1),
        "tick_spacing": int(ap.get("tickSpacing") or 0) or TICK_SPACING.get(fee),
        "basis": "uniswap",
    }


def _uni_v4_pool(cid: int, w3: Web3, ap: dict, tl: str) -> dict | None:
    """Petakan satu entri pool v4 ListPools → dict pool bot, HANYA yang bisa dipakai
    bot: vanilla (hooks=0), sisi lawan quote dikenal, PoolKey autentik (hash ==
    poolId). Native ETH (currency 0x0) dihitung quote. None kalau bukan / ber-hooks."""
    c0 = str(ap.get("token0") or "")
    c1 = str(ap.get("token1") or "")
    hooks = str((ap.get("hooks") or {}).get("address") or V4_NATIVE)
    if not c0 or not c1 or int(hooks, 16) != 0:
        return None
    if tl not in (c0.lower(), c1.lower()):
        return None
    c0 = Web3.to_checksum_address(c0)
    c1 = Web3.to_checksum_address(c1)
    qsym, q_is_c1 = _v4_quote_side(cid, c0, c1)
    if qsym is None:
        return None
    fee, spacing = int(ap.get("fee")), int(ap.get("tickSpacing"))
    key = (c0, c1, fee, spacing, Web3.to_checksum_address(hooks))
    pid = v4_pool_id(key)
    if "0x" + pid.hex() != str(ap.get("poolId")).lower():   # PoolKey harus menghasilkan poolId ini
        return None
    qaddr = c1 if q_is_c1 else c0
    return {
        "ver": 4, "dex": v4_dex(cid), "pool": "0x" + pid.hex(), "pool_id": pid, "key": key,
        "fee": fee, "tick_spacing": spacing, "quote_sym": qsym, "quote_addr": qaddr,
        "quote_decimals": _v4_currency_info(w3, cid, qaddr)["decimals"],
        "quote_usd": quote_usd_price(w3, cid, qsym), "quote_is_token1": q_is_c1,
        "token0": c0, "token1": c1, "basis": "uniswap",
    }


def uni_discover(cid: int, token: str) -> dict | None:
    """Pool discovery cepat via API Uniswap (ListPools): v3 + v4 vanilla yang salah
    satu sisinya quote dikenal bot. Bentuk balikan sama dengan discover_pools.
    None kalau API mati / token tak ada pool cocok → caller fallback ke scan RPC.

    Dict pool tetap diverifikasi on-chain di mint builder (assert_pool_orientation)
    sebelum dana bergerak — API cuma untuk kecepatan tampilan, bukan sumber
    tepercaya untuk transaksi."""
    pools = uni_pools(cid, token)
    if not pools:
        return None
    cfg = CHAINS[cid]
    w3 = get_w3(cid)
    tl = token.lower()
    quotes_lc = {a.lower(): s for s, a in cfg["quotes"].items()}
    # ListPools TIDAK mengirim volume (cuma TVL + apr) → vol 24 jam dari dexscreener,
    # key-nya pairAddress = alamat pool (v3) / poolId (v4), sama dengan p["pool"].
    try:
        vols = dex_volumes(cid, token)
    except Exception:
        vols = {}
    out = []
    for ap in pools:
        try:
            proto = str(ap.get("protocolVersion"))
            if proto == "PROTOCOL_VERSION_V3":
                p = _uni_v3_pool(cid, w3, ap, tl, quotes_lc)
            elif proto == "PROTOCOL_VERSION_V4":
                p = _uni_v4_pool(cid, w3, ap, tl)
            else:
                continue
            if not p:
                continue
            tvl = float(ap.get("totalLiquidityUsd") or 0)
            vol = vols.get(str(p["pool"]).lower())
            # Pool kecil tetap ditampilkan (ditandai "thin"). Yang dibuang hanya pool
            # yang benar-benar mati: tanpa TVL DAN tanpa volume — ListPools memuat
            # ratusan pool semacam itu dan cuma jadi sampah di daftar.
            if tvl <= 0 and not vol:
                continue
            p["tvl_usd"] = tvl
            p["vol24_usd"] = vol
            apr = ap.get("apr")
            if apr is None:
                apr = ap.get("totalApr")
            if apr is not None:
                p["apr_pct"] = float(apr)
            else:
                # tak dikirim API → estimasi sendiri: fee 24 jam × 365 ÷ TVL
                v = p["vol24_usd"]
                p["apr_pct"] = (v * p["fee"] / 1e6 / tvl * 365 * 100) if (v and tvl) else None
            out.append(p)
        except Exception:
            continue
    if not out:
        return None
    out.sort(key=lambda p: p["tvl_usd"], reverse=True)
    try:
        tinfo = token_info(w3, Web3.to_checksum_address(token))
    except Exception:
        tinfo = {"symbol": "?", "decimals": 18, "name": ""}
    return {"token": tinfo, "pools": out}


# Selisih harga maksimum sebuah pool terhadap pool TERDALAM token yang sama.
# Pool yang lewat batas ini dibuang dari daftar: bukan peluang, tapi jebakan —
# harganya menyimpang justru KARENA tak ada yang mengarbitrase (untungnya lebih
# kecil dari gas). Kalau di-LP, arbitraser-lah yang akhirnya menyeret harga pool
# itu ke harga pasar memakai modal kita. Kasus nyata: House/USDT v3 berisi ~$148
# harganya 100,3% di atas House/WBNB v2 yang bervolume $2,5jt.
PRICE_DEVIATION_MAX = 0.25


def _pool_price_usd(p: dict, token_dec: int, token_addr: str) -> float | None:
    """Harga USD 1 unit token yang DICARI menurut pool ini. None kalau tak bisa
    dihitung (data tak lengkap, atau token yang dicari justru jadi sisi quote di
    pool ini — harganya jadi harga token lain, tidak sebanding antar pool)."""
    qdec, qusd = p.get("quote_decimals"), p.get("quote_usd") or 0
    if qdec is None or qusd <= 0:
        return None
    t = str(token_addr).lower()
    meme = str(p.get("token0") if p.get("quote_is_token1") else p.get("token1") or "").lower()
    if meme != t:
        return None
    if p.get("ver") == 2:
        rq, rm = p.get("reserve_quote") or 0, p.get("reserve_meme") or 0
        if rq <= 0 or rm <= 0:
            return None
        return (rq / 10 ** qdec) / (rm / 10 ** token_dec) * qusd
    sq = p.get("sqrtp") or 0
    if sq <= 0:
        return None
    raw = (sq / Q96) ** 2                       # token1 per token0, satuan wei
    price_q = raw if p.get("quote_is_token1") else (1 / raw if raw else 0)
    return price_q * 10 ** (token_dec - qdec) * qusd


def _fill_missing_sqrtp(chain_id: int, pools: list, limit: int = 12) -> None:
    """Isi sqrtPrice pool yang belum punya (jalur indexer Uniswap tidak mengirimnya).
    Dibatasi ke pool ber-TVL teratas: itu yang ditampilkan UI dan yang mungkin
    dipilih user, sementara membaca slot0 untuk ratusan pool jelas terlalu mahal."""
    need = [p for p in sorted(pools, key=lambda p: p.get("tvl_usd") or 0, reverse=True)
            if not p.get("sqrtp") and p.get("ver", 3) in (3, 4)][:limit]
    if not need:
        return
    w3 = get_w3(chain_id)

    def fetch(p):
        try:
            if p.get("ver") == 4:
                pid = p.get("pool_id") or bytes.fromhex(str(p["pool"]).removeprefix("0x"))
                p["sqrtp"] = v4_slot0(w3, chain_id, pid)[0]
            else:
                pool = w3.eth.contract(address=Web3.to_checksum_address(p["pool"]), abi=POOL_ABI)
                p["sqrtp"] = pool.functions.slot0().call()[0]
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(fetch, need))


# Endpoint yang dipakai web defi.krystal.app sendiri (ditemukan dari bundel JS-nya).
# CATATAN: yang v1 cuma melayani Solana — untuk EVM harus v2, itu sebabnya
# /all/v1/lp_explorer/top_pools menjawab "chain id 56 not supported".
# Dipakai HANYA untuk angka tampilan (TVL/volume/APR). Tidak pernah jadi dasar
# membangun transaksi: alamat pool tetap diverifikasi ke factory on-chain.
_KRYSTAL_POOLS = "https://api.krystal.app/all/v2/lp_explorer/top_pools"


_KRYSTAL_PROTO = {          # protocol Krystal → (DEX kita, versi pool)
    "pancakev2": ("PancakeSwap", 2), "pancakev3": ("PancakeSwap", 3),
    "uniswapv2": ("Uniswap", 2), "uniswapv3": ("Uniswap", 3), "uniswapv4": ("Uniswap", 4),
}


# Header yang dikirim web Krystal sendiri. python-requests default (User-Agent
# "python-requests/2.x", tanpa origin/referer) gampang dijegal Cloudflare dari IP
# datacenter — gejalanya request "sukses" tapi hasilnya kosong, dan bot diam-diam
# jatuh ke scan RPC penuh. `skipCheckAutomation=true` mematikan pengecekan dukungan
# automation di sisi server (ikon robot di UI mereka); kita tidak memakainya, dan
# tanpa itu request dingin terukur 4 detik, dengan itu 0,4 detik.
_KRYSTAL_HDR = {
    "accept": "application/json",
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "origin": "https://defi.krystal.app",
    "referer": "https://defi.krystal.app/",
}
_KRYSTAL_LAST_ERR = ""      # sebab kegagalan terakhir, ditampilkan UI saat fallback

# Cloudflare di depan api.krystal.app menyaring lewat TLS fingerprint (JA3), BUKAN
# cuma User-Agent: dari IP datacenter, python-requests kena 403 walau header-nya
# sudah meniru browser persis (terbukti di VPS — 403 tanpa header, dan dengan header
# jawabannya tetap bukan JSON). curl_cffi meniru handshake TLS Chrome sungguhan dan
# tembus. Opsional: kalau paketnya tidak ada, jalur requests biasa tetap dipakai —
# yang hilang cuma kecepatan discovery, bukan fungsinya.
try:
    from curl_cffi import requests as _cffi_requests
except Exception:               # pragma: no cover
    _cffi_requests = None


def krystal_last_error() -> str:
    return _KRYSTAL_LAST_ERR


# Beberapa sumber data di file ini duduk di belakang Cloudflare yang sama: API
# Krystal, indexer Uniswap (ListPools/ListPositions), dan dexscreener. Dari IP
# datacenter ketiganya menolak python-requests — dan penolakannya BUKAN exception,
# melainkan halaman HTML, sehingga pemanggilnya cuma melihat "hasil kosong" lalu
# diam-diam jatuh ke jalur lambat. Semua request ke sana harus lewat helper ini.
def _proxy_list(_cache=[]) -> list[str]:
    """Proxy dari env `PROXY_LIST` — `ip:port:user:pass` atau URL penuh, dipisah
    koma/baris/spasi. Kredensialnya tinggal di `.env`, JANGAN pernah masuk repo.

    Dipakai HANYA untuk API data pasar (Krystal / indexer Uniswap / dexscreener /
    GeckoTerminal), TIDAK untuk RPC. Angka dari API itu memang sudah diperlakukan
    sebagai tampilan belaka dan tiap pool tetap diverifikasi on-chain, jadi operator
    proxy tidak bisa mengarahkan transaksi. Menyalurkan RPC lewat pihak ketiga akan
    membuang jaminan itu."""
    if _cache:
        return _cache[0]
    out = []
    for tok in re.split(r"[,\s]+", os.environ.get("PROXY_LIST", "").strip()):
        if not tok:
            continue
        if "://" in tok:
            out.append(tok)
            continue
        parts = tok.split(":")
        if len(parts) == 4:
            ip, port, user, pw = parts
            out.append(f"http://{quote(user, safe='')}:{quote(pw, safe='')}@{ip}:{port}")
        elif len(parts) == 2:
            out.append(f"http://{tok}")
    _cache.append(out)
    return out


_PROXY_GOOD = [0]      # indeks proxy yang terakhir berhasil — dicoba duluan


def _cf_request(method: str, url: str, **kw):
    """Request ke sumber data luar, tahan Cloudflare DAN blokir IP.

    Urutannya: langsung dulu (host sehat tidak membayar apa pun), lalu proxy satu
    per satu kalau jawabannya 4xx/5xx atau error. Cloudflare menolak dengan 403 +
    halaman HTML, bukan exception, jadi status code ikut diperiksa."""
    def _try(proxies):
        if _cffi_requests is not None:
            try:
                fn = getattr(_cffi_requests, method)
                return fn(url, impersonate="chrome", proxies=proxies, **kw)
            except Exception:
                pass
        return getattr(requests, method)(url, proxies=proxies, **kw)

    last = None
    try:
        last = _try(None)
        if last.status_code < 400:
            return last
    except Exception:
        pass
    proxies = _proxy_list()
    for i in range(len(proxies)):
        p = proxies[(_PROXY_GOOD[0] + i) % len(proxies)]
        try:
            r = _try({"http": p, "https": p})
            if r.status_code < 400:
                _PROXY_GOOD[0] = (_PROXY_GOOD[0] + i) % len(proxies)
                return r
            last = r
        except Exception:
            continue
    if last is None:
        raise RuntimeError("semua jalur (langsung + proxy) gagal")
    return last


def _cf_get(url: str, **kw):
    return _cf_request("get", url, **kw)


def _cf_post(url: str, **kw):
    return _cf_request("post", url, **kw)


def _krystal_get(params: dict, timeout: int = 15):
    return _cf_get(_KRYSTAL_POOLS, params=params, headers=_KRYSTAL_HDR, timeout=timeout)


def krystal_raw(chain_id: int, token: str, _cache={}, ttl: int = 120) -> list:
    """Entri mentah dari API Krystal. List kosong kalau gagal / chain tak dilayani —
    pemanggil WAJIB tetap jalan tanpa ini."""
    key = (chain_id, str(token).lower())
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    out = []
    global _KRYSTAL_LAST_ERR
    for attempt in range(2):    # sekali ulang: hiccup 1 request tidak boleh
        try:                    # mematikan seluruh jalur Krystal
            r = _krystal_get({"chainId": chain_id, "tokenAddress": str(token).lower(),
                              "skipCheckAutomation": "true"})
            got = (r.json() or {}).get("result") or []
            if isinstance(got, list) and got:
                out = got
                _KRYSTAL_LAST_ERR = ""
                break
            _KRYSTAL_LAST_ERR = f"HTTP {r.status_code}, hasil kosong"
        except Exception as e:
            _KRYSTAL_LAST_ERR = f"{type(e).__name__}: {str(e)[:80]}"
        if attempt == 0:
            time.sleep(0.6)
    if not out:
        # JANGAN cache hasil kosong selama ttl penuh. Krystal bisa menjawab HTTP 200
        # dengan payload error (result hilang) — dulu itu ikut di-cache 120 detik,
        # sehingga SETIAP discovery dalam 2 menit berikutnya jatuh ke scan RPC penuh
        # dengan seluruh saringannya. Gejalanya: token yang di web Krystal punya 20
        # pool cuma muncul 4 di bot, plus "78 pool disembunyikan".
        # Hasil lama (kalau ada) tetap dipakai; kalau tidak, kosong tanpa di-cache.
        return hit[0] if hit else []
    _cache[key] = (out, time.time())
    return out


def krystal_pools(chain_id: int, token: str) -> dict:
    """{pool_address_lower: {tvl_usd, vol24_usd, apr_pct}} — angka tampilan saja."""
    out = {}
    for p in krystal_raw(chain_id, token):
        s = p.get("stat24h") or {}
        addr = str(p.get("poolAddress") or "").lower()
        if not addr:
            continue
        out[addr] = {
            "tvl_usd": float(p.get("tvlUsd") or 0),
            "vol24_usd": float(s.get("volumeUsd") or 0) or None,
            "apr_pct": float(s.get("apr")) if s.get("apr") is not None else None,
        }
    return out


# Pool v4 fee bebas memakai spacing bebas, tapi polanya konsisten: spacing ≈ fee/50
# (fee 40000→800, 12500→250, 18888→378). Tier klasik tetap dicoba juga.
_V4_SPACING_FIXED = (1, 10, 50, 60, 200)
# Krystal menandai ETH native dengan sentinel 0xEeee…, Uniswap v4 memakai address(0).
# Tanpa dinormalkan, PoolKey yang disusun tidak akan pernah menghasilkan poolId yang
# sama dan SEMUA pool ber-quote ETH native ikut terbuang (terjadi di FRONG: pool
# $618k hilang dari daftar).
_NATIVE_SENTINELS = {"0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", V4_NATIVE}


def _norm_currency(a: str) -> str:
    return V4_NATIVE if str(a).lower() in _NATIVE_SENTINELS else Web3.to_checksum_address(a)


def _spacing_candidates(fee: int) -> list[int]:
    base = fee / 50
    cands = {int(round(base)), int(base), int(base) + 1, *_V4_SPACING_FIXED,
             *(s for f, s in V4_FEE_SPACINGS if f == fee)}
    return [s for s in sorted(cands) if 1 <= s <= 32767]


def _v4_key_from_indexer(chain_id: int, token: str, pool_id_hex: str) -> tuple | None:
    """PoolKey dari indexer Uniswap: fee DAN tickSpacing dikirim apa adanya, jadi
    tidak perlu menebak. Tetap dibuktikan lewat hash sebelum dipakai."""
    want = str(pool_id_hex).lower()
    try:
        for ap in (uni_pools(chain_id, token) or []):
            if str(ap.get("poolId", "")).lower() != want:
                continue
            hooks = _norm_currency((ap.get("hooks") or {}).get("address") or V4_NATIVE)
            if int(hooks, 16) != 0:
                return None
            key = (*sort_tokens(_norm_currency(ap["token0"]), _norm_currency(ap["token1"])),
                   int(ap["fee"]), int(ap["tickSpacing"]), Web3.to_checksum_address(V4_NATIVE))
            if "0x" + v4_pool_id(key).hex().removeprefix("0x") == want:
                return key
    except Exception:
        return None
    return None


def _v4_key_from_krystal(entry: dict, pool_id_hex: str) -> tuple | None:
    """Susun PoolKey v4 dari data Krystal, dibuktikan lewat hash.

    Krystal tidak mengirim tickSpacing, jadi nilainya dicoba satu per satu dan
    diterima HANYA kalau v4_pool_id(key) == poolId yang mereka sebut. Hash cocok =
    kunci autentik (mustahil dipalsukan), jadi ini aman dipakai membangun transaksi
    — dan tidak butuh getLogs yang sering ditolak RPC publik."""
    try:
        hooks = Web3.to_checksum_address(entry.get("hooks") or V4_NATIVE)
        if int(hooks, 16) != 0:
            return None                     # pool ber-hooks tidak didukung
        c0, c1 = sort_tokens(_norm_currency(entry["token0"]["address"]),
                             _norm_currency(entry["token1"]["address"]))
        want = str(pool_id_hex).lower()
        fees = []
        if entry.get("dynamicFee"):
            fees.append(0x800000)           # penanda fee dinamis di PoolKey
        for f in (entry.get("lpFee"), entry.get("feeTier")):
            if f is not None:
                fees.append(int(round(float(f) * 10000)))
        for fee in dict.fromkeys(fees):
            for sp in _spacing_candidates(fee):
                key = (c0, c1, fee, sp, hooks)
                if "0x" + v4_pool_id(key).hex().removeprefix("0x") == want:
                    return key
    except Exception:
        return None
    return None


def token_chains(token: str, _cache={}, ttl: int = 180) -> list[tuple[int, float]]:
    """Chain mana saja yang punya pool untuk token ini — [(chain_id, tvl_total)] urut TVL.

    Endpoint `top_pools` Krystal jalan TANPA `chainId` dan tiap entri membawa
    `chainId` sendiri, jadi satu request sudah cukup memetakan token ke chain.
    Itulah cara defi.krystal.app/pools bekerja: satu daftar lintas chain, filter
    chain cuma dipakai untuk menyempitkan.

    Hanya chain yang ADA di CHAINS yang dikembalikan — token di chain yang tidak
    didukung bot tidak ada gunanya ditawarkan. Kosong = Krystal tidak tahu token
    ini (bukan berarti token tidak ada; caller harus tetap jalan tanpa hasil)."""
    key = str(token).lower()
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    tot: dict[int, float] = {}
    try:
        r = _krystal_get({"tokenAddress": key, "skipCheckAutomation": "true"})
        for p in (r.json().get("result") or []):
            try:
                cid = int(p.get("chainId") or 0)
            except (TypeError, ValueError):
                continue
            if cid in CHAINS:
                tot[cid] = tot.get(cid, 0.0) + float(p.get("tvlUsd") or 0)
    except Exception:
        return hit[0] if hit else []
    out = sorted(tot.items(), key=lambda kv: -kv[1])
    if out:
        _cache[key] = (out, time.time())
    return out


def token_chains_onchain(token: str) -> list[int]:
    """Cadangan kalau Krystal tidak tahu token itu: cek kontraknya BENAR-BENAR ada
    (punya bytecode) di tiap chain. Jauh lebih lambat daripada token_chains() karena
    satu eth_getCode per chain, jadi dipakai hanya saat Krystal nihil."""
    out = []
    for cid in CHAINS:
        try:
            w3 = get_w3(cid)
            if len(w3.eth.get_code(Web3.to_checksum_address(token))) > 2:
                out.append(cid)
        except Exception:
            continue
    return out


_GECKO_POOLS = "https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{token}/pools"
# dexId GeckoTerminal → (nama DEX di CHAINS, versi). Slug mereka tidak seragam
# ("pancakeswap_v2" pakai garis bawah, "pancakeswap-v3-bsc" pakai strip + sufiks
# chain), jadi pencocokannya lewat potongan kata, bukan tabel kaku.
def _gecko_proto(chain_id: int, dex_id: str) -> tuple[str | None, int | None]:
    d = str(dex_id or "").lower().replace("_", "-")
    ver = 4 if "-v4" in d else 3 if "-v3" in d else 2 if "-v2" in d else None
    if ver is None:
        return None, None
    for name in dex_names(chain_id):
        if name.lower().replace(" ", "") in d.replace("-", ""):
            return name, ver
    return None, None


def _v4_key_search(c0: str, c1: str, pool_id_hex: str, fee_hint: int, span: int = 400) -> tuple | None:
    """Cari PoolKey v4 dengan menebak fee di sekitar `fee_hint`, dibuktikan hash.

    Perlu karena GeckoTerminal cuma menyebut fee yang SUDAH DIBULATKAN di nama pool
    ("BNBCAT / USDT 4.202%" untuk fee asli 42122). v4_pool_id() itu keccak lokal —
    tanpa RPC — jadi mencoba ribuan kombinasi praktis gratis (terukur 16 pool < 1
    detik). Hash cocok = kunci autentik, aman dipakai membangun transaksi.

    Pool ber-hooks tidak akan pernah cocok (hooks bukan alamat nol) — itu memang
    yang diinginkan: bot tidak mendukungnya."""
    want = str(pool_id_hex).lower()
    zero = "0x" + "00" * 20
    for d in range(span):
        for fee in ({fee_hint + d, fee_hint - d} if d else {fee_hint}):
            if fee <= 0 or fee >= 0x800000:
                continue
            for sp in _spacing_candidates(fee):
                key = (c0, c1, fee, sp, zero)
                if "0x" + v4_pool_id(key).hex().removeprefix("0x") == want:
                    return key
    return None


def discover_gecko(chain_id: int, token: str) -> list[dict]:
    """Daftar pool dari GeckoTerminal. Pengganti indexer Uniswap + Krystal di host
    yang diblokir Cloudflare — endpoint ini TIDAK di belakang Cloudflare (terbukti
    200 dari VPS yang ditolak dua sumber lain).

    Sama seperti sumber luar lain, angkanya cuma untuk tampilan/pengurutan; tiap
    pool tetap diverifikasi on-chain sebelum bisa dipakai: v2/v3 lewat `token0()/
    token1()` + kepemilikan factory, v4 lewat hash PoolKey."""
    cfg = CHAINS[chain_id]
    net = cfg.get("gecko")
    if not net:
        return []
    try:
        r = _cf_get(_GECKO_POOLS.format(net=net, token=str(token).lower()),
                    timeout=15, headers={"accept": "application/json"})
        rows = (r.json() or {}).get("data") or []
    except Exception:
        return []
    w3 = get_w3(chain_id)
    tl = str(token).lower()
    quotes_lc = {a.lower(): s for s, a in cfg["quotes"].items()}

    def build(p):
        try:
            a = p.get("attributes") or {}
            rel = p.get("relationships") or {}
            dname, ver = _gecko_proto(chain_id, (rel.get("dex") or {}).get("data", {}).get("id"))
            if not dname or (ver == 4 and not has_v4(chain_id, dname)):
                return None
            addr = str(a.get("address") or "")
            stats = {"tvl_usd": float(a.get("reserve_in_usd") or 0),
                     "vol24_usd": float((a.get("volume_usd") or {}).get("h24") or 0) or None,
                     "apr_pct": None, "tvl_src": "gecko", "basis": "gecko"}
            if ver == 4:
                # fee dari nama pool ("… / USDT 4.202%") — sudah dibulatkan, jadi
                # dipakai sebagai TEBAKAN AWAL lalu dicari yang hash-nya cocok
                try:
                    pct = float(str(a.get("name") or "").rsplit(" ", 1)[1].rstrip("%"))
                except (IndexError, ValueError):
                    return None
                b = _norm_currency(str((rel.get("base_token") or {}).get("data", {}).get("id", "")).split("_")[-1])
                q_ = _norm_currency(str((rel.get("quote_token") or {}).get("data", {}).get("id", "")).split("_")[-1])
                c0, c1 = sort_tokens(b, q_)
                key = _v4_key_search(c0, c1, addr, int(round(pct * 10000)))
                if not key:
                    return None                  # ber-hooks atau fee di luar jangkauan
                if tl not in (key[0].lower(), key[1].lower()):
                    return None
                qa = key[1] if key[0].lower() == tl else key[0]
                qsym, qusd, qdec = _quote_meta(w3, chain_id, qa, quotes_lc)
                if qusd <= 0:
                    return None
                pid = v4_pool_id(key)
                sqrtp, tick = v4_slot0(w3, chain_id, pid)
                return {"ver": 4, "dex": dname, "pool": "0x" + pid.hex().removeprefix("0x"),
                        "pool_id": pid, "key": key, "fee": key[2], "tick_spacing": key[3],
                        "quote_sym": qsym, "quote_addr": qa, "quote_decimals": qdec,
                        "quote_usd": qusd, "sqrtp": sqrtp, "tick": tick,
                        "token0": key[0], "token1": key[1],
                        "quote_is_token1": qa.lower() == key[1].lower(),
                        "foreign_quote": qa.lower() not in quotes_lc, **stats}
            pc = w3.eth.contract(address=Web3.to_checksum_address(addr),
                                 abi=POOL_ABI if ver == 3 else V2_PAIR_ABI)
            t0, t1 = pc.functions.token0().call(), pc.functions.token1().call()
            if tl not in (t0.lower(), t1.lower()):
                return None
            fee = pc.functions.fee().call() if ver == 3 else cfg.get("v2_fee", 3000)
            # kepemilikan factory diverifikasi on-chain — daftar GeckoTerminal memuat
            # SEMUA dex di chain itu, termasuk yang tidak kita dukung
            owner = (which_dex_v3(w3, chain_id, addr, *sort_tokens(t0, t1), fee) if ver == 3
                     else which_dex_v2(w3, chain_id, addr, t0, t1))
            if not owner:
                return None
            q = t1 if t0.lower() == tl else t0
            qsym, qusd, qdec = _quote_meta(w3, chain_id, q, quotes_lc)
            if qusd <= 0:
                return None
            out = {"ver": ver, "dex": owner, "pool": Web3.to_checksum_address(addr), "fee": fee,
                   "quote_sym": qsym, "quote_addr": Web3.to_checksum_address(q),
                   "quote_decimals": qdec, "quote_usd": qusd,
                   "token0": Web3.to_checksum_address(t0), "token1": Web3.to_checksum_address(t1),
                   "quote_is_token1": q.lower() == t1.lower(),
                   "foreign_quote": q.lower() not in quotes_lc, **stats}
            if ver == 3:
                s0 = pc.functions.slot0().call()
                out.update({"sqrtp": s0[0], "tick": s0[1],
                            "tick_spacing": pc.functions.tickSpacing().call(),
                            "liquidity": pc.functions.liquidity().call()})
            else:
                rq, rm = _v2_pair_reserves(w3, addr, q)
                if rq <= 0 or rm <= 0:
                    return None
                out.update({"reserve_quote": rq, "reserve_meme": rm,
                            "sqrtp": None, "tick": None, "liquidity": None})
            return out
        except Exception:
            return None

    res = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        for x in ex.map(build, rows[:25]):
            if x:
                res.append(x)
    return res


def discover_krystal(chain_id: int, token: str) -> list[dict]:
    """Bangun daftar pool dari Krystal, TIAP POOL DIVERIFIKASI ON-CHAIN.

    Krystal cepat (<1 detik) dan angkanya sama dengan yang dilihat user di web
    mereka, tapi tetap data luar: alamat pool wajib cocok dengan factory DEX
    on-chain (v2/v3) atau PoolKey wajib menghasilkan poolId yang sama (v4)
    sebelum boleh dipakai membangun transaksi.

    List kosong = token tidak ada di Krystal → caller pakai discovery sendiri."""
    raw = krystal_raw(chain_id, token)
    if not raw:
        return []
    w3 = get_w3(chain_id)
    token = Web3.to_checksum_address(token)
    tl = token.lower()
    quotes_lc = {a.lower(): s for s, a in CHAINS[chain_id]["quotes"].items()}

    def build(entry):
        try:
            dname, ver = _KRYSTAL_PROTO.get(str(entry.get("protocol") or "").lower(), (None, None))
            if not dname or dname not in dex_names(chain_id):
                return None                      # DEX yang tidak kita dukung
            if ver == 4 and not has_v4(chain_id, dname):
                return None
            addr = str(entry.get("poolAddress") or "")
            s = entry.get("stat24h") or {}
            stats = {"tvl_usd": float(entry.get("tvlUsd") or 0),
                     "vol24_usd": float(s.get("volumeUsd") or 0) or None,
                     "apr_pct": float(s.get("apr")) if s.get("apr") is not None else None,
                     "tvl_src": "krystal", "basis": "krystal"}
            if ver == 4:
                # dari data Krystal dulu (tanpa RPC, dibuktikan lewat hash); log
                # Initialize jadi cadangan karena getLogs rentang lebar sering ditolak
                # indexer (fee+spacing eksak) → data Krystal (tebak spacing) → log
                key = (_v4_key_from_indexer(chain_id, token, addr)
                       or _v4_key_from_krystal(entry, addr)
                       or _v4_key_from_init(w3, chain_id, addr))
                if not key:
                    return None
                c0, c1 = Web3.to_checksum_address(key[0]), Web3.to_checksum_address(key[1])
                if tl not in (c0.lower(), c1.lower()):
                    return None
                q = c1 if c0.lower() == tl else c0
                qsym, qusd, qdec = _quote_meta(w3, chain_id, q, quotes_lc)
                if qusd <= 0:
                    return None
                pid = v4_pool_id(key)
                sqrtp = v4_slot0(w3, chain_id, pid)[0]
                return {"ver": 4, "dex": dname, "pool": "0x" + pid.hex().removeprefix("0x"),
                        "pool_id": pid, "key": key, "fee": key[2], "tick_spacing": key[3],
                        "quote_sym": qsym, "quote_addr": q, "quote_decimals": qdec,
                        "quote_usd": qusd, "quote_is_token1": q.lower() == c1.lower(),
                        "token0": c0, "token1": c1, "sqrtp": sqrtp, "tick": None,
                        "foreign_quote": q.lower() not in quotes_lc and q.lower() != V4_NATIVE,
                        **stats}
            pc = w3.eth.contract(address=Web3.to_checksum_address(addr),
                                 abi=POOL_ABI if ver == 3 else V2_PAIR_ABI)
            t0, t1 = pc.functions.token0().call(), pc.functions.token1().call()
            if tl not in (t0.lower(), t1.lower()):
                return None
            q = t1 if t0.lower() == tl else t0
            # Krystal sudah menyebut protokolnya, jadi cukup verifikasi ke SATU
            # factory (bukan menyisir semua DEX) — separuh panggilan RPC hemat.
            dc = dex_cfg(chain_id, dname)
            if ver == 3:
                fee = pc.functions.fee().call()
                f3 = w3.eth.contract(address=Web3.to_checksum_address(dc["factory"]),
                                     abi=FACTORY_ABI)
                if f3.functions.getPool(t0, t1, fee).call().lower() != addr.lower():
                    return None                  # bukan pool factory DEX itu
            else:
                v2f = w3.eth.contract(address=Web3.to_checksum_address(dc["v2_factory"]),
                                      abi=V2_FACTORY_ABI)
                if v2f.functions.getPair(t0, t1).call().lower() != addr.lower():
                    return None
                fee = dc.get("v2_fee", 3000)
            qsym, qusd, qdec = _quote_meta(w3, chain_id, q, quotes_lc)
            if qusd <= 0:
                return None
            p = {"ver": ver, "dex": dname, "pool": Web3.to_checksum_address(addr), "fee": fee,
                 "quote_sym": qsym, "quote_addr": Web3.to_checksum_address(q),
                 "quote_decimals": qdec, "quote_usd": qusd,
                 "token0": Web3.to_checksum_address(t0), "token1": Web3.to_checksum_address(t1),
                 "quote_is_token1": q.lower() == t1.lower(),
                 "foreign_quote": q.lower() not in quotes_lc, **stats}
            if ver == 3:
                slot0 = pc.functions.slot0().call()
                p.update({"sqrtp": slot0[0], "tick": slot0[1],
                          "tick_spacing": pc.functions.tickSpacing().call(),
                          "liquidity": pc.functions.liquidity().call()})
            else:
                rq, rm = _v2_pair_reserves(w3, addr, q)
                if rq <= 0 or rm <= 0:
                    return None
                p.update({"reserve_quote": rq, "reserve_meme": rm,
                          "sqrtp": None, "tick": None, "liquidity": None})
            return p
        except Exception:
            return None

    out = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        for r in ex.map(build, raw[:20]):
            if r:
                out.append(r)
    return out


def _quote_meta(w3: Web3, chain_id: int, q: str, quotes_lc: dict) -> tuple[str, float, int]:
    """(simbol, harga USD, desimal) sisi quote — quote tetap, native, atau auto-deteksi."""
    ql = str(q).lower()
    if ql == V4_NATIVE:      # ETH native di v4: harganya = harga wrapped
        cfg = CHAINS[chain_id]
        return cfg["native_symbol"], quote_usd_price(w3, chain_id, cfg["wrapped_symbol"]), 18
    qdec = token_info(w3, Web3.to_checksum_address(q))["decimals"]
    if ql in quotes_lc:
        sym = quotes_lc[ql]
        return sym, quote_usd_price(w3, chain_id, sym), qdec
    if quote_backing_usd(w3, chain_id, q) <= 0:
        return "?", 0.0, qdec
    sym = register_quote(chain_id, token_info(w3, Web3.to_checksum_address(q))["symbol"], q)
    return sym, quote_usd_price(w3, chain_id, sym), qdec


def _fill_onchain_tvl(chain_id: int, pools: list, token: str, token_dec: int,
                      limit: int = 12) -> None:
    """Hitung ulang TVL pool v3 dari SALDO NYATA di kontrak pool.

    Angka TVL dari indexer Uniswap (`totalLiquidityUsd`) sering meleset jauh —
    terukur $24,4k untuk pool yang saldonya benar-benar $40,7k. Karena TVL yang
    menentukan urutan daftar DAN jadi patokan filter harga, angkanya harus dari
    chain. Dibatasi pool teratas: dua balanceOf per pool terlalu mahal untuk ratusan."""
    token = Web3.to_checksum_address(token)
    # Sisi meme harus benar-benar token yang dicari; kalau token itu justru jadi sisi
    # quote (mis. mencari USDG), rumus di bawah menghitung saldo token lain dan TVL-nya
    # ngawur — sama seperti jebakan di _pool_price_usd.
    tl = token.lower()
    cand = [p for p in sorted(pools, key=lambda p: p.get("tvl_usd") or 0, reverse=True)
            if p.get("ver", 3) == 3 and p.get("basis") == "uniswap" and p.get("sqrtp")
            and str(p.get("token0") if p.get("quote_is_token1") else p.get("token1") or "").lower() == tl
            ][:limit]
    if not cand:
        return
    w3 = get_w3(chain_id)

    def fix(p):
        try:
            qdec, qusd = p["quote_decimals"], p.get("quote_usd") or 0
            if qusd <= 0:
                return
            q_bal = erc20(w3, p["quote_addr"]).functions.balanceOf(
                Web3.to_checksum_address(p["pool"])).call() / 10 ** qdec
            m_bal = erc20(w3, token).functions.balanceOf(
                Web3.to_checksum_address(p["pool"])).call() / 10 ** token_dec
            raw = (p["sqrtp"] / Q96) ** 2
            meme_in_q = (raw if p.get("quote_is_token1") else (1 / raw if raw else 0)) \
                * 10 ** (token_dec - qdec)
            p["tvl_usd"] = q_bal * qusd + m_bal * meme_in_q * qusd
            p["tvl_src"] = "chain"
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(fix, cand))


def _fill_v4_tvl(chain_id: int, pools: list, limit: int = 8) -> None:
    """TVL pool v4 dari reserve VIRTUAL sisi quote (liquidity × harga), dibaca lewat
    StateView. Dipakai hanya kalau tidak ada sumber luar (Krystal/dexscreener):
    saldo per-pool v4 tak bisa dibaca karena semua currency ditahan satu PoolManager.
    Angkanya menghitung likuiditas aktif di tick sekarang saja, jadi bisa lebih kecil
    dari TVL sebenarnya — tapi jauh lebih dekat daripada angka indexer."""
    cand = [p for p in sorted(pools, key=lambda p: p.get("tvl_usd") or 0, reverse=True)
            if p.get("ver") == 4 and p.get("tvl_src") not in ("krystal", "dexscreener")][:limit]
    if not cand or not verify_v4(get_w3(chain_id), chain_id):
        return
    w3 = get_w3(chain_id)
    sv = _v4c(w3, chain_id, "v4_stateview", V4_STATEVIEW_ABI)

    def fix(p):
        try:
            pid = p.get("pool_id") or bytes.fromhex(str(p["pool"]).removeprefix("0x"))
            sqrtp, _tick, _, _ = sv.functions.getSlot0(pid).call()
            liq = sv.functions.getLiquidity(pid).call()
            if not sqrtp or not liq:
                p["tvl_usd"] = 0.0
                return
            q_virt = (liq * sqrtp // Q96) if p.get("quote_is_token1") else (liq * Q96 // sqrtp)
            p["tvl_usd"] = q_virt / 10 ** p["quote_decimals"] * (p.get("quote_usd") or 0) * 2
            p["tvl_src"] = "chain-virtual"
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(fix, cand))


def _drop_dead_pools(pools: list, keep_ratio: float = 0.05) -> tuple[list, list]:
    """Buang pool yang tidak benar-benar hidup: tanpa TVL, atau tanpa volume 24 jam.

    Pool ber-quote aneh TIDAK dibuang selama ada TVL+volume — justru itu yang dicari.
    Yang dibuang adalah ekor mati: pool v4 hasil indexer yang tak pernah
    diperdagangkan (78 pool token memes menyusut jadi 16).

    Katup pengaman: volume kosong belum tentu berarti mati — sering cuma tidak
    terindeks. Pool tanpa volume tetap dipertahankan kalau TVL-nya masih ≥5% pool
    terdalam. Tanpa ini, pool Uniswap v4 CAKE/USDT ber-TVL $922k ikut terbuang."""
    alive = [p.get("tvl_usd") or 0 for p in pools if (p.get("tvl_usd") or 0) > 0]
    if not alive:
        return pools, []
    floor = max(alive) * keep_ratio
    kept, dropped = [], []
    for p in pools:
        tvl = p.get("tvl_usd") or 0
        vol = p.get("vol24_usd") or 0
        if tvl > 0 and (vol > 0 or tvl >= floor):
            kept.append(p)
        else:
            dropped.append(p)
    return kept, dropped


def _drop_offprice_pools(pools: list, token_dec: int, token_addr: str) -> tuple[list, list]:
    """Buang pool yang harganya menyimpang jauh dari pool terdalam.

    Patokannya pool ber-TVL terbesar, bukan angka mutlak: pool terdalam yang
    paling sering diarbitrase, jadi harganya paling dekat ke pasar. Pool yang
    harganya tak bisa dihitung dibiarkan lewat (tidak ada dasar untuk membuang)."""
    for p in pools:
        p["price_usd"] = _pool_price_usd(p, token_dec, token_addr)
    priced = [p for p in pools if p.get("price_usd")]
    if len(priced) < 2:
        return pools, []
    ref = max(priced, key=lambda p: p.get("tvl_usd") or 0)
    rp = ref["price_usd"]
    kept, dropped = [], []
    for p in pools:
        x = p.get("price_usd")
        if p is not ref and x and rp and abs(x / rp - 1) > PRICE_DEVIATION_MAX:
            p["deviation"] = x / rp - 1
            dropped.append(p)
        else:
            kept.append(p)
    return kept, dropped


def discover_any(chain_id: int, token_addr: str) -> dict:
    """Discovery pool untuk SEMUA UI (bot & web): API Uniswap dulu (lengkap, cepat,
    termasuk v4 fee non-standar), fallback scan RPC kalau API mati / nihil.
    Terakhir ditambah pool ber-quote di luar daftar tetap (mis. RTX/NVDAB) — banyak
    memecoin cuma punya pool jenis ini, tak terjangkau scan quote biasa."""
    # Krystal duluan: <1 detik, dan angkanya sama persis dengan yang dilihat user di
    # web mereka. Tiap pool tetap diverifikasi on-chain di discover_krystal.
    kr = []
    try:
        kr = discover_krystal(chain_id, token_addr)
    except Exception:
        kr = []
    src_name = "krystal"
    if not kr:
        # Krystal gagal/tidak kenal → GeckoTerminal. Endpoint mereka TIDAK di belakang
        # Cloudflare, jadi ini satu-satunya daftar pool yang tetap jalan di host yang
        # IP-nya kena managed challenge (terukur di VPS: Krystal & indexer Uniswap
        # dua-duanya "Just a moment…", GeckoTerminal 200). Ia juga memuat pool v4
        # ber-fee non-standar yang mustahil ditemukan scan tier tetap.
        try:
            kr = discover_gecko(chain_id, token_addr)
            src_name = "gecko"
        except Exception:
            kr = []
    if kr:
        try:
            tinfo = token_info(get_w3(chain_id), Web3.to_checksum_address(token_addr))
        except Exception:
            tinfo = {"symbol": "?", "decimals": 18, "name": ""}
        res = {"token": tinfo, "pools": kr, "source": src_name}
        for p in res["pools"]:
            p["thin"] = (p.get("tvl_usd") or 0) < 50
        # Pool dari Krystal TIDAK disaring lagi (daftar mereka sudah tersaring
        # >=$1K TVL). Harga menyimpang cuma DITANDAI, bukan dibuang, supaya isi
        # daftarnya sama dengan web Krystal.
        tdec = tinfo.get("decimals", 18)
        for p in res["pools"]:
            p["price_usd"] = _pool_price_usd(p, tdec, token_addr)
        priced = [p for p in res["pools"] if p.get("price_usd")]
        if len(priced) > 1:
            ref = max(priced, key=lambda p: p.get("tvl_usd") or 0)
            for p in priced:
                dev = p["price_usd"] / ref["price_usd"] - 1
                if p is not ref and abs(dev) > PRICE_DEVIATION_MAX:
                    p["deviation"] = dev
        # Krystal terbukti melewatkan pool ber-quote aneh (RUBY/RDDT $40k tidak ada
        # di daftar mereka padahal itu pool terbesarnya). Jadi pencari pair aneh TETAP
        # dijalankan dan hasilnya digabung — persis aturan "pair aneh pakai discovery
        # sendiri". Angka pool yang Krystal punya tetap dari Krystal.
        try:
            seen = {str(p["pool"]).lower() for p in res["pools"]}
            extra = [p for p in discover_foreign_pools(get_w3(chain_id), chain_id,
                                                       token_addr, seen)
                     if (p.get("tvl_usd") or 0) > 0]
            res["pools"] += extra
        except Exception:
            pass
        res["pools"].sort(key=lambda p: p.get("tvl_usd") or 0, reverse=True)
        res["dropped_dead"], res["dropped_offprice"] = [], []
        res["hook_pools"] = count_hook_pools(chain_id, token_addr)
        return res

    # Token tidak ada di Krystal (pair aneh) → discovery sendiri.
    # Indexer Uniswap menutup sisi Uniswap (termasuk pool v4 ber-fee non-standar yang
    # tak terjangkau scan tier), scan RPC menutup DEX lain. Keduanya digabung — bukan
    # salah satu — supaya di BSC pool PancakeSwap DAN Uniswap sama-sama muncul.
    uni = None
    try:
        uni = uni_discover(chain_id, token_addr)
    except Exception:
        uni = None
    skip = {uni_api_dex(chain_id)} if (uni and uni.get("pools")) else set()
    res = discover_pools(chain_id, token_addr, skip_dexes=skip)
    if uni and uni.get("pools"):
        seen = {str(p["pool"]).lower() for p in res["pools"]}
        res["pools"] += [p for p in uni["pools"] if str(p["pool"]).lower() not in seen]
        if str((res.get("token") or {}).get("symbol", "?")) == "?":
            res["token"] = uni.get("token") or res.get("token")

    # BERURUTAN, jangan diparalelkan dengan scan di atas: keduanya memukul RPC yang
    # sama dan pada endpoint publik hasilnya justru kena rate-limit (terukur 19s vs
    # 10s untuk token ber-pool banyak).
    try:
        skip = {str(p["pool"]).lower() for p in res["pools"]}
        extra = discover_foreign_pools(get_w3(chain_id), chain_id, token_addr, skip)
        if extra:
            res["pools"] = sorted(res["pools"] + extra,
                                  key=lambda p: p["tvl_usd"], reverse=True)
    except Exception:
        pass
    # Pool bernilai kecil tidak lagi dibuang — ditandai supaya UI bisa memperingatkan
    # (slippage besar, harga gampang digeser, fee kemungkinan tak menutup gas).
    for p in res["pools"]:
        p["thin"] = (p.get("tvl_usd") or 0) < 50
    tdec = (res.get("token") or {}).get("decimals", 18)
    try:
        _fill_missing_sqrtp(chain_id, res["pools"])
        _fill_onchain_tvl(chain_id, res["pools"], token_addr, tdec)
        # TVL v4 tidak bisa dibaca dari saldo pool (semua currency ditahan
        # PoolManager yang sama) → pakai likuiditas riil dexscreener kalau ada
        dexliq = {}
        for pr in _dex_pairs(chain_id, token_addr):
            lq = float((pr.get("liquidity") or {}).get("usd") or 0)
            if lq > 0:
                dexliq[str(pr.get("pairAddress") or "").lower()] = lq
        # Krystal mengukur pool v4 dengan benar (dexscreener sering tak meng-index
        # poolId v4 sama sekali) → dipakai duluan, dexscreener jadi cadangan.
        kr = krystal_pools(chain_id, token_addr)
        for p in res["pools"]:
            k = kr.get(str(p["pool"]).lower())
            if k and k.get("tvl_usd"):
                # untuk v3/v2 hitungan on-chain kita sudah tepat; Krystal cuma
                # menambal volume/APR. Untuk v4 TVL-nya ikut Krystal.
                if p.get("ver") == 4 or p.get("tvl_src") != "chain":
                    p["tvl_usd"] = k["tvl_usd"]
                    p["tvl_src"] = "krystal"
                if k.get("vol24_usd") is not None:
                    p["vol24_usd"] = k["vol24_usd"]
                if k.get("apr_pct") is not None:
                    p["apr_pct"] = k["apr_pct"]
                continue
            if p.get("ver") == 4:
                real = dexliq.get(str(p["pool"]).lower())
                if real:
                    p["tvl_usd"] = real
                    p["tvl_src"] = "dexscreener"
        _fill_v4_tvl(chain_id, res["pools"])
        res["pools"].sort(key=lambda p: p.get("tvl_usd") or 0, reverse=True)
    except Exception:
        pass
    # Urutan penting: buang pool mati DULU, baru filter harga — patokan harga harus
    # diambil dari pool yang benar-benar diperdagangkan.
    res["pools"], res["dropped_dead"] = _drop_dead_pools(res["pools"])
    res["pools"], res["dropped_offprice"] = _drop_offprice_pools(
        res["pools"], (res.get("token") or {}).get("decimals", 18), token_addr)
    res["pools"].sort(key=lambda p: p.get("tvl_usd") or 0, reverse=True)
    res["hook_pools"] = count_hook_pools(chain_id, token_addr)
    return res


# ---------- Discovery pool via scan RPC (fallback) ----------
def discover_pools(chain_id: int, token_addr: str, skip_dexes=()) -> dict:
    """Scan semua quote × fee tier (paralel). Return {token, pools} urut TVL desc.
    skip_dexes: DEX yang pool v3/v4-nya sudah didapat dari indexer — pair v2 TETAP
    dipindai karena indexer Uniswap tidak mengembalikan pool v2 sama sekali."""
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
    token = Web3.to_checksum_address(token_addr)

    quotes = [(qsym, Web3.to_checksum_address(qaddr)) for qsym, qaddr in cfg["quotes"].items()
              if Web3.to_checksum_address(qaddr) != token]
    # Satu chain bisa punya beberapa DEX (BSC: PancakeSwap + Uniswap). Tiap DEX punya
    # factory DAN daftar fee tier sendiri, jadi kombinasinya dibangun per DEX dan
    # tiap pool membawa asalnya di p["dex"] — itu yang menentukan kontrak mana yang
    # dipakai saat mint/close nanti.
    combos = [(dname, qsym, q, fee)
              for dname in dex_names(chain_id) if dname not in skip_dexes
              for qsym, q in quotes
              for fee in fee_tiers(chain_id, dname)]

    with ThreadPoolExecutor(max_workers=5) as ex:
        tinfo_f = ex.submit(token_info, w3, token)
        qmeta_f = {qsym: (ex.submit(token_info, w3, q),
                          ex.submit(quote_usd_price, w3, chain_id, qsym))
                   for qsym, q in quotes}
        factories = {d: w3.eth.contract(address=Web3.to_checksum_address(dex_cfg(chain_id, d)["factory"]),
                                        abi=FACTORY_ABI) for d in dex_names(chain_id)}
        addr_futs = [(c, ex.submit(
            factories[c[0]].functions.getPool(*sort_tokens(token, c[2]), c[3]).call))
            for c in combos]
        found = []
        for (dname, qsym, q, fee), fut in addr_futs:
            try:
                pool_addr = fut.result()
            except Exception:
                continue
            if int(pool_addr, 16) != 0:
                found.append((dname, qsym, q, fee, pool_addr))

        def detail(item):
            dname, qsym, q, fee, pool_addr = item
            pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
            slot0 = pool.functions.slot0().call()
            liq = pool.functions.liquidity().call()
            qdec = qmeta_f[qsym][0].result()["decimals"]
            qusd = qmeta_f[qsym][1].result()
            q_bal = erc20(w3, q).functions.balanceOf(pool_addr).call() / 10 ** qdec
            t0, t1 = sort_tokens(token, q)
            q_is_t1 = q == t1
            # TVL = kedua sisi reserve (bukan sisi quote × 2). Di pool v3 likuiditas
            # sering menumpuk jauh di luar range pada sisi meme, jadi "quote × 2"
            # bisa meleset puluhan kali lipat — dan APR ikut ngaco karenanya.
            m_usd = 0.0
            try:
                mdec = tinfo_f.result()["decimals"]
                raw = (slot0[0] / Q96) ** 2                       # token1 per token0 (rasio wei)
                meme_in_q = (raw if q_is_t1 else (1 / raw if raw else 0)) * 10 ** (mdec - qdec)
                m_bal = erc20(w3, token).functions.balanceOf(pool_addr).call() / 10 ** mdec
                m_usd = m_bal * meme_in_q * qusd
            except Exception:
                m_usd = q_bal * qusd      # gagal baca sisi meme → balik ke estimasi lama
            # Pool mati (liquidity 0 / tick mentok batas = harga tak pernah diset benar):
            # rasio slot0 jadi ekstrem (~1e38) sehingga saldo debu ikut jadi TVL raksasa.
            # Pool begini tak bisa dipakai LP — nilai nol saja biar tak nongol di atas.
            if liq == 0 or abs(slot0[1]) >= MAX_TICK - 1:
                m_usd = 0.0
                q_bal = 0.0
            return {
                "ver": 3, "dex": dname, "pool": pool_addr, "fee": fee,
                "quote_sym": qsym, "quote_addr": q,
                "quote_decimals": qdec, "quote_usd": qusd,
                "tick": slot0[1], "sqrtp": slot0[0], "liquidity": liq,
                "tvl_usd": q_bal * qusd + m_usd,
                "token0": t0, "token1": t1, "quote_is_token1": q_is_t1,
            }

        vols_f = ex.submit(dex_volumes, chain_id, token)
        v2_futs = [ex.submit(discover_v2_pools, w3, chain_id, token, d)
                   for d in dex_names(chain_id)]
        v4_f = (ex.submit(discover_v4_pools, w3, chain_id, token)
                if v4_dex(chain_id) not in skip_dexes else None)
        pools = []
        for fut in [ex.submit(detail, it) for it in found]:
            try:
                pools.append(fut.result())
            except Exception:
                continue
        for fut in (*v2_futs, v4_f):
            if fut is None:
                continue
            try:
                pools += fut.result()
            except Exception:
                continue
        # pool fee non-standar (v3 custom tier / v4 fee-spacing bebas) dari dexscreener,
        # semua diverifikasi on-chain sebelum masuk daftar
        try:
            skip_v3 = {p["pool"].lower() for p in pools if p.get("ver", 3) == 3}
            skip_v4 = {str(p["pool"]).lower() for p in pools if p.get("ver") == 4}
            pools += discover_dex_pools(w3, chain_id, token, skip_v3, skip_v4)
        except Exception:
            pass
        tinfo = tinfo_f.result()
        vols = vols_f.result()

    # TVL v4 dari dexscreener (reserve riil, termasuk likuiditas parkir di luar range —
    # estimasi virtual cuma menghitung liquidity aktif di tick sekarang, bisa jauh
    # di bawah angka UI Uniswap). Probe round-trip tetap jadi gerbang keamanannya.
    dexliq = {}
    try:
        for pr in _dex_pairs(chain_id, token):
            lq = float((pr.get("liquidity") or {}).get("usd") or 0)
            if lq > 0:
                dexliq[(pr.get("pairAddress") or "").lower()] = lq
    except Exception:
        pass
    for p in pools:
        if p.get("ver") == 4:
            real = dexliq.get(str(p["pool"]).lower())
            if real:
                p["tvl_usd"] = real
    pools.sort(key=lambda p: p["tvl_usd"], reverse=True)

    for p in pools:
        v = vols.get(p["pool"].lower())
        p["vol24_usd"] = v
        # APR estimasi pool: fee 24 jam × 365 ÷ TVL
        p["apr_pct"] = (v * p["fee"] / 1e6 / p["tvl_usd"] * 365 * 100) if (v and p["tvl_usd"]) else None
    pools.sort(key=lambda p: p["tvl_usd"], reverse=True)
    return {"token": tinfo, "pools": pools}


V4_INIT_TOPIC = "0x" + Web3.keccak(
    text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex().removeprefix("0x")


def _v4_key_from_init(w3: Web3, chain_id: int, pool_id_hex: str, _cache={}) -> tuple | None:
    """PoolKey dari event Initialize PoolManager (immutable → cache permanen).
    Return None kalau tidak ketemu / hash tidak cocok / pakai hooks."""
    cfg = v4_cfg(chain_id)
    ck = (chain_id, pool_id_hex.lower())
    if ck in _cache:
        return _cache[ck]
    pm = Web3.to_checksum_address(cfg["v4_pm"])
    log = None
    try:
        logs = w3.eth.get_logs({"address": pm, "fromBlock": 0, "toBlock": "latest",
                                "topics": [V4_INIT_TOPIC, pool_id_hex]})
        if logs:
            lg = logs[0]
            log = {"topics": [t.hex() if hasattr(t, "hex") else t for t in lg["topics"]],
                   "data": lg["data"].hex() if hasattr(lg["data"], "hex") else lg["data"]}
    except Exception:
        # RPC batasi range getLogs → fallback API explorer Blockscout
        try:
            r = requests.get(f"{cfg['explorer']}/api", params={
                "module": "logs", "action": "getLogs", "fromBlock": "0", "toBlock": "latest",
                "address": pm, "topic0": V4_INIT_TOPIC, "topic1": pool_id_hex,
                "topic0_1_opr": "and"}, timeout=15)
            res = r.json().get("result")
            if isinstance(res, list) and res:
                log = res[0]
        except Exception:
            pass
    key = None
    if log:
        try:
            t2, t3 = log["topics"][2], log["topics"][3]
            c0 = Web3.to_checksum_address("0x" + str(t2).removeprefix("0x")[-40:])
            c1 = Web3.to_checksum_address("0x" + str(t3).removeprefix("0x")[-40:])
            d = str(log["data"]).removeprefix("0x")
            fee, sp = int(d[0:64], 16), int(d[64:128], 16)
            hooks = "0x" + d[152:192]
            cand = (c0, c1, fee, sp, Web3.to_checksum_address(hooks))
            # verifikasi: hash key harus == poolId, dan hanya pool vanilla (hooks 0)
            calc = "0x" + v4_pool_id(cand).hex().removeprefix("0x")
            if calc.lower() == pool_id_hex.lower() and int(hooks, 16) == 0:
                key = cand
        except Exception:
            key = None
    _cache[ck] = key
    return key


def which_dex_v3(w3: Web3, chain_id: int, addr: str, t0: str, t1: str, fee: int) -> str | None:
    """DEX pemilik pool v3 ini — dicek ke factory tiap DEX. None kalau bukan milik
    satu pun (pool DEX lain yang ikut nyasar di daftar dexscreener)."""
    for d in dex_names(chain_id):
        try:
            f = w3.eth.contract(address=Web3.to_checksum_address(dex_cfg(chain_id, d)["factory"]),
                                abi=FACTORY_ABI)
            if f.functions.getPool(t0, t1, fee).call().lower() == str(addr).lower():
                return d
        except Exception:
            continue
    return None


def which_dex_v2(w3: Web3, chain_id: int, pair: str, a: str, b: str) -> str | None:
    """DEX pemilik pair v2 ini. Dipakai juga saat aksi: alamat pair tidak menyimpan
    router mana yang harus dipakai, jadi ditanyakan ke factory."""
    for d in dex_names(chain_id):
        c = dex_cfg(chain_id, d)
        if not c.get("v2_factory"):
            continue
        try:
            f = w3.eth.contract(address=Web3.to_checksum_address(c["v2_factory"]), abi=V2_FACTORY_ABI)
            if f.functions.getPair(a, b).call().lower() == str(pair).lower():
                return d
        except Exception:
            continue
    return None


def discover_dex_pools(w3: Web3, chain_id: int, token: str,
                       skip_v3: set, skip_v4: set) -> list[dict]:
    """Pool tambahan dari daftar dexscreener yang kelewat scan standar:
    v3 fee non-standar (diverifikasi via factory.getPool) dan v4 fee/spacing
    custom (PoolKey dipulihkan dari log Initialize, hash diverifikasi)."""
    cfg = CHAINS[chain_id]
    token = Web3.to_checksum_address(token)
    quotes_lc = {a.lower(): s for s, a in cfg["quotes"].items()}
    out = []
    pairs = _dex_pairs(chain_id, token)
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)

    cands = []
    n3 = n4 = 0
    for pr in pairs:
        labels = pr.get("labels") or []
        addr = pr.get("pairAddress") or ""
        # tanpa ambang likuiditas: pool kecil tetap jadi kandidat, verifikasi on-chain
        # + probe round-trip di build() yang jadi gerbangnya
        if "v3" in labels and addr.lower() not in skip_v3 and n3 < 6:
            cands.append(("v3", addr))
            n3 += 1
        elif ("v4" in labels and any_has_v4(chain_id) and len(addr) == 66
              and addr.lower() not in skip_v4 and n4 < 8):
            cands.append(("v4", addr))
            n4 += 1

    def build(item):
        kind, addr = item
        try:
            if kind == "v3":
                pool = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=POOL_ABI)
                t0, t1 = pool.functions.token0().call(), pool.functions.token1().call()
                fee = pool.functions.fee().call()
                # otentikasi: alamat harus terdaftar di salah satu factory chain ini.
                # Ini juga yang menyaring pool DEX yang tidak didukung (mis. BiSwap)
                # yang ikut muncul di daftar dexscreener.
                dname = which_dex_v3(w3, chain_id, addr, t0, t1, fee)
                if not dname:
                    return None
                if t1.lower() in quotes_lc:
                    qsym, q, q_is_t1 = quotes_lc[t1.lower()], t1, True
                elif t0.lower() in quotes_lc:
                    qsym, q, q_is_t1 = quotes_lc[t0.lower()], t0, False
                else:
                    return None
                slot0 = pool.functions.slot0().call()
                qdec = token_info(w3, q)["decimals"]
                qusd = quote_usd_price(w3, chain_id, qsym)
                q_bal = erc20(w3, q).functions.balanceOf(addr).call() / 10 ** qdec
                if q_bal <= 0:   # sisi quote kosong = tidak ada yang bisa di-LP
                    return None
                meme = t0 if q_is_t1 else t1
                mdec = token_info(w3, meme)["decimals"]
                raw = (slot0[0] / Q96) ** 2
                meme_in_q = (raw if q_is_t1 else (1 / raw if raw else 0)) * 10 ** (mdec - qdec)
                m_bal = erc20(w3, meme).functions.balanceOf(addr).call() / 10 ** mdec
                return {
                    "ver": 3, "dex": dname, "pool": Web3.to_checksum_address(addr), "fee": fee,
                    "tick_spacing": pool.functions.tickSpacing().call(),
                    "quote_sym": qsym, "quote_addr": q, "quote_decimals": qdec, "quote_usd": qusd,
                    "tick": slot0[1], "sqrtp": slot0[0],
                    "liquidity": pool.functions.liquidity().call(),
                    "tvl_usd": q_bal * qusd + m_bal * meme_in_q * qusd,
                    "token0": t0, "token1": t1, "quote_is_token1": q_is_t1,
                }
            else:
                key = _v4_key_from_init(w3, chain_id, addr)
                if not key:
                    return None
                qsym4, q_is_c1 = _v4_quote_side(chain_id, key[0], key[1])   # discovery: tanpa w3
                if not qsym4:
                    return None
                qaddr = key[1] if q_is_c1 else key[0]
                if qaddr.lower() == V4_NATIVE:
                    qsym4 = cfg["native_symbol"]
                pid = v4_pool_id(key)
                sv = _v4c(w3, chain_id, "v4_stateview", V4_STATEVIEW_ABI)
                sqrtp, tick, _, _ = sv.functions.getSlot0(pid).call()
                pliq = sv.functions.getLiquidity(pid).call()
                if sqrtp == 0 or pliq == 0:
                    return None
                qinfo = _v4_currency_info(w3, chain_id, qaddr)
                price_sym = qsym4 if qaddr.lower() != V4_NATIVE else cfg["wrapped_symbol"]
                qusd = quote_usd_price(w3, chain_id, price_sym)
                q_virt = (pliq * sqrtp // Q96) if q_is_c1 else (pliq * Q96 // sqrtp if sqrtp else 0)
                tvl = q_virt / 10 ** qinfo["decimals"] * qusd * 2
                if tvl <= 0:
                    return None
                probe = int(min(100 / qusd if qusd else 0,
                                q_virt / 10 ** qinfo["decimals"] / 100 or 1) * 10 ** qinfo["decimals"]) or 1
                if not v4_roundtrip_ok(w3, chain_id, key, q_is_c1, probe):
                    return None
                return {
                    "ver": 4, "dex": v4_dex(chain_id), "pool": "0x" + pid.hex().removeprefix("0x"), "pool_id": pid,
                    "key": key, "fee": key[2], "tick_spacing": key[3],
                    "quote_sym": qsym4, "quote_addr": key[1] if q_is_c1 else key[0],
                    "quote_decimals": qinfo["decimals"], "quote_usd": qusd,
                    "tick": tick, "sqrtp": sqrtp, "liquidity": pliq, "tvl_usd": tvl,
                    "token0": key[0], "token1": key[1], "quote_is_token1": q_is_c1,
                }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=5) as ex:
        for r in ex.map(build, cands):
            if r:
                out.append(r)
    return out


# ---------- Discovery pool ber-quote di luar daftar tetap ----------
def count_hook_pools(chain_id: int, token: str) -> int:
    """Berapa pool v4 ber-hooks yang ADA tapi sengaja tidak didukung.

    Hook = kontrak arbitrer yang ikut jalan di tiap swap/mint/burn, bisa memotong
    hasil atau memblokir penarikan. Bot menolaknya, tapi jumlahnya harus disebutkan
    ke user — kalau tidak, pool besar bisa hilang dari daftar tanpa penjelasan
    (kasus nyata: RUBY/RDDT ber-TVL $40k)."""
    n = 0
    seen = set()
    try:
        for e in (uni_pools(chain_id, token) or []):
            if str(e.get("protocolVersion")) != "PROTOCOL_VERSION_V4":
                continue
            h = str((e.get("hooks") or {}).get("address") or V4_NATIVE)
            pid = str(e.get("poolId", "")).lower()
            if pid and pid not in seen and int(h, 16) != 0:
                seen.add(pid)
                n += 1
    except Exception:
        pass
    try:
        for e in krystal_raw(chain_id, token):
            h = str(e.get("hooks") or V4_NATIVE)
            pid = str(e.get("poolAddress", "")).lower()
            if pid and pid not in seen and h and int(h, 16) != 0:
                seen.add(pid)
                n += 1
    except Exception:
        pass
    return n


def _dexliq_of(chain_id: int, token: str, addr: str) -> float:
    """Likuiditas USD satu pool menurut dexscreener (v4 tak bisa dihitung on-chain)."""
    for pr in _dex_pairs(chain_id, token):
        if str(pr.get("pairAddress") or "").lower() == str(addr).lower():
            return float((pr.get("liquidity") or {}).get("usd") or 0)
    return 0.0


def _foreign_counter(pr: dict, token_lc: str) -> tuple[str, str] | None:
    """(alamat, simbol) sisi lawan dari entri dexscreener. None kalau tidak jelas."""
    for side, other in (("baseToken", "quoteToken"), ("quoteToken", "baseToken")):
        a = str((pr.get(side) or {}).get("address") or "")
        if a.lower() == token_lc:
            o = pr.get(other) or {}
            oa = str(o.get("address") or "")
            if oa.startswith("0x") and len(oa) == 42:
                return oa, str(o.get("symbol") or "?")
    return None


def discover_foreign_pools(w3: Web3, chain_id: int, token: str, skip: set,
                           max_pools: int = 6) -> list[dict]:
    """Pool token terhadap token lawan DI LUAR daftar quote tetap — mis. RTX/NVDAB,
    yang jumlahnya banyak di memecoin dan tak akan pernah ketemu oleh scan quote biasa.

    Kandidat datang dari dexscreener, tapi tidak ada satu pun angka darinya yang
    dipercaya untuk transaksi: alamat pool WAJIB cocok dengan factory chain ini
    (itu juga yang menendang pool DEX lain), dan token lawannya wajib bisa dihargai
    USD on-chain — tanpa harga, nilai posisi & PnL tidak bisa dihitung."""
    cfg = CHAINS[chain_id]
    token = Web3.to_checksum_address(token)
    token_lc = token.lower()
    quotes_lc = {str(a).lower() for a in cfg["quotes"].values()}
    pairs = _dex_pairs(chain_id, token)
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)

    v2f = (w3.eth.contract(address=Web3.to_checksum_address(cfg["v2_factory"]), abi=V2_FACTORY_ABI)
           if cfg.get("v2_factory") else None)
    f3 = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)

    cands, seen = [], set()
    for pr in pairs:
        addr = str(pr.get("pairAddress") or "")
        if not addr.startswith("0x") or len(addr) not in (42, 66) or addr.lower() in skip:
            continue
        c = _foreign_counter(pr, token_lc)
        if not c or c[0].lower() in quotes_lc or c[0].lower() == token_lc:
            continue          # quote tetap → sudah ditangani scan biasa
        if addr.lower() in seen:
            continue
        seen.add(addr.lower())
        labels = pr.get("labels") or []
        if "v4" in labels:
            # poolId 32-byte, bukan alamat. PoolKey dipulihkan & dibuktikan lewat hash.
            ver = 4 if (any_has_v4(chain_id) and len(addr) == 66) else None
        elif "v3" in labels:
            ver = 3
        elif "v2" in labels or not labels:
            ver = 2
        else:
            ver = None
        if ver is None:
            continue
        # v4: addr itu poolId 32-byte — JANGAN di-checksum (lihat catatan di CLAUDE.md)
        addr_n = addr.lower() if ver == 4 else Web3.to_checksum_address(addr)
        cands.append((ver, addr_n, Web3.to_checksum_address(c[0]), c[1],
                      float((pr.get("volume") or {}).get("h24") or 0)))
        if len(cands) >= max_pools:
            break

    def build(item):
        ver, addr, counter, csym, vol24 = item
        try:
            if ver == 4:
                key = (_v4_key_from_indexer(chain_id, token, addr)
                       or _v4_key_from_init(w3, chain_id, addr))
                if not key:
                    return None
                c0, c1 = Web3.to_checksum_address(key[0]), Web3.to_checksum_address(key[1])
                q = c1 if c0.lower() == token.lower() else c0
                if quote_backing_usd(w3, chain_id, q) <= 0:
                    return None
                qsym, qusd, qdec = _quote_meta(w3, chain_id, q, {a.lower(): sname
                                                                 for sname, a in cfg["quotes"].items()})
                if qusd <= 0:
                    return None
                pid = v4_pool_id(key)
                sqrtp = v4_slot0(w3, chain_id, pid)[0]
                p = {"ver": 4, "dex": v4_dex(chain_id),
                     "pool": "0x" + pid.hex().removeprefix("0x"), "pool_id": pid, "key": key,
                     "fee": key[2], "tick_spacing": key[3], "sqrtp": sqrtp, "tick": None,
                     "quote_sym": qsym, "quote_addr": q, "quote_decimals": qdec,
                     "quote_usd": qusd, "quote_is_token1": q.lower() == c1.lower(),
                     "token0": c0, "token1": c1, "tvl_usd": 0.0,
                     "vol24_usd": vol24 or None, "foreign_quote": q.lower() != V4_NATIVE}
                lq = _dexliq_of(chain_id, token, addr)
                if lq:
                    p["tvl_usd"] = lq
                    p["tvl_src"] = "dexscreener"
                v, tvl = p.get("vol24_usd"), p["tvl_usd"]
                p["apr_pct"] = (v * p["fee"] / 1e6 / tvl * 365 * 100) if (v and tvl) else None
                return p
            # Syaratnya bukan sekadar "punya harga" (token_usd_price bisa jatuh ke
            # dexscreener untuk token apa pun), tapi punya sokongan likuiditas
            # on-chain terhadap quote tetap — itu yang membuat nilainya bisa
            # diverifikasi sendiri DAN membuat auto-buy/auto-swap punya rute.
            if quote_backing_usd(w3, chain_id, counter) <= 0:
                return None
            qusd = token_usd_price(w3, chain_id, counter)
            if qusd <= 0:
                return None       # tak bisa dihargai → nilai posisi/PnL mustahil dihitung
            qdec = token_info(w3, counter)["decimals"]
            if ver == 2:
                dname = which_dex_v2(w3, chain_id, addr, token, counter)
                if not dname:
                    return None   # bukan pair factory chain ini
                cfg2 = dex_cfg(chain_id, dname)
                rq, rm = _v2_pair_reserves(w3, addr, counter)
                if rq == 0 or rm == 0:
                    return None   # pair kosong: harga belum ada, deposit pertama yang menentukannya
                num, den = cfg2.get("v2_swap_num", 997), cfg2.get("v2_swap_den", 1000)
                probe = int(min(100 / qusd, rq / 10 ** qdec / 100 or 1) * 10 ** qdec) or 1
                o1 = probe * num * rm // (rq * den + probe * num)
                back = o1 * num * rq // (rm * den + o1 * num)
                if back < probe * 70 // 100:
                    return None   # dust / harga dimanipulasi
                t0, t1 = sort_tokens(token, counter)
                p = {"ver": 2, "dex": dname, "pool": addr, "fee": cfg2.get("v2_fee", 3000),
                     "tick": None, "sqrtp": None, "liquidity": None,
                     "reserve_quote": rq, "reserve_meme": rm,
                     "tvl_usd": rq / 10 ** qdec * qusd * 2}
            else:
                pool = w3.eth.contract(address=addr, abi=POOL_ABI)
                t0, t1 = pool.functions.token0().call(), pool.functions.token1().call()
                fee = pool.functions.fee().call()
                dname = which_dex_v3(w3, chain_id, addr, t0, t1, fee)
                if not dname:
                    return None
                slot0 = pool.functions.slot0().call()
                q_bal = erc20(w3, counter).functions.balanceOf(addr).call() / 10 ** qdec
                mdec = token_info(w3, token)["decimals"]
                raw = (slot0[0] / Q96) ** 2
                q_is_t1 = Web3.to_checksum_address(t1) == counter
                meme_in_q = (raw if q_is_t1 else (1 / raw if raw else 0)) * 10 ** (mdec - qdec)
                m_bal = erc20(w3, token).functions.balanceOf(addr).call() / 10 ** mdec
                p = {"ver": 3, "dex": dname, "pool": addr, "fee": fee,
                     "tick_spacing": pool.functions.tickSpacing().call(),
                     "tick": slot0[1], "sqrtp": slot0[0],
                     "liquidity": pool.functions.liquidity().call(),
                     "tvl_usd": q_bal * qusd + m_bal * meme_in_q * qusd}
            t0, t1 = sort_tokens(token, counter)
            p.update({
                "quote_sym": register_quote(chain_id, csym, counter),
                "quote_addr": counter, "quote_decimals": qdec, "quote_usd": qusd,
                "token0": t0, "token1": t1, "quote_is_token1": counter == t1,
                "vol24_usd": vol24 or None, "foreign_quote": True,
            })
            v, tvl = p.get("vol24_usd"), p["tvl_usd"]
            p["apr_pct"] = (v * p["fee"] / 1e6 / tvl * 365 * 100) if (v and tvl) else None
            return p
        except Exception:
            return None

    out = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(build, cands):
            if r:
                out.append(r)
    return out


# ---------- Kalkulasi range strategi ----------
# Mode (dalam ruang HARGA MEME):
#   lower  = [P·(1−low%), P]            → single-sided, deposit QUOTE (cuan kalau harga turun)
#   upper  = [P, P·(1+up%)]             → single-sided, deposit MEME (cuan kalau harga naik)
#   wide   = [P·(1−low%), P·(1+up%)]    → dua sisi (butuh quote + meme, auto-swap)
#   stable = wide dengan lebar sempit (±low%/up% kecil)
def calc_strategy_range(cur_tick: int, fee: int, quote_is_token1: bool, mode: str,
                        low_pct: float, up_pct: float, gap: int = 1,
                        spacing: int | None = None) -> tuple[int, int]:
    sp = spacing or TICK_SPACING[fee]
    ln = math.log(1.0001)
    dn_ticks = int(abs(math.log(max(1e-9, 1 - low_pct / 100)) / ln))  # jarak sisi harga-turun
    up_ticks = int(abs(math.log(1 + up_pct / 100) / ln))              # jarak sisi harga-naik

    if mode in ("wide", "stable"):
        # harga-turun = tick bawah kalau quote=token1, tick atas kalau quote=token0
        below = dn_ticks if quote_is_token1 else up_ticks
        above = up_ticks if quote_is_token1 else dn_ticks
        lo = round_down(cur_tick - below, sp)
        hi = round_up(cur_tick + above, sp)
        if lo >= hi:
            lo = hi - sp
    else:
        # single-sided: posisi all-token1 ⇔ range di bawah tick; all-token0 ⇔ di atas.
        # gap × spacing dari harga: kalau nempel persis (gap 0), harga bisa nyebrang masuk
        # range selama wrap/approve → liquidity 0 → mint revert '0x' (retry menangani).
        deposit_token1 = quote_is_token1 if mode == "lower" else not quote_is_token1
        width = dn_ticks if mode == "lower" else up_ticks
        if deposit_token1:
            hi = round_down(cur_tick, sp) - sp * gap
            lo = round_down(cur_tick - width, sp)
            if lo >= hi:
                lo = hi - sp
        else:
            lo = round_up(cur_tick + 1, sp) + sp * gap
            hi = round_up(cur_tick + width, sp)
            if hi <= lo:
                hi = lo + sp
    return max(lo, MIN_TICK), min(hi, MAX_TICK)


def calc_quote_only_range(cur_tick: int, fee: int, width_pct: float, quote_is_token1: bool) -> tuple[int, int]:
    """Kompat lama: mode lower."""
    return calc_strategy_range(cur_tick, fee, quote_is_token1, "lower", width_pct, width_pct)


# ---------- Range bebas (batas ditentukan user, tidak dipatok ke harga sekarang) ----------
def ticks_from_prices(p_lo: float, p_hi: float, fee: int, quote_is_token1: bool,
                      mdec: int, qdec: int, spacing: int | None = None) -> tuple[int, int]:
    """Harga meme (dalam satuan quote) → batas tick, dibulatkan ke tick spacing.
    Dipakai untuk range yang letaknya bebas — termasuk yang seluruhnya di bawah
    atau di atas harga sekarang (mis. harga 60k, range 20k–40k)."""
    sp = spacing or TICK_SPACING[fee]
    if not (p_lo > 0 and p_hi > 0):
        raise RuntimeError("Batas range harus lebih besar dari 0.")

    def tick_of(p: float) -> float:
        raw = p * 10 ** (qdec - mdec) if quote_is_token1 else 10 ** (mdec - qdec) / p
        if raw <= 0:
            raise RuntimeError("Batas range di luar jangkauan.")
        return math.log(raw) / math.log(1.0001)

    ta, tb = sorted([tick_of(p_lo), tick_of(p_hi)])
    lo = round_down(int(math.floor(ta)), sp)
    hi = round_up(int(math.ceil(tb)), sp)
    if lo >= hi:
        hi = lo + sp
    return max(lo, MIN_TICK), min(hi, MAX_TICK)


def effective_mode(tick_lower: int, tick_upper: int, cur_tick: int, quote_is_token1: bool) -> str:
    """Sisi yang harus disetor untuk range tertentu.
    tick ≥ upper → posisi 100% token1 · tick < lower → 100% token0 · sisanya dua sisi."""
    if cur_tick >= tick_upper:
        return "lower" if quote_is_token1 else "upper"
    if cur_tick < tick_lower:
        return "upper" if quote_is_token1 else "lower"
    return "wide"


def _range_of(strategy: dict, cur_tick: int, fee: int, q_is_t1: bool,
              spacing: int | None) -> tuple[int, int, str]:
    """(tick_lower, tick_upper, mode efektif) untuk strategi apa pun.
    Range bebas dipakai apa adanya; mode-nya diturunkan dari posisi range
    terhadap harga sekarang, bukan dari pilihan user."""
    ticks = strategy.get("ticks")
    if ticks:
        lo, hi = int(ticks[0]), int(ticks[1])
        return lo, hi, effective_mode(lo, hi, cur_tick, q_is_t1)
    mode = strategy["mode"]
    lo, hi = calc_strategy_range(cur_tick, fee, q_is_t1, mode, strategy["low_pct"],
                                 strategy["up_pct"], strategy.get("gap", 1), spacing=spacing)
    return lo, hi, mode


def pool_volatility_daily(w3: Web3, pool_addr: str) -> float | None:
    """Estimasi volatilitas harian % dari TWAP oracle pool (drift tick 1 jam × √24).
    None kalau oracle belum punya riwayat (observationCardinality=1)."""
    pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
    try:
        cur = pool.functions.slot0().call()[1]
        cums = pool.functions.observe([3600, 0]).call()[0]
        twap = (cums[1] - cums[0]) / 3600
        drift_1h = abs(1.0001 ** (cur - twap) - 1) * 100
        return drift_1h * math.sqrt(24)
    except Exception:
        return None


# ---------- Aksi: wrap, approve, mint ----------
def find_pool_dex(w3: Web3, chain_id: int, a: str, b: str,
                  amount_in_wei: int = 0) -> tuple[str | None, int, str | None]:
    """Cari pool v3 pasangan (a, b) di SEMUA DEX chain ini. Return (pool, fee, dex).

    Tanpa `amount_in_wei`: pilih pool terdalam (dipakai untuk baca harga — pool dust
    bisa menyimpan harga ngaco).

    Dengan `amount_in_wei`: pilih yang paling MURAH untuk jumlah itu. Fee terendah
    saja bukan patokan — pool 0,05% yang tipis bisa jauh lebih mahal daripada pool
    0,3% yang dalam, karena price impact-nya menelan selisih fee. Skornya
    (1 − fee) × kedalaman/(kedalaman + jumlah): pendekatan constant-product, jadi
    perkiraan, tapi memperhitungkan dua-duanya. Untuk jumlah kecil skor ini otomatis
    dimenangkan fee terendah; untuk jumlah besar dimenangkan pool terdalam."""
    a = Web3.to_checksum_address(a)
    t0, t1 = sort_tokens(a, Web3.to_checksum_address(b))
    best, best_fee, best_score, best_dex = None, 0, -1.0, None
    for dname in dex_names(chain_id):
        factory = w3.eth.contract(
            address=Web3.to_checksum_address(dex_cfg(chain_id, dname)["factory"]), abi=FACTORY_ABI)
        for f in fee_tiers(chain_id, dname):
            try:
                addr = factory.functions.getPool(t0, t1, f).call()
            except Exception:
                continue
            if int(addr, 16) == 0:
                continue
            try:
                bal = erc20(w3, a).functions.balanceOf(addr).call()
            except Exception:
                continue
            if bal <= 0:
                continue
            try:
                # Saldo bukan bukti pool bisa dipakai: pool bisa punya DEBU dan
                # likuiditas aktif 0 — swap ke situ selalu revert '0x'. Terbukti di
                # WETH/MSFT fee 3000 (Robinhood): saldo 53 wei, liquidity() 0, rute
                # langsung ini menang atas 2-hop WETH→USDG→MSFT yang sebenarnya jalan,
                # dan mint gagal di jalur auto-buy quote.
                if w3.eth.contract(address=addr, abi=POOL_ABI).functions.liquidity().call() <= 0:
                    continue
            except Exception:
                continue
            if amount_in_wei > 0:
                score = (1 - f / 1e6) * (bal / (bal + amount_in_wei))
            else:
                score = float(bal)
            if score > best_score:
                best, best_fee, best_score, best_dex = addr, f, score, dname
    return best, best_fee, best_dex


def find_pool(w3: Web3, chain_id: int, a: str, b: str) -> tuple[str | None, int]:
    addr, fee, _ = find_pool_dex(w3, chain_id, a, b)
    return addr, fee


def wrapped_per_quote_wei(w3: Web3, chain_id: int, quote_addr: str) -> float:
    """Kurs wei wrapped per wei quote via pool wrapped/quote v3. Kalau pool langsung
    tidak ada (lazim untuk quote hasil auto-deteksi seperti NVDAB yang cuma
    berpasangan dengan USDT), kursnya dihitung dari harga USD kedua sisi."""
    cfg = CHAINS[chain_id]
    wrapped = Web3.to_checksum_address(cfg["wrapped"])
    quote = Web3.to_checksum_address(quote_addr)
    pool_addr, _ = find_pool(w3, chain_id, wrapped, quote)
    if pool_addr:
        raw = _pool_price_t1_per_t0(w3, pool_addr)  # t1-wei per t0-wei
        t0, _ = sort_tokens(wrapped, quote)
        return (1 / raw if raw else 0) if wrapped == t0 else raw
    q_usd = token_usd_price(w3, chain_id, quote)
    w_usd = quote_usd_price(w3, chain_id, cfg["wrapped_symbol"])
    if q_usd <= 0 or w_usd <= 0:
        raise RuntimeError(f"Tidak ada pool {cfg['wrapped_symbol']}/quote untuk konversi.")
    qdec = token_info(w3, quote)["decimals"]
    return q_usd / w_usd * 10 ** (18 - qdec)   # wei wrapped per wei quote


def ensure_quote_balance(w3: Web3, chain_id: int, pk: str, quote_addr: str, need_wei: int,
                         slippage_pct: float = 5.0) -> list[tuple[str, str]]:
    """Pastikan saldo quote cukup. Quote = wrapped → auto-wrap native.
    Quote lain (mis. USDG) → wrap native seperlunya lalu swap wrapped → quote.
    Return list (label, txhash)."""
    cfg = CHAINS[chain_id]
    account = w3.eth.account.from_key(pk)
    txs = []
    quote = Web3.to_checksum_address(quote_addr)
    wrapped = Web3.to_checksum_address(cfg["wrapped"])
    bal = erc20(w3, quote).functions.balanceOf(account.address).call()
    if bal >= need_wei:
        return txs
    deficit = need_wei - bal
    gas_reserve = gas_reserve_wei(chain_id, w3)

    if quote == wrapped:
        native = w3.eth.get_balance(account.address)
        if native < deficit + gas_reserve:
            raise RuntimeError(
                f"Saldo native kurang untuk wrap: punya {native / 1e18:.6f}, butuh {deficit / 1e18:.6f} + gas")
        weth = w3.eth.contract(address=quote, abi=WETH_ABI)
        h = send_tx(w3, pk, {"to": quote, "value": deficit, "data": calldata(weth.functions.deposit())})
        wait_ok(w3, h, "wrap")
        txs.append(("wrap", h))
        return txs

    # Modal gabungan: pakai quote lain yang SUDAH ada di wallet duluan. Untuk pool
    # ber-quote asing (mis. MSFT) itu 1 hop dari USDG, sedangkan jalur wrapped di bawah
    # butuh wrap + 2 hop — fee & slippage dobel, dan bisa gagal karena ETH-nya kurang
    # padahal USDG-nya menumpuk. compute_amount sudah menghitung saldo ini sebagai modal,
    # jadi eksekusinya wajib bisa mengambilnya.
    qdec_t = token_info(w3, quote)["decimals"]
    q_usd_t = token_usd_price(w3, chain_id, quote)
    for sym, oaddr in cfg["quotes"].items():
        if deficit <= 0:
            break
        oc = Web3.to_checksum_address(oaddr)
        if oc == quote or oc == wrapped:
            continue
        try:
            obal = erc20(w3, oc).functions.balanceOf(account.address).call()
            ousd = quote_usd_price(w3, chain_id, sym)
            if obal <= 0 or ousd <= 0 or q_usd_t <= 0:
                continue
            odec = token_info(w3, oc)["decimals"]
            spend = min(obal, int(deficit / 10 ** qdec_t * q_usd_t / ousd * 10 ** odec * 1.03))
            if spend <= 0:
                continue
            before = erc20(w3, quote).functions.balanceOf(account.address).call()
            txs += swap_any(chain_id, pk, oc, quote, spend, slippage_pct)
            got = poll_balance(w3, quote, account.address, before + 1) - before
            if got > 0:
                deficit -= got
        except Exception:
            continue
    if deficit <= 0:
        return txs

    # quote bukan wrapped: tutup kekurangan dengan swap wrapped → quote
    route = swap_route(w3, chain_id, wrapped, quote)   # jumlah belum diketahui di sini
    if route is None:
        raise RuntimeError(
            f"Saldo quote kurang dan tidak ada rute {cfg['wrapped_symbol']}→quote untuk auto-swap.")
    if len(route) == 1:
        rate = wrapped_per_quote_wei(w3, chain_id, quote)
        need_in = int(deficit * rate * 1.02)  # +2% margin biar hasil swap ≥ deficit
    else:
        # 2-hop: tidak ada pool langsung untuk menghitung kurs → pakai harga USD
        # kedua sisi, margin lebih besar karena fee & slippage dibayar dua kali
        qdec = token_info(w3, quote)["decimals"]
        q_usd = token_usd_price(w3, chain_id, quote)
        w_usd = quote_usd_price(w3, chain_id, cfg["wrapped_symbol"])
        if q_usd <= 0 or w_usd <= 0:
            raise RuntimeError("Harga USD quote/wrapped tidak terbaca — auto-buy quote dibatalkan.")
        need_in = int(deficit / 10 ** qdec * q_usd / w_usd * 1e18 * 1.05)
    if need_in <= 0:
        raise RuntimeError("Konversi kurs wrapped/quote gagal (rate 0).")
    wbal = erc20(w3, wrapped).functions.balanceOf(account.address).call()
    if wbal < need_in:
        wrap_amt = need_in - wbal
        native = w3.eth.get_balance(account.address)
        if native < wrap_amt + gas_reserve:
            raise RuntimeError(
                f"Saldo {cfg['wrapped_symbol']}+native kurang untuk beli quote: "
                f"butuh ~{need_in / 1e18:.6f} {cfg['wrapped_symbol']}, "
                f"punya {(wbal + native) / 1e18:.6f}")
        weth = w3.eth.contract(address=wrapped, abi=WETH_ABI)
        h = send_tx(w3, pk, {"to": wrapped, "value": wrap_amt, "data": calldata(weth.functions.deposit())})
        wait_ok(w3, h, "wrap")
        txs.append(("wrap", h))
        # tunggu saldo wrapped benar-benar terbaca sebelum swap — tanpa ini estimasi
        # gas swap bisa jalan di replika RPC yang belum sinkron dan revert
        # TRANSFER_FROM_FAILED walaupun wrap-nya sudah sukses
        poll_balance(w3, wrapped, account.address, need_in)
    try:
        for lbl, h in swap_any(chain_id, pk, wrapped, quote, need_in, slippage_pct):
            txs.append((lbl, h))
    except Exception as e:
        qs = token_info(w3, quote)["symbol"]
        raise RuntimeError(f"Gagal beli {qs} dari {cfg['wrapped_symbol']}: {e}")
    got = poll_balance(w3, quote, account.address, need_wei)
    if got < int(need_wei * 0.97):
        raise RuntimeError(f"Hasil swap ke quote kurang: {got} < {need_wei} (slippage terlalu besar?)")
    return txs


def ensure_approval(w3: Web3, pk: str, token_addr: str, spender: str, need_wei: int) -> list[tuple[str, str]]:
    account = w3.eth.account.from_key(pk)
    token = Web3.to_checksum_address(token_addr)
    spender = Web3.to_checksum_address(spender)
    c = erc20(w3, token)
    if c.functions.allowance(account.address, spender).call() >= need_wei:
        return []
    h = send_tx(w3, pk, {"to": token, "data": calldata(c.functions.approve(spender, MAX_UINT256))})
    wait_ok(w3, h, "approve")
    return [("approve", h)]


def plan_two_sided(sqrtp_x96: int, tick_lower: int, tick_upper: int,
                   budget_quote_wei: int, quote_is_token1: bool) -> tuple[int, int]:
    """Bagi budget quote jadi (quote_keep_wei, quote_to_swap_wei) supaya rasio
    token0:token1 pas untuk range dua sisi pada harga sekarang."""
    spn = sqrtp_x96 / Q96
    sa = math.sqrt(1.0001 ** tick_lower)
    sb = math.sqrt(1.0001 ** tick_upper)
    spn = min(max(spn, sa), sb)
    p = spn * spn  # harga raw token1 per token0
    # nilai (dalam token1) per unit L: sisi token1 = (spn-sa); sisi token0 = (sb-spn)/(spn*sb) × p
    v1 = spn - sa
    v0 = (sb - spn) / (spn * sb) * p
    if v0 + v1 <= 0:
        raise RuntimeError("Range degenerate.")
    frac_other = (v0 / (v0 + v1)) if quote_is_token1 else (v1 / (v0 + v1))
    swap_wei = int(budget_quote_wei * frac_other)
    return budget_quote_wei - swap_wei, swap_wei


def assert_pool_orientation(w3: Web3, pool_info: dict, chain_id: int | None = None) -> None:
    """Pagar keamanan dana: verifikasi identitas pool di dict cocok dengan
    kebenaran on-chain SEBELUM membangun transaksi.

    Dict pool bisa berasal dari indexer luar (API Uniswap) yang cepat tapi tidak boleh
    dipercaya untuk membangun tx. Kalau orientasi quote/meme, fee, atau PoolKey
    salah, mint bisa menaruh dana di sisi/pool yang keliru.

    v3: baca token0()/token1()/fee() langsung dari kontrak pool, batalkan kalau
        tidak cocok.
    v4: poolId ADALAH hash dari PoolKey, jadi recompute v4_pool_id(key) dan
        pastikan == pool_id (mustahil memalsukan PoolKey untuk poolId tertentu),
        plus tolak pool ber-hooks (bot cuma dukung vanilla)."""
    ver = pool_info.get("ver", 3)
    if ver == 4:
        key = pool_info.get("key")
        pid = pool_info.get("pool_id")
        if not key or pid is None:
            raise RuntimeError("Data pool v4 tidak lengkap — cari ulang tokennya.")
        pid_b = pid if isinstance(pid, (bytes, bytearray)) else bytes.fromhex(str(pid).removeprefix("0x"))
        if Web3.to_checksum_address(key[4]) != Web3.to_checksum_address(V4_NATIVE):
            raise RuntimeError(
                "Pool v4 memakai hooks — tidak didukung. Transaksi dibatalkan demi "
                "keamanan dana.")
        if v4_pool_id(tuple(key)) != pid_b:
            raise RuntimeError(
                "Verifikasi pool v4 gagal: PoolKey tidak menghasilkan poolId yang "
                "sama. Transaksi dibatalkan demi keamanan dana — cari ulang tokennya.")
        return
    if ver != 3:
        return
    pc = w3.eth.contract(address=Web3.to_checksum_address(pool_info["pool"]), abi=POOL_ABI)
    on0 = pc.functions.token0().call()
    on1 = pc.functions.token1().call()
    on_fee = pc.functions.fee().call()
    if (on0.lower() != str(pool_info["token0"]).lower()
            or on1.lower() != str(pool_info["token1"]).lower()
            or int(on_fee) != int(pool_info["fee"])):
        raise RuntimeError(
            "Verifikasi pool gagal: token0/token1/fee tidak cocok dengan kontrak "
            "on-chain. Transaksi dibatalkan demi keamanan dana — cari ulang tokennya.")
    # Satu chain bisa punya beberapa DEX dengan pool token+fee yang SAMA. Cek di atas
    # lolos untuk keduanya, jadi tanpa cek ini mint pool Uniswap bisa dieksekusi
    # memakai NPM PancakeSwap dan dana mendarat di pool yang salah — bukan gagal,
    # tapi salah tempat.
    if chain_id is not None:
        owner = which_dex_v3(w3, chain_id, pool_info["pool"], on0, on1, int(on_fee))
        want = pool_info.get("dex") or dex_name(chain_id)
        if owner != want:
            raise RuntimeError(
                f"Verifikasi pool gagal: pool ini milik {owner or 'DEX tak dikenal'}, "
                f"bukan {want}. Transaksi dibatalkan demi keamanan dana.")


def mint_position(chain_id: int, pk: str, pool_info: dict, budget: float,
                  strategy: dict, slippage_pct: float) -> dict:
    """Mint LP sesuai strategi.
    strategy = {mode: lower|upper|wide|stable, low_pct, up_pct}
    budget dalam satuan QUOTE untuk lower/wide/stable, satuan MEME untuk upper."""
    w3 = get_w3(chain_id)
    # pool_cfg, BUKAN CHAINS[chain_id]: di chain ber-DEX ganda, NPM harus milik DEX
    # pool ini. Salah NPM = dana mendarat di pool DEX lain dengan token+fee sama.
    cfg = pool_cfg(chain_id, pool_info)
    account = w3.eth.account.from_key(pk)
    npm_addr = Web3.to_checksum_address(cfg["npm"])
    mode = strategy["mode"]

    quote = Web3.to_checksum_address(pool_info["quote_addr"])
    qdec = pool_info["quote_decimals"]
    q_is_t1 = pool_info["quote_is_token1"]
    meme = Web3.to_checksum_address(pool_info["token0"] if q_is_t1 else pool_info["token1"])
    mdec = token_info(w3, meme)["decimals"]

    pool = w3.eth.contract(address=Web3.to_checksum_address(pool_info["pool"]), abi=POOL_ABI)
    # dict pool bisa datang dari indexer (API Uniswap) → verifikasi orientasi DAN
    # kepemilikan DEX on-chain dulu; kalau tak cocok, batal sebelum dana bergerak.
    assert_pool_orientation(w3, pool_info, chain_id)
    steps = []
    slip = (100 - slippage_pct) / 100

    # Range bebas: mode ditentukan oleh letak range terhadap harga, bukan pilihan user.
    if strategy.get("ticks"):
        mode = effective_mode(int(strategy["ticks"][0]), int(strategy["ticks"][1]),
                              pool.functions.slot0().call()[1], q_is_t1)

    # ---- Fase 1: persiapan (wrap / swap / approve) — harga boleh gerak selama ini ----
    keep_wei = meme_got = dep_wei = 0
    if mode == "upper":
        dep_wei = int(Decimal(str(budget)) * Decimal(10) ** mdec)
        if dep_wei <= 0:
            raise RuntimeError("Amount 0.")
        bal = erc20(w3, meme).functions.balanceOf(account.address).call()
        if bal < dep_wei:
            if dep_wei - bal <= dep_wei // 10000 + 1:
                dep_wei = bal  # selisih pembulatan float dari amount 100% — pakai saldo penuh
            else:
                raise RuntimeError(f"Saldo meme kurang: punya {bal / 10 ** mdec:.6g}, butuh {budget}")
        steps += ensure_approval(w3, pk, meme, npm_addr, dep_wei)
        deposited_usd = budget * _meme_usd(w3, chain_id, pool_info)
    elif mode in ("wide", "stable"):
        budget_wei = int(Decimal(str(budget)) * Decimal(10) ** qdec)
        if budget_wei <= 0:
            raise RuntimeError("Amount 0.")
        steps += ensure_quote_balance(w3, chain_id, pk, quote, budget_wei, slippage_pct)
        budget_wei = min(budget_wei, erc20(w3, quote).functions.balanceOf(account.address).call())
        slot0 = pool.functions.slot0().call()
        t_lo, t_hi, _ = _range_of(strategy, slot0[1], pool_info["fee"], q_is_t1,
                                  pool_info.get("tick_spacing"))
        keep_wei, swap_wei = plan_two_sided(slot0[0], t_lo, t_hi, budget_wei, q_is_t1)
        # meme yang sudah ada di wallet dihitung duluan — swap cuma nutup kekurangan
        raw = (slot0[0] / Q96) ** 2  # token1 per token0 (rasio wei)
        meme_bal = erc20(w3, meme).functions.balanceOf(account.address).call()
        if q_is_t1:
            meme_val_q = int(meme_bal * raw)
        else:
            meme_val_q = int(meme_bal / raw) if raw else 0
        keep_frac = keep_wei / budget_wei if budget_wei else 0
        quote_dep = min(int((budget_wei + meme_val_q) * keep_frac), budget_wei)
        swap_wei = max(0, budget_wei - quote_dep)
        swapped = False
        if swap_wei > budget_wei // 500:  # <0.2% budget = dust, skip
            h = swap_to_token(chain_id, pk, quote, meme, pool_info["fee"], swap_wei, slippage_pct)
            if h:
                steps.append(("swap", h))
                swapped = True
        keep_wei = quote_dep
        # deposit desired = SEMUA meme di wallet (kelebihan dikembalikan NPM otomatis);
        # polling karena replika RPC bisa telat lihat hasil swap
        meme_got = poll_balance(w3, meme, account.address, meme_bal + 1) if swapped \
            else erc20(w3, meme).functions.balanceOf(account.address).call()
        steps += ensure_approval(w3, pk, quote, npm_addr, keep_wei)
        steps += ensure_approval(w3, pk, meme, npm_addr, meme_got)
        implied_total_q = int(quote_dep / keep_frac) if keep_frac > 0 else budget_wei + meme_val_q
        deposited_usd = min(budget_wei + meme_val_q, implied_total_q) / 10 ** qdec * pool_info["quote_usd"]
    else:  # lower — deposit quote single-sided
        dep_wei = int(Decimal(str(budget)) * Decimal(10) ** qdec)
        if dep_wei <= 0:
            raise RuntimeError("Amount 0.")
        steps += ensure_quote_balance(w3, chain_id, pk, quote, dep_wei, slippage_pct)
        dep_wei = min(dep_wei, erc20(w3, quote).functions.balanceOf(account.address).call())
        steps += ensure_approval(w3, pk, quote, npm_addr, dep_wei)
        deposited_usd = dep_wei / 10 ** qdec * pool_info["quote_usd"]

    # ---- Fase 2: baca harga TERAKHIR baru mint; retry kalau harga nyebrang range ----
    npm = w3.eth.contract(address=npm_addr, abi=NPM_ABI)
    receipt = None
    last_err = None
    for attempt in range(3):
        slot0 = pool.functions.slot0().call()
        cur_tick = slot0[1]
        tick_lower, tick_upper, now_mode = _range_of(
            strategy, cur_tick, pool_info["fee"], q_is_t1, pool_info.get("tick_spacing"))
        if now_mode != mode:
            # harga menyeberang batas range setelah dana disiapkan → sisi token yang
            # dibutuhkan berubah. Lebih baik berhenti daripada mint dengan sisi salah;
            # dana tetap utuh di wallet.
            raise RuntimeError(
                f"Harga bergerak melewati batas range saat transaksi disiapkan "
                f"(butuh sisi '{now_mode}', dana sudah disiapkan untuk '{mode}'). "
                f"Dana aman di wallet — atur ulang range lalu coba lagi.")

        if mode == "upper":
            if not q_is_t1:  # meme = token1
                a0d, a1d, a0m, a1m = 0, dep_wei, 0, int(dep_wei * slip)
            else:
                a0d, a1d, a0m, a1m = dep_wei, 0, int(dep_wei * slip), 0
        elif mode in ("wide", "stable"):
            a0d, a1d = (meme_got, keep_wei) if q_is_t1 else (keep_wei, meme_got)
            # min dihitung dari pemakaian riil (desired bisa >> terpakai kalau meme berlebih);
            # slippage utama sudah kena di swap, ini cuma pagar rasio
            liq = int(liquidity_for_amounts(slot0[0], tick_lower, tick_upper, a0d, a1d))
            u0, u1 = amounts_from_liquidity(liq, slot0[0], tick_lower, tick_upper)
            a0m, a1m = int(u0 * slip * 0.95), int(u1 * slip * 0.95)
        else:
            if q_is_t1:
                a0d, a1d, a0m, a1m = 0, dep_wei, 0, int(dep_wei * slip)
            else:
                a0d, a1d, a0m, a1m = dep_wei, 0, int(dep_wei * slip), 0

        params = (pool_info["token0"], pool_info["token1"], pool_info["fee"], tick_lower, tick_upper,
                  a0d, a1d, a0m, a1m, account.address, int(time.time()) + DEADLINE_SECS)
        try:
            h = send_tx(w3, pk, {"to": npm_addr, "data": calldata(npm.functions.mint(params))})
            receipt = wait_ok(w3, h, "mint")
            steps.append(("mint", h))
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2)
    if receipt is None:
        raise RuntimeError(
            "Mint revert 3× — harga lagi bergerak cepat melewati range (liquidity=0). "
            f"Coba lagi atau perlebar range. Detail: {last_err}")

    token_id = None
    for log in receipt.logs:
        if (log.address.lower() == npm_addr.lower() and len(log.topics) == 4
                and log.topics[0].hex().removeprefix("0x") == ERC721_TRANSFER_TOPIC.removeprefix("0x")):
            token_id = int(log.topics[3].hex(), 16)
            break
    if token_id is not None:
        # tampil di /list SEKARANG juga — jangan nunggu indexer Uniswap (telat menit-an)
        _active_add(chain_id, account.address, token_id)

    if mode in ("wide", "stable"):
        # USD dari jumlah AKTUAL yang masuk posisi (termasuk meme dari wallet)
        amts = _increase_amounts(receipt, npm_addr)
        if amts:
            a0, a1 = amts
            q_amt, m_amt = (a1, a0) if q_is_t1 else (a0, a1)
            raw = (slot0[0] / Q96) ** 2
            mprice_q = raw if q_is_t1 else (1 / raw if raw else 0)
            deposited_usd = (q_amt + m_amt * mprice_q) / 10 ** qdec * pool_info["quote_usd"]

    deposit_sym = (token_info(w3, meme)["symbol"] if mode == "upper" else pool_info["quote_sym"])
    return {
        "token_id": token_id, "steps": steps, "mode": mode,
        "tick_lower": tick_lower, "tick_upper": tick_upper, "cur_tick": cur_tick,
        "deposited": budget, "deposit_sym": deposit_sym,
        "deposited_usd": deposited_usd,
    }


def token_supply(w3: Web3, addr: str, _cache={}) -> float:
    """Total supply (satuan manusia), cache 10 menit. Untuk display market cap (FDV)."""
    key = addr.lower()
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < 600:
        return hit[0]
    c = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)
    supply = c.functions.totalSupply().call() / 10 ** token_info(w3, addr)["decimals"]
    _cache[key] = (supply, time.time())
    return supply


def _meme_usd(w3: Web3, chain_id: int, pool_info: dict) -> float:
    """Harga USD 1 meme via harga pool × harga USD quote.

    WAJIB sadar versi: pool_info["pool"] itu ALAMAT kontrak untuk v2/v3, tapi
    poolId 32-byte untuk v4. Dulu fungsi ini selalu memperlakukannya sebagai alamat,
    jadi mode Upper di pool v4 mana pun mati dengan "Unknown format '0x…'" (66 hex
    dipaksa jadi alamat 20 byte)."""
    ver = pool_info.get("ver", 3)
    q_is_t1 = pool_info["quote_is_token1"]
    meme = pool_info["token0"] if q_is_t1 else pool_info["token1"]
    qdec = pool_info["quote_decimals"]

    if ver == 2:
        rq, rm = _v2_pair_reserves(w3, pool_info["pool"], pool_info["quote_addr"])
        if rm <= 0:
            return 0.0
        mdec = _v4_currency_info(w3, chain_id, meme)["decimals"]
        return (rq / 10 ** qdec) / (rm / 10 ** mdec) * pool_info["quote_usd"]

    if ver == 4:
        pid = pool_info.get("pool_id") or bytes.fromhex(str(pool_info["pool"]).removeprefix("0x"))
        sp = v4_slot0(w3, chain_id, pid)[0]
    else:
        pool = w3.eth.contract(address=Web3.to_checksum_address(pool_info["pool"]), abi=POOL_ABI)
        sp = pool.functions.slot0().call()[0]
    if not sp:
        return 0.0
    raw = (sp / Q96) ** 2  # token1 per token0
    # currency v4 bisa ETH native (address(0)) — token_info gagal untuk itu
    mdec = _v4_currency_info(w3, chain_id, meme)["decimals"]
    meme_in_q = raw * 10 ** (mdec - qdec) if q_is_t1 else (1 / raw) * 10 ** (mdec - qdec)
    return meme_in_q * pool_info["quote_usd"]


# ---------- Listing posisi ----------
import json as _json
import os as _os
_ACTIVE_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".active_positions.json")
_ACTIVE_CACHE: dict = {}   # "chain:wallet" → [jumlah NFT, [tokenId aktif]]


def _active_load() -> dict:
    global _ACTIVE_CACHE
    if not _ACTIVE_CACHE and _os.path.exists(_ACTIVE_FILE):
        try:
            with open(_ACTIVE_FILE) as f:
                _ACTIVE_CACHE = _json.load(f)
        except Exception:
            _ACTIVE_CACHE = {}
    return _ACTIVE_CACHE


def _active_save(key: str, n: int, tids: list):
    _ACTIVE_CACHE[key] = [n, tids]
    try:
        tmp = _ACTIVE_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(_ACTIVE_CACHE, f)
        _os.replace(tmp, _ACTIVE_FILE)
    except Exception:
        pass


def _active_add(chain_id: int, addr: str, tid: int):
    """Daftarkan tokenId BARU ke cache aktif langsung saat mint sukses.
    Indexer Uniswap telat beberapa menit untuk posisi baru — tanpa ini posisi
    fresh-mint tidak muncul di /list sampai terindeks."""
    key = f"{chain_id}:{addr.lower()}"
    hit = _active_load().get(key)
    if not hit:
        return  # belum pernah list → refresh berikutnya scan sendiri
    n, tids = hit[0] + 1, list(hit[1])  # mint = balanceOf +1
    if tid not in tids:
        tids.append(tid)
    _active_save(key, n, tids)


def _is_active(p) -> bool:
    return not (p[7] == 0 and p[10] == 0 and p[11] == 0)   # liq==0 & owed0==0 & owed1==0


# API resmi Uniswap (sama persis dengan app.uniswap.org). Read-only: body cuma
# alamat wallet publik — tanpa key/tanda tangan/tx, tanpa API key.
_UNI_POS_API = "https://interface.gateway.uniswap.org/v2/data.v1.DataApiService/ListPositions"
_UNI_HDR = {
    "content-type": "application/json",
    "connect-protocol-version": "1",
    "x-request-source": "uniswap-web",
    "origin": "https://app.uniswap.org",
    "referer": "https://app.uniswap.org/",
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
}


def uniswap_v3_token_ids(chain_id: int, address: str, _cache={}, ttl: int = 20) -> list[int] | None:
    """Set tokenId posisi v3 aktif dari API resmi Uniswap (sama seperti
    app.uniswap.org). Dipakai HANYA sebagai daftar kandidat yang lengkap — detail
    tiap posisi (nilai, fee, tick sekarang, in/out-range) tetap dibaca on-chain
    lewat RPC-mu, jadi otoritatif dan segar.

    Kenapa perlu: wallet lama bisa punya ratusan NFT NPM (tutup posisi tidak
    mem-burn NFT), dan posisi yang di-mint langsung di app.uniswap.org tidak ada di
    cache bot. Enumerasi indeks yang cuma memindai N NFT terakhir bisa MELEWATKAN
    posisi aktif yang indeksnya lama — API Uniswap tidak. Read-only & aman (cuma
    alamat publik). None kalau gagal → caller fallback ke scan indeks.

    Di chain non-Uniswap (BSC/PancakeSwap) selalu None: indexer ini tidak mengenal
    NPM PancakeSwap, jadi daftar posisi murni dari enumerasi NFT on-chain."""
    if not uni_api_dex(chain_id):
        return None
    key = (chain_id, address.lower())
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    body = {"address": Web3.to_checksum_address(address), "chainIds": [chain_id],
            "protocolVersions": ["PROTOCOL_VERSION_V3"],
            "positionStatuses": ["POSITION_STATUS_IN_RANGE", "POSITION_STATUS_OUT_OF_RANGE"],
            "pageSize": 100, "includeHidden": True}
    try:
        r = _cf_post(_UNI_POS_API, headers=_UNI_HDR, json=body, timeout=10)
        raw = r.json().get("positions")
        if not isinstance(raw, list):
            return None
        ids = []
        for p in raw:
            d = p.get("v3Position")
            if d and str(d.get("tokenId", "")).isdigit():
                ids.append(int(d["tokenId"]))
        _cache[key] = (ids, time.time())
        return ids
    except Exception:
        return None


def list_positions(chain_id: int, pk: str, max_positions: int = 40,
                   full: bool = False, dex: str | None = None) -> list[dict]:
    """Posisi v3 aktif. Daftar tokenId kandidat diambil dari API Uniswap (LENGKAP —
    termasuk posisi lama & yang di-mint di luar bot), lalu detail tiap posisi dibaca
    on-chain (otoritatif & segar). Kalau API Uniswap mati, fallback ke enumerasi
    indeks NPM: set tokenId aktif disimpan; refresh cuma cek yang aktif + NFT baru
    (balanceOf naik). Scan indeks penuh saat pertama / jumlah turun / full=True.

    Catatan: scan indeks hanya memindai max_positions NFT TERAKHIR, jadi bisa
    melewatkan posisi aktif ber-indeks lama pada wallet dengan ratusan NFT — sebab
    itu jalur Uniswap diutamakan."""
    w3 = get_w3(chain_id)
    dex = dex or dex_name(chain_id)
    cfg = dex_cfg(chain_id, dex)
    account = w3.eth.account.from_key(pk)
    npm = w3.eth.contract(address=Web3.to_checksum_address(cfg["npm"]), abi=NPM_ABI)
    factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)

    # cache tokenId aktif dipisah per DEX — NPM berbeda punya penomoran sendiri
    ck = f"{chain_id}:{dex_slug(dex)}:{account.address.lower()}"
    hit = _active_load().get(ck)
    # indexer Uniswap cuma berlaku untuk NPM Uniswap
    uni_ids = uniswap_v3_token_ids(chain_id, account.address) if dex == uni_api_dex(chain_id) else None

    with ThreadPoolExecutor(max_workers=8) as ex:
        n = None
        if uni_ids is not None:
            # Daftar kandidat lengkap dari Uniswap, di-union dengan tokenId aktif yang
            # tercatat (menangkap posisi liq=0 tapi fee belum diklaim, yang oleh
            # Uniswap dianggap "closed"). _is_active on-chain menyaring yang benar mati.
            cached = [int(t) for t in hit[1]] if hit else []
            tids = list(dict.fromkeys([*uni_ids, *cached]))
            # Indexer Uniswap bisa TELAT BERJAM-JAM untuk mint baru (terbukti di
            # Robinhood chain) → SELALU enumerasi NFT terbaru on-chain dan union.
            # Stateless: tidak bergantung snapshot (snapshot bisa terlanjur menyerap
            # balanceOf tanpa sempat enumerasi NFT-nya). 10 terbaru cukup untuk
            # mint beruntun; melebar (maks 20) kalau snapshot bilang lebih banyak.
            n = npm.functions.balanceOf(account.address).call()
            lookback = 10
            if hit and n > hit[0]:
                lookback = min(max(lookback, n - hit[0]), 20)
            new_idxs = list(range(max(0, n - lookback), n))
            new_tids = ex.map(
                lambda i: npm.functions.tokenOfOwnerByIndex(account.address, i).call(), new_idxs)
            tids = list(dict.fromkeys([*tids, *map(int, new_tids)]))
        else:
            n = npm.functions.balanceOf(account.address).call()
            if hit and not full and n >= hit[0]:
                # Jalur cepat cadangan: cek ulang tokenId aktif + enumerasi HANYA NFT
                # baru (indeks hit[0]..n-1). Tutup posisi tidak mem-burn NFT → balanceOf
                # cuma naik saat mint, jadi "n turun" memicu scan penuh.
                new_idxs = list(range(n - 1, hit[0] - 1, -1))
                new_tids = list(ex.map(
                    lambda i: npm.functions.tokenOfOwnerByIndex(account.address, i).call(), new_idxs))
                tids = [int(t) for t in hit[1]] + new_tids
            else:
                idxs = list(range(n - 1, max(-1, n - 1 - max_positions), -1))
                tids = list(ex.map(
                    lambda i: npm.functions.tokenOfOwnerByIndex(account.address, i).call(), idxs))

        raws = list(ex.map(lambda t: npm.functions.positions(t).call(), tids))
        active = [(tid, p) for tid, p in zip(tids, raws) if _is_active(p)]
        try:
            if n is None:
                n = npm.functions.balanceOf(account.address).call()
            _active_save(ck, n, [tid for tid, _ in active])
        except Exception:
            pass
        results = list(ex.map(lambda tp: _position_detail(w3, chain_id, npm, factory, account, *tp), active))
    return [r for r in results if r]


def empty_position_ids(chain_id: int, pk: str, dex: str | None = None) -> list[int]:
    """tokenId NFT v3 yang benar-benar kosong: liquidity 0 DAN tokensOwed 0.

    Close tidak mem-burn NFT, jadi sisanya menumpuk dan setiap refresh daftar posisi
    membayar satu `positions()` per NFT — terukur 127 NFT untuk 1 posisi hidup."""
    w3 = get_w3(chain_id)
    account = w3.eth.account.from_key(pk)
    npm = w3.eth.contract(address=Web3.to_checksum_address(dex_cfg(chain_id, dex)["npm"]), abi=NPM_ABI)
    n = npm.functions.balanceOf(account.address).call()
    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        tids = list(ex.map(
            lambda i: npm.functions.tokenOfOwnerByIndex(account.address, i).call(), range(n)))
        raws = list(ex.map(lambda t: npm.functions.positions(t).call(), tids))
    for tid, p in zip(tids, raws):
        if p[7] == 0 and p[10] == 0 and p[11] == 0:
            out.append(int(tid))
    return out


def burn_empty(chain_id: int, pk: str, dex: str | None = None,
               batch: int = 25, max_batches: int = 8) -> dict:
    """Burn NFT posisi kosong lewat `multicall`, batch per batch.

    AMAN: `burn` di NPM me-require liquidity == 0 DAN tokensOwed0/1 == 0, jadi
    posisi yang masih berisi (termasuk yang sudah decrease tapi belum collect)
    ditolak kontraknya sendiri. Daftarnya tetap disaring ulang di sini supaya satu
    NFT berisi tidak membatalkan seluruh batch.

    Dibatasi `max_batches` supaya satu perintah tidak berubah jadi puluhan menit tx.
    """
    w3 = get_w3(chain_id)
    npm_addr = Web3.to_checksum_address(dex_cfg(chain_id, dex)["npm"])
    npm = w3.eth.contract(address=npm_addr, abi=NPM_ABI)
    ids = empty_position_ids(chain_id, pk, dex)
    steps, burned = [], 0
    for bi in range(min(max_batches, (len(ids) + batch - 1) // batch)):
        chunk = ids[bi * batch:(bi + 1) * batch]
        if not chunk:
            break
        calls = [npm.encode_abi("burn", args=[t]) for t in chunk]
        tx = {"to": npm_addr, "data": calldata(npm.functions.multicall(calls))}
        _step(f"🔥 burn {len(chunk)} NFT kosong (batch {bi + 1})")
        _preflight(w3, w3.eth.account.from_key(pk).address, tx)
        h = send_tx(w3, pk, tx)
        wait_ok(w3, h, f"burn {len(chunk)} NFT")
        steps.append(("burn", h))
        burned += len(chunk)
    return {"steps": steps, "burned": burned, "sisa": max(0, len(ids) - burned), "total": len(ids)}


def pool_addr_of(factory, t0: str, t1: str, fee: int, _cache={}) -> str:
    """factory.getPool — pemetaan immutable, aman di-cache selamanya.
    Menghemat 1 panggilan RPC per posisi tiap kali daftar posisi disegarkan."""
    key = (t0.lower(), t1.lower(), fee)
    if key not in _cache:
        _cache[key] = factory.functions.getPool(t0, t1, fee).call()
    return _cache[key]


def quote_backing_usd(w3: Web3, chain_id: int, token: str, _cache={}) -> float:
    """Likuiditas USD token ini terhadap quote TETAP, dibaca on-chain (pool v3 + pair
    v2). Sengaja TIDAK memakai fallback harga dexscreener seperti token_usd_price:
    di sini pertanyaannya bukan 'berapa harganya' tapi 'apakah sisi ini benar-benar
    bisa ditukar & dinilai on-chain' — itu yang memenuhi syarat jadi quote."""
    key = (chain_id, token.lower())
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < 300:
        return hit[0]
    cfg = CHAINS[chain_id]
    token = Web3.to_checksum_address(token)
    best = 0.0
    try:
        f3 = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)
        v2f = (w3.eth.contract(address=Web3.to_checksum_address(cfg["v2_factory"]), abi=V2_FACTORY_ABI)
               if cfg.get("v2_factory") else None)
        for qsym, qaddr in cfg["quotes"].items():
            q = Web3.to_checksum_address(qaddr)
            if q == token:
                best = float("inf")
                break
            qusd = quote_usd_price(w3, chain_id, qsym)
            if qusd <= 0:
                continue
            qdec = token_info(w3, q)["decimals"]
            t0s, t1s = sort_tokens(token, q)
            addrs = []
            for fee in fee_tiers(chain_id):
                a = f3.functions.getPool(t0s, t1s, fee).call()
                if int(a, 16):
                    addrs.append(a)
            if v2f:
                a = v2f.functions.getPair(token, q).call()
                if int(a, 16):
                    addrs.append(a)
            for a in addrs:
                try:
                    bal = erc20(w3, q).functions.balanceOf(a).call() / 10 ** qdec * qusd
                    best = max(best, bal)
                except Exception:
                    continue
    except Exception:
        pass
    _cache[key] = (best, time.time())
    return best


def resolve_quote_side(w3: Web3, chain_id: int, t0: str, t1: str,
                       sym0: str = "", sym1: str = "") -> tuple[str | None, bool]:
    """(quote_sym, quote_is_token1) untuk sepasang token posisi.

    Quote tetap didahulukan. Kalau tidak ada (pool ber-quote auto-deteksi, mis.
    RTX/NVDAB), sisi quote = sisi yang harganya bisa dibaca on-chain — itu satu-
    satunya sisi yang bisa dipakai menilai posisi dalam USD. Tanpa ini posisi
    semacam itu tampil bernilai 0 (v3) atau hilang sama sekali (v2)."""
    quotes_lc = {a.lower(): s for s, a in CHAINS[chain_id]["quotes"].items()}
    if t1.lower() in quotes_lc:
        return quotes_lc[t1.lower()], True
    if t0.lower() in quotes_lc:
        return quotes_lc[t0.lower()], False
    # Kedua sisi bisa saja "punya harga" (token_usd_price jatuh ke dexscreener untuk
    # token apa pun), jadi pemilihannya pakai sokongan likuiditas on-chain: sisi yang
    # benar-benar bisa ditukar ke quote tetap. Untuk RTX/NVDAB → NVDAB.
    b1 = quote_backing_usd(w3, chain_id, t1)
    b0 = quote_backing_usd(w3, chain_id, t0)
    if max(b0, b1) <= 0:
        return None, True
    is_t1 = b1 >= b0
    addr = t1 if is_t1 else t0
    sym = (sym1 if is_t1 else sym0) or token_info(w3, addr)["symbol"]
    return register_quote(chain_id, sym, addr), is_t1


def _position_detail(w3: Web3, chain_id: int, npm, factory, account, tid: int, p) -> dict | None:
    cfg = CHAINS[chain_id]
    try:
        (_, _, t0, t1, fee, tick_lo, tick_hi, liq, _, _, owed0, owed1) = p

        i0, i1 = token_info(w3, t0), token_info(w3, t1)
        pool_addr = pool_addr_of(factory, t0, t1, fee)
        slot0 = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI).functions.slot0().call()
        sqrtp, cur_tick = slot0[0], slot0[1]

        a0_raw, a1_raw = amounts_from_liquidity(liq, sqrtp, tick_lo, tick_hi)
        # fee unclaimed: static call collect (NPM nge-poke pool dulu → akurat)
        try:
            f0, f1 = npm.functions.collect((tid, account.address, MAX_UINT128, MAX_UINT128)).call(
                {"from": account.address})
        except Exception:
            f0, f1 = owed0, owed1

        # tentukan sisi quote untuk valuasi USD
        qsym, q_is_t1 = resolve_quote_side(w3, chain_id, t0, t1, i0["symbol"], i1["symbol"])

        raw_price = (sqrtp / Q96) ** 2  # token1 per token0, raw
        usd = unclaimed_usd = 0.0
        usd0 = usd1 = fees_usd0 = fees_usd1 = 0.0
        mc_lower = mc_upper = mc_now = None
        if qsym:
            qusd = quote_usd_price(w3, chain_id, qsym)
            if q_is_t1:
                qdec, mdec = i1["decimals"], i0["decimals"]
                meme_addr = t0
                meme_in_q = raw_price * 10 ** (mdec - qdec)  # quote per 1 meme
                usd0 = (a0_raw / 10 ** mdec) * meme_in_q * qusd
                usd1 = a1_raw / 10 ** qdec * qusd
                fees_usd0 = (f0 / 10 ** mdec) * meme_in_q * qusd
                fees_usd1 = f1 / 10 ** qdec * qusd
            else:
                qdec, mdec = i0["decimals"], i1["decimals"]
                meme_addr = t1
                meme_in_q = (1 / raw_price) * 10 ** (mdec - qdec) if raw_price else 0
                usd0 = a0_raw / 10 ** qdec * qusd
                usd1 = (a1_raw / 10 ** mdec) * meme_in_q * qusd
                fees_usd0 = f0 / 10 ** qdec * qusd
                fees_usd1 = (f1 / 10 ** mdec) * meme_in_q * qusd
            usd = usd0 + usd1
            unclaimed_usd = fees_usd0 + fees_usd1

            # market cap (FDV) di batas range + sekarang — display gaya GMGN
            def meme_q_at(t):
                r = tick_to_price(t)
                return (r if q_is_t1 else (1 / r if r else 0)) * 10 ** (mdec - qdec)
            try:
                supply = token_supply(w3, meme_addr)
                mcs = sorted([meme_q_at(tick_lo) * qusd * supply, meme_q_at(tick_hi) * qusd * supply])
                mc_lower, mc_upper = mcs
                mc_now = meme_in_q * qusd * supply
            except Exception:
                pass

        return {
            "token_id": tid, "token0": t0, "token1": t1, "sym0": i0["symbol"], "sym1": i1["symbol"],
            "dec0": i0["decimals"], "dec1": i1["decimals"], "fee": fee, "pool": pool_addr,
            "tick_lower": tick_lo, "tick_upper": tick_hi, "cur_tick": cur_tick,
            "liquidity": liq, "amount0": a0_raw / 10 ** i0["decimals"], "amount1": a1_raw / 10 ** i1["decimals"],
            "fees0": f0 / 10 ** i0["decimals"], "fees1": f1 / 10 ** i1["decimals"],
            "in_range": tick_lo <= cur_tick < tick_hi,
            # liq==0 tapi tokensOwed>0 = decreaseLiquidity SUDAH jalan, collect BELUM.
            # Uniswap v3 memindahkan POKOK ke tokensOwed — field yang sama dengan fee —
            # jadi angka "unclaimed" di keadaan ini pokok + fee, bukan fee saja. Tanpa
            # penanda ini kartu menulis "Nilai $0.00 / Fee unclaimed $472" dan user
            # mengira dananya hilang (kejadian nyata di #757291).
            "pending_claim": liq == 0 and (f0 > 0 or f1 > 0),
            "value_usd": usd, "unclaimed_usd": unclaimed_usd,
            "usd0": usd0, "usd1": usd1, "fees_usd0": fees_usd0, "fees_usd1": fees_usd1,
            "quote_sym": qsym, "quote_is_token1": q_is_t1,
            "mc_lower": mc_lower, "mc_upper": mc_upper, "mc_now": mc_now,
        }
    except Exception:
        return None


# ---------- Saldo token wallet (via Alchemy) ----------
def wallet_tokens(chain_id: int, address: str) -> list[dict]:
    """Semua ERC20 non-nol di wallet. Butuh RPC Alchemy; selain itu return []."""
    w3 = get_w3(chain_id)
    if "alchemy" not in str(w3.provider.endpoint_uri):
        return []
    try:
        res = w3.provider.make_request("alchemy_getTokenBalances",
                                       [Web3.to_checksum_address(address), "erc20"])
        items = res.get("result", {}).get("tokenBalances", [])
    except Exception:
        return []
    out = []
    for tb in items:
        bal = int(tb.get("tokenBalance") or "0x0", 16)
        if bal == 0:
            continue
        try:
            info = token_info(w3, tb["contractAddress"])
        except Exception:
            continue
        out.append({"address": info["address"], "symbol": info["symbol"],
                    "decimals": info["decimals"], "raw": bal})
    return out


def token_usd_price(w3: Web3, chain_id: int, token_addr: str, _cache={}) -> float:
    """Harga USD 1 token via pool v3 terbaik vs quote. 0 kalau tidak ada pool."""
    key = (chain_id, token_addr.lower())
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < 120:
        return hit[0]
    cfg = CHAINS[chain_id]
    token = Web3.to_checksum_address(token_addr)
    factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)
    mdec = token_info(w3, token)["decimals"]
    price = 0.0
    # pilih pool dengan likuiditas sisi-quote terbesar — pool dust bisa nyimpan
    # harga ngaco ratusan kali lipat (mis. pool kosong fee 0.3%)
    best_liq_usd = 0.0
    for qsym, qaddr in cfg["quotes"].items():
        q = Web3.to_checksum_address(qaddr)
        if q == token:
            price = quote_usd_price(w3, chain_id, qsym)
            best_liq_usd = float("inf")
            break
        qd = token_info(w3, q)["decimals"]
        qusd = quote_usd_price(w3, chain_id, qsym)
        t0, t1 = sort_tokens(token, q)
        for fee in fee_tiers(chain_id):
            pool = factory.functions.getPool(t0, t1, fee).call()
            if int(pool, 16) == 0:
                continue
            try:
                liq_usd = erc20(w3, q).functions.balanceOf(pool).call() / 10 ** qd * qusd
                if liq_usd < 10 or liq_usd <= best_liq_usd:
                    continue  # dust / kalah likuid dari kandidat sebelumnya
                raw = _pool_price_t1_per_t0(w3, pool)
            except Exception:
                continue
            in_q = raw * 10 ** (mdec - qd) if token == t0 else ((1 / raw) * 10 ** (mdec - qd) if raw else 0)
            price = in_q * qusd
            best_liq_usd = liq_usd
    # fallback 1: pair Uniswap V2 — filter & kompetisi likuiditas sama seperti v3
    # (pair dust tanpa filter pernah bikin harga meleset 10^12×, mis. NVDA $15 miliar)
    if cfg.get("v2_factory"):
        v2f = w3.eth.contract(address=Web3.to_checksum_address(cfg["v2_factory"]), abi=V2_FACTORY_ABI)
        for qsym, qaddr in cfg["quotes"].items():
            q = Web3.to_checksum_address(qaddr)
            if q == token:
                continue
            try:
                pair = v2f.functions.getPair(token, q).call()
                if int(pair, 16) == 0:
                    continue
                pc = w3.eth.contract(address=Web3.to_checksum_address(pair), abi=V2_PAIR_ABI)
                r0, r1, _ = pc.functions.getReserves().call()
                rt, rq = (r0, r1) if pc.functions.token0().call().lower() == token.lower() else (r1, r0)
                if rt == 0:
                    continue
                qd = token_info(w3, q)["decimals"]
                qusd = quote_usd_price(w3, chain_id, qsym)
                liq_usd = rq / 10 ** qd * qusd
                if liq_usd < 10 or liq_usd <= best_liq_usd:
                    continue
                price = (rq / rt) * 10 ** (mdec - qd) * qusd
                best_liq_usd = liq_usd
            except Exception:
                continue
    # fallback 2: API dexscreener (menutup v4, quote non-standar, dll).
    # Juga dipakai sebagai pembanding kalau backing on-chain lemah (<$500) —
    # likuiditas terbesar menang.
    if not price or best_liq_usd < 500:
        try:
            r = _cf_get(f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=8)
            pairs = [p for p in (r.json().get("pairs") or [])
                     if p.get("chainId") == cfg.get("dexscreener")]
            pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
            if pairs:
                dex_liq = float((pairs[0].get("liquidity") or {}).get("usd") or 0)
                dex_price = float(pairs[0].get("priceUsd") or 0)
                if dex_price > 0 and dex_liq > best_liq_usd:
                    price = dex_price
        except Exception:
            pass
    _cache[key] = (price, time.time())
    return price


# ---------- Riwayat harga (untuk chart) ----------
def price_history(chain_id: int, pool_addr: str, span_secs: int, points: int = 72) -> list[tuple[int, int]]:
    """Sample tick pool di blok-blok lampau (butuh RPC archive, mis. Alchemy).
    Return [(timestamp, tick)] urut waktu naik."""
    w3 = get_w3(chain_id)
    latest = w3.eth.get_block("latest")
    lb, lt = latest["number"], latest["timestamp"]
    probe_back = min(lb - 1, 50_000)
    old = w3.eth.get_block(lb - probe_back)
    per_block = max((lt - old["timestamp"]) / probe_back, 0.01)
    span_blocks = min(int(span_secs / per_block), lb - 1)
    if span_blocks < points:
        span_blocks = min(points, lb - 1)

    pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
    blocks = [lb - span_blocks + int(i * span_blocks / (points - 1)) for i in range(points)]

    def tick_at(b):
        try:
            return pool.functions.slot0().call(block_identifier=b)[1]
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=5) as ex:
        ticks = list(ex.map(tick_at, blocks))
    out = []
    for b, t in zip(blocks, ticks):
        if t is not None:
            out.append((int(lt - (lb - b) * per_block), t))
    if len(out) < 5:
        raise RuntimeError("Riwayat harga tidak tersedia di RPC ini (butuh archive node / Alchemy).")
    return out


# ---------- Add / Reduce posisi ----------
def increase_position(chain_id: int, pk: str, token_id: int, budget_quote: float,
                      slippage_pct: float, dex: str | None = None) -> dict:
    """Tambah dana ke posisi yang ada. Budget dalam satuan quote; komposisi
    (quote/meme) dihitung otomatis dari posisi range vs harga sekarang —
    meme existing di wallet dipakai duluan, swap cuma nutup kekurangan."""
    w3 = get_w3(chain_id)
    cfg = dex_cfg(chain_id, dex)
    account = w3.eth.account.from_key(pk)
    npm_addr = Web3.to_checksum_address(cfg["npm"])
    npm = w3.eth.contract(address=npm_addr, abi=NPM_ABI)

    (_, _, t0, t1, fee, tick_lo, tick_hi, liq, _, _, _, _) = npm.functions.positions(token_id).call()
    quotes_lc = {a.lower(): s for s, a in cfg["quotes"].items()}
    if t1.lower() in quotes_lc:
        quote, meme, q_is_t1, qsym = t1, t0, True, quotes_lc[t1.lower()]
    elif t0.lower() in quotes_lc:
        quote, meme, q_is_t1, qsym = t0, t1, False, quotes_lc[t0.lower()]
    else:
        raise RuntimeError("Pair tanpa quote yang dikenal bot.")
    qdec = token_info(w3, quote)["decimals"]
    budget_wei = int(Decimal(str(budget_quote)) * Decimal(10) ** qdec)
    if budget_wei <= 0:
        raise RuntimeError("Amount 0.")

    factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)
    pool_addr = factory.functions.getPool(t0, t1, fee).call()
    pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)

    steps = []
    slip = (100 - slippage_pct) / 100
    steps += ensure_quote_balance(w3, chain_id, pk, quote, budget_wei, slippage_pct)
    budget_wei = min(budget_wei, erc20(w3, quote).functions.balanceOf(account.address).call())

    slot0 = pool.functions.slot0().call()
    # rasio quote:meme mengikuti posisi range vs harga (plan_two_sided nge-clamp
    # harga ke dalam range → out-of-range otomatis jadi 100% satu sisi)
    keep_wei, swap_wei = plan_two_sided(slot0[0], tick_lo, tick_hi, budget_wei, q_is_t1)
    raw = (slot0[0] / Q96) ** 2
    meme_price_q = raw if q_is_t1 else (1 / raw if raw else 0)
    meme_bal = erc20(w3, meme).functions.balanceOf(account.address).call()
    meme_val_q = int(meme_bal * meme_price_q)
    keep_frac = keep_wei / budget_wei if budget_wei else 0
    quote_dep = min(int((budget_wei + meme_val_q) * keep_frac), budget_wei)
    swap_wei = max(0, budget_wei - quote_dep)
    swapped = False
    if swap_wei > budget_wei // 500:
        h = swap_to_token(chain_id, pk, quote, meme, fee, swap_wei, slippage_pct)
        if h:
            steps.append(("swap", h))
            swapped = True
    else:
        swap_wei = 0
    meme_have = poll_balance(w3, meme, account.address, meme_bal + 1) if swapped \
        else erc20(w3, meme).functions.balanceOf(account.address).call()
    if quote_dep > 0:
        steps += ensure_approval(w3, pk, quote, npm_addr, quote_dep)
    if meme_have > 0:
        steps += ensure_approval(w3, pk, meme, npm_addr, meme_have)

    receipt = None
    last_err = None
    for attempt in range(3):
        s0 = pool.functions.slot0().call()
        a0d, a1d = (meme_have, quote_dep) if q_is_t1 else (quote_dep, meme_have)
        lq = int(liquidity_for_amounts(s0[0], tick_lo, tick_hi, a0d, a1d))
        if lq <= 0:
            raise RuntimeError(
                "Liquidity terhitung 0 — posisi in-range butuh dua sisi tapi salah satu "
                "sisi kosong (saldo meme 0 dan swap ter-skip). Coba amount lebih besar.")
        u0, u1 = amounts_from_liquidity(lq, s0[0], tick_lo, tick_hi)
        a0m, a1m = int(u0 * slip * 0.95), int(u1 * slip * 0.95)
        params = (token_id, a0d, a1d, a0m, a1m, int(time.time()) + DEADLINE_SECS)
        try:
            h = send_tx(w3, pk, {"to": npm_addr,
                                 "data": calldata(npm.functions.increaseLiquidity(params))})
            receipt = wait_ok(w3, h, "increaseLiquidity")
            steps.append(("increase", h))
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2)
    if receipt is None:
        raise RuntimeError(f"Add gagal 3× (harga bergerak / saldo kurang). Detail: {last_err}")

    qusd = quote_usd_price(w3, chain_id, qsym)
    minfo = token_info(w3, meme)
    amts = _increase_amounts(receipt, npm_addr)
    if amts:
        # USD dari jumlah AKTUAL yang masuk (termasuk meme dari wallet), bukan estimasi budget
        a0, a1 = amts
        q_amt, m_amt = (a1, a0) if q_is_t1 else (a0, a1)
        added_usd = (q_amt + m_amt * meme_price_q) / 10 ** qdec * qusd
        quote_in, meme_in = q_amt / 10 ** qdec, m_amt / 10 ** minfo["decimals"]
    else:
        implied_total = int(quote_dep / keep_frac) if keep_frac > 0 else budget_wei + meme_val_q
        added_usd = min(budget_wei + meme_val_q, implied_total) / 10 ** qdec * qusd
        quote_in = meme_in = None
    return {"steps": steps, "added_usd": added_usd, "quote_sym": qsym,
            "quote_dep": quote_dep / 10 ** qdec,
            "quote_in": quote_in, "meme_in": meme_in, "meme_sym": minfo["symbol"]}


def decrease_position(chain_id: int, pk: str, token_id: int, pct: int,
                      dex: str | None = None) -> dict:
    """Kurangi posisi pct% (decrease + collect). Fee unclaimed ikut terambil.
    Token hasil pengurangan tetap di wallet (tanpa auto-swap)."""
    if not 1 <= pct <= 99:
        raise RuntimeError("Persen harus 1–99 (100% = pakai Close).")
    w3 = get_w3(chain_id)
    cfg = dex_cfg(chain_id, dex)
    account = w3.eth.account.from_key(pk)
    npm = w3.eth.contract(address=Web3.to_checksum_address(cfg["npm"]), abi=NPM_ABI)

    (_, _, t0, t1, fee, _, _, liq, _, _, _, _) = npm.functions.positions(token_id).call()
    part = liq * pct // 100
    if part == 0:
        raise RuntimeError("Liquidity 0 — posisi sudah kosong.")
    i0, i1 = token_info(w3, t0), token_info(w3, t1)

    steps = []
    params = (token_id, part, 0, 0, int(time.time()) + DEADLINE_SECS)
    h = send_tx(w3, pk, {"to": cfg["npm"], "data": calldata(npm.functions.decreaseLiquidity(params))})
    wait_ok(w3, h, "decreaseLiquidity")
    steps.append(("decrease", h))

    got0, got1 = npm.functions.collect((token_id, account.address, MAX_UINT128, MAX_UINT128)).call(
        {"from": account.address})
    h = send_tx(w3, pk, {"to": cfg["npm"],
                         "data": calldata(npm.functions.collect((token_id, account.address, MAX_UINT128, MAX_UINT128)))})
    wait_ok(w3, h, "collect")
    steps.append(("collect", h))

    return {"steps": steps, "got0": got0 / 10 ** i0["decimals"], "got1": got1 / 10 ** i1["decimals"],
            "sym0": i0["symbol"], "sym1": i1["symbol"]}


def collect_fees(chain_id: int, pk: str, token_id: int, dex: str | None = None) -> dict:
    """Klaim fee unclaimed saja — liquidity posisi tidak berubah.
    (NPM.collect nge-poke pool dulu kalau liquidity > 0, jadi fee ter-update.)"""
    w3 = get_w3(chain_id)
    cfg = dex_cfg(chain_id, dex)
    account = w3.eth.account.from_key(pk)
    npm = w3.eth.contract(address=Web3.to_checksum_address(cfg["npm"]), abi=NPM_ABI)

    (_, _, t0, t1, *_rest) = npm.functions.positions(token_id).call()
    i0, i1 = token_info(w3, t0), token_info(w3, t1)

    got0, got1 = npm.functions.collect((token_id, account.address, MAX_UINT128, MAX_UINT128)).call(
        {"from": account.address})
    if got0 == 0 and got1 == 0:
        raise RuntimeError("Tidak ada fee untuk diklaim.")
    h = send_tx(w3, pk, {"to": cfg["npm"],
                         "data": calldata(npm.functions.collect((token_id, account.address, MAX_UINT128, MAX_UINT128)))})
    wait_ok(w3, h, "collect")

    return {"steps": [("collect", h)],
            "got0": got0 / 10 ** i0["decimals"], "got1": got1 / 10 ** i1["decimals"],
            "sym0": i0["symbol"], "sym1": i1["symbol"]}


# ---------- Close + auto-swap ----------
def verify_router(w3: Web3, chain_id: int, dex: str | None = None, _cache={}) -> bool:
    """Cek router.factory() == factory DEX ini sebelum swap pertama."""
    key = (chain_id, dex)
    if key in _cache:
        return _cache[key]
    cfg = dex_cfg(chain_id, dex)
    try:
        r = w3.eth.contract(address=Web3.to_checksum_address(cfg["router"]), abi=ROUTER_ABI)
        ok = r.functions.factory().call().lower() == cfg["factory"].lower()
    except Exception:
        ok = False
    _cache[key] = ok
    return ok


def swap_to_token(chain_id: int, pk: str, token_in: str, token_out: str, fee: int,
                  amount_in_wei: int, slippage_pct: float,
                  dex: str | None = None) -> str | None:
    """Swap exactInputSingle via SwapRouter02. Return txhash, None kalau skip.
    Router HARUS milik DEX pool yang dipakai — di chain ber-DEX ganda, router
    PancakeSwap tidak bisa menyentuh pool Uniswap."""
    if amount_in_wei <= 0:
        return None
    w3 = get_w3(chain_id)
    account = w3.eth.account.from_key(pk)
    token_in = Web3.to_checksum_address(token_in)
    token_out = Web3.to_checksum_address(token_out)

    t0, t1 = sort_tokens(token_in, token_out)
    pool_addr = None
    if dex:
        try:
            f3 = w3.eth.contract(address=Web3.to_checksum_address(dex_cfg(chain_id, dex)["factory"]),
                                 abi=FACTORY_ABI)
            a = f3.functions.getPool(t0, t1, fee).call()
            pool_addr = a if int(a, 16) else None
        except Exception:
            pool_addr = None
    if not pool_addr:
        pool_addr, fee, dex = find_pool_dex(w3, chain_id, token_in, token_out, amount_in_wei)
        if not pool_addr:
            raise RuntimeError("Pool untuk swap tidak ditemukan.")
    cfg = dex_cfg(chain_id, dex)
    if not verify_router(w3, chain_id, dex):
        raise RuntimeError("Router gagal verifikasi (factory mismatch) — auto-swap dibatalkan.")

    raw_price = _pool_price_t1_per_t0(w3, pool_addr)  # t1 per t0 raw
    if token_in == t0:
        out_est = amount_in_wei * raw_price
    else:
        out_est = amount_in_wei / raw_price if raw_price else 0
    # potong fee pool: harga spot belum menghitungnya, dan di pool ber-fee besar
    # slippage user habis dimakan fee sehingga swap selalu revert
    out_est *= (1 - fee / 1e6)
    min_out = int(out_est * (100 - slippage_pct) / 100)

    # Saldo token masuk WAJIB dicek di sini: kalau kurang, router balas
    # "TransferHelper: TRANSFER_FROM_FAILED" — revert mentah yang tak menyebut
    # token, jumlah, maupun langkahnya. Pakai poll: sesudah wrap/swap sebelumnya,
    # replika RPC bisa masih menunjukkan saldo lama (read-after-write).
    sym_in = token_info(w3, token_in)["symbol"]
    dec_in = token_info(w3, token_in)["decimals"]
    bal_in = poll_balance(w3, token_in, account.address, amount_in_wei, tries=6)
    if bal_in <= 0 or bal_in < int(amount_in_wei * 0.99):
        raise RuntimeError(
            f"Saldo {sym_in} kurang untuk swap: punya {bal_in / 10 ** dec_in:.8f}, "
            f"butuh {amount_in_wei / 10 ** dec_in:.8f}")
    # Selisih tipis (jumlah dihitung dari saldo yang dibaca sepersekian detik lebih
    # awal, atau token fee-on-transfer) TIDAK boleh menggagalkan swap: router balas
    # 'STF' — safeTransferFrom gagal — tanpa menyebut token maupun jumlahnya. Jual
    # sebanyak yang BENAR-BENAR ada.
    if bal_in < amount_in_wei:
        out_est *= bal_in / amount_in_wei      # minOut ikut turun, kalau tidak swap revert
        amount_in_wei = bal_in
        min_out = int(out_est * (100 - slippage_pct) / 100)

    ensure_approval(w3, pk, token_in, cfg["router"], amount_in_wei)
    router = w3.eth.contract(address=Web3.to_checksum_address(cfg["router"]), abi=ROUTER_ABI)
    sym_out = token_info(w3, token_out)["symbol"]

    def _build():
        params = (token_in, token_out, fee, account.address, amount_in_wei, min_out, 0)
        return {"to": cfg["router"], "data": calldata(router.functions.exactInputSingle(params))}

    tx = _build()
    try:
        _preflight(w3, account.address, tx)
    except Exception as e:
        # 'STF' = TransferHelper.safeTransferFrom gagal: router tidak bisa menarik
        # token. Dengan saldo sudah dipastikan cukup di atas, sisanya soal allowance
        # — bisa terbaca basi dari replika RPC, atau habis dipakai swap sebelumnya.
        # Setel ulang sekali lalu simulasikan lagi sebelum menyerah.
        if "STF" in str(e) or "TRANSFER_FROM_FAILED" in str(e):
            c = erc20(w3, token_in)
            h_ap = send_tx(w3, pk, {"to": token_in,
                                    "data": calldata(c.functions.approve(cfg["router"], MAX_UINT256))})
            wait_ok(w3, h_ap, "approve ulang")
            try:
                _preflight(w3, account.address, tx)
            except Exception as e2:
                raise RuntimeError(f"Swap {sym_in}→{sym_out} (fee {fee}) ditolak pool: {e2}")
        else:
            raise RuntimeError(f"Swap {sym_in}→{sym_out} (fee {fee}) ditolak pool: {e}")
    h = send_tx(w3, pk, tx)
    wait_ok(w3, h, "swap")
    return h


def _hop_candidates(chain_id: int) -> list[str]:
    """Token perantara untuk swap 2-hop, stable dulu lalu wrapped."""
    cfg = CHAINS[chain_id]
    mids = [cfg["quotes"][s] for s in cfg["stable_syms"] if s in cfg["quotes"]]
    mids.append(cfg["wrapped"])
    return [Web3.to_checksum_address(a) for a in mids]


def swap_route(w3: Web3, chain_id: int, token_in: str, token_out: str,
               amount_in_wei: int = 0) -> list[tuple] | None:
    """Rute swap v3: [(token_in, token_out, fee)] kalau ada pool langsung, atau dua
    hop lewat stable/wrapped. None kalau tidak ada rute.

    Quote hasil auto-deteksi sering TIDAK punya pool langsung ke wrapped (mis.
    NVDAB cuma berpasangan dengan USDT), jadi 2-hop wajib ada supaya auto-buy saat
    mint dan auto-swap saat close tetap jalan."""
    token_in = Web3.to_checksum_address(token_in)
    token_out = Web3.to_checksum_address(token_out)
    if token_in == token_out:
        return []
    pool, fee, dx = find_pool_dex(w3, chain_id, token_in, token_out, amount_in_wei)
    if pool:
        return [(token_in, token_out, fee, dx)]
    for mid in _hop_candidates(chain_id):
        if mid in (token_in, token_out):
            continue
        p1, f1, d1 = find_pool_dex(w3, chain_id, token_in, mid, amount_in_wei)
        if not p1:
            continue
        p2, f2, d2 = find_pool_dex(w3, chain_id, mid, token_out)
        if p2:
            return [(token_in, mid, f1, d1), (mid, token_out, f2, d2)]
    return None


def swap_any(chain_id: int, pk: str, token_in: str, token_out: str,
             amount_in_wei: int, slippage_pct: float) -> list[tuple[str, str]]:
    """Swap token apa pun → token apa pun lewat rute langsung / 2-hop. Tiap hop
    memakai swap_to_token (exactInputSingle) yang sudah teruji — bukan calldata
    multi-hop baru. Return [(label, txhash)]."""
    if amount_in_wei <= 0:
        return []
    w3 = get_w3(chain_id)
    route = swap_route(w3, chain_id, token_in, token_out, amount_in_wei)
    if route is None:
        si = token_info(w3, Web3.to_checksum_address(token_in))["symbol"]
        so = token_info(w3, Web3.to_checksum_address(token_out))["symbol"]
        raise RuntimeError(f"Tidak ada rute swap {si} → {so} (langsung maupun 2-hop).")
    account = w3.eth.account.from_key(pk)
    out = []
    amt = amount_in_wei
    for i, (a, b, fee, dx) in enumerate(route):
        before = erc20(w3, b).functions.balanceOf(account.address).call()
        h = swap_to_token(chain_id, pk, a, b, fee, amt, slippage_pct, dex=dx)
        if not h:
            break
        si = token_info(w3, a)["symbol"]
        so = token_info(w3, b)["symbol"]
        out.append((f"swap {si}→{so}", h))
        if i + 1 < len(route):
            # hop berikutnya memakai jumlah yang BENAR-BENAR diterima, bukan estimasi
            got = poll_balance(w3, b, account.address, before + 1)
            amt = got - before
            if amt <= 0:
                raise RuntimeError(f"Hop {si}→{so} tidak menghasilkan saldo — swap berhenti.")
    return out


def close_position(chain_id: int, pk: str, token_id: int, slippage_pct: float,
                   autoswap: bool, dex: str | None = None) -> dict:
    """Full exit: decreaseLiquidity(all) + collect(max), lalu auto-swap non-wrapped → wrapped."""
    w3 = get_w3(chain_id)
    cfg = dex_cfg(chain_id, dex)
    account = w3.eth.account.from_key(pk)
    npm = w3.eth.contract(address=Web3.to_checksum_address(cfg["npm"]), abi=NPM_ABI)

    p = npm.functions.positions(token_id).call()
    (_, _, t0, t1, fee, tick_lo, tick_hi, liq, _, _, _, _) = p
    i0, i1 = token_info(w3, t0), token_info(w3, t1)
    # saldo sebelum close — dipakai menghitung ekspektasi saldo setelah collect
    pre0 = erc20(w3, t0).functions.balanceOf(account.address).call()
    pre1 = erc20(w3, t1).functions.balanceOf(account.address).call()
    steps = []

    if liq > 0:
        params = (token_id, liq, 0, 0, int(time.time()) + DEADLINE_SECS)
        h = send_tx(w3, pk, {"to": cfg["npm"], "data": calldata(npm.functions.decreaseLiquidity(params))})
        wait_ok(w3, h, "decreaseLiquidity")
        steps.append(("decrease", h))

    # simulasikan collect untuk tahu jumlah yang diterima
    got0, got1 = npm.functions.collect((token_id, account.address, MAX_UINT128, MAX_UINT128)).call(
        {"from": account.address})
    h = send_tx(w3, pk, {"to": cfg["npm"],
                         "data": calldata(npm.functions.collect((token_id, account.address, MAX_UINT128, MAX_UINT128)))})
    wait_ok(w3, h, "collect")
    steps.append(("collect", h))

    swaps = []
    if autoswap:
        wrapped = Web3.to_checksum_address(cfg["wrapped"])
        for taddr, got, pre, info in ((t0, got0, pre0, i0), (t1, got1, pre1, i1)):
            if Web3.to_checksum_address(taddr) == wrapped:
                continue
            # Jual HANYA hasil close ini, bukan seluruh saldo: token yang sudah ada
            # di wallet sebelum close itu milik user untuk keperluan lain, jangan
            # ikut dijual. Saldo dibaca dengan polling karena replika RPC bisa masih
            # 0 sesaat setelah collect (toleransi 90% untuk token fee-on-transfer).
            expected = pre + int(got * 0.9)
            bal = poll_balance(w3, taddr, account.address, max(expected, 1))
            if bal == 0:
                swaps.append((info["symbol"], "SWAP GAGAL: saldo terbaca 0 (RPC lag) — jual manual/close lagi"))
                continue
            proceeds = bal - pre if pre else bal
            if proceeds <= 0:
                swaps.append((info["symbol"], "dilewati: hasil close tidak terbaca, saldo lama tidak disentuh"))
                continue
            try:
                # swap_any: pool langsung kalau ada, kalau tidak 2-hop lewat stable —
                # sisi pool ber-quote auto-deteksi (mis. NVDAB) tidak punya pool
                # langsung ke wrapped, tanpa ini auto-swap-nya selalu gagal.
                for _lbl, sh in swap_any(chain_id, pk, taddr, wrapped, proceeds, slippage_pct):
                    swaps.append((info["symbol"], sh))
            except Exception as e:
                swaps.append((info["symbol"], f"SWAP GAGAL: {e}"))

    return {
        "steps": steps, "swaps": swaps,
        "got0": got0 / 10 ** i0["decimals"], "got1": got1 / 10 ** i1["decimals"],
        "sym0": i0["symbol"], "sym1": i1["symbol"],
    }


# ══════════════════════════ Uniswap V2 ══════════════════════════
def verify_v2_router(w3: Web3, chain_id: int, dex: str | None = None, _cache={}) -> bool:
    """Fail-closed: router.factory()==v2_factory dan router.WETH()==wrapped."""
    key = (chain_id, dex)
    if key in _cache:
        return _cache[key]
    cfg = dex_cfg(chain_id, dex)
    try:
        r = w3.eth.contract(address=Web3.to_checksum_address(cfg["v2_router"]), abi=V2_ROUTER_ABI)
        ok = (r.functions.factory().call().lower() == cfg["v2_factory"].lower()
              and r.functions.WETH().call().lower() == cfg["wrapped"].lower())
    except Exception:
        ok = False
    _cache[key] = ok
    return ok


def _v2_fot_output(w3: Web3, account_addr: str, router, router_addr: str,
                   path: list, amount_in: int, est_out: int, deadline: int) -> int:
    """Keluaran NYATA swap fee-on-transfer, dicari lewat simulasi berjenjang.

    Fungsi ...SupportingFeeOnTransferTokens tidak mengembalikan nilai apa pun, jadi
    jumlahnya tidak bisa dibaca langsung. Yang bisa: menyempitkan amountOutMin sampai
    simulasi berhenti lolos — batas itulah keluaran sebenarnya. Dipakai supaya bot
    tidak perlu mengirim swap ber-amountOutMin 0 (undangan sandwich)."""
    lo, hi = 0, max(est_out, 1)
    for _ in range(14):
        mid = (lo + hi) // 2
        if mid <= lo:
            break
        data = calldata(router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            amount_in, mid, path, account_addr, deadline))
        try:
            w3.eth.call({"from": account_addr, "to": router_addr, "data": data})
            lo = mid
        except Exception:
            hi = mid
    return lo


def _v2_swap_exec(w3: Web3, chain_id: int, pk: str, router, router_addr: str,
                  path: list, amount_in: int, slippage_pct: float) -> str:
    """Swap lewat router v2, otomatis jatuh ke jalur fee-on-transfer kalau perlu.
    Return txhash."""
    account = w3.eth.account.from_key(pk)
    slip = (100 - slippage_pct) / 100
    deadline = int(time.time()) + DEADLINE_SECS
    est = router.functions.getAmountsOut(amount_in, path).call()[-1]
    if est <= 0:
        raise RuntimeError("Estimasi hasil swap 0 — likuiditas pair terlalu tipis.")
    ensure_approval(w3, pk, path[0], router_addr, amount_in)

    data = calldata(router.functions.swapExactTokensForTokens(
        amount_in, int(est * slip), path, account.address, deadline))
    try:
        _preflight(w3, account.address, {"to": router_addr, "data": data})
    except Exception as e:
        if not any(s in str(e) for s in ("Pancake: K", "UniswapV2: K")):
            raise      # revert lain (saldo kurang, harga bergerak) — bukan soal pajak
        # Token memungut pajak transfer: pakai varian FoT dengan batas bawah yang
        # dihitung dari keluaran nyata, bukan dari getAmountsOut yang kelewat optimis.
        real = _v2_fot_output(w3, account.address, router, router_addr, path,
                              amount_in, est, deadline)
        if real <= 0:
            raise RuntimeError(
                f"Swap {token_info(w3, path[0])['symbol']} gagal: token menahan seluruh "
                f"hasil transfer (kemungkinan honeypot).")
        tax_pct = (1 - real / est) * 100
        if tax_pct > 50:
            raise RuntimeError(
                f"Pajak transfer {token_info(w3, path[0])['symbol']} ~{tax_pct:.0f}% — "
                f"terlalu besar, swap dibatalkan.")
        data = calldata(router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            amount_in, int(real * slip), path, account.address, deadline))
        _preflight(w3, account.address, {"to": router_addr, "data": data})

    h = send_tx(w3, pk, {"to": router_addr, "data": data})
    wait_ok(w3, h, "swap v2")
    return h


def _preflight(w3: Web3, account_addr: str, tx: dict):
    """Simulasi eth_call sebelum kirim — send_tx fallback gas 500k bakal
    broadcast buta kalau estimate gagal, jadi revert harus ketahuan di sini."""
    try:
        w3.eth.call({"from": account_addr, **tx})
    except Exception as e:
        raise RuntimeError(f"Simulasi tx gagal (tidak dikirim): {e}")


def _v2_pair_reserves(w3: Web3, pair_addr: str, quote: str) -> tuple[int, int]:
    """(reserve_quote, reserve_meme) wei."""
    pc = w3.eth.contract(address=Web3.to_checksum_address(pair_addr), abi=V2_PAIR_ABI)
    r0, r1, _ = pc.functions.getReserves().call()
    t0 = pc.functions.token0().call()
    return (r0, r1) if t0.lower() == quote.lower() else (r1, r0)


V2_MINT_TOPIC = Web3.keccak(text="Mint(address,uint256,uint256)").hex()
V2_BURN_TOPIC = Web3.keccak(text="Burn(address,uint256,uint256,address)").hex()


def _v2_event_amounts(receipt, pair_addr: str, topic: str) -> tuple[int, int] | None:
    for log in receipt.logs:
        if (log.address.lower() == pair_addr.lower() and log.topics
                and log.topics[0].hex().removeprefix("0x") == topic.removeprefix("0x")):
            d = log.data.hex().removeprefix("0x")
            if len(d) >= 128:
                return int(d[0:64], 16), int(d[64:128], 16)
    return None


def discover_v2_pools(w3: Web3, chain_id: int, token: str, dex: str | None = None) -> list[dict]:
    """Pair v2 token × semua quote di SATU DEX. Bentuk dict kompatibel pool_info v3 + ver=2."""
    cfg = dex_cfg(chain_id, dex)
    if not cfg.get("v2_factory"):
        return []
    token = Web3.to_checksum_address(token)
    v2f = w3.eth.contract(address=Web3.to_checksum_address(cfg["v2_factory"]), abi=V2_FACTORY_ABI)
    out = []
    for qsym, qaddr in cfg["quotes"].items():
        q = Web3.to_checksum_address(qaddr)
        if q == token:
            continue
        try:
            pair = v2f.functions.getPair(token, q).call()
            if int(pair, 16) == 0:
                continue
            rq, rm = _v2_pair_reserves(w3, pair, q)
            if rq == 0 or rm == 0:
                continue
            qdec = token_info(w3, q)["decimals"]
            qusd = quote_usd_price(w3, chain_id, qsym)
            tvl = rq / 10 ** qdec * qusd * 2
            # Tidak ada ambang TVL minimum: pool kecil pun tetap ditampilkan (ditandai
            # "thin" untuk UI). Gerbang keamanannya adalah probe round-trip di bawah —
            # itu yang menangkal pool dust/harga dimanipulasi, bukan angka TVL.
            # round-trip lokal $100: pair yang tidak bisa serap swap kecil = dust/manipulasi.
            # Konstanta fee ikut router chain ini (Uniswap 997/1000, Pancake 9975/10000).
            num, den = cfg.get("v2_swap_num", 997), cfg.get("v2_swap_den", 1000)
            probe = int(min(100 / qusd if qusd else 0, rq / 10 ** qdec / 100 or 1) * 10 ** qdec) or 1
            o1 = probe * num * rm // (rq * den + probe * num)
            back = o1 * num * rq // (rm * den + o1 * num)
            if back < probe * 70 // 100:
                continue
            t0, t1 = sort_tokens(token, q)
            out.append({
                "ver": 2, "dex": cfg.get("dex"), "pool": pair, "fee": cfg.get("v2_fee", 3000),
                "quote_sym": qsym, "quote_addr": q,
                "quote_decimals": qdec, "quote_usd": qusd,
                "tick": None, "sqrtp": None, "liquidity": None,
                "reserve_quote": rq, "reserve_meme": rm,
                "tvl_usd": tvl, "token0": t0, "token1": t1, "quote_is_token1": q == t1,
            })
        except Exception:
            continue
    return out


def mint_v2(chain_id: int, pk: str, pool_info: dict, budget: float, slippage_pct: float) -> dict:
    """Add liquidity v2: swap ~50% quote → meme (meme existing dipakai duluan),
    lalu router.addLiquidity dengan mins ber-slippage. Budget satuan quote."""
    w3 = get_w3(chain_id)
    cfg = pool_cfg(chain_id, pool_info)
    if not verify_v2_router(w3, chain_id, cfg.get("dex")):
        raise RuntimeError("V2 router gagal verifikasi on-chain (factory/WETH mismatch) — batal.")
    account = w3.eth.account.from_key(pk)
    router_addr = Web3.to_checksum_address(cfg["v2_router"])
    router = w3.eth.contract(address=router_addr, abi=V2_ROUTER_ABI)
    pair = Web3.to_checksum_address(pool_info["pool"])
    quote = Web3.to_checksum_address(pool_info["quote_addr"])
    meme = Web3.to_checksum_address(pool_info["token0"] if pool_info["quote_is_token1"] else pool_info["token1"])
    qdec = pool_info["quote_decimals"]
    minfo = token_info(w3, meme)
    slip = (100 - slippage_pct) / 100
    deadline = int(time.time()) + DEADLINE_SECS

    budget_wei = int(Decimal(str(budget)) * Decimal(10) ** qdec)
    if budget_wei <= 0:
        raise RuntimeError("Amount 0.")
    steps = ensure_quote_balance(w3, chain_id, pk, quote, budget_wei, slippage_pct)
    budget_wei = min(budget_wei, erc20(w3, quote).functions.balanceOf(account.address).call())

    rq, rm = _v2_pair_reserves(w3, pair, quote)
    if rq == 0 or rm == 0:
        raise RuntimeError("Reserves pair v2 kosong.")
    meme_bal = erc20(w3, meme).functions.balanceOf(account.address).call()
    meme_val_q = meme_bal * rq // rm
    # target 50/50: quote yang ditahan = setengah total modal (quote + nilai meme existing)
    quote_keep = min((budget_wei + meme_val_q) // 2, budget_wei)
    swap_in = budget_wei - quote_keep
    swapped = False
    if swap_in > budget_wei // 500:
        # _v2_swap_exec otomatis pindah ke jalur fee-on-transfer kalau pair menolak
        # dengan "Pancake: K" — token berpajak seperti RTX butuh itu.
        h = _v2_swap_exec(w3, chain_id, pk, router, router_addr,
                          [quote, meme], swap_in, slippage_pct)
        steps.append(("swap", h))
        swapped = True
    meme_have = poll_balance(w3, meme, account.address, meme_bal + 1) if swapped \
        else meme_bal
    if quote_keep <= 0 or meme_have <= 0:
        raise RuntimeError("Salah satu sisi 0 — v2 butuh dua sisi (quote + meme).")

    rq, rm = _v2_pair_reserves(w3, pair, quote)  # fresh setelah swap
    meme_need = quote_keep * rm // rq
    if meme_have < meme_need:
        # Meme yang benar-benar dipegang lebih sedikit dari yang dituntut rasio pool
        # (fee + price impact swap, atau reserve bergerak sesudahnya). Router memakai
        # sisi yang paling membatasi, jadi sisi quote HARUS ikut diturunkan — kalau
        # tidak, amountAOptimal jatuh di bawah amountAMin dan router menolak dengan
        # INSUFFICIENT_A_AMOUNT.
        quote_keep = meme_have * rq // rm
        meme_need = meme_have
        if quote_keep <= 0:
            raise RuntimeError("Sisi meme terlalu kecil untuk dipasangkan — coba amount lebih besar.")
    meme_desired = min(meme_have, meme_need + meme_need // 100 + 1)
    a_min = int(quote_keep * slip)
    b_min = int(min(meme_need, meme_desired) * slip)
    steps += ensure_approval(w3, pk, quote, router_addr, quote_keep)
    steps += ensure_approval(w3, pk, meme, router_addr, meme_desired)
    lp_before = erc20(w3, pair).functions.balanceOf(account.address).call()
    data = calldata(router.functions.addLiquidity(
        quote, meme, quote_keep, meme_desired, a_min, b_min, account.address, deadline))
    _preflight(w3, account.address, {"to": router_addr, "data": data})
    h = send_tx(w3, pk, {"to": router_addr, "data": data})
    receipt = wait_ok(w3, h, "addLiquidity v2")
    steps.append(("addLiquidity", h))

    # Patokan fee: √k per LP saat masuk. Disimpan pemanggil (registry) supaya nanti
    # bisa dihitung berapa fee yang sudah mengendap ke dalam posisi.
    lp_after = poll_balance(w3, pair, account.address, lp_before + 1)
    try:
        nrq, nrm = _v2_pair_reserves(w3, pair, quote)
        ntot = erc20(w3, pair).functions.totalSupply().call()
        k_per_lp = math.sqrt(nrq * nrm) / ntot if ntot else 0.0
    except Exception:
        k_per_lp = 0.0

    qusd = pool_info["quote_usd"]
    amts = _v2_event_amounts(receipt, pair, V2_MINT_TOPIC)
    if amts:
        a0, a1 = amts
        q_amt, m_amt = (a1, a0) if pool_info["quote_is_token1"] else (a0, a1)
    else:
        q_amt, m_amt = quote_keep, meme_desired
    m_in_q = m_amt * rq // rm if rm else 0
    deposited_usd = (q_amt + m_in_q) / 10 ** qdec * qusd
    return {"steps": steps, "pair": pair, "deposited_usd": deposited_usd,
            "quote_in": q_amt / 10 ** qdec, "meme_in": m_amt / 10 ** minfo["decimals"],
            "quote_sym": pool_info["quote_sym"], "meme_sym": minfo["symbol"],
            "k_per_lp": k_per_lp, "lp_before": lp_before, "lp_after": lp_after}


def _v2_position_detail(w3: Web3, chain_id: int, pair_addr: str, account_addr: str) -> dict | None:
    cfg = CHAINS[chain_id]
    try:
        pair = Web3.to_checksum_address(pair_addr)
        pc = w3.eth.contract(address=pair, abi=V2_PAIR_ABI)
        lp = erc20(w3, pair).functions.balanceOf(account_addr).call()
        if lp == 0:
            return None
        total = erc20(w3, pair).functions.totalSupply().call()
        r0, r1, _ = pc.functions.getReserves().call()
        t0, t1 = pc.functions.token0().call(), pc.functions.token1().call()
        i0, i1 = token_info(w3, t0), token_info(w3, t1)
        a0, a1 = r0 * lp // total, r1 * lp // total

        qsym, q_is_t1 = resolve_quote_side(w3, chain_id, t0, t1, i0["symbol"], i1["symbol"])
        if not qsym:
            return None       # kedua sisi tak bisa dihargai — nilai posisi mustahil dihitung
        qusd = quote_usd_price(w3, chain_id, qsym)
        rq, rm = (r1, r0) if q_is_t1 else (r0, r1)
        aq, am = (a1, a0) if q_is_t1 else (a0, a1)
        qdec = (i1 if q_is_t1 else i0)["decimals"]
        usd_q = aq / 10 ** qdec * qusd
        usd_m = (am * rq // rm) / 10 ** qdec * qusd if rm else 0
        # √k per LP token: patokan fee v2. Perdagangan menggeser harga tanpa mengubah
        # angka ini — yang menaikkannya HANYA fee yang mengendap di reserve. Jadi
        # selisihnya terhadap nilai saat mint = fee yang sudah kamu kumpulkan.
        k_per_lp = (math.sqrt(r0 * r1) / total) if total else 0.0
        usd0, usd1 = (usd_m, usd_q) if q_is_t1 else (usd_q, usd_m)
        return {
            "ver": 2, "pid": f"v2:{pair.lower()}", "token_id": f"v2:{pair.lower()}",
            "dex": which_dex_v2(w3, chain_id, pair, t0, t1),
            "token0": t0, "token1": t1, "sym0": i0["symbol"], "sym1": i1["symbol"],
            "dec0": i0["decimals"], "dec1": i1["decimals"],
            "fee": CHAINS[chain_id].get("v2_fee", 3000), "pool": pair,
            "tick_lower": None, "tick_upper": None, "cur_tick": None,
            "liquidity": lp, "amount0": a0 / 10 ** i0["decimals"], "amount1": a1 / 10 ** i1["decimals"],
            "fees0": 0.0, "fees1": 0.0, "in_range": True,
            "value_usd": usd0 + usd1, "unclaimed_usd": 0.0,
            "usd0": usd0, "usd1": usd1, "fees_usd0": 0.0, "fees_usd1": 0.0,
            "quote_sym": qsym, "quote_is_token1": q_is_t1, "k_per_lp": k_per_lp,
            "mc_lower": None, "mc_upper": None, "mc_now": None,
        }
    except Exception:
        return None


def reduce_v2(chain_id: int, pk: str, pair_addr: str, pct: int, slippage_pct: float,
              autoswap: bool = False) -> dict:
    """removeLiquidity pct% (100 = close). Mins ber-slippage dari share reserves.
    autoswap: jual meme hasil penarikan → wrapped via router v2."""
    if not 1 <= pct <= 100:
        raise RuntimeError("Persen 1–100.")
    w3 = get_w3(chain_id)
    pair = Web3.to_checksum_address(pair_addr)
    pc = w3.eth.contract(address=pair, abi=V2_PAIR_ABI)
    # Alamat pair tidak menyimpan router mana yang berhak — tanyakan ke factory tiap
    # DEX. Salah router = removeLiquidity gagal (atau lebih buruk, pair lain).
    _t0, _t1 = pc.functions.token0().call(), pc.functions.token1().call()
    dex = which_dex_v2(w3, chain_id, pair, _t0, _t1)
    if not dex:
        raise RuntimeError("Pair v2 ini tidak terdaftar di factory DEX mana pun — batal.")
    cfg = dex_cfg(chain_id, dex)
    if not verify_v2_router(w3, chain_id, dex):
        raise RuntimeError("V2 router gagal verifikasi on-chain — batal.")
    account = w3.eth.account.from_key(pk)
    router_addr = Web3.to_checksum_address(cfg["v2_router"])
    router = w3.eth.contract(address=router_addr, abi=V2_ROUTER_ABI)
    slip = (100 - slippage_pct) / 100
    deadline = int(time.time()) + DEADLINE_SECS

    # saldo kedua sisi SEBELUM remove — dipakai supaya auto-swap cuma menjual hasil
    # penarikan, bukan token yang memang sudah ada di wallet
    _pt0, _pt1 = pc.functions.token0().call(), pc.functions.token1().call()
    pre_bal = {_pt0.lower(): erc20(w3, _pt0).functions.balanceOf(account.address).call(),
               _pt1.lower(): erc20(w3, _pt1).functions.balanceOf(account.address).call()}
    lp = erc20(w3, pair).functions.balanceOf(account.address).call()
    if lp == 0:
        raise RuntimeError("Saldo LP 0.")
    part = lp if pct == 100 else lp * pct // 100
    total = erc20(w3, pair).functions.totalSupply().call()
    r0, r1, _ = pc.functions.getReserves().call()
    t0, t1 = pc.functions.token0().call(), pc.functions.token1().call()
    i0, i1 = token_info(w3, t0), token_info(w3, t1)
    exp0, exp1 = r0 * part // total, r1 * part // total

    steps = ensure_approval(w3, pk, pair, router_addr, part)
    data = calldata(router.functions.removeLiquidity(
        t0, t1, part, int(exp0 * slip), int(exp1 * slip), account.address, deadline))
    _preflight(w3, account.address, {"to": router_addr, "data": data})
    h = send_tx(w3, pk, {"to": router_addr, "data": data})
    receipt = wait_ok(w3, h, "removeLiquidity v2")
    steps.append(("remove", h))
    amts = _v2_event_amounts(receipt, pair, V2_BURN_TOPIC) or (exp0, exp1)

    swaps = []
    if autoswap:
        wrapped = Web3.to_checksum_address(cfg["wrapped"])
        quotes_lc = {a.lower() for a in cfg["quotes"].values()}

        def v2_swap(taddr, path, bal):
            return _v2_swap_exec(w3, chain_id, pk, router, router_addr, path, bal, slippage_pct)

        def has_v3_route(a):
            try:
                return swap_route(w3, chain_id, a, wrapped) is not None
            except Exception:
                return False

        # Urutan penting: sisi yang TIDAK punya rute ke wrapped dikerjakan duluan supaya
        # bisa dikonversi ke sisi lawannya, lalu sisi lawan itu yang dijual ke wrapped.
        # Kasus nyata: pair RTX/NVDAB — RTX cuma ada di pair ini, NVDAB cuma punya
        # pool v3 ke USDT. Tanpa urutan ini, keduanya gagal dijual.
        sides = sorted(((t0, i0), (t1, i1)),
                       key=lambda s: has_v3_route(Web3.to_checksum_address(s[0])))
        for taddr, info in sides:
            if taddr.lower() == wrapped.lower():
                continue
            taddr = Web3.to_checksum_address(taddr)
            bal = erc20(w3, taddr).functions.balanceOf(account.address).call()
            bal -= pre_bal.get(taddr.lower(), 0)      # hasil penarikan saja
            if bal <= 0:
                continue
            other = Web3.to_checksum_address(t1 if taddr == Web3.to_checksum_address(t0) else t0)
            err = None
            # 1) rute v2: langsung ke wrapped, atau lewat sisi lawan pair ini
            path = [taddr, wrapped]
            if taddr.lower() not in quotes_lc and other.lower() != wrapped.lower():
                path = [taddr, other, wrapped]
            try:
                swaps.append((info["symbol"], v2_swap(taddr, path, bal)))
                continue
            except Exception as e:
                err = e
            # 2) rute v3 (langsung / 2-hop lewat stable) — dipakai saat sisi ini tidak
            #    punya pair v2 ke wrapped, mis. quote auto-deteksi yang cuma ada di v3
            try:
                hs = swap_any(chain_id, pk, taddr, wrapped, bal, slippage_pct)
                if hs:
                    swaps += [(lbl, h) for lbl, h in hs]   # label per hop, bukan nama sisi
                    continue
            except Exception as e:
                err = e
            # 3) terakhir: jual ke sisi lawan lewat pair ini sendiri — sisi lawan yang
            #    punya rute ke wrapped akan menjualnya di iterasi berikutnya
            if other.lower() != wrapped.lower():
                try:
                    swaps.append((info["symbol"], v2_swap(taddr, [taddr, other], bal)))
                    continue
                except Exception as e:
                    err = e
            swaps.append((info["symbol"], f"SWAP GAGAL: {err}"))

    return {"steps": steps, "swaps": swaps,
            "got0": amts[0] / 10 ** i0["decimals"], "got1": amts[1] / 10 ** i1["decimals"],
            "sym0": i0["symbol"], "sym1": i1["symbol"], "closed": pct == 100}


# ══════════════════════════ Uniswap V4 ══════════════════════════
def uni_api_dex(chain_id: int) -> str | None:
    """DEX yang boleh memakai indexer resmi Uniswap (ListPools/ListPositions)."""
    for d in dex_names(chain_id):
        if dex_cfg(chain_id, d).get("uni_api"):
            return d
    return None


def v4_dex(chain_id: int) -> str | None:
    """DEX pemilik deployment v4 di chain ini. Cuma ada satu (BSC: Uniswap;
    Robinhood: Uniswap), jadi fungsi-fungsi v4 tidak perlu dioper nama DEX."""
    for d in dex_names(chain_id):
        if has_v4(chain_id, d):
            return d
    return None


def v4_cfg(chain_id: int) -> dict:
    return dex_cfg(chain_id, v4_dex(chain_id))


def _v4c(w3: Web3, chain_id: int, which: str, abi):
    return w3.eth.contract(address=Web3.to_checksum_address(v4_cfg(chain_id)[which]), abi=abi)


def verify_v4(w3: Web3, chain_id: int, dex: str | None = None, _cache={}) -> bool:
    """Fail-closed: posm/stateview/UR semua harus menunjuk PoolManager yang sama
    dan posm.permit2() harus Permit2 canonical. Salah satu gagal = semua aksi v4 batal."""
    dex = dex or v4_dex(chain_id)
    key = (chain_id, dex)
    if key in _cache:
        return _cache[key]
    cfg = dex_cfg(chain_id, dex)
    try:
        pm = cfg["v4_pm"].lower()
        posm = _v4c(w3, chain_id, "v4_posm", V4_POSM_ABI)
        sv = _v4c(w3, chain_id, "v4_stateview", V4_STATEVIEW_ABI)
        ur = _v4c(w3, chain_id, "v4_router", V4_UR_ABI)
        qt = _v4c(w3, chain_id, "v4_quoter", V4_QUOTER_ABI)
        ok = (posm.functions.poolManager().call().lower() == pm
              and posm.functions.permit2().call().lower() == cfg["permit2"].lower()
              and sv.functions.poolManager().call().lower() == pm
              and ur.functions.poolManager().call().lower() == pm
              and qt.functions.poolManager().call().lower() == pm)
    except Exception:
        ok = False
    _cache[key] = ok
    return ok


def v4_pool_key(a: str, b: str, fee: int, spacing: int) -> tuple:
    """(currency0, currency1, fee, tickSpacing, hooks) — currency sorted ascending,
    native ETH = address(0) selalu currency0. Hooks selalu 0 (pool vanilla saja)."""
    # sort_tokens, bukan sorted(): di sini kebetulan aman karena .lower() bikin
    # urutan string = urutan numerik, tapi jangan tinggalkan pola yang mudah salah
    # disalin ke tempat yang alamatnya checksum (lihat catatan di sort_tokens).
    c0, c1 = sort_tokens(a.lower(), b.lower())
    return (Web3.to_checksum_address(c0), Web3.to_checksum_address(c1), fee, spacing,
            Web3.to_checksum_address(V4_NATIVE))


def v4_pool_id(key: tuple) -> bytes:
    return Web3.keccak(abi_encode(["address", "address", "uint24", "int24", "address"], list(key)))


def v4_slot0(w3: Web3, chain_id: int, pool_id: bytes) -> tuple[int, int]:
    sv = _v4c(w3, chain_id, "v4_stateview", V4_STATEVIEW_ABI)
    s = sv.functions.getSlot0(pool_id).call()
    return s[0], s[1]


def _v4_currency_info(w3: Web3, chain_id: int, cur: str) -> dict:
    if cur.lower() == V4_NATIVE:
        cfg = CHAINS[chain_id]
        return {"address": V4_NATIVE, "symbol": cfg["native_symbol"], "decimals": 18}
    return token_info(w3, cur)


def _v4_balance(w3: Web3, cur: str, addr: str) -> int:
    if cur.lower() == V4_NATIVE:
        return w3.eth.get_balance(addr)
    return erc20(w3, cur).functions.balanceOf(addr).call()


def _v4_quote_side(chain_id: int, c0: str, c1: str, w3: Web3 | None = None) -> tuple[str | None, bool]:
    """(quote_sym, quote_is_c1). Native ETH dihitung quote (dihargai = wrapped).

    Dengan `w3`, pair yang tidak punya quote tetap ikut diselesaikan lewat
    `resolve_quote_side()` — sisi yang punya sokongan likuiditas on-chain didaftarkan
    sebagai quote runtime, persis seperti jalur v3/v2. Tanpa itu posisi v4 ber-quote
    asing tampil bernilai $0,00 dan setiap aksinya ditolak "Pair tanpa quote yang
    dikenal bot" (kasus nyata: PACK/NVDA #645408 — 1,08567 NVDA di dalamnya).

    `w3` sengaja TIDAK dipakai di jalur discovery: `resolve_quote_side` membaca
    likuiditas on-chain untuk kedua sisi, kalau dipanggil per pool hasil indexer
    biayanya meledak. Discovery punya `discover_foreign_pools()` untuk kasus itu."""
    cfg = CHAINS[chain_id]
    quotes_lc = {a.lower(): s for s, a in cfg["quotes"].items()}
    if c1.lower() in quotes_lc:
        return quotes_lc[c1.lower()], True
    if c0.lower() in quotes_lc:
        return quotes_lc[c0.lower()], False
    if c0.lower() == V4_NATIVE:
        return cfg["wrapped_symbol"], False   # harga native = harga wrapped
    # Quote runtime yang SUDAH terdaftar dipakai langsung: resolve_quote_side membaca
    # sokongan likuiditas kedua sisi (terukur 17 detik), terlalu mahal untuk diulang
    # tiap refresh daftar posisi.
    reg = _EXTRA_QUOTES.get(chain_id, {})
    for s, a in reg.items():
        if a.lower() == c1.lower():
            return s, True
        if a.lower() == c0.lower():
            return s, False
    if w3 is not None:
        try:
            return resolve_quote_side(w3, chain_id, c0, c1)
        except Exception:
            pass
    return None, True


def v4_roundtrip_ok(w3: Web3, chain_id: int, key: tuple, quote_is_c1: bool,
                    probe_wei: int, max_loss_pct: float = 30.0) -> bool:
    """Uji kesehatan pool: swap simulasi quote→meme→quote via Quoter.
    Pool dust / harga dimanipulasi (tick ekstrem) bakal rugi besar atau revert.
    Ini satu-satunya cara murah memfilter pool v4 beracun — TVL virtual bisa dipalsukan."""
    if probe_wei <= 0:
        return False
    try:
        qt = _v4c(w3, chain_id, "v4_quoter", V4_QUOTER_ABI)
        z1 = not quote_is_c1  # quote→meme: zeroForOne kalau quote = currency0
        out1, _ = qt.functions.quoteExactInputSingle(
            (tuple(key), z1, min(probe_wei, MAX_UINT128), b"")).call()
        if out1 <= 0:
            return False
        out2, _ = qt.functions.quoteExactInputSingle(
            (tuple(key), not z1, min(out1, MAX_UINT128), b"")).call()
        return out2 >= probe_wei * (100 - max_loss_pct) / 100
    except Exception:
        return False


def discover_v4_pools(w3: Web3, chain_id: int, token: str) -> list[dict]:
    """Scan pool v4 vanilla (hooks=0) token × (native, semua quote) × fee standar.
    TVL proxy dari liquidity aktif; pool dust (< $10) dibuang."""
    cfg = v4_cfg(chain_id)
    if not cfg.get("v4_stateview") or not verify_v4(w3, chain_id):
        return []
    token = Web3.to_checksum_address(token)
    sv = _v4c(w3, chain_id, "v4_stateview", V4_STATEVIEW_ABI)
    cands = [(cfg["native_symbol"], V4_NATIVE)] + list(cfg["quotes"].items())
    out = []
    for qsym, qaddr in cands:
        if qaddr.lower() == token.lower():
            continue
        for fee, spacing in V4_FEE_SPACINGS:
            try:
                key = v4_pool_key(token, qaddr, fee, spacing)
                pid = v4_pool_id(key)
                sqrtp, tick, _, _ = sv.functions.getSlot0(pid).call()
                if sqrtp == 0:
                    continue
                liq = sv.functions.getLiquidity(pid).call()
                if liq == 0:
                    continue
                q_is_c1 = key[1].lower() == qaddr.lower()
                qinfo = _v4_currency_info(w3, chain_id, qaddr)
                price_sym = qsym if qaddr.lower() != V4_NATIVE else cfg["wrapped_symbol"]
                qusd = quote_usd_price(w3, chain_id, price_sym)
                # reserve virtual sisi quote di harga sekarang (proxy TVL, bukan angka pasti)
                if q_is_c1:
                    q_virt = liq * sqrtp // Q96
                else:
                    q_virt = liq * Q96 // sqrtp if sqrtp else 0
                tvl = q_virt / 10 ** qinfo["decimals"] * qusd * 2
                if tvl <= 0:
                    continue
                # probe $100 (atau 1% reserve virtual) round-trip — buang pool beracun
                probe = int(min(100 / qusd if qusd else 0, q_virt / 10 ** qinfo["decimals"] / 100 or 1)
                            * 10 ** qinfo["decimals"]) or 1
                if not v4_roundtrip_ok(w3, chain_id, key, q_is_c1, probe):
                    continue
                out.append({
                    "ver": 4, "dex": v4_dex(chain_id), "pool": "0x" + pid.hex().removeprefix("0x"), "pool_id": pid,
                    "key": key, "fee": fee, "tick_spacing": spacing,
                    "quote_sym": qsym, "quote_addr": key[1] if q_is_c1 else key[0],
                    "quote_decimals": qinfo["decimals"], "quote_usd": qusd,
                    "tick": tick, "sqrtp": sqrtp, "liquidity": liq, "tvl_usd": tvl,
                    "token0": key[0], "token1": key[1], "quote_is_token1": q_is_c1,
                })
            except Exception:
                continue
    return out


def ensure_permit2(w3: Web3, chain_id: int, pk: str, token: str, spender: str,
                   need_wei: int) -> list[tuple[str, str]]:
    """Approval dua tahap Permit2: ERC20→Permit2 (sekali, infinite — standar Permit2),
    lalu Permit2→spender DIBATASI: jumlah pas + kedaluwarsa 1 jam."""
    cfg = CHAINS[chain_id]
    account = w3.eth.account.from_key(pk)
    token = Web3.to_checksum_address(token)
    p2_addr = Web3.to_checksum_address(cfg["permit2"])
    p2 = w3.eth.contract(address=p2_addr, abi=PERMIT2_ABI)
    steps = []
    if erc20(w3, token).functions.allowance(account.address, p2_addr).call() < need_wei:
        h = send_tx(w3, pk, {"to": token,
                             "data": calldata(erc20(w3, token).functions.approve(p2_addr, MAX_UINT256))})
        wait_ok(w3, h, "approve permit2")
        steps.append(("approve", h))
    spender = Web3.to_checksum_address(spender)
    amt, exp, _ = p2.functions.allowance(account.address, token, spender).call()
    now = int(time.time())
    if amt < need_wei or exp < now + DEADLINE_SECS:
        need160 = min(need_wei, 2 ** 160 - 1)
        h = send_tx(w3, pk, {"to": p2_addr,
                             "data": calldata(p2.functions.approve(token, spender, need160, now + 3600))})
        wait_ok(w3, h, "permit2 approve")
        steps.append(("permit2", h))
    return steps


def _v4_unlock(actions: list[int], params: list[bytes]) -> bytes:
    return abi_encode(["bytes", "bytes[]"], [bytes(actions), params])


_V4_POOLKEY_T = "(address,address,uint24,int24,address)"


def v4_swap(chain_id: int, pk: str, key: tuple, token_in: str, amount_in: int,
            slippage_pct: float) -> str | None:
    """Swap exact-in single via UniversalRouter (command V4_SWAP).
    minOut dihitung dari harga pool sekarang − slippage."""
    if amount_in <= 0:
        return None
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
    if not verify_v4(w3, chain_id):
        raise RuntimeError("Kontrak V4 gagal verifikasi on-chain — swap dibatalkan.")
    account = w3.eth.account.from_key(pk)
    ur_addr = Web3.to_checksum_address(cfg["v4_router"])
    ur = _v4c(w3, chain_id, "v4_router", V4_UR_ABI)
    pid = v4_pool_id(key)
    sqrtp, _ = v4_slot0(w3, chain_id, pid)
    zero_for_one = token_in.lower() == key[0].lower()
    # minOut WAJIB dihitung dari hasil quoter, bukan harga spot: harga spot tidak
    # memotong fee pool. Di pool fee 5%, slippage 5% habis dimakan fee saja sehingga
    # tidak ada toleransi untuk price impact — swap PASTI revert V4TooLittleReceived
    # (terbukti: minta 1.851,17 BULL, dapat 1.848,24 = kurang 0,16%).
    out_est = 0
    try:
        qt = _v4c(w3, chain_id, "v4_quoter", V4_QUOTER_ABI)
        out_est = qt.functions.quoteExactInputSingle(
            (tuple(key), zero_for_one, min(amount_in, MAX_UINT128), b"")).call()[0]
    except Exception:
        out_est = 0
    if out_est <= 0:
        # Quoter gagal (mis. fee dinamis) → harga spot dikurangi fee statis kalau ada.
        raw = (sqrtp / Q96) ** 2  # c1 per c0
        spot = amount_in * raw if zero_for_one else (amount_in / raw if raw else 0)
        fee_ppm = key[2] if key[2] < 0x800000 else 0
        out_est = spot * (1 - fee_ppm / 1e6)
    min_out = int(out_est * (100 - slippage_pct) / 100)
    if min_out <= 0:
        raise RuntimeError("Estimasi hasil swap v4 = 0.")
    cur_in = key[0] if zero_for_one else key[1]
    cur_out = key[1] if zero_for_one else key[0]

    value = 0
    if cur_in.lower() == V4_NATIVE:
        value = amount_in
    else:
        ensure_permit2(w3, chain_id, pk, cur_in, ur_addr, amount_in)

    amount_in = min(amount_in, MAX_UINT128)
    if cfg.get("v4_swap_hop_field"):
        # build custom (Robinhood): field ekstra minHopPriceX36 sebelum hookData; 0 = tanpa limit
        p_swap = abi_encode([f"({_V4_POOLKEY_T},bool,uint128,uint128,uint256,bytes)"],
                            [(tuple(key), zero_for_one, amount_in, min(min_out, MAX_UINT128), 0, b"")])
    else:
        p_swap = abi_encode([f"({_V4_POOLKEY_T},bool,uint128,uint128,bytes)"],
                            [(tuple(key), zero_for_one, amount_in, min(min_out, MAX_UINT128), b"")])
    p_settle = abi_encode(["address", "uint256"], [cur_in, amount_in])
    p_take = abi_encode(["address", "uint256"], [cur_out, min_out])
    unlock = _v4_unlock([V4_SWAP_IN_SINGLE, V4_SETTLE_ALL, V4_TAKE_ALL], [p_swap, p_settle, p_take])
    data = calldata(ur.functions.execute(bytes([UR_CMD_V4_SWAP]), [unlock],
                                         int(time.time()) + DEADLINE_SECS))
    tx = {"to": ur_addr, "data": data, "value": value}
    _preflight(w3, account.address, tx)
    h = send_tx(w3, pk, tx)
    wait_ok(w3, h, "swap v4")
    return h


def other_quote_capital(w3: Web3, chain_id: int, addr: str, quote_addr: str,
                        margin: float = 0.97) -> int:
    """Saldo quote LAIN di wallet, dinyatakan dalam satuan quote pool ini.

    Dipakai supaya "50% saldo" berarti 50% dari seluruh modal yang bisa dipakai
    (mis. ETH + WETH + USDG untuk pool ber-quote ETH), bukan cuma satu token.
    margin memotong perkiraan untuk fee & slippage swap — jangan dihilangkan,
    kalau kelebihan hitung mint-nya gagal di tengah jalan."""
    cfg = CHAINS[chain_id]
    q = str(quote_addr).lower()
    wrapped = str(cfg["wrapped"]).lower()
    qdec = 18 if q == V4_NATIVE else token_info(w3, Web3.to_checksum_address(quote_addr))["decimals"]
    qsym = cfg["wrapped_symbol"] if q in (V4_NATIVE, wrapped) else None
    qusd = (quote_usd_price(w3, chain_id, qsym) if qsym
            else token_usd_price(w3, chain_id, quote_addr))
    if qusd <= 0:
        return 0
    total = 0
    for sym, addr_o in cfg["quotes"].items():
        ol = str(addr_o).lower()
        # native & wrapped diurus terpisah (1:1, tinggal wrap/unwrap)
        if ol == q or ol == wrapped:
            continue
        try:
            c = erc20(w3, addr_o)
            bal = c.functions.balanceOf(Web3.to_checksum_address(addr)).call()
            if bal <= 0:
                continue
            odec = token_info(w3, Web3.to_checksum_address(addr_o))["decimals"]
            ousd = quote_usd_price(w3, chain_id, sym)
            if ousd <= 0:
                continue
            total += int(bal / 10 ** odec * ousd / qusd * 10 ** qdec * margin)
        except Exception:
            continue
    return total


def ensure_native_balance(w3: Web3, chain_id: int, pk: str, need_wei: int,
                          slippage_pct: float = 5.0) -> list[tuple[str, str]]:
    """Pastikan saldo NATIVE cukup — kalau kurang, unwrap WETH secukupnya.

    WETH itu 1:1 dengan native, jadi memperlakukannya sebagai modal terpisah cuma
    menyulitkan user: wallet berisi 0,2 WETH tapi bot bilang "saldo kurang" untuk
    pool ber-quote ETH native."""
    account = w3.eth.account.from_key(pk)
    txs = []
    bal = w3.eth.get_balance(account.address)
    if bal >= need_wei:
        return txs
    deficit = need_wei - bal
    wrapped = Web3.to_checksum_address(CHAINS[chain_id]["wrapped"])
    wbal = erc20(w3, wrapped).functions.balanceOf(account.address).call()
    if wbal <= 0:
        return txs
    weth = w3.eth.contract(address=wrapped, abi=WETH_ABI)
    if wbal > 0:
        # Unwrap LEBIH dari kekurangan: tx unwrap (dan swap di bawah) membakar gas dari
        # saldo native yang sama, jadi unwrap pas-pasan selalu mendarat kurang sebanyak
        # gas yang baru saja dipakai — terukur "punya 0.130946, butuh 0.130789 + gas".
        amt = min(wbal, deficit + gas_reserve_wei(chain_id, w3))
        h = send_tx(w3, pk, {"to": wrapped, "data": calldata(weth.functions.withdraw(amt))})
        wait_ok(w3, h, "unwrap")
        txs.append(("unwrap", h))
        # kekurangan dihitung ulang dari saldo NYATA, bukan dikurangi angka rencana
        deficit = need_wei - w3.eth.get_balance(account.address)
    if deficit <= 0:
        return txs
    # Masih kurang → jual quote lain (mis. USDG) jadi wrapped, lalu unwrap.
    # Hanya sebanyak kekurangannya, bukan seluruh saldo.
    cfg = CHAINS[chain_id]
    w_usd = quote_usd_price(w3, chain_id, cfg["wrapped_symbol"])
    for sym, oaddr in cfg["quotes"].items():
        if deficit <= 0:
            break
        if str(oaddr).lower() == wrapped.lower():   # wrapped di-checksum, oaddr belum tentu
            continue
        try:
            c = erc20(w3, oaddr)
            obal = c.functions.balanceOf(account.address).call()
            if obal <= 0:
                continue
            odec = token_info(w3, Web3.to_checksum_address(oaddr))["decimals"]
            ousd = quote_usd_price(w3, chain_id, sym)
            if ousd <= 0 or w_usd <= 0:
                continue
            # +3% biaya swap, + cadangan gas: swap dan unwrap di bawah ikut membakar native
            need_o = int((deficit + gas_reserve_wei(chain_id, w3))
                         / 1e18 * w_usd / ousd * 10 ** odec * 1.03)
            spend = min(obal, need_o)
            if spend <= 0:
                continue
            before = erc20(w3, wrapped).functions.balanceOf(account.address).call()
            txs += swap_any(chain_id, pk, oaddr, wrapped, spend, slippage_pct)
            got = poll_balance(w3, wrapped, account.address, before + 1) - before
            if got <= 0:
                continue
            h = send_tx(w3, pk, {"to": wrapped, "data": calldata(weth.functions.withdraw(got))})
            wait_ok(w3, h, "unwrap")
            txs.append(("unwrap", h))
            deficit = need_wei - w3.eth.get_balance(account.address)
        except Exception:
            continue
    return txs


def _v4_ensure_funds(w3: Web3, chain_id: int, pk: str, currency: str, need_wei: int,
                     slippage_pct: float) -> list[tuple[str, str]]:
    """Native → unwrap WETH kalau perlu. ERC20 → jalur ensure_quote_balance biasa."""
    if currency.lower() == V4_NATIVE:
        account = w3.eth.account.from_key(pk)
        gas_reserve = gas_reserve_wei(chain_id, w3)
        txs = ensure_native_balance(w3, chain_id, pk, need_wei + gas_reserve, slippage_pct)
        bal = w3.eth.get_balance(account.address)
        if bal < need_wei + gas_reserve:
            raise RuntimeError(
                f"Saldo native+WETH kurang: punya {bal / 1e18:.6f}, "
                f"butuh {need_wei / 1e18:.6f} + gas")
        return txs
    return ensure_quote_balance(w3, chain_id, pk, currency, need_wei, slippage_pct)


V4_TID_RE = None  # placeholder biar grep gampang


def mint_v4(chain_id: int, pk: str, pool_info: dict, budget: float,
            strategy: dict, slippage_pct: float) -> dict:
    """Mint posisi v4 via PositionManager.modifyLiquidities.
    Mode sama dengan v3 (lower/upper/wide/stable); budget satuan quote (upper: meme)."""
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
    if not verify_v4(w3, chain_id):
        raise RuntimeError("Kontrak V4 gagal verifikasi on-chain — batal.")
    # dict pool bisa datang dari indexer (API Uniswap) → pastikan PoolKey autentik
    # (hash == poolId) dan tanpa hooks sebelum dana bergerak.
    assert_pool_orientation(w3, pool_info)
    account = w3.eth.account.from_key(pk)
    posm_addr = Web3.to_checksum_address(cfg["v4_posm"])
    posm = _v4c(w3, chain_id, "v4_posm", V4_POSM_ABI)
    key = tuple(pool_info["key"])
    pid = v4_pool_id(key)
    spacing = pool_info["tick_spacing"]
    mode = strategy["mode"]
    q_is_t1 = pool_info["quote_is_token1"]
    quote = key[1] if q_is_t1 else key[0]
    meme = key[0] if q_is_t1 else key[1]
    qdec = pool_info["quote_decimals"]
    minfo = token_info(w3, meme)  # meme tidak pernah native
    steps = []

    # Range bebas: mode ditentukan oleh letak range terhadap harga.
    if strategy.get("ticks"):
        mode = effective_mode(int(strategy["ticks"][0]), int(strategy["ticks"][1]),
                              v4_slot0(w3, chain_id, pid)[1], q_is_t1)

    # ---- Fase 1: siapkan dana ----
    keep_wei = meme_got = dep_wei = 0
    if mode == "upper":
        dep_wei = int(Decimal(str(budget)) * Decimal(10) ** minfo["decimals"])
        if dep_wei <= 0:
            raise RuntimeError("Amount 0.")
        bal = erc20(w3, meme).functions.balanceOf(account.address).call()
        if bal < dep_wei:
            if dep_wei - bal <= dep_wei // 10000 + 1:
                dep_wei = bal  # selisih pembulatan float dari amount 100% — pakai saldo penuh
            else:
                raise RuntimeError(f"Saldo meme kurang: punya {bal / 10 ** minfo['decimals']:.6g}, butuh {budget}")
    else:
        budget_wei = int(Decimal(str(budget)) * Decimal(10) ** qdec)
        if budget_wei <= 0:
            raise RuntimeError("Amount 0.")
        steps += _v4_ensure_funds(w3, chain_id, pk, quote, budget_wei, slippage_pct)
        avail = _v4_balance(w3, quote, account.address)
        if quote.lower() == V4_NATIVE:
            avail = max(0, avail - w3.to_wei("0.0005", "ether"))
        budget_wei = min(budget_wei, avail)
        if mode in ("wide", "stable"):
            sqrtp, cur_tick = v4_slot0(w3, chain_id, pid)
            t_lo, t_hi, _ = _range_of(strategy, cur_tick, pool_info["fee"], q_is_t1, spacing)
            keep, _sw = plan_two_sided(sqrtp, t_lo, t_hi, budget_wei, q_is_t1)
            raw = (sqrtp / Q96) ** 2
            meme_price_q = raw if q_is_t1 else (1 / raw if raw else 0)
            meme_bal = erc20(w3, meme).functions.balanceOf(account.address).call()
            meme_val_q = int(meme_bal * meme_price_q)
            keep_frac = keep / budget_wei if budget_wei else 0
            quote_dep = min(int((budget_wei + meme_val_q) * keep_frac), budget_wei)
            swap_wei = max(0, budget_wei - quote_dep)
            swapped = False
            if swap_wei > budget_wei // 500:
                h = v4_swap(chain_id, pk, key, quote, swap_wei, slippage_pct)
                if h:
                    steps.append(("swap", h))
                    swapped = True
            keep_wei = quote_dep
            meme_got = poll_balance(w3, meme, account.address, meme_bal + 1) if swapped else meme_bal
        else:  # lower — quote saja
            dep_wei = budget_wei

    # ---- Fase 2: mint (retry 3×, harga dibaca ulang tiap attempt) ----
    receipt = None
    last_err = None
    for attempt in range(3):
        sqrtp, cur_tick = v4_slot0(w3, chain_id, pid)
        tick_lower, tick_upper, now_mode = _range_of(
            strategy, cur_tick, pool_info["fee"], q_is_t1, spacing)
        if now_mode != mode:
            raise RuntimeError(
                f"Harga bergerak melewati batas range saat transaksi disiapkan "
                f"(butuh sisi '{now_mode}', dana sudah disiapkan untuk '{mode}'). "
                f"Dana aman di wallet — atur ulang range lalu coba lagi.")
        if mode == "upper":
            a0d, a1d = (dep_wei, 0) if q_is_t1 else (0, dep_wei)
        elif mode in ("wide", "stable"):
            a0d, a1d = (meme_got, keep_wei) if q_is_t1 else (keep_wei, meme_got)
        else:
            a0d, a1d = (0, dep_wei) if q_is_t1 else (dep_wei, 0)
        lq = int(liquidity_for_amounts(sqrtp, tick_lower, tick_upper, a0d, a1d))
        if lq <= 0:
            raise RuntimeError("Liquidity terhitung 0 — cek amount / salah satu sisi kosong.")
        lq = lq - lq // 5000 - 1  # margin pembulatan: jumlah yang ditarik posm ≤ desired
        u0, u1 = amounts_from_liquidity(lq, sqrtp, tick_lower, tick_upper)
        a0max = min(int(u0 * (1 + slippage_pct / 100)) + 2, MAX_UINT128, max(a0d, 2))
        a1max = min(int(u1 * (1 + slippage_pct / 100)) + 2, MAX_UINT128, max(a1d, 2))
        for cur, amax in ((key[0], a0max), (key[1], a1max)):
            if cur.lower() != V4_NATIVE and amax > 2:  # 2 wei = sisi kosong single-sided
                steps += ensure_permit2(w3, chain_id, pk, cur, posm_addr, amax)
        actions = [V4_MINT, V4_SETTLE_PAIR]
        p_mint = abi_encode(
            [_V4_POOLKEY_T, "int24", "int24", "uint256", "uint128", "uint128", "address", "bytes"],
            [key, tick_lower, tick_upper, lq, a0max, a1max, account.address, b""])
        params = [p_mint, abi_encode(["address", "address"], [key[0], key[1]])]
        value = 0
        if key[0].lower() == V4_NATIVE:
            value = a0max
            actions.append(V4_SWEEP)
            params.append(abi_encode(["address", "address"], [V4_NATIVE, account.address]))
        data = calldata(posm.functions.modifyLiquidities(
            _v4_unlock(actions, params), int(time.time()) + DEADLINE_SECS))
        tx = {"to": posm_addr, "data": data, "value": value}
        try:
            _preflight(w3, account.address, tx)
            h = send_tx(w3, pk, tx)
            receipt = wait_ok(w3, h, "mint v4")
            steps.append(("mint", h))
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2)
    if receipt is None:
        raise RuntimeError(f"Mint v4 gagal 3× (harga bergerak?). Detail: {last_err}")

    token_id = None
    for log in receipt.logs:
        if (log.address.lower() == posm_addr.lower() and len(log.topics) == 4
                and log.topics[0].hex().removeprefix("0x") == ERC721_TRANSFER_TOPIC.removeprefix("0x")):
            token_id = int(log.topics[3].hex(), 16)
            break

    qusd = pool_info["quote_usd"]
    raw = (sqrtp / Q96) ** 2
    mprice_q = raw if q_is_t1 else (1 / raw if raw else 0)
    uq, um = (u1, u0) if q_is_t1 else (u0, u1)
    deposited_usd = (uq + um * mprice_q) / 10 ** qdec * qusd
    deposit_sym = minfo["symbol"] if mode == "upper" else pool_info["quote_sym"]
    return {"token_id": token_id, "steps": steps, "mode": mode,
            "tick_lower": tick_lower, "tick_upper": tick_upper, "cur_tick": cur_tick,
            "deposited": budget, "deposit_sym": deposit_sym, "deposited_usd": deposited_usd}


def _v4_tick_from_info(info: int) -> tuple[int, int]:
    """PositionInfo packed: [200b poolId][24b tickUpper][24b tickLower][8b flag]."""
    def s24(v):
        return v - 2 ** 24 if v >= 2 ** 23 else v
    return s24((info >> 8) & 0xFFFFFF), s24((info >> 32) & 0xFFFFFF)


def _v4_pending_fees(w3: Web3, chain_id: int, pid: bytes, tid: int,
                     lo: int, hi: int, liq: int) -> tuple[int, int]:
    """Fee unclaimed = liq × (feeGrowthInside sekarang − snapshot posisi) / 2^128."""
    cfg = CHAINS[chain_id]
    sv = _v4c(w3, chain_id, "v4_stateview", V4_STATEVIEW_ABI)
    fg0, fg1 = sv.functions.getFeeGrowthInside(pid, lo, hi).call()
    _, fg0l, fg1l = sv.functions.getPositionInfo(
        pid, Web3.to_checksum_address(cfg["v4_posm"]), lo, hi, tid.to_bytes(32, "big")).call()
    f0 = liq * ((fg0 - fg0l) % 2 ** 256) // 2 ** 128
    f1 = liq * ((fg1 - fg1l) % 2 ** 256) // 2 ** 128
    return f0, f1


def _v4_tvl_onchain(w3: Web3, chain_id: int, p: dict) -> float:
    """TVL pool v4 dari StateView: reserve virtual sisi quote x harga x 2.

    Saldo per-pool v4 TIDAK bisa dibaca (semua currency ditahan satu PoolManager),
    jadi ini perkiraan dari `liquidity` di harga sekarang — bukan reserve nyata.
    Dipakai hanya kalau dexscreener tidak meng-index poolId itu."""
    sv = _v4c(w3, chain_id, "v4_stateview", V4_STATEVIEW_ABI)
    sqrtp = sv.functions.getSlot0(p["pool_id"]).call()[0]
    liq = sv.functions.getLiquidity(p["pool_id"]).call()
    if not sqrtp or not liq:
        return 0.0
    q_is_c1 = bool(p.get("quote_is_token1"))
    qaddr = p["token1"] if q_is_c1 else p["token0"]
    qdec = _v4_currency_info(w3, chain_id, qaddr)["decimals"]
    qusd = quote_usd_price(w3, chain_id, p["quote_sym"])
    q_virt = (liq * sqrtp // Q96) if q_is_c1 else (liq * Q96 // sqrtp)
    return q_virt / 10 ** qdec * qusd * 2


def pool_stats(w3: Web3, chain_id: int, p: dict, _cache={}) -> dict:
    """Angka tingkat-POOL untuk kartu detail posisi: TVL, volume 24 jam, fee, kisi.

    Dihitung on-demand dan HANYA untuk kartu satu posisi — jangan dipanggil dari
    daftar posisi: TVL v4 butuh StateView dan volume butuh dexscreener, jadi biayanya
    per-posisi. Cache 60 detik.

    Semua nilainya boleh None dan semuanya angka TAMPILAN. Jangan pernah dipakai
    membangun transaksi — sumbernya indexer luar dan perkiraan reserve virtual."""
    ident = str(p.get("pool") or "").lower()
    ck = (chain_id, ident)
    hit = _cache.get(ck)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    ver = p.get("ver", 3)
    q_is_t1 = bool(p.get("quote_is_token1"))
    meme = p["token0"] if q_is_t1 else p["token1"]
    out = {"tvl_usd": None, "vol24_usd": None, "fee_pct": None,
           "tick_spacing": p.get("tick_spacing"), "dex": p.get("dex"), "tvl_src": None}
    try:
        out["fee_pct"] = int(p["fee"]) / 1e4
    except Exception:
        pass
    try:
        out["vol24_usd"] = dex_volumes(chain_id, meme).get(ident) or None
    except Exception:
        pass
    try:
        if ver == 4:
            tvl = _dexliq_of(chain_id, meme, ident)
            out["tvl_src"] = "dexscreener" if tvl > 0 else "chain"
            out["tvl_usd"] = (tvl or _v4_tvl_onchain(w3, chain_id, p)) or None
        else:
            # v2/v3: saldo NYATA kedua sisi di kontrak pool — lebih akurat daripada
            # angka indexer (terukur $24,4k untuk pool yang saldonya $40,7k)
            addr = Web3.to_checksum_address(p["pool"])
            qaddr = p["token1"] if q_is_t1 else p["token0"]
            qdec = p["dec1"] if q_is_t1 else p["dec0"]
            mdec = p["dec0"] if q_is_t1 else p["dec1"]
            qusd = quote_usd_price(w3, chain_id, p["quote_sym"])
            musd = token_usd_price(w3, chain_id, meme)
            qb = erc20(w3, qaddr).functions.balanceOf(addr).call() / 10 ** qdec
            mb = erc20(w3, meme).functions.balanceOf(addr).call() / 10 ** mdec
            out["tvl_src"] = "chain"
            out["tvl_usd"] = (qb * qusd + mb * musd) or None
    except Exception:
        pass
    _cache[ck] = (out, time.time())
    return out


def _v4_position_detail(w3: Web3, chain_id: int, tid: int, account_addr: str) -> dict | None:
    """None kalau posisi bukan milik wallet / sudah di-burn / kosong."""
    cfg = CHAINS[chain_id]
    posm = _v4c(w3, chain_id, "v4_posm", V4_POSM_ABI)
    try:
        if posm.functions.ownerOf(tid).call().lower() != account_addr.lower():
            return None
    except Exception:
        return None  # burned
    try:
        key, info = posm.functions.getPoolAndPositionInfo(tid).call()
        key = tuple(key)
        tick_lo, tick_hi = _v4_tick_from_info(info)
        liq = posm.functions.getPositionLiquidity(tid).call()
        pid = v4_pool_id(key)
        sqrtp, cur_tick = v4_slot0(w3, chain_id, pid)
        f0 = f1 = 0
        if liq > 0:
            f0, f1 = _v4_pending_fees(w3, chain_id, pid, tid, tick_lo, tick_hi, liq)
        if liq == 0 and f0 == 0 and f1 == 0:
            return None

        i0 = _v4_currency_info(w3, chain_id, key[0])
        i1 = _v4_currency_info(w3, chain_id, key[1])
        a0_raw, a1_raw = amounts_from_liquidity(liq, sqrtp, tick_lo, tick_hi)
        qsym, q_is_t1 = _v4_quote_side(chain_id, key[0], key[1], w3)

        raw_price = (sqrtp / Q96) ** 2
        usd = unclaimed_usd = 0.0
        usd0 = usd1 = fees_usd0 = fees_usd1 = 0.0
        mc_lower = mc_upper = mc_now = None
        if qsym:
            qusd = quote_usd_price(w3, chain_id, qsym)
            if q_is_t1:
                qdec, mdec, meme_addr = i1["decimals"], i0["decimals"], key[0]
                meme_in_q = raw_price * 10 ** (mdec - qdec)
                usd0 = (a0_raw / 10 ** mdec) * meme_in_q * qusd
                usd1 = a1_raw / 10 ** qdec * qusd
                fees_usd0 = (f0 / 10 ** mdec) * meme_in_q * qusd
                fees_usd1 = f1 / 10 ** qdec * qusd
            else:
                qdec, mdec, meme_addr = i0["decimals"], i1["decimals"], key[1]
                meme_in_q = (1 / raw_price) * 10 ** (mdec - qdec) if raw_price else 0
                usd0 = a0_raw / 10 ** qdec * qusd
                usd1 = (a1_raw / 10 ** mdec) * meme_in_q * qusd
                fees_usd0 = f0 / 10 ** qdec * qusd
                fees_usd1 = (f1 / 10 ** mdec) * meme_in_q * qusd
            usd = usd0 + usd1
            unclaimed_usd = fees_usd0 + fees_usd1
            try:
                supply = token_supply(w3, meme_addr)

                def meme_q_at(t):
                    r = tick_to_price(t)
                    return (r if q_is_t1 else (1 / r if r else 0)) * 10 ** (mdec - qdec)
                mcs = sorted([meme_q_at(tick_lo) * qusd * supply, meme_q_at(tick_hi) * qusd * supply])
                mc_lower, mc_upper = mcs
                mc_now = meme_in_q * qusd * supply
            except Exception:
                pass

        return {
            "ver": 4, "pid": f"v4:{tid}", "token_id": f"v4:{tid}", "v4_tid": tid,
            "dex": v4_dex(chain_id), "key": key, "pool_id": pid,
            "token0": key[0], "token1": key[1], "sym0": i0["symbol"], "sym1": i1["symbol"],
            "dec0": i0["decimals"], "dec1": i1["decimals"], "fee": key[2],
            # tick_spacing WAJIB ikut: fee v4 bebas (58200 dst) sehingga tabel
            # TICK_SPACING tidak memuatnya, dan box_pct() jatuh ke default 60 —
            # presisi kisi yang ditampilkan jadi salah.
            "tick_spacing": key[3],
            "pool": "0x" + pid.hex().removeprefix("0x"),
            "tick_lower": tick_lo, "tick_upper": tick_hi, "cur_tick": cur_tick,
            "liquidity": liq, "amount0": a0_raw / 10 ** i0["decimals"], "amount1": a1_raw / 10 ** i1["decimals"],
            "fees0": f0 / 10 ** i0["decimals"], "fees1": f1 / 10 ** i1["decimals"],
            "in_range": tick_lo <= cur_tick < tick_hi,
            "value_usd": usd, "unclaimed_usd": unclaimed_usd,
            "usd0": usd0, "usd1": usd1, "fees_usd0": fees_usd0, "fees_usd1": fees_usd1,
            "quote_sym": qsym, "quote_is_token1": q_is_t1,
            "mc_lower": mc_lower, "mc_upper": mc_upper, "mc_now": mc_now,
        }
    except Exception:
        return None


def increase_v4(chain_id: int, pk: str, tid: int, budget_quote: float,
                slippage_pct: float) -> dict:
    """Tambah dana ke posisi v4. Komposisi mengikuti range vs harga (sama seperti v3)."""
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
    if not verify_v4(w3, chain_id):
        raise RuntimeError("Kontrak V4 gagal verifikasi on-chain — batal.")
    account = w3.eth.account.from_key(pk)
    posm_addr = Web3.to_checksum_address(cfg["v4_posm"])
    posm = _v4c(w3, chain_id, "v4_posm", V4_POSM_ABI)
    key, info = posm.functions.getPoolAndPositionInfo(tid).call()
    key = tuple(key)
    tick_lo, tick_hi = _v4_tick_from_info(info)
    pid = v4_pool_id(key)
    qsym, q_is_t1 = _v4_quote_side(chain_id, key[0], key[1], w3)
    if not qsym:
        raise RuntimeError("Pair tanpa quote yang dikenal bot.")
    quote = key[1] if q_is_t1 else key[0]
    meme = key[0] if q_is_t1 else key[1]
    qinfo = _v4_currency_info(w3, chain_id, quote)
    minfo = token_info(w3, meme)
    qdec = qinfo["decimals"]
    budget_wei = int(Decimal(str(budget_quote)) * Decimal(10) ** qdec)
    if budget_wei <= 0:
        raise RuntimeError("Amount 0.")

    steps = _v4_ensure_funds(w3, chain_id, pk, quote, budget_wei, slippage_pct)
    avail = _v4_balance(w3, quote, account.address)
    if quote.lower() == V4_NATIVE:
        avail = max(0, avail - w3.to_wei("0.0005", "ether"))
    budget_wei = min(budget_wei, avail)

    sqrtp, _ = v4_slot0(w3, chain_id, pid)
    keep_wei, _sw = plan_two_sided(sqrtp, tick_lo, tick_hi, budget_wei, q_is_t1)
    raw = (sqrtp / Q96) ** 2
    meme_price_q = raw if q_is_t1 else (1 / raw if raw else 0)
    meme_bal = erc20(w3, meme).functions.balanceOf(account.address).call()
    meme_val_q = int(meme_bal * meme_price_q)
    keep_frac = keep_wei / budget_wei if budget_wei else 0
    quote_dep = min(int((budget_wei + meme_val_q) * keep_frac), budget_wei)
    swap_wei = max(0, budget_wei - quote_dep)
    swapped = False
    if swap_wei > budget_wei // 500:
        h = v4_swap(chain_id, pk, key, quote, swap_wei, slippage_pct)
        if h:
            steps.append(("swap", h))
            swapped = True
    meme_have = poll_balance(w3, meme, account.address, meme_bal + 1) if swapped else meme_bal

    receipt = None
    last_err = None
    for attempt in range(3):
        sqrtp, _ = v4_slot0(w3, chain_id, pid)
        a0d, a1d = (meme_have, quote_dep) if q_is_t1 else (quote_dep, meme_have)
        lq = int(liquidity_for_amounts(sqrtp, tick_lo, tick_hi, a0d, a1d))
        if lq <= 0:
            raise RuntimeError(
                "Liquidity terhitung 0 — posisi in-range butuh dua sisi tapi salah satu kosong.")
        lq = lq - lq // 5000 - 1
        u0, u1 = amounts_from_liquidity(lq, sqrtp, tick_lo, tick_hi)
        a0max = min(int(u0 * (1 + slippage_pct / 100)) + 2, MAX_UINT128, max(a0d, 2))
        a1max = min(int(u1 * (1 + slippage_pct / 100)) + 2, MAX_UINT128, max(a1d, 2))
        for cur, amax in ((key[0], a0max), (key[1], a1max)):
            if cur.lower() != V4_NATIVE and amax > 2:  # 2 wei = sisi kosong single-sided
                steps += ensure_permit2(w3, chain_id, pk, cur, posm_addr, amax)
        actions = [V4_INCREASE, V4_SETTLE_PAIR]
        p_inc = abi_encode(["uint256", "uint256", "uint128", "uint128", "bytes"],
                           [tid, lq, a0max, a1max, b""])
        params = [p_inc, abi_encode(["address", "address"], [key[0], key[1]])]
        value = 0
        if key[0].lower() == V4_NATIVE:
            value = a0max
            actions.append(V4_SWEEP)
            params.append(abi_encode(["address", "address"], [V4_NATIVE, account.address]))
        data = calldata(posm.functions.modifyLiquidities(
            _v4_unlock(actions, params), int(time.time()) + DEADLINE_SECS))
        tx = {"to": posm_addr, "data": data, "value": value}
        try:
            _preflight(w3, account.address, tx)
            h = send_tx(w3, pk, tx)
            receipt = wait_ok(w3, h, "increase v4")
            steps.append(("increase", h))
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2)
    if receipt is None:
        raise RuntimeError(f"Add v4 gagal 3×. Detail: {last_err}")

    qusd = quote_usd_price(w3, chain_id, qsym)
    uq, um = (u1, u0) if q_is_t1 else (u0, u1)
    added_usd = (uq + um * meme_price_q) / 10 ** qdec * qusd
    return {"steps": steps, "added_usd": added_usd, "quote_sym": qsym,
            "quote_in": uq / 10 ** qdec, "meme_in": um / 10 ** minfo["decimals"],
            "meme_sym": minfo["symbol"], "quote_dep": quote_dep / 10 ** qdec}


def _v4_modify(w3, chain_id, pk, posm, actions, params, what) -> str:
    account = w3.eth.account.from_key(pk)
    data = calldata(posm.functions.modifyLiquidities(
        _v4_unlock(actions, params), int(time.time()) + DEADLINE_SECS))
    tx = {"to": Web3.to_checksum_address(CHAINS[chain_id]["v4_posm"]), "data": data}
    _preflight(w3, account.address, tx)
    h = send_tx(w3, pk, tx)
    wait_ok(w3, h, what)
    return h


def decrease_v4(chain_id: int, pk: str, tid: int, pct: int, slippage_pct: float) -> dict:
    """Kurangi posisi v4 pct% (fee unclaimed ikut terambil — v4 menyetor fee
    setiap modifyLiquidity). Mins ber-slippage dari harga sekarang."""
    if not 1 <= pct <= 99:
        raise RuntimeError("Persen harus 1–99 (100% = pakai Close).")
    w3 = get_w3(chain_id)
    if not verify_v4(w3, chain_id):
        raise RuntimeError("Kontrak V4 gagal verifikasi on-chain — batal.")
    account = w3.eth.account.from_key(pk)
    posm = _v4c(w3, chain_id, "v4_posm", V4_POSM_ABI)
    key, info = posm.functions.getPoolAndPositionInfo(tid).call()
    key = tuple(key)
    tick_lo, tick_hi = _v4_tick_from_info(info)
    liq = posm.functions.getPositionLiquidity(tid).call()
    part = liq * pct // 100
    if part == 0:
        raise RuntimeError("Liquidity 0 — posisi sudah kosong.")
    pid = v4_pool_id(key)
    sqrtp, _ = v4_slot0(w3, chain_id, pid)
    u0, u1 = amounts_from_liquidity(part, sqrtp, tick_lo, tick_hi)
    slip = (100 - slippage_pct) / 100
    p_dec = abi_encode(["uint256", "uint256", "uint128", "uint128", "bytes"],
                       [tid, part, int(u0 * slip), int(u1 * slip), b""])
    p_take = abi_encode(["address", "address", "address"], [key[0], key[1], account.address])
    h = _v4_modify(w3, chain_id, pk, posm, [V4_DECREASE, V4_TAKE_PAIR], [p_dec, p_take], "decrease v4")
    f0, f1 = _v4_pending_fees(w3, chain_id, pid, tid, tick_lo, tick_hi, liq) if liq else (0, 0)
    i0 = _v4_currency_info(w3, chain_id, key[0])
    i1 = _v4_currency_info(w3, chain_id, key[1])
    return {"steps": [("decrease", h)],
            "got0": (u0 + f0) / 10 ** i0["decimals"], "got1": (u1 + f1) / 10 ** i1["decimals"],
            "sym0": i0["symbol"], "sym1": i1["symbol"]}


def collect_v4(chain_id: int, pk: str, tid: int) -> dict:
    """Klaim fee posisi v4: DECREASE_LIQUIDITY 0 + TAKE_PAIR."""
    w3 = get_w3(chain_id)
    if not verify_v4(w3, chain_id):
        raise RuntimeError("Kontrak V4 gagal verifikasi on-chain — batal.")
    account = w3.eth.account.from_key(pk)
    posm = _v4c(w3, chain_id, "v4_posm", V4_POSM_ABI)
    key, info = posm.functions.getPoolAndPositionInfo(tid).call()
    key = tuple(key)
    tick_lo, tick_hi = _v4_tick_from_info(info)
    liq = posm.functions.getPositionLiquidity(tid).call()
    pid = v4_pool_id(key)
    f0, f1 = _v4_pending_fees(w3, chain_id, pid, tid, tick_lo, tick_hi, liq) if liq else (0, 0)
    if f0 == 0 and f1 == 0:
        raise RuntimeError("Tidak ada fee untuk diklaim.")
    p_dec = abi_encode(["uint256", "uint256", "uint128", "uint128", "bytes"],
                       [tid, 0, 0, 0, b""])
    p_take = abi_encode(["address", "address", "address"], [key[0], key[1], account.address])
    h = _v4_modify(w3, chain_id, pk, posm, [V4_DECREASE, V4_TAKE_PAIR], [p_dec, p_take], "collect v4")
    i0 = _v4_currency_info(w3, chain_id, key[0])
    i1 = _v4_currency_info(w3, chain_id, key[1])
    return {"steps": [("collect", h)],
            "got0": f0 / 10 ** i0["decimals"], "got1": f1 / 10 ** i1["decimals"],
            "sym0": i0["symbol"], "sym1": i1["symbol"]}


def close_v4(chain_id: int, pk: str, tid: int, slippage_pct: float, autoswap: bool) -> dict:
    """Full exit v4: BURN_POSITION + TAKE_PAIR (principal + fee sekaligus),
    lalu auto-swap meme → quote pool via UR. Quote native = terima ETH langsung."""
    w3 = get_w3(chain_id)
    if not verify_v4(w3, chain_id):
        raise RuntimeError("Kontrak V4 gagal verifikasi on-chain — batal.")
    account = w3.eth.account.from_key(pk)
    posm = _v4c(w3, chain_id, "v4_posm", V4_POSM_ABI)
    key, info = posm.functions.getPoolAndPositionInfo(tid).call()
    key = tuple(key)
    tick_lo, tick_hi = _v4_tick_from_info(info)
    liq = posm.functions.getPositionLiquidity(tid).call()
    pid = v4_pool_id(key)
    sqrtp, _ = v4_slot0(w3, chain_id, pid)
    u0, u1 = amounts_from_liquidity(liq, sqrtp, tick_lo, tick_hi)
    f0, f1 = _v4_pending_fees(w3, chain_id, pid, tid, tick_lo, tick_hi, liq) if liq else (0, 0)
    slip = (100 - slippage_pct) / 100
    i0 = _v4_currency_info(w3, chain_id, key[0])
    i1 = _v4_currency_info(w3, chain_id, key[1])
    qsym, q_is_t1 = _v4_quote_side(chain_id, key[0], key[1], w3)
    meme = key[0] if q_is_t1 else key[1]

    pre_meme = erc20(w3, meme).functions.balanceOf(account.address).call() if meme.lower() != V4_NATIVE else 0

    p_burn = abi_encode(["uint256", "uint128", "uint128", "bytes"],
                        [tid, int(u0 * slip), int(u1 * slip), b""])
    p_take = abi_encode(["address", "address", "address"], [key[0], key[1], account.address])
    h = _v4_modify(w3, chain_id, pk, posm, [V4_BURN, V4_TAKE_PAIR], [p_burn, p_take], "close v4")
    steps = [("burn", h)]

    swaps = []
    if autoswap and meme.lower() != V4_NATIVE:
        got_meme = u0 + f0 if q_is_t1 else u1 + f1
        expected = pre_meme + int(got_meme * 0.9)
        bal = poll_balance(w3, meme, account.address, max(int(expected), 1))
        # hanya hasil close yang dijual; saldo meme yang sudah ada sebelumnya milik user
        bal = (bal - pre_meme) if pre_meme else bal
        msym = (i0 if q_is_t1 else i1)["symbol"]
        if bal == 0:
            swaps.append((msym, "SWAP GAGAL: saldo terbaca 0 (RPC lag) — jual manual"))
        else:
            try:
                sh = v4_swap(chain_id, pk, key, meme, bal, slippage_pct)
                if sh:
                    swaps.append((msym, sh))
            except Exception as e:
                swaps.append((msym, f"SWAP GAGAL: {e}"))

    return {"steps": steps, "swaps": swaps,
            "got0": (u0 + f0) / 10 ** i0["decimals"], "got1": (u1 + f1) / 10 ** i1["decimals"],
            "sym0": i0["symbol"], "sym1": i1["symbol"]}


# ══════════════════════════ Dispatcher lintas-versi ══════════════════════════
def dex_slug(d: str) -> str:
    return str(d or "").lower().replace(" ", "")


def parse_pid(pid) -> tuple[int, object]:
    """'183469' → (3, 183469) · 'v4:12' → (4, 12) · 'v2:0xabc' → (2, '0xabc')
    · 'uniswap:99' → (3, 99) di DEX non-utama."""
    s = str(pid)
    if s.startswith("v4:"):
        return 4, int(s[3:])
    if s.startswith("v2:"):
        return 2, s[3:]
    if ":" in s:
        return 3, int(s.split(":", 1)[1])
    return 3, int(s)


def pid_dex(chain_id: int, pid) -> str | None:
    """DEX pemilik posisi. v3 di DEX non-utama diberi awalan slug DEX karena tokenId
    NPM berbeda bisa bertabrakan. v2 dikembalikan None — pemiliknya dicari on-chain
    dari factory (alamat pair unik, jadi tidak ambigu)."""
    s = str(pid)
    if s.startswith("v4:"):
        return v4_dex(chain_id)
    if s.startswith("v2:"):
        return None
    if ":" in s:
        slug = s.split(":", 1)[0]
        for d in dex_names(chain_id):
            if dex_slug(d) == slug:
                return d
        return None
    return dex_name(chain_id)


def make_pid(chain_id: int, ver: int, ref, dex: str | None = None) -> str:
    """Bentuk pid. DEX utama tetap memakai format lama supaya history.json yang
    sudah ada tetap terbaca."""
    if ver == 4:
        return f"v4:{ref}"
    if ver == 2:
        return f"v2:{str(ref).lower()}"
    if dex and dex != dex_name(chain_id):
        return f"{dex_slug(dex)}:{ref}"
    return str(ref)


def list_all_positions(chain_id: int, pk: str, v2_refs: list[str] = (),
                       v4_refs: list[str] = (), full: bool = False) -> list[dict]:
    """Posisi v3 (enumerasi NPM) + v4/v2 (dari registry bot). v3 diberi ver/pid."""
    w3 = get_w3(chain_id)
    account = w3.eth.account.from_key(pk)
    out = []
    for dname in dex_names(chain_id):
        try:
            for p in list_positions(chain_id, pk, full=full, dex=dname):
                p.setdefault("ver", 3)
                p["dex"] = dname
                p["pid"] = make_pid(chain_id, 3, p["token_id"], dname)
                out.append(p)
        except Exception:
            continue
    for r in v4_refs:
        try:
            d = _v4_position_detail(w3, chain_id, int(r), account.address)
            if d:
                out.append(d)
        except Exception:
            continue
    for r in v2_refs:
        d = _v2_position_detail(w3, chain_id, r, account.address)
        if d:
            out.append(d)
    return out


def add_any(chain_id: int, pk: str, pid, budget_quote: float, slippage_pct: float) -> dict:
    ver, ref = parse_pid(pid)
    if ver == 3:
        return increase_position(chain_id, pk, ref, budget_quote, slippage_pct,
                                 dex=pid_dex(chain_id, pid))
    if ver == 4:
        return increase_v4(chain_id, pk, ref, budget_quote, slippage_pct)
    raise RuntimeError("Add posisi v2: paste alamat token lagi lalu pilih pool [v2] yang sama.")


def reduce_any(chain_id: int, pk: str, pid, pct: int, slippage_pct: float) -> dict:
    ver, ref = parse_pid(pid)
    if ver == 3:
        return decrease_position(chain_id, pk, ref, pct, dex=pid_dex(chain_id, pid))
    if ver == 4:
        return decrease_v4(chain_id, pk, ref, pct, slippage_pct)
    return reduce_v2(chain_id, pk, ref, pct, slippage_pct, autoswap=False)


def collect_any(chain_id: int, pk: str, pid) -> dict:
    ver, ref = parse_pid(pid)
    if ver == 3:
        return collect_fees(chain_id, pk, ref, dex=pid_dex(chain_id, pid))
    if ver == 4:
        return collect_v4(chain_id, pk, ref)
    raise RuntimeError("Fee LP v2 auto-compound ke dalam posisi — tidak ada yang bisa diklaim terpisah.")


def close_any(chain_id: int, pk: str, pid, slippage_pct: float, autoswap: bool) -> dict:
    ver, ref = parse_pid(pid)
    if ver == 3:
        return close_position(chain_id, pk, ref, slippage_pct, autoswap,
                              dex=pid_dex(chain_id, pid))
    if ver == 4:
        return close_v4(chain_id, pk, ref, slippage_pct, autoswap)
    return reduce_v2(chain_id, pk, ref, 100, slippage_pct, autoswap=autoswap)


# ══════════════════════════ Rebalance ══════════════════════════
def _span_to_pcts(span: int, mode: str) -> tuple[float, float]:
    """Konversi lebar range lama (tick) → (low_pct, up_pct) untuk strategi baru.
    wide = span dibagi dua sisi; lower/upper = span penuh satu sisi."""
    def dn(t):  # % turun untuk t tick ke bawah
        return (1 - 1.0001 ** -t) * 100
    def up(t):  # % naik untuk t tick ke atas
        return (1.0001 ** t - 1) * 100
    span = max(span, 2)
    if mode == "wide":
        half = span // 2
        return dn(half), up(half)
    if mode == "lower":
        return dn(span), 100.0
    return 50.0, up(span)  # upper


def _wallet_balance(w3: Web3, cur: str, addr: str) -> int:
    if cur.lower() == V4_NATIVE:
        return w3.eth.get_balance(addr)
    return erc20(w3, cur).functions.balanceOf(addr).call()


def _poll_wallet(w3: Web3, cur: str, addr: str, min_expected: int,
                 tries: int = 10, delay: float = 0.7) -> int:
    """poll_balance versi sadar-native — currency v4 bisa address(0), yang tidak
    punya kontrak ERC20 untuk di-balanceOf."""
    bal = 0
    for _ in range(tries):
        try:
            bal = _wallet_balance(w3, cur, addr)
        except Exception:
            bal = 0
        if bal >= min_expected:
            return bal
        time.sleep(delay)
    return bal


def rebalance_position(chain_id: int, pk: str, pid, mode: str, slippage_pct: float,
                       gap: int = 1) -> dict:
    """Close posisi → swap komposisi sesuai mode → mint ulang dengan lebar range
    sama, dipusatkan di harga sekarang. Fee unclaimed ikut ter-reinvest.
    Hanya dana HASIL posisi ini yang dipakai (delta saldo, bukan seluruh wallet)."""
    ver, ref = parse_pid(pid)
    if ver == 2:
        raise RuntimeError("Posisi v2 full-range — tidak perlu rebalance.")
    if mode not in ("wide", "lower", "upper"):
        raise RuntimeError("Mode rebalance: wide / lower / upper.")
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
    account = w3.eth.account.from_key(pk)

    # ---- baca posisi lama + susun pool_info untuk mint ulang ----
    if ver == 3:
        npm = w3.eth.contract(address=Web3.to_checksum_address(cfg["npm"]), abi=NPM_ABI)
        (_, _, t0, t1, fee, lo, hi, liq, _, _, _, _) = npm.functions.positions(ref).call()
        if liq == 0:
            raise RuntimeError("Liquidity 0 — posisi sudah kosong.")
        factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)
        pool_addr = factory.functions.getPool(t0, t1, fee).call()
        pool_c = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
        quotes_lc = {a.lower(): s for s, a in cfg["quotes"].items()}
        if t1.lower() in quotes_lc:
            qsym, q_is_t1 = quotes_lc[t1.lower()], True
        elif t0.lower() in quotes_lc:
            qsym, q_is_t1 = quotes_lc[t0.lower()], False
        else:
            raise RuntimeError("Pair tanpa quote yang dikenal bot.")
        quote = t1 if q_is_t1 else t0
        meme = t0 if q_is_t1 else t1
        qdec = token_info(w3, quote)["decimals"]
        try:
            spacing = pool_c.functions.tickSpacing().call()
        except Exception:
            spacing = TICK_SPACING.get(fee)
        s0 = pool_c.functions.slot0().call()
        pool_info = {"ver": 3, "pool": pool_addr, "fee": fee, "tick_spacing": spacing,
                     "quote_sym": qsym, "quote_addr": quote, "quote_decimals": qdec,
                     "quote_usd": quote_usd_price(w3, chain_id, qsym),
                     "tick": s0[1], "sqrtp": s0[0],
                     "token0": t0, "token1": t1, "quote_is_token1": q_is_t1}
    else:
        if not verify_v4(w3, chain_id):
            raise RuntimeError("Kontrak V4 gagal verifikasi on-chain — batal.")
        posm = _v4c(w3, chain_id, "v4_posm", V4_POSM_ABI)
        key, info = posm.functions.getPoolAndPositionInfo(ref).call()
        key = tuple(key)
        lo, hi = _v4_tick_from_info(info)
        liq = posm.functions.getPositionLiquidity(ref).call()
        if liq == 0:
            raise RuntimeError("Liquidity 0 — posisi sudah kosong.")
        qsym, q_is_t1 = _v4_quote_side(chain_id, key[0], key[1], w3)
        if not qsym:
            raise RuntimeError("Pair tanpa quote yang dikenal bot.")
        quote = key[1] if q_is_t1 else key[0]
        meme = key[0] if q_is_t1 else key[1]
        if quote.lower() == V4_NATIVE:
            qsym = cfg["native_symbol"]
        qinfo = _v4_currency_info(w3, chain_id, quote)
        qdec = qinfo["decimals"]
        pid4 = v4_pool_id(key)
        sqrtp, tick = v4_slot0(w3, chain_id, pid4)
        price_sym = qsym if quote.lower() != V4_NATIVE else cfg["wrapped_symbol"]
        pool_info = {"ver": 4, "dex": v4_dex(chain_id), "pool": "0x" + pid4.hex().removeprefix("0x"), "pool_id": pid4,
                     "key": key, "fee": key[2], "tick_spacing": key[3],
                     "quote_sym": qsym, "quote_addr": quote, "quote_decimals": qdec,
                     "quote_usd": quote_usd_price(w3, chain_id, price_sym),
                     "tick": tick, "sqrtp": sqrtp,
                     "token0": key[0], "token1": key[1], "quote_is_token1": q_is_t1}
    minfo = token_info(w3, meme)
    span = hi - lo
    if span >= 400_000:
        raise RuntimeError("Range posisi hampir full-range — tidak akan pernah keluar range, "
                           "rebalance tidak berguna.")

    # ---- close (tanpa autoswap; komposisi diatur di bawah) ----
    pre_q = _wallet_balance(w3, quote, account.address)
    pre_m = erc20(w3, meme).functions.balanceOf(account.address).call()
    if ver == 3:
        closed = close_position(chain_id, pk, ref, slippage_pct, autoswap=False)
    else:
        closed = close_v4(chain_id, pk, ref, slippage_pct, autoswap=False)
    steps = list(closed["steps"])

    got_m = (closed["got0"] if q_is_t1 else closed["got1"]) * 10 ** minfo["decimals"]
    got_q = (closed["got1"] if q_is_t1 else closed["got0"]) * 10 ** pool_info["quote_decimals"]
    # Sisi QUOTE dulu tidak pernah ditunggu: posisi single-sided (mode Lower) pulang
    # 100% quote, jadi got_m == 0 dan tidak ada polling sama sekali. Replika RPC yang
    # telat menjawab saldo pra-close bikin kedua delta 0 dan rebalance batal padahal
    # close-nya sudah sukses.
    if got_q > 0:
        _poll_wallet(w3, quote, account.address, pre_q + int(got_q * 0.9))
    if got_m > 0:
        poll_balance(w3, meme, account.address, pre_m + int(got_m * 0.9))
    m_delta = q_delta = 0
    for _ in range(8):   # replika bisa telat beberapa detik — jangan menyerah sekali baca
        m_delta = max(0, erc20(w3, meme).functions.balanceOf(account.address).call() - pre_m)
        q_delta = max(0, _wallet_balance(w3, quote, account.address) - pre_q)  # native: minus gas
        if q_delta or m_delta:
            break
        time.sleep(1.5)
    if q_delta == 0 and m_delta == 0:
        raise RuntimeError("Hasil close terbaca 0 (RPC lag) — dana aman di wallet, mint manual saja.")

    # ---- swap komposisi sesuai mode (hanya dana hasil close) ----
    sqrtp_now = (pool_info["sqrtp"] if ver == 3 else v4_slot0(w3, chain_id, pool_info["pool_id"])[0])
    raw = (sqrtp_now / Q96) ** 2
    mprice_q = raw if q_is_t1 else (1 / raw if raw else 0)  # quote-wei per meme-wei

    def do_swap(token_in, token_out, amt_wei):
        if amt_wei <= 0:
            return
        if ver == 3:
            h = swap_to_token(chain_id, pk, token_in, token_out, pool_info["fee"], amt_wei, slippage_pct)
        else:
            h = v4_swap(chain_id, pk, pool_info["key"], token_in, amt_wei, slippage_pct)
        if h:
            steps.append(("swap", h))

    low_pct, up_pct = _span_to_pcts(span, mode)
    if mode == "lower" and m_delta > 0:
        do_swap(meme, quote, m_delta)  # lower = 100% quote
    elif mode == "upper" and q_delta > 0:
        keep_gas = w3.to_wei("0.0005", "ether") if quote.lower() == V4_NATIVE else 0
        do_swap(quote, meme, max(0, q_delta - keep_gas))  # upper = 100% meme
    elif mode == "wide":
        # sisi meme berlebih → jual kelebihannya ke quote (arah quote→meme diurus mesin mint)
        cur_tick_now = pool_info["tick"] if ver == 3 else v4_slot0(w3, chain_id, pool_info["pool_id"])[1]
        sp_ = pool_info["tick_spacing"] or TICK_SPACING.get(pool_info["fee"], 60)
        t_lo, t_hi = calc_strategy_range(cur_tick_now, pool_info["fee"], q_is_t1, "wide",
                                         low_pct, up_pct, gap, spacing=sp_)
        total_q = q_delta + int(m_delta * mprice_q)
        keep, _sw = plan_two_sided(sqrtp_now, t_lo, t_hi, max(total_q, 1), q_is_t1)
        need_m_q = max(total_q, 1) - keep          # nilai sisi meme yang dibutuhkan (quote-wei)
        have_m_q = int(m_delta * mprice_q)
        excess_q = have_m_q - need_m_q
        if excess_q > total_q // 100 and mprice_q > 0:
            do_swap(meme, quote, int(excess_q / mprice_q))

    # Nilai USD yang BENAR-BENAR keluar dari posisi (principal + fee — burn v4 dan
    # decrease+collect v3 sama-sama menarik keduanya). Dipakai pemanggil sebagai
    # cadangan pencatatan PnL kalau snapshot posisi lama gagal dibaca.
    closed_usd = 0.0
    try:
        closed_usd = ((q_delta + (m_delta * mprice_q if mprice_q else 0)) / 10 ** qdec
                      * pool_info["quote_usd"])
    except Exception:
        closed_usd = 0.0

    # ---- baca ulang delta setelah swap → budget mint (hanya proceeds) ----
    time.sleep(1)
    q_delta = max(0, _wallet_balance(w3, quote, account.address) - pre_q)
    m_delta = max(0, erc20(w3, meme).functions.balanceOf(account.address).call() - pre_m)
    if mode == "upper":
        budget = m_delta / 10 ** minfo["decimals"]
    else:
        if quote.lower() == V4_NATIVE:
            q_delta = max(0, q_delta - w3.to_wei("0.0005", "ether"))
        budget = q_delta / 10 ** qdec
    if budget <= 0:
        raise RuntimeError("Budget mint 0 setelah close+swap — dana aman di wallet, mint manual saja.")

    strategy = {"mode": mode, "low_pct": low_pct, "up_pct": up_pct, "gap": gap}
    if ver == 3:
        r = mint_position(chain_id, pk, pool_info, budget, strategy, slippage_pct)
    else:
        r = mint_v4(chain_id, pk, pool_info, budget, strategy, slippage_pct)
    steps += r["steps"]

    return {"ver": ver, "old_ref": ref, "steps": steps,
            "closed_got0": closed["got0"], "closed_got1": closed["got1"],
            "closed_sym0": closed["sym0"], "closed_sym1": closed["sym1"],
            "closed_usd": closed_usd,
            "token_id": r["token_id"], "mode": mode,
            "tick_lower": r["tick_lower"], "tick_upper": r["tick_upper"],
            "cur_tick": r["cur_tick"], "deposited": r["deposited"],
            "deposit_sym": r["deposit_sym"], "deposited_usd": r["deposited_usd"],
            "quote_sym": pool_info["quote_sym"], "meme_sym": minfo["symbol"]}
