from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import threading
import uuid
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from douyin_knowledge import __version__
from douyin_knowledge.cli import main as cli_main
from douyin_knowledge.contracts import CliError, failure, success
from douyin_knowledge.paths import default_instance_root
from douyin_knowledge.semantic_handoff import CANDIDATE_HANDLE, HANDOFF_MANIFEST

GATEWAY_PROTOCOL = "douyin-knowledge-agent-gateway-v1"
GATEWAY_STATE_VERSION = 1
ASSIGNMENT_PATTERN = re.compile(r"^assignment-[a-f0-9]{32}$")
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_CANDIDATE_BYTES = 2 * 1024 * 1024
_ENVELOPE_KEYS = {
    "schema_version",
    "ok",
    "operation",
    "data",
    "error",
    "warnings",
    "safe_summary",
}


@dataclass(frozen=True)
class GatewayVisual:
    assignment_ref: str
    handle: str
    mime_type: str
    sha256: str
    content: bytes


def default_gateway_root(instance_root: Path) -> Path:
    root = instance_root.expanduser().resolve()
    if root.parent == root:
        raise CliError(
            "gateway_workspace_required",
            "a separate gateway workspace is required for this instance root",
            "configure DOUYIN_KNOWLEDGE_GATEWAY_ROOT outside the private instance",
        )
    return root.parent / f"{root.name}.agent-gateway"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AgentGateway:
    """Host-neutral, candidate-only gateway over the authoritative JSON CLI."""

    def __init__(
        self,
        instance_root: Path | None = None,
        workspace_root: Path | None = None,
        *,
        cli_runner: Callable[[list[str]], tuple[int, str]] | None = None,
    ) -> None:
        self.instance_root = (instance_root or default_instance_root()).expanduser().resolve()
        self.workspace_root = (
            workspace_root or default_gateway_root(self.instance_root)
        ).expanduser().resolve()
        self._validate_workspace()
        self.assignments_root = self.workspace_root / "assignments"
        self.assignments_root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.workspace_root / "gateway-state-v1.json"
        self._lock = threading.RLock()
        self._cli_runner = cli_runner or self._run_cli_in_process

    def _validate_workspace(self) -> None:
        root = self.instance_root
        workspace = self.workspace_root
        if workspace == root or workspace.is_relative_to(root) or root.is_relative_to(workspace):
            raise CliError(
                "gateway_workspace_unsafe",
                "gateway workspace must be separate from the private instance",
                "choose a sibling directory dedicated to isolated agent handoffs",
            )
        if workspace.exists() and (not workspace.is_dir() or workspace.is_symlink()):
            raise CliError(
                "gateway_workspace_unsafe",
                "gateway workspace must be a real directory",
                "choose a non-symlink directory outside the private instance",
            )
        workspace.mkdir(parents=True, exist_ok=True)

    def _run_cli_in_process(self, arguments: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(
                ["--root", str(self.instance_root), *arguments, "--json"]
            )
        return exit_code, output.getvalue()

    def _invoke(self, arguments: list[str]) -> dict[str, Any]:
        with self._lock:
            _exit_code, raw = self._cli_runner(arguments)
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CliError(
                "gateway_cli_protocol_invalid",
                "the local CLI did not return one complete JSON envelope",
                "repair or reinstall the matching douyin-knowledge release",
            ) from exc
        if not isinstance(payload, dict) or set(payload) != _ENVELOPE_KEYS:
            raise CliError(
                "gateway_cli_protocol_invalid",
                "the local CLI returned an unsupported envelope",
                "repair or reinstall the matching douyin-knowledge release",
            )
        if payload.get("ok") is not True:
            error = payload.get("error")
            if not isinstance(error, dict):
                raise CliError(
                    "gateway_cli_protocol_invalid",
                    "the local CLI failure envelope is incomplete",
                    "repair or reinstall the matching douyin-knowledge release",
                )
            raise CliError(
                str(error.get("code") or "gateway_cli_failed"),
                str(error.get("message") or "the local CLI operation failed"),
                str(error.get("user_action") or "inspect the local installation"),
                retryable=bool(error.get("retryable")),
                preserved_checkpoint=bool(error.get("preserved_checkpoint", True)),
            )
        return payload

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": GATEWAY_STATE_VERSION, "assignments": []}
        if self.state_path.is_symlink():
            raise CliError(
                "gateway_state_invalid",
                "gateway state must not be a symbolic link",
                "restore the gateway state from a trusted local backup",
            )
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CliError(
                "gateway_state_invalid",
                "gateway state is unreadable",
                "restore the gateway state before assigning another worker",
            ) from exc
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != GATEWAY_STATE_VERSION
            or not isinstance(state.get("assignments"), list)
        ):
            raise CliError(
                "gateway_state_invalid",
                "gateway state uses an unsupported contract",
                "restore the gateway state before assigning another worker",
            )
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        _atomic_json(self.state_path, state)

    def _assignment(self, assignment_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not ASSIGNMENT_PATTERN.fullmatch(assignment_ref):
            raise CliError(
                "gateway_assignment_invalid",
                "assignment reference is invalid",
                "use the assignment reference returned by gateway_prepare_handoff",
            )
        state = self._state()
        matches = [
            item
            for item in state["assignments"]
            if isinstance(item, dict) and item.get("assignment_ref") == assignment_ref
        ]
        if len(matches) != 1:
            raise CliError(
                "gateway_assignment_missing",
                "gateway assignment is not active",
                "prepare a new handoff for the selected job",
            )
        return state, matches[0]

    def _assignment_directory(self, assignment: dict[str, Any]) -> Path:
        relative = assignment.get("directory_handle")
        if not isinstance(relative, str) or relative != assignment.get("assignment_ref"):
            raise CliError(
                "gateway_state_invalid",
                "gateway assignment directory binding is invalid",
                "preserve the handoff and repair the gateway state",
            )
        directory = (self.assignments_root / relative).resolve()
        if (
            directory.parent != self.assignments_root.resolve()
            or not directory.is_dir()
            or directory.is_symlink()
        ):
            raise CliError(
                "gateway_assignment_directory_invalid",
                "gateway assignment directory is unavailable",
                "restore the isolated handoff or prepare a new assignment",
            )
        return directory

    def _manifest(self, assignment: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        directory = self._assignment_directory(assignment)
        path = directory / HANDOFF_MANIFEST
        if not path.is_file() or path.is_symlink():
            raise CliError(
                "gateway_manifest_invalid",
                "gateway handoff manifest is unavailable",
                "discard the handoff and prepare a new assignment",
            )
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CliError(
                "gateway_manifest_invalid",
                "gateway handoff manifest is unreadable",
                "discard the handoff and prepare a new assignment",
            ) from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("job_ref") != assignment.get("job_ref")
            or manifest.get("handoff_ref") != assignment.get("handoff_ref")
            or manifest.get("candidate_handle") != CANDIDATE_HANDLE
            or not isinstance(manifest.get("files"), list)
            or _sha256(path) != assignment.get("manifest_sha256")
        ):
            raise CliError(
                "gateway_manifest_invalid",
                "gateway handoff manifest does not match the assignment",
                "discard the handoff and prepare a new assignment",
            )
        return directory, manifest

    @staticmethod
    def _entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        for item in manifest["files"]:
            if not isinstance(item, dict):
                raise CliError(
                    "gateway_manifest_invalid",
                    "gateway handoff file inventory is invalid",
                    "discard the handoff and prepare a new assignment",
                )
            handle = item.get("handle")
            if not isinstance(handle, str) or handle in entries or "\\" in handle:
                raise CliError(
                    "gateway_manifest_invalid",
                    "gateway handoff contains an invalid handle",
                    "discard the handoff and prepare a new assignment",
                )
            pure = PurePosixPath(handle)
            if pure.is_absolute() or ".." in pure.parts or handle in {"", "."}:
                raise CliError(
                    "gateway_manifest_invalid",
                    "gateway handoff contains an unsafe handle",
                    "discard the handoff and prepare a new assignment",
                )
            entries[handle] = item
        return entries

    def _verified_file(
        self, assignment: dict[str, Any], handle: str
    ) -> tuple[Path, dict[str, Any], dict[str, dict[str, Any]]]:
        directory, manifest = self._manifest(assignment)
        entries = self._entries(manifest)
        entry = entries.get(handle)
        if entry is None:
            raise CliError(
                "gateway_handle_not_allowed",
                "requested handle is not part of this isolated assignment",
                "use one handle returned by gateway_get_manifest",
            )
        path = (directory / Path(PurePosixPath(handle))).resolve()
        try:
            path.relative_to(directory)
        except ValueError as exc:
            raise CliError(
                "gateway_handle_not_allowed",
                "requested handle escaped the isolated assignment",
                "use one handle returned by gateway_get_manifest",
            ) from exc
        if not path.is_file() or path.is_symlink():
            raise CliError(
                "gateway_file_invalid",
                "requested assignment file is unavailable",
                "discard the handoff and prepare a new assignment",
            )
        if path.stat().st_size != entry.get("size_bytes") or _sha256(path) != entry.get(
            "sha256"
        ):
            raise CliError(
                "gateway_file_mismatch",
                "requested assignment file no longer matches its manifest",
                "discard the handoff and prepare a new assignment",
            )
        return path, manifest, entries

    @staticmethod
    def _required_handles(entries: dict[str, dict[str, Any]]) -> tuple[set[str], set[str]]:
        visuals = {handle for handle in entries if handle.startswith("visual-evidence/")}
        texts = set(entries) - visuals
        return texts, visuals

    def _record_read(self, state: dict[str, Any], assignment: dict[str, Any], handle: str) -> None:
        field = "visual_reads" if handle.startswith("visual-evidence/") else "text_reads"
        values = assignment.setdefault(field, [])
        if handle not in values:
            values.append(handle)
            values.sort()
            self._save_state(state)

    def capabilities(self) -> dict[str, Any]:
        return success(
            "gateway_capabilities",
            {
                "gateway_protocol": GATEWAY_PROTOCOL,
                "release_version": __version__,
                "mode": "candidate-only",
                "transports": ["python", "mcp-stdio", "file-handoff"],
                "features": {
                    "read_safe_status": True,
                    "plan_fixed_jobs": True,
                    "materialize_isolated_handoff": True,
                    "read_complete_text_inventory": True,
                    "open_complete_visual_inventory": True,
                    "atomic_candidate_submission": True,
                    "deterministic_candidate_ingest": True,
                    "full_orchestration": False,
                    "interactive_login": False,
                    "publication": False,
                },
                "limits": {
                    "max_active_semantic_assignments": 2,
                    "max_candidate_bytes": MAX_CANDIDATE_BYTES,
                },
            },
            summary="candidate-only agent gateway capabilities reported",
        )

    def doctor(self) -> dict[str, Any]:
        payload = self._invoke(["doctor"])
        return success(
            "gateway_doctor",
            payload["data"],
            summary="local capability checks completed through the authoritative CLI",
            warnings=payload["warnings"],
        )

    def status(self) -> dict[str, Any]:
        payload = self._invoke(["status"])
        return success(
            "gateway_status",
            payload["data"],
            summary="local status read through the authoritative CLI",
            warnings=payload["warnings"],
        )

    def plan(self, limit: int = 1, status: str | None = None) -> dict[str, Any]:
        arguments = ["plan", "--limit", str(limit)]
        if status is not None:
            arguments.extend(["--status", status])
        payload = self._invoke(arguments)
        return success(
            "gateway_plan",
            payload["data"],
            summary="fixed job references planned without mutation",
            warnings=payload["warnings"],
        )

    def prepare_handoff(self, job_ref: str, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise CliError(
                "confirmation_required",
                "gateway handoff materialization requires explicit confirmation",
                "review the fixed job and isolated worker scope, then confirm",
            )
        with self._lock:
            state = self._state()
            active = [
                item
                for item in state["assignments"]
                if isinstance(item, dict) and item.get("status") != "cleaned"
            ]
            if any(item.get("job_ref") == job_ref for item in active):
                raise CliError(
                    "gateway_assignment_duplicate",
                    "the selected job already has a gateway assignment",
                    "resume or clean up the existing assignment instead of duplicating it",
                )
            assignment_ref = f"assignment-{uuid.uuid4().hex}"
            directory = self.assignments_root / assignment_ref
            payload = self._invoke(
                [
                    "handoff",
                    "materialize",
                    "--job-ref",
                    job_ref,
                    "--directory",
                    str(directory),
                    "--confirm",
                ]
            )
            data = payload["data"]
            assignment = {
                "assignment_ref": assignment_ref,
                "directory_handle": assignment_ref,
                "job_ref": job_ref,
                "handoff_ref": data["handoff_ref"],
                "cleanup_token": data["cleanup_token"],
                "manifest_handle": data["manifest_handle"],
                "candidate_handle": data["candidate_handle"],
                "packet_sha256": data["packet_sha256"],
                "manifest_sha256": data["manifest_sha256"],
                "status": "active",
                "manifest_read": False,
                "text_reads": [],
                "visual_reads": [],
                "submit_attempts": 0,
            }
            state["assignments"].append(assignment)
            self._save_state(state)
        return success(
            "gateway_prepare_handoff",
            {
                "assignment_ref": assignment_ref,
                "job_ref": job_ref,
                "handoff_ref": data["handoff_ref"],
                "manifest_handle": data["manifest_handle"],
                "candidate_handle": data["candidate_handle"],
                "packet_sha256": data["packet_sha256"],
                "counts": data["counts"],
                "assignment_capacity": data["assignment_capacity"],
            },
            summary="isolated semantic assignment prepared without exposing a local path",
        )

    def get_manifest(self, assignment_ref: str) -> dict[str, Any]:
        with self._lock:
            state, assignment = self._assignment(assignment_ref)
            _directory, manifest = self._manifest(assignment)
            assignment["manifest_read"] = True
            self._save_state(state)
        return success(
            "gateway_get_manifest",
            {"assignment_ref": assignment_ref, "manifest": manifest},
            summary="isolated handoff manifest returned",
        )

    def read_text(self, assignment_ref: str, handle: str) -> dict[str, Any]:
        with self._lock:
            state, assignment = self._assignment(assignment_ref)
            path, _manifest, _entries = self._verified_file(assignment, handle)
            if handle.startswith("visual-evidence/") or path.suffix.lower() not in {
                ".json",
                ".md",
            }:
                raise CliError(
                    "gateway_handle_type_invalid",
                    "requested handle is not a readable text resource",
                    "use gateway_open_visual for visual evidence",
                )
            if path.stat().st_size > MAX_TEXT_BYTES:
                raise CliError(
                    "gateway_text_too_large",
                    "requested text resource exceeds the gateway limit",
                    "recreate the bounded evidence packet with the current release",
                )
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise CliError(
                    "gateway_text_invalid",
                    "requested text resource is not valid UTF-8",
                    "discard the handoff and prepare a new assignment",
                ) from exc
            self._record_read(state, assignment, handle)
        return success(
            "gateway_read_text",
            {
                "assignment_ref": assignment_ref,
                "handle": handle,
                "sha256": _sha256(path),
                "content": content,
            },
            summary="one verified text resource returned",
        )

    def open_visual(self, assignment_ref: str, handle: str) -> GatewayVisual:
        with self._lock:
            state, assignment = self._assignment(assignment_ref)
            path, _manifest, _entries = self._verified_file(assignment, handle)
            if not handle.startswith("visual-evidence/") or path.suffix.lower() not in {
                ".jpg",
                ".jpeg",
                ".png",
            }:
                raise CliError(
                    "gateway_handle_type_invalid",
                    "requested handle is not visual evidence",
                    "use one visual handle returned by gateway_get_manifest",
                )
            content = path.read_bytes()
            self._record_read(state, assignment, handle)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return GatewayVisual(
            assignment_ref=assignment_ref,
            handle=handle,
            mime_type=mime_type,
            sha256=_sha256(path),
            content=content,
        )

    def assignment_status(self, assignment_ref: str) -> dict[str, Any]:
        with self._lock:
            _state, assignment = self._assignment(assignment_ref)
            _directory, manifest = self._manifest(assignment)
            entries = self._entries(manifest)
            required_texts, required_visuals = self._required_handles(entries)
            read_texts = set(assignment.get("text_reads") or [])
            read_visuals = set(assignment.get("visual_reads") or [])
        return success(
            "gateway_assignment_status",
            {
                "assignment_ref": assignment_ref,
                "job_ref": assignment["job_ref"],
                "status": assignment["status"],
                "manifest_read": bool(assignment.get("manifest_read")),
                "required_text_count": len(required_texts),
                "read_text_count": len(required_texts & read_texts),
                "required_visual_count": len(required_visuals),
                "opened_visual_count": len(required_visuals & read_visuals),
                "missing_text_handles": sorted(required_texts - read_texts),
                "missing_visual_handles": sorted(required_visuals - read_visuals),
                "submit_attempts": int(assignment.get("submit_attempts") or 0),
            },
            summary="gateway assignment evidence traversal status reported",
        )

    def submit_candidate(
        self, assignment_ref: str, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock:
            state, assignment = self._assignment(assignment_ref)
            directory, manifest = self._manifest(assignment)
            entries = self._entries(manifest)
            required_texts, required_visuals = self._required_handles(entries)
            missing_texts = required_texts - set(assignment.get("text_reads") or [])
            missing_visuals = required_visuals - set(assignment.get("visual_reads") or [])
            if not assignment.get("manifest_read") or missing_texts or missing_visuals:
                raise CliError(
                    "gateway_evidence_incomplete",
                    "candidate submission requires the complete text and visual inventory",
                    "read the manifest, every text handle, and every visual handle before retrying",
                )
            attempts = int(assignment.get("submit_attempts") or 0)
            if attempts >= 2:
                raise CliError(
                    "gateway_submission_blocked",
                    "candidate submission already failed twice",
                    "preserve the assignment and inspect the deterministic validation error",
                )
            if not isinstance(candidate, dict):
                raise CliError(
                    "gateway_candidate_invalid",
                    "candidate must be one JSON object",
                    "generate the candidate from candidate.schema.json",
                )
            try:
                encoded = (
                    json.dumps(candidate, ensure_ascii=False, indent=2, allow_nan=False)
                    + "\n"
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise CliError(
                    "gateway_candidate_invalid",
                    "candidate must be one strict JSON object",
                    "generate the candidate from candidate.schema.json",
                ) from exc
            if len(encoded) > MAX_CANDIDATE_BYTES:
                raise CliError(
                    "gateway_candidate_too_large",
                    "candidate exceeds the gateway size limit",
                    "remove unsupported or duplicated content and retry",
                )
            candidate_path = directory / CANDIDATE_HANDLE
            temporary = candidate_path.with_name(
                f".{candidate_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.write_bytes(encoded)
                os.replace(temporary, candidate_path)
            finally:
                temporary.unlink(missing_ok=True)
            assignment["submit_attempts"] = attempts + 1
            self._save_state(state)
            try:
                payload = self._invoke(
                    [
                        "handoff",
                        "ingest",
                        "--job-ref",
                        str(assignment["job_ref"]),
                        "--directory",
                        str(directory),
                    ]
                )
            except CliError:
                assignment["status"] = "rejected" if attempts == 0 else "blocked"
                self._save_state(state)
                raise
            assignment["status"] = "ingested"
            self._save_state(state)
        return success(
            "gateway_submit_candidate",
            {
                "assignment_ref": assignment_ref,
                "job_ref": assignment["job_ref"],
                "status": "ingested",
                "ingest": payload["data"],
            },
            summary="candidate atomically written and ingested through deterministic gates",
            warnings=payload["warnings"],
        )

    def cleanup_assignment(self, assignment_ref: str, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise CliError(
                "confirmation_required",
                "gateway assignment cleanup requires explicit confirmation",
                "confirm removal of the isolated handoff after ingest or abandonment",
            )
        with self._lock:
            state, assignment = self._assignment(assignment_ref)
            directory = self._assignment_directory(assignment)
            payload = self._invoke(
                [
                    "handoff",
                    "cleanup",
                    "--job-ref",
                    str(assignment["job_ref"]),
                    "--directory",
                    str(directory),
                    "--token",
                    str(assignment["cleanup_token"]),
                    "--confirm",
                ]
            )
            state["assignments"] = [
                item
                for item in state["assignments"]
                if not isinstance(item, dict)
                or item.get("assignment_ref") != assignment_ref
            ]
            self._save_state(state)
        return success(
            "gateway_cleanup_assignment",
            {
                "assignment_ref": assignment_ref,
                "removed": bool(payload["data"].get("removed")),
            },
            summary="verified isolated assignment removed",
            warnings=payload["warnings"],
        )


def safe_gateway_call(operation: str, callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return callback()
    except CliError as exc:
        return failure(operation, exc)
    except Exception:
        return failure(
            operation,
            CliError(
                "gateway_internal_error",
                "the gateway operation failed without exposing private diagnostics",
                "inspect the local gateway process and preserve the current assignment",
                exit_code=1,
            ),
        )
