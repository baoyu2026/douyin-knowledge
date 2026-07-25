from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from app.analyze_video import JOB_ID_PATTERN
from app.content_packet import ContentPacketError, build_content_packet
from app.evidence_bundle import EvidenceBundleError, build_evidence_bundle
from app.structured_content import (
    StructuredContentError,
    _analysis_inputs,
    _effective_schema,
    _library_catalog,
    render_structured_json_artifact,
    validate_structured_payload,
)
from douyin_knowledge.contracts import CliError
from douyin_knowledge.review import latest_candidate_decision, latest_job_review

PROTOCOL_VERSION = 1
CANDIDATE_FIELDS = (
    "protocol_version",
    "schema_version",
    "job_ref",
    "packet_sha256",
    "content",
)
NON_REPAIRABLE_ERRORS = frozenset(
    {
        "candidate_json_invalid",
        "candidate_schema_invalid",
        "candidate_protocol_mismatch",
        "candidate_schema_version_mismatch",
        "candidate_job_mismatch",
        "candidate_packet_mismatch",
        "packet_manifest_missing",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_bytes(path, encoded)


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _handle(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _inside(root: Path, supplied: Path) -> Path:
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        candidate = candidate.resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise CliError(
            "candidate_input_outside_root",
            "candidate input must be inside the configured instance root",
            "write the candidate to the exported task directory",
        ) from exc
    return candidate


def _candidate_schema(content_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://douyin-knowledge.local/schemas/candidate-v1.schema.json",
        "title": "Douyin Knowledge Candidate v1",
        "type": "object",
        "additionalProperties": False,
        "required": list(CANDIDATE_FIELDS),
        "properties": {
            "protocol_version": {"const": PROTOCOL_VERSION},
            "schema_version": {"const": 1},
            "job_ref": {"type": "string", "pattern": JOB_ID_PATTERN.pattern},
            "packet_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "content": copy.deepcopy(content_schema),
        },
    }


def export_packet(root: Path, job_ref: str) -> dict[str, Any]:
    root = root.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_ref):
        raise CliError(
            "invalid_job_ref",
            "job reference is invalid",
            "select a job reference returned by plan or status",
        )
    task = root / "data" / "tasks" / job_ref / "semantic-v1"
    packet_path = task / "content-packet.json"
    schema_path = task / "candidate.schema.json"
    instructions_path = task / "worker-instructions.md"
    manifest_path = task / "protocol-manifest.json"
    try:
        packet = build_content_packet(root, job_ref, packet_path)
        _inputs, frames, _duration = _analysis_inputs(root, job_ref)
        evidence = build_evidence_bundle(root, job_ref, task, frames)
        catalog = _library_catalog(root)
        content_schema, _effective_path = _effective_schema(root, job_ref, catalog, frames)
    except (ContentPacketError, EvidenceBundleError, StructuredContentError) as exc:
        raise CliError(
            getattr(exc, "code", "packet_export_failed"),
            "content packet prerequisites are incomplete",
            "complete local analysis and required evidence before exporting",
        ) from exc
    candidate_schema = _candidate_schema(content_schema)
    _atomic_json(schema_path, candidate_schema)
    schema_hash = _sha256(schema_path)
    candidate_handle = _handle(root, task / "candidate-v1.json")
    evidence_manifest_handle = _handle(root, evidence.manifest_path)
    evidence_chunk_handles = [_handle(root, path) for path in evidence.chunk_paths]
    visual_handles = [_handle(root, path) for path in evidence.visual_paths]
    instructions = (
        "Read only content-packet.json, candidate.schema.json, evidence-manifest.json, "
        "and the exact evidence chunk and visual files listed by that manifest.\n"
        "Read every evidence chunk before writing the candidate; chunks contain the complete "
        "sanitized ASR/OCR/timeline evidence and must not be silently skipped.\n"
        "Inspect the listed visual files before writing visual_evidence. If this host cannot "
        "read images, stop and report the capability gap instead of fabricating visual claims.\n"
        "Write one pure JSON object to candidate-v1.json.tmp, close it, then rename it "
        "to candidate-v1.json.\n"
        f"Set job_ref to {job_ref} and packet_sha256 to {packet.sha256}.\n"
        "Do not output Markdown fences, commentary, paths, URLs, credentials, or raw IDs.\n"
        "Do not publish, mutate SQLite, or claim completion; candidate import is authoritative.\n"
    )
    _atomic_bytes(instructions_path, instructions.encode("utf-8"))
    manifest = {
        "protocol_version": PROTOCOL_VERSION,
        "job_ref": job_ref,
        "packet_sha256": packet.sha256,
        "candidate_schema_sha256": schema_hash,
        "evidence_manifest_sha256": _sha256(evidence.manifest_path),
        "evidence_chunks": evidence.payload["chunks"],
        "visuals": evidence.payload["visuals"],
        "candidate_handle": candidate_handle,
    }
    _atomic_json(manifest_path, manifest)
    return {
        "job_ref": job_ref,
        "protocol_version": PROTOCOL_VERSION,
        "packet_handle": _handle(root, packet_path),
        "packet_sha256": packet.sha256,
        "packet_size_bytes": packet.size_bytes,
        "estimated_tokens": packet.estimated_tokens,
        "candidate_schema_handle": _handle(root, schema_path),
        "candidate_schema_sha256": schema_hash,
        "evidence_manifest_handle": evidence_manifest_handle,
        "evidence_manifest_sha256": _sha256(evidence.manifest_path),
        "evidence_chunk_handles": evidence_chunk_handles,
        "visual_handles": visual_handles,
        "worker_instructions_handle": _handle(root, instructions_path),
        "candidate_output_handle": candidate_handle,
    }


def _load_object(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(
            code,
            "candidate JSON could not be read",
            "write one complete UTF-8 JSON object and retry the import",
        ) from exc
    if not isinstance(value, dict):
        raise CliError(
            code,
            "candidate must be one JSON object",
            "write one complete UTF-8 JSON object and retry the import",
        )
    return value


def _quarantine(root: Path, job_ref: str, candidate: Path) -> str | None:
    try:
        digest = _sha256(candidate)
        destination = root / "quarantine" / "candidates" / job_ref / f"{digest}.json"
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            shutil.copyfile(candidate, temporary)
            os.replace(temporary, destination)
        return digest
    except OSError:
        return None


def _record_rejection(
    root: Path,
    job_ref: str,
    candidate: Path,
    *,
    code: str,
) -> None:
    digest = _quarantine(root, job_ref, candidate)
    task = root / "data" / "tasks" / job_ref / "semantic-v1"
    _atomic_json(
        task / "last-rejection.json",
        {
            "protocol_version": PROTOCOL_VERSION,
            "job_ref": job_ref,
            "error_code": code,
            "candidate_sha256": digest,
        },
    )


def repair_contract(root: Path, job_ref: str) -> dict[str, Any]:
    root = root.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_ref):
        raise CliError(
            "invalid_job_ref",
            "job reference is invalid",
            "select a job reference returned by plan or status",
        )
    task = root / "data" / "tasks" / job_ref / "semantic-v1"
    rejection = _load_object(task / "last-rejection.json", "candidate_rejection_missing")
    code = str(rejection.get("error_code") or "candidate_rejection_missing")
    repairable = code not in NON_REPAIRABLE_ERRORS
    contract = {
        "protocol_version": PROTOCOL_VERSION,
        "job_ref": job_ref,
        "source_candidate_sha256": rejection.get("candidate_sha256"),
        "error_code": code,
        "repairable": repairable,
        "action": "repair_content_once" if repairable else "regenerate",
        "max_repair_attempts": 1 if repairable else 0,
        "editable_top_level_fields": ["content"] if repairable else [],
        "immutable_top_level_fields": [
            "protocol_version",
            "schema_version",
            "job_ref",
            "packet_sha256",
        ],
    }
    path = task / "repair-contract.json"
    _atomic_json(path, contract)
    return {
        "job_ref": job_ref,
        "error_code": code,
        "repairable": repairable,
        "action": contract["action"],
        "max_repair_attempts": contract["max_repair_attempts"],
        "contract_handle": _handle(root, path),
    }


def import_candidate(root: Path, job_ref: str, supplied: Path) -> dict[str, Any]:
    root = root.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_ref):
        raise CliError(
            "invalid_job_ref",
            "job reference is invalid",
            "select a job reference returned by plan or status",
        )
    candidate_path = _inside(root, supplied)
    task = root / "data" / "tasks" / job_ref / "semantic-v1"
    manifest = _load_object(task / "protocol-manifest.json", "packet_manifest_missing")
    candidate = _load_object(candidate_path, "candidate_json_invalid")
    try:
        if len(candidate) != len(CANDIDATE_FIELDS) or set(candidate) != set(CANDIDATE_FIELDS):
            raise CliError(
                "candidate_schema_invalid",
                "candidate fields do not match protocol version 1",
                "regenerate the candidate from the exported schema",
            )
        if candidate.get("protocol_version") != PROTOCOL_VERSION:
            raise CliError(
                "candidate_protocol_mismatch",
                "candidate protocol version does not match",
                "regenerate the candidate from the current packet",
            )
        if candidate.get("schema_version") != 1:
            raise CliError(
                "candidate_schema_version_mismatch",
                "candidate schema version does not match",
                "regenerate the candidate from the current schema",
            )
        if candidate.get("job_ref") != job_ref:
            raise CliError(
                "candidate_job_mismatch",
                "candidate job reference does not match",
                "regenerate the candidate from the selected packet",
            )
        if candidate.get("packet_sha256") != manifest.get("packet_sha256"):
            raise CliError(
                "candidate_packet_mismatch",
                "candidate packet hash does not match",
                "discard the candidate and regenerate it from the current packet",
            )
        content = candidate.get("content")
        if not isinstance(content, dict):
            raise CliError(
                "candidate_schema_invalid",
                "candidate content must be one JSON object",
                "regenerate the candidate from the exported schema",
            )
        validate_structured_payload(root, job_ref, content)
    except CliError as exc:
        _record_rejection(root, job_ref, candidate_path, code=exc.code)
        raise
    except StructuredContentError as exc:
        _record_rejection(root, job_ref, candidate_path, code=exc.code)
        raise CliError(
            exc.code,
            "candidate content did not pass deterministic gates",
            "use the bounded repair contract when the error is retryable",
        ) from exc

    accepted_path = task / "candidate-v1.json"
    raw_path = root / "orchestration" / "structured-content" / job_ref / "response-v1.json"
    draft_path = root / "orchestration" / "content-drafts" / f"{job_ref}-content.md"
    already_staged = raw_path.is_file() and draft_path.is_file()
    replaced = False
    candidate_hash = _sha256(candidate_path)
    latest_review = latest_job_review(root / "data" / "knowledge.db", job_ref=job_ref)
    official_replacement = bool(
        accepted_path.resolve() == candidate_path.resolve()
        and latest_review is not None
        and latest_review[0] != candidate_hash
    )
    if official_replacement and latest_review is not None:
        if latest_review[1] != "reject":
            replacement_hash = _quarantine(root, job_ref, candidate_path)
            history = root / "quarantine" / "candidates" / job_ref / f"{latest_review[0]}.json"
            if replacement_hash is not None and history.is_file():
                _atomic_bytes(accepted_path, history.read_bytes())
            raise CliError(
                "candidate_already_imported",
                "a different candidate has already been imported",
                "reject the current candidate before importing one replacement",
            )
        replaced = True
    if accepted_path.is_file():
        accepted = _load_object(accepted_path, "candidate_json_invalid")
        if accepted != candidate:
            accepted_hash = _sha256(accepted_path)
            decision = latest_candidate_decision(
                root / "data" / "knowledge.db",
                job_ref=job_ref,
                candidate_sha256=accepted_hash,
            )
            if decision != "reject":
                raise CliError(
                    "candidate_already_imported",
                    "a different candidate has already been imported",
                    "reject the current candidate before importing one replacement",
                )
            if _quarantine(root, job_ref, accepted_path) != accepted_hash:
                raise CliError(
                    "candidate_history_unavailable",
                    "the rejected candidate could not be preserved for audit",
                    "correct private storage access before importing the replacement",
                )
            _atomic_json(accepted_path, candidate)
            replaced = True
            reused = False
        else:
            reused = already_staged and not replaced
    else:
        _atomic_json(accepted_path, candidate)
        reused = False
    candidate_hash = _sha256(accepted_path)
    _atomic_json(raw_path, content)
    try:
        render_structured_json_artifact(root, job_ref, raw_path, draft_path)
    except StructuredContentError as exc:
        raise CliError(
            exc.code,
            "candidate rendering or staging validation failed",
            "correct only the rejected fields and import one replacement candidate",
        ) from exc
    return {
        "job_ref": job_ref,
        "status": "staged",
        "reused": reused,
        "replaced": replaced,
        "candidate_sha256": candidate_hash,
        "candidate_handle": _handle(root, accepted_path),
        "draft_handle": _handle(root, draft_path),
    }
