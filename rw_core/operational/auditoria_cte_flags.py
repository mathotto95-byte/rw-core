from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso
from typing import Any

import pandas as pd

from rw_core.database.connection import get_connection
from rw_core.database.schema import adapt_sql


FLAGS_AUDITORIA_CTE_KMM = [
    "CONFERIDO",
    "DIVERGENCIA VALIDADA",
    "AGUARDANDO CORRECAO",
    "CORRIGIDO",
    "IGNORAR NA ANALISE",
    "ANALISAR MANUALMENTE",
]


def normalizar_documento(value: Any, digits_only: bool = True) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    text = re.sub(r"\s+", "", text)
    if digits_only:
        digits = re.sub(r"\D+", "", text)
        return digits or text
    return text


def ensure_auditoria_cte_flags_tables(conn) -> None:
    conn.execute(
        adapt_sql(
            conn,
            """
            create table if not exists auditoria_cte_flags (
                id integer primary key autoincrement,
                registro_key text not null,
                id_registro text,
                cte_norm text not null default '',
                nota_fiscal_norm text not null default '',
                chave_nfe text not null default '',
                flag text not null,
                marcado integer not null default 1,
                observacao text,
                usuario text,
                data_hora text not null,
                origem_acao text not null,
                lote_acao text,
                data_criacao text,
                data_atualizacao text
            )
            """,
        )
    )
    conn.execute(
        adapt_sql(
            conn,
            """
            create table if not exists historico_auditoria_cte_flags (
                id integer primary key autoincrement,
                data_hora text not null,
                usuario text,
                acao text not null,
                flag text not null,
                quantidade_linhas integer,
                quantidade_ctes integer,
                quantidade_nfs integer,
                quantidade_afetados integer,
                filtros_aplicados_json text,
                observacao text,
                lote_acao text,
                origem_painel text
            )
            """,
        )
    )
    conn.execute("create unique index if not exists ux_auditoria_cte_flags_registro_flag on auditoria_cte_flags(registro_key, flag)")
    conn.execute("create index if not exists idx_auditoria_cte_flags_marcado on auditoria_cte_flags(marcado)")
    conn.execute("create index if not exists idx_auditoria_cte_flags_flag on auditoria_cte_flags(flag)")
    conn.execute("create index if not exists idx_hist_auditoria_cte_flags_data on historico_auditoria_cte_flags(data_hora)")


def _first_value(row: pd.Series, aliases: list[str]) -> Any:
    for alias in aliases:
        if alias in row.index:
            value = row.get(alias)
            if value is not None and not pd.isna(value) and str(value).strip():
                return value
    return ""


def _registro_key(payload: dict[str, str]) -> str:
    if payload.get("id_registro"):
        return f"id:{payload['id_registro']}"
    if payload.get("chave_nfe"):
        return f"chave:{payload['chave_nfe']}"
    if payload.get("cte_norm") or payload.get("nota_fiscal_norm"):
        return f"cte_nf:{payload.get('cte_norm', '')}:{payload.get('nota_fiscal_norm', '')}"
    parts = [
        payload.get("cte_norm", ""),
        payload.get("nota_fiscal_norm", ""),
        payload.get("placa_norm", ""),
        payload.get("data_emissao", ""),
    ]
    return "fallback:" + ":".join(parts)


def obter_chaves_registros_auditoria(df: pd.DataFrame) -> list[dict[str, str]]:
    registros: list[dict[str, str]] = []
    if df is None or df.empty:
        return registros

    for _, row in df.iterrows():
        id_registro = normalizar_documento(
            _first_value(row, ["id_registro", "id_linha", "signature", "id"]),
            digits_only=False,
        )
        cte_norm = normalizar_documento(_first_value(row, ["cte_norm", "numero_cte_norm", "cte_numero", "CT-e", "CTE", "cte"]))
        nota_fiscal_norm = normalizar_documento(
            _first_value(row, ["nota_fiscal_norm", "notas_fiscais", "Nota Fiscal", "NF", "nf", "nota_fiscal"])
        )
        chave_nfe = normalizar_documento(_first_value(row, ["chave_nfe", "chave_acesso", "chave_cte", "Chave NFE"]))
        payload = {
            "id_registro": id_registro,
            "cte_norm": cte_norm,
            "nota_fiscal_norm": nota_fiscal_norm,
            "chave_nfe": chave_nfe,
            "placa_norm": normalizar_documento(_first_value(row, ["placa", "Placa"]), digits_only=False),
            "data_emissao": str(_first_value(row, ["emissao_cte", "Data", "data_emissao_cte"]) or "").strip(),
        }
        payload["registro_key"] = _registro_key(payload)
        if payload["registro_key"] not in {"id:", "cte_nf::", "fallback::::"}:
            registros.append(payload)
    return registros


def carregar_flags_ativas() -> pd.DataFrame:
    try:
        with get_connection() as conn:
            ensure_auditoria_cte_flags_tables(conn)
            rows = conn.execute("select * from auditoria_cte_flags order by data_hora desc, id desc").fetchall()
            return pd.DataFrame([dict(row) for row in rows])
    except Exception:
        return pd.DataFrame()


def carregar_historico_flags(limit: int = 20) -> pd.DataFrame:
    try:
        with get_connection() as conn:
            ensure_auditoria_cte_flags_tables(conn)
            rows = conn.execute(
                """
                select data_hora, usuario, acao, flag, quantidade_linhas,
                       quantidade_ctes, quantidade_nfs, quantidade_afetados,
                       observacao, filtros_aplicados_json, lote_acao
                from historico_auditoria_cte_flags
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
            return pd.DataFrame([dict(row) for row in rows])
    except Exception:
        return pd.DataFrame()


def get_flags_disponiveis(flags_df: pd.DataFrame | None = None) -> list[str]:
    saved: list[str] = []
    if flags_df is not None and not flags_df.empty and "flag" in flags_df.columns:
        saved = [str(flag).strip() for flag in flags_df["flag"].dropna().unique() if str(flag).strip()]
    return sorted(set(FLAGS_AUDITORIA_CTE_KMM + saved))


def aplicar_flags_na_view(view: pd.DataFrame, flags_df: pd.DataFrame) -> pd.DataFrame:
    enriched = view.copy()
    registros = obter_chaves_registros_auditoria(enriched)
    keys_by_index = [registro["registro_key"] for registro in registros]
    if len(keys_by_index) != len(enriched):
        keys_by_index = []
        for _, row in enriched.iterrows():
            registro = obter_chaves_registros_auditoria(pd.DataFrame([row]))
            keys_by_index.append(registro[0]["registro_key"] if registro else "")
    enriched["registro_key"] = keys_by_index

    for column, default in {
        "flags_ativas": "",
        "quantidade_flags": 0,
        "ultima_flag_data": "",
        "ultima_flag_usuario": "",
        "ultima_flag_observacao": "",
    }.items():
        enriched[column] = default

    if flags_df is None or flags_df.empty:
        return enriched
    active = flags_df[pd.to_numeric(flags_df.get("marcado", 0), errors="coerce").fillna(0).astype(int).eq(1)].copy()
    if active.empty:
        return enriched
    active["registro_key"] = active["registro_key"].fillna("").astype(str)
    active["data_hora_sort"] = pd.to_datetime(active.get("data_hora"), errors="coerce")
    grouped = active.sort_values("data_hora_sort").groupby("registro_key", dropna=False)
    summary = grouped.agg(
        flags_ativas=("flag", lambda values: " | ".join(sorted(set(str(value) for value in values if str(value).strip())))),
        quantidade_flags=("flag", lambda values: len(set(str(value) for value in values if str(value).strip()))),
        ultima_flag_data=("data_hora", "last"),
        ultima_flag_usuario=("usuario", "last"),
        ultima_flag_observacao=("observacao", "last"),
    )
    enriched = enriched.drop(columns=["flags_ativas", "quantidade_flags", "ultima_flag_data", "ultima_flag_usuario", "ultima_flag_observacao"]).merge(
        summary,
        how="left",
        left_on="registro_key",
        right_index=True,
    )
    enriched["flags_ativas"] = enriched["flags_ativas"].fillna("")
    enriched["quantidade_flags"] = pd.to_numeric(enriched["quantidade_flags"], errors="coerce").fillna(0).astype(int)
    enriched["ultima_flag_data"] = enriched["ultima_flag_data"].fillna("")
    enriched["ultima_flag_usuario"] = enriched["ultima_flag_usuario"].fillna("")
    enriched["ultima_flag_observacao"] = enriched["ultima_flag_observacao"].fillna("")
    return enriched


def aplicar_flag_em_massa(
    registros: list[dict[str, str]],
    flag: str,
    marcado: int,
    usuario: str,
    observacao: str,
    filtros_aplicados: dict[str, Any],
    quantidade_linhas: int,
    quantidade_ctes: int,
    quantidade_nfs: int,
) -> tuple[int, str]:
    now = brasilia_now_iso()
    lote_acao = f"auditoria_cte_kmm:{uuid.uuid4().hex[:12]}"
    acao = "MARCAR_FLAG_MASSA_FILTRO" if int(marcado) == 1 else "DESMARCAR_FLAG_MASSA_FILTRO"
    quantidade_afetados = 0
    flag = str(flag or "").strip()
    observacao = str(observacao or "").strip()

    with get_connection() as conn:
        ensure_auditoria_cte_flags_tables(conn)
        for registro in registros:
            existing = conn.execute(
                "select id, marcado, observacao from auditoria_cte_flags where registro_key = ? and flag = ?",
                (registro["registro_key"], flag),
            ).fetchone()
            if existing:
                if (
                    int(existing["marcado"] or 0) == int(marcado)
                    and str(existing["observacao"] or "") == observacao
                    and int(marcado) == 0
                ):
                    continue
                conn.execute(
                    """
                    update auditoria_cte_flags
                    set marcado = ?, observacao = ?, usuario = ?, data_hora = ?,
                        origem_acao = 'MASSA_FILTRO', lote_acao = ?, data_atualizacao = ?
                    where id = ?
                    """,
                    (int(marcado), observacao, usuario, now, lote_acao, now, existing["id"]),
                )
                quantidade_afetados += 1
            elif int(marcado) == 1:
                conn.execute(
                    """
                    insert into auditoria_cte_flags (
                        registro_key, id_registro, cte_norm, nota_fiscal_norm,
                        chave_nfe, flag, marcado, observacao, usuario,
                        data_hora, origem_acao, lote_acao, data_criacao,
                        data_atualizacao
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'MASSA_FILTRO', ?, ?, ?)
                    """,
                    (
                        registro["registro_key"],
                        registro.get("id_registro", ""),
                        registro.get("cte_norm", ""),
                        registro.get("nota_fiscal_norm", ""),
                        registro.get("chave_nfe", ""),
                        flag,
                        int(marcado),
                        observacao,
                        usuario,
                        now,
                        lote_acao,
                        now,
                        now,
                    ),
                )
                quantidade_afetados += 1
        filtros_json = json.dumps(filtros_aplicados, ensure_ascii=False, default=str)
        conn.execute(
            """
            insert into historico_auditoria_cte_flags (
                data_hora, usuario, acao, flag, quantidade_linhas,
                quantidade_ctes, quantidade_nfs, quantidade_afetados,
                filtros_aplicados_json, observacao, lote_acao, origem_painel
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AUDITORIA_CTE_KMM')
            """,
            (
                now,
                usuario,
                acao,
                flag,
                int(quantidade_linhas),
                int(quantidade_ctes),
                int(quantidade_nfs),
                int(quantidade_afetados),
                filtros_json,
                observacao,
                lote_acao,
            ),
        )
        conn.commit()
    return quantidade_afetados, lote_acao


def diagnostico_flags(view: pd.DataFrame, historico: pd.DataFrame) -> dict[str, object]:
    quantidade_flags = pd.to_numeric(view.get("quantidade_flags", pd.Series(0, index=view.index)), errors="coerce").fillna(0)
    ultima_acao = historico.iloc[0].to_dict() if historico is not None and not historico.empty else {}
    return {
        "Registros com flag": int(quantidade_flags.gt(0).sum()),
        "Registros sem flag": int(quantidade_flags.eq(0).sum()),
        "Flags ativas": int(quantidade_flags.sum()),
        "Ultima acao em massa": ultima_acao.get("data_hora", "-"),
        "Registros na ultima acao": int(ultima_acao.get("quantidade_afetados") or 0) if ultima_acao else 0,
    }
