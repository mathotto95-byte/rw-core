from __future__ import annotations


def run_client_indicators(conn) -> None:
    conn.execute("delete from analise_indicadores_cliente")
    rows = conn.execute(
        """
        select coalesce(cliente, cobranca, 'SEM CLIENTE') cliente,
               sum(coalesce(volume, 0)) volume,
               sum(coalesce(total_conhecimento, 0)) valor,
               count(*) quantidade
        from base_kmm_original
        where coalesce(status_registro, 'ATIVO') = 'ATIVO'
        group by coalesce(cliente, cobranca, 'SEM CLIENTE')
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            insert or replace into analise_indicadores_cliente (
                cliente, volume, valor, quantidade, indicador, status, observacao, signature
            ) values (?, ?, ?, ?, 'Consolidado KMM', 'ATIVO', '', ?)
            """,
            (row["cliente"], row["volume"], row["valor"], row["quantidade"], f"cliente:{row['cliente']}"),
        )


def run_plate_indicators(conn) -> None:
    conn.execute("delete from analise_indicadores_placa")
    conn.execute("delete from analise_desempenho_frota")
    rows = conn.execute(
        """
        select coalesce(placa, 'SEM PLACA') placa,
               sum(coalesce(volume, 0)) volume,
               sum(coalesce(total_conhecimento, 0)) valor,
               count(*) quantidade
        from base_kmm_original
        where coalesce(status_registro, 'ATIVO') = 'ATIVO'
        group by coalesce(placa, 'SEM PLACA')
        """
    ).fetchall()
    for row in rows:
        status = "SEM DADOS SUFICIENTES" if row["placa"] == "SEM PLACA" else "BOM"
        values = (
            row["placa"],
            row["volume"],
            row["valor"],
            row["quantidade"],
            "Consolidado KMM",
            status,
            "",
            f"placa:{row['placa']}",
        )
        conn.execute(
            """
            insert or replace into analise_indicadores_placa (
                placa, volume, valor, quantidade, indicador, status, observacao, signature
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        conn.execute(
            """
            insert or replace into analise_desempenho_frota (
                placa, volume, valor, quantidade, indicador, status, observacao, signature
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )


def run_time_indicators(conn) -> None:
    conn.execute("delete from analise_indicadores_tempo")
    rows = conn.execute(
        """
        select coalesce(cobranca, cliente, 'SEM CLIENTE') cliente,
               avg(minutos) media_minutos,
               count(*) quantidade
        from analise_tempo_nf_cte
        where minutos is not null
        group by coalesce(cobranca, cliente, 'SEM CLIENTE')
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            insert or replace into analise_indicadores_tempo (
                cliente, valor, quantidade, indicador, status, observacao, signature
            ) values (?, ?, ?, 'Tempo medio NF x CT-e em minutos', 'ATIVO', '', ?)
            """,
            (row["cliente"], row["media_minutos"], row["quantidade"], f"tempo:{row['cliente']}"),
        )


def run(conn) -> set[str]:
    run_client_indicators(conn)
    run_plate_indicators(conn)
    run_time_indicators(conn)
    return set()
