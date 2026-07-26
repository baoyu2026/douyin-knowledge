# ruff: noqa: E501
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.analyze_video import JOB_ID_PATTERN
from app.content_stage import (
    GENERIC_CATEGORIES,
    GENERIC_TAGS,
    SENSITIVE_PATTERNS,
    ContentStageError,
    ValidatedDraft,
    validate_content_draft,
)
from app.keyframe_selection import resolve_keyframes
from app.security import GateError, harden_private_project_directory
from douyin_knowledge.result_archive import logical_library_handle, results_root

STRUCTURED_SCHEMA_VERSION = 2
STRUCTURED_SCHEMA_RELATIVE = Path("schemas/structured-content-v2.schema.json")
RETRIABLE_CODES = frozenset(
    {
        "structured_runner_failed",
        "structured_runner_timeout",
        "structured_runner_empty",
        "structured_json_invalid",
        "structured_schema_invalid",
    }
)


class StructuredContentError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        attempts: list[dict[str, Any]] | None = None,
        quarantine: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.attempts = attempts or []
        self.quarantine = quarantine


@dataclass(frozen=True)
class StructuredContentResult:
    payload: dict[str, Any]
    raw_path: Path
    draft: ValidatedDraft
    schema_path: Path
    manifest_path: Path
    runner_calls: int
    retry_count: int
    elapsed_seconds: float
    attempts: list[dict[str, Any]]
    reused_json: bool = False


@dataclass(frozen=True)
class StructuredGenerationResult:
    payload: dict[str, Any]
    raw_path: Path
    schema_path: Path
    manifest_path: Path
    runner_calls: int
    retry_count: int
    elapsed_seconds: float
    attempts: list[dict[str, Any]]
    reused_json: bool = False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _single_line(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _load_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StructuredContentError(code, f"无法读取结构化 JSON：{path.name}") from exc
    if not isinstance(value, dict):
        raise StructuredContentError(code, "结构化 JSON 顶层必须是对象")
    return value


def load_structured_schema(root: Path) -> dict[str, Any]:
    schema = _load_json(root.resolve() / STRUCTURED_SCHEMA_RELATIVE, "structured_schema_missing")
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise StructuredContentError("structured_schema_invalid", "结构化 Schema 根契约无效")
    return schema


def _resolve_ref(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise StructuredContentError("structured_schema_invalid", "仅允许本地 Schema 引用")
    current: Any = root_schema
    for part in reference[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            raise StructuredContentError("structured_schema_invalid", "Schema 引用不存在")
        current = current[key]
    if not isinstance(current, dict):
        raise StructuredContentError("structured_schema_invalid", "Schema 引用目标无效")
    return current


def _schema_errors(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str = "$",
) -> list[str]:
    if "$ref" in schema:
        return _schema_errors(value, _resolve_ref(root_schema, str(schema["$ref"])), root_schema, path)
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: enum")
    expected = schema.get("type")
    type_ok = True
    if expected == "object":
        type_ok = isinstance(value, dict)
    elif expected == "array":
        type_ok = isinstance(value, list)
    elif expected == "string":
        type_ok = isinstance(value, str)
    elif expected == "integer":
        type_ok = isinstance(value, int) and not isinstance(value, bool)
    if expected and not type_ok:
        return [f"{path}: type={expected}"]
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key}: additionalProperty")
        for key, child in properties.items():
            if key in value and isinstance(child, dict):
                errors.extend(_schema_errors(value[key], child, root_schema, f"{path}.{key}"))
    elif isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            errors.append(f"{path}: minItems")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: maxItems")
        if schema.get("uniqueItems"):
            serialized = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if len(serialized) != len(set(serialized)):
                errors.append(f"{path}: uniqueItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, item_schema, root_schema, f"{path}[{index}]"))
    elif isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            errors.append(f"{path}: minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path}: maxLength")
        pattern = schema.get("pattern")
        if pattern and not re.fullmatch(str(pattern), value):
            errors.append(f"{path}: pattern")
    elif isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < int(schema["minimum"]):
            errors.append(f"{path}: minimum")
        if "maximum" in schema and value > int(schema["maximum"]):
            errors.append(f"{path}: maximum")
    return errors


def validate_json_schema(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    errors = _schema_errors(payload, schema, schema)
    if errors:
        raise StructuredContentError(
            "structured_schema_invalid",
            "结构化响应不符合 Schema：" + "; ".join(errors[:12]),
        )


def _analysis_inputs(root: Path, job_id: str) -> tuple[dict[str, str], list[Path], float]:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise StructuredContentError("invalid_job_id", "JobId 格式无效")
    analysis = root / "data" / "jobs" / job_id / "analysis"
    paths = {
        "技术分析": analysis / "summary.md",
        "完整转写": analysis / "transcript.md",
        "OCR": analysis / "ocr.md",
        "完整时间轴": analysis / "timeline.md",
    }
    values: dict[str, str] = {}
    for label, path in paths.items():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise StructuredContentError("structured_input_missing", f"缺少{label}") from exc
        if not text.strip():
            raise StructuredContentError("structured_input_missing", f"{label}为空")
        values[label] = text
    manifest = _load_json(analysis / "manifest.json", "structured_input_invalid")
    try:
        selected = resolve_keyframes(analysis, manifest, max_count=None, min_count=3)
    except ValueError as exc:
        raise StructuredContentError("structured_input_missing", "关键帧不足") from exc
    frames = [path for _item, path in selected]
    duration = float((manifest.get("source") or {}).get("duration_seconds") or 0)
    return values, frames, duration


def _library_catalog(root: Path) -> dict[str, Path]:
    catalog: dict[str, Path] = {}
    duplicates: set[str] = set()
    for path in sorted(results_root(root).glob("*/*/内容整理.md")):
        try:
            document = path.read_text(encoding="utf-8")
            if not document.startswith("---\n"):
                continue
            marker = document.find("\n---\n", 4)
            metadata = yaml.safe_load(document[4:marker]) if marker >= 0 else None
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if not isinstance(metadata, dict):
            continue
        title = _single_line(metadata.get("标题") or metadata.get("title"))
        if not title:
            continue
        if title in catalog:
            duplicates.add(title)
        else:
            catalog[title] = path.resolve()
    for title in duplicates:
        catalog.pop(title, None)
    return catalog


def _effective_schema(
    root: Path,
    job_id: str,
    catalog: dict[str, Path],
    frames: list[Path],
) -> tuple[dict[str, Any], Path]:
    schema = copy.deepcopy(load_structured_schema(root))
    related_items = schema["properties"]["related_knowledge"]
    related_items["minItems"] = 1 if catalog else 0
    related = related_items["items"]["properties"]["title"]
    related["enum"] = sorted(catalog)
    frame_index = schema["properties"]["visual_evidence"]["items"]["properties"]["frame_index"]
    frame_index["maximum"] = len(frames)
    directory = root / "orchestration" / "structured-content" / job_id
    try:
        harden_private_project_directory(root, directory)
    except GateError as exc:
        raise StructuredContentError("structured_private_acl_failed", "结构化目录 ACL 无效") from exc
    path = directory / "effective-schema-v1.json"
    encoded = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)
    return schema, path


def build_structured_prompt(root: Path, job_id: str) -> tuple[str, list[Path], dict[str, Path]]:
    inputs, frames, duration = _analysis_inputs(root.resolve(), job_id)
    catalog = _library_catalog(root.resolve())
    blocks = "\n\n".join(f"### {label}\n{text}" for label, text in inputs.items())
    titles = "\n".join(f"- {title}" for title in sorted(catalog))
    frame_map = "\n".join(f"- frame_index={index}: {path.name}" for index, path in enumerate(frames, 1))
    prompt = f"""你是高质量中文知识整理与事实复核编辑。最终响应只能是符合所附 JSON Schema 的单个 JSON 对象，不得输出 Markdown、YAML、代码围栏或解释。

任务要求：
- 综合完整转写、OCR、时间轴、技术摘要和关键帧，生成可独立阅读、可复用、可行动的深度知识稿数据。
- 纠正 ASR/OCR 中的专有名词错误；每个传播到正文的专有名词和数字都必须在对应 review 数组登记证据与 verdict。
- 无法确认的事实标记 unresolved，并同步写入 pending_review；此时 review_status 必须是 needs_review。否则 review_status 为 verified。
- primary_category 必须具体，禁止“未分类、待复核、其他、人工智能与数字工具”。tags 至少两个且不得使用默认占位标签。
- related_knowledge.title 只能从给定标题清单选择；不要输出路径，程序会确定性解析链接。
- visual_evidence.frame_index 只能使用给定关键帧序号；不要输出文件名或路径，程序会确定性解析附件。
- timeline_interpretation 使用视频内时间，视频时长约 {duration:.1f} 秒。
- 各字段职责必须互不重叠：content_summary 只交代开场动机、讨论范围和最终结论；core_points 只列不展开的关键判断；argument_structure 负责逐步给出论点与证据；cases_and_data 只保留具名案例、演示和有证据支持的基准数字；reusable_knowledge 提炼可迁移方法；action_items 写成可执行检查项。不得在多个章节换句话重复同一结论。
- 生成前先逐段核对完整证据，确保覆盖开场动机、每个主要时间区间、具名案例或演示、有证据支持的基准数字以及结尾结论。timeline_interpretation 必须覆盖开头、中段和结尾；证据中确实不存在的案例或数字不要编造。
- visual_evidence.argument_step 必须指向 argument_structure 中真实存在的 step，并选择最能直接支持该论点的关键帧。默认精选 3 至 5 张；只有存在更多彼此不同且不可替代的画面证据时才可增加到 8 张。
- coverage_review 必须先盘点完整证据中的主要主题，再逐项记录 covered、intentionally_omitted 或 unresolved；它是内部审计账本，不要把它复制进可发布正文。清晰、中心性的数字不得为了使用 not_applicable 而从正文中删掉。
- action_items 应尽量包含检查对象、观测指标、验证方式、通过条件和风险信号；证据没有给出阈值时明确由使用者填写，禁止编造。
- 禁止输出 URL、Cookie、签名、请求信息、作品 ID、JobId 或内部路径。
- 不要为了迎合 Schema 编造数字。没有数字时，numeric_review 使用一条 not_applicable 说明。

### 可选相关知识标题
{titles}

### 关键帧映射
{frame_map}

{blocks}
"""
    return prompt, frames, catalog


def _load_runner_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise StructuredContentError("structured_runner_config_invalid", "结构化 runner 配置无效") from exc
    if not isinstance(config, dict) or config.get("version") != 1:
        raise StructuredContentError("structured_runner_config_invalid", "结构化 runner 配置版本无效")
    return config


def _runner_command(
    config_path: Path,
    *,
    root: Path,
    schema: Path,
    output: Path,
    images: list[Path],
) -> tuple[list[str], int, int, str]:
    config = _load_runner_config(config_path)
    executable = _single_line(config.get("executable"))
    arguments = config.get("arguments")
    resolved = shutil.which(executable) if executable else None
    if not resolved or not isinstance(arguments, list) or not arguments:
        raise StructuredContentError("structured_runner_config_invalid", "结构化 runner 命令不完整")
    command = [resolved]
    for raw in arguments:
        if not isinstance(raw, str):
            raise StructuredContentError("structured_runner_config_invalid", "结构化 runner 参数无效")
        if raw == "{images}":
            for image in images:
                command.extend(["--image", str(image)])
            continue
        command.append(
            raw.replace("{root}", str(root))
            .replace("{schema}", str(schema))
            .replace("{output}", str(output))
        )
    required = {"exec", "--ephemeral", "--sandbox", "read-only", "--output-schema"}
    if not required.issubset(command) or "--output-last-message" not in command:
        raise StructuredContentError("structured_runner_config_unsafe", "结构化 runner 未启用只读 Schema 模式")
    if any(value in command for value in ("--full-auto", "--dangerously-bypass-approvals-and-sandbox")):
        raise StructuredContentError("structured_runner_config_unsafe", "结构化 runner 包含高风险参数")
    timeout = config.get("timeout_seconds", 570)
    deadline = config.get("total_deadline_seconds", 600)
    if not isinstance(timeout, int) or not 30 <= timeout <= 600:
        raise StructuredContentError("structured_runner_config_invalid", "单次超时必须在 30-600 秒")
    if not isinstance(deadline, int) or not timeout <= deadline <= 600:
        raise StructuredContentError("structured_runner_config_invalid", "总时限必须在单次超时与 600 秒之间")
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", _single_line(config.get("name"))).strip("-_")
    return command, timeout, deadline, name or "structured-runner"


def _quarantine(
    root: Path,
    job_id: str,
    candidate: Path,
    *,
    runner: str,
    code: str,
) -> dict[str, Any] | None:
    if not candidate.is_file() or not candidate.stat().st_size:
        candidate.unlink(missing_ok=True)
        return None
    directory = root / "quarantine" / "structured-content" / job_id
    try:
        harden_private_project_directory(root, directory)
    except GateError as exc:
        raise StructuredContentError("structured_quarantine_acl_failed", "隔离目录 ACL 无效") from exc
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = directory / f"{timestamp}-{runner}-{code}-{uuid.uuid4().hex[:8]}.json"
    os.replace(candidate, destination)
    return {
        "path": str(destination),
        "sha256": sha256_file(destination),
        "runner": runner,
        "error_code": code,
        "quarantined_at": datetime.now(UTC).isoformat(),
    }


def _resolve_payload(
    root: Path,
    job_id: str,
    payload: dict[str, Any],
    catalog: dict[str, Path],
    frames: list[Path],
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, Any]]]:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if any(pattern.search(encoded) for pattern in SENSITIVE_PATTERNS):
        raise StructuredContentError("structured_privacy_rejected", "结构化响应包含禁止字段")
    category = _single_line(payload["primary_category"])
    tags = [_single_line(value) for value in payload["tags"]]
    if category in GENERIC_CATEGORIES:
        raise StructuredContentError("structured_category_invalid", "主分类不具体")
    if len(tags) != len(set(tags)) or any(tag in GENERIC_TAGS for tag in tags):
        raise StructuredContentError("structured_tags_invalid", "标签包含占位值或重复")
    noun_rows = payload["proper_noun_review"]
    number_rows = payload["numeric_review"]
    pending = payload["pending_review"]
    coverage = payload.get("coverage_review") or []
    _validate_coverage_review(coverage)
    unresolved = any(row["verdict"] == "unresolved" for row in [*noun_rows, *number_rows])
    unresolved = unresolved or any(row["disposition"] == "unresolved" for row in coverage)
    if unresolved != bool(pending):
        raise StructuredContentError("structured_pending_review_invalid", "未决事实与待复核项不一致")
    expected_status = "needs_review" if pending else "verified"
    if payload["review_status"] != expected_status:
        raise StructuredContentError("structured_review_status_invalid", "复核状态与待复核项不一致")
    _validate_content_distinctness(payload)
    _validate_timeline_coverage(root, job_id, payload["timeline_interpretation"])
    related: list[dict[str, str]] = []
    for item in payload["related_knowledge"]:
        title = _single_line(item["title"])
        if title not in catalog:
            raise StructuredContentError("structured_related_invalid", "相关知识标题无法唯一解析")
        related.append(
            {
                "title": title,
                "path": logical_library_handle(root, catalog[title]),
                "reason": _single_line(item["reason"]),
            }
        )
    argument_steps = {int(item["step"]) for item in payload["argument_structure"]}
    visual: list[dict[str, Any]] = []
    observed_indices: set[int] = set()
    placement_flags = [item.get("argument_step") is not None for item in payload["visual_evidence"]]
    if any(placement_flags) and not all(placement_flags):
        raise StructuredContentError(
            "structured_visual_evidence_invalid",
            "关键帧正文位置必须全部提供或全部省略",
        )
    for item in payload["visual_evidence"]:
        index = int(item["frame_index"])
        argument_step = int(item["argument_step"]) if item.get("argument_step") is not None else None
        if (
            index in observed_indices
            or index < 1
            or index > len(frames)
            or (argument_step is not None and argument_step not in argument_steps)
        ):
            raise StructuredContentError("structured_visual_evidence_invalid", "关键帧序号无效或重复")
        observed_indices.add(index)
        resolved = {"frame": frames[index - 1].name, "claim": _single_line(item["claim"])}
        if argument_step is not None:
            resolved["argument_step"] = argument_step
        visual.append(resolved)
    return payload, related, visual


def _normalized_similarity_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", _single_line(value)).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _validate_coverage_review(rows: list[dict[str, Any]]) -> None:
    topics: set[str] = set()
    for item in rows:
        topic = _normalized_similarity_text(item.get("topic"))
        if not topic or topic in topics:
            raise StructuredContentError(
                "structured_coverage_review_invalid",
                "覆盖审计包含空主题或重复主题",
            )
        topics.add(topic)
        disposition = item.get("disposition")
        destination = item.get("destination")
        if (disposition == "covered") != (destination != "not_published"):
            raise StructuredContentError(
                "structured_coverage_review_invalid",
                "覆盖审计的处理结论与正文落位不一致",
            )


def _validate_content_distinctness(payload: dict[str, Any]) -> None:
    units: list[tuple[str, str]] = []
    for field in ("content_summary", "core_points", "reusable_knowledge", "action_items"):
        units.extend((field, _single_line(value)) for value in payload[field])
    for field in ("argument_structure", "cases_and_data"):
        units.extend((field, _single_line(item["claim"])) for item in payload[field])

    normalized = [(_field, text, _normalized_similarity_text(text)) for _field, text in units]
    observed: dict[str, str] = {}
    for field, _text, value in normalized:
        if len(value) < 16:
            continue
        if value in observed:
            raise StructuredContentError(
                "structured_content_duplicate",
                "结构化正文包含标准化后完全重复的内容",
            )
        observed[value] = field


def _timecode_seconds(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _validate_timeline_coverage(root: Path, job_id: str, rows: list[dict[str, Any]]) -> None:
    manifest = _load_json(
        root / "data" / "jobs" / job_id / "analysis" / "manifest.json",
        "structured_input_invalid",
    )
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    duration = source.get("duration_seconds")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return
    points = sorted(_timecode_seconds(_single_line(row["timestamp"])) for row in rows)
    has_opening = points[0] <= max(30.0, duration * 0.15)
    has_middle = any(duration * 0.2 <= point <= duration * 0.8 for point in points)
    has_closing = points[-1] >= duration * 0.8
    if not (has_opening and has_middle and has_closing):
        raise StructuredContentError(
            "structured_timeline_coverage_invalid",
            "时间轴必须覆盖视频开头、中段和结尾",
        )


def _bullets(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values)


def _argument_markdown(
    arguments: list[dict[str, Any]], visual: list[dict[str, Any]]
) -> str:
    by_step: dict[int, list[dict[str, Any]]] = {}
    for item in visual:
        if item.get("argument_step") is not None:
            by_step.setdefault(int(item["argument_step"]), []).append(item)
    blocks = []
    for item in arguments:
        step = int(item["step"])
        block = (
            f"### {_single_line(item['claim'])}\n\n"
            f"**证据**：{_single_line(item['evidence'])}"
        )
        for frame in by_step.get(step, []):
            claim = _single_line(frame["claim"])
            block += f"\n\n![{claim}](精选关键帧/{frame['frame']})\n\n*画面说明：{claim}*"
        blocks.append(block)
    return "\n\n".join(blocks)


def render_structured_markdown(
    root: Path,
    job_id: str,
    payload: dict[str, Any],
    *,
    catalog: dict[str, Path] | None = None,
    frames: list[Path] | None = None,
) -> str:
    root = root.resolve()
    if catalog is None or frames is None:
        _inputs, selected, _duration = _analysis_inputs(root, job_id)
        catalog = _library_catalog(root)
        frames = selected
    payload, related, visual = _resolve_payload(root, job_id, payload, catalog, frames)
    metadata: dict[str, Any] = {
        "schema_version": int(payload["schema_version"]),
        "title": _single_line(payload["title"]),
        "primary_category": _single_line(payload["primary_category"]),
        "tags": [_single_line(value) for value in payload["tags"]],
        "review_status": payload["review_status"],
        "proper_noun_review": payload["proper_noun_review"],
        "numeric_review": payload["numeric_review"],
        "related_knowledge": related,
        "visual_evidence": visual,
        "pending_review": payload["pending_review"],
    }
    title = metadata["title"]
    sections: list[tuple[str, str]] = [
        ("基本信息", "本稿由本地完整转写、OCR、技术摘要、完整时间轴与关键帧联合整理，并经过结构化事实门禁。"),
        ("一句话总结", _single_line(payload["one_sentence_summary"])),
        ("内容摘要", "\n\n".join(_single_line(value) for value in payload["content_summary"])),
        ("核心观点", _bullets([_single_line(value) for value in payload["core_points"]])),
        (
            "论证结构",
            _argument_markdown(payload["argument_structure"], visual),
        ),
        (
            "关键案例与数据",
            "\n".join(
                f"- **结论**：{_single_line(item['claim'])}\n  - **依据**：{_single_line(item['evidence'])}"
                for item in payload["cases_and_data"]
            ),
        ),
        (
            "专有名词与数字复核",
            "\n".join(
                [
                    f"- **专有名词 `{_single_line(item['term'])}`** → `{_single_line(item['normalized'])}`：{_single_line(item['evidence'])}（{item['verdict']}）"
                    for item in payload["proper_noun_review"]
                ]
                + [
                    f"- **数字 `{_single_line(item['value'])}`** → `{_single_line(item['normalized'])}`：{_single_line(item['evidence'])}（{item['verdict']}）"
                    for item in payload["numeric_review"]
                ]
            ),
        ),
        (
            "时间轴",
            "\n".join(
                f"- **{_single_line(item['timestamp'])}**：{_single_line(item['explanation'])}"
                for item in payload["timeline_interpretation"]
            ),
        ),
        ("可复用知识", _bullets([_single_line(value) for value in payload["reusable_knowledge"]])),
        ("行动建议", _bullets([_single_line(value) for value in payload["action_items"]])),
        ("关键词", "、".join(_single_line(value) for value in payload["keywords"])),
        (
            "相关内容",
            "\n".join(
                f"- **[{item['title']}]({item['path']})**：{item['reason']}" for item in related
            )
            if related
            else "当前知识库暂无可关联内容；后续新增笔记时可重新执行关联检查。",
        ),
        (
            "画面证据",
            (
                "关键画面已嵌入对应论证步骤。索引如下：\n\n"
                + "\n".join(
                    f"- **论证步骤 {item['argument_step']} / {item['frame']}**：{item['claim']}"
                    for item in visual
                )
                if all(item.get("argument_step") is not None for item in visual)
                else "\n".join(f"- **{item['frame']}**：{item['claim']}" for item in visual)
            ),
        ),
        (
            "待复核项",
            _bullets([_single_line(value) for value in payload["pending_review"]])
            if payload["pending_review"]
            else "当前没有待复核事项，后续证据变化时重新执行结构化事实门禁。",
        ),
    ]
    front_matter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    body = "\n\n".join(f"## {heading}\n\n{text}" for heading, text in sections)
    return f"---\n{front_matter}\n---\n\n# {title}\n\n{body}\n"


def validate_structured_payload(
    root: Path,
    job_id: str,
    payload: dict[str, Any],
    *,
    schema: dict[str, Any] | None = None,
    catalog: dict[str, Path] | None = None,
    frames: list[Path] | None = None,
) -> None:
    root = root.resolve()
    if catalog is None or frames is None:
        _inputs, selected, _duration = _analysis_inputs(root, job_id)
        catalog = _library_catalog(root)
        frames = selected
    if schema is None:
        schema, _path = _effective_schema(root, job_id, catalog, frames)
    validate_json_schema(payload, schema)
    _resolve_payload(root, job_id, payload, catalog, frames)


def validate_structured_artifacts(
    root: Path,
    job_id: str,
    raw_path: Path,
    draft_path: Path,
) -> tuple[dict[str, Any], ValidatedDraft]:
    root = root.resolve()
    inputs, frames, _duration = _analysis_inputs(root, job_id)
    del inputs
    catalog = _library_catalog(root)
    schema, _schema_path = _effective_schema(root, job_id, catalog, frames)
    payload = _load_json(raw_path, "structured_json_invalid")
    validate_structured_payload(
        root,
        job_id,
        payload,
        schema=schema,
        catalog=catalog,
        frames=frames,
    )
    expected = render_structured_markdown(root, job_id, payload, catalog=catalog, frames=frames)
    try:
        observed = draft_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StructuredContentError("structured_render_missing", "确定性渲染稿不存在") from exc
    if observed != expected:
        raise StructuredContentError("structured_render_mismatch", "确定性渲染稿与结构化数据不一致")
    try:
        draft = validate_content_draft(root, job_id, draft_path)
    except ContentStageError as exc:
        raise StructuredContentError(exc.code, "渲染稿未通过既有高质量门禁") from exc
    return payload, draft


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_structured_content(
    root: Path,
    job_id: str,
    config_path: Path,
    raw_path: Path,
    draft_path: Path,
    *,
    max_calls: int = 2,
    generate_only: bool = False,
) -> StructuredContentResult | StructuredGenerationResult:
    root = root.resolve()
    raw_path = raw_path.resolve()
    draft_path = draft_path.resolve()
    managed_root = (root / "orchestration" / "structured-content").resolve()
    drafts_root = (root / "orchestration" / "content-drafts").resolve()
    try:
        raw_path.relative_to(managed_root)
        draft_path.relative_to(drafts_root)
    except ValueError as exc:
        raise StructuredContentError("structured_output_invalid", "结构化输出越出受管目录") from exc
    if not 1 <= max_calls <= 2:
        raise StructuredContentError("structured_retry_invalid", "结构化调用次数必须为一或二")
    prompt, frames, catalog = build_structured_prompt(root, job_id)
    schema, schema_path = _effective_schema(root, job_id, catalog, frames)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = raw_path.parent / "manifest-v1.json"

    if raw_path.is_file():
        payload = _load_json(raw_path, "structured_json_invalid")
        validate_structured_payload(
            root, job_id, payload, schema=schema, catalog=catalog, frames=frames
        )
        if generate_only:
            if not manifest_path.is_file():
                _write_manifest(
                    manifest_path,
                    {
                        "schema_version": STRUCTURED_SCHEMA_VERSION,
                        "job_id": job_id,
                        "created_at": datetime.now(UTC).isoformat(),
                        "runner": "reused-managed-json",
                        "runner_calls": 0,
                        "retry_count": 0,
                        "elapsed_seconds": 0.0,
                        "schema": {
                            "path": str(schema_path),
                            "sha256": sha256_file(schema_path),
                        },
                        "raw_json": {
                            "path": str(raw_path),
                            "sha256": sha256_file(raw_path),
                        },
                        "attempts": [],
                    },
                )
            return StructuredGenerationResult(
                payload=payload,
                raw_path=raw_path,
                schema_path=schema_path,
                manifest_path=manifest_path,
                runner_calls=0,
                retry_count=0,
                elapsed_seconds=0.0,
                attempts=[],
                reused_json=True,
            )
        expected = render_structured_markdown(root, job_id, payload, catalog=catalog, frames=frames)
        if not draft_path.is_file() or draft_path.read_text(encoding="utf-8") != expected:
            temporary_draft = draft_path.with_name(f".{draft_path.name}.{uuid.uuid4().hex}.tmp")
            temporary_draft.write_text(expected, encoding="utf-8")
            try:
                validate_content_draft(root, job_id, temporary_draft)
            except ContentStageError as exc:
                temporary_draft.unlink(missing_ok=True)
                raise StructuredContentError(exc.code, "确定性渲染稿未通过门禁") from exc
            os.replace(temporary_draft, draft_path)
        payload, draft = validate_structured_artifacts(root, job_id, raw_path, draft_path)
        return StructuredContentResult(
            payload=payload,
            raw_path=raw_path,
            draft=draft,
            schema_path=schema_path,
            manifest_path=manifest_path,
            runner_calls=0,
            retry_count=0,
            elapsed_seconds=0.0,
            attempts=[],
            reused_json=True,
        )

    attempts: list[dict[str, Any]] = []
    started = time.monotonic()
    runner_started_at = datetime.now(UTC).isoformat()
    valid_payload: dict[str, Any] | None = None
    valid_candidate: Path | None = None
    runner_name = "structured-runner"
    deadline_seconds = 600
    for call_index in range(1, max_calls + 1):
        candidate = raw_path.with_name(f".{raw_path.name}.{uuid.uuid4().hex}.candidate")
        command, timeout, deadline_seconds, runner_name = _runner_command(
            config_path,
            root=root,
            schema=schema_path,
            output=candidate,
            images=frames,
        )
        elapsed = time.monotonic() - started
        remaining = deadline_seconds - elapsed
        if remaining < 30:
            raise StructuredContentError(
                "structured_deadline_exceeded",
                "结构化内容实验总时限不足以安全重试",
                attempts=attempts,
            )
        attempt_started = time.monotonic()
        current_prompt = prompt
        if call_index > 1:
            previous_code = attempts[-1]["error_code"]
            current_prompt = (
                prompt
                + "\n\n上一次响应因传输或 Schema 错误被拒绝，错误码为 "
                + str(previous_code)
                + "。本次仍只输出符合 Schema 的单个 JSON 对象，不要解释。\n"
            )
        environment = os.environ.copy()
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_COLOR": "1"})
        result: subprocess.CompletedProcess[str] | None = None
        code: str | None = None
        quarantine: dict[str, Any] | None = None
        try:
            result = subprocess.run(
                command,
                cwd=root,
                input=current_prompt,
                text=True,
                encoding="utf-8",
                errors="strict",
                capture_output=True,
                timeout=min(timeout, max(30, int(remaining))),
                check=False,
                env=environment,
            )
            if result.returncode != 0:
                code = "structured_runner_failed"
                raise StructuredContentError(code, "结构化 runner 非零退出")
            if not candidate.is_file() or not candidate.read_text(encoding="utf-8").strip():
                code = "structured_runner_empty"
                raise StructuredContentError(code, "结构化 runner 返回空响应")
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                code = "structured_json_invalid"
                raise StructuredContentError(code, "结构化 runner 返回无效 JSON") from exc
            if not isinstance(payload, dict):
                code = "structured_json_invalid"
                raise StructuredContentError(code, "结构化 runner 顶层不是对象")
            try:
                validate_structured_payload(
                    root,
                    job_id,
                    payload,
                    schema=schema,
                    catalog=catalog,
                    frames=frames,
                )
            except StructuredContentError as exc:
                code = exc.code
                raise
            valid_payload = payload
            valid_candidate = candidate
        except subprocess.TimeoutExpired:
            code = "structured_runner_timeout"
        except (OSError, UnicodeError) as exc:
            code = "structured_runner_failed"
            if candidate.is_file():
                quarantine = _quarantine(
                    root, job_id, candidate, runner=runner_name, code=code
                )
            attempt = {
                "call": call_index,
                "status": "failed",
                "error_code": code,
                "duration_seconds": round(time.monotonic() - attempt_started, 3),
                "returncode": getattr(result, "returncode", None),
                "stderr_sha256": hashlib.sha256(
                    str(getattr(result, "stderr", "")).encode("utf-8")
                ).hexdigest(),
                "quarantine": quarantine,
            }
            attempts.append(attempt)
            raise StructuredContentError(code, "结构化 runner 执行失败", attempts=attempts) from exc
        except StructuredContentError as exc:
            code = exc.code
        if valid_payload is not None and valid_candidate is not None:
            attempts.append(
                {
                    "call": call_index,
                    "status": "completed",
                    "error_code": None,
                    "duration_seconds": round(time.monotonic() - attempt_started, 3),
                    "returncode": int(getattr(result, "returncode", 0)),
                    "stderr_sha256": hashlib.sha256(
                        str(getattr(result, "stderr", "")).encode("utf-8")
                    ).hexdigest(),
                    "quarantine": None,
                }
            )
            break
        code = code or "structured_runner_failed"
        quarantine = _quarantine(root, job_id, candidate, runner=runner_name, code=code)
        attempts.append(
            {
                "call": call_index,
                "status": "failed",
                "error_code": code,
                "duration_seconds": round(time.monotonic() - attempt_started, 3),
                "returncode": getattr(result, "returncode", None),
                "stderr_sha256": hashlib.sha256(
                    str(getattr(result, "stderr", "")).encode("utf-8")
                ).hexdigest(),
                "quarantine": quarantine,
            }
        )
        if code not in RETRIABLE_CODES or call_index >= max_calls:
            raise StructuredContentError(
                code,
                "结构化内容生成失败",
                attempts=attempts,
                quarantine=quarantine,
            )

    if valid_payload is None or valid_candidate is None:
        raise StructuredContentError("structured_runner_failed", "结构化内容未生成", attempts=attempts)
    if generate_only:
        os.replace(valid_candidate, raw_path)
        elapsed_seconds = round(time.monotonic() - started, 3)
        manifest = {
            "schema_version": STRUCTURED_SCHEMA_VERSION,
            "job_id": job_id,
            "created_at": datetime.now(UTC).isoformat(),
            "runner_started_at": runner_started_at,
            "runner": runner_name,
            "runner_calls": len(attempts),
            "retry_count": max(0, len(attempts) - 1),
            "elapsed_seconds": elapsed_seconds,
            "deadline_seconds": deadline_seconds,
            "schema": {"path": str(schema_path), "sha256": sha256_file(schema_path)},
            "raw_json": {"path": str(raw_path), "sha256": sha256_file(raw_path)},
            "attempts": attempts,
        }
        _write_manifest(manifest_path, manifest)
        return StructuredGenerationResult(
            payload=valid_payload,
            raw_path=raw_path,
            schema_path=schema_path,
            manifest_path=manifest_path,
            runner_calls=len(attempts),
            retry_count=max(0, len(attempts) - 1),
            elapsed_seconds=elapsed_seconds,
            attempts=attempts,
        )
    rendered = render_structured_markdown(
        root, job_id, valid_payload, catalog=catalog, frames=frames
    )
    temporary_draft = draft_path.with_name(f".{draft_path.name}.{uuid.uuid4().hex}.tmp")
    temporary_draft.write_text(rendered, encoding="utf-8")
    try:
        draft = validate_content_draft(root, job_id, temporary_draft)
    except ContentStageError as exc:
        quarantine = _quarantine(
            root,
            job_id,
            valid_candidate,
            runner=runner_name,
            code=f"render-{exc.code}",
        )
        temporary_draft.unlink(missing_ok=True)
        raise StructuredContentError(
            exc.code,
            "确定性渲染稿未通过既有门禁",
            attempts=attempts,
            quarantine=quarantine,
        ) from exc
    os.replace(valid_candidate, raw_path)
    os.replace(temporary_draft, draft_path)
    payload, draft = validate_structured_artifacts(root, job_id, raw_path, draft_path)
    elapsed_seconds = round(time.monotonic() - started, 3)
    manifest = {
        "schema_version": STRUCTURED_SCHEMA_VERSION,
        "job_id": job_id,
        "created_at": datetime.now(UTC).isoformat(),
        "runner_started_at": runner_started_at,
        "runner": runner_name,
        "runner_calls": len(attempts),
        "retry_count": max(0, len(attempts) - 1),
        "elapsed_seconds": elapsed_seconds,
        "deadline_seconds": deadline_seconds,
        "schema": {"path": str(schema_path), "sha256": sha256_file(schema_path)},
        "raw_json": {"path": str(raw_path), "sha256": sha256_file(raw_path)},
        "rendered_markdown": {"path": str(draft_path), "sha256": sha256_file(draft_path)},
        "attempts": attempts,
    }
    temporary_manifest = manifest_path.with_name(f".{manifest_path.name}.{uuid.uuid4().hex}.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary_manifest, manifest_path)
    return StructuredContentResult(
        payload=payload,
        raw_path=raw_path,
        draft=draft,
        schema_path=schema_path,
        manifest_path=manifest_path,
        runner_calls=len(attempts),
        retry_count=max(0, len(attempts) - 1),
        elapsed_seconds=elapsed_seconds,
        attempts=attempts,
    )


def generate_structured_json(
    root: Path,
    job_id: str,
    config_path: Path,
    raw_path: Path,
    *,
    max_calls: int = 2,
) -> StructuredGenerationResult:
    root = root.resolve()
    unused_draft = (
        root / "orchestration" / "content-drafts" / f".{job_id}.generate-only.md"
    )
    result = run_structured_content(
        root,
        job_id,
        config_path,
        raw_path,
        unused_draft,
        max_calls=max_calls,
        generate_only=True,
    )
    if not isinstance(result, StructuredGenerationResult):
        raise StructuredContentError(
            "structured_generation_internal", "结构化生成阶段返回类型无效"
        )
    return result


def validate_structured_json_artifact(
    root: Path,
    job_id: str,
    raw_path: Path,
) -> dict[str, Any]:
    root = root.resolve()
    raw_path = raw_path.resolve()
    managed_root = (root / "orchestration" / "structured-content").resolve()
    try:
        raw_path.relative_to(managed_root)
    except ValueError as exc:
        raise StructuredContentError(
            "structured_output_invalid", "结构化 JSON 越出私有受管目录"
        ) from exc
    _inputs, frames, _duration = _analysis_inputs(root, job_id)
    catalog = _library_catalog(root)
    schema, _schema_path = _effective_schema(root, job_id, catalog, frames)
    payload = _load_json(raw_path, "structured_json_invalid")
    validate_structured_payload(
        root,
        job_id,
        payload,
        schema=schema,
        catalog=catalog,
        frames=frames,
    )
    return payload


def render_structured_json_artifact(
    root: Path,
    job_id: str,
    raw_path: Path,
    draft_path: Path,
) -> ValidatedDraft:
    root = root.resolve()
    raw_path = raw_path.resolve()
    draft_path = draft_path.resolve()
    drafts_root = (root / "orchestration" / "content-drafts").resolve()
    try:
        draft_path.relative_to(drafts_root)
    except ValueError as exc:
        raise StructuredContentError(
            "structured_output_invalid", "确定性渲染稿越出受管目录"
        ) from exc
    payload = validate_structured_json_artifact(root, job_id, raw_path)
    rendered = render_structured_markdown(root, job_id, payload)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    if draft_path.is_file() and draft_path.read_text(encoding="utf-8") == rendered:
        try:
            return validate_content_draft(root, job_id, draft_path)
        except ContentStageError as exc:
            raise StructuredContentError(
                exc.code, "确定性渲染稿未通过既有高质量或隐私门禁"
            ) from exc
    temporary = draft_path.with_name(f".{draft_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        try:
            draft = validate_content_draft(root, job_id, temporary)
        except ContentStageError as exc:
            quarantine = _quarantine(
                root,
                job_id,
                raw_path,
                runner="deterministic-render",
                code=f"render-{exc.code}",
            )
            raise StructuredContentError(
                exc.code,
                "确定性渲染稿未通过既有高质量或隐私门禁",
                quarantine=quarantine,
            ) from exc
        os.replace(temporary, draft_path)
    finally:
        temporary.unlink(missing_ok=True)
    manifest_path = raw_path.parent / "manifest-v1.json"
    if manifest_path.is_file():
        manifest = _load_json(manifest_path, "structured_manifest_invalid")
        manifest["rendered_markdown"] = {
            "path": str(draft_path),
            "sha256": sha256_file(draft_path),
        }
        _write_manifest(manifest_path, manifest)
    return ValidatedDraft(path=draft_path, metadata=draft.metadata, body=draft.body)
