from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from app.analyze_video import JOB_ID_PATTERN
from app.publish_library import INDEX_DIR_NAME, generate_indexes
from douyin_knowledge.result_archive import (
    LEGACY_LIBRARY_ROOT,
    RESULTS_MIGRATION_STATE,
    ResultsConfigError,
    configured_results_root,
)

REQUIRED_ENTRY_PATHS = (
    Path("内容整理.md"),
    Path("原视频.mp4"),
    Path("资料信息.yml"),
    Path("附件/完整时间轴.md"),
    Path("精选关键帧"),
)


class ResultsMigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _tree_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    if path.is_symlink() or any(item.is_symlink() for item in path.rglob("*")):
        raise ResultsMigrationError(
            "results_migration_symlink", "legacy results must not contain symbolic links"
        )
    files = sorted(item for item in path.rglob("*") if item.is_file())
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        hasher.update(len(relative).to_bytes(8, "big"))
        hasher.update(relative)
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


def _entry_ref(path: Path) -> str:
    try:
        payload = yaml.safe_load((path / "资料信息.yml").read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ResultsMigrationError(
            "results_migration_manifest_invalid", "a legacy result manifest is invalid"
        ) from exc
    value = payload.get("entry_ref") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not JOB_ID_PATTERN.fullmatch(value):
        raise ResultsMigrationError(
            "results_migration_manifest_invalid", "a legacy result has no stable entry reference"
        )
    return value


def _discover_entries(legacy_root: Path) -> list[Path]:
    if not legacy_root.is_dir():
        return []
    if legacy_root.is_symlink():
        raise ResultsMigrationError(
            "results_migration_symlink", "the legacy results root must not be a symbolic link"
        )
    entries: list[Path] = []
    for category in sorted(legacy_root.iterdir()):
        if category.name == INDEX_DIR_NAME:
            continue
        if category.is_symlink():
            raise ResultsMigrationError(
                "results_migration_symlink", "legacy results must not contain symbolic links"
            )
        if not category.is_dir():
            continue
        for entry in sorted(category.iterdir()):
            if entry.is_symlink() or (
                entry.is_dir() and any(item.is_symlink() for item in entry.rglob("*"))
            ):
                raise ResultsMigrationError(
                    "results_migration_symlink", "legacy results must not contain symbolic links"
                )
            if not entry.is_dir():
                continue
            required = [entry / relative for relative in REQUIRED_ENTRY_PATHS]
            if any(not item.exists() for item in required):
                raise ResultsMigrationError(
                    "results_migration_entry_incomplete", "a legacy result is incomplete"
                )
            if any(not item.is_file() for item in required[:4]) or not required[4].is_dir():
                raise ResultsMigrationError(
                    "results_migration_entry_incomplete", "a legacy result has invalid artifacts"
                )
            frame_root = entry / "精选关键帧"
            if not any(item.is_file() for item in frame_root.iterdir()):
                raise ResultsMigrationError(
                    "results_migration_entry_incomplete", "a legacy result has no cited frames"
                )
            entries.append(entry)
    return entries


def _checkpoint_records(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultsMigrationError(
            "results_migration_state_invalid", "the migration checkpoint is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise ResultsMigrationError(
            "results_migration_state_invalid", "the migration checkpoint is unsupported"
        )
    entries = payload.get("entries")
    if payload.get("schema_version") != 1 or not isinstance(entries, list):
        raise ResultsMigrationError(
            "results_migration_state_invalid", "the migration checkpoint is unsupported"
        )
    records: dict[str, dict[str, str]] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            raise ResultsMigrationError(
                "results_migration_state_invalid", "the migration checkpoint entry is invalid"
            )
        source = raw.get("source")
        target = raw.get("target")
        digest = raw.get("sha256")
        paths = (source, target)
        if (
            not all(isinstance(value, str) and value for value in (*paths, digest))
            or len(str(digest)) != 64
            or any(character not in "0123456789abcdef" for character in str(digest))
            or any(
                PurePosixPath(str(value)).is_absolute()
                or "\\" in str(value)
                or ":" in PurePosixPath(str(value)).parts[0]
                or any(part in {"", ".", ".."} for part in PurePosixPath(str(value)).parts)
                for value in paths
            )
            or str(source) in records
        ):
            raise ResultsMigrationError(
                "results_migration_state_invalid", "the migration checkpoint entry is unsafe"
            )
        records[str(source)] = {
            "source": str(source),
            "target": str(target),
            "sha256": str(digest),
        }
    targets = [record["target"] for record in records.values()]
    if len(targets) != len(set(targets)):
        raise ResultsMigrationError(
            "results_migration_state_invalid", "migration checkpoint targets are duplicated"
        )
    return records


def _target_for(destination: Path, source: Path, entry_ref: str) -> Path:
    category = source.parent.name
    title = source.name
    category_root = destination / category
    category_root.mkdir(parents=True, exist_ok=True)
    for index in range(1, 1000):
        suffix = "" if index == 1 else f" ({index})"
        stem = title[: 80 - len(suffix)].rstrip(" .-")
        candidate = category_root / f"{stem}{suffix}"
        try:
            candidate.resolve().relative_to(destination)
        except ValueError as exc:
            raise ResultsMigrationError(
                "results_migration_target_invalid", "a migration target escapes the results root"
            ) from exc
        if not candidate.exists():
            return candidate
        if candidate.is_dir():
            try:
                if _entry_ref(candidate) == entry_ref:
                    return candidate
            except ResultsMigrationError:
                pass
    raise ResultsMigrationError(
        "results_migration_collision", "too many results share the same category and title"
    )


def _copy_verified(source: Path, target: Path, expected_digest: str) -> str:
    if target.is_dir():
        if _tree_digest(target) != expected_digest:
            raise ResultsMigrationError(
                "results_migration_conflict",
                "an existing migrated result differs from the preserved legacy result",
            )
        return "reused"
    if target.exists():
        raise ResultsMigrationError(
            "results_migration_conflict", "a migration target is occupied by a file"
        )
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copytree(source, temporary, copy_function=shutil.copy2)
        if _tree_digest(temporary) != expected_digest:
            raise ResultsMigrationError(
                "results_migration_verification_failed", "a copied result failed verification"
            )
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    if _tree_digest(target) != expected_digest:
        raise ResultsMigrationError(
            "results_migration_verification_failed", "a migrated result failed final verification"
        )
    return "copied"


def _register_target(
    database: Path,
    instance_root: Path,
    entry_ref: str,
    source: Path,
    target: Path,
) -> bool:
    if not database.is_file():
        return False
    try:
        with sqlite3.connect(database, timeout=30) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'collection_items'"
            ).fetchone()
            if table is None:
                return False
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(collection_items)")
            }
            if not {"job_id", "library_path"}.issubset(columns):
                return False
            row = connection.execute(
                "SELECT library_path FROM collection_items WHERE job_id = ?", (entry_ref,)
            ).fetchone()
            if row is None or not row[0]:
                return False
            registered = Path(str(row[0]))
            if not registered.is_absolute():
                registered = instance_root / registered
            registered = registered.resolve()
            if registered == target.resolve():
                return True
            if registered != source.resolve():
                raise ResultsMigrationError(
                    "results_migration_registry_mismatch",
                    "a legacy result reference does not match its registered source",
                )
            cursor = connection.execute(
                "UPDATE collection_items SET library_path = ? WHERE job_id = ?",
                (str(target), entry_ref),
            )
            return cursor.rowcount == 1
    except sqlite3.Error as exc:
        raise ResultsMigrationError(
            "results_migration_registry_failed", "the migrated result could not be registered"
        ) from exc


def migrate_legacy_results(instance_root: Path) -> dict[str, Any]:
    instance_root = instance_root.resolve()
    try:
        destination = configured_results_root(instance_root)
    except ResultsConfigError as exc:
        raise ResultsMigrationError(exc.code, str(exc)) from exc
    if destination is None or not destination.is_dir():
        raise ResultsMigrationError(
            "results_root_required", "a configured results root is required before migration"
        )
    legacy_root = instance_root / LEGACY_LIBRARY_ROOT
    if destination == legacy_root.resolve():
        raise ResultsMigrationError(
            "results_migration_same_root", "the configured results root is the legacy library"
        )

    entries = _discover_entries(legacy_root)
    entry_refs: dict[str, Path] = {}
    source_refs: dict[str, str] = {}
    for source in entries:
        entry_ref = _entry_ref(source)
        if entry_ref in entry_refs:
            raise ResultsMigrationError(
                "results_migration_duplicate_reference",
                "multiple legacy results use the same stable entry reference",
            )
        entry_refs[entry_ref] = source
        source_refs[source.relative_to(legacy_root).as_posix()] = entry_ref
    checkpoint = _checkpoint_records(instance_root / RESULTS_MIGRATION_STATE)
    if any(source not in source_refs for source in checkpoint):
        raise ResultsMigrationError(
            "results_migration_source_changed",
            "a previously migrated legacy result is no longer available",
        )
    copied = 0
    reused = 0
    registered = 0
    records: list[dict[str, str]] = []
    for source in entries:
        source_handle = source.relative_to(legacy_root).as_posix()
        entry_ref = source_refs[source_handle]
        digest = _tree_digest(source)
        previous = checkpoint.get(source_handle)
        if previous is not None:
            if previous["sha256"] != digest:
                raise ResultsMigrationError(
                    "results_migration_source_changed",
                    "a preserved legacy result changed after migration began",
                )
            target = (destination / Path(*PurePosixPath(previous["target"]).parts)).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise ResultsMigrationError(
                    "results_migration_state_invalid",
                    "a migration checkpoint target escapes the results root",
                ) from exc
        else:
            target = _target_for(destination, source, entry_ref)
        outcome = _copy_verified(source, target, digest)
        copied += int(outcome == "copied")
        reused += int(outcome == "reused")
        records.append(
            {
                "source": source_handle,
                "target": target.relative_to(destination).as_posix(),
                "sha256": digest,
            }
        )
        _atomic_json(
            instance_root / RESULTS_MIGRATION_STATE,
            {"schema_version": 1, "complete": False, "entries": records},
        )
        registered += int(
            _register_target(
                instance_root / "data" / "knowledge.db",
                instance_root,
                entry_ref,
                source,
                target,
            )
        )

    generate_indexes(destination)
    _atomic_json(
        instance_root / RESULTS_MIGRATION_STATE,
        {"schema_version": 1, "complete": True, "entries": records},
    )
    return {
        "status": "migrated" if entries else "no_work",
        "discovered": len(entries),
        "copied": copied,
        "reused": reused,
        "verified": len(entries),
        "registered": registered,
        "source_preserved": True,
        "index_rebuilt": True,
        "state_handle": RESULTS_MIGRATION_STATE.as_posix(),
    }
