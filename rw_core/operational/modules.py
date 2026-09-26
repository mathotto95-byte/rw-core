from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class OperationalModule:
    name: str
    label: str
    imports: tuple[str, ...]
    normalized_tables: tuple[str, ...]
    analyses: tuple[str, ...]
    panels: tuple[str, ...]
    task_types: tuple[str, ...]
    exports: tuple[str, ...]
    logs: tuple[str, ...] = ("painel_processamento_logs",)
    flow: tuple[str, ...] = ("Importacao", "Normalizacao", "Cruzamento", "Geracao de tarefa/prazo", "Painel", "Exportacao")

    @property
    def dependencies(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.imports, *self.normalized_tables, *self.analyses)))


MODULES: dict[str, OperationalModule] = {
    "Fretes IPP": OperationalModule(
        name="Fretes IPP",
        label="Fretes IPP",
        imports=("base_fretes_ipp_original", "base_portal26_original", "base_coupa_original", "base_kmm_original"),
        normalized_tables=("base_kmm_fat_notas_normalizadas", "base_kmm_faturamento_consolidado", "base_fretes_ipp_notas_normalizadas", "base_portal_ipp_normalizada", "base_coupa_fluxos_normalizados"),
        analyses=("analise_fretes_ipp", "analise_ipiranga_fretes_portal26", "analise_kmm_x_coupa", "tarefas_operacionais"),
        panels=(
            "Resumo Fretes IPP",
            "Pendentes de Lancamento",
            "Pagamento Programado",
            "Pagamento OK",
            "Divergencia de Valor",
            "Portal sem KMM",
            "Tarefas Frete",
            "Historico Frete",
        ),
        task_types=("PENDENTE LANCAMENTO PORTAL", "DIVERGENCIA DE VALOR"),
        exports=("Resumo Fretes IPP", "Pendentes de Lancamento", "Historico Frete"),
        logs=("fretes_ipp_logs", "painel_ipiranga_logs", "geracao_lancamento_frete_logs", "painel_processamento_logs"),
    ),
    "Portal IPP": OperationalModule(
        name="Portal IPP",
        label="Portal IPP",
        imports=("base_portal_ipp_original", "base_portal26_original", "base_fretes_ipp_original", "base_kmm_original"),
        normalized_tables=("base_portal_ipp_normalizada", "base_fretes_ipp_notas_normalizadas"),
        analyses=("analise_ipiranga_fretes_portal26", "analise_kmm_portal_ipp"),
        panels=("Portal sem KMM", "Pagamento Programado", "Pagamento OK", "Pendentes de Vinculo", "Historico Portal"),
        task_types=("PENDENTE LANCAMENTO PORTAL", "PORTAL SEM KMM", "DIVERGENCIA DE VALOR"),
        exports=("Portal IPP", "Portal sem KMM", "Pagamentos"),
        logs=("painel_ipiranga_logs", "painel_processamento_logs"),
    ),
    "Correcao de Valores": OperationalModule(
        name="Correcao de Valores",
        label="Correcao de Valores",
        imports=("base_kmm_original", "base_coupa_original"),
        normalized_tables=("base_kmm_fat_notas_normalizadas", "base_kmm_faturamento_consolidado", "base_coupa_fluxos_normalizados"),
        analyses=("analise_complementos_frete", "analise_fretes_ipp", "analise_kmm_x_coupa", "tarefas_operacionais"),
        panels=("Correcao de Valores", "Divergencias de Valor", "Historico Correcoes"),
        task_types=("CORRECAO DE VALOR",),
        exports=("Correcao de Valores", "Tarefas", "Diagnostico"),
        logs=("complementos_frete_logs", "painel_processamento_logs"),
    ),
    "Complementos de Frete": OperationalModule(
        name="Complementos de Frete",
        label="Complementos de Frete",
        imports=("base_kmm_original", "base_kmm_notas_normalizadas"),
        normalized_tables=("base_kmm_fat_notas_normalizadas", "base_kmm_faturamento_consolidado"),
        analyses=("analise_complementos_frete", "analise_fretes_ipp", "tarefas_operacionais"),
        panels=("Complemento de Frete", "Analise Manual", "Historico Complementos"),
        task_types=("COMPLEMENTO DE FRETE",),
        exports=("Complemento de Frete", "Analise Manual", "Tarefas"),
        logs=("complementos_frete_logs", "painel_processamento_logs"),
    ),
    "Complementos de Pedagio": OperationalModule(
        name="Complementos de Pedagio",
        label="Complementos de Pedagio",
        imports=("base_kmm_original", "base_vale_pedagio_rota_eixo"),
        normalized_tables=("base_kmm_fat_notas_normalizadas", "base_kmm_faturamento_consolidado"),
        analyses=("analise_kmm_x_coupa",),
        panels=("Resumo Pedagio", "Divergencias de Pedagio", "Complementos de Pedagio Pendentes", "Historico Pedagio"),
        task_types=("COMPLEMENTO DE PEDAGIO",),
        exports=("Complementos de Pedagio", "Historico Pedagio"),
    ),
    "Indicadores": OperationalModule(
        name="Indicadores",
        label="Indicadores",
        imports=("base_kmm_original", "base_nsdocs_original", "base_coupa_original"),
        normalized_tables=("base_kmm_notas_normalizadas", "base_coupa_fluxos_normalizados"),
        analyses=("analise_indicadores_cliente", "analise_indicadores_placa", "analise_indicadores_tempo"),
        panels=("Indicadores por Cliente", "Indicadores por Placa", "Indicadores de Tempo"),
        task_types=(),
        exports=("Indicadores",),
    ),
    "Tarefas e Prazos": OperationalModule(
        name="Tarefas e Prazos",
        label="Tarefas e Prazos",
        imports=(),
        normalized_tables=(),
        analyses=("tarefas_operacionais",),
        panels=("Central de Tarefas",),
        task_types=("PENDENTE LANCAMENTO PORTAL", "CORRECAO DE VALORES", "COMPLEMENTO DE FRETE", "COMPLEMENTO DE PEDAGIO"),
        exports=("Tarefas Operacionais",),
    ),
    "Relatorios": OperationalModule(
        name="Relatorios",
        label="Relatorios",
        imports=(),
        normalized_tables=(),
        analyses=("painel_processamento_logs", "tarefas_operacionais"),
        panels=("Relatorios",),
        task_types=(),
        exports=("Excel", "CSV", "Backup"),
    ),
}


PANEL_DEPENDENCIES: dict[str, list[str]] = {
    module.name: list(module.dependencies)
    for module in MODULES.values()
}
PANEL_DEPENDENCIES.update(
    {
        "Ipiranga": list(MODULES["Fretes IPP"].dependencies),
        "Validacao Tripla IPP": ["analise_tripla_ipiranga", "base_kmm_original", "base_kmm_fat_notas_normalizadas", "base_portal_ipp_normalizada", "base_coupa_fluxos_normalizados"],
        "KMM x Coupa": ["analise_kmm_x_coupa", "base_kmm_original", "base_coupa_fluxos_normalizados", "base_km_origem_destino"],
        "Conferencia Vale Pedagio": ["base_vale_pedagio_rota_eixo", "analise_kmm_x_coupa"],
    }
)


def module_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for module in MODULES.values():
        rows.append(
            {
                "Modulo": module.label,
                "Importacoes necessarias": ", ".join(module.imports) or "-",
                "Tabelas normalizadas": ", ".join(module.normalized_tables) or "-",
                "Analises salvas": ", ".join(module.analyses) or "-",
                "Paineis": ", ".join(module.panels) or "-",
                "Tarefas": ", ".join(module.task_types) or "-",
                "Prazos": "5 dias para Fretes/Portal; 60 dias para complementos" if module.task_types else "-",
                "Exportacoes": ", ".join(module.exports) or "-",
                "Logs": ", ".join(module.logs) or "-",
                "Fluxo": " -> ".join(module.flow),
            }
        )
    return rows


def dependencies_for(panel: str) -> list[str]:
    return PANEL_DEPENDENCIES.get(panel, [])


def unique_dependencies(panels: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for panel in panels:
        for dependency in dependencies_for(panel):
            seen.setdefault(dependency, None)
    return list(seen)
