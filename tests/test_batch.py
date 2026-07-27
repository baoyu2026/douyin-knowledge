from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from douyin_knowledge.batch import batch_status, create_batch, fixed_plan
from douyin_knowledge.contracts import CliError

JOB_ONE = "aweme-0123456789abcdefabcd"
JOB_TWO = "aweme-fedcba9876543210abcd"


def _registry(root: Path) -> Path:
    database = root / "data" / "knowledge.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE collection_items(
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                currently_collected INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE publication_sagas(
                saga_id TEXT PRIMARY KEY,
                job_ref TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE publication_targets(
                saga_id TEXT NOT NULL,
                target_name TEXT NOT NULL,
                relative_handle TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO collection_items(job_id, status) VALUES (?, 'new')",
            [(JOB_ONE,), (JOB_TWO,)],
        )
    return database


def _planned(*job_refs: str) -> list[dict[str, object]]:
    return [
        {"job_ref": job_ref, "status": "new", "position": index}
        for index, job_ref in enumerate(job_refs, start=1)
    ]


def _current_candidate(root: Path, job_ref: str, *, imported: bool) -> None:
    semantic = root / "data" / "tasks" / job_ref / "semantic-v1"
    semantic.mkdir(parents=True)
    packet_hash = "a" * 64
    candidate = {
        "protocol_version": 1,
        "schema_version": 2,
        "job_ref": job_ref,
        "packet_sha256": packet_hash,
        "content": {},
    }
    candidate_path = semantic / "candidate-v1.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    candidate_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    manifest = {"packet_sha256": packet_hash}
    if imported:
        manifest["imported_candidate_sha256"] = candidate_hash
    (semantic / "protocol-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (semantic / "content-packet.json").write_text("{}", encoding="utf-8")


def test_batch_create_fixes_inventory_and_reports_resume_actions(tmp_path: Path) -> None:
    _registry(tmp_path)
    created = create_batch(
        tmp_path,
        planned=_planned(JOB_ONE, JOB_TWO),
        requested_status="new",
    )

    assert created["item_count"] == 2
    assert created["job_refs"] == [JOB_ONE, JOB_TWO]
    assert created["state_handle"].startswith("data/batches/batch-")

    packet = tmp_path / "data" / "tasks" / JOB_ONE / "semantic-v1"
    packet.mkdir(parents=True)
    (packet / "protocol-manifest.json").write_text(
        json.dumps({"packet_sha256": "a" * 64}), encoding="utf-8"
    )
    (packet / "content-packet.json").write_text("{}", encoding="utf-8")

    status = batch_status(tmp_path, str(created["batch_ref"]))

    assert status["complete"] is False
    assert status["by_stage"] == {"packet_ready": 1, "planned": 1}
    assert status["recommended"]["cpu_job_ref"] == JOB_TWO
    assert status["recommended"]["semantic_job_refs"] == [JOB_ONE]


def test_batch_status_derives_candidate_staged_and_accepted_states(tmp_path: Path) -> None:
    database = _registry(tmp_path)
    created = create_batch(
        tmp_path,
        planned=_planned(JOB_ONE, JOB_TWO),
        requested_status="new",
    )
    _current_candidate(tmp_path, JOB_ONE, imported=False)
    _current_candidate(tmp_path, JOB_TWO, imported=True)
    draft = tmp_path / "orchestration" / "content-drafts" / f"{JOB_TWO}-content.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("draft", encoding="utf-8")

    first = batch_status(tmp_path, str(created["batch_ref"]))
    assert first["by_stage"] == {"semantic_ready": 1, "staged": 1}
    assert first["recommended"]["import_job_refs"] == [JOB_ONE]
    assert first["recommended"]["publish_job_ref"] == JOB_TWO

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE collection_items SET status = 'completed' WHERE job_id = ?", (JOB_TWO,)
        )
        connection.execute(
            "INSERT INTO publication_sagas VALUES (?, ?, 'accepted', ?, ?)",
            ("pub-latest", JOB_TWO, "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"),
        )
        connection.execute(
            "INSERT INTO publication_targets VALUES (?, 'library', ?, 'verified')",
            ("pub-latest", "results/AI/Readable title"),
        )

    second = batch_status(tmp_path, str(created["batch_ref"]))
    accepted = next(item for item in second["items"] if item["job_ref"] == JOB_TWO)
    assert accepted["stage"] == "accepted"
    assert accepted["result_handle"] == "results/AI/Readable title"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE publication_targets SET status = 'mismatch' WHERE saga_id = 'pub-latest'"
        )
    drifted = batch_status(tmp_path, str(created["batch_ref"]))
    failed = next(item for item in drifted["items"] if item["job_ref"] == JOB_TWO)
    assert failed["stage"] == "failed"
    assert failed["error_code"] == "publication_target_drift"


def test_batch_create_rejects_overlap_until_existing_jobs_are_accepted(tmp_path: Path) -> None:
    database = _registry(tmp_path)
    create_batch(tmp_path, planned=_planned(JOB_ONE), requested_status="new")

    with pytest.raises(CliError) as error:
        create_batch(tmp_path, planned=_planned(JOB_ONE), requested_status="new")
    assert error.value.code == "batch_job_already_claimed"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE collection_items SET status = 'completed' WHERE job_id = ?", (JOB_ONE,)
        )
    with pytest.raises(CliError) as unaccepted:
        create_batch(tmp_path, planned=_planned(JOB_ONE), requested_status="new")
    assert unaccepted.value.code == "batch_job_already_claimed"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO publication_sagas VALUES (?, ?, 'accepted', ?, ?)",
            ("pub-accepted", JOB_ONE, "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"),
        )
        connection.execute(
            "INSERT INTO publication_targets VALUES (?, 'library', ?, 'verified')",
            ("pub-accepted", "results/AI/Accepted"),
        )
    replacement = create_batch(
        tmp_path,
        planned=[{"job_ref": JOB_ONE, "status": "completed", "position": 1}],
        requested_status=None,
    )
    assert replacement["item_count"] == 1


def test_batch_status_rejects_changed_or_corrupt_inventory(tmp_path: Path) -> None:
    database = _registry(tmp_path)
    created = create_batch(tmp_path, planned=_planned(JOB_ONE), requested_status="new")
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM collection_items WHERE job_id = ?", (JOB_ONE,))

    with pytest.raises(CliError) as changed:
        batch_status(tmp_path, str(created["batch_ref"]))
    assert changed.value.code == "batch_inventory_changed"

    checkpoint = tmp_path / str(created["state_handle"])
    checkpoint.write_text("{}", encoding="utf-8")
    with pytest.raises(CliError) as invalid:
        batch_status(tmp_path, str(created["batch_ref"]))
    assert invalid.value.code == "batch_checkpoint_invalid"


def test_fixed_plan_and_status_never_replace_an_uncollected_job(tmp_path: Path) -> None:
    database = _registry(tmp_path)
    assert [item["job_ref"] for item in fixed_plan(
        tmp_path, job_refs=[JOB_ONE, JOB_TWO], required_status="new"
    )] == [JOB_ONE, JOB_TWO]
    created = create_batch(
        tmp_path,
        planned=_planned(JOB_ONE, JOB_TWO),
        requested_status="new",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE collection_items SET currently_collected = 0 WHERE job_id = ?",
            (JOB_ONE,),
        )

    status = batch_status(tmp_path, str(created["batch_ref"]))

    assert status["status"] == "blocked"
    assert status["recommended"]["failed_job_refs"] == [JOB_ONE]
    assert status["recommended"]["cpu_job_ref"] == JOB_TWO
    failed = next(item for item in status["items"] if item["job_ref"] == JOB_ONE)
    assert failed["error_code"] == "job_no_longer_collected"
