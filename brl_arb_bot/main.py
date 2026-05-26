"""
main.py — Ponto de entrada do bot de arbitragem BRL stablecoins.
"""

import logging
import os
from pathlib import Path
from telegram.ext import ApplicationBuilder

from vault.vault import init_db
from bot.handlers import registrar_todos_handlers
from bot.admin import registrar_admin_handlers
from bot.dashboard import registrar_dashboard_handlers


def _load_env() -> None:
    """Carrega variaveis de ambiente do .env local e da raiz do workspace."""
    paths = [
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]

    try:
        from dotenv import load_dotenv  # type: ignore

        for p in paths:
            if p.exists():
                load_dotenv(p, override=False)
        return
    except Exception:
        pass

    # Fallback sem dependencia externa.
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Configure TELEGRAM_BOT_TOKEN no .env")

    admin_id = os.environ.get("ADMIN_TELEGRAM_ID", "0")
    if admin_id == "0":
        raise RuntimeError("Configure ADMIN_TELEGRAM_ID no .env")

    init_db()

    app = ApplicationBuilder().token(token).build()

    registrar_todos_handlers(app)      # aluno: cadastro, iniciar, parar, status
    registrar_dashboard_handlers(app)  # aluno: painel com operações e lucros
    registrar_admin_handlers(app)      # admin: dashboard, usuários, ranking

    print(f"🤖 Bot rodando | Admin ID: {admin_id}")
    app.run_polling()


if __name__ == "__main__":
    main()
