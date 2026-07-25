from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.collection_registry import (
    PIPELINE_VERSION,
    CollectionRegistry,
    update_item_by_job,
)
from app.managed_batch import BatchConfig, OwnerLease, run_managed_batch, sanitize


def _fixture(tmp_path: Path) -> BatchConfig:
    root = tmp_path / "root"
    task = tmp_path / "task"
    task.mkdir(parents=True)
    (task / "state.json").write_text(
        json.dumps({"status": "owner_released"}), encoding="utf-8"
    )
    db = root / "data" / "knowledge.db"
    registry = CollectionRegistry(db, root=root)
    snapshot = registry.begin_snapshot(pipeline_version=PIPELINE_VERSION)
    registry.record_snapshot_page(
        snapshot,
        [{"source_id": f"source-{index}"} for index in range(1, 8)],
    )
    registry.complete_snapshot(snapshot, pipeline_version=PIPELINE_VERSION)
    for index in range(1, 5):
        source_id = f"source-{index}"
        item = registry.get(source_id)
        assert item is not None
        job_dir = root / "data" / "jobs" / item.job_id
        library_dir = root / "library" / f"fixture-{index}"
        job_dir.mkdir(parents=True)
        library_dir.mkdir(parents=True)
        media = job_dir / "source.mp4"
        media.write_bytes(f"fixture-{index}".encode())
        registry.mark_completed(
            source_id,
            pipeline_version=PIPELINE_VERSION,
            media_sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
            job_path=job_dir,
            library_path=library_dir,
        )
    return BatchConfig(
        root=root,
        task_dir=task,
        completed_before_run=2,
        target_completed_this_run=5,
    )


def test_owner_lease_is_exclusive_and_cleans_up(tmp_path: Path) -> None:
    lease = OwnerLease(tmp_path)
    lease.acquire()
    second = OwnerLease(tmp_path)
    try:
        try:
            second.acquire()
        except FileExistsError:
            pass
        else:
            raise AssertionError("second owner unexpectedly acquired the lock")
    finally:
        lease.release()
    assert not lease.lock.exists()
    assert not lease.owner.exists()


def test_driver_stops_after_exactly_three_new_completions(tmp_path: Path, monkeypatch) -> None:
    config = _fixture(tmp_path)
    calls: list[int] = []

    def fake_invoke(config: BatchConfig, _lease: OwnerLease, item) -> int:
        calls.append(item.last_position)
        job_dir = config.root / "data" / "jobs" / item.job_id
        library_dir = config.root / "library" / item.job_id
        job_dir.mkdir(parents=True)
        library_dir.mkdir(parents=True)
        media = job_dir / "source.mp4"
        media.write_bytes(f"completed-{item.last_position}".encode())
        registry = CollectionRegistry(
            config.root / "data" / "knowledge.db", root=config.root
        )
        registry.mark_completed(
            item.source_id,
            pipeline_version=PIPELINE_VERSION,
            media_sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
            job_path=job_dir,
            library_path=library_dir,
        )
        return 0

    monkeypatch.setattr(
        "app.managed_batch.acceptance_records",
        lambda *_args: [
            {
                "title": f"title-{index}",
                "category": "test",
                "review": "已复核",
                "acceptance": {"ok": True},
            }
            for index in range(3)
        ],
    )
    assert run_managed_batch(
        config, invoke=fake_invoke, quality_check=lambda _config: None
    ) == 0
    assert calls == [5, 6, 7]
    state = json.loads((config.task_dir / "state.json").read_text(encoding="utf-8"))
    assert state["completed_this_run"] == 5
    assert state["status"] == "batch_ready_for_report"


def test_sanitize_removes_urls_ids_and_session_values() -> None:
    value = (
        "https://example.test/a aweme-0123456789abcdef0123 "
        "token=secret 123456789012345678"
    )
    sanitized = sanitize(value)
    assert "https://" not in sanitized
    assert "secret" not in sanitized
    assert "aweme-" not in sanitized
    assert "123456789012345678" not in sanitized


def test_dry_run_quality_blocker_has_no_side_effects(tmp_path: Path) -> None:
    original = _fixture(tmp_path)
    config = BatchConfig(
        root=original.root,
        task_dir=original.task_dir,
        completed_before_run=original.completed_before_run,
        target_completed_this_run=original.target_completed_this_run,
        dry_run=True,
    )
    state_path = config.task_dir / "state.json"
    database_path = config.root / "data" / "knowledge.db"
    before_state = state_path.read_bytes()
    before_database = database_path.read_bytes()

    assert run_managed_batch(config) == 2

    assert state_path.read_bytes() == before_state
    assert database_path.read_bytes() == before_database
    assert not (config.task_dir / "managed-driver.lock").exists()
    assert not (config.task_dir / "managed-driver-owner.json").exists()
    assert not (config.task_dir / "run.log").exists()


def test_dry_run_success_does_not_change_state_database_or_lock(tmp_path: Path) -> None:
    original = _fixture(tmp_path)
    config = BatchConfig(
        root=original.root,
        task_dir=original.task_dir,
        completed_before_run=original.completed_before_run,
        target_completed_this_run=original.target_completed_this_run,
        dry_run=True,
    )
    state_path = config.task_dir / "state.json"
    database_path = config.root / "data" / "knowledge.db"
    before_state = state_path.read_bytes()
    before_database = database_path.read_bytes()

    assert run_managed_batch(config, quality_check=lambda _config: None) == 0

    assert state_path.read_bytes() == before_state
    assert database_path.read_bytes() == before_database
    assert not (config.task_dir / "managed-driver.lock").exists()
    assert not (config.task_dir / "managed-driver-owner.json").exists()
    assert not (config.task_dir / "run.log").exists()


def test_downloaded_checkpoint_is_resumed_before_new_items(tmp_path: Path, monkeypatch) -> None:
    config = _fixture(tmp_path)
    registry = CollectionRegistry(
        config.root / "data" / "knowledge.db", root=config.root
    )
    downloaded = registry.get("source-5")
    assert downloaded is not None
    job_dir = config.root / "data" / "jobs" / downloaded.job_id
    job_dir.mkdir(parents=True)
    media = job_dir / "source.mp4"
    media.write_bytes(b"downloaded checkpoint")
    update_item_by_job(
        config.root / "data" / "knowledge.db",
        downloaded.job_id,
        status="downloaded",
        media_sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
        job_path=job_dir,
    )
    statuses: list[str] = []

    def fake_invoke(config: BatchConfig, _lease: OwnerLease, item) -> int:
        statuses.append(item.status)
        current_job = config.root / "data" / "jobs" / item.job_id
        current_job.mkdir(parents=True, exist_ok=True)
        current_media = current_job / "source.mp4"
        if not current_media.exists():
            current_media.write_bytes(f"completed-{item.last_position}".encode())
        library = config.root / "library" / item.job_id
        library.mkdir(parents=True)
        CollectionRegistry(
            config.root / "data" / "knowledge.db", root=config.root
        ).mark_completed(
            item.source_id,
            pipeline_version=PIPELINE_VERSION,
            media_sha256=hashlib.sha256(current_media.read_bytes()).hexdigest(),
            job_path=current_job,
            library_path=library,
        )
        return 0

    monkeypatch.setattr(
        "app.managed_batch.acceptance_records",
        lambda *_args: [
            {
                "title": f"title-{index}",
                "category": "test",
                "review": "已复核",
                "acceptance": {"ok": True},
            }
            for index in range(3)
        ],
    )
    assert run_managed_batch(
        config, invoke=fake_invoke, quality_check=lambda _config: None
    ) == 0
    assert statuses == ["downloaded", "new", "new"]
