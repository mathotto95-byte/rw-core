from __future__ import annotations

from typing import BinaryIO

import pandas as pd

from rw_core.database.connection import get_connection
from rw_core.database.schema import BASE_TABLES
from rw_core.importers.import_service import import_excel_file


def carregar_base(nome_base: str, origem: str = "manual_excel", file: BinaryIO | None = None, sheet_name: str | None = None, username: str = "", user_role: str = "") -> pd.DataFrame:
    """Interface padrao para carga de bases.

    Hoje o sistema trabalha com Excel manual. Se `file` for informado, a rotina
    reaproveita o importador existente. Sem arquivo, retorna a tabela operacional
    ja persistida no SQLite.
    """
    if origem not in {"manual", "manual_excel"}:
        raise ValueError("Origem ainda nao habilitada. Use manual_excel.")
    if file is not None:
        import_excel_file(file, nome_base, sheet_name=sheet_name, username=username, user_role=user_role)
    table = BASE_TABLES.get(nome_base, nome_base)
    with get_connection() as conn:
        try:
            rows = conn.execute(f"select * from {table}").fetchall()
        except Exception:
            rows = []
    return pd.DataFrame([dict(row) for row in rows])

