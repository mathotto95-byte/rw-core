from __future__ import annotations

from io import BytesIO
from typing import BinaryIO

import pandas as pd

from rw_core.normalizers.fields import make_unique_columns, normalize_column_name

# Mantem os nomes antigos das constantes por compatibilidade, mas o layout
# revisado do Portal IPP usa AK como "Valor do Frete_1".
PORTAL_IPP_VALOR_AW_POSITION = 36
PORTAL_IPP_VALOR_AW_LETTER = "AK"
PORTAL_IPP_AW_INTERNAL_COLUMN = "__portal_ipp_aw_value"
PORTAL_IPP_AW_HEADER_INTERNAL_COLUMN = "__portal_ipp_aw_header"
PORTAL_IPP_AW_INDEX_INTERNAL_COLUMN = "__portal_ipp_aw_index"
PORTAL_IPP_COLUMN_COUNT_INTERNAL_COLUMN = "__portal_ipp_column_count"
PORTAL_IPP_POSITIONAL_COLUMNS = {
    "numero_original": ("__portal_ipp_col_b_numero", 1),
    "cnpj_emitente": ("__portal_ipp_col_e_cnpj_emitente", 4),
    "data_emissao_portal": ("__portal_ipp_col_g_data_emissao", 6),
    "cnpj_destinatario": ("__portal_ipp_col_h_cnpj_destinatario", 7),
    "complemento_frete": ("__portal_ipp_col_n_complemento_frete", 13),
    "nome_produto_portal": ("__portal_ipp_col_ah_nome", 33),
    "qtd": ("__portal_ipp_col_ai_quantidade", 34),
    "valor_unitario_frete": ("__portal_ipp_col_aj_valor_unitario_frete", 35),
    "valor_portal_frete": ("__portal_ipp_col_ak_valor_frete_1", 36),
}


def _bytes(file: BinaryIO) -> bytes:
    file.seek(0)
    return file.read()


def list_excel_sheets(file: BinaryIO) -> list[str]:
    content = _bytes(file)
    with pd.ExcelFile(BytesIO(content)) as excel:
        return excel.sheet_names


def read_excel_sheet(file: BinaryIO, sheet_name: str) -> pd.DataFrame:
    content = _bytes(file)
    df = pd.read_excel(BytesIO(content), sheet_name=sheet_name, dtype=object)
    df = df.dropna(how="all")
    df.columns = make_unique_columns(list(df.columns))
    return df.reset_index(drop=True)


def _portal_sheet_has_number(content: bytes, sheet_name: str) -> bool:
    try:
        sample = pd.read_excel(BytesIO(content), sheet_name=sheet_name, dtype=object, nrows=8, header=None)
    except Exception:
        return False
    for _, row in sample.iterrows():
        names = {normalize_column_name(value) for value in row.tolist()}
        if "numero" in names:
            return True
    return False


def _portal_header_row(raw: pd.DataFrame) -> int:
    limit = min(len(raw), 12)
    for index in range(limit):
        names = [normalize_column_name(value) for value in raw.iloc[index].tolist()]
        if "numero" in names:
            return index
    return 0


def obter_coluna_valor_portal_aw(df: pd.DataFrame) -> tuple[pd.Series | None, str | None, str]:
    if df is None or df.empty:
        return None, None, "DATAFRAME_PORTAL_VAZIO"
    if len(df.columns) >= PORTAL_IPP_VALOR_AW_POSITION + 1:
        nome_coluna_aw = str(df.columns[PORTAL_IPP_VALOR_AW_POSITION])
        return df.iloc[:, PORTAL_IPP_VALOR_AW_POSITION], nome_coluna_aw, PORTAL_IPP_VALOR_AW_LETTER

    possiveis_nomes = [
        "Valor Portal",
        "Valor do Portal",
        "Valor Frete",
        "Valor do Frete",
        "Valor do Frete_1",
        "Valor Total",
        "Valor Total Frete",
        "Valor Programado",
        "Valor Pagamento",
        "Total",
    ]
    normalized_lookup = {normalize_column_name(column): column for column in df.columns}
    for nome in possiveis_nomes:
        normalized = normalize_column_name(nome)
        if normalized in normalized_lookup:
            column = normalized_lookup[normalized]
            return df[column], str(column), str(column)
    return None, None, "COLUNA_AW_NAO_ENCONTRADA"


def adicionar_colunas_posicionais_portal_ipp(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    enriched = df.copy()
    for column_name, index in PORTAL_IPP_POSITIONAL_COLUMNS.values():
        if column_name not in enriched.columns and index < len(enriched.columns):
            enriched[column_name] = enriched.iloc[:, index]
    return enriched


def read_portal_ipp_sheet(file: BinaryIO, sheet_name: str | None = None) -> tuple[pd.DataFrame, str, dict[str, object]]:
    content = _bytes(file)
    with pd.ExcelFile(BytesIO(content)) as excel:
        sheets = excel.sheet_names
    candidates = []
    if sheet_name and sheet_name in sheets:
        candidates.append(sheet_name)
    candidates.extend([sheet for sheet in sheets if sheet.upper().strip() == "IPP" and sheet not in candidates])
    candidates.extend([sheet for sheet in sheets if sheet not in candidates])

    chosen = candidates[0] if candidates else (sheet_name or 0)
    for candidate in candidates:
        if _portal_sheet_has_number(content, candidate):
            chosen = candidate
            break

    raw = pd.read_excel(BytesIO(content), sheet_name=chosen, dtype=object, header=None)
    raw = raw.dropna(how="all").reset_index(drop=True)
    if raw.empty:
        return pd.DataFrame(), str(chosen), {"header_row": 0, "numero_encontrado": False, "columns": []}

    header_index = _portal_header_row(raw)
    header_values = raw.iloc[header_index].tolist()
    columns = make_unique_columns(header_values)
    df = raw.iloc[header_index + 1:].copy()
    df.columns = columns
    df = df.dropna(how="all").reset_index(drop=True)
    df = adicionar_colunas_posicionais_portal_ipp(df)
    serie_aw, nome_coluna_aw, origem_valor_portal = obter_coluna_valor_portal_aw(df)
    if origem_valor_portal == PORTAL_IPP_VALOR_AW_LETTER and len(header_values) >= PORTAL_IPP_VALOR_AW_POSITION + 1:
        nome_coluna_aw = str(header_values[PORTAL_IPP_VALOR_AW_POSITION])
    if serie_aw is not None:
        df[PORTAL_IPP_AW_INTERNAL_COLUMN] = serie_aw.reset_index(drop=True)
        df[PORTAL_IPP_AW_HEADER_INTERNAL_COLUMN] = nome_coluna_aw or ""
        df[PORTAL_IPP_AW_INDEX_INTERNAL_COLUMN] = PORTAL_IPP_VALOR_AW_POSITION if origem_valor_portal == PORTAL_IPP_VALOR_AW_LETTER else ""
    df[PORTAL_IPP_COLUMN_COUNT_INTERNAL_COLUMN] = len(columns)
    diagnostic = {
        "header_row": header_index + 1,
        "numero_encontrado": "numero" in columns,
        "columns": columns,
        "quantidade_colunas": len(columns),
        "valor_portal_aw_encontrado": origem_valor_portal == PORTAL_IPP_VALOR_AW_LETTER,
        "valor_portal_aw_nome_coluna": nome_coluna_aw or "",
        "valor_portal_aw_indice": PORTAL_IPP_VALOR_AW_POSITION if origem_valor_portal == PORTAL_IPP_VALOR_AW_LETTER else "",
        "valor_portal_origem": origem_valor_portal,
        "sheet": chosen,
    }
    return df, str(chosen), diagnostic
