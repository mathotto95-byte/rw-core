from __future__ import annotations

from statistics import median

from src.config.settings import load_config
from rw_core.inconsistencies.service import upsert_inconsistency


def route_key(row) -> tuple:
    return (
        row["origem"] or "",
        row["destino"] or "",
        row["cobranca"] or "",
        row["cliente"] or "",
        row["produto"] or row["mercadoria"] or "",
    )


def run(conn) -> set[str]:
    tolerance = load_config()["tolerances"]["freight_unit"]
    conn.execute("delete from analise_frete_unitario")
    active: set[str] = set()
    rows = conn.execute(
        """
        select * from base_kmm_original
        where frete_unitario is not null
          and status_complemento_normalizado = 'NORMAL'
          and coalesce(status_registro, 'ATIVO') = 'ATIVO'
        order by emissao_cte, id
        """
    ).fetchall()
    groups: dict[tuple, list[float]] = {}
    for row in rows:
        key = route_key(row)
        history = groups.get(key, [])
        current = row["frete_unitario"] or 0
        if len(history) < 1:
            reference = None
            diff = None
            status = "SEM HISTORICO PARA COMPARACAO"
            observation = "Primeiro registro desta rota/cobranca/produto."
        else:
            reference = median(history)
            diff = current - reference
            status = "FRETE OK" if abs(diff) <= tolerance else "FRETE UNITARIO DIVERGENTE DO HISTORICO"
            observation = ""
        signature = f"frete_unitario:{row['id']}"
        conn.execute(
            """
            insert or replace into analise_frete_unitario (
                kmm_id, cliente, cobranca, origem, destino, produto, frete_unitario,
                frete_referencia, diferenca, status_analise, observacao, signature
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["cliente"],
                row["cobranca"],
                row["origem"],
                row["destino"],
                row["produto"] or row["mercadoria"],
                current,
                reference,
                diff,
                status,
                observation,
                signature,
            ),
        )
        if status == "FRETE UNITARIO DIVERGENTE DO HISTORICO":
            active.add(
                upsert_inconsistency(
                    conn,
                    {
                        "signature": f"inc:frete_unitario:{row['id']}",
                        "modulo_origem": "Frete Unitario",
                        "tipo_inconsistencia": "Frete unitario divergente",
                        "cliente": row["cliente"],
                        "placa": row["placa"],
                        "nota_fiscal": row["nota_fiscal"],
                        "cte": row["cte_numero"],
                        "origem": row["origem"],
                        "destino": row["destino"],
                        "cobranca": row["cobranca"],
                        "produto": row["produto"] or row["mercadoria"],
                        "valor_envolvido": current,
                        "diferenca_valor": diff,
                        "prioridade": "MEDIA",
                        "observacao": f"Referencia: {reference}; atual: {current}.",
                        "arquivo_origem": row["arquivo_origem"],
                    },
                )
            )
        groups.setdefault(key, []).append(current)
    return active
