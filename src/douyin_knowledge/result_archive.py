from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

RESULTS_CONFIG = Path("config/results.yml")
RESULTS_LAYOUT = "category-title-v1"
RESULTS_HANDLE_ROOT = "results"
LEGACY_LIBRARY_ROOT = Path("library")
RESULTS_MIGRATION_STATE = Path("data/migrations/results-v1.json")
PRIVATE_INSTANCE_DIRS = frozenset(
    {"config", "data", "logs", "orchestration", "output", "quarantine", "schemas"}
)


class ResultsConfigError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def default_results_config() -> str:
    return (
        "version: 1\n"
        "root: null\n"
        f"layout: {RESULTS_LAYOUT}\n"
        "source_video: copy\n"
    )


def _load_config(instance_root: Path) -> dict[str, Any] | None:
    path = instance_root.resolve() / RESULTS_CONFIG
    if not path.is_file():
        return None
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ResultsConfigError(
            "results_config_invalid", "the results configuration could not be read"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("layout") != RESULTS_LAYOUT
        or payload.get("source_video") != "copy"
    ):
        raise ResultsConfigError(
            "results_config_invalid", "the results configuration is not supported"
        )
    configured = payload.get("root")
    if configured is not None and not isinstance(configured, str):
        raise ResultsConfigError(
            "results_config_invalid", "the configured results root is invalid"
        )
    return payload


def configured_results_root(instance_root: Path) -> Path | None:
    instance_root = instance_root.resolve()
    payload = _load_config(instance_root)
    if payload is None:
        return None
    configured = payload.get("root")
    if not isinstance(configured, str) or not configured.strip():
        return None
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = instance_root / candidate
    return candidate.resolve()


def results_root(instance_root: Path) -> Path:
    """Return the configured archive, with a legacy fallback for library-level APIs."""
    instance_root = instance_root.resolve()
    return configured_results_root(instance_root) or (instance_root / LEGACY_LIBRARY_ROOT)


def results_handle(instance_root: Path, target: Path) -> str:
    root = results_root(instance_root)
    try:
        relative = target.resolve().relative_to(root)
    except ValueError as exc:
        raise ResultsConfigError(
            "results_target_invalid", "the result target is outside the configured root"
        ) from exc
    return (PurePosixPath(RESULTS_HANDLE_ROOT) / relative.as_posix()).as_posix()


def resolve_results_handle(instance_root: Path, handle: str) -> Path:
    relative = PurePosixPath(handle.replace("\\", "/"))
    if (
        not relative.parts
        or relative.parts[0] != RESULTS_HANDLE_ROOT
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ResultsConfigError("results_target_invalid", "the result handle is invalid")
    root = configured_results_root(instance_root)
    if root is None:
        raise ResultsConfigError(
            "results_root_required", "a results root must be configured before publication"
        )
    candidate = (root / Path(*relative.parts[1:])).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ResultsConfigError(
            "results_target_invalid", "the result handle escapes the configured root"
        ) from exc
    return candidate


def logical_library_handle(instance_root: Path, target: Path) -> str:
    root = results_root(instance_root)
    try:
        relative = target.resolve().relative_to(root)
    except ValueError as exc:
        raise ResultsConfigError(
            "results_target_invalid", "the knowledge entry is outside the results root"
        ) from exc
    return (PurePosixPath("library") / relative.as_posix()).as_posix()


def resolve_logical_library_handle(instance_root: Path, handle: str) -> Path:
    relative = PurePosixPath(handle.replace("\\", "/"))
    if (
        len(relative.parts) < 2
        or relative.parts[0] != "library"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ResultsConfigError("results_target_invalid", "the library handle is invalid")
    root = results_root(instance_root)
    candidate = (root / Path(*relative.parts[1:])).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ResultsConfigError(
            "results_target_invalid", "the library handle escapes the results root"
        ) from exc
    return candidate


def _validate_target(instance_root: Path, target: Path) -> None:
    instance_root = instance_root.resolve()
    if target == instance_root:
        raise ResultsConfigError(
            "results_root_unsafe", "the private instance root cannot be used as the results root"
        )
    try:
        relative = target.relative_to(instance_root)
    except ValueError:
        return
    if relative.parts and relative.parts[0].casefold() in PRIVATE_INSTANCE_DIRS:
        raise ResultsConfigError(
            "results_root_unsafe", "the results root overlaps private runtime data"
        )


def _root_is_locked(instance_root: Path) -> bool:
    migration_state = instance_root / RESULTS_MIGRATION_STATE
    if migration_state.is_file():
        try:
            payload = json.loads(migration_state.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ResultsConfigError(
                "results_lock_check_failed",
                "results migration history could not be checked before changing the root",
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ResultsConfigError(
                "results_lock_check_failed",
                "results migration history is invalid",
            )
        entries = payload.get("entries")
        if isinstance(entries, list) and entries:
            return True
    database = instance_root / "data" / "knowledge.db"
    if not database.is_file():
        return False
    try:
        with sqlite3.connect(database) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'publication_targets'"
            ).fetchone()
            if table is None:
                return False
            row = connection.execute(
                "SELECT 1 FROM publication_targets "
                "WHERE relative_handle LIKE 'results/%' LIMIT 1"
            ).fetchone()
    except sqlite3.Error as exc:
        raise ResultsConfigError(
            "results_lock_check_failed",
            "publication history could not be checked before changing the results root",
        ) from exc
    return row is not None


def _atomic_config(path: Path, payload: dict[str, Any]) -> None:
    rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure_results_root(instance_root: Path, target: Path) -> dict[str, Any]:
    instance_root = instance_root.resolve()
    config_path = instance_root / RESULTS_CONFIG
    if not (instance_root / "config" / "config.yml").is_file():
        raise ResultsConfigError(
            "instance_not_initialized", "initialize the private instance before configuration"
        )
    expanded = target.expanduser()
    if not expanded.is_absolute():
        raise ResultsConfigError(
            "results_root_absolute_required", "the results root must be an absolute path"
        )
    resolved = expanded.resolve()
    _validate_target(instance_root, resolved)
    previous = configured_results_root(instance_root)
    if previous is not None and previous != resolved and _root_is_locked(instance_root):
        raise ResultsConfigError(
            "results_root_locked",
            "the results root cannot change after accepted publication targets exist",
        )
    created = not resolved.exists()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise OSError("not a directory")
        probe = resolved / f".douyin-knowledge-write-{uuid.uuid4().hex}.tmp"
        probe.write_text("probe\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ResultsConfigError(
            "results_root_unwritable", "the results root could not be created or written"
        ) from exc
    payload = {
        "version": 1,
        "root": resolved.as_posix(),
        "layout": RESULTS_LAYOUT,
        "source_video": "copy",
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_config(config_path, payload)
    return {
        "configured": True,
        "changed": previous != resolved,
        "created": created,
        "layout": RESULTS_LAYOUT,
        "source_video": "copy",
    }
