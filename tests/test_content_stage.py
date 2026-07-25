# ruff: noqa: E501
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.collection_registry import begin_snapshot, complete_snapshot, ingest_snapshot_page
from app.content_stage import (
    ContentStageError,
    _runner_command,
    build_runner_prompt,
    run_content_stage,
    validate_content_draft,
)
from app.probe_one import stable_job_id
from app.publish_library import publish_job


def _make_analyzed_job(root: Path, *, frame_count: int = 3) -> str:
    source_id = "private-content-stage"
    snapshot = begin_snapshot(root / "data" / "knowledge.db")
    ingest_snapshot_page(
        root / "data" / "knowledge.db",
        snapshot_id=snapshot.snapshot_id,
        cursor=0,
        next_cursor=None,
        has_more=False,
        items=[{"aweme_id": source_id}],
    )
    complete_snapshot(root / "data" / "knowledge.db", snapshot.snapshot_id)
    job_id = stable_job_id({"aweme_id": source_id})
    job_dir = root / "data" / "jobs" / job_id
    analysis = job_dir / "analysis"
    keyframes = analysis / "keyframes"
    keyframes.mkdir(parents=True)
    (job_dir / "source.mp4").write_bytes(b"high quality fixture video")
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "source": {"position": 1},
                "title": "技术证据整理样板",
                "author": "测试作者",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    items = []
    for index in range(frame_count):
        name = f"frame-{index + 1:03d}.jpg"
        (keyframes / name).write_bytes(f"frame-{index}".encode())
        items.append({"file": f"keyframes/{name}", "timestamp": index})
    (analysis / "manifest.json").write_text(
        json.dumps(
            {
                "source": {"duration_seconds": 12.0},
                "keyframes": {"items": items},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    transcript = {"segments": [{"start": 0, "end": 2, "text": "完整中文转写证据"}]}
    (analysis / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
    )
    (analysis / "ocr.json").write_text(
        json.dumps({"items": [{"text": "画面中文证据"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (analysis / "summary.md").write_text("# 技术分析\n\n技术分析完整。\n", encoding="utf-8")
    (analysis / "timeline.md").write_text("# 内容时间轴\n", encoding="utf-8")
    related = root / "library" / "参考分类" / "参考知识"
    related.mkdir(parents=True)
    (related / "内容整理.md").write_text(
        "---\n标题: 已有参考知识\n主分类: 参考分类\n标签: [参考]\n---\n\n# 已有参考知识\n",
        encoding="utf-8",
    )
    with sqlite3.connect(root / "data" / "knowledge.db") as connection:
        connection.execute(
            "UPDATE collection_items SET status = 'analyzed' WHERE job_id = ?", (job_id,)
        )
    return job_id


def _draft_text(*, category: str = "软件工程", tags: str = "[内容治理, 事实复核]") -> str:
    sections = {
        "基本信息": "本稿依据完整转写、OCR、技术分析和选定关键帧整理。",
        "一句话总结": "高质量内容整理必须把事实复核和发布门禁放在自动完成之前。",
        "内容摘要": "该材料说明技术分析只是输入，最终知识稿仍需完成结构化重写、证据核对、分类校正和相关知识连接。整理结果应能独立阅读，也应明确哪些结论来自语音、画面或已有知识。",
        "核心观点": "- 技术产物不能直接冒充内容成品。\n- 每项高风险事实都需要可追溯的复核结论。",
        "论证结构": "先检查输入完整性，再生成候选内容，然后执行格式、事实、隐私和发布门禁，最后写入知识库。",
        "关键案例与数据": "本样板没有需要传播的外部统计数字，重点是展示可恢复的质量门禁。",
        "专有名词与数字复核": "专有名词 Content Runner 已依据技术材料统一；本样板没有待传播数字。",
        "时间轴": "开头说明输入，中段完成复核，结尾给出发布条件和恢复策略。",
        "可复用知识": "将生成和发布拆成独立阶段，并以结构化契约连接，可以让失败停留在可恢复状态。",
        "行动建议": "先验证稿件，再写 Library 与 Vault；任何门禁失败都不得更新完成状态。",
        "关键词": "内容治理、事实复核、高质量发布、可恢复流程",
        "相关内容": "关联已有参考知识，用于比较技术分析与最终知识整理的职责边界。",
        "画面证据": "关键帧展示了与口述一致的中文证据，支持流程必须经过复核的结论。",
        "待复核项": "当前没有待复核事项；后续输入变化时重新执行全部门禁。",
    }
    body = "\n\n".join(f"## {heading}\n\n{text}" for heading, text in sections.items())
    return f"""---
schema_version: 1
title: 高质量内容门禁样板
primary_category: {category}
tags: {tags}
review_status: verified
proper_noun_review:
  - term: Content Runner
    normalized: Content Runner
    evidence: 技术分析与完整转写一致
    verdict: verified
numeric_review:
  - value: none
    normalized: 无需复核数字
    evidence: 正文未传播统计数字
    verdict: not_applicable
related_knowledge:
  - title: 已有参考知识
    path: library/参考分类/参考知识/内容整理.md
    reason: 用于比较技术输入与知识成品
visual_evidence:
  - frame: frame-001.jpg
    claim: 画面中文证据与口述一致
  - frame: frame-002.jpg
    claim: The second inspected frame supplies distinct visual evidence for publication.
  - frame: frame-003.jpg
    claim: The third inspected frame supplies distinct visual evidence for publication.
pending_review: []
---

# 高质量内容门禁样板

{body}
"""


def _write_draft(root: Path, text: str | None = None) -> Path:
    path = root / "orchestration" / "content-drafts" / "fixture-content.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or _draft_text(), encoding="utf-8")
    return path


def test_low_quality_ocr_cannot_be_marked_verified(tmp_path: Path) -> None:
    job_id = _make_analyzed_job(tmp_path)
    manifest_path = tmp_path / "data" / "jobs" / job_id / "analysis" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["coverage_report"] = {"ocr_quality_status": "needs_review"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    draft = _write_draft(tmp_path)

    with pytest.raises(ContentStageError) as error:
        validate_content_draft(tmp_path, job_id, draft)
    assert error.value.code == "content_review_status_invalid"

    draft.write_text(
        draft.read_text(encoding="utf-8").replace(
            "review_status: verified", "review_status: needs_review"
        ),
        encoding="utf-8",
    )
    assert validate_content_draft(tmp_path, job_id, draft).evidence_status == "needs_review"


def test_runner_prompt_contains_complete_utf8_inputs_and_images(tmp_path: Path) -> None:
    job_id = _make_analyzed_job(tmp_path)
    prompt, images = build_runner_prompt(tmp_path, job_id)
    assert "完整中文转写证据" in prompt
    assert "画面中文证据" in prompt
    assert "library/参考分类/参考知识/内容整理.md" in prompt
    assert "review_status` 只能是英文枚举 `verified` 或 `needs_review`" in prompt
    assert "proper_noun_review[].verdict` 与 `numeric_review[].verdict`" in prompt
    assert "`verified`、`unresolved` 或 `not_applicable`" in prompt
    assert "不得把全文包在 Markdown 代码围栏" in prompt
    assert len(images) == 3
    assert all(image.is_file() for image in images)


@pytest.mark.parametrize("value", ["已复核", "approved", "VERIFIED"])
def test_draft_rejects_non_contract_review_status(tmp_path: Path, value: str) -> None:
    job_id = _make_analyzed_job(tmp_path)
    text = _draft_text().replace("review_status: verified", f"review_status: {value}")
    with pytest.raises(ContentStageError) as error:
        validate_content_draft(tmp_path, job_id, _write_draft(tmp_path, text))
    assert error.value.code == "content_review_status_invalid"


def test_draft_rejects_non_contract_review_verdict(tmp_path: Path) -> None:
    job_id = _make_analyzed_job(tmp_path)
    text = _draft_text().replace("verdict: verified", "verdict: 已复核", 1)
    with pytest.raises(ContentStageError) as error:
        validate_content_draft(tmp_path, job_id, _write_draft(tmp_path, text))
    assert error.value.code == "content_review_verdict_invalid"


def test_runner_command_is_replaceable_and_forces_safe_mode(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "runner.yml"
    config.write_text(
        "version: 1\n"
        "executable: replacement-runner\n"
        "arguments: [--ephemeral, --sandbox, read-only, '{root}', '{output}', '{images}']\n"
        "timeout_seconds: 60\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.content_stage.shutil.which", lambda value: f"resolved-{value}")
    output = tmp_path / "draft.md"
    image = tmp_path / "frame.jpg"
    command, timeout, protocol = _runner_command(
        config, root=tmp_path, output=output, images=[image]
    )
    assert command[0] == "resolved-replacement-runner"
    assert command[-2:] == ["--image", str(image)]
    assert str(tmp_path) in command
    assert str(output) in command
    assert timeout == 60
    assert protocol == "file"


def test_valid_draft_passes_all_content_gates(tmp_path: Path) -> None:
    job_id = _make_analyzed_job(tmp_path)
    draft = validate_content_draft(tmp_path, job_id, _write_draft(tmp_path))
    assert draft.category == "软件工程"
    assert draft.tags == ["内容治理", "事实复核"]
    assert draft.review_status == "已复核"


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        ("proper_noun_review:\n  - verdict: verified", "content_review_incomplete"),
        ("primary_category: 人工智能与数字工具", "content_category_invalid"),
        ("tags: [待复核, 知识管理]", "content_tags_invalid"),
        (
            "path: library/参考分类/不存在/内容整理.md",
            "content_links_invalid",
        ),
    ],
)
def test_draft_rejects_missing_fact_category_tag_and_link_gates(
    tmp_path: Path, replacement: str, code: str
) -> None:
    job_id = _make_analyzed_job(tmp_path)
    text = _draft_text()
    if replacement.startswith("proper_noun"):
        text = text.replace(
            "proper_noun_review:\n  - term: Content Runner\n    normalized: Content Runner\n    evidence: 技术分析与完整转写一致\n    verdict: verified",
            replacement,
        )
    elif replacement.startswith("primary_category"):
        text = text.replace("primary_category: 软件工程", replacement)
    elif replacement.startswith("tags"):
        text = text.replace("tags: [内容治理, 事实复核]", replacement)
    else:
        text = text.replace("path: library/参考分类/参考知识/内容整理.md", replacement)
    with pytest.raises(ContentStageError) as error:
        validate_content_draft(tmp_path, job_id, _write_draft(tmp_path, text))
    assert error.value.code == code


def test_draft_rejects_unreviewed_number_and_private_fields(tmp_path: Path) -> None:
    job_id = _make_analyzed_job(tmp_path)
    numbered = _draft_text().replace("没有需要传播的外部统计数字", "准确率达到 95%")
    with pytest.raises(ContentStageError) as number_error:
        validate_content_draft(tmp_path, job_id, _write_draft(tmp_path, numbered))
    assert number_error.value.code == "content_numbers_unreviewed"

    private = _draft_text() + "\nrequest_url: forbidden\n"
    with pytest.raises(ContentStageError) as private_error:
        validate_content_draft(tmp_path, job_id, _write_draft(tmp_path, private))
    assert private_error.value.code == "content_privacy_rejected"


def test_numeric_gate_ignores_related_links_but_checks_visible_body_text(
    tmp_path: Path,
) -> None:
    job_id = _make_analyzed_job(tmp_path)
    base = _draft_text()
    linked = base.replace(
        "关联已有参考知识",
        "关联[已有参考知识](library/01-输入/02-参考知识/内容整理.md)",
    )
    validate_content_draft(tmp_path, job_id, _write_draft(tmp_path, linked))

    related_title_number = linked.replace("[已有参考知识]", "[第2份已有参考知识]")
    validate_content_draft(
        tmp_path, job_id, _write_draft(tmp_path, related_title_number)
    )

    visible_number = linked.replace(
        "该材料说明技术分析只是输入",
        "该材料说明第2份技术分析只是输入",
    )
    with pytest.raises(ContentStageError) as visible_number_error:
        validate_content_draft(
            tmp_path, job_id, _write_draft(tmp_path, visible_number)
        )
    assert visible_number_error.value.code == "content_numbers_unreviewed"


def test_numeric_gate_ignores_source_timecodes_but_checks_years(
    tmp_path: Path,
) -> None:
    job_id = _make_analyzed_job(tmp_path)
    base = _draft_text()
    cited = base.replace(
        "先检查输入完整性",
        "证据见 02:17 至 03:29；先检查输入完整性",
    )
    validate_content_draft(tmp_path, job_id, _write_draft(tmp_path, cited))

    year_claim = cited.replace(
        "该材料说明技术分析只是输入",
        "2026年该材料说明技术分析只是输入",
    )
    with pytest.raises(ContentStageError) as year_error:
        validate_content_draft(tmp_path, job_id, _write_draft(tmp_path, year_claim))
    assert year_error.value.code == "content_numbers_unreviewed"


def test_numeric_gate_checks_body_numbers_and_accepts_registered_numbers(
    tmp_path: Path,
) -> None:
    job_id = _make_analyzed_job(tmp_path)
    numbered = _draft_text().replace("没有需要传播的外部统计数字", "准确率达到 95%")
    with pytest.raises(ContentStageError) as unreviewed_error:
        validate_content_draft(tmp_path, job_id, _write_draft(tmp_path, numbered))
    assert unreviewed_error.value.code == "content_numbers_unreviewed"

    reviewed = numbered.replace(
        "  - value: none\n    normalized: 无需复核数字\n    evidence: 正文未传播统计数字\n    verdict: not_applicable",
        "  - value: 95%\n    normalized: 95%\n    evidence: 正文明确写出准确率达到 95%\n    verdict: verified",
    )
    validate_content_draft(tmp_path, job_id, _write_draft(tmp_path, reviewed))


def test_no_draft_never_marks_completed(tmp_path: Path) -> None:
    job_id = _make_analyzed_job(tmp_path)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    publish_job(
        tmp_path,
        job_id=job_id,
        category="软件工程",
        title="低质量待复核",
        tags=["低质量", "待人工处理"],
        vault=vault,
    )
    with sqlite3.connect(tmp_path / "data" / "knowledge.db") as connection:
        status = connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    assert status == "analyzed"


def test_runner_failure_does_not_publish_or_complete(tmp_path: Path, monkeypatch) -> None:
    job_id = _make_analyzed_job(tmp_path)
    output = tmp_path / "orchestration" / "content-drafts" / "failed-content.md"
    config = tmp_path / "runner.yml"
    config.write_text(
        "version: 1\n"
        "executable: mock\n"
        "output_protocol: file\n"
        "arguments: [--ephemeral, read-only, '{output}']\n"
        "timeout_seconds: 30\n",
        encoding="utf-8",
    )

    class Result:
        returncode = 7

    monkeypatch.setattr("app.content_stage.shutil.which", lambda _value: "mock")
    monkeypatch.setattr("app.content_stage.subprocess.run", lambda *args, **kwargs: Result())
    with pytest.raises(ContentStageError) as error:
        run_content_stage(tmp_path, job_id, config, output)
    assert error.value.code == "content_runner_failed"
    assert not output.exists()
    assert not (tmp_path / "library" / "软件工程").exists()
    with sqlite3.connect(tmp_path / "data" / "knowledge.db") as connection:
        status = connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    assert status == "analyzed"


def test_nonempty_invalid_candidate_is_quarantined_and_temporary_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _make_analyzed_job(tmp_path)
    output = tmp_path / "orchestration" / "content-drafts" / "invalid-content.md"
    config = tmp_path / "runner.yml"
    config.write_text(
        "version: 1\n"
        "name: primary-test\n"
        "executable: mock\n"
        "output_protocol: file\n"
        "arguments: [--ephemeral, read-only, '{output}']\n"
        "timeout_seconds: 30\n",
        encoding="utf-8",
    )

    class Result:
        returncode = 0
        stdout = ""

    def fake_run(command, **_kwargs):
        temporary = Path(command[-1])
        temporary.write_text(
            _draft_text().replace("review_status: verified", "review_status: 已复核"),
            encoding="utf-8",
        )
        return Result()

    monkeypatch.setattr("app.content_stage.shutil.which", lambda _value: "mock")
    monkeypatch.setattr("app.content_stage.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.content_stage.harden_private_project_directory",
        lambda _root, path: path.mkdir(parents=True, exist_ok=True),
    )
    with pytest.raises(ContentStageError) as error:
        run_content_stage(tmp_path, job_id, config, output)
    assert error.value.code == "content_review_status_invalid"
    evidence = error.value.quarantine
    assert evidence is not None
    quarantined = Path(evidence["path"])
    assert quarantined.is_file()
    assert evidence["runner"] == "primary-test"
    assert evidence["error_code"] == "content_review_status_invalid"
    assert len(evidence["sha256"]) == 64
    assert not output.exists()
    assert not list(output.parent.glob(".*.tmp"))


def test_private_candidate_is_quarantined_but_never_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _make_analyzed_job(tmp_path)
    output = tmp_path / "orchestration" / "content-drafts" / "private-content.md"
    config = tmp_path / "runner.yml"
    config.write_text(
        "version: 1\n"
        "name: privacy-test\n"
        "executable: mock\n"
        "output_protocol: file\n"
        "arguments: [--ephemeral, read-only, '{output}']\n"
        "timeout_seconds: 30\n",
        encoding="utf-8",
    )

    class Result:
        returncode = 0
        stdout = ""

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_text(_draft_text() + "\nrequest_url: forbidden\n", encoding="utf-8")
        return Result()

    monkeypatch.setattr("app.content_stage.shutil.which", lambda _value: "mock")
    monkeypatch.setattr("app.content_stage.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.content_stage.harden_private_project_directory",
        lambda _root, path: path.mkdir(parents=True, exist_ok=True),
    )
    with pytest.raises(ContentStageError) as error:
        run_content_stage(tmp_path, job_id, config, output)
    assert error.value.code == "content_privacy_rejected"
    assert Path(error.value.quarantine["path"]).is_file()
    assert not output.exists()


def test_valid_fixture_draft_publishes_without_preemptive_completion(tmp_path: Path) -> None:
    job_id = _make_analyzed_job(tmp_path, frame_count=12)
    draft = _write_draft(
        tmp_path,
        _draft_text().replace("frame: frame-003.jpg", "frame: frame-012.jpg"),
    )
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    target = publish_job(
        tmp_path,
        job_id=job_id,
        category="会被内容稿覆盖",
        title="会被内容稿覆盖",
        tags=["会被覆盖", "默认标签"],
        vault=vault,
        content_draft=draft,
        quality_mode="high-quality",
    )
    document = (target / "内容整理.md").read_text(encoding="utf-8")
    assert "主分类: 软件工程" in document
    assert "高质量内容门禁样板" in document
    assert "会被内容稿覆盖" not in document
    assert {path.name for path in (target / "精选关键帧").iterdir()} == {
        "frame-001.jpg",
        "frame-002.jpg",
        "frame-012.jpg",
    }
    note = vault / "40-Resources" / "抖音收藏" / "软件工程" / "高质量内容门禁样板.md"
    assert note.is_file()
    with sqlite3.connect(tmp_path / "data" / "knowledge.db") as connection:
        status = connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    assert status == "analyzed"
