from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.analyze_video import JOB_ID_PATTERN
from app.content_stage import ContentStageError, validate_content_draft
from douyin_knowledge.contracts import CliError
from douyin_knowledge.protocol import export_packet

STALE_LEASE_SECONDS = 2 * 60 * 60


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


class _JobLease:
    def __init__(self, root: Path, job_ref: str) -> None:
        self.root = root
        self.job_ref = job_ref
        self.path = root / "data" / "tasks" / job_ref / "run.lock"
        self.acquired = False

    def __enter__(self) -> _JobLease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            age = 0
        if age > STALE_LEASE_SECONDS:
            digest = "unknown"
            try:
                digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
            except OSError:
                pass
            quarantine = self.root / "quarantine" / "run-locks" / self.job_ref
            quarantine.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(self.path, quarantine / f"{digest}.lock")
            except FileNotFoundError:
                pass
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise CliError(
                "run_locked",
                "the selected job already has an active run lease",
                "wait for the active run or inspect status before retrying",
                retryable=True,
            ) from exc
        try:
            os.write(
                descriptor,
                (json.dumps({"job_ref": self.job_ref, "started_at": _now()}) + "\n").encode(),
            )
        finally:
            os.close(descriptor)
        self.acquired = True
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)


def _load_checkpoint(path: Path, job_ref: str) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": 1,
            "job_ref": job_ref,
            "status": "new",
            "stages": {},
            "failures": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(
            "run_checkpoint_invalid",
            "the run checkpoint could not be read",
            "restore the last verified checkpoint before retrying",
        ) from exc
    if not isinstance(payload, dict) or payload.get("job_ref") != job_ref:
        raise CliError(
            "run_checkpoint_invalid",
            "the run checkpoint does not match the selected job",
            "restore the last verified checkpoint before retrying",
        )
    return payload


def _complete_stage(
    checkpoint_path: Path,
    checkpoint: dict[str, Any],
    stage: str,
    *,
    reused: bool,
) -> None:
    checkpoint.setdefault("stages", {})[stage] = {
        "status": "completed",
        "reused": reused,
        "completed_at": _now(),
    }
    checkpoint.update({"status": "running", "current_stage": stage, "error": None})
    _atomic_json(checkpoint_path, checkpoint)


def _item(database: Path, job_ref: str) -> sqlite3.Row:
    if not JOB_ID_PATTERN.fullmatch(job_ref):
        raise CliError(
            "invalid_job_ref",
            "job reference is invalid",
            "select a job reference returned by plan",
        )
    if not database.is_file():
        raise CliError(
            "registry_missing",
            "the collection registry is unavailable",
            "initialize and sync the instance before running a job",
        )
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM collection_items WHERE job_id = ?", (job_ref,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise CliError(
            "job_not_planned",
            "the requested job is not present in the registry",
            "select one stable job reference returned by plan",
        )
    return row


def _run_private(command: list[str], *, stage: str, timeout: int) -> None:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CliError(
            f"{stage}_failed",
            f"{stage} did not complete",
            "inspect the private local log before retrying the same job",
            retryable=True,
        ) from exc
    if completed.returncode != 0:
        raise CliError(
            f"{stage}_failed",
            f"{stage} did not complete",
            "inspect the private local log before retrying the same job",
            retryable=True,
        )


def _download(root: Path, job_ref: str, row: sqlite3.Row) -> bool:
    source = root / "data" / "jobs" / job_ref / "source.mp4"
    if source.is_file() and source.stat().st_size > 0:
        return True
    columns = set(row.keys())
    source_id = str(row["source_id"] if "source_id" in columns else "")
    position = int(row["last_position"] if "last_position" in columns else 0)
    if not source_id or position < 1:
        raise CliError(
            "download_binding_missing",
            "the registry lacks a stable source binding for this job",
            "sync the collection snapshot again before downloading",
        )
    handoff = root / "data" / "jobs" / job_ref / "download-handoff.json"
    _run_private(
        [
            sys.executable,
            "-m",
            "app.probe_one",
            "--root",
            str(root),
            "--position",
            str(position),
            "--job-id",
            job_ref,
            "--aweme-id",
            source_id,
            "--handoff",
            str(handoff),
        ],
        stage="download",
        timeout=900,
    )
    try:
        binding = json.loads(handoff.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(
            "download_handoff_invalid",
            "download completed without a valid identity handoff",
            "inspect the private log before retrying the same job",
            retryable=True,
        ) from exc
    if binding.get("job_id") != job_ref or not source.is_file():
        raise CliError(
            "download_binding_changed",
            "download output did not match the selected stable job",
            "sync again and select the returned job reference",
        )
    return False


def _analyze(root: Path, job_ref: str) -> bool:
    manifest = root / "data" / "jobs" / job_ref / "analysis" / "manifest.json"
    if manifest.is_file() and manifest.stat().st_size > 0:
        return True
    _run_private(
        [
            sys.executable,
            "-m",
            "app.analyze_video",
            "--root",
            str(root),
            "--job-id",
            job_ref,
        ],
        stage="analysis",
        timeout=7200,
    )
    if not manifest.is_file():
        raise CliError(
            "analysis_incomplete",
            "local analysis did not produce a manifest",
            "inspect the private analysis log before retrying",
            retryable=True,
        )
    return False


def run_job(root: Path, *, job_ref: str, stop_after: str) -> dict[str, Any]:
    root = root.resolve()
    row = _item(root / "data" / "knowledge.db", job_ref)
    checkpoint_path = root / "data" / "tasks" / job_ref / "run-checkpoint.json"
    with _JobLease(root, job_ref):
        checkpoint = _load_checkpoint(checkpoint_path, job_ref)
        stage = "download"
        try:
            download_reused = _download(root, job_ref, row)
            _complete_stage(
                checkpoint_path, checkpoint, "download", reused=download_reused
            )
            common: dict[str, Any] = {
                "job_ref": job_ref,
                "model_calls": 0,
                "publish": False,
                "download_reused": download_reused,
            }
            if stop_after == "download":
                result = {**common, "status": "downloaded"}
            else:
                stage = "analysis"
                analysis_reused = _analyze(root, job_ref)
                _complete_stage(
                    checkpoint_path, checkpoint, "analysis", reused=analysis_reused
                )
                common["analysis_reused"] = analysis_reused
                if stop_after == "analysis":
                    result = {**common, "status": "analyzed"}
                else:
                    stage = "packet"
                    packet = export_packet(root, job_ref)
                    _complete_stage(checkpoint_path, checkpoint, "packet", reused=False)
                    common["packet_handle"] = packet["packet_handle"]
                    common["candidate_schema_handle"] = packet["candidate_schema_handle"]
                    common["candidate_output_handle"] = packet["candidate_output_handle"]
                    if stop_after == "packet":
                        result = {**common, "status": "packet_ready"}
                    else:
                        stage = "staging"
                        candidate = root / packet["candidate_output_handle"]
                        draft = (
                            root
                            / "orchestration"
                            / "content-drafts"
                            / f"{job_ref}-content.md"
                        )
                        if not candidate.is_file() or not draft.is_file():
                            raise CliError(
                                "candidate_required",
                                "staging requires an imported candidate",
                                "have one AI worker write the candidate JSON, then import it",
                            )
                        try:
                            validate_content_draft(root, job_ref, draft)
                        except (ContentStageError, OSError) as exc:
                            raise CliError(
                                getattr(exc, "code", "staging_invalid"),
                                "staged content did not pass deterministic validation",
                                "repair the rejected candidate fields and import once more",
                            ) from exc
                        _complete_stage(
                            checkpoint_path, checkpoint, "staging", reused=True
                        )
                        result = {
                            **common,
                            "status": "staged",
                            "draft_handle": draft.relative_to(root).as_posix(),
                        }
            checkpoint.update(
                {
                    "status": "stopped",
                    "current_stage": None,
                    "stop_after": stop_after,
                    "stopped_at": _now(),
                    "error": None,
                }
            )
            _atomic_json(checkpoint_path, checkpoint)
            return result
        except CliError as exc:
            key = f"{stage}:{exc.code}"
            failures = checkpoint.setdefault("failures", {})
            count = int(failures.get(key, 0)) + 1
            failures[key] = count
            checkpoint.update(
                {
                    "status": "paused",
                    "current_stage": stage,
                    "error": exc.code,
                    "same_failure_count": count,
                    "paused_at": _now(),
                }
            )
            _atomic_json(checkpoint_path, checkpoint)
            raise CliError(
                exc.code,
                exc.message,
                exc.user_action,
                retryable=bool(exc.retryable and count < 2),
                preserved_checkpoint=True,
                exit_code=exc.exit_code,
            ) from exc
