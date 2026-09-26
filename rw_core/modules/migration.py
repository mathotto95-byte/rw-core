from __future__ import annotations

import json
from typing import Any

import pandas as pd

from rw_core.database.connection import get_connection
from rw_core.database.migrations import MODULAR_COPY_MAP, initialize_modular_database
from rw_core.modules.repository import insert_system_log


MODULE_COPY_MAP = {
    "IPIRANGA": {
        source: target
        for source, target in MODULAR_COPY_MAP.items()
        if target.startswith("mod_ipiranga_")
    },
    "FATURAMENTO": {
        source: target
        for source, target in MODULAR_COPY_MAP.items()
        if target.startswith("mod_faturamento_")
    },
}


def _is_postgres(conn) -> bool:
    return getattr(conn, "db_type", "sqlite") == "postgres"


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_exists(conn, table: str) -> bool:
    if _is_postgres(conn):
        row = conn.execute(
            """
            select 1
            from information_schema.tables
            where table_schema = 'public' and table_name = ?
            limit 1
            """,
            (table,),
        ).fetchone()
    else:
        row = conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
            (table,),
        ).fetchone()
    return bool(row)


def _table_columns(conn, table: str) -> list[str]:
    if not _table_exists(conn, table):
        return []
    if _is_postgres(conn):
        rows = conn.execute(
            """
            select column_name
            from information_schema.columns
            where table_schema = 'public' and table_name = ?
            order by ordinal_position
            """,
            (table,),
        ).fetchall()
        return [str(row["column_name"]) for row in rows]
    return [str(row["name"]) for row in conn.execute(f"pragma table_info({_quote(table)})").fetchall()]


def _table_count(conn, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    row = conn.execute(f"select count(*) from {_quote(table)}").fetchone()
    return int(row[0] or 0) if row else 0


def _copy_table(conn, source: str, target: str, replace_existing: bool = False) -> dict[str, Any]:
    if not _table_exists(conn, source):
        return {
            "tabela_origem": source,
            "tabela_destino": target,
            "linhas_origem": 0,
            "linhas_destino": _table_count(conn, target),
            "status": "IGNORADO",
            "observacao": "Tabela origem nao existe.",
        }
    if not _table_exists(conn, target):
        return {
            "tabela_origem": source,
            "tabela_destino": target,
            "linhas_origem": _table_count(conn, source),
            "linhas_destino": 0,
            "status": "ERRO",
            "observacao": "Tabela modular nao existe apos inicializacao.",
        }

    source_count = _table_count(conn, source)
    target_before = _table_count(conn, target)
    if target_before > 0 and not replace_existing:
        return {
            "tabela_origem": source,
            "tabela_destino": target,
            "linhas_origem": source_count,
            "linhas_destino": target_before,
            "status": "MANTIDO",
            "observacao": "Tabela modular ja possui dados; copia evitada para nao duplicar lote.",
        }

    source_columns = _table_columns(conn, source)
    target_columns = _table_columns(conn, target)
    common_columns = [column for column in source_columns if column in target_columns and column != "id"]
    if not common_columns:
        return {
            "tabela_origem": source,
            "tabela_destino": target,
            "linhas_origem": source_count,
            "linhas_destino": target_before,
            "status": "ERRO",
            "observacao": "Nenhuma coluna compativel encontrada.",
        }

    if replace_existing:
        conn.execute(f"delete from {_quote(target)}")
    quoted_columns = ", ".join(_quote(column) for column in common_columns)
    conn.execute(
        f"insert into {_quote(target)} ({quoted_columns}) "
        f"select {quoted_columns} from {_quote(source)}"
    )
    target_after = _table_count(conn, target)
    expected = source_count if replace_existing or target_before == 0 else target_before + source_count
    return {
        "tabela_origem": source,
        "tabela_destino": target,
        "linhas_origem": source_count,
        "linhas_destino": target_after,
        "status": "OK" if target_after == expected else "ATENCAO",
        "observacao": f"{len(common_columns)} coluna(s) copiadas.",
    }


def copiar_dados_modulares(module: str = "TODOS", usuario: str = "sistema", replace_existing: bool = False) -> pd.DataFrame:
    initialize_modular_database()
    module_key = str(module or "TODOS").strip().upper()
    if module_key in MODULE_COPY_MAP:
        copy_map = MODULE_COPY_MAP[module_key]
    else:
        module_key = "TODOS"
        copy_map = MODULAR_COPY_MAP

    rows = []
    with get_connection() as conn:
        for source, target in copy_map.items():
            rows.append(_copy_table(conn, source, target, replace_existing=replace_existing))

    report = pd.DataFrame(rows)
    status_counts = report["status"].value_counts().to_dict() if not report.empty else {}
    try:
        insert_system_log(
            usuario=usuario,
            modulo=module_key,
            submodulo="MIGRACAO_MODULAR",
            acao="COPIAR_DADOS_PARA_MODULOS",
            nivel="INFO" if not status_counts.get("ERRO") else "ERRO",
            mensagem=f"Migracao modular executada para {module_key}.",
            detalhes={"replace_existing": replace_existing, "status": status_counts, "relatorio": rows},
        )
    except Exception as exc:
        report["observacao_log"] = f"Log nao registrado: {exc}"
    return report


def report_to_json(report: pd.DataFrame) -> str:
    return json.dumps(report.to_dict(orient="records"), ensure_ascii=False, default=str)
