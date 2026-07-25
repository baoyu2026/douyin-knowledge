from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.analyze_video import JOB_ID_PATTERN

CONTENT_PACKET_SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 64 * 1024
MIN_MAX_BYTES = 4 * 1024
SOURCE_FILES = {
    "summary": "summary.md",
    "transcript": "transcript.md",
    "ocr": "ocr.md",
    "timeline": "timeline.md",
    "manifest": "manifest.json",
}
PRIVATE_TEXT_PATTERN = re.compile(
    r"(?i)\b(cookie|session(?:id)?|signature|request[_ -]?url|authorization|"
    r"chat(?:s|log)?|run\.log|registry)\b"
)
URL_PATTERN = re.compile(r"(?i)https?://\S+")
ABSOLUTE_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:\\|/(?:Users|home)/)\S+")
FACT_PATTERN = re.compile(r"\d|\b[A-Z][A-Z0-9-]{1,}\b")
MARKDOWN_NOISE_PATTERN = re.compile(r"^[|:\-\s]+$")


class ContentPacketError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContentPacketResult:
    path: Path
    payload: dict[str, Any]
    size_bytes: int
    estimated_tokens: int
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _encoded(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _clean_record(value: str) -> str:
    value = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    if not value or MARKDOWN_NOISE_PATTERN.fullmatch(value):
        return ""
    if PRIVATE_TEXT_PATTERN.search(value) or URL_PATTERN.search(value):
        return ""
    value = ABSOLUTE_PATH_PATTERN.sub("[private-path-removed]", value)
    return value[:800].strip()


def _records(text: str) -> list[str]:
    seen: set[str] = set()
    records: list[str] = []
    for raw in text.splitlines():
        value = _clean_record(raw)
        key = re.sub(r"[\s|*_`#>-]+", "", value).casefold()
        if not value or not key or key in seen:
            continue
        seen.add(key)
        records.append(value)
    return records


def _coverage_order(values: list[str]) -> list[str]:
    if len(values) < 3:
        return values
    indices = [0, len(values) - 1]
    intervals = [(0, len(values) - 1)]
    while intervals:
        start, end = intervals.pop(0)
        middle = (start + end) // 2
        if middle not in indices:
            indices.append(middle)
        if middle - start > 1:
            intervals.append((start, middle))
        if end - middle > 1:
            intervals.append((middle, end))
    return [values[index] for index in indices]


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        return _clean_record(value) or None
    return None


def _manifest_facts(manifest: dict[str, Any]) -> dict[str, Any]:
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    asr = manifest.get("asr") if isinstance(manifest.get("asr"), dict) else {}
    ocr = manifest.get("ocr") if isinstance(manifest.get("ocr"), dict) else {}
    return {
        "duration_seconds": _safe_scalar(source.get("duration_seconds")),
        "dimensions": [
            _safe_scalar(source.get("width")),
            _safe_scalar(source.get("height")),
        ],
        "asr_engine": _safe_scalar(asr.get("engine")),
        "asr_segment_count": _safe_scalar(asr.get("segment_count")),
        "ocr_engine": _safe_scalar(ocr.get("engine")),
        "ocr_line_count": _safe_scalar(ocr.get("line_count")),
    }


def _selected_keyframes(
    analysis: Path, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    keyframes = manifest.get("keyframes")
    items = keyframes.get("items") if isinstance(keyframes, dict) else None
    if not isinstance(items, list):
        raise ContentPacketError("analysis manifest has no keyframe list")
    valid: list[tuple[dict[str, Any], Path]] = []
    analysis_root = analysis.resolve()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            continue
        path = (analysis / item["file"]).resolve()
        try:
            path.relative_to(analysis_root)
        except ValueError:
            continue
        if path.is_file():
            valid.append((item, path))
    if not valid:
        raise ContentPacketError("analysis manifest has no usable keyframes")
    count = min(8, len(valid))
    if count == 1:
        selected = valid
    else:
        indices = [
            round(index * (len(valid) - 1) / (count - 1)) for index in range(count)
        ]
        selected = [valid[index] for index in dict.fromkeys(indices)]
    return [
        {
            "frame_index": index,
            "file": path.name,
            "timestamp_seconds": _safe_scalar(item.get("timestamp")),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for index, (item, path) in enumerate(selected, 1)
    ]


def _append_with_limit(
    packet: dict[str, Any], target: list[str], values: Iterable[str], max_bytes: int
) -> None:
    for value in values:
        target.append(value)
        if len(_encoded(packet)) > max_bytes:
            target.pop()


def build_content_packet(
    root: Path,
    job_id: str,
    output: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ContentPacketResult:
    root = root.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise ContentPacketError("invalid job ID")
    if max_bytes < MIN_MAX_BYTES:
        raise ContentPacketError(f"max_bytes must be at least {MIN_MAX_BYTES}")
    analysis = root / "data" / "jobs" / job_id / "analysis"
    paths = {name: analysis / filename for name, filename in SOURCE_FILES.items()}
    texts: dict[str, str] = {}
    for name, path in paths.items():
        try:
            texts[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ContentPacketError(f"missing or invalid {name} input") from exc
    try:
        manifest = json.loads(texts["manifest"])
    except json.JSONDecodeError as exc:
        raise ContentPacketError("invalid analysis manifest") from exc
    if not isinstance(manifest, dict):
        raise ContentPacketError("analysis manifest must be an object")

    packet: dict[str, Any] = {
        "schema_version": CONTENT_PACKET_SCHEMA_VERSION,
        "job_ref": job_id,
        "limits": {"max_bytes": max_bytes},
        "source_file_hashes": {
            name: {
                "file": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for name, path in paths.items()
        },
        "manifest_facts": _manifest_facts(manifest),
        "selected_keyframes": _selected_keyframes(analysis, manifest),
        "evidence": {
            "timeline": [],
            "numeric_and_proper_nouns": [],
            "key_passages": [],
        },
    }
    metadata_size = len(_encoded(packet))
    if metadata_size > max_bytes:
        raise ContentPacketError("max_bytes is too small for hashes and keyframe metadata")

    timeline = _records(texts["timeline"])
    factual = _records(
        "\n".join(texts[name] for name in ("ocr", "summary", "transcript"))
    )
    factual = [value for value in factual if FACT_PATTERN.search(value)]
    passages = _records(
        "\n".join(texts[name] for name in ("summary", "transcript", "ocr"))
    )
    evidence = packet["evidence"]
    available = max_bytes - metadata_size
    timeline_limit = metadata_size + int(available * 0.45)
    _append_with_limit(
        packet,
        evidence["timeline"],
        _coverage_order(timeline),
        timeline_limit,
    )
    factual_limit = len(_encoded(packet)) + int(available * 0.30)
    _append_with_limit(
        packet,
        evidence["numeric_and_proper_nouns"],
        factual,
        min(max_bytes, factual_limit),
    )
    used = set(evidence["timeline"]) | set(evidence["numeric_and_proper_nouns"])
    _append_with_limit(
        packet,
        evidence["key_passages"],
        (value for value in passages if value not in used),
        max_bytes,
    )

    encoded = _encoded(packet)
    if len(encoded) > max_bytes:
        raise ContentPacketError("content packet exceeded max_bytes")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return ContentPacketResult(
        path=output,
        payload=packet,
        size_bytes=len(encoded),
        estimated_tokens=math.ceil(len(encoded.decode("utf-8")) / 4),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )
