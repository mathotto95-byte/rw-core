from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from src.config.settings import BACKUPS_DIR, DATA_DIR, DB_PATH, EXPORTS_DIR


DEFAULT_WAL_LIMIT_BYTES = 64 * 1024 * 1024


def _bytes_to_mb(value: int | float) -> float:
    return round(float(value or 0) / (1024 * 1024), 2)


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size) if path.exists() else 0
    except Exception:
        return 0


def sqlite_sidecar_paths(db_path: Path) -> dict[str, Path]:
    return {
        "database": db_path,
        "wal": Path(str(db_path) + "-wal"),
        "shm": Path(str(db_path) + "-shm"),
    }


def sqlite_storage_snapshot(db_path: Path) -> dict[str, Any]:
    paths = sqlite_sidecar_paths(db_path)
    sizes = {name: _file_size(path) for name, path in paths.items()}
    return {
        "path": str(db_path),
        "database_mb": _bytes_to_mb(sizes["database"]),
        "wal_mb": _bytes_to_mb(sizes["wal"]),
        "shm_mb": _bytes_to_mb(sizes["shm"]),
        "total_mb": _bytes_to_mb(sum(sizes.values())),
        "exists": db_path.exists(),
    }


def checkpoint_sqlite_database(db_path: Path, *, vacuum: bool = False) -> dict[str, Any]:
    before = sqlite_storage_snapshot(db_path)
    if not db_path.exists():
        return {**before, "status": "IGNORADO", "message": "Banco SQLite nao encontrado."}
    started = time.perf_counter()
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("pragma busy_timeout = 30000")
            conn.execute("pragma wal_checkpoint(TRUNCATE)")
            conn.execute("pragma optimize")
            if vacuum:
                conn.execute("vacuum")
        finally:
            conn.close()
        after = sqlite_storage_snapshot(db_path)
        return {
            **after,
            "status": "SUCESSO",
            "message": "Checkpoint WAL concluido.",
            "before": before,
            "freed_mb": round(float(before["total_mb"]) - float(after["total_mb"]), 2),
            "duration_seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        after = sqlite_storage_snapshot(db_path)
        return {
            **after,
            "status": "ERRO",
            "message": str(exc)[:1000],
            "before": before,
            "freed_mb": round(float(before["total_mb"]) - float(after["total_mb"]), 2),
            "duration_seconds": round(time.perf_counter() - started, 3),
        }


def checkpoint_if_wal_large(db_path: Path, limit_bytes: int = DEFAULT_WAL_LIMIT_BYTES) -> dict[str, Any]:
    wal_path = sqlite_sidecar_paths(db_path)["wal"]
    if _file_size(wal_path) < int(limit_bytes or DEFAULT_WAL_LIMIT_BYTES):
        return {**sqlite_storage_snapshot(db_path), "status": "IGNORADO_WAL_PEQUENO", "message": "WAL abaixo do limite."}
    return checkpoint_sqlite_database(db_path, vacuum=False)


def cleanup_generated_files(max_age_hours: int = 24) -> dict[str, Any]:
    cutoff = time.time() - max(int(max_age_hours or 24), 1) * 3600
    candidates: list[Path] = []
    for directory in [EXPORTS_DIR, BACKUPS_DIR / "modulos"]:
        if directory.exists():
            candidates.extend(path for path in directory.rglob("*") if path.is_file())
    safe_patterns = (
        ".invalid.sqlite3",
        ".invalid.manifest.json",
        ".tmp",
        ".bak",
        ".zip",
    )
    deleted: list[dict[str, Any]] = []
    skipped = 0
    for path in candidates:
        try:
            name = path.name.lower()
            if path.stat().st_mtime >= cutoff:
                skipped += 1
                continue
            if not (path.is_relative_to(EXPORTS_DIR) or any(name.endswith(pattern) for pattern in safe_patterns)):
                skipped += 1
                continue
            size = _file_size(path)
            path.unlink()
            deleted.append({"arquivo": str(path), "mb": _bytes_to_mb(size)})
        except Exception:
            skipped += 1
    return {
        "status": "SUCESSO",
        "deleted_files": len(deleted),
        "deleted_mb": round(sum(float(item["mb"]) for item in deleted), 2),
        "deleted": deleted[:100],
        "skipped": skipped,
    }


def storage_maintenance_summary() -> dict[str, Any]:
    databases = [DB_PATH]
    snapshots = [sqlite_storage_snapshot(path) for path in databases]
    data_total = 0
    for path in DATA_DIR.rglob("*") if DATA_DIR.exists() else []:
        if path.is_file():
            data_total += _file_size(path)
    return {
        "databases": snapshots,
        "data_total_mb": _bytes_to_mb(data_total),
        "wal_total_mb": round(sum(float(item["wal_mb"]) for item in snapshots), 2),
    }


def run_local_storage_maintenance(*, vacuum: bool = False, cleanup_age_hours: int = 24) -> dict[str, Any]:
    started = time.perf_counter()
    database_results = [
        checkpoint_sqlite_database(DB_PATH, vacuum=vacuum),
    ]
    cleanup = cleanup_generated_files(cleanup_age_hours)
    return {
        "status": "SUCESSO" if all(result.get("status") != "ERRO" for result in database_results) else "PARCIAL",
        "databases": database_results,
        "cleanup": cleanup,
        "duration_seconds": round(time.perf_counter() - started, 3),
    }
