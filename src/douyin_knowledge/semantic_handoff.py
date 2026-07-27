from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from app.analyze_video import JOB_ID_PATTERN
from douyin_knowledge.contracts import CliError
from douyin_knowledge.protocol import (
    CANDIDATE_FIELDS,
    PROTOCOL_VERSION,
    export_packet,
    import_candidate,
)
from douyin_knowledge.protocol import repair_contract as build_repair_contract

HANDOFF_SCHEMA_VERSION = 1
HANDOFF_MANIFEST = "handoff-manifest.json"
CANDIDATE_HANDLE = "candidate-v1.json"
HANDOFF_FIELDS = frozenset(
    {
        "schema_version",
        "protocol_version",
        "handoff_ref",
        "job_ref",
        "packet_sha256",
        "cleanup_token_sha256",
        "candidate_handle",
        "counts",
        "files",
    }
)
HANDOFF_REPAIR_FIELDS = HANDOFF_FIELDS | {"repair_contract"}
FILE_ENTRY_FIELDS = frozenset({"handle", "size_bytes", "sha256"})
COUNT_FIELDS = frozenset({"bundle_files", "evidence_chunks", "evidence_records", "visuals"})
STATIC_HANDLES = frozenset(
    {
        "content-packet.json",
        "candidate.schema.json",
        "evidence-manifest.json",
        "worker-instructions.md",
    }
)
HANDOFF_REF_PATTERN = re.compile(r"^handoff-[a-f0-9]{32}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
CHUNK_PATTERN = re.compile(r"^evidence-chunks/chunk-[0-9]{3}\.json$")
VISUAL_PATTERN = re.compile(r"^visual-evidence/frame-[0-9]{3}\.(?:jpg|jpeg|png)$")
ASSIGNMENT_STATE_VERSION = 1
MAX_ACTIVE_ASSIGNMENTS = 2
ACTIVE_ASSIGNMENT_STATUSES = frozenset({"preparing", "active"})
ASSIGNMENT_LOCK_SCHEMA_VERSION = 1
INVALID_LOCK_STALE_SECONDS = 5 * 60
CLEANUP_TOKEN_PREFIX = "shc_"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: object) -> None:
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_bytes(path, content)


def _assignment_state_path(root: Path) -> Path:
    return root / "data" / "tasks" / "semantic-handoff-assignments-v1.json"


def _directory_sha256(directory: Path) -> str:
    canonical = os.path.normcase(str(directory.resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        import ctypes

        query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            query_limited_information, False, pid
        )
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return False


class _AssignmentLock:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "data" / "tasks" / ".semantic-handoff-assignments.lock"
        self.descriptor: int | None = None
        self.lease_id = uuid.uuid4().hex

    def _quarantine_if_stale(self) -> bool:
        try:
            before = self.path.stat()
            content = self.path.read_bytes()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        pid: int | None = None
        valid_owner = False
        try:
            owner = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            owner = None
        if (
            isinstance(owner, dict)
            and set(owner) == {"schema_version", "lease_id", "pid", "created_at"}
            and owner.get("schema_version") == ASSIGNMENT_LOCK_SCHEMA_VERSION
            and isinstance(owner.get("lease_id"), str)
            and re.fullmatch(r"[a-f0-9]{32}", owner["lease_id"])
            and isinstance(owner.get("pid"), int)
            and owner["pid"] > 0
            and isinstance(owner.get("created_at"), (int, float))
            and owner["created_at"] > 0
        ):
            pid = owner["pid"]
            valid_owner = True
        elif re.fullmatch(rb"[1-9][0-9]*", content.strip()):
            pid = int(content.strip())

        if pid is not None and _pid_alive(pid):
            return False
        invalid_lock_is_recent = (
            time.time() - before.st_mtime <= INVALID_LOCK_STALE_SECONDS
        )
        if not valid_owner and pid is None and invalid_lock_is_recent:
            return False
        try:
            after = self.path.stat()
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                return False
            if self.path.read_bytes() != content:
                return False
            digest = hashlib.sha256(content).hexdigest()
            quarantine = self.root / "quarantine" / "semantic-handoff-locks"
            quarantine.mkdir(parents=True, exist_ok=True)
            os.replace(self.path, quarantine / f"{digest}-{uuid.uuid4().hex}.lock")
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(100):
            try:
                self.descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                owner = {
                    "schema_version": ASSIGNMENT_LOCK_SCHEMA_VERSION,
                    "lease_id": self.lease_id,
                    "pid": os.getpid(),
                    "created_at": time.time(),
                }
                os.write(
                    self.descriptor,
                    (json.dumps(owner, separators=(",", ":")) + "\n").encode("utf-8"),
                )
                os.fsync(self.descriptor)
                return
            except FileExistsError:
                if self._quarantine_if_stale():
                    continue
                time.sleep(0.01)
        raise CliError(
            "semantic_assignment_state_busy",
            "semantic assignment state is busy",
            "retry the same handoff command",
            retryable=True,
        )

    def __exit__(self, _type: object, _value: object, _traceback: object) -> bool:
        if self.descriptor is not None:
            os.close(self.descriptor)
        try:
            owner = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            owner = None
        if isinstance(owner, dict) and owner.get("lease_id") == self.lease_id:
            self.path.unlink(missing_ok=True)
        return False


def _assignment_lock(root: Path) -> _AssignmentLock:
    return _AssignmentLock(root)


def _load_assignment_state(root: Path) -> dict[str, Any]:
    path = _assignment_state_path(root)
    if not path.is_file():
        return {"schema_version": ASSIGNMENT_STATE_VERSION, "assignments": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(
            "semantic_assignment_state_invalid",
            "semantic assignment state could not be read",
            "repair private assignment state before creating another handoff",
        ) from exc
    if (
        not isinstance(state, dict)
        or set(state) != {"schema_version", "assignments"}
        or state.get("schema_version") != ASSIGNMENT_STATE_VERSION
        or not isinstance(state.get("assignments"), list)
        or not all(isinstance(item, dict) for item in state["assignments"])
    ):
        raise CliError(
            "semantic_assignment_state_invalid",
            "semantic assignment state is invalid",
            "repair private assignment state before creating another handoff",
        )
    return state


def _assignment_capacity(state: dict[str, Any]) -> dict[str, int]:
    used = sum(
        item.get("status") in ACTIVE_ASSIGNMENT_STATUSES
        for item in state["assignments"]
    )
    return {"used": used, "available": max(0, MAX_ACTIVE_ASSIGNMENTS - used)}


def semantic_assignment_snapshot(root: Path) -> dict[str, Any]:
    root = root.resolve()
    with _assignment_lock(root):
        state = _load_assignment_state(root)
        capacity = _assignment_capacity(state)
        job_refs = sorted(
            {
                str(item["job_ref"])
                for item in state["assignments"]
                if item.get("status") in ACTIVE_ASSIGNMENT_STATUSES
                and isinstance(item.get("job_ref"), str)
                and JOB_ID_PATTERN.fullmatch(str(item["job_ref"]))
            }
        )
        return {**capacity, "job_refs": job_refs}


def semantic_assignment_capacity(root: Path) -> dict[str, int]:
    snapshot = semantic_assignment_snapshot(root)
    return {"used": int(snapshot["used"]), "available": int(snapshot["available"])}


def _reserve_assignment(
    root: Path,
    *,
    handoff_ref: str,
    job_ref: str,
    token_sha256: str,
    directory_sha256: str,
) -> None:
    with _assignment_lock(root):
        state = _load_assignment_state(root)
        if any(
            item.get("job_ref") == job_ref
            and item.get("status") in ACTIVE_ASSIGNMENT_STATUSES
            for item in state["assignments"]
        ):
            raise CliError(
                "semantic_job_already_assigned",
                "the selected job already has an unfinished semantic assignment",
                "resume or clean up the existing handoff instead of assigning it twice",
            )
        if _assignment_capacity(state)["available"] == 0:
            raise CliError(
                "semantic_assignment_limit_reached",
                "two semantic assignments are already unfinished",
                "ingest or clean up one existing handoff before materializing another",
            )
        state["assignments"].append(
            {
                "handoff_ref": handoff_ref,
                "job_ref": job_ref,
                "cleanup_token_sha256": token_sha256,
                "directory_sha256": directory_sha256,
                "packet_sha256": None,
                "manifest_sha256": None,
                "status": "preparing",
            }
        )
        _atomic_json(_assignment_state_path(root), state)


def _update_assignment(
    root: Path,
    handoff_ref: str,
    *,
    status: str,
    packet_sha256: str | None = None,
    manifest_sha256: str | None = None,
) -> None:
    with _assignment_lock(root):
        state = _load_assignment_state(root)
        matches = [item for item in state["assignments"] if item.get("handoff_ref") == handoff_ref]
        if len(matches) != 1:
            raise CliError(
                "semantic_assignment_missing",
                "private semantic assignment state is missing",
                "do not ingest or clean up an untracked handoff",
            )
        assignment = matches[0]
        assignment["status"] = status
        if packet_sha256 is not None:
            assignment["packet_sha256"] = packet_sha256
        if manifest_sha256 is not None:
            assignment["manifest_sha256"] = manifest_sha256
        _atomic_json(_assignment_state_path(root), state)


def _remove_assignment(root: Path, handoff_ref: str, *, missing_ok: bool) -> None:
    with _assignment_lock(root):
        state = _load_assignment_state(root)
        retained = [
            item for item in state["assignments"] if item.get("handoff_ref") != handoff_ref
        ]
        if len(retained) == len(state["assignments"]) and not missing_ok:
            raise CliError(
                "semantic_assignment_missing",
                "private semantic assignment state is missing",
                "do not clean up an untracked handoff",
            )
        state["assignments"] = retained
        _atomic_json(_assignment_state_path(root), state)


def _verify_assignment(
    root: Path,
    manifest: dict[str, Any],
    manifest_sha256: str,
    *,
    directory_sha256: str,
    allowed_statuses: frozenset[str],
) -> str:
    with _assignment_lock(root):
        state = _load_assignment_state(root)
        matches = [
            item
            for item in state["assignments"]
            if item.get("handoff_ref") == manifest.get("handoff_ref")
        ]
        if len(matches) != 1:
            raise CliError(
                "semantic_assignment_missing",
                "semantic handoff is not tracked by the private instance",
                "use a handoff created by this instance",
            )
        assignment = matches[0]
        if (
            assignment.get("status") not in allowed_statuses
            or assignment.get("job_ref") != manifest.get("job_ref")
            or assignment.get("packet_sha256") != manifest.get("packet_sha256")
            or assignment.get("manifest_sha256") != manifest_sha256
            or assignment.get("cleanup_token_sha256")
            != manifest.get("cleanup_token_sha256")
            or assignment.get("directory_sha256") != directory_sha256
        ):
            raise CliError(
                "semantic_assignment_mismatch",
                "semantic handoff does not match its private assignment",
                "use the original tracked handoff and token",
            )
        return str(assignment["status"])


def _verify_cleanup_recovery(
    root: Path,
    *,
    job_ref: str,
    token_sha256: str,
    directory_sha256: str,
) -> str:
    with _assignment_lock(root):
        state = _load_assignment_state(root)
        matches = [
            item
            for item in state["assignments"]
            if item.get("job_ref") == job_ref
            and item.get("cleanup_token_sha256") == token_sha256
            and item.get("directory_sha256") == directory_sha256
            and item.get("status") == "cleaning"
        ]
        if len(matches) != 1:
            raise CliError(
                "semantic_assignment_missing",
                "cleanup recovery does not match a tracked semantic assignment",
                "retry cleanup with the original directory and token",
            )
        return str(matches[0]["handoff_ref"])


def _load_object(path: Path, code: str, message: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(code, message, "recreate the handoff from the current packet") from exc
    if not isinstance(value, dict):
        raise CliError(code, message, "recreate the handoff from the current packet")
    return value


def _external_directory(root: Path, supplied: Path, *, must_exist: bool) -> Path:
    root = root.resolve()
    expanded = supplied.expanduser()
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if absolute.is_symlink():
        raise CliError(
            "handoff_directory_unsafe",
            "handoff directory must not be a symbolic link",
            "choose a real directory outside the configured instance root",
        )
    try:
        directory = absolute.resolve(strict=must_exist)
    except OSError as exc:
        raise CliError(
            "handoff_directory_missing",
            "handoff directory could not be resolved",
            "choose an accessible directory outside the configured instance root",
        ) from exc
    if directory == root or directory.is_relative_to(root) or root.is_relative_to(directory):
        raise CliError(
            "handoff_directory_inside_root",
            "handoff directory must be outside the configured instance root",
            "choose a separate empty directory controlled by the calling worker",
        )
    if must_exist and (not directory.is_dir() or directory.is_symlink()):
        raise CliError(
            "handoff_directory_missing",
            "handoff directory is missing or is not a real directory",
            "use the directory created by handoff materialize",
        )
    return directory


def _source(root: Path, handle: str) -> Path:
    try:
        path = (root / Path(handle)).resolve()
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CliError(
            "handoff_source_invalid",
            "exported packet inventory escaped the private instance",
            "repair the private instance before materializing another handoff",
        ) from exc
    if not path.is_file() or path.is_symlink():
        raise CliError(
            "handoff_source_missing",
            "one exported packet file is missing",
            "export the current packet again before materializing",
        )
    return path


def _entry(handle: str, path: Path) -> dict[str, Any]:
    return {"handle": handle, "size_bytes": path.stat().st_size, "sha256": _sha256(path)}


def _valid_handle(value: object) -> str | None:
    if not isinstance(value, str) or "\\" in value:
        return None
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or value in {"", "."}:
        return None
    if value in STATIC_HANDLES or CHUNK_PATTERN.fullmatch(value) or VISUAL_PATTERN.fullmatch(
        value
    ):
        return value
    return None


def _remove_staging(directory: Path) -> None:
    if directory.is_dir() and directory.name.startswith(".") and ".handoff-" in directory.name:
        shutil.rmtree(directory, ignore_errors=True)


def materialize_handoff(root: Path, job_ref: str, supplied: Path) -> dict[str, Any]:
    root = root.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_ref):
        raise CliError(
            "invalid_job_ref",
            "job reference is invalid",
            "select a job reference returned by plan or status",
        )
    directory = _external_directory(root, supplied, must_exist=False)
    parent = directory.parent
    if not parent.is_dir():
        raise CliError(
            "handoff_parent_missing",
            "handoff directory parent does not exist",
            "create the parent directory and retry with a new or empty child directory",
        )
    reused_empty = directory.exists()
    if reused_empty:
        if not directory.is_dir() or directory.is_symlink() or any(directory.iterdir()):
            raise CliError(
                "handoff_directory_not_empty",
                "handoff directory must be new or empty",
                "choose a new empty directory and retry",
            )

    handoff_ref = f"handoff-{uuid.uuid4().hex}"
    cleanup_token = f"{CLEANUP_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    token_sha256 = hashlib.sha256(cleanup_token.encode("utf-8")).hexdigest()
    _reserve_assignment(
        root,
        handoff_ref=handoff_ref,
        job_ref=job_ref,
        token_sha256=token_sha256,
        directory_sha256=_directory_sha256(directory),
    )
    try:
        return _materialize_reserved(
            root,
            job_ref,
            directory,
            reused_empty=reused_empty,
            handoff_ref=handoff_ref,
            cleanup_token=cleanup_token,
            token_sha256=token_sha256,
        )
    except Exception:
        _remove_assignment(root, handoff_ref, missing_ok=True)
        raise


def _materialize_reserved(
    root: Path,
    job_ref: str,
    directory: Path,
    *,
    reused_empty: bool,
    handoff_ref: str,
    cleanup_token: str,
    token_sha256: str,
) -> dict[str, Any]:
    parent = directory.parent
    exported = export_packet(root, job_ref)
    sources: list[tuple[str, Path]] = [
        ("content-packet.json", _source(root, str(exported["packet_handle"]))),
        ("candidate.schema.json", _source(root, str(exported["candidate_schema_handle"]))),
        ("evidence-manifest.json", _source(root, str(exported["evidence_manifest_handle"]))),
        ("worker-instructions.md", _source(root, str(exported["worker_instructions_handle"]))),
    ]
    sources.extend(
        (f"evidence-chunks/{Path(str(handle)).name}", _source(root, str(handle)))
        for handle in exported["evidence_chunk_handles"]
    )
    sources.extend(
        (f"visual-evidence/{Path(str(handle)).name}", _source(root, str(handle)))
        for handle in exported["visual_handles"]
    )
    handles = [handle for handle, _path in sources]
    if len(set(handles)) != len(handles):
        raise CliError(
            "handoff_source_invalid",
            "exported packet inventory contains duplicate file names",
            "repair the packet export before materializing another handoff",
        )

    evidence = _load_object(
        next(path for handle, path in sources if handle == "evidence-manifest.json"),
        "handoff_source_invalid",
        "exported evidence manifest is invalid",
    )
    staging = parent / f".{directory.name}.handoff-{uuid.uuid4().hex}.tmp"
    if staging.exists():
        raise CliError(
            "handoff_stage_conflict",
            "handoff staging directory already exists",
            "retry the same materialize command",
        )
    staging.mkdir(mode=0o700)
    try:
        entries: list[dict[str, Any]] = []
        for handle, source in sources:
            target = staging / Path(PurePosixPath(handle))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            entries.append(_entry(handle, target))
        counts = {
            "bundle_files": len(entries) + 1,
            "evidence_chunks": len(exported["evidence_chunk_handles"]),
            "evidence_records": int(evidence.get("record_count") or 0),
            "visuals": len(exported["visual_handles"]),
        }
        manifest = {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "handoff_ref": handoff_ref,
            "job_ref": job_ref,
            "packet_sha256": exported["packet_sha256"],
            "cleanup_token_sha256": token_sha256,
            "candidate_handle": CANDIDATE_HANDLE,
            "counts": counts,
            "files": entries,
        }
        _atomic_json(staging / HANDOFF_MANIFEST, manifest)
        manifest_sha256 = _sha256(staging / HANDOFF_MANIFEST)
        _update_assignment(
            root,
            handoff_ref,
            status="active",
            packet_sha256=str(exported["packet_sha256"]),
            manifest_sha256=manifest_sha256,
        )
        if reused_empty:
            directory.rmdir()
        elif directory.exists():
            raise CliError(
                "handoff_directory_not_empty",
                "handoff directory changed while materializing",
                "choose a new empty directory and retry",
            )
        try:
            os.replace(staging, directory)
        except OSError:
            if reused_empty and not directory.exists():
                directory.mkdir()
            raise
    except CliError:
        _remove_staging(staging)
        raise
    except OSError as exc:
        _remove_staging(staging)
        raise CliError(
            "handoff_materialize_failed",
            "semantic handoff could not be materialized atomically",
            "verify the empty destination is writable and retry",
        ) from exc

    return {
        "job_ref": job_ref,
        "handoff_ref": handoff_ref,
        "packet_sha256": exported["packet_sha256"],
        "manifest_sha256": manifest_sha256,
        "manifest_handle": HANDOFF_MANIFEST,
        "candidate_handle": CANDIDATE_HANDLE,
        "cleanup_token": cleanup_token,
        "counts": counts,
        "assignment_capacity": semantic_assignment_capacity(root),
    }


def _load_handoff_manifest(directory: Path, job_ref: str) -> dict[str, Any]:
    manifest = _load_object(
        directory / HANDOFF_MANIFEST,
        "handoff_manifest_invalid",
        "semantic handoff manifest is missing or invalid",
    )
    if set(manifest) not in {HANDOFF_FIELDS, HANDOFF_REPAIR_FIELDS}:
        raise CliError(
            "handoff_manifest_invalid",
            "semantic handoff manifest fields are invalid",
            "recreate the handoff from the current packet",
        )
    if (
        manifest.get("schema_version") != HANDOFF_SCHEMA_VERSION
        or manifest.get("protocol_version") != PROTOCOL_VERSION
        or manifest.get("job_ref") != job_ref
        or not HANDOFF_REF_PATTERN.fullmatch(str(manifest.get("handoff_ref") or ""))
        or not SHA256_PATTERN.fullmatch(str(manifest.get("packet_sha256") or ""))
        or not SHA256_PATTERN.fullmatch(str(manifest.get("cleanup_token_sha256") or ""))
        or manifest.get("candidate_handle") != CANDIDATE_HANDLE
    ):
        raise CliError(
            "handoff_manifest_mismatch",
            "semantic handoff manifest does not match the selected job or protocol",
            "use the matching job reference or recreate the handoff",
        )
    repair = manifest.get("repair_contract")
    if repair is not None and not isinstance(repair, dict):
        raise CliError(
            "handoff_manifest_invalid",
            "semantic handoff repair contract is invalid",
            "recreate the handoff from the current packet",
        )
    counts = manifest.get("counts")
    files = manifest.get("files")
    if not isinstance(counts, dict) or set(counts) != COUNT_FIELDS or not isinstance(files, list):
        raise CliError(
            "handoff_manifest_invalid",
            "semantic handoff inventory is invalid",
            "recreate the handoff from the current packet",
        )
    return manifest


def _validate_inventory(
    directory: Path,
    manifest: dict[str, Any],
    *,
    candidate_required: bool,
    allow_partial: bool = False,
) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for value in manifest["files"]:
        if not isinstance(value, dict) or set(value) != FILE_ENTRY_FIELDS:
            raise CliError(
                "handoff_manifest_invalid",
                "semantic handoff file inventory is invalid",
                "recreate the handoff from the current packet",
            )
        handle = _valid_handle(value.get("handle"))
        if (
            handle is None
            or handle in entries
            or not isinstance(value.get("size_bytes"), int)
            or int(value["size_bytes"]) < 0
            or not SHA256_PATTERN.fullmatch(str(value.get("sha256") or ""))
        ):
            raise CliError(
                "handoff_manifest_invalid",
                "semantic handoff file inventory is invalid",
                "recreate the handoff from the current packet",
            )
        entries[handle] = value
    if not STATIC_HANDLES.issubset(entries):
        raise CliError(
            "handoff_manifest_invalid",
            "semantic handoff is missing required worker files",
            "recreate the handoff from the current packet",
        )
    chunk_handles = {handle for handle in entries if CHUNK_PATTERN.fullmatch(handle)}
    visual_handles = {handle for handle in entries if VISUAL_PATTERN.fullmatch(handle)}
    counts = manifest["counts"]
    if (
        counts.get("bundle_files") != len(entries) + 1
        or counts.get("evidence_chunks") != len(chunk_handles)
        or counts.get("visuals") != len(visual_handles)
        or not isinstance(counts.get("evidence_records"), int)
        or counts["evidence_records"] < 0
    ):
        raise CliError(
            "handoff_manifest_invalid",
            "semantic handoff counts do not match its inventory",
            "recreate the handoff from the current packet",
        )

    expected_files = {HANDOFF_MANIFEST, *entries}
    if candidate_required:
        expected_files.add(CANDIDATE_HANDLE)
    expected_dirs = {
        str(PurePosixPath(handle).parent)
        for handle in entries
        if str(PurePosixPath(handle).parent) != "."
    }
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    try:
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise CliError(
                    "handoff_inventory_invalid",
                    "semantic handoff contains a symbolic link",
                    "remove the unsafe handoff and materialize a new one",
                )
            relative = path.relative_to(directory).as_posix()
            if path.is_dir():
                actual_dirs.add(relative)
            elif path.is_file():
                actual_files.add(relative)
            else:
                raise CliError(
                    "handoff_inventory_invalid",
                    "semantic handoff contains an unsupported entry",
                    "remove the unsafe handoff and materialize a new one",
                )
    except OSError as exc:
        raise CliError(
            "handoff_inventory_unreadable",
            "semantic handoff inventory could not be read",
            "restore access to the handoff directory and retry",
        ) from exc
    optional_candidate = {CANDIDATE_HANDLE} if CANDIDATE_HANDLE in actual_files else set()
    allowed_files = expected_files | optional_candidate
    invalid_inventory = (
        HANDOFF_MANIFEST not in actual_files
        or not actual_files.issubset(allowed_files)
        or not actual_dirs.issubset(expected_dirs)
        if allow_partial
        else actual_files != allowed_files or actual_dirs != expected_dirs
    )
    if invalid_inventory:
        raise CliError(
            "handoff_inventory_invalid",
            "semantic handoff directory contains missing or unexpected entries",
            "restore the exact materialized inventory and fixed candidate file",
        )
    for handle, value in entries.items():
        path = directory / Path(PurePosixPath(handle))
        if allow_partial and not path.exists():
            continue
        if path.stat().st_size != value["size_bytes"] or _sha256(path) != value["sha256"]:
            raise CliError(
                "handoff_file_mismatch",
                "one materialized worker file no longer matches its manifest",
                "discard the handoff and materialize the current packet again",
            )
    return entries


def _validate_bundle(
    root: Path,
    job_ref: str,
    directory: Path,
    *,
    candidate_required: bool,
    require_current: bool,
    allow_partial: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = _load_handoff_manifest(directory, job_ref)
    entries = _validate_inventory(
        directory,
        manifest,
        candidate_required=candidate_required,
        allow_partial=allow_partial,
    )
    if allow_partial:
        return manifest, entries
    packet_path = directory / "content-packet.json"
    if _sha256(packet_path) != manifest["packet_sha256"]:
        raise CliError(
            "handoff_packet_mismatch",
            "materialized content packet hash does not match the handoff manifest",
            "discard the handoff and materialize the current packet again",
        )
    packet = _load_object(
        directory / "content-packet.json",
        "handoff_packet_invalid",
        "materialized content packet is invalid",
    )
    evidence = _load_object(
        directory / "evidence-manifest.json",
        "handoff_evidence_invalid",
        "materialized evidence manifest is invalid",
    )
    if packet.get("job_ref") != job_ref or evidence.get("job_ref") != job_ref:
        raise CliError(
            "handoff_job_mismatch",
            "materialized packet files do not match the selected job",
            "use the matching job reference or recreate the handoff",
        )
    evidence_chunks = evidence.get("chunks")
    evidence_visuals = evidence.get("visuals")
    if (
        evidence.get("complete_sanitized_evidence") is not True
        or evidence.get("complete_visual_inventory") is not True
        or not isinstance(evidence_chunks, list)
        or not isinstance(evidence_visuals, list)
        or evidence.get("record_count") != manifest["counts"]["evidence_records"]
        or len(evidence_chunks) != manifest["counts"]["evidence_chunks"]
        or len(evidence_visuals) != manifest["counts"]["visuals"]
    ):
        raise CliError(
            "handoff_evidence_invalid",
            "materialized evidence inventory is incomplete",
            "discard the handoff and materialize the current packet again",
        )
    evidence_entry = entries["evidence-manifest.json"]
    if require_current:
        official = _load_object(
            root / "data" / "tasks" / job_ref / "semantic-v1" / "protocol-manifest.json",
            "packet_manifest_missing",
            "current semantic packet manifest is missing",
        )
        if (
            official.get("packet_sha256") != manifest["packet_sha256"]
            or official.get("candidate_schema_sha256")
            != entries["candidate.schema.json"]["sha256"]
            or official.get("evidence_manifest_sha256") != evidence_entry["sha256"]
        ):
            raise CliError(
                "handoff_packet_stale",
                "semantic handoff no longer matches the current packet",
                "discard it and materialize a new handoff for the same job",
            )
    return manifest, entries


def ingest_handoff(root: Path, job_ref: str, supplied: Path) -> dict[str, Any]:
    root = root.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_ref):
        raise CliError(
            "invalid_job_ref",
            "job reference is invalid",
            "select a job reference returned by plan or status",
        )
    directory = _external_directory(root, supplied, must_exist=True)
    manifest, _entries = _validate_bundle(
        root,
        job_ref,
        directory,
        candidate_required=True,
        require_current=True,
    )
    _verify_assignment(
        root,
        manifest,
        _sha256(directory / HANDOFF_MANIFEST),
        directory_sha256=_directory_sha256(directory),
        allowed_statuses=frozenset({"active"}),
    )
    candidate_path = directory / CANDIDATE_HANDLE
    candidate = _load_object(
        candidate_path,
        "candidate_json_invalid",
        "candidate JSON could not be read",
    )
    if set(candidate) != set(CANDIDATE_FIELDS):
        raise CliError(
            "candidate_schema_invalid",
            "candidate fields do not match the protocol schema",
            "regenerate candidate-v1.json from the materialized schema",
        )
    if candidate.get("job_ref") != job_ref:
        raise CliError(
            "candidate_job_mismatch",
            "candidate job reference does not match",
            "regenerate the candidate from the selected handoff",
        )
    if candidate.get("packet_sha256") != manifest["packet_sha256"]:
        raise CliError(
            "candidate_packet_mismatch",
            "candidate packet hash does not match",
            "regenerate the candidate from the selected handoff",
        )
    task = root / "data" / "tasks" / job_ref / "semantic-v1"
    staging = task / f".handoff-{manifest['handoff_ref']}-{uuid.uuid4().hex}.json"
    _atomic_bytes(staging, candidate_path.read_bytes())
    try:
        imported = import_candidate(root, job_ref, staging)
    finally:
        staging.unlink(missing_ok=True)
    _update_assignment(root, str(manifest["handoff_ref"]), status="ingested")
    return {
        "handoff_ref": manifest["handoff_ref"],
        **imported,
        "assignment_capacity": semantic_assignment_capacity(root),
    }


def materialize_handoff_repair_contract(
    root: Path, job_ref: str, supplied: Path
) -> dict[str, Any]:
    root = root.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_ref):
        raise CliError(
            "invalid_job_ref",
            "job reference is invalid",
            "select the job reference returned by handoff materialize",
        )
    directory = _external_directory(root, supplied, must_exist=True)
    manifest, _entries = _validate_bundle(
        root,
        job_ref,
        directory,
        candidate_required=True,
        require_current=True,
    )
    manifest_path = directory / HANDOFF_MANIFEST
    manifest_sha256 = _sha256(manifest_path)
    _verify_assignment(
        root,
        manifest,
        manifest_sha256,
        directory_sha256=_directory_sha256(directory),
        allowed_statuses=frozenset({"active"}),
    )
    result = build_repair_contract(root, job_ref)
    contract_path = _source(root, str(result["contract_handle"]))
    contract = _load_object(
        contract_path,
        "candidate_repair_contract_invalid",
        "bounded candidate repair contract is invalid",
    )
    candidate_sha256 = _sha256(directory / CANDIDATE_HANDLE)
    if (
        contract.get("repairable") is not True
        or contract.get("action") != "repair_content_once"
        or contract.get("max_repair_attempts") != 1
        or contract.get("source_candidate_sha256") != candidate_sha256
    ):
        raise CliError(
            "candidate_repair_contract_mismatch",
            "bounded candidate repair contract does not match this handoff candidate",
            "regenerate the candidate or retry repair for the matching rejection",
        )
    if manifest.get("repair_contract") == contract:
        reused = True
    else:
        original = manifest_path.read_bytes()
        updated = {**manifest, "repair_contract": contract}
        _atomic_json(manifest_path, updated)
        try:
            _update_assignment(
                root,
                str(manifest["handoff_ref"]),
                status="active",
                manifest_sha256=_sha256(manifest_path),
            )
        except Exception:
            _atomic_bytes(manifest_path, original)
            raise
        reused = False
    return {
        "job_ref": job_ref,
        "handoff_ref": manifest["handoff_ref"],
        "error_code": contract["error_code"],
        "action": contract["action"],
        "max_repair_attempts": contract["max_repair_attempts"],
        "repair_contract_handle": HANDOFF_MANIFEST,
        "reused": reused,
    }


def cleanup_handoff(
    root: Path,
    job_ref: str,
    supplied: Path,
    cleanup_token: str,
) -> dict[str, Any]:
    root = root.resolve()
    if not JOB_ID_PATTERN.fullmatch(job_ref):
        raise CliError(
            "invalid_job_ref",
            "job reference is invalid",
            "select the job reference returned by handoff materialize",
        )
    directory = _external_directory(root, supplied, must_exist=False)
    supplied_hash = hashlib.sha256(cleanup_token.encode("utf-8")).hexdigest()
    directory_hash = _directory_sha256(directory)
    if not directory.exists():
        handoff_ref = _verify_cleanup_recovery(
            root,
            job_ref=job_ref,
            token_sha256=supplied_hash,
            directory_sha256=directory_hash,
        )
        _remove_assignment(root, handoff_ref, missing_ok=False)
        return {
            "job_ref": job_ref,
            "handoff_ref": handoff_ref,
            "removed": True,
            "deleted_files": 0,
            "assignment_capacity": semantic_assignment_capacity(root),
        }
    if not directory.is_dir() or directory.is_symlink():
        raise CliError(
            "handoff_directory_missing",
            "handoff directory is missing or is not a real directory",
            "use the directory created by handoff materialize",
        )
    manifest_path = directory / HANDOFF_MANIFEST
    if not manifest_path.is_file():
        handoff_ref = _verify_cleanup_recovery(
            root,
            job_ref=job_ref,
            token_sha256=supplied_hash,
            directory_sha256=directory_hash,
        )
        try:
            if any(directory.iterdir()):
                raise OSError("cleanup recovery directory is not empty")
            directory.rmdir()
        except OSError as exc:
            raise CliError(
                "handoff_cleanup_incomplete",
                "semantic handoff cleanup stopped before the directory was fully removed",
                "preserve the directory and retry cleanup with the same token",
                retryable=True,
            ) from exc
        _remove_assignment(root, handoff_ref, missing_ok=False)
        return {
            "job_ref": job_ref,
            "handoff_ref": handoff_ref,
            "removed": True,
            "deleted_files": 0,
            "assignment_capacity": semantic_assignment_capacity(root),
        }

    manifest = _load_handoff_manifest(directory, job_ref)
    if not secrets.compare_digest(supplied_hash, str(manifest["cleanup_token_sha256"])):
        raise CliError(
            "handoff_cleanup_token_mismatch",
            "cleanup token does not match this semantic handoff",
            "use the cleanup token returned by the matching materialize command",
        )
    assignment_status = _verify_assignment(
        root,
        manifest,
        _sha256(manifest_path),
        directory_sha256=directory_hash,
        allowed_statuses=frozenset({"active", "ingested", "cleaning"}),
    )
    _manifest, entries = _validate_bundle(
        root,
        job_ref,
        directory,
        candidate_required=False,
        require_current=False,
        allow_partial=assignment_status == "cleaning",
    )
    if assignment_status != "cleaning":
        _update_assignment(root, str(manifest["handoff_ref"]), status="cleaning")
    deleted = 0
    try:
        candidate = directory / CANDIDATE_HANDLE
        if candidate.is_file():
            candidate.unlink()
            deleted += 1
        for handle in sorted(entries, key=lambda value: value.count("/"), reverse=True):
            target = directory / Path(PurePosixPath(handle))
            if target.is_file():
                target.unlink()
                deleted += 1
        for child in (directory / "evidence-chunks", directory / "visual-evidence"):
            if child.is_dir():
                child.rmdir()
        manifest_path.unlink()
        deleted += 1
        directory.rmdir()
    except OSError as exc:
        raise CliError(
            "handoff_cleanup_incomplete",
            "semantic handoff cleanup stopped before the directory was fully removed",
            "preserve the directory and retry cleanup with the same token",
            retryable=True,
        ) from exc
    _remove_assignment(root, str(manifest["handoff_ref"]), missing_ok=False)
    return {
        "job_ref": job_ref,
        "handoff_ref": manifest["handoff_ref"],
        "removed": True,
        "deleted_files": deleted,
        "assignment_capacity": semantic_assignment_capacity(root),
    }
