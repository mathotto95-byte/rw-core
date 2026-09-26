from __future__ import annotations

import pandas as pd
import streamlit as st

from rw_core.dashboards.components import metric_grid, render_dataframe
from rw_core.modules.faturamento.repository import (
    active_de_para_by_cnpj,
    create_de_para_coupa,
    de_para_coupa,
    de_para_history,
    import_logs,
    set_de_para_active,
    update_de_para_coupa,
)
from rw_core.modules.faturamento.service import dashboard_summary, operational_indicators
from rw_core.modules.migration import copiar_dados_modulares
from rw_core.normalizers.fields import normalize_cnpj
from rw_core.reports.exporter import dataframe_to_excel


def render_page() -> None:
    st.title("Faturamento")
    metric_grid(operational_indicators(), columns=5)
    summary = dashboard_summary()
    if not summary.empty and summary["Modo"].astype(str).str.contains("FALLBACK").any():
        st.warning("Este módulo está usando dados da estrutura antiga porque uma ou mais tabelas modulares ainda estão vazias.")
    st.subheader("Importações e bases")
    render_dataframe(summary, height=260, max_rows=50)
    st.subheader("Últimas importações")
    render_dataframe(import_logs(), height=280, max_rows=20)


def render_imports_page() -> None:
    st.title("Importações Faturamento")
    st.caption("Fontes obrigatórias: Coupa, NSDOCS e LCTE. As importações atuais continuam em Importar Arquivos durante a transição.")
    render_dataframe(dashboard_summary(), height=260, max_rows=50)
    render_dataframe(import_logs(50), height=420, max_rows=50)


def render_imports_transition_page(usuario: str = "sistema") -> None:
    st.title("Importacoes Faturamento")
    st.caption("Fontes obrigatorias: Coupa, NSDOCS e LCTE. As importacoes atuais continuam em Importar Arquivos durante a transicao.")
    summary = dashboard_summary()
    if not summary.empty and summary["Modo"].astype(str).str.contains("FALLBACK").any():
        st.warning("Uma ou mais fontes ainda estao usando fallback antigo. Copie para a estrutura modular quando estiver pronto para testar.")
    render_dataframe(summary, height=260, max_rows=50)

    st.subheader("Copiar bases antigas para modulo Faturamento")
    substituir = st.checkbox(
        "Substituir dados modulares existentes antes de copiar",
        value=False,
        help="Por seguranca, deixe desligado. Ligado, limpa as tabelas modulares de Faturamento e copia novamente das tabelas antigas.",
        key="mod_faturamento_copy_replace",
    )
    if st.button("Copiar dados para Faturamento Modular", type="primary", use_container_width=True):
        with st.spinner("Copiando dados antigos para tabelas modulares Faturamento..."):
            report = copiar_dados_modulares("FATURAMENTO", usuario, replace_existing=substituir)
        st.success("Copia modular Faturamento concluida.")
        render_dataframe(report, height=320, max_rows=100)
        st.download_button(
            "Baixar relatorio da copia Faturamento",
            dataframe_to_excel({"relatorio": report}),
            "relatorio_copia_faturamento_modular.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    render_dataframe(import_logs(50), height=420, max_rows=50)


def render_logs_page() -> None:
    st.title("Logs Faturamento")
    render_dataframe(import_logs(100), height=560, max_rows=100)


def render_de_para_coupa_page(usuario: str) -> None:
    st.title("De/Para Coupa")
    st.caption("Vincule CNPJ/Razão Social do LCTE à nomenclatura usada na Coupa. Exclusões são lógicas via inativação.")

    with st.expander("Adicionar vínculo", expanded=False):
        with st.form("form_mod_de_para_coupa"):
            col_a, col_b = st.columns(2)
            cnpj = col_a.text_input("CNPJ")
            razao = col_b.text_input("Razão social LCTE")
            nomenclatura = st.text_input("Nomenclatura Coupa")
            col_c, col_d, col_e = st.columns(3)
            tipo_vinculo = col_c.selectbox("Tipo de vínculo", ["CLIENTE", "ORIGEM", "DESTINO", "ORIGEM_DESTINO", "OUTRO"])
            origem_destino = col_d.selectbox("Origem/Destino", ["", "ORIGEM", "DESTINO", "AMBOS"])
            uf = col_e.text_input("UF")
            col_f, col_g = st.columns(2)
            cidade = col_f.text_input("Cidade")
            cliente = col_g.text_input("Cliente")
            observacao = st.text_area("Observação")
            submitted = st.form_submit_button("Salvar vínculo", type="primary", use_container_width=True)
            if submitted:
                missing = [label for label, value in {
                    "CNPJ": cnpj,
                    "Razão social LCTE": razao,
                    "Nomenclatura Coupa": nomenclatura,
                    "Tipo de vínculo": tipo_vinculo,
                    "Observação": observacao,
                }.items() if not str(value or "").strip()]
                if missing:
                    st.error("Preencha: " + ", ".join(missing))
                else:
                    existing = active_de_para_by_cnpj(normalize_cnpj(cnpj))
                    if existing and any(str(row.get("nomenclatura_coupa") or "") != nomenclatura for row in existing):
                        st.warning("Já existe vínculo ativo para este CNPJ com outra nomenclatura. Revise antes de manter os dois ativos.")
                    create_de_para_coupa(
                        {
                            "cnpj": cnpj,
                            "razao_social_lcte": razao,
                            "nomenclatura_coupa": nomenclatura,
                            "tipo_vinculo": tipo_vinculo,
                            "origem_destino": origem_destino,
                            "uf": uf,
                            "cidade": cidade,
                            "cliente": cliente,
                            "observacao": observacao,
                        },
                        usuario,
                    )
                    st.success("Vínculo criado.")
                    st.rerun()

    search = st.text_input("Buscar por CNPJ, razão social ou nomenclatura Coupa")
    include_inactive = st.checkbox("Mostrar inativos", value=True)
    df = de_para_coupa(search, include_inactive)
    render_dataframe(df, height=360, max_rows=300)

    if df.empty:
        return

    ids = [int(value) for value in df["id"].dropna().tolist()]
    selected_id = st.selectbox("Selecionar vínculo para histórico/ação", ids)
    selected_row = df[df["id"].eq(selected_id)].iloc[0].to_dict() if selected_id else {}
    col_a, col_b = st.columns(2)
    if col_a.button("Inativar vínculo", use_container_width=True, disabled=not bool(selected_row.get("ativo"))):
        set_de_para_active(selected_id, False, usuario, "Inativação manual pelo módulo Faturamento.")
        st.success("Vínculo inativado.")
        st.rerun()
    if col_b.button("Reativar vínculo", use_container_width=True, disabled=bool(selected_row.get("ativo"))):
        set_de_para_active(selected_id, True, usuario, "Reativação manual pelo módulo Faturamento.")
        st.success("Vínculo reativado.")
        st.rerun()

    with st.expander("Editar vínculo selecionado", expanded=False):
        with st.form("form_edit_mod_de_para_coupa"):
            col_a, col_b = st.columns(2)
            edit_cnpj = col_a.text_input("CNPJ", value=str(selected_row.get("cnpj") or ""), key="edit_depara_cnpj")
            edit_razao = col_b.text_input("Razão social LCTE", value=str(selected_row.get("razao_social_lcte") or ""), key="edit_depara_razao")
            edit_nomenclatura = st.text_input("Nomenclatura Coupa", value=str(selected_row.get("nomenclatura_coupa") or ""), key="edit_depara_nomenclatura")
            col_c, col_d, col_e = st.columns(3)
            tipos = ["CLIENTE", "ORIGEM", "DESTINO", "ORIGEM_DESTINO", "OUTRO"]
            origens = ["", "ORIGEM", "DESTINO", "AMBOS"]
            edit_tipo = col_c.selectbox(
                "Tipo de vínculo",
                tipos,
                index=tipos.index(str(selected_row.get("tipo_vinculo") or "CLIENTE")) if str(selected_row.get("tipo_vinculo") or "CLIENTE") in tipos else 0,
                key="edit_depara_tipo",
            )
            edit_origem_destino = col_d.selectbox(
                "Origem/Destino",
                origens,
                index=origens.index(str(selected_row.get("origem_destino") or "")) if str(selected_row.get("origem_destino") or "") in origens else 0,
                key="edit_depara_origem_destino",
            )
            edit_uf = col_e.text_input("UF", value=str(selected_row.get("uf") or ""), key="edit_depara_uf")
            col_f, col_g = st.columns(2)
            edit_cidade = col_f.text_input("Cidade", value=str(selected_row.get("cidade") or ""), key="edit_depara_cidade")
            edit_cliente = col_g.text_input("Cliente", value=str(selected_row.get("cliente") or ""), key="edit_depara_cliente")
            edit_observacao = st.text_area("Observação", value=str(selected_row.get("observacao") or ""), key="edit_depara_observacao")
            edit_submitted = st.form_submit_button("Salvar edição", type="primary", use_container_width=True)
            if edit_submitted:
                update_de_para_coupa(
                    selected_id,
                    {
                        "cnpj": edit_cnpj,
                        "razao_social_lcte": edit_razao,
                        "nomenclatura_coupa": edit_nomenclatura,
                        "tipo_vinculo": edit_tipo,
                        "origem_destino": edit_origem_destino,
                        "uf": edit_uf,
                        "cidade": edit_cidade,
                        "cliente": edit_cliente,
                        "observacao": edit_observacao,
                    },
                    usuario,
                )
                st.success("Vínculo atualizado.")
                st.rerun()

    history = de_para_history(selected_id)
    st.subheader("Histórico do vínculo")
    render_dataframe(history if not history.empty else pd.DataFrame(), height=280, max_rows=50)
