from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from src.config.settings import BACKUPS_DIR, ensure_directories
from rw_core.database.connection import get_connection
from rw_core.database.migrations import modular_tables
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso


IPIRANGA_BACKUP_TABLES = [
    "analise_fretes_ipp",
    "analise_ipiranga_fretes_portal26",
    "conferencia_ipiranga_fretes",
    "historico_ipiranga_fretes",
    "painel_ipiranga_logs",
    "tarefas_operacionais",
    "historico_tarefas_operacionais",
    "controle_tarefas_ipiranga",
    "historico_tarefas_ipiranga",
    "mod_ipiranga_portal_ipp_original",
    "mod_ipiranga_portal_ipp_normalizada",
    "mod_ipiranga_portal26_original",
    "mod_ipiranga_portal26_normalizada",
    "mod_ipiranga_km_ipp_original",
    "mod_ipiranga_km_ipp_normalizada",
    "mod_ipiranga_logs_importacao",
]

FATURAMENTO_BACKUP_TABLES = [
    table
    for table in modular_tables()
    if table.startswith("mod_faturamento_") or table in {"historico_backups_modular", "logs_sistema_modular"}
]

SISTEMA_BACKUP_TABLES = [
    "usuarios",
    "usuario_permissoes_modulo",
    "configuracoes",
    "painel_preferencias_colunas",
    "security_logs",
    "backup_logs",
    "backup_table_logs",
    "restore_logs",
    "restore_table_logs",
    "backup_automatico_logs",
    "historico_backups_modular",
]

MODULE_BACKUP_TABLES = {
    "IPIRANGA": IPIRANGA_BACKUP_TABLES,
    "FATURAMENTO": FATURAMENTO_BACKUP_TABLES,
    "SISTEMA": SISTEMA_BACKUP_TABLES,
}
MODULE_BACKUP_RETENTION = 12


def backup_tables_for_module(module: str) -> list[str]:
    module_key = str(module or "").strip().upper()
    if module_key in MODULE_BACKUP_TABLES:
        return list(MODULE_BACKUP_TABLES[module_key])
    tables: list[str] = []
    for module_tables in MODULE_BACKUP_TABLES.values():
        tables.extend(module_tables)
    return list(dict.fromkeys(tables))


def existing_modules_for_backup() -> list[str]:
    return list(MODULE_BACKUP_TABLES)


def _safe_module_name(module: str) -> str:
    text = str(module or "GERAL").strip().upper()
    text = re.sub(r"[^A-Z0-9_]+", "_", text)
    return text.strip("_") or "GERAL"


def _module_backup_dir(module: str) -> Path:
    ensure_directories()
    path = BACKUPS_DIR / "modulos" / _safe_module_name(module).lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
        (table,),
    ).fetchone()
    return bool(row)


def _quote_sqlite(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _copy_table(source: sqlite3.Connection, target: sqlite3.Connection, table: str) -> int:
    if not _table_exists(source, table):
        return 0
    row = source.execute(
        "select sql from sqlite_master where type = 'table' and name = ?",
        (table,),
    ).fetchone()
    create_sql = str(row[0] if row else "")
    if not create_sql:
        return 0
    target.execute(create_sql)
    columns = [col["name"] for col in source.execute(f"pragma table_info({_quote_sqlite(table)})").fetchall()]
    if not columns:
        return 0
    quoted_columns = ", ".join(_quote_sqlite(column) for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    insert_sql = f"insert into {_quote_sqlite(table)} ({quoted_columns}) values ({placeholders})"
    copied = 0
    cursor = source.execute(f"select {quoted_columns} from {_quote_sqlite(table)}")
    while True:
        rows = cursor.fetchmany(1000)
        if not rows:
            break
        target.executemany(insert_sql, [tuple(row[column] for column in columns) for row in rows])
        copied += len(rows)
    return copied


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _last_good_total(module: str) -> int:
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                select detalhes_json
                from historico_backups_modular
                where modulo = ? and status = 'SUCESSO'
                order by id desc
                limit 1
                """,
                (_safe_module_name(module),),
            ).fetchone()
        if not row:
            return 0
        details = json.loads(row["detalhes_json"] or "{}")
        return int(details.get("total_registros") or 0)
    except Exception:
        return 0


def _insert_history(
    *,
    module: str,
    username: str,
    backup_type: str,
    status: str,
    backup_file: str,
    size: int,
    message: str,
    details: dict[str, Any],
) -> None:
    try:
        with get_connection() as conn:
            conn.execute(
                """
                insert into historico_backups_modular (
                    data_hora, usuario, tipo_backup, modulo, status, arquivo_backup,
                    destino, tamanho, mensagem, detalhes_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    brasilia_now_iso(),
                    username,
                    backup_type,
                    _safe_module_name(module),
                    status,
                    backup_file,
                    "data/backups/modulos",
                    int(size or 0),
                    message[:2000],
                    json.dumps(details, ensure_ascii=False, default=str),
                ),
            )
    except Exception:
        return


def _write_manifest(path: Path, payload: dict[str, Any]) -> Path:
    manifest_path = path.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return manifest_path


def _update_latest(module: str, payload: dict[str, Any]) -> None:
    latest_path = _module_backup_dir(module) / "latest.json"
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _enforce_retention(module: str, keep: int = MODULE_BACKUP_RETENTION) -> None:
    directory = _module_backup_dir(module)
    snapshots = sorted(
        [path for path in directory.glob("*.sqlite3") if not path.name.endswith(".invalid.sqlite3")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_snapshot in snapshots[max(int(keep or MODULE_BACKUP_RETENTION), 1) :]:
        try:
            old_snapshot.unlink()
            old_manifest = old_snapshot.with_suffix(".manifest.json")
            if old_manifest.exists():
                old_manifest.unlink()
        except Exception:
            continue


@contextmanager
def _default_connection_factory() -> Iterator[sqlite3.Connection]:
    with get_connection() as conn:
        yield conn


def create_module_backup(
    module: str,
    username: str = "sistema",
    backup_type: str = "AUTO_MODULAR",
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    module_key = _safe_module_name(module)
    tables = backup_tables_for_module(module_key)
    timestamp = brasilia_now().strftime("%Y%m%d_%H%M%S")
    directory = _module_backup_dir(module_key)
    snapshot_path = directory / f"{timestamp}_{module_key.lower()}.sqlite3"
    started = time.perf_counter()
    status = "SUCESSO"
    message = "Backup modular criado com sucesso."
    table_counts: dict[str, int] = {}
    errors: list[str] = []
    factory = connection_factory or _default_connection_factory

    try:
        with factory() as source_conn:
            source_conn.row_factory = sqlite3.Row
            target_conn = sqlite3.connect(snapshot_path)
            target_conn.row_factory = sqlite3.Row
            try:
                for table in tables:
                    try:
                        table_counts[table] = _copy_table(source_conn, target_conn, table)
                    except Exception as exc:
                        errors.append(f"{table}: {exc}")
                        table_counts[table] = 0
                target_conn.commit()
            finally:
                target_conn.close()
    except Exception as exc:
        status = "ERRO"
        message = f"Falha ao criar backup modular: {exc}"
        errors.append(str(exc))

    total_records = sum(table_counts.values())
    last_good_total = _last_good_total(module_key)
    if status == "SUCESSO" and total_records == 0 and last_good_total > 0:
        status = "IGNORADO_BASE_LOCAL_VAZIA"
        message = "Backup modular ignorado: base atual vazia e ultimo backup bom possui dados."
        invalid_path = snapshot_path.with_name(snapshot_path.stem + ".invalid.sqlite3")
        try:
            snapshot_path.replace(invalid_path)
            snapshot_path = invalid_path
        except Exception:
            pass
    elif status == "SUCESSO" and last_good_total > 0 and total_records < max(1, int(last_good_total * 0.15)):
        status = "IGNORADO_REGRESSAO"
        message = "Backup modular ignorado: queda brusca de registros em relacao ao ultimo backup bom."
        invalid_path = snapshot_path.with_name(snapshot_path.stem + ".invalid.sqlite3")
        try:
            snapshot_path.replace(invalid_path)
            snapshot_path = invalid_path
        except Exception:
            pass

    file_hash = _sha256_file(snapshot_path) if snapshot_path.exists() else ""
    details = {
        "modulo": module_key,
        "tabelas": table_counts,
        "total_registros": total_records,
        "ultimo_total_bom": last_good_total,
        "hash_sha256": file_hash,
        "erros": errors,
        "duracao_segundos": round(time.perf_counter() - started, 3),
    }
    manifest = {
        "data_hora": brasilia_now_iso(),
        "usuario": username,
        "tipo_backup": backup_type,
        "modulo": module_key,
        "status": status,
        "arquivo_backup": str(snapshot_path),
        "tamanho": snapshot_path.stat().st_size if snapshot_path.exists() else 0,
        "mensagem": message,
        "detalhes": details,
    }
    _write_manifest(snapshot_path, manifest)
    if status == "SUCESSO":
        _update_latest(module_key, manifest)
        _enforce_retention(module_key)
    _insert_history(
        module=module_key,
        username=username,
        backup_type=backup_type,
        status=status,
        backup_file=str(snapshot_path),
        size=manifest["tamanho"],
        message=message,
        details=details,
    )
    return manifest


def create_all_module_backups(
    username: str = "sistema",
    backup_type: str = "AUTO_MODULAR",
    modules: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    selected = list(modules or existing_modules_for_backup())
    results = []
    for module in selected:
        results.append(create_module_backup(module, username=username, backup_type=backup_type))
    return results
