# engine/chain.py
# Gerenciamento dos ciclos de arbitragem em cadeia
import asyncio
import json
from datetime import datetime
from . import prices, arbitrage, executor
from vault import vault

TOKENS_CADEIA = ["USDT","USDC","BRZ","BRLA","BRL1"]

async def loop_cadeia(telegram_id, bot, bot_data):
    while bot_data.get(f"running_{telegram_id}", True):
        posicao = vault.get_posicao_aberta(telegram_id)
        if not posicao:
            # Monitorar oportunidades partindo de USDT
            oportunidade = detectar_proximo_hop("USDT")
            if oportunidade:
                # Alertar usuário ou iniciar ciclo
                pass  # Integrar com bot para alerta
        else:
            if pode_fechar_ciclo(posicao):
                # Alertar usuário para fechar ciclo
                pass
            else:
                oportunidade = detectar_proximo_hop(posicao["token_atual"])
                if oportunidade:
                    # Alertar usuário ou executar hop
                    pass
        await asyncio.sleep(15)

    # Ao fechar ciclo, aguarda e reinicia
    await asyncio.sleep(3)
    if bot_data.get(f"running_{telegram_id}", True):
        asyncio.create_task(loop_cadeia(telegram_id, bot, bot_data))

def iniciar_ciclo(telegram_id, user, bot_data):
    if vault.get_posicao_aberta(telegram_id):
        return  # Já existe ciclo aberto
    oportunidade = detectar_proximo_hop("USDT")
    if not oportunidade:
        return  # Nenhuma oportunidade
    ciclo_numero = vault.get_numero_ciclos(telegram_id) + 1
    saldo_entrada = oportunidade["amount_entrada"]
    token_atual = oportunidade["para"]
    amount_token = oportunidade["amount_saida"]
    vault.criar_posicao(telegram_id, ciclo_numero, token_atual, amount_token, saldo_entrada)
    # Disparar loop em background
    import asyncio
    asyncio.create_task(loop_cadeia(telegram_id, None, bot_data))

def detectar_proximo_hop(token_atual, chain_id=137):
    # Busca pares e calcula spread líquido
    # Exemplo fictício
    oportunidades = []
    for token_para in TOKENS_CADEIA:
        if token_para == token_atual:
            continue
        # Simulação: buscar dados reais de arbitrage/prices
        spread_bruto = 0.5  # Exemplo
        gas = 0.01
        fee = 0.01
        slippage = 0.01
        spread_liquido = spread_bruto - gas - fee - slippage
        if spread_liquido >= 0.35:
            oportunidades.append({
                "de": token_atual,
                "para": token_para,
                "spread_liquido": spread_liquido,
                "amount_entrada": 100.0,
                "amount_saida": 100.0 * (1 + spread_liquido/100),
                "spread_bruto": spread_bruto,
                "gas": gas,
                "fee": fee,
                "slippage": slippage
            })
    if oportunidades:
        return max(oportunidades, key=lambda x: x["spread_liquido"])
    return None

def pode_fechar_ciclo(posicao):
    if posicao["token_atual"] in ["USDT", "USDC"] and posicao["saldo_atual_usd"] > posicao["saldo_entrada"]:
        return True
    return False

def executar_hop(posicao, oportunidade, user):
    # Chama executor.py para swap
    resultado = executor.executar_swap(
        user=user,
        token_de=oportunidade["de"],
        token_para=oportunidade["para"],
        amount=oportunidade["amount_entrada"]
    )
    # Atualiza posição
    hops = json.loads(posicao["hops"]) if posicao["hops"] else []
    novo_hop = {
        "hop": len(hops)+1,
        "de": oportunidade["de"],
        "para": oportunidade["para"],
        "amount_entrada": oportunidade["amount_entrada"],
        "amount_saida": resultado["amount_saida"],
        "spread_pct": oportunidade["spread_bruto"],
        "lucro_usd": resultado.get("lucro_usd", 0),
        "tx_hash": resultado.get("tx_hash", ""),
        "timestamp": datetime.utcnow().isoformat()
    }
    hops.append(novo_hop)
    vault.atualizar_posicao(
        posicao["telegram_id"],
        oportunidade["para"],
        resultado["amount_saida"],
        resultado["saldo_atual_usd"],
        json.dumps(hops)
    )
    return resultado

def fechar_ciclo(posicao, user, bot):
    # Swap final para USDT
    resultado = executor.executar_swap(
        user=user,
        token_de=posicao["token_atual"],
        token_para="USDT",
        amount=posicao["amount_token"]
    )
    lucro_realizado = resultado["saldo_atual_usd"] - posicao["saldo_entrada"]
    vault.fechar_posicao(posicao["telegram_id"], resultado["saldo_atual_usd"])
    # Registrar na tabela operacoes (não implementado aqui)
    return {
        "entrada": posicao["saldo_entrada"],
        "saida": resultado["saldo_atual_usd"],
        "lucro": lucro_realizado,
        "hops": json.loads(posicao["hops"]),
        "ciclo_numero": posicao["ciclo_numero"]
    }
