from __future__ import annotations

from datetime import datetime
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso
from pathlib import Path

import pandas as pd

from src.config.settings import DB_PATH
from rw_core.database.connection import get_connection
from rw_core.database.schema import table_columns


def _table_names(conn) -> list[str]:
    rows = conn.execute(
        """
        select name
        from sqlite_master
        where type = 'table'
          and name not like 'sqlite_%'
        order by name
        """
    ).fetchall()
    return [row["name"] for row in rows]


def _count_rows(conn, table: str) -> int:
    try:
        return int(conn.execute(f"select count(*) from {table}").fetchone()[0] or 0)
    except Exception:
        return 0


def _duplicates(conn, table: str) -> int:
    columns = table_columns(conn, table)
    candidates = ["signature", "row_hash", "key_hash", "hash_registro"]
    for column in candidates:
        if column not in columns:
            continue
        try:
            value = conn.execute(
                f"""
                select count(*)
                from (
                    select {column}, count(*) qtd
                    from {table}
                    where coalesce({column}, '') <> ''
                    group by {column}
                    having count(*) > 1
                ) duplicated
                """
            ).fetchone()[0]
            return int(value or 0)
        except Exception:
            return 0
    return 0


def _month_counts(conn, table: str) -> tuple[int, int]:
    columns = table_columns(conn, table)
    date_column = next((column for column in ["data_importacao", "data_processamento", "created_at", "data_criacao"] if column in columns), "")
    if not date_column:
        return 0, 0
    now = brasilia_now()
    current = now.strftime("%Y-%m")
    previous_month = (now.replace(day=1) - pd.DateOffset(days=1)).strftime("%Y-%m")
    try:
        current_count = conn.execute(f"select count(*) from {table} where substr(coalesce({date_column}, ''), 1, 7) = ?", (current,)).fetchone()[0]
        previous_count = conn.execute(f"select count(*) from {table} where substr(coalesce({date_column}, ''), 1, 7) = ?", (previous_month,)).fetchone()[0]
    except Exception:
        return 0, 0
    return int(current_count or 0), int(previous_count or 0)


def database_diagnostics(db_path: Path | None = None) -> dict[str, pd.DataFrame | dict[str, float]]:
    path = db_path or DB_PATH
    size_bytes = path.stat().st_size if path.exists() else 0
    with get_connection() as conn:
        tables = _table_names(conn)
        table_rows = []
        current_total = 0
        previous_total = 0
        for table in tables:
            rows = _count_rows(conn, table)
            duplicated = _duplicates(conn, table)
            current_month, previous_month = _month_counts(conn, table)
            current_total += current_month
            previous_total += previous_month
            table_rows.append(
                {
                    "Tabela": table,
                    "Registros": rows,
                    "Duplicidades estimadas": duplicated,
                    "Registros mes atual": current_month,
                    "Registros mes anterior": previous_month,
                }
            )
    tables_df = pd.DataFrame(table_rows).sort_values("Registros", ascending=False) if table_rows else pd.DataFrame()
    monthly_growth = max(current_total, previous_total, 0)
    mb_total = size_bytes / (1024 * 1024)
    bytes_per_row = size_bytes / max(int(tables_df["Registros"].sum()) if not tables_df.empty else 1, 1)
    mb_per_month = monthly_growth * bytes_per_row / (1024 * 1024)
    gb_5y = mb_per_month * 60 / 1024
    summary = {
        "Tamanho SQLite MB": round(mb_total, 2),
        "Quantidade de tabelas": int(len(tables)),
        "Total de registros": int(tables_df["Registros"].sum()) if not tables_df.empty else 0,
        "Registros por mes estimado": int(monthly_growth),
        "MB por mes estimado": round(mb_per_month, 2),
        "GB estimado em 5 anos": round(gb_5y, 2),
    }
    heavy = tables_df.head(15).copy() if not tables_df.empty else pd.DataFrame()
    growth = pd.DataFrame(
        [
            {"Periodo": "Mes anterior", "Registros": int(previous_total)},
            {"Periodo": "Mes atual", "Registros": int(current_total)},
            {"Periodo": "Estimativa mensal", "Registros": int(monthly_growth)},
        ]
    )
    return {"summary": summary, "tables": tables_df, "heavy": heavy, "growth": growth}

