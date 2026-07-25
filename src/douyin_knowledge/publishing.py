from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.analyze_video import sha256_file
from app.content_stage import ContentStageError, validate_content_draft
from app.obsidian_publish import NOTE_ROOT, _obsidian_component, configured_vault
from app.publish_library import PublicationError, publish_job, resolve_library_target
from douyin_knowledge.contracts import CliError
from douyin_knowledge.publication import (
    accept_publication,
    begin_publication,
    reconcile_publications,
    seal_publication_targets,
)
from douyin_knowledge.result_archive import (
    ResultsConfigError,
    configured_results_root,
    results_handle,
)
from douyin_knowledge.review import latest_candidate_decision, require_current_candidate

PRIVATE_PATTERNS = (
    re.compile(r"(?i)\b(cookie|sessionid?|signature|request[_ -]?url)\b"),
    re.compile(r"(?i)\b(job[_ -]?id|source[_ -]?id|aweme[_ -]?id)\b"),
    re.compile(r"\baweme-[a-f0-9]{20}\b", re.I),
)


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _private_text_ok(*paths: Path) -> bool:
    try:
        content = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    except (OSError, UnicodeError):
        return False
    return not any(pattern.search(content) for pattern in PRIVATE_PATTERNS)


def _registered_vault_note(database: Path, job_ref: str) -> Path | None:
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT publication.note_path FROM obsidian_publications AS publication "
                "JOIN collection_items AS item USING(source_id) WHERE item.job_id = ?",
                (job_ref,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or not isinstance(row[0], str) or not row[0].strip():
        return None
    return Path(row[0])


def publish_staged_job(
    root: Path,
    *,
    job_ref: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    database = root / "data" / "knowledge.db"
    _candidate_path, candidate_digest = require_current_candidate(root, job_ref)
    if (
        latest_candidate_decision(
            database, job_ref=job_ref, candidate_sha256=candidate_digest
        )
        == "reject"
    ):
        raise CliError(
            "candidate_rejected",
            "the current candidate was rejected after publication",
            "import a corrected candidate from the current packet before publishing again",
        )
    try:
        archive_root = configured_results_root(root)
    except ResultsConfigError as exc:
        raise CliError(
            exc.code,
            "the human-readable results archive configuration is invalid",
            "configure a writable results folder before publishing",
        ) from exc
    if archive_root is None or not archive_root.is_dir():
        raise CliError(
            "results_root_required",
            "publishing requires an explicitly configured results folder",
            "run configure results with an absolute path and explicit confirmation",
        )
    draft_path = root / "orchestration" / "content-drafts" / f"{job_ref}-content.md"
    try:
        draft = validate_content_draft(root, job_ref, draft_path)
    except (ContentStageError, OSError) as exc:
        raise CliError(
            getattr(exc, "code", "content_draft_invalid"),
            "the staged content draft is no longer valid",
            "import a valid candidate from the current packet before publishing",
        ) from exc
    source = root / "data" / "jobs" / job_ref / "source.mp4"
    if not source.is_file():
        raise CliError(
            "source_media_missing",
            "publication source media is missing",
            "resume the same job through local analysis before publishing",
        )
    vault = configured_vault(root)
    if vault is None:
        raise CliError(
            "obsidian_vault_required",
            "publishing requires an explicitly configured Obsidian vault",
            "set vault in config/obsidian.yml and retry",
        )
    library_target = resolve_library_target(
        root,
        job_id=job_ref,
        category=draft.category,
        title=draft.title,
        source_video=source,
    )
    library_handle = results_handle(root, library_target / "内容整理.md")
    # Obsidian applies its own path sanitization to the human-facing category.
    vault_category = _obsidian_component(draft.category, field="category")
    vault_title = _obsidian_component(draft.title, field="title")
    vault_relative = _registered_vault_note(database, job_ref) or (
        NOTE_ROOT / vault_category / f"{vault_title}.md"
    )
    vault_handle = (Path("vault") / vault_relative).as_posix()
    draft_digest = _digest(draft_path)
    media_digest = sha256_file(source)
    archive_scope = hashlib.sha256(str(archive_root).casefold().encode()).hexdigest()
    key = idempotency_key or hashlib.sha256(
        f"{job_ref}:{draft_digest}:{media_digest}:{archive_scope}".encode()
    ).hexdigest()
    saga = begin_publication(
        root,
        database,
        job_ref=job_ref,
        idempotency_key=key,
        draft_sha256=draft_digest,
        media_sha256=media_digest,
        targets={
            "library": (library_handle, None),
            "vault": (vault_handle, None),
        },
    )
    try:
        library = publish_job(
            root,
            job_id=job_ref,
            category=draft.category,
            title=draft.title,
            tags=draft.tags,
            vault=vault,
            content_draft=draft_path,
            quality_mode="high-quality",
        )
    except PublicationError as exc:
        raise CliError(
            exc.code,
            "publication artifacts could not be written",
            "inspect the private publication log and reconcile the same job",
        ) from exc
    library_note = library / "内容整理.md"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT publication.note_path FROM obsidian_publications AS publication "
            "JOIN collection_items AS item USING(source_id) WHERE item.job_id = ?",
            (job_ref,),
        ).fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if row is None:
        raise CliError(
            "vault_publication_missing",
            "the vault publication was not registered",
            "reconcile the same publication before retrying",
        )
    vault_note = vault / Path(str(row[0]))
    seal_publication_targets(
        database,
        saga["saga_id"],
        expected={"library": _digest(library_note), "vault": _digest(vault_note)},
    )
    reconciled = reconcile_publications(root, database, job_ref=job_ref)
    current = next(item for item in reconciled if item["saga_id"] == saga["saga_id"])
    if current["state"] != "published_unaccepted":
        raise CliError(
            "publication_verification_failed",
            "publication targets did not match their sealed digests",
            "inspect the journal and retry only the same publication",
        )
    return accept_publication(
        database,
        saga["saga_id"],
        checks={
            "sqlite_integrity": integrity == "ok",
            "privacy": _private_text_ok(library_note, vault_note),
            "content": library_note.is_file() and vault_note.is_file(),
        },
    )
