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
from app.keyframe_selection import resolve_keyframes

CONTENT_PACKET_SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 64 * 1024
MIN_MAX_BYTES = 4 * 1024
SOURCE_FILES = {
    "summary": "summary.md",
    "transcript": "transcript.md",
    "transcript_json": "transcript.json",
    "ocr": "ocr.md",
    "ocr_json": "ocr.json",
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
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _clean_record_detail(value: str) -> tuple[str, bool]:
    value = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    if not value or MARKDOWN_NOISE_PATTERN.fullmatch(value):
        return "", False
    if PRIVATE_TEXT_PATTERN.search(value) or URL_PATTERN.search(value):
        return "", False
    value = ABSOLUTE_PATH_PATTERN.sub("[private-path-removed]", value)
    truncated = len(value) > 800
    return value[:800].strip(), truncated


def _clean_record(value: str) -> str:
    return _clean_record_detail(value)[0]


def _records(text: str) -> list[str]:
    return _records_with_stats(text)[0]


def _records_with_stats(text: str) -> tuple[list[str], int]:
    seen: set[str] = set()
    records: list[str] = []
    long_records_truncated = 0
    for raw in text.splitlines():
        value, shortened = _clean_record_detail(raw)
        key = re.sub(r"[\s|*_`#>-]+", "", value).casefold()
        if not value or not key or key in seen:
            continue
        seen.add(key)
        records.append(value)
        long_records_truncated += int(shortened)
    return records, long_records_truncated


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
    coverage = (
        manifest.get("coverage_report") if isinstance(manifest.get("coverage_report"), dict) else {}
    )
    safe_coverage = {
        key: _safe_scalar(coverage.get(key))
        for key in (
            "scan_reached_end",
            "tail_frame_readable",
            "timeline_span_ratio",
            "tail_gap_seconds",
            "max_selected_gap_seconds",
            "candidate_count",
            "candidates_omitted",
            "output_limit_reached",
            "asr_source_duration_ratio",
            "asr_duration_status",
            "asr_quality_status",
            "ocr_quality_status",
            "ocr_line_count",
            "ocr_frames_with_text",
        )
        if key in coverage
    }
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
        "coverage_report": safe_coverage,
    }


def _selected_keyframes(analysis: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        selected = resolve_keyframes(analysis, manifest, max_count=None)
    except ValueError as exc:
        raise ContentPacketError(str(exc)) from exc
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

    source_records: dict[str, list[str]] = {}
    long_record_counts: dict[str, int] = {}
    for name in ("summary", "transcript", "ocr", "timeline"):
        records, long_records_truncated = _records_with_stats(texts[name])
        source_records[name] = records
        long_record_counts[name] = long_records_truncated
    source_coverage = {
        name: {
            "total_records": len(records),
            "included_records": len(records),
            "truncated": False,
            "long_records_truncated": long_record_counts[name],
            "first_record_included": False,
            "last_record_included": False,
        }
        for name, records in source_records.items()
    }

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
        "source_coverage": source_coverage,
        "evidence": {
            "timeline": [],
            "numeric_and_proper_nouns": [],
            "key_passages": [],
        },
    }
    metadata_size = len(_encoded(packet))
    if metadata_size > max_bytes:
        raise ContentPacketError("max_bytes is too small for hashes and keyframe metadata")

    timeline = source_records["timeline"]
    factual = _records("\n".join(texts[name] for name in ("ocr", "summary", "transcript")))
    factual = [value for value in factual if FACT_PATTERN.search(value)]
    passages = _records("\n".join(texts[name] for name in ("summary", "transcript", "ocr")))
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

    included_values = {value for values in evidence.values() for value in values}
    for name, records in source_records.items():
        coverage = source_coverage[name]
        included_count = sum(value in included_values for value in records)
        coverage["included_records"] = included_count
        coverage["truncated"] = included_count < len(records) or long_record_counts[name] > 0
        coverage["first_record_included"] = bool(records and records[0] in included_values)
        coverage["last_record_included"] = bool(records and records[-1] in included_values)

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
