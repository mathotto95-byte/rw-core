from __future__ import annotations

import streamlit as st

from rw_core.dashboards.components import render_dataframe
from rw_core.modules.faturamento.auditoria_cte.service import preview


def render_page() -> None:
    st.title("Auditoria CT-e KMM - Faturamento")
    data = preview()
    if data["fallback"]:
        st.warning("Este módulo está usando dados da estrutura antiga porque a tabela modular ainda está vazia.")
    st.caption(f"Tabela em uso: {data['table']}")
    render_dataframe(data["lcte"], height=520, max_rows=100)

