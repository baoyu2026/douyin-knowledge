from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import importlib.util
import json
import os
import shutil
import sqlite3
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from douyin_knowledge import __version__
from douyin_knowledge.contracts import CliError, failure, success
from douyin_knowledge.operations import run_job
from douyin_knowledge.paths import default_instance_root, repository_root
from douyin_knowledge.platform_ops import install_asr_model
from douyin_knowledge.platform_ops import login as platform_login
from douyin_knowledge.platform_ops import sync as platform_sync
from douyin_knowledge.protocol import export_packet, import_candidate, repair_contract
from douyin_knowledge.publication import PublicationStateError, reconcile_publications
from douyin_knowledge.publishing import publish_reviewed_job
from douyin_knowledge.review import list_reviews, record_review

INSTANCE_DIRS = (
    "config",
    "data/jobs",
    "data/tasks",
    "library",
    "logs",
    "output",
    "orchestration",
    "quarantine",
    "schemas",
)
DEFAULT_CONFIG = """version: 1
project:
  data_root: ./data
  library_root: ./library
  obsidian_vault: null
analysis:
  asr_model: small
  device: cpu
  compute_type: int8
content:
  protocol: file-json-v1
  max_model_calls_per_item: 2
publishing:
  enabled: false
  require_confirmation: true
"""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise CliError(
            "invalid_arguments",
            "command arguments are invalid",
            "use --help and retry with one documented command",
        )
DOWNLOADER_CONFIG = """link:
  - https://www.douyin.com/user/self?showTab=favorite_collection
path: data/downloads
mode: [collect]
number:
  post: 0
  like: 0
  allmix: 0
  mix: 0
  music: 0
  collect: 20
  collectmix: 0
increase:
  post: false
  like: false
  allmix: false
  mix: false
  music: false
music: false
cover: true
avatar: false
json: true
folderstyle: true
author_dir: nickname_uid
video_quality: 720p
thread: 2
rate_limit: 1
retry_times: 3
proxy: ""
database: true
database_path: data/douyin-downloader.db
auto_cookie: false
progress:
  quiet_logs: true
transcript:
  enabled: false
browser_fallback:
  enabled: false
comments:
  enabled: false
notifications:
  enabled: false
  providers: []
"""


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_schemas(root: Path) -> None:
    for name in (
        "structured-content-v1.schema.json",
        "cli-envelope-v1.schema.json",
        "config-v1.schema.json",
    ):
        source = repository_root() / "schemas" / name
        destination = root / "schemas" / name
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            if source.is_file():
                shutil.copyfile(source, temporary)
            else:
                resource = importlib.resources.files("douyin_knowledge").joinpath(
                    "resources", name
                )
                temporary.write_bytes(resource.read_bytes())
            os.replace(temporary, destination)
        except (OSError, FileNotFoundError) as exc:
            raise CliError(
                "package_resource_missing",
                "a required versioned schema is unavailable",
                "reinstall the douyin-knowledge package",
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)


def _init(root: Path) -> dict[str, Any]:
    config = root / "config" / "config.yml"
    downloader = root / "config" / "downloader.yml"
    obsidian = root / "config" / "obsidian.yml"
    schemas = tuple(
        root / "schemas" / name
        for name in (
            "structured-content-v1.schema.json",
            "cli-envelope-v1.schema.json",
            "config-v1.schema.json",
        )
    )
    reused = (
        config.is_file()
        and downloader.is_file()
        and obsidian.is_file()
        and all(schema.is_file() for schema in schemas)
        and all((root / relative).is_dir() for relative in INSTANCE_DIRS)
    )
    for relative in INSTANCE_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    if not config.exists():
        _atomic_text(config, DEFAULT_CONFIG)
    if not downloader.exists():
        _atomic_text(downloader, DOWNLOADER_CONFIG)
    if not obsidian.exists():
        _atomic_text(obsidian, "vault: null\n")
    _copy_schemas(root)
    return success(
        "init",
        {
            "reused": reused,
            "created": [] if reused else list(INSTANCE_DIRS),
            "publishing_enabled": False,
        },
        summary="instance is initialized",
    )


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _status(root: Path) -> dict[str, Any]:
    database = root / "data" / "knowledge.db"
    counts: Counter[str] = Counter()
    integrity = "missing"
    if database.is_file():
        try:
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            with connection:
                connection.execute("PRAGMA query_only = ON")
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                if _table_exists(connection, "collection_items"):
                    counts.update(
                        dict(
                            connection.execute(
                                "SELECT status, COUNT(*) FROM collection_items "
                                "GROUP BY status ORDER BY status"
                            ).fetchall()
                        )
                    )
        except sqlite3.Error as exc:
            raise CliError(
                "database_unreadable",
                "the knowledge database could not be read",
                "restore the last verified database backup",
            ) from exc
        finally:
            if "connection" in locals():
                connection.close()
    publication = {"intent": 0, "published_unaccepted": 0, "accepted": 0}
    if database.is_file():
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
            if _table_exists(connection, "publication_sagas"):
                publication.update(
                    dict(
                        connection.execute(
                            "SELECT state, COUNT(*) FROM publication_sagas GROUP BY state"
                        ).fetchall()
                    )
                )
    pending_review = 0
    candidates = sorted((root / "data" / "tasks").glob("*/semantic-v1/candidate-v1.json"))
    if candidates:
        latest: dict[tuple[str, str], str] = {}
        if database.is_file():
            with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
                if _table_exists(connection, "review_records"):
                    rows = connection.execute(
                        "SELECT job_ref, candidate_sha256, decision FROM review_records "
                        "ORDER BY review_id"
                    ).fetchall()
                    latest = {
                        (str(job_ref), str(candidate_sha256)): str(decision)
                        for job_ref, candidate_sha256, decision in rows
                    }
        for candidate in candidates:
            job_ref = candidate.parents[1].name
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if latest.get((job_ref, digest)) != "approve":
                pending_review += 1
    active_stage = None
    active_job_ref = None
    active_runs: list[dict[str, str]] = []
    for lock in sorted((root / "data" / "tasks").glob("*/run.lock")):
        job_ref = lock.parent.name
        stage = "download"
        checkpoint = lock.parent / "run-checkpoint.json"
        try:
            state = json.loads(checkpoint.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("current_stage"), str):
                stage = str(state["current_stage"])
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        active_runs.append({"job_ref": job_ref, "stage": stage})
    if active_runs:
        active_job_ref = active_runs[0]["job_ref"]
        active_stage = active_runs[0]["stage"]
    data = {
        "total": sum(counts.values()),
        "by_status": dict(sorted(counts.items())),
        "sqlite_integrity": integrity,
        "active_stage": active_stage,
        "active_job_ref": active_job_ref,
        "active_runs": active_runs,
        "pending_review": pending_review,
        "publication": publication,
    }
    return success("status", data, summary=f"{data['total']} collection items")


def _plan(root: Path, limit: int) -> dict[str, Any]:
    if not 1 <= limit <= 5:
        raise CliError(
            "invalid_limit",
            "limit must be between 1 and 5",
            "choose a limit from 1 to 5 after a successful canary",
        )
    if limit > 1 and not _canary_completed(root):
        raise CliError(
            "canary_required",
            "batch planning requires a successful current-version canary",
            "run one confirmed no-publish canary before planning a batch",
        )
    database = root / "data" / "knowledge.db"
    items: list[dict[str, Any]] = []
    if database.is_file():
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            if _table_exists(connection, "collection_items"):
                columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(collection_items)")
                }
                if {"job_id", "status"}.issubset(columns):
                    position = "last_position" if "last_position" in columns else "0"
                    collected = "currently_collected" if "currently_collected" in columns else "1"
                    rows = connection.execute(
                        f"SELECT job_id, status, {position} AS position "
                        "FROM collection_items "
                        f"WHERE {collected} = 1 AND status != 'completed' "
                        "ORDER BY position ASC, job_id ASC LIMIT ?",
                        (limit,),
                    ).fetchall()
                    items = [
                        {
                            "job_ref": str(row["job_id"]),
                            "status": str(row["status"]),
                            "position": int(row["position"] or 0),
                        }
                        for row in rows
                    ]
        finally:
            connection.close()
    return success(
        "plan",
        {"limit": limit, "items": items, "publish": False},
        summary=f"planned {len(items)} items without claiming work",
    )


def _canary_path(root: Path) -> Path:
    return root / "data" / "safety" / "canary-v1.json"


def _canary_completed(root: Path) -> bool:
    try:
        payload = json.loads(_canary_path(root).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("status") == "packet_ready"
        and payload.get("version") == __version__
    )


def _record_canary(root: Path, data: dict[str, Any]) -> None:
    _atomic_text(
        _canary_path(root),
        json.dumps(
            {
                "schema_version": 1,
                "version": __version__,
                "status": data.get("status"),
                "job_ref": data.get("job_ref"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
    )


def _publishing_enabled(root: Path) -> bool:
    path = root / "config" / "config.yml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CliError(
            "config_invalid",
            "the runtime configuration could not be read",
            "initialize or repair config/config.yml before publishing",
        ) from exc
    publishing = payload.get("publishing") if isinstance(payload, dict) else None
    return bool(isinstance(publishing, dict) and publishing.get("enabled") is True)


def _doctor(root: Path) -> dict[str, Any]:
    from app.analyze_video import resolve_ffmpeg
    from app.obsidian_publish import configured_vault
    from app.pipeline import _check_playwright_chromium
    from app.security import (
        GateError,
        validate_cookie_file,
        validate_downloader_config,
        windows_acl_metadata,
    )

    database = root / "data" / "knowledge.db"
    config = root / "config"
    cookie = config / "cookies.json"
    initialized = (config / "config.yml").is_file()
    downloader_config_valid = True
    try:
        validate_downloader_config(root)
    except Exception:
        downloader_config_valid = False
    try:
        browser_state = _check_playwright_chromium(active_probe=False)
        browser_runtime = bool(
            browser_state
            and browser_state != "package_available_browser_missing"
            and not str(browser_state).startswith("unavailable:")
        )
    except Exception:
        browser_runtime = False
    try:
        resolve_ffmpeg()
        ffmpeg_available = True
    except Exception:
        ffmpeg_available = False
    snapshots = root / "data" / "models" / "huggingface" / "hub"
    asr_model_present = any(
        snapshots.glob("models--Systran--faster-whisper-*/snapshots/*/model.bin")
    )
    acl = windows_acl_metadata(config)
    acl_private = bool(
        acl.get("exists")
        and (
            os.name != "nt"
            or (
                acl.get("acl_check_returncode") == 0
                and acl.get("access_rules_protected") is True
                and not acl.get("broad_acl_identities")
            )
        )
    )
    database_integrity = "missing"
    if database.is_file():
        try:
            with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
                database_integrity = str(
                    connection.execute("PRAGMA integrity_check").fetchone()[0]
                )
        except sqlite3.Error:
            database_integrity = "failed"
    try:
        vault = configured_vault(root)
        vault_configured = bool(
            vault is not None and vault.is_dir() and (vault / ".obsidian").is_dir()
        )
    except Exception:
        vault_configured = False
    cookie_valid = False
    if cookie.is_file():
        try:
            validate_cookie_file(cookie)
            cookie_valid = True
        except (GateError, OSError):
            cookie_valid = False
    checks = {
        "initialized": initialized,
        "database_present": database.is_file(),
        "database_integrity": database_integrity,
        "downloader_config_valid": downloader_config_valid,
        "cookie_present": cookie.is_file(),
        "cookie_valid": cookie_valid,
        "private_acl": acl_private,
        "structured_schema_present": (
            root / "schemas" / "structured-content-v1.schema.json"
        ).is_file(),
        "playwright_package": importlib.util.find_spec("playwright") is not None,
        "chromium_runtime": browser_runtime,
        "ffmpeg_available": ffmpeg_available,
        "asr_model_present": asr_model_present,
        "vault_configured": vault_configured,
    }
    actions = []
    if not checks["chromium_runtime"]:
        actions.append("install_playwright_chromium")
    if not checks["ffmpeg_available"]:
        actions.append("install_ffmpeg")
    if not checks["cookie_valid"]:
        actions.append("run_confirmed_login")
    if not checks["asr_model_present"]:
        actions.append("install_local_asr_model")
    if not checks["vault_configured"]:
        actions.append("configure_obsidian_vault")
    ready_for_login = bool(
        initialized
        and downloader_config_valid
        and checks["structured_schema_present"]
        and browser_runtime
        and acl_private
    )
    ready_for_sync = ready_for_login and checks["cookie_valid"]
    ready_for_analysis = ffmpeg_available and asr_model_present
    return success(
        "doctor",
        {
            "ready": ready_for_login and database_integrity in {"missing", "ok"},
            "ready_for_login": ready_for_login,
            "ready_for_sync": ready_for_sync,
            "ready_for_analysis": ready_for_analysis,
            "ready_for_publish": vault_configured,
            "checks": checks,
            "repair_actions": actions,
            "version": __version__,
        },
        summary="doctor completed without reading credential contents",
    )


def _confirmation(operation: str) -> None:
    raise CliError(
        "confirmation_required",
        f"{operation} requires explicit confirmation",
        f"review the operation scope, then repeat {operation} with --confirm",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="douyin-knowledge")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "init", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--json", action="store_true")
    plan = subparsers.add_parser("plan")
    plan.add_argument("--limit", type=int, default=1)
    plan.add_argument("--json", action="store_true")
    packet = subparsers.add_parser("packet")
    packet_commands = packet.add_subparsers(dest="packet_command", required=True)
    packet_export = packet_commands.add_parser("export")
    packet_export.add_argument("--job-ref", required=True)
    packet_export.add_argument("--json", action="store_true")
    candidate = subparsers.add_parser("candidate")
    candidate_commands = candidate.add_subparsers(dest="candidate_command", required=True)
    candidate_import = candidate_commands.add_parser("import")
    candidate_import.add_argument("--job-ref", required=True)
    candidate_import.add_argument("--input", type=Path, required=True)
    candidate_import.add_argument("--json", action="store_true")
    candidate_repair = candidate_commands.add_parser("repair-contract")
    candidate_repair.add_argument("--job-ref", required=True)
    candidate_repair.add_argument("--json", action="store_true")
    review = subparsers.add_parser("review")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_list = review_commands.add_parser("list")
    review_list.add_argument("--job-ref")
    review_list.add_argument("--json", action="store_true")
    review_record = review_commands.add_parser("record")
    review_record.add_argument("--job-ref", required=True)
    review_record.add_argument("--decision", choices=("approve", "reject"), required=True)
    review_record.add_argument("--note", default="")
    review_record.add_argument("--json", action="store_true")
    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--job-ref")
    reconcile.add_argument("--json", action="store_true")
    publish = subparsers.add_parser("publish")
    publish.add_argument("--job-ref", required=True)
    publish.add_argument("--idempotency-key")
    publish.add_argument("--confirm", action="store_true")
    publish.add_argument("--json", action="store_true")
    run = subparsers.add_parser("run")
    run.add_argument("--job-ref", required=True)
    run.add_argument(
        "--stop-after", choices=("download", "analysis", "packet", "staging"), required=True
    )
    run.add_argument("--confirm", action="store_true")
    run.add_argument("--retry-after-fix", action="store_true")
    run.add_argument("--json", action="store_true")
    for name in ("login", "sync"):
        command = subparsers.add_parser(name)
        command.add_argument("--confirm", action="store_true")
        command.add_argument("--json", action="store_true")
    canary = subparsers.add_parser("canary")
    canary.add_argument("--limit", type=int, default=1)
    canary.add_argument("--no-publish", action="store_true", required=True)
    canary.add_argument("--confirm", action="store_true")
    canary.add_argument("--json", action="store_true")
    model = subparsers.add_parser("model")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_install = model_commands.add_parser("install")
    model_install.add_argument("--name", choices=("tiny", "base", "small"), default="small")
    model_install.add_argument("--confirm", action="store_true")
    model_install.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except CliError as exc:
        print(json.dumps(failure("cli", exc), ensure_ascii=False, sort_keys=True))
        return exc.exit_code
    root = (args.root or default_instance_root()).expanduser().resolve()
    operation = str(args.command)
    try:
        if operation == "init":
            payload = _init(root)
        elif operation == "status":
            payload = _status(root)
        elif operation == "plan":
            payload = _plan(root, int(args.limit))
        elif operation == "packet":
            data = export_packet(root, str(args.job_ref))
            payload = success(
                "packet_export",
                data,
                summary="semantic packet exported without model execution",
            )
        elif operation == "candidate":
            if args.candidate_command == "import":
                data = import_candidate(root, str(args.job_ref), args.input)
                payload = success(
                    "candidate_import",
                    data,
                    summary="candidate imported and staged through deterministic gates",
                )
            else:
                data = repair_contract(root, str(args.job_ref))
                payload = success(
                    "candidate_repair_contract",
                    data,
                    summary="bounded candidate repair contract generated",
                )
        elif operation == "review":
            database = root / "data" / "knowledge.db"
            if args.review_command == "record":
                data = record_review(
                    root,
                    database,
                    job_ref=str(args.job_ref),
                    decision=str(args.decision),
                    note=str(args.note),
                )
                payload = success("review_record", data, summary="review decision recorded")
            else:
                items = list_reviews(database, job_ref=args.job_ref)
                payload = success(
                    "review_list",
                    {"items": items},
                    summary=f"listed {len(items)} review decisions",
                )
        elif operation == "reconcile":
            items = reconcile_publications(
                root, root / "data" / "knowledge.db", job_ref=args.job_ref
            )
            payload = success(
                "reconcile",
                {"items": items},
                summary=f"reconciled {len(items)} publication transactions",
            )
        elif operation == "run":
            if not args.confirm:
                _confirmation(operation)
            data = run_job(
                root,
                job_ref=str(args.job_ref),
                stop_after=str(args.stop_after),
                retry_after_fix=bool(args.retry_after_fix),
            )
            payload = success(
                "run",
                data,
                summary=f"job stopped safely after {args.stop_after}",
            )
        elif operation == "publish":
            if not args.confirm:
                _confirmation(operation)
            if not _publishing_enabled(root):
                raise CliError(
                    "publishing_disabled",
                    "publishing is disabled in the runtime configuration",
                    "set publishing.enabled to true after reviewing the configured targets",
                )
            data = publish_reviewed_job(
                root,
                job_ref=str(args.job_ref),
                idempotency_key=args.idempotency_key,
            )
            payload = success(
                "publish",
                data,
                summary="publication verified and accepted",
            )
        elif operation == "login":
            if not args.confirm:
                _confirmation(operation)
            payload = success(
                "login",
                platform_login(root),
                summary="interactive login completed and credentials were validated",
            )
        elif operation == "sync":
            if not args.confirm:
                _confirmation(operation)
            payload = success(
                "sync",
                platform_sync(root),
                summary="collection snapshot synchronized without downloading media",
            )
        elif operation == "canary":
            if not args.confirm:
                _confirmation(operation)
            if int(args.limit) != 1:
                raise CliError(
                    "canary_limit_invalid",
                    "canary limit must be exactly one",
                    "run one no-publish canary before any batch",
                )
            planned = _plan(root, 1)["data"]["items"]
            data = (
                {"status": "no_work", "items": [], "publish": False, "model_calls": 0}
                if not planned
                else run_job(
                    root,
                    job_ref=str(planned[0]["job_ref"]),
                    stop_after="packet",
                )
            )
            if data.get("status") == "packet_ready":
                _record_canary(root, data)
            payload = success(
                "canary",
                data,
                summary="single no-publish canary stopped at the semantic packet",
            )
        elif operation == "model":
            if not args.confirm:
                _confirmation("model install")
            data = install_asr_model(root, name=str(args.name))
            payload = success(
                "model_install",
                data,
                summary="local ASR model installed for offline inference",
            )
        else:
            payload = _doctor(root)
        exit_code = 0
    except CliError as exc:
        payload = failure(operation, exc)
        exit_code = exc.exit_code
    except PublicationStateError as exc:
        error = CliError(
            exc.code,
            str(exc),
            "inspect publication status and retry only the same job",
        )
        payload = failure(operation, error)
        exit_code = error.exit_code
    except Exception:
        error = CliError(
            "internal_error",
            "the operation failed without changing the preserved checkpoint",
            "inspect the private local log and retry only after correcting the cause",
            retryable=False,
            exit_code=1,
        )
        payload = failure(operation, error)
        exit_code = error.exit_code
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
