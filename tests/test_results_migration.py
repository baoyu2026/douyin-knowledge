from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from douyin_knowledge.cli import main
from douyin_knowledge.result_archive import ResultsConfigError, configure_results_root
from douyin_knowledge.results_migration import (
    ResultsMigrationError,
    migrate_legacy_results,
)


def _initialized(instance: Path) -> None:
    config = instance / "config"
    config.mkdir(parents=True)
    (config / "config.yml").write_text("version: 1\n", encoding="utf-8")


def _legacy_entry(
    instance: Path,
    *,
    entry_ref: str = "aweme-0123456789abcdefabcd",
    category: str = "AI 工具",
    title: str = "清晰标题",
) -> Path:
    entry = instance / "library" / category / title
    (entry / "附件").mkdir(parents=True)
    (entry / "精选关键帧").mkdir()
    (entry / "内容整理.md").write_text(
        "---\n"
        f"标题: {title}\n"
        f"主分类: {category}\n"
        "标签:\n  - 测试\n"
        "新增时间: '2026-07-26T00:00:00+00:00'\n"
        "复核状态: 已复核\n"
        "---\n\n# 内容\n",
        encoding="utf-8",
    )
    (entry / "原视频.mp4").write_bytes(b"verified legacy video")
    (entry / "附件" / "完整时间轴.md").write_text("# 时间轴\n", encoding="utf-8")
    (entry / "精选关键帧" / "frame-001.jpg").write_bytes(b"frame")
    (entry / "资料信息.yml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entry_ref": entry_ref,
                "title": title,
                "category": category,
                "layout": "category-title-v1",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return entry


def _registry(instance: Path, entry_ref: str, source: Path) -> Path:
    database = instance / "data" / "knowledge.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE collection_items(job_id TEXT PRIMARY KEY, library_path TEXT)"
        )
        connection.execute(
            "INSERT INTO collection_items(job_id, library_path) VALUES (?, ?)",
            (entry_ref, str(source)),
        )
    return database


def test_migration_copies_verifies_indexes_registers_and_is_idempotent(
    tmp_path: Path,
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    source = _legacy_entry(instance, entry_ref=entry_ref)
    database = _registry(instance, entry_ref, source)
    destination = tmp_path / "人类成果库"
    configure_results_root(instance, destination)

    first = migrate_legacy_results(instance)

    target = destination / "AI 工具" / "清晰标题"
    assert first == {
        "status": "migrated",
        "discovered": 1,
        "copied": 1,
        "reused": 0,
        "verified": 1,
        "registered": 1,
        "source_preserved": True,
        "index_rebuilt": True,
        "state_handle": "data/migrations/results-v1.json",
    }
    assert source.is_dir()
    assert (target / "原视频.mp4").read_bytes() == b"verified legacy video"
    assert (destination / "00-总索引" / "全部视频.md").is_file()
    with sqlite3.connect(database) as connection:
        registered = connection.execute(
            "SELECT library_path FROM collection_items WHERE job_id = ?", (entry_ref,)
        ).fetchone()[0]
    assert Path(registered) == target
    state = json.loads(
        (instance / "data" / "migrations" / "results-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["complete"] is True
    assert len(state["entries"]) == 1

    second = migrate_legacy_results(instance)

    assert second["copied"] == 0
    assert second["reused"] == 1
    assert second["verified"] == 1


def test_migration_preserves_distinct_same_title_entries(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    _legacy_entry(instance, entry_ref="aweme-0123456789abcdefabcd")
    destination = tmp_path / "results"
    configure_results_root(instance, destination)
    occupied = _legacy_entry(
        tmp_path / "other",
        entry_ref="aweme-fedcba9876543210abcd",
    )
    target = destination / "AI 工具" / "清晰标题"
    target.parent.mkdir(parents=True)
    target.mkdir()
    for item in occupied.rglob("*"):
        relative = item.relative_to(occupied)
        output = target / relative
        if item.is_dir():
            output.mkdir(parents=True, exist_ok=True)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(item.read_bytes())

    result = migrate_legacy_results(instance)

    assert result["copied"] == 1
    assert (destination / "AI 工具" / "清晰标题 (2)" / "内容整理.md").is_file()


def test_migration_rejects_changed_existing_copy(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    _legacy_entry(instance)
    destination = tmp_path / "results"
    configure_results_root(instance, destination)
    migrate_legacy_results(instance)
    (destination / "AI 工具" / "清晰标题" / "内容整理.md").write_text(
        "changed", encoding="utf-8"
    )

    with pytest.raises(ResultsMigrationError) as error:
        migrate_legacy_results(instance)

    assert error.value.code == "results_migration_conflict"


def test_migration_rejects_an_incomplete_legacy_directory(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    incomplete = instance / "library" / "AI 工具" / "缺少笔记"
    incomplete.mkdir(parents=True)
    (incomplete / "原视频.mp4").write_bytes(b"video")
    configure_results_root(instance, tmp_path / "results")

    with pytest.raises(ResultsMigrationError) as error:
        migrate_legacy_results(instance)

    assert error.value.code == "results_migration_entry_incomplete"


def test_migration_rejects_duplicate_entry_references(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    _legacy_entry(instance, entry_ref=entry_ref, title="第一个标题")
    _legacy_entry(instance, entry_ref=entry_ref, title="第二个标题")
    configure_results_root(instance, tmp_path / "results")

    with pytest.raises(ResultsMigrationError) as error:
        migrate_legacy_results(instance)

    assert error.value.code == "results_migration_duplicate_reference"


def test_migration_rejects_a_legacy_entry_symlink(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    external = _legacy_entry(tmp_path / "external")
    category = instance / "library" / "AI 工具"
    category.mkdir(parents=True)
    try:
        (category / "外部链接").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    configure_results_root(instance, tmp_path / "results")

    with pytest.raises(ResultsMigrationError) as error:
        migrate_legacy_results(instance)

    assert error.value.code == "results_migration_symlink"


def test_migration_rejects_a_manifest_that_points_to_another_registered_job(
    tmp_path: Path,
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    source = _legacy_entry(instance, entry_ref=entry_ref)
    database = _registry(instance, entry_ref, tmp_path / "different-result")
    configure_results_root(instance, tmp_path / "results")

    with pytest.raises(ResultsMigrationError) as error:
        migrate_legacy_results(instance)

    assert error.value.code == "results_migration_registry_mismatch"
    with sqlite3.connect(database) as connection:
        registered = connection.execute(
            "SELECT library_path FROM collection_items WHERE job_id = ?", (entry_ref,)
        ).fetchone()[0]
    assert Path(registered) != source


def test_checkpoint_prevents_duplicate_when_migrated_manifest_is_corrupted(
    tmp_path: Path,
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    _legacy_entry(instance)
    destination = tmp_path / "results"
    configure_results_root(instance, destination)
    migrate_legacy_results(instance)
    target = destination / "AI 工具" / "清晰标题"
    (target / "资料信息.yml").write_text("invalid: [", encoding="utf-8")

    with pytest.raises(ResultsMigrationError) as error:
        migrate_legacy_results(instance)

    assert error.value.code == "results_migration_conflict"
    assert not (destination / "AI 工具" / "清晰标题 (2)").exists()


def test_completed_migration_locks_results_root(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    _legacy_entry(instance)
    first = tmp_path / "first-results"
    configure_results_root(instance, first)
    migrate_legacy_results(instance)

    with pytest.raises(ResultsConfigError) as error:
        configure_results_root(instance, tmp_path / "second-results")

    assert error.value.code == "results_root_locked"


def test_partial_migration_checkpoint_locks_results_root(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    first = tmp_path / "first-results"
    configure_results_root(instance, first)
    state = instance / "data" / "migrations" / "results-v1.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "complete": False,
                "entries": [
                    {
                        "source": "分类/标题",
                        "target": "分类/标题",
                        "sha256": "a" * 64,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ResultsConfigError) as error:
        configure_results_root(instance, tmp_path / "second-results")

    assert error.value.code == "results_root_locked"


def test_migration_does_not_change_status_or_publication_history(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    source = _legacy_entry(instance, entry_ref=entry_ref)
    database = instance / "data" / "knowledge.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE collection_items("
            "job_id TEXT PRIMARY KEY, library_path TEXT, status TEXT)"
        )
        connection.execute(
            "INSERT INTO collection_items VALUES (?, ?, 'completed')",
            (entry_ref, str(source)),
        )
        connection.execute(
            "CREATE TABLE publication_targets("
            "saga_id TEXT, target_name TEXT, relative_handle TEXT, expected_sha256 TEXT)"
        )
        connection.execute(
            "INSERT INTO publication_targets VALUES "
            "('pub-preserved', 'library', 'library/AI 工具/清晰标题/内容整理.md', ?)",
            ("b" * 64,),
        )
    configure_results_root(instance, tmp_path / "results")

    migrate_legacy_results(instance)

    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (entry_ref,)
        ).fetchone()[0]
        target = connection.execute(
            "SELECT saga_id, target_name, relative_handle, expected_sha256 "
            "FROM publication_targets"
        ).fetchone()
    assert status == "completed"
    assert target == (
        "pub-preserved",
        "library",
        "library/AI 工具/清晰标题/内容整理.md",
        "b" * 64,
    )


def test_migrate_results_cli_requires_confirmation_and_returns_safe_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    _legacy_entry(instance)
    configure_results_root(instance, tmp_path / "results")

    assert main(["--root", str(instance), "migrate", "results", "--json"]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["error"]["code"] == "confirmation_required"

    assert (
        main(
            [
                "--root",
                str(instance),
                "migrate",
                "results",
                "--confirm",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "migrate_results"
    assert payload["data"]["copied"] == 1
    assert str(instance) not in json.dumps(payload, ensure_ascii=False)
    assert str(tmp_path / "results") not in json.dumps(payload, ensure_ascii=False)
