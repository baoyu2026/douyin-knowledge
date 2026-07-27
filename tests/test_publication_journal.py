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


def accept_target(
    root: Path,
    db: Path,
    *,
    key: str,
    content: bytes,
    handle: str = "library/topic/note.md",
) -> dict[str, object]:
    saga = begin_publication(
        root,
        db,
        job_ref=JOB_REF,
        idempotency_key=key,
        draft_sha256=digest(f"draft-{key}".encode()),
        media_sha256="2" * 64,
        targets={"library": (handle, digest(content))},
    )
    target = root / Path(handle)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    current = next(
        item
        for item in reconcile_publications(root, db, job_ref=JOB_REF)
        if item["saga_id"] == saga["saga_id"]
    )
    assert current["state"] == "published_unaccepted"
    return accept_publication(
        db,
        str(saga["saga_id"]),
        checks={"sqlite_integrity": True, "privacy": True, "content": True},
    )


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


def test_republication_supersedes_prior_acceptance_without_mutating_its_evidence(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    first = accept_target(tmp_path, db, key="first", content=b"first version")

    second_saga = begin_publication(
        tmp_path,
        db,
        job_ref=JOB_REF,
        idempotency_key="correction",
        draft_sha256="4" * 64,
        media_sha256="2" * 64,
        targets={"library": ("library/topic/note.md", digest(b"corrected version"))},
    )
    target = tmp_path / "library" / "topic" / "note.md"
    target.write_bytes(b"corrected version")

    before_acceptance = reconcile_publications(tmp_path, db, job_ref=JOB_REF)
    assert before_acceptance[0]["state"] == "accepted"
    assert before_acceptance[0]["targets"] == {"library": "verified"}
    assert before_acceptance[1]["state"] == "published_unaccepted"

    second = accept_publication(
        db,
        second_saga["saga_id"],
        checks={"sqlite_integrity": True, "privacy": True, "content": True},
    )
    assert second["state"] == "accepted"
    assert publication_status(db, JOB_REF)["saga_id"] == second["saga_id"]

    history = reconcile_publications(tmp_path, db, job_ref=JOB_REF)
    assert history[0]["saga_id"] == first["saga_id"]
    assert history[0]["state"] == "superseded"
    assert history[0]["targets"] == {"library": "verified"}
    assert history[1]["state"] == "accepted"
    resealed = seal_publication_targets(
        db,
        str(first["saga_id"]),
        expected={"library": digest(b"first version")},
    )
    assert resealed["state"] == "superseded"
    assert resealed["targets"] == {"library": "verified"}
    assert resealed["reused"] is True
    with sqlite3.connect(db) as connection:
        old = connection.execute(
            "SELECT superseded_at, superseded_by_saga_id FROM publication_sagas "
            "WHERE saga_id = ?",
            (first["saga_id"],),
        ).fetchone()
        assert old is not None and old[0] is not None
        assert old[1] == second["saga_id"]
        assert connection.execute(
            "SELECT COUNT(*) FROM publication_acceptances WHERE saga_id IN (?, ?)",
            (first["saga_id"], second["saga_id"]),
        ).fetchone()[0] == 2


def test_reconcile_reports_current_drift_without_reclassifying_superseded_history(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    first = accept_target(tmp_path, db, key="first", content=b"first version")
    second = accept_target(tmp_path, db, key="second", content=b"second version")
    (tmp_path / "library" / "topic" / "note.md").unlink()

    reconciled = reconcile_publications(tmp_path, db, job_ref=JOB_REF)

    assert reconciled[0]["saga_id"] == first["saga_id"]
    assert reconciled[0]["state"] == "superseded"
    assert reconciled[0]["targets"] == {"library": "verified"}
    assert reconciled[1]["saga_id"] == second["saga_id"]
    assert reconciled[1]["state"] == "accepted"
    assert reconciled[1]["targets"] == {"library": "missing"}


def test_delayed_older_transaction_cannot_supersede_a_newer_acceptance(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    target = tmp_path / "library" / "topic" / "note.md"
    target.parent.mkdir(parents=True)
    older = begin_publication(
        tmp_path,
        db,
        job_ref=JOB_REF,
        idempotency_key="older-delayed",
        draft_sha256="1" * 64,
        media_sha256="2" * 64,
        targets={"library": ("library/topic/note.md", digest(b"older"))},
    )
    target.write_bytes(b"older")
    assert reconcile_publications(tmp_path, db, job_ref=JOB_REF)[0]["state"] == (
        "published_unaccepted"
    )

    newer = begin_publication(
        tmp_path,
        db,
        job_ref=JOB_REF,
        idempotency_key="newer-accepted",
        draft_sha256="3" * 64,
        media_sha256="2" * 64,
        targets={"library": ("library/topic/note.md", digest(b"newer"))},
    )
    target.write_bytes(b"newer")
    current = reconcile_publications(tmp_path, db, job_ref=JOB_REF)
    assert current[1]["state"] == "published_unaccepted"
    accepted = accept_publication(
        db,
        str(newer["saga_id"]),
        checks={"sqlite_integrity": True, "privacy": True, "content": True},
    )
    assert accepted["state"] == "accepted"

    with pytest.raises(PublicationStateError) as overtaken:
        accept_publication(
            db,
            str(older["saga_id"]),
            checks={"sqlite_integrity": True, "privacy": True, "content": True},
        )
    assert overtaken.value.code == "publication_overtaken"
    assert publication_status(db, JOB_REF)["saga_id"] == newer["saga_id"]


def test_existing_publication_database_is_upgraded_and_history_is_backfilled(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE publication_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE publication_sagas (
                saga_id TEXT PRIMARY KEY,
                job_ref TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL,
                draft_sha256 TEXT NOT NULL,
                media_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('intent', 'published_unaccepted', 'accepted')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE publication_targets (
                saga_id TEXT NOT NULL,
                target_name TEXT NOT NULL,
                relative_handle TEXT NOT NULL,
                expected_sha256 TEXT NOT NULL,
                observed_sha256 TEXT,
                status TEXT NOT NULL,
                observed_at TEXT,
                PRIMARY KEY (saga_id, target_name)
            );
            CREATE TABLE publication_acceptances (
                saga_id TEXT PRIMARY KEY,
                checks_json TEXT NOT NULL,
                accepted_at TEXT NOT NULL
            );
            INSERT INTO publication_schema_migrations VALUES (1, '2026-01-01');
            """
        )
        for index, saga_id in enumerate(("pub-old", "pub-current"), start=1):
            timestamp = f"2026-01-0{index}T00:00:00+00:00"
            connection.execute(
                "INSERT INTO publication_sagas VALUES (?, ?, ?, ?, ?, ?, "
                "'accepted', ?, ?)",
                (
                    saga_id,
                    JOB_REF,
                    saga_id,
                    str(index) * 64,
                    str(index + 2) * 64,
                    str(index + 4) * 64,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO publication_targets VALUES (?, 'library', ?, ?, ?, "
                "'verified', ?)",
                (
                    saga_id,
                    f"library/{saga_id}.md",
                    str(index + 6) * 64,
                    str(index + 6) * 64,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO publication_acceptances VALUES (?, '{}', ?)",
                (saga_id, timestamp),
            )

    assert publication_status(db, JOB_REF)["saga_id"] == "pub-current"
    history = reconcile_publications(tmp_path, db, job_ref=JOB_REF)
    assert history[0]["state"] == "superseded"
    assert history[0]["targets"] == {"library": "verified"}
    assert history[1]["state"] == "accepted"
    assert history[1]["targets"] == {"library": "missing"}
    with sqlite3.connect(db) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(publication_sagas)")
        }
        assert {"superseded_at", "superseded_by_saga_id"} <= columns
        assert connection.execute(
            "SELECT MAX(version) FROM publication_schema_migrations"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT superseded_by_saga_id FROM publication_sagas "
            "WHERE saga_id = 'pub-old'"
        ).fetchone()[0] == "pub-current"
