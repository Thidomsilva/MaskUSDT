"""
executor.py — Assina e executa swaps via 1inch Swap API.
Chamado quando o aluno clica em 'Executar agora' no Telegram.
"""

import logging
import os
import aiohttp
from web3 import Web3
from config import TOKENS, NETWORKS

logger = logging.getLogger(__name__)

# RPCs com fallback ordenados por confiabilidade (testados mai/2026)
_RPCS_FALLBACK: dict[int, list[str]] = {
    137: [
        "https://polygon-bor-rpc.publicnode.com",   # ✅ principal
        "https://polygon.gateway.tenderly.co",
        "https://polygon.drpc.org",
    ],
}

ONEINCH_SWAP_URL = "https://api.1inch.dev/swap/v6.0/{chain_id}/swap"
ZEROX_QUOTE_URL = "https://api.0x.org/swap/allowance-holder/quote"
JUMPER_QUOTE_URL = "https://li.quest/v1/quote"
OKU_API_BASE = os.getenv("OKU_API_BASE", "https://canoe.v2.icarus.tools").rstrip("/")

CHAIN_SLUG = {
    1: "ethereum",
    137: "polygon",
    42161: "arbitrum",
    8453: "base",
}


def _erro_dex(msg: str) -> dict:
    return {"erro": msg}


def _resumir_erro_http(texto: str, limite: int = 140) -> str:
    t = " ".join((texto or "").split())
    return t[:limite] + ("..." if len(t) > limite else "")

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


def _dex_priority() -> list[str]:
    """Ordem de tentativa dos adapters de execução."""
    raw = os.getenv("DEX_PRIORITY", "1inch,zerox,jumper,oku,llama")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _rpc_web3(chain_id: int) -> Web3:
    """Tenta RPCs em ordem; retorna o primeiro conectado (fallback seguro)."""
    urls: list[str] = []
    primary = NETWORKS[chain_id]["rpc"]
    urls.append(primary)
    for r in _RPCS_FALLBACK.get(chain_id, []):
        if r not in urls:
            urls.append(r)
    for url in urls:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    # último recurso — retorna sem verificar; chamador vai receber erro explícito
    return Web3(Web3.HTTPProvider(urls[0]))


def _token_decimais(chain_id: int, token_address: str) -> int:
    """Busca decimais reais do ERC-20; fallback conservador em caso de falha."""
    try:
        w3 = _rpc_web3(chain_id)
        if not w3.is_connected():
            return 6
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_DECIMALS_ABI,
        )
        return int(contract.functions.decimals().call())
    except Exception:
        return 6


async def buscar_rota_swap(
    chain_id:   int,
    token_from: str,   # símbolo ex: "USDT"
    token_to:   str,   # símbolo ex: "BRZ"
    amount_usd: float,
    wallet:     str,
) -> dict | None:
    """
    Consulta a 1inch para obter a melhor rota e os dados da transação.
    Retorna o dict com 'tx' pronto para assinar, ou None em caso de erro.
    """
    tokens_rede = TOKENS.get(chain_id, {})
    addr_from   = tokens_rede.get(token_from)
    addr_to     = tokens_rede.get(token_to)

    if not addr_from or not addr_to:
        logger.error(f"Token não encontrado na rede {chain_id}: {token_from} ou {token_to}")
        return None

    # Converte amount_usd para unidades do token de entrada usando decimais reais.
    decimais_from = _token_decimais(chain_id, addr_from)
    amount_wei = int(amount_usd * (10 ** decimais_from))

    params = {
        "src":              addr_from,
        "dst":              addr_to,
        "amount":           str(amount_wei),
        "from":             wallet,
        "slippage":         "0.5",       # 0.5% slippage máximo
        "disableEstimate":  "false",
        "allowPartialFill": "false",
    }

    url = ONEINCH_SWAP_URL.format(chain_id=chain_id)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, headers=_oneinch_headers(),
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    erro = await r.text()
                    logger.error(f"1inch swap erro {r.status}: {erro}")
                    return _erro_dex(f"http {r.status}: {_resumir_erro_http(erro)}")
                data = await r.json()
                return data
    except Exception as e:
        logger.error(f"Erro ao buscar rota 1inch: {e}")
        return _erro_dex(str(e))


async def buscar_rota_swap_zerox(
    chain_id: int,
    token_from: str,
    token_to: str,
    amount_usd: float,
    wallet: str,
) -> dict | None:
    """Fallback de rota via 0x Allowance Holder Quote API."""
    tokens_rede = TOKENS.get(chain_id, {})
    addr_from = tokens_rede.get(token_from)
    addr_to = tokens_rede.get(token_to)

    if not addr_from or not addr_to:
        return None

    decimais_from = _token_decimais(chain_id, addr_from)
    amount_wei = int(amount_usd * (10 ** decimais_from))

    params = {
        "chainId": str(chain_id),
        "sellToken": addr_from,
        "buyToken": addr_to,
        "sellAmount": str(amount_wei),
        "taker": wallet,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                ZEROX_QUOTE_URL,
                params=params,
                headers=_zerox_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    erro = await r.text()
                    logger.error(f"0x quote erro {r.status}: {erro}")
                    return _erro_dex(f"http {r.status}: {_resumir_erro_http(erro)}")

                data = await r.json()
                tx_raw = data.get("transaction")
                if not tx_raw:
                    return None

                tx = {
                    "to": tx_raw.get("to"),
                    "data": tx_raw.get("data"),
                    "value": str(tx_raw.get("value", "0")),
                    "gas": str(tx_raw.get("gas", "0")),
                }

                if tx_raw.get("gasPrice"):
                    tx["gasPrice"] = str(tx_raw.get("gasPrice"))
                if tx_raw.get("maxFeePerGas"):
                    tx["maxFeePerGas"] = str(tx_raw.get("maxFeePerGas"))
                if tx_raw.get("maxPriorityFeePerGas"):
                    tx["maxPriorityFeePerGas"] = str(tx_raw.get("maxPriorityFeePerGas"))

                return {"tx": tx, "fonte": "0x"}
    except Exception as e:
        logger.error(f"Erro ao buscar rota 0x: {e}")
        return _erro_dex(str(e))


async def buscar_rota_swap_jumper(
    chain_id: int,
    token_from: str,
    token_to: str,
    amount_usd: float,
    wallet: str,
) -> dict | None:
    """Quote e tx via Jumper (Li.Fi)."""
    tokens_rede = TOKENS.get(chain_id, {})
    addr_from = tokens_rede.get(token_from)
    addr_to = tokens_rede.get(token_to)
    if not addr_from or not addr_to:
        return None

    decimais_from = _token_decimais(chain_id, addr_from)
    amount_wei = int(amount_usd * (10 ** decimais_from))

    params = {
        "fromChain": str(chain_id),
        "toChain": str(chain_id),
        "fromToken": addr_from,
        "toToken": addr_to,
        "fromAddress": wallet,
        "toAddress": wallet,
        "fromAmount": str(amount_wei),
        "slippage": os.getenv("LIFI_SLIPPAGE", "0.5"),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                JUMPER_QUOTE_URL,
                params=params,
                headers=_jumper_headers(),
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                if r.status != 200:
                    erro = await r.text()
                    logger.error(f"Jumper quote erro {r.status}: {erro}")
                    return _erro_dex(f"http {r.status}: {_resumir_erro_http(erro)}")

                data = await r.json()
                tx_raw = data.get("transactionRequest")
                if not tx_raw:
                    return None

                tx = {
                    "to": tx_raw.get("to"),
                    "data": tx_raw.get("data"),
                    "value": str(tx_raw.get("value", "0")),
                    "gas": str(tx_raw.get("gasLimit", tx_raw.get("gas", "0"))),
                }
                if tx_raw.get("gasPrice"):
                    tx["gasPrice"] = str(tx_raw.get("gasPrice"))
                if tx_raw.get("maxFeePerGas"):
                    tx["maxFeePerGas"] = str(tx_raw.get("maxFeePerGas"))
                if tx_raw.get("maxPriorityFeePerGas"):
                    tx["maxPriorityFeePerGas"] = str(tx_raw.get("maxPriorityFeePerGas"))

                return {"tx": tx, "fonte": "jumper", "raw": data}
    except Exception as e:
        logger.error(f"Erro ao buscar rota Jumper: {e}")
        return _erro_dex(str(e))


async def buscar_rota_swap_oku(
    chain_id: int,
    token_from: str,
    token_to: str,
    amount_usd: float,
    wallet: str,
) -> dict | None:
    """Quote e tx via Oku Trade (Canoe API)."""
    tokens_rede = TOKENS.get(chain_id, {})
    addr_from = tokens_rede.get(token_from)
    addr_to = tokens_rede.get(token_to)
    chain_slug = CHAIN_SLUG.get(chain_id)
    if not addr_from or not addr_to or not chain_slug:
        return None

    market = os.getenv("OKU_MARKET_ID", "zeroex")
    url = f"{OKU_API_BASE}/market/{market}/swap_quote"
    payload = {
        "chain": chain_slug,
        "account": wallet,
        "dstAddress": wallet,
        "isExactIn": True,
        "inTokenAddress": addr_from,
        "outTokenAddress": addr_to,
        "inTokenAmount": str(amount_usd),
        "slippage": int(os.getenv("OKU_SLIPPAGE_BPS", "50")),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                if r.status != 200:
                    erro = await r.text()
                    logger.error(f"Oku quote erro {r.status}: {erro}")
                    return _erro_dex(f"http {r.status}: {_resumir_erro_http(erro)}")

                data = await r.json()
                tx_raw = data.get("executionInfo", {}).get("trade") or {}
                if not tx_raw:
                    return None

                tx = {
                    "to": tx_raw.get("to"),
                    "data": tx_raw.get("data"),
                    "value": str(tx_raw.get("value", "0")),
                }

                return {"tx": tx, "fonte": "oku", "raw": data}
    except Exception as e:
        logger.error(f"Erro ao buscar rota Oku: {e}")
        return _erro_dex(str(e))


async def buscar_rota_swap_llama(
    chain_id: int,
    token_from: str,
    token_to: str,
    amount_usd: float,
    wallet: str,
) -> dict | None:
    """Adapter configurável para LlamaSwap via endpoint definido em env."""
    endpoint = os.getenv("LLAMASWAP_QUOTE_URL", "").strip()
    if not endpoint:
        return None

    tokens_rede = TOKENS.get(chain_id, {})
    addr_from = tokens_rede.get(token_from)
    addr_to = tokens_rede.get(token_to)
    if not addr_from or not addr_to:
        return None

    decimais_from = _token_decimais(chain_id, addr_from)
    amount_wei = int(amount_usd * (10 ** decimais_from))

    payload = {
        "chainId": chain_id,
        "src": addr_from,
        "dst": addr_to,
        "amount": str(amount_wei),
        "from": wallet,
        "slippage": os.getenv("LLAMASWAP_SLIPPAGE", "0.5"),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                if r.status != 200:
                    erro = await r.text()
                    logger.error(f"LlamaSwap quote erro {r.status}: {erro}")
                    return _erro_dex(f"http {r.status}: {_resumir_erro_http(erro)}")

                data = await r.json()
                tx_raw = data.get("tx") or data.get("transactionRequest") or data.get("transaction")
                if not tx_raw:
                    return None

                tx = {
                    "to": tx_raw.get("to"),
                    "data": tx_raw.get("data"),
                    "value": str(tx_raw.get("value", "0")),
                    "gas": str(tx_raw.get("gas", tx_raw.get("gasLimit", "0"))),
                }
                if tx_raw.get("gasPrice"):
                    tx["gasPrice"] = str(tx_raw.get("gasPrice"))
                if tx_raw.get("maxFeePerGas"):
                    tx["maxFeePerGas"] = str(tx_raw.get("maxFeePerGas"))
                if tx_raw.get("maxPriorityFeePerGas"):
                    tx["maxPriorityFeePerGas"] = str(tx_raw.get("maxPriorityFeePerGas"))

                return {"tx": tx, "fonte": "llama", "raw": data}
    except Exception as e:
        logger.error(f"Erro ao buscar rota LlamaSwap: {e}")
        return _erro_dex(str(e))


async def executar_swap(
    chain_id:   int,
    token_from: str,
    token_to:   str,
    amount_usd: float,
    wallet:     str,
    private_key: str,
) -> dict:
    """
    Executa o swap completo:
    1. Busca rota na 1inch
    2. Assina a transação localmente (private key nunca sai do servidor)
    3. Envia para a rede
    4. Retorna resultado com tx_hash ou erro

    Retorna:
        {"sucesso": True,  "tx_hash": "0x...", "explorer": "https://..."}
        {"sucesso": False, "erro": "mensagem"}
    """
    # 1. Busca rota em múltiplas DEXs configuradas
    rota = None
    fonte = None
    diagnostico = []

    for dex in _dex_priority():
        if dex == "1inch":
            rota = await buscar_rota_swap(chain_id, token_from, token_to, amount_usd, wallet)
        elif dex == "zerox":
            rota = await buscar_rota_swap_zerox(chain_id, token_from, token_to, amount_usd, wallet)
        elif dex == "jumper":
            rota = await buscar_rota_swap_jumper(chain_id, token_from, token_to, amount_usd, wallet)
        elif dex == "oku":
            rota = await buscar_rota_swap_oku(chain_id, token_from, token_to, amount_usd, wallet)
        elif dex == "llama":
            rota = await buscar_rota_swap_llama(chain_id, token_from, token_to, amount_usd, wallet)

        if rota and rota.get("tx"):
            fonte = rota.get("fonte", dex)
            break

        if rota and rota.get("erro"):
            diagnostico.append(f"{dex}: {rota['erro']}")
        else:
            diagnostico.append(f"{dex}: sem rota")

    if not rota or not rota.get("tx"):
        detalhe = " | ".join(diagnostico[:4]) if diagnostico else "sem detalhes"
        return {
            "sucesso": False,
            "erro": f"Não foi possível obter rota nas DEXs configuradas. Diagnóstico: {detalhe}",
        }

    tx_data = rota.get("tx")
    if not tx_data:
        return {"sucesso": False, "erro": "Resposta inválida da DEX (sem campo tx)."}

    # 2. Conecta na rede
    w3 = _rpc_web3(chain_id)

    if not w3.is_connected():
        return {"sucesso": False, "erro": f"Sem conexão com RPC da rede {chain_id}."}

    try:
        account = w3.eth.account.from_key(private_key)

        # Monta a transação com nonce e gas atuais
        tx = {
            "from":     account.address,
            "to":       Web3.to_checksum_address(tx_data["to"]),
            "data":     tx_data["data"],
            "value":    int(tx_data.get("value", 0)),
            "nonce":    w3.eth.get_transaction_count(account.address),
            "chainId":  chain_id,
        }

        gas_limite = int(tx_data.get("gas", 0) or 0)
        if gas_limite <= 0:
            try:
                gas_limite = int(w3.eth.estimate_gas({
                    "from": account.address,
                    "to": tx["to"],
                    "data": tx["data"],
                    "value": tx["value"],
                }))
            except Exception:
                gas_limite = 250000
        tx["gas"] = gas_limite

        # Compatível com redes EIP-1559 e legadas.
        if tx_data.get("maxFeePerGas") and tx_data.get("maxPriorityFeePerGas"):
            tx["maxFeePerGas"] = int(tx_data["maxFeePerGas"])
            tx["maxPriorityFeePerGas"] = int(tx_data["maxPriorityFeePerGas"])
        elif tx_data.get("gasPrice"):
            tx["gasPrice"] = int(tx_data["gasPrice"])
        else:
            tx["gasPrice"] = int(w3.eth.gas_price)

        # 3. Assina localmente
        signed = w3.eth.account.sign_transaction(tx, private_key)

        # 4. Envia para a rede
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        # Monta link do explorer
        explorers = {
            1:     f"https://etherscan.io/tx/{tx_hash_hex}",
            137:   f"https://polygonscan.com/tx/{tx_hash_hex}",
            42161: f"https://arbiscan.io/tx/{tx_hash_hex}",
            8453:  f"https://basescan.org/tx/{tx_hash_hex}",
        }

        logger.info(f"Swap executado: {tx_hash_hex} na rede {chain_id} via {fonte or 'desconhecida'}")

        return {
            "sucesso":  True,
            "tx_hash":  tx_hash_hex,
            "explorer": explorers.get(chain_id, tx_hash_hex),
            "fonte": fonte,
        }

    except Exception as e:
        logger.error(f"Erro ao assinar/enviar tx: {e}")
        return {"sucesso": False, "erro": str(e)}
