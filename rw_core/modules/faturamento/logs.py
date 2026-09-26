from __future__ import annotations

from rw_core.database.connection import get_connection
from rw_core.database.migrations import import_log_payload
from rw_core.utils.timezone import brasilia_now_iso


def registrar_log_importacao(**kwargs) -> None:
    kwargs = dict(kwargs)
    kwargs["data_hora"] = kwargs.get("data_hora") or brasilia_now_iso()
    payload = import_log_payload(**kwargs)
    with get_connection() as conn:
        conn.execute(
            """
            insert into mod_faturamento_logs_importacao (
                data_hora, usuario, tipo_importacao, arquivo_origem, hash_arquivo,
                quantidade_linhas, quantidade_registros_inseridos,
                quantidade_registros_atualizados, quantidade_registros_ignorados,
                status, mensagem, detalhes_json, lote_importacao
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(payload[column] for column in [
                "data_hora", "usuario", "tipo_importacao", "arquivo_origem", "hash_arquivo",
                "quantidade_linhas", "quantidade_registros_inseridos",
                "quantidade_registros_atualizados", "quantidade_registros_ignorados",
                "status", "mensagem", "detalhes_json", "lote_importacao",
            ]),
        )
