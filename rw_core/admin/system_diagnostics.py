from __future__ import annotations

import json
import os
import re
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pandas as pd

from src.config.settings import DB_PATH, ROOT_DIR
from rw_core.database.connection import (
    connect_supabase,
    get_connection,
    get_database_config,
    get_supabase_config,
    supabase_is_configured,
)
from rw_core.reports.exporter import dataframe_to_excel
from rw_core.utils.timezone import brasilia_now_iso


SENSITIVE_ENV_PATTERNS = ("PASSWORD", "PASS", "KEY", "TOKEN", "SECRET", "JWT")
BACKUP_NAME_PATTERNS = ("backup", "restore", "supabase", "historico_backup", "catalogo_backup", "snapshot")
BACKUP_FILE_EXTENSIONS = {".zip", ".db", ".sqlite", ".sqlite3", ".dump", ".sql", ".csv", ".parquet", ".xlsx"}
INDEX_FIELDS = [
    "cte_norm",
    "nota_fiscal_norm",
    "chave_nfe",
    "placa_norm",
    "data_emissao",
    "data_hora",
    "cnpj_cpf_remetente_norm",
    "cnpj_cpf_destinatario_norm",
    "codigo_coupa_norm",
    "origem_coupa_norm",
    "destino_coupa_norm",
    "produto_grupo_coupa",
    "lote_importacao",
]
CODE_SCAN_TERMS = [
    "backup",
    "restore",
    "supabase",
    "enviar",
    "receber",
    "restaurar",
    "salvar_backup",
    "load_backup",
    "download_backup",
    "upload_backup",
    "to_supabase",
    "from_supabase",
]


def _df(rows: list[dict[str, Any]], columns: list[str] | None = None) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns or [])


def _mask_value(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "****"
    return f"{text[:4]}****{text[-4:]}"


def mask_database_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc
        if "@" in netloc:
            credentials, host = netloc.rsplit("@", 1)
            user = credentials.split(":", 1)[0] if credentials else ""
            netloc = f"{user}:****@{host}" if user else f"****@{host}"
        query_items = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            query_items.append((key, "****" if any(token in key.upper() for token in SENSITIVE_ENV_PATTERNS) else value))
        return urlunparse(parsed._replace(netloc=netloc, query=urlencode(query_items)))
    except Exception:
        return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:****@", url)


def _safe_env_value(name: str) -> str:
    value = os.getenv(name) or ""
    if not value:
        return ""
    if name.upper() == "DATABASE_URL":
        return mask_database_url(value)
    if any(token in name.upper() for token in SENSITIVE_ENV_PATTERNS):
        return _mask_value(value)
    return value


def _safe_stat(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except Exception:
        return {"size_bytes": 0, "size_mb": 0.0, "modified_at": "", "created_at": ""}
    return {
        "size_bytes": int(stat.st_size),
        "size_mb": round(stat.st_size / (1024 * 1024), 3),
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(timespec="seconds"),
    }


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_names_sqlite(conn) -> list[str]:
    rows = conn.execute(
        """
        select name
        from sqlite_master
        where type = 'table'
          and name not like 'sqlite_%'
        order by name
        """
    ).fetchall()
    return [str(row["name"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]


def _table_columns_sqlite(conn, table: str) -> list[str]:
    try:
        return [str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in conn.execute(f"pragma table_info({_quote_identifier(table)})").fetchall()]
    except Exception:
        return []


def _sqlite_indexes(conn, table: str) -> list[dict[str, Any]]:
    indexes: list[dict[str, Any]] = []
    try:
        index_rows = conn.execute(f"pragma index_list({_quote_identifier(table)})").fetchall()
        for row in index_rows:
            index_name = str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            unique = int(row["unique"] if isinstance(row, sqlite3.Row) else row[2] or 0)
            cols = []
            try:
                for info in conn.execute(f"pragma index_info({_quote_identifier(index_name)})").fetchall():
                    cols.append(str(info["name"] if isinstance(info, sqlite3.Row) else info[2]))
            except Exception:
                cols = []
            indexes.append({"name": index_name, "unique": unique, "columns": cols})
    except Exception:
        pass
    return indexes


def _count_rows(conn, table: str) -> int:
    try:
        return int(conn.execute(f"select count(*) from {_quote_identifier(table)}").fetchone()[0] or 0)
    except Exception:
        return 0


def _latest_update_estimate(conn, table: str, columns: list[str]) -> str:
    candidates = [
        "updated_at",
        "data_atualizacao",
        "data_hora_importacao",
        "data_importacao",
        "data_hora_fim",
        "data_hora_inicio",
        "created_at",
        "data_criacao",
    ]
    for column in candidates:
        if column not in columns:
            continue
        try:
            row = conn.execute(f"select max({_quote_identifier(column)}) from {_quote_identifier(table)}").fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            continue
    return ""


def module_from_name(name: str) -> str:
    text = str(name or "").lower()
    if "estadias" in text or "rastreador" in text or "control" in text or "ots_otd" in text or "otd" in text or "agenda" in text:
        return "Legado removido"
    if "coupa" in text:
        return "Coupa"
    if "faturamento" in text or "nsdocs" in text or "kmm" in text or "cte" in text:
        return "Faturamento"
    if "ipiranga" in text or "ipp" in text or "portal26" in text or "fretes_ipp" in text:
        return "Ipiranga"
    if "usuario" in text or "security" in text or "config" in text or "backup" in text or "restore" in text or "log" in text:
        return "Administracao"
    return "Geral"


def _table_status(name: str, rows: int, indexed: bool) -> str:
    text = str(name or "").lower()
    statuses = []
    if rows == 0:
        statuses.append("VAZIA")
    if any(token in text for token in ("tmp", "temp", "staging")):
        statuses.append("TEMPORARIA")
    if any(token in text for token in BACKUP_NAME_PATTERNS):
        statuses.append("BACKUP NO BANCO")
    if rows > 250_000:
        statuses.append("MUITO GRANDE")
    elif rows > 50_000:
        statuses.append("GRANDE")
    if rows > 10_000 and not indexed:
        statuses.append("SEM INDICE RELEVANTE")
    return " | ".join(statuses) if statuses else "OK"


def _probable_sqlite_status(path: Path, active_path: Path, tables: int, records: int) -> str:
    name = path.name.lower()
    if path.resolve() == active_path.resolve():
        return "ATIVO"
    if records == 0:
        return "VAZIO"
    if "backup" in name or "restore" in name or "antes_restore" in name:
        return "POSSIVEL BACKUP"
    if any(token in name for token in ("copy", "copia", "old", "anterior")):
        return "POSSIVEL COPIA"
    return "INDEFINIDO"


def _sqlite_file_metrics(path: Path) -> tuple[int, int]:
    if not path.exists() or path.stat().st_size == 0:
        return 0, 0
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        tables = [
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
            ).fetchall()
        ]
        records = 0
        for table in tables:
            try:
                records += int(conn.execute(f"select count(*) from {_quote_identifier(table)}").fetchone()[0] or 0)
            except Exception:
                pass
        return len(tables), records
    except Exception:
        return 0, 0
    finally:
        if conn is not None:
            conn.close()


def _iter_project_files(patterns: set[str] | None = None) -> list[Path]:
    skip_dirs = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    files: list[Path] = []
    for path in ROOT_DIR.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if not path.is_file():
            continue
        if patterns is None or path.suffix.lower() in patterns:
            files.append(path)
    return files


def diagnose_current_database() -> pd.DataFrame:
    main_config = get_database_config()
    supa_config = get_supabase_config()
    env_name = os.getenv("CONTROLE_ENV") or os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or os.getenv("ENV") or "local"
    database_url = supa_config.database_url or os.getenv("DATABASE_URL") or os.getenv("CONTROLE_DATABASE_URL") or ""
    supabase_host = supa_config.host or (urlparse(database_url).hostname if database_url else "")
    using_supabase_host = "supabase" in str(supabase_host or "").lower()
    active_sources = ["SQLite/local" if main_config.db_type == "sqlite" else "PostgreSQL"]
    if supabase_is_configured():
        active_sources.append("Supabase configurado")
    status = "OK"
    message = "PostgreSQL detectado como banco principal." if main_config.db_type == "postgres" else "SQLite detectado como banco principal."
    if main_config.db_type == "sqlite" and supabase_is_configured():
        status = "ATENCAO"
        message = "SQLite detectado como banco principal com Supabase configurado como backup/sincronizacao."
    if main_config.db_type == "sqlite" and str(env_name).lower() in {"prod", "production", "producao"}:
        status = "RISCO"
        message = "SQLite detectado como banco principal em ambiente de producao. Avaliar risco de uso simultaneo."
    rows = [
        {"Item": "Banco principal detectado", "Valor": main_config.db_type.upper(), "Status": status, "Mensagem": message},
        {"Item": "Caminho SQLite", "Valor": str(DB_PATH), "Status": "OK" if DB_PATH.exists() else "ATENCAO", "Mensagem": "Arquivo existe." if DB_PATH.exists() else "Arquivo SQLite ainda nao existe."},
        {"Item": "DATABASE_URL configurada", "Valor": mask_database_url(database_url), "Status": "OK" if database_url else "INDEFINIDO", "Mensagem": "Valor mascarado." if database_url else "Nao configurada."},
        {"Item": "Host PostgreSQL/Supabase", "Valor": supabase_host or "", "Status": "OK" if supabase_host else "INDEFINIDO", "Mensagem": "Host Supabase detectado." if using_supabase_host else ""},
        {"Item": "Ambiente atual", "Valor": str(env_name), "Status": "OK", "Mensagem": ""},
        {"Item": "Usa banco local", "Valor": "SIM" if main_config.db_type == "sqlite" else "NAO", "Status": "OK", "Mensagem": ""},
        {"Item": "Usa Supabase", "Valor": "SIM" if supabase_is_configured() else "NAO", "Status": "OK" if supabase_is_configured() else "ATENCAO", "Mensagem": "Supabase configurado." if supabase_is_configured() else "Sem Supabase configurado."},
        {"Item": "Mais de uma fonte ativa", "Valor": "SIM" if len(active_sources) > 1 else "NAO", "Status": "ATENCAO" if len(active_sources) > 1 else "OK", "Mensagem": ", ".join(active_sources)},
    ]
    return _df(rows, ["Item", "Valor", "Status", "Mensagem"])


def diagnose_sqlite_files() -> pd.DataFrame:
    sqlite_files = [path for path in _iter_project_files({".db", ".sqlite", ".sqlite3"}) if path.exists()]
    if DB_PATH.exists() and DB_PATH not in sqlite_files:
        sqlite_files.append(DB_PATH)
    rows = []
    for path in sorted(set(sqlite_files), key=lambda item: str(item).lower()):
        tables, records = _sqlite_file_metrics(path)
        stat = _safe_stat(path)
        rows.append(
            {
                "Arquivo": path.name,
                "Caminho": str(path),
                "Tamanho MB": stat["size_mb"],
                "Ultima modificacao": stat["modified_at"],
                "Quantidade de tabelas": tables,
                "Quantidade de registros": records,
                "Status": _probable_sqlite_status(path, DB_PATH, tables, records),
            }
        )
    return _df(rows, ["Arquivo", "Caminho", "Tamanho MB", "Ultima modificacao", "Quantidade de tabelas", "Quantidade de registros", "Status"])


def diagnose_sqlite_tables() -> pd.DataFrame:
    rows = []
    with get_connection() as conn:
        if getattr(conn, "db_type", "sqlite") != "sqlite":
            return _df(rows)
        for table in _table_names_sqlite(conn):
            columns = _table_columns_sqlite(conn, table)
            indexes = _sqlite_indexes(conn, table)
            records = _count_rows(conn, table)
            rows.append(
                {
                    "Tabela": table,
                    "Modulo provavel": module_from_name(table),
                    "Quantidade de registros": records,
                    "Tamanho estimado": "Proporcional ao SQLite",
                    "Possui indice?": "SIM" if indexes else "NAO",
                    "Ultima atualizacao estimada": _latest_update_estimate(conn, table, columns),
                    "Status": _table_status(table, records, bool(indexes)),
                }
            )
    return _df(rows).sort_values("Quantidade de registros", ascending=False, ignore_index=True) if rows else _df(rows)


def diagnose_backup_tables(tables: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if tables.empty:
        return _df(rows)
    for _, row in tables.iterrows():
        table = str(row.get("Tabela") or "")
        if any(token in table.lower() for token in BACKUP_NAME_PATTERNS):
            rows.append(
                {
                    "Tabela": table,
                    "Quantidade de registros": int(row.get("Quantidade de registros") or 0),
                    "Modulo provavel": row.get("Modulo provavel") or module_from_name(table),
                    "Ultima atualizacao": row.get("Ultima atualizacao estimada") or "",
                    "Status": row.get("Status") or "",
                    "Observacao": "Indicio de backup/restauracao/log de backup salvo no proprio banco.",
                }
            )
    return _df(rows, ["Tabela", "Quantidade de registros", "Modulo provavel", "Ultima atualizacao", "Status", "Observacao"])


def diagnose_local_backup_files() -> pd.DataFrame:
    candidate_dirs = [
        ROOT_DIR / "backups",
        ROOT_DIR / "backup",
        ROOT_DIR / "data" / "backups",
        ROOT_DIR / "data" / "exports",
        ROOT_DIR / "exports",
        ROOT_DIR / "temp",
        ROOT_DIR / "tmp",
        ROOT_DIR / "local_backups",
        ROOT_DIR / "data" / "database",
    ]
    files: list[Path] = []
    for directory in candidate_dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.lower() in BACKUP_FILE_EXTENSIONS:
                files.append(path)
    rows = []
    for path in sorted(set(files), key=lambda item: str(item).lower()):
        stat = _safe_stat(path)
        status = "VAZIO" if stat["size_bytes"] == 0 else "POSSIVEL BACKUP"
        if path.suffix.lower() == ".zip":
            status = "BACKUP ZIP"
        elif path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            status = "BANCO SQLITE"
        elif path.suffix.lower() in {".xlsx", ".csv", ".parquet"}:
            status = "EXPORTACAO"
        if stat["size_mb"] > 200:
            status = f"{status} | MUITO GRANDE"
        if any(token in path.name.lower() for token in ("tmp", "temp")):
            status = f"{status} | TEMPORARIO"
        rows.append(
            {
                "Arquivo": path.name,
                "Caminho": str(path),
                "Tamanho MB": stat["size_mb"],
                "Data criacao/modificacao": stat["modified_at"],
                "Modulo identificado": module_from_name(path.name),
                "Tipo provavel": path.suffix.lower().lstrip(".").upper(),
                "Status": status,
            }
        )
    return _df(rows, ["Arquivo", "Caminho", "Tamanho MB", "Data criacao/modificacao", "Modulo identificado", "Tipo provavel", "Status"])


def diagnose_backup_versioning(local_backups: pd.DataFrame, backup_tables: pd.DataFrame) -> pd.DataFrame:
    filenames = local_backups["Arquivo"].fillna("").astype(str).tolist() if not local_backups.empty else []
    dated = [name for name in filenames if re.search(r"20\d{6}[_-]?\d{0,6}|20\d{2}[-_]\d{2}[-_]\d{2}", name)]
    risky = [name for name in filenames if re.search(r"backup(_|-)?(atual|completo)?\.(zip|db|sqlite3?)$|ultimo_backup", name, flags=re.I)]
    history_tables = set(backup_tables["Tabela"].fillna("").astype(str).str.lower().tolist()) if not backup_tables.empty else set()
    exists_history = any("log" in name or "historico" in name or "restore" in name for name in history_tables)
    status = "OK" if dated and not risky else "ATENCAO"
    if filenames and not dated:
        status = "RISCO ALTO"
    rows = [
        {"Item": "Existe versionamento?", "Valor": "SIM" if dated and len(dated) == len(filenames) else "PARCIAL" if dated else "NAO", "Status": status},
        {"Item": "Backups possuem data/hora no nome?", "Valor": "SIM" if dated else "NAO", "Status": "OK" if dated else "RISCO ALTO"},
        {"Item": "Existe arquivo com nome de sobrescrita?", "Valor": "SIM" if risky else "NAO", "Status": "RISCO ALTO" if risky else "OK"},
        {"Item": "Existe catalogo/historico de backups?", "Valor": "SIM" if exists_history else "NAO", "Status": "OK" if exists_history else "ATENCAO"},
        {"Item": "Existe ultimo backup provavel?", "Valor": "SIM" if filenames else "NAO", "Status": "OK" if filenames else "RISCO ALTO"},
        {"Item": "Existe historico de restauracao?", "Valor": "SIM" if any("restore" in name for name in history_tables) else "NAO", "Status": "OK" if any("restore" in name for name in history_tables) else "ATENCAO"},
    ]
    return _df(rows, ["Item", "Valor", "Status"])


def diagnose_backup_integrity(local_backups: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if local_backups.empty:
        return _df(rows, ["Backup", "Modulo", "Data", "Tamanho", "Registros", "Metadata", "Hash", "Status", "Mensagem"])
    for _, item in local_backups.iterrows():
        path = Path(str(item.get("Caminho") or ""))
        status = "NAO VALIDADO"
        message = "Validacao estrutural limitada; nenhuma restauracao executada."
        metadata = "NAO"
        hash_status = "NAO"
        records: int | str = ""
        try:
            stat = _safe_stat(path)
            if stat["size_bytes"] == 0:
                status = "VAZIO"
                message = "Arquivo vazio."
            elif path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                tables, records_count = _sqlite_file_metrics(path)
                records = records_count
                status = "VALIDO PROVAVEL" if tables and records_count else "VAZIO" if tables == 0 or records_count == 0 else "INCOMPLETO"
                metadata = "SIM" if tables else "NAO"
            elif path.suffix.lower() == ".zip":
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                metadata = "SIM" if any("manifest" in name.lower() or "metadata" in name.lower() for name in names) else "NAO"
                hash_status = "SIM" if any("hash" in name.lower() for name in names) else "NAO"
                status = "VALIDO PROVAVEL" if names else "VAZIO"
                records = len(names)
            elif path.suffix.lower() in {".xlsx", ".csv", ".parquet", ".sql", ".dump"}:
                status = "NAO VALIDADO"
                message = "Arquivo de exportacao identificado, sem leitura completa nesta etapa."
        except Exception as exc:
            status = "ERRO AO LER"
            message = str(exc)
        rows.append(
            {
                "Backup": path.name,
                "Modulo": item.get("Modulo identificado") or module_from_name(path.name),
                "Data": item.get("Data criacao/modificacao") or "",
                "Tamanho": item.get("Tamanho MB") or 0,
                "Registros": records,
                "Metadata": metadata,
                "Hash": hash_status,
                "Status": status,
                "Mensagem": message,
            }
        )
    return _df(rows, ["Backup", "Modulo", "Data", "Tamanho", "Registros", "Metadata", "Hash", "Status", "Mensagem"])


def diagnose_supabase() -> tuple[pd.DataFrame, pd.DataFrame]:
    config = get_supabase_config()
    env_rows = []
    for name in sorted(set(os.environ) | {"DATABASE_URL", "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_SERVICE_KEY", "DB_HOST", "DB_NAME", "DB_USER"}):
        if "SUPABASE" in name.upper() or name.upper() in {"DATABASE_URL", "DB_HOST", "DB_NAME", "DB_USER", "DB_PORT", "DB_SSLMODE"}:
            env_rows.append({"Configuracao": name, "Valor": _safe_env_value(name), "Status": "CONFIGURADO" if os.getenv(name) else "NAO CONFIGURADO"})
    configured = supabase_is_configured()
    connected = False
    table_rows = []
    error = ""
    if configured:
        pg_conn = None
        try:
            pg_conn = connect_supabase()
            connected = True
            tables = [
                str(row[0])
                for row in pg_conn.execute(
                    "select table_name from information_schema.tables where table_schema = 'public' order by table_name"
                ).fetchall()
            ]
            for table in tables:
                count = 0
                try:
                    count = int(pg_conn.execute(f"select count(*) from {_quote_identifier(table)}").fetchone()[0] or 0)
                except Exception:
                    count = 0
                table_rows.append(
                    {
                        "Tabela": table,
                        "Registros": count,
                        "Status": _table_status(table, count, False),
                        "Observacao": "Tabela no Supabase.",
                    }
                )
        except Exception as exc:
            error = str(exc)
        finally:
            if pg_conn is not None:
                pg_conn.close()
    summary = _df(
        [
            {"Item": "Supabase configurado?", "Valor": "SIM" if configured else "NAO", "Status": "OK" if configured else "ATENCAO", "Mensagem": ""},
            {"Item": "Supabase conectado?", "Valor": "SIM" if connected else "NAO", "Status": "OK" if connected else "ATENCAO", "Mensagem": error},
            {"Item": "Supabase usado como banco?", "Valor": "NAO" if get_database_config().db_type == "sqlite" else "SIM/INDEFINIDO", "Status": "OK" if get_database_config().db_type == "sqlite" else "ATENCAO", "Mensagem": "Banco principal atual e SQLite."},
            {"Item": "Supabase usado como backup?", "Valor": "SIM" if configured else "NAO", "Status": "ATENCAO" if configured else "RISCO", "Mensagem": "Ha rotinas de envio/recebimento Supabase no codigo."},
            {"Item": "Host", "Valor": config.safe_host, "Status": "OK" if config.safe_host else "INDEFINIDO", "Mensagem": ""},
            {"Item": "Quantidade de tabelas Supabase", "Valor": len(table_rows), "Status": "OK" if connected else "NAO VALIDADO", "Mensagem": ""},
        ],
        ["Item", "Valor", "Status", "Mensagem"],
    )
    return summary, _df(table_rows, ["Tabela", "Registros", "Status", "Observacao"])


def diagnose_indexes(tables: pd.DataFrame) -> pd.DataFrame:
    rows = []
    with get_connection() as conn:
        if getattr(conn, "db_type", "sqlite") != "sqlite":
            return _df(rows)
        for table in _table_names_sqlite(conn):
            columns = set(_table_columns_sqlite(conn, table))
            indexes = _sqlite_indexes(conn, table)
            indexed_columns = {column for index in indexes for column in index.get("columns", [])}
            records = _count_rows(conn, table)
            for field in INDEX_FIELDS:
                if field not in columns:
                    continue
                priority = "ALTA" if records > 10_000 else "MEDIA" if records > 1_000 else "BAIXA"
                rows.append(
                    {
                        "Tabela": table,
                        "Campo sugerido": field,
                        "Indice existe?": "SIM" if field in indexed_columns else "NAO",
                        "Quantidade registros": records,
                        "Prioridade": priority,
                    }
                )
    return _df(rows, ["Tabela", "Campo sugerido", "Indice existe?", "Quantidade registros", "Prioridade"])


def scan_backup_code() -> pd.DataFrame:
    rows = []
    term_regex = re.compile("|".join(re.escape(term) for term in CODE_SCAN_TERMS), flags=re.I)
    current_func = ""
    for path in [ROOT_DIR / "app.py", *sorted((ROOT_DIR / "src").rglob("*.py"))]:
        if not path.exists() or any(part in {".venv", "__pycache__"} for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, start=1):
            func_match = re.match(r"\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)", line)
            if func_match:
                current_func = func_match.group(1)
            if not term_regex.search(line):
                continue
            line_lower = line.lower()
            risks = []
            if "delete from" in line_lower or "drop table" in line_lower or "replace" in line_lower:
                risks.append("Pode sobrescrever/apagar dados")
            if "supabase" in line_lower and any(word in line_lower for word in ("enviar", "backup", "copy_table")):
                risks.append("Envia dados ao Supabase")
            if "receber" in line_lower or "restore" in line_lower:
                risks.append("Recebe/restaura dados")
            if "backup" in line_lower and "empty" not in line_lower and "vazio" not in line_lower:
                risks.append("Verificar validacao de backup vazio")
            rows.append(
                {
                    "Arquivo": str(path.relative_to(ROOT_DIR)),
                    "Funcao": current_func,
                    "Linha aproximada": lineno,
                    "Descricao provavel": line.strip()[:220],
                    "Risco identificado": "; ".join(risks) if risks else "Baixo/indeterminado",
                }
            )
    return _df(rows, ["Arquivo", "Funcao", "Linha aproximada", "Descricao provavel", "Risco identificado"])


def diagnose_restore(code_scan: pd.DataFrame) -> pd.DataFrame:
    text = " ".join(code_scan.get("Descricao provavel", pd.Series(dtype=str)).fillna("").astype(str).str.lower().tolist())
    rows = [
        ("Existe restauracao por modulo?", "SIM" if "restore_table" in text else "INDEFINIDO", "MEDIO"),
        ("Existe restauracao completa?", "SIM" if "receber_banco_do_supabase" in text or "restore_supabase_to_sqlite" in text else "NAO", "ALTO"),
        ("Existe restauracao do Supabase?", "SIM" if "supabase" in text and ("receber" in text or "restore" in text) else "NAO", "MEDIO"),
        ("Existe restauracao por arquivo?", "INDEFINIDO", "MEDIO"),
        ("A restauracao cria backup pre-restore?", "SIM" if "antes_restore" in text or "backup_local_sqlite_before_replace" in text else "NAO", "ALTO"),
        ("A restauracao valida quantidade de registros?", "SIM" if "total_remote" in text or "total_registros" in text else "INDEFINIDO", "MEDIO"),
        ("A restauracao exige confirmacao?", "SIM" if "confirmo" in text or "checkbox" in text else "INDEFINIDO", "ALTO"),
        ("A restauracao restringe perfil ADMIN?", "INDEFINIDO", "ALTO"),
        ("A restauracao permite escolher versao?", "NAO", "MEDIO"),
        ("A restauracao pode restaurar backup vazio?", "RISCO A VALIDAR", "ALTO"),
    ]
    return _df([{"Pergunta": p, "Resposta": r, "Risco": risk} for p, r, risk in rows], ["Pergunta", "Resposta", "Risco"])


def diagnose_multiuser(tables: pd.DataFrame, code_scan: pd.DataFrame) -> pd.DataFrame:
    table_names = set(tables["Tabela"].fillna("").astype(str).str.lower().tolist()) if not tables.empty else set()
    code_text = " ".join(code_scan.get("Descricao provavel", pd.Series(dtype=str)).fillna("").astype(str).str.lower().tolist())
    checks = [
        ("Login", "usuarios" in table_names, "usuarios"),
        ("Perfil ADMIN", "admin" in code_text, "config/security.yaml"),
        ("Perfil OPERACIONAL", "operacional" in code_text, "config/security.yaml"),
        ("Perfil CONSULTA", "consulta" in code_text, "config/security.yaml"),
        ("Logs de importacao", any("logs_importacao" in name or name == "importacoes" for name in table_names), "importacoes/logs_importacao"),
        ("Logs de backup", any("backup" in name and "log" in name for name in table_names), "backup_automatico_logs/backup_logs"),
        ("Trava por modulo", "backup_runtime_busy" in code_text or "runtime_control" in code_text, "backup_runtime_control"),
        ("Trava de restauracao", "begin_backup_runtime" in code_text, "backup_runtime_control"),
        ("Historico de acoes", "security_logs" in table_names, "security_logs"),
        ("Registro de usuario nos dados importados", "usuario_importacao" in code_text or "usuario" in code_text, "usuario_importacao"),
    ]
    rows = []
    for item, exists, where in checks:
        rows.append(
            {
                "Item": item,
                "Existe?": "SIM" if exists else "NAO",
                "Tabela/Funcao encontrada": where if exists else "",
                "Status": "OK" if exists else "ATENCAO",
                "Recomendacao": "" if exists else "Mapear/implementar controle antes de ampliar uso simultaneo.",
            }
        )
    return _df(rows, ["Item", "Existe?", "Tabela/Funcao encontrada", "Status", "Recomendacao"])


def diagnose_performance(tables: pd.DataFrame, indexes: pd.DataFrame, code_scan: pd.DataFrame) -> pd.DataFrame:
    rows = []
    large_without_index = []
    if not tables.empty:
        large_without_index = tables[
            (pd.to_numeric(tables["Quantidade de registros"], errors="coerce").fillna(0) > 10_000)
            & tables["Possui indice?"].fillna("").astype(str).ne("SIM")
        ]["Tabela"].astype(str).tolist()
    if large_without_index:
        rows.append({"Problema": "Tabelas grandes sem indice", "Local provavel": ", ".join(large_without_index[:8]), "Impacto": "Consultas e filtros lentos", "Recomendacao": "Planejar criacao de indices apos validacao."})
    dataframe_lines = code_scan[code_scan["Descricao provavel"].fillna("").astype(str).str.contains("dataframe|data_editor|read_sql|read_excel", case=False, na=False)] if not code_scan.empty else pd.DataFrame()
    if not dataframe_lines.empty:
        rows.append({"Problema": "Paineis com DataFrames/consultas potencialmente grandes", "Local provavel": f"{len(dataframe_lines)} ocorrencias no codigo", "Impacto": "Renderizacao lenta e alto uso de memoria", "Recomendacao": "Paginar, limitar consultas e preferir filtros no banco."})
    if not indexes.empty and (indexes["Indice existe?"].eq("NAO") & indexes["Prioridade"].isin(["ALTA", "MEDIA"])).any():
        rows.append({"Problema": "Campos importantes sem indice", "Local provavel": "Aba Indices", "Impacto": "Cruzamentos lentos", "Recomendacao": "Criar plano de indices por prioridade."})
    rows.append({"Problema": "Backup/restore pode ser pesado", "Local provavel": "Funcoes Supabase e exportacao", "Impacto": "Pode bloquear sessao se executado em horario de uso", "Recomendacao": "Manter operacoes manuais, versionadas e com travas."})
    return _df(rows, ["Problema", "Local provavel", "Impacto", "Recomendacao"])


def diagnose_modules(tables: pd.DataFrame, local_backups: pd.DataFrame) -> pd.DataFrame:
    modules = ["Ipiranga", "Faturamento", "Coupa", "Administracao", "Geral", "Legado removido"]
    rows = []
    for module in modules:
        module_tables = tables[tables["Modulo provavel"].eq(module)] if not tables.empty else pd.DataFrame()
        module_backups = local_backups[local_backups["Modulo identificado"].eq(module)] if not local_backups.empty else pd.DataFrame()
        records = int(pd.to_numeric(module_tables.get("Quantidade de registros", pd.Series(dtype=int)), errors="coerce").fillna(0).sum()) if not module_tables.empty else 0
        risks = []
        if not module_tables.empty and module_tables["Status"].fillna("").astype(str).str.contains("GRANDE|MUITO GRANDE|SEM INDICE", regex=True).any():
            risks.append("volume/indice")
        if module_tables.empty:
            risks.append("sem tabelas identificadas")
        rows.append(
            {
                "Modulo": module,
                "Tabelas encontradas": int(len(module_tables)),
                "Registros": records,
                "Backups encontrados": int(len(module_backups)),
                "Logs": int(module_tables["Tabela"].fillna("").astype(str).str.contains("log|historico", case=False, regex=True).sum()) if not module_tables.empty else 0,
                "Riscos": ", ".join(risks) if risks else "OK",
            }
        )
    return _df(rows, ["Modulo", "Tabelas encontradas", "Registros", "Backups encontrados", "Logs", "Riscos"])


def build_risk_summary(
    current_db: pd.DataFrame,
    local_backups: pd.DataFrame,
    backup_versioning: pd.DataFrame,
    indexes: pd.DataFrame,
    supabase_summary: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    risk_points = []
    principal = current_db[current_db["Item"].eq("Banco principal detectado")]
    if not principal.empty and str(principal.iloc[0].get("Valor")).upper() == "SQLITE":
        risk_points.append(("Banco principal em SQLite", "LARANJA", "Avaliar PostgreSQL/Supabase como banco operacional para multiusuario."))
    if local_backups.empty:
        risk_points.append(("Ausencia de backup local/exportacao identificada", "VERMELHO", "Criar backup ZIP completo validado e versionado."))
    if not backup_versioning.empty and backup_versioning["Status"].fillna("").astype(str).str.contains("RISCO", case=False, na=False).any():
        risk_points.append(("Versionamento de backup fraco", "VERMELHO", "Usar nomes com data/hora e catalogo de backups."))
    if not indexes.empty and (indexes["Indice existe?"].eq("NAO") & indexes["Prioridade"].eq("ALTA")).any():
        risk_points.append(("Campos de alto volume sem indice", "LARANJA", "Planejar indices em campos criticos."))
    supa_configured = supabase_summary[supabase_summary["Item"].eq("Supabase configurado?")]
    if not supa_configured.empty and supa_configured.iloc[0].get("Valor") == "NAO":
        risk_points.append(("Sem Supabase configurado", "LARANJA", "Garantir backup externo fora do SQLite local."))
    severity_order = {"VERMELHO": 4, "LARANJA": 3, "AMARELO": 2, "VERDE": 1}
    general = "VERDE"
    if risk_points:
        general = max((item[1] for item in risk_points), key=lambda value: severity_order.get(value, 0))
    label = {
        "VERDE": "VERDE - Estrutura saudavel",
        "AMARELO": "AMARELO - Atencao",
        "LARANJA": "LARANJA - Risco elevado",
        "VERMELHO": "VERMELHO - Risco critico",
    }[general]
    summary = _df(
        [
            {"Indicador": "Status geral do sistema", "Valor": label},
            {"Indicador": "Pontos de risco identificados", "Valor": len(risk_points)},
            {"Indicador": "Gerado em", "Valor": brasilia_now_iso()},
        ]
    )
    risks = _df(
        [{"Risco": risk, "Classificacao": level, "Recomendacao": rec} for risk, level, rec in risk_points],
        ["Risco", "Classificacao", "Recomendacao"],
    )
    return summary, risks


def build_recommendations(risks: pd.DataFrame, backup_versioning: pd.DataFrame, indexes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if not risks.empty:
        for _, risk in risks.iterrows():
            priority = "Prioridade 1 - Urgente" if risk.get("Classificacao") == "VERMELHO" else "Prioridade 2 - Importante"
            rows.append({"Prioridade": priority, "Recomendacao": risk.get("Recomendacao") or "", "Motivo": risk.get("Risco") or ""})
    if not indexes.empty and (indexes["Indice existe?"].eq("NAO")).any():
        rows.append({"Prioridade": "Prioridade 2 - Importante", "Recomendacao": "Criar plano de indices por modulo, sem aplicar automaticamente.", "Motivo": "Campos criticos sem indice."})
    rows.append({"Prioridade": "Prioridade 3 - Melhoria", "Recomendacao": "Criar catalogo visual de backups e limpeza controlada de logs antigos.", "Motivo": "Governanca e desempenho."})
    return _df(rows, ["Prioridade", "Recomendacao", "Motivo"])


def build_system_diagnostic_report() -> dict[str, Any]:
    current_db = diagnose_current_database()
    sqlite_files = diagnose_sqlite_files()
    tables = diagnose_sqlite_tables()
    backup_tables = diagnose_backup_tables(tables)
    local_backups = diagnose_local_backup_files()
    backup_versioning = diagnose_backup_versioning(local_backups, backup_tables)
    backup_integrity = diagnose_backup_integrity(local_backups)
    supabase_summary, supabase_tables = diagnose_supabase()
    indexes = diagnose_indexes(tables)
    code_scan = scan_backup_code()
    restore = diagnose_restore(code_scan)
    multiuser = diagnose_multiuser(tables, code_scan)
    performance = diagnose_performance(tables, indexes, code_scan)
    modules = diagnose_modules(tables, local_backups)
    risk_summary, risks = build_risk_summary(current_db, local_backups, backup_versioning, indexes, supabase_summary)
    recommendations = build_recommendations(risks, backup_versioning, indexes)
    sheets = {
        "Resumo": risk_summary,
        "Banco Atual": current_db,
        "Supabase": pd.concat([supabase_summary, supabase_tables.rename(columns={"Tabela": "Item", "Registros": "Valor", "Observacao": "Mensagem"})], ignore_index=True, sort=False),
        "Tabelas": tables,
        "Backups Locais": local_backups,
        "Backups Banco": backup_tables,
        "Backup Versionamento": backup_versioning,
        "Backup Integridade": backup_integrity,
        "Restauracao": restore,
        "Multiusuario": multiuser,
        "Desempenho": performance,
        "Indices": indexes,
        "Modulos": modules,
        "Codigo Backup": code_scan,
        "Riscos": risks,
        "Recomendacoes": recommendations,
        "SQLite Arquivos": sqlite_files,
    }
    return {"generated_at": brasilia_now_iso(), "sheets": sheets}


def report_to_excel_bytes(report: dict[str, Any]) -> bytes:
    sheets = report.get("sheets") or {}
    return dataframe_to_excel({str(name)[:31]: df for name, df in sheets.items() if isinstance(df, pd.DataFrame)})


def report_to_json_bytes(report: dict[str, Any]) -> bytes:
    payload = {"generated_at": report.get("generated_at"), "sheets": {}}
    for name, df in (report.get("sheets") or {}).items():
        if isinstance(df, pd.DataFrame):
            payload["sheets"][name] = df.fillna("").to_dict(orient="records")
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
