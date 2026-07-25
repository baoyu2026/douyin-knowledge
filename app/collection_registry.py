from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PIPELINE_VERSION = "2"
PIPELINE_VERSION = DEFAULT_PIPELINE_VERSION
PROCESSABLE_STATUSES = frozenset({"new", "failed", "incomplete", "downloaded", "analyzed"})


class CollectionRegistryError(RuntimeError):
    pass


class SnapshotSyncError(CollectionRegistryError):
    def __init__(self, snapshot_id: str, reason: str) -> None:
        super().__init__(reason)
        self.snapshot_id = snapshot_id
        self.reason = reason


@dataclass(frozen=True)
class RegistryItem:
    source_id: str
    job_id: str
    first_seen_at: str
    last_seen_at: str
    currently_collected: bool
    uncollected_at: str | None
    last_position: int
    status: str
    pipeline_version: str | None
    media_sha256: str | None
    job_path: str | None
    library_path: str | None
    error: str | None
    snapshot_id: str


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_id: str
    item_count: int
    next_item: RegistryItem | None


@dataclass(frozen=True)
class SnapshotHandle:
    snapshot_id: str
    resumed: bool = False


@dataclass(frozen=True)
class ProcessDecision:
    should_process: bool
    reason: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_source_id(item: Mapping[str, Any] | str) -> str:
    if isinstance(item, str):
        value = item
    else:
        value = str(item.get("aweme_id") or item.get("source_id") or "")
    value = value.strip()
    if not value:
        raise ValueError("source_id_missing")
    return value


def stable_collection_job_id(item: Mapping[str, Any] | str) -> str:
    source_id = canonical_source_id(item)
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:20]
    return f"aweme-{digest}"


def _connect(db_path: Path, *, initialize: bool = True) -> sqlite3.Connection:
    if initialize:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    if initialize:
        ensure_collection_schema(connection)
    return connection


def ensure_collection_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS collection_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            state TEXT NOT NULL CHECK (state IN ('in_progress', 'completed', 'failed')),
            item_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            pipeline_version TEXT
        );

        CREATE TABLE IF NOT EXISTS collection_items (
            source_id TEXT PRIMARY KEY,
            aweme_id TEXT,
            job_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            currently_collected INTEGER NOT NULL DEFAULT 1 CHECK (currently_collected IN (0, 1)),
            uncollected_at TEXT,
            last_position INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'new'
                CHECK (status IN (
                    'new', 'processing', 'downloaded', 'analyzed',
                    'completed', 'failed', 'incomplete'
                )),
            pipeline_version TEXT,
            media_sha256 TEXT,
            job_path TEXT,
            library_path TEXT,
            error TEXT,
            snapshot_id TEXT NOT NULL,
            FOREIGN KEY (snapshot_id) REFERENCES collection_snapshots(snapshot_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_collection_items_aweme_id
            ON collection_items(aweme_id) WHERE aweme_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_collection_items_job_id
            ON collection_items(job_id);

        CREATE TABLE IF NOT EXISTS collection_snapshot_items (
            snapshot_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            aweme_id TEXT,
            position INTEGER NOT NULL,
            media_sha256 TEXT,
            PRIMARY KEY (snapshot_id, source_id),
            UNIQUE (snapshot_id, position),
            FOREIGN KEY (snapshot_id) REFERENCES collection_snapshots(snapshot_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshot_aweme_id
            ON collection_snapshot_items(snapshot_id, aweme_id)
            WHERE aweme_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS collection_snapshot_pages (
            snapshot_id TEXT NOT NULL,
            cursor TEXT NOT NULL,
            next_cursor TEXT,
            has_more INTEGER NOT NULL CHECK (has_more IN (0, 1)),
            PRIMARY KEY (snapshot_id, cursor),
            FOREIGN KEY (snapshot_id) REFERENCES collection_snapshots(snapshot_id)
        );
        """
    )
    snapshot_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(collection_snapshots)")
    }
    if "pipeline_version" not in snapshot_columns:
        connection.execute("ALTER TABLE collection_snapshots ADD COLUMN pipeline_version TEXT")
    connection.commit()


def _row_to_item(row: sqlite3.Row) -> RegistryItem:
    return RegistryItem(
        source_id=row["source_id"],
        job_id=row["job_id"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        currently_collected=bool(row["currently_collected"]),
        uncollected_at=row["uncollected_at"],
        last_position=row["last_position"],
        status=row["status"],
        pipeline_version=row["pipeline_version"],
        media_sha256=row["media_sha256"],
        job_path=row["job_path"],
        library_path=row["library_path"],
        error=row["error"],
        snapshot_id=row["snapshot_id"],
    )


def _safe_error(exc: BaseException) -> str:
    return type(exc).__name__


class CollectionRegistry:
    def __init__(self, db_path: Path, *, root: Path | None = None) -> None:
        self.db_path = db_path
        self.root = (root or db_path.parent.parent).resolve()
        with _connect(self.db_path):
            pass

    def begin_snapshot(
        self,
        *,
        snapshot_id: str | None = None,
        pipeline_version: str | None = None,
    ) -> str:
        value = snapshot_id or uuid.uuid4().hex
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO collection_snapshots(
                    snapshot_id, started_at, state, pipeline_version
                ) VALUES (?, ?, 'in_progress', ?)
                """,
                (value, utc_now(), pipeline_version),
            )
        return value

    def record_snapshot_page(
        self,
        snapshot_id: str,
        items: list[Mapping[str, Any]],
    ) -> int:
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = connection.execute(
                "SELECT state FROM collection_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise CollectionRegistryError("snapshot_not_found")
            if snapshot["state"] != "in_progress":
                raise CollectionRegistryError("snapshot_not_open")
            next_position = connection.execute(
                """
                SELECT COALESCE(MAX(position), 0) + 1
                FROM collection_snapshot_items WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()[0]
            inserted = 0
            for raw_item in items:
                source_id = canonical_source_id(raw_item)
                aweme_id = str(raw_item.get("aweme_id") or "").strip() or None
                media_sha256 = str(raw_item.get("media_sha256") or "").strip() or None
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO collection_snapshot_items(
                        snapshot_id, source_id, aweme_id, position, media_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (snapshot_id, source_id, aweme_id, next_position, media_sha256),
                )
                if cursor.rowcount:
                    inserted += 1
                    next_position += 1
            connection.execute(
                """
                UPDATE collection_snapshots
                SET item_count = (
                    SELECT COUNT(*) FROM collection_snapshot_items WHERE snapshot_id = ?
                )
                WHERE snapshot_id = ?
                """,
                (snapshot_id, snapshot_id),
            )
        return inserted

    def fail_snapshot(self, snapshot_id: str, error: str) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE collection_snapshots
                SET state = 'failed', completed_at = ?, error = ?
                WHERE snapshot_id = ? AND state = 'in_progress'
                """,
                (utc_now(), error, snapshot_id),
            )

    def complete_snapshot(self, snapshot_id: str, *, pipeline_version: str) -> int:
        now = utc_now()
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = connection.execute(
                "SELECT state FROM collection_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise CollectionRegistryError("snapshot_not_found")
            if snapshot["state"] == "completed":
                return connection.execute(
                    "SELECT item_count FROM collection_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()[0]
            if snapshot["state"] != "in_progress":
                raise CollectionRegistryError("snapshot_not_open")

            rows = connection.execute(
                """
                SELECT source_id, aweme_id, position, media_sha256
                FROM collection_snapshot_items
                WHERE snapshot_id = ? ORDER BY position
                """,
                (snapshot_id,),
            ).fetchall()
            for row in rows:
                existing = connection.execute(
                    "SELECT * FROM collection_items WHERE source_id = ?",
                    (row["source_id"],),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO collection_items(
                            source_id, aweme_id, job_id, first_seen_at, last_seen_at,
                            currently_collected, uncollected_at, last_position, status,
                            pipeline_version, media_sha256, snapshot_id
                        ) VALUES (?, ?, ?, ?, ?, 1, NULL, ?, 'new', ?, ?, ?)
                        """,
                        (
                            row["source_id"],
                            row["aweme_id"],
                            stable_collection_job_id(row["source_id"]),
                            now,
                            now,
                            row["position"],
                            pipeline_version,
                            row["media_sha256"],
                            snapshot_id,
                        ),
                    )
                    continue

                status = existing["status"]
                error = existing["error"]
                old_hash = existing["media_sha256"]
                new_hash = row["media_sha256"] or old_hash
                version_changed = existing["pipeline_version"] != pipeline_version
                hash_changed = bool(row["media_sha256"] and row["media_sha256"] != old_hash)
                if status == "completed" and (version_changed or hash_changed):
                    status = "new"
                    error = None
                connection.execute(
                    """
                    UPDATE collection_items
                    SET aweme_id = COALESCE(?, aweme_id), last_seen_at = ?,
                        currently_collected = 1, uncollected_at = NULL, last_position = ?,
                        status = ?, pipeline_version = ?, media_sha256 = ?, error = ?,
                        snapshot_id = ?
                    WHERE source_id = ?
                    """,
                    (
                        row["aweme_id"],
                        now,
                        row["position"],
                        status,
                        pipeline_version,
                        new_hash,
                        error,
                        snapshot_id,
                        row["source_id"],
                    ),
                )

            connection.execute(
                """
                UPDATE collection_items
                SET currently_collected = 0, uncollected_at = COALESCE(uncollected_at, ?)
                WHERE currently_collected = 1
                  AND source_id NOT IN (
                      SELECT source_id FROM collection_snapshot_items WHERE snapshot_id = ?
                  )
                """,
                (now, snapshot_id),
            )
            connection.execute(
                """
                UPDATE collection_snapshots
                SET state = 'completed', completed_at = ?, error = NULL, item_count = ?
                WHERE snapshot_id = ?
                """,
                (now, len(rows), snapshot_id),
            )
        return len(rows)

    def get(self, source: Mapping[str, Any] | str) -> RegistryItem | None:
        source_id = canonical_source_id(source)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM collection_items WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        return _row_to_item(row) if row is not None else None

    def snapshot_state(self, snapshot_id: str) -> str | None:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT state FROM collection_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return row[0] if row is not None else None

    def _resolve_artifact(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def _artifacts_valid(self, item: RegistryItem) -> bool:
        job_path = self._resolve_artifact(item.job_path)
        library_path = self._resolve_artifact(item.library_path)
        if job_path is None or library_path is None or not library_path.exists():
            return False
        source = job_path / "source.mp4" if job_path.is_dir() else job_path
        if not source.is_file() or source.stat().st_size <= 0:
            return False
        if item.media_sha256 and _file_sha256(source) != item.media_sha256:
            return False
        return True

    def should_process(
        self,
        item: RegistryItem,
        *,
        pipeline_version: str,
        artifacts_valid: Callable[[RegistryItem], bool] | None = None,
    ) -> bool:
        if not item.currently_collected:
            return False
        if item.status in PROCESSABLE_STATUSES or item.status == "processing":
            return True
        if item.status != "completed" or item.pipeline_version != pipeline_version:
            return True
        validator = artifacts_valid or self._artifacts_valid
        return not validator(item)

    def next_item(
        self,
        snapshot_id: str,
        *,
        pipeline_version: str,
        artifacts_valid: Callable[[RegistryItem], bool] | None = None,
    ) -> RegistryItem | None:
        with _connect(self.db_path) as connection:
            state = connection.execute(
                "SELECT state FROM collection_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if state is None or state[0] != "completed":
                raise CollectionRegistryError("complete_snapshot_required")
            rows = connection.execute(
                """
                SELECT item.* FROM collection_snapshot_items AS snapshot_item
                JOIN collection_items AS item USING (source_id)
                WHERE snapshot_item.snapshot_id = ?
                ORDER BY snapshot_item.position
                """,
                (snapshot_id,),
            ).fetchall()
        for row in rows:
            item = _row_to_item(row)
            if self.should_process(
                item,
                pipeline_version=pipeline_version,
                artifacts_valid=artifacts_valid,
            ):
                return item
        return None

    def mark_processing(self, source: Mapping[str, Any] | str) -> None:
        self._mark_status(source, "processing", error=None)

    def mark_failed(self, source: Mapping[str, Any] | str, *, error: str) -> None:
        self._mark_status(source, "failed", error=error)

    def mark_incomplete(self, source: Mapping[str, Any] | str, *, error: str) -> None:
        self._mark_status(source, "incomplete", error=error)

    def _mark_status(
        self,
        source: Mapping[str, Any] | str,
        status: str,
        *,
        error: str | None,
    ) -> None:
        source_id = canonical_source_id(source)
        with _connect(self.db_path) as connection:
            changed = connection.execute(
                "UPDATE collection_items SET status = ?, error = ? WHERE source_id = ?",
                (status, error, source_id),
            ).rowcount
        if not changed:
            raise CollectionRegistryError("source_not_found")

    def mark_completed(
        self,
        source: Mapping[str, Any] | str,
        *,
        pipeline_version: str,
        media_sha256: str,
        job_path: Path,
        library_path: Path,
    ) -> None:
        source_id = canonical_source_id(source)
        with _connect(self.db_path) as connection:
            changed = connection.execute(
                """
                UPDATE collection_items
                SET status = 'completed', pipeline_version = ?, media_sha256 = ?,
                    job_path = ?, library_path = ?, error = NULL
                WHERE source_id = ?
                """,
                (
                    pipeline_version,
                    media_sha256,
                    self._stored_path(job_path),
                    self._stored_path(library_path),
                    source_id,
                ),
            ).rowcount
        if not changed:
            raise CollectionRegistryError("source_not_found")

    def _stored_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            return str(resolved)

    def counts(self) -> dict[str, Any]:
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM collection_items GROUP BY status ORDER BY status"
            ).fetchall()
            current = connection.execute(
                "SELECT COUNT(*) FROM collection_items WHERE currently_collected = 1"
            ).fetchone()[0]
            total = connection.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0]
        return {
            "total": total,
            "currently_collected": current,
            "by_status": {row[0]: row[1] for row in rows},
        }


def begin_snapshot(
    db_path: Path,
    *,
    pipeline_version: str = PIPELINE_VERSION,
) -> SnapshotHandle:
    registry = CollectionRegistry(db_path)
    snapshot_id = registry.begin_snapshot(pipeline_version=pipeline_version)
    return SnapshotHandle(snapshot_id=snapshot_id)


def ingest_snapshot_page(
    db_path: Path,
    *,
    snapshot_id: str,
    cursor: int | str,
    next_cursor: int | str | None,
    has_more: bool,
    items: list[Mapping[str, Any]],
) -> int:
    CollectionRegistry(db_path)
    cursor_key = str(cursor)
    expected = (None if next_cursor is None else str(next_cursor), int(has_more))
    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        snapshot = connection.execute(
            "SELECT state FROM collection_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if snapshot is None:
            raise CollectionRegistryError("snapshot_not_found")
        if snapshot["state"] != "in_progress":
            raise CollectionRegistryError("snapshot_not_open")
        existing = connection.execute(
            """
            SELECT next_cursor, has_more FROM collection_snapshot_pages
            WHERE snapshot_id = ? AND cursor = ?
            """,
            (snapshot_id, cursor_key),
        ).fetchone()
        if existing is not None:
            if (existing["next_cursor"], existing["has_more"]) != expected:
                raise CollectionRegistryError("snapshot_page_conflict")
            return 0
        previous = connection.execute(
            """
            SELECT next_cursor, has_more FROM collection_snapshot_pages
            WHERE snapshot_id = ? ORDER BY rowid DESC LIMIT 1
            """,
            (snapshot_id,),
        ).fetchone()
        if previous is None and cursor_key != "0":
            raise CollectionRegistryError("snapshot_must_start_at_zero")
        if previous is not None:
            if not previous["has_more"]:
                raise CollectionRegistryError("snapshot_already_has_last_page")
            if previous["next_cursor"] != cursor_key:
                raise CollectionRegistryError("snapshot_cursor_discontinuity")

        next_position = connection.execute(
            """
            SELECT COALESCE(MAX(position), 0) + 1
            FROM collection_snapshot_items WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()[0]
        inserted = 0
        for raw_item in items:
            source_id = canonical_source_id(raw_item)
            aweme_id = str(raw_item.get("aweme_id") or "").strip() or None
            media_sha256 = str(raw_item.get("media_sha256") or "").strip() or None
            result = connection.execute(
                """
                INSERT OR IGNORE INTO collection_snapshot_items(
                    snapshot_id, source_id, aweme_id, position, media_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (snapshot_id, source_id, aweme_id, next_position, media_sha256),
            )
            if result.rowcount:
                inserted += 1
                next_position += 1
        connection.execute(
            """
            INSERT INTO collection_snapshot_pages(
                snapshot_id, cursor, next_cursor, has_more
            ) VALUES (?, ?, ?, ?)
            """,
            (snapshot_id, cursor_key, expected[0], expected[1]),
        )
        connection.execute(
            """
            UPDATE collection_snapshots
            SET item_count = (
                SELECT COUNT(*) FROM collection_snapshot_items WHERE snapshot_id = ?
            )
            WHERE snapshot_id = ?
            """,
            (snapshot_id, snapshot_id),
        )
    return inserted


def complete_snapshot(db_path: Path, snapshot_id: str) -> int:
    registry = CollectionRegistry(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT pipeline_version FROM collection_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        pages = connection.execute(
            """
            SELECT cursor, has_more FROM collection_snapshot_pages
            WHERE snapshot_id = ? ORDER BY rowid
            """,
            (snapshot_id,),
        ).fetchall()
    if row is None:
        raise CollectionRegistryError("snapshot_not_found")
    if not pages or pages[0]["cursor"] != "0" or pages[-1]["has_more"]:
        raise CollectionRegistryError("snapshot_incomplete")
    return registry.complete_snapshot(
        snapshot_id,
        pipeline_version=row[0] or PIPELINE_VERSION,
    )


def pause_snapshot(db_path: Path, snapshot_id: str, error: str) -> None:
    CollectionRegistry(db_path).fail_snapshot(snapshot_id, error)


def stable_job_id_for_source(source_id: str) -> str:
    return stable_collection_job_id(source_id)


def claim_item(
    db_path: Path,
    source_id: str,
    *,
    pipeline_version: str,
    observed_media_sha256: str = "",
) -> ProcessDecision:
    registry = CollectionRegistry(db_path)
    item = registry.get(source_id)
    if item is None or not item.currently_collected:
        return ProcessDecision(False, "source_not_currently_collected")
    if item.status == "completed" and item.pipeline_version == pipeline_version:
        if not observed_media_sha256:
            registry.mark_incomplete(source_id, error="media_missing")
            return ProcessDecision(True, "media_missing")
        if item.media_sha256 != observed_media_sha256:
            registry.mark_incomplete(source_id, error="media_hash_changed")
            return ProcessDecision(True, "media_hash_changed")
        if registry._artifacts_valid(item):
            return ProcessDecision(False, "completed_current")
        registry.mark_incomplete(source_id, error="artifacts_invalid")
        return ProcessDecision(True, "artifacts_invalid")
    if item.pipeline_version != pipeline_version:
        registry._mark_status(source_id, "new", error=None)
        return ProcessDecision(True, "pipeline_version_changed")
    reason = "retry" if item.status in {"failed", "incomplete"} else item.status
    registry.mark_processing(source_id)
    return ProcessDecision(True, reason)


def update_item_by_job(
    db_path: Path,
    job_id: str | None,
    *,
    status: str,
    media_sha256: str | None = None,
    job_path: Path | str | None = None,
    library_path: Path | str | None = None,
    error: str | None = None,
    preserve_completed: bool = False,
) -> bool:
    if not job_id or not db_path.exists():
        return False
    allowed_statuses = {
        "new",
        "processing",
        "downloaded",
        "analyzed",
        "completed",
        "failed",
        "incomplete",
    }
    if status not in allowed_statuses:
        raise ValueError("invalid_collection_status")
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None or (preserve_completed and row["status"] == "completed"):
            return False
        updates = ["status = ?", "error = ?"]
        values: list[Any] = [status, error]
        if media_sha256 is not None:
            updates.append("media_sha256 = ?")
            values.append(media_sha256)
        if job_path is not None:
            updates.append("job_path = ?")
            values.append(str(job_path))
        if library_path is not None:
            updates.append("library_path = ?")
            values.append(str(library_path))
        if status == "completed":
            updates.append("pipeline_version = COALESCE(pipeline_version, ?)")
            values.append(PIPELINE_VERSION)
        values.append(job_id)
        connection.execute(
            f"UPDATE collection_items SET {', '.join(updates)} WHERE job_id = ?",
            values,
        )
    return True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def synchronize_collection(
    registry: CollectionRegistry,
    fetch_page: Callable[[int | str], Mapping[str, Any]],
    *,
    pipeline_version: str = DEFAULT_PIPELINE_VERSION,
    max_pages: int = 10_000,
    artifacts_valid: Callable[[RegistryItem], bool] | None = None,
) -> SnapshotResult:
    snapshot_id = registry.begin_snapshot(pipeline_version=pipeline_version)
    cursor: int | str = 0
    seen_cursors: set[str] = set()
    try:
        for _page_number in range(max_pages):
            cursor_key = json.dumps(cursor, ensure_ascii=False, sort_keys=True)
            if cursor_key in seen_cursors:
                raise CollectionRegistryError("cursor_cycle")
            seen_cursors.add(cursor_key)
            page = fetch_page(cursor)
            if not isinstance(page, Mapping):
                raise CollectionRegistryError("collect_page_invalid")
            if page.get("status_code") not in (None, 0, "0"):
                raise CollectionRegistryError("collect_page_denied")
            raw_items = page.get("aweme_list")
            if not isinstance(raw_items, list):
                raise CollectionRegistryError("collect_page_invalid")
            has_more = bool(page.get("has_more"))
            next_cursor = page.get("cursor")
            if has_more and (next_cursor is None or next_cursor == cursor):
                raise CollectionRegistryError("cursor_not_advanced")
            ingest_snapshot_page(
                registry.db_path,
                snapshot_id=snapshot_id,
                cursor=cursor,
                next_cursor=next_cursor,
                has_more=has_more,
                items=[item for item in raw_items if isinstance(item, Mapping)],
            )
            if not has_more:
                break
            cursor = next_cursor
        else:
            raise CollectionRegistryError("page_limit_exceeded")
        item_count = complete_snapshot(registry.db_path, snapshot_id)
    except BaseException as exc:
        registry.fail_snapshot(snapshot_id, _safe_error(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        reason = str(exc) if isinstance(exc, CollectionRegistryError) else _safe_error(exc)
        raise SnapshotSyncError(snapshot_id, reason) from exc

    return SnapshotResult(
        snapshot_id=snapshot_id,
        item_count=item_count,
        next_item=registry.next_item(
            snapshot_id,
            pipeline_version=pipeline_version,
            artifacts_valid=artifacts_valid,
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local dynamic collection registry")
    parser.add_argument("command", choices=("init", "status", "next", "sync-json"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--db", type=Path)
    parser.add_argument("--snapshot-id")
    parser.add_argument("--pipeline-version", default=DEFAULT_PIPELINE_VERSION)
    parser.add_argument("--pages-json", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    db_path = (args.db or root / "data" / "knowledge.db").resolve()
    registry = CollectionRegistry(db_path, root=root)
    if args.command == "init":
        result: dict[str, Any] = {"status": "ok"}
    elif args.command == "status":
        result = registry.counts()
    elif args.command == "sync-json":
        if args.pages_json is None:
            print(json.dumps({"status": "controlled_failure", "reason": "pages_required"}))
            return 2
        try:
            pages = json.loads(args.pages_json.read_text(encoding="utf-8"))
            if not isinstance(pages, list):
                raise ValueError("pages_must_be_list")
            index = 0

            def fetch_page(_cursor: int | str) -> Mapping[str, Any]:
                nonlocal index
                value = pages[index]
                index += 1
                if not isinstance(value, Mapping):
                    raise ValueError("page_must_be_object")
                return value

            synced = synchronize_collection(
                registry,
                fetch_page,
                pipeline_version=args.pipeline_version,
                max_pages=len(pages),
            )
        except (OSError, ValueError, SnapshotSyncError) as exc:
            print(
                json.dumps(
                    {"status": "controlled_failure", "reason": _safe_error(exc)},
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        result = {
            "status": "ok",
            "snapshot_id": synced.snapshot_id,
            "item_count": synced.item_count,
            "has_next": synced.next_item is not None,
            **({"position": synced.next_item.last_position} if synced.next_item else {}),
        }
    else:
        if not args.snapshot_id:
            print(json.dumps({"status": "controlled_failure", "reason": "snapshot_required"}))
            return 2
        try:
            item = registry.next_item(
                args.snapshot_id,
                pipeline_version=args.pipeline_version,
            )
        except CollectionRegistryError as exc:
            print(
                json.dumps({"status": "controlled_failure", "reason": str(exc)}),
                file=sys.stderr,
            )
            return 2
        result = {
            "status": "ok",
            "has_next": item is not None,
            **({"position": item.last_position} if item else {}),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
