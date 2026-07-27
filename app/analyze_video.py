from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import traceback
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.collection_registry import update_item_by_job

CONTROLLED_FAILURE_EXIT = 4
INTERNAL_FAILURE_EXIT = 5
PROBE_DIR = Path("data/probe-collect")
OUTPUT_DIR = Path("data/analysis/collect-probe-sample")
ANALYSIS_VERSION = 3
JOBS_DIR = Path("data/jobs")
JOB_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
REQUIRED_ANALYSIS_ARTIFACTS = {
    "transcript.json",
    "transcript.md",
    "ocr.json",
    "ocr.md",
    "timeline.md",
    "summary.md",
    "manifest.json",
}
MAX_DURATION_SECONDS = 4 * 60 * 60
DEPENDENCIES = {
    "faster_whisper": "faster-whisper",
    "rapidocr": "rapidocr",
    "onnxruntime": "onnxruntime",
    "cv2": "opencv-python-headless",
    "PIL": "Pillow",
    "numpy": "numpy",
}


class AnalysisError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AnalysisConfig:
    asr_model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    sample_interval_seconds: float = 0.2
    coverage_interval_seconds: float = 15.0
    scene_threshold: float = 0.12
    duplicate_hamming_ratio: float = 0.08
    max_keyframes: int = 40
    beam_size: int = 1
    cpu_threads: int = 2
    transcript_override: Path | None = None


@dataclass(frozen=True)
class RuntimeDependencies:
    cv2: Any
    np: Any
    whisper_model: Any
    rapid_ocr: Any


class DiagnosticLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def emit(self, stage: str, status: str, **details: object) -> None:
        record = {
            "time": datetime.now(UTC).isoformat(),
            "stage": stage,
            "status": status,
            **details,
        }
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def format_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    minutes, remainder = divmod(milliseconds, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    if millis:
        return f"{minutes:02d}:{whole_seconds:02d}.{millis:03d}"
    return f"{minutes:02d}:{whole_seconds:02d}"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_probe_video(root: Path, explicit_input: Path | None = None) -> Path:
    probe_dir = (root / PROBE_DIR).resolve()
    try:
        candidates = (
            [explicit_input.resolve()] if explicit_input else sorted(probe_dir.glob("*.mp4"))
        )
    except OSError as exc:
        raise AnalysisError(
            "probe_input_unavailable", f"无法读取 data/probe-collect：{exc}"
        ) from exc

    if explicit_input:
        try:
            candidates[0].relative_to(probe_dir)
        except ValueError as exc:
            raise AnalysisError(
                "input_outside_probe", "输入文件必须位于 data/probe-collect 内"
            ) from exc
    if len(candidates) != 1:
        raise AnalysisError(
            "probe_input_count",
            f"data/probe-collect 必须且只能包含 1 个 MP4，当前发现 {len(candidates)} 个",
        )
    video = candidates[0]
    _validate_nonempty_mp4(video, "invalid_probe_input")
    return video


def _validate_nonempty_mp4(video: Path, code: str = "invalid_job_input") -> None:
    try:
        valid = video.is_file() and video.suffix.lower() == ".mp4" and video.stat().st_size > 0
    except OSError as exc:
        raise AnalysisError("input_unavailable", f"无法访问输入 MP4：{exc}") from exc
    if not valid:
        raise AnalysisError(code, "输入必须是非空 MP4 文件")


def _inside(path: Path, parent: Path, code: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise AnalysisError(code, "输入和输出必须位于 data/jobs 内") from exc
    return resolved


def resolve_job_paths(
    root: Path,
    *,
    job_id: str | None,
    explicit_input: Path | None,
    explicit_output: Path | None,
) -> tuple[Path, Path, str]:
    jobs_root = (root / JOBS_DIR).resolve()
    if job_id:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise AnalysisError("invalid_job_id", "job ID 格式不安全")
        job_dir = jobs_root / job_id
        video = _inside(explicit_input or job_dir / "source.mp4", jobs_root, "input_outside_jobs")
        output = _inside(explicit_output or job_dir / "analysis", jobs_root, "output_outside_jobs")
        expected_job_dir = jobs_root / job_id
        if video.parent != expected_job_dir or output.parent != expected_job_dir:
            raise AnalysisError("job_path_mismatch", "显式路径必须属于指定 job")
    else:
        if explicit_input is None or explicit_output is None:
            raise AnalysisError(
                "job_arguments_required",
                "请提供 --job-id，或同时提供 --input 与 --output",
            )
        video = _inside(explicit_input, jobs_root, "input_outside_jobs")
        output = _inside(explicit_output, jobs_root, "output_outside_jobs")
        if video.parent != output.parent:
            raise AnalysisError("job_path_mismatch", "输入与输出必须属于同一个 job 目录")
        job_id = video.parent.name
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise AnalysisError("invalid_job_id", "job 目录名称格式不安全")
    _validate_nonempty_mp4(video)
    return video, output, job_id


def load_current_manifest(video: Path, output: Path) -> dict[str, Any] | None:
    manifest_path = output / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("analysis_version") != ANALYSIS_VERSION:
            return None
        coverage = manifest.get("coverage_report")
        if not isinstance(coverage, dict) or coverage.get("scan_reached_end") is not True:
            return None
        source = manifest.get("source", {})
        if source.get("sha256") != sha256_file(video):
            return None
        required = set(REQUIRED_ANALYSIS_ARTIFACTS)
        if (manifest.get("asr") or {}).get("audio_present") is not False:
            required.add("audio.wav")
        if not all((output / name).is_file() for name in required):
            return None
        if not (output / "keyframes").is_dir():
            return None
        return manifest
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def resolve_local_asr_model(root: Path, requested: str) -> str:
    candidate = Path(requested)
    if candidate.is_absolute() or candidate.exists():
        return str(candidate.resolve())
    if not re.fullmatch(r"[A-Za-z0-9._-]+", requested):
        return requested
    snapshots = (
        root
        / "data"
        / "models"
        / "huggingface"
        / "hub"
        / f"models--Systran--faster-whisper-{requested}"
        / "snapshots"
    )
    try:
        valid = [
            path
            for path in snapshots.iterdir()
            if path.is_dir()
            and (path / "config.json").is_file()
            and (path / "model.bin").is_file()
            and (path / "tokenizer.json").is_file()
        ]
    except OSError:
        return requested
    if not valid:
        return requested
    selected = max(valid, key=lambda path: path.stat().st_mtime_ns)
    return str(selected.resolve())


def missing_dependencies() -> list[str]:
    return [
        package
        for module, package in DEPENDENCIES.items()
        if importlib.util.find_spec(module) is None
    ]


def load_runtime_dependencies() -> RuntimeDependencies:
    missing = missing_dependencies()
    if missing:
        command = ".\\.venv\\Scripts\\python.exe -m pip install -e ."
        raise AnalysisError(
            "missing_dependencies",
            f"缺少本地分析依赖：{', '.join(missing)}。请先运行：{command}",
        )
    import cv2
    import numpy as np
    from faster_whisper import WhisperModel
    from rapidocr import RapidOCR

    return RuntimeDependencies(cv2=cv2, np=np, whisper_model=WhisperModel, rapid_ocr=RapidOCR)


def resolve_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise AnalysisError("ffmpeg_unavailable", f"FFmpeg 不可用：{exc}") from exc
    if not Path(executable).is_file():
        raise AnalysisError("ffmpeg_unavailable", "FFmpeg 可执行文件不存在")
    return executable


def inspect_video(video: Path, deps: RuntimeDependencies) -> dict[str, float | int]:
    capture = deps.cv2.VideoCapture(str(video))
    try:
        if not capture.isOpened():
            raise AnalysisError("video_open_failed", "OpenCV 无法打开输入 MP4")
        fps = float(capture.get(deps.cv2.CAP_PROP_FPS))
        frame_count = int(capture.get(deps.cv2.CAP_PROP_FRAME_COUNT))
        width = int(capture.get(deps.cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(deps.cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        capture.release()
    if not math.isfinite(fps) or fps <= 0 or frame_count <= 0:
        raise AnalysisError("invalid_video_metadata", "MP4 的帧率或帧数无效")
    duration = frame_count / fps
    if duration > MAX_DURATION_SECONDS:
        raise AnalysisError(
            "video_too_long",
            f"收藏样本超过 4 小时安全上限，检测到 {duration:.3f} 秒",
        )
    return {
        "duration_seconds": round(duration, 3),
        "fps": round(fps, 3),
        "frame_count": frame_count,
        "width": width,
        "height": height,
    }


def extract_audio(ffmpeg: str, video: Path, output: Path) -> bool:
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AnalysisError("audio_extraction_failed", f"音频提取失败：{exc}") from exc
    stderr = result.stderr.decode("utf-8", errors="replace").strip()[-1000:]
    no_audio_stream = "matches no streams" in stderr or "does not contain any stream" in stderr
    if no_audio_stream:
        output.unlink(missing_ok=True)
        return False
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise AnalysisError("audio_extraction_failed", f"FFmpeg 音频提取失败：{stderr}")
    return True


def _word_payload(word: Any) -> dict[str, object]:
    probability = getattr(word, "probability", None)
    return {
        "start": round(float(word.start), 3),
        "end": round(float(word.end), 3),
        "text": str(word.word),
        "confidence": round(float(probability), 6) if probability is not None else None,
    }


def load_transcript_override(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError("transcript_override_invalid", "无法读取预计算转写") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise AnalysisError("transcript_override_invalid", "预计算转写结构无效")
    for segment in payload["segments"]:
        if not isinstance(segment, dict) or not all(
            key in segment for key in ("start", "end", "text")
        ):
            raise AnalysisError("transcript_override_invalid", "预计算转写片段无效")
    return payload


def transcribe_audio(
    audio: Path,
    config: AnalysisConfig,
    deps: RuntimeDependencies,
) -> dict[str, Any]:
    try:
        model = deps.whisper_model(
            config.asr_model,
            device=config.device,
            compute_type=config.compute_type,
            local_files_only=True,
            cpu_threads=config.cpu_threads,
            num_workers=1,
        )
        segments_iterator, info = model.transcribe(
            str(audio),
            language="zh",
            beam_size=config.beam_size,
            vad_filter=True,
            word_timestamps=True,
            condition_on_previous_text=False,
        )
        segments = []
        for index, segment in enumerate(segments_iterator):
            words = [_word_payload(word) for word in (segment.words or [])]
            word_confidences = [
                word["confidence"] for word in words if word["confidence"] is not None
            ]
            confidence = (
                round(sum(word_confidences) / len(word_confidences), 6)
                if word_confidences
                else None
            )
            segments.append(
                {
                    "id": index,
                    "start": round(float(segment.start), 3),
                    "end": round(float(segment.end), 3),
                    "text": str(segment.text).strip(),
                    "confidence": confidence,
                    "avg_logprob": round(float(segment.avg_logprob), 6),
                    "no_speech_probability": round(float(segment.no_speech_prob), 6),
                    "words": words,
                }
            )
    except Exception as exc:
        raise AnalysisError(
            "asr_failed",
            f"本地 ASR 失败；请确认 faster-whisper 模型已预先下载到本机且参数正确：{exc}",
        ) from exc
    return {
        "schema_version": 1,
        "engine": "faster-whisper",
        "model": config.asr_model,
        "language": str(getattr(info, "language", "zh")),
        "language_probability": _optional_round(getattr(info, "language_probability", None)),
        "duration_seconds": _optional_round(getattr(info, "duration", None), 3),
        "segments": segments,
    }


def _optional_round(value: object, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None else None


def _frame_signature(gray: Any, deps: RuntimeDependencies) -> Any:
    resized = deps.cv2.resize(gray, (16, 16), interpolation=deps.cv2.INTER_AREA)
    bits = (resized >= float(resized.mean())).astype(deps.np.uint8).reshape(-1)
    return deps.np.packbits(bits).tobytes()


def hamming_ratio(left: bytes, right: bytes) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("frame signatures must be non-empty and have equal length")
    differing_bits = sum(
        (left_byte ^ right_byte).bit_count()
        for left_byte, right_byte in zip(left, right, strict=True)
    )
    return differing_bits / (len(left) * 8)


def extract_keyframes(
    video: Path,
    output_dir: Path,
    metadata: dict[str, float | int],
    config: AnalysisConfig,
    deps: RuntimeDependencies,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = float(metadata["duration_seconds"])
    capture = deps.cv2.VideoCapture(str(video))
    candidates: list[dict[str, Any]] = []
    previous_gray = None
    coverage_interval = min(
        config.coverage_interval_seconds,
        max(config.sample_interval_seconds, duration / 4),
    )
    last_coverage_time = -coverage_interval
    sample_count = 0
    valid_sample_count = 0
    last_attempted = 0.0
    last_decodable = 0.0
    last_observation: dict[str, Any] | None = None
    expected_tail_timestamp = max(
        0.0,
        (int(metadata["frame_count"]) - 1) / float(metadata["fps"]),
    )

    def observe(timestamp: float, *, force_tail: bool = False) -> None:
        nonlocal previous_gray, last_coverage_time, sample_count
        nonlocal valid_sample_count, last_attempted, last_decodable, last_observation
        last_attempted = max(last_attempted, timestamp)
        sample_count += 1
        capture.set(deps.cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = capture.read()
        if not ok:
            return
        valid_sample_count += 1
        last_decodable = max(last_decodable, timestamp)
        gray = deps.cv2.cvtColor(frame, deps.cv2.COLOR_BGR2GRAY)
        comparison = deps.cv2.resize(gray, (320, 180), interpolation=deps.cv2.INTER_AREA)
        scene_score = (
            1.0
            if previous_gray is None
            else float(deps.np.mean(deps.cv2.absdiff(comparison, previous_gray))) / 255.0
        )
        previous_gray = comparison
        coverage_due = timestamp - last_coverage_time >= coverage_interval
        reason = (
            "first"
            if last_observation is None
            else (
                "tail"
                if force_tail
                else ("scene_change" if scene_score >= config.scene_threshold else "coverage")
            )
        )
        observation = {
            "timestamp": timestamp,
            "scene_score": scene_score,
            "signature": _frame_signature(gray, deps),
            "reason": reason,
        }
        last_observation = observation
        if not candidates or force_tail or scene_score >= config.scene_threshold or coverage_due:
            if candidates and abs(float(candidates[-1]["timestamp"]) - timestamp) < 1e-6:
                candidates[-1] = observation
            else:
                candidates.append(observation)
            if coverage_due:
                last_coverage_time = timestamp

    try:
        if not capture.isOpened():
            raise AnalysisError("video_open_failed", "OpenCV 无法为关键帧提取打开 MP4")
        timestamp = 0.0
        while timestamp < expected_tail_timestamp:
            observe(timestamp)
            timestamp += config.sample_interval_seconds
        observe(expected_tail_timestamp, force_tail=True)
    finally:
        capture.release()
    if not candidates and last_observation is not None:
        candidates.append(last_observation)
    if not candidates:
        raise AnalysisError("keyframe_extraction_failed", "未能从 MP4 提取任何关键帧")
    if config.max_keyframes < 1:
        raise AnalysisError("keyframe_limit_invalid", "关键帧上限必须大于 0")

    desired_coverage = max(1, math.ceil(duration / coverage_interval) + 1)
    minimum_coverage = min(config.max_keyframes, 3 if config.max_keyframes >= 3 else 2)
    coverage_slots = min(
        config.max_keyframes,
        max(
            minimum_coverage,
            min(desired_coverage, math.ceil(config.max_keyframes * 0.6)),
        ),
    )
    candidate_tail = float(candidates[-1]["timestamp"])
    targets = (
        [0.0]
        if coverage_slots == 1
        else [index * candidate_tail / (coverage_slots - 1) for index in range(coverage_slots)]
    )
    selected_indices: list[int] = []
    selection_reasons: dict[int, str] = {}
    for target_index, target in enumerate(targets):
        candidates_by_distance = sorted(
            range(len(candidates)),
            key=lambda index: (
                abs(float(candidates[index]["timestamp"]) - target),
                -float(candidates[index]["scene_score"]),
                index,
            ),
        )
        endpoint = target_index in {0, len(targets) - 1}
        candidate_index = candidates_by_distance[0]
        if not endpoint:
            unique_nearby = next(
                (
                    index
                    for index in candidates_by_distance
                    if index not in selected_indices
                    and all(
                        hamming_ratio(
                            candidates[index]["signature"], candidates[selected]["signature"]
                        )
                        > config.duplicate_hamming_ratio
                        for selected in selected_indices
                    )
                ),
                None,
            )
            if unique_nearby is None:
                continue
            candidate_index = unique_nearby
        if candidate_index not in selected_indices:
            selected_indices.append(candidate_index)
        if target_index == 0:
            selection_reasons[candidate_index] = "first"
        elif target_index == len(targets) - 1 and selection_reasons.get(candidate_index) != "first":
            selection_reasons[candidate_index] = "tail"
        else:
            selection_reasons.setdefault(candidate_index, "coverage")

    ranked = sorted(
        (index for index in range(len(candidates)) if index not in selected_indices),
        key=lambda index: (
            -float(candidates[index]["scene_score"]),
            float(candidates[index]["timestamp"]),
        ),
    )
    for candidate_index in ranked:
        if len(selected_indices) >= config.max_keyframes:
            break
        signature = candidates[candidate_index]["signature"]
        distances = [
            hamming_ratio(signature, candidates[index]["signature"]) for index in selected_indices
        ]
        if distances and min(distances) <= config.duplicate_hamming_ratio:
            continue
        selected_indices.append(candidate_index)
    minimum_output = min(config.max_keyframes, len(candidates), 3)
    fallback_targets = (
        []
        if minimum_output == 0
        else (
            [candidate_tail / 2]
            if minimum_output == 1
            else [index * candidate_tail / (minimum_output - 1) for index in range(minimum_output)]
        )
    )
    for target in fallback_targets:
        if len(selected_indices) >= minimum_output:
            break
        if any(
            abs(float(candidates[index]["timestamp"]) - target) < 1e-6
            for index in selected_indices
        ):
            continue
        candidate_index = min(
            (index for index in range(len(candidates)) if index not in selected_indices),
            key=lambda index: (abs(float(candidates[index]["timestamp"]) - target), index),
            default=None,
        )
        if candidate_index is not None:
            selected_indices.append(candidate_index)
            selection_reasons.setdefault(candidate_index, "minimum_coverage")

    selected_indices.sort(key=lambda index: float(candidates[index]["timestamp"]))
    selected = [candidates[index] for index in selected_indices]
    capture = deps.cv2.VideoCapture(str(video))
    records: list[dict[str, Any]] = []
    emitted_signatures: list[bytes] = []
    try:
        if not capture.isOpened():
            raise AnalysisError("video_open_failed", "OpenCV 无法为关键帧提取打开 MP4")
        for index, (candidate_index, candidate) in enumerate(
            zip(selected_indices, selected, strict=True), 1
        ):
            timestamp = float(candidate["timestamp"])
            capture.set(deps.cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
            ok, frame = capture.read()
            if not ok:
                raise AnalysisError("keyframe_extraction_failed", "关键帧回读失败")
            signature = candidate["signature"]
            nearest_distance = min(
                (hamming_ratio(signature, value) for value in emitted_signatures),
                default=1.0,
            )
            filename = f"frame-{index:03d}-{round(timestamp * 1000):09d}ms.jpg"
            target = output_dir / filename
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp.jpg")
            try:
                if not deps.cv2.imwrite(str(temp), frame, [deps.cv2.IMWRITE_JPEG_QUALITY, 92]):
                    raise AnalysisError("keyframe_write_failed", f"无法写入关键帧 {filename}")
                os.replace(temp, target)
            finally:
                with contextlib.suppress(OSError):
                    temp.unlink(missing_ok=True)
            record = {
                "id": index,
                "timestamp": round(timestamp, 3),
                "file": f"keyframes/{filename}",
                "reason": selection_reasons.get(candidate_index, str(candidate["reason"])),
                "scene_score": round(float(candidate["scene_score"]), 6),
                "nearest_hash_distance": round(nearest_distance, 6),
            }
            records.append(record)
            emitted_signatures.append(signature)
    finally:
        capture.release()

    selected_timestamps = [float(item["timestamp"]) for item in selected]
    boundaries = [0.0, *selected_timestamps, duration]
    max_gap = max(
        (right - left for left, right in zip(boundaries, boundaries[1:], strict=False)),
        default=duration,
    )
    tail_gap = max(0.0, duration - selected_timestamps[-1])
    scan_tolerance = max(config.sample_interval_seconds, 2 / float(metadata["fps"]))
    coverage_report = {
        "sample_interval_seconds": config.sample_interval_seconds,
        "coverage_interval_seconds": round(coverage_interval, 3),
        "scan_sample_count": sample_count,
        "valid_sample_count": valid_sample_count,
        "scanned_until_seconds": round(last_attempted, 3),
        "expected_tail_seconds": round(expected_tail_timestamp, 3),
        "last_decodable_seconds": round(last_decodable, 3),
        "scan_reached_end": expected_tail_timestamp - last_decodable <= scan_tolerance,
        "tail_frame_readable": abs(expected_tail_timestamp - last_decodable) <= 1e-3,
        "candidate_count": len(candidates),
        "candidate_limit": None,
        "candidate_limit_reached": False,
        "output_limit": config.max_keyframes,
        "output_limit_reached": len(records) >= config.max_keyframes,
        "candidates_omitted": max(0, len(candidates) - len(records)),
        "duplicate_backfill_floor": minimum_output,
        "selected_count": len(records),
        "coverage_target_count": len(targets),
        "coverage_targets_met": len(
            {index for index in selected_indices if index in selection_reasons}
        ),
        "timeline_span_ratio": round(
            min(1.0, max(0.0, (selected_timestamps[-1] - selected_timestamps[0]) / duration))
            if duration
            else 1.0,
            6,
        ),
        "tail_gap_seconds": round(tail_gap, 3),
        "max_selected_gap_seconds": round(max_gap, 3),
    }
    return records, coverage_report


def _ocr_value(result: Any, name: str) -> Any:
    if hasattr(result, name):
        return getattr(result, name)
    if isinstance(result, dict):
        return result.get(name)
    return None


def _box_payload(box: Any) -> list[list[float]] | None:
    if box is None:
        return None
    raw = box.tolist() if hasattr(box, "tolist") else box
    try:
        return [[round(float(value), 3) for value in point] for point in raw]
    except (TypeError, ValueError):
        return None


def run_ocr(
    stage_dir: Path,
    keyframes: list[dict[str, Any]],
    deps: RuntimeDependencies,
) -> dict[str, Any]:
    try:
        engine = deps.rapid_ocr()
    except Exception as exc:
        raise AnalysisError("ocr_initialization_failed", f"RapidOCR 本地初始化失败：{exc}") from exc
    frames: list[dict[str, Any]] = []
    for keyframe in keyframes:
        try:
            result = engine(str(stage_dir / str(keyframe["file"])))
            boxes = _ocr_value(result, "boxes")
            texts = _ocr_value(result, "txts")
            scores = _ocr_value(result, "scores")
            if boxes is None and texts is None and scores is None:
                texts = []
                scores = []
                boxes = []
            elif texts is None or scores is None:
                raise TypeError("RapidOCR 返回值缺少 txts/scores")
            boxes = boxes if boxes is not None else [None] * len(texts)
            lines = []
            for box, text, score in zip(boxes, texts, scores, strict=False):
                normalized = str(text).strip()
                if not normalized:
                    continue
                lines.append(
                    {
                        "text": normalized,
                        "confidence": round(float(score), 6),
                        "box": _box_payload(box),
                    }
                )
        except AnalysisError:
            raise
        except Exception as exc:
            raise AnalysisError(
                "ocr_failed",
                f"RapidOCR 处理 {keyframe['file']} 失败：{exc}",
            ) from exc
        frames.append(
            {
                "keyframe_id": keyframe["id"],
                "timestamp": keyframe["timestamp"],
                "file": keyframe["file"],
                "lines": lines,
            }
        )
    return {"schema_version": 1, "engine": "RapidOCR", "frames": frames}


def _md_text(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def render_transcript_markdown(transcript: dict[str, Any]) -> str:
    lines = ["# 语音转写", "", "| 时间 | 文本 | 置信度 |", "| --- | --- | ---: |"]
    for segment in transcript["segments"]:
        confidence = segment["confidence"]
        confidence_text = f"{confidence:.3f}" if confidence is not None else "N/A"
        interval = f"{format_timestamp(segment['start'])}-{format_timestamp(segment['end'])}"
        lines.append(f"| {interval} | {_md_text(segment['text'])} | {confidence_text} |")
    if not transcript["segments"]:
        lines.append("| - | 未识别到语音文本 | N/A |")
    return "\n".join(lines) + "\n"


def render_ocr_markdown(ocr: dict[str, Any]) -> str:
    lines = ["# 画面文字识别", ""]
    for frame in ocr["frames"]:
        lines.extend([f"## [{format_timestamp(frame['timestamp'])}] {frame['file']}", ""])
        if not frame["lines"]:
            lines.extend(["未识别到文字。", ""])
            continue
        lines.extend(["| 文本 | 置信度 |", "| --- | ---: |"])
        for item in frame["lines"]:
            lines.append(f"| {_md_text(item['text'])} | {item['confidence']:.3f} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _segments_near(
    timestamp: float,
    segments: list[dict[str, Any]],
    radius: float = 7.5,
) -> list[dict[str, Any]]:
    return [
        segment
        for segment in segments
        if float(segment["end"]) >= timestamp - radius
        and float(segment["start"]) <= timestamp + radius
    ]


def render_timeline(transcript: dict[str, Any], ocr: dict[str, Any]) -> str:
    events: list[tuple[float, str, str, str]] = []
    for segment in transcript["segments"]:
        confidence = segment["confidence"]
        confidence_text = f"{confidence:.3f}" if confidence is not None else "N/A"
        events.append((float(segment["start"]), "ASR", str(segment["text"]), confidence_text))
    for frame in ocr["frames"]:
        for line in frame["lines"]:
            events.append(
                (float(frame["timestamp"]), "OCR", str(line["text"]), f"{line['confidence']:.3f}")
            )
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    lines = ["# 内容时间轴", "", "| 时间点 | 来源 | 内容 | 置信度 |", "| --- | --- | --- | ---: |"]
    for timestamp, source, text, confidence in events:
        lines.append(
            f"| {format_timestamp(timestamp)} | {source} | {_md_text(text)} | {confidence} |"
        )
    if not events:
        lines.append("| - | - | 未识别到可用的语音或画面文字 | N/A |")
    return "\n".join(lines) + "\n"


def render_deterministic_summary(transcript: dict[str, Any], ocr: dict[str, Any]) -> str:
    segments = transcript["segments"]
    frames = ocr["frames"]
    ocr_line_count = sum(len(frame["lines"]) for frame in frames)
    lines = [
        "# 视频内容摘要",
        "",
        "> 生成方式：确定性结构化摘要；仅重排本地 ASR/OCR 结果，不调用 LLM 或外部 API。",
        "",
        "## 内容概览",
        "",
        f"- 语音识别片段：{len(segments)} 个。",
        f"- 去重关键帧：{len(frames)} 张；OCR 文本：{ocr_line_count} 条。",
        "",
        "## 音频与视觉联合要点",
        "",
    ]
    for frame in frames:
        timestamp = float(frame["timestamp"])
        nearby = _segments_near(timestamp, segments)
        visual = "；".join(item["text"] for item in frame["lines"])
        spoken = " ".join(segment["text"] for segment in nearby)
        lines.append(f"### [{format_timestamp(timestamp)}] 关键画面")
        lines.append("")
        lines.append(f"- 画面文字（OCR）：{visual or '未识别到文字'}")
        if nearby:
            start = min(float(item["start"]) for item in nearby)
            end = max(float(item["end"]) for item in nearby)
            lines.append(
                f"- 同期语音（ASR，[{format_timestamp(start)}-{format_timestamp(end)}]）：{spoken}"
            )
        else:
            lines.append("- 同期语音（ASR）：该时间点附近未识别到语音文本。")
        lines.append("")
    covered_ids = {
        int(segment["id"])
        for frame in frames
        for segment in _segments_near(float(frame["timestamp"]), segments)
    }
    uncovered = [segment for segment in segments if int(segment["id"]) not in covered_ids]
    if uncovered:
        lines.extend(["## 其余语音证据", ""])
        for segment in uncovered:
            interval = f"{format_timestamp(segment['start'])}-{format_timestamp(segment['end'])}"
            lines.append(f"- [{interval}] {segment['text']}")
        lines.append("")
    if not frames and not segments:
        lines.extend(["没有可供摘要的 ASR/OCR 文本。", ""])
    return "\n".join(lines).rstrip() + "\n"


def _artifact_manifest(stage_dir: Path) -> dict[str, dict[str, object]]:
    artifacts: dict[str, dict[str, object]] = {}
    for path in sorted(item for item in stage_dir.rglob("*") if item.is_file()):
        relative = path.relative_to(stage_dir).as_posix()
        if relative == "manifest.json" or path.name.startswith("."):
            continue
        artifacts[relative] = {"size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return artifacts


def publish_directory(stage_dir: Path, output_dir: Path) -> None:
    backup = output_dir.with_name(f".{output_dir.name}.{uuid.uuid4().hex}.backup")
    moved_existing = False
    try:
        if output_dir.exists():
            os.replace(output_dir, backup)
            moved_existing = True
        os.replace(stage_dir, output_dir)
    except Exception:
        if moved_existing and backup.exists() and not output_dir.exists():
            os.replace(backup, output_dir)
        raise
    finally:
        if backup.exists() and output_dir.exists():
            shutil.rmtree(backup, ignore_errors=True)


def run_analysis(
    video: Path,
    output_dir: Path,
    ffmpeg: str,
    deps: RuntimeDependencies,
    config: AnalysisConfig,
    log: DiagnosticLog,
) -> dict[str, Any]:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = output_dir.with_name(f".{output_dir.name}.{uuid.uuid4().hex}.tmp")
    stage_dir.mkdir()
    try:
        log.emit("inspect", "started")
        metadata = inspect_video(video, deps)
        log.emit("inspect", "ok", **metadata)

        log.emit("audio", "started")
        audio_present = extract_audio(ffmpeg, video, stage_dir / "audio.wav") is not False
        log.emit("audio", "ok" if audio_present else "not_present")

        log.emit("asr", "started", model=config.asr_model, local_files_only=True)
        if config.transcript_override is not None:
            transcript = load_transcript_override(config.transcript_override)
            log.emit("asr", "reused_precomputed", segments=len(transcript["segments"]))
        elif audio_present:
            transcript = transcribe_audio(stage_dir / "audio.wav", config, deps)
        else:
            transcript = {
                "language": "not_applicable",
                "language_probability": None,
                "duration_seconds": float(metadata["duration_seconds"]),
                "segments": [],
            }
        try:
            asr_reported_duration = float(transcript["duration_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnalysisError(
                "asr_duration_missing",
                "ASR 未报告已处理音频时长，无法验证完整性",
            ) from exc
        source_duration = float(metadata["duration_seconds"])
        if not math.isfinite(asr_reported_duration) or asr_reported_duration <= 0:
            raise AnalysisError("asr_duration_missing", "ASR 已处理音频时长无效")
        asr_duration_ratio = asr_reported_duration / source_duration
        if not 0.95 <= asr_duration_ratio <= 1.05:
            raise AnalysisError(
                "asr_duration_mismatch",
                "ASR 已处理音频时长与源视频明显不一致",
            )
        detected_language = str(transcript.get("language") or "unknown")
        language_probability = _optional_round(transcript.get("language_probability"))
        low_confidence_segment_count = sum(
            1
            for segment in transcript["segments"]
            if segment.get("confidence") is not None and float(segment["confidence"]) < 0.5
        )
        asr_quality_status = "not_applicable"
        if audio_present or config.transcript_override is not None:
            asr_quality_status = (
                "needs_review"
                if detected_language not in {"zh", "yue"}
                or language_probability is None
                or language_probability < 0.7
                or low_confidence_segment_count > 0
                else "verified"
            )
        atomic_write_json(stage_dir / "transcript.json", transcript)
        atomic_write_text(stage_dir / "transcript.md", render_transcript_markdown(transcript))
        log.emit("asr", "ok", segments=len(transcript["segments"]))

        log.emit("keyframes", "started")
        keyframes, coverage_report = extract_keyframes(
            video, stage_dir / "keyframes", metadata, config, deps
        )
        coverage_report.update(
            {
                "source_duration_seconds": round(source_duration, 3),
                "asr_reported_duration_seconds": round(asr_reported_duration, 3),
                "asr_source_duration_ratio": round(asr_duration_ratio, 6),
                "asr_duration_status": "verified" if audio_present else "not_applicable",
                "detected_language": detected_language,
                "language_probability": language_probability,
                "low_confidence_segment_count": low_confidence_segment_count,
                "asr_quality_status": asr_quality_status,
            }
        )
        if not coverage_report["scan_reached_end"] or not coverage_report[
            "tail_frame_readable"
        ]:
            raise AnalysisError(
                "video_tail_unreadable",
                "视频尾部未能成功解码，拒绝生成不完整分析",
            )
        log.emit("keyframes", "ok", unique_frames=len(keyframes))

        log.emit("ocr", "started")
        ocr = run_ocr(stage_dir, keyframes, deps)
        atomic_write_json(stage_dir / "ocr.json", ocr)
        atomic_write_text(stage_dir / "ocr.md", render_ocr_markdown(ocr))
        ocr_line_count = sum(len(frame["lines"]) for frame in ocr["frames"])
        ocr_frames_with_text = sum(1 for frame in ocr["frames"] if frame["lines"])
        low_confidence_ocr_lines = sum(
            1
            for frame in ocr["frames"]
            for line in frame["lines"]
            if float(line["confidence"]) < 0.5
        )
        ocr_quality_status = (
            "needs_review"
            if ocr_line_count == 0 or low_confidence_ocr_lines * 4 > ocr_line_count
            else "verified"
        )
        coverage_report.update(
            {
                "ocr_quality_status": ocr_quality_status,
                "ocr_line_count": ocr_line_count,
                "ocr_frames_with_text": ocr_frames_with_text,
                "ocr_low_confidence_line_count": low_confidence_ocr_lines,
            }
        )
        log.emit(
            "ocr",
            "ok",
            frames=len(ocr["frames"]),
            lines=ocr_line_count,
            quality_status=ocr_quality_status,
        )

        atomic_write_text(stage_dir / "timeline.md", render_timeline(transcript, ocr))
        atomic_write_text(stage_dir / "summary.md", render_deterministic_summary(transcript, ocr))
        manifest = {
            "schema_version": 1,
            "analysis_version": ANALYSIS_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "source": {
                "path": str(video),
                "size_bytes": video.stat().st_size,
                "sha256": sha256_file(video),
                **metadata,
            },
            "local_only": True,
            "summary_mode": "deterministic",
            "asr": {
                "engine": "faster-whisper" if audio_present else "none",
                "audio_present": audio_present,
                "model": config.asr_model,
                "device": config.device,
                "compute_type": config.compute_type,
                "local_files_only": True,
                "segment_count": len(transcript["segments"]),
                "reported_duration_seconds": round(asr_reported_duration, 3),
                "source_duration_ratio": round(asr_duration_ratio, 6),
                "detected_language": detected_language,
                "language_probability": language_probability,
                "low_confidence_segment_count": low_confidence_segment_count,
                "quality_status": asr_quality_status,
            },
            "ocr": {
                "engine": "RapidOCR",
                "frame_count": len(ocr["frames"]),
                "line_count": ocr_line_count,
                "frames_with_text": ocr_frames_with_text,
                "low_confidence_line_count": low_confidence_ocr_lines,
                "quality_status": ocr_quality_status,
            },
            "keyframes": {
                "count": len(keyframes),
                "deduplication": "16x16 average-hash Hamming distance",
                "duplicate_hamming_ratio": config.duplicate_hamming_ratio,
                "items": keyframes,
            },
            "coverage_report": coverage_report,
            "artifacts": _artifact_manifest(stage_dir),
        }
        atomic_write_json(stage_dir / "manifest.json", manifest)
        publish_directory(stage_dir, output_dir)
        log.emit("publish", "ok", output=str(output_dir))
        return manifest
    except Exception:
        log.emit("analysis", "failed", traceback=traceback.format_exc())
        raise
    finally:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze one local collection job")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--job-id", help="stable job directory name under data/jobs")
    parser.add_argument("--input", type=Path, help="explicit MP4 path under data/jobs")
    parser.add_argument("--output", type=Path, help="explicit analysis directory under data/jobs")
    parser.add_argument("--transcript-override", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--asr-model", default=os.environ.get("FASTER_WHISPER_MODEL", "small"))
    parser.add_argument("--device", default=os.environ.get("FASTER_WHISPER_DEVICE", "cpu"))
    parser.add_argument(
        "--compute-type",
        default=os.environ.get("FASTER_WHISPER_COMPUTE_TYPE", "int8"),
    )
    return parser


def _rooted_argument(root: Path, value: Path | None) -> Path | None:
    if value is None or value.is_absolute():
        return value
    return root / value


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    job_id: str | None = None
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    try:
        log = DiagnosticLog(root / "logs" / f"analyze-job-{run_id}.jsonl")
    except OSError as exc:
        payload = {
            "status": "controlled_failure",
            "code": "log_unavailable",
            "message": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return CONTROLLED_FAILURE_EXIT
    try:
        log.emit("run", "started", local_only=True)
        video, output, job_id = resolve_job_paths(
            root,
            job_id=args.job_id,
            explicit_input=_rooted_argument(root, args.input),
            explicit_output=_rooted_argument(root, args.output),
        )
        manifest = load_current_manifest(video, output)
        reused = manifest is not None
        if manifest is None:
            deps = load_runtime_dependencies()
            ffmpeg = resolve_ffmpeg()
            transcript_override = _rooted_argument(root, args.transcript_override)
            if transcript_override is not None:
                transcript_override = transcript_override.resolve()
                try:
                    transcript_override.relative_to((root / JOBS_DIR).resolve())
                except ValueError as exc:
                    raise AnalysisError(
                        "transcript_override_outside_jobs",
                        "预计算转写必须位于 data/jobs 内",
                    ) from exc
            config = AnalysisConfig(
                asr_model=resolve_local_asr_model(root, args.asr_model),
                device=args.device,
                compute_type=args.compute_type,
                transcript_override=transcript_override,
            )
            manifest = run_analysis(video, output, ffmpeg, deps, config, log)
        else:
            log.emit("analysis", "reused", job_id=job_id)
        update_item_by_job(
            root / "data" / "knowledge.db",
            job_id,
            status="analyzed",
            media_sha256=sha256_file(video),
            preserve_completed=True,
        )
        payload = {
            "status": "ok",
            "artifacts": len(manifest["artifacts"]),
            "reused": reused,
            "log": str(log.path),
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except AnalysisError as exc:
        if job_id is not None:
            update_item_by_job(
                root / "data" / "knowledge.db",
                job_id,
                status="failed",
                error=exc.code,
                preserve_completed=True,
            )
        log.emit("run", "controlled_failure", code=exc.code, message=str(exc))
        payload = {
            "status": "controlled_failure",
            "code": exc.code,
            "message": str(exc),
            "log": str(log.path),
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return CONTROLLED_FAILURE_EXIT
    except Exception:
        log.emit("run", "internal_failure", traceback=traceback.format_exc())
        payload = {
            "status": "internal_failure",
            "code": "internal_error",
            "message": "分析失败，详细信息见诊断日志",
            "log": str(log.path),
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return INTERNAL_FAILURE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
