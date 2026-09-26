from __future__ import annotations

from rw_core.modules.repository import read_preferred_table


def read_lcte(limit: int = 500):
    return read_preferred_table("mod_faturamento_lcte_normalizada", "base_kmm_original", limit)

