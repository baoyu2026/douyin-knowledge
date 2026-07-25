from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

REQUIRED_ACCEPTANCE_CHECKS = frozenset({"sqlite_integrity", "privacy", "content"})


class PublicationStateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS publication_schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS publication_sagas (
            saga_id TEXT PRIMARY KEY,
            job_ref TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            draft_sha256 TEXT NOT NULL,
            media_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('intent', 'published_unaccepted', 'accepted')
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS publication_sagas_job_ref
            ON publication_sagas(job_ref, created_at DESC);

        CREATE TABLE IF NOT EXISTS publication_targets (
            saga_id TEXT NOT NULL,
            target_name TEXT NOT NULL,
            relative_handle TEXT NOT NULL,
            expected_sha256 TEXT NOT NULL,
            observed_sha256 TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('planned', 'pending', 'missing', 'mismatch', 'verified')
            ),
            observed_at TEXT,
            PRIMARY KEY (saga_id, target_name),
            FOREIGN KEY (saga_id) REFERENCES publication_sagas(saga_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS publication_acceptances (
            saga_id TEXT PRIMARY KEY,
            checks_json TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            FOREIGN KEY (saga_id) REFERENCES publication_sagas(saga_id)
                ON DELETE CASCADE
        );
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO publication_schema_migrations(version, applied_at) "
        "VALUES (1, ?)",
        (_now(),),
    )


def _validate_sha256(value: str, *, field: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise PublicationStateError("invalid_digest", f"{field} must be a SHA-256 digest")
    return normalized


def _relative_handle(value: str) -> str:
    raw = value.replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or path.drive
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PublicationStateError(
            "unsafe_target_handle", "publication target must be a relative handle"
        )
    return path.as_posix()


def _request_hash(
    *,
    job_ref: str,
    draft_sha256: str,
    media_sha256: str,
    targets: dict[str, tuple[str, str | None]],
) -> str:
    canonical = {
        "job_ref": job_ref,
        "draft_sha256": draft_sha256,
        "media_sha256": media_sha256,
        "targets": {
            name: {"handle": handle, "sha256": digest or None}
            for name, (handle, digest) in sorted(targets.items())
        },
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _target_states(connection: sqlite3.Connection, saga_id: str) -> dict[str, str]:
    rows = connection.execute(
        "SELECT target_name, status FROM publication_targets "
        "WHERE saga_id = ? ORDER BY target_name",
        (saga_id,),
    ).fetchall()
    return {str(row["target_name"]): str(row["status"]) for row in rows}


def _serialize_saga(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    reused: bool = False,
) -> dict[str, Any]:
    return {
        "saga_id": str(row["saga_id"]),
        "job_ref": str(row["job_ref"]),
        "state": str(row["state"]),
        "targets": _target_states(connection, str(row["saga_id"])),
        "reused": reused,
    }


def begin_publication(
    root: Path,
    database: Path,
    *,
    job_ref: str,
    idempotency_key: str,
    draft_sha256: str,
    media_sha256: str,
    targets: dict[str, tuple[str, str | None]],
) -> dict[str, Any]:
    root = root.resolve()
    if not job_ref.strip() or not idempotency_key.strip() or not targets:
        raise PublicationStateError(
            "publication_request_invalid", "job, idempotency key, and targets are required"
        )
    normalized_targets: dict[str, tuple[str, str | None]] = {}
    for name, target in targets.items():
        if not name.strip() or len(target) != 2:
            raise PublicationStateError(
                "publication_request_invalid", "each publication target must be named"
            )
        handle = _relative_handle(str(target[0]))
        resolved = (root / Path(handle)).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PublicationStateError(
                "unsafe_target_handle", "publication target escapes the instance root"
            ) from exc
        expected = target[1]
        normalized_targets[name] = (
            handle,
            _validate_sha256(str(expected), field=f"targets.{name}")
            if expected is not None
            else None,
        )
    draft_digest = _validate_sha256(draft_sha256, field="draft_sha256")
    media_digest = _validate_sha256(media_sha256, field="media_sha256")
    request_digest = _request_hash(
        job_ref=job_ref,
        draft_sha256=draft_digest,
        media_sha256=media_digest,
        targets=normalized_targets,
    )
    timestamp = _now()
    with _connect(database) as connection:
        registry_item = connection.execute(
            "SELECT 1 FROM collection_items WHERE job_id = ?", (job_ref,)
        ).fetchone()
        if registry_item is None:
            raise PublicationStateError(
                "registry_item_missing", "the publication job is missing from the registry"
            )
        existing = connection.execute(
            "SELECT * FROM publication_sagas WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if str(existing["request_sha256"]) != request_digest:
                rows = connection.execute(
                    "SELECT target_name, relative_handle, expected_sha256, status "
                    "FROM publication_targets WHERE saga_id = ? ORDER BY target_name",
                    (str(existing["saga_id"]),),
                ).fetchall()
                targets_by_name = {str(row["target_name"]): row for row in rows}
                can_retarget = (
                    str(existing["state"]) == "intent"
                    and str(existing["job_ref"]) == job_ref
                    and str(existing["draft_sha256"]) == draft_digest
                    and str(existing["media_sha256"]) == media_digest
                    and set(targets_by_name) == set(normalized_targets)
                    and all(digest is None for _handle, digest in normalized_targets.values())
                )
                changed: list[tuple[str, str]] = []
                if can_retarget:
                    for name, (handle, _digest_value) in normalized_targets.items():
                        row = targets_by_name[name]
                        if str(row["relative_handle"]) == handle:
                            continue
                        if str(row["status"]) == "verified":
                            can_retarget = False
                            break
                        changed.append((name, handle))
                if not can_retarget or not changed:
                    raise PublicationStateError(
                        "idempotency_conflict",
                        "the idempotency key was already used for a different request",
                    )
                for name, handle in changed:
                    row = targets_by_name[name]
                    status = "pending" if str(row["expected_sha256"] or "") else "planned"
                    connection.execute(
                        "UPDATE publication_targets SET relative_handle = ?, status = ?, "
                        "observed_sha256 = NULL, observed_at = NULL "
                        "WHERE saga_id = ? AND target_name = ?",
                        (handle, status, str(existing["saga_id"]), name),
                    )
                connection.execute(
                    "UPDATE publication_sagas SET request_sha256 = ?, updated_at = ? "
                    "WHERE saga_id = ?",
                    (request_digest, timestamp, str(existing["saga_id"])),
                )
                existing = connection.execute(
                    "SELECT * FROM publication_sagas WHERE saga_id = ?",
                    (str(existing["saga_id"]),),
                ).fetchone()
                assert existing is not None
            return _serialize_saga(connection, existing, reused=True)
        saga_id = f"pub-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO publication_sagas("
            "saga_id, job_ref, idempotency_key, request_sha256, draft_sha256, "
            "media_sha256, state, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, 'intent', ?, ?)",
            (
                saga_id,
                job_ref,
                idempotency_key,
                request_digest,
                draft_digest,
                media_digest,
                timestamp,
                timestamp,
            ),
        )
        connection.executemany(
            "INSERT INTO publication_targets("
            "saga_id, target_name, relative_handle, expected_sha256, status"
            ") VALUES (?, ?, ?, ?, ?)",
            [
                (saga_id, name, handle, digest or "", "pending" if digest else "planned")
                for name, (handle, digest) in sorted(normalized_targets.items())
            ],
        )
        row = connection.execute(
            "SELECT * FROM publication_sagas WHERE saga_id = ?", (saga_id,)
        ).fetchone()
        assert row is not None
        return _serialize_saga(connection, row)


def _path_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    hasher = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    if not path.is_dir():
        return None
    files = sorted(item for item in path.rglob("*") if item.is_file())
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        hasher.update(len(relative).to_bytes(8, "big"))
        hasher.update(relative)
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


def _target_path(root: Path, handle: str) -> Path:
    relative = PurePosixPath(_relative_handle(handle))
    if relative.parts[0] != "vault":
        candidate = (root / Path(relative.as_posix())).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise PublicationStateError(
                "unsafe_target_handle", "publication target escapes the instance root"
            ) from exc
        return candidate
    vault_root = root / "vault"
    config = root / "config" / "obsidian.yml"
    if config.is_file():
        try:
            payload = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
            configured = payload.get("vault") if isinstance(payload, dict) else None
            if isinstance(configured, str) and configured.strip():
                vault_root = Path(configured).expanduser()
                if not vault_root.is_absolute():
                    vault_root = root / vault_root
                vault_root = vault_root.resolve()
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise PublicationStateError(
                "vault_config_invalid", "the configured vault path could not be read"
            ) from exc
    candidate = (vault_root / Path(*relative.parts[1:])).resolve()
    try:
        candidate.relative_to(vault_root.resolve())
    except ValueError as exc:
        raise PublicationStateError(
            "unsafe_target_handle", "publication target escapes the configured vault"
        ) from exc
    return candidate


def seal_publication_targets(
    database: Path,
    saga_id: str,
    *,
    expected: dict[str, str],
) -> dict[str, Any]:
    normalized = {
        name: _validate_sha256(digest, field=f"targets.{name}")
        for name, digest in expected.items()
    }
    with _connect(database) as connection:
        saga = connection.execute(
            "SELECT * FROM publication_sagas WHERE saga_id = ?", (saga_id,)
        ).fetchone()
        if saga is None:
            raise PublicationStateError(
                "publication_missing", "the publication transaction does not exist"
            )
        rows = connection.execute(
            "SELECT target_name, expected_sha256, status FROM publication_targets "
            "WHERE saga_id = ? ORDER BY target_name",
            (saga_id,),
        ).fetchall()
        names = {str(row["target_name"]) for row in rows}
        if names != set(normalized):
            raise PublicationStateError(
                "publication_targets_mismatch",
                "sealed targets must exactly match the publication intent",
            )
        for row in rows:
            name = str(row["target_name"])
            existing = str(row["expected_sha256"] or "")
            if existing and existing != normalized[name]:
                if str(saga["state"]) != "intent" or str(row["status"]) == "verified":
                    raise PublicationStateError(
                        "publication_digest_conflict",
                        "a publication target was already sealed with another digest",
                    )
            connection.execute(
                "UPDATE publication_targets SET expected_sha256 = ?, status = 'pending', "
                "observed_sha256 = NULL, observed_at = NULL "
                "WHERE saga_id = ? AND target_name = ?",
                (normalized[name], saga_id, name),
            )
        current = connection.execute(
            "SELECT * FROM publication_sagas WHERE saga_id = ?", (saga_id,)
        ).fetchone()
        assert current is not None
        sealed_before = all(row["expected_sha256"] for row in rows)
        return _serialize_saga(connection, current, reused=sealed_before)


def reconcile_publications(
    root: Path,
    database: Path,
    *,
    job_ref: str | None = None,
) -> list[dict[str, Any]]:
    root = root.resolve()
    with _connect(database) as connection:
        if job_ref is None:
            sagas = connection.execute(
                "SELECT * FROM publication_sagas ORDER BY created_at, saga_id"
            ).fetchall()
        else:
            sagas = connection.execute(
                "SELECT * FROM publication_sagas WHERE job_ref = ? "
                "ORDER BY created_at, saga_id",
                (job_ref,),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for saga in sagas:
            saga_id = str(saga["saga_id"])
            targets = connection.execute(
                "SELECT * FROM publication_targets WHERE saga_id = ? "
                "ORDER BY target_name",
                (saga_id,),
            ).fetchall()
            for target in targets:
                handle = _relative_handle(str(target["relative_handle"]))
                candidate = _target_path(root, handle)
                expected = str(target["expected_sha256"] or "")
                observed = _path_digest(candidate)
                if not expected:
                    status = "planned"
                elif observed is None:
                    status = "missing"
                elif observed == expected:
                    status = "verified"
                else:
                    status = "mismatch"
                connection.execute(
                    "UPDATE publication_targets SET observed_sha256 = ?, status = ?, "
                    "observed_at = ? WHERE saga_id = ? AND target_name = ?",
                    (observed, status, _now(), saga_id, str(target["target_name"])),
                )
            states = _target_states(connection, saga_id)
            state = str(saga["state"])
            if state != "accepted":
                state = (
                    "published_unaccepted"
                    if states and all(value == "verified" for value in states.values())
                    else "intent"
                )
                connection.execute(
                    "UPDATE publication_sagas SET state = ?, updated_at = ? WHERE saga_id = ?",
                    (state, _now(), saga_id),
                )
            current = connection.execute(
                "SELECT * FROM publication_sagas WHERE saga_id = ?", (saga_id,)
            ).fetchone()
            assert current is not None
            results.append(_serialize_saga(connection, current))
        return results


def publication_status(database: Path, job_ref: str) -> dict[str, Any]:
    with _connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM publication_sagas WHERE job_ref = ? "
            "ORDER BY created_at DESC, saga_id DESC LIMIT 1",
            (job_ref,),
        ).fetchone()
        if row is None:
            raise PublicationStateError(
                "publication_missing", "no publication exists for this job"
            )
        return _serialize_saga(connection, row)


def accept_publication(
    database: Path,
    saga_id: str,
    *,
    checks: dict[str, bool],
) -> dict[str, Any]:
    with _connect(database) as connection:
        saga = connection.execute(
            "SELECT * FROM publication_sagas WHERE saga_id = ?", (saga_id,)
        ).fetchone()
        if saga is None:
            raise PublicationStateError(
                "publication_missing", "the publication transaction does not exist"
            )
        if str(saga["state"]) == "accepted":
            return _serialize_saga(connection, saga, reused=True)
        if str(saga["state"]) != "published_unaccepted":
            raise PublicationStateError(
                "publication_not_observed",
                "all publication targets must be observed before acceptance",
            )
        if (
            not REQUIRED_ACCEPTANCE_CHECKS.issubset(checks)
            or not all(checks[name] is True for name in REQUIRED_ACCEPTANCE_CHECKS)
            or not all(value is True for value in checks.values())
        ):
            raise PublicationStateError(
                "acceptance_checks_failed",
                "sqlite integrity, privacy, and content checks must all pass",
            )
        timestamp = _now()
        connection.execute(
            "INSERT INTO publication_acceptances(saga_id, checks_json, accepted_at) "
            "VALUES (?, ?, ?)",
            (saga_id, json.dumps(checks, sort_keys=True), timestamp),
        )
        updated = connection.execute(
            "UPDATE collection_items SET status = 'completed' WHERE job_id = ?",
            (str(saga["job_ref"]),),
        )
        if updated.rowcount != 1:
            raise PublicationStateError(
                "registry_item_missing", "the publication job is missing from the registry"
            )
        connection.execute(
            "UPDATE publication_sagas SET state = 'accepted', updated_at = ? "
            "WHERE saga_id = ?",
            (timestamp, saga_id),
        )
        current = connection.execute(
            "SELECT * FROM publication_sagas WHERE saga_id = ?", (saga_id,)
        ).fetchone()
        assert current is not None
        return _serialize_saga(connection, current)
