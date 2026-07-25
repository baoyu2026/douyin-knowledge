from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.analyze_video import JOB_ID_PATTERN
from app.content_packet import _clean_record

EVIDENCE_BUNDLE_SCHEMA_VERSION = 1
DEFAULT_CHUNK_BYTES = 32 * 1024
MIN_CHUNK_BYTES = 4 * 1024
TEXT_FRAGMENT_CHARS = 600
TIMECODE_PATTERN = re.compile(r"(?P<minute>\d{2,}):(?P<second>\d{2})(?:\.(?P<millis>\d{1,3}))?")


class EvidenceBundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidenceBundleResult:
    manifest_path: Path
    chunk_paths: tuple[Path, ...]
    visual_paths: tuple[Path, ...]
    payload: dict[str, Any]


def _encoded(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceBundleError(f"missing or invalid {label}") from exc
    if not isinstance(value, dict):
        raise EvidenceBundleError(f"invalid {label}")
    return value


def _fragments(value: object) -> tuple[list[str], int]:
    if not isinstance(value, str):
        return [], 0
    compact = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    if not compact:
        return [], 0
    values: list[str] = []
    omitted = 0
    for offset in range(0, len(compact), TEXT_FRAGMENT_CHARS):
        fragment = _clean_record(compact[offset : offset + TEXT_FRAGMENT_CHARS])
        if fragment:
            values.append(fragment)
        else:
            omitted += 1
    return values, omitted


def _transcript_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    omitted = 0
    segments = payload.get("segments") if isinstance(payload.get("segments"), list) else []
    for segment in segments:
        if not isinstance(segment, dict):
            omitted += 1
            continue
        fragments, dropped = _fragments(segment.get("text"))
        omitted += dropped
        for part, text in enumerate(fragments, 1):
            records.append(
                {
                    "source": "asr",
                    "start_seconds": round(float(segment.get("start") or 0), 3),
                    "end_seconds": round(float(segment.get("end") or 0), 3),
                    "part": part,
                    "text": text,
                    "confidence": segment.get("confidence"),
                }
            )
    return records, omitted


def _ocr_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    omitted = 0
    frames = payload.get("frames") if isinstance(payload.get("frames"), list) else []
    for frame in frames:
        if not isinstance(frame, dict):
            omitted += 1
            continue
        lines = frame.get("lines") if isinstance(frame.get("lines"), list) else []
        for line_index, line in enumerate(lines, 1):
            if not isinstance(line, dict):
                omitted += 1
                continue
            fragments, dropped = _fragments(line.get("text"))
            omitted += dropped
            for part, text in enumerate(fragments, 1):
                records.append(
                    {
                        "source": "ocr",
                        "timestamp_seconds": round(float(frame.get("timestamp") or 0), 3),
                        "frame_index": frame.get("keyframe_id"),
                        "line": line_index,
                        "part": part,
                        "text": text,
                        "confidence": line.get("confidence"),
                    }
                )
    return records, omitted


def _timeline_seconds(value: str) -> float | None:
    match = TIMECODE_PATTERN.search(value)
    if not match:
        return None
    seconds = int(match.group("minute")) * 60 + int(match.group("second"))
    millis = match.group("millis")
    return round(seconds + (int(millis) / (10 ** len(millis)) if millis else 0), 3)


def _markdown_records(path: Path, source: str) -> tuple[list[dict[str, Any]], int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise EvidenceBundleError(f"missing or invalid {source}") from exc
    records: list[dict[str, Any]] = []
    omitted = 0
    for line_number, line in enumerate(lines, 1):
        fragments, dropped = _fragments(line)
        omitted += dropped
        for part, text in enumerate(fragments, 1):
            record: dict[str, Any] = {
                "source": source,
                "line": line_number,
                "part": part,
                "text": text,
            }
            timestamp = _timeline_seconds(text)
            if timestamp is not None:
                record["timestamp_seconds"] = timestamp
            records.append(record)
    return records, omitted


def _record_time(record: dict[str, Any]) -> tuple[float, int, int]:
    value = record.get("start_seconds", record.get("timestamp_seconds"))
    timestamp = float(value) if isinstance(value, (int, float)) else float("inf")
    source_order = {"asr": 0, "ocr": 1, "timeline": 2, "summary": 3}
    return timestamp, source_order.get(str(record.get("source")), 9), int(record.get("line") or 0)


def _write_chunks(
    output_dir: Path,
    job_ref: str,
    records: list[dict[str, Any]],
    max_chunk_bytes: int,
) -> tuple[Path, ...]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    maximum_chunk_count = max(1, len(records))
    for record in records:
        candidate = current + [record]
        probe = {
            "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "job_ref": job_ref,
            "chunk_index": len(chunks) + 1,
            "chunk_count": maximum_chunk_count,
            "records": candidate,
        }
        if current and len(_encoded(probe)) > max_chunk_bytes:
            chunks.append(current)
            current = [record]
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)

    paths: list[Path] = []
    chunk_count = len(chunks)
    for index, values in enumerate(chunks, 1):
        payload = {
            "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "job_ref": job_ref,
            "chunk_index": index,
            "chunk_count": chunk_count,
            "records": values,
        }
        content = _encoded(payload)
        if len(content) > max_chunk_bytes:
            raise EvidenceBundleError("one sanitized evidence record exceeds chunk limit")
        path = output_dir / "evidence-chunks" / f"chunk-{index:03d}.json"
        _atomic_bytes(path, content)
        paths.append(path)
    return tuple(paths)


def _copy_visuals(output_dir: Path, frames: list[Path]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for index, source in enumerate(frames, 1):
        suffix = (
            source.suffix.lower() if source.suffix.lower() in {".jpg", ".jpeg", ".png"} else ".jpg"
        )
        target = output_dir / "visual-evidence" / f"frame-{index:03d}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        paths.append(target)
    return tuple(paths)


def build_evidence_bundle(
    root: Path,
    job_ref: str,
    output_dir: Path,
    frames: list[Path],
    *,
    max_chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> EvidenceBundleResult:
    root = root.resolve()
    output_dir = output_dir.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_ref):
        raise EvidenceBundleError("invalid job reference")
    if max_chunk_bytes < MIN_CHUNK_BYTES:
        raise EvidenceBundleError(f"max_chunk_bytes must be at least {MIN_CHUNK_BYTES}")
    analysis = root / "data" / "jobs" / job_ref / "analysis"
    transcript_json = analysis / "transcript.json"
    if transcript_json.is_file():
        transcript, transcript_omitted = _transcript_records(
            _object(transcript_json, "transcript")
        )
    else:
        transcript, transcript_omitted = _markdown_records(
            analysis / "transcript.md", "asr"
        )
    ocr_json = analysis / "ocr.json"
    if ocr_json.is_file():
        ocr, ocr_omitted = _ocr_records(_object(ocr_json, "ocr"))
    else:
        ocr, ocr_omitted = _markdown_records(analysis / "ocr.md", "ocr")
    timeline, timeline_omitted = _markdown_records(analysis / "timeline.md", "timeline")
    summary, summary_omitted = _markdown_records(analysis / "summary.md", "summary")
    records = sorted([*transcript, *ocr, *timeline, *summary], key=_record_time)
    chunk_paths = _write_chunks(output_dir, job_ref, records, max_chunk_bytes)
    visual_paths = _copy_visuals(output_dir, frames)

    source_counts = {
        "asr": len(transcript),
        "ocr": len(ocr),
        "timeline": len(timeline),
        "summary": len(summary),
    }
    payload = {
        "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "job_ref": job_ref,
        "complete_sanitized_evidence": True,
        "record_count": len(records),
        "source_record_counts": source_counts,
        "omitted_private_or_empty_fragments": (
            transcript_omitted + ocr_omitted + timeline_omitted + summary_omitted
        ),
        "chunks": [
            {
                "file": path.relative_to(output_dir).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in chunk_paths
        ],
        "visuals": [
            {
                "frame_index": index,
                "file": path.relative_to(output_dir).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for index, path in enumerate(visual_paths, 1)
        ],
    }
    manifest_path = output_dir / "evidence-manifest.json"
    _atomic_bytes(manifest_path, _encoded(payload))
    return EvidenceBundleResult(
        manifest_path=manifest_path,
        chunk_paths=chunk_paths,
        visual_paths=visual_paths,
        payload=payload,
    )
