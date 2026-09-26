from __future__ import annotations

from rw_core.modules.faturamento.coupa.repository import read_coupa, read_validacao_tarifas


COUPA_STATUS_SUGERIDOS = [
    "COUPA OK",
    "SEM MATCH COUPA",
    "PRODUTO NAO MAPEADO",
    "ORIGEM/DESTINO NAO MAPEADO",
    "FORA DO PERIODO COUPA",
    "TARIFA DIVERGENTE",
    "VOLUME DIVERGENTE",
    "VALOR FATURADO DIVERGENTE",
    "POSSIVEL COMPLEMENTO PENDENTE",
    "FATURADO ACIMA DO CONTRATADO",
]


def validation_preview(limit: int = 100):
    coupa, coupa_fallback, coupa_table = read_coupa(limit)
    results, results_fallback, results_table = read_validacao_tarifas(limit)
    return {
        "coupa": coupa,
        "results": results,
        "fallback": coupa_fallback or results_fallback,
        "tables": {"Coupa": coupa_table, "Validação": results_table},
    }

