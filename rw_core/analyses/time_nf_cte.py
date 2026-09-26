from __future__ import annotations

from datetime import timedelta

from rw_core.inconsistencies.service import upsert_inconsistency
from rw_core.normalizers.fields import to_datetime


def classify_range(minutes: float | None) -> str:
    if minutes is None:
        return ""
    if minutes < 10:
        return "Excelente"
    if minutes <= 30:
        return "No prazo"
    if minutes <= 60:
        return "Prazo 2"
    return "Verificar"


def nf_time_parts(value: object) -> tuple[str, str, str]:
    parsed = to_datetime(str(value or ""))
    if not parsed:
        return "", "", "NAO"
    hour = parsed.strftime("%H:%M:%S")
    valid = "NAO" if hour in {"00:00:00"} else "SIM"
    return parsed.date().isoformat(), hour, valid


def fuso_adjustment(uf_origem: object, nf_date):
    uf = str(uf_origem or "").strip().upper()
    if nf_date and uf in {"MT", "MS"}:
        return nf_date + timedelta(minutes=60), "Sim", 60, "UF origem MT/MS - conversao para horario de Brasilia"
    return nf_date, "Nao", 0, ""


def run(conn) -> dict:
    conn.execute("delete from analise_tempo_nf_cte")
    active: set[str] = set()
    metrics = {
        "quantidade_ajuste_fuso": 0,
        "quantidade_horario_0000": 0,
        "quantidade_gerada": 0,
    }
    rows = conn.execute(
        """
        select a.numero_nf, a.emissao_nf, a.status_analise, k.*
        from analise_nsdocs_x_kmm a
        left join base_kmm_notas_normalizadas k
          on k.nota_fiscal_normalizada = a.numero_nf
         and k.status_complemento_normalizado = 'NORMAL'
         and coalesce(k.status_registro, 'ATIVO') = 'ATIVO'
        where a.status_analise in ('EMITIDA', 'NF COM EMISSAO ZERADA/INVALIDA', 'FORA DA REGRA - COMPLEMENTO')
        """
    ).fetchall()
    for row in rows:
        nf_date = to_datetime(row["emissao_nf"])
        cte_date = to_datetime(row["emissao_cte"])
        nf_adjusted, fuso_aplicado, fuso_minutes, fuso_reason = fuso_adjustment(row["uf_origem"], nf_date)
        original_minutes = adjusted_minutes = hours = None
        if row["status_analise"] == "FORA DA REGRA - COMPLEMENTO":
            status = "FORA DA REGRA - COMPLEMENTO"
            observation = "CT-e complementar fora do calculo principal."
        elif not nf_date:
            status = "NF COM EMISSAO ZERADA/INVALIDA"
            observation = "Emissao NF ausente ou invalida."
        elif not cte_date:
            status = "SEM DATA DE EMISSAO DO CT-e"
            observation = "CT-e sem data valida."
        else:
            original_minutes = (cte_date - nf_date).total_seconds() / 60
            adjusted_minutes = (cte_date - nf_adjusted).total_seconds() / 60 if nf_adjusted else original_minutes
            hours = adjusted_minutes / 60 if adjusted_minutes is not None else None
            status = classify_range(adjusted_minutes)
            observation = ""
        if fuso_aplicado == "Sim":
            metrics["quantidade_ajuste_fuso"] += 1
        signature = f"tempo_nf_cte:{row['numero_nf']}:{row['cte_numero']}"
        nf_date_only, nf_time_only, nf_time_valid = nf_time_parts(row["emissao_nf"])
        if nf_time_only == "00:00:00":
            metrics["quantidade_horario_0000"] += 1
        conn.execute(
            """
            insert or replace into analise_tempo_nf_cte (
                numero_nf, cte_numero, emissao_nf, emissao_cte, cliente, cobranca,
                origem, destino, placa, inserido_por, minutos, horas, faixa_tempo,
                status_prazo, observacao, uf_origem, data_emissao_nf_original,
                data_emissao_nf_ajustada, ajuste_fuso_aplicado, minutos_ajuste_fuso,
                motivo_ajuste_fuso, tempo_original_minutos, tempo_ajustado_minutos,
                data_emissao_nf, hora_emissao_nf, emissao_nf_tem_horario_valido,
                signature
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["numero_nf"],
                row["cte_numero"],
                row["emissao_nf"],
                row["emissao_cte"],
                row["cobranca"],
                row["cobranca"],
                row["origem"],
                row["destino"],
                row["placa"],
                row["inserido_por"],
                adjusted_minutes,
                hours,
                classify_range(adjusted_minutes),
                status,
                observation,
                row["uf_origem"],
                row["emissao_nf"],
                nf_adjusted.isoformat(timespec="seconds") if nf_adjusted else "",
                fuso_aplicado,
                fuso_minutes,
                fuso_reason,
                original_minutes,
                adjusted_minutes,
                nf_date_only,
                nf_time_only,
                nf_time_valid,
                signature,
            ),
        )
        metrics["quantidade_gerada"] += 1
        if status == "Verificar":
            active.add(
                upsert_inconsistency(
                    conn,
                    {
                        "signature": f"inc:tempo_nf_cte:fora_prazo:{row['numero_nf']}:{row['cte_numero']}",
                        "modulo_origem": "Tempo NF x CT-e",
                        "tipo_inconsistencia": "Tempo NF x CT-e acima de 1 hora",
                        "nota_fiscal": row["numero_nf"],
                        "cte": row["cte_numero"],
                        "placa": row["placa"],
                        "origem": row["origem"],
                        "destino": row["destino"],
                        "cobranca": row["cobranca"],
                        "tempo_envolvido": adjusted_minutes,
                        "prioridade": "MEDIA",
                        "observacao": f"{adjusted_minutes:.0f} minutos entre NF e CT-e. Verificar emissao acima de 1 hora.",
                        "arquivo_origem": row["arquivo_origem"],
                    },
                )
            )
    metrics["registros_gerados"] = metrics["quantidade_gerada"]
    metrics["registros_processados"] = len(rows)
    metrics["active"] = active
    return metrics
