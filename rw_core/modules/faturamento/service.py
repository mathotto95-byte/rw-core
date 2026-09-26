from __future__ import annotations

import pandas as pd

from rw_core.modules.faturamento.repository import source_counts
from rw_core.modules.repository import table_count


def dashboard_summary() -> pd.DataFrame:
    return pd.DataFrame(source_counts())


def operational_indicators() -> dict[str, int]:
    return {
        "CT-es faturados": table_count("mod_faturamento_lcte_notas_normalizadas") or table_count("base_kmm_fat_notas_normalizadas"),
        "NFs": table_count("mod_faturamento_nsdocs_normalizada") or table_count("base_nsdocs_original"),
        "Divergências Coupa": table_count("mod_faturamento_coupa_validacao_tarifas"),
        "Flags pendentes": table_count("mod_faturamento_auditoria_cte_flags"),
        "Validações Tripla IPP": table_count("mod_faturamento_validacao_tripla_ipp_resultado"),
    }

