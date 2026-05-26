from engine.chain import iniciar_ciclo, loop_cadeia
from vault.vault import get_posicao_aberta, get_numero_ciclos, get_saldo_historico
# ─── Comando /ciclo ──────────────────────────────────────────────────────────
async def ciclo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    posicao = get_posicao_aberta(uid)
    if posicao:
        from datetime import datetime
        tempo_aberto = (datetime.fromisoformat(posicao["updated_at"]) - datetime.fromisoformat(posicao["created_at"])).total_seconds()
        minutos = int(tempo_aberto // 60)
        hops = len(json.loads(posicao["hops"]))
        lucro = posicao["saldo_atual_usd"] - posicao["saldo_entrada"]
        await update.message.reply_text(
            f"🔗 *Ciclo {posicao['ciclo_numero']} em andamento*\n"
            f"Token atual: `{posicao['token_atual']}`\n"
            f"Amount: `{posicao['amount_token']:.4f}`\n"
            f"Saldo entrada: `${posicao['saldo_entrada']:.2f}`\n"
            f"Saldo estimado atual: `${posicao['saldo_atual_usd']:.2f}`\n"
            f"Lucro acumulado: `${lucro:.2f}`\n"
            f"Hops: `{hops}`\n"
            f"Tempo aberto: `{minutos} min`",
            parse_mode="Markdown"
        )
    else:
        # Mostra último ciclo fechado
        hist = get_saldo_historico(uid)
        await update.message.reply_text(
            f"💰 *Resumo dos ciclos*\n"
            f"Saldo inicial: `${hist['saldo_inicial']:.2f}`\n"
            f"Saldo atual: `${hist['saldo_atual']:.2f}`\n"
            f"Lucro total: `${hist['lucro_total']:.2f}`\n"
            f"Total ciclos: `{hist['total_ciclos']}`\n"
            f"Tempo médio por ciclo: `{int(hist['tempo_medio_ciclo']//60)} min`",
            parse_mode="Markdown"
        )

# ─── Botão painel aluno ──────────────────────────────────────────────────────
def _menu_aluno():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Meu Ciclo Atual", callback_data="painel|ciclo")],
        [InlineKeyboardButton("📊 Histórico", callback_data="painel|historico")],
        [InlineKeyboardButton("⚙️ Modo", callback_data="painel|modo")],
    ])

"""
handlers.py — Handlers do bot Telegram com botões inline Executar / Ignorar.
"""

import asyncio
import json
import logging
import os
import uuid
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.error import BadRequest
from telegram.ext import (
    ContextTypes, ConversationHandler,
    CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)
from vault.vault import (
    save_user, get_user,
    user_exists, historico_usuario, registrar_operacao,
    set_user_trading_mode, is_admin,
)
from engine.arbitrage import loop_usuario
from engine.executor import executar_swap
from engine.prices import buscar_saldo_polygon

logger = logging.getLogger(__name__)

# GIF exibido no /start — coloque a URL ou file_id do Telegram no .env
MENU_GIF: str = os.getenv("MENU_GIF", "")

WAIT_ADDRESS, WAIT_PK = range(2)
MENU_HAMBURGER = "☰ Menu"
TOKENS_USD = {"USDT", "USDC", "DAI"}
TOKENS_BRL = {"BRZ", "BRLA", "BRL1"}


def _teclado_hamburger() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MENU_HAMBURGER],
            ["▶ Iniciar", "⏹ Parar"],
            ["📊 Status", "📋 Histórico"],
            ["⚙️ Modo", "📱 Painel"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def _modo_usuario(user: dict | None) -> str:
    return (user or {}).get("trading_mode", "manual")


def _teclado_start_novo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Cadastrar", callback_data="start|cadastrar")],
    ])


def _teclado_start_cadastrado(admin_user: bool = False) -> InlineKeyboardMarkup:
    linhas = [
        [InlineKeyboardButton("▶ Iniciar monitor", callback_data="start|iniciar")],
        [InlineKeyboardButton("⚙️ Modo Manual/Auto", callback_data="start|modo")],
        [InlineKeyboardButton("📱 Abrir painel", callback_data="start|painel")],
    ]
    if admin_user:
        linhas.append([InlineKeyboardButton("🛡️ Painel admin", callback_data="start|admin")])
    return InlineKeyboardMarkup(linhas)


def _teclado_modo(modo_atual: str) -> InlineKeyboardMarkup:
    manual = "🟢 Manual" if modo_atual == "manual" else "⚪ Manual"
    auto = "🟢 Automático" if modo_atual == "auto" else "⚪ Automático"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(manual, callback_data="mode|manual")],
        [InlineKeyboardButton(auto, callback_data="mode|auto")],
    ])


async def _send_menu(
    message,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    """Envia GIF animado com botões inline. Cai para texto puro se GIF não configurado ou inválido."""
    if MENU_GIF:
        try:
            await message.reply_animation(
                animation=MENU_GIF,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass  # GIF inválido → fallback
    await message.reply_text(caption, parse_mode="Markdown", reply_markup=reply_markup)


def _nome_modo(modo: str) -> str:
    return "Manual" if modo == "manual" else "Automático"


def _texto_modo(modo: str) -> str:
    if modo == "auto":
        detalhe = "executa compra/venda automaticamente quando houver oportunidade válida"
    else:
        detalhe = "apenas alerta, e você decide em Executar/Ignorar"
    return (
        "⚙️ *Modo de operação*\n\n"
        f"Atual: *{_nome_modo(modo)}*\n"
        f"Regra: {detalhe}."
    )


def _resolver_tokens_execucao(oport) -> tuple[str, str]:
    token_from = getattr(oport, "token_from", None)
    token_to = getattr(oport, "token_to", None)
    if token_from and token_to:
        return token_from, token_to

    if oport.preco_brl < oport.preco_usd:
        return oport.token_usd, oport.token_brl
    return oport.token_brl, oport.token_usd


def _store_exec_payload(bot_data: dict, uid: int, payload: dict) -> str:
    """Guarda payload de execucao e retorna id curto para callback_data."""
    pending_all = bot_data.setdefault("pending_exec", {})
    pending_user = pending_all.setdefault(str(uid), {})

    payload_id = uuid.uuid4().hex[:12]
    pending_user[payload_id] = payload

    # Mantem janela pequena para evitar crescimento infinito.
    while len(pending_user) > 100:
        oldest_key = next(iter(pending_user))
        pending_user.pop(oldest_key, None)

    return payload_id


# ─── Alerta com botões inline ─────────────────────────────────────────────────

def montar_alerta(oport, bot_data: dict | None = None, uid: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    from config import NETWORKS
    rede = NETWORKS[oport.chain_id]["name"]
    amount_usd_equiv = float(getattr(oport, "amount_usd_equiv", oport.amount_usd))
    valor_final = amount_usd_equiv + oport.lucro_usd

    token_from, token_to = _resolver_tokens_execucao(oport)

    texto = (
        f"🔔 *Oportunidade detectada!*\n\n"
        f"Rede: `{rede}`\n"
        f"Par: `{oport.token_brl}/{oport.token_usd}`\n"
        f"Direção: `{oport.direcao}`\n"
        f"🟢 Spread: `{oport.spread_pct:.3f}%`\n"
        f"Operação: `{oport.amount_usd:.6f} {token_from}` (~`${amount_usd_equiv:.2f}`)\n"
        f"─────────────────────\n"
        f"🔴 Fee swap: `-${oport.fee_swap_usd:.4f}`\n"
        f"🔴 Gas est.: `-${oport.gas_usd:.4f}`\n"
        f"🟡 *Lucro líquido est.: `${oport.lucro_usd:.4f}`*\n"
        f"🔵 *Valor final est.: `${valor_final:.4f}`*"
    )
    payload = {
        "c":  oport.chain_id,
        "fb": token_from,
        "tu": token_to,
        "am": oport.amount_usd,
        "sp": round(oport.spread_pct, 3),
        "lu": round(oport.lucro_usd, 4),
    }

    if bot_data is not None and uid is not None:
        payload_ref = _store_exec_payload(bot_data, uid, payload)
        callback_exec = f"exec|{payload_ref}"
    else:
        # Fallback de compatibilidade (evitar uso em producao por limite de 64 bytes).
        callback_exec = f"exec|{json.dumps(payload, separators=(',', ':'))}"

    teclado = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Executar agora", callback_data=callback_exec),
        InlineKeyboardButton("❌ Ignorar",        callback_data="ignore"),
    ]])
    return texto, teclado


# ─── Callback botões inline ───────────────────────────────────────────────────

async def callback_botao(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as exc:
        if "Query is too old" not in str(exc) and "query id is invalid" not in str(exc):
            raise
        return
    uid  = query.from_user.id
    data = query.data

    if data == "ignore":
        await query.edit_message_text("❌ Oportunidade ignorada.")
        return

    if data == "start|iniciar":
        msg = await _iniciar_monitor(uid, ctx)
        await query.answer("Monitor iniciado." if msg.startswith("🟢") else "Não foi possível iniciar.")
        await query.message.reply_text(msg, parse_mode="Markdown")
        return

    if data == "start|modo":
        user = get_user(uid)
        if not user:
            await query.answer("Use /cadastrar primeiro.", show_alert=True)
            return
        modo = _modo_usuario(user)
        await query.message.reply_text(
            _texto_modo(modo),
            parse_mode="Markdown",
            reply_markup=_teclado_modo(modo),
        )
        return

    if data == "start|painel":
        user = get_user(uid)
        if not user:
            await query.answer("Use /cadastrar primeiro.", show_alert=True)
            return
        await query.message.reply_text(
            "📱 *Meu Painel*\n\nEscolha o que deseja ver:",
            parse_mode="Markdown",
            reply_markup=_menu_aluno(),
        )
        return

    if data == "painel|ciclo":
        posicao = get_posicao_aberta(uid)
        if posicao:
            from datetime import datetime
            tempo_aberto = (datetime.fromisoformat(posicao["updated_at"]) - datetime.fromisoformat(posicao["created_at"])).total_seconds()
            minutos = int(tempo_aberto // 60)
            hops = len(json.loads(posicao["hops"]))
            lucro = posicao["saldo_atual_usd"] - posicao["saldo_entrada"]
            await query.message.reply_text(
                f"Status: 🟢 Ciclo {posicao['ciclo_numero']} em andamento\n"
                f"Posição: `{posicao['token_atual']}` (${posicao['saldo_atual_usd']:.2f})\n"
                f"Entrada: `${posicao['saldo_entrada']:.2f}`\n"
                f"Lucro acumulado: `${lucro:.2f}`\n"
                f"Hops: `{hops}`\n"
                f"Tempo: `{minutos} min`",
                parse_mode="Markdown"
            )
        else:
            hist = get_saldo_historico(uid)
            await query.message.reply_text(
                f"Nenhum ciclo aberto.\n\n"
                f"Saldo atual: `${hist['saldo_atual']:.2f}`\n"
                f"Total ciclos: `{hist['total_ciclos']}`",
                parse_mode="Markdown"
            )
        return

    if data == "start|admin":
        if not is_admin(uid):
            await query.answer("⛔ Acesso restrito.", show_alert=True)
            return
        from bot.admin import _menu_admin
        from vault.vault import VAULT_DB
        import sqlite3
        con = sqlite3.connect(VAULT_DB)
        total_ativos = con.execute("SELECT COUNT(*) FROM posicoes WHERE status='aberto'").fetchone()[0]
        fechados_hoje = con.execute("SELECT COUNT(*) FROM posicoes WHERE status='fechado' AND date(updated_at)=date('now')").fetchone()[0]
        lucro_medio = con.execute("SELECT AVG(saldo_atual_usd - saldo_entrada) FROM posicoes WHERE status='fechado' AND date(updated_at)=date('now')").fetchone()[0] or 0
        tempo_medio = con.execute("SELECT AVG((julianday(updated_at)-julianday(created_at))*24*60) FROM posicoes WHERE status='fechado' AND date(updated_at)=date('now')").fetchone()[0] or 0
        con.close()
        await query.message.reply_text(
            f"🔗 Ciclos ativos agora: {total_ativos}\n"
            f"✅ Ciclos fechados hoje: {fechados_hoje}\n"
            f"💰 Lucro médio por ciclo: ${lucro_medio:.2f}\n"
            f"⏱ Tempo médio por ciclo: {int(tempo_medio)}min",
            parse_mode="Markdown",
            reply_markup=_menu_admin(),
        )
        return
def registrar_todos_handlers_ciclo(app):
    app.add_handler(CommandHandler("ciclo", ciclo))
# No final do arquivo, registre o handler do ciclo
definicoes_extra = [CommandHandler("ciclo", ciclo)]

    if data.startswith("mode|"):
        novo_modo = data.split("|", 1)[1]
        try:
            set_user_trading_mode(uid, novo_modo)
        except Exception:
            await query.answer("Erro ao atualizar modo.", show_alert=True)
            return
        user = get_user(uid)
        modo = _modo_usuario(user)
        try:
            await query.edit_message_text(
                _texto_modo(modo),
                parse_mode="Markdown",
                reply_markup=_teclado_modo(modo),
            )
        except BadRequest as exc:
            # Telegram retorna erro quando o usuário toca no mesmo modo já selecionado.
            if "Message is not modified" not in str(exc):
                raise
        return

    if data.startswith("exec|"):
        raw_payload = data.split("|", 1)[1]

        if raw_payload.startswith("{"):
            payload = json.loads(raw_payload)
        else:
            pending_all = ctx.bot_data.get("pending_exec", {})
            pending_user = pending_all.get(str(uid), {})
            payload = pending_user.pop(raw_payload, None)
            if not payload:
                await query.edit_message_text(
                    "⚠️ Este alerta expirou. Aguarde o próximo sinal.",
                )
                return

        user       = get_user(uid, include_pk=True)
        if not user:
            await query.edit_message_text("❌ Usuário não encontrado. Use /cadastrar.")
            return

        chain_id   = payload["c"]
        token_from = payload["fb"]
        token_to   = payload["tu"]
        amount_usd = payload["am"]
        spread_pct = payload["sp"]
        lucro_est  = payload["lu"]
        par_exec   = f"{token_from} -> {token_to}"

        await query.edit_message_text(
            query.message.text + "\n\n⏳ *Executando swap...*\n"
            f"Par de execução: `{par_exec}`\n"
            "_Assinando transação e enviando para a rede._",
            parse_mode="Markdown"
        )

        resultado = await executar_swap(
            chain_id=chain_id,
            token_from=token_from,
            token_to=token_to,
            amount_usd=amount_usd,
            wallet=user["dex_address"],
            private_key=user["dex_pk"],
        )

        from config import NETWORKS
        rede_nome = NETWORKS[chain_id]["name"]
        par = f"{token_from}/{token_to}"

        if resultado["sucesso"]:
            tx_hash  = resultado["tx_hash"]
            explorer = resultado["explorer"]
            fonte    = resultado.get("fonte") or "dex"
            approves = resultado.get("approve_explorers") or []
            received_amount = float(resultado.get("received_token_amount") or 0)
            received_symbol = resultado.get("received_token_symbol") or token_to
            approve_txt = ""
            if approves:
                linhas = [f"• [Approve {i + 1}]({url})" for i, url in enumerate(approves[:3])]
                approve_txt = "\n\n✅ *Approve automático detectado*\n" + "\n".join(linhas)

            ciclo_txt = ""
            close_cycle = os.getenv("CLOSE_CYCLE_ENABLED", "true").strip().lower() in {
                "1", "true", "yes", "y", "on"
            }
            received_amount_exact = resultado.get("received_token_amount_str")
            received_amount_wei = int(resultado.get("received_token_wei") or 0)
            if close_cycle and token_from in TOKENS_USD and token_to in TOKENS_BRL and received_amount_wei > 0 and received_amount_exact:
                await query.message.reply_text(
                    "🔁 Tentando fechar ciclo para realizar lucro em USD...",
                    parse_mode="Markdown",
                )
                resultado_volta = await executar_swap(
                    chain_id=chain_id,
                    token_from=token_to,
                    token_to=token_from,
                    amount_usd=received_amount_exact,
                    wallet=user["dex_address"],
                    private_key=user["dex_pk"],
                )
                if resultado_volta.get("sucesso"):
                    usd_recebido = float(resultado_volta.get("received_token_amount") or 0)
                    lucro_real = usd_recebido - float(amount_usd)
                    ciclo_txt = (
                        "\n\n🔁 *Ciclo fechado*\n"
                        f"Volta: `{token_to}->{token_from}`\n"
                        f"Recebido: `{usd_recebido:.6f} {token_from}`\n"
                        f"💰 Lucro realizado: `${lucro_real:.4f}`"
                    )
                else:
                    ciclo_txt = (
                        "\n\n⚠️ *Ciclo não fechado*\n"
                        f"Saldo permaneceu em `{received_symbol}`.\n"
                        f"Erro na volta: `{resultado_volta.get('erro', 'desconhecido')}`"
                    )
            registrar_operacao(uid, rede_nome, par, spread_pct, lucro_est, tx_hash, "sucesso")
            await query.edit_message_text(
                f"✅ *Swap executado!*\n\n"
                f"Rede: `{rede_nome}`\n"
                f"Par: `{par}`\n"
                f"Lucro est.: `${lucro_est:.4f}`\n\n"
                f"Via: `{fonte}`\n"
                f"🔗 [Ver no explorer]({explorer})"
                f"{approve_txt}"
                f"{ciclo_txt}",
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        else:
            registrar_operacao(uid, rede_nome, par, spread_pct, 0, "", "erro")
            tx_hash = resultado.get("tx_hash")
            explorer = resultado.get("explorer")
            approves = resultado.get("approve_explorers") or []
            detalhe_tx = ""
            if tx_hash and explorer:
                detalhe_tx = f"\n\n🔗 [Ver transação]({explorer})"
            detalhe_approve = ""
            if approves:
                linhas = [f"• [Approve {i + 1}]({url})" for i, url in enumerate(approves[:3])]
                detalhe_approve = "\n\nℹ️ *Approve automático enviado*\n" + "\n".join(linhas)
            await query.edit_message_text(
                f"❌ *Erro ao executar swap*\n\n`{resultado['erro']}`{detalhe_tx}{detalhe_approve}",
                parse_mode="Markdown"
            )


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_exists(uid):
        user   = get_user(uid)
        addr   = user["dex_address"]
        resumo = f"{addr[:8]}...{addr[-4:]}"
        modo   = _nome_modo(_modo_usuario(user))

        # Busca saldos na Polygon em background (sem travar o bot se RPC lento)
        saldo_txt = "⏳ carregando..."
        try:
            saldos = await buscar_saldo_polygon(addr)
            pol  = saldos.get("POL")
            usdt = saldos.get("USDT")
            usdc = saldos.get("USDC")
            brz  = saldos.get("BRZ")
            brla = saldos.get("BRLA")
            brl1 = saldos.get("BRL1")
            linhas = []
            if pol  is not None: linhas.append(f"  • POL:   `{pol:.4f}`")
            if usdt is not None: linhas.append(f"  • USDT:  `{usdt:.2f}`")
            if usdc is not None: linhas.append(f"  • USDC:  `{usdc:.2f}`")
            if brz  is not None: linhas.append(f"  • BRZ:   `{brz:.2f}`")
            if brla is not None: linhas.append(f"  • BRLA:  `{brla:.2f}`")
            if brl1 is not None: linhas.append(f"  • BRL1:  `{brl1:.2f}`")
            saldo_txt = "\n".join(linhas) if linhas else "_indisponível_"
        except Exception:
            saldo_txt = "_indisponível_"

        caption = (
            f"👋 *Bem-vindo de volta!*\n\n"
            f"✅ `{resumo}`  📍 Polygon\n"
            f"{saldo_txt}\n\n"
            f"⚙️ Modo: *{modo}*"
        )
        await _send_menu(update.message, caption, _teclado_start_cadastrado(is_admin(uid)))
        await update.message.reply_text(
            "Use o botão ☰ Menu para atalhos rápidos.",
            reply_markup=_teclado_hamburger(),
        )
    else:
        caption = (
            "🤖 *Bot de Arbitragem BRL Stablecoins*\n\n"
            "Monitora spreads entre BRZ, BRLA, BRL1 e USDT em tempo real.\n\n"
            "📍 *Rede: Polygon*\n"
            "Para operar você precisará de:\n"
            "  • *USDT* — capital de trade\n"
            "  • *POL* — gas (mín. 5 POL)"
        )
        await _send_menu(update.message, caption, _teclado_start_novo())
        await update.message.reply_text(
            "Use o botão ☰ Menu para navegar com botões.",
            reply_markup=_teclado_hamburger(),
        )


# ─── Cadastro ─────────────────────────────────────────────────────────────────

async def cadastrar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 *Passo 1/2 — Endereço da carteira*\n\n"
        "Crie uma carteira *dedicada exclusivamente ao bot*.\n"
        "⚠️ *Nunca use sua MetaMask principal.*\n\n"
        "📍 Certifique-se de que essa carteira tenha:\n"
        "  • *USDT* na rede *Polygon* (capital de operação)\n"
        "  • *POL* na rede *Polygon* (mínimo 5 POL para gas)\n\n"
        "Cole o endereço público (0x...):",
        parse_mode="Markdown"
    )
    return WAIT_ADDRESS


async def cadastrar_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entrada do fluxo de cadastro via botão inline no /start."""
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📌 *Passo 1/2 — Endereço da carteira*\n\n"
        "Crie uma carteira *dedicada exclusivamente ao bot*.\n"
        "⚠️ *Nunca use sua MetaMask principal.*\n\n"
        "📍 Certifique-se de que essa carteira tenha:\n"
        "  • *USDT* na rede *Polygon* (capital de operação)\n"
        "  • *POL* na rede *Polygon* (mínimo 5 POL para gas)\n\n"
        "Cole o endereço público (0x...):",
        parse_mode="Markdown"
    )
    return WAIT_ADDRESS


async def receber_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text("❌ Endereço inválido. Tente novamente:")
        return WAIT_ADDRESS
    ctx.user_data["address"] = address
    await update.message.reply_text(
        "📌 *Passo 2/2 — Private Key*\n\n"
        "Cole a private key da carteira dedicada:\n\n"
        "⚠️ Será *deletada do chat imediatamente* após o registro.",
        parse_mode="Markdown"
    )
    return WAIT_PK


async def receber_pk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    username = update.effective_user.username or ""
    pk       = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    pk_limpa = pk.replace("0x", "")
    if len(pk_limpa) != 64:
        await update.message.reply_text("❌ Private key inválida. Use /cadastrar novamente.")
        return ConversationHandler.END
    save_user(uid, username, ctx.user_data["address"], pk)
    ctx.user_data.clear()
    await update.message.reply_text(
        "🎉 *Cadastro completo!*\n\n"
        "✅ Carteira registrada e criptografada.\n"
        "Você nunca mais precisará digitar suas credenciais.\n\n"
        "📍 *Lembrete antes de iniciar:*\n"
        "Garanta que sua carteira tenha *USDT + POL* na Polygon.\n"
        "Sem saldo de POL o bot não consegue pagar o gas e as transações falharão.\n\n"
        "Escolha o próximo passo abaixo.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶ Iniciar monitor", callback_data="start|iniciar")],
            [InlineKeyboardButton("⚙️ Definir modo", callback_data="start|modo")],
        ]),
    )
    await update.message.reply_text(
        "Ative o menu rápido para facilitar o uso:",
        reply_markup=_teclado_hamburger(),
    )
    return ConversationHandler.END


async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Cadastro cancelado.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ─── Comandos operacionais ────────────────────────────────────────────────────

async def _iniciar_monitor(uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    user = get_user(uid)
    if not user:
        return "❌ Use /cadastrar primeiro."
    if ctx.bot_data.get(f"running_{uid}"):
        return "⚠️ Monitor já está rodando."

    from config import INTERVALO_SCAN_SEG

    ctx.bot_data[f"running_{uid}"] = True
    modo = _nome_modo(_modo_usuario(user))
    asyncio.create_task(loop_usuario(uid, ctx.bot, ctx.bot_data, intervalo=INTERVALO_SCAN_SEG))
    return (
        "🟢 *Monitor iniciado!*\n\n"
        f"Varrendo pares a cada {INTERVALO_SCAN_SEG}s.\n"
        f"Modo atual: *{modo}*\n"
        "Manual: envia alerta com Executar/Ignorar.\n"
        "Automático: executa compra/venda quando surgir oportunidade."
    )


async def iniciar_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(await _iniciar_monitor(uid, ctx), parse_mode="Markdown")


async def parar_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ctx.bot_data[f"running_{uid}"] = False
    await update.message.reply_text("🔴 Monitor pausado.")


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = get_user(uid)
    if not user:
        await update.message.reply_text("❌ Não cadastrado. Use /cadastrar.")
        return
    rodando = ctx.bot_data.get(f"running_{uid}", False)
    estado  = "🟢 Rodando" if rodando else "🔴 Parado"
    addr    = user["dex_address"]
    modo    = _nome_modo(_modo_usuario(user))
    build   = os.getenv("BOT_BUILD", "desconhecido")
    started = os.getenv("BOT_STARTED_AT", "desconhecido")
    await update.message.reply_text(
        f"📊 *Status*\n\n"
        f"Estado: {estado}\n"
        f"Modo: *{modo}*\n"
        f"Build: `{build}`\n"
        f"Iniciado em: `{started}`\n"
        f"Carteira: `{addr[:8]}...{addr[-4:]}`\n"
        f"Redes: Ethereum • Polygon • Arbitrum • Base\n"
        f"Pares: BRZ/USDT • BRZ/USDC • BRLA/USDC",
        parse_mode="Markdown"
    )


async def modo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    if not user:
        await update.message.reply_text("❌ Use /cadastrar primeiro.")
        return

    modo_atual = _modo_usuario(user)
    await update.message.reply_text(
        _texto_modo(modo_atual),
        parse_mode="Markdown",
        reply_markup=_teclado_modo(modo_atual),
    )


async def historico(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    hist = historico_usuario(uid, limite=10)
    if not hist:
        await update.message.reply_text("Nenhuma operação registrada ainda.")
        return
    linhas = ["📋 *Últimas operações:*\n"]
    for op in hist:
        emoji = "✅" if op["status"] == "sucesso" else "❌"
        linhas.append(
            f"{emoji} {op['rede']} | {op['par']} | "
            f"spread {op['spread_pct']:.2f}% | lucro ${op['lucro_usd']:.4f}"
        )
    await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")


async def ajuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Comandos*\n\n"
        "/start — Tela inicial\n"
        "/cadastrar — Registrar carteira\n"
        "/iniciar — Ligar monitor\n"
        "/modo — Manual/Automático\n"
        "/painel — Menu do aluno\n"
        "/parar — Pausar monitor\n"
        "/status — Estado atual\n"
        "/historico — Últimas 10 operações\n"
        "/menu — Mostrar teclado de atalhos\n"
        "/help — Esta mensagem",
        parse_mode="Markdown"
    )


async def menu_hamburger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    if user:
        await update.message.reply_text(
            "☰ *Menu rápido*\nEscolha uma opção pelos botões abaixo.",
            parse_mode="Markdown",
            reply_markup=_teclado_hamburger(),
        )
    else:
        await update.message.reply_text(
            "☰ Menu disponível. Primeiro finalize seu cadastro em /cadastrar.",
            reply_markup=_teclado_hamburger(),
        )


async def atalhos_hamburger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    texto = (update.message.text or "").strip()

    if texto == MENU_HAMBURGER:
        await menu_hamburger(update, ctx)
        return
    if texto == "▶ Iniciar":
        await iniciar_bot(update, ctx)
        return
    if texto == "⏹ Parar":
        await parar_bot(update, ctx)
        return
    if texto == "📊 Status":
        await status(update, ctx)
        return
    if texto == "📋 Histórico":
        await historico(update, ctx)
        return
    if texto == "⚙️ Modo":
        await modo(update, ctx)
        return
    if texto == "📱 Painel":
        from bot.dashboard import menu_dashboard
        await menu_dashboard(update, ctx)
        return


# ─── Registra handlers ────────────────────────────────────────────────────────

def get_conversation_handler():
    return ConversationHandler(
        entry_points=[
            CommandHandler("cadastrar", cadastrar),
            CallbackQueryHandler(cadastrar_callback, pattern=r"^start\|cadastrar$"),
        ],
        states={
            WAIT_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_address)],
            WAIT_PK:      [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_pk)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )


def registrar_todos_handlers(app):
    app.add_handler(get_conversation_handler())
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("menu",      menu_hamburger))
    app.add_handler(CommandHandler("help",      ajuda))
    app.add_handler(CommandHandler("status",    status))
    app.add_handler(CommandHandler("modo",      modo))
    app.add_handler(CommandHandler("iniciar",   iniciar_bot))
    app.add_handler(CommandHandler("parar",     parar_bot))
    app.add_handler(CommandHandler("historico", historico))
    app.add_handler(MessageHandler(
        filters.Regex(r"^(☰ Menu|▶ Iniciar|⏹ Parar|📊 Status|📋 Histórico|⚙️ Modo|📱 Painel)$"),
        atalhos_hamburger,
    ))
    app.add_handler(CallbackQueryHandler(
        callback_botao,
        pattern=r"^(exec\||ignore$|start\|iniciar$|start\|modo$|start\|painel$|start\|admin$|mode\|)"
    ))
