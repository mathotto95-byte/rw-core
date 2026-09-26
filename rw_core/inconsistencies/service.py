from __future__ import annotations

from datetime import datetime
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso
from typing import Any

import pandas as pd

from rw_core.database.connection import get_connection


def priority_for(kind: str) -> str:
    high = [
        "NOTA FISCAL NAO ENCONTRADA NO KMM",
        "CT-E NAO ENCONTRADO NA COUPA",
        "VALOR DIVERGENTE",
        "FATURADO SEM COUPA",
        "ERRO ICMS",
        "ERRO CFOP",
        "POSSIVEL ESTADIA",
        "TEMPO PARADO ACIMA DA FRANQUIA",
    ]
    medium = [
        "FRETE UNITARIO DIVERGENTE",
        "TEMPO NF X CT-E ACIMA DE 1 HORA",
        "VOLUME DIVERGENTE",
        "COUPA SEM FATURAMENTO",
        "VALOR PENDENTE NO COUPA",
        "DIVERGENCIA DE ORIGEM/DESTINO",
        "FALHA PROCESSO CARGA",
        "FALHA PROCESSO DESCARGA",
    ]
    normalized = kind.upper()
    if any(term in normalized for term in high):
        return "ALTA"
    if any(term in normalized for term in medium):
        return "MEDIA"
    return "BAIXA"


def upsert_inconsistency(conn, data: dict[str, Any]) -> str:
    signature = data["signature"]
    existing = conn.execute(
        "select id, status_tratamento from inconsistencias where signature = ?",
        (signature,),
    ).fetchone()
    fields = [
        "modulo_origem",
        "tipo_inconsistencia",
        "cliente",
        "placa",
        "nota_fiscal",
        "cte",
        "viagem",
        "codigo_monitoramento",
        "origem",
        "destino",
        "cobranca",
        "produto",
        "periodo",
        "pedido_coupa",
        "valor_envolvido",
        "volume_envolvido",
        "tempo_envolvido",
        "diferenca_valor",
        "diferenca_volume",
        "percentual_diferenca",
        "prioridade",
        "observacao",
        "arquivo_origem",
        "data_identificacao",
        "signature",
    ]
    data.setdefault("prioridade", priority_for(data.get("tipo_inconsistencia", "")))
    data.setdefault("data_identificacao", brasilia_now_iso())
    if existing:
        if existing["status_tratamento"] in ["CORRIGIDO NA ORIGEM", "RESOLVIDO APOS ATUALIZACAO"]:
            conn.execute(
                "update inconsistencias set status_tratamento = 'EM ABERTO' where id = ?",
                (existing["id"],),
            )
        return signature
    placeholders = ", ".join(["?"] * len(fields))
    conn.execute(
        f"insert into inconsistencias ({', '.join(fields)}) values ({placeholders})",
        [data.get(field) for field in fields],
    )
    return signature


def resolve_missing(active_signatures: set[str], module: str | None = None) -> None:
    with get_connection() as conn:
        module_filter = "and modulo_origem = ?" if module else ""
        params = [module] if module else []
        rows = conn.execute(
            f"""
            select id, signature, status_tratamento from inconsistencias
            where status_tratamento in ('EM ABERTO', 'EM ANALISE', 'AGUARDANDO RETORNO')
            {module_filter}
            """,
            params,
        ).fetchall()
        now = brasilia_now_iso()
        for row in rows:
            if row["signature"] in active_signatures:
                continue
            conn.execute(
                "update inconsistencias set status_tratamento = 'RESOLVIDO APOS ATUALIZACAO' where id = ?",
                (row["id"],),
            )
            conn.execute(
                """
                insert into historico_tratamento_inconsistencias (
                    inconsistencia_id, status_anterior, status_novo, responsavel,
                    observacao, acao_tomada, motivo_decisao, data_alteracao,
                    tipo_atualizacao, regra_correcao
                ) values (?, ?, ?, '', ?, ?, ?, ?, 'AUTOMATICA', ?)
                """,
                (
                    row["id"],
                    row["status_tratamento"],
                    "RESOLVIDO APOS ATUALIZACAO",
                    "Inconsistencia nao reapareceu no recalculo.",
                    "Atualizacao automatica",
                    "Ausente no conjunto atual de regras",
                    now,
                    "recalculate_all",
                ),
            )


def list_inconsistencies() -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql_query("select * from inconsistencias order by id desc", conn)


def get_inconsistency_history(inconsistency_id: int) -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql_query(
            """
            select * from historico_tratamento_inconsistencias
            where inconsistencia_id = ?
            order by id desc
            """,
            conn,
            params=(int(inconsistency_id),),
        )


def save_treatment(
    inconsistency_id: int,
    new_status: str,
    responsible: str,
    observation: str,
    action: str,
    reason: str,
    user_role: str = "",
) -> None:
    now = brasilia_now_iso()
    with get_connection() as conn:
        current = conn.execute(
            "select status_tratamento from inconsistencias where id = ?",
            (int(inconsistency_id),),
        ).fetchone()
        previous = current["status_tratamento"] if current else ""
        conn.execute(
            """
            update inconsistencias
            set status_tratamento = ?, responsavel = ?, observacao = ?,
                sync_status = 'ALTERADO_LOCAL', updated_at = ?
            where id = ?
            """,
            (new_status, responsible, observation, now, int(inconsistency_id)),
        )
        conn.execute(
            """
            insert into historico_tratamento_inconsistencias (
                inconsistencia_id, status_anterior, status_novo, responsavel,
                observacao, acao_tomada, motivo_decisao, data_alteracao,
                tipo_atualizacao, regra_correcao, usuario, perfil_usuario
            ) values (?, ?, ?, ?, ?, ?, ?, ?, 'MANUAL', '', ?, ?)
            """,
            (
                int(inconsistency_id),
                previous,
                new_status,
                responsible,
                observation,
                action,
                reason,
                now,
                responsible,
                user_role,
            ),
        )
