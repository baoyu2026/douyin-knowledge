from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

import douyin_knowledge.results_migration as results_migration
from douyin_knowledge.cli import main
from douyin_knowledge.result_archive import ResultsConfigError, configure_results_root
from douyin_knowledge.results_migration import (
    ResultsMigrationError,
    cleanup_legacy_results,
    inspect_legacy_results,
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


def _registry_by_media(
    instance: Path,
    entry_ref: str,
    source: Path,
    *,
    duplicate_entry_ref: str | None = None,
) -> Path:
    database = instance / "data" / "knowledge.db"
    database.parent.mkdir(parents=True)
    media_digest = hashlib.sha256((source / "原视频.mp4").read_bytes()).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE collection_items("
            "job_id TEXT PRIMARY KEY, library_path TEXT, media_sha256 TEXT)"
        )
        connection.execute(
            "INSERT INTO collection_items VALUES (?, NULL, ?)",
            (entry_ref, media_digest),
        )
        if duplicate_entry_ref is not None:
            connection.execute(
                "INSERT INTO collection_items VALUES (?, NULL, ?)",
                (duplicate_entry_ref, media_digest),
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
        "selected": 1,
        "duplicates_skipped": 0,
        "copied": 1,
        "reused": 0,
        "verified": 1,
        "registered": 1,
        "manifests_generated": 0,
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


def test_migration_inspection_returns_only_issue_counts(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    title = "不应出现在输出中的标题"
    incomplete = instance / "library" / "AI 工具" / title
    incomplete.mkdir(parents=True)
    (incomplete / "原视频.mp4").write_bytes(b"video")

    result = inspect_legacy_results(instance)

    assert result["discovered"] == 1
    assert result["complete"] == 0
    assert result["incomplete"] == 1
    assert result["repairable"] == 0
    assert result["blocked"] == 1
    assert result["duplicates_skipped"] == 0
    assert result["migration_ready"] is False
    assert result["issues"] == {
        "missing_entry_manifest": 1,
        "missing_keyframes_directory": 1,
        "missing_knowledge_note": 1,
        "missing_timeline": 1,
    }
    assert title not in json.dumps(result, ensure_ascii=False)


def test_missing_legacy_manifest_is_repaired_only_in_copy_and_reused(
    tmp_path: Path,
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    source = _legacy_entry(instance, entry_ref=entry_ref)
    (source / "资料信息.yml").unlink()
    database = _registry(instance, entry_ref, source)
    destination = tmp_path / "results"
    configure_results_root(instance, destination)

    inspection = inspect_legacy_results(instance)

    assert inspection == {
        "discovered": 1,
        "complete": 1,
        "incomplete": 0,
        "repairable": 1,
        "blocked": 0,
        "duplicates_skipped": 0,
        "migration_ready": True,
        "issues": {"repairable_entry_manifest": 1},
    }

    first = migrate_legacy_results(instance)

    target = destination / "AI 工具" / "清晰标题"
    assert first["copied"] == 1
    assert first["manifests_generated"] == 1
    assert not (source / "资料信息.yml").exists()
    manifest = yaml.safe_load((target / "资料信息.yml").read_text(encoding="utf-8"))
    assert manifest["entry_ref"] == entry_ref
    assert manifest["migrated_from_legacy"] is True
    state = json.loads(
        (instance / "data" / "migrations" / "results-v1.json").read_text(
            encoding="utf-8"
        )
    )
    record = state["entries"][0]
    assert record["entry_ref"] == entry_ref
    assert record["source_sha256"] != record["sha256"]
    with sqlite3.connect(database) as connection:
        assert Path(
            connection.execute(
                "SELECT library_path FROM collection_items WHERE job_id = ?", (entry_ref,)
            ).fetchone()[0]
        ) == target

    second = migrate_legacy_results(instance)

    assert second["copied"] == 0
    assert second["reused"] == 1
    assert second["manifests_generated"] == 1
    assert not (destination / "AI 工具" / "清晰标题 (2)").exists()
    assert not (source / "资料信息.yml").exists()


def test_missing_legacy_manifest_without_unique_registry_is_blocked(
    tmp_path: Path,
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    source = _legacy_entry(instance)
    (source / "资料信息.yml").unlink()
    configure_results_root(instance, tmp_path / "results")

    inspection = inspect_legacy_results(instance)

    assert inspection["migration_ready"] is False
    assert inspection["repairable"] == 0
    assert inspection["blocked"] == 1
    assert inspection["issues"] == {"missing_entry_manifest": 1}
    with pytest.raises(ResultsMigrationError) as error:
        migrate_legacy_results(instance)
    assert error.value.code == "results_migration_manifest_invalid"


def test_missing_manifest_uses_unique_registered_media_fingerprint(
    tmp_path: Path,
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    source = _legacy_entry(instance, entry_ref=entry_ref)
    (source / "资料信息.yml").unlink()
    database = _registry_by_media(instance, entry_ref, source)
    destination = tmp_path / "results"
    configure_results_root(instance, destination)

    inspection = inspect_legacy_results(instance)
    result = migrate_legacy_results(instance)

    target = destination / "AI 工具" / "清晰标题"
    assert inspection["migration_ready"] is True
    assert inspection["repairable"] == 1
    assert result["manifests_generated"] == 1
    with sqlite3.connect(database) as connection:
        registered = connection.execute(
            "SELECT library_path FROM collection_items WHERE job_id = ?", (entry_ref,)
        ).fetchone()[0]
    assert Path(registered) == target
    assert not (source / "资料信息.yml").exists()


def test_duplicate_registered_media_fingerprint_does_not_guess_entry_ref(
    tmp_path: Path,
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    source = _legacy_entry(instance)
    (source / "资料信息.yml").unlink()
    _registry_by_media(
        instance,
        "aweme-0123456789abcdefabcd",
        source,
        duplicate_entry_ref="aweme-fedcba9876543210abcd",
    )
    configure_results_root(instance, tmp_path / "results")

    inspection = inspect_legacy_results(instance)

    assert inspection["migration_ready"] is False
    assert inspection["blocked"] == 1
    with pytest.raises(ResultsMigrationError) as error:
        migrate_legacy_results(instance)
    assert error.value.code == "results_migration_manifest_invalid"


def test_registered_path_wins_over_stale_media_duplicate(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    canonical = _legacy_entry(instance, entry_ref=entry_ref, title="权威成果")
    stale = _legacy_entry(instance, entry_ref=entry_ref, title="旧副本")
    (canonical / "资料信息.yml").unlink()
    (stale / "资料信息.yml").unlink()
    database = instance / "data" / "knowledge.db"
    database.parent.mkdir(parents=True)
    media_digest = hashlib.sha256((canonical / "原视频.mp4").read_bytes()).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE collection_items("
            "job_id TEXT PRIMARY KEY, library_path TEXT, media_sha256 TEXT)"
        )
        connection.execute(
            "INSERT INTO collection_items VALUES (?, ?, ?)",
            (entry_ref, str(canonical), media_digest),
        )
    destination = tmp_path / "results"
    configure_results_root(instance, destination)

    inspection = inspect_legacy_results(instance)
    first = migrate_legacy_results(instance)
    post_migration_inspection = inspect_legacy_results(instance)
    second = migrate_legacy_results(instance)

    assert inspection["migration_ready"] is True
    assert inspection["discovered"] == 2
    assert inspection["duplicates_skipped"] == 1
    assert first["discovered"] == 2
    assert first["selected"] == 1
    assert first["duplicates_skipped"] == 1
    assert first["copied"] == 1
    assert post_migration_inspection["migration_ready"] is True
    assert post_migration_inspection["duplicates_skipped"] == 1
    assert second["reused"] == 1
    assert second["duplicates_skipped"] == 1
    assert (destination / "AI 工具" / "权威成果").is_dir()
    assert not (destination / "AI 工具" / "旧副本").exists()
    assert stale.is_dir()

    cleaned = cleanup_legacy_results(instance)

    assert cleaned["deleted"] == 2
    assert cleaned["verified"] == 1
    assert cleaned["duplicates_deleted"] == 1
    assert not (instance / "library").exists()


def test_media_duplicates_without_authoritative_source_are_blocked(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    first = _legacy_entry(instance, entry_ref=entry_ref, title="副本一")
    second = _legacy_entry(instance, entry_ref=entry_ref, title="副本二")
    (first / "资料信息.yml").unlink()
    (second / "资料信息.yml").unlink()
    _registry_by_media(instance, entry_ref, first)
    configure_results_root(instance, tmp_path / "results")

    inspection = inspect_legacy_results(instance)

    assert inspection["migration_ready"] is False
    assert inspection["blocked"] == 2
    assert inspection["issues"]["duplicate_reference_conflict"] == 2
    with pytest.raises(ResultsMigrationError) as error:
        migrate_legacy_results(instance)
    assert error.value.code == "results_migration_duplicate_reference"


def test_legacy_checkpoint_without_source_digest_remains_supported(
    tmp_path: Path,
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    _legacy_entry(instance)
    destination = tmp_path / "results"
    configure_results_root(instance, destination)
    migrate_legacy_results(instance)
    state_path = instance / "data" / "migrations" / "results-v1.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["entries"][0].pop("source_sha256")
    state["entries"][0].pop("entry_ref")
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    result = migrate_legacy_results(instance)

    assert result["reused"] == 1


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


def test_migrate_inspect_cli_is_read_only_and_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    title = "私有标题"
    incomplete = instance / "library" / "分类" / title
    incomplete.mkdir(parents=True)

    assert main(["--root", str(instance), "migrate", "inspect", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "migrate_inspect"
    assert payload["data"]["incomplete"] == 1
    serialized = json.dumps(payload, ensure_ascii=False)
    assert title not in serialized
    assert str(instance) not in serialized


def test_cleanup_removes_only_verified_legacy_results_and_is_idempotent(
    tmp_path: Path,
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    source = _legacy_entry(instance, entry_ref=entry_ref)
    database = _registry(instance, entry_ref, source)
    destination = tmp_path / "results"
    configure_results_root(instance, destination)
    migrate_legacy_results(instance)

    first = cleanup_legacy_results(instance)
    second = cleanup_legacy_results(instance)

    assert first == {
        "status": "cleaned",
        "deleted": 1,
        "verified": 1,
        "duplicates_deleted": 0,
        "source_removed": True,
        "results_preserved": True,
        "publication_history_preserved": True,
        "state_handle": "data/migrations/results-cleanup-v1.json",
    }
    assert second == first
    assert not (instance / "library").exists()
    assert (destination / "AI 工具" / "清晰标题" / "内容整理.md").is_file()
    assert (instance / "data" / "migrations" / "results-v1.json").is_file()
    assert inspect_legacy_results(instance)["discovered"] == 0
    with sqlite3.connect(database) as connection:
        registered = connection.execute(
            "SELECT library_path FROM collection_items WHERE job_id = ?", (entry_ref,)
        ).fetchone()[0]
    assert Path(registered) == destination / "AI 工具" / "清晰标题"


def test_cleanup_refuses_unknown_legacy_files(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    source = _legacy_entry(instance, entry_ref=entry_ref)
    _registry(instance, entry_ref, source)
    destination = tmp_path / "results"
    configure_results_root(instance, destination)
    migrate_legacy_results(instance)
    (instance / "library" / "unknown.bin").write_bytes(b"do not delete")

    with pytest.raises(ResultsMigrationError) as error:
        cleanup_legacy_results(instance)

    assert error.value.code == "results_cleanup_scope_unsafe"
    assert (instance / "library" / "unknown.bin").is_file()
    assert (destination / "AI 工具" / "清晰标题").is_dir()


def test_cleanup_refuses_a_changed_migrated_target(tmp_path: Path) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    source = _legacy_entry(instance, entry_ref=entry_ref)
    _registry(instance, entry_ref, source)
    destination = tmp_path / "results"
    configure_results_root(instance, destination)
    migrate_legacy_results(instance)
    (destination / "AI 工具" / "清晰标题" / "内容整理.md").write_text(
        "changed", encoding="utf-8"
    )

    with pytest.raises(ResultsMigrationError) as error:
        cleanup_legacy_results(instance)

    assert error.value.code == "results_cleanup_target_unverified"
    assert (instance / "library").is_dir()


def test_cleanup_resumes_after_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    source = _legacy_entry(instance, entry_ref=entry_ref)
    _registry(instance, entry_ref, source)
    configure_results_root(instance, tmp_path / "results")
    migrate_legacy_results(instance)
    real_rmtree = results_migration.shutil.rmtree

    def fail_delete(_path: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(results_migration.shutil, "rmtree", fail_delete)
    with pytest.raises(ResultsMigrationError) as error:
        cleanup_legacy_results(instance)
    assert error.value.code == "results_cleanup_delete_failed"
    assert not (instance / "library").exists()

    monkeypatch.setattr(results_migration.shutil, "rmtree", real_rmtree)
    resumed = cleanup_legacy_results(instance)

    assert resumed["source_removed"] is True
    assert resumed["deleted"] == 1


def test_cleanup_cli_requires_confirmation_and_returns_safe_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = tmp_path / "private"
    _initialized(instance)
    entry_ref = "aweme-0123456789abcdefabcd"
    source = _legacy_entry(instance, entry_ref=entry_ref)
    _registry(instance, entry_ref, source)
    configure_results_root(instance, tmp_path / "results")
    migrate_legacy_results(instance)

    assert main(["--root", str(instance), "migrate", "cleanup", "--json"]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["error"]["code"] == "confirmation_required"

    assert (
        main(
            [
                "--root",
                str(instance),
                "migrate",
                "cleanup",
                "--confirm",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "migrate_cleanup"
    assert payload["data"]["deleted"] == 1
    serialized = json.dumps(payload, ensure_ascii=False)
    assert str(instance) not in serialized
    assert str(tmp_path / "results") not in serialized
