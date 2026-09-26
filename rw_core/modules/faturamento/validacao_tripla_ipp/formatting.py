from __future__ import annotations

import pandas as pd


MONEY_COLUMN_HINTS = ("valor", "frete", "tarifa", "pedagio", "icms", "diferenca", "bitrem", "rodotrem")
MONEY_COLUMN_SKIP_HINTS = ("indice", "coluna", "quantidade_colunas")
MONEY_COLUMN_AS_NUMBER_HINTS = ("base valor esperado",)
NUMBER_COLUMN_SKIP_HINTS = ("cnpj", "cpf", "nf", "cte", "codigo", "cod.", "coluna", "lote", "arquivo")
DATE_COLUMN_HINTS = ("data", "date")
CNPJ_COLUMN_HINTS = ("cnpj", "cpf")


def _format_real_br(value: object) -> str:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(number):
        return ""
    formatted = f"{float(number):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def _format_number_br(value: object) -> str:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(number):
        return ""
    number = float(number)
    decimals = 0 if abs(number - round(number)) < 0.000001 else 2
    formatted = f"{number:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return formatted


def _parse_datetime_series(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str).str.strip()
    iso_mask = text.str.match(r"^\d{4}-\d{2}-\d{2}")
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    if iso_mask.any():
        parsed.loc[iso_mask] = pd.to_datetime(series.loc[iso_mask], errors="coerce", dayfirst=False)
    non_iso_mask = ~iso_mask
    if non_iso_mask.any():
        parsed.loc[non_iso_mask] = pd.to_datetime(series.loc[non_iso_mask], errors="coerce", dayfirst=True)
    return parsed


def _parse_datetime_value(value: object) -> pd.Timestamp:
    return _parse_datetime_series(pd.Series([value])).iloc[0]


def _is_identifier_number_column(normalized: str) -> bool:
    if any(hint in normalized for hint in NUMBER_COLUMN_SKIP_HINTS):
        return True
    return normalized == "id" or normalized.startswith("id ") or normalized.endswith(" id") or normalized.endswith("_id")


def _format_date_br(value: object) -> str:
    parsed = _parse_datetime_value(value)
    if pd.isna(parsed):
        return "" if str(value or "").strip().upper() in {"", "NONE", "NAN", "<NA>", "NAT"} else str(value)
    if parsed.hour or parsed.minute or parsed.second:
        return parsed.strftime("%d/%m/%Y %H:%M")
    return parsed.strftime("%d/%m/%Y")


def _format_date_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    view = df.copy()
    for column in view.columns:
        normalized = str(column).lower()
        if not any(hint in normalized for hint in DATE_COLUMN_HINTS):
            continue
        parsed = _parse_datetime_series(view[column])
        if parsed.notna().sum() == 0:
            continue
        view[column] = view[column].map(_format_date_br)
    return view


def _format_report_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    view = _format_date_columns(df)
    for column in view.columns:
        normalized = str(column).lower()
        if any(hint in normalized for hint in MONEY_COLUMN_SKIP_HINTS):
            continue
        numeric = pd.to_numeric(view[column], errors="coerce")
        if numeric.notna().sum() == 0:
            continue
        if any(hint in normalized for hint in MONEY_COLUMN_HINTS) and not any(hint in normalized for hint in MONEY_COLUMN_AS_NUMBER_HINTS):
            view[column] = view[column].map(_format_real_br)
        elif not _is_identifier_number_column(normalized):
            view[column] = view[column].map(_format_number_br)
    return view


def _format_money_columns(df: pd.DataFrame) -> pd.DataFrame:
    return _format_report_columns(df)


def _format_export_columns(df: pd.DataFrame) -> pd.DataFrame:
    return _format_report_columns(df)


def _format_csv_export_columns(df: pd.DataFrame) -> pd.DataFrame:
    view = _format_export_columns(df)
    if view is None or view.empty:
        return view
    view = view.copy()
    for column in view.columns:
        normalized = str(column).lower()
        if not any(hint in normalized for hint in CNPJ_COLUMN_HINTS):
            continue
        values = view[column].fillna("").astype(str).str.strip()
        view[column] = values.map(lambda value: f'="{value.replace(chr(34), chr(34) + chr(34))}"' if value else "")
    return view
