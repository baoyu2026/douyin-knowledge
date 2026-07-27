from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.analyze_video import JOB_ID_PATTERN, atomic_write_text, format_timestamp, sha256_file
from app.collection_registry import update_item_by_job
from app.content_stage import ValidatedDraft, validate_content_draft
from app.keyframe_selection import resolve_keyframes
from douyin_knowledge.result_archive import RESULTS_LAYOUT, results_root

JOBS_DIR = Path("data/jobs")
INDEX_DIR_NAME = "00-总索引"
INDEX_FILES = ("全部视频.md", "按主题.md", "最近新增.md")
LEGACY_INDEX_FILES = ("待人工复核.md",)
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PublicationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _single_line(value: object) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def safe_component(value: str, *, field: str, slug: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", _single_line(value))
    normalized = INVALID_PATH_CHARS.sub("-", normalized).strip(" .-")
    if slug:
        normalized = re.sub(r"\s+", "-", normalized)
        normalized = re.sub(r"-+", "-", normalized)
    if not normalized or normalized in {".", ".."}:
        raise PublicationError("unsafe_path", f"{field} 不能为空或解析为特殊路径")
    if normalized.upper() in WINDOWS_RESERVED_NAMES:
        raise PublicationError("unsafe_path", f"{field} 使用了系统保留名称")
    if len(normalized) > 80:
        normalized = normalized[:80].rstrip(" .-")
    if not normalized:
        raise PublicationError("unsafe_path", f"{field} 无法生成安全目录名")
    return normalized


def normalize_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        value = safe_component(tag, field="标签")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(value)
    if not normalized:
        raise PublicationError("tags_required", "至少需要一个标签")
    return normalized


def _load_json_mapping(path: Path, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(code, f"无法读取 {path.name}") from exc
    if not isinstance(payload, dict):
        raise PublicationError(code, f"{path.name} 必须是 JSON 对象")
    return payload


def _resolve_inside(path: Path, parent: Path, code: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise PublicationError(code, "路径越出允许目录") from exc
    return resolved


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temp)
        os.replace(temp, target)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def _front_matter(document: str) -> tuple[dict[str, Any], str]:
    if not document.startswith("---\n"):
        return {}, document
    marker = document.find("\n---\n", 4)
    if marker < 0:
        return {}, document
    try:
        metadata = yaml.safe_load(document[4:marker]) or {}
    except yaml.YAMLError:
        return {}, document
    if not isinstance(metadata, dict):
        return {}, document
    return metadata, document[marker + 5 :]


def _existing_metadata(path: Path) -> dict[str, Any]:
    try:
        metadata, _body = _front_matter(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    return metadata


def _registered_library_target(root: Path, job_id: str) -> Path | None:
    db_path = root / "data" / "knowledge.db"
    if not db_path.is_file():
        return None
    try:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT library_path FROM collection_items WHERE job_id = ?",
                (job_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise PublicationError("library_registry_invalid", "无法读取知识条目注册信息") from exc
    if row is None or not row[0]:
        return None
    registered = Path(row[0])
    if not registered.is_absolute():
        registered = root / registered
    return registered.resolve()


def _entry_ref(path: Path) -> str | None:
    manifest = path / "资料信息.yml"
    if not manifest.is_file():
        return None
    try:
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    value = payload.get("entry_ref") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else None


def resolve_library_target(
    root: Path,
    *,
    job_id: str,
    category: str,
    title: str,
    source_video: Path,
) -> Path:
    root = root.resolve()
    library_root = results_root(root)
    safe_category = safe_component(category, field="主分类")
    safe_title = safe_component(title, field="标题")
    registered = _registered_library_target(root, job_id)
    if registered is not None:
        try:
            registered.relative_to(library_root)
        except ValueError:
            pass
        else:
            if registered.exists() and not registered.is_dir():
                raise PublicationError("library_collision", "已登记的知识条目路径被文件占用")
            return registered

    category_root = _resolve_inside(
        library_root / safe_category,
        library_root,
        "library_path_invalid",
    )
    for index in range(1, 1000):
        suffix = "" if index == 1 else f" ({index})"
        stem = safe_title[: 80 - len(suffix)].rstrip(" .-")
        candidate = _resolve_inside(
            category_root / f"{stem}{suffix}",
            library_root,
            "library_path_invalid",
        )
        if not candidate.exists():
            return candidate
        if not candidate.is_dir():
            continue
        if not any(candidate.iterdir()):
            return candidate
        entry_ref = _entry_ref(candidate)
        if entry_ref == job_id:
            return candidate
        existing_video = candidate / "原视频.mp4"
        try:
            if (
                entry_ref is None
                and existing_video.is_file()
                and source_video.is_file()
                and sha256_file(existing_video) == sha256_file(source_video)
            ):
                return candidate
        except OSError:
            pass
    raise PublicationError("library_collision", "同分类下的同名知识条目过多")


def _write_entry_manifest(
    target: Path,
    *,
    job_id: str,
    title: str,
    category: str,
) -> None:
    payload = {
        "schema_version": 1,
        "entry_ref": job_id,
        "title": title,
        "category": category,
        "layout": RESULTS_LAYOUT,
        "source_video": "原视频.mp4",
        "knowledge_note": "内容整理.md",
        "timeline": "附件/完整时间轴.md",
        "keyframes": "精选关键帧/",
    }
    rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    atomic_write_text(target / "资料信息.yml", rendered)


def _select_keyframes(
    analysis_dir: Path,
    manifest: dict[str, Any],
    draft: ValidatedDraft | None = None,
) -> list[Path]:
    if draft is not None:
        raw_evidence = draft.metadata.get("visual_evidence")
        if not isinstance(raw_evidence, list) or not 3 <= len(raw_evidence) <= 8:
            raise PublicationError(
                "invalid_keyframe_selection",
                "高质量发布必须引用 3 至 8 张有效关键帧",
            )
        try:
            available = {
                path.name: path
                for _item, path in resolve_keyframes(
                    analysis_dir, manifest, max_count=None, min_count=3
                )
            }
        except ValueError as exc:
            raise PublicationError(
                "insufficient_keyframes", "发布至少需要 3 张有效关键帧"
            ) from exc
        selected: list[Path] = []
        observed: set[str] = set()
        for item in raw_evidence:
            name = item.get("frame") if isinstance(item, dict) else None
            if not isinstance(name, str) or name in observed or name not in available:
                raise PublicationError(
                    "invalid_keyframe_selection",
                    "高质量内容稿引用了无效或重复的关键帧",
                )
            observed.add(name)
            selected.append(available[name])
        return selected
    try:
        selected = resolve_keyframes(analysis_dir, manifest, max_count=8, min_count=3)
    except ValueError as exc:
        raise PublicationError(
            "insufficient_keyframes", "发布至少需要 3 张有效关键帧"
        ) from exc
    return [path for _item, path in selected]


def _transcript_evidence(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    segments = transcript.get("segments") or []
    return [
        segment
        for segment in segments
        if isinstance(segment, dict) and _single_line(segment.get("text"))
    ]


def _trim(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _render_content(
    *,
    job: dict[str, Any],
    manifest: dict[str, Any],
    transcript: dict[str, Any],
    title: str,
    category: str,
    tags: list[str],
    added_at: str,
    review_status: str,
    evidence_status: str,
) -> str:
    segments = _transcript_evidence(transcript)
    texts = [_single_line(segment.get("text")) for segment in segments]
    joined = " ".join(texts)
    one_line = _trim(joined, 140) if joined else "待人工根据视频内容补充一句话总结。"
    summary = _trim(joined, 600) if joined else "本地分析未获得可用语音文本，请结合关键帧复核。"
    viewpoints = texts[:5] or ["待人工提炼核心观点。"]
    source = job.get("source") if isinstance(job.get("source"), dict) else {}
    source_meta = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    duration = source_meta.get("duration_seconds")
    duration_text = f"{float(duration):.1f} 秒" if isinstance(duration, (int, float)) else "未知"
    author = _single_line(job.get("author")) or "未知"
    position = source.get("position") or "未知"
    case_lines = [text for text in texts if re.search(r"\d", text)][:5]
    yaml_metadata = {
        "标题": title,
        "主分类": category,
        "标签": tags,
        "新增时间": added_at,
        "复核状态": review_status,
        "证据核验状态": evidence_status,
        "质量模式": "低质量待复核",
    }
    header = yaml.safe_dump(
        yaml_metadata,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    lines = [
        "---",
        header,
        "---",
        "",
        f"# {title}",
        "",
        "## 基本信息",
        "",
        f"- 作者：{author}",
        f"- 收藏位置：第 {position} 条",
        f"- 视频时长：{duration_text}",
        f"- 主分类：{category}",
        f"- 标签：{'、'.join(tags)}",
        f"- 人工复核：{review_status}",
        "",
        "## 一句话总结",
        "",
        one_line,
        "",
        "## 内容摘要",
        "",
        summary,
        "",
        "## 核心观点",
        "",
    ]
    lines.extend(f"- {text}" for text in viewpoints)
    lines.extend(["", "## 论证结构", ""])
    if segments:
        for index, segment in enumerate(segments[:8], start=1):
            start = float(segment.get("start") or 0)
            lines.append(
                f"{index}. [{format_timestamp(start)}] {_single_line(segment.get('text'))}"
            )
    else:
        lines.append("1. 待人工根据完整时间轴梳理论证结构。")
    lines.extend(["", "## 案例数据", ""])
    lines.extend(f"- {text}" for text in case_lines or ["未自动识别到明确数字案例，待人工复核。"])
    lines.extend(
        [
            "",
            "## 时间轴",
            "",
            "详见 [附件/完整时间轴.md](附件/完整时间轴.md)。",
            "",
            "## 可复用知识",
            "",
            "- 可将核心观点按实际场景改写为检查清单或操作步骤。",
            "- 引用前请回看原视频和时间轴，确认语境与识别准确性。",
            "",
            "## 行动建议",
            "",
            "- 完成人工复核，修正 ASR/OCR 可能造成的专有名词和数字误差。",
            "- 将确认后的观点关联到相同标签的知识条目。",
            "",
            "## 关键词",
            "",
            "、".join(tags),
            "",
            "## 相关内容",
            "",
            "参见 [按主题索引](../../00-总索引/按主题.md) 中的同分类和同标签内容。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_validated_draft(
    draft: ValidatedDraft,
    *,
    title: str,
    category: str,
    tags: list[str],
    added_at: str,
    review_status: str,
    evidence_status: str,
) -> str:
    metadata = {
        "标题": title,
        "主分类": category,
        "标签": tags,
        "新增时间": added_at,
        "复核状态": review_status,
        "证据核验状态": evidence_status,
        "质量模式": "高质量",
    }
    header = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{header}\n---\n\n{draft.body.strip()}\n"


def _verify_library_publication(
    target: Path, source_video: Path, selected_frames: list[Path]
) -> None:
    document = target / "内容整理.md"
    information = target / "资料信息.yml"
    video = target / "原视频.mp4"
    timeline = target / "附件" / "完整时间轴.md"
    frames = [
        path
        for path in (target / "精选关键帧").glob("*")
        if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    ]
    expected_names = {path.name for path in selected_frames}
    if (
        not document.is_file()
        or not information.is_file()
        or not timeline.is_file()
        or not video.is_file()
        or sha256_file(video) != sha256_file(source_video)
        or not expected_names.issubset({path.name for path in frames})
        or not 3 <= len(frames) <= 8
    ):
        raise PublicationError("library_verification_failed", "Library 发布后验收失败")


def _verify_obsidian_publication(root: Path, vault: Path, job_id: str) -> None:
    with sqlite3.connect(root / "data" / "knowledge.db") as connection:
        row = connection.execute(
            "SELECT publication.note_path, publication.attachment_path, item.library_path "
            "FROM obsidian_publications AS publication "
            "JOIN collection_items AS item USING(source_id) WHERE item.job_id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise PublicationError("obsidian_registry_missing", "Obsidian 发布登记缺失")
    note = (vault / row[0]).resolve()
    attachments = (vault / row[1]).resolve()
    library_entry = Path(row[2]).resolve() if row[2] else None
    try:
        note.relative_to(vault.resolve())
        attachments.relative_to(vault.resolve())
    except ValueError as exc:
        raise PublicationError("obsidian_registry_path_invalid", "Obsidian 登记路径无效") from exc
    frames = [
        path
        for path in attachments.glob("*")
        if path.is_file()
        and re.fullmatch(
            r"frame-\d{3}(?:-\d{9}ms)?\.(?:jpe?g|png)",
            path.name,
            re.IGNORECASE,
        )
    ]
    complete = (
        note.is_file()
        and (attachments / "完整时间轴.md").is_file()
        and 3 <= len(frames) <= 8
    )
    if complete and library_entry is not None:
        from app.obsidian_publish import _raw_video_target

        expected_video = (library_entry / "原视频.mp4").resolve()
        linked_video = _raw_video_target(note.read_text(encoding="utf-8"))
        complete = bool(
            linked_video == expected_video
            and linked_video.is_file()
            and sha256_file(linked_video) == sha256_file(expected_video)
        )
    if not complete:
        raise PublicationError("obsidian_verification_failed", "Obsidian 发布后验收失败")


def publish_job(
    root: Path,
    *,
    job_id: str,
    category: str,
    title: str | None,
    tags: list[str],
    review_status: str = "未检查（可选）",
    vault: Path | None = None,
    content_draft: Path | None = None,
    quality_mode: str = "low-review",
) -> Path:
    root = root.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise PublicationError("invalid_job_id", "job ID 格式不安全")
    job_dir = _resolve_inside(root / JOBS_DIR / job_id, root / JOBS_DIR, "job_outside_root")
    source_video = job_dir / "source.mp4"
    analysis_dir = job_dir / "analysis"
    job = _load_json_mapping(job_dir / "job.json", "job_state_invalid")
    manifest = _load_json_mapping(analysis_dir / "manifest.json", "analysis_invalid")
    transcript = _load_json_mapping(analysis_dir / "transcript.json", "analysis_invalid")
    timeline = analysis_dir / "timeline.md"
    if not source_video.is_file() or source_video.stat().st_size <= 0 or not timeline.is_file():
        raise PublicationError("analysis_incomplete", "job 缺少原视频或完整分析产物")
    if job.get("job_id") != job_id:
        raise PublicationError("job_state_mismatch", "job 状态与目录不一致")
    if quality_mode not in {"low-review", "high-quality"}:
        raise PublicationError("quality_mode_invalid", "发布质量模式无效")
    if quality_mode == "high-quality" and content_draft is None:
        raise PublicationError("content_draft_required", "高质量发布缺少内容稿")

    draft: ValidatedDraft | None = None
    evidence_status = "needs_review"
    if content_draft is not None:
        try:
            draft = validate_content_draft(root, job_id, content_draft)
        except Exception as exc:
            code = getattr(exc, "code", "content_draft_invalid")
            raise PublicationError(str(code), "高质量内容稿未通过门禁") from exc
        category = draft.category
        title = draft.title
        tags = draft.tags
        evidence_status = draft.evidence_status
        quality_mode = "high-quality"

    from app.obsidian_publish import configured_vault, publish_to_obsidian

    selected_vault = vault.resolve() if vault is not None else configured_vault(root)
    if quality_mode == "high-quality" and selected_vault is None:
        raise PublicationError("obsidian_vault_required", "高质量发布必须写入 Obsidian")

    safe_category = safe_component(category, field="主分类")
    display_title = _single_line(title) or _single_line(job.get("title")) or job_id
    normalized_tags = normalize_tags(tags)
    library_root = results_root(root)
    target = resolve_library_target(
        root,
        job_id=job_id,
        category=safe_category,
        title=display_title,
        source_video=source_video,
    )
    document_path = target / "内容整理.md"
    existing = _existing_metadata(document_path)
    existing_video = target / "原视频.mp4"
    if existing_video.exists():
        try:
            same_media = (
                existing_video.is_file()
                and sha256_file(existing_video) == sha256_file(source_video)
            )
        except OSError as exc:
            raise PublicationError("library_collision", "无法验证已有知识条目") from exc
        if not same_media and _entry_ref(target) != job_id:
            raise PublicationError("library_collision", "同名知识条目属于不同视频")
    added_at = _single_line(existing.get("新增时间")) or datetime.now(UTC).isoformat()
    selected_frames = _select_keyframes(analysis_dir, manifest, draft)
    if draft is not None:
        content = _render_validated_draft(
            draft,
            title=display_title,
            category=safe_category,
            tags=normalized_tags,
            added_at=added_at,
            review_status=review_status,
            evidence_status=evidence_status,
        )
    else:
        content = _render_content(
            job=job,
            manifest=manifest,
            transcript=transcript,
            title=display_title,
            category=safe_category,
            tags=normalized_tags,
            added_at=added_at,
            review_status=review_status,
            evidence_status=evidence_status,
        )

    target.mkdir(parents=True, exist_ok=True)
    _atomic_copy(source_video, target / "原视频.mp4")
    selected_frame_names = {frame.name for frame in selected_frames}
    frame_target = target / "精选关键帧"
    if frame_target.is_dir():
        for existing_frame in frame_target.iterdir():
            if (
                existing_frame.is_file()
                and existing_frame.suffix.lower() in {".jpg", ".jpeg", ".png"}
                and existing_frame.name not in selected_frame_names
            ):
                existing_frame.unlink()
    for frame in selected_frames:
        _atomic_copy(frame, frame_target / frame.name)
    _atomic_copy(timeline, target / "附件" / "完整时间轴.md")
    atomic_write_text(document_path, content)
    _write_entry_manifest(
        target,
        job_id=job_id,
        title=display_title,
        category=safe_category,
    )
    generate_indexes(library_root)
    _verify_library_publication(target, source_video, selected_frames)
    update_item_by_job(
        root / "data" / "knowledge.db",
        job_id,
        status="analyzed",
        media_sha256=sha256_file(source_video),
        job_path=job_dir,
        library_path=target,
    )
    if selected_vault is not None:
        publish_to_obsidian(
            root,
            selected_vault,
            job_id=job_id,
            library_entry=target,
        )
        _verify_obsidian_publication(root, selected_vault, job_id)
    return target


def discover_entries(library_root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not library_root.exists():
        return entries
    for document in sorted(library_root.glob("*/*/内容整理.md")):
        try:
            relative = document.relative_to(library_root)
        except ValueError:
            continue
        if relative.parts[0] == INDEX_DIR_NAME or document.is_symlink():
            continue
        metadata = _existing_metadata(document)
        title = _single_line(metadata.get("标题"))
        category = _single_line(metadata.get("主分类"))
        tags = metadata.get("标签")
        if not title or not category or not isinstance(tags, list):
            continue
        entries.append(
            {
                "title": title,
                "category": category,
                "tags": [_single_line(tag) for tag in tags if _single_line(tag)],
                "added_at": _single_line(metadata.get("新增时间")),
                "review_status": _single_line(metadata.get("复核状态")) or "未检查（可选）",
                "link": "../" + relative.as_posix(),
            }
        )
    return entries


def _entry_link(entry: dict[str, Any]) -> str:
    title = str(entry["title"]).replace("[", "\\[").replace("]", "\\]")
    return f"[{title}]({entry['link']})"


def generate_indexes(library_root: Path) -> None:
    entries = discover_entries(library_root)
    index_dir = library_root / INDEX_DIR_NAME
    index_dir.mkdir(parents=True, exist_ok=True)

    all_lines = [
        "# 全部视频",
        "",
        "| 标题 | 主分类 | 标签 | 新增时间 | 检查状态 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in sorted(entries, key=lambda item: (item["category"], item["title"])):
        all_lines.append(
            f"| {_entry_link(entry)} | {entry['category']} | {'、'.join(entry['tags'])} | "
            f"{entry['added_at']} | {entry['review_status']} |"
        )
    if not entries:
        all_lines.append("| 暂无内容 | - | - | - | - |")

    topic_lines = ["# 按主题", ""]
    categories = sorted({entry["category"] for entry in entries})
    for category in categories:
        topic_lines.extend([f"## {category}", ""])
        for entry in sorted(
            (item for item in entries if item["category"] == category),
            key=lambda item: item["title"],
        ):
            topic_lines.append(f"- {_entry_link(entry)}：{'、'.join(entry['tags'])}")
        topic_lines.append("")
    tag_names = sorted({tag for entry in entries for tag in entry["tags"]})
    if tag_names:
        topic_lines.extend(["## 标签", ""])
        for tag in tag_names:
            links = "、".join(_entry_link(entry) for entry in entries if tag in entry["tags"])
            topic_lines.append(f"- **{tag}**：{links}")
        topic_lines.append("")
    if not entries:
        topic_lines.append("暂无内容。")

    recent_lines = ["# 最近新增", ""]
    recent = sorted(entries, key=lambda item: item["added_at"], reverse=True)[:50]
    recent_lines.extend(
        f"- {entry['added_at'] or '时间未知'} · {_entry_link(entry)} · {entry['category']}"
        for entry in recent
    )
    if not recent:
        recent_lines.append("暂无内容。")

    rendered = {
        "全部视频.md": "\n".join(all_lines).rstrip() + "\n",
        "按主题.md": "\n".join(topic_lines).rstrip() + "\n",
        "最近新增.md": "\n".join(recent_lines).rstrip() + "\n",
    }
    for name in INDEX_FILES:
        atomic_write_text(index_dir / name, rendered[name])
    for name in LEGACY_INDEX_FILES:
        (index_dir / name).unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish one analyzed job to the knowledge library"
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--title")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--content-draft", type=Path)
    parser.add_argument(
        "--quality-mode",
        choices=("low-review", "high-quality"),
        default="low-review",
    )
    parser.add_argument(
        "--review-status",
        choices=("未检查（可选）", "已检查"),
        default="未检查（可选）",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        target = publish_job(
            args.root,
            job_id=args.job_id,
            category=args.category,
            title=args.title,
            tags=args.tag,
            review_status=args.review_status,
            vault=args.vault,
            content_draft=args.content_draft,
            quality_mode=args.quality_mode,
        )
    except PublicationError as exc:
        print(
            json.dumps(
                {"status": "controlled_failure", "code": exc.code, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 4
    print(
        json.dumps(
            {"status": "ok", "library_entry": str(target.relative_to(args.root.resolve()))},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
