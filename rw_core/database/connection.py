from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso
from typing import Any, Iterator
from urllib.parse import quote_plus, urlparse

from src.config.settings import DB_PATH, ensure_directories

logger = logging.getLogger(__name__)

POSTGRES_TABLES_WITH_ID = {
    "importacoes",
    "arquivos_importados",
    "base_kmm_original",
    "base_fat_kmm_original",
    "base_kmm_notas_normalizadas",
    "base_kmm_fat_notas_normalizadas",
    "base_kmm_faturamento_consolidado",
    "base_nsdocs_original",
    "base_coupa_original",
    "base_coupa_fluxos_normalizados",
    "base_portal_ipp_original",
    "base_portal_ipp_normalizada",
    "base_km_origem_destino",
    "base_vale_pedagio_rota_eixo",
    "historico_vale_pedagio_rota_eixo",
    "analise_nsdocs_x_kmm",
    "analise_tempo_nf_cte",
    "analise_auditoria_kmm",
    "analise_frete_unitario",
    "analise_kmm_x_coupa",
    "analise_kmm_portal_ipp",
    "analise_tripla_ipiranga",
    "analise_coupa_x_faturado_volume",
    "analise_coupa_x_faturado_valor",
    "analise_indicadores_cliente",
    "analise_indicadores_placa",
    "analise_indicadores_tempo",
    "analise_desempenho_frota",
    "inconsistencias",
    "historico_tratamento_inconsistencias",
    "conferencia_notas_sem_cte",
    "historico_conferencia_notas_sem_cte",
    "conferencia_auditoria_kmm",
    "historico_conferencia_auditoria_kmm",
    "conferencia_tempo_nf_cte",
    "historico_conferencia_tempo_nf_cte",
    "base_fretes_ipp_original",
    "base_fretes_ipp_notas_normalizadas",
    "base_portal26_original",
    "base_bases_ipp",
    "analise_ipiranga_fretes_portal26",
    "conferencia_ipiranga_fretes",
    "historico_ipiranga_fretes",
    "geracao_lancamento_frete_logs",
    "painel_ipiranga_logs",
    "modelo_lancamento_frete_mapeamento",
    "historico_notas_portal26",
    "controle_tarefas_ipiranga",
    "historico_tarefas_ipiranga",
    "tarefas_operacionais",
    "historico_tarefas_operacionais",
    "analise_fretes_ipp",
    "fretes_ipp_logs",
    "analise_complementos_operacionais",
    "analise_complementos_frete",
    "complementos_frete_logs",
    "modelos_planilhas_complementos",
    "base_kmm_tempos_normalizada",
    "de_para_origem_coupa_kmm",
    "de_para_coupa_fat",
    "de_para_produto_coupa_kmm",
    "diagnostico_kmm_x_coupa",
    "diagnostico_validacao_tripla_ipp",
    "diagnostico_coupa_x_faturado",
    "usuarios",
    "usuario_permissoes_modulo",
    "security_logs",
    "backup_automatico_logs",
    "logs_sistema_modular",
    "historico_backups_modular",
    "mod_ipiranga_portal_ipp_original",
    "mod_ipiranga_portal_ipp_normalizada",
    "mod_ipiranga_portal26_original",
    "mod_ipiranga_portal26_normalizada",
    "mod_ipiranga_km_ipp_original",
    "mod_ipiranga_km_ipp_normalizada",
    "mod_ipiranga_logs_importacao",
    "mod_faturamento_coupa_original",
    "mod_faturamento_coupa_normalizada",
    "mod_faturamento_coupa_lcte_original",
    "mod_faturamento_coupa_lcte_normalizada",
    "mod_faturamento_coupa_lcte_notas_normalizadas",
    "mod_faturamento_coupa_contrato_original",
    "mod_faturamento_coupa_contrato_normalizada",
    "mod_faturamento_coupa_mapeamento_original",
    "mod_faturamento_coupa_mapeamento_normalizada",
    "mod_faturamento_coupa_mapeamento_pendencias",
    "mod_faturamento_coupa_mapeamento_logs",
    "mod_faturamento_coupa_mapeamento_historico",
    "mod_faturamento_coupa_logs_importacao",
    "mod_faturamento_coupa_validacao_resultado",
    "mod_faturamento_coupa_diagnostico_lcte_mapeamento_coupa",
    "mod_faturamento_coupa_saldo_lcte_resultado",
    "mod_faturamento_nsdocs_original",
    "mod_faturamento_nsdocs_normalizada",
    "mod_faturamento_lcte_original",
    "mod_faturamento_lcte_normalizada",
    "mod_faturamento_lcte_notas_normalizadas",
    "mod_faturamento_logs_importacao",
    "mod_faturamento_de_para_cnpj_coupa",
    "mod_faturamento_historico_de_para_cnpj_coupa",
    "mod_faturamento_coupa_validacao_tarifas",
    "mod_faturamento_validacao_tripla_ipp",
    "mod_faturamento_validacao_tripla_ipp_resultado",
    "mod_faturamento_validacao_tripla_ipp_filial_mapeamento",
    "mod_faturamento_auditoria_cte_flags",
    "mod_faturamento_historico_auditoria_cte_flags",
    "mod_faturamento_tempo_nf_cte_resultados",
    "painel_preferencias_colunas",
}

_ENGINE = None
_LAST_CONNECTION_AT: datetime | None = None


def _cache_resource(func):
    try:
        import streamlit as st

        return st.cache_resource(show_spinner=False)(func)
    except Exception:
        return func


@dataclass(frozen=True)
class DatabaseConfig:
    db_type: str
    database_url: str = ""
    host: str = ""
    port: str = "5432"
    name: str = ""
    user: str = ""
    password: str = ""
    sslmode: str = "require"
    sqlite_path: str = str(DB_PATH)

    @property
    def display_name(self) -> str:
        if self.db_type != "postgres":
            return self.sqlite_path
        if self.name:
            return self.name
        if self.database_url:
            parsed = urlparse(self.database_url)
            return parsed.path.lstrip("/") or "postgres"
        return "postgres"

    @property
    def safe_host(self) -> str:
        host = self.host
        if not host and self.database_url:
            host = urlparse(self.database_url).hostname or ""
        if not host:
            return ""
        parts = host.split(".")
        return parts[0] if len(parts) == 1 else f"{parts[0]}.***"


class DatabaseConnectionError(RuntimeError):
    pass


def _read_streamlit_database_secrets() -> dict[str, Any]:
    try:
        import streamlit as st

        database = st.secrets.get("database", {})
        return dict(database) if database else {}
    except Exception:
        return {}


def _setting(secrets: dict[str, Any], name: str, default: str = "") -> str:
    value = secrets.get(name)
    if value in [None, ""]:
        value = os.getenv(name) or os.getenv(f"CONTROLE_{name}")
    return str(value) if value not in [None, ""] else default


def _is_online_environment() -> bool:
    production_markers = [
        "STREAMLIT_CLOUD",
        "STREAMLIT_SHARING_MODE",
        "RENDER",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_SERVICE_ID",
        "DYNO",
        "FLY_APP_NAME",
        "K_SERVICE",
    ]
    if any(os.getenv(marker) for marker in production_markers):
        return True
    environment = (
        os.getenv("CONTROLE_ENV")
        or os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or os.getenv("ENV")
        or ""
    ).strip().lower()
    return environment in {"prod", "production", "producao"}


def _postgres_is_configured(config: DatabaseConfig) -> bool:
    return bool(config.database_url or all([config.host, config.name, config.user, config.password]))


def get_database_config() -> DatabaseConfig:
    return DatabaseConfig(db_type="sqlite", sqlite_path=str(DB_PATH))


def get_supabase_config() -> DatabaseConfig:
    secrets = _read_streamlit_database_secrets()
    return DatabaseConfig(
        db_type="postgres",
        database_url=_setting(secrets, "DATABASE_URL"),
        host=_setting(secrets, "DB_HOST"),
        port=_setting(secrets, "DB_PORT", "5432"),
        name=_setting(secrets, "DB_NAME"),
        user=_setting(secrets, "DB_USER"),
        password=_setting(secrets, "DB_PASSWORD"),
        sslmode=_setting(secrets, "DB_SSLMODE", "require"),
        sqlite_path=str(DB_PATH),
    )


def supabase_is_configured() -> bool:
    return _postgres_is_configured(get_supabase_config())


def _postgres_url_from_config(config: DatabaseConfig) -> str:
    if config.database_url:
        return config.database_url
    if not _postgres_is_configured(config):
        raise DatabaseConnectionError(
            "Banco PostgreSQL nao configurado. Configure as credenciais no Streamlit Secrets."
        )
    user = quote_plus(config.user)
    password = quote_plus(config.password)
    host = config.host
    name = quote_plus(config.name)
    return f"postgresql://{user}:{password}@{host}:{config.port}/{name}?sslmode={quote_plus(config.sslmode)}"


@_cache_resource
def _build_engine(db_type: str, url: str):
    try:
        from sqlalchemy import create_engine
    except ImportError as exc:
        raise DatabaseConnectionError("Instale sqlalchemy para usar a camada de banco persistente.") from exc

    if db_type == "postgres":
        return create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=5,
            max_overflow=10,
            connect_args={"sslmode": "require"},
        )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        connect_args={"check_same_thread": False, "timeout": 30},
    )


def get_engine():
    global _ENGINE
    config = get_database_config()
    if _ENGINE is not None:
        return _ENGINE
    if config.db_type == "postgres":
        _ENGINE = _build_engine("postgres", _postgres_url_from_config(config))
    else:
        ensure_directories()
        _ENGINE = _build_engine("sqlite", f"sqlite:///{DB_PATH}")
    return _ENGINE


def get_supabase_engine():
    config = get_supabase_config()
    return _build_engine("postgres", _postgres_url_from_config(config))


def _translate_postgres_sql(sql: str) -> str:
    translated = sql.strip()
    insert_ignore = re.match(r"insert\s+or\s+ignore\s+into\s+", translated, flags=re.IGNORECASE)
    if insert_ignore:
        translated = re.sub(
            r"insert\s+or\s+ignore\s+into\s+",
            "insert into ",
            translated,
            count=1,
            flags=re.IGNORECASE,
        )
        translated = f"{translated} on conflict do nothing"
    translated = _translate_postgres_insert_or_replace(translated)
    translated = translated.replace("?", "%s")
    return translated


def _translate_postgres_insert_or_replace(sql: str) -> str:
    match = re.match(
        r"\s*insert\s+or\s+replace\s+into\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*?)\)\s*values\s*\((.*?)\)\s*$",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return sql

    table, columns_sql, values_sql = match.groups()
    columns = [column.strip() for column in columns_sql.replace("\n", " ").split(",")]
    conflict_columns = _postgres_conflict_columns(columns)
    if not conflict_columns:
        return f"insert into {table} ({columns_sql}) values ({values_sql}) on conflict do nothing"

    update_columns = [column for column in columns if column not in set(conflict_columns) and column != "id"]
    if not update_columns:
        action = "do nothing"
    else:
        assignments = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
        action = f"do update set {assignments}"
    conflict_target = ", ".join(conflict_columns)
    return f"insert into {table} ({columns_sql}) values ({values_sql}) on conflict ({conflict_target}) {action}"


def _postgres_conflict_columns(columns: list[str]) -> list[str]:
    if "signature" in columns:
        return ["signature"]
    if "chave" in columns:
        return ["chave"]
    if "username" in columns:
        return ["username"]
    if "kmm_original_id" in columns and "nota_fiscal_normalizada" in columns:
        return ["kmm_original_id", "nota_fiscal_normalizada"]
    return []


def _table_from_insert(sql: str) -> str:
    match = re.match(r"\s*insert\s+(?:or\s+(?:ignore|replace)\s+)?into\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, flags=re.IGNORECASE)
    return match.group(1).lower() if match else ""


class CursorProxy:
    def __init__(self, cursor, db_type: str) -> None:
        self._cursor = cursor
        self._db_type = db_type
        self.lastrowid = getattr(cursor, "lastrowid", None)

    def execute(self, sql: str, params: Any = None):
        if self._db_type == "postgres":
            table = _table_from_insert(sql)
            translated = _translate_postgres_sql(sql)
            should_return_id = (
                table in POSTGRES_TABLES_WITH_ID
                and " returning " not in translated.lower()
                and " on conflict " not in translated.lower()
            )
            if should_return_id:
                translated = f"{translated} returning id"
            self._cursor.execute(translated, params or ())
            if should_return_id:
                row = self._cursor.fetchone()
                self.lastrowid = row[0] if row else None
        else:
            self._cursor.execute(sql, params or ())
            self.lastrowid = getattr(self._cursor, "lastrowid", None)
        return self

    def executemany(self, sql: str, seq_of_params):
        if self._db_type == "postgres":
            from psycopg2.extras import execute_batch

            execute_batch(self._cursor, _translate_postgres_sql(sql), seq_of_params, page_size=1000)
        else:
            self._cursor.executemany(sql, seq_of_params)
        self.lastrowid = getattr(self._cursor, "lastrowid", None)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self) -> None:
        self._cursor.close()

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class ConnectionProxy:
    def __init__(self, connection, db_type: str) -> None:
        self._connection = connection
        self.db_type = db_type

    def execute(self, sql: str, params: Any = None) -> CursorProxy:
        cursor = self.cursor()
        return cursor.execute(sql, params)

    def executemany(self, sql: str, seq_of_params) -> CursorProxy:
        cursor = self.cursor()
        return cursor.executemany(sql, seq_of_params)

    def cursor(self) -> CursorProxy:
        return CursorProxy(self._connection.cursor(), self.db_type)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def _connect_sqlite() -> ConnectionProxy:
    ensure_directories()
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma busy_timeout = 30000")
    conn.execute("pragma wal_autocheckpoint = 1000")
    conn.execute("pragma journal_size_limit = 67108864")
    return ConnectionProxy(conn, "sqlite")


def _connect_postgres(config: DatabaseConfig) -> ConnectionProxy:
    if not _postgres_is_configured(config):
        raise DatabaseConnectionError(
            "Banco PostgreSQL nao configurado. Configure as credenciais no Streamlit Secrets."
        )
    try:
        import psycopg2
        from psycopg2.extras import DictCursor
    except ImportError as exc:
        raise DatabaseConnectionError("Instale psycopg2-binary para conectar ao PostgreSQL.") from exc

    try:
        if config.database_url:
            conn = psycopg2.connect(config.database_url, cursor_factory=DictCursor)
        else:
            conn = psycopg2.connect(
                host=config.host,
                port=config.port,
                dbname=config.name,
                user=config.user,
                password=config.password,
                sslmode=config.sslmode,
                cursor_factory=DictCursor,
            )
        return ConnectionProxy(conn, "postgres")
    except Exception as exc:
        logger.exception("Erro tecnico ao conectar ao PostgreSQL.")
        raise DatabaseConnectionError(
            "Nao foi possivel conectar ao banco de dados persistente. Verifique as configuracoes em Secrets."
        ) from exc


def connect_supabase() -> ConnectionProxy:
    return _connect_postgres(get_supabase_config())


@contextmanager
def get_connection() -> Iterator[ConnectionProxy]:
    global _LAST_CONNECTION_AT
    config = get_database_config()
    conn = _connect_postgres(config) if config.db_type == "postgres" else _connect_sqlite()
    _LAST_CONNECTION_AT = brasilia_now()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_database_status(table_names: list[str] | tuple[str, ...]) -> dict[str, Any]:
    status: dict[str, Any] = {
        "db_type": "",
        "database_name": "",
        "safe_host": "",
        "last_connection_at": _LAST_CONNECTION_AT.isoformat(timespec="seconds") if _LAST_CONNECTION_AT else "",
        "last_import_at": "",
        "last_sync_at": "",
        "last_sync_user": "",
        "connected": False,
        "error": "",
        "table_counts": {},
    }
    try:
        config = get_database_config()
        status["db_type"] = config.db_type
        status["database_name"] = config.display_name
        status["safe_host"] = config.safe_host
        with get_connection() as conn:
            conn.execute("select 1").fetchone()
            status["connected"] = True
            status["last_connection_at"] = _LAST_CONNECTION_AT.isoformat(timespec="seconds") if _LAST_CONNECTION_AT else ""
            try:
                row = conn.execute("select max(data_importacao) from importacoes").fetchone()
                status["last_import_at"] = row[0] if row and row[0] else ""
            except Exception:
                status["last_import_at"] = ""
            try:
                row = conn.execute("select valor from configuracoes where chave = 'ultima_sincronizacao_supabase'").fetchone()
                status["last_sync_at"] = row[0] if row and row[0] else ""
                row = conn.execute("select valor from configuracoes where chave = 'ultimo_usuario_sincronizacao'").fetchone()
                status["last_sync_user"] = row[0] if row and row[0] else ""
            except Exception:
                status["last_sync_at"] = ""
                status["last_sync_user"] = ""
            for table in table_names:
                try:
                    status["table_counts"][table] = conn.execute(f"select count(*) from {table}").fetchone()[0]
                except Exception:
                    status["table_counts"][table] = "indisponivel"
    except Exception as exc:
        logger.exception("Erro tecnico ao diagnosticar banco de dados.")
        status["error"] = str(exc)
    return status


def get_database_status(table_names: list[str] | tuple[str, ...]) -> dict[str, Any]:
    return check_database_status(table_names)


def execute_query(sql: str, params: tuple | list | dict | None = None) -> list[dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.execute(sql, params or ())
        if cursor.description is None:
            return []
        return [dict(row) for row in cursor.fetchall()]


def read_sql(sql: str, params: tuple | list | dict | None = None):
    import pandas as pd

    with get_connection() as conn:
        return pd.read_sql_query(sql, conn._connection, params=params)


def write_df(df, table: str, if_exists: str = "append", index: bool = False) -> int:
    with get_connection() as conn:
        df.to_sql(table, conn._connection, if_exists=if_exists, index=index)
        return int(len(df))


def upsert(table: str, payload: dict[str, Any], conflict_columns: list[str] | tuple[str, ...]) -> None:
    columns = list(payload.keys())
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    values = tuple(payload[column] for column in columns)
    if not conflict_columns:
        sql = f"insert into {table} ({column_sql}) values ({placeholders})"
    else:
        update_columns = [column for column in columns if column not in set(conflict_columns)]
        if update_columns:
            update_sql = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
            conflict_sql = ", ".join(conflict_columns)
            sql = f"insert or replace into {table} ({column_sql}) values ({placeholders})"
            if get_database_config().db_type == "postgres":
                sql = f"insert into {table} ({column_sql}) values ({placeholders}) on conflict ({conflict_sql}) do update set {update_sql}"
        else:
            sql = f"insert or ignore into {table} ({column_sql}) values ({placeholders})"
    with get_connection() as conn:
        conn.execute(sql, values)


def init_database() -> None:
    from rw_core.database.schema import initialize_database

    initialize_database()
