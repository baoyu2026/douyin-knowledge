from __future__ import annotations

import argparse
import json
import subprocess
import wave
from pathlib import Path
from typing import Any

from app.analyze_video import (
    AnalysisConfig,
    RuntimeDependencies,
    atomic_write_json,
    load_runtime_dependencies,
    resolve_ffmpeg,
    resolve_local_asr_model,
    transcribe_audio,
)


def _chunk_index(path: Path) -> int:
    return int(path.stem.split("-")[-1])


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        frame_rate = handle.getframerate()
        if frame_rate <= 0:
            raise RuntimeError("chunk_frame_rate_invalid")
        return handle.getnframes() / frame_rate


def _valid_checkpoint(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("segments"), list)


def _offset_payload(payload: dict[str, Any], offset: float) -> dict[str, Any]:
    for segment in payload.get("segments", []):
        segment["start"] = round(float(segment.get("start") or 0) + offset, 3)
        segment["end"] = round(float(segment.get("end") or 0) + offset, 3)
        for word in segment.get("words", []):
            word["start"] = round(float(word.get("start") or 0) + offset, 3)
            word["end"] = round(float(word.get("end") or 0) + offset, 3)
    return payload


def _transcribe(
    audio: Path,
    output: Path,
    *,
    offset: float,
    config: AnalysisConfig,
    deps: RuntimeDependencies,
) -> None:
    payload = transcribe_audio(audio, config, deps)
    atomic_write_json(output, _offset_payload(payload, offset))


def _retry_parts(chunk: Path, retry_dir: Path, *, ffmpeg: str) -> list[Path]:
    retry_dir.mkdir(parents=True, exist_ok=True)
    parts = sorted(retry_dir.glob("chunk-*.wav"))
    if parts:
        return parts
    result = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(chunk),
            "-f",
            "segment",
            "-segment_time",
            "60",
            "-reset_timestamps",
            "1",
            str(retry_dir / "chunk-%03d.wav"),
        ],
        capture_output=True,
        check=False,
        timeout=600,
    )
    parts = sorted(retry_dir.glob("chunk-*.wav"))
    if result.returncode != 0 or not parts:
        raise RuntimeError("chunk_split_failed")
    return parts


def _combine(paths: list[Path]) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    probabilities: list[float] = []
    duration = 0.0
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        probabilities.append(float(payload.get("language_probability") or 0))
        duration += float(payload.get("duration_seconds") or 0)
        segments.extend(payload.get("segments") or [])
    for index, segment in enumerate(segments):
        segment["id"] = index
    return {
        "schema_version": 1,
        "engine": "faster-whisper-chunked",
        "model": "small",
        "language": "zh",
        "language_probability": round(sum(probabilities) / max(1, len(probabilities)), 6),
        "duration_seconds": round(duration, 3),
        "segments": segments,
    }


def build_transcript(root: Path, job_id: str) -> Path:
    jobs_dir = (root / "data" / "jobs").resolve()
    job_dir = (jobs_dir / job_id).resolve()
    job_dir.relative_to(jobs_dir)
    chunks_dir = job_dir / "asr-chunks"
    chunks = sorted(chunks_dir.glob("chunk-*.wav"))
    if not chunks:
        raise RuntimeError("chunk_checkpoint_missing")

    deps = load_runtime_dependencies()
    config = AnalysisConfig(
        asr_model=resolve_local_asr_model(root, "small"),
        device="cpu",
        compute_type="int8",
        beam_size=1,
        cpu_threads=2,
    )
    ffmpeg = resolve_ffmpeg()
    offset = 0.0
    for chunk in chunks:
        output = chunk.with_suffix(".json")
        chunk_duration = _wav_duration(chunk)
        if _valid_checkpoint(output):
            offset += chunk_duration
            continue
        index = _chunk_index(chunk)
        retry_dir = chunks_dir / f"retry-{index:03d}"
        try:
            _transcribe(chunk, output, offset=offset, config=config, deps=deps)
        except Exception:
            parts = _retry_parts(chunk, retry_dir, ffmpeg=ffmpeg)
            part_offset = offset
            for part in parts:
                part_output = part.with_suffix(".json")
                if not _valid_checkpoint(part_output):
                    _transcribe(
                        part,
                        part_output,
                        offset=part_offset,
                        config=config,
                        deps=deps,
                    )
                part_offset += _wav_duration(part)
            atomic_write_json(output, _combine([part.with_suffix(".json") for part in parts]))
        offset += chunk_duration

    transcript = _combine([chunk.with_suffix(".json") for chunk in chunks])
    target = job_dir / "precomputed-transcript.json"
    atomic_write_json(target, transcript)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume a local chunked ASR checkpoint")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    try:
        target = build_transcript(args.root.resolve(), args.job_id)
    except Exception as exc:
        print(json.dumps({"status": "controlled_failure", "reason": type(exc).__name__}))
        return 4
    print(json.dumps({"status": "ok", "transcript": target.name}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
