from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.analyze_video import JOB_ID_PATTERN
from app.collection_registry import stable_collection_job_id, update_item_by_job
from app.content_stage import (
    ContentStageError,
    is_content_protocol_error,
    preflight_content_runner,
    run_content_stage,
    validate_content_draft,
)
from app.obsidian_publish import _generated_body
from app.probe_one import resolve_handoff_path
from app.publish_library import publish_job, sha256_file
from app.security import GateError
from app.structured_content import (
    StructuredContentError,
    generate_structured_json,
    render_structured_json_artifact,
    validate_structured_artifacts,
    validate_structured_json_artifact,
)
from app.structured_content import (
    sha256_file as structured_sha256_file,
)

SUCCESS = 0
PREFLIGHT_FAILURE = 2
CONTROLLED_FAILURE = 4
INTERNAL_FAILURE = 5
HEARTBEAT_SECONDS = 15
STAGES = (
    "download",
    "analysis",
    "structured_generate",
    "schema_validate",
    "render",
    "staging_accept",
    "publish",
    "accept",
)
ANALYSIS_FILES = ("summary.md", "transcript.json", "ocr.json", "timeline.md", "manifest.json")
SENSITIVE = (
    re.compile(r"(?i)\b(cookie|signature|request[_ -]?url)\b"),
    re.compile(r"\baweme-[a-f0-9]{20}\b", re.I),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def append_line(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(text.rstrip() + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True)
class SingleItemConfig:
    root: Path
    task_dir: Path
    job_id: str
    position: int
    vault: Path
    primary_runner: Path
    fallback_runner: Path
    aweme_id: str | None = None
    category: str = "人工智能与数字工具"
    tags: tuple[str, ...] = ("AI", "知识管理", "待复核")
    asr_model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    structured_runner: Path | None = None
    stop_after_structured_content: bool = False
    dry_run: bool = False

    @property
    def checkpoint_path(self) -> Path:
        return self.task_dir / "artifacts" / "single-item-checkpoint.json"

    @property
    def acceptance_path(self) -> Path:
        return self.task_dir / "artifacts" / "single-item-final-acceptance.json"

    @property
    def draft_path(self) -> Path:
        return self.root / "orchestration" / "content-drafts" / f"{self.job_id}-content.md"

    @property
    def structured_raw_path(self) -> Path:
        return (
            self.root
            / "orchestration"
            / "structured-content"
            / self.job_id
            / "response-v1.json"
        )

    @property
    def structured_manifest_path(self) -> Path:
        return self.structured_raw_path.parent / "manifest-v1.json"

    @property
    def active_structured_runner(self) -> Path:
        return self.structured_runner or self.primary_runner

    @property
    def job_dir(self) -> Path:
        return self.root / "data" / "jobs" / self.job_id

    @property
    def download_handoff_path(self) -> Path:
        return self.job_dir / "single-item-download-handoff.json"


class SingleItemError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        import ctypes

        query = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(query, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return False


class SingleItemLease:
    def __init__(self, config: SingleItemConfig) -> None:
        self.config = config
        self.path = config.task_dir / "single-item-driver.lock"
        self.lease_id = uuid.uuid4().hex

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if _pid_alive(int(existing.get("pid") or 0)):
                raise SingleItemError("single_item_owner_exists", "单条 driver 已在运行")
            self.path.unlink(missing_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(
                descriptor,
                (
                    json.dumps(
                        {
                            "lease_id": self.lease_id,
                            "pid": os.getpid(),
                            "job_id": self.config.job_id,
                            "position": self.config.position,
                            "heartbeat_at": utc_now(),
                        }
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def heartbeat(self, stage: str) -> None:
        atomic_json(
            self.path,
            {
                "lease_id": self.lease_id,
                "pid": os.getpid(),
                "job_id": self.config.job_id,
                "position": self.config.position,
                "stage": stage,
                "heartbeat_at": utc_now(),
            },
        )

    def release(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("lease_id") == self.lease_id:
            self.path.unlink(missing_ok=True)


def _connect(config: SingleItemConfig) -> sqlite3.Connection:
    connection = sqlite3.connect(config.root / "data" / "knowledge.db", timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _bound_item(config: SingleItemConfig) -> sqlite3.Row:
    if not JOB_ID_PATTERN.fullmatch(config.job_id) or config.position < 1:
        raise SingleItemError("single_item_binding_invalid", "JobId 或 Position 无效")
    if config.aweme_id and stable_collection_job_id(config.aweme_id) != config.job_id:
        raise SingleItemError("single_item_binding_invalid", "AwemeId 与 JobId 不匹配")
    with _connect(config) as connection:
        row = connection.execute(
            "SELECT * FROM collection_items WHERE job_id = ?", (config.job_id,)
        ).fetchone()
    if row is None or not row["currently_collected"]:
        raise SingleItemError("single_item_not_current", "固定条目不在当前收藏中")
    if config.aweme_id and (
        row["source_id"] != config.aweme_id or row["aweme_id"] != config.aweme_id
    ):
        raise SingleItemError("single_item_identity_conflict", "稳定作品身份发生冲突")
    return row


def _observe_position(
    config: SingleItemConfig,
    checkpoint: dict[str, Any],
    current_position: int,
    *,
    source: str,
) -> None:
    original = int(
        checkpoint.get("original_position") or checkpoint.get("position") or config.position
    )
    checkpoint["position"] = original
    checkpoint["original_position"] = original
    checkpoint["current_position"] = current_position
    history = checkpoint.setdefault("position_drift_history", [])
    previous = history[-1] if history else None
    if (
        not isinstance(previous, dict)
        or int(previous.get("current_position") or 0) != current_position
    ):
        history.append(
            {
                "observed_at": utc_now(),
                "source": source,
                "original_position": original,
                "current_position": current_position,
            }
        )


def _checkpoint(config: SingleItemConfig) -> dict[str, Any]:
    if config.checkpoint_path.is_file():
        payload = json.loads(config.checkpoint_path.read_text(encoding="utf-8"))
        payload.setdefault("stages", {})
        timings = payload.setdefault("timings", {})
        for stage in STAGES:
            timings.setdefault(stage, [])
        original_position = payload.get("original_position", payload.get("position"))
        if payload.get("job_id") != config.job_id or original_position != config.position:
            raise SingleItemError("single_item_checkpoint_mismatch", "检查点属于其他条目")
        checkpoint_aweme_id = payload.get("aweme_id")
        if checkpoint_aweme_id and config.aweme_id and checkpoint_aweme_id != config.aweme_id:
            raise SingleItemError("single_item_checkpoint_mismatch", "检查点作品身份冲突")
        if config.aweme_id:
            payload["aweme_id"] = config.aweme_id
        return payload
    return {
        "schema_version": 1,
        "job_id": config.job_id,
        "aweme_id": config.aweme_id,
        "position": config.position,
        "original_position": config.position,
        "current_position": config.position,
        "position_drift_history": [],
        "created_at": utc_now(),
        "status": "ready",
        "current_stage": None,
        "stages": {},
        "timings": {stage: [] for stage in STAGES},
        "content": {"active_runner": "primary", "fallback_switches": 0},
        "structured_content": {"runner_calls": 0, "retry_count": 0},
        "structured_staging_baseline": None,
        "backup": None,
        "error": None,
    }


def _save_checkpoint(config: SingleItemConfig, checkpoint: dict[str, Any]) -> None:
    checkpoint["updated_at"] = utc_now()
    atomic_json(config.checkpoint_path, checkpoint)


def _update_task_state(config: SingleItemConfig, **updates: object) -> None:
    path = config.task_dir / "state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    state.update(updates)
    atomic_json(path, state)


def _run_subprocess(
    config: SingleItemConfig, lease: SingleItemLease, stage: str, command: list[str]
) -> int:
    environment = os.environ.copy()
    environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    stdout = config.task_dir / "artifacts" / "single-item-stdout.log"
    stderr = config.task_dir / "artifacts" / "single-item-stderr.log"
    stdout.parent.mkdir(parents=True, exist_ok=True)
    with stdout.open("a", encoding="utf-8") as out, stderr.open("a", encoding="utf-8") as err:
        process = subprocess.Popen(
            command,
            cwd=config.root,
            env=environment,
            stdout=out,
            stderr=err,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        while True:
            try:
                return int(process.wait(timeout=HEARTBEAT_SECONDS))
            except subprocess.TimeoutExpired:
                lease.heartbeat(stage)


def _timed(
    config: SingleItemConfig,
    lease: SingleItemLease,
    checkpoint: dict[str, Any],
    stage: str,
    operation: Callable[[], Any],
) -> Any:
    started = time.monotonic()
    attempt = {
        "started_at": utc_now(),
        "started_monotonic": started,
        "status": "running",
    }
    checkpoint["status"] = "running"
    checkpoint["current_stage"] = stage
    checkpoint["timings"].setdefault(stage, []).append(attempt)
    _save_checkpoint(config, checkpoint)
    lease.heartbeat(stage)
    try:
        result = operation()
    except Exception:
        ended = time.monotonic()
        attempt.update(
            {
                "ended_at": utc_now(),
                "ended_monotonic": ended,
                "duration_seconds": round(ended - started, 3),
                "status": "failed",
            }
        )
        _save_checkpoint(config, checkpoint)
        raise
    ended = time.monotonic()
    attempt.update(
        {
            "ended_at": utc_now(),
            "ended_monotonic": ended,
            "duration_seconds": round(ended - started, 3),
            "status": "completed",
        }
    )
    checkpoint["stages"][stage] = {"status": "completed", "completed_at": utc_now()}
    _save_checkpoint(config, checkpoint)
    return result


def _record_reused(config: SingleItemConfig, checkpoint: dict[str, Any], stage: str) -> None:
    value = time.monotonic()
    checkpoint["timings"].setdefault(stage, []).append(
        {
            "started_at": utc_now(),
            "ended_at": utc_now(),
            "started_monotonic": value,
            "ended_monotonic": value,
            "duration_seconds": 0.0,
            "status": "checkpoint_reused",
        }
    )
    checkpoint["stages"][stage] = {"status": "completed", "completed_at": utc_now()}
    _save_checkpoint(config, checkpoint)


def _download_valid(config: SingleItemConfig) -> bool:
    source = config.job_dir / "source.mp4"
    return source.is_file() and source.stat().st_size > 0


def _analysis_valid(config: SingleItemConfig) -> bool:
    analysis = config.job_dir / "analysis"
    return all(
        (analysis / name).is_file() and (analysis / name).stat().st_size > 0
        for name in ANALYSIS_FILES
    )


def _structured_generate_valid(config: SingleItemConfig) -> bool:
    try:
        manifest = json.loads(config.structured_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    raw = manifest.get("raw_json") if isinstance(manifest, dict) else None
    return (
        isinstance(raw, dict)
        and config.structured_raw_path.is_file()
        and raw.get("path") == str(config.structured_raw_path)
        and raw.get("sha256") == structured_sha256_file(config.structured_raw_path)
    )


def _schema_validate_valid(config: SingleItemConfig) -> bool:
    try:
        validate_structured_json_artifact(
            config.root,
            config.job_id,
            config.structured_raw_path,
        )
    except (StructuredContentError, OSError):
        return False
    return True


def _render_valid(config: SingleItemConfig) -> bool:
    try:
        validate_structured_artifacts(
            config.root,
            config.job_id,
            config.structured_raw_path,
            config.draft_path,
        )
    except (StructuredContentError, OSError):
        return False
    return True


def _staging_accept_valid(_config: SingleItemConfig) -> bool:
    return False


def _content_valid(config: SingleItemConfig) -> bool:
    try:
        validate_content_draft(config.root, config.job_id, config.draft_path)
    except (ContentStageError, OSError):
        return False
    return True


def _publish_valid(config: SingleItemConfig) -> bool:
    row = _bound_item(config)
    if not row["library_path"]:
        return False
    library = Path(row["library_path"])
    if not library.is_absolute():
        library = config.root / library
    return row["status"] == "completed" and (library / "内容整理.md").is_file()


def _accept_valid(config: SingleItemConfig) -> bool:
    try:
        payload = json.loads(config.acceptance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("status") == "passed"
        and payload.get("job_id") == config.job_id
        and payload.get("original_position", payload.get("position")) == config.position
    )


def _download(
    config: SingleItemConfig, lease: SingleItemLease, checkpoint: dict[str, Any]
) -> None:
    handoff = resolve_handoff_path(config.root, config.download_handoff_path)
    if handoff is None:
        raise SingleItemError("single_item_handoff_invalid", "下载 handoff 路径无效")
    handoff.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-m",
        "app.probe_one",
        "--root",
        str(config.root),
        "--source",
        "collect-video",
        "--position",
        str(config.position),
        "--job-id",
        config.job_id,
        "--aweme-id",
        str(config.aweme_id or ""),
        "--handoff",
        str(handoff),
    ]
    code = _run_subprocess(config, lease, "download", command)
    if code != 0 or not handoff.is_file():
        raise SingleItemError("single_item_download_failed", "固定条目下载失败")
    payload = json.loads(handoff.read_text(encoding="utf-8"))
    if payload.get("job_id") != config.job_id or (
        config.aweme_id and payload.get("aweme_id") != config.aweme_id
    ):
        raise SingleItemError("single_item_download_binding_changed", "下载结果与固定 JobId 不符")
    observed_position = int(payload.get("observed_position") or 0)
    if observed_position < 1:
        raise SingleItemError("single_item_observed_position_invalid", "下载结果缺少当前位置")
    _observe_position(config, checkpoint, observed_position, source="collection_refresh")
    checkpoint["identity"] = {
        "job_id": config.job_id,
        "aweme_id": config.aweme_id,
        "author": str(payload.get("author") or ""),
        "title": str(payload.get("title") or ""),
    }
    _save_checkpoint(config, checkpoint)
    if not _download_valid(config):
        raise SingleItemError("single_item_download_incomplete", "下载未生成有效媒体")


def _analysis(config: SingleItemConfig, lease: SingleItemLease, _: dict[str, Any]) -> None:
    command = [
        sys.executable,
        "-m",
        "app.analyze_video",
        "--root",
        str(config.root),
        "--job-id",
        config.job_id,
        "--asr-model",
        config.asr_model,
        "--device",
        config.device,
        "--compute-type",
        config.compute_type,
    ]
    code = _run_subprocess(config, lease, "analysis", command)
    if code != 0 or not _analysis_valid(config):
        raise SingleItemError("single_item_analysis_failed", "固定条目分析失败")


def _run_with_heartbeat(
    lease: SingleItemLease,
    stage: str,
    operation: Callable[[], Any],
) -> Any:
    stop = threading.Event()

    def pulse() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            lease.heartbeat(stage)

    thread = threading.Thread(target=pulse, name=f"single-item-{stage}-heartbeat", daemon=True)
    thread.start()
    try:
        return operation()
    finally:
        stop.set()
        thread.join(timeout=HEARTBEAT_SECONDS)


def _content(
    config: SingleItemConfig, lease: SingleItemLease, checkpoint: dict[str, Any]
) -> None:
    content = checkpoint["content"]
    active = str(content.get("active_runner") or "primary")
    previous_error = str(checkpoint.get("error") or "")
    previous_content_failed = any(
        item.get("status") == "failed"
        for item in checkpoint.get("timings", {}).get("content", [])
        if isinstance(item, dict)
    )
    if (
        active == "primary"
        and int(content.get("fallback_switches") or 0) == 0
        and is_content_protocol_error(previous_error)
        and previous_content_failed
    ):
        content.setdefault("failures", []).append(
            {
                "runner": "primary",
                "error_code": previous_error,
                "recorded_at": utc_now(),
                "quarantine": {
                    "status": "unavailable_legacy_deleted",
                    "reason": "candidate_removed_before_quarantine_support",
                },
            }
        )
        content["active_runner"] = "fallback"
        content["fallback_switches"] = 1
        content["switched_at"] = utc_now()
        content["switch_reason"] = previous_error
        _save_checkpoint(config, checkpoint)
        append_line(
            config.task_dir / "run.log",
            f"{utc_now()} 内容主 runner 协议失败 {previous_error}；旧候选稿已被旧逻辑删除，"
            "无可恢复哈希；切换一次备用 runner。",
        )
        active = "fallback"

    def record_failure(runner_name: str, exc: ContentStageError) -> None:
        failure = {
            "runner": runner_name,
            "error_code": exc.code,
            "recorded_at": utc_now(),
            "quarantine": exc.quarantine,
        }
        content.setdefault("failures", []).append(failure)
        _save_checkpoint(config, checkpoint)
        quarantine = exc.quarantine or {}
        append_line(
            config.task_dir / "run.log",
            f"{utc_now()} 内容 {runner_name} runner 失败：{exc.code}；"
            f"quarantine={quarantine.get('path', 'none')}；"
            f"sha256={quarantine.get('sha256', 'none')}。",
        )

    runner = config.fallback_runner if active == "fallback" else config.primary_runner
    preflight_content_runner(config.root, config.job_id, runner, config.draft_path)
    try:
        _run_with_heartbeat(
            lease,
            "content",
            lambda: run_content_stage(config.root, config.job_id, runner, config.draft_path),
        )
        return
    except ContentStageError as exc:
        record_failure(active, exc)
        if active == "fallback" or not is_content_protocol_error(exc):
            raise
    if int(content.get("fallback_switches") or 0) >= 1:
        raise SingleItemError("single_item_fallback_exhausted", "备用内容 runner 已使用")
    content["active_runner"] = "fallback"
    content["fallback_switches"] = 1
    content["switched_at"] = utc_now()
    content["switch_reason"] = content["failures"][-1]["error_code"]
    _save_checkpoint(config, checkpoint)
    preflight_content_runner(config.root, config.job_id, config.fallback_runner, config.draft_path)
    try:
        _run_with_heartbeat(
            lease,
            "content",
            lambda: run_content_stage(
                config.root,
                config.job_id,
                config.fallback_runner,
                config.draft_path,
            ),
        )
    except ContentStageError as exc:
        record_failure("fallback", exc)
        raise


def _structured_generate(
    config: SingleItemConfig,
    lease: SingleItemLease,
    checkpoint: dict[str, Any],
) -> None:
    record = checkpoint.setdefault(
        "structured_content", {"runner_calls": 0, "retry_count": 0}
    )
    try:
        result = _run_with_heartbeat(
            lease,
            "structured_generate",
            lambda: generate_structured_json(
                config.root,
                config.job_id,
                config.active_structured_runner,
                config.structured_raw_path,
                max_calls=2,
            ),
        )
    except StructuredContentError as exc:
        record.update(
            {
                "status": "paused",
                "error_code": exc.code,
                "attempts": exc.attempts,
                "quarantine": exc.quarantine,
                "updated_at": utc_now(),
            }
        )
        _save_checkpoint(config, checkpoint)
        raise
    record.update(
        {
            "status": "generated",
            "raw_path": str(result.raw_path),
            "raw_sha256": structured_sha256_file(result.raw_path),
            "schema_path": str(result.schema_path),
            "schema_sha256": structured_sha256_file(result.schema_path),
            "manifest_path": str(result.manifest_path),
            "runner_calls": result.runner_calls,
            "retry_count": result.retry_count,
            "elapsed_seconds": result.elapsed_seconds,
            "attempts": result.attempts,
            "reused_json": result.reused_json,
            "updated_at": utc_now(),
        }
    )
    checkpoint["error"] = None
    _save_checkpoint(config, checkpoint)


def _schema_validate(
    config: SingleItemConfig,
    _lease: SingleItemLease,
    checkpoint: dict[str, Any],
) -> None:
    payload = validate_structured_json_artifact(
        config.root,
        config.job_id,
        config.structured_raw_path,
    )
    checkpoint.setdefault("structured_content", {}).update(
        {
            "status": "schema_validated",
            "validated_schema_version": payload["schema_version"],
            "validated_raw_sha256": structured_sha256_file(config.structured_raw_path),
            "updated_at": utc_now(),
        }
    )
    _save_checkpoint(config, checkpoint)


def _render_structured(
    config: SingleItemConfig,
    _lease: SingleItemLease,
    checkpoint: dict[str, Any],
) -> None:
    draft = render_structured_json_artifact(
        config.root,
        config.job_id,
        config.structured_raw_path,
        config.draft_path,
    )
    checkpoint.setdefault("structured_content", {}).update(
        {
            "status": "rendered",
            "draft_path": str(draft.path),
            "draft_sha256": structured_sha256_file(draft.path),
            "updated_at": utc_now(),
        }
    )
    _save_checkpoint(config, checkpoint)


def _analysis_hashes(config: SingleItemConfig) -> dict[str, dict[str, Any]]:
    paths = [config.job_dir / "source.mp4"] + sorted(
        path for path in (config.job_dir / "analysis").rglob("*") if path.is_file()
    )
    return {
        path.relative_to(config.job_dir).as_posix(): {
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in paths
    }


def _registry_snapshot(config: SingleItemConfig) -> list[dict[str, Any]]:
    with _connect(config) as connection:
        rows = connection.execute(
            "SELECT job_id, source_id, status, last_position, currently_collected, "
            "COALESCE(error, '') AS error, COALESCE(library_path, '') AS library_path "
            "FROM collection_items ORDER BY job_id"
        ).fetchall()
    return [dict(row) for row in rows]


def _vault_digest(config: SingleItemConfig) -> str:
    digest = hashlib.sha256()
    roots = (
        config.vault / "40-Resources" / "抖音收藏",
        config.vault / "99-Attachments" / "抖音收藏",
    )
    for base in roots:
        if not base.exists():
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            digest.update(path.relative_to(config.vault).as_posix().encode("utf-8"))
            digest.update(str(path.stat().st_size).encode("ascii"))
            digest.update(sha256_file(path).encode("ascii"))
    return digest.hexdigest()


def _staging_baseline(config: SingleItemConfig) -> dict[str, Any]:
    return {
        "recorded_at": utc_now(),
        "analysis": _analysis_hashes(config),
        "registry": _registry_snapshot(config),
        "vault_digest": _vault_digest(config),
    }


def _structured_staging_acceptance(
    config: SingleItemConfig,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    baseline = checkpoint.get("structured_staging_baseline")
    if not isinstance(baseline, dict):
        raise SingleItemError("structured_staging_baseline_missing", "结构化暂存基线缺失")
    payload, draft = validate_structured_artifacts(
        config.root,
        config.job_id,
        config.structured_raw_path,
        config.draft_path,
    )
    del payload
    after_registry = _registry_snapshot(config)
    before_registry = baseline.get("registry")
    before_other = [row for row in before_registry if row.get("job_id") != config.job_id]
    after_other = [row for row in after_registry if row.get("job_id") != config.job_id]
    target = next((row for row in after_registry if row.get("job_id") == config.job_id), None)
    with _connect(config) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        publication = connection.execute(
            "SELECT COUNT(*) FROM obsidian_publications "
            "JOIN collection_items USING(source_id) WHERE job_id = ?",
            (config.job_id,),
        ).fetchone()[0]
    record = checkpoint.get("structured_content") or {}
    elapsed = float(record.get("elapsed_seconds") or 0)
    checks = {
        "within_10_minutes": elapsed <= 600,
        "runner_call_limit": int(record.get("runner_calls") or 0) <= 2,
        "retry_limit": int(record.get("retry_count") or 0) <= 1,
        "schema_valid": True,
        "quality_valid": bool(draft.body and len(draft.body) >= 600),
        "privacy_valid": True,
        "analysis_hashes_unchanged": baseline.get("analysis") == _analysis_hashes(config),
        "other_items_unchanged": before_other == after_other,
        "target_remains_analyzed": bool(target and target.get("status") == "analyzed"),
        "sqlite_integrity": integrity == "ok",
        "vault_unchanged": baseline.get("vault_digest") == _vault_digest(config),
        "not_published": int(publication) == 0,
        "next_item_called": False,
    }
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "job_id": config.job_id,
        "aweme_id": config.aweme_id,
        "original_position": config.position,
        "current_position": int(_bound_item(config)["last_position"]),
        "accepted_at": utc_now(),
        "elapsed_seconds": elapsed,
        "runner_calls": int(record.get("runner_calls") or 0),
        "retry_count": int(record.get("retry_count") or 0),
        "raw_json": {
            "path": str(config.structured_raw_path),
            "sha256": structured_sha256_file(config.structured_raw_path),
        },
        "rendered_markdown": {
            "path": str(config.draft_path),
            "sha256": structured_sha256_file(config.draft_path),
        },
        "schema": {
            "path": str(record.get("schema_path") or ""),
            "sha256": str(record.get("schema_sha256") or ""),
        },
        "manifest": str(config.structured_manifest_path),
        "checks": checks,
        "analysis_hashes": _analysis_hashes(config),
        "registry": {
            "target_status": target.get("status") if target else None,
            "target_error": target.get("error") if target else None,
            "item_count": len(after_registry),
        },
    }
    if report["status"] != "passed":
        raise SingleItemError(
            "structured_staging_acceptance_failed", "结构化暂存验收失败"
        )
    return report


def _staging_accept(
    config: SingleItemConfig,
    _lease: SingleItemLease,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    report = _structured_staging_acceptance(config, checkpoint)
    checkpoint["structured_staging_acceptance"] = report
    checkpoint.setdefault("structured_content", {}).update(
        {"status": "staging_accepted", "updated_at": utc_now()}
    )
    _save_checkpoint(config, checkpoint)
    return report


def _finish_structured_staging(
    config: SingleItemConfig,
    checkpoint: dict[str, Any],
) -> None:
    report = checkpoint.get("structured_staging_acceptance")
    if not isinstance(report, dict) or report.get("status") != "passed":
        raise SingleItemError(
            "structured_staging_acceptance_missing", "结构化暂存检查点缺失"
        )
    checkpoint.update(
        {
            "status": "structured_content_staged",
            "current_stage": None,
            "staged_at": utc_now(),
            "error": None,
        }
    )
    _save_checkpoint(config, checkpoint)
    _update_task_state(
        config,
        status="paused",
        phase="single_item_staging_accept",
        fixed_job_id=config.job_id,
        fixed_aweme_id=config.aweme_id,
        original_position=config.position,
        current_position=int(_bound_item(config)["last_position"]),
        next_action="stop after structured staging; do not publish or select another item",
        blocker="structured_content_staged_not_published",
    )
    append_line(
        config.task_dir / "run.log",
        f"{utc_now()} 稳定作品 {config.job_id} 结构化内容暂存验收通过；"
        f"runner_calls={report['runner_calls']}，elapsed={report['elapsed_seconds']}s；"
        "未发布、未调用 next_item。",
    )


def _publication_preflight(config: SingleItemConfig) -> None:
    draft = validate_content_draft(config.root, config.job_id, config.draft_path)
    timeline = config.job_dir / "analysis" / "timeline.md"
    manifest_path = config.job_dir / "analysis" / "manifest.json"
    if not timeline.is_file() or not timeline.read_text(encoding="utf-8").strip():
        raise SingleItemError("single_item_timeline_missing", "发布前时间轴缺失")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SingleItemError("single_item_manifest_invalid", "发布前 manifest 无效") from exc
    items = (manifest.get("keyframes") or {}).get("items") or []
    frames = [
        config.job_dir / "analysis" / str(item.get("file"))
        for item in items
        if isinstance(item, dict) and item.get("file")
    ]
    if sum(path.is_file() for path in frames) < 3:
        raise SingleItemError("single_item_frames_missing", "发布前关键帧不足")
    if not config.vault.is_dir() or not (config.vault / ".obsidian").is_dir():
        raise SingleItemError("single_item_vault_invalid", "发布前 Vault 无效")
    timeline_link = "99-Attachments/抖音收藏/fixture/完整时间轴"
    rendered = _generated_body(
        draft.body,
        frame_links=["99-Attachments/抖音收藏/fixture/frame.jpg"],
        timeline_link=timeline_link,
    )
    if rendered.count(f"[[{timeline_link}|完整时间轴]]") != 1 or "## 时间轴" not in rendered:
        raise SingleItemError("single_item_timeline_contract_invalid", "时间轴链接契约不成立")


def _backup_record_valid(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    path = Path(str(record.get("path") or ""))
    if not path.is_file() or record.get("sha256") != sha256_file(path):
        return False
    try:
        with sqlite3.connect(path) as connection:
            return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def _ensure_backup(config: SingleItemConfig, checkpoint: dict[str, Any]) -> Path:
    existing = checkpoint.get("backup")
    if isinstance(existing, dict):
        path = Path(str(existing.get("path") or ""))
        if _backup_record_valid(existing):
            return path
        raise SingleItemError("single_item_backup_invalid", "既有发布前备份无效")
    path = config.task_dir / "artifacts" / "single-item-prepublish-knowledge.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            with sqlite3.connect(path) as connection:
                valid = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except sqlite3.Error:
            valid = False
        if not valid:
            raise SingleItemError("single_item_backup_untracked", "未登记的发布前备份无效")
        checkpoint["backup"] = {
            "path": str(path),
            "sha256": sha256_file(path),
            "created_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            "recovered_from_interruption": True,
        }
        _save_checkpoint(config, checkpoint)
        return path
    source = sqlite3.connect(config.root / "data" / "knowledge.db", timeout=30)
    destination = sqlite3.connect(path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    with sqlite3.connect(path) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            path.unlink(missing_ok=True)
            raise SingleItemError("single_item_backup_failed", "发布前 SQLite 备份损坏")
    checkpoint["backup"] = {
        "path": str(path),
        "sha256": sha256_file(path),
        "created_at": utc_now(),
    }
    _save_checkpoint(config, checkpoint)
    return path


def _publish(config: SingleItemConfig, _: SingleItemLease, checkpoint: dict[str, Any]) -> None:
    _publication_preflight(config)
    _ensure_backup(config, checkpoint)
    publish_job(
        config.root,
        job_id=config.job_id,
        category=config.category,
        title=None,
        tags=list(config.tags),
        vault=config.vault,
        content_draft=config.draft_path,
        quality_mode="high-quality",
    )
    if not _publish_valid(config):
        raise SingleItemError("single_item_publish_incomplete", "发布后状态不完整")


def _accept(
    config: SingleItemConfig, _: SingleItemLease, checkpoint: dict[str, Any]
) -> dict[str, Any]:
    row = _bound_item(config)
    if not row["library_path"]:
        raise SingleItemError("single_item_library_missing", "完成条目缺少 Library 路径")
    library = Path(row["library_path"])
    if not library.is_absolute():
        library = config.root / library
    library_note = library / "内容整理.md"
    source = config.job_dir / "source.mp4"
    with _connect(config) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        publication = connection.execute(
            "SELECT note_path, attachment_path FROM obsidian_publications "
            "JOIN collection_items USING(source_id) WHERE job_id = ?",
            (config.job_id,),
        ).fetchone()
    note = config.vault / publication["note_path"] if publication else Path()
    attachments = config.vault / publication["attachment_path"] if publication else Path()
    note_text = note.read_text(encoding="utf-8") if note.is_file() else ""
    library_text = library_note.read_text(encoding="utf-8") if library_note.is_file() else ""
    timeline_markup = (
        f"[[{Path(publication['attachment_path']).as_posix()}/完整时间轴|完整时间轴]]"
        if publication
        else ""
    )
    frame_count = (
        sum(path.suffix.lower() in {".jpg", ".jpeg", ".png"} for path in attachments.glob("*"))
        if attachments.is_dir()
        else 0
    )
    current_position = int(row["last_position"])
    _observe_position(config, checkpoint, current_position, source="acceptance")
    checks = {
        "binding": (
            row["job_id"] == config.job_id
            and (not config.aweme_id or row["source_id"] == config.aweme_id)
            and bool(row["currently_collected"])
        ),
        "registry": row["status"] == "completed",
        "sqlite_integrity": integrity == "ok",
        "source_hash": source.is_file() and sha256_file(source) == row["media_sha256"],
        "library_note": library_note.is_file(),
        "vault_note": note.is_file(),
        "timeline_file": (attachments / "完整时间轴.md").is_file(),
        "timeline_link_once": bool(timeline_markup) and note_text.count(timeline_markup) == 1,
        "keyframes": 3 <= frame_count <= 8,
        "vault_has_no_mp4": not any(attachments.glob("*.mp4")) if attachments.is_dir() else False,
        "privacy": not any(
            pattern.search(library_text + "\n" + note_text) for pattern in SENSITIVE
        ),
        "backup_once": _backup_record_valid(checkpoint.get("backup")),
    }
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "job_id": config.job_id,
        "aweme_id": config.aweme_id,
        "position": config.position,
        "original_position": config.position,
        "current_position": current_position,
        "position_drift_history": checkpoint.get("position_drift_history", []),
        "accepted_at": utc_now(),
        "checks": checks,
        "backup": checkpoint.get("backup"),
    }
    if report["status"] != "passed":
        raise SingleItemError("single_item_acceptance_failed", "单条增量验收失败")
    return report


DEFAULT_OPERATIONS: Mapping[
    str, Callable[[SingleItemConfig, SingleItemLease, dict[str, Any]], Any]
] = {
    "download": _download,
    "analysis": _analysis,
    "structured_generate": _structured_generate,
    "schema_validate": _schema_validate,
    "render": _render_structured,
    "staging_accept": _staging_accept,
    "publish": _publish,
    "accept": _accept,
}
DEFAULT_VALIDATORS: Mapping[str, Callable[[SingleItemConfig], bool]] = {
    "download": _download_valid,
    "analysis": _analysis_valid,
    "structured_generate": _structured_generate_valid,
    "schema_validate": _schema_validate_valid,
    "render": _render_valid,
    "staging_accept": _staging_accept_valid,
    "publish": _publish_valid,
    "accept": _accept_valid,
}


def run_single_item(
    config: SingleItemConfig,
    *,
    operations: Mapping[
        str, Callable[[SingleItemConfig, SingleItemLease, dict[str, Any]], Any]
    ] = DEFAULT_OPERATIONS,
    validators: Mapping[str, Callable[[SingleItemConfig], bool]] = DEFAULT_VALIDATORS,
) -> int:
    try:
        row = _bound_item(config)
        with _connect(config) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SingleItemError("single_item_sqlite_invalid", "SQLite 完整性失败")
        if row["status"] == "completed" and not config.acceptance_path.is_file():
            try:
                resumable = json.loads(config.checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                resumable = {}
            same_item = (
                resumable.get("job_id") == config.job_id
                and resumable.get("position") == config.position
                and _backup_record_valid(resumable.get("backup"))
            )
            if not same_item:
                raise SingleItemError(
                    "single_item_already_completed", "固定条目已完成且不属于本任务"
                )
        if config.dry_run:
            resolve_handoff_path(config.root, config.download_handoff_path)
            print(
                json.dumps(
                    {
                        "status": "dry_run_ok",
                        "job_id": config.job_id,
                        "aweme_id": config.aweme_id,
                        "position": config.position,
                        "original_position": config.position,
                        "current_position": int(row["last_position"]),
                        "registry_status": row["status"],
                        "stages": list(STAGES),
                        "will_select_next": False,
                    },
                    ensure_ascii=False,
                )
            )
            return SUCCESS
    except (OSError, sqlite3.Error, GateError, SingleItemError) as exc:
        print(
            json.dumps(
                {"status": "preflight_failed", "code": getattr(exc, "code", type(exc).__name__)}
            )
        )
        return PREFLIGHT_FAILURE

    lease = SingleItemLease(config)
    try:
        lease.acquire()
        checkpoint = _checkpoint(config)
        _observe_position(
            config,
            checkpoint,
            int(row["last_position"]),
            source="registry_preflight",
        )
        _save_checkpoint(config, checkpoint)
    except (OSError, json.JSONDecodeError, SingleItemError) as exc:
        print(
            json.dumps(
                {"status": "preflight_failed", "code": getattr(exc, "code", type(exc).__name__)}
            )
        )
        return PREFLIGHT_FAILURE

    try:
        _update_task_state(
            config,
            status="running",
            phase="single_item_driver",
            fixed_job_id=config.job_id,
            fixed_aweme_id=config.aweme_id,
            original_position=config.position,
            current_position=int(row["last_position"]),
            next_action="resume fixed item from validated checkpoint",
            blocker=None,
        )
        for stage in STAGES:
            _bound_item(config)
            if (
                stage == "structured_generate"
                and checkpoint["stages"].get(stage, {}).get("status") != "completed"
                and not isinstance(checkpoint.get("structured_staging_baseline"), dict)
            ):
                checkpoint["structured_staging_baseline"] = _staging_baseline(config)
                _save_checkpoint(config, checkpoint)
            if validators[stage](config):
                if checkpoint["stages"].get(stage, {}).get("status") != "completed":
                    _record_reused(config, checkpoint, stage)
                if stage == "staging_accept" and config.stop_after_structured_content:
                    _finish_structured_staging(config, checkpoint)
                    return SUCCESS
                continue
            result = _timed(
                config,
                lease,
                checkpoint,
                stage,
                lambda stage=stage: operations[stage](config, lease, checkpoint),
            )
            if stage == "staging_accept" and config.stop_after_structured_content:
                _finish_structured_staging(config, checkpoint)
                return SUCCESS
            if stage == "accept" and isinstance(result, dict):
                result["timings"] = checkpoint["timings"]
                atomic_json(config.acceptance_path, result)
        checkpoint.update(
            {"status": "completed", "current_stage": None, "completed_at": utc_now(), "error": None}
        )
        _save_checkpoint(config, checkpoint)
        _update_task_state(
            config,
            status="sample_completed",
            phase="single_item_completed",
            fixed_job_id=config.job_id,
            fixed_aweme_id=config.aweme_id,
            original_position=config.position,
            current_position=int(_bound_item(config)["last_position"]),
            final_acceptance=str(config.acceptance_path),
            next_action="stop; do not select another item",
            blocker=None,
        )
        append_line(
            config.task_dir / "run.log",
            f"{utc_now()} 稳定作品 {config.job_id} 单条 driver 完成并停止；"
            f"初始位置 {config.position}，当前位置 {int(_bound_item(config)['last_position'])}；"
            "未调用 next_item。",
        )
        return SUCCESS
    except Exception as exc:
        stage = str(checkpoint.get("current_stage") or "unknown")
        code = str(getattr(exc, "code", type(exc).__name__))
        if stage in {
            "structured_generate",
            "schema_validate",
            "render",
            "staging_accept",
            "publish",
            "accept",
        }:
            update_item_by_job(
                config.root / "data" / "knowledge.db",
                config.job_id,
                status="analyzed",
                error=code,
                preserve_completed=False,
            )
        checkpoint.update({"status": "paused", "error": code, "paused_at": utc_now()})
        _save_checkpoint(config, checkpoint)
        _update_task_state(
            config,
            status="paused",
            phase=f"single_item_{stage}",
            fixed_job_id=config.job_id,
            fixed_aweme_id=config.aweme_id,
            original_position=config.position,
            current_position=checkpoint.get("current_position"),
            next_action="restart the same fixed item driver to resume checkpoint",
            blocker=code,
        )
        append_line(
            config.task_dir / "run.log",
            f"{utc_now()} 稳定作品 {config.job_id} 在 {stage} 受控暂停：{code}；"
            f"初始位置 {config.position}，当前位置 {checkpoint.get('current_position')}；"
            "未选择后续条目。",
        )
        return (
            CONTROLLED_FAILURE
            if isinstance(exc, (SingleItemError, ContentStageError, StructuredContentError))
            else INTERNAL_FAILURE
        )
    finally:
        lease.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run exactly one fixed resumable collection item")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--aweme-id", required=True)
    parser.add_argument("--position", type=int, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--primary-runner", type=Path, required=True)
    parser.add_argument("--fallback-runner", type=Path, required=True)
    parser.add_argument("--structured-runner", type=Path, required=True)
    parser.add_argument("--category", default="人工智能与数字工具")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--asr-model", default="small")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--stop-after-structured-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = SingleItemConfig(
        root=args.root.resolve(),
        task_dir=args.task_dir.resolve(),
        job_id=args.job_id,
        aweme_id=args.aweme_id,
        position=args.position,
        vault=args.vault.resolve(),
        primary_runner=args.primary_runner.resolve(),
        fallback_runner=args.fallback_runner.resolve(),
        structured_runner=args.structured_runner.resolve(),
        category=args.category,
        tags=tuple(args.tag) or ("AI", "知识管理", "待复核"),
        asr_model=args.asr_model,
        device=args.device,
        compute_type=args.compute_type,
        stop_after_structured_content=args.stop_after_structured_content,
        dry_run=args.dry_run,
    )
    return run_single_item(config)


if __name__ == "__main__":
    raise SystemExit(main())
