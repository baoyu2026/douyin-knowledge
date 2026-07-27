from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from douyin_knowledge import __version__
from tests.test_batch import JOB_ONE, JOB_TWO, _registry
from tests.test_public_cli import all_strings, invoke


def test_batch_cli_fixes_scope_and_exposes_read_only_resume_plan(
    tmp_path: Path, capsys
) -> None:
    _registry(tmp_path)
    command = [
        "--root",
        str(tmp_path),
        "batch",
        "create",
        "--job-ref",
        JOB_ONE,
        "--status",
        "new",
        "--json",
    ]

    blocked_code, blocked = invoke(command, capsys)
    assert blocked_code == 2
    assert blocked["error"]["code"] == "confirmation_required"

    code, created = invoke([*command[:-1], "--confirm", "--json"], capsys)
    assert code == 0
    assert created["operation"] == "batch_create"
    assert created["data"]["job_refs"] == [JOB_ONE]
    batch_ref = created["data"]["batch_ref"]

    for subcommand in ("status", "resume"):
        read_code, payload = invoke(
            [
                "--root",
                str(tmp_path),
                "batch",
                subcommand,
                "--batch-ref",
                batch_ref,
                "--json",
            ],
            capsys,
        )
        assert read_code == 0
        assert payload["operation"] == f"batch_{subcommand}"
        assert payload["data"]["scope"] == {"job_refs": [JOB_ONE], "locked": True}
        assert payload["data"]["resources"] == {
            "cpu_busy": False,
            "semantic_slots_used": 0,
            "semantic_slots_available": 2,
            "semantic_job_refs": [],
            "publisher_busy": False,
        }
        assert str(tmp_path).casefold() not in "\n".join(all_strings(payload)).casefold()


def test_batch_cli_requires_current_canary_before_more_than_one_job(
    tmp_path: Path, capsys
) -> None:
    _registry(tmp_path)
    command = [
        "--root",
        str(tmp_path),
        "batch",
        "create",
        "--job-ref",
        JOB_ONE,
        "--job-ref",
        JOB_TWO,
        "--status",
        "new",
        "--confirm",
        "--json",
    ]

    blocked_code, blocked = invoke(command, capsys)
    assert blocked_code == 2
    assert blocked["error"]["code"] == "canary_required"

    marker = tmp_path / "data" / "safety" / "canary-v1.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"status": "packet_ready", "version": __version__}),
        encoding="utf-8",
    )
    code, created = invoke(command, capsys)

    assert code == 0
    assert created["data"]["item_count"] == 2
    assert created["data"]["job_refs"] == [JOB_ONE, JOB_TWO]


def test_status_reports_global_capacity_and_recorded_publication_drift(
    tmp_path: Path, capsys
) -> None:
    database = _registry(tmp_path)
    leases = tmp_path / "data" / "run-leases"
    leases.mkdir(parents=True)
    (leases / "cpu.lock").write_text("{}", encoding="utf-8")
    (leases / "publisher.lock").write_text("{}", encoding="utf-8")
    assignments = tmp_path / "data" / "tasks" / "semantic-handoff-assignments-v1.json"
    assignments.parent.mkdir(parents=True)
    assignments.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assignments": [
                    {"job_ref": JOB_ONE, "status": "active"},
                    {"job_ref": JOB_TWO, "status": "ingested"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO publication_sagas VALUES (?, ?, 'accepted', ?, ?)",
            ("pub-drift", JOB_ONE, "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"),
        )
        connection.execute(
            "INSERT INTO publication_targets VALUES (?, 'library', ?, 'mismatch')",
            ("pub-drift", "results/AI/Drifted"),
        )

    code, payload = invoke(["--root", str(tmp_path), "status", "--json"], capsys)

    assert code == 0
    assert payload["data"]["resources"] == {
        "cpu_busy": True,
        "semantic_slots_used": 1,
        "semantic_slots_available": 1,
        "publisher_busy": True,
    }
    assert payload["data"]["publication_drift_count"] == 1
