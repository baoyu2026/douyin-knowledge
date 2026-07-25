from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from douyin_knowledge.publication import (
    PublicationStateError,
    accept_publication,
    begin_publication,
    publication_status,
    reconcile_publications,
    seal_publication_targets,
)

JOB_REF = "aweme-0123456789abcdefabcd"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def database(root: Path) -> Path:
    path = root / "data" / "knowledge.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE collection_items(job_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO collection_items(job_id, status) VALUES (?, 'analyzed')",
            (JOB_REF,),
        )
    return path


def test_publication_journal_reconciles_files_before_acceptance(tmp_path: Path) -> None:
    db = database(tmp_path)
    library_bytes = b"library-note-v1"
    vault_bytes = b"vault-note-v1"
    saga = begin_publication(
        tmp_path,
        db,
        job_ref=JOB_REF,
        idempotency_key="publish-one",
        draft_sha256="1" * 64,
        media_sha256="2" * 64,
        targets={
            "library": ("library/topic/note.md", digest(library_bytes)),
            "vault": ("vault/topic/note.md", digest(vault_bytes)),
        },
    )
    assert saga["state"] == "intent"

    library = tmp_path / "library" / "topic" / "note.md"
    library.parent.mkdir(parents=True)
    library.write_bytes(library_bytes)
    first = reconcile_publications(tmp_path, db, job_ref=JOB_REF)
    assert first[0]["state"] == "intent"
    assert first[0]["targets"] == {"library": "verified", "vault": "missing"}

    vault = tmp_path / "vault" / "topic" / "note.md"
    vault.parent.mkdir(parents=True)
    vault.write_bytes(vault_bytes)
    second = reconcile_publications(tmp_path, db, job_ref=JOB_REF)
    assert second[0]["state"] == "published_unaccepted"
    with sqlite3.connect(db) as connection:
        status = connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (JOB_REF,)
        ).fetchone()[0]
    assert status == "analyzed"

    accepted = accept_publication(
        db,
        saga["saga_id"],
        checks={"sqlite_integrity": True, "privacy": True, "content": True},
    )
    assert accepted["state"] == "accepted"
    with sqlite3.connect(db) as connection:
        status = connection.execute(
            "SELECT status FROM collection_items WHERE job_id = ?", (JOB_REF,)
        ).fetchone()[0]
    assert status == "completed"


def test_publication_idempotency_rejects_same_key_with_different_request(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    arguments = {
        "job_ref": JOB_REF,
        "idempotency_key": "publish-one",
        "draft_sha256": "1" * 64,
        "media_sha256": "2" * 64,
        "targets": {"library": ("library/note.md", "3" * 64)},
    }
    first = begin_publication(tmp_path, db, **arguments)
    second = begin_publication(tmp_path, db, **arguments)
    assert first["saga_id"] == second["saga_id"]
    assert second["reused"] is True

    changed = dict(arguments)
    changed["draft_sha256"] = "4" * 64
    with pytest.raises(PublicationStateError) as error:
        begin_publication(tmp_path, db, **changed)
    assert error.value.code == "idempotency_conflict"


def test_publication_intent_requires_existing_registry_job(tmp_path: Path) -> None:
    db = database(tmp_path)
    with pytest.raises(PublicationStateError) as error:
        begin_publication(
            tmp_path,
            db,
            job_ref="aweme-ffffffffffffffffffff",
            idempotency_key="orphan",
            draft_sha256="1" * 64,
            media_sha256="2" * 64,
            targets={"library": ("library/note.md", "3" * 64)},
        )
    assert error.value.code == "registry_item_missing"


def test_acceptance_requires_verified_targets_and_all_checks(tmp_path: Path) -> None:
    db = database(tmp_path)
    saga = begin_publication(
        tmp_path,
        db,
        job_ref=JOB_REF,
        idempotency_key="publish-one",
        draft_sha256="1" * 64,
        media_sha256="2" * 64,
        targets={"library": ("library/missing.md", "3" * 64)},
    )

    with pytest.raises(PublicationStateError) as missing:
        accept_publication(db, saga["saga_id"], checks={"privacy": True})
    assert missing.value.code == "publication_not_observed"
    assert publication_status(db, JOB_REF)["state"] == "intent"


def test_acceptance_rejects_any_reported_failed_check(tmp_path: Path) -> None:
    db = database(tmp_path)
    content = b"verified"
    saga = begin_publication(
        tmp_path,
        db,
        job_ref=JOB_REF,
        idempotency_key="failed-extra-check",
        draft_sha256="1" * 64,
        media_sha256="2" * 64,
        targets={"library": ("library/note.md", digest(content))},
    )
    note = tmp_path / "library" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_bytes(content)
    reconcile_publications(tmp_path, db, job_ref=JOB_REF)

    with pytest.raises(PublicationStateError) as error:
        accept_publication(
            db,
            saga["saga_id"],
            checks={
                "sqlite_integrity": True,
                "privacy": True,
                "content": True,
                "vault_timeline": False,
            },
        )
    assert error.value.code == "acceptance_checks_failed"


def test_publication_intent_can_be_sealed_after_idempotent_writes(tmp_path: Path) -> None:
    db = database(tmp_path)
    saga = begin_publication(
        tmp_path,
        db,
        job_ref=JOB_REF,
        idempotency_key="planned-write",
        draft_sha256="1" * 64,
        media_sha256="2" * 64,
        targets={"library": ("library/planned.md", None)},
    )
    assert saga["targets"] == {"library": "planned"}

    note = tmp_path / "library" / "planned.md"
    note.parent.mkdir(parents=True)
    note.write_bytes(b"planned publication")
    sealed = seal_publication_targets(
        db,
        saga["saga_id"],
        expected={"library": digest(b"planned publication")},
    )
    assert sealed["targets"] == {"library": "pending"}
    assert reconcile_publications(tmp_path, db, job_ref=JOB_REF)[0]["state"] == (
        "published_unaccepted"
    )


def test_unverified_intent_can_retarget_and_reseal_same_publication(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    arguments = {
        "job_ref": JOB_REF,
        "idempotency_key": "retarget-planned-write",
        "draft_sha256": "1" * 64,
        "media_sha256": "2" * 64,
        "targets": {"vault": ("vault/topic with spaces/note.md", None)},
    }
    saga = begin_publication(tmp_path, db, **arguments)
    seal_publication_targets(
        db,
        saga["saga_id"],
        expected={"vault": digest(b"first-write")},
    )
    reconcile_publications(tmp_path, db, job_ref=JOB_REF)

    changed = dict(arguments)
    changed["targets"] = {"vault": ("vault/topic-with-spaces/note.md", None)}
    reused = begin_publication(tmp_path, db, **changed)
    assert reused["saga_id"] == saga["saga_id"]
    assert reused["reused"] is True

    note = tmp_path / "vault" / "topic-with-spaces" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_bytes(b"retry-write")
    seal_publication_targets(
        db,
        saga["saga_id"],
        expected={"vault": digest(b"retry-write")},
    )
    reconciled = reconcile_publications(tmp_path, db, job_ref=JOB_REF)
    assert reconciled[0]["state"] == "published_unaccepted"
    assert reconciled[0]["targets"] == {"vault": "verified"}


def test_verified_intent_target_cannot_be_retargeted(tmp_path: Path) -> None:
    db = database(tmp_path)
    content = b"verified-target"
    arguments = {
        "job_ref": JOB_REF,
        "idempotency_key": "verified-target",
        "draft_sha256": "1" * 64,
        "media_sha256": "2" * 64,
        "targets": {
            "library": ("library/note.md", digest(content)),
            "vault": ("vault/missing.md", None),
        },
    }
    begin_publication(tmp_path, db, **arguments)
    note = tmp_path / "library" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_bytes(content)
    reconcile_publications(tmp_path, db, job_ref=JOB_REF)

    changed = dict(arguments)
    changed["targets"] = {
        "library": ("library/other.md", None),
        "vault": ("vault/missing.md", None),
    }
    with pytest.raises(PublicationStateError) as error:
        begin_publication(tmp_path, db, **changed)
    assert error.value.code == "idempotency_conflict"
