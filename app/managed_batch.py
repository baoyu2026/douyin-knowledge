from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import yaml

from app.collection_registry import PIPELINE_VERSION, CollectionRegistry, RegistryItem

SUCCESS = 0
PREFLIGHT_FAILURE = 2
CONTROLLED_FAILURE = 4
INTERNAL_FAILURE = 5
HEARTBEAT_SECONDS = 15
MAX_ITEM_ATTEMPTS = 2
REQUIRED_SECTIONS = (
    "## 一句话总结",
    "## 内容摘要",
    "## 核心观点",
    "## 论证结构",
    "## 案例数据",
    "## 时间轴",
    "## 可复用知识",
    "## 行动建议",
    "## 关键词",
    "## 相关内容",
)
SENSITIVE_PATTERNS = (
    (re.compile(r"https?://\S+", re.I), "[REDACTED_URL]"),
    (
        re.compile(r"(?i)\b(cookie|session|signature|token)\b\s*[=:]\s*\S+"),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"\baweme-[a-f0-9]{20}\b", re.I), "[REDACTED_JOB]"),
    (re.compile(r"\b\d{15,24}\b"), "[REDACTED_ID]"),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sanitize(value: str) -> str:
    result = value
    for pattern, replacement in SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def append_line(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(sanitize(value.rstrip()) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True)
class BatchConfig:
    root: Path
    task_dir: Path
    completed_before_run: int = 2
    target_completed_this_run: int = 5
    category: str = "人工智能与数字工具"
    tags: tuple[str, ...] = ("AI", "知识管理", "待复核")
    vault: Path | None = None
    content_runner_config: Path | None = None
    dry_run: bool = False


class OwnerLease:
    def __init__(self, task_dir: Path) -> None:
        self.lock = task_dir / "managed-driver.lock"
        self.owner = task_dir / "managed-driver-owner.json"
        self.state = task_dir / "state.json"
        self.lease_id = uuid.uuid4().hex
        self.started_at = utc_now()
        self.last_progress_at = self.started_at
        self.stage = "starting"

    def acquire(self) -> None:
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            payload = {
                "lease_id": self.lease_id,
                "pid": os.getpid(),
                "started_at": self.started_at,
            }
            os.write(descriptor, (json.dumps(payload) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            self.heartbeat()
        except Exception:
            self.release()
            raise

    def heartbeat(self, stage: str | None = None, *, progress: bool = False) -> None:
        if stage is not None:
            self.stage = stage
        if progress:
            self.last_progress_at = utc_now()
        atomic_json(
            self.owner,
            {
                "lease_id": self.lease_id,
                "pid": os.getpid(),
                "started_at": self.started_at,
                "heartbeat_at": utc_now(),
                "last_progress_at": self.last_progress_at,
                "stage": self.stage,
            },
        )
        self._update_task_heartbeat()

    def _update_task_heartbeat(self) -> None:
        try:
            state = json.loads(self.state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        owner = state.get("controlled_owner")
        if (
            state.get("status") != "running"
            or not isinstance(owner, dict)
            or owner.get("lease_id") != self.lease_id
        ):
            return
        state["heartbeat"] = utc_now()
        state["last_progress_at"] = self.last_progress_at
        atomic_json(self.state, state)

    def _owns(self, path: Path) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return payload.get("lease_id") == self.lease_id

    def release(self) -> None:
        if self._owns(self.owner):
            self.owner.unlink(missing_ok=True)
        if self._owns(self.lock):
            self.lock.unlink(missing_ok=True)


def _connect(config: BatchConfig) -> sqlite3.Connection:
    connection = sqlite3.connect(config.root / "data" / "knowledge.db", timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    return dict(
        connection.execute(
            "select status, count(*) from collection_items "
            "where currently_collected = 1 group by status"
        ).fetchall()
    )


def _row_to_item(row: sqlite3.Row) -> RegistryItem:
    values = {key: row[key] for key in RegistryItem.__dataclass_fields__}
    values["currently_collected"] = bool(values["currently_collected"])
    return RegistryItem(**values)


def _job_dir(config: BatchConfig, item: RegistryItem) -> Path:
    if item.job_path:
        value = Path(item.job_path)
        return value if value.is_absolute() else config.root / value
    return config.root / "data" / "jobs" / item.job_id


def preflight(config: BatchConfig) -> tuple[str, dict[str, int], list[RegistryItem]]:
    if (config.task_dir / "managed-driver.lock").exists() or (
        config.task_dir / "managed-driver-owner.json"
    ).exists():
        raise RuntimeError("owner_exists")
    state = json.loads((config.task_dir / "state.json").read_text(encoding="utf-8"))
    if state.get("status") not in {"owner_released", "managed_driver_ready"}:
        raise RuntimeError("task_not_ready_for_driver")
    with _connect(config) as connection:
        if connection.execute("pragma integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("sqlite_integrity_failed")
        snapshot = connection.execute(
            "select snapshot_id, item_count, state from collection_snapshots "
            "order by completed_at desc limit 1"
        ).fetchone()
        if snapshot is None or snapshot["state"] != "completed" or snapshot["item_count"] <= 0:
            raise RuntimeError("complete_snapshot_missing")
        counts = _counts(connection)
        downloaded_rows = connection.execute(
            "select * from collection_items "
            "where status = 'downloaded' and currently_collected = 1"
        ).fetchall()
    downloaded = []
    for row in downloaded_rows:
        item = _row_to_item(row)
        media = _job_dir(config, item) / "source.mp4"
        if not media.is_file() or media.stat().st_size <= 0:
            raise RuntimeError("downloaded_media_missing")
        if item.media_sha256 and _sha256(media) != item.media_sha256:
            raise RuntimeError("downloaded_media_hash_mismatch")
        downloaded.append(item)
    completed_this_run = counts.get("completed", 0) - config.completed_before_run
    if completed_this_run < 0 or completed_this_run > config.target_completed_this_run:
        raise RuntimeError("completion_baseline_mismatch")
    return snapshot["snapshot_id"], counts, downloaded


def high_quality_launch_blocker(config: BatchConfig) -> str | None:
    """Block launch until the strict content stage is wired end to end."""
    process_one = config.root / "scripts" / "process-one.ps1"
    publisher = config.root / "app" / "publish_library.py"
    content_stage = config.root / "app" / "content_stage.py"
    runner_config = config.content_runner_config or config.root / "config" / "content-runner.yml"
    try:
        process_text = process_one.read_text(encoding="utf-8-sig")
        publisher_text = publisher.read_text(encoding="utf-8")
        stage_text = content_stage.read_text(encoding="utf-8")
        runner = yaml.safe_load(runner_config.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return "high_quality_content_stage_missing"
    process_has_draft = all(
        value in process_text
        for value in ("ContentDraft", "ContentRunnerConfig", "app.content_stage", "--content-draft")
    )
    publisher_has_draft = all(
        value in publisher_text
        for value in ("--content-draft", "validate_content_draft", "high-quality")
    )
    validator_fields = (
        "proper_noun_review",
        "numeric_review",
        "related_knowledge",
        "visual_evidence",
    )
    validator_present = all(value in stage_text for value in validator_fields)
    runner_safe = (
        isinstance(runner, dict)
        and runner.get("version") == 1
        and "read-only" in (runner.get("arguments") or [])
        and "--ephemeral" in (runner.get("arguments") or [])
    )
    if not all((process_has_draft, publisher_has_draft, validator_present, runner_safe)):
        return "curated_content_input_missing"
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pump(stream: TextIO, target: Path, prefix: str) -> None:
    for line in stream:
        append_line(target, f"{utc_now()} {prefix} {line}")


def run_command(command: list[str], config: BatchConfig, lease: OwnerLease, stage: str) -> int:
    stdout_log = config.task_dir / "artifacts" / "managed-stdout.log"
    stderr_log = config.task_dir / "artifacts" / "managed-stderr.log"
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["OMP_NUM_THREADS"] = "2"
    process = subprocess.Popen(
        command,
        cwd=config.root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(
            target=_pump, args=(process.stdout, stdout_log, "OUT"), daemon=True
        ),
        threading.Thread(
            target=_pump, args=(process.stderr, stderr_log, "ERR"), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()
    try:
        lease.heartbeat(stage)
        while True:
            try:
                return int(process.wait(timeout=HEARTBEAT_SECONDS))
            except subprocess.TimeoutExpired:
                lease.heartbeat(stage)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=5)


def _completed_ids(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "select job_id from collection_items "
            "where status = 'completed' and currently_collected = 1"
        )
    }


def _invoke_item(config: BatchConfig, lease: OwnerLease, item: RegistryItem) -> int:
    job_dir = _job_dir(config, item)
    transcript = job_dir / "precomputed-transcript.json"
    chunks = job_dir / "asr-chunks"
    if chunks.is_dir() and not transcript.is_file():
        code = run_command(
            [
                sys.executable,
                "-m",
                "app.chunked_transcript",
                "--root",
                str(config.root),
                "--job-id",
                item.job_id,
            ],
            config,
            lease,
            "resuming_chunked_asr",
        )
        if code != 0:
            return code
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(config.root / "scripts" / "process-one.ps1"),
        "-Position",
        str(item.last_position),
        "-Category",
        config.category,
        "-Tag",
        config.tags[0],
    ]
    if config.vault is not None:
        command.extend(["-Vault", str(config.vault)])
    if transcript.is_file():
        command.extend(["-TranscriptOverride", str(transcript)])
    source = job_dir / "source.mp4"
    if source.is_file():
        command.extend(["-JobId", item.job_id])
    runner_config = config.content_runner_config or config.root / "config" / "content-runner.yml"
    draft = config.root / "orchestration" / "content-drafts" / f"{item.job_id}-content.md"
    if draft.is_file():
        command.extend(["-ContentDraft", str(draft)])
    else:
        command.extend(["-ContentRunnerConfig", str(runner_config)])
    return run_command(command, config, lease, "processing_item")


def _read_metadata(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    marker = text.find("\n---\n", 4)
    payload = yaml.safe_load(text[4:marker]) if marker >= 0 else {}
    return payload if isinstance(payload, dict) else {}


def acceptance_records(config: BatchConfig, job_ids: set[str]) -> list[dict[str, object]]:
    records = []
    with _connect(config) as connection:
        for job_id in sorted(job_ids):
            row = connection.execute(
                "select item.*, publication.note_path from collection_items item "
                "left join obsidian_publications publication using(source_id) "
                "where item.job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                continue
            item = _row_to_item(row)
            job_dir = _job_dir(config, item)
            source = job_dir / "source.mp4"
            library = Path(item.library_path or "")
            if library and not library.is_absolute():
                library = config.root / library
            note = library / "内容整理.md"
            metadata = _read_metadata(note) if note.is_file() else {}
            text = note.read_text(encoding="utf-8") if note.is_file() else ""
            vault_note = (
                config.vault / row["note_path"]
                if config.vault and row["note_path"]
                else None
            )
            manifest = job_dir / "analysis" / "manifest.json"
            manifest_valid = False
            if manifest.is_file() and source.is_file():
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                manifest_valid = payload.get("source", {}).get("sha256") == _sha256(source)
            vault_text = (
                vault_note.read_text(encoding="utf-8")
                if vault_note is not None and vault_note.is_file()
                else ""
            )
            privacy_text = text + "\n" + vault_text
            privacy_ok = not any(
                pattern.search(privacy_text)
                for pattern in (
                    re.compile(r"(?i)\b(cookie|signature|request[_ -]?url)\b"),
                    re.compile(r"\baweme-[a-f0-9]{20}\b", re.I),
                )
            )
            records.append(
                {
                    "title": str(metadata.get("标题") or "未命名"),
                    "category": str(metadata.get("主分类") or "待复核"),
                    "review": str(metadata.get("复核状态") or "待人工复核"),
                    "acceptance": {
                        "source_hash": source.is_file()
                        and _sha256(source) == item.media_sha256,
                        "manifest": manifest_valid,
                        "structured_note": all(section in text for section in REQUIRED_SECTIONS),
                        "high_quality": metadata.get("质量模式") == "高质量",
                        "real_classification": bool(
                            metadata.get("主分类")
                            and metadata.get("主分类") != "人工智能与数字工具"
                            and isinstance(metadata.get("标签"), list)
                            and len(metadata["标签"]) >= 2
                            and "待复核" not in metadata["标签"]
                        ),
                        "privacy": privacy_ok,
                        "library": note.is_file(),
                        "obsidian": bool(vault_note and vault_note.is_file()),
                        "registry": item.status == "completed",
                    },
                }
            )
    return records


def _write_final_report(
    config: BatchConfig,
    *,
    records: list[dict[str, object]],
    counts: dict[str, int],
    exit_code: int,
) -> None:
    now = utc_now()
    completed_this_run = counts.get("completed", 0) - config.completed_before_run
    should_process = sum(
        count
        for status, count in counts.items()
        if status
        in {"new", "failed", "incomplete", "downloaded", "analyzed", "processing"}
    )
    lines = [
        "# 受管批处理驱动",
        "",
        f"- 退出状态：{exit_code}",
        f"- 本轮完成：{completed_this_run}/{config.target_completed_this_run}",
        f"- remaining should_process：{should_process}",
        "- owner lock：已清理",
        "",
        f"## 本次新增 {len(records)} 条",
        "",
    ]
    for record in records:
        accepted = all(record["acceptance"].values())
        lines.append(
            f"- {record['title']}｜{record['category']}｜"
            f"四层验收：{'通过' if accepted else '待复核'}｜复核：{record['review']}"
        )
    lines.extend(
        ["", "## 失败与待复核", "", "- 驱动失败：无", "- 内容复核：见各条复核状态", ""]
    )
    report_path = config.task_dir / "artifacts" / "managed-driver.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "\n".join(lines), encoding="utf-8"
    )
    state_path = config.task_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "batch_ready_for_report",
            "phase": "managed_batch_complete",
            "last_progress_at": now,
            "completed_this_run": completed_this_run,
            "completed_since_last_report": completed_this_run,
            "last_completed_titles": [record["title"] for record in records],
            "registry_counts": counts,
            "controlled_owner": None,
            "heartbeat": None,
            "driver_exit_code": exit_code,
            "failed_items": [],
            "next_action": "report completed batch and stop",
            "blocker": None,
        }
    )
    atomic_json(state_path, state)
    (config.task_dir / "summary.md").write_text(
        "# 阶段总结\n\n"
        "- 当前状态：`batch_ready_for_report`\n"
        f"- 本轮完成：{completed_this_run}/{config.target_completed_this_run}\n"
        f"- 新增条目：{'；'.join(str(record['title']) for record in records)}\n"
        f"- Registry：completed {counts.get('completed', 0)}、"
        f"downloaded {counts.get('downloaded', 0)}、new {counts.get('new', 0)}\n"
        f"- remaining should_process：{should_process}\n"
        "- 驱动已按累计目标退出，未领取下一条；owner PID/lock/heartbeat 已清理。\n",
        encoding="utf-8",
    )
    append_line(
        config.task_dir / "run.log",
        f"{now}：唯一受管驱动达到本轮 {completed_this_run}/"
        f"{config.target_completed_this_run}，已退出且未领取下一条；owner lock 已清理。",
    )
    atomic_json(
        config.task_dir / "artifacts" / "managed-driver-exit.json",
        {
            "exit_code": exit_code,
            "exited_at": now,
            "completed_this_run": completed_this_run,
        },
    )


def _write_paused_state(config: BatchConfig, *, exit_code: int, blocker: str) -> None:
    state_path = config.task_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    with _connect(config) as connection:
        counts = _counts(connection)
    state.update(
        {
            "status": "paused",
            "phase": "managed_driver_preflight",
            "last_progress_at": utc_now(),
            "completed_this_run": counts.get("completed", 0)
            - config.completed_before_run,
            "registry_counts": counts,
            "controlled_owner": None,
            "heartbeat": None,
            "driver_exit_code": exit_code,
            "next_action": "add curated content generation and validation before launch",
            "blocker": blocker,
        }
    )
    atomic_json(state_path, state)


def run_managed_batch(
    config: BatchConfig,
    invoke: Callable[[BatchConfig, OwnerLease, RegistryItem], int] = _invoke_item,
    quality_check: Callable[[BatchConfig], str | None] = high_quality_launch_blocker,
) -> int:
    try:
        snapshot_id, initial_counts, _downloaded = preflight(config)
    except Exception as exc:
        if not config.dry_run:
            append_line(
                config.task_dir / "run.log",
                f"{utc_now()}：受管驱动预检暂停：{type(exc).__name__}",
            )
        return PREFLIGHT_FAILURE
    initial_completed = initial_counts.get("completed", 0) - config.completed_before_run
    blocker = quality_check(config)
    if blocker is not None:
        if config.dry_run:
            print(json.dumps({"status": "dry_run_blocked", "blocker": blocker}))
        else:
            _write_paused_state(config, exit_code=PREFLIGHT_FAILURE, blocker=blocker)
        return PREFLIGHT_FAILURE
    if config.dry_run:
        registry = CollectionRegistry(
            config.root / "data" / "knowledge.db", root=config.root
        )
        next_item = registry.next_item(snapshot_id, pipeline_version=PIPELINE_VERSION)
        print(
            json.dumps(
                {
                    "status": "dry_run_ok",
                    "completed_this_run": initial_completed,
                    "has_next": next_item is not None,
                }
            )
        )
        return SUCCESS

    lease = OwnerLease(config.task_dir)
    try:
        lease.acquire()
    except FileExistsError:
        return PREFLIGHT_FAILURE
    exit_code = INTERNAL_FAILURE
    try:
        with _connect(config) as connection:
            initial_ids = _completed_ids(connection)
        state = json.loads((config.task_dir / "state.json").read_text(encoding="utf-8"))
        state.update(
            {
                "status": "running",
                "phase": "managed_batch",
                "controlled_owner": {
                    "lease_id": lease.lease_id,
                    "pid": os.getpid(),
                    "started_at": lease.started_at,
                },
                "heartbeat": utc_now(),
                "driver_exit_code": None,
            }
        )
        atomic_json(config.task_dir / "state.json", state)
        registry = CollectionRegistry(
            config.root / "data" / "knowledge.db", root=config.root
        )
        attempts: dict[int, int] = {}
        while True:
            with _connect(config) as connection:
                counts = _counts(connection)
            completed_this_run = counts.get("completed", 0) - config.completed_before_run
            if completed_this_run == config.target_completed_this_run:
                exit_code = SUCCESS
                break
            if completed_this_run > config.target_completed_this_run:
                exit_code = CONTROLLED_FAILURE
                break
            item = registry.next_item(snapshot_id, pipeline_version=PIPELINE_VERSION)
            if item is None:
                exit_code = CONTROLLED_FAILURE
                break
            position = item.last_position
            attempts[position] = attempts.get(position, 0) + 1
            if attempts[position] > MAX_ITEM_ATTEMPTS:
                exit_code = CONTROLLED_FAILURE
                break
            before = counts.get("completed", 0)
            code = invoke(config, lease, item)
            with _connect(config) as connection:
                after_counts = _counts(connection)
            after = after_counts.get("completed", 0)
            if code == 0 and after == before + 1:
                lease.heartbeat("item_completed", progress=True)
                append_line(
                    config.task_dir / "run.log",
                    f"{utc_now()}：受管驱动完成 1 条，本轮累计 "
                    f"{after - config.completed_before_run}/"
                    f"{config.target_completed_this_run}。",
                )
                continue
            append_line(
                config.task_dir / "run.log",
                f"{utc_now()}：单条处理受控失败，退出码 {code}，"
                f"尝试 {attempts[position]}/{MAX_ITEM_ATTEMPTS}。",
            )
        if exit_code == SUCCESS:
            with _connect(config) as connection:
                final_counts = _counts(connection)
                new_ids = _completed_ids(connection) - initial_ids
            records = acceptance_records(config, new_ids)
            expected_new = config.target_completed_this_run - initial_completed
            accepted = len(records) == expected_new and all(
                all(record["acceptance"].values()) for record in records
            )
            if not accepted:
                exit_code = CONTROLLED_FAILURE
            else:
                _write_final_report(
                    config, records=records, counts=final_counts, exit_code=exit_code
                )
    except Exception as exc:
        append_line(
            config.task_dir / "run.log",
            f"{utc_now()}：受管驱动内部暂停：{type(exc).__name__}",
        )
        exit_code = INTERNAL_FAILURE
    finally:
        lease.release()
        if exit_code != SUCCESS:
            _write_paused_state(
                config,
                exit_code=exit_code,
                blocker="managed_driver_controlled_failure",
            )
            atomic_json(
                config.task_dir / "artifacts" / "managed-driver-exit.json",
                {"exit_code": exit_code, "exited_at": utc_now()},
            )
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one bounded managed collection batch")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--completed-before-run", type=int, default=2)
    parser.add_argument("--target-completed-this-run", type=int, default=5)
    parser.add_argument("--category", default="人工智能与数字工具")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--content-runner-config", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = BatchConfig(
        root=args.root.resolve(),
        task_dir=args.task_dir.resolve(),
        completed_before_run=args.completed_before_run,
        target_completed_this_run=args.target_completed_this_run,
        category=args.category,
        tags=tuple(args.tag) or ("AI", "知识管理", "待复核"),
        vault=args.vault.resolve() if args.vault else None,
        content_runner_config=(
            args.content_runner_config.resolve() if args.content_runner_config else None
        ),
        dry_run=args.dry_run,
    )
    return run_managed_batch(config)


if __name__ == "__main__":
    raise SystemExit(main())
