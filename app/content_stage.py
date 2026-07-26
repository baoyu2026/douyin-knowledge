from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.analyze_video import JOB_ID_PATTERN
from app.collection_registry import update_item_by_job
from app.keyframe_selection import resolve_keyframes
from app.security import GateError, harden_private_project_directory
from douyin_knowledge.result_archive import (
    ResultsConfigError,
    resolve_logical_library_handle,
    results_root,
)

CONTENT_SCHEMA_VERSION = 2
SUPPORTED_CONTENT_SCHEMA_VERSIONS = {1, CONTENT_SCHEMA_VERSION}
REQUIRED_SECTIONS = (
    "基本信息",
    "一句话总结",
    "内容摘要",
    "核心观点",
    "论证结构",
    "关键案例与数据",
    "专有名词与数字复核",
    "时间轴",
    "可复用知识",
    "行动建议",
    "关键词",
    "相关内容",
    "画面证据",
    "待复核项",
)
GENERIC_CATEGORIES = {"未分类", "待复核", "其他", "人工智能与数字工具"}
GENERIC_TAGS = {"待复核", "知识管理"}
SENSITIVE_PATTERNS = (
    re.compile(r"https?://\S+", re.I),
    re.compile(r"(?i)\b(cookie|sessionid?|signature|request[_ -]?url)\b"),
    re.compile(r"(?i)\b(job[_ -]?id|source[_ -]?id|aweme[_ -]?id)\b"),
    re.compile(r"\baweme-[a-f0-9]{20}\b", re.I),
    re.compile(r"(?<!\d)\d{15,24}(?!\d)"),
)
NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?%?(?![A-Za-z0-9_])"
)
MARKDOWN_INLINE_LINK_PATTERN = re.compile(
    r"!?\[(?P<label>[^\]\n]+)\]\((?:[^()\n]|\([^()\n]*\))*\)"
)
TIMECODE_PATTERN = re.compile(
    r"(?<!\d)(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?(?!\d)"
)
CONTENT_PROTOCOL_ERROR_CODES = frozenset(
    {
        "content_runner_empty",
        "content_front_matter_missing",
        "content_front_matter_invalid",
        "content_schema_invalid",
        "content_metadata_incomplete",
        "content_review_status_invalid",
        "content_review_incomplete",
        "content_review_verdict_invalid",
        "content_pending_review_invalid",
        "content_pending_review_missing",
        "content_sections_incomplete",
    }
)


class ContentStageError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        quarantine: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.quarantine = quarantine


def is_content_protocol_error(error: ContentStageError | str) -> bool:
    code = error.code if isinstance(error, ContentStageError) else error
    return code in CONTENT_PROTOCOL_ERROR_CODES


@dataclass(frozen=True)
class ValidatedDraft:
    path: Path
    metadata: dict[str, Any]
    body: str

    @property
    def title(self) -> str:
        return str(self.metadata["title"])

    @property
    def category(self) -> str:
        return str(self.metadata["primary_category"])

    @property
    def tags(self) -> list[str]:
        return [str(value) for value in self.metadata["tags"]]

    @property
    def review_status(self) -> str:
        return "已复核" if self.metadata["review_status"] == "verified" else "待人工复核"

    @property
    def evidence_status(self) -> str:
        return str(self.metadata["review_status"])


def _front_matter(document: str) -> tuple[dict[str, Any], str]:
    normalized = document.lstrip("\ufeff")
    if not normalized.startswith("---\n"):
        raise ContentStageError("content_front_matter_missing", "内容稿缺少 YAML 元数据")
    marker = normalized.find("\n---\n", 4)
    if marker < 0:
        raise ContentStageError("content_front_matter_invalid", "内容稿 YAML 边界不完整")
    try:
        metadata = yaml.safe_load(normalized[4:marker])
    except yaml.YAMLError as exc:
        raise ContentStageError("content_front_matter_invalid", "内容稿 YAML 无法解析") from exc
    if not isinstance(metadata, dict):
        raise ContentStageError("content_front_matter_invalid", "内容稿 YAML 必须是对象")
    return metadata, normalized[marker + 5 :].strip() + "\n"


def _single_line(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _required_text(metadata: dict[str, Any], key: str) -> str:
    value = _single_line(metadata.get(key))
    if not value:
        raise ContentStageError("content_metadata_incomplete", f"内容稿缺少 {key}")
    return value


def _review_rows(
    metadata: dict[str, Any], key: str, fields: tuple[str, ...]
) -> list[dict[str, Any]]:
    rows = metadata.get(key)
    if not isinstance(rows, list) or not rows:
        raise ContentStageError("content_review_incomplete", f"内容稿缺少 {key}")
    for row in rows:
        if not isinstance(row, dict) or any(not _single_line(row.get(field)) for field in fields):
            raise ContentStageError("content_review_incomplete", f"内容稿 {key} 结构不完整")
        if row.get("verdict") not in {"verified", "unresolved", "not_applicable"}:
            raise ContentStageError("content_review_verdict_invalid", f"内容稿 {key} 结论无效")
    return rows


def _section(body: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(?P<value>.*?)(?=^## |\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group("value").strip() if match else ""


def _validate_related(root: Path, metadata: dict[str, Any]) -> None:
    related = metadata.get("related_knowledge")
    if not isinstance(related, list):
        raise ContentStageError("content_links_missing", "内容稿缺少已有知识关联")
    library_root = results_root(root)
    if not related:
        if any(library_root.glob("*/*/内容整理.md")):
            raise ContentStageError("content_links_missing", "内容稿缺少已有知识关联")
        return
    for item in related:
        if not isinstance(item, dict):
            raise ContentStageError("content_links_invalid", "相关知识必须是结构化条目")
        title = _single_line(item.get("title"))
        path_value = _single_line(item.get("path"))
        reason = _single_line(item.get("reason"))
        if not title or not path_value or not reason:
            raise ContentStageError("content_links_invalid", "相关知识条目不完整")
        try:
            candidate = resolve_logical_library_handle(root, path_value)
        except ResultsConfigError as exc:
            raise ContentStageError("content_links_invalid", "相关知识链接越出 Library") from exc
        if candidate.name != "内容整理.md" or not candidate.is_file():
            raise ContentStageError("content_links_invalid", "相关知识链接不存在")


def _validate_visual_evidence(root: Path, job_id: str, metadata: dict[str, Any]) -> None:
    evidence = metadata.get("visual_evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ContentStageError("content_visual_evidence_missing", "内容稿缺少画面证据")
    keyframes = (root / "data" / "jobs" / job_id / "analysis" / "keyframes").resolve()
    for item in evidence:
        if not isinstance(item, dict):
            raise ContentStageError("content_visual_evidence_invalid", "画面证据结构无效")
        frame = _single_line(item.get("frame"))
        claim = _single_line(item.get("claim"))
        if not frame or not claim or Path(frame).name != frame:
            raise ContentStageError("content_visual_evidence_invalid", "画面证据条目不完整")
        candidate = (keyframes / frame).resolve()
        try:
            candidate.relative_to(keyframes)
        except ValueError as exc:
            raise ContentStageError("content_visual_evidence_invalid", "画面证据路径无效") from exc
        if not candidate.is_file() or candidate.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise ContentStageError("content_visual_evidence_invalid", "画面证据文件不存在")


def _validate_numbers(body: str, rows: list[dict[str, Any]]) -> None:
    check_body = body
    for heading in ("时间轴", "画面证据", "相关内容"):
        value = _section(body, heading)
        check_body = check_body.replace(value, "")
    # Link labels are reader-visible content; destinations are program-controlled paths.
    check_body = MARKDOWN_INLINE_LINK_PATTERN.sub(
        lambda match: match.group("label"), check_body
    )
    # Source citations use timestamps as coordinates, not as factual numeric claims.
    check_body = TIMECODE_PATTERN.sub("", check_body)
    observed = set(NUMBER_PATTERN.findall(check_body))
    if not observed:
        return
    reviewed = " ".join(
        _single_line(row.get(field))
        for row in rows
        for field in ("value", "normalized", "evidence")
    )
    if any(number not in reviewed for number in observed):
        raise ContentStageError("content_numbers_unreviewed", "内容稿存在未登记复核的数字")


def _analysis_coverage(root: Path, job_id: str) -> dict[str, Any]:
    path = root / "data" / "jobs" / job_id / "analysis" / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    coverage = manifest.get("coverage_report") if isinstance(manifest, dict) else None
    return coverage if isinstance(coverage, dict) else {}


def validate_content_draft(root: Path, job_id: str, path: Path) -> ValidatedDraft:
    root = root.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise ContentStageError("invalid_job_id", "job ID 格式无效")
    try:
        document = path.resolve().read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContentStageError("content_draft_unreadable", "无法读取 UTF-8 内容稿") from exc
    if any(pattern.search(document) for pattern in SENSITIVE_PATTERNS):
        raise ContentStageError("content_privacy_rejected", "内容稿包含禁止发布的隐私字段")
    metadata, body = _front_matter(document)
    if metadata.get("schema_version") not in SUPPORTED_CONTENT_SCHEMA_VERSIONS:
        raise ContentStageError("content_schema_invalid", "内容稿版本不受支持")
    title = _required_text(metadata, "title")
    category = _required_text(metadata, "primary_category")
    if category in GENERIC_CATEGORIES:
        raise ContentStageError("content_category_invalid", "内容稿主分类不具体")
    tags = metadata.get("tags")
    if not isinstance(tags, list):
        raise ContentStageError("content_tags_invalid", "内容稿标签必须是列表")
    normalized_tags = list(dict.fromkeys(_single_line(value) for value in tags))
    invalid_tags = any(not value or value in GENERIC_TAGS for value in normalized_tags)
    if len(normalized_tags) < 2 or invalid_tags:
        raise ContentStageError("content_tags_invalid", "内容稿需要真实多标签")
    if metadata.get("review_status") not in {"verified", "needs_review"}:
        raise ContentStageError("content_review_status_invalid", "内容稿复核状态无效")
    noun_rows = _review_rows(
        metadata,
        "proper_noun_review",
        ("term", "normalized", "evidence", "verdict"),
    )
    number_rows = _review_rows(
        metadata,
        "numeric_review",
        ("value", "normalized", "evidence", "verdict"),
    )
    pending = metadata.get("pending_review")
    if not isinstance(pending, list) or any(not _single_line(value) for value in pending):
        raise ContentStageError("content_pending_review_invalid", "待复核项必须是文本列表")
    unresolved = any(row["verdict"] == "unresolved" for row in (*noun_rows, *number_rows))
    if unresolved and not pending:
        raise ContentStageError("content_pending_review_missing", "未决复核结果必须进入待复核项")
    if unresolved and metadata["review_status"] != "needs_review":
        raise ContentStageError("content_review_status_invalid", "未决事实必须标记待复核")
    if pending and metadata["review_status"] != "needs_review":
        raise ContentStageError("content_review_status_invalid", "待复核项与复核状态不一致")
    coverage = _analysis_coverage(root, job_id)
    if (
        coverage.get("ocr_quality_status") == "needs_review"
        and metadata["review_status"] != "needs_review"
    ):
        raise ContentStageError(
            "content_review_status_invalid",
            "OCR 质量不足时内容稿必须标记 needs_review",
        )
    for heading in REQUIRED_SECTIONS:
        if len(_section(body, heading)) < 8:
            raise ContentStageError("content_sections_incomplete", f"内容稿章节不完整：{heading}")
    if len(body) < 600 or not re.search(rf"^#\s+{re.escape(title)}\s*$", body, re.MULTILINE):
        raise ContentStageError("content_body_too_shallow", "内容稿正文过短或标题不一致")
    _validate_numbers(body, number_rows)
    _validate_related(root, metadata)
    _validate_visual_evidence(root, job_id, metadata)
    metadata["tags"] = normalized_tags
    return ValidatedDraft(path=path.resolve(), metadata=metadata, body=body)


def _load_analysis_inputs(root: Path, job_id: str) -> tuple[dict[str, str], list[Path]]:
    analysis = root / "data" / "jobs" / job_id / "analysis"
    required = {
        "技术分析": analysis / "summary.md",
        "完整转写": analysis / "transcript.json",
        "OCR": analysis / "ocr.json",
        "时间轴": analysis / "timeline.md",
        "关键帧清单": analysis / "manifest.json",
    }
    payload: dict[str, str] = {}
    for label, path in required.items():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ContentStageError("content_input_missing", f"缺少{label}") from exc
        if not text.strip():
            raise ContentStageError("content_input_missing", f"{label}为空")
        payload[label] = text
    try:
        manifest = json.loads(payload["关键帧清单"])
    except json.JSONDecodeError as exc:
        raise ContentStageError("content_input_invalid", "关键帧清单无法解析") from exc
    if not isinstance(manifest, dict):
        raise ContentStageError("content_input_invalid", "关键帧清单结构无效")
    try:
        selected = resolve_keyframes(analysis, manifest, max_count=8, min_count=3)
    except ValueError as exc:
        raise ContentStageError("content_input_missing", "关键帧不足") from exc
    return payload, [path for _item, path in selected]


def _knowledge_index(root: Path) -> str:
    lines = []
    for path in sorted((root / "library").glob("*/*/内容整理.md")):
        try:
            metadata, _body = _front_matter(path.read_text(encoding="utf-8"))
        except (OSError, ContentStageError):
            continue
        title = _single_line(metadata.get("标题") or metadata.get("title"))
        category = _single_line(metadata.get("主分类") or metadata.get("primary_category"))
        if title:
            lines.append(f"- {title} | {category} | {path.relative_to(root).as_posix()}")
    if not lines:
        raise ContentStageError("knowledge_index_missing", "已有知识索引为空")
    return "\n".join(lines)


def build_runner_prompt(root: Path, job_id: str) -> tuple[str, list[Path]]:
    inputs, frames = _load_analysis_inputs(root, job_id)
    schema_example = """---
schema_version: 1
title: 真实且具体的标题
primary_category: 真实主分类
tags: [标签一, 标签二]
review_status: verified
proper_noun_review:
  - {term: 原始词, normalized: 核定词, evidence: 复核依据, verdict: verified}
numeric_review:
  - {value: 原始数字, normalized: 核定数字, evidence: 复核依据, verdict: verified}
related_knowledge:
  - {title: 已有笔记标题, path: library/分类/目录/内容整理.md, reason: 关联理由}
visual_evidence:
  - {frame: frame-001.jpg, claim: 画面支持的结论}
pending_review: []
---"""
    sections = "\n".join(f"## {heading}" for heading in REQUIRED_SECTIONS)
    input_blocks = "\n\n".join(
        f"### {label}\n```text\n{text}\n```" for label, text in inputs.items()
    )
    frame_paths = "\n".join(f"- {path}" for path in frames)
    prompt = f"""你是高质量知识整理与事实复核编辑。
只输出 UTF-8 Markdown 内容稿，不输出解释、代码围栏或前后说明。

必须综合技术分析、完整转写、OCR、关键帧和已有知识索引，
不能把 ASR 摘要、静态分类或默认标签当成整理结果。
逐项复核专有名词、产品、论文和正文数字；无法确认就标 unresolved 并写入 pending_review。
相关知识只能链接到给定的本地 Library 路径，不得生成网址。
禁止写入 Cookie、签名、请求 URL、作品标识或内部任务标识。

输出必须从第一行 `---` 开始，并严格使用下面的完整 YAML front matter schema：
- `review_status` 只能是英文枚举 `verified` 或 `needs_review`，禁止翻译成中文或使用其他值。
- `proper_noun_review[].verdict` 与 `numeric_review[].verdict` 只能是英文枚举：
  `verified`、`unresolved` 或 `not_applicable`。
- 只要存在 `unresolved` 或非空 `pending_review`，
  `review_status` 必须是 `needs_review`；否则使用 `verified`。
- front matter 之后直接输出 Markdown 正文，不得把全文包在 Markdown 代码围栏中。

最小合法 YAML 契约示例：
{schema_example}

正文必须以与 title 完全一致的一级标题开头，并依次完整包含以下章节：
{sections}

内容要达到可独立阅读、可复用和可行动的深度。时间轴引用完整证据；画面证据必须使用随请求附带的关键帧文件名。

### 已有知识索引
{_knowledge_index(root)}

### 关键帧文件
{frame_paths}

{input_blocks}
"""
    return prompt, frames


def _load_runner_config(config_path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ContentStageError("content_runner_config_invalid", "内容 runner 配置无效") from exc
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ContentStageError("content_runner_config_invalid", "内容 runner 配置版本无效")
    return config


def _runner_command(
    config_path: Path, *, root: Path, output: Path, images: list[Path]
) -> tuple[list[str], int, str]:
    config = _load_runner_config(config_path)
    executable = _single_line(config.get("executable"))
    arguments = config.get("arguments")
    output_protocol = _single_line(config.get("output_protocol") or "file")
    resolved_executable = shutil.which(executable) if executable else None
    if not resolved_executable or not isinstance(arguments, list) or not arguments:
        raise ContentStageError("content_runner_config_invalid", "内容 runner 命令不完整")
    if output_protocol not in {"file", "stdout"}:
        raise ContentStageError("content_runner_protocol_invalid", "内容 runner 输出协议无效")
    command = [resolved_executable]
    for raw in arguments:
        if not isinstance(raw, str):
            raise ContentStageError("content_runner_config_invalid", "内容 runner 参数无效")
        if raw == "{images}":
            for image in images:
                command.extend(["--image", str(image)])
            continue
        command.append(raw.replace("{root}", str(root)).replace("{output}", str(output)))
    timeout = config.get("timeout_seconds", 1800)
    if not isinstance(timeout, int) or not 30 <= timeout <= 7200:
        raise ContentStageError("content_runner_config_invalid", "内容 runner 超时无效")
    forbidden = {"--dangerously-bypass-approvals-and-sandbox", "--full-auto"}
    if any(value in forbidden for value in command):
        raise ContentStageError("content_runner_config_unsafe", "内容 runner 配置包含高风险参数")
    if output_protocol == "file":
        if "{output}" not in arguments:
            raise ContentStageError("content_runner_protocol_invalid", "文件协议缺少输出占位符")
        if "read-only" not in command or "--ephemeral" not in command:
            raise ContentStageError(
                "content_runner_config_unsafe", "内容 runner 未启用只读临时模式"
            )
    else:
        required = {"--print", "--no-session-persistence", "--permission-mode", "plan"}
        if not required.issubset(command):
            raise ContentStageError(
                "content_runner_config_unsafe", "标准输出 runner 未启用安全临时模式"
            )
        if "--tools" not in command or any(value in command for value in ("Bash", "Edit", "Write")):
            raise ContentStageError("content_runner_config_unsafe", "标准输出 runner 工具权限过宽")
    return command, timeout, output_protocol


def preflight_content_runner(
    root: Path, job_id: str, config_path: Path, output: Path
) -> tuple[str, list[Path]]:
    root = root.resolve()
    output = output.resolve()
    drafts_root = (root / "orchestration" / "content-drafts").resolve()
    try:
        output.relative_to(drafts_root)
    except ValueError as exc:
        raise ContentStageError("content_output_invalid", "内容稿输出必须位于受管目录") from exc
    _prompt, images = build_runner_prompt(root, job_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    probe = output.parent / f".{output.name}.{uuid.uuid4().hex}.preflight"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError as exc:
        raise ContentStageError("content_output_unwritable", "内容稿输出目录不可写") from exc
    finally:
        probe.unlink(missing_ok=True)
    _command, _timeout, protocol = _runner_command(
        config_path, root=root, output=output, images=images
    )
    return protocol, images


def _runner_name(config_path: Path) -> str:
    config = _load_runner_config(config_path)
    value = _single_line(config.get("name") or config_path.stem)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.") or "runner"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quarantine_candidate(
    root: Path,
    job_id: str,
    temporary: Path,
    *,
    runner: str,
    error_code: str,
) -> dict[str, Any]:
    directory = (root / "quarantine" / "content-drafts" / job_id).resolve()
    try:
        harden_private_project_directory(root, directory)
    except GateError as exc:
        raise ContentStageError("content_quarantine_acl_failed", "候选稿隔离目录 ACL 无效") from exc
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = directory / f"{timestamp}-{runner}-{uuid.uuid4().hex[:8]}.md"
    os.replace(temporary, destination)
    return {
        "path": str(destination),
        "sha256": _sha256(destination),
        "error_code": error_code,
        "runner": runner,
        "quarantined_at": datetime.now(UTC).isoformat(),
    }


def run_content_stage(root: Path, job_id: str, config_path: Path, output: Path) -> ValidatedDraft:
    root = root.resolve()
    output = output.resolve()
    drafts_root = (root / "orchestration" / "content-drafts").resolve()
    try:
        output.relative_to(drafts_root)
    except ValueError as exc:
        raise ContentStageError("content_output_invalid", "内容稿输出必须位于受管目录") from exc
    prompt, images = build_runner_prompt(root, job_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    runner_name = _runner_name(config_path)
    command, timeout, output_protocol = _runner_command(
        config_path, root=root, output=temporary, images=images
    )
    environment = os.environ.copy()
    environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_COLOR": "1"})
    try:
        result = subprocess.run(
            command,
            cwd=root,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
        if result.returncode != 0:
            raise ContentStageError("content_runner_failed", "内容 runner 未生成稿件")
        if output_protocol == "stdout":
            stdout = getattr(result, "stdout", "")
            if stdout and stdout.strip():
                temporary.write_text(stdout.strip() + "\n", encoding="utf-8")
        if not temporary.is_file() or not temporary.read_text(encoding="utf-8").strip():
            raise ContentStageError("content_runner_empty", "内容 runner 返回空稿件")
        try:
            draft = validate_content_draft(root, job_id, temporary)
        except ContentStageError as exc:
            try:
                quarantine = _quarantine_candidate(
                    root,
                    job_id,
                    temporary,
                    runner=runner_name,
                    error_code=exc.code,
                )
            except (OSError, ContentStageError) as quarantine_error:
                if isinstance(quarantine_error, ContentStageError):
                    raise
                raise ContentStageError(
                    "content_quarantine_failed", "候选稿无法安全隔离"
                ) from quarantine_error
            exc.quarantine = quarantine
            raise
        os.replace(temporary, output)
        return ValidatedDraft(path=output, metadata=draft.metadata, body=draft.body)
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise ContentStageError("content_runner_failed", "内容 runner 执行失败") from exc
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and validate one high-quality draft")
    parser.add_argument("command", choices=("run", "validate"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--runner-config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    try:
        if args.command == "run":
            if args.runner_config is None:
                raise ContentStageError("content_runner_config_missing", "缺少内容 runner 配置")
            run_content_stage(root, args.job_id, args.runner_config.resolve(), args.output)
        else:
            validate_content_draft(root, args.job_id, args.output)
    except ContentStageError as exc:
        update_item_by_job(
            root / "data" / "knowledge.db",
            args.job_id,
            status="analyzed",
            error=exc.code,
            preserve_completed=True,
        )
        print(json.dumps({"status": "controlled_failure", "code": exc.code}), file=sys.stderr)
        return 4
    print(json.dumps({"status": "ok", "draft": args.output.name}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
