from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.collection_registry import PIPELINE_VERSION, CollectionRegistry, update_item_by_job
from app.content_stage import ContentStageError
from app.single_item_driver import (
    CONTROLLED_FAILURE,
    PREFLIGHT_FAILURE,
    SUCCESS,
    SingleItemConfig,
    SingleItemError,
    _content,
    _download,
    _ensure_backup,
    _run_with_heartbeat,
    run_single_item,
)


def _fixture(tmp_path: Path, *, dry_run: bool = False) -> tuple[SingleItemConfig, str]:
    root = tmp_path / "root"
    task = tmp_path / "task"
    vault = tmp_path / "vault"
    task.mkdir(parents=True)
    (task / "state.json").write_text("{}\n", encoding="utf-8")
    (vault / ".obsidian").mkdir(parents=True)
    registry = CollectionRegistry(root / "data" / "knowledge.db", root=root)
    snapshot = registry.begin_snapshot(pipeline_version=PIPELINE_VERSION)
    registry.record_snapshot_page(
        snapshot,
        [
            {"source_id": "fixed-source", "aweme_id": "fixed-source"},
            {"source_id": "next-source", "aweme_id": "next-source"},
        ],
    )
    registry.complete_snapshot(snapshot, pipeline_version=PIPELINE_VERSION)
    fixed = registry.get("fixed-source")
    next_item = registry.get("next-source")
    assert fixed is not None and next_item is not None
    primary = root / "config" / "primary.yml"
    fallback = root / "config" / "fallback.yml"
    primary.parent.mkdir(parents=True)
    primary.write_text("version: 1\n", encoding="utf-8")
    fallback.write_text("version: 1\n", encoding="utf-8")
    return (
        SingleItemConfig(
            root=root,
            task_dir=task,
            job_id=fixed.job_id,
            aweme_id=fixed.source_id,
            position=1,
            vault=vault,
            primary_runner=primary,
            fallback_runner=fallback,
            dry_run=dry_run,
        ),
        next_item.job_id,
    )


def _marker_valid(stage: str):
    return lambda config: (config.task_dir / "artifacts" / f"{stage}.done").is_file()


def _mark(config: SingleItemConfig, stage: str) -> None:
    path = config.task_dir / "artifacts" / f"{stage}.done"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok\n", encoding="utf-8")


def test_fixed_driver_resumes_checkpoint_and_never_selects_next(tmp_path: Path) -> None:
    config, next_job_id = _fixture(tmp_path)
    calls: list[str] = []
    fail_content = True

    def operation(stage: str):
        def run(config, _lease, _checkpoint):
            nonlocal fail_content
            calls.append(stage)
            if stage == "download":
                config.job_dir.mkdir(parents=True, exist_ok=True)
                (config.job_dir / "source.mp4").write_bytes(b"fixture")
            if stage == "analysis":
                update_item_by_job(
                    config.root / "data" / "knowledge.db",
                    config.job_id,
                    status="analyzed",
                    job_path=config.job_dir,
                    media_sha256=hashlib.sha256(b"fixture").hexdigest(),
                )
            if stage == "structured_generate" and fail_content:
                fail_content = False
                raise SingleItemError("fixture_interrupted", "fixture interruption")
            if stage == "publish":
                library = config.root / "library" / "fixture"
                library.mkdir(parents=True, exist_ok=True)
                (library / "内容整理.md").write_text("fixture\n", encoding="utf-8")
                update_item_by_job(
                    config.root / "data" / "knowledge.db",
                    config.job_id,
                    status="completed",
                    library_path=library,
                    media_sha256=hashlib.sha256(b"fixture").hexdigest(),
                )
            _mark(config, stage)
            if stage == "accept":
                return {
                    "status": "passed",
                    "job_id": config.job_id,
                    "position": config.position,
                    "checks": {"fixture": True},
                }
            return None

        return run

    operations = {
        stage: operation(stage)
        for stage in (
            "download",
            "analysis",
            "structured_generate",
            "schema_validate",
            "render",
            "staging_accept",
            "publish",
            "accept",
        )
    }
    validators = {stage: _marker_valid(stage) for stage in operations}

    assert (
        run_single_item(config, operations=operations, validators=validators) == CONTROLLED_FAILURE
    )
    assert calls == ["download", "analysis", "structured_generate"]
    assert run_single_item(config, operations=operations, validators=validators) == SUCCESS
    assert calls == [
        "download",
        "analysis",
        "structured_generate",
        "structured_generate",
        "schema_validate",
        "render",
        "staging_accept",
        "publish",
        "accept",
    ]

    with sqlite3.connect(config.root / "data" / "knowledge.db") as connection:
        fixed_status = connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (config.job_id,)
        ).fetchone()[0]
        next_status = connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (next_job_id,)
        ).fetchone()[0]
    assert fixed_status == "completed"
    assert next_status == "new"
    acceptance = json.loads(config.acceptance_path.read_text(encoding="utf-8"))
    assert acceptance["status"] == "passed"
    assert acceptance["timings"]["accept"][-1]["status"] == "completed"
    assert not (config.task_dir / "single-item-driver.lock").exists()


def test_resume_after_schema_checkpoint_skips_download_analysis_and_generation(
    tmp_path: Path,
) -> None:
    config, next_job_id = _fixture(tmp_path)
    calls: list[str] = []
    fail_schema = True

    def operation(stage: str):
        def run(current, _lease, _checkpoint):
            nonlocal fail_schema
            calls.append(stage)
            if stage == "download":
                current.job_dir.mkdir(parents=True, exist_ok=True)
                (current.job_dir / "source.mp4").write_bytes(b"fixture")
            if stage == "analysis":
                update_item_by_job(
                    current.root / "data" / "knowledge.db",
                    current.job_id,
                    status="analyzed",
                    job_path=current.job_dir,
                    media_sha256=hashlib.sha256(b"fixture").hexdigest(),
                )
            if stage == "schema_validate" and fail_schema:
                fail_schema = False
                raise SingleItemError("fixture_schema_pause", "fixture pause")
            if stage == "publish":
                library = current.root / "library" / "fixture"
                library.mkdir(parents=True, exist_ok=True)
                (library / "内容整理.md").write_text("fixture\n", encoding="utf-8")
                update_item_by_job(
                    current.root / "data" / "knowledge.db",
                    current.job_id,
                    status="completed",
                    library_path=library,
                    media_sha256=hashlib.sha256(b"fixture").hexdigest(),
                )
            _mark(current, stage)
            if stage == "accept":
                return {
                    "status": "passed",
                    "job_id": current.job_id,
                    "position": current.position,
                    "checks": {"fixture": True},
                }
            return None

        return run

    operations = {stage: operation(stage) for stage in (
        "download",
        "analysis",
        "structured_generate",
        "schema_validate",
        "render",
        "staging_accept",
        "publish",
        "accept",
    )}
    validators = {stage: _marker_valid(stage) for stage in operations}

    assert (
        run_single_item(config, operations=operations, validators=validators)
        == CONTROLLED_FAILURE
    )
    assert calls == ["download", "analysis", "structured_generate", "schema_validate"]
    assert run_single_item(config, operations=operations, validators=validators) == SUCCESS
    assert calls == [
        "download",
        "analysis",
        "structured_generate",
        "schema_validate",
        "schema_validate",
        "render",
        "staging_accept",
        "publish",
        "accept",
    ]
    checkpoint = json.loads(config.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["stages"]["structured_generate"]["status"] == "completed"
    assert checkpoint["stages"]["schema_validate"]["status"] == "completed"
    with sqlite3.connect(config.root / "data" / "knowledge.db") as connection:
        assert connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (next_job_id,)
        ).fetchone()[0] == "new"


def test_stop_after_structured_content_never_enters_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, next_job_id = _fixture(tmp_path)
    config = SingleItemConfig(
        **{
            **config.__dict__,
            "stop_after_structured_content": True,
        }
    )
    calls: list[str] = []
    finished: list[str] = []
    validators = {
        "download": lambda _config: True,
        "analysis": lambda _config: True,
        "structured_generate": lambda _config: False,
        "schema_validate": lambda _config: False,
        "render": lambda _config: False,
        "staging_accept": lambda _config: False,
        "publish": lambda _config: False,
        "accept": lambda _config: False,
    }
    operations = {
        stage: (
            lambda _config, _lease, _checkpoint, current=stage: calls.append(current)
        )
        for stage in (
            "structured_generate",
            "schema_validate",
            "render",
            "staging_accept",
            "publish",
            "accept",
        )
    }
    monkeypatch.setattr(
        "app.single_item_driver._staging_baseline",
        lambda _config: {"analysis": {}, "registry": [], "vault_digest": "fixture"},
    )
    monkeypatch.setattr(
        "app.single_item_driver._finish_structured_staging",
        lambda _config, _checkpoint: finished.append("staged"),
    )

    assert run_single_item(config, operations=operations, validators=validators) == SUCCESS
    assert calls == [
        "structured_generate",
        "schema_validate",
        "render",
        "staging_accept",
    ]
    assert finished == ["staged"]
    assert not (config.task_dir / "artifacts" / "phase6-content-acceptance.json").exists()
    with sqlite3.connect(config.root / "data" / "knowledge.db") as connection:
        assert connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (next_job_id,)
        ).fetchone()[0] == "new"


def test_prepublish_backup_is_reused_after_publish_interruption(tmp_path: Path) -> None:
    config, _next_job = _fixture(tmp_path)
    checkpoint = {
        "backup": None,
        "timings": {
            stage: []
            for stage in ("download", "analysis", "structured_content", "publish", "accept")
        },
    }
    first = _ensure_backup(config, checkpoint)
    first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
    first_mtime = first.stat().st_mtime_ns
    second = _ensure_backup(config, checkpoint)
    assert second == first
    assert hashlib.sha256(second.read_bytes()).hexdigest() == first_hash
    assert second.stat().st_mtime_ns == first_mtime
    recovered_checkpoint = {"backup": None}
    recovered = _ensure_backup(config, recovered_checkpoint)
    assert recovered == first
    assert recovered_checkpoint["backup"]["recovered_from_interruption"] is True
    assert (
        len(list((config.task_dir / "artifacts").glob("single-item-prepublish-knowledge.db"))) == 1
    )


def test_completed_publish_checkpoint_can_resume_acceptance(tmp_path: Path) -> None:
    config, next_job_id = _fixture(tmp_path)
    library = config.root / "library" / "fixture"
    library.mkdir(parents=True)
    (library / "内容整理.md").write_text("fixture\n", encoding="utf-8")
    update_item_by_job(
        config.root / "data" / "knowledge.db",
        config.job_id,
        status="completed",
        library_path=library,
    )
    backup = config.task_dir / "artifacts" / "single-item-prepublish-knowledge.db"
    backup.parent.mkdir(parents=True)
    source = sqlite3.connect(config.root / "data" / "knowledge.db")
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    checkpoint = {
        "schema_version": 1,
        "job_id": config.job_id,
        "position": config.position,
        "stages": {
            stage: {"status": "completed"}
            for stage in (
                "download",
                "analysis",
                "structured_generate",
                "schema_validate",
                "render",
                "staging_accept",
                "publish",
            )
        },
        "timings": {
            stage: []
            for stage in (
                "download",
                "analysis",
                "structured_generate",
                "schema_validate",
                "render",
                "staging_accept",
                "publish",
                "accept",
            )
        },
        "content": {"active_runner": "primary", "fallback_switches": 0},
        "backup": {"path": str(backup), "sha256": hashlib.sha256(backup.read_bytes()).hexdigest()},
    }
    config.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    config.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    validators = {
        stage: (lambda _config: True)
        for stage in (
            "download",
            "analysis",
            "structured_generate",
            "schema_validate",
            "render",
            "staging_accept",
            "publish",
        )
    }
    validators["accept"] = lambda current: current.acceptance_path.is_file()
    operations = {
        "accept": lambda current, _lease, _checkpoint: {
            "status": "passed",
            "job_id": current.job_id,
            "position": current.position,
            "checks": {"fixture": True},
        }
    }
    assert run_single_item(config, operations=operations, validators=validators) == SUCCESS
    with sqlite3.connect(config.root / "data" / "knowledge.db") as connection:
        assert (
            connection.execute(
                "SELECT status FROM collection_items WHERE job_id = ?", (next_job_id,)
            ).fetchone()[0]
            == "new"
        )


def test_content_heartbeat_continues_during_in_process_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Lease:
        calls: list[str] = []

        def heartbeat(self, stage: str) -> None:
            self.calls.append(stage)

    monkeypatch.setattr("app.single_item_driver.HEARTBEAT_SECONDS", 0.01)
    lease = Lease()

    def slow_operation() -> str:
        time.sleep(0.04)
        return "done"

    assert _run_with_heartbeat(lease, "content", slow_operation) == "done"
    assert len(lease.calls) >= 2
    assert set(lease.calls) == {"content"}


def test_content_switches_once_only_for_empty_primary(tmp_path: Path, monkeypatch) -> None:
    config, _next_job = _fixture(tmp_path)
    checkpoint = {
        "content": {"active_runner": "primary", "fallback_switches": 0},
    }
    calls: list[Path] = []

    monkeypatch.setattr(
        "app.single_item_driver.preflight_content_runner",
        lambda _root, _job, runner, _output: ("file", [runner]),
    )

    def fake_run(_root, _job, runner, _output):
        calls.append(runner)
        if runner == config.primary_runner:
            raise ContentStageError("content_runner_empty", "empty")
        return object()

    monkeypatch.setattr("app.single_item_driver.run_content_stage", fake_run)
    monkeypatch.setattr("app.single_item_driver._save_checkpoint", lambda *_args: None)
    _content(config, object(), checkpoint)
    assert calls == [config.primary_runner, config.fallback_runner]
    assert checkpoint["content"]["fallback_switches"] == 1


def test_content_quarantines_protocol_failure_and_switches_fallback_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _next_job = _fixture(tmp_path)
    checkpoint = {
        "content": {"active_runner": "primary", "fallback_switches": 0},
    }
    calls: list[Path] = []
    evidence = {
        "path": str(config.root / "quarantine" / "candidate.md"),
        "sha256": "a" * 64,
        "error_code": "content_review_status_invalid",
        "runner": "primary",
    }
    monkeypatch.setattr(
        "app.single_item_driver.preflight_content_runner",
        lambda _root, _job, runner, _output: ("file", [runner]),
    )

    def fake_run(_root, _job, runner, _output):
        calls.append(runner)
        if runner == config.primary_runner:
            raise ContentStageError(
                "content_review_status_invalid", "invalid", quarantine=evidence
            )
        return object()

    monkeypatch.setattr("app.single_item_driver.run_content_stage", fake_run)
    monkeypatch.setattr("app.single_item_driver._save_checkpoint", lambda *_args: None)
    _content(config, object(), checkpoint)
    assert calls == [config.primary_runner, config.fallback_runner]
    content = checkpoint["content"]
    assert content["fallback_switches"] == 1
    assert content["active_runner"] == "fallback"
    assert content["failures"][0]["quarantine"] == evidence


@pytest.mark.parametrize("code", ["content_body_too_shallow", "content_privacy_rejected"])
def test_content_quality_and_privacy_failures_never_switch_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    config, _next_job = _fixture(tmp_path)
    checkpoint = {
        "content": {"active_runner": "primary", "fallback_switches": 0},
    }
    calls: list[Path] = []
    evidence = {
        "path": str(config.root / "quarantine" / "candidate.md"),
        "sha256": "b" * 64,
        "error_code": code,
        "runner": "primary",
    }
    monkeypatch.setattr(
        "app.single_item_driver.preflight_content_runner",
        lambda _root, _job, runner, _output: ("file", [runner]),
    )

    def fake_run(_root, _job, runner, _output):
        calls.append(runner)
        raise ContentStageError(code, "quality failure", quarantine=evidence)

    monkeypatch.setattr("app.single_item_driver.run_content_stage", fake_run)
    monkeypatch.setattr("app.single_item_driver._save_checkpoint", lambda *_args: None)
    with pytest.raises(ContentStageError) as error:
        _content(config, object(), checkpoint)
    assert error.value.code == code
    assert calls == [config.primary_runner]
    assert checkpoint["content"]["fallback_switches"] == 0
    assert checkpoint["content"]["failures"][0]["quarantine"] == evidence


def test_content_fallback_protocol_failure_stops_without_third_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _next_job = _fixture(tmp_path)
    checkpoint = {
        "content": {"active_runner": "primary", "fallback_switches": 0},
    }
    calls: list[Path] = []
    monkeypatch.setattr(
        "app.single_item_driver.preflight_content_runner",
        lambda _root, _job, runner, _output: ("file", [runner]),
    )

    def fake_run(_root, _job, runner, _output):
        calls.append(runner)
        raise ContentStageError("content_review_status_invalid", "invalid")

    monkeypatch.setattr("app.single_item_driver.run_content_stage", fake_run)
    monkeypatch.setattr("app.single_item_driver._save_checkpoint", lambda *_args: None)
    with pytest.raises(ContentStageError):
        _content(config, object(), checkpoint)
    assert calls == [config.primary_runner, config.fallback_runner]
    assert checkpoint["content"]["fallback_switches"] == 1
    assert len(checkpoint["content"]["failures"]) == 2


def test_legacy_protocol_checkpoint_resumes_directly_with_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _next_job = _fixture(tmp_path)
    checkpoint = {
        "error": "content_review_status_invalid",
        "timings": {"content": [{"status": "failed"}]},
        "content": {"active_runner": "primary", "fallback_switches": 0},
    }
    calls: list[Path] = []
    monkeypatch.setattr(
        "app.single_item_driver.preflight_content_runner",
        lambda _root, _job, runner, _output: ("stdout", [runner]),
    )
    monkeypatch.setattr(
        "app.single_item_driver.run_content_stage",
        lambda _root, _job, runner, _output: calls.append(runner),
    )
    monkeypatch.setattr("app.single_item_driver._save_checkpoint", lambda *_args: None)
    _content(config, object(), checkpoint)
    assert calls == [config.fallback_runner]
    assert checkpoint["content"]["active_runner"] == "fallback"
    assert checkpoint["content"]["fallback_switches"] == 1
    failure = checkpoint["content"]["failures"][0]
    assert failure["runner"] == "primary"
    assert failure["quarantine"]["status"] == "unavailable_legacy_deleted"


def test_content_does_not_fallback_on_nonempty_runner_failure(tmp_path: Path, monkeypatch) -> None:
    config, _next_job = _fixture(tmp_path)
    checkpoint = {
        "content": {"active_runner": "primary", "fallback_switches": 0},
    }
    calls: list[Path] = []
    monkeypatch.setattr(
        "app.single_item_driver.preflight_content_runner",
        lambda _root, _job, runner, _output: ("file", [runner]),
    )

    def fake_run(_root, _job, runner, _output):
        calls.append(runner)
        raise ContentStageError("content_runner_failed", "failed")

    monkeypatch.setattr("app.single_item_driver.run_content_stage", fake_run)
    with pytest.raises(ContentStageError):
        _content(config, object(), checkpoint)
    assert calls == [config.primary_runner]
    assert checkpoint["content"]["fallback_switches"] == 0


def test_dry_run_has_no_state_database_or_checkpoint_side_effects(tmp_path: Path, capsys) -> None:
    config, _next_job = _fixture(tmp_path, dry_run=True)
    state = config.task_dir / "state.json"
    database = config.root / "data" / "knowledge.db"
    before_state = state.read_bytes()
    before_database = database.read_bytes()

    assert run_single_item(config) == SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["will_select_next"] is False
    assert state.read_bytes() == before_state
    assert database.read_bytes() == before_database
    assert not config.checkpoint_path.exists()
    assert not (config.task_dir / "single-item-driver.lock").exists()


def test_dry_run_tracks_position_drift_by_stable_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, next_job_id = _fixture(tmp_path, dry_run=True)
    registry = CollectionRegistry(config.root / "data" / "knowledge.db", root=config.root)
    snapshot = registry.begin_snapshot(pipeline_version=PIPELINE_VERSION)
    registry.record_snapshot_page(
        snapshot,
        [
            {"source_id": "new-head-1", "aweme_id": "new-head-1"},
            {"source_id": "new-head-2", "aweme_id": "new-head-2"},
            {"source_id": config.aweme_id, "aweme_id": config.aweme_id},
            {"source_id": "next-source", "aweme_id": "next-source"},
        ],
    )
    registry.complete_snapshot(snapshot, pipeline_version=PIPELINE_VERSION)

    assert run_single_item(config) == SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == config.job_id
    assert payload["aweme_id"] == config.aweme_id
    assert payload["original_position"] == 1
    assert payload["current_position"] == 3
    assert payload["will_select_next"] is False
    with sqlite3.connect(config.root / "data" / "knowledge.db") as connection:
        assert connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (next_job_id,)
        ).fetchone()[0] == "new"
    assert not config.checkpoint_path.exists()


def test_dry_run_rejects_probe_one_handoff_contract_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    config, _next_job = _fixture(tmp_path, dry_run=True)

    def reject_handoff(_root, _path):
        from app.security import GateError

        raise GateError("handoff 路径必须位于 data/jobs")

    monkeypatch.setattr("app.single_item_driver.resolve_handoff_path", reject_handoff)

    assert run_single_item(config) == PREFLIGHT_FAILURE
    assert not config.checkpoint_path.exists()
    assert not (config.task_dir / "single-item-driver.lock").exists()


def test_download_uses_probe_one_handoff_contract(tmp_path: Path, monkeypatch) -> None:
    config, _next_job = _fixture(tmp_path)
    observed: list[Path] = []

    def fake_subprocess(_config, _lease, stage, command):
        assert stage == "download"
        assert command[command.index("--job-id") + 1] == config.job_id
        assert command[command.index("--aweme-id") + 1] == config.aweme_id
        handoff = Path(command[command.index("--handoff") + 1])
        observed.append(handoff)
        child_script = """
import json
import sys
from pathlib import Path
import app.probe_one as probe
from app.security import windows_acl_metadata
root = Path(sys.argv[1])
handoff = Path(sys.argv[2])
job_id = sys.argv[3]
aweme_id = sys.argv[4]
def acl_only_preflight(current_root):
    metadata = windows_acl_metadata(current_root / "config")
    assert isinstance(metadata.get("acl_check_returncode"), int)
probe.sync_preflight = acl_only_preflight
probe._sync_preflight_with_diagnostics(root)
resolved = probe.resolve_handoff_path(root, handoff)
assert resolved == handoff.resolve()
resolved.parent.mkdir(parents=True, exist_ok=True)
resolved.write_text(
    json.dumps(
        {"job_id": job_id, "aweme_id": aweme_id, "observed_position": 10}
    ),
    encoding="utf-8",
)
print(json.dumps({"handoff": str(resolved), "acl": "ok"}))
"""
        environment = os.environ.copy()
        environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                child_script,
                str(config.root),
                str(handoff),
                config.job_id,
                config.aweme_id,
            ],
            cwd=config.root,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        assert "UnicodeDecodeError" not in completed.stderr
        assert "TypeError" not in completed.stderr
        assert json.loads(completed.stdout)["handoff"] == str(handoff.resolve())
        (config.job_dir / "source.mp4").write_bytes(b"fixture")
        return 0

    monkeypatch.setattr("app.single_item_driver._run_subprocess", fake_subprocess)
    _download(config, object(), {})

    assert observed == [config.download_handoff_path]
    checkpoint = json.loads(config.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["original_position"] == 1
    assert checkpoint["current_position"] == 10
    assert checkpoint["position_drift_history"][-1]["current_position"] == 10
    assert config.download_handoff_path.is_relative_to(config.root / "data" / "jobs")
    assert not (config.task_dir / "artifacts" / "single-item-download-handoff.json").exists()
