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
            "# 摘要\nFDE 帮助 30 家企业落地。\n重复观点\n"
            "request_url: https://secret.invalid/x\n"
        ),
        "transcript.md": (
            "# 转写\n00:01 FDE 是前沿部署工程师。\n重复观点\n"
            "cookie=secret-cookie\n"
        ),
        "ocr.md": "# OCR\n00:02 已服务 30+ 企业客户。\nsessionid: private-session\n",
        "timeline.md": (
            "# 时间轴\n00:00 开始说明 FDE。\n00:02 展示 30+ 客户。\n"
            "00:04 给出结论。\n"
        ),
    }
    for name, text in values.items():
        (analysis / name).write_text(text, encoding="utf-8")
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
    first = build_content_packet(
        tmp_path, JOB_ID, tmp_path / "first.json", max_bytes=4096
    )
    second = build_content_packet(
        tmp_path, JOB_ID, tmp_path / "second.json", max_bytes=4096
    )

    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.sha256 == hashlib.sha256(first.path.read_bytes()).hexdigest()
    assert first.size_bytes <= 4096
    assert first.estimated_tokens > 0
    for source, filename in {
        "summary": "summary.md",
        "transcript": "transcript.md",
        "ocr": "ocr.md",
        "timeline": "timeline.md",
        "manifest": "manifest.json",
    }.items():
        expected = hashlib.sha256((analysis / filename).read_bytes()).hexdigest()
        assert first.payload["source_file_hashes"][source]["sha256"] == expected
    assert all(
        len(item["sha256"]) == 64
        for item in first.payload["selected_keyframes"]
    )
    assert first.payload["evidence"]["timeline"]
    assert first.payload["evidence"]["numeric_and_proper_nouns"]


def test_content_packet_excludes_private_and_unrelated_fields(tmp_path: Path) -> None:
    _fixture(tmp_path)
    result = build_content_packet(
        tmp_path, JOB_ID, tmp_path / "packet.json", max_bytes=8192
    )
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
