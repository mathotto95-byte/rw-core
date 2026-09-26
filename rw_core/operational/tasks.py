from __future__ import annotations

from datetime import datetime, timedelta
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso
from typing import Any

import pandas as pd

from rw_core.database.connection import get_connection
from rw_core.normalizers.fields import normalize_document_number, parse_number, to_datetime


OPEN_STATUSES = {"TAREFA ABERTA", "PENDENTE LANCAMENTO PORTAL", "AGUARDANDO RETORNO", "VENCENDO", "VENCIDA"}
RESOLVED_STATUSES = {"RESOLVIDA", "CANCELADA"}


def prazo_status(deadline: str | None, resolved: bool = False, base_date: datetime | None = None) -> tuple[int | None, str]:
    if resolved:
        return None, "RESOLVIDA"
    limit = to_datetime(deadline or "")
    if not limit:
        return None, "SEM PRAZO"
    today = (base_date or brasilia_now()).date()
    days = (limit.date() - today).days
    if days < 0:
        return days, "VENCIDA"
    if days == 0:
        return days, "VENCE HOJE"
    if days <= 5:
        return days, "VENCENDO EM 5 DIAS"
    if days <= 7:
        return days, "VENCENDO EM 7 DIAS"
    if days <= 15:
        return days, "VENCENDO EM 15 DIAS"
    return days, "NO PRAZO"


def complemento_prazo_status(data_emissao_nf: str | None, base_date: datetime | None = None) -> tuple[str, int | None, str]:
    emission = to_datetime(data_emissao_nf or "")
    if not emission:
        return "", None, "SEM DATA NF"
    deadline = emission + timedelta(days=60)
    days, status = prazo_status(deadline.isoformat(timespec="seconds"), base_date=base_date)
    if days is not None and days < 0:
        status = "FORA DO PRAZO DE SOLICITACAO"
    return deadline.isoformat(timespec="seconds"), days, status


def _task_signature(modulo: str, submodulo: str, tipo_tarefa: str, cte: Any, nf: Any) -> str:
    return ":".join(
        [
            "tarefa",
            str(modulo or "").strip().upper(),
            str(submodulo or "").strip().upper(),
            str(tipo_tarefa or "").strip().upper(),
            str(cte or "").strip(),
            normalize_document_number(nf),
        ]
    )


def upsert_task(conn, payload: dict[str, Any]) -> str:
    now = brasilia_now_iso()
    modulo = str(payload.get("modulo") or "").strip()
    submodulo = str(payload.get("submodulo") or "").strip()
    tipo_tarefa = str(payload.get("tipo_tarefa") or "").strip()
    cte = str(payload.get("cte") or "").strip()
    nf = normalize_document_number(payload.get("nf"))
    signature = payload.get("chave_referencia") or _task_signature(modulo, submodulo, tipo_tarefa, cte, nf)
    prazo_limite = str(payload.get("prazo_limite") or "")
    existing = conn.execute("select * from tarefas_operacionais where chave_referencia = ?", (signature,)).fetchone()
    resolved = bool(existing and str(existing["status_tarefa"] or "") in RESOLVED_STATUSES)
    dias_restantes, status_prazo = prazo_status(prazo_limite, resolved=resolved)
    status_tarefa = str(payload.get("status_tarefa") or ("RESOLVIDA" if resolved else "TAREFA ABERTA"))
    if status_tarefa not in RESOLVED_STATUSES and status_prazo == "VENCIDA":
        status_tarefa = "VENCIDA"
    previous_status = str(existing["status_tarefa"] or "") if existing else ""
    conn.execute(
        """
        insert or replace into tarefas_operacionais (
            id, modulo, submodulo, tipo_tarefa, cte, nf, chave_referencia,
            valor_referencia, data_base, prazo_limite, dias_restantes,
            status_prazo, status_tarefa, responsavel, observacao,
            cliente, placa, data_criacao, data_atualizacao, data_resolucao,
            usuario_criacao, usuario_atualizacao, origem_tarefa, nota_fiscal_norm,
            origem, destino, produto, valor_faturado, data_emissao_nf,
            source_table, source_signature,
            signature, sync_status, created_at, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ALTERADO_LOCAL', ?, ?)
        """,
        (
            existing["id"] if existing else None,
            modulo,
            submodulo,
            tipo_tarefa,
            cte,
            nf,
            signature,
            parse_number(payload.get("valor_referencia")),
            str(payload.get("data_base") or ""),
            prazo_limite,
            dias_restantes,
            status_prazo,
            status_tarefa,
            str(payload.get("responsavel") or ""),
            str(payload.get("observacao") or ""),
            str(payload.get("cliente") or ""),
            str(payload.get("placa") or ""),
            str(existing["data_criacao"]) if existing and existing["data_criacao"] else now,
            now,
            str(existing["data_resolucao"]) if existing and existing["data_resolucao"] else "",
            str(existing["usuario_criacao"]) if existing and existing["usuario_criacao"] else str(payload.get("usuario_criacao") or ""),
            str(payload.get("usuario_atualizacao") or payload.get("usuario_criacao") or ""),
            str(payload.get("origem_tarefa") or ""),
            nf,
            str(payload.get("origem") or ""),
            str(payload.get("destino") or ""),
            str(payload.get("produto") or ""),
            parse_number(payload.get("valor_faturado")),
            str(payload.get("data_emissao_nf") or ""),
            str(payload.get("source_table") or ""),
            str(payload.get("source_signature") or ""),
            signature,
            now,
            now,
        ),
    )
    action = "TAREFA_ATUALIZADA" if existing else "TAREFA_CRIADA"
    if not existing or previous_status != status_tarefa:
        conn.execute(
            """
            insert or replace into historico_tarefas_operacionais (
                tarefa_id, chave_referencia, modulo, tipo_tarefa, cte, nf,
                acao, valor_anterior, valor_novo, usuario, data_hora,
                observacao, signature, sync_status, created_at, updated_at
            ) values (
                (select id from tarefas_operacionais where chave_referencia = ?),
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NOVO_LOCAL', ?, ?
            )
            """,
            (
                signature,
                signature,
                modulo,
                tipo_tarefa,
                cte,
                nf,
                action,
                previous_status,
                status_tarefa,
                str(payload.get("usuario_atualizacao") or payload.get("usuario_criacao") or ""),
                now,
                str(payload.get("observacao") or ""),
                f"hist_tarefa:{signature}:{action}:{now}",
                now,
                now,
            ),
        )
    return signature


def sync_ipiranga_portal_tasks(conn, username: str = "") -> dict[str, int]:
    now_dt = brasilia_now()
    now = now_dt.isoformat(timespec="seconds")
    deadline = (now_dt + timedelta(days=5)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        select *
        from analise_ipiranga_fretes_portal26
        where upper(coalesce(status, '')) like 'PENDENTE LAN%'
          and coalesce(cte, '') <> ''
          and coalesce(nota_fiscal, '') <> ''
        """
    ).fetchall()
    active_signatures: set[str] = set()
    created = 0
    for row in rows:
        item = dict(row)
        signature = _task_signature("Fretes IPP", "Pendentes de Lancamento", "PENDENTE LANCAMENTO PORTAL", item.get("cte"), item.get("nota_fiscal"))
        active_signatures.add(signature)
        existing = conn.execute("select id from tarefas_operacionais where chave_referencia = ?", (signature,)).fetchone()
        upsert_task(
            conn,
            {
                "modulo": "Fretes IPP",
                "submodulo": "Pendentes de Lancamento",
                "tipo_tarefa": "PENDENTE LANCAMENTO PORTAL",
                "cte": item.get("cte"),
                "nf": item.get("nota_fiscal"),
                "chave_referencia": signature,
                "valor_referencia": item.get("valor"),
                "data_base": now,
                "prazo_limite": deadline,
                "status_tarefa": "PENDENTE LANCAMENTO PORTAL",
                "responsavel": username,
                "observacao": "NF com CT-e no KMM/FAT sem registro localizado no Portal IPP.",
                "cliente": item.get("cnpj_cpf_remetente"),
                "placa": item.get("placa"),
                "usuario_criacao": username,
                "usuario_atualizacao": username,
                "source_table": "analise_ipiranga_fretes_portal26",
                "source_signature": item.get("signature"),
            },
        )
        if not existing:
            created += 1
    resolved = 0
    open_rows = conn.execute(
        """
        select *
        from tarefas_operacionais
        where modulo = 'Fretes IPP'
          and tipo_tarefa = 'PENDENTE LANCAMENTO PORTAL'
          and coalesce(status_tarefa, '') not in ('RESOLVIDA', 'CANCELADA')
        """
    ).fetchall()
    for row in open_rows:
        item = dict(row)
        if item["chave_referencia"] in active_signatures:
            continue
        conn.execute(
            """
            update tarefas_operacionais
            set status_tarefa = 'RESOLVIDA',
                status_prazo = 'RESOLVIDA',
                data_resolucao = ?,
                data_atualizacao = ?,
                usuario_atualizacao = ?,
                observacao = coalesce(observacao, '') || ' Resolvida automaticamente: NF localizada/tratada no Portal IPP.',
                sync_status = 'ALTERADO_LOCAL',
                updated_at = ?
            where id = ?
            """,
            (now, now, username, now, item["id"]),
        )
        resolved += 1
    return {"tarefas_criadas": created, "tarefas_ativas": len(active_signatures), "tarefas_resolvidas": resolved}


def refresh_task_deadlines(username: str = "") -> int:
    now = brasilia_now_iso()
    updated = 0
    with get_connection() as conn:
        rows = conn.execute(
            """
            select *
            from tarefas_operacionais
            where coalesce(status_tarefa, '') not in ('RESOLVIDA', 'CANCELADA')
            """
        ).fetchall()
        for row in rows:
            item = dict(row)
            days, status = prazo_status(item.get("prazo_limite"))
            task_status = item.get("status_tarefa") or "TAREFA ABERTA"
            if status == "VENCIDA":
                task_status = "VENCIDA"
            conn.execute(
                """
                update tarefas_operacionais
                set dias_restantes = ?,
                    status_prazo = ?,
                    status_tarefa = ?,
                    data_atualizacao = ?,
                    usuario_atualizacao = ?,
                    updated_at = ?
                where id = ?
                """,
                (days, status, task_status, now, username, now, item["id"]),
            )
            updated += 1
    return updated


def list_tasks() -> pd.DataFrame:
    refresh_task_deadlines()
    with get_connection() as conn:
        rows = conn.execute("select * from tarefas_operacionais order by prazo_limite, data_criacao desc, id desc").fetchall()
    return pd.DataFrame([dict(row) for row in rows])


def task_cards(df: pd.DataFrame | None = None) -> dict[str, Any]:
    if df is None:
        df = list_tasks()
    if df.empty:
        return {
            "Total de tarefas abertas": 0,
            "Tarefas vencendo hoje": 0,
            "Tarefas vencendo em 5 dias": 0,
            "Tarefas vencidas": 0,
            "Tarefas resolvidas no mes": 0,
            "Valor financeiro em pendencia": 0,
        }
    status_task = df.get("status_tarefa", pd.Series("", index=df.index)).fillna("").astype(str)
    status_deadline = df.get("status_prazo", pd.Series("", index=df.index)).fillna("").astype(str)
    open_mask = ~status_task.isin(list(RESOLVED_STATUSES))
    today = brasilia_now().strftime("%Y-%m")
    resolution_month = df.get("data_resolucao", pd.Series("", index=df.index)).fillna("").astype(str).str[:7]
    values = pd.to_numeric(df.get("valor_referencia", pd.Series(0, index=df.index)), errors="coerce").fillna(0)
    return {
        "Total de tarefas abertas": int(open_mask.sum()),
        "Tarefas vencendo hoje": int((open_mask & status_deadline.eq("VENCE HOJE")).sum()),
        "Tarefas vencendo em 5 dias": int((open_mask & status_deadline.isin(["VENCENDO EM 5 DIAS", "VENCE HOJE"])).sum()),
        "Tarefas vencidas": int((open_mask & status_deadline.eq("VENCIDA")).sum()),
        "Tarefas resolvidas no mes": int((status_task.eq("RESOLVIDA") & resolution_month.eq(today)).sum()),
        "Valor financeiro em pendencia": float(values[open_mask].sum()),
    }
