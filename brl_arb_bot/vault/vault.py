"""
vault.py — Cofre AES-256 para credenciais dos alunos.
Acesso ao banco: somente o admin (ADMIN_TELEGRAM_ID no .env).
"""

import os
import sqlite3
from cryptography.fernet import Fernet
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

VAULT_DB  = Path("vault/users.db")
KEY_FILE  = Path("vault/.vault_key")

# ─── ID do administrador ──────────────────────────────────────────────────────
ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))


def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID


# ─── Criptografia ─────────────────────────────────────────────────────────────

def _load_or_create_key() -> bytes:
    KEY_FILE.parent.mkdir(exist_ok=True)
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    KEY_FILE.chmod(0o600)
    return key


_fernet = Fernet(_load_or_create_key())


def _enc(v: str) -> str:
    return _fernet.encrypt(v.encode()).decode()

def _dec(v: str) -> str:
    return _fernet.decrypt(v.encode()).decode()


# ─── Banco de dados ───────────────────────────────────────────────────────────

def init_db():
    VAULT_DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(VAULT_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id  INTEGER PRIMARY KEY,
            username     TEXT,
            dex_address  TEXT,
            dex_pk       TEXT,
            trading_mode TEXT DEFAULT 'manual',
            active       INTEGER DEFAULT 1,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS operacoes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id  INTEGER,
            rede         TEXT,
            par          TEXT,
            spread_pct   REAL,
            lucro_usd    REAL,
            tx_hash      TEXT,
            status       TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)

    # Migração simples para bases antigas.
    cols = {
        row[1] for row in con.execute("PRAGMA table_info(users)").fetchall()
    }
    if "trading_mode" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN trading_mode TEXT DEFAULT 'manual'")

    con.commit()
    con.close()


# ─── Usuários ─────────────────────────────────────────────────────────────────

def save_user(telegram_id: int, username: str, address: str, pk: str):
    con = sqlite3.connect(VAULT_DB)
    con.execute("""
        INSERT INTO users (telegram_id, username, dex_address, dex_pk)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username=excluded.username,
            dex_address=excluded.dex_address,
            dex_pk=excluded.dex_pk
    """, (telegram_id, username, address, _enc(pk)))
    con.commit()
    con.close()


def get_user(telegram_id: int) -> dict | None:
    con = sqlite3.connect(VAULT_DB)
    row = con.execute(
        "SELECT telegram_id,username,dex_address,dex_pk,trading_mode,active,created_at "
        "FROM users WHERE telegram_id=? AND active=1", (telegram_id,)
    ).fetchone()
    con.close()
    if not row:
        return None
    return {
        "telegram_id": row[0],
        "username":    row[1],
        "dex_address": row[2],
        "dex_pk":      _dec(row[3]),
        "trading_mode": row[4] or "manual",
        "active":      row[5],
        "created_at":  row[6],
    }


def set_user_trading_mode(telegram_id: int, mode: str) -> None:
    modo = mode.lower().strip()
    if modo not in {"manual", "auto"}:
        raise ValueError("Modo inválido")

    con = sqlite3.connect(VAULT_DB)
    con.execute(
        "UPDATE users SET trading_mode=? WHERE telegram_id=?",
        (modo, telegram_id),
    )
    con.commit()
    con.close()


def user_exists(telegram_id: int) -> bool:
    con = sqlite3.connect(VAULT_DB)
    row = con.execute(
        "SELECT 1 FROM users WHERE telegram_id=?", (telegram_id,)
    ).fetchone()
    con.close()
    return row is not None


# ─── Operações ────────────────────────────────────────────────────────────────

def registrar_operacao(telegram_id: int, rede: str, par: str,
                        spread_pct: float, lucro_usd: float,
                        tx_hash: str, status: str):
    con = sqlite3.connect(VAULT_DB)
    con.execute("""
        INSERT INTO operacoes (telegram_id,rede,par,spread_pct,lucro_usd,tx_hash,status)
        VALUES (?,?,?,?,?,?,?)
    """, (telegram_id, rede, par, spread_pct, lucro_usd, tx_hash, status))
    con.commit()
    con.close()


def historico_usuario(telegram_id: int, limite: int = 10) -> list[dict]:
    con = sqlite3.connect(VAULT_DB)
    rows = con.execute("""
        SELECT rede,par,spread_pct,lucro_usd,tx_hash,status,created_at
        FROM operacoes WHERE telegram_id=?
        ORDER BY created_at DESC LIMIT ?
    """, (telegram_id, limite)).fetchall()
    con.close()
    return [{"rede":r[0],"par":r[1],"spread_pct":r[2],
             "lucro_usd":r[3],"tx_hash":r[4],"status":r[5],"created_at":r[6]}
            for r in rows]


# ─── Funções exclusivas do admin ──────────────────────────────────────────────

def admin_listar_usuarios() -> list[dict]:
    """Retorna todos os usuários cadastrados (sem expor private key)."""
    con = sqlite3.connect(VAULT_DB)
    rows = con.execute("""
        SELECT telegram_id, username, dex_address, active, created_at
        FROM users ORDER BY created_at DESC
    """).fetchall()
    con.close()
    return [{"telegram_id":r[0],"username":r[1],
             "dex_address":r[2],"active":r[3],"created_at":r[4]}
            for r in rows]


def admin_stats_gerais() -> dict:
    """Retorna estatísticas globais do sistema."""
    con = sqlite3.connect(VAULT_DB)

    total_usuarios  = con.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    total_ops       = con.execute("SELECT COUNT(*) FROM operacoes").fetchone()[0]
    total_lucro     = con.execute("SELECT COALESCE(SUM(lucro_usd),0) FROM operacoes WHERE status='sucesso'").fetchone()[0]
    ops_sucesso     = con.execute("SELECT COUNT(*) FROM operacoes WHERE status='sucesso'").fetchone()[0]
    ops_erro        = con.execute("SELECT COUNT(*) FROM operacoes WHERE status='erro'").fetchone()[0]
    lucro_hoje      = con.execute("""
        SELECT COALESCE(SUM(lucro_usd),0) FROM operacoes
        WHERE status='sucesso' AND date(created_at)=date('now')
    """).fetchone()[0]
    melhor_spread   = con.execute("SELECT COALESCE(MAX(spread_pct),0) FROM operacoes WHERE status='sucesso'").fetchone()[0]

    # Top rede por operações
    top_rede = con.execute("""
        SELECT rede, COUNT(*) as n FROM operacoes
        WHERE status='sucesso' GROUP BY rede ORDER BY n DESC LIMIT 1
    """).fetchone()

    con.close()
    return {
        "total_usuarios": total_usuarios,
        "total_ops":      total_ops,
        "ops_sucesso":    ops_sucesso,
        "ops_erro":       ops_erro,
        "total_lucro":    round(total_lucro, 4),
        "lucro_hoje":     round(lucro_hoje, 4),
        "melhor_spread":  round(melhor_spread, 3),
        "top_rede":       top_rede[0] if top_rede else "—",
    }


def admin_lucro_por_usuario() -> list[dict]:
    """Ranking de lucro por aluno."""
    con = sqlite3.connect(VAULT_DB)
    rows = con.execute("""
        SELECT o.telegram_id, u.username,
               COUNT(*) as ops,
               COALESCE(SUM(o.lucro_usd),0) as lucro_total
        FROM operacoes o
        LEFT JOIN users u ON u.telegram_id = o.telegram_id
        WHERE o.status='sucesso'
        GROUP BY o.telegram_id
        ORDER BY lucro_total DESC
    """).fetchall()
    con.close()
    return [{"telegram_id":r[0],"username":r[1] or "—",
             "ops":r[2],"lucro_total":round(r[3],4)}
            for r in rows]


def admin_ops_recentes(limite: int = 15) -> list[dict]:
    """Últimas operações de todos os usuários."""
    con = sqlite3.connect(VAULT_DB)
    rows = con.execute("""
        SELECT o.telegram_id, u.username, o.rede, o.par,
               o.spread_pct, o.lucro_usd, o.status, o.created_at
        FROM operacoes o
        LEFT JOIN users u ON u.telegram_id = o.telegram_id
        ORDER BY o.created_at DESC LIMIT ?
    """, (limite,)).fetchall()
    con.close()
    return [{"telegram_id":r[0],"username":r[1] or "—","rede":r[2],
             "par":r[3],"spread_pct":r[4],"lucro_usd":r[5],
             "status":r[6],"created_at":r[7]}
            for r in rows]
