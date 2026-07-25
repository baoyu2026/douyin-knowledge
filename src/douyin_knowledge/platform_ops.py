from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.collection_registry import (
    begin_snapshot,
    complete_snapshot,
    ingest_snapshot_page,
    pause_snapshot,
)
from app.probe_one import fetch_one_collected_video
from app.security import (
    GateError,
    allowed_cookie_path,
    harden_private_project_directory,
    load_cookie_values,
    login_block_path,
    login_preflight,
    sync_preflight,
    validate_cookie_file,
)
from douyin_knowledge.contracts import CliError
from douyin_knowledge.paths import repository_root

ALLOWED_ASR_MODELS = frozenset({"tiny", "base", "small"})


def _gate_error(operation: str, exc: Exception) -> CliError:
    return CliError(
        f"{operation}_preflight_failed",
        f"{operation} security preflight did not pass",
        "run doctor and correct the reported local prerequisites",
    )


def _sync_favorite_projection(root: Path) -> dict[str, Any]:
    """Update the optional Vault projection without invalidating a completed snapshot."""
    from app.obsidian_publish import configured_vault, sync_favorite_states

    try:
        vault = configured_vault(root)
        if vault is None:
            return {"favorite_state_sync": "not_configured", "favorite_state_updates": 0}
        updates = sync_favorite_states(root, vault)
    except Exception:
        return {"favorite_state_sync": "blocked", "favorite_state_updates": 0}
    return {"favorite_state_sync": "completed", "favorite_state_updates": updates}


def login(root: Path) -> dict[str, Any]:
    root = root.resolve()
    temporary = root / "config" / "cookies.json.tmp"
    blocker = login_block_path(root)
    cookie = allowed_cookie_path(root)
    try:
        for name in ("config", "data", "output", "logs"):
            harden_private_project_directory(root, root / name)
        login_preflight(root)
    except GateError as exc:
        raise _gate_error("login", exc) from exc
    blocker.write_text("login replacement is incomplete; sync is blocked\n", encoding="ascii")
    temporary.unlink(missing_ok=True)
    vendor = repository_root() / "vendor" / "douyin-downloader"
    working_directory = vendor if vendor.is_dir() else None
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "tools.cookie_fetcher", "--output", str(temporary)],
            cwd=working_directory,
            stdin=None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=900,
        )
        if completed.returncode != 0:
            raise CliError(
                "login_failed",
                "interactive login did not complete",
                "close the browser flow, run doctor, and retry login explicitly",
            )
        validate_cookie_file(temporary)
        os.replace(temporary, cookie)
        harden_private_project_directory(root, root / "config")
        validate_cookie_file(cookie)
        blocker.unlink(missing_ok=True)
    except CliError:
        raise
    except (GateError, OSError, subprocess.SubprocessError) as exc:
        raise CliError(
            "login_failed",
            "interactive login did not complete",
            "run doctor and retry login explicitly",
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {"authenticated": True, "cookie_keys_validated": True}


async def _sync_collection(root: Path) -> dict[str, Any]:
    database = root / "data" / "knowledge.db"
    cookies = load_cookie_values(allowed_cookie_path(root))
    snapshot = begin_snapshot(database)
    seen = 0

    async def page_handler(data: dict[str, Any], cursor: str) -> None:
        nonlocal seen
        raw = data.get("aweme_list")
        items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        seen += ingest_snapshot_page(
            database,
            snapshot_id=snapshot.snapshot_id,
            cursor=cursor,
            next_cursor=data.get("cursor"),
            has_more=bool(data.get("has_more")),
            items=items,
        )

    try:
        result = await fetch_one_collected_video(
            cookies,
            position=1_000_000_000,
            page_handler=page_handler,
        )
        if result.reason != "position_not_available" or result.has_more is not False:
            raise CliError(
                "sync_incomplete",
                "collection snapshot did not reach its final page",
                "retry sync once after checking the login state",
            )
        total = complete_snapshot(database, snapshot.snapshot_id)
    except Exception as exc:
        try:
            pause_snapshot(database, snapshot.snapshot_id, "public_sync_interrupted")
        except Exception:
            pass
        if isinstance(exc, CliError):
            raise
        raise CliError(
            "sync_failed",
            "collection synchronization stopped safely",
            "inspect the private log and retry sync once",
        ) from exc
    return {
        "snapshot_items": total,
        "new_snapshot_items": seen,
        "downloaded": 0,
        **_sync_favorite_projection(root),
    }


def sync(root: Path) -> dict[str, Any]:
    root = root.resolve()
    try:
        sync_preflight(root)
    except GateError as exc:
        raise _gate_error("sync", exc) from exc
    return asyncio.run(_sync_collection(root))


def sqlite_integrity(root: Path) -> bool:
    database = root / "data" / "knowledge.db"
    if not database.is_file():
        return True
    try:
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
            return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def install_asr_model(root: Path, *, name: str) -> dict[str, Any]:
    if name not in ALLOWED_ASR_MODELS:
        raise CliError(
            "asr_model_invalid",
            "ASR model must be tiny, base, or small",
            "choose a supported bounded local model",
        )
    root = root.resolve()
    cache = root / "data" / "models" / "huggingface" / "hub"
    try:
        harden_private_project_directory(root, root / "data")
        from huggingface_hub import snapshot_download

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            snapshot = Path(
                snapshot_download(
                    repo_id=f"Systran/faster-whisper-{name}",
                    cache_dir=cache,
                    local_files_only=False,
                )
            )
        required = ("config.json", "model.bin", "tokenizer.json")
        if not all((snapshot / filename).is_file() for filename in required):
            raise OSError("model snapshot is incomplete")
    except Exception as exc:
        if isinstance(exc, CliError):
            raise
        raise CliError(
            "asr_model_install_failed",
            "the local ASR model download did not complete",
            "check network access and free disk space before retrying",
        ) from exc
    return {"name": name, "installed": True, "local_only_ready": True}
