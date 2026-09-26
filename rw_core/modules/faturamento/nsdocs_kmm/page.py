from __future__ import annotations

import streamlit as st

from rw_core.dashboards.components import render_dataframe
from rw_core.modules.faturamento.nsdocs_kmm.service import preview


def render_page() -> None:
    st.title("NSDOCS x KMM - Faturamento")
    data = preview()
    if data["fallback"]:
        st.warning("Este módulo está usando dados da estrutura antiga porque a tabela modular ainda está vazia.")
    st.caption(f"Tabelas em uso: {data['tables']}")
    st.subheader("NSDOCS")
    render_dataframe(data["nsdocs"], height=260, max_rows=100)
    st.subheader("LCTE/KMM")
    render_dataframe(data["lcte"], height=260, max_rows=100)

