from __future__ import annotations

import json
from pathlib import Path

from app.evidence_bundle import build_evidence_bundle
from tests.test_structured_content import _fixture


def _add_machine_readable_analysis(analysis: Path) -> None:
    (analysis / "transcript.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "start": 0,
                        "end": 1,
                        "text": "baseline evidence",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (analysis / "ocr.json").write_text(
        json.dumps(
            {
                "frames": [
                    {
                        "keyframe_id": 1,
                        "timestamp": 0,
                        "lines": [{"text": "screen evidence", "confidence": 0.8}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_evidence_bundle_preserves_all_sanitized_records_and_visuals(tmp_path: Path) -> None:
    job_ref = _fixture(tmp_path)
    analysis = tmp_path / "data" / "jobs" / job_ref / "analysis"
    _add_machine_readable_analysis(analysis)
    transcript_path = analysis / "transcript.json"
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript["segments"] = [
        {
            "start": index * 2.0,
            "end": index * 2.0 + 1.5,
            "text": f"segment-{index:03d} " + ("evidence " * 30),
            "confidence": 0.9,
        }
        for index in range(40)
    ]
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")
    manifest = json.loads((analysis / "manifest.json").read_text(encoding="utf-8"))
    frames = [analysis / item["file"] for item in manifest["keyframes"]["items"]]

    result = build_evidence_bundle(
        tmp_path,
        job_ref,
        tmp_path / "data" / "tasks" / job_ref / "semantic-v1",
        frames[:8],
        max_chunk_bytes=4096,
    )

    assert len(result.chunk_paths) > 1
    assert len(result.visual_paths) == min(8, len(frames))
    records = []
    for path in result.chunk_paths:
        assert path.stat().st_size <= 4096
        records.extend(json.loads(path.read_text(encoding="utf-8"))["records"])
    asr_text = " ".join(item["text"] for item in records if item["source"] == "asr")
    for index in range(40):
        assert f"segment-{index:03d}" in asr_text
    assert result.payload["record_count"] == len(records)
    assert result.payload["complete_sanitized_evidence"] is True


def test_evidence_bundle_reports_private_fragments_without_leaking_them(tmp_path: Path) -> None:
    job_ref = _fixture(tmp_path)
    analysis = tmp_path / "data" / "jobs" / job_ref / "analysis"
    _add_machine_readable_analysis(analysis)
    transcript_path = analysis / "transcript.json"
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript["segments"].append(
        {"start": 20, "end": 21, "text": "Authorization secret", "confidence": 1.0}
    )
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")
    manifest = json.loads((analysis / "manifest.json").read_text(encoding="utf-8"))
    frames = [analysis / item["file"] for item in manifest["keyframes"]["items"]]

    result = build_evidence_bundle(tmp_path, job_ref, tmp_path / "task", frames[:3])

    combined = "\n".join(path.read_text(encoding="utf-8") for path in result.chunk_paths)
    assert "Authorization secret" not in combined
    assert result.payload["omitted_private_or_empty_fragments"] >= 1
