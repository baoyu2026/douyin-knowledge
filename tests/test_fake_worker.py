from __future__ import annotations

import json
from pathlib import Path

import pytest

from douyin_knowledge.fake_worker import FakeWorkerError, main, run_fake_worker
from douyin_knowledge.protocol import export_packet, import_candidate
from tests.test_structured_content import _fixture, _payload


@pytest.fixture(autouse=True)
def private_directory_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.structured_content.harden_private_project_directory",
        lambda _root, path: path.mkdir(parents=True, exist_ok=True),
    )


def test_fake_worker_completes_file_protocol_without_model(
    tmp_path: Path,
) -> None:
    job_ref = _fixture(tmp_path)
    exported = export_packet(tmp_path, job_ref)
    content = tmp_path / "data" / "tasks" / job_ref / "semantic-v1" / "fixture-content.json"
    content.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    packet = tmp_path / exported["packet_handle"]
    schema = tmp_path / exported["candidate_schema_handle"]
    output = tmp_path / exported["candidate_output_handle"]

    first_hash = run_fake_worker(packet, schema, content, output)
    first_bytes = output.read_bytes()
    second_hash = run_fake_worker(packet, schema, content, output)

    assert first_hash == second_hash
    assert output.read_bytes() == first_bytes
    imported = import_candidate(tmp_path, job_ref, output)
    assert imported["status"] == "staged"


def test_fake_worker_cli_delegates_exact_file_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[Path, Path, Path, Path]] = []
    monkeypatch.setattr(
        "douyin_knowledge.fake_worker.run_fake_worker",
        lambda packet, schema, content, output: observed.append(
            (packet, schema, content, output)
        ),
    )
    paths = [tmp_path / name for name in ("packet", "schema", "content", "output")]

    assert (
        main(
            [
                "--packet",
                str(paths[0]),
                "--schema",
                str(paths[1]),
                "--content",
                str(paths[2]),
                "--output",
                str(paths[3]),
            ]
        )
        == 0
    )
    assert observed == [tuple(paths)]


def test_fake_worker_rejects_non_object_packet(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")
    with pytest.raises(FakeWorkerError):
        run_fake_worker(invalid, invalid, invalid, tmp_path / "output.json")
