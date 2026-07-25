from __future__ import annotations

from pathlib import Path

import pytest

from app.keyframe_selection import resolve_keyframes


def test_keyframe_selection_is_deterministic_scene_aware_and_covers_tail(tmp_path: Path) -> None:
    analysis = tmp_path / "analysis"
    keyframes = analysis / "keyframes"
    keyframes.mkdir(parents=True)
    items = []
    for index in range(20):
        name = f"frame-{index:03d}.jpg"
        (keyframes / name).write_bytes(str(index).encode())
        items.append(
            {
                "file": f"keyframes/{name}",
                "timestamp": index * 5,
                "scene_score": 1.0 if index == 10 else 0.01,
            }
        )
    manifest = {"keyframes": {"items": items}}

    first = resolve_keyframes(analysis, manifest, max_count=8, min_count=3)
    second = resolve_keyframes(analysis, manifest, max_count=8, min_count=3)

    assert [path.name for _item, path in first] == [path.name for _item, path in second]
    assert first[0][0]["timestamp"] == 0
    assert first[-1][0]["timestamp"] == 95
    assert any(item["timestamp"] == 50 for item, _path in first)
    assert len(first) == 8

    complete = resolve_keyframes(analysis, manifest, max_count=None, min_count=3)
    assert [path.name for _item, path in complete] == [
        f"frame-{index:03d}.jpg" for index in range(20)
    ]


def test_keyframe_selection_rejects_paths_outside_keyframe_directory(tmp_path: Path) -> None:
    analysis = tmp_path / "analysis"
    (analysis / "keyframes").mkdir(parents=True)
    (analysis / "not-a-frame.jpg").write_bytes(b"x")
    manifest = {"keyframes": {"items": [{"file": "not-a-frame.jpg", "timestamp": 0}]}}

    with pytest.raises(ValueError, match="insufficient"):
        resolve_keyframes(analysis, manifest)
