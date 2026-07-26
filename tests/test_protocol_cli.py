from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from app.structured_content import StructuredContentError
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


def extend_keyframes(root: Path, job_ref: str, count: int) -> None:
    analysis = root / "data" / "jobs" / job_ref / "analysis"
    manifest_path = analysis / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest["keyframes"]["items"]
    for index in range(len(items) + 1, count + 1):
        name = f"frame-{index:03d}.jpg"
        (analysis / "keyframes" / name).write_bytes(f"frame-{index}".encode())
        items.append({"file": f"keyframes/{name}", "timestamp": index * 5})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def test_packet_export_is_bounded_safe_and_self_describing(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    extend_keyframes(tmp_path, job_ref, 40)

    payload = export_packet(tmp_path, job_ref, capsys)

    data = payload["data"]
    packet_path = tmp_path / data["packet_handle"]
    schema_path = tmp_path / data["candidate_schema_handle"]
    instructions_path = tmp_path / data["worker_instructions_handle"]
    evidence_manifest_path = tmp_path / data["evidence_manifest_handle"]
    assert packet_path.is_file()
    assert schema_path.is_file()
    assert instructions_path.is_file()
    assert evidence_manifest_path.is_file()
    assert data["evidence_chunk_handles"]
    assert len(data["visual_handles"]) == 40
    assert all((tmp_path / handle).is_file() for handle in data["evidence_chunk_handles"])
    assert all((tmp_path / handle).is_file() for handle in data["visual_handles"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    evidence_manifest = json.loads(evidence_manifest_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert packet["job_ref"] == job_ref
    assert evidence_manifest["complete_sanitized_evidence"] is True
    assert evidence_manifest["complete_visual_inventory"] is True
    assert evidence_manifest["required_visual_inspection_count"] == 40
    assert evidence_manifest["visual_count"] == 40
    assert evidence_manifest["record_count"] > 0
    assert [item["sha256"] for item in evidence_manifest["visuals"]] == [
        item["sha256"] for item in packet["selected_keyframes"]
    ]
    assert "job_id" not in packet
    assert data["packet_sha256"]
    assert data["evidence_manifest_sha256"]
    assert schema["required"] == [
        "protocol_version",
        "schema_version",
        "job_ref",
        "packet_sha256",
        "content",
    ]
    visual_schema = schema["properties"]["content"]["properties"]["visual_evidence"]
    assert visual_schema["minItems"] == 3
    assert visual_schema["maxItems"] == 8
    assert visual_schema["items"]["properties"]["frame_index"]["maximum"] == 40
    assert "argument_step" in visual_schema["items"]["properties"]
    assert str(tmp_path).casefold() not in "\n".join(all_strings(payload)).casefold()
    instructions = instructions_path.read_text(encoding="utf-8")
    assert "Read every evidence chunk" in instructions
    assert "Inspect every listed visual file" in instructions
    assert "select only 3 to 8" in instructions
    assert "cannot read images" in instructions
    assert "complete coverage inventory" in instructions
    assert "argument_step" in instructions


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
                "schema_version": 2,
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
                "schema_version": 2,
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


def test_candidate_import_replaces_objectively_stale_packet_without_reject(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    first_export = export_packet(tmp_path, job_ref, capsys)["data"]
    candidate_path = (
        tmp_path / "data" / "tasks" / job_ref / "semantic-v1" / "candidate-input.json"
    )
    candidate_path.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "schema_version": 2,
                "job_ref": job_ref,
                "packet_sha256": first_export["packet_sha256"],
                "content": _payload(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    first_code, first = invoke(
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
    assert first_code == 0

    summary = tmp_path / "data" / "jobs" / job_ref / "analysis" / "summary.md"
    summary.write_text(summary.read_text(encoding="utf-8") + "\nnew evidence\n", encoding="utf-8")
    second_export = export_packet(tmp_path, job_ref, capsys)["data"]
    replacement = _payload()
    replacement["title"] = "更新证据后的企业 AI 交付方法"
    candidate_path.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "schema_version": 2,
                "job_ref": job_ref,
                "packet_sha256": second_export["packet_sha256"],
                "content": replacement,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    second_code, second = invoke(
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

    assert second_code == 0
    assert second["data"]["replaced"] is True
    old_history = (
        tmp_path
        / "quarantine"
        / "candidates"
        / job_ref
        / f"{first['data']['candidate_sha256']}.json"
    )
    assert old_history.is_file()


def test_candidate_import_same_packet_replacement_still_requires_reject(
    tmp_path: Path, capsys
) -> None:
    job_ref = _fixture(tmp_path)
    exported = export_packet(tmp_path, job_ref, capsys)["data"]
    candidate_path = (
        tmp_path / "data" / "tasks" / job_ref / "semantic-v1" / "candidate-input.json"
    )

    def write_candidate(title: str) -> None:
        content = _payload()
        content["title"] = title
        candidate_path.write_text(
            json.dumps(
                {
                    "protocol_version": 1,
                    "schema_version": 2,
                    "job_ref": job_ref,
                    "packet_sha256": exported["packet_sha256"],
                    "content": content,
                },
                ensure_ascii=False,
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
    write_candidate("同一证据包的首个候选标题")
    first_code, first = invoke(arguments, capsys)
    write_candidate("同一证据包的另一个候选标题")
    second_code, second = invoke(arguments, capsys)

    assert first_code == 0
    assert second_code == 2
    assert second["error"]["code"] == "candidate_already_imported"
    accepted = tmp_path / first["data"]["candidate_handle"]
    assert json.loads(accepted.read_text(encoding="utf-8"))["content"]["title"] == (
        "同一证据包的首个候选标题"
    )


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
                "schema_version": 2,
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


def test_rendering_rejection_produces_bounded_repair_contract(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
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
                "schema_version": 2,
                "job_ref": job_ref,
                "packet_sha256": exported["packet_sha256"],
                "content": _payload(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def reject_render(*_args, **_kwargs) -> None:
        raise StructuredContentError(
            "content_numbers_unreviewed", "fixture rendering rejection"
        )

    monkeypatch.setattr(
        "douyin_knowledge.protocol.render_structured_json_artifact", reject_render
    )
    code, rejected = invoke(
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
    assert rejected["error"]["code"] == "content_numbers_unreviewed"
    assert rejected["error"]["retryable"] is True

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
    assert repair["data"]["error_code"] == "content_numbers_unreviewed"
    assert repair["data"]["repairable"] is True
    contract = json.loads(
        (tmp_path / repair["data"]["contract_handle"]).read_text(encoding="utf-8")
    )
    assert contract["max_repair_attempts"] == 1
    invariants = " ".join(contract["required_content_invariants"])
    assert "primary_category" in invariants
    assert "related_knowledge" in invariants
    assert "publishable section" in invariants
    assert "revalidate every required content invariant" in contract["repair_instruction"]


def test_non_content_rejection_requires_prerequisite_fix_not_candidate_repair(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
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
                "schema_version": 2,
                "job_ref": job_ref,
                "packet_sha256": exported["packet_sha256"],
                "content": _payload(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def reject_prerequisite(*_args, **_kwargs) -> None:
        raise StructuredContentError("structured_input_missing", "fixture prerequisite")

    monkeypatch.setattr(
        "douyin_knowledge.protocol.validate_structured_payload", reject_prerequisite
    )
    code, rejected = invoke(
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
    assert rejected["error"]["code"] == "structured_input_missing"
    assert rejected["error"]["retryable"] is False

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


def test_repair_contract_diagnoses_legacy_render_failure_without_rewriting_history(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_ref = _fixture(tmp_path)
    exported = export_packet(tmp_path, job_ref, capsys)["data"]
    task = tmp_path / "data" / "tasks" / job_ref / "semantic-v1"
    candidate_path = task / "candidate-v1.json"
    raw_path = (
        tmp_path / "orchestration" / "structured-content" / job_ref / "response-v1.json"
    )
    candidate = {
        "protocol_version": 1,
        "schema_version": 2,
        "job_ref": job_ref,
        "packet_sha256": exported["packet_sha256"],
        "content": _payload(),
    }
    candidate_path.write_text(
        json.dumps(candidate, ensure_ascii=False), encoding="utf-8"
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(candidate["content"], ensure_ascii=False), encoding="utf-8"
    )
    candidate_before = candidate_path.read_bytes()
    raw_before = raw_path.read_bytes()

    def reject_render(*_args, **_kwargs) -> None:
        raise StructuredContentError(
            "content_numbers_unreviewed", "legacy rendering rejection"
        )

    monkeypatch.setattr(
        "douyin_knowledge.protocol.render_structured_json_artifact", reject_render
    )
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
    assert repair["data"]["error_code"] == "content_numbers_unreviewed"
    assert repair["data"]["repairable"] is True
    assert candidate_path.read_bytes() == candidate_before
    assert raw_path.read_bytes() == raw_before
    assert not list(task.glob(".repair-diagnostic-*.json"))


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
                "schema_version": 2,
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
                "schema_version": 2,
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
