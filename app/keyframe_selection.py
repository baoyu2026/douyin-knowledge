from __future__ import annotations

from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})


def _timestamp(item: dict[str, Any], fallback: int) -> float:
    value = item.get("timestamp")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _scene_score(item: dict[str, Any]) -> float:
    try:
        value = float(item.get("scene_score") or 0)
    except (TypeError, ValueError):
        return 0.0
    return min(max(value, 0.0), 1.0)


def resolve_keyframes(
    analysis_dir: Path,
    manifest: dict[str, Any],
    *,
    max_count: int = 8,
    min_count: int = 1,
) -> list[tuple[dict[str, Any], Path]]:
    if max_count < 1 or min_count < 1 or min_count > max_count:
        raise ValueError("invalid keyframe selection limits")
    raw_items = (manifest.get("keyframes") or {}).get("items") or []
    if not isinstance(raw_items, list):
        raise ValueError("analysis manifest has no keyframe list")
    keyframe_root = (analysis_dir / "keyframes").resolve()
    valid: list[tuple[dict[str, Any], Path, int]] = []
    for original_index, item in enumerate(raw_items):
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            continue
        path = (analysis_dir / item["file"]).resolve()
        try:
            path.relative_to(keyframe_root)
        except ValueError:
            continue
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            valid.append((item, path, original_index))
    valid.sort(key=lambda value: (_timestamp(value[0], value[2]), value[1].name))
    if len(valid) < min_count:
        raise ValueError("analysis manifest has insufficient usable keyframes")
    if len(valid) <= max_count:
        return [(item, path) for item, path, _index in valid]
    if max_count == 1:
        item, path, _index = valid[0]
        return [(item, path)]

    first_time = _timestamp(valid[0][0], 0)
    last_time = _timestamp(valid[-1][0], len(valid) - 1)
    span = max(last_time - first_time, 1.0)
    targets = [first_time + span * index / (max_count - 1) for index in range(max_count)]
    selected: list[int] = []
    for target_index, target in enumerate(targets):
        if target_index == 0:
            chosen = 0
        elif target_index == len(targets) - 1:
            chosen = len(valid) - 1
        else:
            available = [index for index in range(1, len(valid) - 1) if index not in selected]
            chosen = min(
                available,
                key=lambda index: (
                    abs(_timestamp(valid[index][0], index) - target) / span
                    - _scene_score(valid[index][0]) * 0.05,
                    index,
                ),
            )
        if chosen not in selected:
            selected.append(chosen)
    selected.sort()
    return [(valid[index][0], valid[index][1]) for index in selected]
