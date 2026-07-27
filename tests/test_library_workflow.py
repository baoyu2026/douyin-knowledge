import asyncio
import io
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from app.analyze_video import AnalysisError, resolve_job_paths
from app.collection_registry import begin_snapshot, complete_snapshot, ingest_snapshot_page
from app.migrate_library import MigrationError, build_plan
from app.obsidian_publish import _generated_body, _raw_video_target, _refresh_raw_video_link
from app.probe_one import (
    CollectResult,
    FetchResult,
    Reporter,
    _collection_page_result,
    execute_probe,
    existing_job_source,
    stable_job_id,
)
from app.publish_library import PublicationError, _front_matter, publish_job, safe_component


def register_collection_item(root: Path, aweme_id: str) -> str:
    db_path = root / "data" / "knowledge.db"
    snapshot = begin_snapshot(db_path)
    ingest_snapshot_page(
        db_path,
        snapshot_id=snapshot.snapshot_id,
        cursor=0,
        next_cursor=None,
        has_more=False,
        items=[{"aweme_id": aweme_id}],
    )
    complete_snapshot(db_path, snapshot.snapshot_id)
    return db_path


def make_job(root: Path, *, aweme_id: str = "private-aweme-2", title: str = "路径安全 标题") -> str:
    job_id = stable_job_id({"aweme_id": aweme_id})
    job_dir = root / "data" / "jobs" / job_id
    analysis = job_dir / "analysis"
    keyframes = analysis / "keyframes"
    keyframes.mkdir(parents=True)
    (job_dir / "source.mp4").write_bytes(b"fixture video")
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "source": {"aweme_id": aweme_id, "position": 2, "cursor": "private-cursor"},
                "title": title,
                "author": "作者",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    items = []
    for index in range(10):
        filename = f"frame-{index + 1:03d}.jpg"
        (keyframes / filename).write_bytes(f"frame-{index}".encode())
        items.append(
            {
                "id": index + 1,
                "timestamp": float(index),
                "file": f"keyframes/{filename}",
            }
        )
    manifest = {
        "source": {"duration_seconds": 30.0},
        "keyframes": {"count": len(items), "items": items},
    }
    (analysis / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    transcript = {
        "segments": [
            {"id": 0, "start": 0.0, "end": 2.0, "text": "第一条核心观点包含 3 个步骤"},
            {"id": 1, "start": 2.0, "end": 4.0, "text": "第二条观点用于验证分类与标签"},
        ]
    }
    (analysis / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
    )
    (analysis / "timeline.md").write_text("# 内容时间轴\n", encoding="utf-8")
    return job_id


def test_position_two_selects_second_collection_entry() -> None:
    result = _collection_page_result(
        {
            "status_code": 0,
            "aweme_list": [{"aweme_id": "first"}, {"aweme_id": "second"}],
            "cursor": 20,
            "has_more": True,
        },
        position=2,
    )

    assert result.item == {"aweme_id": "second"}
    assert result.position == 2
    assert result.cursor == 20
    assert result.has_more is True


def test_job_download_layout_is_stable_and_stdout_is_private(tmp_path: Path) -> None:
    private_aweme_id = "private-aweme-id-position-2"
    private_cursor = "private-cursor-signature"
    private_url = "https://v.douyinvod.com/private-request-url"
    selected = CollectResult(
        {
            "aweme_id": private_aweme_id,
            "desc": "private-title",
            "video": {"play_addr": {"url_list": [private_url]}},
        },
        position=2,
        cursor=private_cursor,
    )
    destinations = []

    async def source_fetcher():
        return selected

    async def fetcher(url, destination):
        destinations.append((url, destination))
        return FetchResult(200, True, True, size_bytes=100, signature="iso-bmff")

    stream = io.StringIO()
    result = asyncio.run(
        execute_probe(
            source_fetcher=source_fetcher,
            fetcher=fetcher,
            output_dir=tmp_path / "data" / "jobs",
            reporter=Reporter(stream),
            use_job_layout=True,
        )
    )

    job_id = stable_job_id(selected.item or {})
    assert result == 0
    assert destinations == [(private_url, tmp_path / "data" / "jobs" / job_id)]
    state = json.loads((destinations[0][1] / "job.json").read_text(encoding="utf-8"))
    assert state["source"] == {
        "aweme_id": private_aweme_id,
        "position": 2,
        "cursor": private_cursor,
    }
    output = stream.getvalue()
    assert job_id not in output
    assert private_aweme_id not in output
    assert private_cursor not in output
    assert private_url not in output
    assert '"signature"' not in output


def test_existing_job_source_is_reused_without_a_network_fetch(tmp_path: Path) -> None:
    job_dir = tmp_path / "data" / "jobs" / "aweme-existing"
    job_dir.mkdir(parents=True)
    body = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
    (job_dir / "source.mp4").write_bytes(body)

    result = existing_job_source(job_dir)

    assert result == FetchResult(
        200,
        True,
        True,
        size_bytes=len(body),
        signature="iso-bmff",
        reused=True,
    )


def test_explicit_analysis_job_paths_and_traversal_guard(tmp_path: Path) -> None:
    job_id = "aweme-0123456789abcdefabcd"
    job_dir = tmp_path / "data" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    video = job_dir / "source.mp4"
    video.write_bytes(b"video")

    assert resolve_job_paths(
        tmp_path,
        job_id=job_id,
        explicit_input=None,
        explicit_output=None,
    ) == (video, job_dir / "analysis", job_id)

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    with pytest.raises(AnalysisError) as error:
        resolve_job_paths(
            tmp_path,
            job_id=None,
            explicit_input=outside,
            explicit_output=job_dir / "analysis",
        )
    assert error.value.code == "input_outside_jobs"


@pytest.mark.parametrize("value", ["..", ".", "CON", "///", "  "])
def test_unsafe_library_path_components_are_rejected(value: str) -> None:
    with pytest.raises(PublicationError):
        safe_component(value, field="测试", slug=True)


def test_publish_is_idempotent_and_builds_classification_tags_and_indexes(
    tmp_path: Path,
) -> None:
    private_aweme_id = "private-aweme-for-library"
    job_id = make_job(tmp_path, aweme_id=private_aweme_id)

    first = publish_job(
        tmp_path,
        job_id=job_id,
        category="软件工程",
        title=None,
        tags=["Python", "测试", "Python"],
    )
    first_document = (first / "内容整理.md").read_text(encoding="utf-8")
    second = publish_job(
        tmp_path,
        job_id=job_id,
        category="软件工程",
        title=None,
        tags=["Python", "测试"],
    )

    assert first == second
    assert (second / "内容整理.md").read_text(encoding="utf-8") == first_document
    assert {path.name for path in second.iterdir()} == {
        "内容整理.md",
        "原视频.mp4",
        "精选关键帧",
        "附件",
        "资料信息.yml",
    }
    assert len(list((second / "精选关键帧").iterdir())) == 8
    assert {path.name for path in (second / "附件").iterdir()} == {"完整时间轴.md"}
    for heading in (
        "基本信息",
        "一句话总结",
        "内容摘要",
        "核心观点",
        "论证结构",
        "案例数据",
        "时间轴",
        "可复用知识",
        "行动建议",
        "关键词",
        "相关内容",
    ):
        assert f"## {heading}" in first_document
    assert "主分类：软件工程" in first_document
    assert "Python、测试" in first_document
    assert private_aweme_id not in first_document
    assert job_id not in first_document
    assert "private-cursor" not in first_document

    index_dir = tmp_path / "library" / "00-总索引"
    assert {path.name for path in index_dir.iterdir()} == {
        "全部视频.md",
        "按主题.md",
        "最近新增.md",
    }
    assert "路径安全 标题" in (index_dir / "全部视频.md").read_text(encoding="utf-8")
    assert "## 软件工程" in (index_dir / "按主题.md").read_text(encoding="utf-8")
    assert "**Python**" in (index_dir / "按主题.md").read_text(encoding="utf-8")
    assert "路径安全 标题" in (index_dir / "最近新增.md").read_text(encoding="utf-8")
    assert not (index_dir / "待人工复核.md").exists()


def test_publish_numbers_same_title_for_different_video_without_exposing_source_id(
    tmp_path: Path,
) -> None:
    first_id = make_job(tmp_path, aweme_id="private-first", title="同名标题")
    publish_job(
        tmp_path,
        job_id=first_id,
        category="软件工程",
        title="同名标题",
        tags=["测试"],
    )
    second_id = make_job(tmp_path, aweme_id="private-second", title="同名标题")
    (tmp_path / "data" / "jobs" / second_id / "source.mp4").write_bytes(
        b"different fixture video"
    )

    second = publish_job(
        tmp_path,
        job_id=second_id,
        category="软件工程",
        title="同名标题",
        tags=["测试"],
    )

    assert second.name == "同名标题 (2)"
    document = (
        tmp_path / "library" / "软件工程" / "同名标题" / "内容整理.md"
    ).read_text(encoding="utf-8")
    assert "private-first" not in document
    assert "private-second" not in document
    assert second_id not in (second / "内容整理.md").read_text(encoding="utf-8")


def test_publish_keeps_distinct_jobs_separate_even_when_media_bytes_match(
    tmp_path: Path,
) -> None:
    first_id = make_job(tmp_path, aweme_id="private-identical-first", title="重复标题")
    first = publish_job(
        tmp_path,
        job_id=first_id,
        category="软件工程",
        title="重复标题",
        tags=["测试"],
    )
    second_id = make_job(tmp_path, aweme_id="private-identical-second", title="重复标题")
    second = publish_job(
        tmp_path,
        job_id=second_id,
        category="软件工程",
        title="重复标题",
        tags=["测试"],
    )

    assert first.name == "重复标题"
    assert second.name == "重复标题 (2)"


def test_correction_reuses_the_established_human_directory(tmp_path: Path) -> None:
    source_id = "private-corrected-title"
    register_collection_item(tmp_path, source_id)
    job_id = make_job(tmp_path, aweme_id=source_id, title="原始标题")
    first = publish_job(
        tmp_path,
        job_id=job_id,
        category="软件工程",
        title="原始标题",
        tags=["测试"],
    )
    corrected = publish_job(
        tmp_path,
        job_id=job_id,
        category="新分类",
        title="修正后的标题",
        tags=["测试"],
    )

    assert corrected == first
    assert corrected.parts[-2:] == ("软件工程", "原始标题")
    assert "# 修正后的标题" in (corrected / "内容整理.md").read_text(encoding="utf-8")


def test_obsidian_low_quality_publish_preserves_annotations_without_completion(
    tmp_path: Path,
) -> None:
    aweme_id = "private-obsidian-item"
    register_collection_item(tmp_path, aweme_id)
    job_id = make_job(tmp_path, aweme_id=aweme_id, title="幂等发布：样板")
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)

    publish_job(
        tmp_path,
        job_id=job_id,
        category="软件工程",
        title=None,
        tags=["发布器", "幂等"],
        vault=vault,
    )
    note = vault / "40-Resources" / "抖音收藏" / "软件工程" / "幂等发布：样板.md"
    document = note.read_text(encoding="utf-8")
    first_metadata, _body = _front_matter(document)
    first_uploaded_at = first_metadata["uploaded_at"]
    assert isinstance(first_uploaded_at, datetime)
    assert first_uploaded_at.tzinfo is not None
    assert isinstance(first_metadata["updated_at"], datetime)
    assert first_metadata["updated_at"].tzinfo is not None
    with sqlite3.connect(tmp_path / "data" / "knowledge.db") as connection:
        first_published_at = connection.execute(
            "SELECT published_at FROM obsidian_publications"
        ).fetchone()[0]
    assert first_uploaded_at.isoformat() == first_published_at
    document = re.sub(r"^uploaded_at:.*\n", "", document, count=1, flags=re.MULTILINE)
    document = re.sub(
        r"^updated_at:.*\n",
        "updated_at: 2000-01-01T00:00:00+00:00\n",
        document,
        count=1,
        flags=re.MULTILINE,
    )
    document = document.replace("这里的内容不会被自动更新覆盖。", "我的人工判断：保持不变。")
    document = re.sub(
        r"(?m)^- \[在本机打开原视频\]\([^\n]+\)$",
        f"- [在本机打开原视频]({(tmp_path / 'stale.mp4').as_uri()})",
        document,
    )
    note.write_text(document, encoding="utf-8")
    attachment_dir = vault / "99-Attachments" / "抖音收藏" / "幂等发布：样板"
    (attachment_dir / "frame-999.jpg").write_bytes(b"stale managed frame")
    (attachment_dir / "my-note.jpg").write_bytes(b"unmanaged user attachment")

    publish_job(
        tmp_path,
        job_id=job_id,
        category="软件工程",
        title=None,
        tags=["发布器", "幂等"],
        vault=vault,
    )
    repeated = note.read_text(encoding="utf-8")
    repeated_metadata, _body = _front_matter(repeated)
    assert repeated_metadata["uploaded_at"] == first_uploaded_at
    assert repeated_metadata["updated_at"].year != 2000
    assert repeated.count("我的人工判断：保持不变。") == 1
    assert repeated.count("<!-- AUTO-GENERATED:START -->") == 1
    assert "\\n" not in repeated
    assert job_id not in repeated
    assert aweme_id not in repeated
    assert not list(vault.rglob("*.mp4"))
    assert len(list(attachment_dir.glob("frame-*.jpg"))) == 8
    assert not (attachment_dir / "frame-999.jpg").exists()
    assert (attachment_dir / "my-note.jpg").read_bytes() == b"unmanaged user attachment"
    assert (attachment_dir / "完整时间轴.md").is_file()
    source_video = tmp_path / "library" / "软件工程" / "幂等发布-样板" / "原视频.mp4"
    assert _raw_video_target(repeated) == source_video.resolve()
    assert (
        "[[99-Attachments/抖音收藏/幂等发布：样板/完整时间轴|完整时间轴]]"
        in repeated
    )
    assert "[附件/完整时间轴.md]" not in repeated

    with sqlite3.connect(tmp_path / "data" / "knowledge.db") as connection:
        assert connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (job_id,)
        ).fetchone()[0] == "analyzed"
        assert connection.execute("SELECT COUNT(*) FROM obsidian_publications").fetchone()[0] == 1
        assert connection.execute(
            "SELECT published_at FROM obsidian_publications"
        ).fetchone()[0] == first_published_at


def test_existing_timeline_section_gets_one_vault_link() -> None:
    library_body = (
        "## 一句话总结\n\n摘要。\n\n"
        "## 时间轴\n\n开头介绍背景，中段展开论证。\n\n"
        "## 可复用知识\n\n结论。"
    )
    timeline_link = "99-Attachments/抖音收藏/样板/完整时间轴"

    first = _generated_body(
        library_body,
        frame_links=["99-Attachments/抖音收藏/样板/frame-001.jpg"],
        timeline_link=timeline_link,
    )
    second = _generated_body(
        library_body,
        frame_links=["99-Attachments/抖音收藏/样板/frame-001.jpg"],
        timeline_link=timeline_link,
    )

    expected = f"[[{timeline_link}|完整时间轴]]"
    assert first.count(expected) == 1
    assert second == first
    timeline_section = first.split("## 时间轴", 1)[1].split("## 可复用知识", 1)[0]
    assert expected in timeline_section


def test_generated_body_embeds_frames_beside_their_argument_without_gallery() -> None:
    library_body = (
        "## 论证结构\n\n"
        "### 1. 第一个论点\n\n"
        "**证据**：第一条证据。\n\n"
        "![第一张图](精选关键帧/frame-001.jpg)\n\n"
        "### 2. 第二个论点\n\n"
        "**证据**：第二条证据。\n\n"
        "![第二张图](精选关键帧/frame-002.jpg)\n\n"
        "## 时间轴\n\n00:00 开始。"
    )
    generated = _generated_body(
        library_body,
        frame_links=[
            "99-Attachments/抖音收藏/样板/frame-001.jpg",
            "99-Attachments/抖音收藏/样板/frame-002.jpg",
        ],
        timeline_link="99-Attachments/抖音收藏/样板/完整时间轴",
    )

    assert "![[99-Attachments/抖音收藏/样板/frame-001.jpg|第一张图]]" in generated
    assert "![[99-Attachments/抖音收藏/样板/frame-002.jpg|第二张图]]" in generated
    assert "## 精选关键帧" not in generated


def test_raw_video_link_refresh_rejects_duplicate_managed_links(tmp_path: Path) -> None:
    source = tmp_path / "中文 #100% (样片).mp4"
    source.write_bytes(b"video")
    stale = (tmp_path / "stale.mp4").as_uri()
    document = (
        "## 原始资料\n\n"
        f"- [在本机打开原视频]({stale})\n"
        f"- [在本机打开原视频]({stale})\n"
    )

    with pytest.raises(PublicationError) as duplicate:
        _refresh_raw_video_link(document, source)
    assert duplicate.value.code == "obsidian_video_link_duplicate"

    refreshed = _refresh_raw_video_link("## 原始资料\n", source)
    assert _raw_video_target(refreshed) == source.resolve()


def test_obsidian_failure_does_not_mark_registry_completed(tmp_path: Path) -> None:
    aweme_id = "private-obsidian-failure"
    register_collection_item(tmp_path, aweme_id)
    job_id = make_job(tmp_path, aweme_id=aweme_id, title="失败状态门禁")
    invalid_vault = tmp_path / "not-a-vault"
    invalid_vault.mkdir()

    with pytest.raises(PublicationError) as error:
        publish_job(
            tmp_path,
            job_id=job_id,
            category="软件工程",
            title=None,
            tags=["发布门禁"],
            vault=invalid_vault,
        )
    assert error.value.code == "obsidian_vault_invalid"

    import sqlite3

    with sqlite3.connect(tmp_path / "data" / "knowledge.db") as connection:
        assert connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (job_id,)
        ).fetchone()[0] == "analyzed"
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name = 'obsidian_publications'"
        ).fetchone()[0] == 0


def test_obsidian_publish_rejects_unmapped_same_title_with_different_media(
    tmp_path: Path,
) -> None:
    first_aweme = "private-obsidian-first"
    register_collection_item(tmp_path, first_aweme)
    first_job = make_job(tmp_path, aweme_id=first_aweme, title="同名知识卡")
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    publish_job(
        tmp_path,
        job_id=first_job,
        category="软件工程",
        title=None,
        tags=["碰撞保护"],
        vault=vault,
    )

    second_aweme = "private-obsidian-second"
    snapshot = begin_snapshot(tmp_path / "data" / "knowledge.db")
    ingest_snapshot_page(
        tmp_path / "data" / "knowledge.db",
        snapshot_id=snapshot.snapshot_id,
        cursor=0,
        next_cursor=None,
        has_more=False,
        items=[{"aweme_id": first_aweme}, {"aweme_id": second_aweme}],
    )
    complete_snapshot(tmp_path / "data" / "knowledge.db", snapshot.snapshot_id)
    second_job = make_job(tmp_path, aweme_id=second_aweme, title="同名知识卡")
    (tmp_path / "data" / "jobs" / second_job / "source.mp4").write_bytes(b"different")
    with pytest.raises(PublicationError) as error:
        publish_job(
            tmp_path,
            job_id=second_job,
            category="软件工程",
            title=None,
            tags=["碰撞保护"],
            vault=vault,
        )
    assert error.value.code in {"library_collision", "obsidian_collision"}


def test_obsidian_reanalysis_updates_same_note_and_preserves_annotations(
    tmp_path: Path,
) -> None:
    aweme_id = "private-obsidian-reanalysis"
    register_collection_item(tmp_path, aweme_id)
    job_id = make_job(tmp_path, aweme_id=aweme_id, title="重新分析样板")
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    first = publish_job(
        tmp_path,
        job_id=job_id,
        category="软件工程",
        title=None,
        tags=["重新分析"],
        vault=vault,
    )
    note = vault / "40-Resources" / "抖音收藏" / "软件工程" / "重新分析样板.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            "这里的内容不会被自动更新覆盖。", "重新分析仍保留这条批注。"
        ),
        encoding="utf-8",
    )
    source = tmp_path / "data" / "jobs" / job_id / "source.mp4"
    source.write_bytes(b"updated fixture video")

    second = publish_job(
        tmp_path,
        job_id=job_id,
        category="软件工程",
        title=None,
        tags=["重新分析"],
        vault=vault,
    )

    assert first == second
    assert "重新分析仍保留这条批注。" in note.read_text(encoding="utf-8")
    assert (first / "原视频.mp4").read_bytes() == b"updated fixture video"


def test_registry_favorite_state_sync_preserves_note_body(tmp_path: Path) -> None:
    from app.obsidian_publish import sync_favorite_states

    aweme_id = "private-favorite-state"
    register_collection_item(tmp_path, aweme_id)
    job_id = make_job(tmp_path, aweme_id=aweme_id, title="收藏状态样板")
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    publish_job(
        tmp_path,
        job_id=job_id,
        category="软件工程",
        title=None,
        tags=["收藏状态"],
        vault=vault,
    )
    note = vault / "40-Resources" / "抖音收藏" / "软件工程" / "收藏状态样板.md"
    before = note.read_text(encoding="utf-8")
    snapshot = begin_snapshot(tmp_path / "data" / "knowledge.db")
    ingest_snapshot_page(
        tmp_path / "data" / "knowledge.db",
        snapshot_id=snapshot.snapshot_id,
        cursor=0,
        next_cursor=None,
        has_more=False,
        items=[],
    )
    complete_snapshot(tmp_path / "data" / "knowledge.db", snapshot.snapshot_id)

    assert sync_favorite_states(tmp_path, vault) == 1
    after = note.read_text(encoding="utf-8")
    assert "favorite_state: uncollected" in after
    assert before.split("---\n", 2)[2] == after.split("---\n", 2)[2]
    assert sync_favorite_states(tmp_path, vault) == 0


def test_unconfigured_obsidian_vault_is_optional(tmp_path: Path) -> None:
    from app.obsidian_publish import configured_vault

    config = tmp_path / "config"
    config.mkdir()
    (config / "obsidian.yml").write_text("vault: null\n", encoding="utf-8")

    assert configured_vault(tmp_path) is None


def test_migration_plan_has_no_side_effect_and_blocks_sensitive_path(tmp_path: Path) -> None:
    correct = tmp_path / "data" / "probe-collect" / "correct.mp4"
    correct.parent.mkdir(parents=True)
    correct.write_bytes(b"correct")
    old = tmp_path / "data" / "probe" / "wrong.mp4"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"wrong")

    plan = build_plan(
        tmp_path,
        aweme_id="private-migration-id",
        correct_source=correct,
        correct_analysis=None,
        archive_old=[old],
    )

    assert plan.job_id == stable_job_id({"aweme_id": "private-migration-id"})
    assert correct.exists()
    assert old.exists()
    assert not (tmp_path / "data" / "jobs").exists()
    assert not (tmp_path / "archive").exists()

    sensitive = tmp_path / "config" / "cookies.json"
    sensitive.parent.mkdir()
    sensitive.write_text("must-not-be-read", encoding="utf-8")
    with pytest.raises(MigrationError) as error:
        build_plan(
            tmp_path,
            aweme_id="x",
            correct_source=sensitive,
            correct_analysis=None,
            archive_old=[],
        )
    assert error.value.code == "sensitive_path_blocked"
