from __future__ import annotations

import time
from datetime import datetime
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso
from typing import Any

import pandas as pd

from rw_core.database.connection import get_connection
from rw_core.normalizers.fields import normalizar_nf, normalizar_valor_monetario, to_datetime
from rw_core.operational.tasks import RESOLVED_STATUSES, complemento_prazo_status, upsert_task


VALUE_TOLERANCE = 0.02
MODULE_NAME = "COMPLEMENTOS"
CORRECTION_TYPE = "CORRECAO DE VALOR"
FREIGHT_COMPLEMENT_TYPE = "COMPLEMENTO DE FRETE"
CORRECTION_COMPLEMENT = "CORRECAO DE VALOR PORTAL"
FREIGHT_COMPLEMENT = "COMPLEMENTO DE FRETE"
MANUAL_COMPLEMENT = "ANALISE MANUAL"
NO_COMPLEMENT = "DIVERGENCIA SEM COMPLEMENTO"
COUPA_ADJUSTMENT = "AJUSTE DE VALOR COUPA"


def _value(row: dict[str, Any], field: str, default: Any = "") -> Any:
    value = row.get(field, default)
    return default if value is None else value


def _number(value: Any) -> float | None:
    return normalizar_valor_monetario(value)


def _financial_key(row: dict[str, Any]) -> str:
    cte = str(row.get("cte") or "").strip()
    nf = str(row.get("nota_fiscal_norm") or row.get("nf") or "").strip()
    return cte or nf


def _deadline_status(data_emissao_nf: Any) -> tuple[str, int | None, str]:
    deadline, days, status = complemento_prazo_status(str(data_emissao_nf or ""))
    if status == "SEM DATA NF":
        return "", None, "DATA NF AUSENTE"
    if status == "VENCIDA":
        return deadline, days, "VENCIDO"
    return deadline, days, status


def _initial_status(tipo_complemento: str, status_prazo: str, value: float | None) -> str:
    if status_prazo in {"VENCIDO", "FORA DO PRAZO DE SOLICITACAO"}:
        return "FORA DO PRAZO"
    if tipo_complemento == FREIGHT_COMPLEMENT and (value or 0) > VALUE_TOLERANCE:
        return "COMPLEMENTO SUGERIDO"
    if tipo_complemento == CORRECTION_COMPLEMENT:
        return "CORRECAO SUGERIDA"
    return "PENDENTE ANALISE"


def _task_signature(task_type: str, cte: Any, nf: Any) -> str:
    return f"COMPLEMENTOS|{task_type}|{str(cte or '').strip()}|{normalizar_nf(nf)}"


def _task_by_reference(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        select *
        from tarefas_operacionais
        where modulo = ?
        """,
        (MODULE_NAME,),
    ).fetchall()
    return {str(row["chave_referencia"] or ""): dict(row) for row in rows if row["chave_referencia"]}


def _resolve_missing_tasks(conn, active: set[str], username: str) -> int:
    now = brasilia_now_iso()
    rows = conn.execute(
        """
        select *
        from tarefas_operacionais
        where modulo = ?
          and tipo_tarefa in (?, ?)
          and coalesce(status_tarefa, '') not in ('RESOLVIDA', 'CANCELADA')
        """,
        (MODULE_NAME, CORRECTION_TYPE, FREIGHT_COMPLEMENT_TYPE),
    ).fetchall()
    resolved = 0
    for row in rows:
        item = dict(row)
        if item.get("chave_referencia") in active:
            continue
        conn.execute(
            """
            update tarefas_operacionais
            set status_tarefa = 'RESOLVIDA',
                status_prazo = 'RESOLVIDA',
                data_resolucao = ?,
                data_atualizacao = ?,
                usuario_atualizacao = ?,
                observacao = trim(coalesce(observacao, '') || ' Divergencia nao identificada apos novo recalculo.'),
                sync_status = 'ALTERADO_LOCAL',
                updated_at = ?
            where id = ?
            """,
            (now, now, username, now, item["id"]),
        )
        conn.execute(
            """
            insert or replace into historico_tarefas_operacionais (
                tarefa_id, chave_referencia, modulo, tipo_tarefa, cte, nf,
                acao, valor_anterior, valor_novo, usuario, data_hora,
                observacao, signature, sync_status, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, 'TAREFA_RESOLVIDA', ?, 'RESOLVIDA', ?, ?, ?, ?, 'NOVO_LOCAL', ?, ?)
            """,
            (
                item["id"],
                item.get("chave_referencia"),
                item.get("modulo"),
                item.get("tipo_tarefa"),
                item.get("cte"),
                item.get("nf"),
                item.get("status_tarefa"),
                username,
                now,
                "Divergencia nao identificada apos novo recalculo.",
                f"hist_tarefa:{item.get('chave_referencia')}:RESOLVIDA:{now}",
                now,
                now,
            ),
        )
        resolved += 1
    return resolved


def _task_status(conn, signature: str) -> tuple[str, str | None]:
    row = conn.execute("select id, status_tarefa from tarefas_operacionais where chave_referencia = ?", (signature,)).fetchone()
    if not row:
        return "", None
    return str(row["status_tarefa"] or ""), str(row["id"])


def _base_payload(row: dict[str, Any], processed_at: str, username: str) -> dict[str, Any]:
    nf = normalizar_nf(row.get("nota_fiscal_norm") or row.get("nota_fiscal"))
    data_nf = _value(row, "data_emissao_nf") or _value(row, "data_emissao")
    deadline, days, prazo_status = _deadline_status(data_nf)
    return {
        "data_analise": processed_at,
        "cte": str(_value(row, "cte")),
        "nf": nf,
        "nota_fiscal_norm": nf,
        "cliente": _value(row, "cliente"),
        "placa": _value(row, "placa"),
        "motorista": _value(row, "motorista"),
        "origem": _value(row, "origem") or _value(row, "municipio_remetente"),
        "destino": _value(row, "destino") or _value(row, "municipio_destinatario"),
        "produto": _value(row, "produto"),
        "operacao": _value(row, "operacao"),
        "data_emissao_nf": data_nf,
        "data_emissao_cte": _value(row, "data_emissao"),
        "data_limite_60_dias": deadline,
        "dias_restantes": days,
        "status_prazo_60_dias": prazo_status,
        "valor_faturado_kmm": _number(_value(row, "valor_faturado_kmm")),
        "valor_portal_ipp": _number(_value(row, "valor_portal")),
        "valor_acordado_coupa": _number(_value(row, "valor_acordado_coupa")),
        "diferenca_kmm_portal": _number(_value(row, "diferenca_kmm_portal")),
        "diferenca_kmm_coupa": _number(_value(row, "diferenca_kmm_coupa")),
        "diferenca_portal_coupa": None,
        "cte_normal": _value(row, "cte_normal"),
        "ctes_complementares": _value(row, "ctes_complementares"),
        "complemento_original": _value(row, "complemento_original"),
        "observacao_original": _value(row, "observacao_original"),
        "motivo_identificacao_complemento": _value(row, "motivo_identificacao_complemento"),
        "valor_cte_normal": _number(_value(row, "valor_cte_normal")),
        "valor_cte_complementar": _number(_value(row, "valor_cte_complementar")),
        "valor_kmm_total": _number(_value(row, "valor_kmm_total")) or _number(_value(row, "valor_faturado_kmm")),
        "tem_complemento": _value(row, "tem_complemento"),
        "status_complemento_cte": _value(row, "status_complemento_cte"),
        "diferenca_sem_complemento": _number(_value(row, "diferenca_sem_complemento")),
        "diferenca_com_complemento": _number(_value(row, "diferenca_com_complemento")) or _number(_value(row, "diferenca_kmm_coupa")),
        "status_tarefa": "",
        "tarefa_id": "",
        "observacao": "",
        "usuario_analise": username,
        "arquivo_origem": _value(row, "arquivo_kmm") or _value(row, "arquivo_portal"),
        "lote_importacao": _value(row, "signature"),
    }


def _insert_analysis(conn, payload: dict[str, Any]) -> None:
    columns = [
        "signature", "data_analise", "cte", "nf", "nota_fiscal_norm", "cliente",
        "placa", "motorista", "origem", "destino", "produto", "operacao",
        "data_emissao_nf", "data_emissao_cte", "data_limite_60_dias",
        "dias_restantes", "status_prazo_60_dias", "valor_faturado_kmm",
        "valor_portal_ipp", "valor_acordado_coupa", "valor_correto_sugerido",
        "diferenca_kmm_portal", "diferenca_kmm_coupa", "diferenca_portal_coupa",
        "valor_complemento_sugerido", "tipo_complemento", "motivo_complemento",
        "origem_divergencia", "status_complemento", "status_tarefa", "tarefa_id",
        "observacao", "usuario_analise", "arquivo_origem", "lote_importacao",
        "cte_normal", "ctes_complementares", "complemento_original",
        "observacao_original", "motivo_identificacao_complemento",
        "valor_cte_normal", "valor_cte_complementar", "valor_kmm_total",
        "tem_complemento", "status_complemento_cte", "diferenca_sem_complemento",
        "diferenca_com_complemento",
    ]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"insert or replace into analise_complementos_frete ({', '.join(columns)}) values ({placeholders})",
        tuple(payload.get(column) for column in columns),
    )


def _create_task(conn, payload: dict[str, Any], task_type: str, submodule: str, username: str) -> str:
    signature = _task_signature(task_type, payload.get("cte"), payload.get("nf"))
    upsert_task(
        conn,
        {
            "modulo": MODULE_NAME,
            "submodulo": submodule,
            "tipo_tarefa": task_type,
            "origem_tarefa": "AUTOMATICA",
            "cte": payload.get("cte"),
            "nf": payload.get("nf"),
            "chave_referencia": signature,
            "valor_referencia": abs(_number(payload.get("valor_complemento_sugerido")) or 0),
            "valor_faturado": payload.get("valor_faturado_kmm"),
            "data_base": payload.get("data_emissao_nf") or payload.get("data_analise"),
            "data_emissao_nf": payload.get("data_emissao_nf"),
            "prazo_limite": payload.get("data_limite_60_dias"),
            "status_tarefa": "ABERTA",
            "responsavel": username,
            "observacao": payload.get("motivo_complemento") or "",
            "cliente": payload.get("cliente"),
            "placa": payload.get("placa"),
            "origem": payload.get("origem"),
            "destino": payload.get("destino"),
            "produto": payload.get("produto"),
            "usuario_criacao": username,
            "usuario_atualizacao": username,
            "source_table": "analise_complementos_frete",
            "source_signature": payload.get("signature"),
        },
    )
    return signature


def _add_payload(rows: list[dict[str, Any]], payload: dict[str, Any], kind: str, motive: str, source: str, value: float | None, correct_value: float | None, observation: str = "") -> None:
    payload = dict(payload)
    payload["tipo_complemento"] = kind
    payload["motivo_complemento"] = motive
    payload["origem_divergencia"] = source
    payload["valor_complemento_sugerido"] = value
    payload["valor_correto_sugerido"] = correct_value
    payload["status_complemento"] = _initial_status(kind, payload.get("status_prazo_60_dias", ""), value)
    payload["observacao"] = observation
    payload["signature"] = "complemento:{}:{}:{}:{}".format(
        kind,
        str(payload.get("cte") or "").strip(),
        str(payload.get("nota_fiscal_norm") or "").strip(),
        source,
    )
    rows.append(payload)


def run(username: str = "") -> dict[str, Any]:
    started = time.perf_counter()
    started_at = brasilia_now_iso()
    processed_at = started_at
    metrics = {
        "registros_analisados": 0,
        "correcoes_identificadas": 0,
        "complementos_identificados": 0,
        "analise_manual": 0,
        "tarefas_criadas": 0,
        "tarefas_atualizadas": 0,
        "tarefas_resolvidas": 0,
    }
    message = ""
    status = "SUCESSO"
    with get_connection() as conn:
        try:
            source_rows = [dict(row) for row in conn.execute("select * from analise_fretes_ipp").fetchall()]
            if not source_rows:
                status = "SEM_DADOS"
                message = "E necessario recalcular o modulo Fretes IPP antes de gerar complementos."
                conn.execute("delete from analise_complementos_frete")
                return {"status": status, "mensagem": message, **metrics}

            conn.execute("delete from analise_complementos_frete")
            existing_tasks = _task_by_reference(conn)
            active_tasks: set[str] = set()
            generated: list[dict[str, Any]] = []

            for source in source_rows:
                metrics["registros_analisados"] += 1
                payload = _base_payload(source, processed_at, username)
                financial_key = _financial_key(payload)
                tipo_registro = str(source.get("tipo_registro") or "")
                status_portal = str(source.get("status_portal_ipp") or "")
                value_kmm = payload.get("valor_faturado_kmm")
                value_portal = payload.get("valor_portal_ipp")
                value_coupa = payload.get("valor_acordado_coupa")

                if tipo_registro == "PORTAL_SEM_KMM" or str(source.get("status_final_frete") or "") == "PORTAL SEM KMM":
                    _add_payload(generated, payload, MANUAL_COMPLEMENT, "PORTAL SEM KMM", "Portal IPP", None, None, "Nota existe no Portal IPP, mas nao foi localizada no KMM/FAT.")
                    metrics["analise_manual"] += 1
                    continue

                if status_portal == "PENDENTE LANCAMENTO PORTAL":
                    _add_payload(generated, payload, MANUAL_COMPLEMENT, "FRETE NAO LANCADO NO PORTAL", "KMM x Portal IPP", None, None, "Referencia ao prazo de 5 dias ja controlado no modulo Fretes IPP.")
                    metrics["analise_manual"] += 1

                if value_kmm is not None and value_portal is not None and abs(value_kmm - value_portal) > VALUE_TOLERANCE:
                    diff = value_kmm - value_portal
                    motive = "VALOR PORTAL MENOR QUE KMM" if diff > 0 else "VALOR PORTAL MAIOR QUE KMM"
                    correction = dict(payload)
                    correction["diferenca_kmm_portal"] = diff
                    _add_payload(generated, correction, CORRECTION_COMPLEMENT, motive, "KMM x Portal IPP", diff, value_kmm)
                    metrics["correcoes_identificadas"] += 1

                if value_kmm is not None and value_coupa is not None and abs(value_coupa - value_kmm) > VALUE_TOLERANCE:
                    diff_coupa = value_coupa - value_kmm
                    coupa_payload = dict(payload)
                    coupa_payload["diferenca_kmm_coupa"] = value_kmm - value_coupa
                    if diff_coupa > VALUE_TOLERANCE:
                        _add_payload(generated, coupa_payload, FREIGHT_COMPLEMENT, "VALOR KMM MENOR QUE COUPA", "KMM x Coupa", diff_coupa, value_coupa)
                        metrics["complementos_identificados"] += 1
                    else:
                        _add_payload(generated, coupa_payload, NO_COMPLEMENT, "VALOR KMM MAIOR QUE COUPA", "KMM x Coupa", None, value_coupa)
                        metrics["analise_manual"] += 1

                if value_portal is not None and value_coupa is not None and abs(value_portal - value_coupa) > VALUE_TOLERANCE:
                    portal_coupa = dict(payload)
                    portal_coupa["diferenca_portal_coupa"] = value_portal - value_coupa
                    if not any(item.get("nota_fiscal_norm") == payload.get("nota_fiscal_norm") and item.get("origem_divergencia") == "Portal IPP x Coupa" for item in generated):
                        _add_payload(generated, portal_coupa, COUPA_ADJUSTMENT, "VALOR PORTAL DIFERENTE DA COUPA", "Portal IPP x Coupa", None, value_coupa)
                        metrics["analise_manual"] += 1

                status_coupa = str(source.get("status_coupa") or "").upper()
                tarifa = payload.get("tarifa_coupa")
                if "SEM MATCH" in status_coupa:
                    _add_payload(generated, payload, MANUAL_COMPLEMENT, "SEM MATCH COUPA", "KMM x Coupa", None, None)
                    metrics["analise_manual"] += 1
                elif "SEM TARIFA" in status_coupa or (value_coupa is None and tarifa is None and str(source.get("camada_match_coupa") or "")):
                    _add_payload(generated, payload, MANUAL_COMPLEMENT, "SEM TARIFA COUPA", "KMM x Coupa", None, None)
                    metrics["analise_manual"] += 1

                if payload.get("status_prazo_60_dias") == "DATA NF AUSENTE":
                    _add_payload(generated, payload, MANUAL_COMPLEMENT, "DATA NF AUSENTE", "Dados de origem", None, None)
                    metrics["analise_manual"] += 1

            for payload in generated:
                task_signature = ""
                if payload["tipo_complemento"] == CORRECTION_COMPLEMENT:
                    task_signature = _create_task(conn, payload, CORRECTION_TYPE, "CORRECAO DE VALORES", username)
                elif payload["tipo_complemento"] == FREIGHT_COMPLEMENT:
                    task_signature = _create_task(conn, payload, FREIGHT_COMPLEMENT_TYPE, "COMPLEMENTO DE FRETE", username)

                if task_signature:
                    active_tasks.add(task_signature)
                    task_status, task_id = _task_status(conn, task_signature)
                    payload["status_tarefa"] = task_status
                    payload["tarefa_id"] = task_id
                    if task_signature in existing_tasks:
                        metrics["tarefas_atualizadas"] += 1
                    else:
                        metrics["tarefas_criadas"] += 1
                _insert_analysis(conn, payload)

            metrics["tarefas_resolvidas"] = _resolve_missing_tasks(conn, active_tasks, username)
        except Exception as exc:
            status = "ERRO"
            message = str(exc)
            raise
        finally:
            finished = brasilia_now_iso()
            conn.execute(
                """
                insert or replace into complementos_frete_logs (
                    data_hora_inicio, data_hora_fim, usuario, status,
                    registros_analisados, correcoes_identificadas,
                    complementos_identificados, tarefas_criadas, tarefas_atualizadas,
                    tarefas_resolvidas, mensagem, signature,
                    sync_status, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NOVO_LOCAL', ?, ?)
                """,
                (
                    started_at,
                    finished,
                    username,
                    status,
                    metrics["registros_analisados"],
                    metrics["correcoes_identificadas"],
                    metrics["complementos_identificados"],
                    metrics["tarefas_criadas"],
                    metrics["tarefas_atualizadas"],
                    metrics["tarefas_resolvidas"],
                    message,
                    f"complementos_log:{started_at}",
                    finished,
                    finished,
                ),
            )
    return {"status": status, "mensagem": message, **metrics, "duracao_segundos": round(time.perf_counter() - started, 3)}


def load_analysis() -> pd.DataFrame:
    with get_connection() as conn:
        rows = conn.execute("select * from analise_complementos_frete order by data_limite_60_dias, cte, nf").fetchall()
    return pd.DataFrame([dict(row) for row in rows])


def load_tasks() -> pd.DataFrame:
    with get_connection() as conn:
        rows = conn.execute(
            """
            select *
            from tarefas_operacionais
            where modulo = ?
            order by prazo_limite, data_criacao desc
            """,
            (MODULE_NAME,),
        ).fetchall()
    return pd.DataFrame([dict(row) for row in rows])


def load_logs(limit: int = 50) -> pd.DataFrame:
    with get_connection() as conn:
        rows = conn.execute("select * from complementos_frete_logs order by id desc limit ?", (limit,)).fetchall()
    return pd.DataFrame([dict(row) for row in rows])


def _unique_financial_sum(df: pd.DataFrame, value_column: str) -> float:
    if df.empty or value_column not in df.columns:
        return 0.0
    view = df.copy()
    cte = view.get("cte", pd.Series("", index=view.index)).fillna("").astype(str)
    nf = view.get("nota_fiscal_norm", pd.Series("", index=view.index)).fillna("").astype(str)
    view["_financial_key"] = cte.where(cte.ne(""), nf)
    values = pd.to_numeric(view[value_column], errors="coerce").fillna(0).abs()
    view["_value"] = values
    return float(view.groupby("_financial_key", dropna=False)["_value"].max().sum())


def summary_cards(df: pd.DataFrame, kind: str) -> dict[str, Any]:
    if df.empty:
        return {
            "Total sugerido": 0,
            "Valor total": 0,
            "No prazo": 0,
            "Vencendo em 7 dias": 0,
            "Vencidos": 0,
            "Resolvidos": 0,
        }
    view = df[df.get("tipo_complemento", pd.Series("", index=df.index)).fillna("").astype(str).eq(kind)].copy()
    prazo = view.get("status_prazo_60_dias", pd.Series("", index=view.index)).fillna("").astype(str)
    status_complemento = view.get("status_complemento", pd.Series("", index=view.index)).fillna("").astype(str)
    status_tarefa = view.get("status_tarefa", pd.Series("", index=view.index)).fillna("").astype(str)
    return {
        "Total sugerido": int(len(view)),
        "Valor total": _unique_financial_sum(view, "valor_complemento_sugerido"),
        "No prazo": int(prazo.eq("NO PRAZO").sum()),
        "Vencendo em 7 dias": int(prazo.isin(["VENCENDO EM 7 DIAS", "VENCE HOJE"]).sum()),
        "Vencidos": int((prazo.isin(["VENCIDO", "FORA DO PRAZO DE SOLICITACAO"]) | status_complemento.eq("FORA DO PRAZO")).sum()),
        "Resolvidos": int(status_tarefa.isin(list(RESOLVED_STATUSES)).sum() + status_complemento.eq("RESOLVIDO").sum()),
    }


def diagnostics() -> dict[str, pd.DataFrame]:
    df = load_analysis()
    tasks = load_tasks()
    logs = load_logs()
    duplicates = pd.DataFrame()
    if not df.empty:
        duplicates = df.groupby(["cte", "nota_fiscal_norm", "tipo_complemento"], dropna=False).size().reset_index(name="Quantidade")
        duplicates = duplicates[duplicates["Quantidade"].gt(1)]
    summary = pd.DataFrame(
        [
            {"Indicador": "Registros analisados", "Valor": int(len(df))},
            {"Indicador": "Correcoes de valor identificadas", "Valor": int(df.get("tipo_complemento", pd.Series(dtype=str)).eq(CORRECTION_COMPLEMENT).sum()) if not df.empty else 0},
            {"Indicador": "Complementos de frete identificados", "Valor": int(df.get("tipo_complemento", pd.Series(dtype=str)).eq(FREIGHT_COMPLEMENT).sum()) if not df.empty else 0},
            {"Indicador": "Casos em analise manual", "Valor": int(df.get("tipo_complemento", pd.Series(dtype=str)).isin([MANUAL_COMPLEMENT, NO_COMPLEMENT, COUPA_ADJUSTMENT]).sum()) if not df.empty else 0},
            {"Indicador": "Tarefas abertas", "Valor": int((~tasks.get("status_tarefa", pd.Series(dtype=str)).isin(list(RESOLVED_STATUSES))).sum()) if not tasks.empty else 0},
            {"Indicador": "Registros fora do prazo", "Valor": int(df.get("status_complemento", pd.Series(dtype=str)).eq("FORA DO PRAZO").sum()) if not df.empty else 0},
            {"Indicador": "Registros sem data NF", "Valor": int(df.get("status_prazo_60_dias", pd.Series(dtype=str)).eq("DATA NF AUSENTE").sum()) if not df.empty else 0},
            {"Indicador": "Duplicidades por CT-e/NF", "Valor": int(len(duplicates))},
        ]
    )
    return {
        "Resumo": summary,
        "Duplicidades por CT-e/NF": duplicates,
        "Tarefas": tasks,
        "Logs": logs,
    }
