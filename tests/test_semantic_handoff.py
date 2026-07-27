from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from tests.test_public_cli import all_strings, invoke
from tests.test_structured_content import _fixture, _payload

SECOND_JOB_REF = "aweme-11111111111111111111"
THIRD_JOB_REF = "aweme-22222222222222222222"


@pytest.fixture(autouse=True)
def private_directory_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.structured_content.harden_private_project_directory",
        lambda _root, path: path.mkdir(parents=True, exist_ok=True),
    )


def handoff_directory(root: Path, suffix: str) -> Path:
    return root.parent / f"{root.name}-{suffix}"


def materialize(
    root: Path,
    destination: Path,
    job_ref: str,
    capsys,
) -> dict[str, object]:
    code, payload = invoke(
        [
            "--root",
            str(root),
            "handoff",
            "materialize",
            "--job-ref",
            job_ref,
            "--directory",
            str(destination),
            "--confirm",
            "--json",
        ],
        capsys,
    )
    assert code == 0, payload
    assert payload["ok"] is True
    return payload


def cleanup(
    root: Path,
    destination: Path,
    job_ref: str,
    token: str,
    capsys,
) -> dict[str, object]:
    code, payload = invoke(
        [
            "--root",
            str(root),
            "handoff",
            "cleanup",
            "--job-ref",
            job_ref,
            "--directory",
            str(destination),
            "--token",
            token,
            "--confirm",
            "--json",
        ],
        capsys,
    )
    assert code == 0, payload
    return payload


def write_candidate(destination: Path, job_ref: str, packet_sha256: str, title: str) -> None:
    content = _payload()
    content["title"] = title
    (destination / "candidate-v1.json").write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "schema_version": 2,
                "job_ref": job_ref,
                "packet_sha256": packet_sha256,
                "content": content,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def clone_fixture_job(root: Path, source_job_ref: str, job_ref: str) -> None:
    shutil.copytree(
        root / "data" / "jobs" / source_job_ref,
        root / "data" / "jobs" / job_ref,
    )
    database = root / "data" / "knowledge.db"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        source = connection.execute(
            "SELECT * FROM collection_items WHERE job_id = ?", (source_job_ref,)
        ).fetchone()
        assert source is not None
        values = dict(source)
        values.update(
            {
                "source_id": f"source-{job_ref}",
                "aweme_id": f"aweme-id-{job_ref}",
                "job_id": job_ref,
                "last_position": int(values["last_position"]) + int(job_ref[-2:], 16),
                "job_path": None,
                "library_path": None,
            }
        )
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO collection_items ({', '.join(columns)}) VALUES ({placeholders})",
            [values[column] for column in columns],
        )


def test_handoff_materialize_is_atomic_sanitized_and_path_safe(tmp_path: Path, capsys) -> None:
    job_ref = _fixture(tmp_path)
    destination = handoff_directory(tmp_path, "materialized")
    destination.mkdir()
    arguments = [
        "--root",
        str(tmp_path),
        "handoff",
        "materialize",
        "--job-ref",
        job_ref,
        "--directory",
        str(destination),
        "--json",
    ]

    blocked_code, blocked = invoke(arguments, capsys)
    assert blocked_code == 2
    assert blocked["error"]["code"] == "confirmation_required"
    assert not list(destination.iterdir())

    code, payload = invoke([*arguments[:-1], "--confirm", "--json"], capsys)
    assert code == 0
    data = payload["data"]
    manifest = json.loads((destination / data["manifest_handle"]).read_text(encoding="utf-8"))
    actual_files = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    expected_files = {"handoff-manifest.json", *[item["handle"] for item in manifest["files"]]}

    assert payload["operation"] == "handoff_materialize"
    assert data["candidate_handle"] == "candidate-v1.json"
    assert data["cleanup_token"].startswith("shc_")
    assert data["assignment_capacity"] == {"used": 1, "available": 1}
    assert data["counts"]["bundle_files"] == len(actual_files)
    assert data["counts"]["evidence_chunks"] > 0
    assert data["counts"]["evidence_records"] > 0
    assert data["counts"]["visuals"] >= 3
    assert actual_files == expected_files
    assert not (destination / "candidate-v1.json").exists()
    assert data["cleanup_token"] not in (destination / "handoff-manifest.json").read_text(
        encoding="utf-8"
    )
    assert manifest["cleanup_token_sha256"] == hashlib.sha256(
        data["cleanup_token"].encode("utf-8")
    ).hexdigest()
    assert not any(
        part.casefold() in {"knowledge.db", "logs", "cookies.json", "video.mp4"}
        for path in actual_files
        for part in Path(path).parts
    )
    assert str(tmp_path).casefold() not in "\n".join(all_strings(payload)).casefold()
    assert str(destination).casefold() not in "\n".join(all_strings(payload)).casefold()

    state = json.loads(
        (tmp_path / "data" / "tasks" / "semantic-handoff-assignments-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["assignments"] == [
        {
            "handoff_ref": data["handoff_ref"],
            "job_ref": job_ref,
            "cleanup_token_sha256": manifest["cleanup_token_sha256"],
            "directory_sha256": hashlib.sha256(
                os.path.normcase(str(destination.resolve())).encode("utf-8")
            ).hexdigest(),
            "packet_sha256": data["packet_sha256"],
            "manifest_sha256": data["manifest_sha256"],
            "status": "active",
        }
    ]

    cleaned = cleanup(tmp_path, destination, job_ref, data["cleanup_token"], capsys)
    assert cleaned["data"]["removed"] is True
    assert not destination.exists()


def test_handoff_materialize_rejects_inside_root_and_nonempty_target(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    inside = tmp_path / "worker-handoff"
    nonempty = handoff_directory(tmp_path, "nonempty")
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("keep", encoding="utf-8")

    inside_code, inside_payload = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "materialize",
            "--job-ref",
            job_ref,
            "--directory",
            str(inside),
            "--confirm",
            "--json",
        ],
        capsys,
    )
    nonempty_code, nonempty_payload = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "materialize",
            "--job-ref",
            job_ref,
            "--directory",
            str(nonempty),
            "--confirm",
            "--json",
        ],
        capsys,
    )

    assert inside_code == nonempty_code == 2
    assert inside_payload["error"]["code"] == "handoff_directory_inside_root"
    assert nonempty_payload["error"]["code"] == "handoff_directory_not_empty"
    assert (nonempty / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "data" / "tasks" / "semantic-handoff-assignments-v1.json").exists()


def test_handoff_limits_unfinished_assignments_before_export(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    from douyin_knowledge import semantic_handoff

    job_ref = _fixture(tmp_path)
    clone_fixture_job(tmp_path, job_ref, SECOND_JOB_REF)
    clone_fixture_job(tmp_path, job_ref, THIRD_JOB_REF)
    first_dir = handoff_directory(tmp_path, "slot-one")
    second_dir = handoff_directory(tmp_path, "slot-two")
    third_dir = handoff_directory(tmp_path, "slot-three")
    first = materialize(tmp_path, first_dir, job_ref, capsys)["data"]
    duplicate_code, duplicate = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "materialize",
            "--job-ref",
            job_ref,
            "--directory",
            str(handoff_directory(tmp_path, "duplicate")),
            "--confirm",
            "--json",
        ],
        capsys,
    )
    assert duplicate_code == 2
    assert duplicate["error"]["code"] == "semantic_job_already_assigned"
    second = materialize(tmp_path, second_dir, SECOND_JOB_REF, capsys)["data"]
    state = json.loads(
        (tmp_path / "data" / "tasks" / "semantic-handoff-assignments-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["status"] for item in state["assignments"]] == ["active", "active"]

    calls = 0

    def unexpected_export(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("third assignment must fail before packet export")

    original_export = semantic_handoff.export_packet
    monkeypatch.setattr("douyin_knowledge.semantic_handoff.export_packet", unexpected_export)
    code, payload = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "materialize",
            "--job-ref",
            THIRD_JOB_REF,
            "--directory",
            str(third_dir),
            "--confirm",
            "--json",
        ],
        capsys,
    )

    assert code == 2, payload
    assert payload["error"]["code"] == "semantic_assignment_limit_reached"
    assert calls == 0
    assert not third_dir.exists()

    monkeypatch.setattr("douyin_knowledge.semantic_handoff.export_packet", original_export)
    write_candidate(first_dir, job_ref, first["packet_sha256"], "释放语义任务槽位的候选内容")
    ingest_code, _ingested = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "ingest",
            "--job-ref",
            job_ref,
            "--directory",
            str(first_dir),
            "--json",
        ],
        capsys,
    )
    assert ingest_code == 0
    third = materialize(tmp_path, third_dir, THIRD_JOB_REF, capsys)["data"]

    cleanup(tmp_path, first_dir, job_ref, first["cleanup_token"], capsys)
    cleanup(tmp_path, second_dir, SECOND_JOB_REF, second["cleanup_token"], capsys)
    cleanup(tmp_path, third_dir, THIRD_JOB_REF, third["cleanup_token"], capsys)


def test_handoff_assignment_lock_refuses_live_owner_and_quarantines_dead_owner(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    lock = tmp_path / "data" / "tasks" / ".semantic-handoff-assignments.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lease_id": "a" * 32,
                "pid": os.getpid(),
                "created_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    live_dir = handoff_directory(tmp_path, "live-lock")

    live_code, live_payload = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "materialize",
            "--job-ref",
            job_ref,
            "--directory",
            str(live_dir),
            "--confirm",
            "--json",
        ],
        capsys,
    )

    assert live_code == 2
    assert live_payload["error"]["code"] == "semantic_assignment_state_busy"
    assert lock.is_file()
    lock.unlink()

    lock.write_text("2147483647", encoding="ascii")
    recovered_dir = handoff_directory(tmp_path, "recovered-lock")
    recovered = materialize(tmp_path, recovered_dir, job_ref, capsys)["data"]

    quarantined = tmp_path / "quarantine" / "semantic-handoff-locks"
    assert len(list(quarantined.glob("*.lock"))) == 1
    assert not lock.exists()
    cleanup(
        tmp_path,
        recovered_dir,
        job_ref,
        recovered["cleanup_token"],
        capsys,
    )


def test_handoff_ingest_stages_candidate_releases_slot_and_allows_cleanup(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    destination = handoff_directory(tmp_path, "ingest")
    materialized = materialize(tmp_path, destination, job_ref, capsys)["data"]
    write_candidate(
        destination,
        job_ref,
        materialized["packet_sha256"],
        "隔离交接导入的企业 AI 落地方法",
    )

    code, payload = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "ingest",
            "--job-ref",
            job_ref,
            "--directory",
            str(destination),
            "--json",
        ],
        capsys,
    )

    assert code == 0
    assert payload["operation"] == "handoff_ingest"
    assert payload["data"]["status"] == "staged"
    assert payload["data"]["handoff_ref"] == materialized["handoff_ref"]
    assert payload["data"]["assignment_capacity"] == {"used": 0, "available": 2}
    accepted = tmp_path / payload["data"]["candidate_handle"]
    assert accepted.is_file()
    assert payload["data"]["candidate_sha256"] == hashlib.sha256(
        accepted.read_bytes()
    ).hexdigest()
    assert (
        tmp_path
        / "quarantine"
        / "candidates"
        / job_ref
        / f"{payload['data']['candidate_sha256']}.json"
    ).is_file()
    state = json.loads(
        (tmp_path / "data" / "tasks" / "semantic-handoff-assignments-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["assignments"][0]["status"] == "ingested"
    assert str(tmp_path).casefold() not in "\n".join(all_strings(payload)).casefold()
    assert str(destination).casefold() not in "\n".join(all_strings(payload)).casefold()

    cleaned = cleanup(
        tmp_path,
        destination,
        job_ref,
        materialized["cleanup_token"],
        capsys,
    )
    assert cleaned["data"]["removed"] is True
    final_state = json.loads(
        (tmp_path / "data" / "tasks" / "semantic-handoff-assignments-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert final_state["assignments"] == []


def test_handoff_ingest_rejects_extra_or_tampered_bundle_files(tmp_path: Path, capsys) -> None:
    job_ref = _fixture(tmp_path)
    destination = handoff_directory(tmp_path, "tampered")
    materialized = materialize(tmp_path, destination, job_ref, capsys)["data"]
    write_candidate(
        destination,
        job_ref,
        materialized["packet_sha256"],
        "待严格校验的企业 AI 交付方法",
    )
    extra = destination / "notes.txt"
    extra.write_text("unexpected", encoding="utf-8")
    arguments = [
        "--root",
        str(tmp_path),
        "handoff",
        "ingest",
        "--job-ref",
        job_ref,
        "--directory",
        str(destination),
        "--json",
    ]

    extra_code, extra_payload = invoke(arguments, capsys)
    assert extra_code == 2
    assert extra_payload["error"]["code"] == "handoff_inventory_invalid"
    extra.unlink()

    instructions = destination / "worker-instructions.md"
    original = instructions.read_bytes()
    instructions.write_bytes(original + b"tampered")
    tampered_code, tampered_payload = invoke(arguments, capsys)
    assert tampered_code == 2
    assert tampered_payload["error"]["code"] == "handoff_file_mismatch"
    assert not (
        tmp_path / "data" / "tasks" / job_ref / "semantic-v1" / "candidate-v1.json"
    ).exists()

    instructions.write_bytes(original)
    cleanup(
        tmp_path,
        destination,
        job_ref,
        materialized["cleanup_token"],
        capsys,
    )


def test_handoff_ingest_rejects_candidate_packet_mismatch(tmp_path: Path, capsys) -> None:
    job_ref = _fixture(tmp_path)
    destination = handoff_directory(tmp_path, "wrong-packet")
    materialized = materialize(tmp_path, destination, job_ref, capsys)["data"]
    write_candidate(destination, job_ref, "0" * 64, "错误证据包对应的企业 AI 交付方法")

    code, payload = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "ingest",
            "--job-ref",
            job_ref,
            "--directory",
            str(destination),
            "--json",
        ],
        capsys,
    )

    assert code == 2
    assert payload["error"]["code"] == "candidate_packet_mismatch"
    cleanup(
        tmp_path,
        destination,
        job_ref,
        materialized["cleanup_token"],
        capsys,
    )


def test_handoff_repair_contract_is_hash_bound_to_the_rejected_candidate(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    destination = handoff_directory(tmp_path, "repair-contract")
    materialized = materialize(tmp_path, destination, job_ref, capsys)["data"]
    write_candidate(
        destination,
        job_ref,
        materialized["packet_sha256"],
        "需要一次受限修复的企业 AI 交付候选",
    )
    candidate_sha256 = hashlib.sha256(
        (destination / "candidate-v1.json").read_bytes()
    ).hexdigest()
    task = tmp_path / "data" / "tasks" / job_ref / "semantic-v1"
    (task / "last-rejection.json").write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "job_ref": job_ref,
                "error_code": "structured_coverage_review_invalid",
                "candidate_sha256": candidate_sha256,
            }
        ),
        encoding="utf-8",
    )

    arguments = [
        "--root",
        str(tmp_path),
        "handoff",
        "repair-contract",
        "--job-ref",
        job_ref,
        "--directory",
        str(destination),
        "--json",
    ]
    code, payload = invoke(arguments, capsys)

    assert code == 0, payload
    assert payload["operation"] == "handoff_repair_contract"
    assert payload["data"]["max_repair_attempts"] == 1
    manifest_path = destination / "handoff-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    contract = manifest["repair_contract"]
    assert contract["source_candidate_sha256"] == candidate_sha256
    assert contract["editable_top_level_fields"] == ["content"]
    state = json.loads(
        (tmp_path / "data" / "tasks" / "semantic-handoff-assignments-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["assignments"][0]["manifest_sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()

    reused_code, reused = invoke(arguments, capsys)
    assert reused_code == 0
    assert reused["data"]["reused"] is True
    ingest_code, ingested = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "ingest",
            "--job-ref",
            job_ref,
            "--directory",
            str(destination),
            "--json",
        ],
        capsys,
    )
    assert ingest_code == 0, ingested
    cleanup(
        tmp_path,
        destination,
        job_ref,
        materialized["cleanup_token"],
        capsys,
    )


def test_handoff_cleanup_requires_matching_token_and_exact_inventory(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    destination = handoff_directory(tmp_path, "cleanup")
    materialized = materialize(tmp_path, destination, job_ref, capsys)["data"]
    base = [
        "--root",
        str(tmp_path),
        "handoff",
        "cleanup",
        "--job-ref",
        job_ref,
        "--directory",
        str(destination),
        "--token",
        materialized["cleanup_token"],
    ]

    confirmation_code, confirmation_payload = invoke([*base, "--json"], capsys)
    assert confirmation_code == 2
    assert confirmation_payload["error"]["code"] == "confirmation_required"
    wrong_code, wrong_payload = invoke(
        [*base[:-1], "wrong-token", "--confirm", "--json"], capsys
    )
    assert wrong_code == 2
    assert wrong_payload["error"]["code"] == "handoff_cleanup_token_mismatch"
    assert destination.is_dir()

    extra = destination / "unmanaged.txt"
    extra.write_text("must survive refused cleanup", encoding="utf-8")
    inventory_code, inventory_payload = invoke([*base, "--confirm", "--json"], capsys)
    assert inventory_code == 2
    assert inventory_payload["error"]["code"] == "handoff_inventory_invalid"
    assert extra.read_text(encoding="utf-8") == "must survive refused cleanup"
    extra.unlink()

    cleaned = cleanup(
        tmp_path,
        destination,
        job_ref,
        materialized["cleanup_token"],
        capsys,
    )
    assert cleaned["data"]["removed"] is True
    assert not destination.exists()


def test_handoff_cleanup_resumes_after_final_directory_removal_is_interrupted(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_ref = _fixture(tmp_path)
    destination = handoff_directory(tmp_path, "cleanup-resume")
    materialized = materialize(tmp_path, destination, job_ref, capsys)["data"]
    original_rmdir = Path.rmdir
    interrupted = False

    def interrupt_final_rmdir(path: Path) -> None:
        nonlocal interrupted
        if path == destination and not interrupted:
            interrupted = True
            raise OSError("simulated final directory removal interruption")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", interrupt_final_rmdir)
    code, payload = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "cleanup",
            "--job-ref",
            job_ref,
            "--directory",
            str(destination),
            "--token",
            materialized["cleanup_token"],
            "--confirm",
            "--json",
        ],
        capsys,
    )

    assert code == 2
    assert payload["error"]["code"] == "handoff_cleanup_incomplete"
    assert destination.is_dir()
    assert not any(destination.iterdir())

    monkeypatch.setattr(Path, "rmdir", original_rmdir)
    recovered = cleanup(
        tmp_path,
        destination,
        job_ref,
        materialized["cleanup_token"],
        capsys,
    )

    assert recovered["data"]["removed"] is True
    assert recovered["data"]["assignment_capacity"] == {"used": 0, "available": 2}
    assert not destination.exists()


def test_handoff_ingest_preserves_previous_candidate_history(tmp_path: Path, capsys) -> None:
    job_ref = _fixture(tmp_path)
    first_dir = handoff_directory(tmp_path, "history-one")
    first = materialize(tmp_path, first_dir, job_ref, capsys)["data"]
    write_candidate(first_dir, job_ref, first["packet_sha256"], "首版企业 AI 交付候选内容")
    first_code, first_ingest = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "ingest",
            "--job-ref",
            job_ref,
            "--directory",
            str(first_dir),
            "--json",
        ],
        capsys,
    )
    assert first_code == 0

    summary = tmp_path / "data" / "jobs" / job_ref / "analysis" / "summary.md"
    summary.write_text(summary.read_text(encoding="utf-8") + "\nnew evidence\n", encoding="utf-8")
    second_dir = handoff_directory(tmp_path, "history-two")
    second = materialize(tmp_path, second_dir, job_ref, capsys)["data"]
    write_candidate(second_dir, job_ref, second["packet_sha256"], "新版企业 AI 交付候选内容")
    second_code, second_ingest = invoke(
        [
            "--root",
            str(tmp_path),
            "handoff",
            "ingest",
            "--job-ref",
            job_ref,
            "--directory",
            str(second_dir),
            "--json",
        ],
        capsys,
    )

    assert second_code == 0
    assert second_ingest["data"]["replaced"] is True
    old_hash = first_ingest["data"]["candidate_sha256"]
    assert (tmp_path / "quarantine" / "candidates" / job_ref / f"{old_hash}.json").is_file()

    cleanup(tmp_path, first_dir, job_ref, first["cleanup_token"], capsys)
    cleanup(tmp_path, second_dir, job_ref, second["cleanup_token"], capsys)
