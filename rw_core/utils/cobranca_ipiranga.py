from __future__ import annotations

import unicodedata
from typing import Any

import pandas as pd


COBRANCA_ALIASES = [
    "razao_social_cobranca",
    "razao_social_da_cobranca",
    "razao_social_cobranca_cliente",
    "razao_social_da_cobranca_cliente",
    "Razao social da cobranca",
    "Razao social da cobranca/cliente",
    "Razão social da cobrança",
    "Razao Social da Cobranca",
    "Razao Social da Cobranca/Cliente",
    "Razão Social da Cobrança",
    "Razao social cobranca",
    "Razão social cobrança",
    "razao_social_tomador",
    "razao_social_do_tomador",
    "tomador",
    "pagador",
    "Cliente cobranca",
    "Cliente cobrança",
    "Cobranca",
    "Cobrança",
    "cliente_cobranca",
    "cobranca",
]

CLIENTE_ALIASES = ["cliente", "Cliente", "cliente_original", "razao_social_cliente", "razao_social_do_cliente"]


def normalizar_texto_empresa(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.upper().strip().split())


def _normalizar_chave_coluna(value: Any) -> str:
    text = normalizar_texto_empresa(value).lower()
    text = "".join(char if char.isalnum() else "_" for char in text)
    return "_".join(piece for piece in text.split("_") if piece)


def encontrar_coluna(df: pd.DataFrame, possible_names: list[str]) -> str | None:
    if df is None or df.empty:
        return None
    normalized_columns = {}
    for column in df.columns:
        normalized_columns[normalizar_texto_empresa(column)] = column
        normalized_columns[_normalizar_chave_coluna(column)] = column
    for name in possible_names:
        match = normalized_columns.get(normalizar_texto_empresa(name)) or normalized_columns.get(_normalizar_chave_coluna(name))
        if match:
            return match
    return None


def _first_row_value(row: Any, aliases: list[str]) -> Any:
    if hasattr(row, "to_dict"):
        row_lookup = row.to_dict()
    elif isinstance(row, dict):
        row_lookup = row
    else:
        row_lookup = None
    normalized_lookup = {}
    if row_lookup is not None:
        normalized_lookup = {_normalizar_chave_coluna(key): key for key in row_lookup.keys()}
    for alias in aliases:
        try:
            value = row[alias]
        except (KeyError, IndexError, TypeError):
            key = normalized_lookup.get(_normalizar_chave_coluna(alias))
            if not key:
                continue
            value = row_lookup[key]
        if value is not None and not pd.isna(value) and str(value).strip():
            return value
    return ""


def cobranca_from_row(row: Any) -> str:
    return str(_first_row_value(row, COBRANCA_ALIASES) or "").strip()


def cliente_from_row(row: Any) -> str:
    return str(_first_row_value(row, CLIENTE_ALIASES) or "").strip()


def cliente_equivalente_from_row(row: Any) -> str:
    cliente = cliente_from_row(row)
    return cliente or cobranca_from_row(row)


def is_ipiranga_cobranca_row(row: Any) -> bool:
    cobranca_norm = normalizar_texto_empresa(_first_row_value(row, ["razao_social_cobranca_norm", *COBRANCA_ALIASES]))
    if "IPIRANGA" in cobranca_norm:
        return True
    if cobranca_norm:
        return False
    return "IPIRANGA" in normalizar_texto_empresa(
        _first_row_value(row, ["cliente_equivalente_norm", *CLIENTE_ALIASES])
    )


def enriquecer_cliente_cobranca(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    view = df.copy()
    cobranca_column = encontrar_coluna(view, COBRANCA_ALIASES)
    cliente_column = encontrar_coluna(view, CLIENTE_ALIASES)

    cobranca = view[cobranca_column].fillna("").astype(str) if cobranca_column else pd.Series("", index=view.index)
    cliente = view[cliente_column].fillna("").astype(str) if cliente_column else pd.Series("", index=view.index)
    cliente_equivalente = cliente.where(cliente.str.strip().ne(""), cobranca)

    if "razao_social_cobranca" not in view.columns:
        view["razao_social_cobranca"] = cobranca
    if "razao_social_cobranca_norm" not in view.columns:
        view["razao_social_cobranca_norm"] = cobranca.apply(normalizar_texto_empresa)
    if "cliente_equivalente" not in view.columns:
        view["cliente_equivalente"] = cliente_equivalente
    if "cliente_equivalente_norm" not in view.columns:
        view["cliente_equivalente_norm"] = cliente_equivalente.apply(normalizar_texto_empresa)
    return view


def filtrar_registros_ipiranga_por_cobranca(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    view = enriquecer_cliente_cobranca(df)
    total_antes = len(view)
    cobranca_norm = view["razao_social_cobranca_norm"].fillna("").astype(str)
    cliente_equivalente_norm = view["cliente_equivalente_norm"].fillna("").astype(str)
    cobranca_preenchida = cobranca_norm.str.strip().ne("")
    cliente_base = view.get("cliente", pd.Series("", index=view.index)).fillna("").astype(str)
    mascara_ipiranga = cobranca_norm.str.contains("IPIRANGA", na=False)
    fallback_mask = ~cobranca_preenchida & cliente_equivalente_norm.str.contains("IPIRANGA", na=False)
    final_mask = mascara_ipiranga | fallback_mask
    filtered = view[final_mask].copy()
    razoes = (
        view.assign(razao_exibicao=view["razao_social_cobranca"].fillna("").astype(str).str.strip().replace("", "SEM RAZAO SOCIAL"))
        .groupby("razao_exibicao", dropna=False)
        .size()
        .reset_index(name="Registros")
        .rename(columns={"razao_exibicao": "Razao social da cobranca"})
        .sort_values("Registros", ascending=False)
    )
    diagnostics = {
        "total_antes": int(total_antes),
        "total_depois": int(len(filtered)),
        "removidos": int(total_antes - len(filtered)),
        "cliente_vazio": int(cliente_base.str.strip().eq("").sum()),
        "cobranca_preenchida": int(cobranca_preenchida.sum()),
        "cobranca_ipiranga": int(mascara_ipiranga.sum()),
        "resumo_razao_social": razoes,
    }
    return filtered, diagnostics


def aplicar_filtro_visual_razao_social(
    df: pd.DataFrame,
    selected: str,
    all_label: str = "Todas Ipiranga",
) -> pd.DataFrame:
    view = enriquecer_cliente_cobranca(df)
    if not selected or selected == all_label:
        return view
    selected_norm = normalizar_texto_empresa(selected)
    return view[view["razao_social_cobranca_norm"].fillna("").astype(str).eq(selected_norm)].copy()


def opcoes_razao_social_ipiranga(df: pd.DataFrame) -> list[str]:
    view = enriquecer_cliente_cobranca(df)
    if view.empty:
        return []
    mask = view["razao_social_cobranca_norm"].fillna("").astype(str).str.contains("IPIRANGA", na=False)
    values = sorted(
        value
        for value in view.loc[mask, "razao_social_cobranca"].fillna("").astype(str).str.strip().unique()
        if value
    )
    return values
