from __future__ import annotations

from rw_core.modules.repository import read_preferred_table


def read_resultados(limit: int = 500):
    return read_preferred_table("mod_faturamento_tempo_nf_cte_resultados", "analise_tempo_nf_cte", limit)

