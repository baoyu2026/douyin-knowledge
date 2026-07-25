from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from app.collection_registry import CollectionRegistry
from douyin_knowledge.publication import (
    accept_publication,
    begin_publication,
    reconcile_publications,
)


def accept_item_for_test(
    registry: CollectionRegistry,
    source_id: str,
    *,
    pipeline_version: str,
    media_sha256: str,
    job_path: Path,
    library_path: Path,
) -> None:
    item = registry.get(source_id)
    assert item is not None
    marker = (
        registry.root
        / "orchestration"
        / "test-publication-targets"
        / f"{item.job_id}-{uuid.uuid4().hex}.txt"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("accepted publication fixture\n", encoding="utf-8")
    digest = hashlib.sha256(marker.read_bytes()).hexdigest()
    saga = begin_publication(
        registry.root,
        registry.db_path,
        job_ref=item.job_id,
        idempotency_key=uuid.uuid4().hex,
        draft_sha256="1" * 64,
        media_sha256=media_sha256,
        targets={"fixture": (marker.relative_to(registry.root).as_posix(), digest)},
    )
    reconciled = reconcile_publications(
        registry.root, registry.db_path, job_ref=item.job_id
    )
    assert reconciled[-1]["state"] == "published_unaccepted"
    accepted = accept_publication(
        registry.db_path,
        saga["saga_id"],
        checks={"sqlite_integrity": True, "privacy": True, "content": True},
    )
    assert accepted["state"] == "accepted"
    registry.mark_completed(
        source_id,
        pipeline_version=pipeline_version,
        media_sha256=media_sha256,
        job_path=job_path,
        library_path=library_path,
    )
