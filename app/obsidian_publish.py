from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import yaml

from app.analyze_video import atomic_write_text, sha256_file
from app.publish_library import INVALID_PATH_CHARS, PublicationError, _front_matter

AUTO_START = "<!-- AUTO-GENERATED:START -->"
AUTO_END = "<!-- AUTO-GENERATED:END -->"
NOTE_ROOT = Path("40-Resources/抖音收藏")
ATTACHMENT_ROOT = Path("99-Attachments/抖音收藏")
MANAGED_PROPERTIES = {
    "type",
    "source",
    "title",
    "category",
    "topics",
    "author",
    "duration_sec",
    "quality",
    "review_status",
    "evidence_status",
    "favorite_state",
    "processed_at",
    "uploaded_at",
    "content_version",
    "cover",
    "related_notes",
    "tags",
}
MANAGED_FRAME_NAME = re.compile(
    r"^frame-\d{3}(?:-\d{9}ms)?\.(?:jpe?g|png)$",
    re.IGNORECASE,
)


def ensure_obsidian_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS obsidian_publications (
            source_id TEXT PRIMARY KEY,
            media_sha256 TEXT NOT NULL,
            note_path TEXT NOT NULL UNIQUE,
            attachment_path TEXT NOT NULL,
            published_at TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES collection_items(source_id)
        )
        """
    )


def configured_vault(root: Path) -> Path | None:
    config_path = root / "config" / "obsidian.yml"
    if not config_path.is_file():
        return None
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PublicationError("obsidian_config_invalid", "无法读取 Obsidian 配置") from exc
    raw = config.get("vault") if isinstance(config, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise PublicationError("obsidian_config_invalid", "Obsidian 配置缺少 Vault 路径")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def sync_favorite_states(root: Path, vault: Path) -> int:
    """Reflect registry collection state into already-published note properties."""
    root = root.resolve()
    vault = vault.resolve()
    if not vault.is_dir() or not (vault / ".obsidian").is_dir():
        raise PublicationError("obsidian_vault_invalid", "目标不是可用的 Obsidian Vault")
    db_path = root / "data" / "knowledge.db"
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        ensure_obsidian_schema(connection)
        rows = connection.execute(
            """
            SELECT publication.note_path, item.currently_collected
            FROM obsidian_publications AS publication
            JOIN collection_items AS item USING(source_id)
            """
        ).fetchall()

    changed = 0
    for row in rows:
        note = (vault / Path(row["note_path"])).resolve()
        try:
            note.relative_to(vault)
        except ValueError as exc:
            raise PublicationError(
                "obsidian_registry_path_invalid", "发布登记路径越出 Vault"
            ) from exc
        if not note.is_file():
            raise PublicationError("obsidian_note_missing", "已登记的 Obsidian 笔记不存在")
        document = note.read_text(encoding="utf-8")
        metadata, body = _front_matter(document)
        expected = "active" if row["currently_collected"] else "uncollected"
        if metadata.get("favorite_state") == expected:
            continue
        metadata["favorite_state"] = expected
        rendered = _render_front_matter({}, metadata) + body
        atomic_write_text(note, rendered.rstrip() + "\n")
        changed += 1
    return changed


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temp)
        os.replace(temp, target)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def _split_generated(document: str) -> tuple[str, str]:
    start = document.find(AUTO_START)
    end = document.find(AUTO_END)
    if start < 0 or end < start:
        raise PublicationError("obsidian_boundary_missing", "已有笔记缺少自动更新边界")
    return document[: start + len(AUTO_START)], document[end:]


def _summary_from_body(body: str) -> str:
    match = re.search(
        r"^## 一句话总结\s*$\s*(.+?)(?=^## |\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return "已完成本地分析与结构化整理。"
    return " ".join(match.group(1).strip().split())


def _render_front_matter(existing: dict[str, Any], managed: dict[str, Any]) -> str:
    metadata = {key: value for key, value in existing.items() if key not in MANAGED_PROPERTIES}
    metadata.update(managed)
    return (
        "---\n"
        + yaml.safe_dump(
            metadata,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()
        + "\n---\n"
    )


def _aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _replace_front_matter(document: str, rendered: str) -> str:
    _metadata, body = _front_matter(document)
    return rendered + body.lstrip("\n")


def _generated_body(
    library_body: str,
    *,
    frame_links: list[str],
    timeline_link: str,
) -> str:
    body = library_body.strip().replace("## 案例数据", "## 关键案例与数据")
    body = re.sub(
        r"\[附件/完整时间轴\.md\]\(附件/完整时间轴\.md\)",
        f"[[{timeline_link}|完整时间轴]]",
        body,
    )
    frame_block = "## 精选关键帧\n\n" + "\n\n".join(f"![[{link}]]" for link in frame_links)
    timeline_heading = re.search(r"^## (?:完整)?时间轴\s*$", body, flags=re.MULTILINE)
    if timeline_heading:
        timeline_link_markup = f"[[{timeline_link}|完整时间轴]]"
        next_heading = re.search(
            r"^## ", body[timeline_heading.end() :], flags=re.MULTILINE
        )
        timeline_end = (
            timeline_heading.end() + next_heading.start()
            if next_heading
            else len(body)
        )
        if timeline_link_markup not in body[timeline_heading.start() : timeline_end]:
            body = (
                body[:timeline_end].rstrip()
                + f"\n\n详细逐段记录见 {timeline_link_markup}。\n\n"
                + body[timeline_end:].lstrip()
            )
        body = (
            body[: timeline_heading.start()]
            + frame_block
            + "\n\n"
            + body[timeline_heading.start() :]
        )
    else:
        body += "\n\n" + frame_block + "\n\n## 完整时间轴\n\n"
        body += f"详细逐段记录见 [[{timeline_link}|完整时间轴]]。"
    return body.strip()


def _raw_video_target(document: str) -> Path | None:
    match = re.search(r"\(file:///([^\s)]+)\)", document)
    if not match:
        return None
    parsed = urlparse("file:///" + match.group(1))
    value = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:/", value):
        value = value[1:]
    return Path(value)


def _validate_unmapped_existing(note: Path, source_video: Path, media_hash: str) -> None:
    try:
        document = note.read_text(encoding="utf-8")
        linked = _raw_video_target(document)
        same = linked is not None and linked.is_file() and sha256_file(linked) == media_hash
    except OSError as exc:
        raise PublicationError("obsidian_collision", "无法验证已有同名笔记") from exc
    if not same or sha256_file(source_video) != media_hash:
        raise PublicationError("obsidian_collision", "同名笔记属于其他媒体")


def _obsidian_component(value: str, *, field: str) -> str:
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    normalized = INVALID_PATH_CHARS.sub("-", normalized).strip(" .-")
    if not normalized or normalized in {".", ".."}:
        raise PublicationError("unsafe_path", f"{field} 无法生成安全文件名")
    normalized = normalized[:80].rstrip(" .-")
    if not normalized:
        raise PublicationError("unsafe_path", f"{field} 无法生成安全文件名")
    return normalized


def publish_to_obsidian(
    root: Path,
    vault: Path,
    *,
    job_id: str,
    library_entry: Path,
) -> Path:
    root = root.resolve()
    vault = vault.resolve()
    if not vault.is_dir() or not (vault / ".obsidian").is_dir():
        raise PublicationError("obsidian_vault_invalid", "目标不是可用的 Obsidian Vault")
    document_path = library_entry / "内容整理.md"
    source_video = library_entry / "原视频.mp4"
    timeline = library_entry / "附件" / "完整时间轴.md"
    frames = sorted((library_entry / "精选关键帧").glob("*"))
    if not document_path.is_file() or not source_video.is_file() or not timeline.is_file():
        raise PublicationError("obsidian_source_incomplete", "知识条目缺少发布产物")
    frames = [path for path in frames if path.suffix.lower() in {".jpg", ".jpeg", ".png"}]
    if not 3 <= len(frames) <= 8:
        raise PublicationError("obsidian_frames_invalid", "精选关键帧数量必须为 3 至 8 张")

    library_document = document_path.read_text(encoding="utf-8")
    metadata, library_body = _front_matter(library_document)
    title = str(metadata.get("标题") or "").strip()
    category = str(metadata.get("主分类") or "").strip()
    topics = metadata.get("标签") or []
    if not title or not category or not isinstance(topics, list):
        raise PublicationError("obsidian_metadata_invalid", "知识条目元数据不完整")
    safe_title = _obsidian_component(title, field="标题")
    safe_category = _obsidian_component(category, field="主分类")
    media_hash = sha256_file(source_video)

    db_path = root / "data" / "knowledge.db"
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        ensure_obsidian_schema(connection)
        item = connection.execute(
            "SELECT source_id, currently_collected FROM collection_items WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if item is None:
            raise PublicationError("obsidian_registry_missing", "外部注册表缺少当前条目")
        existing = connection.execute(
            "SELECT media_sha256, note_path, attachment_path, published_at "
            "FROM obsidian_publications "
            "WHERE source_id = ?",
            (item["source_id"],),
        ).fetchone()
        note_relative = (
            Path(existing["note_path"])
            if existing
            else NOTE_ROOT / safe_category / f"{safe_title}.md"
        )
        attachment_relative = (
            Path(existing["attachment_path"])
            if existing
            else ATTACHMENT_ROOT / safe_title
        )

    note = vault / note_relative
    attachment_dir = vault / attachment_relative
    if note.exists() and existing is None:
        _validate_unmapped_existing(note, source_video, media_hash)
    elif note.exists() and not note.is_file():
        raise PublicationError("obsidian_collision", "笔记路径被其他对象占用")

    attachment_dir.mkdir(parents=True, exist_ok=True)
    selected_frame_names = {frame.name for frame in frames}
    for existing_frame in attachment_dir.iterdir():
        if (
            existing_frame.is_file()
            and MANAGED_FRAME_NAME.fullmatch(existing_frame.name)
            and existing_frame.name not in selected_frame_names
        ):
            existing_frame.unlink()
    for frame in frames:
        _atomic_copy(frame, attachment_dir / frame.name)
    _atomic_copy(timeline, attachment_dir / "完整时间轴.md")

    attachment_posix = attachment_relative.as_posix()
    frame_links = [f"{attachment_posix}/{frame.name}" for frame in frames]
    timeline_link = f"{attachment_posix}/完整时间轴"
    generated = _generated_body(
        library_body,
        frame_links=frame_links,
        timeline_link=timeline_link,
    )
    job_path = root / "data" / "jobs" / job_id / "job.json"
    manifest_path = root / "data" / "jobs" / job_id / "analysis" / "manifest.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_meta = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    duration = source_meta.get("duration_seconds")
    existing_document = note.read_text(encoding="utf-8") if note.is_file() else ""
    existing_metadata, _existing_body = _front_matter(existing_document)
    processed_at = str(existing_metadata.get("processed_at") or datetime.now(UTC).date())
    uploaded_at = _aware_datetime(existing_metadata.get("uploaded_at"))
    if uploaded_at is None and existing is not None:
        uploaded_at = _aware_datetime(existing["published_at"])
    if uploaded_at is None:
        uploaded_at = datetime.now().astimezone().replace(microsecond=0)
    related_notes = existing_metadata.get("related_notes") or []
    managed = {
        "type": "douyin-video",
        "source": "抖音收藏",
        "title": title,
        "category": category,
        "topics": [str(value) for value in topics],
        "author": str(job.get("author") or "未知"),
        "duration_sec": int(round(float(duration))) if isinstance(duration, (int, float)) else 0,
        "quality": "high" if metadata.get("质量模式") == "高质量" else "low-review",
        "review_status": "unreviewed",
        "evidence_status": (
            "verified" if metadata.get("证据核验状态") == "verified" else "needs_review"
        ),
        "favorite_state": "active" if item["currently_collected"] else "uncollected",
        "processed_at": processed_at,
        "uploaded_at": uploaded_at,
        "content_version": 2,
        "cover": f"[[{frame_links[0]}]]",
        "related_notes": related_notes,
        "tags": ["来源/抖音收藏", f"领域/{category}"]
        + [f"主题/{value}" for value in topics],
    }
    front_matter = _render_front_matter(existing_metadata, managed)
    if existing_document:
        prefix, suffix = _split_generated(existing_document)
        body_document = prefix + "\n" + generated + "\n" + suffix
        rendered = _replace_front_matter(body_document, front_matter)
    else:
        rendered = (
            front_matter
            + "\n> [!summary] 核心摘要\n> "
            + _summary_from_body(library_body)
            + "\n\n"
            + AUTO_START
            + "\n"
            + generated
            + "\n"
            + AUTO_END
            + "\n\n## 我的批注\n\n> [!note] 手工内容区\n"
            + "> 这里的内容不会被自动更新覆盖。\n\n"
            + "## 原始资料\n\n- [在本机打开原视频]("
            + source_video.as_uri()
            + ")\n- 原始技术产物保留在项目目录，不进入 Obsidian Sync。\n"
        )
    if "\\n" in rendered:
        raise PublicationError("obsidian_literal_newline", "生成笔记包含字面换行转义")
    atomic_write_text(note, rendered.rstrip() + "\n")

    with sqlite3.connect(db_path) as connection:
        ensure_obsidian_schema(connection)
        connection.execute(
            """
            INSERT INTO obsidian_publications(
                source_id, media_sha256, note_path, attachment_path, published_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                media_sha256 = excluded.media_sha256,
                note_path = excluded.note_path,
                attachment_path = excluded.attachment_path
            """,
            (
                item["source_id"],
                media_hash,
                note_relative.as_posix(),
                attachment_relative.as_posix(),
                uploaded_at.isoformat(),
            ),
        )
    return note
