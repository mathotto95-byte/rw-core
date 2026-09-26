from __future__ import annotations

from rw_core.modules.repository import read_preferred_table


def read_notas(limit: int = 500):
    return read_preferred_table("mod_faturamento_nsdocs_normalizada", "base_nsdocs_original", limit)


def read_lcte_notas(limit: int = 500):
    return read_preferred_table("mod_faturamento_lcte_notas_normalizadas", "base_kmm_fat_notas_normalizadas", limit)

