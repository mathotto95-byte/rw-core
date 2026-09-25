from __future__ import annotations

import shutil
import hashlib
import logging
from datetime import datetime
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso
from pathlib import Path
from typing import Any, BinaryIO, Callable

from src.config.settings import UPLOADS_DIR, load_config
from rw_core.database.connection import get_connection
from rw_core.database.schema import BASE_TABLES
from rw_core.importers.excel_importer import (
    PORTAL_IPP_AW_HEADER_INTERNAL_COLUMN,
    PORTAL_IPP_AW_INDEX_INTERNAL_COLUMN,
    PORTAL_IPP_AW_INTERNAL_COLUMN,
    PORTAL_IPP_COLUMN_COUNT_INTERNAL_COLUMN,
    PORTAL_IPP_POSITIONAL_COLUMNS,
    PORTAL_IPP_VALOR_AW_LETTER,
    PORTAL_IPP_VALOR_AW_POSITION,
    read_excel_sheet,
    read_portal_ipp_sheet,
)
from rw_core.normalizers.fields import (
    converter_data_excel_robusta,
    get_first,
    hash_dict,
    normalizar_nf,
    normalizar_produto_ipiranga,
    normalizar_valor_monetario,
    normalize_cnpj,
    normalize_column_name,
    normalize_document_number,
    normalize_location_key,
    normalize_plate,
    normalize_text,
    normalizar_produto_kmm_coupa,
    normalizar_texto_match,
    parse_date,
    parse_excel_date,
    parse_number,
    safe_json,
    is_empty,
    split_nf_list,
)
from rw_core.operational.kmm_faturamento import classify_kmm_complement_fields


KM_BASE_TYPE = "KM ORIGEM DESTINO"
KMM_CANONICAL_BASE_TYPE = "KMM / LCTE"
KMM_UI_BASE_TYPE = "KMM / LCTE / FAT"
KMM_COMPAT_BASE_TYPES = {KMM_CANONICAL_BASE_TYPE, KMM_UI_BASE_TYPE, "FAT_07_26"}
IMPORT_CHUNK_SIZE = 1000
logger = logging.getLogger(__name__)
MANUAL_TOLL_BASE_TYPE = "Atualização Manual Vale Pedágio"

KMM_REQUIRED_LAYOUT_FIELDS = {
    "cte_numero": "N. conhec.",
    "nota_fiscal": "Notas fiscais",
    "emissao_cte": "Data emissao",
    "emissao_nf": "Data Emissao NF",
    "cobranca": "Razao social da cobranca",
    "origem": "Local da coleta",
    "destino": "Local da entrega",
    "mercadoria": "Mercadoria",
    "volume": "Volume",
    "peso_frete": "Peso frete",
    "total_conhecimento": "Total do conhec.",
    "tabela_frete": "Tabela de frete",
}
KMM_LCTE_POSITIONAL_COLUMNS = {
    "complemento": ("lcte_col_b", 1),
    "emissao_cte": ("lcte_col_e", 4),
    "cte_numero": ("lcte_col_p", 15),
    "nota_fiscal": ("lcte_col_q", 16),
    "volume": ("lcte_col_s", 18),
    "frete_unitario": ("lcte_col_u", 20),
    "peso_frete": ("lcte_col_v", 21),
    "cnpj_destinatario": ("lcte_col_ao", 40),
    "cnpj_remetente": ("lcte_col_ap", 41),
}

TEXT_FIELDS = {
    "emitente",
    "destinatario",
    "natureza_operacao",
    "cliente",
    "cobranca",
    "origem",
    "uf_origem",
    "destino",
    "uf_destino",
    "produto",
    "mercadoria",
    "motorista",
    "observacao",
    "inserido_por",
    "tabela_frete",
    "item",
    "tipo_contratacao",
    "resposta_transportador",
    "resposta_transportador_norm",
    "origem_coupa_tipo",
    "produto_normalizado",
    "dt_inicio_original",
    "dt_termino_original",
    "dt_inicio_dt",
    "dt_termino_dt",
    "mes_referencia_coupa",
    "origem_norm",
    "destino_norm",
    "produto_norm",
    "cliente_norm",
    "rota_norm",
    "tipo_contratacao_norm",
    "arquivo",
    "viagem",
    "codigo",
    "pedido",
    "codigo_monitoramento",
    "agendamento",
    "cidade_uf",
    "referencia",
    "codigo_oferta",
    "tipo_operacao",
    "status",
    "numero_original",
    "numero_norm",
    "nota_fiscal_norm",
    "vinculo",
    "canhoto",
    "pago",
    "vinculo_norm",
    "canhoto_norm",
    "pago_norm",
    "data_emissao_portal_original",
    "data_emissao_portal_dt",
    "valor_portal_original",
    "coluna_origem_valor_portal",
    "nome_coluna_aw_portal",
    "quantidade_colunas_portal",
    "tipo_frete",
    "tipo_frete_norm",
    "nome_produto_portal",
}

def get_first_document(row: dict[str, Any], aliases: list[str]) -> Any:
    fallback = None
    for alias in aliases:
        value = get_first(row, [alias])
        if value is None or is_empty(value):
            continue
        document = normalizar_nf(value)
        if document and document != "0":
            return value
        if fallback is None:
            fallback = value
    return fallback


NUMBER_FIELDS = {
    "volume",
    "valor",
    "frete_unitario",
    "total_conhecimento",
    "peso_frete",
    "pedagio",
    "vale_pedagio",
    "pedagio_informado",
    "base_icms",
    "valor_icms",
    "base_icms_st",
    "valor_icms_st",
    "qtd",
    "distancia",
    "valor_bitrem",
    "valor_rodotrem",
    "latitude",
    "longitude",
    "permanencia",
    "km",
    "valor_portal",
    "valor_portal_frete",
    "valor_portal_pedagio",
    "valor_portal_base_calculo",
    "valor_portal_imposto",
    "valor_portal_total",
    "valor_unitario_frete",
    "indice_coluna_valor_portal",
    "quantidade_colunas_portal",
}

DATE_FIELDS = {
    "emissao_nf",
    "emissao_cte",
    "dt_inicio",
    "dt_termino",
    "data_inicio",
    "data_hora",
    "data_limite",
    "data",
    "data_carga",
    "data_descarga",
}

DOC_FIELDS = {"numero_nf", "nota_fiscal", "cte_numero"}


def canonical_base_type(base_type: str) -> str:
    return KMM_CANONICAL_BASE_TYPE if base_type in KMM_COMPAT_BASE_TYPES else base_type


def canonical_table_for_base(base_type: str) -> str:
    return BASE_TABLES[canonical_base_type(base_type)]


def aliases_for_base(base_type: str) -> dict[str, list[str]]:
    config = load_config()
    aliases = dict(config.get("column_aliases", {}).get(canonical_base_type(base_type), {}))
    if base_type in KMM_COMPAT_BASE_TYPES:
        fat_aliases = config.get("column_aliases", {}).get("FAT_07_26", {})
        for field, values in fat_aliases.items():
            aliases[field] = list(dict.fromkeys([*aliases.get(field, []), *values]))
        for field, (column, _) in KMM_LCTE_POSITIONAL_COLUMNS.items():
            aliases[field] = list(dict.fromkeys([*aliases.get(field, []), column]))
    return aliases


def add_kmm_lcte_positional_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    enriched = df.copy()
    for column_name, index in KMM_LCTE_POSITIONAL_COLUMNS.values():
        if column_name not in enriched.columns and index < len(enriched.columns):
            enriched[column_name] = enriched.iloc[:, index]
    return enriched


def duplicate_key_base_type(base_type: str) -> str:
    return KMM_CANONICAL_BASE_TYPE if base_type in KMM_COMPAT_BASE_TYPES else base_type


def validate_kmm_lcte_fat_layout(df) -> dict[str, Any]:
    aliases = aliases_for_base(KMM_CANONICAL_BASE_TYPE)
    columns = {normalize_column_name(column) for column in df.columns}
    missing = []
    found = {}
    for field, label in KMM_REQUIRED_LAYOUT_FIELDS.items():
        candidates = {normalize_column_name(alias) for alias in aliases.get(field, [])}
        matched = sorted(columns & candidates)
        if matched:
            found[field] = matched[0]
        else:
            missing.append(label)
    return {
        "ok": not missing,
        "missing": missing,
        "found": found,
        "columns": sorted(columns),
    }


def normalize_row(row: dict[str, Any], base_type: str, file_name: str) -> dict[str, Any]:
    base_type = canonical_base_type(base_type)
    aliases = aliases_for_base(base_type)
    config = load_config()
    normalized: dict[str, Any] = {}
    for field, field_aliases in aliases.items():
        raw = get_first(row, field_aliases)
        if field in TEXT_FIELDS:
            normalized[field] = normalize_text(raw)
        elif field in NUMBER_FIELDS:
            normalized[field] = parse_number(raw)
        elif field in DATE_FIELDS:
            normalized[field] = parse_date(raw)
        elif field in DOC_FIELDS:
            if base_type in KMM_COMPAT_BASE_TYPES and field == "nota_fiscal":
                notes = split_nf_list(raw)
                normalized["notas_fiscais_individuais"] = notes
                normalized[field] = " / ".join(notes) if notes else normalize_document_number(raw)
            else:
                normalized[field] = normalize_document_number(raw)
        elif field == "transportador_cnpj":
            normalized[field] = normalize_cnpj(raw)
        elif field == "placa":
            normalized[field] = normalize_plate(raw)
        else:
            normalized[field] = raw

    if base_type in KMM_COMPAT_BASE_TYPES:
        aliases = aliases_for_base(base_type)
        emissao_original = get_first(row, aliases.get("emissao_cte", []))
        emissao_nf_original = get_first(row, aliases.get("emissao_nf", []))
        volume_original = get_first(row, aliases.get("volume", []))
        valor_original = get_first(row, aliases.get("peso_frete", []))
        if is_empty(valor_original):
            valor_original = get_first(row, aliases.get("total_conhecimento", []))
        normalized.update(classify_kmm_complement_fields(normalized.get("complemento"), normalized.get("observacao")))
        normalized["cte"] = normalized.get("cte_numero")
        normalized["data_emissao_original"] = "" if is_empty(emissao_original) else str(emissao_original)
        normalized["data_emissao_dt"] = parse_excel_date(emissao_original) or normalized.get("emissao_cte") or ""
        normalized["data_emissao_nf_original"] = "" if is_empty(emissao_nf_original) else str(emissao_nf_original)
        normalized["data_emissao_nf_dt"] = parse_excel_date(emissao_nf_original) or normalized.get("emissao_nf") or ""
        normalized["mes_referencia"] = normalized["data_emissao_dt"][:7] if normalized.get("data_emissao_dt") else ""
        normalized["cliente_original"] = normalized.get("cobranca") or normalized.get("cliente") or ""
        normalized["origem_original"] = normalized.get("origem") or ""
        normalized["destino_original"] = normalized.get("destino") or ""
        normalized["produto_original"] = normalized.get("mercadoria") or normalized.get("produto") or ""
        normalized["operacao_original"] = normalized.get("tabela_frete") or ""
        normalized["placa_original"] = normalized.get("placa") or ""
        normalized["volume_original"] = "" if is_empty(volume_original) else str(volume_original)
        normalized["valor_kmm_original"] = "" if is_empty(valor_original) else str(valor_original)
        normalized["origem_norm"] = normalizar_texto_match(normalized.get("origem"))
        normalized["destino_norm"] = normalizar_texto_match(normalized.get("destino"))
        normalized["produto_norm"] = normalizar_produto_kmm_coupa(normalized.get("mercadoria") or normalized.get("produto"))
        normalized["cliente_norm"] = normalizar_texto_match(normalized.get("cobranca") or normalized.get("cliente"))
        normalized["operacao_norm"] = normalizar_texto_match(normalized.get("tabela_frete"))
        normalized["placa_norm"] = normalize_plate(normalized.get("placa"))
        normalized["volume_normalizado"] = normalize_fat_volume(normalized.get("volume"))
        normalized["valor_kmm"] = normalized.get("peso_frete") if normalized.get("peso_frete") not in [None, ""] else normalized.get("total_conhecimento")
        normalized["rota_norm"] = f"{normalized['origem_norm']}>{normalized['destino_norm']}" if normalized["origem_norm"] and normalized["destino_norm"] else ""
        normalized["tipo_base_origem"] = "KMM_LCTE_FAT"
    if base_type == "NSDOCS":
        normalized["transportador_cnpj"] = normalize_cnpj(normalized.get("transportador_cnpj"))
    if base_type == "COUPA" and not normalized.get("arquivo"):
        normalized["arquivo"] = file_name
    if base_type == "COUPA":
        aliases = config.get("column_aliases", {}).get(base_type, {})
        dt_inicio_original = get_first(row, aliases.get("dt_inicio", []))
        dt_termino_original = get_first(row, aliases.get("dt_termino", []))
        normalized["resposta_transportador_norm"] = normalize_text(normalized.get("resposta_transportador"))
        origem_norm = normalizar_texto_match(normalized.get("origem"))
        if origem_norm in {"T TODOS", "TODOS", "T TODO"}:
            origem_norm = "TODOS"
        normalized["origem_coupa_tipo"] = "TODOS" if origem_norm == "TODOS" else ""
        normalized["produto_normalizado"] = normalize_coupa_product(normalized.get("produto"))
        normalized["dt_inicio_original"] = "" if is_empty(dt_inicio_original) else str(dt_inicio_original)
        normalized["dt_termino_original"] = "" if is_empty(dt_termino_original) else str(dt_termino_original)
        normalized["dt_inicio_dt"] = parse_excel_date(dt_inicio_original)
        normalized["dt_termino_dt"] = parse_excel_date(dt_termino_original)
        normalized["dt_inicio_formatada"] = normalized["dt_inicio_dt"][:10] if normalized["dt_inicio_dt"] else ""
        normalized["dt_termino_formatada"] = normalized["dt_termino_dt"][:10] if normalized["dt_termino_dt"] else ""
        normalized["origem_original"] = normalized.get("origem") or ""
        normalized["destino_original"] = normalized.get("destino") or ""
        normalized["produto_original"] = normalized.get("produto") or ""
        normalized["tipo_contratacao_original"] = normalized.get("tipo_contratacao") or ""
        normalized["resposta_transportador_original"] = normalized.get("resposta_transportador") or ""
        normalized["origem_norm"] = origem_norm
        normalized["destino_norm"] = normalizar_texto_match(normalized.get("destino"))
        normalized["produto_norm"] = normalized["produto_normalizado"]
        normalized["tipo_contratacao_norm"] = normalizar_texto_match(normalized.get("tipo_contratacao"))
        normalized["rota_norm"] = f"{normalized['origem_norm']}>{normalized['destino_norm']}" if normalized["origem_norm"] and normalized["destino_norm"] else ""
        try:
            normalized["mes_referencia_coupa"] = datetime.fromisoformat(normalized["dt_inicio_dt"]).strftime("%Y-%m") if normalized["dt_inicio_dt"] else ""
        except ValueError:
            normalized["mes_referencia_coupa"] = ""
    if base_type == "PORTAL IPP":
        aliases = config.get("column_aliases", {}).get(base_type, {})
        positional_aliases = {field: column for field, (column, _) in PORTAL_IPP_POSITIONAL_COLUMNS.items()}
        numero_original = get_first_document(
            row,
            [
                positional_aliases.get("numero_original", ""),
                *aliases.get("numero_original", []),
                *aliases.get("nota_fiscal", []),
                "Notas fiscais",
                "Nota fiscal",
                "NF",
                "Numero",
                "Número",
            ],
        )
        data_portal_original = get_first(row, [positional_aliases.get("data_emissao_portal", ""), *(aliases.get("data_emissao_portal", []) or aliases.get("data", []))])
        produto_original = get_first(row, [positional_aliases.get("nome_produto_portal", ""), *(aliases.get("nome_produto_portal", []) or aliases.get("produto", []))])
        valor_portal_original = row.get(PORTAL_IPP_AW_INTERNAL_COLUMN)
        nome_coluna_aw = row.get(PORTAL_IPP_AW_HEADER_INTERNAL_COLUMN) or ""
        indice_coluna_aw = row.get(PORTAL_IPP_AW_INDEX_INTERNAL_COLUMN)
        valor_portal = normalizar_valor_monetario(valor_portal_original)
        coluna_origem_valor_portal = PORTAL_IPP_VALOR_AW_LETTER if PORTAL_IPP_AW_INTERNAL_COLUMN in row else ""
        if not coluna_origem_valor_portal:
            fallback_aliases = [
                *aliases.get("valor_portal", []),
                *aliases.get("valor_portal_frete", []),
                *aliases.get("valor", []),
                positional_aliases.get("valor_portal_frete", ""),
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
            valor_portal_original = get_first(row, fallback_aliases)
            valor_portal = normalizar_valor_monetario(valor_portal_original)
            coluna_origem_valor_portal = "NOME_COLUNA" if valor_portal_original is not None else "COLUNA_AW_NAO_ENCONTRADA"
            nome_coluna_aw = ""
            indice_coluna_aw = ""
        valor_frete = valor_portal
        if valor_frete is None and coluna_origem_valor_portal != PORTAL_IPP_VALOR_AW_LETTER:
            valor_frete = normalizar_valor_monetario(get_first(row, aliases.get("valor_portal_frete", []) or aliases.get("valor", [])))
        valor_base = normalizar_valor_monetario(get_first(row, aliases.get("valor_portal_base_calculo", [])))
        valor_imposto = normalizar_valor_monetario(get_first(row, aliases.get("valor_portal_imposto", [])))
        valor_total = normalizar_valor_monetario(get_first(row, aliases.get("valor_portal_total", [])))
        if valor_total is None and (valor_base is not None or valor_imposto is not None):
            valor_total = (valor_base or 0) + (valor_imposto or 0)
        numero_norm = normalizar_nf(numero_original)
        normalized["numero_original"] = "" if is_empty(numero_original) else str(numero_original)
        normalized["numero_norm"] = numero_norm
        normalized["nota_fiscal_norm"] = numero_norm
        normalized["nota_fiscal"] = numero_norm
        normalized["data_emissao_portal_original"] = "" if is_empty(data_portal_original) else str(data_portal_original)
        normalized["data_emissao_portal_dt"] = converter_data_excel_robusta(data_portal_original)
        normalized["vinculo"] = normalize_text(get_first(row, aliases.get("vinculo", [])))
        normalized["canhoto"] = normalize_text(get_first(row, aliases.get("canhoto", [])))
        normalized["pago"] = normalize_text(get_first(row, aliases.get("pago", [])))
        normalized["vinculo_norm"] = normalizar_texto_match(normalized["vinculo"])
        normalized["canhoto_norm"] = normalizar_texto_match(normalized["canhoto"])
        normalized["pago_norm"] = normalizar_texto_match(normalized["pago"])
        normalized["valor_portal_original"] = "" if is_empty(valor_portal_original) else str(valor_portal_original)
        normalized["valor_portal"] = valor_portal
        normalized["coluna_origem_valor_portal"] = coluna_origem_valor_portal
        normalized["nome_coluna_aw_portal"] = "" if is_empty(nome_coluna_aw) else str(nome_coluna_aw)
        normalized["indice_coluna_valor_portal"] = PORTAL_IPP_VALOR_AW_POSITION if coluna_origem_valor_portal == PORTAL_IPP_VALOR_AW_LETTER else indice_coluna_aw
        normalized["quantidade_colunas_portal"] = parse_number(row.get(PORTAL_IPP_COLUMN_COUNT_INTERNAL_COLUMN))
        normalized["valor_portal_frete"] = valor_frete
        normalized["valor_portal_pedagio"] = normalizar_valor_monetario(get_first(row, aliases.get("valor_portal_pedagio", [])))
        normalized["valor_portal_base_calculo"] = valor_base
        normalized["valor_portal_imposto"] = valor_imposto
        normalized["valor_portal_total"] = valor_total
        normalized["valor_unitario_frete"] = normalizar_valor_monetario(get_first(row, [positional_aliases.get("valor_unitario_frete", ""), *aliases.get("valor_unitario_frete", [])]))
        normalized["valor"] = valor_frete
        normalized["tipo_frete"] = normalize_text(get_first(row, aliases.get("tipo_frete", [])))
        normalized["tipo_frete_norm"] = normalizar_texto_match(normalized["tipo_frete"])
        normalized["nome_produto_portal"] = normalize_text(produto_original)
        normalized["produto"] = normalized["nome_produto_portal"] or normalized.get("produto")
        normalized["produto_norm"] = normalizar_produto_ipiranga(normalized.get("produto"))
        normalized["qtd"] = parse_number(get_first(row, [positional_aliases.get("qtd", ""), *aliases.get("qtd", [])]))
    if base_type == KM_BASE_TYPE:
        normalized["origem_normalizada"] = normalize_location_key(normalized.get("origem"))
        normalized["destino_normalizada"] = normalize_location_key(normalized.get("destino"))
        normalized["status"] = normalize_text(normalized.get("status")) or "ATIVO"
    return normalized


def normalize_coupa_product(value: Any) -> str:
    return normalizar_produto_kmm_coupa(value)


def normalize_fat_volume(value: Any) -> float | None:
    volume = parse_number(value)
    if volume is None:
        return None
    return volume * 1000 if 0 < volume < 100 else volume


def normalize_row_for_hash(row: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
    normalized_raw = {
        key: normalize_text(value)
        for key, value in row.items()
        if not is_empty(value)
    }
    return {
        "mapped": {key: value for key, value in normalized.items() if value not in [None, ""]},
        "raw": normalized_raw,
    }


def classify_kmm_complement(normalized: dict[str, Any]) -> str:
    config = load_config()
    rules = config.get("complement_rules", {})
    complement = normalize_text(normalized.get(rules.get("field", "complemento")))
    observation = normalize_text(normalized.get("observacao"))
    if complement in set(rules.get("true_values", ["SIM"])):
        return "COMPLEMENTAR"
    if any(term in observation for term in rules.get("observation_contains", [])):
        return "COMPLEMENTAR"
    return "NORMAL"


def build_key_hash(base_type: str, normalized: dict[str, Any]) -> str:
    keys = load_config().get("duplicate_keys", {}).get(duplicate_key_base_type(base_type), [])
    key_values = {key: normalized.get(key) for key in keys if normalized.get(key) not in [None, ""]}
    return hash_dict(key_values) if key_values else ""


def validate_normalized_row(base_type: str, normalized: dict[str, Any]) -> None:
    if base_type != KM_BASE_TYPE:
        return
    if not normalized.get("origem_normalizada"):
        raise ValueError("Origem obrigatoria na base KM ORIGEM DESTINO.")
    if not normalized.get("destino_normalizada"):
        raise ValueError("Destino obrigatorio na base KM ORIGEM DESTINO.")
    if normalized.get("km") in [None, ""]:
        raise ValueError("KM obrigatorio e numerico na base KM ORIGEM DESTINO.")


def save_upload(file: BinaryIO, filename: str) -> str:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = brasilia_now().strftime("%Y%m%d_%H%M%S")
    safe_name = Path(filename).name
    target = UPLOADS_DIR / f"{timestamp}_{safe_name}"
    file.seek(0)
    with target.open("wb") as output:
        shutil.copyfileobj(file, output)
    return str(target)


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def insert_imported_file(
    conn,
    import_id: int,
    filename: str,
    base_type: str,
    stored_path: str,
    username: str = "",
    status: str = "IMPORTADO",
) -> None:
    conn.execute(
        """
        insert into arquivos_importados (
            importacao_id, nome_arquivo, tipo_base, caminho_armazenado,
            data_importacao, usuario, status, hash_arquivo
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            import_id,
            filename,
            base_type,
            stored_path,
            brasilia_now_iso(),
            username,
            status,
            file_hash(stored_path),
        ),
    )


def insert_import_history(
    conn,
    filename: str,
    base_type: str,
    sheet: str,
    total: int,
    username: str = "",
    user_role: str = "",
) -> int:
    now = brasilia_now_iso()
    cursor = conn.execute(
        """
        insert into importacoes (
            nome_arquivo, tipo_base, aba_importada, data_importacao, linhas_lidas,
            linhas_novas, linhas_atualizadas, linhas_duplicadas, linhas_erro, status, mensagem_erro,
            usuario, perfil_usuario
        ) values (?, ?, ?, ?, ?, 0, 0, 0, 0, 'EM PROCESSAMENTO', '', ?, ?)
        """,
        (filename, base_type, sheet, now, total, username, user_role),
    )
    return int(cursor.lastrowid)


def update_import_history(conn, import_id: int, new_rows: int, updated_rows: int, duplicates: int, errors: int, status: str, message: str = "") -> None:
    conn.execute(
        """
        update importacoes
        set linhas_novas = ?, linhas_atualizadas = ?, linhas_duplicadas = ?, linhas_erro = ?, status = ?, mensagem_erro = ?
        where id = ?
        """,
        (new_rows, updated_rows, duplicates, errors, status, message, import_id),
    )


def active_key_rows(conn, table: str, base_type: str, key_hash: str) -> list:
    if not key_hash:
        return []
    return conn.execute(
        f"""
        select id from {table}
        where tipo_base = ?
          and key_hash = ?
          and coalesce(status_registro, 'ATIVO') = 'ATIVO'
        """,
        (base_type, key_hash),
    ).fetchall()


def mark_key_rows_replaced(conn, table: str, base_type: str, key_hash: str) -> int:
    rows = active_key_rows(conn, table, base_type, key_hash)
    if not rows:
        return 0
    ids = [int(row["id"]) for row in rows]
    placeholders = ", ".join(["?"] * len(ids))
    conn.execute(
        f"update {table} set status_registro = 'SUBSTITUIDO' where id in ({placeholders})",
        ids,
    )
    if base_type in KMM_COMPAT_BASE_TYPES:
        conn.execute(
            f"update base_kmm_fat_notas_normalizadas set status_registro = 'SUBSTITUIDO' where base_original_id in ({placeholders}) and tipo_base = ?",
            [*ids, base_type],
        )
    if base_type in KMM_COMPAT_BASE_TYPES:
        conn.execute(
            f"update base_kmm_notas_normalizadas set status_registro = 'SUBSTITUIDO' where kmm_original_id in ({placeholders})",
            ids,
        )
    elif base_type == "COUPA":
        conn.execute(
            f"update base_coupa_fluxos_normalizados set status_registro = 'SUBSTITUIDO' where coupa_original_id in ({placeholders})",
            ids,
        )
    elif base_type == "PORTAL IPP":
        conn.execute(
            f"update base_portal_ipp_normalizada set status_registro = 'SUBSTITUIDO' where portal_original_id in ({placeholders})",
            ids,
        )
    return len(ids)


def active_content_rows(conn, table: str, base_type: str, content_hash: str) -> list:
    if not content_hash:
        return []
    return conn.execute(
        f"""
        select id from {table}
        where tipo_base = ?
          and coalesce(status_registro, 'ATIVO') = 'ATIVO'
          and (hash_registro = ? or row_hash = ?)
        """,
        (base_type, content_hash, content_hash),
    ).fetchall()


def mark_content_rows_replaced(conn, table: str, base_type: str, content_hash: str) -> int:
    rows = active_content_rows(conn, table, base_type, content_hash)
    if not rows:
        return 0
    ids = [int(row["id"]) for row in rows]
    placeholders = ", ".join(["?"] * len(ids))
    conn.execute(
        f"update {table} set status_registro = 'SUBSTITUIDO' where id in ({placeholders})",
        ids,
    )
    return len(ids)


def versioned_row_hash(content_hash: str, import_id: int, sequence: int) -> str:
    return hash_dict({"hash_registro": content_hash, "importacao_id": import_id, "sequencia": sequence})


def active_km_key_row(conn, table: str, key_hash: str):
    if not key_hash:
        return None
    return conn.execute(
        f"""
        select id, km from {table}
        where tipo_base = ?
          and key_hash = ?
          and coalesce(status_registro, 'ATIVO') in ('ATIVO', 'NOVO REGISTRO', 'KM ATUALIZADO')
          and coalesce(status, 'ATIVO') = 'ATIVO'
        order by id desc
        limit 1
        """,
        (KM_BASE_TYPE, key_hash),
    ).fetchone()


def same_km(current: Any, incoming: Any) -> bool:
    if current in [None, ""] or incoming in [None, ""]:
        return current in [None, ""] and incoming in [None, ""]
    try:
        return abs(float(current) - float(incoming)) <= 0.000001
    except (TypeError, ValueError):
        return False


def km_content_hash(normalized: dict[str, Any]) -> str:
    return hash_dict(
        {
            "origem_normalizada": normalized.get("origem_normalizada"),
            "destino_normalizada": normalized.get("destino_normalizada"),
            "km": normalized.get("km"),
        }
    )


def import_km_origem_destino_rows(
    conn,
    table: str,
    import_id: int,
    filename: str,
    sheet_name: str,
    records: list[dict[str, Any]],
) -> tuple[int, int, int, int]:
    new_rows = updated_rows = duplicate_rows = error_rows = 0
    for sequence, record in enumerate(records, start=1):
        try:
            normalized = normalize_row(record, KM_BASE_TYPE, filename)
            validate_normalized_row(KM_BASE_TYPE, normalized)
            content_hash = km_content_hash(normalized)
            row_hash = versioned_row_hash(content_hash, import_id, sequence)
            key_hash = build_key_hash(KM_BASE_TYPE, normalized)
            active_row = active_km_key_row(conn, table, key_hash)
            if active_row:
                conn.execute(
                    f"update {table} set status_registro = 'SUBSTITUIDO' where id = ?",
                    (int(active_row["id"]),),
                )
                normalized["status_registro"] = (
                    "NOVO REGISTRO" if same_km(active_row["km"], normalized.get("km")) else "KM ATUALIZADO"
                )
                updated_rows += 1
                duplicate_rows += 1
            else:
                normalized["status_registro"] = "NOVO REGISTRO"
            insert_base_row(
                conn,
                table,
                import_id,
                KM_BASE_TYPE,
                filename,
                sheet_name,
                record,
                normalized,
                row_hash,
                key_hash,
                content_hash,
            )
            new_rows += 1
        except Exception:
            error_rows += 1
    return new_rows, updated_rows, duplicate_rows, error_rows


def base_row_fields() -> list[str]:
    return [
        "importacao_id",
        "tipo_base",
        "arquivo_origem",
        "aba_origem",
        "data_importacao",
        "row_hash",
        "hash_registro",
        "key_hash",
        "original_json",
        "normalized_json",
        "status_registro",
        "numero_nf",
        "nota_fiscal",
        "chave_acesso",
        "cte_numero",
        "chave_cte",
        "chave_nfe",
        "transportador_cnpj",
        "emitente",
        "destinatario",
        "natureza_operacao",
        "cliente",
        "cobranca",
        "origem",
        "origem_normalizada",
        "uf_origem",
        "destino",
        "destino_normalizada",
        "uf_destino",
        "produto",
        "mercadoria",
        "placa",
        "motorista",
        "emissao_nf",
        "emissao_cte",
        "volume",
        "valor",
        "frete_unitario",
        "total_conhecimento",
        "peso_frete",
        "pedagio",
        "vale_pedagio",
        "pedagio_informado",
        "id_icms_st",
        "cfop",
        "base_icms",
        "valor_icms",
        "base_icms_st",
        "valor_icms_st",
        "complemento",
        "status_complemento_normalizado",
        "status",
        "observacao",
        "inserido_por",
        "tabela_frete",
        "item",
        "qtd",
        "dt_inicio",
        "dt_termino",
        "distancia",
        "tipo_contratacao",
        "valor_bitrem",
        "valor_rodotrem",
        "resposta_transportador",
        "resposta_transportador_norm",
        "origem_coupa_tipo",
        "produto_normalizado",
        "dt_inicio_original",
        "dt_termino_original",
        "dt_inicio_dt",
        "dt_termino_dt",
        "mes_referencia_coupa",
        "origem_norm",
        "destino_norm",
        "produto_norm",
        "cliente_norm",
        "rota_norm",
        "tipo_contratacao_norm",
        "dt_inicio_formatada",
        "dt_termino_formatada",
        "data_emissao_original",
        "data_emissao_dt",
        "data_emissao_nf_original",
        "data_emissao_nf_dt",
        "mes_referencia",
        "origem_original",
        "destino_original",
        "produto_original",
        "tipo_contratacao_original",
        "resposta_transportador_original",
        "cliente_original",
        "operacao_original",
        "placa_original",
        "volume_original",
        "valor_kmm_original",
        "operacao_norm",
        "placa_norm",
        "volume_normalizado",
        "valor_kmm",
        "cte",
        "numero_original",
        "numero_norm",
        "nota_fiscal_norm",
        "vinculo",
        "canhoto",
        "pago",
        "vinculo_norm",
        "canhoto_norm",
        "pago_norm",
        "data_emissao_portal_original",
        "data_emissao_portal_dt",
        "valor_portal_original",
        "valor_portal",
        "coluna_origem_valor_portal",
        "nome_coluna_aw_portal",
        "indice_coluna_valor_portal",
        "quantidade_colunas_portal",
        "valor_portal_frete",
        "valor_portal_pedagio",
        "valor_portal_base_calculo",
        "valor_portal_imposto",
        "valor_portal_total",
        "valor_unitario_frete",
        "tipo_frete",
        "tipo_frete_norm",
        "nome_produto_portal",
        "arquivo",
        "viagem",
        "codigo",
        "pedido",
        "codigo_monitoramento",
        "codigo_oferta",
        "agendamento",
        "data_inicio",
        "data_carga",
        "data_descarga",
        "data_hora",
        "data_limite",
        "cidade_uf",
        "latitude",
        "longitude",
        "referencia",
        "permanencia",
        "km",
        "tipo_operacao",
        "tipo_base_origem",
        "lote_importacao",
        "created_at",
        "updated_at",
        "source_file",
        "import_batch_id",
        "sync_status",
        "last_synced_at",
    ]


def base_row_values(
    import_id: int,
    base_type: str,
    file_name: str,
    sheet: str,
    original: dict[str, Any],
    normalized: dict[str, Any],
    row_hash: str,
    key_hash: str,
    content_hash: str | None = None,
    imported_at: str | None = None,
) -> list[Any]:
    fields = base_row_fields()
    return [
        import_id,
        base_type,
        file_name,
        sheet,
        imported_at or brasilia_now_iso(),
        row_hash,
        content_hash or row_hash,
        key_hash,
        safe_json(original),
        safe_json(normalized),
        normalized.get("status_registro") or "ATIVO",
    ] + [
        import_id if field == "lote_importacao" and base_type in KMM_COMPAT_BASE_TYPES else normalized.get(field)
        for field in fields[11:-6]
    ] + [
        imported_at or brasilia_now_iso(),
        imported_at or brasilia_now_iso(),
        file_name,
        import_id,
        "NOVO_LOCAL",
        None,
    ]


def insert_base_row(
    conn,
    table: str,
    import_id: int,
    base_type: str,
    file_name: str,
    sheet: str,
    original: dict[str, Any],
    normalized: dict[str, Any],
    row_hash: str,
    key_hash: str,
    content_hash: str | None = None,
) -> int:
    fields = base_row_fields()
    values = base_row_values(
        import_id,
        base_type,
        file_name,
        sheet,
        original,
        normalized,
        row_hash,
        key_hash,
        content_hash,
    )
    placeholders = ", ".join(["?"] * len(fields))
    cursor = conn.execute(
        f"insert into {table} ({', '.join(fields)}) values ({placeholders})",
        values,
    )
    return int(cursor.lastrowid)


def insert_kmm_notes(conn, base_row_id: int, import_id: int, normalized: dict[str, Any], file_name: str) -> None:
    notes = normalized.get("notas_fiscais_individuais") or split_nf_list(normalized.get("nota_fiscal"))
    if not notes:
        return
    for note in notes:
        conn.execute(
            """
            insert or ignore into base_kmm_notas_normalizadas (
                kmm_original_id, importacao_id, cte_numero, nota_fiscal, nota_fiscal_normalizada,
                chave_cte, chave_nfe, emissao_nf, emissao_cte, situacao, complemento,
                status_complemento_normalizado, placa, motorista, cliente, cobranca, uf_origem, origem,
                uf_destino, destino, mercadoria, volume, valor, frete_unitario, total_conhecimento,
                peso_frete, pedagio, vale_pedagio, pedagio_informado, id_icms_st, cfop, base_icms, valor_icms,
                base_icms_st, valor_icms_st, inserido_por, tabela_frete, arquivo_origem,
                data_importacao
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                base_row_id,
                import_id,
                normalized.get("cte_numero"),
                normalized.get("nota_fiscal"),
                note,
                normalized.get("chave_cte"),
                normalized.get("chave_nfe"),
                normalized.get("emissao_nf"),
                normalized.get("emissao_cte"),
                normalized.get("situacao"),
                normalized.get("complemento"),
                normalized.get("status_complemento_normalizado"),
                normalized.get("placa"),
                normalized.get("motorista"),
                normalized.get("cliente"),
                normalized.get("cobranca"),
                normalized.get("uf_origem"),
                normalized.get("origem"),
                normalized.get("uf_destino"),
                normalized.get("destino"),
                normalized.get("mercadoria"),
                normalized.get("volume"),
                normalized.get("total_conhecimento"),
                normalized.get("frete_unitario"),
                normalized.get("total_conhecimento"),
                normalized.get("peso_frete"),
                normalized.get("pedagio"),
                normalized.get("vale_pedagio"),
                normalized.get("pedagio_informado"),
                normalized.get("id_icms_st"),
                normalized.get("cfop"),
                normalized.get("base_icms"),
                normalized.get("valor_icms"),
                normalized.get("base_icms_st"),
                normalized.get("valor_icms_st"),
                normalized.get("inserido_por"),
                normalized.get("tabela_frete"),
                file_name,
                brasilia_now_iso(),
            ),
        )


def insert_coupa_flow(conn, base_row_id: int, import_id: int, normalized: dict[str, Any], file_name: str) -> None:
    conn.execute(
        """
        insert into base_coupa_fluxos_normalizados (
            coupa_original_id, importacao_id, item, qtd, dt_inicio, dt_termino,
            origem, destino, distancia, tipo_contratacao, produto, valor_bitrem,
            valor_rodotrem, resposta_transportador, resposta_transportador_norm,
            origem_coupa_tipo, produto_normalizado, dt_inicio_original, dt_termino_original,
            dt_inicio_dt, dt_termino_dt, dt_inicio_formatada, dt_termino_formatada,
            mes_referencia_coupa, origem_norm, destino_norm, produto_norm,
            tipo_contratacao_norm, rota_norm, arquivo, reajuste, arquivo_origem,
            data_importacao
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            base_row_id,
            import_id,
            normalized.get("item"),
            normalized.get("qtd"),
            normalized.get("dt_inicio"),
            normalized.get("dt_termino"),
            normalized.get("origem"),
            normalized.get("destino"),
            normalized.get("distancia"),
            normalized.get("tipo_contratacao"),
            normalized.get("produto"),
            normalized.get("valor_bitrem"),
            normalized.get("valor_rodotrem"),
            normalized.get("resposta_transportador"),
            normalized.get("resposta_transportador_norm"),
            normalized.get("origem_coupa_tipo"),
            normalized.get("produto_normalizado"),
            normalized.get("dt_inicio_original"),
            normalized.get("dt_termino_original"),
            normalized.get("dt_inicio_dt"),
            normalized.get("dt_termino_dt"),
            normalized.get("dt_inicio_formatada"),
            normalized.get("dt_termino_formatada"),
            normalized.get("mes_referencia_coupa"),
            normalized.get("origem_norm"),
            normalized.get("destino_norm"),
            normalized.get("produto_norm"),
            normalized.get("tipo_contratacao_norm"),
            normalized.get("rota_norm"),
            normalized.get("arquivo"),
            normalized.get("reajuste"),
            file_name,
            brasilia_now_iso(),
        ),
    )


def chunks(values: list[Any], size: int = IMPORT_CHUNK_SIZE):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def placeholders(count: int) -> str:
    return ", ".join(["?"] * count)


def fetch_active_rows_by_column(conn, table: str, base_type: str, column: str, values: set[str]) -> dict[str, list]:
    values = {value for value in values if value}
    found: dict[str, list] = {}
    for batch in chunks(list(values)):
        rows = conn.execute(
            f"""
            select id, {column}, km
            from {table}
            where tipo_base = ?
              and {column} in ({placeholders(len(batch))})
              and coalesce(status_registro, 'ATIVO') = 'ATIVO'
            """,
            [base_type, *batch],
        ).fetchall()
        for row in rows:
            found.setdefault(str(row[column]), []).append(row)
    return found


def mark_rows_replaced_bulk(conn, table: str, ids: list[int], base_type: str = "") -> None:
    ids = sorted({int(row_id) for row_id in ids if row_id})
    for batch in chunks(ids):
        conn.execute(
            f"update {table} set status_registro = 'SUBSTITUIDO' where id in ({placeholders(len(batch))})",
            batch,
        )
        if base_type in KMM_COMPAT_BASE_TYPES:
            conn.execute(
                f"update base_kmm_fat_notas_normalizadas set status_registro = 'SUBSTITUIDO' where base_original_id in ({placeholders(len(batch))}) and tipo_base = ?",
                [*batch, base_type],
            )
        if base_type in KMM_COMPAT_BASE_TYPES:
            conn.execute(
                f"update base_kmm_notas_normalizadas set status_registro = 'SUBSTITUIDO' where kmm_original_id in ({placeholders(len(batch))})",
                batch,
            )
        elif base_type == "COUPA":
            conn.execute(
                f"update base_coupa_fluxos_normalizados set status_registro = 'SUBSTITUIDO' where coupa_original_id in ({placeholders(len(batch))})",
                batch,
            )
        elif base_type == "PORTAL IPP":
            conn.execute(
                f"update base_portal_ipp_normalizada set status_registro = 'SUBSTITUIDO' where portal_original_id in ({placeholders(len(batch))})",
                batch,
            )


def bulk_insert_base_rows(conn, table: str, values: list[list[Any]]) -> None:
    if not values:
        return
    fields = base_row_fields()
    sql = f"insert into {table} ({', '.join(fields)}) values ({placeholders(len(fields))})"
    for batch in chunks(values):
        conn.executemany(sql, batch)


def fetch_inserted_base_ids(conn, table: str, row_hashes: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for batch in chunks(row_hashes):
        rows = conn.execute(
            f"select id, row_hash from {table} where row_hash in ({placeholders(len(batch))})",
            batch,
        ).fetchall()
        mapping.update({str(row["row_hash"]): int(row["id"]) for row in rows})
    return mapping


def kmm_note_values(base_row_id: int, import_id: int, normalized: dict[str, Any], file_name: str, imported_at: str) -> list[tuple]:
    notes = normalized.get("notas_fiscais_individuais") or split_nf_list(normalized.get("nota_fiscal"))
    if not notes:
        return []
    return [
        (
            base_row_id,
            import_id,
            normalized.get("cte_numero"),
            normalized.get("nota_fiscal"),
            note,
            normalized.get("chave_cte"),
            normalized.get("chave_nfe"),
            normalized.get("emissao_nf"),
            normalized.get("emissao_cte"),
            normalized.get("situacao"),
            normalized.get("complemento"),
            normalized.get("status_complemento_normalizado"),
            normalized.get("placa"),
            normalized.get("motorista"),
            normalized.get("cliente"),
            normalized.get("cobranca"),
            normalized.get("uf_origem"),
            normalized.get("origem"),
            normalized.get("uf_destino"),
            normalized.get("destino"),
            normalized.get("mercadoria"),
            normalized.get("volume"),
            normalized.get("total_conhecimento"),
            normalized.get("frete_unitario"),
            normalized.get("total_conhecimento"),
            normalized.get("peso_frete"),
            normalized.get("pedagio"),
            normalized.get("vale_pedagio"),
            normalized.get("pedagio_informado"),
            normalized.get("id_icms_st"),
            normalized.get("cfop"),
            normalized.get("base_icms"),
            normalized.get("valor_icms"),
            normalized.get("base_icms_st"),
            normalized.get("valor_icms_st"),
            normalized.get("inserido_por"),
            normalized.get("tabela_frete"),
            file_name,
            imported_at,
        )
        for note in notes
    ]


def bulk_insert_kmm_notes(conn, values: list[tuple]) -> None:
    if not values:
        return
    sql = """
        insert or ignore into base_kmm_notas_normalizadas (
            kmm_original_id, importacao_id, cte_numero, nota_fiscal, nota_fiscal_normalizada,
            chave_cte, chave_nfe, emissao_nf, emissao_cte, situacao, complemento,
            status_complemento_normalizado, placa, motorista, cliente, cobranca, uf_origem, origem,
            uf_destino, destino, mercadoria, volume, valor, frete_unitario, total_conhecimento,
            peso_frete, pedagio, vale_pedagio, pedagio_informado, id_icms_st, cfop, base_icms, valor_icms,
            base_icms_st, valor_icms_st, inserido_por, tabela_frete, arquivo_origem,
            data_importacao
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for batch in chunks(values):
        conn.executemany(sql, batch)


def kmm_fat_note_values(base_row_id: int, import_id: int, base_type: str, normalized: dict[str, Any], file_name: str, imported_at: str) -> list[tuple]:
    notes = normalized.get("notas_fiscais_individuais") or split_nf_list(normalized.get("nota_fiscal"))
    if not notes:
        return []
    return [
        (
            base_row_id,
            import_id,
            base_type,
            normalized.get("data_emissao_dt") or normalized.get("emissao_cte"),
            normalized.get("data_emissao_dt") or normalized.get("emissao_cte"),
            normalized.get("cte") or normalized.get("cte_numero"),
            normalized.get("cte_numero"),
            normalized.get("nota_fiscal"),
            note,
            normalizar_nf(note),
            normalized.get("total_conhecimento"),
            normalized.get("peso_frete"),
            normalized.get("valor_kmm"),
            normalized.get("frete_unitario"),
            normalized.get("volume"),
            normalized.get("volume_normalizado"),
            normalized.get("mercadoria"),
            normalized.get("produto") or normalized.get("mercadoria"),
            normalized.get("produto_norm"),
            normalized.get("origem"),
            normalized.get("destino"),
            normalized.get("origem_norm"),
            normalized.get("destino_norm"),
            normalized.get("origem"),
            normalized.get("destino"),
            normalized.get("placa"),
            normalized.get("placa_norm"),
            normalized.get("base_icms"),
            normalized.get("valor_icms"),
            normalized.get("tabela_frete"),
            normalized.get("operacao_norm"),
            file_name,
            normalized.get("complemento_original"),
            normalized.get("observacao_original"),
            normalized.get("complemento_coluna_b_norm"),
            normalized.get("observacao_norm"),
            normalized.get("complemento_observacao_norm"),
            normalized.get("cte_complementar_norm"),
            normalized.get("motivo_identificacao_complemento"),
            normalized.get("chave_nfe"),
            imported_at,
            "ATIVO",
            f"kmm_fat_nf:{base_type}:{base_row_id}:{normalizar_nf(note)}",
        )
        for note in notes
    ]


def bulk_insert_kmm_fat_notes(conn, values: list[tuple]) -> None:
    if not values:
        return
    sql = """
        insert or replace into base_kmm_fat_notas_normalizadas (
            base_original_id, importacao_id, tipo_base, data_emissao, data_emissao_dt,
            cte, cte_numero, nota_fiscal_original, nota_fiscal_individual,
            nota_fiscal_norm, total_conhecimento, peso_frete, valor_faturado_kmm,
            frete_unitario, volume, volume_normalizado, mercadoria, produto,
            produto_norm, origem, destino, origem_norm, destino_norm,
            municipio_remetente, municipio_destinatario, placa, placa_norm,
            base_icms, valor_icms, tabela_frete, operacao_norm, arquivo_origem,
            complemento_original, observacao_original, complemento_coluna_b_norm,
            observacao_norm, complemento_observacao_norm, cte_complementar_norm,
            motivo_identificacao_complemento, chave_nfe, data_importacao,
            status_registro, signature
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for batch in chunks(values):
        conn.executemany(sql, batch)


def portal_ipp_values(base_row_id: int, import_id: int, normalized: dict[str, Any], file_name: str, imported_at: str) -> tuple | None:
    numero_norm = normalizar_nf(normalized.get("numero_norm") or normalized.get("nota_fiscal"))
    if not numero_norm:
        return None
    return (
        base_row_id,
        import_id,
        normalized.get("numero_original"),
        numero_norm,
        numero_norm,
        normalized.get("data_emissao_portal_dt"),
        normalized.get("vinculo"),
        normalized.get("canhoto"),
        normalized.get("pago"),
        normalized.get("vinculo_norm"),
        normalized.get("canhoto_norm"),
        normalized.get("pago_norm"),
        normalized.get("valor_portal_original"),
        normalized.get("valor_portal"),
        normalized.get("coluna_origem_valor_portal"),
        normalized.get("nome_coluna_aw_portal"),
        normalized.get("indice_coluna_valor_portal"),
        normalized.get("quantidade_colunas_portal"),
        normalized.get("valor_portal_frete"),
        normalized.get("valor_portal_pedagio"),
        normalized.get("valor_portal_base_calculo"),
        normalized.get("valor_portal_imposto"),
        normalized.get("valor_portal_total"),
        normalized.get("valor_unitario_frete"),
        normalized.get("tipo_frete"),
        normalized.get("tipo_frete_norm"),
        normalized.get("produto"),
        normalized.get("produto_norm"),
        normalized.get("qtd"),
        file_name,
        imported_at,
        "ATIVO",
        f"portal_ipp:{numero_norm}:{base_row_id}",
    )


def bulk_insert_portal_ipp(conn, values: list[tuple]) -> None:
    values = [value for value in values if value]
    if not values:
        return
    sql = """
        insert or replace into base_portal_ipp_normalizada (
            portal_original_id, importacao_id, numero_original, numero_norm,
            nota_fiscal_norm, data_emissao_portal_dt, vinculo, canhoto, pago,
            vinculo_norm, canhoto_norm, pago_norm, valor_portal_original,
            valor_portal, coluna_origem_valor_portal, nome_coluna_aw_portal,
            indice_coluna_valor_portal, quantidade_colunas_portal, valor_portal_frete,
            valor_portal_pedagio, valor_portal_base_calculo, valor_portal_imposto,
            valor_portal_total, valor_unitario_frete, tipo_frete, tipo_frete_norm,
            produto, produto_norm, quantidade, arquivo_origem, data_importacao,
            status_registro, signature
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for batch in chunks(values):
        conn.executemany(sql, batch)


def coupa_flow_values(base_row_id: int, import_id: int, normalized: dict[str, Any], file_name: str, imported_at: str) -> tuple:
    return (
        base_row_id,
        import_id,
        normalized.get("item"),
        normalized.get("qtd"),
        normalized.get("dt_inicio"),
        normalized.get("dt_termino"),
        normalized.get("origem"),
        normalized.get("destino"),
        normalized.get("distancia"),
        normalized.get("tipo_contratacao"),
        normalized.get("produto"),
        normalized.get("valor_bitrem"),
        normalized.get("valor_rodotrem"),
        normalized.get("resposta_transportador"),
        normalized.get("resposta_transportador_norm"),
        normalized.get("origem_coupa_tipo"),
        normalized.get("produto_normalizado"),
        normalized.get("dt_inicio_original"),
        normalized.get("dt_termino_original"),
        normalized.get("dt_inicio_dt"),
        normalized.get("dt_termino_dt"),
        normalized.get("dt_inicio_formatada"),
        normalized.get("dt_termino_formatada"),
        normalized.get("mes_referencia_coupa"),
        normalized.get("origem_norm"),
        normalized.get("destino_norm"),
        normalized.get("produto_norm"),
        normalized.get("tipo_contratacao_norm"),
        normalized.get("rota_norm"),
        normalized.get("arquivo"),
        normalized.get("reajuste"),
        file_name,
        imported_at,
    )


def bulk_insert_coupa_flows(conn, values: list[tuple]) -> None:
    if not values:
        return
    sql = """
        insert into base_coupa_fluxos_normalizados (
            coupa_original_id, importacao_id, item, qtd, dt_inicio, dt_termino,
            origem, destino, distancia, tipo_contratacao, produto, valor_bitrem,
            valor_rodotrem, resposta_transportador, resposta_transportador_norm,
            origem_coupa_tipo, produto_normalizado, dt_inicio_original, dt_termino_original,
            dt_inicio_dt, dt_termino_dt, dt_inicio_formatada, dt_termino_formatada,
            mes_referencia_coupa, origem_norm, destino_norm, produto_norm,
            tipo_contratacao_norm, rota_norm, arquivo, reajuste, arquivo_origem,
            data_importacao
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for batch in chunks(values):
        conn.executemany(sql, batch)


def prepare_rows(
    records: list[dict[str, Any]],
    base_type: str,
    import_id: int,
    filename: str,
    sheet_name: str,
    imported_at: str,
) -> tuple[list[dict[str, Any]], int]:
    prepared = []
    error_rows = 0
    seen_hashes: set[str] = set()
    for sequence, record in enumerate(records, start=1):
        try:
            normalized = normalize_row(record, base_type, filename)
            validate_normalized_row(base_type, normalized)
            content_hash = (
                km_content_hash(normalized)
                if base_type == KM_BASE_TYPE
                else hash_dict(normalize_row_for_hash(record, normalized))
            )
            key_hash = build_key_hash(base_type, normalized)
            dedupe_key = key_hash or content_hash
            if dedupe_key and dedupe_key in seen_hashes:
                error_rows += 1
                continue
            seen_hashes.add(dedupe_key)
            row_hash = versioned_row_hash(content_hash, import_id, sequence)
            prepared.append(
                {
                    "record": record,
                    "normalized": normalized,
                    "content_hash": content_hash,
                    "row_hash": row_hash,
                    "key_hash": key_hash,
                    "values": base_row_values(
                        import_id,
                        base_type,
                        filename,
                        sheet_name,
                        record,
                        normalized,
                        row_hash,
                        key_hash,
                        content_hash,
                        imported_at,
                    ),
                }
            )
        except Exception:
            error_rows += 1
    return prepared, error_rows


def import_prepared_rows_bulk(
    conn,
    table: str,
    import_id: int,
    base_type: str,
    filename: str,
    sheet_name: str,
    records: list[dict[str, Any]],
) -> tuple[int, int, int, int]:
    imported_at = brasilia_now_iso()
    prepared, error_rows = prepare_rows(records, base_type, import_id, filename, sheet_name, imported_at)
    if not prepared:
        return 0, 0, 0, error_rows

    key_hashes = {item["key_hash"] for item in prepared if item["key_hash"]}
    content_hashes = {item["content_hash"] for item in prepared if item["content_hash"]}
    existing_by_key = fetch_active_rows_by_column(conn, table, base_type, "key_hash", key_hashes)
    existing_by_content = fetch_active_rows_by_column(conn, table, base_type, "hash_registro", content_hashes)

    ids_to_replace: list[int] = []
    rows_to_insert: list[dict[str, Any]] = []
    updated_rows = duplicate_rows = 0
    for item in prepared:
        exact_existing = existing_by_content.get(item["content_hash"], [])
        if exact_existing:
            duplicate_rows += len(exact_existing)
            continue
        existing = existing_by_key.get(item["key_hash"], []) if item["key_hash"] else []
        if existing:
            ids_to_replace.extend(int(row["id"]) for row in existing)
            updated_rows += 1
            duplicate_rows += len(existing)
            if base_type == KM_BASE_TYPE:
                current = existing[-1]["km"]
                item["normalized"]["status_registro"] = (
                    "NOVO REGISTRO" if same_km(current, item["normalized"].get("km")) else "KM ATUALIZADO"
                )
                item["values"] = base_row_values(
                    import_id,
                    base_type,
                    filename,
                    sheet_name,
                    item["record"],
                    item["normalized"],
                    item["row_hash"],
                    item["key_hash"],
                    item["content_hash"],
                    imported_at,
                )
        elif base_type == KM_BASE_TYPE:
            item["normalized"]["status_registro"] = "NOVO REGISTRO"
            item["values"] = base_row_values(
                import_id,
                base_type,
                filename,
                sheet_name,
                item["record"],
                item["normalized"],
                item["row_hash"],
                item["key_hash"],
                item["content_hash"],
                    imported_at,
                )
        rows_to_insert.append(item)

    mark_rows_replaced_bulk(conn, table, ids_to_replace, base_type)
    bulk_insert_base_rows(conn, table, [item["values"] for item in rows_to_insert])

    if base_type in {*KMM_COMPAT_BASE_TYPES, "COUPA", "PORTAL IPP"}:
        id_by_hash = fetch_inserted_base_ids(conn, table, [item["row_hash"] for item in rows_to_insert])
        if base_type in KMM_COMPAT_BASE_TYPES:
            fat_note_rows: list[tuple] = []
            for item in rows_to_insert:
                base_row_id = id_by_hash.get(item["row_hash"])
                if base_row_id:
                    fat_note_rows.extend(kmm_fat_note_values(base_row_id, import_id, base_type, item["normalized"], filename, imported_at))
            bulk_insert_kmm_fat_notes(conn, fat_note_rows)
        if base_type in KMM_COMPAT_BASE_TYPES:
            note_rows: list[tuple] = []
            for item in rows_to_insert:
                base_row_id = id_by_hash.get(item["row_hash"])
                if base_row_id:
                    note_rows.extend(kmm_note_values(base_row_id, import_id, item["normalized"], filename, imported_at))
            bulk_insert_kmm_notes(conn, note_rows)
        elif base_type == "COUPA":
            flow_rows = [
                coupa_flow_values(id_by_hash[item["row_hash"]], import_id, item["normalized"], filename, imported_at)
                for item in rows_to_insert
                if item["row_hash"] in id_by_hash
            ]
            bulk_insert_coupa_flows(conn, flow_rows)
        elif base_type == "PORTAL IPP":
            portal_rows = [
                portal_ipp_values(id_by_hash[item["row_hash"]], import_id, item["normalized"], filename, imported_at)
                for item in rows_to_insert
                if item["row_hash"] in id_by_hash
            ]
            bulk_insert_portal_ipp(conn, portal_rows)

    return len(rows_to_insert), updated_rows, duplicate_rows, error_rows


def import_excel_file(
    file: BinaryIO,
    base_type: str,
    sheet_name: str,
    username: str = "",
    user_role: str = "",
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    if base_type == MANUAL_TOLL_BASE_TYPE:
        from src.vale_pedagio.service import import_manual_toll_update

        return import_manual_toll_update(file, sheet_name, username, user_role)

    requested_base_type = base_type
    base_type = canonical_base_type(base_type)
    table = canonical_table_for_base(base_type)
    filename = getattr(file, "name", "arquivo.xlsx")
    try:
        if progress_callback:
            progress_callback(0.10, "Lendo planilha...")
        if base_type == "PORTAL IPP":
            df, detected_sheet, portal_diagnostic = read_portal_ipp_sheet(file, sheet_name)
            sheet_name = detected_sheet
            aw_series = df.get(PORTAL_IPP_AW_INTERNAL_COLUMN)
            aw_numbers = [normalizar_valor_monetario(value) for value in aw_series.tolist()] if aw_series is not None else []
            aw_filled = [value for value in aw_numbers if value is not None]
            logger.info("Portal IPP: quantidade de colunas recebidas: %s", portal_diagnostic.get("quantidade_colunas"))
            logger.info("Portal IPP: coluna de valor identificada como %s", portal_diagnostic.get("valor_portal_aw_nome_coluna"))
            logger.info("Portal IPP: valores da coluna de valor preenchidos: %s", len(aw_filled))
            logger.info("Portal IPP: valores da coluna de valor vazios: %s", max(len(df) - len(aw_filled), 0))
            logger.info("Portal IPP: total valor_portal da coluna de valor: %s", sum(aw_filled) if aw_filled else 0)
            if not portal_diagnostic.get("numero_encontrado"):
                found = ", ".join(str(column) for column in portal_diagnostic.get("columns", []))
                raise ValueError(
                    "A coluna Numero nao foi localizada na planilha Portal IPP. "
                    f"Essa coluna e obrigatoria para validar as notas fiscais. Colunas encontradas: {found}"
                )
        else:
            df = read_excel_sheet(file, sheet_name)
            if base_type in KMM_COMPAT_BASE_TYPES:
                df = add_kmm_lcte_positional_columns(df)
                if normalize_column_name(sheet_name) != "dados":
                    raise ValueError("A aba Dados e obrigatoria para importar KMM/LCTE/FAT.")
                layout = validate_kmm_lcte_fat_layout(df)
                if not layout["ok"]:
                    missing = ", ".join(layout["missing"])
                    found = ", ".join(layout["columns"])
                    raise ValueError(
                        "O arquivo importado nao possui o layout esperado da base KMM/LCTE/FAT. "
                        f"Campos ausentes: {missing}. Colunas encontradas: {found}"
                    )
    except Exception as exc:
        with get_connection() as conn:
            import_id = insert_import_history(conn, filename, base_type, sheet_name, 0, username, user_role)
            update_import_history(conn, import_id, 0, 0, 0, 1, "ERRO", str(exc))
        return {
            "status": "ERRO",
            "error_message": "Nao foi possivel concluir a importacao. Verifique o arquivo e tente novamente.",
            "technical_error": str(exc),
            "new_rows": 0,
            "updated_rows": 0,
            "duplicate_rows": 0,
            "error_rows": 1,
        }

    if progress_callback:
        progress_callback(0.25, "Salvando arquivo e registrando historico...")
    saved_path = save_upload(file, filename)
    new_rows = updated_rows = duplicate_rows = error_rows = 0
    with get_connection() as conn:
        import_id = insert_import_history(conn, filename, base_type, sheet_name, len(df), username, user_role)
        insert_imported_file(conn, import_id, filename, base_type, saved_path, username)
        records = df.to_dict(orient="records")
        if base_type == "PORTAL IPP":
            nf_values = []
            nf_counts: dict[str, int] = {}
            aliases = aliases_for_base(base_type)
            for record in records:
                nf = normalizar_nf(get_first(record, aliases.get("numero_original", []) or aliases.get("nota_fiscal", [])))
                if nf:
                    nf_values.append(nf)
                    nf_counts[nf] = nf_counts.get(nf, 0) + 1
            logger.info("Portal IPP: quantidade de NFs normalizadas: %s", len(set(nf_values)))
            logger.info("Portal IPP: quantidade de duplicidades por NF: %s", sum(1 for count in nf_counts.values() if count > 1))
        if progress_callback:
            progress_callback(0.45, "Normalizando dados e consultando hashes existentes...")
        new_rows, updated_rows, duplicate_rows, error_rows = import_prepared_rows_bulk(
            conn,
            table,
            import_id,
            base_type,
            filename,
            sheet_name,
            records,
        )
        if progress_callback:
            progress_callback(0.90, "Atualizando historico da importacao...")
        if base_type == KM_BASE_TYPE:
            km_message = (
                f"NOVO REGISTRO: {new_rows - updated_rows}; "
                f"DUPLICADO SUBSTITUIDO: {duplicate_rows}; "
                f"KM ATUALIZADO: {updated_rows}; "
                f"KM INVALIDO/ERRO DE IMPORTACAO: {error_rows}"
            )
            update_import_history(conn, import_id, new_rows, updated_rows, duplicate_rows, error_rows, "SUCESSO", km_message)
        else:
            update_import_history(conn, import_id, new_rows, updated_rows, duplicate_rows, error_rows, "SUCESSO")
    if progress_callback:
        progress_callback(1.0, "Importacao concluida.")

    unique_ctes = normalized_notes = 0
    if base_type in KMM_COMPAT_BASE_TYPES:
        with get_connection() as conn:
            unique_ctes = conn.execute(
                """
                select count(distinct cte_numero)
                from base_kmm_notas_normalizadas
                where importacao_id = ?
                  and coalesce(status_registro, 'ATIVO') = 'ATIVO'
                """,
                (import_id,),
            ).fetchone()[0] or 0
            normalized_notes = conn.execute(
                """
                select count(distinct nota_fiscal_normalizada)
                from base_kmm_notas_normalizadas
                where importacao_id = ?
                  and coalesce(status_registro, 'ATIVO') = 'ATIVO'
                """,
                (import_id,),
            ).fetchone()[0] or 0

    return {
        "status": "SUCESSO",
        "saved_path": saved_path,
        "requested_base_type": requested_base_type,
        "base_type": base_type,
        "tipo_identificado": "KMM/LCTE/FAT" if base_type in KMM_COMPAT_BASE_TYPES else base_type,
        "arquivo_importado": filename,
        "records_read": int(len(df)),
        "unique_ctes": int(unique_ctes),
        "normalized_notes": int(normalized_notes),
        "new_rows": new_rows,
        "updated_rows": updated_rows,
        "duplicate_rows": duplicate_rows,
        "error_rows": error_rows,
        "error_message": "",
        "technical_error": "",
    }
