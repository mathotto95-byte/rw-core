from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from io import BytesIO
from typing import Any, BinaryIO

import pandas as pd

from rw_core.database.connection import get_connection, read_sql
from rw_core.database.migrations import initialize_modular_database, import_log_payload
from rw_core.database.schema import ensure_columns
from rw_core.normalizers.fields import (
    hash_dict,
    make_unique_columns,
    normalize_cnpj,
    normalize_column_name,
    normalize_document_number,
    normalize_location_key,
    normalize_plate,
    normalize_text,
    normalizar_produto_kmm_coupa,
    parse_excel_date,
    parse_number,
    safe_json,
    split_nf_list,
)
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso


COUPA_IMPORT_TYPE = "COUPA"
LOG_TABLE = "mod_faturamento_coupa_logs_importacao"

LCTE_ORIGINAL_TABLE = "mod_faturamento_coupa_lcte_original"
LCTE_NORMALIZED_TABLE = "mod_faturamento_coupa_lcte_normalizada"
LCTE_NOTES_TABLE = "mod_faturamento_coupa_lcte_notas_normalizadas"
COUPA_ORIGINAL_TABLE = "mod_faturamento_coupa_contrato_original"
COUPA_NORMALIZED_TABLE = "mod_faturamento_coupa_contrato_normalizada"
MAPPING_ORIGINAL_TABLE = "mod_faturamento_coupa_mapeamento_original"
MAPPING_NORMALIZED_TABLE = "mod_faturamento_coupa_mapeamento_normalizada"
MAPPING_PENDING_TABLE = "mod_faturamento_coupa_mapeamento_pendencias"
MAPPING_HISTORY_TABLE = "mod_faturamento_coupa_mapeamento_historico"
MAPPING_SIMPLE_LOG_TABLE = "mod_faturamento_coupa_mapeamento_logs"
DIAGNOSTIC_TABLE = "mod_faturamento_coupa_diagnostico_lcte_mapeamento_coupa"
IMPORT_SCOPE_TRIPLA_IPP = "VALIDACAO_TRIPLA_IPP"
IMPORT_SCOPE_FATURAMENTO_COUPA = "FATURAMENTO_COUPA"
IMPORT_SCOPE_COLUMN = "escopo_importacao"

VALID_LINK_TYPES = {
    "CNPJ",
    "EXCECAO_PRODUTO",
    "EXCECAO_ROTA",
    "RAZAO_SOCIAL",
    "ORIGEM_DESTINO",
    "PRODUTO",
    "ROTA_PRODUTO",
    "CNPJ_ROTA_PRODUTO",
    "NOMENCLATURA_COUPA",
    "MANUAL",
}

LINK_PRIORITY = {
    "EXCECAO_ROTA": 0,
    "EXCECAO_PRODUTO": 0,
    "CNPJ_ROTA_PRODUTO": 1,
    "ROTA_PRODUTO": 2,
    "ORIGEM_DESTINO": 3,
    "CNPJ": 4,
    "RAZAO_SOCIAL": 5,
    "NOMENCLATURA_COUPA": 6,
    "MANUAL": 7,
}

LCTE_ALIASES = {
    "cte": ["cte", "ct e", "numero cte", "numero ct e", "conhecimento", "n conhec", "no conhec", "n conhec.", "nº conhec", "nº conhec.", "ctrc", "numero"],
    "nota_fiscal": ["nf", "nota fiscal", "numero nf", "nfe", "notas fiscais"],
    "chave_nfe": ["chave nfe", "chave nf e", "chave da nfe", "chave acesso nfe"],
    "data_emissao_cte": ["data emissao cte", "data emissao", "data cte", "emissao cte", "data da emissao"],
    "data_emissao_nf": ["data emissao nf", "data nf", "emissao nf"],
    "hora_emissao_cte": ["hora emissao cte", "hora cte", "hora emissao"],
    "hora_emissao_nf": ["hora emissao nf", "hora nf"],
    "razao_social_cobranca": ["razao social cobranca", "razao social da cobranca", "cliente cobranca", "pagador", "tomador", "razao social"],
    "cnpj_cobranca": ["cnpj cobranca", "cnpj pagador", "cnpj tomador", "cnpj cliente"],
    "remetente": ["remetente", "razao social remetente"],
    "cnpj_remetente": ["cnpj cpf do remetente", "cnpj/cpf do remetente", "cnpj remetente", "cnpj/cpf remetente", "cpf/cnpj remetente"],
    "destinatario": ["destinatario", "razao social destinatario"],
    "cnpj_destinatario": ["cnpj cpf do destinatario", "cnpj/cpf do destinatario", "cnpj destinatario", "cnpj destinatário", "cnpj/cpf destinatario", "cnpj/cpf destinatário", "cpf/cnpj destinatario", "cpf/cnpj destinatário"],
    "local_coleta": ["local coleta", "local da coleta", "municipio remetente origem", "municipio do remetente origem"],
    "local_entrega": ["local entrega", "local da entrega", "municipio destinatario destino", "municipio do destinatario destino"],
    "origem": ["origem", "cidade origem", "municipio origem", "municipio do remetente origem", "local coleta", "local da coleta"],
    "destino": ["destino", "cidade destino", "municipio destino", "municipio do destinatario destino", "local entrega", "local da entrega"],
    "produto": ["produto", "mercadoria", "material"],
    "volume": ["volume", "volume lcte", "qtd volume", "qtde", "quantidade", "qcom", "volumes litros"],
    "volume_litros": ["volume litros", "volumes litros", "litros", "volume l", "volume lcte", "volume"],
    "valor_frete": ["valor frete", "frete", "valor do frete", "total do conhec", "total do conhecimento", "total conhec"],
    "valor_cte": ["valor cte", "valor ct e"],
    "valor_total_cte": ["valor total cte", "valor total", "total cte", "total do conhec", "total do conhecimento", "total conhec"],
    "peso_frete": ["peso frete", "peso"],
    "frete_unitario": ["frete unitario", "valor unitario"],
    "pedagio": ["pedagio", "valor pedagio"],
    "impostos": ["impostos", "icms"],
    "placa": ["placa", "placa veiculo", "veiculo"],
    "motorista": ["motorista", "nome motorista"],
    "operacao": ["operacao", "tipo operacao"],
    "tabela_frete": ["tabela frete", "tabela de frete"],
    "complemento_original": ["complemento", "cte complementar", "tipo complemento"],
    "observacao": ["observacao", "obs", "comentario"],
}

COUPA_ALIASES = {
    "arquivo_coupa": ["arquivo", "arquivo coupa", "nome arquivo"],
    "codigo_coupa": ["codigo", "codigo coupa", "id coupa"],
    "item_coupa": ["item"],
    "contrato_coupa": ["contrato", "contrato coupa", "numero contrato"],
    "nomenclatura_coupa": ["nomenclatura", "descricao", "descrição", "nome", "item descricao"],
    "origem_coupa": ["origem", "origem coupa", "remetente coupa", "local origem", "cidade origem"],
    "destino_coupa": ["destino", "destino coupa", "destinatario coupa", "local destino", "cidade destino"],
    "produto_coupa": ["produto", "produto coupa", "material", "descricao produto", "descrição produto"],
    "cnpj_coupa": ["cnpj", "cnpj coupa", "cnpj cliente"],
    "cnpj_origem_coupa": ["cnpj origem", "cnpj/cpf origem", "cnpj origem coupa", "cnpj remetente coupa"],
    "cnpj_destino_coupa": ["cnpj destino", "cnpj/cpf destino", "cnpj destino coupa", "cnpj destinatario coupa"],
    "cliente_coupa": ["cliente", "cliente coupa", "razao social"],
    "vigencia_inicio": ["dt inicio", "dt início", "data inicio", "data início", "inicio", "início", "vigencia inicial", "vigência inicial", "vigencia inicio"],
    "vigencia_fim": ["dt termino", "dt término", "data termino", "data término", "fim", "vigencia final", "vigência final"],
    "data_base": ["data base"],
    "data_referencia": ["data referencia", "mes referencia"],
    "tarifa_coupa": ["tarifa", "valor tarifa", "preco", "valor unitario"],
    "valor_bitrem": ["bitrem", "bitrem 40 a 49m3", "bitrem 40 a 49 m3", "bitrem (40 a 49m3)", "bitrem (40 a 49m³)"],
    "valor_rodotrem": ["rodotrem", "rodotrem maior 50m3", "rodotrem maior 50 m3", "rodotrem (maior 50m3)", "rodotrem (maior 50m³)"],
    "unidade_tarifa_coupa": ["unidade tarifa", "unidade", "base calculo"],
    "volume_contratado": ["qtd", "qtde", "quantidade", "volume contratado", "qtd contratada", "qtde contratada", "quantidade contratada", "vol", "vol.", "volume", "qty", "quantity"],
    "volume_minimo": ["volume minimo", "minimo"],
    "volume_maximo": ["volume maximo", "maximo"],
    "valor_contratado": ["valor contratado", "valor", "valor acordo"],
    "valor_total_coupa": ["valor total", "total"],
    "status_coupa": ["status", "resposta transportador"],
    "observacao_coupa": ["observacao", "obs"],
}
COUPA_ALIASES["produto_coupa"].extend(["tipo produto"])
COUPA_ALIASES["vigencia_fim"].extend(["termino"])

MAPPING_ALIASES = {
    "descricao_coupa": ["descricao coupa", "descrição coupa", "descricao", "descrição", "nomenclatura coupa", "nome coupa", "coupa"],
    "cnpj": ["cnpj", "cnpj lcte", "cnpj cobranca", "cnpj cobrança", "cnpj faturamento", "cnpj cliente"],
    "ativo": ["ativo"],
    "cnpj_origem": ["cnpj origem", "cnpj remetente", "cnpj origem lcte", "cnpj origem usado", "cnpj origem baixa"],
    "cnpj_destino": ["cnpj destino", "cnpj destinatario", "cnpj destinatário", "cnpj destino lcte", "cnpj destino usado", "cnpj destino baixa"],
    "cnpj_lcte": ["cnpj lcte", "cnpj", "cnpj cobranca"],
    "razao_social_lcte": ["razao social lcte", "razao social", "cliente lcte"],
    "origem_lcte": ["origem lcte", "origem"],
    "destino_lcte": ["destino lcte", "destino"],
    "produto_lcte": ["produto lcte", "produto"],
    "operacao_lcte": ["operacao lcte", "operacao"],
    "tabela_frete_lcte": ["tabela frete lcte", "tabela frete"],
    "codigo_coupa": ["codigo coupa", "codigo"],
    "contrato_coupa": ["contrato coupa", "contrato"],
    "nomenclatura_coupa": ["nomenclatura coupa", "nomenclatura", "descricao coupa"],
    "origem_coupa": ["origem coupa"],
    "destino_coupa": ["destino coupa"],
    "produto_coupa": ["produto coupa", "produto excecao", "produto exceção", "produto"],
    "cliente_coupa": ["cliente coupa"],
    "tipo_vinculo": ["tipo vinculo", "tipo de vinculo", "vinculo"],
    "prioridade": ["prioridade"],
    "vigencia_inicio": ["vigencia inicio", "vigencia inicial", "data inicio"],
    "vigencia_fim": ["vigencia fim", "vigencia final", "data fim"],
    "observacao": ["observacao", "obs"],
}


def _bytes(file: BinaryIO) -> bytes:
    file.seek(0)
    return file.read()


def _read_spreadsheet(content: bytes, filename: str, preferred_sheet: str | list[str] | None = None) -> pd.DataFrame:
    if filename.lower().endswith(".csv"):
        df = pd.read_csv(BytesIO(content), dtype=object, sep=None, engine="python")
    else:
        sheet_name = 0
        if preferred_sheet:
            with pd.ExcelFile(BytesIO(content)) as excel:
                normalized_sheets = {normalize_text(sheet): sheet for sheet in excel.sheet_names}
                preferred = preferred_sheet if isinstance(preferred_sheet, list) else [preferred_sheet]
                sheet_name = excel.sheet_names[0]
                for option in preferred:
                    if normalize_text(option) in normalized_sheets:
                        sheet_name = normalized_sheets[normalize_text(option)]
                        break
        df = pd.read_excel(BytesIO(content), dtype=object, sheet_name=sheet_name)
    df = df.dropna(how="all")
    df.columns = make_unique_columns(list(df.columns))
    return df.reset_index(drop=True)


def _required_groups(required: list[str] | list[list[str]]) -> list[list[str]]:
    if not required:
        return [[]]
    if all(isinstance(field, str) for field in required):
        return [required]  # type: ignore[list-item]
    return required  # type: ignore[return-value]


def _sheet_score(df: pd.DataFrame, aliases: dict[str, list[str]], required: list[str] | list[list[str]], optional: list[str] | None = None) -> int:
    columns = set(df.columns)
    best_score = -1
    for group in _required_groups(required):
        if not all(_has_alias(columns, aliases, field) for field in group):
            continue
        score = sum(3 for field in group if _has_alias(columns, aliases, field))
        score += sum(1 for field in (optional or []) if _has_alias(columns, aliases, field))
        best_score = max(best_score, score)
    return best_score


def _sheet_candidates(content: bytes, sheet: str) -> list[pd.DataFrame]:
    candidates: list[pd.DataFrame] = []
    try:
        df = pd.read_excel(BytesIO(content), dtype=object, sheet_name=sheet).dropna(how="all")
        if not df.empty:
            df.columns = make_unique_columns(list(df.columns))
            candidates.append(df.reset_index(drop=True))
    except Exception:
        pass

    try:
        raw = pd.read_excel(BytesIO(content), dtype=object, sheet_name=sheet, header=None).dropna(how="all")
    except Exception:
        return candidates
    if raw.empty:
        return candidates

    max_header_rows = min(len(raw), 20)
    for header_index in range(max_header_rows):
        header_values = raw.iloc[header_index].tolist()
        if all(pd.isna(value) or str(value).strip() == "" for value in header_values):
            continue
        body = raw.iloc[header_index + 1 :].copy().dropna(how="all")
        if body.empty:
            continue
        body.columns = make_unique_columns(header_values)
        candidates.append(body.reset_index(drop=True))
    return candidates


def _read_best_sheet(
    content: bytes,
    filename: str,
    aliases: dict[str, list[str]],
    required: list[str] | list[list[str]],
    optional: list[str] | None = None,
    preferred_sheet: str | list[str] | None = None,
) -> pd.DataFrame:
    if filename.lower().endswith(".csv"):
        return _read_spreadsheet(content, filename)
    best_df = pd.DataFrame()
    best_sheet = ""
    best_score = -1
    diagnostic_sheet = ""
    diagnostic_columns: list[str] = []
    with pd.ExcelFile(BytesIO(content)) as excel:
        normalized_sheets = {normalize_text(sheet): sheet for sheet in excel.sheet_names}
        preferred = preferred_sheet if isinstance(preferred_sheet, list) else ([preferred_sheet] if preferred_sheet else [])
        ordered_sheets = [normalized_sheets[normalize_text(sheet)] for sheet in preferred if normalize_text(sheet) in normalized_sheets]
        ordered_sheets.extend(sheet for sheet in excel.sheet_names if sheet not in ordered_sheets)
        for sheet in ordered_sheets:
            for df in _sheet_candidates(content, sheet):
                if df.empty:
                    continue
                score = _sheet_score(df, aliases, required, optional)
                if not diagnostic_columns:
                    diagnostic_sheet = sheet
                    diagnostic_columns = list(df.columns)
                elif score > best_score:
                    diagnostic_sheet = sheet
                    diagnostic_columns = list(df.columns)
                if score > best_score:
                    best_score = score
                    best_df = df.reset_index(drop=True)
                    best_sheet = sheet
    minimum = 0
    if best_score < minimum:
        found = list(best_df.columns) if not best_df.empty else diagnostic_columns
        raise ValueError(
            f"Layout invalido em {filename}. Nao encontrei uma aba com as colunas obrigatorias. "
            f"Aba analisada: {best_sheet or diagnostic_sheet or 'nenhuma'} | Colunas encontradas: {found}"
        )
    best_df["aba_origem_importacao"] = best_sheet
    return best_df


def _read_lcte_spreadsheet(content: bytes, filename: str) -> pd.DataFrame:
    return _read_best_sheet(
        content,
        filename,
        LCTE_ALIASES,
        required=[["cte", "nota_fiscal"], ["razao_social_cobranca", "origem", "destino", "produto", "volume"]],
        optional=["data_emissao_cte", "data_emissao_nf", "cnpj_remetente", "cnpj_destinatario", "valor_frete"],
        preferred_sheet=["Dados", "LCTE", "Base"],
    )


def _read_mapping_spreadsheet(content: bytes, filename: str) -> pd.DataFrame:
    return _read_best_sheet(
        content,
        filename,
        MAPPING_ALIASES,
        required=["descricao_coupa", "cnpj"],
        optional=["ativo", "tipo_vinculo", "origem_lcte", "destino_lcte", "produto_lcte"],
        preferred_sheet=["Base", "Pendencias", "Mapeamento"],
    )


def _read_mapping_exception_spreadsheet(content: bytes, filename: str) -> pd.DataFrame:
    return _read_best_sheet(
        content,
        filename,
        MAPPING_ALIASES,
        required=[
            ["descricao_coupa", "produto_coupa", "cnpj"],
            ["origem_coupa", "destino_coupa", "produto_coupa", "cnpj_origem", "cnpj_destino"],
        ],
        optional=["ativo", "observacao", "descricao_coupa", "cnpj", "origem_coupa", "destino_coupa", "cnpj_origem", "cnpj_destino"],
        preferred_sheet=["Excecoes", "Exceções", "Base", "Mapeamento"],
    )


def _has_alias(columns: set[str], aliases: dict[str, list[str]], field: str) -> bool:
    return any(normalize_column_name(alias) in columns for alias in aliases.get(field, []))


def _read_coupa_spreadsheet(content: bytes, filename: str) -> pd.DataFrame:
    valid_sheets = ["COUPA", "MESES", "MESES (2)"]
    if filename.lower().endswith(".csv"):
        df = _read_spreadsheet(content, filename)
        df["aba_origem_coupa"] = ""
        return df
    frames = []
    with pd.ExcelFile(BytesIO(content)) as excel:
        normalized_sheets = {normalize_text(sheet): sheet for sheet in excel.sheet_names}
        candidate_sheets = [normalized_sheets[normalize_text(sheet)] for sheet in valid_sheets if normalize_text(sheet) in normalized_sheets]
        candidate_sheets.extend(sheet for sheet in excel.sheet_names if sheet not in candidate_sheets)
        for sheet in candidate_sheets:
            df = pd.read_excel(BytesIO(content), dtype=object, sheet_name=sheet).dropna(how="all")
            if df.empty:
                continue
            df.columns = make_unique_columns(list(df.columns))
            columns = set(df.columns)
            required = ["item_coupa", "volume_contratado", "vigencia_inicio", "vigencia_fim", "origem_coupa", "destino_coupa", "produto_coupa"]
            if not all(_has_alias(columns, COUPA_ALIASES, field) for field in required):
                continue
            df["aba_origem_coupa"] = sheet
            frames.append(df.reset_index(drop=True))
    if frames:
        return pd.concat(frames, ignore_index=True)
    return _read_spreadsheet(content, filename, preferred_sheet=valid_sheets)


def _file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def normalizar_cnpj(valor: Any) -> str:
    return normalize_cnpj(valor)


def normalizar_cnpj_cpf(valor: Any) -> str:
    if valor is None or pd.isna(valor):
        return ""
    if isinstance(valor, float) and valor.is_integer():
        text = str(int(valor))
    else:
        text = str(valor)
    digits = re.sub(r"\D", "", text)
    if len(digits) == 13:
        return digits.zfill(14)
    return digits


def cnpj_valido(valor: Any) -> str:
    return "SIM" if len(normalizar_cnpj_cpf(valor)) in {11, 14} else "NAO"


def normalizar_cnpj_ou_descricao_coupa(valor: Any) -> str:
    raw = "" if valor is None or pd.isna(valor) else str(valor).strip()
    if re.search(r"[-:]\s*\D", raw):
        return normalizar_descricao_coupa_para_match(raw)
    cnpj = normalizar_cnpj_cpf(valor)
    if len(cnpj) in {11, 14}:
        return cnpj
    return normalizar_descricao_coupa_para_match(valor)


def normalizar_descricao_coupa(valor: Any) -> str:
    return normalizar_descricao_coupa_para_match(valor)


def normalizar_descricao_coupa_sem_prefixo(valor: Any) -> str:
    raw = "" if valor is None or pd.isna(valor) else str(valor)
    text = normalize_text(raw)
    text = re.sub(r"^\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\s*[-:]\s*", "", text)
    text = re.sub(r"^\d{3}\.?\d{3}\.?\d{3}-?\d{2}\s*[-:]\s*", "", text)
    text = re.sub(r"^(?:\d{11,14}|\d{1,6})\s*[-:]\s*", "", text)
    text = re.sub(r"^(?:\d{11,14}|\d{1,6})\s+(?=[A-Z])", "", text)
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"^ESCRITORIO\s+(DE|DO|DA|DOS|DAS)\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalizar_descricao_coupa_para_match(valor: Any) -> str:
    raw = "" if valor is None or pd.isna(valor) else str(valor)
    text = normalize_text(raw)
    text = re.sub(
        r"^(\d{2})\.?(\d{3})\.?(\d{3})/?(\d{4})-?(\d{2})\s*[-:]\s*(?=\S)",
        r"\1\2\3\4\5 ",
        text,
    )
    text = re.sub(
        r"^(\d{2})\s?(\d{3})\s?(\d{3})\s?(\d{4})\s?(\d{2})\s*[-:]\s*(?=\S)",
        r"\1\2\3\4\5 ",
        text,
    )
    text = re.sub(
        r"^(\d{2})\s+(\d{3})\s+(\d{3})\s+(\d{4})\s+(\d{2})\s+(?=[A-Z])",
        r"\1\2\3\4\5 ",
        text,
    )
    text = re.sub(
        r"^(\d{3})\s?(\d{3})\s?(\d{3})\s?(\d{2})\s*[-:]\s*(?=\S)",
        r"\1\2\3\4 ",
        text,
    )
    text = re.sub(
        r"^(\d{3})\.?(\d{3})\.?(\d{3})-?(\d{2})\s*[-:]\s*(?=\S)",
        r"\1\2\3\4 ",
        text,
    )
    text = re.sub(
        r"^(\d{3})\s+(\d{3})\s+(\d{3})\s+(\d{2})\s+(?=[A-Z])",
        r"\1\2\3\4 ",
        text,
    )
    text = re.sub(r"^\d{1,6}\s*-\s*", "", text)
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"^ESCRITORIO\s+(DE|DO|DA|DOS|DAS)\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def extrair_codigo_coupa(valor: Any) -> str:
    text = normalize_text(valor)
    match = re.search(r"TR\d+", text)
    return match.group(0) if match else ""


def parse_qtd_coupa(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return None if pd.isna(value) else float(value)
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    elif "." in text:
        pieces = text.split(".")
        if len(pieces) > 1 and all(len(piece) == 3 for piece in pieces[1:]):
            text = "".join(pieces)
    try:
        return float(text)
    except ValueError:
        return None


def normalizar_produto_grupo(valor: Any) -> str:
    text = normalize_text(valor)
    if not text:
        return "VAZIO"
    key = re.sub(r"[^A-Z0-9]+", " ", text).strip()
    if key in {"T TODOS", "T TODO", "TODOS", "TODO"}:
        return "TODOS"
    if "BIODIESEL" in text or "B100" in text:
        return "BIODIESEL"
    if ("ETANOL" in text or "ALCOOL" in text) and "ANIDRO" in text:
        return "ETANOL ANIDRO"
    if ("ETANOL" in text or "ALCOOL" in text) and "HIDRATADO" in text:
        return "ETANOL HIDRATADO"
    if "ETANOL" in text or "ALCOOL" in text:
        return "ETANOL"
    if any(token in text for token in ("DIESEL", "GASOLINA", "ARLA", "DERIVADO")):
        return "DERIVADOS"
    return text


def produto_excecao_rota(valor: Any) -> bool:
    return normalize_text(valor) in {"EXCECAO", "EXCESSAO"}


def _get(row: dict[str, Any], aliases: dict[str, list[str]], field: str) -> Any:
    for alias in aliases.get(field, []):
        column = normalize_column_name(alias)
        if column in row and not pd.isna(row[column]):
            value = row[column]
            if str(value).strip():
                return value
    return ""


def _get_with_column(row: dict[str, Any], aliases: dict[str, list[str]], field: str) -> tuple[Any, str]:
    for alias in aliases.get(field, []):
        column = normalize_column_name(alias)
        if column in row:
            value = row[column]
            if pd.isna(value) or not str(value).strip():
                return "", column
            return value, column
    return "", ""


def _metadata(kind: str, now: str) -> str:
    return f"COUPA_{kind}_{datetime.fromisoformat(now).strftime('%Y%m%d_%H%M%S')}"


def _clean_json(row: dict[str, Any]) -> str:
    return json.dumps({key: ("" if pd.isna(value) else value) for key, value in row.items()}, ensure_ascii=False, default=str)


def _insert_dict(conn, table: str, payload: dict[str, Any]) -> bool:
    columns = list(payload)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"insert or ignore into {table} ({', '.join(columns)}) values ({placeholders})"
    cursor = conn.execute(sql, tuple(payload[column] for column in columns))
    return int(getattr(cursor, "rowcount", 0) or 0) > 0


def _column_type_for_value(value: Any) -> str:
    if isinstance(value, bool):
        return "integer"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    return "text"


def _ensure_payload_columns(conn, table: str, payload: dict[str, Any]) -> None:
    columns = {
        column: _column_type_for_value(value)
        for column, value in payload.items()
        if column != "id"
    }
    if columns:
        ensure_columns(conn, table, columns)


def _update_by_hash(conn, table: str, payload: dict[str, Any]) -> bool:
    hash_registro = payload.get("hash_registro")
    if not hash_registro:
        return False
    columns = [column for column in payload if column not in {"id", "hash_registro"}]
    if not columns:
        return False
    assignments = ", ".join(f"{column} = ?" for column in columns)
    scope = str(payload.get(IMPORT_SCOPE_COLUMN) or "").strip()
    if scope:
        _ensure_payload_columns(conn, table, {IMPORT_SCOPE_COLUMN: scope})
        cursor = conn.execute(
            f"update {table} set {assignments} where hash_registro = ? and coalesce({IMPORT_SCOPE_COLUMN}, '') = ?",
            tuple(payload[column] for column in columns) + (hash_registro, scope),
        )
    else:
        cursor = conn.execute(
            f"update {table} set {assignments} where hash_registro = ?",
            tuple(payload[column] for column in columns) + (hash_registro,),
        )
    return int(getattr(cursor, "rowcount", 0) or 0) > 0


def _delete_previous_file_import(conn, kind: str, arquivo: str, escopo_importacao: str = "") -> int:
    arquivo = str(arquivo or "").strip()
    tables = {
        "LCTE": [LCTE_NOTES_TABLE, LCTE_NORMALIZED_TABLE, LCTE_ORIGINAL_TABLE],
        "CONTRATO": [COUPA_NORMALIZED_TABLE, COUPA_ORIGINAL_TABLE],
    }.get(kind, [])
    if not arquivo or not tables:
        return 0
    removed = 0
    scope = str(escopo_importacao or "").strip()
    for table in tables:
        if scope:
            _ensure_payload_columns(conn, table, {IMPORT_SCOPE_COLUMN: scope})
            cursor = conn.execute(
                f"delete from {table} where arquivo_origem = ? and coalesce({IMPORT_SCOPE_COLUMN}, '') = ?",
                (arquivo, scope),
            )
        else:
            cursor = conn.execute(f"delete from {table} where arquivo_origem = ?", (arquivo,))
        removed += int(getattr(cursor, "rowcount", 0) or 0)
    return removed


def _log_import(conn, payload: dict[str, Any]) -> None:
    data = import_log_payload(**payload)
    data["created_at"] = data["data_hora"]
    data["updated_at"] = data["data_hora"]
    _ensure_payload_columns(conn, LOG_TABLE, data)
    _insert_dict(conn, LOG_TABLE, data)


def _mapping_history(
    conn,
    *,
    now: str,
    usuario: str,
    acao: str,
    cnpj_norm: str,
    descricao_anterior: str = "",
    descricao_nova: str = "",
    arquivo_origem: str = "",
    lote_importacao: str = "",
    observacao: str = "",
    detalhes: dict[str, Any] | None = None,
) -> None:
    payload = {
        "data_hora": now,
        "usuario": usuario,
        "acao": acao,
        "descricao_coupa": descricao_nova or descricao_anterior,
        "cnpj_norm": cnpj_norm,
        "cnpj_anterior": cnpj_norm if descricao_anterior else "",
        "cnpj_novo": cnpj_norm,
        "status": acao,
        "descricao_anterior": descricao_anterior,
        "descricao_nova": descricao_nova,
        "arquivo_origem": arquivo_origem,
        "lote_importacao": lote_importacao,
        "observacao": observacao,
        "detalhes_json": json.dumps(detalhes or {}, ensure_ascii=False, default=str),
    }
    _ensure_payload_columns(conn, MAPPING_HISTORY_TABLE, payload)
    _insert_dict(conn, MAPPING_HISTORY_TABLE, payload)


def _mapping_log(conn, now: str, usuario: str, status: str, mensagem: str, detalhes: dict[str, Any]) -> None:
    payload = {
        "data_hora": now,
        "usuario": usuario,
        "acao": "IMPORTAR_MAPEAMENTO_SIMPLES",
        "status": status,
        "mensagem": mensagem,
        "detalhes_json": json.dumps(detalhes, ensure_ascii=False, default=str),
    }
    _ensure_payload_columns(conn, MAPPING_SIMPLE_LOG_TABLE, payload)
    _insert_dict(conn, MAPPING_SIMPLE_LOG_TABLE, payload)


def _resolve_pending_mapping(conn, cnpj_norm: str, descricao: str, now: str, usuario: str, lote: str) -> None:
    if not cnpj_norm:
        return
    conn.execute(
        f"""
        update {MAPPING_PENDING_TABLE}
        set status_pendencia = 'RESOLVIDO',
            resolvido = 1,
            data_hora_resolucao = ?,
            usuario_resolucao = ?,
            descricao_coupa_resolvida = ?,
            lote_importacao_resolucao = ?
        where cnpj_norm = ? and coalesce(resolvido, 0) = 0
        """,
        (now, usuario, descricao, lote, cnpj_norm),
    )


def _base_original_payload(
    row: dict[str, Any],
    *,
    lote: str,
    arquivo: str,
    usuario: str,
    now: str,
    file_hash: str,
    index: int,
    row_hash: str,
) -> dict[str, Any]:
    return {
        "lote_importacao": lote,
        "arquivo_origem": arquivo,
        "usuario_importacao": usuario,
        "data_hora_importacao": now,
        "hash_arquivo": file_hash,
        "numero_linha": index,
        "hash_registro": row_hash,
        "dados_json": _clean_json(row),
        "created_at": now,
        "updated_at": now,
    }


def _normalize_lcte(row: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    cte = _get(row, LCTE_ALIASES, "cte")
    nf = _get(row, LCTE_ALIASES, "nota_fiscal")
    razao = _get(row, LCTE_ALIASES, "razao_social_cobranca")
    origem = _get(row, LCTE_ALIASES, "origem")
    destino = _get(row, LCTE_ALIASES, "destino")
    produto = _get(row, LCTE_ALIASES, "produto")
    complemento = _get(row, LCTE_ALIASES, "complemento_original")
    observacao = _get(row, LCTE_ALIASES, "observacao")
    cnpj_cobranca_norm = normalize_cnpj(_get(row, LCTE_ALIASES, "cnpj_cobranca"))
    cnpj_remetente_original = _get(row, LCTE_ALIASES, "cnpj_remetente")
    cnpj_destinatario_original = _get(row, LCTE_ALIASES, "cnpj_destinatario")
    cnpj_remetente_norm = normalizar_cnpj_cpf(cnpj_remetente_original)
    cnpj_destinatario_norm = normalizar_cnpj_cpf(cnpj_destinatario_original)
    cnpj_lcte_para_coupa_norm = cnpj_cobranca_norm or cnpj_remetente_norm or cnpj_destinatario_norm
    origem_cnpj = "NAO_ENCONTRADO"
    if cnpj_cobranca_norm:
        origem_cnpj = "CNPJ_COBRANCA"
    elif cnpj_remetente_norm:
        origem_cnpj = "CNPJ_REMETENTE"
    elif cnpj_destinatario_norm:
        origem_cnpj = "CNPJ_DESTINATARIO"
    volume_original = _get(row, LCTE_ALIASES, "volume_litros") or _get(row, LCTE_ALIASES, "volume")
    volume_lcte = parse_number(volume_original) or 0
    return {
        **meta,
        "cte": str(cte or ""),
        "cte_norm": normalize_document_number(cte),
        "nota_fiscal": str(nf or ""),
        "nota_fiscal_norm": normalize_document_number(nf),
        "chave_nfe": str(_get(row, LCTE_ALIASES, "chave_nfe") or ""),
        "data_emissao_cte": parse_excel_date(_get(row, LCTE_ALIASES, "data_emissao_cte")),
        "data_emissao_lcte": parse_excel_date(_get(row, LCTE_ALIASES, "data_emissao_cte")),
        "data_emissao_nf": parse_excel_date(_get(row, LCTE_ALIASES, "data_emissao_nf")),
        "hora_emissao_cte": str(_get(row, LCTE_ALIASES, "hora_emissao_cte") or ""),
        "hora_emissao_nf": str(_get(row, LCTE_ALIASES, "hora_emissao_nf") or ""),
        "data_hora_cte": parse_excel_date(_get(row, LCTE_ALIASES, "data_emissao_cte")),
        "data_hora_nf": parse_excel_date(_get(row, LCTE_ALIASES, "data_emissao_nf")),
        "razao_social_cobranca": str(razao or ""),
        "razao_social_cobranca_norm": normalize_text(razao),
        "cnpj_cobranca": str(_get(row, LCTE_ALIASES, "cnpj_cobranca") or ""),
        "cnpj_cobranca_norm": cnpj_cobranca_norm,
        "cnpj_lcte_para_coupa_norm": cnpj_lcte_para_coupa_norm,
        "origem_cnpj_lcte_para_coupa": origem_cnpj,
        "remetente": str(_get(row, LCTE_ALIASES, "remetente") or ""),
        "remetente_norm": normalize_text(_get(row, LCTE_ALIASES, "remetente")),
        "cnpj_remetente": str(_get(row, LCTE_ALIASES, "cnpj_remetente") or ""),
        "cnpj_remetente_norm": cnpj_remetente_norm,
        "cnpj_cpf_remetente_original": str(cnpj_remetente_original or ""),
        "cnpj_cpf_remetente_norm": cnpj_remetente_norm,
        "destinatario": str(_get(row, LCTE_ALIASES, "destinatario") or ""),
        "destinatario_norm": normalize_text(_get(row, LCTE_ALIASES, "destinatario")),
        "cnpj_destinatario": str(_get(row, LCTE_ALIASES, "cnpj_destinatario") or ""),
        "cnpj_destinatario_norm": cnpj_destinatario_norm,
        "cnpj_cpf_destinatario_original": str(cnpj_destinatario_original or ""),
        "cnpj_cpf_destinatario_norm": cnpj_destinatario_norm,
        "local_coleta": str(_get(row, LCTE_ALIASES, "local_coleta") or ""),
        "local_coleta_norm": normalize_location_key(_get(row, LCTE_ALIASES, "local_coleta")),
        "local_entrega": str(_get(row, LCTE_ALIASES, "local_entrega") or ""),
        "local_entrega_norm": normalize_location_key(_get(row, LCTE_ALIASES, "local_entrega")),
        "origem": str(origem or ""),
        "origem_norm": normalize_location_key(origem),
        "destino": str(destino or ""),
        "destino_norm": normalize_location_key(destino),
        "produto": str(produto or ""),
        "produto_norm": normalizar_produto_kmm_coupa(produto),
        "produto_lcte": str(produto or ""),
        "produto_lcte_norm": normalizar_produto_kmm_coupa(produto),
        "produto_grupo_lcte": normalizar_produto_grupo(produto),
        "volume_lcte_original": str(volume_original or ""),
        "volume_lcte": volume_lcte,
        "volume_lcte_m3": volume_lcte / 1000 if volume_lcte else 0,
        "volume": parse_number(_get(row, LCTE_ALIASES, "volume")),
        "volume_litros": parse_number(_get(row, LCTE_ALIASES, "volume_litros")) or volume_lcte,
        "valor_frete": parse_number(_get(row, LCTE_ALIASES, "valor_frete")),
        "valor_cte": parse_number(_get(row, LCTE_ALIASES, "valor_cte")),
        "valor_total_cte": parse_number(_get(row, LCTE_ALIASES, "valor_total_cte")),
        "peso_frete": parse_number(_get(row, LCTE_ALIASES, "peso_frete")),
        "frete_unitario": parse_number(_get(row, LCTE_ALIASES, "frete_unitario")),
        "pedagio": parse_number(_get(row, LCTE_ALIASES, "pedagio")),
        "impostos": parse_number(_get(row, LCTE_ALIASES, "impostos")),
        "placa": str(_get(row, LCTE_ALIASES, "placa") or ""),
        "placa_norm": normalize_plate(_get(row, LCTE_ALIASES, "placa")),
        "motorista": str(_get(row, LCTE_ALIASES, "motorista") or ""),
        "operacao": str(_get(row, LCTE_ALIASES, "operacao") or ""),
        "tabela_frete": str(_get(row, LCTE_ALIASES, "tabela_frete") or ""),
        "complemento_original": str(complemento or ""),
        "complemento_norm": normalize_text(complemento),
        "observacao": str(observacao or ""),
        "observacao_norm": normalize_text(observacao),
        "cte_complementar_norm": normalize_document_number(complemento),
        "motivo_identificacao_complemento": "COMPLEMENTO_INFORMADO" if normalize_text(complemento) else "",
        "dados_json": meta["dados_json"],
        "created_at": meta["created_at"],
        "updated_at": meta["updated_at"],
    }


def _normalize_coupa(row: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    nomenclatura = _get(row, COUPA_ALIASES, "nomenclatura_coupa")
    arquivo, arquivo_column = _get_with_column(row, COUPA_ALIASES, "arquivo_coupa")
    codigo = extrair_codigo_coupa(arquivo) or extrair_codigo_coupa(_get(row, COUPA_ALIASES, "codigo_coupa"))
    item, item_column = _get_with_column(row, COUPA_ALIASES, "item_coupa")
    origem, origem_column = _get_with_column(row, COUPA_ALIASES, "origem_coupa")
    destino, destino_column = _get_with_column(row, COUPA_ALIASES, "destino_coupa")
    produto, produto_column = _get_with_column(row, COUPA_ALIASES, "produto_coupa")
    cliente = _get(row, COUPA_ALIASES, "cliente_coupa")
    dt_inicio_original, dt_inicio_column = _get_with_column(row, COUPA_ALIASES, "vigencia_inicio")
    dt_termino_original, dt_termino_column = _get_with_column(row, COUPA_ALIASES, "vigencia_fim")
    dt_inicio = parse_excel_date(dt_inicio_original)
    dt_termino = parse_excel_date(dt_termino_original)
    qtd_original, qtd_column = _get_with_column(row, COUPA_ALIASES, "volume_contratado")
    qtd_contratada = parse_qtd_coupa(qtd_original)
    origem_norm = normalizar_cnpj_ou_descricao_coupa(origem)
    destino_norm = normalizar_cnpj_ou_descricao_coupa(destino)
    return {
        **meta,
        "arquivo_coupa": str(arquivo or ""),
        "coluna_arquivo_origem": arquivo_column,
        "aba_origem_coupa": str(row.get("aba_origem_coupa") or ""),
        "codigo_coupa": codigo,
        "codigo_coupa_norm": codigo,
        "item_coupa": str(item or ""),
        "coluna_item_origem": item_column,
        "contrato_coupa": str(_get(row, COUPA_ALIASES, "contrato_coupa") or ""),
        "nomenclatura_coupa": str(nomenclatura or ""),
        "nomenclatura_coupa_norm": normalize_text(nomenclatura),
        "origem_coupa": str(origem or ""),
        "origem_coupa_norm": origem_norm,
        "origem_coupa_original": str(origem or ""),
        "coluna_origem_origem": origem_column,
        "destino_coupa": str(destino or ""),
        "destino_coupa_norm": destino_norm,
        "destino_coupa_original": str(destino or ""),
        "coluna_destino_origem": destino_column,
        "produto_coupa": str(produto or ""),
        "produto_coupa_norm": normalizar_produto_kmm_coupa(produto),
        "produto_grupo_coupa": normalizar_produto_grupo(produto),
        "produto_coupa_original": str(produto or ""),
        "coluna_produto_origem": produto_column,
        "cnpj_coupa": str(_get(row, COUPA_ALIASES, "cnpj_coupa") or ""),
        "cnpj_coupa_norm": normalize_cnpj(_get(row, COUPA_ALIASES, "cnpj_coupa")),
        "cliente_coupa": str(cliente or ""),
        "cliente_coupa_norm": normalize_text(cliente),
        "vigencia_inicio": dt_inicio,
        "vigencia_fim": dt_termino,
        "dt_inicio_original": str(dt_inicio_original or ""),
        "dt_inicio": dt_inicio,
        "dt_termino_original": str(dt_termino_original or ""),
        "dt_termino": dt_termino,
        "coluna_dt_inicio_origem": dt_inicio_column,
        "coluna_dt_termino_origem": dt_termino_column,
        "data_base": parse_excel_date(_get(row, COUPA_ALIASES, "data_base")),
        "data_referencia": parse_excel_date(_get(row, COUPA_ALIASES, "data_referencia")),
        "tarifa_coupa": parse_number(_get(row, COUPA_ALIASES, "tarifa_coupa")),
        "valor_bitrem": parse_number(_get(row, COUPA_ALIASES, "valor_bitrem")),
        "valor_rodotrem": parse_number(_get(row, COUPA_ALIASES, "valor_rodotrem")),
        "unidade_tarifa_coupa": str(_get(row, COUPA_ALIASES, "unidade_tarifa_coupa") or ""),
        "volume_contratado": qtd_contratada,
        "qtd_contratada": qtd_contratada,
        "qtd_contratada_original": str(qtd_original or ""),
        "coluna_qtd_origem": qtd_column,
        "volume_minimo": parse_number(_get(row, COUPA_ALIASES, "volume_minimo")),
        "volume_maximo": parse_number(_get(row, COUPA_ALIASES, "volume_maximo")),
        "valor_contratado": parse_number(_get(row, COUPA_ALIASES, "valor_contratado")),
        "valor_total_coupa": parse_number(_get(row, COUPA_ALIASES, "valor_total_coupa")),
        "status_coupa": str(_get(row, COUPA_ALIASES, "status_coupa") or ""),
        "observacao_coupa": str(_get(row, COUPA_ALIASES, "observacao_coupa") or ""),
        "dados_json": meta["dados_json"],
        "created_at": meta["created_at"],
        "updated_at": meta["updated_at"],
    }


def _active_flag(value: Any) -> int:
    text = normalize_text(value)
    if text in {"", "SIM", "S", "TRUE", "1", "ATIVO"}:
        return 1
    return 0


def _link_type(value: Any) -> str:
    text = normalize_text(value).replace(" ", "_")
    return text if text in VALID_LINK_TYPES else ("MANUAL" if not text else text)


def _normalize_mapping(row: dict[str, Any], meta: dict[str, Any], usuario: str) -> dict[str, Any]:
    descricao = _get(row, MAPPING_ALIASES, "descricao_coupa") or _get(row, MAPPING_ALIASES, "nomenclatura_coupa")
    cnpj = _get(row, MAPPING_ALIASES, "cnpj") or _get(row, MAPPING_ALIASES, "cnpj_lcte")
    descricao_norm = normalizar_descricao_coupa(descricao)
    cnpj_norm = normalizar_cnpj_cpf(cnpj)
    cnpj_is_valid = cnpj_valido(cnpj)
    tipo = _link_type(_get(row, MAPPING_ALIASES, "tipo_vinculo") or "CNPJ")
    prioridade = parse_number(_get(row, MAPPING_ALIASES, "prioridade")) or LINK_PRIORITY.get("CNPJ", 4)
    razao = _get(row, MAPPING_ALIASES, "razao_social_lcte")
    origem_lcte = _get(row, MAPPING_ALIASES, "origem_lcte")
    destino_lcte = _get(row, MAPPING_ALIASES, "destino_lcte")
    produto_lcte = _get(row, MAPPING_ALIASES, "produto_lcte")
    nome_coupa = descricao
    origem_coupa = _get(row, MAPPING_ALIASES, "origem_coupa")
    destino_coupa = _get(row, MAPPING_ALIASES, "destino_coupa")
    produto_coupa = _get(row, MAPPING_ALIASES, "produto_coupa")
    ativo = _active_flag(_get(row, MAPPING_ALIASES, "ativo"))
    vigencia_fim = parse_excel_date(_get(row, MAPPING_ALIASES, "vigencia_fim"))
    status = "MAPEAMENTO OK"
    reasons = []
    if not cnpj_norm:
        status = "CNPJ VAZIO"
        reasons.append("CNPJ nao preenchido.")
    elif cnpj_is_valid != "SIM":
        status = "CNPJ INVALIDO"
        reasons.append("CNPJ normalizado nao possui 14 digitos.")
    if not descricao_norm:
        status = "DESCRICAO COUPA VAZIA"
        reasons.append("Descricao Coupa nao preenchida.")
    if not ativo:
        status = "INATIVO"
        reasons.append("Mapeamento inativo.")
    if vigencia_fim:
        try:
            if datetime.fromisoformat(vigencia_fim).date() < brasilia_now().date():
                status = "VIGENCIA VENCIDA"
                reasons.append("Vigencia final menor que a data atual.")
        except ValueError:
            pass
    return {
        **meta,
        "hash_linha": meta["hash_registro"],
        "ativo": ativo,
        "descricao_coupa_original": str(descricao or ""),
        "descricao_coupa_norm": descricao_norm,
        "cnpj_original": str(cnpj or ""),
        "cnpj_norm": cnpj_norm,
        "cnpj_mapeamento_norm": cnpj_norm,
        "cnpj_valido": cnpj_is_valid,
        "cnpj_lcte": str(cnpj or ""),
        "cnpj_lcte_norm": cnpj_norm,
        "razao_social_lcte": str(razao or ""),
        "razao_social_lcte_norm": normalize_text(razao),
        "origem_lcte": str(origem_lcte or ""),
        "origem_lcte_norm": normalize_location_key(origem_lcte),
        "destino_lcte": str(destino_lcte or ""),
        "destino_lcte_norm": normalize_location_key(destino_lcte),
        "produto_lcte": str(produto_lcte or ""),
        "produto_lcte_norm": normalizar_produto_kmm_coupa(produto_lcte),
        "operacao_lcte": str(_get(row, MAPPING_ALIASES, "operacao_lcte") or ""),
        "operacao_lcte_norm": normalize_text(_get(row, MAPPING_ALIASES, "operacao_lcte")),
        "tabela_frete_lcte": str(_get(row, MAPPING_ALIASES, "tabela_frete_lcte") or ""),
        "tabela_frete_lcte_norm": normalize_text(_get(row, MAPPING_ALIASES, "tabela_frete_lcte")),
        "codigo_coupa": str(_get(row, MAPPING_ALIASES, "codigo_coupa") or ""),
        "contrato_coupa": str(_get(row, MAPPING_ALIASES, "contrato_coupa") or ""),
        "nomenclatura_coupa": str(nome_coupa or ""),
        "nomenclatura_coupa_norm": descricao_norm,
        "origem_coupa": str(origem_coupa or ""),
        "origem_coupa_norm": normalizar_descricao_coupa_para_match(origem_coupa),
        "destino_coupa": str(destino_coupa or ""),
        "destino_coupa_norm": normalizar_descricao_coupa_para_match(destino_coupa),
        "produto_coupa": str(produto_coupa or ""),
        "produto_coupa_norm": normalizar_produto_kmm_coupa(produto_coupa),
        "cliente_coupa": str(_get(row, MAPPING_ALIASES, "cliente_coupa") or ""),
        "cliente_coupa_norm": normalize_text(_get(row, MAPPING_ALIASES, "cliente_coupa")),
        "tipo_vinculo": tipo,
        "prioridade": int(prioridade or LINK_PRIORITY.get(tipo, 999)),
        "vigencia_inicio": parse_excel_date(_get(row, MAPPING_ALIASES, "vigencia_inicio")),
        "vigencia_fim": vigencia_fim,
        "observacao": str(_get(row, MAPPING_ALIASES, "observacao") or ""),
        "status_mapeamento": status,
        "diagnostico_json": json.dumps({"motivos": reasons}, ensure_ascii=False),
        "created_by": usuario,
        "created_at": meta["created_at"],
        "updated_by": usuario,
        "updated_at": meta["updated_at"],
    }


def _mark_mapping_duplicates(rows: list[dict[str, Any]]) -> None:
    seen: dict[str, int] = {}
    for row in rows:
        key = hash_dict(
            {
                "cnpj": row.get("cnpj_lcte_norm"),
                "origem": row.get("origem_lcte_norm"),
                "destino": row.get("destino_lcte_norm"),
                "produto": row.get("produto_lcte_norm"),
                "nomenclatura": row.get("nomenclatura_coupa_norm"),
                "tipo": row.get("tipo_vinculo"),
                "inicio": row.get("vigencia_inicio"),
                "fim": row.get("vigencia_fim"),
            }
        )
        seen[key] = seen.get(key, 0) + 1
    for row in rows:
        key = hash_dict(
            {
                "cnpj": row.get("cnpj_lcte_norm"),
                "origem": row.get("origem_lcte_norm"),
                "destino": row.get("destino_lcte_norm"),
                "produto": row.get("produto_lcte_norm"),
                "nomenclatura": row.get("nomenclatura_coupa_norm"),
                "tipo": row.get("tipo_vinculo"),
                "inicio": row.get("vigencia_inicio"),
                "fim": row.get("vigencia_fim"),
            }
        )
        if seen.get(key, 0) > 1 and row.get("status_mapeamento") == "MAPEAMENTO OK":
            row["status_mapeamento"] = "MAPEAMENTO DUPLICADO"


def _import_dataframe(
    df: pd.DataFrame,
    *,
    kind: str,
    arquivo: str,
    usuario: str,
    file_hash: str,
    replace_mapping: bool = False,
    escopo_importacao: str = IMPORT_SCOPE_FATURAMENTO_COUPA,
) -> dict[str, Any]:
    now = brasilia_now_iso()
    lote = _metadata(kind, now)
    original_table = {
        "LCTE": LCTE_ORIGINAL_TABLE,
        "CONTRATO": COUPA_ORIGINAL_TABLE,
        "MAPEAMENTO": MAPPING_ORIGINAL_TABLE,
    }[kind]
    normalized_table = {
        "LCTE": LCTE_NORMALIZED_TABLE,
        "CONTRATO": COUPA_NORMALIZED_TABLE,
        "MAPEAMENTO": MAPPING_NORMALIZED_TABLE,
    }[kind]
    normalizer = {
        "LCTE": _normalize_lcte,
        "CONTRATO": _normalize_coupa,
        "MAPEAMENTO": lambda row, meta: _normalize_mapping(row, meta, usuario),
    }[kind]

    inserted = 0
    ignored = 0
    replaced = 0
    normalized_rows: list[dict[str, Any]] = []
    with get_connection() as conn:
        scope = str(escopo_importacao or "").strip()
        if scope:
            _ensure_payload_columns(conn, original_table, {IMPORT_SCOPE_COLUMN: scope})
            _ensure_payload_columns(conn, normalized_table, {IMPORT_SCOPE_COLUMN: scope})
            if kind == "LCTE":
                _ensure_payload_columns(conn, LCTE_NOTES_TABLE, {IMPORT_SCOPE_COLUMN: scope})
        if kind == "MAPEAMENTO" and replace_mapping:
            if scope:
                conn.execute(
                    f"update {MAPPING_NORMALIZED_TABLE} set ativo = 0, updated_at = ?, updated_by = ? where ativo = 1 and coalesce({IMPORT_SCOPE_COLUMN}, '') = ?",
                    (now, usuario, scope),
                )
            else:
                conn.execute(f"update {MAPPING_NORMALIZED_TABLE} set ativo = 0, updated_at = ?, updated_by = ? where ativo = 1", (now, usuario))
        replaced = _delete_previous_file_import(conn, kind, arquivo, scope)
        for index, (_, series) in enumerate(df.iterrows(), start=2):
            row = series.to_dict()
            row_hash = hashlib.sha256(f"{scope}:{file_hash}:{safe_json(row)}".encode("utf-8")).hexdigest()
            original = _base_original_payload(
                row,
                lote=lote,
                arquivo=arquivo,
                usuario=usuario,
                now=now,
                file_hash=file_hash,
                index=index,
                row_hash=row_hash,
            )
            meta = dict(original)
            normalized = normalizer(row, meta)
            if scope:
                original[IMPORT_SCOPE_COLUMN] = scope
                normalized[IMPORT_SCOPE_COLUMN] = scope
            normalized_rows.append(normalized)
        if kind == "MAPEAMENTO":
            _mark_mapping_duplicates(normalized_rows)
        for normalized in normalized_rows:
            original = {key: normalized[key] for key in ["lote_importacao", "arquivo_origem", "usuario_importacao", "data_hora_importacao", "hash_arquivo", "numero_linha", "hash_registro", "dados_json", "created_at", "updated_at"]}
            if scope:
                original[IMPORT_SCOPE_COLUMN] = scope
            _ensure_payload_columns(conn, original_table, original)
            _ensure_payload_columns(conn, normalized_table, normalized)
            original_inserted = _insert_dict(conn, original_table, original)
            normalized_inserted = _insert_dict(conn, normalized_table, normalized)
            normalized_updated = False
            if kind == "CONTRATO" and not normalized_inserted:
                normalized_updated = _update_by_hash(conn, normalized_table, normalized)
            if original_inserted or normalized_inserted:
                inserted += 1
            elif normalized_updated:
                inserted += 1
            else:
                ignored += 1
            if kind == "LCTE" and normalized_inserted:
                for nf in split_nf_list(normalized.get("nota_fiscal") or normalized.get("nota_fiscal_norm")) or [normalized.get("nota_fiscal_norm") or ""]:
                    note_payload = {
                        "lote_importacao": normalized["lote_importacao"],
                        "arquivo_origem": normalized["arquivo_origem"],
                        "usuario_importacao": normalized["usuario_importacao"],
                        "data_hora_importacao": normalized["data_hora_importacao"],
                        "hash_arquivo": normalized["hash_arquivo"],
                        "numero_linha": normalized["numero_linha"],
                        "hash_registro": hashlib.sha256(f"{normalized['hash_registro']}:{nf}".encode("utf-8")).hexdigest(),
                        "cte_norm": normalized["cte_norm"],
                        "nota_fiscal_norm": nf,
                        "chave_nfe": normalized["chave_nfe"],
                        "razao_social_cobranca": normalized["razao_social_cobranca"],
                        "razao_social_cobranca_norm": normalized["razao_social_cobranca_norm"],
                        "cnpj_cobranca_norm": normalized["cnpj_cobranca_norm"],
                        "produto": normalized["produto"],
                        "produto_norm": normalized["produto_norm"],
                        "origem": normalized["origem"],
                        "origem_norm": normalized["origem_norm"],
                        "destino": normalized["destino"],
                        "destino_norm": normalized["destino_norm"],
                        "placa": normalized["placa"],
                        "data_nf": normalized["data_emissao_nf"],
                        "data_cte": normalized["data_emissao_cte"],
                        "volume_litros": normalized["volume_litros"],
                        "valor_frete": normalized["valor_frete"],
                        "complemento_norm": normalized["complemento_norm"],
                        "dados_json": normalized["dados_json"],
                        "created_at": now,
                        "updated_at": now,
                    }
                    if scope:
                        note_payload[IMPORT_SCOPE_COLUMN] = scope
                    _ensure_payload_columns(conn, LCTE_NOTES_TABLE, note_payload)
                    _insert_dict(conn, LCTE_NOTES_TABLE, note_payload)
        _log_import(
            conn,
            {
                "data_hora": now,
                "usuario": usuario,
                "tipo_importacao": "COUPA" if kind == "CONTRATO" else kind,
                "arquivo_origem": arquivo,
                "hash_arquivo": file_hash,
                "quantidade_linhas": len(df),
                "quantidade_registros_inseridos": inserted,
                "quantidade_registros_ignorados": ignored,
                "status": "SUCESSO",
                "mensagem": f"Importacao {kind} concluida.",
                "detalhes": {"colunas": list(df.columns), "replace_mapping": replace_mapping, "registros_substituidos_mesmo_arquivo": replaced, "escopo_importacao": scope},
                "lote_importacao": lote,
            },
        )
    return {"lote_importacao": lote, "linhas": len(df), "inseridos": inserted, "ignorados": ignored, "substituidos": replaced, "status": "SUCESSO"}


def _import_mapping_dataframe(
    df: pd.DataFrame,
    *,
    arquivo: str,
    usuario: str,
    file_hash: str,
    update_existing: bool = False,
    escopo_importacao: str = IMPORT_SCOPE_FATURAMENTO_COUPA,
) -> dict[str, Any]:
    now = brasilia_now_iso()
    lote = _metadata("MAPEAMENTO", now)
    inserted = 0
    updated = 0
    ignored = 0
    conflicts = 0
    duplicates = 0
    invalid = 0
    seen_pairs: set[tuple[str, str]] = set()
    seen_descriptions: dict[str, str] = {}
    with get_connection() as conn:
        scope = str(escopo_importacao or "").strip()
        if scope:
            _ensure_payload_columns(conn, MAPPING_ORIGINAL_TABLE, {IMPORT_SCOPE_COLUMN: scope})
            _ensure_payload_columns(conn, MAPPING_NORMALIZED_TABLE, {IMPORT_SCOPE_COLUMN: scope})
        for index, (_, series) in enumerate(df.iterrows(), start=2):
            row = series.to_dict()
            row_hash = hashlib.sha256(f"{scope}:{file_hash}:{safe_json(row)}".encode("utf-8")).hexdigest()
            original = _base_original_payload(
                row,
                lote=lote,
                arquivo=arquivo,
                usuario=usuario,
                now=now,
                file_hash=file_hash,
                index=index,
                row_hash=row_hash,
            )
            if scope:
                original[IMPORT_SCOPE_COLUMN] = scope
            _ensure_payload_columns(conn, MAPPING_ORIGINAL_TABLE, original)
            _insert_dict(conn, MAPPING_ORIGINAL_TABLE, original)
            normalized = _normalize_mapping(row, dict(original), usuario)
            if scope:
                normalized[IMPORT_SCOPE_COLUMN] = scope
            _ensure_payload_columns(conn, MAPPING_NORMALIZED_TABLE, normalized)
            cnpj_norm = normalized["cnpj_norm"]
            descricao_norm = normalized["descricao_coupa_norm"]
            descricao_original = normalized["descricao_coupa_original"]
            status = normalized["status_mapeamento"]
            if status in {"CNPJ VAZIO", "CNPJ INVALIDO", "DESCRICAO COUPA VAZIA"}:
                normalized["ativo"] = 0
                _insert_dict(conn, MAPPING_NORMALIZED_TABLE, normalized)
                invalid += 1
                _mapping_history(
                    conn,
                    now=now,
                    usuario=usuario,
                    acao=status.replace(" ", "_"),
                    cnpj_norm=cnpj_norm,
                    descricao_nova=descricao_original,
                    arquivo_origem=arquivo,
                    lote_importacao=lote,
                    observacao=status,
                )
                continue
            pair = (cnpj_norm, descricao_norm)
            if pair in seen_pairs:
                duplicates += 1
                ignored += 1
                _mapping_history(
                    conn,
                    now=now,
                    usuario=usuario,
                    acao="DUPLICIDADE_ELIMINADA",
                    cnpj_norm=cnpj_norm,
                    descricao_nova=descricao_original,
                    arquivo_origem=arquivo,
                    lote_importacao=lote,
                    observacao="Duplicidade no arquivo importado.",
                )
                continue
            if descricao_norm in seen_descriptions and seen_descriptions[descricao_norm] != cnpj_norm:
                normalized["ativo"] = 0
                normalized["status_mapeamento"] = "CONFLITO_MAPEAMENTO"
                conflicts += 1
                _insert_dict(conn, MAPPING_NORMALIZED_TABLE, normalized)
                _mapping_history(
                    conn,
                    now=now,
                    usuario=usuario,
                    acao="CONFLITO_MAPEAMENTO",
                    cnpj_norm=cnpj_norm,
                    descricao_anterior=seen_descriptions[descricao_norm],
                    descricao_nova=descricao_original,
                    arquivo_origem=arquivo,
                    lote_importacao=lote,
                    observacao="Mesma descricao Coupa apareceu com CNPJs diferentes no arquivo importado.",
                )
                continue
            seen_pairs.add(pair)
            seen_descriptions[descricao_norm] = cnpj_norm

            existing_description = conn.execute(
                f"""
                select *
                from {MAPPING_NORMALIZED_TABLE}
                where descricao_coupa_norm = ?
                  and cnpj_norm <> ?
                  and coalesce(ativo, 1) = 1
                  and (? = '' or coalesce({IMPORT_SCOPE_COLUMN}, '') = ?)
                order by id desc
                limit 1
                """,
                (descricao_norm, cnpj_norm, scope, scope),
            ).fetchone()
            if existing_description:
                normalized["ativo"] = 0
                normalized["status_mapeamento"] = "CONFLITO_MAPEAMENTO"
                conflicts += 1
                _insert_dict(conn, MAPPING_NORMALIZED_TABLE, normalized)
                _mapping_history(
                    conn,
                    now=now,
                    usuario=usuario,
                    acao="CONFLITO_MAPEAMENTO",
                    cnpj_norm=cnpj_norm,
                    descricao_anterior=str(dict(existing_description).get("cnpj_norm") or ""),
                    descricao_nova=descricao_original,
                    arquivo_origem=arquivo,
                    lote_importacao=lote,
                    observacao="Descricao Coupa ja esta ativa para outro CNPJ.",
                )
                continue

            existing_same_description = conn.execute(
                f"""
                select *
                from {MAPPING_NORMALIZED_TABLE}
                where descricao_coupa_norm = ?
                  and cnpj_norm = ?
                  and coalesce(ativo, 1) = 1
                  and (? = '' or coalesce({IMPORT_SCOPE_COLUMN}, '') = ?)
                order by id desc
                limit 1
                """,
                (descricao_norm, cnpj_norm, scope, scope),
            ).fetchone()
            if existing_same_description:
                existing_dict = dict(existing_same_description)
                conn.execute(
                    f"""
                    update {MAPPING_NORMALIZED_TABLE}
                    set lote_importacao = ?, arquivo_origem = ?, usuario_importacao = ?,
                        data_hora_importacao = ?, hash_arquivo = ?, updated_at = ?, updated_by = ?,
                        status_mapeamento = 'MAPEAMENTO OK',
                        {IMPORT_SCOPE_COLUMN} = ?
                    where id = ?
                    """,
                    (lote, arquivo, usuario, now, file_hash, now, usuario, scope, int(existing_dict["id"])),
                )
                updated += 1
                _resolve_pending_mapping(conn, cnpj_norm, descricao_original, now, usuario, lote)
                _mapping_history(
                    conn,
                    now=now,
                    usuario=usuario,
                    acao="MANTIDO",
                    cnpj_norm=cnpj_norm,
                    descricao_anterior=str(existing_dict.get("descricao_coupa_original") or ""),
                    descricao_nova=descricao_original,
                    arquivo_origem=arquivo,
                    lote_importacao=lote,
                    observacao="Descricao Coupa ja estava cadastrada para o CNPJ.",
                )
                continue

            existing = conn.execute(
                f"""
                select *
                from {MAPPING_NORMALIZED_TABLE}
                where cnpj_norm = ? and coalesce(ativo, 1) = 1
                  and (? = '' or coalesce({IMPORT_SCOPE_COLUMN}, '') = ?)
                order by id desc
                limit 1
                """,
                (cnpj_norm, scope, scope),
            ).fetchone()
            if existing:
                normalized["status_mapeamento"] = "MAPEAMENTO OK"
                normalized["ativo"] = 1
                if _insert_dict(conn, MAPPING_NORMALIZED_TABLE, normalized):
                    inserted += 1
                    _resolve_pending_mapping(conn, cnpj_norm, descricao_original, now, usuario, lote)
                    _mapping_history(
                        conn,
                        now=now,
                        usuario=usuario,
                        acao="ALIAS_CRIADO",
                        cnpj_norm=cnpj_norm,
                        descricao_anterior=str(dict(existing).get("descricao_coupa_original") or ""),
                        descricao_nova=descricao_original,
                        arquivo_origem=arquivo,
                        lote_importacao=lote,
                        observacao="Nova descricao Coupa ativa para o mesmo CNPJ.",
                    )
                else:
                    duplicates += 1
                    ignored += 1
                continue

            existing = conn.execute(
                f"""
                select *
                from {MAPPING_NORMALIZED_TABLE}
                where cnpj_norm = ? and coalesce(ativo, 1) = 1
                  and (? = '' or coalesce({IMPORT_SCOPE_COLUMN}, '') = ?)
                order by id desc
                limit 1
                """,
                (cnpj_norm, scope, scope),
            ).fetchone()
            if not existing:
                normalized["status_mapeamento"] = "MAPEAMENTO OK"
                normalized["ativo"] = 1
                if _insert_dict(conn, MAPPING_NORMALIZED_TABLE, normalized):
                    inserted += 1
                    _resolve_pending_mapping(conn, cnpj_norm, descricao_original, now, usuario, lote)
                    _mapping_history(
                        conn,
                        now=now,
                        usuario=usuario,
                        acao="CRIADO",
                        cnpj_norm=cnpj_norm,
                        descricao_nova=descricao_original,
                        arquivo_origem=arquivo,
                        lote_importacao=lote,
                    )
                else:
                    ignored += 1
                continue

            existing_dict = dict(existing)
            previous_norm = str(existing_dict.get("descricao_coupa_norm") or existing_dict.get("nomenclatura_coupa_norm") or "")
            previous_original = str(existing_dict.get("descricao_coupa_original") or existing_dict.get("nomenclatura_coupa") or "")
            if previous_norm == descricao_norm:
                conn.execute(
                    f"""
                    update {MAPPING_NORMALIZED_TABLE}
                    set lote_importacao = ?, arquivo_origem = ?, usuario_importacao = ?,
                        data_hora_importacao = ?, hash_arquivo = ?, updated_at = ?, updated_by = ?,
                        status_mapeamento = 'MAPEAMENTO OK',
                        {IMPORT_SCOPE_COLUMN} = ?
                    where id = ?
                    """,
                    (lote, arquivo, usuario, now, file_hash, now, usuario, scope, int(existing_dict["id"])),
                )
                updated += 1
                _resolve_pending_mapping(conn, cnpj_norm, descricao_original, now, usuario, lote)
                _mapping_history(
                    conn,
                    now=now,
                    usuario=usuario,
                    acao="MANTIDO",
                    cnpj_norm=cnpj_norm,
                    descricao_anterior=previous_original,
                    descricao_nova=descricao_original,
                    arquivo_origem=arquivo,
                    lote_importacao=lote,
                    observacao="Descricao Coupa ja estava cadastrada para o CNPJ.",
                )
                continue

            if update_existing:
                conn.execute(
                    f"""
                    update {MAPPING_NORMALIZED_TABLE}
                    set descricao_coupa_original = ?, descricao_coupa_norm = ?,
                        nomenclatura_coupa = ?, nomenclatura_coupa_norm = ?,
                        cnpj_original = ?, cnpj_norm = ?, cnpj_lcte = ?, cnpj_lcte_norm = ?,
                        lote_importacao = ?, arquivo_origem = ?, usuario_importacao = ?,
                        data_hora_importacao = ?, hash_arquivo = ?, updated_at = ?, updated_by = ?,
                        {IMPORT_SCOPE_COLUMN} = ?,
                        status_mapeamento = 'MAPEAMENTO OK'
                    where id = ?
                    """,
                    (
                        descricao_original,
                        descricao_norm,
                        descricao_original,
                        descricao_norm,
                        normalized["cnpj_original"],
                        cnpj_norm,
                        normalized["cnpj_original"],
                        cnpj_norm,
                        lote,
                        arquivo,
                        usuario,
                        now,
                        file_hash,
                        now,
                        usuario,
                        scope,
                        int(existing_dict["id"]),
                    ),
                )
                updated += 1
                _resolve_pending_mapping(conn, cnpj_norm, descricao_original, now, usuario, lote)
                _mapping_history(
                    conn,
                    now=now,
                    usuario=usuario,
                    acao="ATUALIZADO",
                    cnpj_norm=cnpj_norm,
                    descricao_anterior=previous_original,
                    descricao_nova=descricao_original,
                    arquivo_origem=arquivo,
                    lote_importacao=lote,
                    observacao="Descricao atualizada por confirmacao do usuario.",
                )
            else:
                normalized["ativo"] = 0
                normalized["status_mapeamento"] = "CONFLITO_MAPEAMENTO"
                _insert_dict(conn, MAPPING_NORMALIZED_TABLE, normalized)
                conflicts += 1
                _mapping_history(
                    conn,
                    now=now,
                    usuario=usuario,
                    acao="CONFLITO_MAPEAMENTO",
                    cnpj_norm=cnpj_norm,
                    descricao_anterior=previous_original,
                    descricao_nova=descricao_original,
                    arquivo_origem=arquivo,
                    lote_importacao=lote,
                    observacao="Descricao diferente importada sem autorizacao para atualizar.",
                )
        _log_import(
            conn,
            {
                "data_hora": now,
                "usuario": usuario,
                "tipo_importacao": "MAPEAMENTO",
                "arquivo_origem": arquivo,
                "hash_arquivo": file_hash,
                "quantidade_linhas": len(df),
                "quantidade_registros_inseridos": inserted,
                "quantidade_registros_atualizados": updated,
                "quantidade_registros_ignorados": ignored,
                "status": "SUCESSO" if not conflicts and not invalid else "ATENCAO",
                "mensagem": "Importacao de mapeamento simplificado concluida.",
                "detalhes": {
                    "modelo": "DESCRICAO_COUPA_CNPJ",
                    "atualizar_existentes": update_existing,
                    "conflitos": conflicts,
                    "duplicidades_eliminadas": duplicates,
                    "invalidos": invalid,
                    "colunas": list(df.columns),
                    "escopo_importacao": scope,
                },
                "lote_importacao": lote,
            },
        )
        _mapping_log(
            conn,
            now,
            usuario,
            "SUCESSO" if not conflicts and not invalid else "ATENCAO",
            "Mapeamento simplificado importado.",
            {
                "lote_importacao": lote,
                "linhas": len(df),
                "inseridos": inserted,
                "atualizados": updated,
                "ignorados": ignored,
                "conflitos": conflicts,
                "duplicidades_eliminadas": duplicates,
                "invalidos": invalid,
            },
        )
    return {
        "lote_importacao": lote,
        "linhas": len(df),
        "inseridos": inserted,
        "atualizados": updated,
        "ignorados": ignored,
        "conflitos": conflicts,
        "duplicidades_eliminadas": duplicates,
        "invalidos": invalid,
        "status": "SUCESSO" if not conflicts and not invalid else "ATENCAO",
    }


def import_lcte(file: BinaryIO, usuario: str = "sistema", escopo_importacao: str = IMPORT_SCOPE_FATURAMENTO_COUPA) -> dict[str, Any]:
    initialize_modular_database()
    content = _bytes(file)
    df = _read_lcte_spreadsheet(content, getattr(file, "name", "lcte.xlsx"))
    return _import_dataframe(df, kind="LCTE", arquivo=getattr(file, "name", "lcte.xlsx"), usuario=usuario, file_hash=_file_hash(content), escopo_importacao=escopo_importacao)


def import_coupa(file: BinaryIO, usuario: str = "sistema", escopo_importacao: str = IMPORT_SCOPE_FATURAMENTO_COUPA) -> dict[str, Any]:
    initialize_modular_database()
    content = _bytes(file)
    df = _read_coupa_spreadsheet(content, getattr(file, "name", "coupa.xlsx"))
    return _import_dataframe(df, kind="CONTRATO", arquivo=getattr(file, "name", "coupa.xlsx"), usuario=usuario, file_hash=_file_hash(content), escopo_importacao=escopo_importacao)


def import_mapping(file: BinaryIO, usuario: str = "sistema", mode: str = "ADICIONAR", escopo_importacao: str = IMPORT_SCOPE_FATURAMENTO_COUPA) -> dict[str, Any]:
    initialize_modular_database()
    mode_key = normalize_text(mode)
    if mode_key == "CANCELAR":
        return {"lote_importacao": "", "linhas": 0, "inseridos": 0, "ignorados": 0, "status": "CANCELADO"}
    content = _bytes(file)
    df = _read_mapping_spreadsheet(content, getattr(file, "name", "mapeamento_coupa_lcte.xlsx"))
    return _import_mapping_dataframe(
        df,
        arquivo=getattr(file, "name", "mapeamento_coupa_lcte.xlsx"),
        usuario=usuario,
        file_hash=_file_hash(content),
        update_existing="ATUALIZAR" in mode_key or "SUBSTITUIR" in mode_key,
        escopo_importacao=escopo_importacao,
    )


def import_mapping_exceptions(
    file: BinaryIO,
    usuario: str = "sistema",
    escopo_importacao: str = IMPORT_SCOPE_FATURAMENTO_COUPA,
) -> dict[str, Any]:
    initialize_modular_database()
    content = _bytes(file)
    arquivo = getattr(file, "name", "excecoes_mapeamento_coupa_produto.xlsx")
    df = _read_mapping_exception_spreadsheet(content, arquivo)
    now = brasilia_now_iso()
    lote = _metadata("MAPEAMENTO_EXCECAO", now)
    file_hash = _file_hash(content)
    inserted = 0
    updated = 0
    ignored = 0
    invalid = 0
    seen_keys: set[tuple[str, ...]] = set()
    with get_connection() as conn:
        scope = str(escopo_importacao or "").strip()
        if scope:
            _ensure_payload_columns(conn, MAPPING_ORIGINAL_TABLE, {IMPORT_SCOPE_COLUMN: scope})
            _ensure_payload_columns(conn, MAPPING_NORMALIZED_TABLE, {IMPORT_SCOPE_COLUMN: scope})
        for index, (_, series) in enumerate(df.iterrows(), start=2):
            row = series.to_dict()
            row_hash = hashlib.sha256(f"{scope}:{file_hash}:{safe_json(row)}".encode("utf-8")).hexdigest()
            original = _base_original_payload(
                row,
                lote=lote,
                arquivo=arquivo,
                usuario=usuario,
                now=now,
                file_hash=file_hash,
                index=index,
                row_hash=row_hash,
            )
            if scope:
                original[IMPORT_SCOPE_COLUMN] = scope
            _ensure_payload_columns(conn, MAPPING_ORIGINAL_TABLE, original)
            _insert_dict(conn, MAPPING_ORIGINAL_TABLE, original)

            normalized = _normalize_mapping(row, dict(original), usuario)
            if scope:
                normalized[IMPORT_SCOPE_COLUMN] = scope
            produto_original = _get(row, MAPPING_ALIASES, "produto_coupa")
            produto_grupo = normalizar_produto_grupo(produto_original)
            origem_coupa = _get(row, MAPPING_ALIASES, "origem_coupa")
            destino_coupa = _get(row, MAPPING_ALIASES, "destino_coupa")
            cnpj_origem = _get(row, MAPPING_ALIASES, "cnpj_origem")
            cnpj_destino = _get(row, MAPPING_ALIASES, "cnpj_destino")
            origem_norm = normalizar_descricao_coupa_para_match(origem_coupa)
            destino_norm = normalizar_descricao_coupa_para_match(destino_coupa)
            cnpj_origem_norm = normalizar_cnpj_cpf(cnpj_origem)
            cnpj_destino_norm = normalizar_cnpj_cpf(cnpj_destino)
            is_route_exception = bool(origem_norm and destino_norm and cnpj_origem_norm and cnpj_destino_norm)
            route_product_marker = produto_excecao_rota(produto_original)
            tipo_vinculo = "EXCECAO_ROTA" if is_route_exception or route_product_marker else "EXCECAO_PRODUTO"
            if tipo_vinculo == "EXCECAO_ROTA":
                normalized.update(
                    {
                        "tipo_vinculo": "EXCECAO_ROTA",
                        "prioridade": LINK_PRIORITY["EXCECAO_ROTA"],
                        "ativo": 1,
                        "descricao_coupa_original": str(origem_coupa or normalized.get("descricao_coupa_original") or ""),
                        "descricao_coupa_norm": origem_norm or str(normalized.get("descricao_coupa_norm") or ""),
                        "nomenclatura_coupa": str(origem_coupa or normalized.get("nomenclatura_coupa") or ""),
                        "nomenclatura_coupa_norm": origem_norm or str(normalized.get("nomenclatura_coupa_norm") or ""),
                        "cnpj_original": str(cnpj_origem or ""),
                        "cnpj_norm": cnpj_origem_norm,
                        "cnpj_lcte": str(cnpj_origem or ""),
                        "cnpj_lcte_norm": cnpj_origem_norm,
                        "cnpj_origem": str(cnpj_origem or ""),
                        "cnpj_origem_norm": cnpj_origem_norm,
                        "cnpj_destino": str(cnpj_destino or ""),
                        "cnpj_destino_norm": cnpj_destino_norm,
                        "origem_coupa": str(origem_coupa or ""),
                        "origem_coupa_norm": origem_norm,
                        "destino_coupa": str(destino_coupa or ""),
                        "destino_coupa_norm": destino_norm,
                        "produto_coupa": str(produto_original or ""),
                        "produto_coupa_norm": normalizar_produto_kmm_coupa(produto_original),
                        "produto_grupo_excecao": "" if route_product_marker else produto_grupo,
                        "observacao": str(_get(row, MAPPING_ALIASES, "observacao") or "Excecao por rota"),
                        "status_mapeamento": "MAPEAMENTO OK",
                    }
                )
                if not (origem_norm and destino_norm and len(cnpj_origem_norm) in {11, 14} and len(cnpj_destino_norm) in {11, 14}):
                    normalized["ativo"] = 0
                    normalized["status_mapeamento"] = "EXCECAO ROTA INVALIDA"
                _ensure_payload_columns(conn, MAPPING_NORMALIZED_TABLE, normalized)
                if normalized["status_mapeamento"] != "MAPEAMENTO OK":
                    _insert_dict(conn, MAPPING_NORMALIZED_TABLE, normalized)
                    invalid += 1
                    continue
                key = ("ROTA", origem_norm, destino_norm, str(normalized.get("produto_grupo_excecao") or ""))
                if key in seen_keys:
                    ignored += 1
                    continue
                seen_keys.add(key)
                existing = conn.execute(
                    f"""
                    select *
                    from {MAPPING_NORMALIZED_TABLE}
                    where coalesce(ativo, 1) = 1
                      and tipo_vinculo = 'EXCECAO_ROTA'
                      and origem_coupa_norm = ?
                      and destino_coupa_norm = ?
                      and coalesce(produto_grupo_excecao, '') = ?
                      and (? = '' or coalesce({IMPORT_SCOPE_COLUMN}, '') = ?)
                    order by id desc
                    limit 1
                    """,
                    (origem_norm, destino_norm, str(normalized.get("produto_grupo_excecao") or ""), scope, scope),
                ).fetchone()
                if existing:
                    conn.execute(
                        f"update {MAPPING_NORMALIZED_TABLE} set ativo = 0, updated_at = ?, updated_by = ? where id = ?",
                        (now, usuario, int(dict(existing)["id"])),
                    )
                if _insert_dict(conn, MAPPING_NORMALIZED_TABLE, normalized):
                    inserted += 1
                else:
                    ignored += 1
                continue

            normalized.update(
                {
                    "tipo_vinculo": "EXCECAO_PRODUTO",
                    "prioridade": LINK_PRIORITY["EXCECAO_PRODUTO"],
                    "produto_coupa": str(produto_original or ""),
                    "produto_coupa_norm": normalizar_produto_kmm_coupa(produto_original),
                    "produto_grupo_excecao": produto_grupo,
                    "observacao": str(_get(row, MAPPING_ALIASES, "observacao") or "Excecao por produto"),
                }
            )
            if produto_grupo == "VAZIO":
                normalized["ativo"] = 0
                normalized["status_mapeamento"] = "PRODUTO COUPA VAZIO"
            elif normalized["status_mapeamento"] == "MAPEAMENTO OK":
                normalized["ativo"] = 1

            _ensure_payload_columns(conn, MAPPING_NORMALIZED_TABLE, normalized)
            if normalized["status_mapeamento"] != "MAPEAMENTO OK":
                _insert_dict(conn, MAPPING_NORMALIZED_TABLE, normalized)
                invalid += 1
                continue

            key = (str(normalized["descricao_coupa_norm"] or ""), produto_grupo)
            if key in seen_keys:
                ignored += 1
                continue
            seen_keys.add(key)

            existing = conn.execute(
                f"""
                select *
                from {MAPPING_NORMALIZED_TABLE}
                where coalesce(ativo, 1) = 1
                  and tipo_vinculo = 'EXCECAO_PRODUTO'
                  and descricao_coupa_norm = ?
                  and coalesce(produto_grupo_excecao, '') = ?
                  and (? = '' or coalesce({IMPORT_SCOPE_COLUMN}, '') = ?)
                order by id desc
                limit 1
                """,
                (normalized["descricao_coupa_norm"], produto_grupo, scope, scope),
            ).fetchone()
            if existing:
                existing_dict = dict(existing)
                if str(existing_dict.get("cnpj_norm") or "") == str(normalized["cnpj_norm"] or ""):
                    conn.execute(
                        f"""
                        update {MAPPING_NORMALIZED_TABLE}
                        set lote_importacao = ?, arquivo_origem = ?, usuario_importacao = ?,
                            data_hora_importacao = ?, hash_arquivo = ?, updated_at = ?, updated_by = ?,
                            produto_coupa = ?, produto_coupa_norm = ?, produto_grupo_excecao = ?,
                            status_mapeamento = 'MAPEAMENTO OK'
                        where id = ?
                        """,
                        (
                            lote,
                            arquivo,
                            usuario,
                            now,
                            file_hash,
                            now,
                            usuario,
                            normalized["produto_coupa"],
                            normalized["produto_coupa_norm"],
                            produto_grupo,
                            int(existing_dict["id"]),
                        ),
                    )
                    updated += 1
                    continue
                conn.execute(
                    f"update {MAPPING_NORMALIZED_TABLE} set ativo = 0, updated_at = ?, updated_by = ? where id = ?",
                    (now, usuario, int(existing_dict["id"])),
                )

            if _insert_dict(conn, MAPPING_NORMALIZED_TABLE, normalized):
                inserted += 1
            else:
                ignored += 1
        _log_import(
            conn,
            {
                "data_hora": now,
                "usuario": usuario,
                "tipo_importacao": "MAPEAMENTO_EXCECAO",
                "arquivo_origem": arquivo,
                "hash_arquivo": file_hash,
                "lote_importacao": lote,
                "linhas": len(df),
                "inseridos": inserted,
                "ignorados": ignored,
                "status": "SUCESSO" if not invalid else "ATENCAO",
                "mensagem": "Excecoes de mapeamento por produto importadas.",
                "detalhes": {"atualizados": updated, "invalidos": invalid, "escopo_importacao": scope},
            },
        )
    return {
        "lote_importacao": lote,
        "linhas": len(df),
        "inseridos": inserted,
        "atualizados": updated,
        "ignorados": ignored,
        "invalidos": invalid,
        "status": "SUCESSO" if not invalid else "ATENCAO",
    }


def _within_period(reference: str, start: str, end: str) -> bool:
    if not reference:
        return True
    try:
        ref = datetime.fromisoformat(reference).date()
        if start and ref < datetime.fromisoformat(start).date():
            return False
        if end and ref > datetime.fromisoformat(end).date():
            return False
    except ValueError:
        return True
    return True


def _matches(value: Any, expected: Any) -> bool:
    return not expected or str(value or "") == str(expected or "")


def _filter_import_scope(df: pd.DataFrame, escopo_importacao: str) -> pd.DataFrame:
    scope = str(escopo_importacao or "").strip()
    if df.empty or not scope:
        return df
    if IMPORT_SCOPE_COLUMN not in df.columns:
        return df.iloc[0:0].copy()
    return df[df[IMPORT_SCOPE_COLUMN].fillna("").astype(str).str.strip().eq(scope)].copy()


def _find_mapping(lcte: pd.Series, mappings: pd.DataFrame) -> pd.Series | None:
    if mappings.empty:
        return None
    cnpj = (
        str(lcte.get("cnpj_lcte_para_coupa_norm") or "")
        or str(lcte.get("cnpj_cobranca_norm") or "")
        or str(lcte.get("cnpj_remetente_norm") or "")
        or str(lcte.get("cnpj_destinatario_norm") or "")
    )
    candidates = mappings[
        mappings.get("ativo", pd.Series(0, index=mappings.index)).fillna(0).astype(int).eq(1)
        & mappings.get("cnpj_norm", mappings.get("cnpj_lcte_norm", pd.Series("", index=mappings.index))).fillna("").astype(str).eq(cnpj)
    ].copy()
    if candidates.empty:
        return None
    candidates["_ok"] = candidates.get("status_mapeamento", pd.Series("", index=candidates.index)).fillna("").astype(str).eq("MAPEAMENTO OK")
    return candidates.sort_values(["_ok", "id"], ascending=[False, False]).iloc[0]


def _find_coupa(mapping: pd.Series, coupa: pd.DataFrame, reference_date: str) -> pd.Series | None:
    if coupa.empty:
        return None
    candidates = coupa.copy()
    descricao = str(mapping.get("descricao_coupa_norm") or mapping.get("nomenclatura_coupa_norm") or "")
    if descricao:
        masks = []
        for field in ["descricao_coupa_norm", "nomenclatura_coupa_norm", "cliente_coupa_norm", "localidade_coupa_norm"]:
            if field in candidates.columns:
                masks.append(candidates[field].fillna("").astype(str).eq(descricao))
        if masks:
            mask = masks[0]
            for item in masks[1:]:
                mask = mask | item
            candidates = candidates[mask]
    codigo = str(mapping.get("codigo_coupa") or "")
    if codigo and "codigo_coupa" in candidates.columns:
        candidates = candidates[candidates["codigo_coupa"].fillna("").astype(str).eq(codigo)]
    if candidates.empty:
        return None
    candidates["_vigente"] = candidates.apply(lambda row: _within_period(reference_date, str(row.get("vigencia_inicio") or ""), str(row.get("vigencia_fim") or "")), axis=1)
    return candidates.sort_values(["_vigente", "id"], ascending=[False, True]).iloc[0]


def gerar_diagnostico(
    usuario: str = "sistema",
    somente_ipiranga: bool = False,
    limit: int = 5000,
    escopo_importacao: str = IMPORT_SCOPE_FATURAMENTO_COUPA,
) -> dict[str, Any]:
    initialize_modular_database()
    now = brasilia_now_iso()
    lote = f"DIAG_COUPA_{datetime.fromisoformat(now).strftime('%Y%m%d_%H%M%S')}"
    lcte = _filter_import_scope(read_sql(f"select * from {LCTE_NORMALIZED_TABLE} order by id desc limit ?", (int(limit),)), escopo_importacao)
    if somente_ipiranga and not lcte.empty:
        lcte = lcte[lcte.get("razao_social_cobranca_norm", pd.Series("", index=lcte.index)).fillna("").astype(str).str.contains("IPIRANGA")]
    mappings = _filter_import_scope(read_sql(f"select * from {MAPPING_NORMALIZED_TABLE} where ativo = 1 order by prioridade asc, id asc"), escopo_importacao)
    coupa = _filter_import_scope(read_sql(f"select * from {COUPA_NORMALIZED_TABLE} order by id asc"), escopo_importacao)
    rows = []
    for _, record in lcte.iterrows():
        cnpj_lcte = str(record.get("cnpj_lcte_para_coupa_norm") or record.get("cnpj_cobranca_norm") or record.get("cnpj_remetente_norm") or record.get("cnpj_destinatario_norm") or "")
        if len(cnpj_lcte) != 14:
            rows.append(_diagnostic_row(lote, now, usuario, record, None, None, "CNPJ LCTE INVALIDO"))
            continue
        mapping = _find_mapping(record, mappings)
        if mapping is None:
            rows.append(_diagnostic_row(lote, now, usuario, record, None, None, "PENDENTE MAPEAMENTO"))
            continue
        if str(mapping.get("status_mapeamento") or "") == "CONFLITO_MAPEAMENTO":
            rows.append(_diagnostic_row(lote, now, usuario, record, mapping, None, "CONFLITO NO MAPEAMENTO"))
            continue
        if not str(mapping.get("descricao_coupa_norm") or mapping.get("nomenclatura_coupa_norm") or ""):
            rows.append(_diagnostic_row(lote, now, usuario, record, mapping, None, "DESCRICAO COUPA VAZIA"))
            continue
        coupa_row = _find_coupa(mapping, coupa, str(record.get("data_emissao_cte") or record.get("data_emissao_nf") or ""))
        if coupa_row is None:
            rows.append(_diagnostic_row(lote, now, usuario, record, mapping, None, "MAPEADO MAS COUPA NAO ENCONTRADO"))
            continue
        duplicate_count = 0
        descricao = str(mapping.get("descricao_coupa_norm") or mapping.get("nomenclatura_coupa_norm") or "")
        if descricao and "nomenclatura_coupa_norm" in coupa.columns:
            duplicate_count = int(coupa["nomenclatura_coupa_norm"].fillna("").astype(str).eq(descricao).sum())
        status = "MAPEADO E COUPA ENCONTRADO"
        if duplicate_count > 1:
            status = "COUPA DUPLICADO PARA DESCRICAO"
        if not _within_period(str(record.get("data_emissao_cte") or record.get("data_emissao_nf") or ""), str(coupa_row.get("vigencia_inicio") or ""), str(coupa_row.get("vigencia_fim") or "")):
            status = "MAPEADO E COUPA ENCONTRADO"
        rows.append(_diagnostic_row(lote, now, usuario, record, mapping, coupa_row, status))
    with get_connection() as conn:
        for row in rows:
            _insert_dict(conn, DIAGNOSTIC_TABLE, row)
    return {"lote_diagnostico": lote, "linhas": len(rows), "status": "SUCESSO"}


def _diagnostic_row(
    lote: str,
    now: str,
    usuario: str,
    lcte: pd.Series,
    mapping: pd.Series | None,
    coupa: pd.Series | None,
    status: str,
) -> dict[str, Any]:
    return {
        "lote_diagnostico": lote,
        "data_hora_diagnostico": now,
        "usuario": usuario,
        "cte_norm": str(lcte.get("cte_norm") or ""),
        "nota_fiscal_norm": str(lcte.get("nota_fiscal_norm") or ""),
        "razao_social_cobranca": str(lcte.get("razao_social_cobranca") or ""),
        "cnpj_cobranca_norm": str(lcte.get("cnpj_cobranca_norm") or ""),
        "cnpj_lcte_para_coupa_norm": str(lcte.get("cnpj_lcte_para_coupa_norm") or lcte.get("cnpj_cobranca_norm") or lcte.get("cnpj_remetente_norm") or lcte.get("cnpj_destinatario_norm") or ""),
        "origem_cnpj_lcte_para_coupa": str(lcte.get("origem_cnpj_lcte_para_coupa") or ""),
        "origem_lcte_norm": str(lcte.get("origem_norm") or ""),
        "destino_lcte_norm": str(lcte.get("destino_norm") or ""),
        "produto_lcte_norm": str(lcte.get("produto_norm") or ""),
        "valor_frete": lcte.get("valor_frete"),
        "volume_litros": lcte.get("volume_litros"),
        "mapeamento_encontrado": "SIM" if mapping is not None else "NAO",
        "id_mapeamento": None if mapping is None else int(mapping.get("id") or 0),
        "tipo_vinculo": "" if mapping is None else str(mapping.get("tipo_vinculo") or "CNPJ"),
        "nomenclatura_coupa_usada": "" if mapping is None else str(mapping.get("descricao_coupa_original") or mapping.get("nomenclatura_coupa") or ""),
        "descricao_coupa_mapeada": "" if mapping is None else str(mapping.get("descricao_coupa_original") or mapping.get("nomenclatura_coupa") or ""),
        "descricao_coupa_norm": "" if mapping is None else str(mapping.get("descricao_coupa_norm") or mapping.get("nomenclatura_coupa_norm") or ""),
        "status_mapeamento_lcte": "PENDENTE MAPEAMENTO" if mapping is None else ("CONFLITO NO MAPEAMENTO" if str(mapping.get("status_mapeamento") or "") == "CONFLITO_MAPEAMENTO" else "MAPEADO"),
        "origem_coupa_mapeada": "" if mapping is None else str(mapping.get("origem_coupa") or ""),
        "destino_coupa_mapeada": "" if mapping is None else str(mapping.get("destino_coupa") or ""),
        "produto_coupa_mapeado": "" if mapping is None else str(mapping.get("produto_coupa") or ""),
        "coupa_encontrado": "SIM" if coupa is not None else "NAO",
        "codigo_coupa": "" if coupa is None else str(coupa.get("codigo_coupa") or ""),
        "tarifa_coupa": None if coupa is None else coupa.get("tarifa_coupa"),
        "unidade_tarifa_coupa": "" if coupa is None else str(coupa.get("unidade_tarifa_coupa") or ""),
        "volume_contratado": None if coupa is None else coupa.get("volume_contratado"),
        "valor_contratado": None if coupa is None else coupa.get("valor_contratado"),
        "vigencia_inicio": "" if coupa is None else str(coupa.get("vigencia_inicio") or ""),
        "vigencia_fim": "" if coupa is None else str(coupa.get("vigencia_fim") or ""),
        "status_diagnostico": status,
        "detalhes_json": json.dumps({}, ensure_ascii=False),
    }
