from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.collection_registry import (
    CollectionRegistry,
    CollectionRegistryError,
    SnapshotSyncError,
    begin_snapshot,
    complete_snapshot,
    ingest_snapshot_page,
    stable_collection_job_id,
    synchronize_collection,
)

PIPELINE_V1 = "test-v1"
PIPELINE_V2 = "test-v2"


def make_registry(tmp_path: Path) -> CollectionRegistry:
    return CollectionRegistry(tmp_path / "data" / "knowledge.db", root=tmp_path)


def fetch_pages(*pages):
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        return pages[len(calls) - 1]

    fetch.calls = calls
    return fetch


def page(ids, *, cursor=0, has_more=False, hashes=None):
    hashes = hashes or {}
    return {
        "status_code": 0,
        "aweme_list": [
            {
                "aweme_id": source_id,
                **({"media_sha256": hashes[source_id]} if source_id in hashes else {}),
            }
            for source_id in ids
        ],
        "cursor": cursor,
        "has_more": has_more,
    }


def complete_with_artifacts(
    registry: CollectionRegistry,
    tmp_path: Path,
    source_id: str,
    *,
    pipeline_version: str = PIPELINE_V1,
    body: bytes | None = None,
):
    body = body or f"media-{source_id}".encode()
    job_id = stable_collection_job_id(source_id)
    job_path = tmp_path / "data" / "jobs" / job_id
    library_path = tmp_path / "library" / source_id
    job_path.mkdir(parents=True, exist_ok=True)
    library_path.mkdir(parents=True, exist_ok=True)
    (job_path / "source.mp4").write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    registry.mark_completed(
        source_id,
        pipeline_version=pipeline_version,
        media_sha256=digest,
        job_path=job_path,
        library_path=library_path,
    )
    return job_path, library_path, digest


def test_snapshot_page_chain_rejects_nonzero_start_discontinuity_and_truncation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "data" / "knowledge.db"

    first = begin_snapshot(db_path, pipeline_version=PIPELINE_V1)
    with pytest.raises(CollectionRegistryError, match="snapshot_must_start_at_zero"):
        ingest_snapshot_page(
            db_path,
            snapshot_id=first.snapshot_id,
            cursor=5,
            next_cursor=10,
            has_more=True,
            items=[{"aweme_id": "a"}],
        )

    second = begin_snapshot(db_path, pipeline_version=PIPELINE_V1)
    ingest_snapshot_page(
        db_path,
        snapshot_id=second.snapshot_id,
        cursor=0,
        next_cursor=10,
        has_more=True,
        items=[{"aweme_id": "a"}],
    )
    with pytest.raises(CollectionRegistryError, match="snapshot_cursor_discontinuity"):
        ingest_snapshot_page(
            db_path,
            snapshot_id=second.snapshot_id,
            cursor=11,
            next_cursor=20,
            has_more=False,
            items=[{"aweme_id": "b"}],
        )
    with pytest.raises(CollectionRegistryError, match="snapshot_incomplete"):
        complete_snapshot(db_path, second.snapshot_id)


def test_top_insert_changes_positions_but_completed_items_stay_skipped(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    first = synchronize_collection(
        registry,
        fetch_pages(page(["old-a", "old-b"])),
        pipeline_version=PIPELINE_V1,
    )
    complete_with_artifacts(registry, tmp_path, "old-a")
    complete_with_artifacts(registry, tmp_path, "old-b")

    second = synchronize_collection(
        registry,
        fetch_pages(page(["new-top", "old-a", "old-b"])),
        pipeline_version=PIPELINE_V1,
    )

    assert first.item_count == 2
    assert second.next_item is not None
    assert second.next_item.source_id == "new-top"
    assert registry.get("old-a").last_position == 2
    assert registry.get("old-b").last_position == 3
    assert registry.get("old-a").status == "completed"
    assert registry.get("old-b").status == "completed"


def test_uncollected_is_soft_marked_and_artifacts_are_not_deleted(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    synchronize_collection(
        registry,
        fetch_pages(page(["keep", "remove"])),
        pipeline_version=PIPELINE_V1,
    )
    job_path, library_path, _digest = complete_with_artifacts(registry, tmp_path, "remove")

    synchronize_collection(
        registry,
        fetch_pages(page(["keep"])),
        pipeline_version=PIPELINE_V1,
    )

    item = registry.get("remove")
    assert item is not None
    assert item.currently_collected is False
    assert item.uncollected_at is not None
    assert (job_path / "source.mp4").exists()
    assert library_path.exists()


def test_recollection_restores_current_and_skips_valid_completed_item(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    synchronize_collection(registry, fetch_pages(page(["returning"])), pipeline_version=PIPELINE_V1)
    complete_with_artifacts(registry, tmp_path, "returning")
    synchronize_collection(registry, fetch_pages(page([])), pipeline_version=PIPELINE_V1)

    result = synchronize_collection(
        registry,
        fetch_pages(page(["returning"])),
        pipeline_version=PIPELINE_V1,
    )

    item = registry.get("returning")
    assert item is not None
    assert item.currently_collected is True
    assert item.uncollected_at is None
    assert item.status == "completed"
    assert result.next_item is None


def test_failed_and_incomplete_items_are_retried_in_snapshot_order(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    first = synchronize_collection(
        registry,
        fetch_pages(page(["failed-item", "later-item"])),
        pipeline_version=PIPELINE_V1,
    )
    registry.mark_failed("failed-item", error="download_failed")

    next_item = registry.next_item(first.snapshot_id, pipeline_version=PIPELINE_V1)
    assert next_item.source_id == "failed-item"

    registry.mark_incomplete("failed-item", error="analysis_incomplete")
    next_item = registry.next_item(first.snapshot_id, pipeline_version=PIPELINE_V1)
    assert next_item.source_id == "failed-item"


def test_duplicate_items_across_pages_are_deduplicated_by_source_id(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    fetch = fetch_pages(
        page(["a", "b"], cursor=10, has_more=True),
        page(["b", "c"], cursor=20, has_more=False),
    )

    result = synchronize_collection(registry, fetch, pipeline_version=PIPELINE_V1)

    assert fetch.calls == [0, 10]
    assert result.item_count == 3
    assert registry.get("a").last_position == 1
    assert registry.get("b").last_position == 2
    assert registry.get("c").last_position == 3


def test_interrupted_snapshot_does_not_soft_uncollect_missing_items(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    synchronize_collection(
        registry,
        fetch_pages(page(["still-there", "not-yet-seen"])),
        pipeline_version=PIPELINE_V1,
    )
    calls = 0

    def interrupted(cursor):
        nonlocal calls
        calls += 1
        if calls == 1:
            return page(["still-there"], cursor=10, has_more=True)
        raise RuntimeError("local mock interruption")

    with pytest.raises(SnapshotSyncError) as error:
        synchronize_collection(registry, interrupted, pipeline_version=PIPELINE_V1)

    assert registry.snapshot_state(error.value.snapshot_id) == "failed"
    assert registry.get("not-yet-seen").currently_collected is True
    assert registry.get("not-yet-seen").uncollected_at is None


def test_pipeline_version_change_marks_completed_item_for_processing(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    synchronize_collection(registry, fetch_pages(page(["versioned"])), pipeline_version=PIPELINE_V1)
    complete_with_artifacts(registry, tmp_path, "versioned")

    result = synchronize_collection(
        registry,
        fetch_pages(page(["versioned"])),
        pipeline_version=PIPELINE_V2,
    )

    assert result.next_item is not None
    assert result.next_item.source_id == "versioned"
    assert registry.get("versioned").status == "new"
    assert registry.get("versioned").pipeline_version == PIPELINE_V2


def test_media_hash_change_marks_completed_item_for_processing(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    old_body = b"old-media"
    old_hash = hashlib.sha256(old_body).hexdigest()
    synchronize_collection(
        registry,
        fetch_pages(page(["changed"], hashes={"changed": old_hash})),
        pipeline_version=PIPELINE_V1,
    )
    complete_with_artifacts(registry, tmp_path, "changed", body=old_body)
    new_hash = hashlib.sha256(b"new-media").hexdigest()

    result = synchronize_collection(
        registry,
        fetch_pages(page(["changed"], hashes={"changed": new_hash})),
        pipeline_version=PIPELINE_V1,
    )

    assert result.next_item is not None
    assert result.next_item.source_id == "changed"
    assert registry.get("changed").status == "new"
    assert registry.get("changed").media_sha256 == new_hash


def test_concurrent_duplicate_page_ingest_is_idempotent(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    snapshot_id = registry.begin_snapshot(pipeline_version=PIPELINE_V1)
    items = [{"aweme_id": "same-a"}, {"aweme_id": "same-b"}]

    with ThreadPoolExecutor(max_workers=8) as pool:
        inserted = list(
            pool.map(
                lambda _index: registry.record_snapshot_page(snapshot_id, items),
                range(16),
            )
        )

    assert sum(inserted) == 2
    assert registry.complete_snapshot(snapshot_id, pipeline_version=PIPELINE_V1) == 2
    with sqlite3.connect(registry.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collection_snapshot_items WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()[0] == 2
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(collection_snapshot_items)")
            if row[2]
        }
    assert indexes


def test_job_id_is_stable_and_position_is_not_part_of_identity(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    synchronize_collection(registry, fetch_pages(page(["identity"])), pipeline_version=PIPELINE_V1)
    first = registry.get("identity")
    synchronize_collection(
        registry,
        fetch_pages(page(["new", "identity"])),
        pipeline_version=PIPELINE_V1,
    )
    moved = registry.get("identity")

    assert first.job_id == moved.job_id == stable_collection_job_id("identity")
    assert first.last_position == 1
    assert moved.last_position == 2
