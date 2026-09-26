from __future__ import annotations

from rw_core.modules.faturamento.tempo_nf_cte.repository import read_resultados


def preview(limit: int = 100):
    df, fallback, table = read_resultados(limit)
    return {"resultados": df, "fallback": fallback, "table": table}

