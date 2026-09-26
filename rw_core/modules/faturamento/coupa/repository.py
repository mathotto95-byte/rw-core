from __future__ import annotations

import json
from typing import Any

import pandas as pd

from rw_core.database.connection import get_connection, read_sql
from rw_core.database.migrations import _table_columns, initialize_modular_database
from rw_core.modules.faturamento.coupa.imports import (
    COUPA_NORMALIZED_TABLE,
    COUPA_ORIGINAL_TABLE,
    DIAGNOSTIC_TABLE,
    IMPORT_SCOPE_COLUMN,
    IMPORT_SCOPE_FATURAMENTO_COUPA,
    LCTE_ORIGINAL_TABLE,
    LCTE_NORMALIZED_TABLE,
    LCTE_NOTES_TABLE,
    LOG_TABLE,
    MAPPING_HISTORY_TABLE,
    MAPPING_NORMALIZED_TABLE,
    MAPPING_ORIGINAL_TABLE,
    MAPPING_PENDING_TABLE,
    MAPPING_SIMPLE_LOG_TABLE,
    normalizar_cnpj_cpf,
    normalizar_descricao_coupa_para_match,
)
from rw_core.modules.repository import read_preferred_table
from rw_core.normalizers.fields import normalize_cnpj
from rw_core.reports.exporter import dataframe_to_excel
from rw_core.utils.timezone import brasilia_now_iso


SALDO_TABLE = "mod_faturamento_coupa_saldo_lcte_resultado"
COUPA_CLEAR_TABLE_GROUPS = {
    "LCTE": [LCTE_NOTES_TABLE, LCTE_NORMALIZED_TABLE, LCTE_ORIGINAL_TABLE],
    "COUPA": [COUPA_NORMALIZED_TABLE, COUPA_ORIGINAL_TABLE],
    "MAPEAMENTO": [
        MAPPING_PENDING_TABLE,
        MAPPING_NORMALIZED_TABLE,
        MAPPING_ORIGINAL_TABLE,
        MAPPING_HISTORY_TABLE,
        MAPPING_SIMPLE_LOG_TABLE,
    ],
    "SALDO E DIAGNOSTICO": [SALDO_TABLE, DIAGNOSTIC_TABLE],
    "LOGS": [LOG_TABLE],
}


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_exists(conn, table: str) -> bool:
    if getattr(conn, "db_type", "sqlite") == "postgres":
        row = conn.execute(
            """
            select 1
            from information_schema.tables
            where table_schema = 'public' and table_name = ?
            limit 1
            """,
            (table,),
        ).fetchone()
    else:
        row = conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = ? limit 1",
            (table,),
        ).fetchone()
    return bool(row)


def _table_count(conn, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    row = conn.execute(f"select count(*) from {_quote_identifier(table)}").fetchone()
    return int(row[0] if row else 0)


def _filter_coupa_module_scope(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if IMPORT_SCOPE_COLUMN not in df.columns:
        return df.iloc[0:0].copy()
    return df[
        df[IMPORT_SCOPE_COLUMN].fillna("").astype(str).str.strip().eq(IMPORT_SCOPE_FATURAMENTO_COUPA)
    ].copy()


def _scope_condition(conn, table: str) -> tuple[str, tuple[Any, ...]]:
    if not _table_exists(conn, table):
        return "", ()
    if IMPORT_SCOPE_COLUMN not in _table_columns(conn, table):
        return "1 = 0", ()
    return f"coalesce({IMPORT_SCOPE_COLUMN}, '') = ?", (IMPORT_SCOPE_FATURAMENTO_COUPA,)


def _append_scope_where(conn, table: str, where_sql: str, params: list[Any]) -> tuple[str, list[Any]]:
    condition, scope_params = _scope_condition(conn, table)
    if not condition:
        return where_sql, params
    where_sql = f"{where_sql} and {condition}" if where_sql else f"where {condition}"
    params.extend(scope_params)
    return where_sql, params


def _scoped_scalar(conn, table: str, expression: str, extra_where: str = "", params: tuple[Any, ...] = ()) -> Any:
    condition, scope_params = _scope_condition(conn, table)
    where = []
    sql_params: list[Any] = []
    if condition:
        where.append(condition)
        sql_params.extend(scope_params)
    if extra_where:
        where.append(f"({extra_where})")
        sql_params.extend(params)
    where_sql = f" where {' and '.join(where)}" if where else ""
    row = conn.execute(f"select {expression} from {table}{where_sql}", tuple(sql_params)).fetchone()
    return row[0] if row else 0


def _filter_import_logs_coupa_scope(logs: pd.DataFrame) -> pd.DataFrame:
    if logs.empty or "detalhes_json" not in logs.columns:
        return logs

    def is_coupa_scope(value: Any) -> bool:
        try:
            details = json.loads(value or "{}") if isinstance(value, str) else {}
        except Exception:
            return False
        return str(details.get("escopo_importacao") or "").strip() == IMPORT_SCOPE_FATURAMENTO_COUPA

    return logs[logs["detalhes_json"].map(is_coupa_scope)].copy()


def clear_coupa_databases(groups: list[str] | tuple[str, ...], usuario: str = "sistema") -> dict[str, Any]:
    initialize_modular_database()
    selected = [str(group or "").strip().upper() for group in groups if str(group or "").strip()]
    if not selected:
        raise ValueError("Selecione ao menos um grupo de dados para limpar.")
    invalid = [group for group in selected if group not in COUPA_CLEAR_TABLE_GROUPS]
    if invalid:
        raise ValueError(f"Grupo(s) invalido(s): {', '.join(invalid)}")

    tables: list[str] = []
    for group in selected:
        tables.extend(COUPA_CLEAR_TABLE_GROUPS[group])
    tables = list(dict.fromkeys(tables))

    rows = []
    started_at = brasilia_now_iso()
    with get_connection() as conn:
        for table in tables:
            before = _table_count(conn, table)
            if _table_exists(conn, table):
                conn.execute(f"delete from {_quote_identifier(table)}")
                if getattr(conn, "db_type", "sqlite") != "postgres":
                    try:
                        conn.execute("delete from sqlite_sequence where name = ?", (table,))
                    except Exception:
                        pass
            after = _table_count(conn, table)
            rows.append({"Tabela": table, "Antes": before, "Depois": after, "Removidos": max(before - after, 0)})
    return {
        "usuario": usuario,
        "data_hora": started_at,
        "grupos": selected,
        "tabelas": rows,
        "total_removidos": sum(row["Removidos"] for row in rows),
    }


def read_coupa(limit: int = 500):
    return read_preferred_table("mod_faturamento_coupa_normalizada", "base_coupa_fluxos_normalizados", limit)


def read_validacao_tarifas(limit: int = 500):
    return read_preferred_table("mod_faturamento_coupa_validacao_tarifas", "analise_kmm_x_coupa", limit)


def _where_like(filtros: dict[str, Any], mapping: dict[str, str]) -> tuple[str, list[Any]]:
    where = []
    params: list[Any] = []
    for key, column in mapping.items():
        value = str(filtros.get(key) or "").strip()
        if value:
            where.append(f"coalesce({column}, '') like ?")
            params.append(f"%{value}%")
    return ("where " + " and ".join(where)) if where else "", params


def _limit_sql(params: list[Any], limit: int | None) -> str:
    if limit and int(limit) > 0:
        params.append(int(limit))
        return "limit ?"
    return ""


def read_lcte_base(filtros: dict[str, Any] | None = None, limit: int | None = 500) -> pd.DataFrame:
    initialize_modular_database()
    filtros = filtros or {}
    where_sql, params = _where_like(
        filtros,
        {
            "lote_importacao": "lote_importacao",
            "arquivo_origem": "arquivo_origem",
            "razao_social_cobranca": "razao_social_cobranca_norm",
            "cnpj_cobranca": "cnpj_cobranca_norm",
            "origem": "origem_norm",
            "destino": "destino_norm",
            "produto": "produto_norm",
            "placa": "placa_norm",
            "cte": "cte_norm",
            "nf": "nota_fiscal_norm",
            "complemento": "complemento_norm",
        },
    )
    if filtros.get("somente_ipiranga"):
        where_sql = f"{where_sql} and razao_social_cobranca_norm like ?" if where_sql else "where razao_social_cobranca_norm like ?"
        params.append("%IPIRANGA%")
    with get_connection() as conn:
        where_sql, params = _append_scope_where(conn, LCTE_NORMALIZED_TABLE, where_sql, params)
    limit_sql = _limit_sql(params, limit)
    return read_sql(
        f"""
        select *
        from {LCTE_NORMALIZED_TABLE}
        {where_sql}
        order by id desc
        {limit_sql}
        """,
        tuple(params),
    )


def read_lcte_notes(limit: int | None = 500) -> pd.DataFrame:
    initialize_modular_database()
    params: list[Any] = []
    with get_connection() as conn:
        where_sql, params = _append_scope_where(conn, LCTE_NOTES_TABLE, "", params)
    limit_sql = _limit_sql(params, limit)
    return read_sql(f"select * from {LCTE_NOTES_TABLE} {where_sql} order by id desc {limit_sql}", tuple(params))


def read_coupa_contracts(filtros: dict[str, Any] | None = None, limit: int | None = 500) -> pd.DataFrame:
    initialize_modular_database()
    where_sql, params = _where_like(
        filtros or {},
        {
            "lote_importacao": "lote_importacao",
            "arquivo_origem": "arquivo_origem",
            "nomenclatura": "nomenclatura_coupa_norm",
            "origem": "origem_coupa_norm",
            "destino": "destino_coupa_norm",
            "produto": "produto_coupa_norm",
            "status": "status_coupa",
        },
    )
    with get_connection() as conn:
        where_sql, params = _append_scope_where(conn, COUPA_NORMALIZED_TABLE, where_sql, params)
    limit_sql = _limit_sql(params, limit)
    return read_sql(
        f"""
        select *
        from {COUPA_NORMALIZED_TABLE}
        {where_sql}
        order by id desc
        {limit_sql}
        """,
        tuple(params),
    )


def read_mapping(filtros: dict[str, Any] | None = None, limit: int | None = 500) -> pd.DataFrame:
    initialize_modular_database()
    filtros = filtros or {}
    where_sql, params = _where_like(
        filtros,
        {
            "tipo_vinculo": "tipo_vinculo",
            "cnpj_lcte": "cnpj_norm",
            "razao_social_lcte": "razao_social_lcte_norm",
            "origem_lcte": "origem_lcte_norm",
            "destino_lcte": "destino_lcte_norm",
            "produto_lcte": "produto_lcte_norm",
            "nomenclatura_coupa": "descricao_coupa_norm",
            "origem_coupa": "origem_coupa_norm",
            "destino_coupa": "destino_coupa_norm",
            "produto_coupa": "produto_coupa_norm",
            "status_mapeamento": "status_mapeamento",
        },
    )
    ativo = str(filtros.get("ativo") or "").strip().upper()
    if ativo in {"SIM", "NAO"}:
        where_sql = f"{where_sql} and ativo = ?" if where_sql else "where ativo = ?"
        params.append(1 if ativo == "SIM" else 0)
    with get_connection() as conn:
        where_sql, params = _append_scope_where(conn, MAPPING_NORMALIZED_TABLE, where_sql, params)
    limit_sql = _limit_sql(params, limit)
    return read_sql(
        f"""
        select *
        from {MAPPING_NORMALIZED_TABLE}
        {where_sql}
        order by ativo desc, prioridade asc, id desc
        {limit_sql}
        """,
        tuple(params),
    )


def read_diagnostic(limit: int | None = 500) -> pd.DataFrame:
    initialize_modular_database()
    params: list[Any] = []
    limit_sql = _limit_sql(params, limit)
    return read_sql(f"select * from {DIAGNOSTIC_TABLE} order by id desc {limit_sql}", tuple(params))


def read_mapping_pending(limit: int | None = 500) -> pd.DataFrame:
    initialize_modular_database()
    params: list[Any] = []
    limit_sql = _limit_sql(params, limit)
    return read_sql(
        f"""
        select *
        from {MAPPING_PENDING_TABLE}
        where coalesce(resolvido, 0) = 0
        order by resolvido asc, qtd_registros_lcte desc, valor_total_faturado desc, id desc
        {limit_sql}
        """,
        tuple(params),
    )


def read_mapping_history(limit: int | None = 100) -> pd.DataFrame:
    initialize_modular_database()
    params: list[Any] = []
    limit_sql = _limit_sql(params, limit)
    return read_sql(f"select * from {MAPPING_HISTORY_TABLE} order by id desc {limit_sql}", tuple(params))


def read_mapping_conflicts(limit: int | None = 500) -> pd.DataFrame:
    initialize_modular_database()
    params: list[Any] = []
    with get_connection() as conn:
        where_sql, params = _append_scope_where(conn, MAPPING_NORMALIZED_TABLE, "where status_mapeamento = 'CONFLITO_MAPEAMENTO'", params)
    limit_sql = _limit_sql(params, limit)
    return read_sql(
        f"""
        select *
        from {MAPPING_NORMALIZED_TABLE}
        {where_sql}
        order by id desc
        {limit_sql}
        """,
        tuple(params),
    )


def read_mapping_exceptions(limit: int | None = 500) -> pd.DataFrame:
    initialize_modular_database()
    params: list[Any] = []
    where_sql = """
        where coalesce(ativo, 1) = 1
          and tipo_vinculo in ('EXCECAO_PRODUTO', 'EXCECAO_ROTA')
    """
    with get_connection() as conn:
        where_sql, params = _append_scope_where(conn, MAPPING_NORMALIZED_TABLE, where_sql.strip(), params)
    limit_sql = _limit_sql(params, limit)
    return read_sql(
        f"""
        select *
        from {MAPPING_NORMALIZED_TABLE}
        {where_sql}
        order by tipo_vinculo, origem_coupa_norm, destino_coupa_norm, descricao_coupa_norm, produto_grupo_excecao, id desc
        {limit_sql}
        """,
        tuple(params),
    )


def read_mapping_duplicates(limit: int | None = 500) -> pd.DataFrame:
    initialize_modular_database()
    params: list[Any] = []
    limit_sql = _limit_sql(params, limit)
    return read_sql(
        f"""
        select *
        from {MAPPING_HISTORY_TABLE}
        where acao = 'DUPLICIDADE_ELIMINADA'
        order by id desc
        {limit_sql}
        """,
        tuple(params),
    )


def read_import_logs(limit: int = 50) -> pd.DataFrame:
    initialize_modular_database()
    logs = read_sql(
        f"""
        select data_hora, usuario, tipo_importacao, arquivo_origem, hash_arquivo,
               lote_importacao, quantidade_linhas, quantidade_registros_inseridos,
               quantidade_registros_ignorados, status, mensagem, detalhes_json
        from {LOG_TABLE}
        order by id desc
        limit ?
        """,
        (max(int(limit), 200),),
    )
    logs = _filter_import_logs_coupa_scope(logs)
    return logs.head(int(limit)).copy()


def coupa_base_diagnostics() -> dict[str, pd.DataFrame]:
    initialize_modular_database()
    coupa = read_coupa_contracts({}, None)
    required = [
        ("Arquivo", "coluna_arquivo_origem", "codigo_coupa_norm"),
        ("Item", "coluna_item_origem", "item_coupa"),
        ("Dt Inicio", "coluna_dt_inicio_origem", "dt_inicio"),
        ("Dt Termino", "coluna_dt_termino_origem", "dt_termino"),
        ("Origem", "coluna_origem_origem", "origem_coupa"),
        ("Destino", "coluna_destino_origem", "destino_coupa"),
        ("Produto", "coluna_produto_origem", "produto_coupa"),
        ("Qtd", "coluna_qtd_origem", "qtd_contratada"),
    ]
    if coupa.empty:
        empty = pd.DataFrame(columns=["Campo", "Coluna usada", "Status", "Linhas preenchidas", "Linhas vazias"])
        return {"Resumo": empty, "Abas": pd.DataFrame(columns=["Aba", "Linhas", "Colunas encontradas"]), "Falhas": empty}

    rows = []
    for label, column_field, value_field in required:
        column_values = coupa.get(column_field, pd.Series("", index=coupa.index)).fillna("").astype(str)
        value_values = coupa.get(value_field, pd.Series("", index=coupa.index))
        if value_field == "qtd_contratada":
            filled = pd.to_numeric(value_values, errors="coerce").notna()
        else:
            filled = value_values.fillna("").astype(str).str.strip().ne("")
        used = ", ".join(sorted(set(column_values[column_values.str.strip().ne("")]))) if not column_values.empty else ""
        rows.append(
            {
                "Campo": label,
                "Coluna usada": used,
                "Status": "OK" if used else "NAO ENCONTRADA",
                "Linhas preenchidas": int(filled.sum()),
                "Linhas vazias": int((~filled).sum()),
            }
        )
    resumo = pd.DataFrame(rows)
    aba_rows = []
    for aba, group in coupa.groupby(coupa.get("aba_origem_coupa", pd.Series("", index=coupa.index)).fillna("").astype(str), dropna=False):
        columns_found: set[str] = set()
        for raw in group.get("dados_json", pd.Series("", index=group.index)).fillna("").astype(str):
            try:
                columns_found.update(json.loads(raw).keys())
            except Exception:
                pass
        aba_rows.append(
            {
                "Aba": str(aba or "NAO INFORMADA"),
                "Linhas": int(len(group)),
                "Colunas encontradas": ", ".join(sorted(columns_found)),
            }
        )
    counts = pd.DataFrame(
        [
            {"Indicador": "Abas importadas", "Valor": ", ".join(sorted(set(coupa.get("aba_origem_coupa", pd.Series("", index=coupa.index)).fillna("").astype(str))))},
            {"Indicador": "Linhas Coupa", "Valor": int(len(coupa))},
            {"Indicador": "Linhas com codigo Coupa", "Valor": int(coupa.get("codigo_coupa_norm", pd.Series("", index=coupa.index)).fillna("").astype(str).str.strip().ne("").sum())},
            {"Indicador": "Linhas com Qtd preenchida", "Valor": int(pd.to_numeric(coupa.get("qtd_contratada", pd.Series(dtype=float)), errors="coerce").notna().sum())},
            {"Indicador": "Linhas com Qtd vazia", "Valor": int(pd.to_numeric(coupa.get("qtd_contratada", pd.Series(dtype=float)), errors="coerce").isna().sum())},
            {"Indicador": "Linhas com Produto", "Valor": int(coupa.get("produto_coupa", pd.Series("", index=coupa.index)).fillna("").astype(str).str.strip().ne("").sum())},
            {"Indicador": "Linhas com Dt Inicio", "Valor": int(coupa.get("dt_inicio", pd.Series("", index=coupa.index)).fillna("").astype(str).str.strip().ne("").sum())},
            {"Indicador": "Linhas com Dt Termino", "Valor": int(coupa.get("dt_termino", pd.Series("", index=coupa.index)).fillna("").astype(str).str.strip().ne("").sum())},
        ]
    )
    return {"Contadores": counts, "Colunas usadas": resumo, "Abas": pd.DataFrame(aba_rows)}


def generate_mapping_pendencies(usuario: str = "sistema", somente_ipiranga: bool = False) -> dict[str, Any]:
    initialize_modular_database()
    now = brasilia_now_iso()
    lote = "PEND_COUPA_" + now.replace("-", "").replace(":", "").replace("T", "_")[:15]
    coupa = read_coupa_contracts({}, None)
    mapped = read_mapping({"ativo": "SIM"}, None)
    mapped_descriptions = set()
    mapped_cnpjs = set()
    if "cnpj_norm" in mapped.columns:
        mapped_cnpjs.update(str(value).strip() for value in mapped["cnpj_norm"].fillna("").astype(str) if str(value).strip())
    for column in ["descricao_coupa_norm", "nomenclatura_coupa_norm", "origem_coupa_norm", "destino_coupa_norm"]:
        if column in mapped.columns:
            mapped_descriptions.update(
                normalizar_descricao_coupa_para_match(value)
                for value in mapped.get(column, pd.Series(dtype=str)).fillna("").astype(str)
                if str(value).strip()
            )
    columns = ["Tipo", "Descricao Coupa", "CNPJ", "Qtd Linhas Coupa", "Exemplo Codigo", "Exemplo Origem", "Exemplo Destino", "Produto", "Status"]
    rows = []
    if not coupa.empty:
        for source, tipo in [("origem_coupa", "ORIGEM"), ("destino_coupa", "DESTINO")]:
            norm_column = f"{source}_norm"
            work = coupa.copy()
            work["_descricao_original"] = work.get(source, pd.Series("", index=work.index)).fillna("").astype(str)
            work["_descricao_norm"] = work.get(norm_column, pd.Series("", index=work.index)).fillna("").astype(str).map(normalizar_descricao_coupa_para_match)
            work = work[work["_descricao_norm"].ne("")]
            resolved = work["_descricao_norm"].map(
                lambda value: (
                    len(normalizar_cnpj_cpf(value)) in {11, 14}
                    or normalizar_cnpj_cpf(value) in mapped_cnpjs
                    or normalizar_descricao_coupa_para_match(value) in mapped_descriptions
                )
            )
            pending = work[~resolved]
            for descricao_norm, group in pending.groupby("_descricao_norm", dropna=False):
                rows.append(
                    {
                        "Tipo": tipo,
                        "Descricao Coupa": str(group["_descricao_original"].mode().iloc[0] if not group.empty else descricao_norm),
                        "CNPJ": "",
                        "Qtd Linhas Coupa": int(len(group)),
                        "Exemplo Codigo": str(group.get("codigo_coupa_norm", pd.Series("", index=group.index)).fillna("").astype(str).iloc[0] if not group.empty else ""),
                        "Exemplo Origem": str(group.get("origem_coupa", pd.Series("", index=group.index)).fillna("").astype(str).iloc[0] if not group.empty else ""),
                        "Exemplo Destino": str(group.get("destino_coupa", pd.Series("", index=group.index)).fillna("").astype(str).iloc[0] if not group.empty else ""),
                        "Produto": str(group.get("produto_coupa", pd.Series("", index=group.index)).fillna("").astype(str).mode().iloc[0] if not group.empty else ""),
                        "Status": "PRECISA MAPEAMENTO",
                    }
                )
    valid = pd.DataFrame(rows, columns=columns).sort_values(["Tipo", "Descricao Coupa"]) if rows else pd.DataFrame(columns=columns)
    filename = f"pendencias_mapeamento_coupa_{now.replace('-', '').replace(':', '').replace('T', '_')[:15]}.xlsx"
    return {
        "lote": lote,
        "pendencias": valid,
        "invalidos": pd.DataFrame(columns=columns),
        "bytes": dataframe_to_excel({"Pendencias": valid}),
        "filename": filename,
    }


def _pending_summary(df: pd.DataFrame, status: str) -> pd.DataFrame:
    columns = ["CNPJ", "Descricao Coupa", "Razao Social LCTE", "Quantidade Registros LCTE", "Valor Total Faturado", "Volume Total", "Primeira Data CT-e", "Ultima Data CT-e", "Exemplo CT-e", "Exemplo NF", "Origem CNPJ", "Status"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for cnpj, group in df.groupby("cnpj_para_coupa", dropna=False):
        rows.append(
            {
                "CNPJ": str(cnpj or ""),
                "Descricao Coupa": "",
                "Razao Social LCTE": str(group.get("razao_social_cobranca", pd.Series("", index=group.index)).fillna("").astype(str).mode().iloc[0] if not group.empty else ""),
                "Quantidade Registros LCTE": int(len(group)),
                "Valor Total Faturado": float(pd.to_numeric(group.get("valor_frete", pd.Series(0, index=group.index)), errors="coerce").fillna(0).sum()),
                "Volume Total": float(pd.to_numeric(group.get("volume_litros", pd.Series(0, index=group.index)), errors="coerce").fillna(0).sum()),
                "Primeira Data CT-e": str(group.get("data_emissao_cte", pd.Series("", index=group.index)).fillna("").astype(str).min()),
                "Ultima Data CT-e": str(group.get("data_emissao_cte", pd.Series("", index=group.index)).fillna("").astype(str).max()),
                "Exemplo CT-e": str(group.get("cte_norm", pd.Series("", index=group.index)).fillna("").astype(str).iloc[0]),
                "Exemplo NF": str(group.get("nota_fiscal_norm", pd.Series("", index=group.index)).fillna("").astype(str).iloc[0]),
                "Origem CNPJ": str(group.get("origem_cnpj", pd.Series("", index=group.index)).fillna("").astype(str).iloc[0]),
                "Status": status,
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values(["Quantidade Registros LCTE", "Valor Total Faturado"], ascending=[False, False])


def latest_import_summary() -> pd.DataFrame:
    logs = read_import_logs(200)
    if logs.empty:
        return pd.DataFrame(columns=["Tipo", "Ultima importacao", "Usuario", "Arquivo", "Linhas", "Status", "Lote"])
    rows = []
    for kind in ["LCTE", "COUPA", "MAPEAMENTO"]:
        current = logs[logs["tipo_importacao"].fillna("").astype(str).str.upper().eq(kind)]
        if current.empty:
            rows.append({"Tipo": kind, "Ultima importacao": "", "Usuario": "", "Arquivo": "", "Linhas": 0, "Status": "SEM IMPORTACAO", "Lote": ""})
        else:
            item = current.iloc[0]
            rows.append(
                {
                    "Tipo": kind,
                    "Ultima importacao": item.get("data_hora", ""),
                    "Usuario": item.get("usuario", ""),
                    "Arquivo": item.get("arquivo_origem", ""),
                    "Linhas": int(item.get("quantidade_linhas") or 0),
                    "Status": item.get("status", ""),
                    "Lote": item.get("lote_importacao", ""),
                }
            )
    return pd.DataFrame(rows)


def coupa_import_cards() -> pd.DataFrame:
    initialize_modular_database()
    with get_connection() as conn:
        rows = [
            {"Base": "LCTE", "Indicador": "linhas importadas", "Valor": _scoped_scalar(conn, LCTE_NORMALIZED_TABLE, "count(*)")},
            {"Base": "LCTE", "Indicador": "CT-es unicos", "Valor": _scoped_scalar(conn, LCTE_NORMALIZED_TABLE, "count(distinct cte_norm)", "coalesce(cte_norm, '') <> ''")},
            {"Base": "LCTE", "Indicador": "NFs unicas", "Valor": _scoped_scalar(conn, LCTE_NORMALIZED_TABLE, "count(distinct nota_fiscal_norm)", "coalesce(nota_fiscal_norm, '') <> ''")},
            {"Base": "LCTE", "Indicador": "valor total faturado", "Valor": _scoped_scalar(conn, LCTE_NORMALIZED_TABLE, "coalesce(sum(valor_frete), 0)")},
            {"Base": "Coupa", "Indicador": "linhas importadas", "Valor": _scoped_scalar(conn, COUPA_NORMALIZED_TABLE, "count(*)")},
            {"Base": "Coupa", "Indicador": "contratos/nomenclaturas", "Valor": _scoped_scalar(conn, COUPA_NORMALIZED_TABLE, "count(distinct nomenclatura_coupa_norm)", "coalesce(nomenclatura_coupa_norm, '') <> ''")},
            {"Base": "Coupa", "Indicador": "tarifas encontradas", "Valor": _scoped_scalar(conn, COUPA_NORMALIZED_TABLE, "count(*)", "tarifa_coupa is not null")},
            {"Base": "Coupa", "Indicador": "produtos distintos", "Valor": _scoped_scalar(conn, COUPA_NORMALIZED_TABLE, "count(distinct produto_coupa_norm)", "coalesce(produto_coupa_norm, '') <> ''")},
            {"Base": "Mapeamento", "Indicador": "linhas importadas", "Valor": _scoped_scalar(conn, MAPPING_NORMALIZED_TABLE, "count(*)")},
            {"Base": "Mapeamento", "Indicador": "vinculos ativos", "Valor": _scoped_scalar(conn, MAPPING_NORMALIZED_TABLE, "count(*)", "ativo = 1")},
            {"Base": "Mapeamento", "Indicador": "CNPJs mapeados", "Valor": _scoped_scalar(conn, MAPPING_NORMALIZED_TABLE, "count(distinct cnpj_norm)", "ativo = 1 and coalesce(cnpj_norm, '') <> ''")},
            {"Base": "Mapeamento", "Indicador": "CNPJs pendentes", "Valor": _scoped_scalar(conn, MAPPING_PENDING_TABLE, "count(distinct cnpj_norm)", "coalesce(resolvido, 0) = 0 and status_pendencia = 'PENDENTE'")},
            {"Base": "Mapeamento", "Indicador": "CNPJs invalidos", "Valor": _scoped_scalar(conn, MAPPING_PENDING_TABLE, "count(distinct cnpj_norm)", "coalesce(resolvido, 0) = 0 and status_pendencia = 'CNPJ_INVALIDO'")},
            {"Base": "Mapeamento", "Indicador": "conflitos", "Valor": _scoped_scalar(conn, MAPPING_NORMALIZED_TABLE, "count(*)", "status_mapeamento = 'CONFLITO_MAPEAMENTO'")},
            {"Base": "Mapeamento", "Indicador": "duplicidades eliminadas", "Valor": _scoped_scalar(conn, MAPPING_HISTORY_TABLE, "count(*)", "acao = 'DUPLICIDADE_ELIMINADA'")},
        ]
    return pd.DataFrame(rows)


def mapping_template_bytes() -> bytes:
    modelo = pd.DataFrame(
        columns=[
            "Descricao Coupa",
            "CNPJ",
        ]
    )
    instrucoes = pd.DataFrame(
        {
            "Instrucao": [
                "Preencher Descricao Coupa exatamente como deve localizar na base Coupa.",
                "Preencher CNPJ com ou sem pontuacao; o sistema normaliza para 14 digitos.",
                "Manter uma linha por CNPJ.",
                "Se um CNPJ aparecer com descricoes diferentes, o sistema marca conflito.",
                "Para resolver pendencias, preencha apenas a coluna Descricao Coupa e importe a planilha novamente.",
                "Nao apagar cabecalhos.",
            ]
        }
    )
    return dataframe_to_excel({"Base": modelo, "Instrucoes": instrucoes})


def mapping_exception_template_bytes() -> bytes:
    modelo = pd.DataFrame(
        [
            {
                "Descricao Coupa": "Base Exemplo",
                "Produto Coupa": "Biodiesel",
                "CNPJ": "00.000.000/0001-00",
                "Origem Coupa": "",
                "Destino Coupa": "",
                "CNPJ Origem": "",
                "CNPJ Destino": "",
                "Observacao": "Usar este CNPJ somente quando a descricao vier com este produto.",
            },
            {
                "Descricao Coupa": "",
                "Produto Coupa": "Excessao",
                "CNPJ": "",
                "Origem Coupa": "6370 - Araucaria",
                "Destino Coupa": "6250 - Sao Jose do Rio Preto",
                "CNPJ Origem": "00.000.000/0001-00",
                "CNPJ Destino": "00.000.000/0002-00",
                "Observacao": "Excecao de rota: origem/destino/CNPJs prevalecem para esta rota.",
            }
        ]
    )
    instrucoes = pd.DataFrame(
        {
            "Instrucao": [
                "Use esta planilha somente para casos em que a mesma Descricao Coupa precise apontar para CNPJs diferentes conforme o produto.",
                "Descricao Coupa deve ser a origem ou destino conforme aparece na Coupa, sem precisar informar codigo inicial.",
                "Produto Coupa aceita nomes como Derivados, Diesel, Biodiesel, Etanol, Etanol Anidro ou Etanol Hidratado.",
                "Para excecao de rota, preencha Origem Coupa, Destino Coupa, CNPJ Origem e CNPJ Destino.",
                "Na excecao de rota, use Produto Coupa = Excessao quando a regra deve valer para a rota inteira.",
                "A excecao tem prioridade sobre o mapeamento simples somente quando Descricao Coupa e Produto Coupa baterem.",
                "O mapeamento simples continua funcionando para todos os demais casos.",
                "Apague a linha de exemplo antes de importar os dados reais.",
            ]
        }
    )
    return dataframe_to_excel({"Excecoes": modelo, "Instrucoes": instrucoes})


def lcte_template_bytes() -> bytes:
    modelo = pd.DataFrame(
        [
            {
                "CT-e": "1001",
                "NF": "9001",
                "Data da Emissao": "01/08/2026",
                "Razao Social Cobranca": "IPIRANGA PRODUTOS DE PETROLEO SA",
                "CNPJ Cobranca": "33.000.167/0001-01",
                "CNPJ/CPF do remetente": "33.337.122/0192-27",
                "CNPJ/CPF do destinatario": "33.337.122/0090-00",
                "Origem": "SAO PAULO",
                "Destino": "CAMPINAS",
                "Produto": "DIESEL",
                "Volume Litros": 30000,
                "Valor Frete": 1500.00,
                "Placa": "ABC1D23",
                "Motorista": "MOTORISTA EXEMPLO",
                "Complemento": "",
            }
        ]
    )
    instrucoes = pd.DataFrame(
        {
            "Instrucao": [
                "Preencha a aba LCTE e mantenha os cabecalhos.",
                "Campos principais para o abatimento: CT-e, NF, Razao Social Cobranca, Origem, Destino, Produto, Volume Litros e Valor Frete.",
                "Use Razao Social Cobranca com IPIRANGA quando quiser filtrar/validar somente Ipiranga.",
                "CNPJ Cobranca, CNPJ/CPF do remetente e CNPJ/CPF do destinatario podem ser preenchidos com ou sem pontuacao.",
                "Datas podem ser preenchidas em formato dd/mm/aaaa.",
                "Volume Litros e Valor Frete devem ficar numericos.",
                "Apague a linha de exemplo antes de importar os dados reais.",
            ]
        }
    )
    return dataframe_to_excel({"LCTE": modelo, "Instrucoes": instrucoes})


def export_current_mapping_bytes() -> bytes:
    mapping = read_mapping({"ativo": "SIM"}, None)
    if not mapping.empty and "tipo_vinculo" in mapping.columns:
        mapping = mapping[~mapping["tipo_vinculo"].fillna("").astype(str).isin(["EXCECAO_PRODUTO", "EXCECAO_ROTA"])]
    simple = pd.DataFrame(
        {
            "Descricao Coupa": mapping.get("descricao_coupa_original", mapping.get("nomenclatura_coupa", pd.Series(dtype=str))),
            "CNPJ": mapping.get("cnpj_original", mapping.get("cnpj_norm", pd.Series(dtype=str))),
        }
    )
    return dataframe_to_excel({"Base": simple})
