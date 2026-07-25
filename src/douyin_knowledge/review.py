from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from douyin_knowledge.contracts import CliError

REVIEW_TABLE = """
CREATE TABLE IF NOT EXISTS review_records (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_ref TEXT NOT NULL,
    candidate_sha256 TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    note_sha256 TEXT NOT NULL,
    private_note TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
)
"""


def _connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute(REVIEW_TABLE)
    schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'review_records'"
    ).fetchone()
    normalized_schema = "".join(str(schema[0]).split()) if schema is not None else ""
    if "UNIQUE(job_ref" in normalized_schema:
        connection.executescript(
            """
            ALTER TABLE review_records RENAME TO review_records_legacy;
            CREATE TABLE review_records (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_ref TEXT NOT NULL,
                candidate_sha256 TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
                note_sha256 TEXT NOT NULL,
                private_note TEXT NOT NULL,
                reviewed_at TEXT NOT NULL
            );
            INSERT INTO review_records(
                review_id, job_ref, candidate_sha256, decision,
                note_sha256, private_note, reviewed_at
            )
            SELECT review_id, job_ref, candidate_sha256, decision,
                   note_sha256, private_note, reviewed_at
            FROM review_records_legacy ORDER BY review_id;
            DROP TABLE review_records_legacy;
            """
        )
    return connection


def _candidate(root: Path, job_ref: str) -> tuple[Path, str]:
    path = root / "data" / "tasks" / job_ref / "semantic-v1" / "candidate-v1.json"
    if not path.is_file():
        raise CliError(
            "candidate_not_staged",
            "the job has no staged candidate to review",
            "import a validated candidate before recording review",
        )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_is_current(root: Path, job_ref: str) -> bool:
    candidate_path = root / "data" / "tasks" / job_ref / "semantic-v1" / "candidate-v1.json"
    manifest_path = root / "data" / "tasks" / job_ref / "semantic-v1" / "protocol-manifest.json"
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(candidate, dict)
        and isinstance(manifest, dict)
        and candidate.get("job_ref") == job_ref
        and candidate.get("packet_sha256")
        and candidate.get("packet_sha256") == manifest.get("packet_sha256")
    )


def require_current_candidate(root: Path, job_ref: str) -> tuple[Path, str]:
    candidate = _candidate(root, job_ref)
    if not candidate_is_current(root, job_ref):
        raise CliError(
            "candidate_stale",
            "the staged candidate does not match the current semantic packet",
            "regenerate and import a candidate from the current packet before review or publish",
        )
    return candidate


def latest_candidate_decision(
    database: Path, *, job_ref: str, candidate_sha256: str
) -> str | None:
    if not database.is_file():
        return None
    with _connect(database) as connection:
        row = connection.execute(
            "SELECT decision FROM review_records "
            "WHERE job_ref = ? AND candidate_sha256 = ? "
            "ORDER BY review_id DESC LIMIT 1",
            (job_ref, candidate_sha256),
        ).fetchone()
    return None if row is None else str(row["decision"])


def latest_job_review(database: Path, *, job_ref: str) -> tuple[str, str] | None:
    if not database.is_file():
        return None
    with _connect(database) as connection:
        row = connection.execute(
            "SELECT candidate_sha256, decision FROM review_records "
            "WHERE job_ref = ? ORDER BY review_id DESC LIMIT 1",
            (job_ref,),
        ).fetchone()
    if row is None:
        return None
    return str(row["candidate_sha256"]), str(row["decision"])


def _archive_candidate(root: Path, job_ref: str, path: Path, digest: str) -> None:
    destination = root / "quarantine" / "candidates" / job_ref / f"{digest}.json"
    if destination.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(path.read_bytes())
        os.replace(temporary, destination)
    except OSError as exc:
        raise CliError(
            "candidate_history_unavailable",
            "the reviewed candidate could not be preserved for audit",
            "correct private storage access before recording the review",
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def record_review(
    root: Path,
    database: Path,
    *,
    job_ref: str,
    decision: str,
    note: str,
) -> dict[str, Any]:
    if decision not in {"approve", "reject"}:
        raise CliError(
            "review_decision_invalid",
            "review decision must be approve or reject",
            "choose one supported review decision",
        )
    _path, candidate_digest = require_current_candidate(root, job_ref)
    _archive_candidate(root, job_ref, _path, candidate_digest)
    normalized_note = " ".join(note.split())[:2000]
    note_digest = hashlib.sha256(normalized_note.encode("utf-8")).hexdigest()
    with _connect(database) as connection:
        latest = connection.execute(
            "SELECT decision, note_sha256 FROM review_records "
            "WHERE job_ref = ? AND candidate_sha256 = ? "
            "ORDER BY review_id DESC LIMIT 1",
            (job_ref, candidate_digest),
        ).fetchone()
        reused = bool(
            latest is not None
            and str(latest["decision"]) == decision
            and str(latest["note_sha256"]) == note_digest
        )
        if reused:
            return {"job_ref": job_ref, "decision": decision, "reused": True}
        connection.execute(
            "INSERT INTO review_records("
            "job_ref, candidate_sha256, decision, note_sha256, private_note, reviewed_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_ref,
                candidate_digest,
                decision,
                note_digest,
                normalized_note,
                datetime.now(UTC).isoformat(),
            ),
        )
    return {"job_ref": job_ref, "decision": decision, "reused": False}


def list_reviews(database: Path, *, job_ref: str | None = None) -> list[dict[str, str]]:
    if not database.is_file():
        return []
    with _connect(database) as connection:
        condition = "WHERE job_ref = ?" if job_ref else ""
        parameters = (job_ref,) if job_ref else ()
        rows = connection.execute(
            "SELECT job_ref, decision FROM review_records "
            f"{condition} ORDER BY review_id",
            parameters,
        ).fetchall()
    return [
        {"job_ref": str(row["job_ref"]), "decision": str(row["decision"])}
        for row in rows
    ]


def approved_candidate(root: Path, database: Path, job_ref: str) -> bool:
    _path, candidate_digest = _candidate(root, job_ref)
    if not candidate_is_current(root, job_ref):
        return False
    if not database.is_file():
        return False
    with _connect(database) as connection:
        row = connection.execute(
            "SELECT decision FROM review_records "
            "WHERE job_ref = ? AND candidate_sha256 = ? "
            "ORDER BY review_id DESC LIMIT 1",
            (job_ref, candidate_digest),
        ).fetchone()
    return row is not None and str(row["decision"]) == "approve"
