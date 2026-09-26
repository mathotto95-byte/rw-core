from __future__ import annotations

from rw_core.modules.repository import latest_import_logs, read_preferred_table, table_count


IPIRANGA_SOURCES = {
    "Portal IPP": ("mod_ipiranga_portal_ipp_normalizada", "base_portal_ipp_normalizada"),
    "Portal26": ("mod_ipiranga_portal26_original", "base_portal26_original"),
    "KM IPP": ("mod_ipiranga_km_ipp_normalizada", "base_fretes_ipp_notas_normalizadas"),
}


def source_counts() -> list[dict[str, str | int]]:
    rows = []
    for label, (modular_table, legacy_table) in IPIRANGA_SOURCES.items():
        modular_count = table_count(modular_table)
        legacy_count = table_count(legacy_table)
        rows.append(
            {
                "Fonte": label,
                "Tabela modular": modular_table,
                "Registros modulares": modular_count,
                "Tabela antiga": legacy_table,
                "Registros antigos": legacy_count,
                "Modo": "MODULAR" if modular_count else ("FALLBACK ANTIGO" if legacy_count else "SEM DADOS"),
            }
        )
    return rows


def read_source(label: str, limit: int = 500):
    modular_table, legacy_table = IPIRANGA_SOURCES[label]
    return read_preferred_table(modular_table, legacy_table, limit)


def import_logs(limit: int = 20):
    return latest_import_logs("mod_ipiranga_logs_importacao", limit)

