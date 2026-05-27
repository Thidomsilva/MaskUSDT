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
    USD_QUOTES_PERMITIDAS,
)
from engine.prices import buscar_todos_precos, cotacao_usd_brl_atual, buscar_saldo_polygon
from engine.executor import executar_swap
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


AUTO_COOLDOWN_SEG = _env_int("AUTO_COOLDOWN_SEG", 45)
MANUAL_ALERT_COOLDOWN_SEG = _env_int("MANUAL_ALERT_COOLDOWN_SEG", 45)
TOKENS_USD = {"USDT", "USDC", "DAI"}
TOKENS_BRL = {"BRZ", "BRLA", "BRL1"}
INVENTORY_MIN_USD = float(os.getenv("INVENTORY_MIN_USD", "5"))


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
) -> list[Oportunidade]:
    precos = await buscar_todos_precos()
    usd_brl = await cotacao_usd_brl_atual()
    preco_brl_teorico_usd = 1.0 / usd_brl if usd_brl > 0 else 0.2
    oportunidades = []

    for chain_id, pares in PARES_MONITORADOS.items():
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
                if preco_brl < preco_usd:
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
            if estrategia == "crypto":
                aviso_key = f"strategy_warn_crypto_{telegram_id}"
                if not bot_data.get(aviso_key):
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=(
                            "🚧 *Crypto Chain ainda em preparação*\n\n"
                            "No momento o Motor 2 ainda não executa operações.\n"
                            "Use /estrategia para voltar ao modo Stable ou Híbrido."
                        ),
                        parse_mode="Markdown",
                    )
                    bot_data[aviso_key] = True
                await asyncio.sleep(intervalo)
                continue

            if estrategia == "hybrid":
                aviso_key = f"strategy_warn_hybrid_{telegram_id}"
                if not bot_data.get(aviso_key):
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=(
                            "🧪 *Híbrido ativo (fase 1)*\n\n"
                            "Por enquanto o Híbrido está operando o Motor Stable.\n"
                            "O Motor Crypto será conectado nas próximas versões."
                        ),
                        parse_mode="Markdown",
                    )
                    bot_data[aviso_key] = True

            saldos_por_chain = {}
            dex_address = user.get("dex_address")
            if dex_address:
                try:
                    saldos_polygon = await buscar_saldo_polygon(dex_address)
                    if isinstance(saldos_polygon, dict):
                        tem_saldo_confiavel = any(
                            saldos_polygon.get(sym) is not None
                            for sym in (*TOKENS_USD, *TOKENS_BRL)
                        )
                        if not tem_saldo_confiavel:
                            logger.warning(
                                f"[uid={telegram_id}] Saldos indisponíveis via RPC; seguindo scanner sem filtro de saldo."
                            )
                        else:
                            saldos_por_chain[137] = saldos_polygon
                except Exception as e:
                    logger.warning(f"[uid={telegram_id}] Falha ao consultar saldo da Polygon: {e}")

            oportunidades = await detectar_oportunidades(saldos_por_chain=saldos_por_chain)
            if oportunidades:
                melhor = oportunidades[0]

                modo = user.get("trading_mode", "manual")
                token_from = melhor.token_from
                token_to = melhor.token_to
                par_exec = f"{token_from}/{token_to}"

                if modo == "auto":
                    last_key = f"auto_last_exec_{telegram_id}"
                    now = time.time()
                    ultimo = float(bot_data.get(last_key, 0.0))

                    if now - ultimo >= AUTO_COOLDOWN_SEG:
                        bot_data[last_key] = now

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
                            and token_to in TOKENS_BRL
                        ):
                            recebido_wei = int(resultado.get("received_token_wei") or 0)
                            recebido = resultado.get("received_token_amount_str")
                            if recebido_wei > 0 and recebido:
                                resultado_volta = await executar_swap(
                                    chain_id=melhor.chain_id,
                                    token_from=token_to,
                                    token_to=token_from,
                                    amount_usd=recebido,
                                    wallet=user["dex_address"],
                                    private_key=user["dex_pk"],
                                )
                                if resultado_volta.get("sucesso"):
                                    usd_recebido = float(resultado_volta.get("received_token_amount") or 0)
                                    lucro_real = usd_recebido - float(melhor.amount_usd)
                                    ciclo_txt = (
                                        f"\n\n🔁 Ciclo fechado `{token_to}->{token_from}`"
                                        f"\n💰 Lucro realizado: `${lucro_real:.4f}`"
                                    )
                                else:
                                    ciclo_txt = (
                                        f"\n\n⚠️ Ciclo não fechado (saldo em {token_to})."
                                        f"\nErro volta: `{resultado_volta.get('erro', 'desconhecido')}`"
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
                    sig = (
                        f"{melhor.chain_id}|{melhor.token_brl}|{melhor.token_usd}|"
                        f"{token_from}|{token_to}|{round(melhor.amount_usd, 2)}"
                    )
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
