"""
prices.py — Busca preços reais de BRZ/BRLA/BRL1 vs USDT/USDC/DAI.
Fontes: 1inch Price API (gratuita) + GeckoTerminal (gratuita, sem key).
"""

import asyncio
import logging
import os
import aiohttp
from functools import lru_cache
from web3 import Web3
from config import TOKENS, NETWORKS, PARES_MONITORADOS

logger = logging.getLogger(__name__)

# ─── 1inch Price API ──────────────────────────────────────────────────────────
# Retorna preço de qualquer token em USD — sem API key necessária para leitura

ONEINCH_PRICE_URL = "https://api.1inch.dev/price/v1.1/{chain_id}"

ZEROX_PRICE_URL = "https://api.0x.org/swap/allowance-holder/price"
ODOS_QUOTE_URL = "https://api.odos.xyz/sor/quote/v2"
JUMPER_QUOTE_URL = "https://li.quest/v1/quote"
OKU_API_BASE = os.getenv("OKU_API_BASE", "https://canoe.v2.icarus.tools").rstrip("/")
LLAMA_COINS_PRICE_URL = "https://coins.llama.fi/prices/current/{coin_key}"

CHAIN_SLUG = {
    1: "ethereum",
    137: "polygon",
    42161: "arbitrum",
    8453: "base",
}

ERC20_DECIMALS_ABI = [{
    "constant": True,
    "inputs": [],
    "name": "decimals",
    "outputs": [{"name": "", "type": "uint8"}],
    "payable": False,
    "stateMutability": "view",
    "type": "function",
}]


def _oneinch_headers() -> dict:
    headers = {"Accept": "application/json"}
    api_key = os.getenv("ONEINCH_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _zerox_headers() -> dict:
    headers = {
        "Accept": "application/json",
        "0x-version": "v2",
    }
    api_key = os.getenv("ZEROX_API_KEY", "").strip()
    if api_key:
        headers["0x-api-key"] = api_key
    return headers


def _jumper_headers() -> dict:
    headers = {"Accept": "application/json"}
    api_key = os.getenv("LIFI_API_KEY", "").strip()
    if api_key:
        headers["x-lifi-api-key"] = api_key
    return headers


def _odos_headers() -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


@lru_cache(maxsize=512)
def _token_decimais(chain_id: int, token_address: str) -> int:
    """Busca decimais do token via RPC com cache para reduzir chamadas."""
    try:
        rpc_url = NETWORKS[chain_id]["rpc"]
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not w3.is_connected():
            return 6
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_DECIMALS_ABI,
        )
        return int(contract.functions.decimals().call())
    except Exception:
        return 6


def _units_to_amount(units: int, decimals: int) -> float:
    return units / (10 ** decimals)


def _amount_to_units(amount: float, decimals: int) -> int:
    return int(amount * (10 ** decimals))


def _token_usd_referencia(chain_id: int) -> tuple[str, str] | tuple[None, None]:
    """Escolhe token USD base para quotes na rede (USDC prioridade)."""
    tokens_chain = TOKENS.get(chain_id, {})
    if "USDC" in tokens_chain:
        return "USDC", tokens_chain["USDC"]
    if "USDT" in tokens_chain:
        return "USDT", tokens_chain["USDT"]
    return None, None


async def preco_1inch(session: aiohttp.ClientSession,
                       chain_id: int, token_address: str) -> float | None:
    """Retorna preço do token em USD via 1inch Price API."""
    url = f"{ONEINCH_PRICE_URL.format(chain_id=chain_id)}/{token_address}"
    try:
        async with session.get(url, headers=_oneinch_headers(), timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return float(data.get("price", 0)) or None
    except Exception as e:
        logger.debug(f"1inch price erro chain={chain_id} token={token_address}: {e}")
        return None


# ─── GeckoTerminal API ────────────────────────────────────────────────────────
# Preços de pools on-chain em tempo real — gratuita, sem API key

GECKO_NETWORK_MAP = {
    1:     "eth",
    137:   "polygon_pos",
    42161: "arbitrum",
    8453:  "base",
}

GECKO_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}"


async def preco_gecko(session: aiohttp.ClientSession,
                       chain_id: int, token_address: str) -> float | None:
    """Retorna preço em USD via GeckoTerminal."""
    network = GECKO_NETWORK_MAP.get(chain_id)
    if not network:
        return None
    url = GECKO_URL.format(network=network, address=token_address.lower())
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            price_str = data["data"]["attributes"].get("price_usd")
            return float(price_str) if price_str else None
    except Exception as e:
        logger.debug(f"GeckoTerminal erro chain={chain_id} token={token_address}: {e}")
        return None


async def preco_jumper(session: aiohttp.ClientSession,
                       chain_id: int,
                       token_address: str,
                       usd_symbol: str,
                       usd_address: str) -> float | None:
    """Preço inferido via quote da Jumper/Li.Fi."""
    try:
        if token_address.lower() == usd_address.lower():
            return 1.0

        usd_dec = _token_decimais(chain_id, usd_address)
        tok_dec = _token_decimais(chain_id, token_address)

        amount_usd = 100.0
        from_amount = _amount_to_units(amount_usd, usd_dec)
        params = {
            "fromChain": str(chain_id),
            "toChain": str(chain_id),
            "fromToken": usd_address,
            "toToken": token_address,
            "fromAddress": "0xaFA09B49Bdf22D46A997935327c1193823000A53",
            "toAddress": "0xaFA09B49Bdf22D46A997935327c1193823000A53",
            "fromAmount": str(from_amount),
            "slippage": "0.5",
        }

        async with session.get(
            JUMPER_QUOTE_URL,
            params=params,
            headers=_jumper_headers(),
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            to_amount_units = int((data.get("estimate") or {}).get("toAmount", 0))
            if to_amount_units <= 0:
                return None

            to_amount = _units_to_amount(to_amount_units, tok_dec)
            if to_amount <= 0:
                return None

            return amount_usd / to_amount
    except Exception as e:
        logger.debug(
            f"Jumper price erro chain={chain_id} usd={usd_symbol} token={token_address}: {e}"
        )
        return None


async def preco_oku_oracle(session: aiohttp.ClientSession,
                           chain_id: int,
                           token_address: str) -> float | None:
    """Preço USD via oracle da Oku Trade (Canoe)."""
    try:
        chain_slug = CHAIN_SLUG.get(chain_id)
        if not chain_slug:
            return None

        async with session.post(
            f"{OKU_API_BASE}/oracle/safe_usd_price",
            json={"chain": chain_slug, "address": token_address},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            price = data.get("usdPrice")
            return float(price) if price else None
    except Exception as e:
        logger.debug(f"Oku oracle erro chain={chain_id} token={token_address}: {e}")
        return None


async def preco_llama(session: aiohttp.ClientSession,
                      chain_id: int,
                      token_address: str) -> float | None:
    """Preço USD via API de preços da Llama."""
    try:
        chain_slug = CHAIN_SLUG.get(chain_id)
        if not chain_slug:
            return None

        coin_key = f"{chain_slug}:{token_address.lower()}"
        url = LLAMA_COINS_PRICE_URL.format(coin_key=coin_key)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None

            data = await r.json()
            coin = (data.get("coins") or {}).get(coin_key)
            if not coin:
                return None
            price = coin.get("price")
            return float(price) if price else None
    except Exception as e:
        logger.debug(f"Llama price erro chain={chain_id} token={token_address}: {e}")
        return None


async def preco_zerox(session: aiohttp.ClientSession,
                      chain_id: int,
                      token_address: str,
                      usd_symbol: str,
                      usd_address: str) -> float | None:
    """Preço inferido via 0x quote: USD token -> token alvo."""
    try:
        if token_address.lower() == usd_address.lower():
            return 1.0

        usd_dec = _token_decimais(chain_id, usd_address)
        tok_dec = _token_decimais(chain_id, token_address)

        amount_usd = 100.0
        sell_amount = _amount_to_units(amount_usd, usd_dec)
        params = {
            "chainId": str(chain_id),
            "sellToken": usd_address,
            "buyToken": token_address,
            "sellAmount": str(sell_amount),
            "taker": "0x0000000000000000000000000000000000000001",
        }
        async with session.get(
            ZEROX_PRICE_URL,
            params=params,
            headers=_zerox_headers(),
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            buy_amount_units = int(data.get("buyAmount", 0))
            if buy_amount_units <= 0:
                return None
            buy_amount = _units_to_amount(buy_amount_units, tok_dec)
            if buy_amount <= 0:
                return None
            return amount_usd / buy_amount
    except Exception as e:
        logger.debug(
            f"0x price erro chain={chain_id} usd={usd_symbol} token={token_address}: {e}"
        )
        return None


async def preco_odos(session: aiohttp.ClientSession,
                     chain_id: int,
                     token_address: str,
                     usd_symbol: str,
                     usd_address: str) -> float | None:
    """Preço inferido via Odos quote: USD token -> token alvo."""
    try:
        if token_address.lower() == usd_address.lower():
            return 1.0

        usd_dec = _token_decimais(chain_id, usd_address)
        tok_dec = _token_decimais(chain_id, token_address)

        amount_usd = 100.0
        sell_amount = _amount_to_units(amount_usd, usd_dec)

        payload = {
            "chainId": chain_id,
            "inputTokens": [{
                "tokenAddress": usd_address,
                "amount": str(sell_amount),
            }],
            "outputTokens": [{
                "tokenAddress": token_address,
                "proportion": 1,
            }],
            "slippageLimitPercent": 0.5,
            "disableRFQs": True,
            "compact": True,
        }

        async with session.post(
            ODOS_QUOTE_URL,
            json=payload,
            headers=_odos_headers(),
            timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200:
                return None

            data = await r.json()
            out_amounts = data.get("outAmounts") or []
            if not out_amounts:
                return None

            buy_amount_units = int(out_amounts[0])
            if buy_amount_units <= 0:
                return None

            buy_amount = _units_to_amount(buy_amount_units, tok_dec)
            if buy_amount <= 0:
                return None

            return amount_usd / buy_amount
    except Exception as e:
        logger.debug(
            f"Odos price erro chain={chain_id} usd={usd_symbol} token={token_address}: {e}"
        )
        return None


# ─── USD/BRL via ExchangeRate API ────────────────────────────────────────────

async def cotacao_usd_brl(session: aiohttp.ClientSession) -> float:
    """Retorna a cotação USD/BRL oficial como referência."""
    try:
        async with session.get(
            "https://api.exchangerate-api.com/v4/latest/USD",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            data = await r.json()
            return float(data["rates"]["BRL"])
    except Exception:
        return 5.0  # fallback conservador


async def cotacao_usd_brl_atual() -> float:
    """Obtém USD/BRL em chamada isolada (fora do scanner principal)."""
    async with aiohttp.ClientSession() as session:
        return await cotacao_usd_brl(session)


ERC20_BALANCE_ABI = [{
    "constant": True,
    "inputs": [{"name": "_owner", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "balance", "type": "uint256"}],
    "payable": False,
    "stateMutability": "view",
    "type": "function",
}]


async def buscar_saldo_polygon(address: str) -> dict:
    """
    Retorna saldos da carteira na Polygon (rede principal).
    Retorna: {"POL": float, "USDT": float, "USDC": float}
    Falhas individuais retornam None para o token afetado.
    Tenta múltiplos RPCs em fallback para maior resiliência.
    """
    import asyncio as _asyncio

    RPCS = [
        "https://polygon-bor-rpc.publicnode.com",   # ✅ testado e funcional
        "https://polygon.gateway.tenderly.co",
        "https://polygon.drpc.org",
    ]
    tokens_polygon = TOKENS[137]
    checksum_addr = Web3.to_checksum_address(address)

    def _w3(rpc: str):
        return Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))

    def _saldo_nativo():
        for rpc in RPCS:
            try:
                w3 = _w3(rpc)
                raw = w3.eth.get_balance(checksum_addr)
                return raw / 1e18
            except Exception:
                continue
        return None

    def _saldo_erc20(symbol: str):
        token_addr = tokens_polygon.get(symbol)
        if not token_addr:
            return None
        for rpc in RPCS:
            try:
                w3 = _w3(rpc)
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(token_addr),
                    abi=ERC20_BALANCE_ABI,
                )
                decimals = _token_decimais(137, token_addr)
                raw = contract.functions.balanceOf(checksum_addr).call()
                return raw / (10 ** decimals)
            except Exception:
                continue
        return None

    loop = _asyncio.get_event_loop()
    pol, usdt, usdc, dai, brz, brla, brl1 = await _asyncio.gather(
        loop.run_in_executor(None, _saldo_nativo),
        loop.run_in_executor(None, _saldo_erc20, "USDT"),
        loop.run_in_executor(None, _saldo_erc20, "USDC"),
        loop.run_in_executor(None, _saldo_erc20, "DAI"),
        loop.run_in_executor(None, _saldo_erc20, "BRZ"),
        loop.run_in_executor(None, _saldo_erc20, "BRLA"),
        loop.run_in_executor(None, _saldo_erc20, "BRL1"),
    )
    return {"POL": pol, "USDT": usdt, "USDC": usdc, "DAI": dai, "BRZ": brz, "BRLA": brla, "BRL1": brl1}


def _normalizar_preco(preco: float | None) -> float | None:
    """Descarta preços inválidos (ex.: -1 de APIs sem suporte)."""
    if preco is None:
        return None
    try:
        valor = float(preco)
    except Exception:
        return None
    if valor <= 0:
        return None
    return valor


# ─── Scanner principal ────────────────────────────────────────────────────────

async def buscar_todos_precos(
    pares_monitorados: dict[int, list[tuple[str, str]]] | None = None,
) -> dict:
    """
    Varre todas as redes e retorna preços de todos os tokens monitorados.
    Retorna:
        {
          chain_id: {
            "USDT": 1.000,
            "USDC": 0.9998,
            "BRZ":  0.1923,   ← em USD
            "BRLA": 0.1921,
            ...
          }
        }
    """
    resultados = {}

    pares_ativos = pares_monitorados or PARES_MONITORADOS

    async with aiohttp.ClientSession() as session:
        tasks = []

        for chain_id, pares in pares_ativos.items():
            tokens_chain = TOKENS.get(chain_id, {})
            # Coleta tokens únicos desta rede
            tokens_necessarios = set()
            for brl_tok, usd_tok in pares:
                if brl_tok in tokens_chain:
                    tokens_necessarios.add(brl_tok)
                if usd_tok in tokens_chain:
                    tokens_necessarios.add(usd_tok)

            for simbolo in tokens_necessarios:
                address = tokens_chain[simbolo]
                tasks.append((chain_id, simbolo, address))

        # Busca em paralelo
        async def fetch_preco(chain_id, simbolo, address):
            usd_symbol, usd_address = _token_usd_referencia(chain_id)

            if simbolo in {"USDT", "USDC", "DAI"}:
                return chain_id, simbolo, 1.0

            # Tenta 1inch primeiro, fallback GeckoTerminal
            preco = _normalizar_preco(await preco_1inch(session, chain_id, address))

            if not preco and usd_address:
                preco = _normalizar_preco(await preco_jumper(session, chain_id, address, usd_symbol, usd_address))

            if not preco:
                preco = _normalizar_preco(await preco_oku_oracle(session, chain_id, address))

            if not preco and usd_address:
                preco = _normalizar_preco(await preco_zerox(session, chain_id, address, usd_symbol, usd_address))

            if not preco and usd_address:
                preco = _normalizar_preco(await preco_odos(session, chain_id, address, usd_symbol, usd_address))

            if not preco:
                preco = _normalizar_preco(await preco_llama(session, chain_id, address))

            if not preco:
                preco = _normalizar_preco(await preco_gecko(session, chain_id, address))

            return chain_id, simbolo, preco

        resultados_brutos = await asyncio.gather(
            *[fetch_preco(c, s, a) for c, s, a in tasks]
        )

        for chain_id, simbolo, preco in resultados_brutos:
            if chain_id not in resultados:
                resultados[chain_id] = {}
            if preco:
                resultados[chain_id][simbolo] = preco

    return resultados
