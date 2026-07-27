from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.analyze_video import JOB_ID_PATTERN
from douyin_knowledge.contracts import CliError
from douyin_knowledge.semantic_handoff import semantic_assignment_snapshot

BATCH_REF_PATTERN = re.compile(r"^batch-[a-f0-9]{20}$")
BATCH_SCHEMA_VERSION = 1
MAX_BATCH_ITEMS = 5


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _batch_path(root: Path, batch_ref: str) -> Path:
    if not BATCH_REF_PATTERN.fullmatch(batch_ref):
        raise CliError(
            "invalid_batch_ref",
            "the batch reference is invalid",
            "select a stable batch reference returned by batch create or batch status",
        )
    return root / "data" / "batches" / f"{batch_ref}.json"


def _load_json(path: Path, *, code: str, message: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(code, message, "restore the last verified private checkpoint") from exc
    if not isinstance(payload, dict):
        raise CliError(code, message, "restore the last verified private checkpoint")
    return payload


def _load_batch(root: Path, batch_ref: str) -> dict[str, Any]:
    path = _batch_path(root, batch_ref)
    if not path.is_file():
        raise CliError(
            "batch_missing",
            "the requested batch does not exist",
            "select a stable batch reference returned by batch create",
        )
    payload = _load_json(
        path,
        code="batch_checkpoint_invalid",
        message="the batch checkpoint could not be read",
    )
    items = payload.get("items")
    if (
        payload.get("schema_version") != BATCH_SCHEMA_VERSION
        or payload.get("batch_ref") != batch_ref
        or not isinstance(payload.get("created_at"), str)
        or not isinstance(items, list)
        or not 1 <= len(items) <= MAX_BATCH_ITEMS
    ):
        raise CliError(
            "batch_checkpoint_invalid",
            "the batch checkpoint failed validation",
            "restore the last verified private checkpoint",
        )
    seen: set[str] = set()
    for item in items:
        job_ref = item.get("job_ref") if isinstance(item, dict) else None
        if (
            not isinstance(job_ref, str)
            or not JOB_ID_PATTERN.fullmatch(job_ref)
            or job_ref in seen
        ):
            raise CliError(
                "batch_checkpoint_invalid",
                "the batch checkpoint contains an invalid job inventory",
                "restore the last verified private checkpoint",
            )
        seen.add(job_ref)
    return payload


def _registry_rows(root: Path, job_refs: list[str]) -> dict[str, sqlite3.Row]:
    database = root / "data" / "knowledge.db"
    if not database.is_file():
        raise CliError(
            "registry_missing",
            "the collection registry is unavailable",
            "initialize and sync the instance before creating or resuming a batch",
        )
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in job_refs)
        rows = connection.execute(
            f"SELECT * FROM collection_items WHERE job_id IN ({placeholders})", job_refs
        ).fetchall()
    finally:
        connection.close()
    return {str(row["job_id"]): row for row in rows}


def fixed_plan(
    root: Path,
    *,
    job_refs: list[str],
    required_status: str | None,
) -> list[dict[str, Any]]:
    root = root.resolve()
    if not 1 <= len(job_refs) <= MAX_BATCH_ITEMS:
        raise CliError(
            "invalid_batch_size",
            "a batch must contain between one and five job references",
            "select one to five stable job references returned by plan",
        )
    if len(set(job_refs)) != len(job_refs) or any(
        not JOB_ID_PATTERN.fullmatch(job_ref) for job_ref in job_refs
    ):
        raise CliError(
            "batch_inventory_invalid",
            "the batch contains invalid or duplicate job references",
            "select unique stable job references returned by plan",
        )
    rows = _registry_rows(root, job_refs)
    if set(rows) != set(job_refs):
        raise CliError(
            "batch_inventory_changed",
            "one or more selected jobs are no longer present in the registry",
            "refresh the collection and select a fixed batch again",
        )
    planned: list[dict[str, Any]] = []
    for job_ref in job_refs:
        row = rows[job_ref]
        keys = set(row.keys())
        if "currently_collected" in keys and int(row["currently_collected"] or 0) != 1:
            raise CliError(
                "job_no_longer_collected",
                "one or more selected jobs are no longer in the current favorites snapshot",
                "refresh the plan and do not substitute another job implicitly",
            )
        status = str(row["status"])
        if required_status is not None and status != required_status:
            raise CliError(
                "batch_status_changed",
                "one or more selected jobs no longer match the requested starting status",
                "refresh the plan and create a new fixed batch",
            )
        planned.append(
            {
                "job_ref": job_ref,
                "status": status,
                "position": int(row["last_position"] or 0) if "last_position" in keys else 0,
            }
        )
    return planned


def _unfinished_batch_jobs(root: Path) -> set[str]:
    directory = root / "data" / "batches"
    if not directory.is_dir():
        return set()
    claimed: set[str] = set()
    for path in directory.glob("batch-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CliError(
                "batch_checkpoint_invalid",
                "an existing batch checkpoint could not be read",
                "restore the last verified private checkpoint before creating more work",
            ) from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        refs = [
            str(item["job_ref"])
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("job_ref"), str)
            and JOB_ID_PATTERN.fullmatch(str(item["job_ref"]))
        ]
        if not refs:
            continue
        try:
            rows = _registry_rows(root, refs)
        except CliError:
            continue
        for ref in refs:
            row = rows.get(ref)
            publication = _latest_publication(root, ref) if row is not None else {}
            if (
                row is None
                or str(row["status"]) != "completed"
                or publication.get("state") != "accepted"
                or publication.get("targets_verified") is not True
            ):
                claimed.add(ref)
    return claimed


def create_batch(
    root: Path,
    *,
    planned: list[dict[str, Any]],
    requested_status: str | None,
) -> dict[str, Any]:
    root = root.resolve()
    if not 1 <= len(planned) <= MAX_BATCH_ITEMS:
        raise CliError(
            "invalid_batch_size",
            "a batch must contain between one and five planned jobs",
            "plan a fixed batch of one to five items",
        )
    job_refs = [str(item.get("job_ref") or "") for item in planned]
    if len(set(job_refs)) != len(job_refs) or any(
        not JOB_ID_PATTERN.fullmatch(job_ref) for job_ref in job_refs
    ):
        raise CliError(
            "batch_inventory_invalid",
            "the planned batch contains invalid or duplicate job references",
            "create the batch only from the current plan output",
        )
    rows = _registry_rows(root, job_refs)
    if set(rows) != set(job_refs):
        raise CliError(
            "batch_inventory_changed",
            "one or more planned jobs are no longer present in the registry",
            "refresh the collection and create a new fixed batch",
        )
    overlap = set(job_refs) & _unfinished_batch_jobs(root)
    if overlap:
        raise CliError(
            "batch_job_already_claimed",
            "one or more jobs already belong to an unfinished batch",
            "resume the existing batch instead of creating overlapping work",
        )
    batch_ref = f"batch-{uuid.uuid4().hex[:20]}"
    created_at = _now()
    payload = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_ref": batch_ref,
        "created_at": created_at,
        "requested_status": requested_status,
        "items": [
            {
                "job_ref": job_ref,
                "starting_status": str(item.get("status") or rows[job_ref]["status"]),
                "position": int(item.get("position") or 0),
            }
            for job_ref, item in zip(job_refs, planned, strict=True)
        ],
    }
    path = _batch_path(root, batch_ref)
    _atomic_json(path, payload)
    return {
        "batch_ref": batch_ref,
        "item_count": len(job_refs),
        "job_refs": job_refs,
        "state_handle": path.relative_to(root).as_posix(),
        "created_at": created_at,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_publication(root: Path, job_ref: str) -> dict[str, Any]:
    database = root / "data" / "knowledge.db"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'publication_sagas'"
        ).fetchone()
        if table is None:
            return {}
        saga = connection.execute(
            "SELECT * FROM publication_sagas WHERE job_ref = ? "
            "ORDER BY created_at DESC, saga_id DESC LIMIT 1",
            (job_ref,),
        ).fetchone()
        if saga is None:
            return {}
        target_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'publication_targets'"
        ).fetchone()
        result_handle = None
        targets_verified = True
        if target_table is not None:
            target_states = connection.execute(
                "SELECT status FROM publication_targets WHERE saga_id = ?",
                (str(saga["saga_id"]),),
            ).fetchall()
            targets_verified = bool(target_states) and all(
                str(target["status"]) == "verified" for target in target_states
            )
            target = connection.execute(
                "SELECT relative_handle FROM publication_targets "
                "WHERE saga_id = ? AND target_name = 'library'",
                (str(saga["saga_id"]),),
            ).fetchone()
            if target is not None:
                handle = str(target["relative_handle"])
                if handle == "results" or handle.startswith("results/"):
                    result_handle = handle
        return {
            "state": str(saga["state"]),
            "updated_at": str(saga["updated_at"]),
            "result_handle": result_handle,
            "targets_verified": targets_verified,
        }
    finally:
        connection.close()


def _candidate_is_current(task: Path, manifest: dict[str, Any]) -> tuple[bool, bool]:
    candidate_path = task / "candidate-v1.json"
    candidate = _read_optional_json(candidate_path)
    current = bool(
        candidate
        and candidate.get("job_ref") == task.parent.name
        and isinstance(manifest.get("packet_sha256"), str)
        and candidate.get("packet_sha256") == manifest.get("packet_sha256")
    )
    if not current:
        return False, False
    imported_hash = manifest.get("imported_candidate_sha256")
    imported = isinstance(imported_hash, str) and imported_hash == _sha256(candidate_path)
    return True, imported


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _item_times(
    checkpoint: dict[str, Any], publication: dict[str, Any], batch_created_at: str
) -> tuple[str, str, float]:
    stages = checkpoint.get("stages") if isinstance(checkpoint.get("stages"), dict) else {}
    timestamps: list[datetime] = []
    for stage in stages.values():
        if not isinstance(stage, dict):
            continue
        for field in ("started_at", "completed_at"):
            parsed = _parse_time(stage.get(field))
            if parsed is not None:
                timestamps.append(parsed)
    for value in (
        checkpoint.get("started_at"),
        checkpoint.get("stopped_at"),
        checkpoint.get("paused_at"),
        publication.get("updated_at"),
    ):
        parsed = _parse_time(value)
        if parsed is not None:
            timestamps.append(parsed)
    created = _parse_time(batch_created_at) or datetime.now(UTC)
    timestamps = [value for value in timestamps if value >= created]
    started = min(timestamps, default=created)
    updated = max(timestamps, default=created)
    return started.isoformat(), updated.isoformat(), round(
        max(0.0, (updated - started).total_seconds()), 3
    )


def _item_status(
    root: Path,
    item: dict[str, Any],
    row: sqlite3.Row,
    *,
    batch_created_at: str,
) -> dict[str, Any]:
    job_ref = str(item["job_ref"])
    task_root = root / "data" / "tasks" / job_ref
    semantic = task_root / "semantic-v1"
    checkpoint = _read_optional_json(task_root / "run-checkpoint.json")
    protocol_manifest = _read_optional_json(semantic / "protocol-manifest.json")
    publication = _latest_publication(root, job_ref)
    candidate_current, candidate_imported = _candidate_is_current(semantic, protocol_manifest)
    registry_status = str(row["status"])
    error_code = checkpoint.get("error") if isinstance(checkpoint.get("error"), str) else None

    row_keys = set(row.keys())
    currently_collected = (
        bool(row["currently_collected"])
        if "currently_collected" in row_keys
        else True
    )
    if (
        registry_status == "completed"
        and publication.get("state") == "accepted"
        and publication.get("targets_verified") is True
    ):
        stage = "accepted"
    elif registry_status == "completed" and publication.get("state") == "accepted":
        stage = "failed"
        error_code = "publication_target_drift"
    elif not currently_collected:
        stage = "failed"
        error_code = "job_no_longer_collected"
    elif checkpoint.get("status") == "paused" or registry_status in {"failed", "incomplete"}:
        stage = "failed"
    elif candidate_imported and (
        root / "orchestration" / "content-drafts" / f"{job_ref}-content.md"
    ).is_file():
        stage = "staged"
    elif candidate_current:
        stage = "semantic_ready"
    elif protocol_manifest and (semantic / "content-packet.json").is_file():
        stage = "packet_ready"
    elif (root / "data" / "jobs" / job_ref / "analysis" / "manifest.json").is_file():
        stage = "analyzed"
    elif (root / "data" / "jobs" / job_ref / "source.mp4").is_file():
        stage = "downloaded"
    else:
        stage = "planned"

    started_at, updated_at, elapsed_seconds = _item_times(
        checkpoint, publication, batch_created_at
    )
    result: dict[str, Any] = {
        "job_ref": job_ref,
        "position": int(item.get("position") or 0),
        "starting_status": str(item.get("starting_status") or ""),
        "registry_status": registry_status,
        "stage": stage,
        "active": (task_root / "run.lock").is_file(),
        "error_code": error_code,
        "started_at": started_at,
        "updated_at": updated_at,
        "elapsed_seconds": elapsed_seconds,
        "stage_timings": (
            checkpoint.get("stages") if isinstance(checkpoint.get("stages"), dict) else {}
        ),
        "publication_state": publication.get("state"),
    }
    if publication.get("result_handle"):
        result["result_handle"] = publication["result_handle"]
    return result


def batch_status(root: Path, batch_ref: str) -> dict[str, Any]:
    root = root.resolve()
    batch = _load_batch(root, batch_ref)
    job_refs = [str(item["job_ref"]) for item in batch["items"]]
    rows = _registry_rows(root, job_refs)
    if set(rows) != set(job_refs):
        raise CliError(
            "batch_inventory_changed",
            "one or more batch jobs are no longer present in the registry",
            "restore the registry before resuming this fixed batch",
        )
    items = [
        _item_status(
            root,
            item,
            rows[str(item["job_ref"])],
            batch_created_at=str(batch["created_at"]),
        )
        for item in batch["items"]
    ]
    counts = Counter(str(item["stage"]) for item in items)
    active = [item for item in items if item["active"]]
    cpu_busy = (root / "data" / "run-leases" / "cpu.lock").is_file()
    semantic = semantic_assignment_snapshot(root)
    assigned_semantic_jobs = set(semantic["job_refs"])
    cpu_candidates = [
        item["job_ref"]
        for item in items
        if item["stage"] in {"planned", "downloaded", "analyzed"}
    ]
    data: dict[str, Any] = {
        "batch_ref": batch_ref,
        "created_at": batch["created_at"],
        "item_count": len(items),
        "completed_count": counts.get("accepted", 0),
        "complete": counts.get("accepted", 0) == len(items),
        "status": (
            "complete"
            if counts.get("accepted", 0) == len(items)
            else ("blocked" if counts.get("failed", 0) else "running")
        ),
        "scope": {"job_refs": job_refs, "locked": True},
        "by_stage": dict(sorted(counts.items())),
        "items": items,
        "recommended": {
            "cpu_job_ref": (
                None if active or cpu_busy else (cpu_candidates[0] if cpu_candidates else None)
            ),
            "semantic_job_refs": [
                item["job_ref"]
                for item in items
                if item["stage"] == "packet_ready"
                and item["job_ref"] not in assigned_semantic_jobs
            ][: int(semantic["available"])],
            "import_job_refs": [
                item["job_ref"] for item in items if item["stage"] == "semantic_ready"
            ],
            "publish_job_ref": next(
                (item["job_ref"] for item in items if item["stage"] == "staged"), None
            ),
            "failed_job_refs": [
                item["job_ref"] for item in items if item["stage"] == "failed"
            ],
        },
        "resources": {
            "cpu_busy": cpu_busy,
            "semantic_slots_used": int(semantic["used"]),
            "semantic_slots_available": int(semantic["available"]),
            "semantic_job_refs": list(semantic["job_refs"]),
            "publisher_busy": (
                root / "data" / "run-leases" / "publisher.lock"
            ).is_file(),
        },
    }
    return data
