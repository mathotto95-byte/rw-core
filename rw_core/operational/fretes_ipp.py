from __future__ import annotations

import time
from datetime import datetime, timedelta
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso
from typing import Any

import pandas as pd

from rw_core.database.connection import get_connection
from rw_core.normalizers.fields import normalizar_nf, normalizar_texto_match, normalizar_valor_monetario, parse_number, to_datetime
from rw_core.operational.kmm_faturamento import rebuild_base_kmm_faturamento_consolidado
from rw_core.operational.tasks import complemento_prazo_status, upsert_task


VALUE_TOLERANCE = 0.02
PENDING_PORTAL_TASK = "PENDENTE LANCAMENTO PORTAL"
VALUE_FIX_TASK = "CORRECAO DE VALOR"


def _value(row, field: str, default: Any = "") -> Any:
    try:
        value = row[field]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _number(value) -> float | None:
    return normalizar_valor_monetario(value)


def _status_flag(value: Any) -> str:
    text = normalizar_texto_match(value)
    if not text:
        return "VAZIO"
    if text in {"SIM", "S", "YES", "Y", "OK", "1", "TRUE", "PAGO"} or text.startswith("SIM "):
        return "SIM"
    return "NAO"


def _is_yes(value: Any) -> bool:
    return _status_flag(value) == "SIM"


def _portal_status(portal_row) -> str:
    if not portal_row:
        return PENDING_PORTAL_TASK
    vinculo = _is_yes(_value(portal_row, "vinculo_norm") or _value(portal_row, "possui_vinculo") or _value(portal_row, "vinculo"))
    canhoto = _is_yes(_value(portal_row, "canhoto_norm") or _value(portal_row, "possui_canhoto") or _value(portal_row, "canhoto"))
    pago = _is_yes(_value(portal_row, "pago_norm") or _value(portal_row, "pago"))
    if not vinculo:
        return "PENDENTE VINCULO"
    if not canhoto:
        return "PENDENTE CANHOTO"
    if not pago:
        return "PAGAMENTO PROGRAMADO"
    return "PAGAMENTO OK"


def _portal_value(portal_row) -> float | None:
    if not portal_row:
        return None
    if str(_value(portal_row, "coluna_origem_valor_portal") or "").strip().upper() == "AW":
        return _number(_value(portal_row, "valor_portal"))
    value = _number(_value(portal_row, "valor_portal"))
    if value is not None:
        return value
    value = _number(_value(portal_row, "valor_portal_frete"))
    if value is not None:
        return value
    return _number(_value(portal_row, "valor_portal_total"))


def _status_value(portal_row, value_kmm: float | None, value_portal: float | None) -> str:
    if not portal_row:
        return "NAO COMPARADO"
    if value_kmm is None or value_portal is None:
        return "VALOR PORTAL DIVERGENTE"
    return "VALOR PORTAL OK" if abs(value_kmm - value_portal) <= VALUE_TOLERANCE else "VALOR PORTAL DIVERGENTE"


def _status_final(status_portal: str, status_value: str, status_coupa: str, portal_only: bool = False) -> str:
    if portal_only:
        return "PORTAL SEM KMM"
    if status_portal == PENDING_PORTAL_TASK:
        return PENDING_PORTAL_TASK
    if status_value == "VALOR PORTAL DIVERGENTE":
        return "VALOR PORTAL DIVERGENTE"
    if status_coupa in {"DIVERGENTE", "VALOR KMM DIVERGENTE COUPA"}:
        return "VALOR KMM DIVERGENTE COUPA"
    if status_portal == "PENDENTE VINCULO":
        return "PENDENTE VINCULO"
    if status_portal == "PENDENTE CANHOTO":
        return "PENDENTE CANHOTO"
    if status_portal == "PAGAMENTO PROGRAMADO":
        return "PAGAMENTO PROGRAMADO"
    if status_portal == "PAGAMENTO OK":
        return "PAGAMENTO OK"
    return "CONFERIDO OK"


def _financial_key(row: dict[str, Any]) -> str:
    cte = str(row.get("cte") or "").strip()
    return cte or str(row.get("nota_fiscal_norm") or "").strip()


def _task_signature(kind: str, cte: Any, nf: Any) -> str:
    return f"FRETES_IPP|{kind}|{str(cte or '').strip()}|{normalizar_nf(nf)}"


def _task_by_reference(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        select *
        from tarefas_operacionais
        where modulo = 'FRETES IPP' or modulo = 'Fretes IPP'
        """
    ).fetchall()
    return {str(row["chave_referencia"] or ""): dict(row) for row in rows if row["chave_referencia"]}


def _resolve_missing_tasks(conn, active: set[str], task_type: str, username: str, note: str) -> int:
    now = brasilia_now_iso()
    rows = conn.execute(
        """
        select *
        from tarefas_operacionais
        where modulo in ('FRETES IPP', 'Fretes IPP')
          and tipo_tarefa = ?
          and coalesce(status_tarefa, '') not in ('RESOLVIDA', 'CANCELADA')
        """,
        (task_type,),
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
                observacao = trim(coalesce(observacao, '') || ' ' || ?),
                sync_status = 'ALTERADO_LOCAL',
                updated_at = ?
            where id = ?
            """,
            (now, now, username, note, now, item["id"]),
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
                note,
                f"hist_tarefa:{item.get('chave_referencia')}:RESOLVIDA:{now}",
                now,
                now,
            ),
        )
        resolved += 1
    return resolved


def _load_kmm_rows(conn) -> list:
    try:
        rebuild_base_kmm_faturamento_consolidado(conn)
        rows = conn.execute(
            """
            select *
            from base_kmm_faturamento_consolidado
            where coalesce(status_registro, 'ATIVO') = 'ATIVO'
              and coalesce(nota_fiscal_norm, '') <> ''
            order by id
            """
        ).fetchall()
        if rows:
            return rows
    except Exception:
        pass
    rows = conn.execute(
        """
        select *
        from base_kmm_fat_notas_normalizadas
        where coalesce(status_registro, 'ATIVO') = 'ATIVO'
          and coalesce(nota_fiscal_norm, '') <> ''
        order by id
        """
    ).fetchall()
    if rows:
        return rows
    rows = conn.execute(
        """
        select
            id as base_original_id,
            frete_original_id as importacao_id,
            'FRETES IPP' as tipo_base,
            data_emissao,
            data_emissao as data_emissao_dt,
            cte,
            cte as cte_numero,
            nota_fiscal_original,
            nota_fiscal_individual,
            nota_fiscal_norm,
            total_conhec as total_conhecimento,
            peso_frete,
            peso_frete as valor_faturado_kmm,
            frete_unitario,
            volume,
            volume_normalizado,
            mercadoria,
            mercadoria as produto,
            '' as produto_norm,
            municipio_remetente as origem,
            municipio_destinatario as destino,
            '' as origem_norm,
            municipio_destinatario_norm as destino_norm,
            municipio_remetente,
            municipio_destinatario,
            placa_tracao as placa,
            '' as placa_norm,
            base_calc_icms as base_icms,
            valor_icms,
            operacao as tabela_frete,
            operacao as operacao_norm,
            cnpj_cpf_remetente as cliente,
            cnpj_cpf_remetente as cobranca,
            arquivo_origem,
            data_importacao,
            'ATIVO' as status_registro,
            signature
        from base_fretes_ipp_notas_normalizadas
        where coalesce(nota_fiscal_norm, '') <> ''
        order by id
        """
    ).fetchall()
    if rows:
        return rows
    return conn.execute(
        """
        select
            id as base_original_id,
            importacao_id,
            'KMM / LCTE' as tipo_base,
            emissao_cte as data_emissao,
            emissao_cte as data_emissao_dt,
            cte_numero as cte,
            cte_numero,
            nota_fiscal as nota_fiscal_original,
            nota_fiscal as nota_fiscal_individual,
            nota_fiscal_normalizada as nota_fiscal_norm,
            total_conhecimento,
            peso_frete,
            valor as valor_faturado_kmm,
            frete_unitario,
            volume,
            volume as volume_normalizado,
            mercadoria,
            mercadoria as produto,
            '' as produto_norm,
            origem,
            destino,
            '' as origem_norm,
            '' as destino_norm,
            origem as municipio_remetente,
            destino as municipio_destinatario,
            placa,
            '' as placa_norm,
            base_icms,
            valor_icms,
            tabela_frete,
            tabela_frete as operacao_norm,
            arquivo_origem,
            data_importacao,
            status_registro,
            ('kmm_nf_fallback:' || id) as signature
        from base_kmm_notas_normalizadas
        where coalesce(status_registro, 'ATIVO') = 'ATIVO'
          and coalesce(nota_fiscal_normalizada, '') <> ''
        order by id
        """
    ).fetchall()


def _load_portal_rows(conn) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            select *
            from base_portal_ipp_normalizada
            where coalesce(status_registro, 'ATIVO') = 'ATIVO'
              and coalesce(nota_fiscal_norm, numero_norm, '') <> ''
            order by id desc
            """
        ).fetchall()
    ]
    seen = {normalizar_nf(row.get("nota_fiscal_norm") or row.get("numero_norm")) for row in rows}
    try:
        portal26_rows = conn.execute(
            """
            select *
            from base_portal26_original
            where coalesce(status_registro, 'ATIVO') = 'ATIVO'
              and coalesce(nota_fiscal_norm, numero_norm, '') <> ''
            order by id desc
            """
        ).fetchall()
    except Exception:
        portal26_rows = []
    for row in portal26_rows:
        item = dict(row)
        nf = normalizar_nf(item.get("nota_fiscal_norm") or item.get("numero_norm"))
        if not nf or nf in seen:
            continue
        item.setdefault("nota_fiscal_norm", nf)
        item.setdefault("numero_norm", nf)
        item.setdefault("valor_portal_frete", item.get("valor_portal_frete"))
        item.setdefault("valor_portal_total", item.get("valor_portal_total"))
        rows.append(item)
        seen.add(nf)
    return rows


def _load_coupa_by_cte(conn) -> dict[str, dict[str, Any]]:
    try:
        rows = conn.execute("select * from analise_kmm_x_coupa where coalesce(cte, '') <> '' order by id desc").fetchall()
    except Exception:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        cte = str(item.get("cte") or "").strip()
        if cte and cte not in result:
            result[cte] = item
    return result


def _kmm_value(row) -> float | None:
    value = _number(_value(row, "peso_frete"))
    if value is not None:
        return value
    value = _number(_value(row, "valor_faturado_kmm"))
    if value is not None:
        return value
    return _number(_value(row, "total_conhecimento"))


def _insert_analysis(conn, payload: dict[str, Any]) -> None:
    columns = [
        "tipo_registro", "data_emissao", "data_emissao_nf", "data_portal", "cte",
        "nota_fiscal", "nota_fiscal_norm", "cliente", "origem", "destino",
        "municipio_remetente", "municipio_destinatario", "produto", "produto_norm",
        "operacao", "placa", "motorista", "volume", "volume_normalizado",
        "valor_faturado_kmm", "valor_total_cte_kmm", "frete_unitario", "base_icms",
        "valor_icms", "numero_portal", "valor_portal", "valor_total_portal",
        "valor_pedagio_portal", "valor_base_calculo_portal", "valor_imposto_portal",
        "tipo_frete_portal", "produto_portal", "quantidade_portal",
        "valor_unitario_portal", "vinculo", "canhoto", "pago", "vinculo_norm",
        "canhoto_norm", "pago_norm", "status_portal_ipp", "status_valor_portal",
        "diferenca_kmm_portal", "valor_acordado_coupa", "tarifa_coupa",
        "status_coupa", "camada_match_coupa", "diferenca_kmm_coupa",
        "cte_normal", "ctes_complementares", "complemento_original",
        "observacao_original", "motivo_identificacao_complemento",
        "valor_cte_normal", "valor_cte_complementar", "valor_kmm_total",
        "tem_complemento", "status_complemento_cte", "diferenca_sem_complemento",
        "diferenca_com_complemento",
        "status_final_frete", "status_tarefa", "status_prazo", "prazo_tarefa",
        "dias_restantes", "responsavel", "observacao", "arquivo_kmm",
        "arquivo_portal", "data_processamento", "usuario_processamento", "signature",
    ]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"insert or replace into analise_fretes_ipp ({', '.join(columns)}) values ({placeholders})",
        tuple(payload.get(column) for column in columns),
    )


def run(username: str = "") -> dict[str, Any]:
    started = time.perf_counter()
    started_at = brasilia_now_iso()
    processed_at = started_at
    metrics = {
        "registros_kmm": 0,
        "registros_portal": 0,
        "registros_gerados": 0,
        "portal_sem_kmm": 0,
        "divergencias_valor": 0,
        "tarefas_criadas": 0,
        "tarefas_atualizadas": 0,
        "tarefas_resolvidas": 0,
    }
    with get_connection() as conn:
        try:
            conn.execute("delete from analise_fretes_ipp")
            kmm_rows = _load_kmm_rows(conn)
            portal_rows = _load_portal_rows(conn)
            coupa_by_cte = _load_coupa_by_cte(conn)
            tasks_by_ref = _task_by_reference(conn)
            portal_by_nf: dict[str, dict[str, Any]] = {}
            for row in portal_rows:
                nf = normalizar_nf(row.get("nota_fiscal_norm") or row.get("numero_norm") or row.get("numero_original"))
                if nf and nf not in portal_by_nf:
                    portal_by_nf[nf] = row
            kmm_nfs: set[str] = set()
            active_pending_tasks: set[str] = set()
            active_value_tasks: set[str] = set()
            metrics["registros_kmm"] = len(kmm_rows)
            metrics["registros_portal"] = len(portal_by_nf)

            for row in kmm_rows:
                nf = normalizar_nf(_value(row, "nota_fiscal_norm") or _value(row, "nota_fiscal_individual"))
                if not nf:
                    continue
                kmm_nfs.add(nf)
                cte = str(_value(row, "cte") or _value(row, "cte_numero") or "").strip()
                portal = portal_by_nf.get(nf)
                coupa = coupa_by_cte.get(cte, {})
                value_kmm = _kmm_value(row)
                total_cte = _number(_value(row, "total_conhecimento"))
                value_portal = _portal_value(portal)
                status_portal = _portal_status(portal)
                status_value = _status_value(portal, value_kmm, value_portal)
                diff_portal = value_kmm - value_portal if value_kmm is not None and value_portal is not None else None
                value_coupa = _number(coupa.get("frete_esperado") or coupa.get("frete_calculado"))
                diff_coupa = _number(coupa.get("diferenca_valor") or coupa.get("diferenca"))
                status_coupa = str(coupa.get("status_final") or coupa.get("status") or "")
                final_status = _status_final(status_portal, status_value, status_coupa)
                data_nf = str(_value(row, "data_emissao_dt") or _value(row, "data_emissao") or "")

                task_ref = ""
                task_info: dict[str, Any] = {}
                if status_portal == PENDING_PORTAL_TASK and cte:
                    task_ref = _task_signature(PENDING_PORTAL_TASK, cte, nf)
                    active_pending_tasks.add(task_ref)
                    existing = tasks_by_ref.get(task_ref)
                    deadline = (brasilia_now() + timedelta(days=5)).isoformat(timespec="seconds")
                    upsert_task(
                        conn,
                        {
                            "modulo": "FRETES IPP",
                            "submodulo": "PORTAL IPP",
                            "tipo_tarefa": PENDING_PORTAL_TASK,
                            "origem_tarefa": "AUTOMATICA",
                            "cte": cte,
                            "nf": nf,
                            "chave_referencia": task_ref,
                            "valor_referencia": value_kmm,
                            "valor_faturado": value_kmm,
                            "data_base": data_nf or processed_at,
                            "data_emissao_nf": data_nf,
                            "prazo_limite": deadline,
                            "status_tarefa": "ABERTA",
                            "responsavel": username,
                            "observacao": "NF com CT-e no KMM/FAT sem registro no Portal IPP.",
                            "cliente": _value(row, "cliente") or _value(row, "cobranca"),
                            "placa": _value(row, "placa"),
                            "origem": _value(row, "origem"),
                            "destino": _value(row, "destino"),
                            "produto": _value(row, "produto") or _value(row, "mercadoria"),
                            "usuario_criacao": username,
                            "usuario_atualizacao": username,
                            "source_table": "analise_fretes_ipp",
                            "source_signature": f"fretes_ipp:{cte}:{nf}",
                        },
                    )
                    metrics["tarefas_criadas" if not existing else "tarefas_atualizadas"] += 1
                    task_info = existing or {"status_tarefa": "ABERTA", "status_prazo": "VENCENDO EM 5 DIAS", "prazo_limite": deadline, "dias_restantes": 5, "responsavel": username}

                if status_value == "VALOR PORTAL DIVERGENTE" and portal and abs(diff_portal or 0) > VALUE_TOLERANCE:
                    metrics["divergencias_valor"] += 1
                    task_ref = _task_signature(VALUE_FIX_TASK, cte, nf)
                    active_value_tasks.add(task_ref)
                    existing = tasks_by_ref.get(task_ref)
                    deadline, days_left, prazo_label = complemento_prazo_status(data_nf or processed_at)
                    if not deadline:
                        deadline = (brasilia_now() + timedelta(days=60)).isoformat(timespec="seconds")
                        days_left = 60
                        prazo_label = "NO PRAZO"
                    upsert_task(
                        conn,
                        {
                            "modulo": "FRETES IPP",
                            "submodulo": "CORRECAO DE VALOR",
                            "tipo_tarefa": VALUE_FIX_TASK,
                            "origem_tarefa": "AUTOMATICA",
                            "cte": cte,
                            "nf": nf,
                            "chave_referencia": task_ref,
                            "valor_referencia": abs(diff_portal or 0),
                            "valor_faturado": value_kmm,
                            "data_base": data_nf or processed_at,
                            "data_emissao_nf": data_nf,
                            "prazo_limite": deadline,
                            "status_tarefa": "ABERTA",
                            "responsavel": username,
                            "observacao": "Divergencia entre valor faturado KMM e valor lancado no Portal IPP.",
                            "cliente": _value(row, "cliente") or _value(row, "cobranca"),
                            "placa": _value(row, "placa"),
                            "origem": _value(row, "origem"),
                            "destino": _value(row, "destino"),
                            "produto": _value(row, "produto") or _value(row, "mercadoria"),
                            "usuario_criacao": username,
                            "usuario_atualizacao": username,
                            "source_table": "analise_fretes_ipp",
                            "source_signature": f"fretes_ipp:{cte}:{nf}",
                        },
                    )
                    metrics["tarefas_criadas" if not existing else "tarefas_atualizadas"] += 1
                    task_info = existing or {"status_tarefa": "ABERTA", "status_prazo": prazo_label, "prazo_limite": deadline, "dias_restantes": days_left, "responsavel": username}

                task = task_info or (tasks_by_ref.get(task_ref, {}) if task_ref else {})
                _insert_analysis(
                    conn,
                    {
                        "tipo_registro": "KMM",
                        "data_emissao": data_nf,
                        "data_emissao_nf": data_nf,
                        "data_portal": _value(portal, "data_emissao_portal_dt") if portal else "",
                        "cte": cte,
                        "nota_fiscal": _value(row, "nota_fiscal_individual") or nf,
                        "nota_fiscal_norm": nf,
                        "cliente": _value(row, "cliente") or _value(row, "cobranca"),
                        "origem": _value(row, "origem"),
                        "destino": _value(row, "destino"),
                        "municipio_remetente": _value(row, "municipio_remetente"),
                        "municipio_destinatario": _value(row, "municipio_destinatario"),
                        "produto": _value(row, "produto") or _value(row, "mercadoria"),
                        "produto_norm": _value(row, "produto_norm"),
                        "operacao": _value(row, "operacao_norm") or _value(row, "tabela_frete"),
                        "placa": _value(row, "placa"),
                        "motorista": _value(row, "motorista"),
                        "volume": _number(_value(row, "volume")),
                        "volume_normalizado": _number(_value(row, "volume_normalizado")),
                        "valor_faturado_kmm": value_kmm,
                        "valor_total_cte_kmm": total_cte,
                        "frete_unitario": _number(_value(row, "frete_unitario")),
                        "base_icms": _number(_value(row, "base_icms")),
                        "valor_icms": _number(_value(row, "valor_icms")),
                        "numero_portal": _value(portal, "numero_original") if portal else "",
                        "valor_portal": value_portal,
                        "valor_total_portal": _number(_value(portal, "valor_portal_total")) if portal else None,
                        "valor_pedagio_portal": _number(_value(portal, "valor_portal_pedagio")) if portal else None,
                        "valor_base_calculo_portal": _number(_value(portal, "valor_portal_base_calculo")) if portal else None,
                        "valor_imposto_portal": _number(_value(portal, "valor_portal_imposto")) if portal else None,
                        "tipo_frete_portal": _value(portal, "tipo_frete") if portal else "",
                        "produto_portal": _value(portal, "produto") if portal else "",
                        "quantidade_portal": _number(_value(portal, "quantidade")) if portal else None,
                        "valor_unitario_portal": _number(_value(portal, "valor_unitario_frete")) if portal else None,
                        "vinculo": _value(portal, "vinculo") if portal else "",
                        "canhoto": _value(portal, "canhoto") if portal else "",
                        "pago": _value(portal, "pago") if portal else "",
                        "vinculo_norm": _status_flag(_value(portal, "vinculo_norm") or _value(portal, "vinculo")) if portal else "VAZIO",
                        "canhoto_norm": _status_flag(_value(portal, "canhoto_norm") or _value(portal, "canhoto")) if portal else "VAZIO",
                        "pago_norm": _status_flag(_value(portal, "pago_norm") or _value(portal, "pago")) if portal else "VAZIO",
                        "status_portal_ipp": status_portal,
                        "status_valor_portal": status_value,
                        "diferenca_kmm_portal": diff_portal,
                        "valor_acordado_coupa": value_coupa,
                        "tarifa_coupa": _number(coupa.get("tarifa_coupa")),
                        "status_coupa": status_coupa,
                        "camada_match_coupa": coupa.get("camada_match"),
                        "diferenca_kmm_coupa": diff_coupa,
                        "cte_normal": _value(row, "cte_normal") or cte,
                        "ctes_complementares": _value(row, "ctes_complementares"),
                        "complemento_original": _value(row, "complemento_original"),
                        "observacao_original": _value(row, "observacao_original"),
                        "motivo_identificacao_complemento": _value(row, "motivo_identificacao_complemento"),
                        "valor_cte_normal": _number(_value(row, "valor_cte_normal")),
                        "valor_cte_complementar": _number(_value(row, "valor_cte_complementar")),
                        "valor_kmm_total": value_kmm,
                        "tem_complemento": _value(row, "tem_complemento"),
                        "status_complemento_cte": _value(row, "status_complemento_cte"),
                        "diferenca_sem_complemento": (_number(_value(row, "valor_cte_normal")) - value_coupa) if _number(_value(row, "valor_cte_normal")) is not None and value_coupa is not None else None,
                        "diferenca_com_complemento": diff_coupa,
                        "status_final_frete": final_status,
                        "status_tarefa": task.get("status_tarefa", ""),
                        "status_prazo": task.get("status_prazo", ""),
                        "prazo_tarefa": task.get("prazo_limite", ""),
                        "dias_restantes": task.get("dias_restantes"),
                        "responsavel": task.get("responsavel", ""),
                        "observacao": "Coupa nao importada. A validacao de valor acordado nao sera exibida." if not coupa_by_cte else "",
                        "arquivo_kmm": _value(row, "arquivo_origem"),
                        "arquivo_portal": _value(portal, "arquivo_origem") if portal else "",
                        "data_processamento": processed_at,
                        "usuario_processamento": username,
                        "signature": f"fretes_ipp:kmm:{cte}:{nf}",
                    },
                )
                metrics["registros_gerados"] += 1

            for nf, portal in portal_by_nf.items():
                if nf in kmm_nfs:
                    continue
                metrics["portal_sem_kmm"] += 1
                status_portal = _portal_status(portal)
                _insert_analysis(
                    conn,
                    {
                        "tipo_registro": "PORTAL_SEM_KMM",
                        "data_portal": _value(portal, "data_emissao_portal_dt"),
                        "nota_fiscal": _value(portal, "numero_original") or nf,
                        "nota_fiscal_norm": nf,
                        "numero_portal": _value(portal, "numero_original") or nf,
                        "valor_portal": _portal_value(portal),
                        "valor_total_portal": _number(_value(portal, "valor_portal_total")),
                        "valor_pedagio_portal": _number(_value(portal, "valor_portal_pedagio")),
                        "valor_base_calculo_portal": _number(_value(portal, "valor_portal_base_calculo")),
                        "valor_imposto_portal": _number(_value(portal, "valor_portal_imposto")),
                        "tipo_frete_portal": _value(portal, "tipo_frete"),
                        "produto_portal": _value(portal, "produto"),
                        "produto": _value(portal, "produto"),
                        "produto_norm": _value(portal, "produto_norm"),
                        "quantidade_portal": _number(_value(portal, "quantidade")),
                        "valor_unitario_portal": _number(_value(portal, "valor_unitario_frete")),
                        "vinculo": _value(portal, "vinculo"),
                        "canhoto": _value(portal, "canhoto"),
                        "pago": _value(portal, "pago"),
                        "vinculo_norm": _status_flag(_value(portal, "vinculo_norm") or _value(portal, "vinculo")),
                        "canhoto_norm": _status_flag(_value(portal, "canhoto_norm") or _value(portal, "canhoto")),
                        "pago_norm": _status_flag(_value(portal, "pago_norm") or _value(portal, "pago")),
                        "status_portal_ipp": status_portal,
                        "status_valor_portal": "NAO COMPARADO",
                        "status_final_frete": "PORTAL SEM KMM",
                        "observacao": "Nota existe no Portal IPP, mas nao foi localizada no KMM/FAT.",
                        "arquivo_portal": _value(portal, "arquivo_origem"),
                        "data_processamento": processed_at,
                        "usuario_processamento": username,
                        "signature": f"fretes_ipp:portal_sem_kmm:{nf}",
                    },
                )
                metrics["registros_gerados"] += 1

            metrics["tarefas_resolvidas"] += _resolve_missing_tasks(
                conn,
                active_pending_tasks,
                PENDING_PORTAL_TASK,
                username,
                "NF localizada no Portal IPP apos nova importacao.",
            )
            metrics["tarefas_resolvidas"] += _resolve_missing_tasks(
                conn,
                active_value_tasks,
                VALUE_FIX_TASK,
                username,
                "Divergencia de valor nao localizada no recalculo atual.",
            )
            status = "SUCESSO"
            message = ""
        except Exception as exc:
            status = "ERRO"
            message = str(exc)
            raise
        finally:
            finished = brasilia_now_iso()
            conn.execute(
                """
                insert or replace into fretes_ipp_logs (
                    data_hora_inicio, data_hora_fim, usuario, status,
                    registros_kmm, registros_portal, registros_gerados,
                    portal_sem_kmm, divergencias_valor, tarefas_criadas,
                    tarefas_atualizadas, tarefas_resolvidas, mensagem_erro,
                    duracao_segundos, signature, sync_status, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NOVO_LOCAL', ?, ?)
                """,
                (
                    started_at,
                    finished,
                    username,
                    status,
                    metrics["registros_kmm"],
                    metrics["registros_portal"],
                    metrics["registros_gerados"],
                    metrics["portal_sem_kmm"],
                    metrics["divergencias_valor"],
                    metrics["tarefas_criadas"],
                    metrics["tarefas_atualizadas"],
                    metrics["tarefas_resolvidas"],
                    message,
                    round(time.perf_counter() - started, 3),
                    f"fretes_ipp_log:{started_at}",
                    finished,
                    finished,
                ),
            )
    return {"status": "SUCESSO", **metrics}


def load_analysis() -> pd.DataFrame:
    with get_connection() as conn:
        rows = conn.execute("select * from analise_fretes_ipp order by data_emissao desc, cte, nota_fiscal").fetchall()
    return pd.DataFrame([dict(row) for row in rows])


def load_tasks() -> pd.DataFrame:
    with get_connection() as conn:
        rows = conn.execute(
            """
            select *
            from tarefas_operacionais
            where modulo in ('FRETES IPP', 'Fretes IPP')
            order by prazo_limite, data_criacao desc
            """
        ).fetchall()
    return pd.DataFrame([dict(row) for row in rows])


def load_logs(limit: int = 50) -> pd.DataFrame:
    with get_connection() as conn:
        rows = conn.execute("select * from fretes_ipp_logs order by id desc limit ?", (limit,)).fetchall()
    return pd.DataFrame([dict(row) for row in rows])


def summary_cards(df: pd.DataFrame, tasks: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {
            "Valor Faturado KMM": 0,
            "Valor Lancado Portal": 0,
            "Diferenca KMM x Portal": 0,
            "Pendentes de Lancamento": 0,
            "Pagamento Programado": 0,
            "Pagamento OK": 0,
            "Divergencias de Valor": 0,
            "Portal sem KMM": 0,
            "Tarefas abertas": 0,
            "Tarefas vencidas": 0,
        }
    kmm = df[df.get("tipo_registro", pd.Series("", index=df.index)).fillna("").astype(str).eq("KMM")].copy()
    cte_key = kmm.get("cte", pd.Series("", index=kmm.index)).fillna("").astype(str)
    nf_key = kmm.get("nota_fiscal_norm", pd.Series("", index=kmm.index)).fillna("").astype(str)
    kmm["_financial_key"] = cte_key.where(cte_key.ne(""), nf_key)
    if not kmm.empty:
        financial = kmm.assign(
            _valor_kmm=pd.to_numeric(kmm.get("valor_faturado_kmm", pd.Series(0, index=kmm.index)), errors="coerce").fillna(0),
            _valor_portal=pd.to_numeric(kmm.get("valor_portal", pd.Series(0, index=kmm.index)), errors="coerce").fillna(0),
        )
        grouped_financial = financial.groupby("_financial_key", dropna=False)[["_valor_kmm", "_valor_portal"]].max()
        value_kmm = grouped_financial["_valor_kmm"].sum()
        value_portal = grouped_financial["_valor_portal"].sum()
    else:
        value_kmm = 0
        value_portal = 0
    task_status = tasks.get("status_tarefa", pd.Series("", index=tasks.index)).fillna("").astype(str) if not tasks.empty else pd.Series(dtype=str)
    return {
        "Valor Faturado KMM": float(value_kmm),
        "Valor Lancado Portal": float(value_portal),
        "Diferenca KMM x Portal": float(value_kmm - value_portal),
        "Pendentes de Lancamento": int(kmm.get("status_portal_ipp", pd.Series("", index=kmm.index)).eq(PENDING_PORTAL_TASK).sum()),
        "Pagamento Programado": int(kmm.get("status_portal_ipp", pd.Series("", index=kmm.index)).eq("PAGAMENTO PROGRAMADO").sum()),
        "Pagamento OK": int(kmm.get("status_portal_ipp", pd.Series("", index=kmm.index)).eq("PAGAMENTO OK").sum()),
        "Divergencias de Valor": int(kmm.get("status_valor_portal", pd.Series("", index=kmm.index)).eq("VALOR PORTAL DIVERGENTE").sum()),
        "Portal sem KMM": int(df.get("status_final_frete", pd.Series("", index=df.index)).eq("PORTAL SEM KMM").sum()),
        "Tarefas abertas": int((~task_status.isin(["RESOLVIDA", "CANCELADA"])).sum()) if not tasks.empty else 0,
        "Tarefas vencidas": int(tasks.get("status_prazo", pd.Series("", index=tasks.index)).eq("VENCIDA").sum()) if not tasks.empty else 0,
    }


def diagnostics() -> dict[str, pd.DataFrame]:
    with get_connection() as conn:
        kmm = pd.read_sql_query("select * from base_kmm_fat_notas_normalizadas where coalesce(status_registro, 'ATIVO') = 'ATIVO' limit 200", conn)
        portal = pd.read_sql_query("select * from base_portal_ipp_normalizada where coalesce(status_registro, 'ATIVO') = 'ATIVO' limit 200", conn)
        analysis = pd.read_sql_query("select * from analise_fretes_ipp", conn)
        logs = pd.read_sql_query("select * from fretes_ipp_logs order by id desc limit 20", conn)
    dup_portal = portal.groupby("nota_fiscal_norm", dropna=False).size().reset_index(name="Quantidade") if not portal.empty and "nota_fiscal_norm" in portal.columns else pd.DataFrame()
    dup_portal = dup_portal[dup_portal["Quantidade"].gt(1)] if not dup_portal.empty else dup_portal
    dup_kmm = kmm.groupby("nota_fiscal_norm", dropna=False).size().reset_index(name="Quantidade") if not kmm.empty and "nota_fiscal_norm" in kmm.columns else pd.DataFrame()
    dup_kmm = dup_kmm[dup_kmm["Quantidade"].gt(1)] if not dup_kmm.empty else dup_kmm
    multi_cte = kmm.groupby("cte", dropna=False)["nota_fiscal_norm"].nunique().reset_index(name="NFs") if not kmm.empty and {"cte", "nota_fiscal_norm"}.issubset(kmm.columns) else pd.DataFrame()
    multi_cte = multi_cte[multi_cte["NFs"].gt(1)] if not multi_cte.empty else multi_cte
    without_nf = analysis[analysis.get("nota_fiscal_norm", pd.Series("", index=analysis.index)).fillna("").astype(str).eq("")] if not analysis.empty else pd.DataFrame()
    summary = pd.DataFrame(
        [
            {"Indicador": "Registros KMM originais", "Valor": len(kmm)},
            {"Indicador": "Notas KMM normalizadas", "Valor": int(kmm.get("nota_fiscal_norm", pd.Series(dtype=str)).fillna("").astype(str).ne("").sum()) if not kmm.empty else 0},
            {"Indicador": "Notas Portal normalizadas", "Valor": int(portal.get("nota_fiscal_norm", pd.Series(dtype=str)).fillna("").astype(str).ne("").sum()) if not portal.empty else 0},
            {"Indicador": "NFs KMM encontradas no Portal", "Valor": int(analysis.get("status_portal_ipp", pd.Series(dtype=str)).fillna("").astype(str).ne(PENDING_PORTAL_TASK).sum()) if not analysis.empty else 0},
            {"Indicador": "NFs KMM nao encontradas no Portal", "Valor": int(analysis.get("status_portal_ipp", pd.Series(dtype=str)).eq(PENDING_PORTAL_TASK).sum()) if not analysis.empty else 0},
            {"Indicador": "NFs Portal sem KMM", "Valor": int(analysis.get("status_final_frete", pd.Series(dtype=str)).eq("PORTAL SEM KMM").sum()) if not analysis.empty else 0},
            {"Indicador": "Divergencias de valor", "Valor": int(analysis.get("status_valor_portal", pd.Series(dtype=str)).eq("VALOR PORTAL DIVERGENTE").sum()) if not analysis.empty else 0},
        ]
    )
    return {
        "Resumo": summary,
        "Amostra KMM normalizada": kmm,
        "Amostra Portal normalizada": portal,
        "Duplicidades Portal por NF": dup_portal,
        "Duplicidades KMM por NF": dup_kmm,
        "CT-es com multiplas NFs": multi_cte,
        "NFs sem normalizacao": without_nf,
        "Logs": logs,
    }
