from __future__ import annotations

from rw_core.modules.faturamento.auditoria_cte.repository import read_lcte


def preview(limit: int = 100):
    df, fallback, table = read_lcte(limit)
    return {"lcte": df, "fallback": fallback, "table": table}

