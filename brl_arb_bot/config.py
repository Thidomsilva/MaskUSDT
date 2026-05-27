"""
config.py — Mapa completo de tokens e pares BRL stablecoins.
Todos os endereços verificados on-chain (mai/2026).

Tokens monitorados:
  BRZ  — Transfero         (Polygon, Ethereum, Arbitrum)
  BRLA — Stabull           (Polygon, Base)
  BRL1 — Consórcio MB/Foxbit/Bitso  (Polygon)
    DAI  — MakerDAO          (Polygon)

Pares: TODOS contra TODOS onde houver liquidez
    BRZ  ↔ USDT, USDC, DAI
    BRLA ↔ USDT, USDC, DAI
    BRL1 ↔ USDT, USDC, DAI
  BRZ  ↔ BRLA              ← novo
  BRZ  ↔ BRL1              ← novo
  BRLA ↔ BRL1              ← novo
"""

import os


def _env_float(nome: str, default: float) -> float:
    raw = os.getenv(nome)
    if raw is None:
        return default
    s = raw.strip()
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _env_int(nome: str, default: int) -> int:
    raw = os.getenv(nome)
    if raw is None:
        return default
    s = raw.strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        return default


def _env_bool(nome: str, default: bool = False) -> bool:
    raw = os.getenv(nome)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

# ─── Redes suportadas ─────────────────────────────────────────────────────────
NETWORKS = {
    1: {
        "name":          "Ethereum",
        "rpc":           "https://rpc.ankr.com/eth",
        "symbol":        "ETH",
        "gas_token_usd": 3000,
        "1inch_chain":   "1",
        "lifi_chain":    "ETH",
    },
    137: {
        "name":          "Polygon",
        "rpc":           "https://polygon-bor-rpc.publicnode.com",
        "symbol":        "POL",
        "gas_token_usd": 0.5,
        "1inch_chain":   "137",
        "lifi_chain":    "POL",
    },
    42161: {
        "name":          "Arbitrum",
        "rpc":           "https://rpc.ankr.com/arbitrum",
        "symbol":        "ETH",
        "gas_token_usd": 3000,
        "1inch_chain":   "42161",
        "lifi_chain":    "ARB",
    },
    8453: {
        "name":          "Base",
        "rpc":           "https://rpc.ankr.com/base",
        "symbol":        "ETH",
        "gas_token_usd": 3000,
        "1inch_chain":   "8453",
        "lifi_chain":    "BAS",
    },
}

# ─── Endereços de token por rede (verificados) ────────────────────────────────
TOKENS = {
    # Ethereum Mainnet
    1: {
        "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "BRZ":  "0x420412E765BFa6d85aaaC94b4f7b708C89be2e2B",
    },
    # Polygon  ← hub principal de liquidez BRL em 2026
    137: {
        "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        "DAI":  "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063",
        "BRZ":  "0x4eD141110F6EeeAbA9A1df36d8c26f684d2475Dc",  # Transfero Polygon
        "BRLA": "0xE6A537a407488807F0bbeb0038B79004f19DDDFb",  # Avenia Polygon
        "BRL1": "0x5C067C80C00eCd2345B05E83A3e758eF799C40B5",  # BRL1 Consortium Polygon
        # Core do Motor 2 (Crypto Chain)
        "WETH": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
        "WBTC": "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6",
        "MATIC": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",  # WMATIC
        "LINK": "0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39",
        "BNB":  "0x3BA4c387f786bFEE076A58914F5Bd38d668B42c3",  # Binance-Peg BNB
    },
    # Arbitrum
    42161: {
        "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "BRZ":  "0x65553aD3B40c1Ce3875B8f53d80bee027590A3a5",
    },
    # Base
    8453: {
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "BRLA": "0xE9185Ee218cae427aF7B9764A011bb89FeA761B4",  # Stabull Base pool
    },
}

# Catálogo expandido do Motor 2 solicitado.
# Obs: XRP/TRX/DOT/SOL ainda não entram no scanner por padrão até validação
# de contratos wrapped + liquidez mínima + rota consistente de execução.
CRYPTO_EXTRA_CATALOG = ["XRP", "TRX", "DOT", "SOL"]

# ─── TODOS os pares monitorados por rede ──────────────────────────────────────
#
# Lógica: qualquer par onde houver liquidez real confirmada.
# BRL vs USD  → arb entre stablecoin BR e dólar digital
# BRL vs BRL  → arb entre emissores diferentes (+ oportunidades, - concorrência)
#
PARES_MONITORADOS = {

    # ── FOCO ATUAL: Polygon ──────────────────────────────────────────────────
    # Hub principal de liquidez BRL. Carteira precisa de: USDT + POL (gas).
    137: [
        # BRL vs USD  (pares com maior volume)
        ("BRZ",  "USDT"),
        ("BRZ",  "USDC"),
        ("BRZ",  "DAI"),
        ("BRLA", "USDT"),
        ("BRLA", "USDC"),
        ("BRLA", "DAI"),
        ("BRL1", "USDT"),
        ("BRL1", "USDC"),
        ("BRL1", "DAI"),
        # BRL vs BRL  ← maior alpha, menor concorrência
        ("BRZ",  "BRLA"),
        ("BRZ",  "BRL1"),
        ("BRLA", "BRL1"),
    ],

    # ── Outras redes (desativadas — ativar quando capital justificar) ────────
    # Ethereum: gas muito alto, só vale com > $1000 de capital
    # 1: [("BRZ", "USDT"), ("BRZ", "USDC")],
    #
    # Arbitrum: pouca liquidez BRL ainda
    # 42161: [("BRZ", "USDT"), ("BRZ", "USDC")],
    #
    # Base: BRLA crescendo — ativar quando liquidez aumentar
    # 8453: [("BRLA", "USDC")],
}


CRYPTO_ENGINE_ENABLED = _env_bool("CRYPTO_ENGINE_ENABLED", False)
CRYPTO_EXTRA_ENABLED = _env_bool("CRYPTO_EXTRA_ENABLED", False)

# Core de monitoramento do Motor 2 (baixo risco operacional inicial)
CRYPTO_PARES_CORE = {
    137: [
        ("WETH", "USDT"),
        ("WETH", "USDC"),
        ("WBTC", "USDT"),
        ("WBTC", "USDC"),
        ("MATIC", "USDT"),
        ("MATIC", "USDC"),
        ("LINK", "USDT"),
        ("LINK", "USDC"),
        ("BNB",  "USDT"),
        ("BNB",  "USDC"),
    ]
}

# Extras solicitados ficam preparados por flag; pares serão ativados após
# validação de endereço wrapped + liquidez + execução nas DEXs.
CRYPTO_PARES_EXTRA: dict[int, list[tuple[str, str]]] = {
    137: []
}


def _merge_pairs(
    base: dict[int, list[tuple[str, str]]],
    extra: dict[int, list[tuple[str, str]]],
) -> dict[int, list[tuple[str, str]]]:
    merged: dict[int, list[tuple[str, str]]] = {
        chain_id: list(pares) for chain_id, pares in base.items()
    }
    for chain_id, pares in extra.items():
        merged.setdefault(chain_id, [])
        merged[chain_id].extend(pares)
    return merged


def pares_por_estrategia(strategy: str) -> dict[int, list[tuple[str, str]]]:
    """Retorna pares monitorados de acordo com a estratégia Atlas."""
    s = (strategy or "stable").strip().lower()

    crypto_pairs: dict[int, list[tuple[str, str]]] = {}
    if CRYPTO_ENGINE_ENABLED:
        crypto_pairs = {k: list(v) for k, v in CRYPTO_PARES_CORE.items()}
        if CRYPTO_EXTRA_ENABLED:
            crypto_pairs = _merge_pairs(crypto_pairs, CRYPTO_PARES_EXTRA)

    if s == "crypto":
        return crypto_pairs

    if s == "hybrid":
        return _merge_pairs(PARES_MONITORADOS, crypto_pairs)

    return {k: list(v) for k, v in PARES_MONITORADOS.items()}

# ─── Thresholds ───────────────────────────────────────────────────────────────
MIN_SPREAD_PCT     = _env_float("MIN_SPREAD_PCT", 0.35)      # % mínimo bruto para calcular
MIN_LUCRO_USD      = _env_float("MIN_LUCRO_USD", 0.30)       # lucro líquido mínimo em USD para alertar
SLIPPAGE_PCT       = _env_float("SLIPPAGE_PCT", 0.3)         # slippage estimado no cálculo de oportunidade (%)
AMOUNT_USDT_PADRAO = _env_float("AMOUNT_USDT_PADRAO", 100)   # tamanho padrão de simulação (USD equivalente)
INTERVALO_SCAN_SEG = _env_int("INTERVALO_SCAN_SEG", 15)       # intervalo entre scans em segundos

# Filtro opcional de quotes USD para monitoramento.
# Ex.: USD_QUOTES_PERMITIDAS=USDT para operar apenas pares contra USDT.
_quotes_env = os.getenv("USD_QUOTES_PERMITIDAS", "USDT,USDC,DAI")
_quotes_validas = {"USDT", "USDC", "DAI"}
_quotes_parsed = {q.strip().upper() for q in _quotes_env.split(",") if q.strip()}

# Evita travar o scanner quando a env vem vazia/inválida no deploy.
USD_QUOTES_PERMITIDAS = (_quotes_parsed & _quotes_validas) or _quotes_validas

# ─── Notas de liquidez (referência para ajuste de amount) ────────────────────
# Polygon BRZ/USDT pool:  ~$41k liquidez  (Uniswap V4)
# Polygon BRLA pools:     liquidez variável via Stabull
# Polygon BRL1:           liquidez institucional via Cainvest/RFQ
# Ethereum BRZ:           menor liquidez DEX, mais spread potencial
# Base BRLA/USDC:         ~$26M volume semestral (crescendo rápido)
#
# RECOMENDAÇÃO: começar com AMOUNT_USDT_PADRAO=100 em Polygon/Base
# Ethereum: só faz sentido com capital > $1000 pelo gas alto
