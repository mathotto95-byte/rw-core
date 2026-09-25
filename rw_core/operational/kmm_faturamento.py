from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any

from rw_core.normalizers.fields import normalizar_nf, normalizar_texto_match, normalize_text, parse_number
from rw_core.utils.cobranca_ipiranga import normalizar_texto_empresa


COMPLEMENT_OBSERVATION_TERMS = {
    "COMPLEMENTO DE VALOR",
    "COMPLEMENTO VALOR",
    "COMPL DE VALOR",
    "COMPL. DE VALOR",
    "COMPLEMENTAR DE VALOR",
    "CTE COMPLEMENTAR",
    "CT-E COMPLEMENTAR",
    "CONHECIMENTO COMPLEMENTAR",
    "COMPLEMENTO FRETE",
    "COMPLEMENTO DE FRETE",
    "DIFERENCA DE FRETE",
    "DIFERENCA VALOR",
    "VALOR COMPLEMENTAR",
}


def _value(row: Any, field: str, default: Any = "") -> Any:
    try:
        value = row[field]
    except (IndexError, KeyError, TypeError):
        value = default
    return default if value is None else value


def normalize_complemento_coluna_b(value: Any) -> str:
    text = normalizar_texto_match(value)
    if text in {"SIM", "S", "TRUE", "1"}:
        return "SIM"
    return "NAO"


def normalize_observacao(value: Any) -> str:
    return " ".join(normalizar_texto_match(value).split())


def normalize_complemento_observacao(value: Any) -> str:
    observation = normalize_observacao(value)
    if any(term in observation for term in COMPLEMENT_OBSERVATION_TERMS):
        return "SIM"
    return "NAO"


def motivo_identificacao_complemento(complemento_b: str, complemento_obs: str) -> str:
    if complemento_b == "SIM" and complemento_obs == "SIM":
        return "COLUNA B + OBSERVACAO"
    if complemento_b == "SIM":
        return "COLUNA B COMPLEMENTO = SIM"
    if complemento_obs == "SIM":
        return "OBSERVACAO INDICA COMPLEMENTO DE VALOR"
    return "NAO E COMPLEMENTO"


def classify_kmm_complement_fields(complemento: Any, observacao: Any) -> dict[str, str]:
    complemento_original = "" if complemento is None else str(complemento)
    observacao_original = "" if observacao is None else str(observacao)
    complemento_coluna_b_norm = normalize_complemento_coluna_b(complemento_original)
    observacao_norm = normalize_observacao(observacao_original)
    complemento_observacao_norm = normalize_complemento_observacao(observacao_original)
    cte_complementar_norm = "SIM" if "SIM" in {complemento_coluna_b_norm, complemento_observacao_norm} else "NAO"
    motivo = motivo_identificacao_complemento(complemento_coluna_b_norm, complemento_observacao_norm)
    return {
        "complemento_original": complemento_original,
        "observacao_original": observacao_original,
        "complemento_coluna_b_norm": complemento_coluna_b_norm,
        "observacao_norm": observacao_norm,
        "complemento_observacao_norm": complemento_observacao_norm,
        "cte_complementar_norm": cte_complementar_norm,
        "motivo_identificacao_complemento": motivo,
        "status_complemento_normalizado": "COMPLEMENTAR" if cte_complementar_norm == "SIM" else "NORMAL",
    }


def kmm_financial_value(row: Any) -> float | None:
    for field in ("peso_frete", "total_conhecimento", "valor_total_cte_kmm", "valor_total_cte", "valor_faturado_kmm", "valor_kmm", "valor"):
        value = parse_number(_value(row, field))
        if value is not None:
            return value
    return None


def _text(row: Any, *fields: str) -> str:
    for field in fields:
        value = str(_value(row, field, "") or "").strip()
        if value:
            return value
    json_payload = _json_payload(row)
    if json_payload:
        lookup = {_json_key(key): value for key, value in json_payload.items()}
        for field in fields:
            value = lookup.get(_json_key(field))
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _json_key(value: Any) -> str:
    return "".join(char if char.isalnum() else "_" for char in normalizar_texto_match(value).lower()).strip("_")


def _json_payload(row: Any) -> dict[str, Any]:
    raw = str(_value(row, "dados_json", "") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _cte(row: Any) -> str:
    return _text(row, "cte", "cte_numero", "numero_conhecimento")


def _nf(row: Any) -> str:
    return normalizar_nf(_value(row, "nota_fiscal_norm") or _value(row, "nota_fiscal_normalizada") or _value(row, "nota_fiscal_individual") or _value(row, "nota_fiscal"))


def _route_product_key(row: Any) -> tuple[str, str, str]:
    origem = _text(row, "origem_norm", "origem_normalizada", "local_coleta_norm")
    origem = origem or normalizar_texto_match(_text(row, "origem", "municipio_remetente", "local_coleta", "local_da_coleta"))
    destino = _text(row, "destino_norm", "destino_normalizada", "local_entrega_norm")
    destino = destino or normalizar_texto_match(_text(row, "destino", "municipio_destinatario", "local_entrega", "local_da_entrega"))
    produto = _text(row, "produto_norm", "produto_normalizado", "produto_lcte_norm")
    produto = produto or normalizar_texto_match(_text(row, "produto", "mercadoria", "produto_lcte"))
    return origem, destino, produto


def chave_faturamento_coupa(row: Any) -> str:
    nf = _nf(row)
    chave_nfe = _text(row, "chave_nfe", "chave_nfe_relacionada", "chave_da_nfe_relacionada_no_cte")
    origem, destino, produto = _route_product_key(row)
    base = nf or chave_nfe
    return "|".join([base, origem, destino, produto])


def _source_rows(conn) -> tuple[list[Any], str]:
    try:
        rows = conn.execute(
            """
            select *
            from mod_faturamento_coupa_lcte_normalizada
            where coalesce(nota_fiscal_norm, '') <> ''
            order by id
            """
        ).fetchall()
    except Exception:
        rows = []
    if rows:
        return rows, "mod_faturamento_coupa_lcte_normalizada"
    try:
        rows = conn.execute(
            """
            select *
            from base_kmm_fat_notas_normalizadas
            where coalesce(status_registro, 'ATIVO') = 'ATIVO'
              and coalesce(nota_fiscal_norm, '') <> ''
            order by id
            """
        ).fetchall()
    except Exception:
        rows = []
    if rows:
        return rows, "base_kmm_fat_notas_normalizadas"
    rows = conn.execute(
        """
        select *
        from base_kmm_original
        where coalesce(status_registro, 'ATIVO') = 'ATIVO'
          and coalesce(cobranca, cliente, '') <> ''
        order by id
        """
    ).fetchall()
    if rows:
        return rows, "base_kmm_original"
    try:
        fallback = conn.execute(
            """
            select *
            from base_fat_kmm_original
            where coalesce(status_registro, 'ATIVO') = 'ATIVO'
              and coalesce(cobranca, cliente, '') <> ''
            order by id
            """
        ).fetchall()
    except Exception:
        fallback = []
    return fallback, "base_fat_kmm_original" if fallback else "base_kmm_original"


def _append_unique(values: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


def _sum_unique_cte(rows: list[Any], complementary: bool, amount_field: str | None = None) -> float:
    seen: set[str] = set()
    total = 0.0
    for row in rows:
        if (_value(row, "cte_complementar_norm") == "SIM") != complementary:
            continue
        cte = _cte(row) or f"row:{_value(row, 'id', len(seen))}"
        if cte in seen:
            continue
        seen.add(cte)
        value = _amount_value(row, amount_field) if amount_field else kmm_financial_value(row)
        if value is not None:
            total += value
    return total


def _amount_value(row: Any, amount_field: str | None) -> float | None:
    if not amount_field:
        return kmm_financial_value(row)
    aliases = {
        "total_conhecimento": ["total_conhecimento", "total_do_conhec", "valor_total_cte", "valor_cte"],
        "peso_frete": ["peso_frete", "valor_faturado_kmm", "valor_total_cte", "valor_cte"],
    }
    for field in aliases.get(amount_field, [amount_field]):
        value = parse_number(_value(row, field))
        if value is None:
            value = parse_number(_text(row, field))
        if value is not None:
            return value
    return None


def _first_non_complement(rows: list[Any]) -> Any:
    for row in rows:
        if _value(row, "cte_complementar_norm") != "SIM":
            return row
    return rows[0]


def rebuild_base_kmm_faturamento_consolidado(conn) -> int:
    rows, source_table = _source_rows(conn)
    conn.execute("delete from base_kmm_faturamento_consolidado")
    if not rows:
        return 0

    normalized_rows: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        fields = classify_kmm_complement_fields(_value(raw, "complemento"), _value(raw, "observacao"))
        item.update(fields)
        item["chave_faturamento_coupa"] = chave_faturamento_coupa(item)
        item["nota_fiscal_norm"] = _nf(item)
        normalized_rows.append(item)

    cte_nfs: dict[str, set[str]] = {}
    for row in normalized_rows:
        cte = _cte(row)
        nf = str(row.get("nota_fiscal_norm") or "").strip()
        if cte and nf:
            cte_nfs.setdefault(cte, set()).add(nf)

    groups: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for row in normalized_rows:
        key = row.get("chave_faturamento_coupa") or f"cte:{_cte(row)}"
        groups.setdefault(key, []).append(row)

    columns = [
        "chave_faturamento_coupa", "nota_fiscal_norm", "nota_fiscal", "nf_original", "chave_nfe",
        "cte", "cte_numero", "cte_normal", "ctes_complementares", "ctes_todos",
        "cliente", "cobranca", "cliente_norm", "razao_social_cobranca",
        "razao_social_cobranca_norm", "cliente_equivalente", "cliente_equivalente_norm",
        "placa", "motorista", "origem", "destino",
        "origem_norm", "destino_norm", "municipio_remetente", "municipio_destinatario",
        "produto", "mercadoria", "produto_norm", "operacao", "operacao_norm",
        "data_emissao_nf", "data_emissao", "data_emissao_dt", "data_emissao_cte_normal",
        "data_primeiro_cte", "data_ultimo_cte", "emissao_cte",
        "valor_cte_normal", "valor_cte_complementar", "valor_faturado_total_kmm",
        "valor", "valor_kmm", "valor_faturado_kmm", "peso_frete", "peso_frete_normal",
        "peso_frete_complementar", "peso_frete_total", "total_conhecimento",
        "total_conhec_normal", "total_conhec_complementar", "total_conhec_total",
        "frete_unitario", "volume", "volume_normalizado", "tem_complemento",
        "quantidade_complementos", "status_complemento_cte", "status_complemento_normalizado",
        "complemento", "observacao", "complemento_original", "observacao_original",
        "complemento_coluna_b_norm", "observacao_norm", "complemento_observacao_norm",
        "cte_complementar_norm", "motivo_identificacao_complemento", "arquivo_origem",
        "lote_importacao", "tipo_base", "source_table", "source_ids", "signature",
        "status_registro",
    ]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"insert or replace into base_kmm_faturamento_consolidado ({', '.join(columns)}) values ({placeholders})"

    inserted = 0
    for key, group in groups.items():
        normal_rows = [row for row in group if row.get("cte_complementar_norm") != "SIM"]
        comp_rows = [row for row in group if row.get("cte_complementar_norm") == "SIM"]
        base = _first_non_complement(group)
        normal_ctes: list[str] = []
        comp_ctes: list[str] = []
        all_ctes: list[str] = []
        motives: list[str] = []
        complemento_original: list[str] = []
        observacao_original: list[str] = []
        files: list[str] = []
        source_ids: list[str] = []
        for row in group:
            cte = _cte(row)
            _append_unique(all_ctes, cte)
            _append_unique(files, _value(row, "arquivo_origem"))
            _append_unique(source_ids, _value(row, "id"))
            _append_unique(motives, _value(row, "motivo_identificacao_complemento"))
            _append_unique(complemento_original, _value(row, "complemento_original") or _value(row, "complemento"))
            _append_unique(observacao_original, _value(row, "observacao_original") or _value(row, "observacao"))
            if row.get("cte_complementar_norm") == "SIM":
                _append_unique(comp_ctes, cte)
            else:
                _append_unique(normal_ctes, cte)
        valor_normal = _sum_unique_cte(group, complementary=False)
        valor_complementar = _sum_unique_cte(group, complementary=True)
        peso_normal = _sum_unique_cte(group, complementary=False, amount_field="peso_frete")
        peso_complementar = _sum_unique_cte(group, complementary=True, amount_field="peso_frete")
        total_normal = _sum_unique_cte(group, complementary=False, amount_field="total_conhecimento")
        total_complementar = _sum_unique_cte(group, complementary=True, amount_field="total_conhecimento")
        total = valor_normal + valor_complementar
        has_unrated_cte = any(len(cte_nfs.get(cte, set())) > 1 for cte in all_ctes)
        if has_unrated_cte:
            status_complemento = "VALOR CT-E NAO RATEADO POR NF"
        else:
            status_complemento = "COMPLEMENTO SEM CT-E NORMAL" if comp_rows and not normal_rows else ("COM COMPLEMENTO" if comp_rows else "SEM COMPLEMENTO")
        cte_normal = " / ".join(normal_ctes)
        ctes_complementares = " / ".join(comp_ctes)
        date_fields = ("data_emissao_dt", "data_emissao_lcte", "data_emissao_cte", "data_hora_cte", "data_emissao", "emissao_cte")
        first_date = min([str(_text(row, *date_fields)) for row in group if _text(row, *date_fields)] or [""])
        last_date = max([str(_text(row, *date_fields)) for row in group if _text(row, *date_fields)] or [""])
        cliente = _text(base, "cliente", "cliente_equivalente", "razao_social_cliente", "razao_social_do_cliente")
        cobranca = _text(
            base,
            "cobranca",
            "razao_social_cobranca",
            "razao_social_da_cobranca",
            "razao_social_cobranca_cliente",
            "razao_social_da_cobranca_cliente",
            "cliente",
        )
        cliente_equivalente = cliente or cobranca
        payload = {
            "chave_faturamento_coupa": key,
            "nota_fiscal_norm": _nf(base),
            "nota_fiscal": _text(base, "nota_fiscal_individual", "nota_fiscal", "nota_fiscal_normalizada"),
            "nf_original": _text(base, "nota_fiscal_original", "nota_fiscal", "nota_fiscal_individual"),
            "chave_nfe": _text(base, "chave_nfe"),
            "cte": cte_normal or ctes_complementares,
            "cte_numero": cte_normal or ctes_complementares,
            "cte_normal": cte_normal,
            "ctes_complementares": ctes_complementares,
            "ctes_todos": " / ".join(all_ctes),
            "cliente": cliente,
            "cobranca": cobranca,
            "cliente_norm": _text(base, "cliente_norm") or normalizar_texto_match(cliente or cobranca),
            "razao_social_cobranca": cobranca,
            "razao_social_cobranca_norm": normalizar_texto_empresa(cobranca),
            "cliente_equivalente": cliente_equivalente,
            "cliente_equivalente_norm": normalizar_texto_empresa(cliente_equivalente),
            "placa": _text(base, "placa"),
            "motorista": _text(base, "motorista"),
            "origem": _text(base, "origem", "municipio_remetente", "local_coleta", "local_da_coleta"),
            "destino": _text(base, "destino", "municipio_destinatario", "local_entrega", "local_da_entrega"),
            "origem_norm": _text(base, "origem_norm", "origem_normalizada", "local_coleta_norm") or normalizar_texto_match(_text(base, "origem", "municipio_remetente", "local_coleta", "local_da_coleta")),
            "destino_norm": _text(base, "destino_norm", "destino_normalizada", "local_entrega_norm") or normalizar_texto_match(_text(base, "destino", "municipio_destinatario", "local_entrega", "local_da_entrega")),
            "municipio_remetente": _text(base, "municipio_remetente", "origem", "local_coleta", "local_da_coleta"),
            "municipio_destinatario": _text(base, "municipio_destinatario", "destino", "local_entrega", "local_da_entrega"),
            "produto": _text(base, "produto", "mercadoria", "produto_lcte"),
            "mercadoria": _text(base, "mercadoria", "produto", "produto_lcte"),
            "produto_norm": _text(base, "produto_norm", "produto_normalizado", "produto_lcte_norm"),
            "operacao": _text(base, "operacao", "operacao_norm", "tabela_frete"),
            "operacao_norm": _text(base, "operacao_norm", "tabela_frete"),
            "data_emissao_nf": _text(base, "data_emissao_nf_dt", "data_hora_nf", "emissao_nf", "data_emissao_nf"),
            "data_emissao": _text(base, *date_fields),
            "data_emissao_dt": _text(base, *date_fields),
            "data_emissao_cte_normal": _text(normal_rows[0], *date_fields) if normal_rows else "",
            "data_primeiro_cte": first_date,
            "data_ultimo_cte": last_date,
            "emissao_cte": _text(base, *date_fields),
            "valor_cte_normal": valor_normal,
            "valor_cte_complementar": valor_complementar,
            "valor_faturado_total_kmm": total,
            "valor": total,
            "valor_kmm": total,
            "valor_faturado_kmm": total,
            "peso_frete": peso_normal + peso_complementar,
            "peso_frete_normal": peso_normal,
            "peso_frete_complementar": peso_complementar,
            "peso_frete_total": peso_normal + peso_complementar,
            "total_conhecimento": total_normal + total_complementar,
            "total_conhec_normal": total_normal,
            "total_conhec_complementar": total_complementar,
            "total_conhec_total": total_normal + total_complementar,
            "frete_unitario": parse_number(_value(base, "frete_unitario")),
            "volume": parse_number(_value(base, "volume") or _value(base, "volume_litros") or _value(base, "volume_lcte")),
            "volume_normalizado": parse_number(_value(base, "volume_normalizado") or _value(base, "volume") or _value(base, "volume_litros") or _value(base, "volume_lcte")),
            "tem_complemento": "SIM" if comp_rows else "NAO",
            "quantidade_complementos": len(set(comp_ctes)),
            "status_complemento_cte": status_complemento,
            "status_complemento_normalizado": "NORMAL",
            "complemento": " / ".join(complemento_original),
            "observacao": " / ".join(observacao_original),
            "complemento_original": " / ".join(complemento_original),
            "observacao_original": " / ".join(observacao_original),
            "complemento_coluna_b_norm": "SIM" if any(row.get("complemento_coluna_b_norm") == "SIM" for row in group) else "NAO",
            "observacao_norm": " / ".join([row.get("observacao_norm", "") for row in group if row.get("observacao_norm")]),
            "complemento_observacao_norm": "SIM" if any(row.get("complemento_observacao_norm") == "SIM" for row in group) else "NAO",
            "cte_complementar_norm": "NAO",
            "motivo_identificacao_complemento": " / ".join(motives),
            "arquivo_origem": " / ".join(files),
            "lote_importacao": _text(base, "lote_importacao", "importacao_id"),
            "tipo_base": _text(base, "tipo_base", "tipo_base_origem"),
            "source_table": source_table,
            "source_ids": " / ".join(source_ids),
            "signature": f"kmm_faturamento_consolidado:{key}",
            "status_registro": "ATIVO",
        }
        conn.execute(sql, tuple(payload.get(column) for column in columns))
        inserted += 1
    return inserted


def carregar_base_kmm_faturamento(conn, somente_normal: bool = True) -> tuple[list[Any], str]:
    """
    Carrega a base unificada KMM/LCTE/FAT.
    Para comparacoes financeiras, usa a consolidacao normal + complementar.
    """
    if somente_normal:
        try:
            rebuild_base_kmm_faturamento_consolidado(conn)
            rows = conn.execute(
                """
                select *
                from base_kmm_faturamento_consolidado
                where coalesce(nota_fiscal_norm, '') <> ''
                order by id
                """
            ).fetchall()
            if rows:
                return rows, "base_kmm_faturamento_consolidado"
        except Exception:
            pass
    rows, table = _source_rows(conn)
    if somente_normal:
        rows = [row for row in rows if _value(row, "status_complemento_normalizado", "NORMAL") == "NORMAL"]
    return rows, table
