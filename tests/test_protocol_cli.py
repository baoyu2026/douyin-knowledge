from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tests.test_public_cli import all_strings, invoke
from tests.test_structured_content import _fixture, _payload


@pytest.fixture(autouse=True)
def private_directory_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.structured_content.harden_private_project_directory",
        lambda _root, path: path.mkdir(parents=True, exist_ok=True),
    )


def export_packet(root: Path, job_ref: str, capsys) -> dict[str, object]:
    code, payload = invoke(
        [
            "--root",
            str(root),
            "packet",
            "export",
            "--job-ref",
            job_ref,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert payload["ok"] is True
    return payload


def test_packet_export_is_bounded_safe_and_self_describing(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)

    payload = export_packet(tmp_path, job_ref, capsys)

    data = payload["data"]
    packet_path = tmp_path / data["packet_handle"]
    schema_path = tmp_path / data["candidate_schema_handle"]
    instructions_path = tmp_path / data["worker_instructions_handle"]
    assert packet_path.is_file()
    assert schema_path.is_file()
    assert instructions_path.is_file()
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert packet["job_ref"] == job_ref
    assert "job_id" not in packet
    assert data["packet_sha256"]
    assert schema["required"] == [
        "protocol_version",
        "schema_version",
        "job_ref",
        "packet_sha256",
        "content",
    ]
    assert str(tmp_path).casefold() not in "\n".join(all_strings(payload)).casefold()


def test_candidate_import_validates_provenance_and_stages_content(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    exported = export_packet(tmp_path, job_ref, capsys)
    data = exported["data"]
    candidate_path = (
        tmp_path / "data" / "tasks" / job_ref / "semantic-v1" / "candidate-input.json"
    )
    candidate_path.write_text(
        json.dumps(
            {
                "content": _payload(),
                "packet_sha256": data["packet_sha256"],
                "job_ref": job_ref,
                "schema_version": 1,
                "protocol_version": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    code, imported = invoke(
        [
            "--root",
            str(tmp_path),
            "candidate",
            "import",
            "--job-ref",
            job_ref,
            "--input",
            str(candidate_path),
            "--json",
        ],
        capsys,
    )

    assert code == 0
    assert imported["ok"] is True
    assert imported["data"]["status"] == "staged"
    accepted = tmp_path / imported["data"]["candidate_handle"]
    assert imported["data"]["candidate_sha256"] == hashlib.sha256(
        accepted.read_bytes()
    ).hexdigest()
    assert (tmp_path / imported["data"]["draft_handle"]).is_file()
    assert str(tmp_path).casefold() not in "\n".join(all_strings(imported)).casefold()


def test_candidate_import_from_official_output_reports_first_use_and_file_hash(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    exported = export_packet(tmp_path, job_ref, capsys)
    candidate_path = tmp_path / exported["data"]["candidate_output_handle"]
    candidate_path.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "schema_version": 1,
                "job_ref": job_ref,
                "packet_sha256": exported["data"]["packet_sha256"],
                "content": _payload(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    arguments = [
        "--root",
        str(tmp_path),
        "candidate",
        "import",
        "--job-ref",
        job_ref,
        "--input",
        str(candidate_path),
        "--json",
    ]

    first_code, first = invoke(arguments, capsys)
    second_code, second = invoke(arguments, capsys)

    expected_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    assert first_code == second_code == 0
    assert first["data"]["reused"] is False
    assert second["data"]["reused"] is True
    assert first["data"]["candidate_sha256"] == expected_hash
    assert second["data"]["candidate_sha256"] == expected_hash


def test_candidate_import_rejects_packet_hash_mismatch(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    export_packet(tmp_path, job_ref, capsys)
    candidate_path = (
        tmp_path / "data" / "tasks" / job_ref / "semantic-v1" / "candidate-input.json"
    )
    candidate_path.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "schema_version": 1,
                "job_ref": job_ref,
                "packet_sha256": "0" * 64,
                "content": _payload(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    code, imported = invoke(
        [
            "--root",
            str(tmp_path),
            "candidate",
            "import",
            "--job-ref",
            job_ref,
            "--input",
            str(candidate_path),
            "--json",
        ],
        capsys,
    )

    assert code == 2
    assert imported["error"]["code"] == "candidate_packet_mismatch"
    assert imported["error"]["retryable"] is False

    code, repair = invoke(
        [
            "--root",
            str(tmp_path),
            "candidate",
            "repair-contract",
            "--job-ref",
            job_ref,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert repair["data"]["repairable"] is False
    assert repair["data"]["action"] == "regenerate"
    assert (tmp_path / repair["data"]["contract_handle"]).is_file()


def test_first_item_can_stage_without_existing_related_knowledge(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    shutil.rmtree(tmp_path / "library")
    exported = export_packet(tmp_path, job_ref, capsys)
    content = _payload()
    content["related_knowledge"] = []
    candidate_path = (
        tmp_path / "data" / "tasks" / job_ref / "semantic-v1" / "candidate-input.json"
    )
    candidate_path.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "schema_version": 1,
                "job_ref": job_ref,
                "packet_sha256": exported["data"]["packet_sha256"],
                "content": content,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    code, imported = invoke(
        [
            "--root",
            str(tmp_path),
            "candidate",
            "import",
            "--job-ref",
            job_ref,
            "--input",
            str(candidate_path),
            "--json",
        ],
        capsys,
    )

    assert code == 0
    assert imported["data"]["status"] == "staged"


def test_run_exports_packet_without_invoking_a_model(tmp_path: Path, capsys) -> None:
    job_ref = _fixture(tmp_path)

    code, payload = invoke(
        [
            "--root",
            str(tmp_path),
            "run",
            "--job-ref",
            job_ref,
            "--stop-after",
            "packet",
            "--confirm",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    assert payload["data"]["status"] == "packet_ready"
    assert payload["data"]["model_calls"] == 0
    assert payload["data"]["publish"] is False
    assert (tmp_path / payload["data"]["packet_handle"]).is_file()


def test_run_staging_reuses_imported_candidate_without_model_call(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    exported = export_packet(tmp_path, job_ref, capsys)["data"]
    candidate_path = (
        tmp_path / "data" / "tasks" / job_ref / "semantic-v1" / "candidate-input.json"
    )
    candidate_path.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "schema_version": 1,
                "job_ref": job_ref,
                "packet_sha256": exported["packet_sha256"],
                "content": _payload(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    code, _imported = invoke(
        [
            "--root",
            str(tmp_path),
            "candidate",
            "import",
            "--job-ref",
            job_ref,
            "--input",
            str(candidate_path),
            "--json",
        ],
        capsys,
    )
    assert code == 0

    code, payload = invoke(
        [
            "--root",
            str(tmp_path),
            "run",
            "--job-ref",
            job_ref,
            "--stop-after",
            "staging",
            "--confirm",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    assert payload["data"]["status"] == "staged"
    assert payload["data"]["model_calls"] == 0
    assert payload["data"]["publish"] is False


def test_canary_is_bounded_to_one_and_never_publishes(tmp_path: Path, capsys) -> None:
    job_ref = _fixture(tmp_path)

    code, payload = invoke(
        [
            "--root",
            str(tmp_path),
            "canary",
            "--limit",
            "1",
            "--no-publish",
            "--confirm",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    assert payload["data"]["job_ref"] == job_ref
    assert payload["data"]["publish"] is False
    assert payload["data"]["model_calls"] == 0
