"""
executor.py — Assina e executa swaps via 1inch Swap API.
Chamado quando o aluno clica em 'Executar agora' no Telegram.
"""

import logging
import os
import re
import aiohttp
from web3 import Web3
try:
    from web3.middleware import geth_poa_middleware  # web3.py v6
except Exception:  # pragma: no cover
    geth_poa_middleware = None
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


def _explorer_tx_url(chain_id: int, tx_hash_hex: str) -> str:
    base = {
        1: "https://etherscan.io/tx/",
        137: "https://polygonscan.com/tx/",
        42161: "https://arbiscan.io/tx/",
        8453: "https://basescan.org/tx/",
    }.get(chain_id)
    return f"{base}{tx_hash_hex}" if base else tx_hash_hex


def _erro_dex(msg: str) -> dict:
    return {"erro": msg}


def _resumir_erro_http(texto: str, limite: int = 140) -> str:
    t = " ".join((texto or "").split())
    return t[:limite] + ("..." if len(t) > limite else "")


def _parse_int_value(valor, default: int = 0) -> int:
    """Converte números vindos de APIs que podem usar decimal ou hex."""
    if valor is None:
        return default
    if isinstance(valor, bool):
        return int(valor)
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float):
        return int(valor)
    if isinstance(valor, str):
        texto = valor.strip()
        if not texto:
            return default
        try:
            return int(texto, 0)
        except ValueError:
            try:
                return int(float(texto))
            except ValueError:
                return default
    return default

ERC20_DECIMALS_ABI = [{
    "constant": True,
    "inputs": [],
    "name": "decimals",
    "outputs": [{"name": "", "type": "uint8"}],
    "payable": False,
    "stateMutability": "view",
    "type": "function",
}]

ERC20_BALANCE_ALLOWANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


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


def _parse_float_env(nome: str, default: float) -> float:
    raw = os.getenv(nome, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _priority_fee_floor_wei(chain_id: int) -> int:
    defaults = {
        137: 30_000_000_000,
    }
    env_name = f"APPROVE_PRIORITY_FEE_WEI_{chain_id}"
    raw = os.getenv(env_name, "").strip() or os.getenv("APPROVE_PRIORITY_FEE_WEI", "").strip()
    if raw:
        return _parse_int_value(raw, defaults.get(chain_id, 1_500_000_000))
    return defaults.get(chain_id, 1_500_000_000)


def _apply_fee_params(tx: dict, w3: Web3, chain_id: int) -> None:
    """Define fees compatíveis com a rede atual para transações locais (ex.: approve)."""
    priority_floor = _priority_fee_floor_wei(chain_id)

    try:
        latest_block = w3.eth.get_block("latest")
        base_fee = int(latest_block.get("baseFeePerGas") or 0)
    except Exception:
        base_fee = 0

    try:
        network_priority = int(getattr(w3.eth, "max_priority_fee", 0) or 0)
    except Exception:
        network_priority = 0

    priority_fee = max(priority_floor, network_priority)
    gas_price = int(w3.eth.gas_price)

    if base_fee > 0:
        max_fee = max(gas_price, base_fee + (priority_fee * 2))
        tx["maxPriorityFeePerGas"] = priority_fee
        tx["maxFeePerGas"] = max_fee
        tx.pop("gasPrice", None)
    else:
        tx["gasPrice"] = max(gas_price, priority_fee)
        tx.pop("maxPriorityFeePerGas", None)
        tx.pop("maxFeePerGas", None)


def _slippage_steps() -> list[str]:
    """Lista de slippage (%) para tentativas de quote do 1inch."""
    raw = os.getenv("SWAP_SLIPPAGE_STEPS", "0.5,1,2,3")
    steps: list[float] = []
    for item in raw.split(","):
        t = item.strip().replace("%", "")
        if not t:
            continue
        try:
            val = float(t)
            if val > 0:
                steps.append(val)
        except Exception:
            continue

    if not steps:
        steps = [_parse_float_env("ONEINCH_SLIPPAGE", 1.0)]

    # Remove duplicados preservando ordem.
    unique: list[float] = []
    for s in steps:
        if s not in unique:
            unique.append(s)

    return [f"{s:g}" for s in unique]


def _extrair_revert_reason(exc: Exception) -> str:
    msg = str(exc) or exc.__class__.__name__
    match = re.search(r"execution reverted(?::\s*(.*))?", msg, re.IGNORECASE)
    if match:
        detalhe = (match.group(1) or "").strip(" '")
        return detalhe or "execution reverted"
    return msg[:220]


def _normalizar_endereco(addr: str | None) -> str | None:
    if not addr:
        return None
    try:
        return Web3.to_checksum_address(addr)
    except Exception:
        return None


def _to_wei_amount(chain_id: int, token_symbol: str, amount_usd: float) -> int:
    token_addr = TOKENS.get(chain_id, {}).get(token_symbol)
    if not token_addr:
        return 0
    decimais = _token_decimais(chain_id, token_addr)
    return int(amount_usd * (10 ** decimais))


def _checar_pretrade_erc20(
    w3: Web3,
    chain_id: int,
    wallet: str,
    token_from: str,
    amount_wei: int,
    spender: str | None,
) -> str | None:
    token_addr = TOKENS.get(chain_id, {}).get(token_from)
    if not token_addr:
        return f"Token {token_from} não mapeado na rede {chain_id}."

    try:
        owner = Web3.to_checksum_address(wallet)
        token = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=ERC20_BALANCE_ALLOWANCE_ABI,
        )
        saldo = int(token.functions.balanceOf(owner).call())
        if saldo < amount_wei:
            return (
                f"Saldo insuficiente de {token_from} para o swap "
                f"(saldo={saldo}, necessário={amount_wei})."
            )

        if spender:
            allow = int(token.functions.allowance(owner, Web3.to_checksum_address(spender)).call())
            if allow < amount_wei:
                return (
                    f"Allowance insuficiente de {token_from} para o contrato {spender}. "
                    f"Aprove o token antes de executar (allowance={allow}, necessário={amount_wei})."
                )
    except Exception as exc:
        logger.warning(f"Falha na pré-checagem ERC20: {exc}")

    return None


def _checar_saldo_gas(
    w3: Web3,
    account_addr: str,
    tx: dict,
) -> str | None:
    try:
        saldo_native = int(w3.eth.get_balance(account_addr))
        gas = int(tx.get("gas", 0) or 0)
        value = int(tx.get("value", 0) or 0)
        if tx.get("maxFeePerGas"):
            preco_gas = int(tx["maxFeePerGas"])
        else:
            preco_gas = int(tx.get("gasPrice", 0) or 0)

        custo_max = value + gas * preco_gas
        if saldo_native < custo_max:
            return (
                "Saldo insuficiente de token nativo para gas/value. "
                f"Saldo={saldo_native}, custo_estimado={custo_max}."
            )
    except Exception as exc:
        logger.warning(f"Falha na checagem de saldo de gas: {exc}")

    return None


def _allowance_atual(
    w3: Web3,
    chain_id: int,
    wallet: str,
    token_from: str,
    spender: str | None,
) -> tuple[bool, int]:
    token_addr = TOKENS.get(chain_id, {}).get(token_from)
    if not token_addr or not spender:
        return False, 0
    try:
        owner = Web3.to_checksum_address(wallet)
        token = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=ERC20_BALANCE_ALLOWANCE_ABI,
        )
        allow = int(token.functions.allowance(owner, Web3.to_checksum_address(spender)).call())
        return True, allow
    except Exception:
        return False, 0


def _auto_approve_erc20(
    w3: Web3,
    chain_id: int,
    private_key: str,
    wallet: str,
    token_from: str,
    spender: str | None,
    amount_wei: int,
) -> tuple[bool, str | None, list[str]]:
    """Faz approve automático quando allowance está abaixo do necessário."""
    token_addr = TOKENS.get(chain_id, {}).get(token_from)
    if not token_addr:
        return False, f"Token {token_from} não mapeado na rede {chain_id}.", []
    if not spender:
        return False, "Contrato spender não informado para approve.", []

    try:
        owner = Web3.to_checksum_address(wallet)
        spender_addr = Web3.to_checksum_address(spender)
        account = w3.eth.account.from_key(private_key)
        token = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=ERC20_BALANCE_ALLOWANCE_ABI,
        )

        approve_max = os.getenv("APPROVE_MAX_UINT", "true").strip().lower() in {"1", "true", "yes", "y", "on"}
        alvo_allowance = (2**256 - 1) if approve_max else amount_wei

        # Alguns tokens exigem zerar allowance antes de definir novo valor.
        passos: list[int] = []
        atual = int(token.functions.allowance(owner, spender_addr).call())
        if atual > 0 and atual < amount_wei:
            passos.append(0)
        passos.append(alvo_allowance)

        gas_mult = _parse_float_env("TX_GAS_MULTIPLIER", 1.15)
        if gas_mult < 1.0:
            gas_mult = 1.0

        receipt_timeout = int(_parse_float_env("SWAP_RECEIPT_TIMEOUT_SEC", 120))
        approve_explorers: list[str] = []

        for novo_valor in passos:
            tx = token.functions.approve(spender_addr, int(novo_valor)).build_transaction({
                "from": owner,
                "nonce": w3.eth.get_transaction_count(owner, "pending"),
                "chainId": chain_id,
                "value": 0,
            })

            gas_est = int(w3.eth.estimate_gas({
                "from": owner,
                "to": tx.get("to"),
                "data": tx.get("data"),
                "value": 0,
            }))
            tx["gas"] = int(gas_est * gas_mult)

            _apply_fee_params(tx, w3, chain_id)

            signed = w3.eth.account.sign_transaction(tx, private_key)
            raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash,
                timeout=receipt_timeout,
                poll_latency=2,
            )
            approve_explorers.append(_explorer_tx_url(chain_id, tx_hash.hex()))
            if int(receipt.get("status", 0)) != 1:
                h = tx_hash.hex()
                return False, f"Approve falhou on-chain (status=0): {h}", approve_explorers

        return True, None, approve_explorers
    except Exception as exc:
        return False, f"Falha no approve automático: {exc}", []


def _simular_tx_call(w3: Web3, tx: dict) -> str | None:
    """Simula a execução para capturar revert antes de enviar tx real."""
    try:
        call_tx = {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["data"],
            "value": tx.get("value", 0),
        }
        w3.eth.call(call_tx, "pending")
    except Exception as exc:
        return _extrair_revert_reason(exc)
    return None


def _configurar_middleware_rede(w3: Web3, chain_id: int) -> None:
    """Configura middleware necessário por rede (ex.: Polygon/POA)."""
    if chain_id in {137} and geth_poa_middleware is not None:
        try:
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        except Exception:
            # Ignora se já foi injetado ou se o provedor não permitir alteração.
            pass


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
            _configurar_middleware_rede(w3, chain_id)
            if w3.is_connected():
                return w3
        except Exception:
            continue
    # último recurso — retorna sem verificar; chamador vai receber erro explícito
    w3 = Web3(Web3.HTTPProvider(urls[0]))
    _configurar_middleware_rede(w3, chain_id)
    return w3


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
    slippage_pct: str | None = None,
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
        "slippage":         (slippage_pct or os.getenv("ONEINCH_SLIPPAGE", "1")),
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

                spender = data.get("allowanceTarget") or tx_raw.get("to")
                return {"tx": tx, "fonte": "0x", "spender": spender}
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

                estimate = data.get("estimate") or {}
                spender = estimate.get("approvalAddress") or tx_raw.get("to")
                return {"tx": tx, "fonte": "jumper", "raw": data, "spender": spender}
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
        "slippage": _parse_int_value(os.getenv("OKU_SLIPPAGE_BPS", "50"), 50),
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

                approval = data.get("executionInfo", {}).get("approval") or {}
                spender = approval.get("allowanceTarget") or tx_raw.get("to")
                return {"tx": tx, "fonte": "oku", "raw": data, "spender": spender}
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

                spender = data.get("allowanceTarget") or tx_raw.get("to")
                return {"tx": tx, "fonte": "llama", "raw": data, "spender": spender}
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
    slippage_steps = _slippage_steps()

    for dex in _dex_priority():
        if dex == "1inch":
            rota = None
            for sl in slippage_steps:
                rota = await buscar_rota_swap(
                    chain_id,
                    token_from,
                    token_to,
                    amount_usd,
                    wallet,
                    slippage_pct=sl,
                )
                if rota and rota.get("tx"):
                    rota["slippage"] = sl
                    break
                if rota and rota.get("erro"):
                    diagnostico.append(f"1inch@{sl}%: {rota['erro']}")
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

    spender = _normalizar_endereco(rota.get("spender") or tx_data.get("to"))
    amount_wei = _to_wei_amount(chain_id, token_from, amount_usd)

    # 2. Conecta na rede
    w3 = _rpc_web3(chain_id)

    if not w3.is_connected():
        return {"sucesso": False, "erro": f"Sem conexão com RPC da rede {chain_id}."}

    try:
        account = w3.eth.account.from_key(private_key)
        approve_explorers: list[str] = []

        # Monta a transação com nonce e gas atuais
        tx = {
            "from":     account.address,
            "to":       Web3.to_checksum_address(tx_data["to"]),
            "data":     tx_data["data"],
            "value":    _parse_int_value(tx_data.get("value", 0)),
            "nonce":    w3.eth.get_transaction_count(account.address, "pending"),
            "chainId":  chain_id,
        }

        erro_pretrade = _checar_pretrade_erc20(
            w3=w3,
            chain_id=chain_id,
            wallet=account.address,
            token_from=token_from,
            amount_wei=amount_wei,
            spender=spender,
        )
        if erro_pretrade:
            auto_approve = os.getenv("AUTO_APPROVE_ERC20", "true").strip().lower() in {
                "1", "true", "yes", "y", "on"
            }
            allowance_baixa = "Allowance insuficiente" in erro_pretrade
            if auto_approve and allowance_baixa:
                ok, erro_approve, approve_explorers = _auto_approve_erc20(
                    w3=w3,
                    chain_id=chain_id,
                    private_key=private_key,
                    wallet=account.address,
                    token_from=token_from,
                    spender=spender,
                    amount_wei=amount_wei,
                )
                if not ok:
                    return {
                        "sucesso": False,
                        "erro": f"{erro_pretrade} | {erro_approve}",
                        "approve_explorers": approve_explorers,
                    }

                allowance_ok, allowance_pos = _allowance_atual(
                    w3=w3,
                    chain_id=chain_id,
                    wallet=account.address,
                    token_from=token_from,
                    spender=spender,
                )
                if (not allowance_ok) or allowance_pos < amount_wei:
                    return {
                        "sucesso": False,
                        "erro": (
                            "Approve automático não elevou allowance ao mínimo necessário. "
                            f"allowance={allowance_pos}, necessário={amount_wei}."
                        ),
                        "approve_explorers": approve_explorers,
                    }
            else:
                return {"sucesso": False, "erro": erro_pretrade}

        # Barreira final anti-revert por allowance: se a leitura falhar ou estiver baixa,
        # tenta approve automático antes de assinar o swap.
        auto_approve = os.getenv("AUTO_APPROVE_ERC20", "true").strip().lower() in {
            "1", "true", "yes", "y", "on"
        }
        if auto_approve and spender:
            allowance_ok, allowance_now = _allowance_atual(
                w3=w3,
                chain_id=chain_id,
                wallet=account.address,
                token_from=token_from,
                spender=spender,
            )
            if (not allowance_ok) or allowance_now < amount_wei:
                ok, erro_approve, approve_extra = _auto_approve_erc20(
                    w3=w3,
                    chain_id=chain_id,
                    private_key=private_key,
                    wallet=account.address,
                    token_from=token_from,
                    spender=spender,
                    amount_wei=amount_wei,
                )
                if approve_extra:
                    approve_explorers.extend(approve_extra)
                if not ok:
                    return {
                        "sucesso": False,
                        "erro": (
                            "Allowance não pôde ser confirmado antes do swap "
                            f"(ok={allowance_ok}, atual={allowance_now}). {erro_approve}"
                        ),
                        "approve_explorers": approve_explorers,
                    }

                allowance_ok, allowance_now = _allowance_atual(
                    w3=w3,
                    chain_id=chain_id,
                    wallet=account.address,
                    token_from=token_from,
                    spender=spender,
                )
                if (not allowance_ok) or allowance_now < amount_wei:
                    return {
                        "sucesso": False,
                        "erro": (
                            "Allowance ainda insuficiente após approve automático. "
                            f"ok={allowance_ok}, allowance={allowance_now}, necessário={amount_wei}."
                        ),
                        "approve_explorers": approve_explorers,
                    }

        gas_limite = _parse_int_value(tx_data.get("gas", 0))
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

        gas_mult = _parse_float_env("TX_GAS_MULTIPLIER", 1.15)
        if gas_mult < 1.0:
            gas_mult = 1.0
        tx["gas"] = int(gas_limite * gas_mult)

        # Compatível com redes EIP-1559 e legadas.
        if tx_data.get("maxFeePerGas") and tx_data.get("maxPriorityFeePerGas"):
            tx["maxFeePerGas"] = _parse_int_value(tx_data["maxFeePerGas"])
            tx["maxPriorityFeePerGas"] = _parse_int_value(tx_data["maxPriorityFeePerGas"])
        elif tx_data.get("gasPrice"):
            tx["gasPrice"] = _parse_int_value(tx_data["gasPrice"])
        else:
            tx["gasPrice"] = int(w3.eth.gas_price)

        erro_saldo = _checar_saldo_gas(w3, account.address, tx)
        if erro_saldo:
            return {
                "sucesso": False,
                "erro": erro_saldo,
                "approve_explorers": approve_explorers,
            }

        revert = _simular_tx_call(w3, tx)
        if revert:
            return {
                "sucesso": False,
                "erro": f"Simulação on-chain falhou antes do envio: {revert}",
                "approve_explorers": approve_explorers,
            }

        # 3. Assina localmente
        signed = w3.eth.account.sign_transaction(tx, private_key)

        # 4. Envia para a rede
        # Compatível com web3.py v5 (rawTransaction) e v6+ (raw_transaction)
        raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        tx_hash_hex = tx_hash.hex()

        receipt_timeout = int(_parse_float_env("SWAP_RECEIPT_TIMEOUT_SEC", 120))
        receipt = w3.eth.wait_for_transaction_receipt(
            tx_hash,
            timeout=receipt_timeout,
            poll_latency=2,
        )
        if int(receipt.get("status", 0)) != 1:
            return {
                "sucesso": False,
                "erro": (
                    "Transação revertida on-chain (status=0). "
                    "Ajuste slippage/valor e tente novamente."
                ),
                "tx_hash": tx_hash_hex,
                "explorer": _explorer_tx_url(chain_id, tx_hash_hex),
                "approve_explorers": approve_explorers,
            }

        sl_info = rota.get("slippage")
        if sl_info:
            logger.info(
                f"Swap executado: {tx_hash_hex} na rede {chain_id} "
                f"via {fonte or 'desconhecida'} (slippage={sl_info}%)"
            )
        else:
            logger.info(f"Swap executado: {tx_hash_hex} na rede {chain_id} via {fonte or 'desconhecida'}")

        return {
            "sucesso":  True,
            "tx_hash":  tx_hash_hex,
            "explorer": _explorer_tx_url(chain_id, tx_hash_hex),
            "fonte": fonte,
            "approve_explorers": approve_explorers,
        }

    except Exception as e:
        logger.error(f"Erro ao assinar/enviar tx: {e}")
        return {"sucesso": False, "erro": str(e)}
