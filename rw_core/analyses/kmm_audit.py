from __future__ import annotations

import json

from src.config.settings import load_config
from rw_core.inconsistencies.service import upsert_inconsistency
from rw_core.normalizers.fields import get_first


def _diff(a, b) -> float:
    return abs((a or 0) - (b or 0))


def _json_payload(row, field: str) -> dict:
    try:
        return json.loads(row[field] or "{}")
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return {}


def _row_value(row, field: str):
    try:
        value = row[field]
        if value not in [None, ""]:
            return value
    except (IndexError, KeyError):
        pass
    normalized = _json_payload(row, "normalized_json")
    if normalized.get(field) not in [None, ""]:
        return normalized.get(field)
    aliases = load_config().get("column_aliases", {}).get("KMM / LCTE", {}).get(field, [])
    return get_first(_json_payload(row, "original_json"), aliases) if aliases else ""


def run(conn) -> set[str]:
    config = load_config()
    audit = config["audit"]
    tolerance = config["tolerances"]["value"]
    conn.execute("delete from analise_auditoria_kmm")
    active: set[str] = set()
    rows = conn.execute(
        """
        select * from base_kmm_original
        where coalesce(status_registro, 'ATIVO') = 'ATIVO'
          and coalesce(status_complemento_normalizado, '') <> 'COMPLEMENTAR'
          and upper(trim(coalesce(complemento, ''))) <> 'SIM'
        """
    ).fetchall()
    for row in rows:
        errors: list[str] = []
        plate = row["placa"] or ""
        volume = row["volume"] or 0
        cst = str(row["id_icms_st"] or "").strip()
        cfop = str(row["cfop"] or "").strip()
        total = row["total_conhecimento"] or 0
        base_icms = row["base_icms"] or 0
        base_st = row["base_icms_st"] or 0
        valor_st = row["valor_icms_st"] or 0

        if volume < audit["volume_min"] and plate not in audit["volume_exception_plates"]:
            errors.append("VOLUME INCORRETO")
        if cst in {str(value) for value in audit["st_error_values"]}:
            errors.append("ERRO DE ST")
        if cst in audit["icms_cst_00_90"] and _diff(total, base_icms) > tolerance:
            errors.append("ERRO ICMS")
        if cst in audit["icms_cst_40"] and _diff(base_icms, 0) > tolerance:
            errors.append("ERRO ICMS")
        if cst in audit["icms_cst_60"] and _diff(total, base_st - valor_st) > tolerance:
            errors.append("ERRO ICMS")
        if cfop and cfop not in {str(value) for value in audit["valid_cfop"]}:
            errors.append("ERRO CFOP")

        result = "OK" if not errors else " | ".join(dict.fromkeys(errors))
        signature = f"audit_kmm:{row['id']}"
        conn.execute(
            """
            insert or replace into analise_auditoria_kmm (
                kmm_id, arquivo_origem, emissao_cte, cte_numero, notas_fiscais, placa, volume,
                id_icms_st, cfop, total_conhecimento, base_icms, valor_icms,
                base_icms_st, valor_icms_st, resultado, lista_erros, cobranca,
                emitente, destinatario, uf_origem, uf_destino, tabela_frete, signature
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["arquivo_origem"],
                row["emissao_cte"],
                row["cte_numero"],
                row["nota_fiscal"],
                plate,
                volume,
                cst,
                cfop,
                total,
                base_icms,
                row["valor_icms"],
                base_st,
                valor_st,
                result,
                " | ".join(errors),
                _row_value(row, "cobranca"),
                _row_value(row, "emitente"),
                _row_value(row, "destinatario"),
                _row_value(row, "uf_origem"),
                _row_value(row, "uf_destino"),
                _row_value(row, "tabela_frete"),
                signature,
            ),
        )
        for error in dict.fromkeys(errors):
            active.add(
                upsert_inconsistency(
                    conn,
                    {
                        "signature": f"inc:audit_kmm:{error}:{row['id']}",
                        "modulo_origem": "Auditoria CT-e KMM",
                        "tipo_inconsistencia": error.title(),
                        "cliente": row["cliente"],
                        "placa": plate,
                        "nota_fiscal": row["nota_fiscal"],
                        "cte": row["cte_numero"],
                        "origem": row["origem"],
                        "destino": row["destino"],
                        "cobranca": row["cobranca"],
                        "valor_envolvido": total,
                        "volume_envolvido": volume,
                        "observacao": result,
                        "arquivo_origem": row["arquivo_origem"],
                    },
                )
            )
    return active
