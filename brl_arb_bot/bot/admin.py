"""
admin.py — Painel administrativo exclusivo para o dono do bot.
Acesso protegido por ADMIN_TELEGRAM_ID no .env.
Navegação 100% via botões inline no Telegram.
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from vault.vault import (
    is_admin,
    admin_stats_gerais,
    admin_listar_usuarios,
    admin_lucro_por_usuario,
    admin_ops_recentes,
)

logger = logging.getLogger(__name__)


# ─── Decorador de proteção ────────────────────────────────────────────────────

def apenas_admin(func):
    """Bloqueia qualquer não-admin silenciosamente."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = (
            update.effective_user.id
            if update.effective_user
            else update.callback_query.from_user.id
        )
        if not is_admin(uid):
            if update.message:
                await update.message.reply_text("⛔ Acesso restrito.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Acesso restrito.", show_alert=True)
            return
        return await func(update, ctx)
    return wrapper


# ─── Menu principal do admin ──────────────────────────────────────────────────

def _menu_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard geral",    callback_data="adm|dashboard")],
        [InlineKeyboardButton("👥 Usuários",           callback_data="adm|usuarios")],
        [InlineKeyboardButton("💰 Lucro por aluno",   callback_data="adm|lucros")],
        [InlineKeyboardButton("🔄 Operações recentes", callback_data="adm|ops")],
        [InlineKeyboardButton("🔙 Fechar",             callback_data="adm|fechar")],
    ])


@apenas_admin
async def painel_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🟠 *ADMIN*\n🛡️ *Painel Administrativo*\n\nAcesso restrito ao administrador.\nO que deseja ver?",
        parse_mode="Markdown",
        reply_markup=_menu_admin()
    )


# ─── Callbacks do painel ──────────────────────────────────────────────────────

@apenas_admin
async def callback_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acao = query.data.split("|")[1]

    if acao == "fechar":
        await query.edit_message_text("✅ Painel fechado.")
        return

    if acao == "dashboard":
        await _mostrar_dashboard(query)
    elif acao == "usuarios":
        await _mostrar_usuarios(query)
    elif acao == "lucros":
        await _mostrar_lucros(query)
    elif acao == "ops":
        await _mostrar_ops(query)
    elif acao == "voltar":
        await query.edit_message_text(
            "🟠 *ADMIN*\n🛡️ *Painel Administrativo*\n\nAcesso restrito ao administrador.\nO que deseja ver?",
            parse_mode="Markdown",
            reply_markup=_menu_admin()
        )


def _botao_voltar() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Menu", callback_data="adm|voltar")
    ]])


async def _mostrar_dashboard(query):
    s = admin_stats_gerais()
    taxa_sucesso = (
        round(s["ops_sucesso"] / s["total_ops"] * 100, 1)
        if s["total_ops"] > 0 else 0
    )

    def fmt_int(v: int) -> str:
        return f"{v:,}".replace(",", ".")

    texto = (
        "📊 *Dashboard Geral*\n\n"
        f"👥 Alunos ativos: `{fmt_int(s['total_usuarios'])}`\n"
        f"🔄 Total de operações: `{fmt_int(s['total_ops'])}`\n"
        f"✅ Sucesso: `{fmt_int(s['ops_sucesso'])}`  ❌ Erro: `{fmt_int(s['ops_erro'])}`\n"
        f"📈 Taxa de sucesso: `{taxa_sucesso}%`\n\n"
        f"💰 Lucro total: `${s['total_lucro']:.4f}`\n"
        f"📅 Lucro hoje: `${s['lucro_hoje']:.4f}`\n\n"
        f"🏆 Melhor spread: `{s['melhor_spread']:.3f}%`\n"
        f"🌐 Rede mais ativa: `{s['top_rede']}`"
    )
    await query.edit_message_text(texto, parse_mode="Markdown",
                                   reply_markup=_botao_voltar())


async def _mostrar_usuarios(query):
    usuarios = admin_listar_usuarios()
    if not usuarios:
        await query.edit_message_text("Nenhum usuário cadastrado ainda.",
                                       reply_markup=_botao_voltar())
        return

    linhas = [f"👥 *Usuários cadastrados ({len(usuarios)}):*\n"]
    for u in usuarios[:20]:   # limita a 20 para não estourar o limite do TG
        status  = "🟢" if u["active"] else "🔴"
        user    = f"@{u['username']}" if u["username"] else f"id:{u['telegram_id']}"
        addr    = u["dex_address"]
        resumo  = f"{addr[:6]}...{addr[-4:]}" if addr else "—"
        data    = u["created_at"][:10]
        linhas.append(f"{status} {user} | `{resumo}` | {data}")

    await query.edit_message_text(
        "\n".join(linhas), parse_mode="Markdown",
        reply_markup=_botao_voltar()
    )


async def _mostrar_lucros(query):
    ranking = admin_lucro_por_usuario()
    if not ranking:
        await query.edit_message_text("Nenhuma operação lucrativa registrada ainda.",
                                       reply_markup=_botao_voltar())
        return

    linhas = ["💰 *Lucro por aluno (ranking):*\n"]
    medalhas = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(ranking[:15]):
        medal = medalhas[i] if i < 3 else f"{i+1}."
        user  = f"@{r['username']}" if r["username"] != "—" else f"id:{r['telegram_id']}"
        linhas.append(
            f"{medal} {user}\n"
            f"   ops: `{r['ops']}` | lucro: `${r['lucro_total']:.4f}`"
        )

    await query.edit_message_text(
        "\n".join(linhas), parse_mode="Markdown",
        reply_markup=_botao_voltar()
    )


async def _mostrar_ops(query):
    ops = admin_ops_recentes(limite=15)
    if not ops:
        await query.edit_message_text("Nenhuma operação registrada ainda.",
                                       reply_markup=_botao_voltar())
        return

    linhas = ["🔄 *Operações recentes:*\n"]
    for op in ops:
        emoji = "✅" if op["status"] == "sucesso" else "❌"
        user  = f"@{op['username']}" if op["username"] != "—" else f"id:{op['telegram_id']}"
        linhas.append(
            f"{emoji} {user} | {op['rede']} | {op['par']}\n"
            f"   spread `{op['spread_pct']:.2f}%` | lucro `${op['lucro_usd']:.4f}` | {op['created_at'][5:16]}"
        )

    await query.edit_message_text(
        "\n".join(linhas), parse_mode="Markdown",
        reply_markup=_botao_voltar()
    )


# ─── Registra handlers do admin ───────────────────────────────────────────────

def registrar_admin_handlers(app):
    app.add_handler(CommandHandler("admin", painel_admin))
    app.add_handler(CallbackQueryHandler(
        callback_admin,
        pattern=r"^adm\|"   # só captura callbacks do painel admin
    ))
