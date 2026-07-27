from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_knowledge.agent_gateway import AgentGateway, safe_gateway_call
from tests.test_public_cli import all_strings
from tests.test_structured_content import _fixture, _payload


@pytest.fixture(autouse=True)
def private_directory_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.structured_content.harden_private_project_directory",
        lambda _root, path: path.mkdir(parents=True, exist_ok=True),
    )


def gateway_fixture(tmp_path: Path) -> tuple[AgentGateway, str]:
    instance = tmp_path / "private-instance"
    workspace = tmp_path / "agent-gateway"
    job_ref = _fixture(instance)
    return AgentGateway(instance, workspace), job_ref


def prepare(gateway: AgentGateway, job_ref: str) -> tuple[str, dict[str, object]]:
    payload = gateway.prepare_handoff(job_ref, confirmed=True)
    assert payload["ok"] is True
    assignment_ref = str(payload["data"]["assignment_ref"])
    manifest_payload = gateway.get_manifest(assignment_ref)
    assert manifest_payload["ok"] is True
    return assignment_ref, manifest_payload["data"]["manifest"]


def complete_candidate(job_ref: str, packet_sha256: str) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "schema_version": 2,
        "job_ref": job_ref,
        "packet_sha256": packet_sha256,
        "content": _payload(),
    }


def test_gateway_capabilities_are_host_neutral_and_candidate_only(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    gateway = AgentGateway(instance, tmp_path / "gateway")

    payload = gateway.capabilities()

    assert payload["ok"] is True
    assert payload["data"]["gateway_protocol"] == "douyin-knowledge-agent-gateway-v1"
    assert payload["data"]["mode"] == "candidate-only"
    assert payload["data"]["transports"] == ["python", "mcp-stdio", "file-handoff"]
    assert payload["data"]["features"]["publication"] is False
    assert str(instance).casefold() not in "\n".join(all_strings(payload)).casefold()


def test_gateway_rejects_workspace_inside_private_instance(tmp_path: Path) -> None:
    with pytest.raises(Exception) as raised:
        AgentGateway(tmp_path, tmp_path / "gateway")

    assert getattr(raised.value, "code", None) == "gateway_workspace_unsafe"


def test_gateway_requires_complete_evidence_before_candidate_submission(
    tmp_path: Path,
) -> None:
    gateway, job_ref = gateway_fixture(tmp_path)
    assignment_ref, manifest = prepare(gateway, job_ref)
    candidate = complete_candidate(job_ref, str(manifest["packet_sha256"]))

    payload = safe_gateway_call(
        "gateway_submit_candidate",
        lambda: gateway.submit_candidate(assignment_ref, candidate),
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "gateway_evidence_incomplete"
    assert not (
        gateway.assignments_root / assignment_ref / "candidate-v1.json"
    ).exists()


def test_gateway_reads_only_manifest_inventory_and_reports_progress(tmp_path: Path) -> None:
    gateway, job_ref = gateway_fixture(tmp_path)
    assignment_ref, manifest = prepare(gateway, job_ref)
    files = manifest["files"]
    text_handles = [
        str(item["handle"])
        for item in files
        if not str(item["handle"]).startswith("visual-evidence/")
    ]
    visual_handles = [
        str(item["handle"])
        for item in files
        if str(item["handle"]).startswith("visual-evidence/")
    ]

    first_text = gateway.read_text(assignment_ref, text_handles[0])
    first_visual = gateway.open_visual(assignment_ref, visual_handles[0])
    blocked = safe_gateway_call(
        "gateway_read_text",
        lambda: gateway.read_text(assignment_ref, "../private/cookies.json"),
    )
    status = gateway.assignment_status(assignment_ref)

    assert first_text["ok"] is True
    assert first_visual.content
    assert first_visual.mime_type == "image/jpeg"
    assert blocked["error"]["code"] == "gateway_handle_not_allowed"
    assert status["data"]["manifest_read"] is True
    assert status["data"]["read_text_count"] == 1
    assert status["data"]["opened_visual_count"] == 1
    assert status["data"]["missing_text_handles"]
    assert status["data"]["missing_visual_handles"]
    serialized = json.dumps(
        [first_text, status], ensure_ascii=False
    )
    assert str(gateway.instance_root).casefold() not in serialized.casefold()
    assert str(gateway.workspace_root).casefold() not in serialized.casefold()


def test_gateway_full_candidate_handoff_ingest_and_cleanup(tmp_path: Path) -> None:
    gateway, job_ref = gateway_fixture(tmp_path)
    assignment_ref, manifest = prepare(gateway, job_ref)
    for item in manifest["files"]:
        handle = str(item["handle"])
        if handle.startswith("visual-evidence/"):
            assert gateway.open_visual(assignment_ref, handle).content
        else:
            assert gateway.read_text(assignment_ref, handle)["ok"] is True

    ready = gateway.assignment_status(assignment_ref)
    submitted = gateway.submit_candidate(
        assignment_ref,
        complete_candidate(job_ref, str(manifest["packet_sha256"])),
    )
    cleaned = gateway.cleanup_assignment(assignment_ref, confirmed=True)

    assert ready["data"]["missing_text_handles"] == []
    assert ready["data"]["missing_visual_handles"] == []
    assert submitted["ok"] is True
    assert submitted["data"]["status"] == "ingested"
    assert cleaned["data"]["removed"] is True
    assert not (gateway.assignments_root / assignment_ref).exists()
    state = json.loads(gateway.state_path.read_text(encoding="utf-8"))
    assert state == {"schema_version": 1, "assignments": []}


def test_gateway_confirmation_and_cli_failures_use_safe_envelopes(tmp_path: Path) -> None:
    gateway, _job_ref = gateway_fixture(tmp_path)

    confirmation = safe_gateway_call(
        "gateway_prepare_handoff",
        lambda: gateway.prepare_handoff("aweme-00000000000000000000", confirmed=False),
    )
    invalid_plan = safe_gateway_call(
        "gateway_plan", lambda: gateway.plan(limit=6)
    )

    assert confirmation["error"]["code"] == "confirmation_required"
    assert invalid_plan["error"]["code"] == "invalid_limit"
    assert not any("Traceback" in value for value in all_strings(invalid_plan))


def test_gateway_rejects_manifest_tampering_and_non_json_candidate(tmp_path: Path) -> None:
    gateway, job_ref = gateway_fixture(tmp_path)
    assignment_ref, manifest = prepare(gateway, job_ref)
    manifest_path = gateway.assignments_root / assignment_ref / "handoff-manifest.json"
    original = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_bytes(
        original.replace('"schema_version": 1', '"schema_version": 2').encode("utf-8")
    )

    tampered = safe_gateway_call(
        "gateway_get_manifest", lambda: gateway.get_manifest(assignment_ref)
    )

    assert tampered["error"]["code"] == "gateway_manifest_invalid"
    manifest_path.write_bytes(original.encode("utf-8"))
    for item in manifest["files"]:
        handle = str(item["handle"])
        if handle.startswith("visual-evidence/"):
            gateway.open_visual(assignment_ref, handle)
        else:
            gateway.read_text(assignment_ref, handle)
    candidate = complete_candidate(job_ref, str(manifest["packet_sha256"]))
    candidate["content"]["not_json"] = float("nan")
    invalid = safe_gateway_call(
        "gateway_submit_candidate",
        lambda: gateway.submit_candidate(assignment_ref, candidate),
    )

    assert invalid["error"]["code"] == "gateway_candidate_invalid"
