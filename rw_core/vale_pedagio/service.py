from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso
from pathlib import Path
from typing import Any, BinaryIO

import pandas as pd

from src.config.settings import UPLOADS_DIR, load_config
from rw_core.database.connection import get_connection
from rw_core.importers.excel_importer import read_excel_sheet
from rw_core.normalizers.fields import (
    get_first,
    hash_dict,
    normalize_column_name,
    normalize_location_key,
    normalize_text,
    parse_number,
)
from rw_core.reports.exporter import dataframe_to_excel


MANUAL_TOLL_BASE_TYPE = "Atualização Manual Vale Pedágio"
TOLL_COLUMNS = [
    "Origem",
    "Destino",
    "Placa",
    "Nota Fiscal",
    "Chave de acesso",
    "Data emissao",
    "Km rota",
    "Mes referencia",
    "Quantidade de eixos",
    "Valor atual do vale pedagio",
    "Valor pedagio informado",
    "Valor por eixo",
    "Valor total atualizado",
    "Observacao",
    "Status",
]


def _json_payload(row, field: str) -> dict:
    try:
        return json.loads(row[field] or "{}")
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return {}


def _row_value(row, field: str):
    try:
        value = row[field]
        if value not in [None, ""]:
            return value
    except (IndexError, KeyError):
        pass
    normalized = _json_payload(row, "normalized_json")
    if normalized.get(field) not in [None, ""]:
        return normalized.get(field)
    aliases = load_config().get("column_aliases", {}).get("KMM / LCTE", {}).get(field, [])
    return get_first(_json_payload(row, "original_json"), aliases) if aliases else ""


def _month_reference(value: Any = None) -> str:
    converted = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(converted):
        converted = pd.Timestamp(brasilia_now())
    return converted.strftime("%Y-%m")


def _format_date(value: Any) -> str:
    converted = pd.to_datetime(value, errors="coerce", dayfirst=True)
    return "" if pd.isna(converted) else converted.strftime("%d/%m/%Y")


def pending_toll_rows(deduplicate_routes: bool = True) -> pd.DataFrame:
    with get_connection() as conn:
        rows = conn.execute(
            """
            select *
            from base_kmm_original
            where coalesce(status_registro, 'ATIVO') = 'ATIVO'
              and coalesce(vale_pedagio, 0) = 0
            order by emissao_cte desc, id desc
            """
        ).fetchall()
    records = []
    for row in rows:
        origem = _row_value(row, "origem")
        destino = _row_value(row, "destino")
        emissao = _row_value(row, "emissao_cte")
        records.append(
            {
                "Origem": origem,
                "Destino": destino,
                "Placa": _row_value(row, "placa"),
                "Nota Fiscal": _row_value(row, "nota_fiscal"),
                "Chave de acesso": _row_value(row, "chave_nfe") or _row_value(row, "chave_acesso"),
                "Data emissao": _format_date(emissao),
                "Km rota": _row_value(row, "km"),
                "Mes referencia": _month_reference(emissao),
                "Quantidade de eixos": "",
                "Valor atual do vale pedagio": _row_value(row, "vale_pedagio") or 0,
                "Valor pedagio informado": _row_value(row, "pedagio_informado") or _row_value(row, "pedagio"),
                "Valor por eixo": "",
                "Valor total atualizado": "",
                "Observacao": "",
                "Status": "PLANILHA GERADA",
                "origem_normalizada": normalize_location_key(origem),
                "destino_normalizada": normalize_location_key(destino),
            }
        )
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=TOLL_COLUMNS)
    if deduplicate_routes:
        df = df.drop_duplicates(
            subset=["origem_normalizada", "destino_normalizada", "Km rota", "Mes referencia"],
            keep="first",
        )
    return df[TOLL_COLUMNS].reset_index(drop=True)


def generate_toll_update_excel() -> bytes:
    df = pending_toll_rows(deduplicate_routes=True)
    return dataframe_to_excel({"atualizacao_pedagio": df})


def _save_upload(file: BinaryIO, filename: str) -> str:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = brasilia_now().strftime("%Y%m%d_%H%M%S")
    target = UPLOADS_DIR / f"{timestamp}_{Path(filename).name}"
    file.seek(0)
    with target.open("wb") as output:
        shutil.copyfileobj(file, output)
    return str(target)


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _insert_import_history(conn, filename: str, sheet: str, total: int, username: str, role: str) -> int:
    cursor = conn.execute(
        """
        insert into importacoes (
            nome_arquivo, tipo_base, aba_importada, data_importacao, linhas_lidas,
            linhas_novas, linhas_duplicadas, linhas_erro, status, mensagem_erro,
            usuario, perfil_usuario
        ) values (?, ?, ?, ?, ?, 0, 0, 0, 'EM PROCESSAMENTO', '', ?, ?)
        """,
        (filename, MANUAL_TOLL_BASE_TYPE, sheet, brasilia_now_iso(), total, username, role),
    )
    return int(cursor.lastrowid)


def _update_import_history(conn, import_id: int, new_rows: int, duplicates: int, errors: int, status: str, message: str) -> None:
    conn.execute(
        """
        update importacoes
        set linhas_novas = ?, linhas_duplicadas = ?, linhas_erro = ?, status = ?, mensagem_erro = ?
        where id = ?
        """,
        (new_rows, duplicates, errors, status, message, import_id),
    )


def _insert_imported_file(conn, import_id: int, filename: str, path: str, username: str) -> None:
    conn.execute(
        """
        insert into arquivos_importados (
            importacao_id, nome_arquivo, tipo_base, caminho_armazenado,
            data_importacao, usuario, status, hash_arquivo
        ) values (?, ?, ?, ?, ?, ?, 'IMPORTADO', ?)
        """,
        (
            import_id,
            filename,
            MANUAL_TOLL_BASE_TYPE,
            path,
            brasilia_now_iso(),
            username,
            _file_hash(path),
        ),
    )


def _value(row: dict[str, Any], *aliases: str) -> Any:
    for alias in aliases:
        key = normalize_column_name(alias)
        if key not in row:
            continue
        value = row[key]
        if value in [None, ""]:
            continue
        try:
            if pd.isna(value):
                continue
        except ValueError:
            pass
        if value not in [None, ""]:
            return value
    return None


def _normalize_manual_row(row: dict[str, Any], filename: str, username: str) -> dict[str, Any]:
    origem = normalize_text(_value(row, "Origem"))
    destino = normalize_text(_value(row, "Destino"))
    quantidade_eixos = parse_number(_value(row, "Quantidade de eixos", "Quantidade eixos", "Eixos"))
    valor_por_eixo = parse_number(_value(row, "Valor por eixo"))
    valor_total = parse_number(_value(row, "Valor total atualizado", "Valor total"))
    km_rota = parse_number(_value(row, "Km rota", "Km"))
    mes_referencia = str(_value(row, "Mes referencia", "Mês referência") or "").strip()
    if not mes_referencia:
        mes_referencia = _month_reference(_value(row, "Data emissao", "Data emissão"))
    else:
        parsed_month = pd.to_datetime(mes_referencia, errors="coerce", dayfirst=True)
        if not pd.isna(parsed_month):
            mes_referencia = parsed_month.strftime("%Y-%m")

    if valor_total in [None, ""] and valor_por_eixo not in [None, ""] and quantidade_eixos not in [None, ""]:
        valor_total = float(valor_por_eixo) * float(quantidade_eixos)

    status = "VALOR ATUALIZADO"
    if valor_total in [None, ""] and valor_por_eixo in [None, ""]:
        status = "SEM VALOR INFORMADO"

    return {
        "origem": origem,
        "destino": destino,
        "origem_normalizada": normalize_location_key(origem),
        "destino_normalizada": normalize_location_key(destino),
        "quantidade_eixos": quantidade_eixos,
        "valor_por_eixo": valor_por_eixo,
        "valor_total_atualizado": valor_total,
        "km_rota": km_rota,
        "mes_referencia": mes_referencia,
        "observacao": normalize_text(_value(row, "Observacao", "Observação")),
        "arquivo_origem": filename,
        "data_importacao": brasilia_now_iso(),
        "usuario_importacao": username,
        "status": status,
    }


def _validate_manual_row(normalized: dict[str, Any]) -> None:
    if not normalized["origem_normalizada"]:
        raise ValueError("Origem obrigatoria.")
    if not normalized["destino_normalizada"]:
        raise ValueError("Destino obrigatorio.")
    if normalized["quantidade_eixos"] in [None, ""]:
        raise ValueError("Quantidade de eixos obrigatoria.")
    if normalized["valor_por_eixo"] in [None, ""] and normalized["valor_total_atualizado"] in [None, ""]:
        raise ValueError("Valor por eixo ou valor total atualizado obrigatorio.")


def _signature(normalized: dict[str, Any]) -> str:
    return hash_dict(
        {
            "origem": normalized["origem_normalizada"],
            "destino": normalized["destino_normalizada"],
            "quantidade_eixos": normalized["quantidade_eixos"],
            "mes_referencia": normalized["mes_referencia"],
        }
    )


def _upsert_manual_row(conn, normalized: dict[str, Any]) -> bool:
    signature = _signature(normalized)
    existing = conn.execute(
        "select * from base_vale_pedagio_rota_eixo where signature = ?",
        (signature,),
    ).fetchone()
    if existing:
        conn.execute(
            """
            update base_vale_pedagio_rota_eixo
            set origem = ?, destino = ?, valor_por_eixo = ?, valor_total_atualizado = ?,
                km_rota = ?, observacao = ?, arquivo_origem = ?, data_importacao = ?,
                usuario_importacao = ?, status = ?
            where id = ?
            """,
            (
                normalized["origem"],
                normalized["destino"],
                normalized["valor_por_eixo"],
                normalized["valor_total_atualizado"],
                normalized["km_rota"],
                normalized["observacao"],
                normalized["arquivo_origem"],
                normalized["data_importacao"],
                normalized["usuario_importacao"],
                normalized["status"],
                existing["id"],
            ),
        )
        conn.execute(
            """
            insert into historico_vale_pedagio_rota_eixo (
                vale_pedagio_id, origem, destino, quantidade_eixos, mes_referencia,
                valor_por_eixo_anterior, valor_total_anterior, valor_por_eixo_novo,
                valor_total_novo, observacao, arquivo_origem, data_alteracao,
                usuario_importacao, tipo_atualizacao, signature
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ATUALIZACAO', ?)
            """,
            (
                existing["id"],
                normalized["origem"],
                normalized["destino"],
                normalized["quantidade_eixos"],
                normalized["mes_referencia"],
                existing["valor_por_eixo"],
                existing["valor_total_atualizado"],
                normalized["valor_por_eixo"],
                normalized["valor_total_atualizado"],
                normalized["observacao"],
                normalized["arquivo_origem"],
                normalized["data_importacao"],
                normalized["usuario_importacao"],
                signature,
            ),
        )
        return True

    cursor = conn.execute(
        """
        insert into base_vale_pedagio_rota_eixo (
            origem, destino, origem_normalizada, destino_normalizada, quantidade_eixos,
            valor_por_eixo, valor_total_atualizado, km_rota, mes_referencia,
            observacao, arquivo_origem, data_importacao, usuario_importacao, status,
            signature
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalized["origem"],
            normalized["destino"],
            normalized["origem_normalizada"],
            normalized["destino_normalizada"],
            normalized["quantidade_eixos"],
            normalized["valor_por_eixo"],
            normalized["valor_total_atualizado"],
            normalized["km_rota"],
            normalized["mes_referencia"],
            normalized["observacao"],
            normalized["arquivo_origem"],
            normalized["data_importacao"],
            normalized["usuario_importacao"],
            normalized["status"],
            signature,
        ),
    )
    conn.execute(
        """
        insert into historico_vale_pedagio_rota_eixo (
            vale_pedagio_id, origem, destino, quantidade_eixos, mes_referencia,
            valor_por_eixo_novo, valor_total_novo, observacao, arquivo_origem,
            data_alteracao, usuario_importacao, tipo_atualizacao, signature
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CRIACAO', ?)
        """,
        (
            int(cursor.lastrowid),
            normalized["origem"],
            normalized["destino"],
            normalized["quantidade_eixos"],
            normalized["mes_referencia"],
            normalized["valor_por_eixo"],
            normalized["valor_total_atualizado"],
            normalized["observacao"],
            normalized["arquivo_origem"],
            normalized["data_importacao"],
            normalized["usuario_importacao"],
            signature,
        ),
    )
    return False


def import_manual_toll_update(
    file: BinaryIO,
    sheet_name: str,
    username: str = "",
    user_role: str = "",
) -> dict[str, Any]:
    filename = getattr(file, "name", "atualizacao_vale_pedagio.xlsx")
    try:
        df = read_excel_sheet(file, sheet_name)
    except Exception as exc:
        return {
            "status": "ERRO",
            "error_message": "Nao foi possivel ler a planilha de atualizacao de pedagio.",
            "technical_error": str(exc),
            "new_rows": 0,
            "updated_rows": 0,
            "duplicate_rows": 0,
            "error_rows": 1,
        }

    saved_path = _save_upload(file, filename)
    new_rows = updated_rows = error_rows = 0
    with get_connection() as conn:
        import_id = _insert_import_history(conn, filename, sheet_name, len(df), username, user_role)
        _insert_imported_file(conn, import_id, filename, saved_path, username)
        for record in df.to_dict(orient="records"):
            try:
                normalized = _normalize_manual_row(record, filename, username)
                _validate_manual_row(normalized)
                was_update = _upsert_manual_row(conn, normalized)
                if was_update:
                    updated_rows += 1
                else:
                    new_rows += 1
            except Exception:
                error_rows += 1
        message = (
            f"VALOR ATUALIZADO: {new_rows + updated_rows}; "
            f"ROTAS ATUALIZADAS: {updated_rows}; "
            f"ERRO NA IMPORTACAO: {error_rows}"
        )
        _update_import_history(conn, import_id, new_rows + updated_rows, updated_rows, error_rows, "SUCESSO", message)

    return {
        "status": "SUCESSO",
        "saved_path": saved_path,
        "new_rows": new_rows + updated_rows,
        "updated_rows": updated_rows,
        "duplicate_rows": updated_rows,
        "error_rows": error_rows,
        "error_message": "",
        "technical_error": "",
    }


def toll_reference_rows() -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql_query(
            "select * from base_vale_pedagio_rota_eixo order by mes_referencia desc, origem, destino",
            conn,
        )


def toll_pending_and_checked() -> tuple[pd.DataFrame, pd.DataFrame]:
    base = pending_toll_rows(deduplicate_routes=False)
    if base.empty:
        return base, base
    refs = toll_reference_rows()
    if refs.empty:
        pending = base.copy()
        pending["Status"] = "PENDENTE DE ATUALIZACAO"
        return pending, pd.DataFrame(columns=[*TOLL_COLUMNS, "Valor atualizado encontrado"])

    ref_keys = set(zip(refs["origem_normalizada"], refs["destino_normalizada"], refs["mes_referencia"]))
    keys = list(zip(
        base["Origem"].apply(normalize_location_key),
        base["Destino"].apply(normalize_location_key),
        base["Mes referencia"],
    ))
    matched = pd.Series([key in ref_keys for key in keys], index=base.index)
    pending = base[~matched].copy()
    checked = base[matched].copy()
    pending["Status"] = "PENDENTE DE ATUALIZACAO"
    checked["Status"] = "CONFERIDO"

    if not checked.empty:
        refs_lookup = refs.sort_values("id").drop_duplicates(
            ["origem_normalizada", "destino_normalizada", "mes_referencia"],
            keep="last",
        )
        refs_lookup = refs_lookup.set_index(["origem_normalizada", "destino_normalizada", "mes_referencia"])
        checked["Valor atualizado encontrado"] = [
            refs_lookup.loc[key, "valor_total_atualizado"] if key in refs_lookup.index else None
            for key in zip(
                checked["Origem"].apply(normalize_location_key),
                checked["Destino"].apply(normalize_location_key),
                checked["Mes referencia"],
            )
        ]
    return pending.reset_index(drop=True), checked.reset_index(drop=True)
