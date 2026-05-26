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
    SLIPPAGE_PCT, AMOUNT_USDT_PADRAO
)
from engine.prices import buscar_todos_precos
from engine.executor import executar_swap
from vault.vault import get_user, registrar_operacao

logger = logging.getLogger(__name__)

AUTO_COOLDOWN_SEG = int(os.getenv("AUTO_COOLDOWN_SEG", "45"))


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
    amount_usd:   float


# Gas estimado por rede (units × gwei × eth_price)
GAS_ESTIMADO = {1: 150_000, 137: 80_000, 42161: 900_000, 8453: 80_000}
GWEI_MEDIO   = {1: 20,      137: 50,     42161: 0.1,     8453: 0.005}
FEE_POOL     = {1: 0.01,    137: 0.01,   42161: 0.01,    8453: 0.01}


def estimar_gas_usd(chain_id: int) -> float:
    gas   = GAS_ESTIMADO.get(chain_id, 100_000)
    gwei  = GWEI_MEDIO.get(chain_id, 10)
    eth   = NETWORKS.get(chain_id, {}).get("gas_token_usd", 3000)
    return gas * gwei * 1e-9 * eth


def estimar_fee_swap(chain_id: int, amount_usd: float) -> float:
    return amount_usd * FEE_POOL.get(chain_id, 0.05) / 100


async def detectar_oportunidades(amount_usd: float = AMOUNT_USDT_PADRAO) -> list[Oportunidade]:
    precos = await buscar_todos_precos()
    oportunidades = []

    for chain_id, pares in PARES_MONITORADOS.items():
        precos_rede = precos.get(chain_id, {})
        rede_nome   = NETWORKS[chain_id]["name"]

        for token_brl, token_usd in pares:
            preco_brl = precos_rede.get(token_brl)
            preco_usd = precos_rede.get(token_usd)
            if not preco_brl or not preco_usd:
                continue

            spread_pct   = abs(preco_brl - preco_usd) / preco_usd * 100
            if spread_pct < MIN_SPREAD_PCT:
                continue

            gas_usd      = estimar_gas_usd(chain_id)
            fee_swap_usd = estimar_fee_swap(chain_id, amount_usd)
            slippage_usd = amount_usd * SLIPPAGE_PCT / 100
            lucro_usd    = amount_usd * spread_pct / 100 - gas_usd - fee_swap_usd - slippage_usd

            if lucro_usd < MIN_LUCRO_USD:
                continue

            direcao = (
                f"Compra {token_brl} → Vende {token_usd}"
                if preco_brl < preco_usd
                else f"Compra {token_usd} → Vende {token_brl}"
            )

            oportunidades.append(Oportunidade(
                chain_id=chain_id, rede=rede_nome,
                token_brl=token_brl, token_usd=token_usd,
                preco_brl=preco_brl, preco_usd=preco_usd,
                spread_pct=spread_pct, fee_swap_usd=fee_swap_usd,
                gas_usd=gas_usd, lucro_usd=lucro_usd,
                direcao=direcao, amount_usd=amount_usd,
            ))

    oportunidades.sort(key=lambda o: o.lucro_usd, reverse=True)
    return oportunidades


async def loop_usuario(telegram_id: int, bot, bot_data: dict, intervalo: int = 20):
    """Loop contínuo — envia alerta com botões inline ao detectar oportunidade."""
    from bot.handlers import montar_alerta

    logger.info(f"[uid={telegram_id}] Loop iniciado.")
    while bot_data.get(f"running_{telegram_id}", False):
        try:
            oportunidades = await detectar_oportunidades()
            if oportunidades:
                melhor = oportunidades[0]
                user = get_user(telegram_id)
                if not user:
                    logger.warning(f"[uid={telegram_id}] Usuário não encontrado no vault, encerrando loop.")
                    bot_data[f"running_{telegram_id}"] = False
                    break

                modo = user.get("trading_mode", "manual")
                token_from = melhor.token_usd if melhor.preco_brl < melhor.preco_usd else melhor.token_brl
                token_to = melhor.token_brl if melhor.preco_brl < melhor.preco_usd else melhor.token_usd
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
                                    f"🟢 Spread: `{melhor.spread_pct:.3f}%`\n"
                                    f"🟡 Lucro est.: `${melhor.lucro_usd:.4f}`\n\n"
                                    f"🔗 [Ver no explorer]({explorer})"
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
                    texto, teclado = montar_alerta(melhor)
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=texto,
                        parse_mode="Markdown",
                        reply_markup=teclado
                    )
                    logger.info(f"[uid={telegram_id}] Alerta enviado: {melhor.rede} {melhor.token_brl}/{melhor.token_usd}")
        except Exception as e:
            logger.error(f"[uid={telegram_id}] Erro no loop: {e}")

        await asyncio.sleep(intervalo)

    logger.info(f"[uid={telegram_id}] Loop encerrado.")
