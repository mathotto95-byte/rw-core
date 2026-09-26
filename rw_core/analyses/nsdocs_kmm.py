from __future__ import annotations

import json

from src.config.settings import load_config
from rw_core.inconsistencies.service import upsert_inconsistency
from rw_core.normalizers.fields import get_first, normalize_document_number, split_nf_list


def _normalized_value(row, field: str) -> str:
    try:
        value = row[field]
        if value:
            return value
    except (IndexError, KeyError):
        pass
    try:
        normalized = json.loads(row["normalized_json"] or "{}")
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return ""
    return normalized.get(field) or ""


def _json_payload(row, field: str) -> dict:
    try:
        return json.loads(row[field] or "{}")
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return {}


def _row_value(row, field: str):
    try:
        return row[field]
    except (IndexError, KeyError):
        return _normalized_value(row, field)


def _kmm_note_list(row) -> list[str]:
    normalized = _json_payload(row, "normalized_json")
    stored_notes = normalized.get("notas_fiscais_individuais")
    if isinstance(stored_notes, list):
        notes = [normalize_document_number(note) for note in stored_notes]
        notes = [note for note in notes if note]
        if notes:
            return list(dict.fromkeys(notes))
    if isinstance(stored_notes, str):
        notes = split_nf_list(stored_notes)
        if notes:
            return notes

    original = _json_payload(row, "original_json")
    aliases = load_config().get("column_aliases", {}).get("KMM / LCTE", {}).get("nota_fiscal", [])
    notes = split_nf_list(get_first(original, aliases))
    if notes:
        return notes
    return split_nf_list(_row_value(row, "nota_fiscal"))


# Campos do KMM que sao os mesmos para todas as notas de uma linha (a lista
# nao inclui "nota_fiscal_normalizada" pois esse valor muda por nota).
_KMM_NOTE_FIELDS = (
    "importacao_id", "cte_numero", "nota_fiscal", "chave_cte", "chave_nfe",
    "emissao_nf", "emissao_cte", "situacao", "complemento",
    "status_complemento_normalizado", "placa", "motorista", "cliente",
    "cobranca", "uf_origem", "origem", "uf_destino", "destino", "mercadoria",
    "volume", "total_conhecimento", "frete_unitario", "peso_frete", "pedagio",
    "vale_pedagio", "id_icms_st", "cfop", "base_icms", "valor_icms",
    "base_icms_st", "valor_icms_st", "inserido_por", "tabela_frete",
    "arquivo_origem", "data_importacao", "status_registro",
)


def sync_kmm_notes_normalized(conn) -> None:
    rows = conn.execute(
        """
        select * from base_kmm_original
        where coalesce(status_registro, 'ATIVO') = 'ATIVO'
        """
    ).fetchall()
    payload = []
    for row in rows:
        notes = _kmm_note_list(row)
        if not notes:
            continue
        # Calcula os campos do KMM uma unica vez por linha (nao por nota).
        # Antes, uma linha com N notas recalculava os mesmos 35+ campos N
        # vezes, incluindo o fallback de parsing de normalized_json/original_json.
        base = {field: _row_value(row, field) for field in _KMM_NOTE_FIELDS}
        status_registro = base["status_registro"] or "ATIVO"
        for note in notes:
            payload.append((
                row["id"],
                base["importacao_id"],
                base["cte_numero"],
                base["nota_fiscal"],
                note,
                base["chave_cte"],
                base["chave_nfe"],
                base["emissao_nf"],
                base["emissao_cte"],
                base["situacao"],
                base["complemento"],
                base["status_complemento_normalizado"],
                base["placa"],
                base["motorista"],
                base["cliente"],
                base["cobranca"],
                base["uf_origem"],
                base["origem"],
                base["uf_destino"],
                base["destino"],
                base["mercadoria"],
                base["volume"],
                base["total_conhecimento"],
                base["frete_unitario"],
                base["total_conhecimento"],
                base["peso_frete"],
                base["pedagio"],
                base["vale_pedagio"],
                base["id_icms_st"],
                base["cfop"],
                base["base_icms"],
                base["valor_icms"],
                base["base_icms_st"],
                base["valor_icms_st"],
                base["inserido_por"],
                base["tabela_frete"],
                base["arquivo_origem"],
                base["data_importacao"],
                status_registro,
            ))
    if not payload:
        return
    conn.executemany(
        """
        insert or ignore into base_kmm_notas_normalizadas (
            kmm_original_id, importacao_id, cte_numero, nota_fiscal, nota_fiscal_normalizada,
            chave_cte, chave_nfe, emissao_nf, emissao_cte, situacao, complemento,
            status_complemento_normalizado, placa, motorista, cliente, cobranca, uf_origem, origem,
            uf_destino, destino, mercadoria, volume, valor, frete_unitario, total_conhecimento,
            peso_frete, pedagio, vale_pedagio, id_icms_st, cfop, base_icms, valor_icms,
            base_icms_st, valor_icms_st, inserido_por, tabela_frete, arquivo_origem,
            data_importacao, status_registro
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )


def run(conn) -> set[str]:
    config = load_config()
    rodo_wall = config["rodo_wall_cnpj"]
    sync_kmm_notes_normalized(conn)
    conn.execute("delete from analise_nsdocs_x_kmm")
    active: set[str] = set()
    nsdocs_rows = conn.execute(
        "select * from base_nsdocs_original where coalesce(status_registro, 'ATIVO') = 'ATIVO'"
    ).fetchall()
    for row in nsdocs_rows:
        nf = normalize_document_number(row["numero_nf"] or row["nota_fiscal"])
        cnpj = row["transportador_cnpj"] or ""
        if cnpj != rodo_wall:
            continue
        kmm = conn.execute(
            """
            select * from base_kmm_notas_normalizadas
            where nota_fiscal_normalizada = ? and status_complemento_normalizado = 'NORMAL'
              and coalesce(status_registro, 'ATIVO') = 'ATIVO'
            order by id limit 1
            """,
            (nf,),
        ).fetchone()
        kmm_complement = conn.execute(
            """
            select * from base_kmm_notas_normalizadas
            where nota_fiscal_normalizada = ? and status_complemento_normalizado = 'COMPLEMENTAR'
              and coalesce(status_registro, 'ATIVO') = 'ATIVO'
            order by id limit 1
            """,
            (nf,),
        ).fetchone()
        if not row["emissao_nf"]:
            status = "NF COM EMISSAO ZERADA/INVALIDA"
            observation = "Emissao da NF ausente ou invalida."
        elif kmm:
            status = "EMITIDA"
            observation = ""
        elif kmm_complement:
            status = "FORA DA REGRA - COMPLEMENTO"
            observation = "NF encontrada apenas em CT-e complementar."
        else:
            status = "NAO EMITIDA"
            observation = "NF NSDOCS considerada e nao localizada na base KMM."

        match = kmm or kmm_complement
        natureza_operacao = _normalized_value(row, "natureza_operacao")
        signature = f"nsdocs_kmm:{row['id']}:{nf}:{status}"
        conn.execute(
            """
            insert or replace into analise_nsdocs_x_kmm (
                nsdocs_id, numero_nf, chave_acesso, emitente, destinatario,
                natureza_operacao, transportador_cnpj, valor_nf, emissao_nf,
                placa_nsdocs, volumes_nsdocs, cte_kmm, emissao_cte, placa_kmm,
                volume_kmm, cobranca_kmm, origem_kmm, destino_kmm, inserido_por,
                status_analise, observacao, arquivo_origem, data_importacao, signature
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                nf,
                row["chave_acesso"],
                row["emitente"],
                row["destinatario"],
                natureza_operacao,
                cnpj,
                row["valor"],
                row["emissao_nf"],
                row["placa"],
                row["volume"],
                match["cte_numero"] if match else None,
                match["emissao_cte"] if match else None,
                match["placa"] if match else None,
                match["volume"] if match else None,
                match["cobranca"] if match else None,
                match["origem"] if match else None,
                match["destino"] if match else None,
                match["inserido_por"] if match else None,
                status,
                observation,
                row["arquivo_origem"],
                row["data_importacao"],
                signature,
            ),
        )
        if status == "NAO EMITIDA":
            active.add(
                upsert_inconsistency(
                    conn,
                    {
                        "signature": f"inc:nsdocs_kmm:nao_emitida:{nf}",
                        "modulo_origem": "NSDOCS x KMM",
                        "tipo_inconsistencia": "Nota fiscal nao encontrada no KMM",
                        "nota_fiscal": nf,
                        "placa": row["placa"],
                        "valor_envolvido": row["valor"],
                        "volume_envolvido": row["volume"],
                        "prioridade": "ALTA",
                        "observacao": observation,
                        "arquivo_origem": row["arquivo_origem"],
                    },
                )
            )
        elif status == "NF COM EMISSAO ZERADA/INVALIDA":
            active.add(
                upsert_inconsistency(
                    conn,
                    {
                        "signature": f"inc:nsdocs_kmm:emissao_invalida:{nf}:{row['id']}",
                        "modulo_origem": "NSDOCS x KMM",
                        "tipo_inconsistencia": "NF com emissao zerada ou invalida",
                        "nota_fiscal": nf,
                        "placa": row["placa"],
                        "prioridade": "BAIXA",
                        "observacao": observation,
                        "arquivo_origem": row["arquivo_origem"],
                    },
                )
            )
    return active
