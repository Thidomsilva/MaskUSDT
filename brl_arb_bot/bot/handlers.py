"""
handlers.py — Handlers do bot Telegram com botões inline Executar / Ignorar.
"""

import asyncio
import json
import logging
import os
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
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
    valor_final = oport.amount_usd + oport.lucro_usd

    token_from, token_to = _resolver_tokens_execucao(oport)

    texto = (
        f"🔔 *Oportunidade detectada!*\n\n"
        f"Rede: `{rede}`\n"
        f"Par: `{oport.token_brl}/{oport.token_usd}`\n"
        f"Direção: `{oport.direcao}`\n"
        f"🟢 Spread: `{oport.spread_pct:.3f}%`\n"
        f"Operação: `${oport.amount_usd:.0f}`\n"
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
        from bot.dashboard import _menu_aluno
        await query.message.reply_text(
            "📱 *Meu Painel*\n\nEscolha o que deseja ver:",
            parse_mode="Markdown",
            reply_markup=_menu_aluno(),
        )
        return

    if data == "start|admin":
        if not is_admin(uid):
            await query.answer("⛔ Acesso restrito.", show_alert=True)
            return
        from bot.admin import _menu_admin
        await query.message.reply_text(
            "🟠 *ADMIN*\n🛡️ *Painel Administrativo*\n\nAcesso restrito ao administrador.\nO que deseja ver?",
            parse_mode="Markdown",
            reply_markup=_menu_admin(),
        )
        return

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
            approve_txt = ""
            if approves:
                linhas = [f"• [Approve {i + 1}]({url})" for i, url in enumerate(approves[:3])]
                approve_txt = "\n\n✅ *Approve automático detectado*\n" + "\n".join(linhas)
            registrar_operacao(uid, rede_nome, par, spread_pct, lucro_est, tx_hash, "sucesso")
            await query.edit_message_text(
                f"✅ *Swap executado!*\n\n"
                f"Rede: `{rede_nome}`\n"
                f"Par: `{par}`\n"
                f"Lucro est.: `${lucro_est:.4f}`\n\n"
                f"Via: `{fonte}`\n"
                f"🔗 [Ver no explorer]({explorer})"
                f"{approve_txt}",
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
        "/help — Esta mensagem",
        parse_mode="Markdown"
    )


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
    )


def registrar_todos_handlers(app):
    app.add_handler(get_conversation_handler())
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("help",      ajuda))
    app.add_handler(CommandHandler("status",    status))
    app.add_handler(CommandHandler("modo",      modo))
    app.add_handler(CommandHandler("iniciar",   iniciar_bot))
    app.add_handler(CommandHandler("parar",     parar_bot))
    app.add_handler(CommandHandler("historico", historico))
    app.add_handler(CallbackQueryHandler(
        callback_botao,
        pattern=r"^(exec\||ignore$|start\|iniciar$|start\|modo$|start\|painel$|start\|admin$|mode\|)"
    ))
