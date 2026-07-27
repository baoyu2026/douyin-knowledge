import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app import analyze_video
from app.analyze_video import (
    AnalysisConfig,
    AnalysisError,
    DiagnosticLog,
    RuntimeDependencies,
    discover_probe_video,
    extract_keyframes,
    hamming_ratio,
    load_runtime_dependencies,
    render_deterministic_summary,
    resolve_local_asr_model,
    run_analysis,
    run_ocr,
    transcribe_audio,
)

FIXTURES = Path(__file__).parent / "fixtures" / "analysis"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_discover_probe_video_requires_exactly_one_local_mp4(tmp_path: Path) -> None:
    probe = tmp_path / "data" / "probe-collect"
    probe.mkdir(parents=True)
    video = probe / "sample.mp4"
    video.write_bytes(b"local fixture")

    assert discover_probe_video(tmp_path) == video

    (probe / "second.mp4").write_bytes(b"second")
    with pytest.raises(AnalysisError, match="必须且只能包含 1 个 MP4"):
        discover_probe_video(tmp_path)

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    with pytest.raises(AnalysisError, match="必须位于 data/probe-collect"):
        discover_probe_video(tmp_path, outside)


def test_resolve_local_asr_model_uses_project_cache(tmp_path: Path) -> None:
    snapshot = (
        tmp_path
        / "data"
        / "models"
        / "huggingface"
        / "hub"
        / "models--Systran--faster-whisper-small"
        / "snapshots"
        / "revision"
    )
    snapshot.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (snapshot / name).write_bytes(b"fixture")

    assert resolve_local_asr_model(tmp_path, "small") == str(snapshot.resolve())
    assert resolve_local_asr_model(tmp_path, "missing") == "missing"


def test_missing_dependencies_are_a_controlled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analyze_video.importlib.util, "find_spec", lambda _name: None)

    with pytest.raises(AnalysisError) as error:
        load_runtime_dependencies()

    assert error.value.code == "missing_dependencies"
    assert "faster-whisper" in str(error.value)
    assert "rapidocr" in str(error.value)
    assert "pip install -e ." in str(error.value)


def test_hamming_ratio_exposes_deterministic_keyframe_dedup_distance() -> None:
    assert hamming_ratio(b"\x00\x00", b"\x00\x00") == 0.0
    assert hamming_ratio(b"\x00", b"\xff") == 1.0
    assert hamming_ratio(b"\x00", b"\x01") == 0.125


def test_extract_audio_reports_video_without_audio_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = SimpleNamespace(
        returncode=1,
        stderr=b"Stream map '0:a:0' matches no streams.",
    )
    monkeypatch.setattr(analyze_video.subprocess, "run", lambda *_args, **_kwargs: result)
    output = tmp_path / "audio.wav"

    assert analyze_video.extract_audio("ffmpeg", tmp_path / "silent.mp4", output) is False
    assert not output.exists()


def test_keyframe_selection_scans_tail_before_deduplicating_global_output(tmp_path: Path) -> None:
    class FakeCapture:
        def __init__(self) -> None:
            self.timestamp = 0.0

        def isOpened(self):
            return True

        def set(self, _property, value):
            self.timestamp = value / 1000.0

        def read(self):
            value = int(self.timestamp * 80) % 256
            return True, np.full((8, 8), value, dtype=np.uint8)

        def release(self):
            return None

    class FakeCV2:
        CAP_PROP_POS_MSEC = 1
        COLOR_BGR2GRAY = 2
        INTER_AREA = 3
        IMWRITE_JPEG_QUALITY = 4

        @staticmethod
        def VideoCapture(_path):
            return FakeCapture()

        @staticmethod
        def cvtColor(frame, _mode):
            return frame

        @staticmethod
        def resize(frame, size, interpolation):
            assert interpolation == FakeCV2.INTER_AREA
            return np.full((size[1], size[0]), int(frame.mean()), dtype=np.uint8)

        @staticmethod
        def absdiff(left, right):
            return np.abs(left.astype(np.int16) - right.astype(np.int16))

        @staticmethod
        def imwrite(path, _frame, _options):
            Path(path).write_bytes(b"jpeg")
            return True

    video = tmp_path / "source.mp4"
    video.write_bytes(b"fixture")
    frames, coverage = extract_keyframes(
        video,
        tmp_path / "keyframes",
        {
            "duration_seconds": 10.0,
            "fps": 10.0,
            "frame_count": 101,
            "width": 8,
            "height": 8,
        },
        AnalysisConfig(max_keyframes=4),
        RuntimeDependencies(FakeCV2, np, None, None),
    )

    assert len(frames) == 3
    assert frames[0]["timestamp"] == 0.0
    assert frames[-1]["timestamp"] == 10.0
    assert coverage["sample_interval_seconds"] == 0.2
    assert coverage["scanned_until_seconds"] == 10.0
    assert coverage["scan_reached_end"] is True
    assert coverage["tail_frame_readable"] is True
    assert coverage["candidate_count"] > 4
    assert coverage["output_limit_reached"] is False
    assert coverage["candidates_omitted"] > 0
    assert coverage["coverage_targets_met"] == coverage["coverage_target_count"]
    assert coverage["timeline_span_ratio"] == 1.0


def test_keyframe_selection_does_not_fill_the_limit_with_duplicate_frames(
    tmp_path: Path,
) -> None:
    class StaticCapture:
        def __init__(self) -> None:
            self.timestamp = 0.0

        def isOpened(self):
            return True

        def set(self, _property, value):
            self.timestamp = value / 1000.0

        def read(self):
            return True, np.zeros((8, 8), dtype=np.uint8)

        def release(self):
            return None

    class StaticCV2:
        CAP_PROP_POS_MSEC = 1
        COLOR_BGR2GRAY = 2
        INTER_AREA = 3
        IMWRITE_JPEG_QUALITY = 4

        @staticmethod
        def VideoCapture(_path):
            return StaticCapture()

        @staticmethod
        def cvtColor(frame, _mode):
            return frame

        @staticmethod
        def resize(frame, size, interpolation):
            assert interpolation == StaticCV2.INTER_AREA
            return np.zeros((size[1], size[0]), dtype=np.uint8)

        @staticmethod
        def absdiff(left, right):
            return np.abs(left.astype(np.int16) - right.astype(np.int16))

        @staticmethod
        def imwrite(path, _frame, _options):
            Path(path).write_bytes(b"jpeg")
            return True

    video = tmp_path / "source.mp4"
    video.write_bytes(b"fixture")

    frames, coverage = extract_keyframes(
        video,
        tmp_path / "keyframes",
        {
            "duration_seconds": 60.0,
            "fps": 10.0,
            "frame_count": 601,
            "width": 8,
            "height": 8,
        },
        AnalysisConfig(max_keyframes=40),
        RuntimeDependencies(StaticCV2, np, None, None),
    )

    assert len(frames) == 3
    assert frames[0]["timestamp"] == 0.0
    assert frames[1]["timestamp"] == pytest.approx(30.0, abs=0.5)
    assert frames[2]["timestamp"] == 60.0
    assert coverage["output_limit_reached"] is False
    assert coverage["candidates_omitted"] > 0
    assert coverage["duplicate_backfill_floor"] == 3


def test_transcribe_audio_keeps_timestamps_and_real_confidence(tmp_path: Path) -> None:
    calls = {}
    word = SimpleNamespace(start=1.1, end=1.4, word="中文", probability=0.91)
    segment = SimpleNamespace(
        start=1.0,
        end=2.0,
        text=" 中文 ",
        avg_logprob=-0.25,
        no_speech_prob=0.02,
        words=[word],
    )
    info = SimpleNamespace(language="zh", language_probability=0.98, duration=143.0)

    class FakeModel:
        def __init__(self, model, **kwargs):
            calls["init"] = (model, kwargs)

        def transcribe(self, audio, **kwargs):
            calls["transcribe"] = (audio, kwargs)
            return iter([segment]), info

    deps = RuntimeDependencies(None, None, FakeModel, None)
    audio = tmp_path / "audio.wav"
    audio.touch()

    result = transcribe_audio(audio, AnalysisConfig(asr_model="local-model"), deps)

    assert calls["init"][1]["local_files_only"] is True
    assert calls["transcribe"][1]["word_timestamps"] is True
    assert result["segments"][0]["start"] == 1.0
    assert result["segments"][0]["confidence"] == 0.91
    assert result["segments"][0]["words"][0]["confidence"] == 0.91


def test_run_ocr_uses_current_rapidocr_output_fields(tmp_path: Path) -> None:
    image = tmp_path / "keyframes" / "one.jpg"
    image.parent.mkdir()
    image.touch()

    class FakeRapidOCR:
        def __call__(self, path):
            assert path == str(image)
            return SimpleNamespace(
                boxes=[[[0, 0], [1, 0], [1, 1], [0, 1]]],
                txts=[" 画面原文 "],
                scores=[0.87654],
            )

    deps = RuntimeDependencies(None, None, None, FakeRapidOCR)
    keyframes = [{"id": 1, "timestamp": 3.5, "file": "keyframes/one.jpg"}]

    result = run_ocr(tmp_path, keyframes, deps)

    assert result["frames"][0]["timestamp"] == 3.5
    assert result["frames"][0]["lines"] == [
        {
            "text": "画面原文",
            "confidence": 0.87654,
            "box": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
        }
    ]


def test_run_ocr_treats_all_none_fields_as_an_empty_frame(tmp_path: Path) -> None:
    image = tmp_path / "keyframes" / "blank.jpg"
    image.parent.mkdir()
    image.touch()

    class FakeRapidOCR:
        def __call__(self, path):
            assert path == str(image)
            return SimpleNamespace(boxes=None, txts=None, scores=None)

    deps = RuntimeDependencies(None, None, None, FakeRapidOCR)
    keyframes = [{"id": 1, "timestamp": 4.0, "file": "keyframes/blank.jpg"}]

    result = run_ocr(tmp_path, keyframes, deps)

    assert result["frames"][0]["lines"] == []


def test_run_ocr_rejects_partially_missing_output_fields(tmp_path: Path) -> None:
    image = tmp_path / "keyframes" / "malformed.jpg"
    image.parent.mkdir()
    image.touch()

    class FakeRapidOCR:
        def __call__(self, _path):
            return SimpleNamespace(boxes=[], txts=["文字"], scores=None)

    deps = RuntimeDependencies(None, None, None, FakeRapidOCR)
    keyframes = [{"id": 1, "timestamp": 5.0, "file": "keyframes/malformed.jpg"}]

    with pytest.raises(AnalysisError) as error:
        run_ocr(tmp_path, keyframes, deps)

    assert error.value.code == "ocr_failed"
    assert "缺少 txts/scores" in str(error.value)


def test_deterministic_summary_merges_audio_and_visual_with_timestamps() -> None:
    summary = render_deterministic_summary(
        load_fixture("transcript.json"),
        load_fixture("ocr.json"),
    )

    assert "确定性结构化摘要" in summary
    assert "[00:10]" in summary
    assert "[00:09.500-00:12]" in summary
    assert "这是本地语音证据" in summary
    assert "这是本地画面证据" in summary
    assert "外部 API" in summary


def install_mock_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = load_fixture("transcript.json")
    ocr = load_fixture("ocr.json")

    monkeypatch.setattr(
        analyze_video,
        "inspect_video",
        lambda _video, _deps: {
            "duration_seconds": 143.0,
            "fps": 25.0,
            "frame_count": 3575,
            "width": 1080,
            "height": 1920,
        },
    )

    def fake_audio(_ffmpeg, _video, output):
        output.write_bytes(b"fixture wav")

    def fake_keyframes(_video, output, _metadata, _config, _deps):
        output.mkdir()
        frame = output / "frame-001-000010000ms.jpg"
        frame.write_bytes(b"fixture jpeg")
        return (
            [
                {
                    "id": 1,
                    "timestamp": 10.0,
                    "file": f"keyframes/{frame.name}",
                    "reason": "first",
                    "scene_score": 1.0,
                    "nearest_hash_distance": 1.0,
                }
            ],
            {
                "scan_reached_end": True,
                "tail_frame_readable": True,
                "candidate_count": 1,
                "selected_count": 1,
            },
        )

    monkeypatch.setattr(analyze_video, "extract_audio", fake_audio)
    monkeypatch.setattr(analyze_video, "transcribe_audio", lambda *_args: transcript)
    monkeypatch.setattr(analyze_video, "extract_keyframes", fake_keyframes)
    monkeypatch.setattr(analyze_video, "run_ocr", lambda *_args: ocr)


def test_run_analysis_publishes_complete_fixed_artifact_set_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_pipeline(monkeypatch)
    video = tmp_path / "data" / "probe-collect" / "sample.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fixture mp4")
    output = tmp_path / "data" / "analysis" / "collect-probe-sample"
    output.mkdir(parents=True)
    (output / "old-result.txt").write_text("old", encoding="utf-8")
    log = DiagnosticLog(tmp_path / "logs" / "analysis.jsonl")

    manifest = run_analysis(
        video,
        output,
        "mock-ffmpeg",
        RuntimeDependencies(None, None, None, None),
        AnalysisConfig(asr_model="fixture-model"),
        log,
    )

    expected = {
        "audio.wav",
        "transcript.json",
        "transcript.md",
        "ocr.json",
        "ocr.md",
        "timeline.md",
        "summary.md",
        "manifest.json",
    }
    assert expected <= {path.name for path in output.iterdir()}
    assert (output / "keyframes").is_dir()
    assert not (output / "old-result.txt").exists()
    assert manifest["summary_mode"] == "deterministic"
    assert manifest["local_only"] is True
    assert manifest["keyframes"]["count"] == 1
    assert manifest["coverage_report"]["asr_duration_status"] == "verified"
    assert manifest["coverage_report"]["asr_source_duration_ratio"] == 1.0
    assert not list(output.parent.glob(".collect-probe-sample.*.tmp"))


def test_run_analysis_rejects_obviously_incomplete_asr_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_pipeline(monkeypatch)
    transcript = load_fixture("transcript.json")
    transcript["duration_seconds"] = 10.0
    monkeypatch.setattr(analyze_video, "transcribe_audio", lambda *_args: transcript)
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"fixture mp4")
    output = tmp_path / "analysis" / "sample"

    with pytest.raises(AnalysisError) as error:
        run_analysis(
            video,
            output,
            "mock-ffmpeg",
            RuntimeDependencies(None, None, None, None),
            AnalysisConfig(asr_model="fixture-model"),
            DiagnosticLog(tmp_path / "logs" / "analysis.jsonl"),
        )

    assert error.value.code == "asr_duration_mismatch"
    assert not output.exists()


def test_run_analysis_rejects_unreadable_exact_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_mock_pipeline(monkeypatch)
    original = analyze_video.extract_keyframes

    def unreadable_tail(*args, **kwargs):
        frames, coverage = original(*args, **kwargs)
        coverage["tail_frame_readable"] = False
        return frames, coverage

    monkeypatch.setattr(analyze_video, "extract_keyframes", unreadable_tail)
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"fixture mp4")

    with pytest.raises(AnalysisError) as error:
        run_analysis(
            video,
            tmp_path / "analysis" / "sample",
            "mock-ffmpeg",
            RuntimeDependencies(None, None, None, None),
            AnalysisConfig(asr_model="fixture-model"),
            DiagnosticLog(tmp_path / "logs" / "analysis.jsonl"),
        )

    assert error.value.code == "video_tail_unreadable"


def test_run_analysis_continues_visual_pipeline_without_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_mock_pipeline(monkeypatch)
    monkeypatch.setattr(analyze_video, "extract_audio", lambda *_args: False)

    def unexpected_asr(*_args):
        raise AssertionError("ASR must not run when the video has no audio stream")

    monkeypatch.setattr(analyze_video, "transcribe_audio", unexpected_asr)
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"fixture mp4")
    output = tmp_path / "analysis" / "sample"

    manifest = run_analysis(
        video,
        output,
        "mock-ffmpeg",
        RuntimeDependencies(None, None, None, None),
        AnalysisConfig(asr_model="fixture-model"),
        DiagnosticLog(tmp_path / "logs" / "analysis.jsonl"),
    )

    transcript = json.loads((output / "transcript.json").read_text(encoding="utf-8"))
    assert transcript["segments"] == []
    assert not (output / "audio.wav").exists()
    assert manifest["asr"]["audio_present"] is False
    assert manifest["asr"]["quality_status"] == "not_applicable"
    assert manifest["coverage_report"]["asr_duration_status"] == "not_applicable"
    assert analyze_video.load_current_manifest(video, output) is not None


def test_run_analysis_marks_empty_ocr_as_needing_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_mock_pipeline(monkeypatch)
    monkeypatch.setattr(
        analyze_video,
        "run_ocr",
        lambda *_args: {
            "schema_version": 1,
            "engine": "RapidOCR",
            "frames": [{"id": 1, "timestamp": 10.0, "file": "one.jpg", "lines": []}],
        },
    )
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"fixture mp4")

    manifest = run_analysis(
        video,
        tmp_path / "analysis" / "sample",
        "mock-ffmpeg",
        RuntimeDependencies(None, None, None, None),
        AnalysisConfig(asr_model="fixture-model"),
        DiagnosticLog(tmp_path / "logs" / "analysis.jsonl"),
    )

    assert manifest["ocr"]["quality_status"] == "needs_review"
    assert manifest["coverage_report"]["ocr_line_count"] == 0


def test_run_analysis_failure_keeps_previous_output_and_diagnostic_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_pipeline(monkeypatch)
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"fixture mp4")
    output = tmp_path / "analysis" / "probe-sample"
    output.mkdir(parents=True)
    previous = output / "summary.md"
    previous.write_text("previous result", encoding="utf-8")
    log = DiagnosticLog(tmp_path / "logs" / "analysis.jsonl")

    def fail_asr(*_args):
        raise AnalysisError("asr_failed", "fixture failure")

    monkeypatch.setattr(analyze_video, "transcribe_audio", fail_asr)

    with pytest.raises(AnalysisError, match="fixture failure"):
        run_analysis(
            video,
            output,
            "mock-ffmpeg",
            RuntimeDependencies(None, None, None, None),
            AnalysisConfig(),
            log,
        )

    assert previous.read_text(encoding="utf-8") == "previous result"
    assert not list(output.parent.glob(".collect-probe-sample.*.tmp"))
    assert '"status": "failed"' in log.path.read_text(encoding="utf-8")
