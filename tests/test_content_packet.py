from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.content_packet import build_content_packet

JOB_ID = "aweme-0123456789abcdefabcd"


def _fixture(root: Path) -> Path:
    analysis = root / "data" / "jobs" / JOB_ID / "analysis"
    keyframes = analysis / "keyframes"
    keyframes.mkdir(parents=True)
    values = {
        "summary.md": (
            "# 摘要\nFDE 帮助 30 家企业落地。\n重复观点\nrequest_url: https://secret.invalid/x\n"
        ),
        "transcript.md": ("# 转写\n00:01 FDE 是前沿部署工程师。\n重复观点\ncookie=secret-cookie\n"),
        "ocr.md": "# OCR\n00:02 已服务 30+ 企业客户。\nsessionid: private-session\n",
        "timeline.md": (
            "# 时间轴\n00:00 开始说明 FDE。\n00:02 展示 30+ 客户。\n00:04 给出结论。\n"
        ),
    }
    for name, text in values.items():
        (analysis / name).write_text(text, encoding="utf-8")
    (analysis / "transcript.json").write_text(
        json.dumps({"segments": []}, ensure_ascii=False), encoding="utf-8"
    )
    (analysis / "ocr.json").write_text(
        json.dumps({"frames": []}, ensure_ascii=False), encoding="utf-8"
    )
    items = []
    for index in range(3):
        name = f"frame-{index + 1:03d}.jpg"
        path = keyframes / name
        path.write_bytes(f"frame-{index}".encode())
        items.append(
            {
                "id": index + 1,
                "timestamp": float(index),
                "file": f"keyframes/{name}",
            }
        )
    manifest = {
        "source": {
            "path": r"E:\private\source.mp4",
            "duration_seconds": 4.0,
            "width": 1080,
            "height": 1920,
            "request_url": "https://secret.invalid/video",
        },
        "asr": {
            "engine": "fixture",
            "segment_count": 3,
            "model": r"E:\private\model",
        },
        "ocr": {"engine": "fixture", "line_count": 2, "cookie": "secret"},
        "keyframes": {"items": items},
        "coverage_report": {
            "scan_reached_end": True,
            "tail_frame_readable": True,
            "timeline_span_ratio": 1.0,
            "asr_source_duration_ratio": 1.0,
            "asr_duration_status": "verified",
        },
        "registry": [{"all": "must-not-leak"}],
        "chat": "must-not-leak",
        "run.log": "must-not-leak",
    }
    (analysis / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return analysis


def test_content_packet_is_deterministic_bounded_and_hashed(tmp_path: Path) -> None:
    analysis = _fixture(tmp_path)
    first = build_content_packet(tmp_path, JOB_ID, tmp_path / "first.json", max_bytes=4096)
    second = build_content_packet(tmp_path, JOB_ID, tmp_path / "second.json", max_bytes=4096)

    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.sha256 == hashlib.sha256(first.path.read_bytes()).hexdigest()
    assert first.size_bytes <= 4096
    assert first.estimated_tokens > 0
    for source, filename in {
        "summary": "summary.md",
        "transcript": "transcript.md",
        "transcript_json": "transcript.json",
        "ocr": "ocr.md",
        "ocr_json": "ocr.json",
        "timeline": "timeline.md",
        "manifest": "manifest.json",
    }.items():
        expected = hashlib.sha256((analysis / filename).read_bytes()).hexdigest()
        assert first.payload["source_file_hashes"][source]["sha256"] == expected
    assert all(len(item["sha256"]) == 64 for item in first.payload["selected_keyframes"])
    assert first.payload["evidence"]["timeline"]
    assert first.payload["evidence"]["numeric_and_proper_nouns"]
    assert first.payload["manifest_facts"]["coverage_report"]["scan_reached_end"] is True
    assert set(first.payload["source_coverage"]) == {
        "summary",
        "transcript",
        "ocr",
        "timeline",
    }
    assert all(
        coverage["included_records"] <= coverage["total_records"]
        for coverage in first.payload["source_coverage"].values()
    )


def test_content_packet_excludes_private_and_unrelated_fields(tmp_path: Path) -> None:
    _fixture(tmp_path)
    result = build_content_packet(tmp_path, JOB_ID, tmp_path / "packet.json", max_bytes=8192)
    text = result.path.read_text(encoding="utf-8").casefold()

    for forbidden in (
        "secret-cookie",
        "private-session",
        "secret.invalid",
        "request_url",
        "registry",
        "must-not-leak",
        "run.log",
        '"chat"',
        r"e:\private",
    ):
        assert forbidden not in text
    assert "fde" in text and "30" in text


def test_content_packet_reports_every_budget_or_record_truncation(tmp_path: Path) -> None:
    analysis = _fixture(tmp_path)
    (analysis / "summary.md").write_text("# 摘要\n" + "超长证据" * 250 + "\n", encoding="utf-8")
    (analysis / "transcript.md").write_text(
        "# 转写\n"
        + "\n".join(f"00:{index:03d} 第 {index} 条唯一转写证据" for index in range(300))
        + "\n",
        encoding="utf-8",
    )
    (analysis / "ocr.md").write_text(
        "# OCR\n"
        + "\n".join(f"00:{index:03d} 第 {index} 条唯一画面文字" for index in range(300))
        + "\n",
        encoding="utf-8",
    )
    (analysis / "timeline.md").write_text(
        "# 时间轴\n"
        + "\n".join(f"00:{index:03d} 第 {index} 条唯一时间证据" for index in range(300))
        + "\n",
        encoding="utf-8",
    )

    result = build_content_packet(tmp_path, JOB_ID, tmp_path / "bounded.json", max_bytes=8192)

    assert result.size_bytes <= 8192
    coverage = result.payload["source_coverage"]
    assert coverage["summary"]["long_records_truncated"] == 1
    assert coverage["summary"]["truncated"] is True
    for source in ("transcript", "ocr", "timeline"):
        assert coverage[source]["total_records"] > coverage[source]["included_records"]
        assert coverage[source]["truncated"] is True
        assert isinstance(coverage[source]["first_record_included"], bool)
        assert isinstance(coverage[source]["last_record_included"], bool)
