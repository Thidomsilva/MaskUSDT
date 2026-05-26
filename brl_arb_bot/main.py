"""
main.py — Ponto de entrada do bot de arbitragem BRL stablecoins.
"""

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
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


def _resolve_build_id() -> str:
    """Tenta obter hash curto do commit em runtime para rastrear deploy."""
    env_build = os.environ.get("BOT_BUILD", "").strip()
    if env_build:
        return env_build

    base_dir = Path(__file__).resolve().parent
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(base_dir),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        return out or "desconhecido"
    except Exception:
        return "desconhecido"


def _env_bool(nome: str, default: bool) -> bool:
    raw = os.environ.get(nome)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _notify_boot_admin(token: str, admin_id: str, build_id: str, started_at: str) -> None:
    if not _env_bool("BOOT_NOTIFY_ADMIN", True):
        return

    text = (
        "🟢 Bot reiniciado\n"
        f"Build: {build_id}\n"
        f"Iniciado: {started_at}"
    )
    body = urlencode({
        "chat_id": admin_id,
        "text": text,
    }).encode()

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(req, timeout=8):
            pass
    except Exception as exc:
        logging.getLogger(__name__).warning(f"Falha ao enviar notificação de boot: {exc}")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Configure TELEGRAM_BOT_TOKEN no .env")

    admin_id = os.environ.get("ADMIN_TELEGRAM_ID", "0")
    if admin_id == "0":
        raise RuntimeError("Configure ADMIN_TELEGRAM_ID no .env")

    init_db()

    build_id = _resolve_build_id()
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    os.environ["BOT_BUILD"] = build_id
    os.environ["BOT_STARTED_AT"] = started_at
    _notify_boot_admin(token, admin_id, build_id, started_at)

    app = ApplicationBuilder().token(token).build()

    registrar_todos_handlers(app)      # aluno: cadastro, iniciar, parar, status
    registrar_dashboard_handlers(app)  # aluno: painel com operações e lucros
    registrar_admin_handlers(app)      # admin: dashboard, usuários, ranking

    print(f"🤖 Bot rodando | Admin ID: {admin_id} | Build: {build_id} | Started: {started_at}")
    app.run_polling()


if __name__ == "__main__":
    main()
