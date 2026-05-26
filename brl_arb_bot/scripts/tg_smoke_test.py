#!/usr/bin/env python3
"""
Smoke test local para preparar validacao no Telegram.

Uso:
  python scripts/tg_smoke_test.py
"""

import asyncio
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    print("Dependencia ausente: python-dotenv")
    print("Instale com: pip install -r requirements.txt")
    sys.exit(1)

from engine.arbitrage import detectar_oportunidades
from vault.vault import init_db


def _check_env() -> list[str]:
    required = [
        "TELEGRAM_BOT_TOKEN",
        "ADMIN_TELEGRAM_ID",
    ]
    missing = [k for k in required if not os.getenv(k, "").strip()]
    return missing


async def _run() -> int:
    here = Path(__file__).resolve()
    project_root = here.parents[1]
    workspace_root = here.parents[2]

    load_dotenv(project_root / ".env")
    load_dotenv(workspace_root / ".env")

    print("[1/4] Validando variaveis de ambiente...")
    missing = _check_env()
    if missing:
        print("ERRO: variaveis obrigatorias ausentes:", ", ".join(missing))
        return 1
    print("OK: variaveis obrigatorias presentes")

    print("[2/4] Inicializando banco/vault...")
    init_db()
    print("OK: banco inicializado")

    print("[3/4] Rodando scanner de oportunidades (1 ciclo)...")
    try:
        oportunidades = await detectar_oportunidades()
    except Exception as exc:
        print(f"ERRO: scanner falhou: {exc}")
        return 1

    print(f"OK: scanner executado | oportunidades: {len(oportunidades)}")
    for idx, op in enumerate(oportunidades[:3], start=1):
        print(
            f"  #{idx} {op.rede} {op.token_brl}/{op.token_usd} "
            f"spread={op.spread_pct:.3f}% lucro={op.lucro_usd:.4f}"
        )

    print("[4/4] Resumo de modo de operacao...")
    print("- Manual: alerta com botoes Executar/Ignorar")
    print("- Automatico: executa swap no loop com cooldown")

    print("\nPronto para testar no Telegram.")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("Interrompido pelo usuario")
        return 130


if __name__ == "__main__":
    sys.exit(main())
