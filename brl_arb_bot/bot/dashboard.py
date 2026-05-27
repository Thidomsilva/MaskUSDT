"""
dashboard.py — Dashboard do aluno (operações, lucros, status).
Navegação via botões inline — tudo dentro do Telegram.
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from vault.vault import get_user, user_exists, historico_usuario

logger = logging.getLogger(__name__)


# ─── Menu do aluno ────────────────────────────────────────────────────────────

def _menu_aluno() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Meu Dashboard",     callback_data="dash|resumo")],
        [InlineKeyboardButton("🔄 Operações",         callback_data="dash|ops")],
        [InlineKeyboardButton("💰 Meus Lucros",       callback_data="dash|lucros")],
        [InlineKeyboardButton("⚙️ Minha Carteira",    callback_data="dash|carteira")],
        [InlineKeyboardButton("🔙 Fechar",            callback_data="dash|fechar")],
    ])


async def menu_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not user_exists(uid):
        await update.message.reply_text("❌ Use /cadastrar primeiro.")
        return
    await update.message.reply_text(
        "📱 *Meu Painel*\n\nEscolha o que deseja ver:",
        parse_mode="Markdown",
        reply_markup=_menu_aluno()
    )


# ─── Callbacks ────────────────────────────────────────────────────────────────

async def callback_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as exc:
        if "Query is too old" not in str(exc) and "query id is invalid" not in str(exc):
            raise
        return
    uid  = query.from_user.id
    acao = query.data.split("|")[1]

    if acao == "fechar":
        await query.edit_message_text("✅ Painel fechado.")
        return

    if acao == "voltar":
        await query.edit_message_text(
            "📱 *Meu Painel*\n\nEscolha o que deseja ver:",
            parse_mode="Markdown",
            reply_markup=_menu_aluno()
        )
        return

    user = get_user(uid)
    if not user:
        await query.edit_message_text("❌ Usuário não encontrado. Use /cadastrar.")
        return

    if acao == "resumo":
        await _mostrar_resumo(query, uid, user, ctx)
    elif acao == "ops":
        await _mostrar_operacoes(query, uid)
    elif acao == "lucros":
        await _mostrar_lucros(query, uid)
    elif acao == "carteira":
        await _mostrar_carteira(query, user)


def _botao_voltar() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Menu", callback_data="dash|voltar")
    ]])


async def _mostrar_resumo(query, uid: int, user: dict, ctx):
    hist    = historico_usuario(uid, limite=100)
    sucesso = [op for op in hist if op["status"] == "sucesso"]
    erros   = [op for op in hist if op["status"] == "erro"]
    lucro_t = sum(op["lucro_usd"] for op in sucesso)
    melhor  = max((op["spread_pct"] for op in sucesso), default=0)
    rodando = ctx.bot_data.get(f"running_{uid}", False)
    estado  = "🟢 Rodando" if rodando else "🔴 Parado"
    addr    = user["dex_address"]
    modo    = "Manual" if user.get("trading_mode", "manual") == "manual" else "Automático"

    await query.edit_message_text(
        f"📊 *Meu Dashboard*\n\n"
        f"Monitor: {estado}\n"
        f"Modo: *{modo}*\n"
        f"Carteira: `{addr[:8]}...{addr[-4:]}`\n\n"
        f"🔄 Total de operações: `{len(hist)}`\n"
        f"✅ Sucesso: `{len(sucesso)}`  ❌ Erro: `{len(erros)}`\n\n"
        f"💰 Lucro acumulado: `${lucro_t:.4f}`\n"
        f"🏆 Melhor spread: `{melhor:.3f}%`",
        parse_mode="Markdown",
        reply_markup=_botao_voltar()
    )


async def _mostrar_operacoes(query, uid: int):
    hist = historico_usuario(uid, limite=10)
    if not hist:
        await query.edit_message_text(
            "Nenhuma operação registrada ainda.\n\nUse /iniciar para começar.",
            reply_markup=_botao_voltar()
        )
        return

    linhas = ["🔄 *Últimas operações:*\n"]
    for op in hist:
        emoji = "✅" if op["status"] == "sucesso" else "❌"
        linhas.append(
            f"{emoji} {op['rede']} | `{op['par']}`\n"
            f"   spread `{op['spread_pct']:.2f}%` | lucro `${op['lucro_usd']:.4f}`\n"
            f"   {op['created_at'][5:16]}"
        )

    await query.edit_message_text(
        "\n".join(linhas), parse_mode="Markdown",
        reply_markup=_botao_voltar()
    )


async def _mostrar_lucros(query, uid: int):
    hist    = historico_usuario(uid, limite=100)
    sucesso = [op for op in hist if op["status"] == "sucesso"]

    if not sucesso:
        await query.edit_message_text(
            "Nenhum lucro registrado ainda.",
            reply_markup=_botao_voltar()
        )
        return

    # Agrupa por rede
    por_rede: dict[str, float] = {}
    for op in sucesso:
        por_rede[op["rede"]] = por_rede.get(op["rede"], 0) + op["lucro_usd"]

    lucro_total = sum(por_rede.values())
    melhor_op   = max(sucesso, key=lambda o: o["lucro_usd"])

    linhas = [
        f"💰 *Meus Lucros*\n\n"
        f"Total acumulado: `${lucro_total:.4f}`\n"
        f"Operações lucrativas: `{len(sucesso)}`\n\n"
        f"*Por rede:*"
    ]
    for rede, lucro in sorted(por_rede.items(), key=lambda x: -x[1]):
        linhas.append(f"  • {rede}: `${lucro:.4f}`")

    linhas.append(
        f"\n🏆 Melhor operação:\n"
        f"  {melhor_op['rede']} | `{melhor_op['par']}`\n"
        f"  spread `{melhor_op['spread_pct']:.2f}%` | `${melhor_op['lucro_usd']:.4f}`"
    )

    await query.edit_message_text(
        "\n".join(linhas), parse_mode="Markdown",
        reply_markup=_botao_voltar()
    )


async def _mostrar_carteira(query, user: dict):
    from engine.prices import buscar_saldo_polygon
    addr = user["dex_address"]
    link_polygon = f"[Ver na Polygon](https://polygonscan.com/address/{addr})"

    # Mostra "consultando" enquanto busca on-chain
    await query.edit_message_text(
        f"⚙️ *Minha Carteira*\n\n"
        f"Endereço:\n`{addr}`\n\n"
        f"📍 *Polygon — Saldo:* ⏳ consultando rede...\n\n"
        f"{link_polygon}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=_botao_voltar(),
    )

    saldo_txt = "_indisponível_"
    try:
        saldos = await buscar_saldo_polygon(addr)
        linhas = []
        if saldos.get("POL")  is not None: linhas.append(f"  • POL:   `{saldos['POL']:.4f}`")
        if saldos.get("USDT") is not None: linhas.append(f"  • USDT:  `{saldos['USDT']:.2f}`")
        if saldos.get("USDC") is not None: linhas.append(f"  • USDC:  `{saldos['USDC']:.2f}`")
        if saldos.get("BRZ")  is not None: linhas.append(f"  • BRZ:   `{saldos['BRZ']:.2f}`")
        if saldos.get("BRLA") is not None: linhas.append(f"  • BRLA:  `{saldos['BRLA']:.2f}`")
        if saldos.get("BRL1") is not None: linhas.append(f"  • BRL1:  `{saldos['BRL1']:.2f}`")
        saldo_txt = "\n".join(linhas) if linhas else "_indisponível_"
    except Exception:
        logger.exception("Falha ao buscar saldos no painel uid=%s addr=%s", user.get("telegram_id"), addr)

    try:
        await query.edit_message_text(
            f"⚙️ *Minha Carteira*\n\n"
            f"Endereço:\n`{addr}`\n\n"
            f"📍 *Polygon — Saldo atual:*\n{saldo_txt}\n\n"
            f"ℹ️ Para operar:\n"
            f"  • *USDT/USDC* — capital de trade\n"
            f"  • *POL* — mínimo 5 POL para gas\n\n"
            f"{link_polygon}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=_botao_voltar(),
        )
    except Exception:
        logger.exception("Falha ao renderizar carteira no painel uid=%s", user.get("telegram_id"))


# ─── Registra handlers ────────────────────────────────────────────────────────

def registrar_dashboard_handlers(app):
    app.add_handler(CommandHandler("painel", menu_dashboard))
    app.add_handler(CallbackQueryHandler(
        callback_dashboard,
        pattern=r"^dash\|"
    ))
