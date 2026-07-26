from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from app.analyze_video import JOB_ID_PATTERN
from app.publish_library import INDEX_DIR_NAME, generate_indexes
from douyin_knowledge.result_archive import (
    LEGACY_LIBRARY_ROOT,
    RESULTS_LAYOUT,
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
ARTIFACT_NAMES = {
    Path("内容整理.md"): "knowledge_note",
    Path("原视频.mp4"): "source_video",
    Path("资料信息.yml"): "entry_manifest",
    Path("附件/完整时间轴.md"): "timeline",
    Path("精选关键帧"): "keyframes_directory",
}
RESULTS_CLEANUP_STATE = Path("data/migrations/results-cleanup-v1.json")
_CLEANUP_STAGING_PREFIX = ".legacy-results-cleanup-"


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
    return _tree_digest_with_files(path, {})


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _tree_digest_with_files(path: Path, synthetic: dict[str, bytes]) -> str:
    hasher = hashlib.sha256()
    if path.is_symlink() or any(item.is_symlink() for item in path.rglob("*")):
        raise ResultsMigrationError(
            "results_migration_symlink", "legacy results must not contain symbolic links"
        )
    files = {
        item.relative_to(path).as_posix(): item
        for item in path.rglob("*")
        if item.is_file()
    }
    for relative in sorted(set(files) | set(synthetic)):
        item = files.get(relative)
        body = synthetic.get(relative)
        if item is not None and body is not None:
            raise ResultsMigrationError(
                "results_migration_manifest_invalid", "a generated artifact already exists"
            )
        encoded_relative = relative.encode("utf-8")
        hasher.update(len(encoded_relative).to_bytes(8, "big"))
        hasher.update(encoded_relative)
        if body is not None:
            hasher.update(body)
            continue
        assert item is not None
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


def _registered_entry(database: Path, instance_root: Path, source: Path) -> tuple[str, str]:
    if not database.is_file():
        raise ResultsMigrationError(
            "results_migration_manifest_invalid",
            "a legacy result without a manifest is not registered",
        )
    try:
        with sqlite3.connect(database) as connection:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(collection_items)")
            }
            if not {"job_id", "library_path"}.issubset(columns):
                raise ResultsMigrationError(
                    "results_migration_manifest_invalid",
                    "the legacy result registry cannot resolve a missing manifest",
                )
            path_matches: list[str] = []
            for job_id, library_path in connection.execute(
                "SELECT job_id, library_path FROM collection_items WHERE library_path IS NOT NULL"
            ):
                if not library_path:
                    continue
                registered = Path(str(library_path))
                if not registered.is_absolute():
                    registered = instance_root / registered
                if registered.resolve() == source.resolve():
                    path_matches.append(str(job_id))
            method = "registered_path"
            matches = path_matches
            if not matches and "media_sha256" in columns:
                media_digest = _file_digest(source / "原视频.mp4")
                method = "media_fingerprint"
                matches = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT job_id FROM collection_items "
                        "WHERE lower(media_sha256) = ?",
                        (media_digest,),
                    )
                ]
    except sqlite3.Error as exc:
        raise ResultsMigrationError(
            "results_migration_registry_failed", "the legacy result registry is unreadable"
        ) from exc
    if len(matches) != 1 or not JOB_ID_PATTERN.fullmatch(matches[0]):
        raise ResultsMigrationError(
            "results_migration_manifest_invalid",
            "a legacy result without a manifest has no unique registered entry",
        )
    return matches[0], method


def _registered_entry_ref(database: Path, instance_root: Path, source: Path) -> str:
    return _registered_entry(database, instance_root, source)[0]


def _select_authoritative_sources(
    source_refs: dict[str, tuple[str, str | None, str]],
) -> tuple[dict[str, tuple[str, str | None, str]], int]:
    grouped: dict[str, list[str]] = {}
    for source_handle, (entry_ref, _manifest, _method) in source_refs.items():
        grouped.setdefault(entry_ref, []).append(source_handle)
    selected: dict[str, tuple[str, str | None, str]] = {}
    skipped = 0
    for handles in grouped.values():
        if len(handles) == 1:
            handle = handles[0]
            selected[handle] = source_refs[handle]
            continue
        authoritative = [
            handle
            for handle in handles
            if source_refs[handle][2] != "media_fingerprint"
        ]
        if len(authoritative) != 1:
            raise ResultsMigrationError(
                "results_migration_duplicate_reference",
                "multiple legacy results use the same stable entry reference",
            )
        selected[authoritative[0]] = source_refs[authoritative[0]]
        skipped += len(handles) - 1
    return selected, skipped


def _generated_manifest(entry_ref: str, source: Path) -> str:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "entry_ref": entry_ref,
            "title": source.name,
            "category": source.parent.name,
            "layout": RESULTS_LAYOUT,
            "source_video": "原视频.mp4",
            "knowledge_note": "内容整理.md",
            "timeline": "附件/完整时间轴.md",
            "keyframes": "精选关键帧/",
            "migrated_from_legacy": True,
        },
        allow_unicode=True,
        sort_keys=False,
    )


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
            required = [
                entry / relative
                for relative in REQUIRED_ENTRY_PATHS
                if relative != Path("资料信息.yml")
            ]
            if any(not item.exists() for item in required):
                raise ResultsMigrationError(
                    "results_migration_entry_incomplete", "a legacy result is incomplete"
                )
            if any(not item.is_file() for item in required[:3]) or not required[3].is_dir():
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


def inspect_legacy_results(instance_root: Path) -> dict[str, Any]:
    instance_root = instance_root.resolve()
    legacy_root = instance_root / LEGACY_LIBRARY_ROOT
    checkpoint = _checkpoint_records(instance_root / RESULTS_MIGRATION_STATE)
    if not legacy_root.exists():
        return {
            "discovered": 0,
            "complete": 0,
            "incomplete": 0,
            "repairable": 0,
            "blocked": 0,
            "duplicates_skipped": 0,
            "migration_ready": True,
            "issues": {},
        }
    issues: Counter[str] = Counter()
    discovered = 0
    complete = 0
    repairable = 0
    resolved_refs: dict[str, tuple[str, str | None, str]] = {}
    if legacy_root.is_symlink() or not legacy_root.is_dir():
        issues["unsafe_legacy_root"] += 1
    else:
        for category in sorted(legacy_root.iterdir()):
            if category.name == INDEX_DIR_NAME:
                continue
            if category.is_symlink():
                issues["symbolic_link"] += 1
                continue
            if not category.is_dir():
                continue
            for entry in sorted(category.iterdir()):
                if entry.is_symlink():
                    discovered += 1
                    issues["symbolic_link"] += 1
                    continue
                if not entry.is_dir():
                    continue
                discovered += 1
                entry_issues = 0
                resolved_entry: tuple[str, str | None, str] | None = None
                if any(item.is_symlink() for item in entry.rglob("*")):
                    issues["symbolic_link"] += 1
                    entry_issues += 1
                for relative, name in ARTIFACT_NAMES.items():
                    artifact = entry / relative
                    expected_directory = relative == Path("精选关键帧")
                    valid = artifact.is_dir() if expected_directory else artifact.is_file()
                    if not valid:
                        issues[f"missing_{name}"] += 1
                        entry_issues += 1
                frames = entry / "精选关键帧"
                if frames.is_dir() and not any(item.is_file() for item in frames.iterdir()):
                    issues["empty_keyframes"] += 1
                    entry_issues += 1
                manifest = entry / "资料信息.yml"
                if manifest.is_file():
                    try:
                        entry_ref = _entry_ref(entry)
                    except ResultsMigrationError:
                        issues["invalid_entry_manifest"] += 1
                        entry_issues += 1
                    else:
                        resolved_entry = (
                            entry_ref,
                            None,
                            "manifest",
                        )
                else:
                    try:
                        source_handle = entry.relative_to(legacy_root).as_posix()
                        previous = checkpoint.get(source_handle)
                        if previous is not None and "entry_ref" in previous:
                            entry_ref = previous["entry_ref"]
                            method = "checkpoint"
                        else:
                            entry_ref, method = _registered_entry(
                                instance_root / "data" / "knowledge.db",
                                instance_root,
                                entry,
                            )
                    except ResultsMigrationError:
                        pass
                    else:
                        issues["repairable_entry_manifest"] += 1
                        repairable += 1
                        issues["missing_entry_manifest"] -= 1
                        if issues["missing_entry_manifest"] == 0:
                            del issues["missing_entry_manifest"]
                        entry_issues -= 1
                        resolved_entry = (
                            entry_ref,
                            None,
                            method,
                        )
                if entry_issues == 0:
                    complete += 1
                    assert resolved_entry is not None
                    resolved_refs[entry.relative_to(legacy_root).as_posix()] = resolved_entry
    duplicates_skipped = 0
    try:
        _selected, duplicates_skipped = _select_authoritative_sources(resolved_refs)
    except ResultsMigrationError:
        duplicate_counts = Counter(
            entry_ref for entry_ref, _manifest, _method in resolved_refs.values()
        )
        conflicting = sum(count for count in duplicate_counts.values() if count > 1)
        issues["duplicate_reference_conflict"] += conflicting
        complete -= conflicting
    blocked = discovered - complete
    blocking_issues = {
        name: count
        for name, count in issues.items()
        if name != "repairable_entry_manifest"
    }
    return {
        "discovered": discovered,
        "complete": complete,
        "incomplete": blocked,
        "repairable": repairable,
        "blocked": blocked,
        "duplicates_skipped": duplicates_skipped,
        "migration_ready": blocked == 0 and not blocking_issues,
        "issues": dict(sorted(issues.items())),
    }


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
        source_digest = raw.get("source_sha256", digest)
        entry_ref = raw.get("entry_ref")
        paths = (source, target)
        if (
            not all(
                isinstance(value, str) and value
                for value in (*paths, digest, source_digest)
            )
            or len(str(digest)) != 64
            or any(character not in "0123456789abcdef" for character in str(digest))
            or len(str(source_digest)) != 64
            or any(
                character not in "0123456789abcdef"
                for character in str(source_digest)
            )
            or (
                entry_ref is not None
                and (
                    not isinstance(entry_ref, str)
                    or not JOB_ID_PATTERN.fullmatch(entry_ref)
                )
            )
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
            "source_sha256": str(source_digest),
        }
        if entry_ref is not None:
            records[str(source)]["entry_ref"] = entry_ref
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


def _copy_verified(
    source: Path,
    target: Path,
    expected_digest: str,
    *,
    generated_manifest: str | None,
) -> str:
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
        if generated_manifest is not None:
            (temporary / "资料信息.yml").write_text(
                generated_manifest, encoding="utf-8", newline="\n"
            )
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
            selected = "library_path"
            if "media_sha256" in columns:
                selected += ", media_sha256"
            row = connection.execute(
                f"SELECT {selected} FROM collection_items WHERE job_id = ?", (entry_ref,)
            ).fetchone()
            if row is None:
                return False
            if not row[0]:
                if "media_sha256" not in columns or not row[1]:
                    return False
                media_digest = _file_digest(source / "原视频.mp4")
                matching_jobs = connection.execute(
                    "SELECT job_id FROM collection_items WHERE lower(media_sha256) = ?",
                    (media_digest,),
                ).fetchall()
                if len(matching_jobs) != 1 or str(matching_jobs[0][0]) != entry_ref:
                    raise ResultsMigrationError(
                        "results_migration_registry_mismatch",
                        "a legacy result has no unique registered media fingerprint",
                    )
                cursor = connection.execute(
                    "UPDATE collection_items SET library_path = ? "
                    "WHERE job_id = ? AND library_path IS NULL",
                    (str(target), entry_ref),
                )
                return cursor.rowcount == 1
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


def _resolve_source_refs(
    instance_root: Path,
    legacy_root: Path,
    entries: list[Path],
    checkpoint: dict[str, dict[str, str]],
) -> tuple[dict[str, tuple[str, str | None, str]], int]:
    source_refs: dict[str, tuple[str, str | None, str]] = {}
    for source in entries:
        source_handle = source.relative_to(legacy_root).as_posix()
        manifest = source / "资料信息.yml"
        generated_manifest = None
        if manifest.is_file():
            entry_ref = _entry_ref(source)
            method = "manifest"
        else:
            previous = checkpoint.get(source_handle)
            if previous is not None and "entry_ref" in previous:
                entry_ref = previous["entry_ref"]
                method = "checkpoint"
            else:
                entry_ref, method = _registered_entry(
                    instance_root / "data" / "knowledge.db", instance_root, source
                )
            generated_manifest = _generated_manifest(entry_ref, source)
        source_refs[source_handle] = (entry_ref, generated_manifest, method)
    return _select_authoritative_sources(source_refs)


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

    discovered_entries = _discover_entries(legacy_root)
    checkpoint = _checkpoint_records(instance_root / RESULTS_MIGRATION_STATE)
    source_refs, duplicates_skipped = _resolve_source_refs(
        instance_root, legacy_root, discovered_entries, checkpoint
    )
    entries = [
        source
        for source in discovered_entries
        if source.relative_to(legacy_root).as_posix() in source_refs
    ]
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
        entry_ref, generated_manifest, _method = source_refs[source_handle]
        source_digest = _tree_digest(source)
        synthetic = (
            {"资料信息.yml": generated_manifest.encode("utf-8")}
            if generated_manifest is not None
            else {}
        )
        digest = _tree_digest_with_files(source, synthetic)
        previous = checkpoint.get(source_handle)
        if previous is not None:
            previous_source_digest = previous.get("source_sha256", previous["sha256"])
            if previous_source_digest != source_digest or previous["sha256"] != digest:
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
        outcome = _copy_verified(
            source,
            target,
            digest,
            generated_manifest=generated_manifest,
        )
        copied += int(outcome == "copied")
        reused += int(outcome == "reused")
        records.append(
            {
                "source": source_handle,
                "target": target.relative_to(destination).as_posix(),
                "sha256": digest,
                "source_sha256": source_digest,
                "entry_ref": entry_ref,
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
        "discovered": len(discovered_entries),
        "selected": len(entries),
        "duplicates_skipped": duplicates_skipped,
        "copied": copied,
        "reused": reused,
        "verified": len(entries),
        "registered": registered,
        "manifests_generated": sum(
            generated_manifest is not None
            for _, generated_manifest, _method in source_refs.values()
        ),
        "source_preserved": True,
        "index_rebuilt": True,
        "state_handle": RESULTS_MIGRATION_STATE.as_posix(),
    }


def _migration_checkpoint_complete(path: Path) -> bool:
    _checkpoint_records(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultsMigrationError(
            "results_migration_state_invalid", "the migration checkpoint is invalid"
        ) from exc
    return isinstance(payload, dict) and payload.get("complete") is True


def _target_is_registered(
    database: Path,
    instance_root: Path,
    entry_ref: str,
    target: Path,
) -> bool:
    if not database.is_file():
        return False
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT library_path FROM collection_items WHERE job_id = ?", (entry_ref,)
            ).fetchone()
    except sqlite3.Error as exc:
        raise ResultsMigrationError(
            "results_cleanup_registry_failed", "the result registry is unreadable"
        ) from exc
    if row is None or not row[0]:
        return False
    registered = Path(str(row[0]))
    if not registered.is_absolute():
        registered = instance_root / registered
    return registered.resolve() == target.resolve()


def _validate_cleanup_scope(legacy_root: Path, entries: list[Path]) -> None:
    if legacy_root.is_symlink() or any(item.is_symlink() for item in legacy_root.rglob("*")):
        raise ResultsMigrationError(
            "results_cleanup_scope_unsafe", "legacy cleanup must not traverse symbolic links"
        )
    known_entries = {entry.resolve() for entry in entries}
    for child in legacy_root.iterdir():
        if child.name == INDEX_DIR_NAME:
            if not child.is_dir():
                raise ResultsMigrationError(
                    "results_cleanup_scope_unsafe", "the legacy index is not a directory"
                )
            continue
        if not child.is_dir():
            raise ResultsMigrationError(
                "results_cleanup_scope_unsafe", "the legacy root contains an unknown file"
            )
        for entry in child.iterdir():
            if not entry.is_dir() or entry.resolve() not in known_entries:
                raise ResultsMigrationError(
                    "results_cleanup_scope_unsafe",
                    "a legacy category contains an unknown artifact",
                )


def _verified_cleanup_plan(instance_root: Path) -> dict[str, Any]:
    destination = configured_results_root(instance_root)
    if destination is None or not destination.is_dir():
        raise ResultsMigrationError(
            "results_root_required", "a configured results root is required before cleanup"
        )
    legacy_root = instance_root / LEGACY_LIBRARY_ROOT
    entries = _discover_entries(legacy_root)
    _validate_cleanup_scope(legacy_root, entries)
    checkpoint_path = instance_root / RESULTS_MIGRATION_STATE
    checkpoint = _checkpoint_records(checkpoint_path)
    if not _migration_checkpoint_complete(checkpoint_path):
        raise ResultsMigrationError(
            "results_cleanup_not_ready", "legacy results have not completed migration"
        )
    source_refs, duplicates_skipped = _resolve_source_refs(
        instance_root, legacy_root, entries, checkpoint
    )
    if set(checkpoint) != set(source_refs):
        raise ResultsMigrationError(
            "results_cleanup_not_ready", "migration does not cover every authoritative result"
        )
    database = instance_root / "data" / "knowledge.db"
    verified = 0
    for source_handle, (entry_ref, generated_manifest, _method) in source_refs.items():
        source = legacy_root / Path(*PurePosixPath(source_handle).parts)
        record = checkpoint[source_handle]
        source_digest = _tree_digest(source)
        expected_source = record.get("source_sha256", record["sha256"])
        synthetic = (
            {"资料信息.yml": generated_manifest.encode("utf-8")}
            if generated_manifest is not None
            else {}
        )
        expected_target = _tree_digest_with_files(source, synthetic)
        if (
            record.get("entry_ref", entry_ref) != entry_ref
            or source_digest != expected_source
            or expected_target != record["sha256"]
        ):
            raise ResultsMigrationError(
                "results_cleanup_source_changed",
                "a legacy result changed after verified migration",
            )
        target = (destination / Path(*PurePosixPath(record["target"]).parts)).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise ResultsMigrationError(
                "results_migration_state_invalid",
                "a migration checkpoint target escapes the results root",
            ) from exc
        if (
            not target.is_dir()
            or _tree_digest(target) != record["sha256"]
            or _entry_ref(target) != entry_ref
            or not _target_is_registered(database, instance_root, entry_ref, target)
        ):
            raise ResultsMigrationError(
                "results_cleanup_target_unverified",
                "a migrated result or registry binding is no longer verified",
            )
        verified += 1
    return {
        "discovered": len(entries),
        "verified": verified,
        "duplicates": duplicates_skipped,
    }


def _cleanup_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultsMigrationError(
            "results_cleanup_state_invalid", "the cleanup checkpoint is invalid"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ResultsMigrationError(
            "results_cleanup_state_invalid", "the cleanup checkpoint is unsupported"
        )
    staging = payload.get("staging")
    digest = payload.get("legacy_sha256")
    counts = (payload.get("discovered"), payload.get("verified"), payload.get("duplicates"))
    if (
        payload.get("phase") not in {"planned", "deleting", "complete"}
        or not isinstance(staging, str)
        or not staging.startswith(_CLEANUP_STAGING_PREFIX)
        or len(staging) != len(_CLEANUP_STAGING_PREFIX) + 32
        or any(character not in "0123456789abcdef" for character in staging[-32:])
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not all(isinstance(count, int) and count >= 0 for count in counts)
    ):
        raise ResultsMigrationError(
            "results_cleanup_state_invalid", "the cleanup checkpoint is unsafe"
        )
    return payload


def cleanup_legacy_results(instance_root: Path) -> dict[str, Any]:
    instance_root = instance_root.resolve()
    legacy_root = instance_root / LEGACY_LIBRARY_ROOT
    state_path = instance_root / RESULTS_CLEANUP_STATE
    state = _cleanup_state(state_path)
    if state is None:
        if not legacy_root.exists():
            return {
                "status": "no_work",
                "deleted": 0,
                "verified": 0,
                "duplicates_deleted": 0,
                "source_removed": True,
                "results_preserved": True,
                "publication_history_preserved": True,
                "state_handle": RESULTS_CLEANUP_STATE.as_posix(),
            }
        plan = _verified_cleanup_plan(instance_root)
        state = {
            "schema_version": 1,
            "phase": "planned",
            "staging": f"{_CLEANUP_STAGING_PREFIX}{uuid.uuid4().hex}",
            "legacy_sha256": _tree_digest(legacy_root),
            **plan,
        }
        _atomic_json(state_path, state)
    staging = state_path.parent / str(state["staging"])
    if state["phase"] == "complete":
        if legacy_root.exists() or staging.exists():
            raise ResultsMigrationError(
                "results_cleanup_state_invalid",
                "legacy results reappeared after completed cleanup",
            )
    else:
        if legacy_root.exists() and staging.exists():
            raise ResultsMigrationError(
                "results_cleanup_state_invalid", "both cleanup sources exist"
            )
        if legacy_root.exists():
            if _tree_digest(legacy_root) != state["legacy_sha256"]:
                raise ResultsMigrationError(
                    "results_cleanup_source_changed",
                    "legacy results changed after cleanup was planned",
                )
            os.replace(legacy_root, staging)
            state["phase"] = "deleting"
            _atomic_json(state_path, state)
        if staging.exists():
            if _tree_digest(staging) != state["legacy_sha256"]:
                raise ResultsMigrationError(
                    "results_cleanup_source_changed",
                    "staged legacy results changed during cleanup",
                )
            try:
                shutil.rmtree(staging)
            except OSError as exc:
                raise ResultsMigrationError(
                    "results_cleanup_delete_failed",
                    "staged legacy results could not be deleted",
                ) from exc
        state["phase"] = "complete"
        _atomic_json(state_path, state)
    return {
        "status": "cleaned",
        "deleted": int(state["discovered"]),
        "verified": int(state["verified"]),
        "duplicates_deleted": int(state["duplicates"]),
        "source_removed": not legacy_root.exists() and not staging.exists(),
        "results_preserved": True,
        "publication_history_preserved": True,
        "state_handle": RESULTS_CLEANUP_STATE.as_posix(),
    }
