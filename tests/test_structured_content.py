# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.analyze_video import ANALYSIS_VERSION
from app.collection_registry import PIPELINE_VERSION, CollectionRegistry
from app.structured_content import (
    StructuredContentError,
    _effective_schema,
    build_structured_prompt,
    generate_structured_json,
    render_structured_json_artifact,
    render_structured_markdown,
    run_structured_content,
    validate_json_schema,
    validate_structured_artifacts,
    validate_structured_json_artifact,
    validate_structured_payload,
)


def _fixture(root: Path) -> str:
    schema_source = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "structured-content-v2.schema.json"
    )
    schema_target = root / "schemas" / "structured-content-v2.schema.json"
    schema_target.parent.mkdir(parents=True)
    shutil.copyfile(schema_source, schema_target)
    registry = CollectionRegistry(root / "data" / "knowledge.db", root=root)
    snapshot = registry.begin_snapshot(pipeline_version=PIPELINE_VERSION)
    registry.record_snapshot_page(
        snapshot,
        [{"source_id": "structured-fixture", "aweme_id": "structured-fixture"}],
    )
    registry.complete_snapshot(snapshot, pipeline_version=PIPELINE_VERSION)
    item = registry.get("structured-fixture")
    assert item is not None
    job_id = item.job_id
    job_dir = root / "data" / "jobs" / job_id
    analysis = job_dir / "analysis"
    keyframes = analysis / "keyframes"
    keyframes.mkdir(parents=True)
    source = job_dir / "source.mp4"
    source.write_bytes(b"fixture-video")
    items = []
    for index in range(3):
        name = f"frame-{index + 1:03d}.jpg"
        (keyframes / name).write_bytes(f"frame-{index}".encode())
        items.append({"file": f"keyframes/{name}", "timestamp": index * 5})
    (analysis / "manifest.json").write_text(
        json.dumps(
            {
                "analysis_version": ANALYSIS_VERSION,
                "source": {
                    "duration_seconds": 1080.0,
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                },
                "keyframes": {"items": items},
                "coverage_report": {"scan_reached_end": True, "tail_frame_readable": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (analysis / "summary.md").write_text("# 技术分析\n\n企业 AI 交付需要确定性门禁。\n", encoding="utf-8")
    (analysis / "transcript.md").write_text("# 完整转写\n\n讲者讨论企业 AI 需求、价值和交付方法。\n", encoding="utf-8")
    (analysis / "ocr.md").write_text("# OCR\n\n企业 AI 落地方法。\n", encoding="utf-8")
    (analysis / "timeline.md").write_text("# 时间轴\n\n开场、论证、结论。\n", encoding="utf-8")
    related = root / "library" / "参考分类" / "已有参考知识"
    related.mkdir(parents=True)
    (related / "内容整理.md").write_text(
        "---\n标题: 已有参考知识\n主分类: 参考分类\n标签: [参考]\n---\n\n# 已有参考知识\n",
        encoding="utf-8",
    )
    (analysis / "audio.wav").write_bytes(b"fixture-audio")
    (analysis / "transcript.json").write_text("{}", encoding="utf-8")
    (analysis / "ocr.json").write_text("{}", encoding="utf-8")
    with sqlite3.connect(root / "data" / "knowledge.db") as connection:
        connection.execute(
            "UPDATE collection_items SET status = 'analyzed' WHERE job_id = ?", (job_id,)
        )
    return job_id


def _payload() -> dict:
    return {
        "schema_version": 2,
        "title": "企业 AI 落地的确定性交付方法",
        "primary_category": "AI工程与企业服务",
        "tags": ["企业AI", "FDE", "交付方法"],
        "review_status": "verified",
        "one_sentence_summary": "企业 AI 项目的价值不在于展示模型能力，而在于把真实业务问题转化为可验证、可交付和可持续使用的系统。",
        "content_summary": [
            "材料从企业需求、技术供给和实施成本三个方面解释 AI 落地服务的商业逻辑，指出需求真实并不等于项目天然成立。",
            "有效交付需要把模糊需求拆成明确场景，通过事实核验、流程设计和持续运营让客户真正使用系统，而不是只完成一次演示。",
        ],
        "core_points": [
            "企业真正购买的是业务结果和稳定使用能力，而不是孤立的模型账号、提示词或短期演示效果。",
            "FDE 的差异化来自业务理解、方案设计与高标准交付的组合，而不是把传统实施岗位换一个名称。",
            "项目定价应绑定可核验的降本或增长结果，并在实施前明确数据、权限、流程和组织责任边界。",
        ],
        "argument_structure": [
            {"step": 1, "claim": "先判断企业 AI 落地需求是否真实且足够具体。", "evidence": "材料区分了老板对 AI 的想象、员工的实际使用障碍和能够进入流程的业务场景。"},
            {"step": 2, "claim": "再判断通用模型和标准产品是否已经覆盖主要问题。", "evidence": "如果模型账号或通用 Agent 已能解决问题，定制实施就缺少持续收费与维护的理由。"},
            {"step": 3, "claim": "最后以可量化结果和交付体系判断项目是否成立。", "evidence": "材料把价值归结为降本、增长和知识传承，并强调方法论与高标准实施的重要性。"},
        ],
        "cases_and_data": [
            {"claim": "知识传承系统可以降低新人重复培训的组织成本。", "evidence": "讲者以老员工带新员工反复发生且人员可能流失的场景说明需求来源。"}
        ],
        "proper_noun_review": [
            {"term": "FDE", "normalized": "Forward Deployed Engineer", "evidence": "演讲主题和完整转写均围绕该岗位及其企业实施职责展开。", "verdict": "verified"},
            {"term": "Harness", "normalized": "Harness Engineering", "evidence": "讲者将其描述为约束模型输出并使任务进入稳定循环的工程阶段。", "verdict": "verified"},
        ],
        "numeric_review": [
            {"value": "none", "normalized": "正文不传播未经复核的统计数字", "evidence": "结构化正文只保留定性案例，所有具体数值留在完整时间轴证据中。", "verdict": "not_applicable"}
        ],
        "timeline_interpretation": [
            {"timestamp": "00:00", "explanation": "开场提出 FDE 是否属于伪命题，并说明讨论将覆盖需求、岗位差异、商业价值和本地适配。"},
            {"timestamp": "04:00", "explanation": "中段把企业价值拆成降本、增长和知识传承，强调必须对应真实流程与可核验结果。"},
            {"timestamp": "17:55", "explanation": "结尾认为 FDE 不是伪命题，但成立条件是体系化设计、真正的方法论和精密交付。"},
        ],
        "reusable_knowledge": [
            "评估企业 AI 项目时，应先区分模型能力、产品能力与组织采用能力，避免把技术可用误判为业务落地。",
            "交付型服务必须建立场景识别、方案设计、执行验收和持续运营的闭环，才能形成可复用的方法资产。",
        ],
        "action_items": [
            "立项前列出客户当前流程、数据来源、责任人和可核验的业务结果。",
            "用小范围真实任务验证系统是否被持续使用，而不是只验收一次演示。",
            "把专有名词、数字、关键画面与结论建立证据映射后再进入发布阶段。",
        ],
        "keywords": ["企业AI", "FDE", "业务场景", "确定性交付", "组织采用"],
        "related_knowledge": [
            {"title": "已有参考知识", "reason": "用于对照技术能力与组织落地之间的职责边界，并复用已有交付判断框架。"}
        ],
        "visual_evidence": [
            {"frame_index": 1, "argument_step": 1, "claim": "开场画面明确给出 FDE 是否为伪命题的核心议题。"},
            {"frame_index": 2, "argument_step": 2, "claim": "讲者背景页展示需求分析、场景识别、方案设计和落地执行的职责链。"},
            {"frame_index": 3, "argument_step": 3, "claim": "问题拆解页将市场需求、岗位差异、商业价值和本地适配并列呈现。"},
        ],
        "coverage_review": [
            {
                "topic": "企业需求与开场动机",
                "disposition": "covered",
                "destination": "content_summary",
                "evidence": "开场与完整转写都先讨论企业采用 AI 的真实需求。",
                "reason": "用于交代讨论背景和问题范围。",
            },
            {
                "topic": "FDE 的职责与差异化",
                "disposition": "covered",
                "destination": "argument_structure",
                "evidence": "中段按需求、产品覆盖和交付体系展开职责边界。",
                "reason": "属于视频的主要推理链。",
            },
            {
                "topic": "知识传承案例",
                "disposition": "covered",
                "destination": "cases_and_data",
                "evidence": "讲者用老员工反复培训新人说明组织成本。",
                "reason": "作为具名业务场景单独保留。",
            },
            {
                "topic": "结尾成立条件",
                "disposition": "covered",
                "destination": "core_points",
                "evidence": "结尾强调体系化设计、方法论和精密交付。",
                "reason": "属于最终结论，保留为核心判断。",
            },
        ],
        "pending_review": [],
    }


@pytest.fixture(autouse=True)
def private_directory_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.structured_content.harden_private_project_directory",
        lambda _root, path: path.mkdir(parents=True, exist_ok=True),
    )


def test_schema_fixture_enforces_enums_required_and_no_extra_fields(tmp_path: Path) -> None:
    job_id = _fixture(tmp_path)
    _prompt, frames, catalog = build_structured_prompt(tmp_path, job_id)
    schema, _path = _effective_schema(tmp_path, job_id, catalog, frames)
    validate_json_schema(_payload(), schema)

    invalid = _payload()
    invalid["review_status"] = "已复核"
    with pytest.raises(StructuredContentError) as error:
        validate_json_schema(invalid, schema)
    assert error.value.code == "structured_schema_invalid"

    extra = _payload()
    extra["markdown"] = "forbidden"
    with pytest.raises(StructuredContentError):
        validate_json_schema(extra, schema)

    missing = _payload()
    del missing["action_items"]
    with pytest.raises(StructuredContentError) as required:
        validate_json_schema(missing, schema)
    assert required.value.code == "structured_schema_invalid"

    missing_coverage = _payload()
    del missing_coverage["coverage_review"]
    with pytest.raises(StructuredContentError) as coverage:
        validate_json_schema(missing_coverage, schema)
    assert coverage.value.code == "structured_schema_invalid"


def test_renderer_is_deterministic_and_program_resolves_paths(tmp_path: Path) -> None:
    job_id = _fixture(tmp_path)
    first = render_structured_markdown(tmp_path, job_id, _payload())
    second = render_structured_markdown(tmp_path, job_id, _payload())
    assert first == second
    assert hashlib.sha256(first.encode("utf-8")).hexdigest() == (
        "7b519c828f54a66977717eb6eac2ae7550eb39c1c4fb03c4eff57d1f413bfa44"
    )
    assert "path: library/参考分类/已有参考知识/内容整理.md" in first
    assert "frame: frame-001.jpg" in first
    assert "argument_step: 1" in first
    assert "frame_index" not in first
    assert "coverage_review" not in first
    assert "![开场画面明确给出 FDE 是否为伪命题的核心议题。](精选关键帧/frame-001.jpg)" in first
    first_step = first.index("### 先判断企业 AI 落地需求是否真实且足够具体。")
    second_step = first.index("### 再判断通用模型和标准产品是否已经覆盖主要问题。")
    first_frame = first.index("精选关键帧/frame-001.jpg")
    assert first_step < first_frame < second_step
    assert "## 时间轴" in first
    assert "## 待复核项" in first
    output = tmp_path / "orchestration" / "content-drafts" / f"{job_id}-content.md"
    output.parent.mkdir(parents=True)
    output.write_text(first, encoding="utf-8")
    raw = tmp_path / "orchestration" / "structured-content" / job_id / "response-v1.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    payload, draft = validate_structured_artifacts(tmp_path, job_id, raw, output)
    assert payload["title"] == draft.title
    assert len(draft.body) >= 600
    before_mtime = output.stat().st_mtime_ns
    rendered = render_structured_json_artifact(tmp_path, job_id, raw, output)
    assert rendered.path == output
    assert output.stat().st_mtime_ns == before_mtime


def test_privacy_and_semantic_quality_fail_without_retry(tmp_path: Path) -> None:
    job_id = _fixture(tmp_path)
    private = _payload()
    private["content_summary"][0] += " request_url is forbidden"
    with pytest.raises(StructuredContentError) as privacy:
        validate_structured_payload(tmp_path, job_id, private)
    assert privacy.value.code == "structured_privacy_rejected"

    generic = _payload()
    generic["primary_category"] = "人工智能与数字工具"
    with pytest.raises(StructuredContentError) as category:
        validate_structured_payload(tmp_path, job_id, generic)
    assert category.value.code == "structured_category_invalid"

    duplicate = _payload()
    duplicate["reusable_knowledge"][0] = duplicate["core_points"][0]
    with pytest.raises(StructuredContentError) as repeated:
        validate_structured_payload(tmp_path, job_id, duplicate)
    assert repeated.value.code == "structured_content_duplicate"

    misplaced = _payload()
    misplaced["visual_evidence"][0]["argument_step"] = 9
    with pytest.raises(StructuredContentError) as placement:
        validate_structured_payload(tmp_path, job_id, misplaced)
    assert placement.value.code == "structured_visual_evidence_invalid"

    partial = _payload()
    del partial["visual_evidence"][0]["argument_step"]
    with pytest.raises(StructuredContentError) as incomplete_placement:
        validate_structured_payload(tmp_path, job_id, partial)
    assert incomplete_placement.value.code == "structured_schema_invalid"

    incomplete_timeline = _payload()
    incomplete_timeline["timeline_interpretation"][-1]["timestamp"] = "06:00"
    with pytest.raises(StructuredContentError) as timeline:
        validate_structured_payload(tmp_path, job_id, incomplete_timeline)
    assert timeline.value.code == "structured_timeline_coverage_invalid"

    duplicate_topic = _payload()
    duplicate_topic["coverage_review"][1]["topic"] = "  企业需求与开场动机  "
    with pytest.raises(StructuredContentError) as coverage:
        validate_structured_payload(tmp_path, job_id, duplicate_topic)
    assert coverage.value.code == "structured_coverage_review_invalid"

    invalid_disposition = _payload()
    invalid_disposition["coverage_review"][0]["destination"] = "not_published"
    with pytest.raises(StructuredContentError) as disposition:
        validate_structured_payload(tmp_path, job_id, invalid_disposition)
    assert disposition.value.code == "structured_coverage_review_invalid"

    unresolved_coverage = _payload()
    unresolved_coverage["coverage_review"][0]["disposition"] = "unresolved"
    unresolved_coverage["coverage_review"][0]["destination"] = "not_published"
    with pytest.raises(StructuredContentError) as unresolved:
        validate_structured_payload(tmp_path, job_id, unresolved_coverage)
    assert unresolved.value.code == "structured_pending_review_invalid"


def test_prompt_assigns_non_overlapping_roles_and_complete_coverage(tmp_path: Path) -> None:
    job_id = _fixture(tmp_path)
    prompt, _frames, _catalog = build_structured_prompt(tmp_path, job_id)

    assert "不得在多个章节换句话重复同一结论" in prompt
    assert "开场动机" in prompt
    assert "具名案例" in prompt
    assert "有证据支持的基准数字" in prompt
    assert "开头、中段和结尾" in prompt
    assert "argument_step" in prompt


def test_split_generate_validate_render_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _fixture(tmp_path)
    config = tmp_path / "runner.yml"
    config.write_text(
        "version: 1\nname: fixture\nexecutable: codex\noutput_protocol: file\n"
        "arguments: [exec, --ephemeral, --sandbox, read-only, --output-schema, '{schema}', --output-last-message, '{output}', '{images}', '-']\n"
        "timeout_seconds: 60\ntotal_deadline_seconds: 120\n",
        encoding="utf-8",
    )
    calls = 0

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
        return Result()

    monkeypatch.setattr("app.structured_content.shutil.which", lambda _value: "codex")
    monkeypatch.setattr("app.structured_content.subprocess.run", fake_run)
    raw = tmp_path / "orchestration" / "structured-content" / job_id / "response-v1.json"
    draft = tmp_path / "orchestration" / "content-drafts" / f"{job_id}-content.md"

    generated = generate_structured_json(tmp_path, job_id, config, raw)
    assert generated.runner_calls == 1
    assert raw.is_file() and generated.manifest_path.is_file()
    assert not draft.exists()
    assert validate_structured_json_artifact(tmp_path, job_id, raw)["schema_version"] == 2
    rendered = render_structured_json_artifact(tmp_path, job_id, raw, draft)
    assert rendered.path == draft and draft.is_file()
    assert generate_structured_json(tmp_path, job_id, config, raw).reused_json is True
    assert calls == 1


def test_schema_error_retries_once_and_writes_managed_raw_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _fixture(tmp_path)
    config = tmp_path / "runner.yml"
    config.write_text(
        "version: 1\nname: fixture\nexecutable: codex\noutput_protocol: file\n"
        "arguments: [exec, --ephemeral, --sandbox, read-only, --output-schema, '{schema}', --output-last-message, '{output}', '{images}', '-']\n"
        "timeout_seconds: 60\ntotal_deadline_seconds: 120\n",
        encoding="utf-8",
    )
    calls = 0

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        output = Path(command[command.index("--output-last-message") + 1])
        value = "```json\n{}\n```" if calls == 1 else json.dumps(_payload(), ensure_ascii=False)
        output.write_text(value, encoding="utf-8")
        return Result()

    monkeypatch.setattr("app.structured_content.shutil.which", lambda _value: "codex")
    monkeypatch.setattr("app.structured_content.subprocess.run", fake_run)
    raw = tmp_path / "orchestration" / "structured-content" / job_id / "response-v1.json"
    draft = tmp_path / "orchestration" / "content-drafts" / f"{job_id}-content.md"
    result = run_structured_content(tmp_path, job_id, config, raw, draft)
    assert result.runner_calls == 2
    assert result.retry_count == 1
    assert raw.is_file() and draft.is_file() and result.manifest_path.is_file()
    assert calls == 2
    quarantined = list((tmp_path / "quarantine" / "structured-content" / job_id).glob("*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8").startswith("```json")
    assert result.attempts[0]["quarantine"]["sha256"] == hashlib.sha256(
        quarantined[0].read_bytes()
    ).hexdigest()
    assert run_structured_content(tmp_path, job_id, config, raw, draft).reused_json is True
    assert calls == 2


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("quality", "structured_category_invalid"),
        ("privacy", "structured_privacy_rejected"),
    ],
)
def test_quality_and_privacy_failure_do_not_use_second_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_code: str,
) -> None:
    job_id = _fixture(tmp_path)
    config = tmp_path / "runner.yml"
    config.write_text(
        "version: 1\nname: fixture\nexecutable: codex\noutput_protocol: file\n"
        "arguments: [exec, --ephemeral, --sandbox, read-only, --output-schema, '{schema}', --output-last-message, '{output}', '{images}', '-']\n"
        "timeout_seconds: 60\ntotal_deadline_seconds: 120\n",
        encoding="utf-8",
    )
    calls = 0

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        output = Path(command[command.index("--output-last-message") + 1])
        payload = _payload()
        if failure == "quality":
            payload["primary_category"] = "人工智能与数字工具"
        else:
            payload["content_summary"][0] += " request_url is forbidden"
        output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return Result()

    monkeypatch.setattr("app.structured_content.shutil.which", lambda _value: "codex")
    monkeypatch.setattr("app.structured_content.subprocess.run", fake_run)
    raw = tmp_path / "orchestration" / "structured-content" / job_id / "response-v1.json"
    draft = tmp_path / "orchestration" / "content-drafts" / f"{job_id}-content.md"
    with pytest.raises(StructuredContentError) as error:
        run_structured_content(tmp_path, job_id, config, raw, draft)
    assert error.value.code == expected_code
    assert calls == 1
    assert not raw.exists() and not draft.exists()
