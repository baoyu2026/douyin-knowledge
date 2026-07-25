from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from app.structured_content import render_structured_markdown
from douyin_knowledge.publication import begin_publication
from douyin_knowledge.review import approved_candidate
from tests.test_library_workflow import make_job, register_collection_item
from tests.test_public_cli import invoke
from tests.test_structured_content import _payload

JOB_REF = "aweme-0123456789abcdefabcd"


def _database(root: Path) -> Path:
    database = root / "data" / "knowledge.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE collection_items("
            "source_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL, "
            "currently_collected INTEGER NOT NULL, last_position INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO collection_items VALUES ('private-source', ?, 'analyzed', 1, 1)",
            (JOB_REF,),
        )
    return database


def test_mutating_platform_commands_require_explicit_confirmation(
    tmp_path: Path, capsys
) -> None:
    _database(tmp_path)
    for arguments in (
        ["login", "--json"],
        ["sync", "--json"],
        ["model", "install", "--name", "small", "--json"],
        ["publish", "--job-ref", JOB_REF, "--json"],
        ["run", "--job-ref", JOB_REF, "--stop-after", "analysis", "--json"],
    ):
        code, payload = invoke(["--root", str(tmp_path), *arguments], capsys)
        assert code == 2
        assert payload["error"]["code"] == "confirmation_required"
        assert payload["error"]["preserved_checkpoint"] is True


def test_review_record_and_list_never_return_private_notes(tmp_path: Path, capsys) -> None:
    _database(tmp_path)
    candidate = tmp_path / "data" / "tasks" / JOB_REF / "semantic-v1" / "candidate-v1.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("{}", encoding="utf-8")

    code, recorded = invoke(
        [
            "--root",
            str(tmp_path),
            "review",
            "record",
            "--job-ref",
            JOB_REF,
            "--decision",
            "approve",
            "--note",
            "private reviewer note",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert recorded["data"] == {"job_ref": JOB_REF, "decision": "approve", "reused": False}

    code, listed = invoke(
        ["--root", str(tmp_path), "review", "list", "--json"], capsys
    )
    assert code == 0
    assert listed["data"]["items"] == [
        {"job_ref": JOB_REF, "decision": "approve"}
    ]
    assert "private reviewer note" not in str(listed)

    code, status = invoke(["--root", str(tmp_path), "status", "--json"], capsys)
    assert code == 0
    assert status["data"]["pending_review"] == 0

    for decision in ("reject", "approve"):
        code, _recorded_again = invoke(
            [
                "--root",
                str(tmp_path),
                "review",
                "record",
                "--job-ref",
                JOB_REF,
                "--decision",
                decision,
                "--note",
                "private reviewer note",
                "--json",
            ],
            capsys,
        )
        assert code == 0
    assert approved_candidate(tmp_path, tmp_path / "data" / "knowledge.db", JOB_REF)


def test_reconcile_cli_reports_relative_publication_state(tmp_path: Path, capsys) -> None:
    database = _database(tmp_path)
    content = b"published"
    target = tmp_path / "library" / "note.md"
    target.parent.mkdir(parents=True)
    begin_publication(
        tmp_path,
        database,
        job_ref=JOB_REF,
        idempotency_key="one",
        draft_sha256="1" * 64,
        media_sha256="2" * 64,
        targets={"library": ("library/note.md", hashlib.sha256(content).hexdigest())},
    )
    target.write_bytes(content)

    code, payload = invoke(
        ["--root", str(tmp_path), "reconcile", "--job-ref", JOB_REF, "--json"],
        capsys,
    )

    assert code == 0
    assert payload["data"]["items"][0]["state"] == "published_unaccepted"
    assert payload["data"]["items"][0]["targets"] == {"library": "verified"}


def test_status_counts_publication_journal_states(tmp_path: Path, capsys) -> None:
    database = _database(tmp_path)
    begin_publication(
        tmp_path,
        database,
        job_ref=JOB_REF,
        idempotency_key="one",
        draft_sha256="1" * 64,
        media_sha256="2" * 64,
        targets={"library": ("library/missing.md", "3" * 64)},
    )

    code, payload = invoke(["--root", str(tmp_path), "status", "--json"], capsys)

    assert code == 0
    assert payload["data"]["publication"] == {
        "intent": 1,
        "published_unaccepted": 0,
        "accepted": 0,
    }


def test_confirmed_publish_requires_review_and_accepts_verified_targets(
    tmp_path: Path, capsys
) -> None:
    source_id = "private-publication-source"
    register_collection_item(tmp_path, source_id)
    job_ref = make_job(tmp_path, aweme_id=source_id)
    related = tmp_path / "library" / "参考分类" / "已有参考知识"
    related.mkdir(parents=True)
    (related / "内容整理.md").write_text("# 已有参考知识\n", encoding="utf-8")
    frames = sorted((tmp_path / "data" / "jobs" / job_ref / "analysis" / "keyframes").glob("*"))
    draft = tmp_path / "orchestration" / "content-drafts" / f"{job_ref}-content.md"
    draft.parent.mkdir(parents=True)
    payload = _payload()
    payload["primary_category"] = "AI 工具与智能体"
    draft.write_text(
        render_structured_markdown(
            tmp_path,
            job_ref,
            payload,
            catalog={"已有参考知识": related / "内容整理.md"},
            frames=frames,
        ),
        encoding="utf-8",
    )
    candidate = tmp_path / "data" / "tasks" / job_ref / "semantic-v1" / "candidate-v1.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("{}\n", encoding="utf-8")
    vault = tmp_path / "external-vault"
    (vault / ".obsidian").mkdir(parents=True)
    config = tmp_path / "config"
    config.mkdir()
    (config / "obsidian.yml").write_text(f"vault: '{vault.as_posix()}'\n", encoding="utf-8")

    code, _review = invoke(
        [
            "--root",
            str(tmp_path),
            "review",
            "record",
            "--job-ref",
            job_ref,
            "--decision",
            "approve",
            "--json",
        ],
        capsys,
    )
    assert code == 0

    code, published = invoke(
        [
            "--root",
            str(tmp_path),
            "publish",
            "--job-ref",
            job_ref,
            "--confirm",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    assert published["data"]["state"] == "accepted"
    assert published["data"]["targets"] == {"library": "verified", "vault": "verified"}
    with sqlite3.connect(tmp_path / "data" / "knowledge.db") as connection:
        assert connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (job_ref,)
        ).fetchone()[0] == "completed"
