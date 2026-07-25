from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from douyin_knowledge.result_archive import (
    ResultsConfigError,
    configure_results_root,
    logical_library_handle,
    resolve_logical_library_handle,
    resolve_results_handle,
    results_handle,
)


def _initialized(root: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True)
    (config / "config.yml").write_text("version: 1\n", encoding="utf-8")


def test_external_results_handles_do_not_expose_the_absolute_root(tmp_path: Path) -> None:
    instance = tmp_path / "private-instance"
    _initialized(instance)
    archive = tmp_path / "给人看的成果"
    configure_results_root(instance, archive)
    note = archive / "软件工程" / "清晰标题" / "内容整理.md"

    assert results_handle(instance, note) == "results/软件工程/清晰标题/内容整理.md"
    assert resolve_results_handle(
        instance, "results/软件工程/清晰标题/内容整理.md"
    ) == note.resolve()
    assert logical_library_handle(instance, note) == (
        "library/软件工程/清晰标题/内容整理.md"
    )
    assert resolve_logical_library_handle(
        instance, "library/软件工程/清晰标题/内容整理.md"
    ) == note.resolve()


@pytest.mark.parametrize("relative", [Path("relative-results"), Path("data/results")])
def test_results_root_rejects_ambiguous_or_private_locations(
    tmp_path: Path, relative: Path
) -> None:
    instance = tmp_path / "private-instance"
    _initialized(instance)
    target = (
        relative
        if not relative.is_absolute() and len(relative.parts) == 1
        else instance / relative
    )

    with pytest.raises(ResultsConfigError) as error:
        configure_results_root(instance, target)

    assert error.value.code in {"results_root_absolute_required", "results_root_unsafe"}


def test_results_root_is_locked_after_a_journaled_result(tmp_path: Path) -> None:
    instance = tmp_path / "private-instance"
    _initialized(instance)
    first = tmp_path / "first-results"
    configure_results_root(instance, first)
    database = instance / "data" / "knowledge.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE publication_targets(relative_handle TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO publication_targets VALUES (?)",
            ("results/分类/标题/内容整理.md",),
        )

    same = configure_results_root(instance, first)
    assert same["changed"] is False
    with pytest.raises(ResultsConfigError) as error:
        configure_results_root(instance, tmp_path / "second-results")

    assert error.value.code == "results_root_locked"
