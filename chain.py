"""
chain.py — Web3 core untuk LP bot: discovery pool, mint single-sided,
listing posisi, close, dan auto-swap. Uniswap V3 di Robinhood (4663) + BSC (56).
"""
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import urlparse

import requests
from eth_abi import encode as abi_encode
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from web3 import Web3
from web3.exceptions import ContractLogicError

Q96 = 2**96
MAX_UINT128 = 2**128 - 1
MAX_UINT256 = 2**256 - 1
TICK_SPACING = {100: 1, 500: 10, 3000: 60, 10000: 200}
MIN_TICK, MAX_TICK = -887272, 887272
DEADLINE_SECS = 1200

CHAINS = {
    4663: {
        "name": "Robinhood",
        "slug": "robinhood",  # slug URL app.uniswap.org
        "dexscreener": "robinhood",
        "gecko": "robinhood",
        "gmgn": "robinhood",
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
    56: {
        "name": "BSC",
        "slug": "bnb",
        "dexscreener": "bsc",
        "gecko": "bsc",
        "gmgn": "bsc",
        "v2_factory": "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",  # Uniswap V2 BSC
        "rpcs": [
            "https://1rpc.io/bnb",
            "https://bsc-dataseed.bnbchain.org",
            "https://bsc-rpc.publicnode.com",
        ],
        "alchemy": "bnb-mainnet",
        "rpc_env": "RPC_56",
        "explorer": "https://bscscan.com",
        "factory": "0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7",
        "npm": "0x7b8A01B39D58278b5DE7e48c8449c9f4F5170613",
        # SwapRouter02 Uniswap di BSC (docs.uniswap.org deployments)
        "router": "0xB971eF87ede563556b2ED4b1C0b0019111Dd85d2",
        # V2 router — diverifikasi on-chain: factory()==v2_factory, WETH()==wrapped
        "v2_router": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        # Uniswap V4 (diverifikasi on-chain, sama seperti Robinhood)
        "v4_pm": "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df",
        "v4_posm": "0x7a4a5c919ae2541aed11041a1aeee68f1287f95b",
        "v4_stateview": "0xd13dd3d6e93f276fafc9db9e6bb47c1180aee0c4",
        "v4_quoter": "0x9f75dd27d6664c475b90e105573e550ff69437b0",
        "v4_router": "0x1906c1d672b88cd1b9ac7593301ca990f94eae07",
        "permit2": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
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
]

FACTORY_ABI = [
    {"constant": True, "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "address"}, {"name": "", "type": "uint24"}], "name": "getPool", "outputs": [{"name": "", "type": "address"}], "type": "function", "stateMutability": "view"},
]

POOL_ABI = [
    {"constant": True, "inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"},
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


def _rpc_session() -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=_rpc_retry()))
    s.mount("http://", HTTPAdapter(max_retries=_rpc_retry()))
    return s


# ---------- Bypass blokir DNS ISP (DNS-over-HTTPS + koneksi langsung ke IP) ----------
class _SNIAdapter(HTTPAdapter):
    """Konek ke IP tapi SNI + verifikasi cert tetap pakai hostname asli."""

    def __init__(self, hostname: str):
        self._hostname = hostname
        super().__init__(max_retries=_rpc_retry())

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
    return Web3(provider)


# ---------- Koneksi & util dasar ----------
_W3_CACHE: dict[int, tuple[Web3, float]] = {}
_NONCE_NEXT: dict[str, int] = {}  # alamat → nonce berikutnya (pelacak lokal utk tx beruntun)


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
        candidates = [Web3(provider)]
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


def send_tx(w3: Web3, pk: str, tx: dict) -> str:
    account = w3.eth.account.from_key(pk)
    tx["to"] = Web3.to_checksum_address(tx["to"])
    tx["from"] = account.address
    tx["chainId"] = w3.eth.chain_id
    tx.setdefault("value", 0)
    try:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.3)
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
        n = max(rpc_n, min(_NONCE_NEXT.get(addr_lc, 0), rpc_n + 3))
        tx["nonce"] = n
        signed = w3.eth.account.sign_transaction(tx, pk)
        try:
            h = w3.eth.send_raw_transaction(signed.raw_transaction)
            _NONCE_NEXT[addr_lc] = n + 1
            return "0x" + h.hex().removeprefix("0x")
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


def wait_ok(w3: Web3, txhash: str, what: str):
    r = w3.eth.wait_for_transaction_receipt(txhash, timeout=180)
    if r.status != 1:
        raise RuntimeError(f"Tx {what} FAILED: {txhash}")
    return r


def tx_link(chain_id: int, h: str) -> str:
    return f"{CHAINS[chain_id]['explorer']}/tx/{h}"


def pos_link(chain_id: int, token_id: int) -> str:
    return f"https://app.uniswap.org/positions/v3/{CHAINS[chain_id]['slug']}/{token_id}"


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


def quote_usd_price(w3: Web3, chain_id: int, quote_sym: str, _cache={}) -> float:
    """Harga USD 1 unit quote. Stable = 1. Wrapped native = dari pool wrapped/stable."""
    cfg = CHAINS[chain_id]
    if quote_sym in cfg["stable_syms"]:
        return 1.0
    key = (chain_id, quote_sym)
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)
    wrapped = Web3.to_checksum_address(cfg["wrapped"])
    for stable_sym in cfg["stable_syms"]:
        stable = Web3.to_checksum_address(cfg["quotes"][stable_sym])
        t0, t1 = sorted([wrapped, stable])
        for fee in (500, 3000, 100, 10000):
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
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}", timeout=8)
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


def dex_vol30(chain_id: int, pool_addr: str, _cache={}) -> float | None:
    """Volume ~30 hari (jumlah candle harian GeckoTerminal; pool muda = sejak listing).
    None kalau tidak terindeks / rate limit."""
    cfg = CHAINS[chain_id]
    slug = cfg.get("gecko")
    if not slug:
        return None
    key = (chain_id, pool_addr.lower())
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < 900:
        return hit[0]
    try:
        r = requests.get(
            f"https://api.geckoterminal.com/api/v2/networks/{slug}/pools/{pool_addr}/ohlcv/day",
            params={"limit": 30}, timeout=10)
        if r.status_code != 200:
            return None  # 429/404 — jangan cache, coba lagi nanti
        candles = r.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        vol = float(sum(c[5] for c in candles)) if candles else None
    except Exception:
        return None
    _cache[key] = (vol, time.time())
    return vol


# ---------- Discovery pool ----------
def discover_pools(chain_id: int, token_addr: str) -> dict:
    """Scan semua quote × fee tier (paralel). Return {token, pools} urut TVL desc."""
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
    token = Web3.to_checksum_address(token_addr)
    factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)

    quotes = [(qsym, Web3.to_checksum_address(qaddr)) for qsym, qaddr in cfg["quotes"].items()
              if Web3.to_checksum_address(qaddr) != token]
    combos = [(qsym, q, fee) for qsym, q in quotes for fee in (100, 500, 3000, 10000)]

    with ThreadPoolExecutor(max_workers=5) as ex:
        tinfo_f = ex.submit(token_info, w3, token)
        qmeta_f = {qsym: (ex.submit(token_info, w3, q),
                          ex.submit(quote_usd_price, w3, chain_id, qsym))
                   for qsym, q in quotes}
        addr_futs = [(c, ex.submit(factory.functions.getPool(*sorted([token, c[1]]) + [c[2]]).call))
                     for c in combos]
        found = []
        for (qsym, q, fee), fut in addr_futs:
            try:
                pool_addr = fut.result()
            except Exception:
                continue
            if int(pool_addr, 16) != 0:
                found.append((qsym, q, fee, pool_addr))

        def detail(item):
            qsym, q, fee, pool_addr = item
            pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
            slot0 = pool.functions.slot0().call()
            liq = pool.functions.liquidity().call()
            qdec = qmeta_f[qsym][0].result()["decimals"]
            qusd = qmeta_f[qsym][1].result()
            q_bal = erc20(w3, q).functions.balanceOf(pool_addr).call() / 10 ** qdec
            t0, t1 = sorted([token, q])
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
            return {
                "ver": 3, "pool": pool_addr, "fee": fee, "quote_sym": qsym, "quote_addr": q,
                "quote_decimals": qdec, "quote_usd": qusd,
                "tick": slot0[1], "sqrtp": slot0[0], "liquidity": liq,
                "tvl_usd": q_bal * qusd + m_usd,
                "token0": t0, "token1": t1, "quote_is_token1": q_is_t1,
            }

        vols_f = ex.submit(dex_volumes, chain_id, token)
        v2_f = ex.submit(discover_v2_pools, w3, chain_id, token)
        v4_f = ex.submit(discover_v4_pools, w3, chain_id, token)
        pools = []
        for fut in [ex.submit(detail, it) for it in found]:
            try:
                pools.append(fut.result())
            except Exception:
                continue
        for fut in (v2_f, v4_f):
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
    cfg = CHAINS[chain_id]
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
        if float((pr.get("liquidity") or {}).get("usd") or 0) < 50:
            continue
        if "v3" in labels and addr.lower() not in skip_v3 and n3 < 6:
            cands.append(("v3", addr))
            n3 += 1
        elif "v4" in labels and len(addr) == 66 and addr.lower() not in skip_v4 and n4 < 8:
            cands.append(("v4", addr))
            n4 += 1

    def build(item):
        kind, addr = item
        try:
            if kind == "v3":
                pool = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=POOL_ABI)
                t0, t1 = pool.functions.token0().call(), pool.functions.token1().call()
                fee = pool.functions.fee().call()
                # otentikasi: alamat harus terdaftar di factory Uniswap
                if factory.functions.getPool(t0, t1, fee).call().lower() != addr.lower():
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
                if q_bal * qusd * 2 < 10:   # gerbang aman tetap pakai sisi quote saja
                    return None
                meme = t0 if q_is_t1 else t1
                mdec = token_info(w3, meme)["decimals"]
                raw = (slot0[0] / Q96) ** 2
                meme_in_q = (raw if q_is_t1 else (1 / raw if raw else 0)) * 10 ** (mdec - qdec)
                m_bal = erc20(w3, meme).functions.balanceOf(addr).call() / 10 ** mdec
                return {
                    "ver": 3, "pool": Web3.to_checksum_address(addr), "fee": fee,
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
                qsym4, q_is_c1 = _v4_quote_side(chain_id, key[0], key[1])
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
                if tvl < 10:
                    return None
                probe = int(min(100 / qusd if qusd else 0,
                                q_virt / 10 ** qinfo["decimals"] / 100 or 1) * 10 ** qinfo["decimals"]) or 1
                if not v4_roundtrip_ok(w3, chain_id, key, q_is_c1, probe):
                    return None
                return {
                    "ver": 4, "pool": "0x" + pid.hex().removeprefix("0x"), "pool_id": pid,
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
def find_pool(w3: Web3, chain_id: int, a: str, b: str) -> tuple[str | None, int]:
    """Cari pool v3 pasangan (a, b) — pilih yang saldo sisi-a terbesar
    (pool dust bisa nyimpan harga ngaco). Return (pool_addr, fee)."""
    cfg = CHAINS[chain_id]
    factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)
    a = Web3.to_checksum_address(a)
    t0, t1 = sorted([a, Web3.to_checksum_address(b)])
    best, best_fee, best_bal = None, 0, -1
    for f in (100, 500, 3000, 10000):
        addr = factory.functions.getPool(t0, t1, f).call()
        if int(addr, 16) == 0:
            continue
        try:
            bal = erc20(w3, a).functions.balanceOf(addr).call()
        except Exception:
            continue
        if bal > best_bal:
            best, best_fee, best_bal = addr, f, bal
    return best, best_fee


def wrapped_per_quote_wei(w3: Web3, chain_id: int, quote_addr: str) -> float:
    """Kurs wei wrapped per wei quote via pool wrapped/quote v3."""
    cfg = CHAINS[chain_id]
    wrapped = Web3.to_checksum_address(cfg["wrapped"])
    quote = Web3.to_checksum_address(quote_addr)
    pool_addr, _ = find_pool(w3, chain_id, wrapped, quote)
    if not pool_addr:
        raise RuntimeError(f"Tidak ada pool {cfg['wrapped_symbol']}/quote untuk konversi.")
    raw = _pool_price_t1_per_t0(w3, pool_addr)  # t1-wei per t0-wei
    t0, _ = sorted([wrapped, quote])
    return (1 / raw if raw else 0) if wrapped == t0 else raw


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
    gas_reserve = w3.to_wei("0.0005", "ether")

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

    # quote bukan wrapped: tutup kekurangan dengan swap wrapped → quote
    pool_addr, fee = find_pool(w3, chain_id, wrapped, quote)
    if not pool_addr:
        raise RuntimeError(
            f"Saldo quote kurang dan tidak ada pool {cfg['wrapped_symbol']}/quote untuk auto-swap.")
    rate = wrapped_per_quote_wei(w3, chain_id, quote)
    need_in = int(deficit * rate * 1.02)  # +2% margin biar hasil swap ≥ deficit
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
    h = swap_to_token(chain_id, pk, wrapped, quote, fee, need_in, slippage_pct)
    if h:
        txs.append(("swap→quote", h))
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


def mint_position(chain_id: int, pk: str, pool_info: dict, budget: float,
                  strategy: dict, slippage_pct: float) -> dict:
    """Mint LP sesuai strategi.
    strategy = {mode: lower|upper|wide|stable, low_pct, up_pct}
    budget dalam satuan QUOTE untuk lower/wide/stable, satuan MEME untuk upper."""
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
    account = w3.eth.account.from_key(pk)
    npm_addr = Web3.to_checksum_address(cfg["npm"])
    mode = strategy["mode"]

    quote = Web3.to_checksum_address(pool_info["quote_addr"])
    qdec = pool_info["quote_decimals"]
    q_is_t1 = pool_info["quote_is_token1"]
    meme = Web3.to_checksum_address(pool_info["token0"] if q_is_t1 else pool_info["token1"])
    mdec = token_info(w3, meme)["decimals"]

    pool = w3.eth.contract(address=Web3.to_checksum_address(pool_info["pool"]), abi=POOL_ABI)
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
    """Harga USD 1 meme via harga pool × harga USD quote."""
    pool = w3.eth.contract(address=Web3.to_checksum_address(pool_info["pool"]), abi=POOL_ABI)
    sp = pool.functions.slot0().call()[0]
    raw = (sp / Q96) ** 2  # token1 per token0
    q_is_t1 = pool_info["quote_is_token1"]
    meme = pool_info["token0"] if q_is_t1 else pool_info["token1"]
    mdec = token_info(w3, meme)["decimals"]
    qdec = pool_info["quote_decimals"]
    meme_in_q = raw * 10 ** (mdec - qdec) if q_is_t1 else (1 / raw) * 10 ** (mdec - qdec)
    return meme_in_q * pool_info["quote_usd"]


# ---------- Listing posisi ----------
def list_positions(chain_id: int, pk: str, max_positions: int = 40) -> list[dict]:
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
    account = w3.eth.account.from_key(pk)
    npm = w3.eth.contract(address=Web3.to_checksum_address(cfg["npm"]), abi=NPM_ABI)
    factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)

    n = npm.functions.balanceOf(account.address).call()
    idxs = list(range(n - 1, max(-1, n - 1 - max_positions), -1))  # terbaru dulu
    with ThreadPoolExecutor(max_workers=5) as ex:
        tids = list(ex.map(lambda i: npm.functions.tokenOfOwnerByIndex(account.address, i).call(), idxs))
        raws = list(ex.map(lambda t: npm.functions.positions(t).call(), tids))

        active = [(tid, p) for tid, p in zip(tids, raws)
                  if not (p[7] == 0 and p[10] == 0 and p[11] == 0)]
        results = list(ex.map(lambda tp: _position_detail(w3, chain_id, npm, factory, account, *tp), active))
    return [r for r in results if r]


def _position_detail(w3: Web3, chain_id: int, npm, factory, account, tid: int, p) -> dict | None:
    cfg = CHAINS[chain_id]
    try:
        (_, _, t0, t1, fee, tick_lo, tick_hi, liq, _, _, owed0, owed1) = p

        i0, i1 = token_info(w3, t0), token_info(w3, t1)
        pool_addr = factory.functions.getPool(t0, t1, fee).call()
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
        quotes_lc = {a.lower(): s for s, a in cfg["quotes"].items()}
        if t1.lower() in quotes_lc:
            qsym, q_is_t1 = quotes_lc[t1.lower()], True
        elif t0.lower() in quotes_lc:
            qsym, q_is_t1 = quotes_lc[t0.lower()], False
        else:
            qsym, q_is_t1 = None, True

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
        t0, t1 = sorted([token, q])
        for fee in (100, 500, 3000, 10000):
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
            r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=8)
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
                      slippage_pct: float) -> dict:
    """Tambah dana ke posisi yang ada. Budget dalam satuan quote; komposisi
    (quote/meme) dihitung otomatis dari posisi range vs harga sekarang —
    meme existing di wallet dipakai duluan, swap cuma nutup kekurangan."""
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
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


def decrease_position(chain_id: int, pk: str, token_id: int, pct: int) -> dict:
    """Kurangi posisi pct% (decrease + collect). Fee unclaimed ikut terambil.
    Token hasil pengurangan tetap di wallet (tanpa auto-swap)."""
    if not 1 <= pct <= 99:
        raise RuntimeError("Persen harus 1–99 (100% = pakai Close).")
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
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


def collect_fees(chain_id: int, pk: str, token_id: int) -> dict:
    """Klaim fee unclaimed saja — liquidity posisi tidak berubah.
    (NPM.collect nge-poke pool dulu kalau liquidity > 0, jadi fee ter-update.)"""
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
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
def verify_router(w3: Web3, chain_id: int, _cache={}) -> bool:
    """Cek router.factory() == factory chain sebelum swap pertama."""
    if chain_id in _cache:
        return _cache[chain_id]
    cfg = CHAINS[chain_id]
    try:
        r = w3.eth.contract(address=Web3.to_checksum_address(cfg["router"]), abi=ROUTER_ABI)
        ok = r.functions.factory().call().lower() == cfg["factory"].lower()
    except Exception:
        ok = False
    _cache[chain_id] = ok
    return ok


def swap_to_token(chain_id: int, pk: str, token_in: str, token_out: str, fee: int,
                  amount_in_wei: int, slippage_pct: float) -> str | None:
    """Swap exactInputSingle via SwapRouter02. Return txhash, None kalau skip."""
    if amount_in_wei <= 0:
        return None
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
    if not verify_router(w3, chain_id):
        raise RuntimeError("Router gagal verifikasi (factory mismatch) — auto-swap dibatalkan.")
    account = w3.eth.account.from_key(pk)
    token_in = Web3.to_checksum_address(token_in)
    token_out = Web3.to_checksum_address(token_out)

    factory = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI)
    t0, t1 = sorted([token_in, token_out])
    pool_addr = factory.functions.getPool(t0, t1, fee).call()
    if int(pool_addr, 16) == 0:
        for f in (500, 3000, 10000, 100):
            pool_addr = factory.functions.getPool(t0, t1, f).call()
            if int(pool_addr, 16) != 0:
                fee = f
                break
        else:
            raise RuntimeError("Pool untuk swap tidak ditemukan.")

    raw_price = _pool_price_t1_per_t0(w3, pool_addr)  # t1 per t0 raw
    if token_in == t0:
        out_est = amount_in_wei * raw_price
    else:
        out_est = amount_in_wei / raw_price if raw_price else 0
    min_out = int(out_est * (100 - slippage_pct) / 100)

    ensure_approval(w3, pk, token_in, cfg["router"], amount_in_wei)
    router = w3.eth.contract(address=Web3.to_checksum_address(cfg["router"]), abi=ROUTER_ABI)
    params = (token_in, token_out, fee, account.address, amount_in_wei, min_out, 0)
    h = send_tx(w3, pk, {"to": cfg["router"], "data": calldata(router.functions.exactInputSingle(params))})
    wait_ok(w3, h, "swap")
    return h


def close_position(chain_id: int, pk: str, token_id: int, slippage_pct: float,
                   autoswap: bool) -> dict:
    """Full exit: decreaseLiquidity(all) + collect(max), lalu auto-swap non-wrapped → wrapped."""
    w3 = get_w3(chain_id)
    cfg = CHAINS[chain_id]
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
            # jual SELURUH saldo token (termasuk sisa lama). Saldo dibaca dengan
            # polling: replika RPC bisa masih 0 sesaat setelah collect (toleransi
            # 90% untuk token fee-on-transfer).
            expected = pre + int(got * 0.9)
            bal = poll_balance(w3, taddr, account.address, max(expected, 1))
            if bal == 0:
                swaps.append((info["symbol"], "SWAP GAGAL: saldo terbaca 0 (RPC lag) — jual manual/close lagi"))
                continue
            try:
                sh = swap_to_token(chain_id, pk, taddr, wrapped, fee, bal, slippage_pct)
                if sh:
                    swaps.append((info["symbol"], sh))
            except Exception as e:
                swaps.append((info["symbol"], f"SWAP GAGAL: {e}"))

    return {
        "steps": steps, "swaps": swaps,
        "got0": got0 / 10 ** i0["decimals"], "got1": got1 / 10 ** i1["decimals"],
        "sym0": i0["symbol"], "sym1": i1["symbol"],
    }


# ══════════════════════════ Uniswap V2 ══════════════════════════
def verify_v2_router(w3: Web3, chain_id: int, _cache={}) -> bool:
    """Fail-closed: router.factory()==v2_factory dan router.WETH()==wrapped."""
    if chain_id in _cache:
        return _cache[chain_id]
    cfg = CHAINS[chain_id]
    try:
        r = w3.eth.contract(address=Web3.to_checksum_address(cfg["v2_router"]), abi=V2_ROUTER_ABI)
        ok = (r.functions.factory().call().lower() == cfg["v2_factory"].lower()
              and r.functions.WETH().call().lower() == cfg["wrapped"].lower())
    except Exception:
        ok = False
    _cache[chain_id] = ok
    return ok


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


def discover_v2_pools(w3: Web3, chain_id: int, token: str) -> list[dict]:
    """Pair v2 token × semua quote. Bentuk dict kompatibel pool_info v3 + ver=2."""
    cfg = CHAINS[chain_id]
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
            if tvl < 10:  # pair dust — harga tidak bisa dipercaya
                continue
            # round-trip lokal $100: pair yang tidak bisa serap swap kecil = dust/manipulasi
            probe = int(min(100 / qusd if qusd else 0, rq / 10 ** qdec / 100 or 1) * 10 ** qdec) or 1
            o1 = probe * 997 * rm // (rq * 1000 + probe * 997)
            back = o1 * 997 * rq // (rm * 1000 + o1 * 997)
            if back < probe * 70 // 100:
                continue
            t0, t1 = sorted([token, q])
            out.append({
                "ver": 2, "pool": pair, "fee": 3000, "quote_sym": qsym, "quote_addr": q,
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
    cfg = CHAINS[chain_id]
    if not verify_v2_router(w3, chain_id):
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
        est_out = router.functions.getAmountsOut(swap_in, [quote, meme]).call()[-1]
        min_out = int(est_out * slip)
        if min_out <= 0:
            raise RuntimeError("Estimasi hasil swap 0 — likuiditas pair terlalu tipis.")
        steps += ensure_approval(w3, pk, quote, router_addr, swap_in)
        data = calldata(router.functions.swapExactTokensForTokens(
            swap_in, min_out, [quote, meme], account.address, deadline))
        _preflight(w3, account.address, {"to": router_addr, "data": data})
        h = send_tx(w3, pk, {"to": router_addr, "data": data})
        wait_ok(w3, h, "swap v2")
        steps.append(("swap", h))
        swapped = True
    meme_have = poll_balance(w3, meme, account.address, meme_bal + 1) if swapped \
        else meme_bal
    if quote_keep <= 0 or meme_have <= 0:
        raise RuntimeError("Salah satu sisi 0 — v2 butuh dua sisi (quote + meme).")

    rq, rm = _v2_pair_reserves(w3, pair, quote)  # fresh setelah swap
    meme_need = quote_keep * rm // rq
    meme_desired = min(meme_have, meme_need + meme_need // 100 + 1)
    a_min = int(quote_keep * slip)
    b_min = int(min(meme_need, meme_desired) * slip)
    steps += ensure_approval(w3, pk, quote, router_addr, quote_keep)
    steps += ensure_approval(w3, pk, meme, router_addr, meme_desired)
    data = calldata(router.functions.addLiquidity(
        quote, meme, quote_keep, meme_desired, a_min, b_min, account.address, deadline))
    _preflight(w3, account.address, {"to": router_addr, "data": data})
    h = send_tx(w3, pk, {"to": router_addr, "data": data})
    receipt = wait_ok(w3, h, "addLiquidity v2")
    steps.append(("addLiquidity", h))

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
            "quote_sym": pool_info["quote_sym"], "meme_sym": minfo["symbol"]}


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

        quotes_lc = {a.lower(): s for s, a in cfg["quotes"].items()}
        if t1.lower() in quotes_lc:
            qsym, q_is_t1 = quotes_lc[t1.lower()], True
        elif t0.lower() in quotes_lc:
            qsym, q_is_t1 = quotes_lc[t0.lower()], False
        else:
            return None
        qusd = quote_usd_price(w3, chain_id, qsym)
        rq, rm = (r1, r0) if q_is_t1 else (r0, r1)
        aq, am = (a1, a0) if q_is_t1 else (a0, a1)
        qdec = (i1 if q_is_t1 else i0)["decimals"]
        usd_q = aq / 10 ** qdec * qusd
        usd_m = (am * rq // rm) / 10 ** qdec * qusd if rm else 0
        usd0, usd1 = (usd_m, usd_q) if q_is_t1 else (usd_q, usd_m)
        return {
            "ver": 2, "pid": f"v2:{pair.lower()}", "token_id": f"v2:{pair.lower()}",
            "token0": t0, "token1": t1, "sym0": i0["symbol"], "sym1": i1["symbol"],
            "dec0": i0["decimals"], "dec1": i1["decimals"], "fee": 3000, "pool": pair,
            "tick_lower": None, "tick_upper": None, "cur_tick": None,
            "liquidity": lp, "amount0": a0 / 10 ** i0["decimals"], "amount1": a1 / 10 ** i1["decimals"],
            "fees0": 0.0, "fees1": 0.0, "in_range": True,
            "value_usd": usd0 + usd1, "unclaimed_usd": 0.0,
            "usd0": usd0, "usd1": usd1, "fees_usd0": 0.0, "fees_usd1": 0.0,
            "quote_sym": qsym, "quote_is_token1": q_is_t1,
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
    cfg = CHAINS[chain_id]
    if not verify_v2_router(w3, chain_id):
        raise RuntimeError("V2 router gagal verifikasi on-chain — batal.")
    account = w3.eth.account.from_key(pk)
    router_addr = Web3.to_checksum_address(cfg["v2_router"])
    router = w3.eth.contract(address=router_addr, abi=V2_ROUTER_ABI)
    pair = Web3.to_checksum_address(pair_addr)
    pc = w3.eth.contract(address=pair, abi=V2_PAIR_ABI)
    slip = (100 - slippage_pct) / 100
    deadline = int(time.time()) + DEADLINE_SECS

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
        for taddr, info in ((t0, i0), (t1, i1)):
            if taddr.lower() == wrapped.lower():
                continue
            bal = erc20(w3, taddr).functions.balanceOf(account.address).call()
            if bal == 0:
                continue
            # path langsung ke wrapped, atau lewat quote pair-nya
            other = t1 if taddr == t0 else t0
            path = [Web3.to_checksum_address(taddr), wrapped]
            if taddr.lower() not in quotes_lc and other.lower() != wrapped.lower():
                path = [Web3.to_checksum_address(taddr), Web3.to_checksum_address(other), wrapped]
            try:
                est = router.functions.getAmountsOut(bal, path).call()[-1]
                if est <= 0:
                    continue
                ensure_approval(w3, pk, taddr, router_addr, bal)
                sdata = calldata(router.functions.swapExactTokensForTokens(
                    bal, int(est * slip), path, account.address, int(time.time()) + DEADLINE_SECS))
                _preflight(w3, account.address, {"to": router_addr, "data": sdata})
                sh = send_tx(w3, pk, {"to": router_addr, "data": sdata})
                wait_ok(w3, sh, "swap v2")
                swaps.append((info["symbol"], sh))
            except Exception as e:
                swaps.append((info["symbol"], f"SWAP GAGAL: {e}"))

    return {"steps": steps, "swaps": swaps,
            "got0": amts[0] / 10 ** i0["decimals"], "got1": amts[1] / 10 ** i1["decimals"],
            "sym0": i0["symbol"], "sym1": i1["symbol"], "closed": pct == 100}


# ══════════════════════════ Uniswap V4 ══════════════════════════
def _v4c(w3: Web3, chain_id: int, which: str, abi):
    return w3.eth.contract(address=Web3.to_checksum_address(CHAINS[chain_id][which]), abi=abi)


def verify_v4(w3: Web3, chain_id: int, _cache={}) -> bool:
    """Fail-closed: posm/stateview/UR semua harus menunjuk PoolManager yang sama
    dan posm.permit2() harus Permit2 canonical. Salah satu gagal = semua aksi v4 batal."""
    if chain_id in _cache:
        return _cache[chain_id]
    cfg = CHAINS[chain_id]
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
    _cache[chain_id] = ok
    return ok


def v4_pool_key(a: str, b: str, fee: int, spacing: int) -> tuple:
    """(currency0, currency1, fee, tickSpacing, hooks) — currency sorted ascending,
    native ETH = address(0) selalu currency0. Hooks selalu 0 (pool vanilla saja)."""
    c0, c1 = sorted([a.lower(), b.lower()])
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


def _v4_quote_side(chain_id: int, c0: str, c1: str) -> tuple[str | None, bool]:
    """(quote_sym, quote_is_c1). Native ETH dihitung quote (dihargai = wrapped)."""
    cfg = CHAINS[chain_id]
    quotes_lc = {a.lower(): s for s, a in cfg["quotes"].items()}
    if c1.lower() in quotes_lc:
        return quotes_lc[c1.lower()], True
    if c0.lower() in quotes_lc:
        return quotes_lc[c0.lower()], False
    if c0.lower() == V4_NATIVE:
        return cfg["wrapped_symbol"], False   # harga native = harga wrapped
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
    cfg = CHAINS[chain_id]
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
                if tvl < 10:
                    continue
                # probe $100 (atau 1% reserve virtual) round-trip — buang pool beracun
                probe = int(min(100 / qusd if qusd else 0, q_virt / 10 ** qinfo["decimals"] / 100 or 1)
                            * 10 ** qinfo["decimals"]) or 1
                if not v4_roundtrip_ok(w3, chain_id, key, q_is_c1, probe):
                    continue
                out.append({
                    "ver": 4, "pool": "0x" + pid.hex().removeprefix("0x"), "pool_id": pid,
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
    raw = (sqrtp / Q96) ** 2  # c1 per c0
    out_est = amount_in * raw if zero_for_one else (amount_in / raw if raw else 0)
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


def _v4_ensure_funds(w3: Web3, chain_id: int, pk: str, currency: str, need_wei: int,
                     slippage_pct: float) -> list[tuple[str, str]]:
    """Native → cukup cek saldo (tanpa wrap). ERC20 → jalur ensure_quote_balance biasa."""
    if currency.lower() == V4_NATIVE:
        account = w3.eth.account.from_key(pk)
        bal = w3.eth.get_balance(account.address)
        gas_reserve = w3.to_wei("0.0005", "ether")
        if bal < need_wei + gas_reserve:
            raise RuntimeError(
                f"Saldo native kurang: punya {bal / 1e18:.6f}, butuh {need_wei / 1e18:.6f} + gas")
        return []
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
        qsym, q_is_t1 = _v4_quote_side(chain_id, key[0], key[1])

        raw_price = (sqrtp / Q96) ** 2
        usd = unclaimed_usd = 0.0
        usd0 = usd1 = fees_usd0 = fees_usd1 = 0.0
        mc_lower = mc_upper = mc_now = None
        if qsym:
            qusd = quote_usd_price(w3, chain_id, qsym if qsym in cfg["quotes"] or qsym in cfg["stable_syms"]
                                   else cfg["wrapped_symbol"])
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
            "key": key, "pool_id": pid,
            "token0": key[0], "token1": key[1], "sym0": i0["symbol"], "sym1": i1["symbol"],
            "dec0": i0["decimals"], "dec1": i1["decimals"], "fee": key[2],
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
    qsym, q_is_t1 = _v4_quote_side(chain_id, key[0], key[1])
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

    qusd = quote_usd_price(w3, chain_id, qsym if qsym in cfg["quotes"] or qsym in cfg["stable_syms"]
                           else cfg["wrapped_symbol"])
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
    qsym, q_is_t1 = _v4_quote_side(chain_id, key[0], key[1])
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
def parse_pid(pid) -> tuple[int, object]:
    """'183469' → (3, 183469) · 'v4:12' → (4, 12) · 'v2:0xabc' → (2, '0xabc')."""
    s = str(pid)
    if s.startswith("v4:"):
        return 4, int(s[3:])
    if s.startswith("v2:"):
        return 2, s[3:]
    return 3, int(s)


def list_all_positions(chain_id: int, pk: str, v2_refs: list[str] = (),
                       v4_refs: list[str] = ()) -> list[dict]:
    """Posisi v3 (enumerasi NPM) + v4/v2 (dari registry bot). v3 diberi ver/pid."""
    w3 = get_w3(chain_id)
    account = w3.eth.account.from_key(pk)
    out = []
    for p in list_positions(chain_id, pk):
        p.setdefault("ver", 3)
        p.setdefault("pid", str(p["token_id"]))
        out.append(p)
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
        return increase_position(chain_id, pk, ref, budget_quote, slippage_pct)
    if ver == 4:
        return increase_v4(chain_id, pk, ref, budget_quote, slippage_pct)
    raise RuntimeError("Add posisi v2: paste alamat token lagi lalu pilih pool [v2] yang sama.")


def reduce_any(chain_id: int, pk: str, pid, pct: int, slippage_pct: float) -> dict:
    ver, ref = parse_pid(pid)
    if ver == 3:
        return decrease_position(chain_id, pk, ref, pct)
    if ver == 4:
        return decrease_v4(chain_id, pk, ref, pct, slippage_pct)
    return reduce_v2(chain_id, pk, ref, pct, slippage_pct, autoswap=False)


def collect_any(chain_id: int, pk: str, pid) -> dict:
    ver, ref = parse_pid(pid)
    if ver == 3:
        return collect_fees(chain_id, pk, ref)
    if ver == 4:
        return collect_v4(chain_id, pk, ref)
    raise RuntimeError("Fee LP v2 auto-compound ke dalam posisi — tidak ada yang bisa diklaim terpisah.")


def close_any(chain_id: int, pk: str, pid, slippage_pct: float, autoswap: bool) -> dict:
    ver, ref = parse_pid(pid)
    if ver == 3:
        return close_position(chain_id, pk, ref, slippage_pct, autoswap)
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
        qsym, q_is_t1 = _v4_quote_side(chain_id, key[0], key[1])
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
        pool_info = {"ver": 4, "pool": "0x" + pid4.hex().removeprefix("0x"), "pool_id": pid4,
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
    if got_m > 0:
        poll_balance(w3, meme, account.address, pre_m + int(got_m * 0.9))
    m_delta = erc20(w3, meme).functions.balanceOf(account.address).call() - pre_m
    q_delta = _wallet_balance(w3, quote, account.address) - pre_q  # native: sudah minus gas
    q_delta = max(0, q_delta)
    m_delta = max(0, m_delta)
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
            "token_id": r["token_id"], "mode": mode,
            "tick_lower": r["tick_lower"], "tick_upper": r["tick_upper"],
            "cur_tick": r["cur_tick"], "deposited": r["deposited"],
            "deposit_sym": r["deposit_sym"], "deposited_usd": r["deposited_usd"],
            "quote_sym": pool_info["quote_sym"], "meme_sym": minfo["symbol"]}
