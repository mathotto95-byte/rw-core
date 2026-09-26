from __future__ import annotations

from rw_core.modules.faturamento.nsdocs_kmm.repository import read_lcte_notas, read_notas


def preview(limit: int = 100):
    nsdocs, nsdocs_fallback, nsdocs_table = read_notas(limit)
    lcte, lcte_fallback, lcte_table = read_lcte_notas(limit)
    return {
        "nsdocs": nsdocs,
        "lcte": lcte,
        "fallback": nsdocs_fallback or lcte_fallback,
        "tables": {"NSDOCS": nsdocs_table, "LCTE": lcte_table},
    }

