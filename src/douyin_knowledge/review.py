from __future__ import annotations

import hashlib
import sqlite3
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
    _path, candidate_digest = _candidate(root, job_ref)
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
