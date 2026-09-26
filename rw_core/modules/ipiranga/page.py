from __future__ import annotations

import streamlit as st

from rw_core.dashboards.components import metric_grid, render_dataframe
from rw_core.modules.ipiranga.repository import import_logs
from rw_core.modules.ipiranga.service import dashboard_summary
from rw_core.modules.migration import copiar_dados_modulares
from rw_core.reports.exporter import dataframe_to_excel


def render_page() -> None:
    st.title("Ipiranga Modular")
    summary = dashboard_summary()
    metrics = {
        row["Fonte"]: f"{int(row['Registros modulares'])} modulares | {int(row['Registros antigos'])} antigos"
        for _, row in summary.iterrows()
    }
    metric_grid(metrics or {"Fontes Ipiranga": "Sem dados"}, columns=3)
    if not summary.empty and summary["Modo"].astype(str).str.contains("FALLBACK").any():
        st.warning("Este módulo está usando dados da estrutura antiga porque uma ou mais tabelas modulares ainda estão vazias.")
    st.subheader("Fontes")
    render_dataframe(summary, height=260, max_rows=50)
    st.subheader("Logs de importação Ipiranga")
    render_dataframe(import_logs(), height=320, max_rows=20)


def render_logs_page() -> None:
    st.title("Logs Ipiranga")
    render_dataframe(import_logs(100), height=560, max_rows=100)


def render_imports_page(usuario: str = "sistema") -> None:
    st.title("Importacoes Ipiranga Modular")
    st.caption("Transicao segura: as importacoes antigas continuam funcionando; esta tela copia os dados para as tabelas modulares paralelas.")
    summary = dashboard_summary()
    if not summary.empty and summary["Modo"].astype(str).str.contains("FALLBACK").any():
        st.warning("Uma ou mais fontes ainda estao usando fallback antigo. Copie para a estrutura modular quando estiver pronto para testar.")
    render_dataframe(summary, height=260, max_rows=50)

    st.subheader("Copiar bases antigas para modulo Ipiranga")
    substituir = st.checkbox(
        "Substituir dados modulares existentes antes de copiar",
        value=False,
        help="Por seguranca, deixe desligado. Ligado, limpa as tabelas modulares de Ipiranga e copia novamente das tabelas antigas.",
        key="mod_ipiranga_copy_replace",
    )
    if st.button("Copiar dados para Ipiranga Modular", type="primary", use_container_width=True):
        with st.spinner("Copiando dados antigos para tabelas modulares Ipiranga..."):
            report = copiar_dados_modulares("IPIRANGA", usuario, replace_existing=substituir)
        st.success("Copia modular Ipiranga concluida.")
        render_dataframe(report, height=320, max_rows=100)
        st.download_button(
            "Baixar relatorio da copia Ipiranga",
            dataframe_to_excel({"relatorio": report}),
            "relatorio_copia_ipiranga_modular.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    st.subheader("Logs de importacao modular")
    render_dataframe(import_logs(50), height=360, max_rows=50)
