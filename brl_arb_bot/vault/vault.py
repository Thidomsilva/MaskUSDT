"""
vault.py — Cofre AES-256 para credenciais dos alunos.
Acesso ao banco: somente o admin (ADMIN_TELEGRAM_ID no .env).
"""

import os
import sqlite3
import logging
from cryptography.fernet import Fernet
from pathlib import Path


logger = logging.getLogger(__name__)


def _load_env() -> None:
    paths = [
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",
    ]

    try:
        from dotenv import load_dotenv  # type: ignore

        for p in paths:
            if p.exists():
                load_dotenv(p, override=False)
        return
    except Exception:
        pass

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

VAULT_DB  = Path("vault/users.db")
KEY_FILE  = Path("vault/.vault_key")

# ─── ID do administrador ──────────────────────────────────────────────────────
def _env_int(nome: str, default: int = 0) -> int:
    raw = os.environ.get(nome)
    if raw is None:
        return default
    s = raw.strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        logger.warning("Variável %s inválida (%r); usando padrão %s", nome, raw, default)
        return default


ADMIN_ID = _env_int("ADMIN_TELEGRAM_ID", 0)


def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID


def _env_bool(nome: str, default: bool = False) -> bool:
    raw = os.environ.get(nome)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _validar_fernet_key(raw_key: str) -> bytes:
    key = raw_key.strip().encode()
    try:
        Fernet(key)
    except Exception as exc:
        raise RuntimeError(
            "VAULT_MASTER_KEY inválida. Gere uma Fernet key URL-safe base64 com 32 bytes."
        ) from exc
    return key


def _lock_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except Exception:
        pass


# ─── Criptografia ─────────────────────────────────────────────────────────────

def _load_or_create_key() -> bytes:
    env_key = os.environ.get("VAULT_MASTER_KEY", "").strip()
    require_env_key = _env_bool("VAULT_REQUIRE_ENV_KEY", False)

    if env_key:
        return _validar_fernet_key(env_key)

    if require_env_key:
        raise RuntimeError(
            "Segurança estrita ativa: defina VAULT_MASTER_KEY no ambiente do deploy."
        )

    KEY_FILE.parent.mkdir(exist_ok=True)
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        _lock_file(KEY_FILE)
        return _validar_fernet_key(key)

    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    _lock_file(KEY_FILE)
    logger.warning(
        "VAULT_MASTER_KEY ausente; usando fallback local em vault/.vault_key. "
        "Para alta segurança, ative VAULT_REQUIRE_ENV_KEY=true e forneça a chave via ambiente."
    )
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

    # Tabela de posições de ciclo
    con.execute("""
        CREATE TABLE IF NOT EXISTS posicoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            ciclo_numero INTEGER,
            token_atual TEXT,
            amount_token REAL,
            saldo_entrada REAL,
            saldo_atual_usd REAL,
            hops TEXT,
            status TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
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
    _lock_file(VAULT_DB)


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


def get_user(telegram_id: int, include_pk: bool = False) -> dict | None:
    con = sqlite3.connect(VAULT_DB)
    row = con.execute(
        "SELECT telegram_id,username,dex_address,dex_pk,trading_mode,active,created_at "
        "FROM users WHERE telegram_id=? AND active=1", (telegram_id,)
    ).fetchone()
    con.close()
    if not row:
        return None
    user = {
        "telegram_id": row[0],
        "username":    row[1],
        "dex_address": row[2],
        "trading_mode": row[4] or "manual",
        "active":      row[5],
        "created_at":  row[6],
    }
    if include_pk:
        user["dex_pk"] = _dec(row[3])
    return user


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



    # ─── Funções de ciclo de arbitragem ───────────────────────────────────────────

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


# ─── Ciclos de arbitragem (compatibilidade) ─────────────────────────────────

def get_numero_ciclos(telegram_id: int) -> int:
    con = sqlite3.connect(VAULT_DB)
    row = con.execute(
        "SELECT COALESCE(MAX(ciclo_numero), 0) FROM posicoes WHERE telegram_id=?",
        (telegram_id,),
    ).fetchone()
    con.close()
    return int((row or [0])[0] or 0)


def get_posicao_aberta(telegram_id: int) -> dict | None:
    con = sqlite3.connect(VAULT_DB)
    row = con.execute(
        """
        SELECT telegram_id, ciclo_numero, token_atual, amount_token,
               saldo_entrada, saldo_atual_usd, hops, status, created_at, updated_at
        FROM posicoes
        WHERE telegram_id=? AND status='aberto'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (telegram_id,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {
        "telegram_id": row[0],
        "ciclo_numero": row[1],
        "token_atual": row[2],
        "amount_token": float(row[3] or 0),
        "saldo_entrada": float(row[4] or 0),
        "saldo_atual_usd": float(row[5] or 0),
        "hops": row[6] or "[]",
        "status": row[7],
        "created_at": row[8],
        "updated_at": row[9],
    }


def criar_posicao(
    telegram_id: int,
    ciclo_numero: int,
    token_atual: str,
    amount_token: float,
    saldo_entrada: float,
) -> None:
    con = sqlite3.connect(VAULT_DB)
    con.execute(
        """
        INSERT INTO posicoes (
            telegram_id, ciclo_numero, token_atual, amount_token,
            saldo_entrada, saldo_atual_usd, hops, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'aberto')
        """,
        (
            telegram_id,
            ciclo_numero,
            token_atual,
            float(amount_token),
            float(saldo_entrada),
            float(saldo_entrada),
            "[]",
        ),
    )
    con.commit()
    con.close()


def atualizar_posicao(
    telegram_id: int,
    token_atual: str,
    amount_token: float,
    saldo_atual_usd: float,
    hops: str,
) -> None:
    con = sqlite3.connect(VAULT_DB)
    con.execute(
        """
        UPDATE posicoes
        SET token_atual=?, amount_token=?, saldo_atual_usd=?, hops=?,
            updated_at=datetime('now')
        WHERE id = (
            SELECT id FROM posicoes
            WHERE telegram_id=? AND status='aberto'
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
        )
        """,
        (token_atual, float(amount_token), float(saldo_atual_usd), hops, telegram_id),
    )
    con.commit()
    con.close()


def fechar_posicao(telegram_id: int, saldo_final_usd: float) -> None:
    con = sqlite3.connect(VAULT_DB)
    con.execute(
        """
        UPDATE posicoes
        SET saldo_atual_usd=?, status='fechado', updated_at=datetime('now')
        WHERE id = (
            SELECT id FROM posicoes
            WHERE telegram_id=? AND status='aberto'
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
        )
        """,
        (float(saldo_final_usd), telegram_id),
    )
    con.commit()
    con.close()


def get_saldo_historico(telegram_id: int) -> dict:
    con = sqlite3.connect(VAULT_DB)

    row_init = con.execute(
        """
        SELECT saldo_entrada
        FROM posicoes
        WHERE telegram_id=?
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        (telegram_id,),
    ).fetchone()

    row_open = con.execute(
        """
        SELECT saldo_atual_usd
        FROM posicoes
        WHERE telegram_id=? AND status='aberto'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (telegram_id,),
    ).fetchone()

    row_last_closed = con.execute(
        """
        SELECT saldo_atual_usd
        FROM posicoes
        WHERE telegram_id=? AND status='fechado'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (telegram_id,),
    ).fetchone()

    row_counts = con.execute(
        """
        SELECT
            COUNT(*) as total_ciclos,
            COALESCE(SUM(saldo_atual_usd - saldo_entrada), 0) as lucro_total,
            COALESCE(AVG((julianday(updated_at) - julianday(created_at)) * 86400), 0) as tempo_medio
        FROM posicoes
        WHERE telegram_id=? AND status='fechado'
        """,
        (telegram_id,),
    ).fetchone()

    con.close()

    saldo_inicial = float((row_init or [0])[0] or 0)
    if row_open and row_open[0] is not None:
        saldo_atual = float(row_open[0])
    elif row_last_closed and row_last_closed[0] is not None:
        saldo_atual = float(row_last_closed[0])
    else:
        saldo_atual = saldo_inicial

    total_ciclos = int((row_counts or [0, 0, 0])[0] or 0)
    lucro_total = float((row_counts or [0, 0, 0])[1] or 0)
    tempo_medio = float((row_counts or [0, 0, 0])[2] or 0)

    return {
        "saldo_inicial": saldo_inicial,
        "saldo_atual": saldo_atual,
        "lucro_total": lucro_total,
        "total_ciclos": total_ciclos,
        "tempo_medio_ciclo": tempo_medio,
    }
