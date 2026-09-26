from __future__ import annotations

from rw_core.analyses import coupa, tripla_ipp
from rw_core.database.connection import get_connection


def _count(conn, table: str) -> int:
    try:
        return int(conn.execute(f"select count(*) from {table}").fetchone()[0] or 0)
    except Exception:
        return 0


def _has_any(conn, tables: list[str]) -> bool:
    return any(_count(conn, table) > 0 for table in tables)


def run_validacao_tripla_ipp(username: str | None = None) -> dict:
    username = username or ""
    with get_connection() as conn:
        if not _has_any(conn, ["base_coupa_original", "base_coupa_fluxos_normalizados"]):
            return {
                "status": "ERRO",
                "painel": "Validacao Tripla IPP",
                "registros_processados": 0,
                "registros_gerados": 0,
                "mensagem": "Base Coupa, KMM/LCTE/FAT ou Portal IPP nao importada.",
            }
        if not _has_any(conn, ["base_fat_kmm_original", "base_kmm_original", "base_kmm_fat_notas_normalizadas"]):
            return {
                "status": "ERRO",
                "painel": "Validacao Tripla IPP",
                "registros_processados": 0,
                "registros_gerados": 0,
                "mensagem": "Base Coupa, KMM/LCTE/FAT ou Portal IPP nao importada.",
            }
        if not _has_any(conn, ["base_portal_ipp_original", "base_portal_ipp_normalizada"]):
            return {
                "status": "ERRO",
                "painel": "Validacao Tripla IPP",
                "registros_processados": 0,
                "registros_gerados": 0,
                "mensagem": "Base Coupa, KMM/LCTE/FAT ou Portal IPP nao importada.",
            }
        coupa.run_kmm_x_coupa(conn, username)
        result = tripla_ipp.run(conn, username)
    return {
        "status": "SUCESSO",
        "painel": "Validacao Tripla IPP",
        "registros_processados": int(result.get("registros_processados", 0) or 0),
        "registros_gerados": int(result.get("registros_gerados", 0) or 0),
        "mensagem": "Validacao Tripla IPP recalculada com sucesso.",
    }
