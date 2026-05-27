"""
arbitrage.py — Detecta spreads e envia alertas com botões inline.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from config import (
    TOKENS, NETWORKS, PARES_MONITORADOS,
    MIN_SPREAD_PCT, MIN_LUCRO_USD,
    SLIPPAGE_PCT, AMOUNT_USDT_PADRAO,
    USD_QUOTES_PERMITIDAS, pares_por_estrategia,
)
from engine.prices import (
    buscar_todos_precos,
    buscar_precos_multifonte,
    cotacao_usd_brl_atual,
    buscar_saldo_polygon,
)
from engine.executor import executar_swap, estimar_retorno_swap
from vault.vault import get_user, registrar_operacao

logger = logging.getLogger(__name__)

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
        logger.warning("Variável %s inválida (%r); usando padrão %s", nome, raw, default)
        return default


def _env_bool(nome: str, default: bool = False) -> bool:
    raw = os.getenv(nome)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


AUTO_COOLDOWN_SEG = _env_int("AUTO_COOLDOWN_SEG", 45)
MANUAL_ALERT_COOLDOWN_SEG = _env_int("MANUAL_ALERT_COOLDOWN_SEG", 45)
TOKENS_USD = {"USDT", "USDC", "DAI"}
TOKENS_BRL = {"BRZ", "BRLA", "BRL1"}
INVENTORY_MIN_USD = float(os.getenv("INVENTORY_MIN_USD", "0.5"))
MONITOR_IGNORE_BALANCE = _env_bool("MONITOR_IGNORE_BALANCE", False)
ENABLE_NON_BRL_SPREAD = _env_bool("ENABLE_NON_BRL_SPREAD", False)
CRYPTO_MULTISOURCE_MIN_FONTES = _env_int("CRYPTO_MULTISOURCE_MIN_FONTES", 2)
CRYPTO_MAX_SPREAD_PCT = float(os.getenv("CRYPTO_MAX_SPREAD_PCT", "8"))
CLOSE_CYCLE_MIN_PROFIT_USD = float(os.getenv("CLOSE_CYCLE_MIN_PROFIT_USD", "0.05"))


@dataclass
class Oportunidade:
    chain_id:     int
    rede:         str
    token_brl:    str
    token_usd:    str
    preco_brl:    float
    preco_usd:    float
    spread_pct:   float
    fee_swap_usd: float
    gas_usd:      float
    lucro_usd:    float
    direcao:      str
    token_from:   str
    token_to:     str
    amount_usd:   float
    amount_usd_equiv: float


def _assinatura_oportunidade(oport: Oportunidade) -> str:
    return (
        f"{oport.chain_id}|{oport.token_brl}|{oport.token_usd}|"
        f"{oport.token_from}|{oport.token_to}|{round(oport.amount_usd, 2)}"
    )


def _grupo_oportunidade(oport: Oportunidade) -> str:
    # Pares BRL/* entram como estratégia stable; demais pares entram como crypto.
    if oport.token_brl in TOKENS_BRL or oport.token_usd in TOKENS_BRL:
        return "stable"
    return "crypto"


def _selecionar_oportunidade_auto(
    oportunidades: list[Oportunidade],
    estrategia: str,
    last_sig_auto: str | None,
    last_group_auto: str | None,
) -> Oportunidade:
    if not oportunidades:
        raise ValueError("Lista de oportunidades vazia")

    candidatas = [o for o in oportunidades if _assinatura_oportunidade(o) != last_sig_auto]
    if not candidatas:
        candidatas = oportunidades

    if (estrategia or "").strip().lower() == "hybrid":
        alvo = "stable" if (last_group_auto == "crypto") else "crypto"
        for o in candidatas:
            if _grupo_oportunidade(o) == alvo:
                return o

    return candidatas[0]


def _close_cycle_intermediarios(token_from: str, token_to: str) -> list[str]:
    raw = os.getenv("CLOSE_CYCLE_INTERMEDIATE_TOKENS", "USDC,DAI,USDT,BRZ,BRLA,BRL1")
    itens = [t.strip().upper() for t in raw.split(",") if t.strip()]
    vistos: set[str] = set()
    saida: list[str] = []
    for t in itens:
        if t in vistos:
            continue
        vistos.add(t)
        if t in {token_from, token_to}:
            continue
        saida.append(t)
    return saida


def _close_cycle_targets(token_base: str) -> list[str]:
    raw = os.getenv("CLOSE_CYCLE_TARGET_TOKENS", ",".join(sorted(USD_QUOTES_PERMITIDAS)))
    itens = [t.strip().upper() for t in raw.split(",") if t.strip()]

    vistos: set[str] = set()
    saida: list[str] = []

    if token_base in TOKENS_USD:
        vistos.add(token_base)
        saida.append(token_base)

    for t in itens:
        if t in vistos:
            continue
        if t not in TOKENS_USD:
            continue
        vistos.add(t)
        saida.append(t)

    if not saida:
        saida = [token_base] if token_base in TOKENS_USD else ["USDT"]

    return saida


async def _planejar_fechamento_ciclo(
    chain_id: int,
    token_base: str,
    token_inventory: str,
    amount_token_inventory: str,
    wallet: str,
) -> dict:
    candidatos: list[dict] = []

    for alvo in _close_cycle_targets(token_base=token_base):
        direto = await estimar_retorno_swap(
            chain_id=chain_id,
            token_from=token_inventory,
            token_to=alvo,
            amount_usd=amount_token_inventory,
            wallet=wallet,
        )
        if direto.get("sucesso"):
            candidatos.append({
                "path": [token_inventory, alvo],
                "retorno_estimado": float(direto.get("expected_out_amount") or 0),
            })

        for mid in _close_cycle_intermediarios(token_from=alvo, token_to=token_inventory):
            leg1 = await estimar_retorno_swap(
                chain_id=chain_id,
                token_from=token_inventory,
                token_to=mid,
                amount_usd=amount_token_inventory,
                wallet=wallet,
            )
            if not leg1.get("sucesso"):
                continue

            amount_mid = leg1.get("expected_out_amount_str")
            if not amount_mid:
                continue

            leg2 = await estimar_retorno_swap(
                chain_id=chain_id,
                token_from=mid,
                token_to=alvo,
                amount_usd=amount_mid,
                wallet=wallet,
            )
            if not leg2.get("sucesso"):
                continue

            candidatos.append({
                "path": [token_inventory, mid, alvo],
                "retorno_estimado": float(leg2.get("expected_out_amount") or 0),
            })

    if not candidatos:
        return {
            "sucesso": False,
            "erro": "Sem rota de fechamento viável (direta ou intermediária).",
        }

    melhor = max(candidatos, key=lambda x: float(x.get("retorno_estimado") or 0))
    return {
        "sucesso": True,
        "path": melhor["path"],
        "retorno_estimado": float(melhor.get("retorno_estimado") or 0),
    }


# Gas estimado por rede (units × gwei × eth_price)
GAS_ESTIMADO = {1: 150_000, 137: 80_000, 42161: 900_000, 8453: 80_000}
GWEI_MEDIO   = {1: 20,      137: 50,     42161: 0.1,     8453: 0.005}
FEE_POOL     = {1: 0.01,    137: 0.01,   42161: 0.01,    8453: 0.01}

# ─── Sanidade de preços ────────────────────────────────────────────────────────
# Preço de tokens BRL em USD: BRL/USD razoável entre 2.2 e 12 → 0.083 a 0.45
BRL_PRECO_MIN_USD = 0.07
BRL_PRECO_MAX_USD = 0.55
# Spread máximo real para BRL/USD. Acima = erro de API, não oportunidade.
MAX_SPREAD_BRL_USD_PCT = 6.0


def estimar_gas_usd(chain_id: int) -> float:
    gas   = GAS_ESTIMADO.get(chain_id, 100_000)
    gwei  = GWEI_MEDIO.get(chain_id, 10)
    eth   = NETWORKS.get(chain_id, {}).get("gas_token_usd", 3000)
    return gas * gwei * 1e-9 * eth


def estimar_fee_swap(chain_id: int, amount_usd: float) -> float:
    return amount_usd * FEE_POOL.get(chain_id, 0.05) / 100


async def detectar_oportunidades(
    amount_usd: float = AMOUNT_USDT_PADRAO,
    saldos_por_chain: dict[int, dict[str, float]] | None = None,
    pares_monitorados: dict[int, list[tuple[str, str]]] | None = None,
) -> list[Oportunidade]:
    pares_ativos = pares_monitorados or PARES_MONITORADOS
    precos = await buscar_todos_precos(pares_monitorados=pares_ativos)
    precos_multifonte = await buscar_precos_multifonte(pares_monitorados=pares_ativos)
    usd_brl = await cotacao_usd_brl_atual()
    preco_brl_teorico_usd = 1.0 / usd_brl if usd_brl > 0 else 0.2
    oportunidades = []

    for chain_id, pares in pares_ativos.items():
        precos_rede = precos.get(chain_id, {})
        rede_nome   = NETWORKS[chain_id]["name"]

        for token_brl, token_usd in pares:
            # Permite restringir monitoramento para uma quote USD específica (ex.: só USDT).
            if token_usd in TOKENS_USD and token_usd not in USD_QUOTES_PERMITIDAS:
                continue

            preco_brl = precos_rede.get(token_brl)
            preco_usd = precos_rede.get(token_usd)
            if not preco_brl or not preco_usd:
                continue

            # Para BRL/USD: medir desvio da paridade cambial (evita falso 80%+ constante).
            if token_brl in TOKENS_BRL and token_usd in TOKENS_USD:
                # Rejeita preços de API fora do range BRL/USD plausível (0.07–0.55 USD)
                if not (BRL_PRECO_MIN_USD <= preco_brl <= BRL_PRECO_MAX_USD):
                    logger.debug(
                        f"[{token_brl}] preço {preco_brl:.6f} USD fora do range plausível "
                        f"[{BRL_PRECO_MIN_USD}, {BRL_PRECO_MAX_USD}] — ignorado"
                    )
                    continue
                spread_pct = abs(preco_brl - preco_brl_teorico_usd) / preco_brl_teorico_usd * 100
                if spread_pct > MAX_SPREAD_BRL_USD_PCT:
                    logger.debug(
                        f"[{token_brl}/{token_usd}] spread {spread_pct:.2f}% "
                        f"> cap {MAX_SPREAD_BRL_USD_PCT}% — descartado (possível erro de API)"
                    )
                    continue
            # Para BRL/BRL: comparar emissor vs emissor (paridade ideal 1:1).
            elif token_brl in TOKENS_BRL and token_usd in TOKENS_BRL:
                # Ambos devem ter preço USD plausível para BRL
                if not (BRL_PRECO_MIN_USD <= preco_brl <= BRL_PRECO_MAX_USD):
                    logger.debug(
                        f"[{token_brl}] preço {preco_brl:.6f} USD fora do range plausível — ignorado"
                    )
                    continue
                if not (BRL_PRECO_MIN_USD <= preco_usd <= BRL_PRECO_MAX_USD):
                    logger.debug(
                        f"[{token_usd}] preço {preco_usd:.6f} USD fora do range plausível — ignorado"
                    )
                    continue
                denom = max(preco_brl, preco_usd)
                spread_pct = abs(preco_brl - preco_usd) / denom * 100 if denom > 0 else 0
                # Dois emissores BRL nunca devem divergir > MAX_SPREAD_BRL_USD_PCT
                if spread_pct > MAX_SPREAD_BRL_USD_PCT:
                    logger.debug(
                        f"[{token_brl}/{token_usd}] spread {spread_pct:.2f}% "
                        f"> cap {MAX_SPREAD_BRL_USD_PCT}% — descartado (possível erro de API)"
                    )
                    continue
            else:
                # Crypto vs USD: usa dispersão multifonte do MESMO ativo para evitar
                # falso positivo por comparar preços absolutos de tokens diferentes.
                if token_usd in TOKENS_USD and token_brl not in TOKENS_BRL:
                    fontes = (precos_multifonte.get(chain_id, {}).get(token_brl) or {})
                    if len(fontes) < CRYPTO_MULTISOURCE_MIN_FONTES:
                        continue

                    precos_fontes = sorted(float(v) for v in fontes.values() if float(v) > 0)
                    if len(precos_fontes) < CRYPTO_MULTISOURCE_MIN_FONTES:
                        continue

                    preco_min = precos_fontes[0]
                    preco_max = precos_fontes[-1]
                    spread_pct = (preco_max - preco_min) / preco_min * 100 if preco_min > 0 else 0

                    if spread_pct > CRYPTO_MAX_SPREAD_PCT:
                        logger.debug(
                            f"[{token_brl}/{token_usd}] spread crypto {spread_pct:.2f}% "
                            f"> cap {CRYPTO_MAX_SPREAD_PCT}% — descartado (outlier de fonte)"
                        )
                        continue
                elif not ENABLE_NON_BRL_SPREAD:
                    logger.debug(
                        f"[{token_brl}/{token_usd}] par não-BRL ignorado "
                        "(ENABLE_NON_BRL_SPREAD=false)."
                    )
                    continue
                else:
                    spread_pct = abs(preco_brl - preco_usd) / preco_usd * 100

            if spread_pct < MIN_SPREAD_PCT:
                continue

            if token_brl in TOKENS_BRL and token_usd in TOKENS_USD:
                if preco_brl < preco_brl_teorico_usd:
                    token_from, token_to = token_usd, token_brl
                    direcao = f"Compra {token_brl} → Vende {token_usd}"
                else:
                    token_from, token_to = token_brl, token_usd
                    direcao = f"Compra {token_usd} → Vende {token_brl}"
            elif token_brl in TOKENS_BRL and token_usd in TOKENS_BRL:
                if preco_brl < preco_usd:
                    token_from, token_to = token_usd, token_brl
                    direcao = f"Compra {token_brl} → Vende {token_usd}"
                else:
                    token_from, token_to = token_brl, token_usd
                    direcao = f"Compra {token_usd} → Vende {token_brl}"
            else:
                if token_usd in TOKENS_USD and token_brl not in TOKENS_BRL:
                    # Para crypto, opera inventário em stable e fecha ciclo em USD.
                    token_from, token_to = token_usd, token_brl
                    direcao = f"Compra {token_brl} (multifonte) → Fecha em {token_usd}"
                elif preco_brl < preco_usd:
                    token_from, token_to = token_usd, token_brl
                    direcao = f"Compra {token_brl} → Vende {token_usd}"
                else:
                    token_from, token_to = token_brl, token_usd
                    direcao = f"Compra {token_usd} → Vende {token_brl}"

            saldos_rede = (saldos_por_chain or {}).get(chain_id) or {}
            amount_usd_equiv = float(amount_usd)
            amount_token_from = float(amount_usd)

            if saldos_rede:
                saldo_raw = saldos_rede.get(token_from)

                # Só aplica filtro estrito quando o saldo do token foi obtido com sucesso.
                # Se RPC falhar e vier None, mantém fallback por AMOUNT_USDT_PADRAO.
                if saldo_raw is not None:
                    saldo_token_from = float(saldo_raw or 0)
                    if saldo_token_from <= 0:
                        continue

                    preco_token_from_usd = 1.0 if token_from in TOKENS_USD else float(precos_rede.get(token_from) or 0)
                    if preco_token_from_usd <= 0:
                        continue

                    saldo_usd_equiv = saldo_token_from * preco_token_from_usd
                    amount_usd_equiv = min(float(amount_usd), saldo_usd_equiv)
                    if amount_usd_equiv < INVENTORY_MIN_USD:
                        continue

                    amount_token_from = amount_usd_equiv / preco_token_from_usd

            gas_usd      = estimar_gas_usd(chain_id)
            fee_swap_usd = estimar_fee_swap(chain_id, amount_usd_equiv)
            slippage_usd = amount_usd_equiv * SLIPPAGE_PCT / 100
            lucro_usd    = amount_usd_equiv * spread_pct / 100 - gas_usd - fee_swap_usd - slippage_usd

            if lucro_usd <= 0:
                continue

            if lucro_usd < MIN_LUCRO_USD:
                continue

            oportunidades.append(Oportunidade(
                chain_id=chain_id, rede=rede_nome,
                token_brl=token_brl, token_usd=token_usd,
                preco_brl=preco_brl, preco_usd=preco_usd,
                spread_pct=spread_pct, fee_swap_usd=fee_swap_usd,
                gas_usd=gas_usd, lucro_usd=lucro_usd,
                direcao=direcao, token_from=token_from, token_to=token_to,
                amount_usd=amount_token_from,
                amount_usd_equiv=amount_usd_equiv,
            ))

    oportunidades.sort(key=lambda o: o.lucro_usd, reverse=True)
    return oportunidades


async def loop_usuario(telegram_id: int, bot, bot_data: dict, intervalo: int = 20):
    """Loop contínuo — envia alerta com botões inline ao detectar oportunidade."""
    from bot.handlers import montar_alerta

    logger.info(f"[uid={telegram_id}] Loop iniciado.")
    while bot_data.get(f"running_{telegram_id}", False):
        try:
            user = get_user(telegram_id, include_pk=True)
            if not user:
                logger.warning(f"[uid={telegram_id}] Usuário não encontrado no vault, encerrando loop.")
                bot_data[f"running_{telegram_id}"] = False
                break

            estrategia = (user.get("strategy") or "stable").strip().lower()
            pares_ativos = pares_por_estrategia(estrategia)
            if not pares_ativos:
                aviso_key = f"strategy_warn_empty_{telegram_id}_{estrategia}"
                if not bot_data.get(aviso_key):
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=(
                            "🚧 *Estratégia selecionada sem pares ativos*\n\n"
                            "Ative o Motor Crypto em `CRYPTO_ENGINE_ENABLED=true` no ambiente."
                        ),
                        parse_mode="Markdown",
                    )
                    bot_data[aviso_key] = True
                await asyncio.sleep(intervalo)
                continue

            saldos_por_chain = {}
            auto_exec_bloqueada_msg = None
            dex_address = user.get("dex_address")
            if MONITOR_IGNORE_BALANCE:
                logger.debug(f"[uid={telegram_id}] MONITOR_IGNORE_BALANCE=true (watch-only)")
                auto_exec_bloqueada_msg = (
                    "Execução automática desativada: MONITOR_IGNORE_BALANCE=true "
                    "mantém o scanner em watch-only."
                )
            elif dex_address:
                try:
                    saldos_polygon = await buscar_saldo_polygon(dex_address)
                    if isinstance(saldos_polygon, dict):
                        tem_saldo_confiavel = any(
                            saldos_polygon.get(sym) is not None
                            for sym in (*TOKENS_USD, *TOKENS_BRL)
                        )
                        if not tem_saldo_confiavel:
                            auto_exec_bloqueada_msg = (
                                "Execução automática desativada: saldo indisponível via RPC "
                                "para validar o token de entrada."
                            )
                            logger.warning(
                                f"[uid={telegram_id}] Saldos indisponíveis via RPC; auto ficará bloqueado até normalizar."
                            )
                        else:
                            saldos_por_chain[137] = saldos_polygon
                except Exception as e:
                    auto_exec_bloqueada_msg = (
                        "Execução automática desativada: falha ao consultar saldo on-chain."
                    )
                    logger.warning(f"[uid={telegram_id}] Falha ao consultar saldo da Polygon: {e}")
            else:
                auto_exec_bloqueada_msg = (
                    "Execução automática desativada: carteira DEX não configurada."
                )

            # --- Filtros personalizados do usuário ---
            moedas_usuario = bot_data.get(f"moedas_usuario_{telegram_id}")
            spread_usuario = bot_data.get(f"spread_usuario_{telegram_id}")
            lucro_usuario = bot_data.get(f"lucro_usuario_{telegram_id}")

            # Filtra pares monitorados pelas moedas escolhidas, se houver
            pares_filtrados = {}
            if moedas_usuario:
                for chain_id, pares in pares_ativos.items():
                    filtrados = [p for p in pares if p[0] in moedas_usuario or p[1] in moedas_usuario]
                    if filtrados:
                        pares_filtrados[chain_id] = filtrados
            else:
                pares_filtrados = pares_ativos

            # Chama detectar_oportunidades normalmente
            oportunidades = await detectar_oportunidades(
                saldos_por_chain=saldos_por_chain,
                pares_monitorados=pares_filtrados,
            )

            # Aplica filtros de spread e lucro mínimo do usuário, se definidos
            if spread_usuario is not None:
                oportunidades = [o for o in oportunidades if o.spread_pct >= spread_usuario]
            if lucro_usuario is not None:
                oportunidades = [o for o in oportunidades if o.lucro_usd >= lucro_usuario]
            if oportunidades:
                modo = user.get("trading_mode", "manual")
                melhor = oportunidades[0]
                if modo == "auto":
                    last_sig_auto = bot_data.get(f"auto_last_sig_{telegram_id}")
                    last_group_auto = bot_data.get(f"auto_last_group_{telegram_id}")
                    melhor = _selecionar_oportunidade_auto(
                        oportunidades=oportunidades,
                        estrategia=estrategia,
                        last_sig_auto=last_sig_auto,
                        last_group_auto=last_group_auto,
                    )

                token_from = melhor.token_from
                token_to = melhor.token_to
                par_exec = f"{token_from}/{token_to}"

                if modo == "auto":
                    guard_key = f"auto_exec_guard_{telegram_id}"
                    if auto_exec_bloqueada_msg:
                        if bot_data.get(guard_key) != auto_exec_bloqueada_msg:
                            bot_data[guard_key] = auto_exec_bloqueada_msg
                            await bot.send_message(
                                chat_id=telegram_id,
                                text=(
                                    "⚠️ *Modo automático em observação*\n\n"
                                    f"{auto_exec_bloqueada_msg}\n"
                                    "O scanner continua monitorando, mas não vai enviar swap até conseguir validar saldo real."
                                ),
                                parse_mode="Markdown",
                            )
                        await asyncio.sleep(intervalo)
                        continue

                    bot_data.pop(guard_key, None)
                    last_key = f"auto_last_exec_{telegram_id}"
                    last_sig_key = f"auto_last_sig_{telegram_id}"
                    last_group_key = f"auto_last_group_{telegram_id}"
                    now = time.time()
                    ultimo = float(bot_data.get(last_key, 0.0))

                    if now - ultimo >= AUTO_COOLDOWN_SEG:
                        bot_data[last_key] = now
                        bot_data[last_sig_key] = _assinatura_oportunidade(melhor)
                        bot_data[last_group_key] = _grupo_oportunidade(melhor)

                        await bot.send_message(
                            chat_id=telegram_id,
                            text=(
                                "🤖 *Modo automático*\n\n"
                                f"Oportunidade detectada em `{melhor.rede}`\n"
                                f"Par de execução: `{par_exec}`\n"
                                "⏳ Executando compra/venda..."
                            ),
                            parse_mode="Markdown",
                        )

                        resultado = await executar_swap(
                            chain_id=melhor.chain_id,
                            token_from=token_from,
                            token_to=token_to,
                            amount_usd=melhor.amount_usd,
                            wallet=user["dex_address"],
                            private_key=user["dex_pk"],
                        )

                        ciclo_txt = ""
                        close_cycle = os.getenv("CLOSE_CYCLE_ENABLED", "true").strip().lower() in {
                            "1", "true", "yes", "y", "on"
                        }
                        if (
                            resultado.get("sucesso")
                            and close_cycle
                            and token_from in TOKENS_USD
                            and token_to not in TOKENS_USD
                        ):
                            recebido_wei = int(resultado.get("received_token_wei") or 0)
                            recebido = resultado.get("received_token_amount_str")
                            if recebido_wei > 0 and recebido:
                                plano_fechamento = await _planejar_fechamento_ciclo(
                                    chain_id=melhor.chain_id,
                                    token_base=token_from,
                                    token_inventory=token_to,
                                    amount_token_inventory=recebido,
                                    wallet=user["dex_address"],
                                )

                                if not plano_fechamento.get("sucesso"):
                                    ciclo_txt = (
                                        f"\n\n⚠️ Ciclo não fechado (saldo em {token_to})."
                                        "\nSem rota de fechamento viável para evitar fechamento negativo."
                                    )
                                else:
                                    retorno_estimado = float(plano_fechamento.get("retorno_estimado") or 0)
                                    alvo_minimo = float(melhor.amount_usd) + CLOSE_CYCLE_MIN_PROFIT_USD
                                    if retorno_estimado < alvo_minimo:
                                        ciclo_txt = (
                                            f"\n\n⚠️ Ciclo não fechado (saldo em {token_to})."
                                            f"\nRetorno estimado da volta: `${retorno_estimado:.4f}` < `${alvo_minimo:.4f}`."
                                        )
                                    else:
                                        path = plano_fechamento.get("path") or [token_to, token_from]
                                        alvo_final = str(path[-1])
                                        if len(path) == 2:
                                            resultado_volta = await executar_swap(
                                                chain_id=melhor.chain_id,
                                                token_from=token_to,
                                                token_to=alvo_final,
                                                amount_usd=recebido,
                                                wallet=user["dex_address"],
                                                private_key=user["dex_pk"],
                                            )
                                            if resultado_volta.get("sucesso"):
                                                recebido_final = float(resultado_volta.get("received_token_amount") or 0)
                                                lucro_real = recebido_final - float(melhor.amount_usd)
                                                ciclo_txt = (
                                                    f"\n\n🔁 Ciclo fechado `{token_to}->{alvo_final}`"
                                                    f"\n💰 Lucro realizado: `${lucro_real:.4f}`"
                                                )
                                            else:
                                                ciclo_txt = (
                                                    f"\n\n⚠️ Ciclo não fechado (saldo em {token_to})."
                                                    f"\nErro volta ({token_to}->{alvo_final}): `{resultado_volta.get('erro', 'desconhecido')}`"
                                                )
                                        else:
                                            mid = str(path[1])
                                            volta_1 = await executar_swap(
                                                chain_id=melhor.chain_id,
                                                token_from=token_to,
                                                token_to=mid,
                                                amount_usd=recebido,
                                                wallet=user["dex_address"],
                                                private_key=user["dex_pk"],
                                            )
                                            if not volta_1.get("sucesso"):
                                                ciclo_txt = (
                                                    f"\n\n⚠️ Ciclo não fechado (saldo em {token_to})."
                                                    f"\nErro volta 1 ({token_to}->{mid}): `{volta_1.get('erro', 'desconhecido')}`"
                                                )
                                            else:
                                                recebido_mid = volta_1.get("received_token_amount_str")
                                                if not recebido_mid:
                                                    ciclo_txt = (
                                                        f"\n\n⚠️ Ciclo não fechado (saldo em {mid})."
                                                        "\nSem valor recebido para executar a volta final."
                                                    )
                                                else:
                                                    volta_2 = await executar_swap(
                                                        chain_id=melhor.chain_id,
                                                        token_from=mid,
                                                        token_to=alvo_final,
                                                        amount_usd=recebido_mid,
                                                        wallet=user["dex_address"],
                                                        private_key=user["dex_pk"],
                                                    )
                                                    if volta_2.get("sucesso"):
                                                        recebido_final = float(volta_2.get("received_token_amount") or 0)
                                                        lucro_real = recebido_final - float(melhor.amount_usd)
                                                        ciclo_txt = (
                                                            f"\n\n🔁 Ciclo fechado `{token_to}->{mid}->{alvo_final}`"
                                                            f"\n💰 Lucro realizado: `${lucro_real:.4f}`"
                                                        )
                                                    else:
                                                        ciclo_txt = (
                                                            f"\n\n⚠️ Ciclo não fechado (saldo em {mid})."
                                                            f"\nErro volta 2 ({mid}->{alvo_final}): `{volta_2.get('erro', 'desconhecido')}`"
                                                        )

                        if resultado.get("sucesso"):
                            tx_hash = resultado.get("tx_hash", "")
                            explorer = resultado.get("explorer", tx_hash)
                            registrar_operacao(
                                telegram_id,
                                melhor.rede,
                                par_exec,
                                melhor.spread_pct,
                                melhor.lucro_usd,
                                tx_hash,
                                "sucesso",
                            )
                            await bot.send_message(
                                chat_id=telegram_id,
                                text=(
                                    "✅ *Swap executado automaticamente!*\n\n"
                                    f"Rede: `{melhor.rede}`\n"
                                    f"Par: `{par_exec}`\n"
                                    f"Operação: `{melhor.amount_usd:.6f} {token_from}` (~`${melhor.amount_usd_equiv:.2f}`)\n"
                                    f"🟢 Spread: `{melhor.spread_pct:.3f}%`\n"
                                    f"🟡 Lucro est.: `${melhor.lucro_usd:.4f}`\n\n"
                                    f"🔗 [Ver no explorer]({explorer})"
                                    f"{ciclo_txt}"
                                ),
                                parse_mode="Markdown",
                                disable_web_page_preview=True,
                            )
                        else:
                            registrar_operacao(
                                telegram_id,
                                melhor.rede,
                                par_exec,
                                melhor.spread_pct,
                                0,
                                "",
                                "erro",
                            )
                            await bot.send_message(
                                chat_id=telegram_id,
                                text=(
                                    "❌ *Falha no modo automático*\n\n"
                                    f"Rede: `{melhor.rede}`\n"
                                    f"Par: `{par_exec}`\n"
                                    f"Erro: `{resultado.get('erro', 'desconhecido')}`"
                                ),
                                parse_mode="Markdown",
                            )
                    else:
                        logger.info(f"[uid={telegram_id}] Cooldown ativo do auto-trade.")
                else:
                    now = time.time()
                    sig = _assinatura_oportunidade(melhor)
                    last_sig_key = f"manual_last_sig_{telegram_id}"
                    last_ts_key = f"manual_last_alert_ts_{telegram_id}"
                    last_sig = bot_data.get(last_sig_key)
                    last_ts = float(bot_data.get(last_ts_key, 0.0))

                    if sig == last_sig and (now - last_ts) < MANUAL_ALERT_COOLDOWN_SEG:
                        logger.info(f"[uid={telegram_id}] Cooldown ativo do alerta manual.")
                    else:
                        texto, teclado = montar_alerta(melhor, bot_data=bot_data, uid=telegram_id)
                        await bot.send_message(
                            chat_id=telegram_id,
                            text=texto,
                            parse_mode="Markdown",
                            reply_markup=teclado
                        )
                        bot_data[last_sig_key] = sig
                        bot_data[last_ts_key] = now
                        logger.info(f"[uid={telegram_id}] Alerta enviado: {melhor.rede} {melhor.token_brl}/{melhor.token_usd}")
        except Exception as e:
            logger.error(f"[uid={telegram_id}] Erro no loop: {e}")

        await asyncio.sleep(intervalo)

    logger.info(f"[uid={telegram_id}] Loop encerrado.")
