from __future__ import annotations

import json
from typing import Any

import pandas as pd

from rw_core.database.connection import get_connection, read_sql
from rw_core.modules.repository import latest_import_logs, read_preferred_table, table_count
from rw_core.normalizers.fields import normalize_cnpj, normalize_text
from rw_core.utils.timezone import brasilia_now_iso


FATURAMENTO_SOURCES = {
    "Coupa": ("mod_faturamento_coupa_normalizada", "base_coupa_fluxos_normalizados"),
    "NSDOCS": ("mod_faturamento_nsdocs_normalizada", "base_nsdocs_original"),
    "LCTE": ("mod_faturamento_lcte_notas_normalizadas", "base_kmm_fat_notas_normalizadas"),
}


def source_counts() -> list[dict[str, str | int]]:
    rows = []
    for label, (modular_table, legacy_table) in FATURAMENTO_SOURCES.items():
        modular_count = table_count(modular_table)
        legacy_count = table_count(legacy_table)
        rows.append(
            {
                "Fonte": label,
                "Tabela modular": modular_table,
                "Registros modulares": modular_count,
                "Tabela antiga": legacy_table,
                "Registros antigos": legacy_count,
                "Modo": "MODULAR" if modular_count else ("FALLBACK ANTIGO" if legacy_count else "SEM DADOS"),
            }
        )
    return rows


def read_source(label: str, limit: int = 500):
    modular_table, legacy_table = FATURAMENTO_SOURCES[label]
    return read_preferred_table(modular_table, legacy_table, limit)


def import_logs(limit: int = 20):
    return latest_import_logs("mod_faturamento_logs_importacao", limit)


def de_para_coupa(search: str = "", include_inactive: bool = True) -> pd.DataFrame:
    where = []
    params: list[Any] = []
    if not include_inactive:
        where.append("ativo = 1")
    if search.strip():
        where.append("(cnpj_norm like ? or razao_social_lcte_norm like ? or nomenclatura_coupa_norm like ?)")
        value = f"%{normalize_text(search)}%"
        cnpj_value = f"%{normalize_cnpj(search)}%"
        params.extend([cnpj_value, value, value])
    where_sql = f"where {' and '.join(where)}" if where else ""
    return read_sql(
        f"""
        select id, cnpj, razao_social_lcte, nomenclatura_coupa, tipo_vinculo,
               origem_destino, observacao, ativo, cidade, uf, cliente,
               created_at, created_by, updated_at, updated_by
        from mod_faturamento_de_para_cnpj_coupa
        {where_sql}
        order by ativo desc, updated_at desc, created_at desc, id desc
        """,
        tuple(params),
    )


def active_de_para_by_cnpj(cnpj_norm: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            select *
            from mod_faturamento_de_para_cnpj_coupa
            where cnpj_norm = ? and ativo = 1
            order by id desc
            """,
            (cnpj_norm,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_de_para_coupa(payload: dict[str, Any], usuario: str) -> int:
    now = brasilia_now_iso()
    row = {
        "cnpj": payload.get("cnpj", ""),
        "cnpj_norm": normalize_cnpj(payload.get("cnpj", "")),
        "razao_social_lcte": payload.get("razao_social_lcte", ""),
        "razao_social_lcte_norm": normalize_text(payload.get("razao_social_lcte", "")),
        "nomenclatura_coupa": payload.get("nomenclatura_coupa", ""),
        "nomenclatura_coupa_norm": normalize_text(payload.get("nomenclatura_coupa", "")),
        "tipo_vinculo": payload.get("tipo_vinculo", ""),
        "origem_destino": payload.get("origem_destino", ""),
        "observacao": payload.get("observacao", ""),
        "ativo": 1,
        "codigo_coupa": payload.get("codigo_coupa", ""),
        "localidade_coupa": payload.get("localidade_coupa", ""),
        "uf": payload.get("uf", ""),
        "cidade": payload.get("cidade", ""),
        "cliente": payload.get("cliente", ""),
        "tipo_operacao": payload.get("tipo_operacao", ""),
        "created_at": now,
        "created_by": usuario,
        "updated_at": now,
        "updated_by": usuario,
    }
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    with get_connection() as conn:
        cursor = conn.execute(
            f"insert into mod_faturamento_de_para_cnpj_coupa ({', '.join(columns)}) values ({placeholders})",
            tuple(row[column] for column in columns),
        )
        new_id = int(cursor.lastrowid or 0)
        conn.execute(
            """
            insert into mod_faturamento_historico_de_para_cnpj_coupa (
                id_de_para, data_hora, usuario, acao, valor_anterior_json, valor_novo_json, observacao
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id, now, usuario, "CRIADO", "{}", json.dumps(row, ensure_ascii=False, default=str), row["observacao"]),
        )
    return new_id


def set_de_para_active(id_de_para: int, active: bool, usuario: str, observacao: str = "") -> None:
    now = brasilia_now_iso()
    with get_connection() as conn:
        previous = conn.execute(
            "select * from mod_faturamento_de_para_cnpj_coupa where id = ?",
            (id_de_para,),
        ).fetchone()
        if not previous:
            return
        previous_payload = dict(previous)
        conn.execute(
            """
            update mod_faturamento_de_para_cnpj_coupa
            set ativo = ?, updated_at = ?, updated_by = ?
            where id = ?
            """,
            (1 if active else 0, now, usuario, id_de_para),
        )
        new_payload = dict(previous_payload)
        new_payload["ativo"] = 1 if active else 0
        new_payload["updated_at"] = now
        new_payload["updated_by"] = usuario
        conn.execute(
            """
            insert into mod_faturamento_historico_de_para_cnpj_coupa (
                id_de_para, data_hora, usuario, acao, valor_anterior_json, valor_novo_json, observacao
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                id_de_para,
                now,
                usuario,
                "REATIVADO" if active else "INATIVADO",
                json.dumps(previous_payload, ensure_ascii=False, default=str),
                json.dumps(new_payload, ensure_ascii=False, default=str),
                observacao,
            ),
        )


def update_de_para_coupa(id_de_para: int, payload: dict[str, Any], usuario: str) -> None:
    now = brasilia_now_iso()
    with get_connection() as conn:
        previous = conn.execute(
            "select * from mod_faturamento_de_para_cnpj_coupa where id = ?",
            (id_de_para,),
        ).fetchone()
        if not previous:
            return
        previous_payload = dict(previous)
        new_payload = {
            "cnpj": payload.get("cnpj", ""),
            "cnpj_norm": normalize_cnpj(payload.get("cnpj", "")),
            "razao_social_lcte": payload.get("razao_social_lcte", ""),
            "razao_social_lcte_norm": normalize_text(payload.get("razao_social_lcte", "")),
            "nomenclatura_coupa": payload.get("nomenclatura_coupa", ""),
            "nomenclatura_coupa_norm": normalize_text(payload.get("nomenclatura_coupa", "")),
            "tipo_vinculo": payload.get("tipo_vinculo", ""),
            "origem_destino": payload.get("origem_destino", ""),
            "observacao": payload.get("observacao", ""),
            "uf": payload.get("uf", ""),
            "cidade": payload.get("cidade", ""),
            "cliente": payload.get("cliente", ""),
            "updated_at": now,
            "updated_by": usuario,
        }
        conn.execute(
            """
            update mod_faturamento_de_para_cnpj_coupa
            set cnpj = ?, cnpj_norm = ?, razao_social_lcte = ?, razao_social_lcte_norm = ?,
                nomenclatura_coupa = ?, nomenclatura_coupa_norm = ?, tipo_vinculo = ?,
                origem_destino = ?, observacao = ?, uf = ?, cidade = ?, cliente = ?,
                updated_at = ?, updated_by = ?
            where id = ?
            """,
            (
                new_payload["cnpj"],
                new_payload["cnpj_norm"],
                new_payload["razao_social_lcte"],
                new_payload["razao_social_lcte_norm"],
                new_payload["nomenclatura_coupa"],
                new_payload["nomenclatura_coupa_norm"],
                new_payload["tipo_vinculo"],
                new_payload["origem_destino"],
                new_payload["observacao"],
                new_payload["uf"],
                new_payload["cidade"],
                new_payload["cliente"],
                now,
                usuario,
                id_de_para,
            ),
        )
        merged_payload = dict(previous_payload)
        merged_payload.update(new_payload)
        conn.execute(
            """
            insert into mod_faturamento_historico_de_para_cnpj_coupa (
                id_de_para, data_hora, usuario, acao, valor_anterior_json, valor_novo_json, observacao
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                id_de_para,
                now,
                usuario,
                "ALTERADO",
                json.dumps(previous_payload, ensure_ascii=False, default=str),
                json.dumps(merged_payload, ensure_ascii=False, default=str),
                new_payload["observacao"],
            ),
        )


def de_para_history(id_de_para: int) -> pd.DataFrame:
    return read_sql(
        """
        select data_hora, usuario, acao, observacao, valor_anterior_json, valor_novo_json
        from mod_faturamento_historico_de_para_cnpj_coupa
        where id_de_para = ?
        order by id desc
        """,
        (id_de_para,),
    )
