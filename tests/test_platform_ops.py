from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.security import GateError
from douyin_knowledge.contracts import CliError
from douyin_knowledge.operations import _run_private, run_job
from douyin_knowledge.paths import default_instance_root, repository_root
from douyin_knowledge.platform_ops import (
    _sync_collection,
    _sync_favorite_projection,
    install_asr_model,
    login,
    sqlite_integrity,
    sync,
)


def test_login_atomically_validates_cookie_without_returning_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("config", "data", "output", "logs"):
        (tmp_path / name).mkdir()
    vendor = tmp_path / "code" / "vendor" / "douyin-downloader"
    vendor.mkdir(parents=True)
    monkeypatch.setattr(
        "douyin_knowledge.platform_ops.harden_private_project_directory",
        lambda _root, _path: None,
    )
    monkeypatch.setattr("douyin_knowledge.platform_ops.login_preflight", lambda _root: None)
    monkeypatch.setattr("douyin_knowledge.platform_ops.repository_root", lambda: tmp_path / "code")

    def fake_run(command, **_kwargs):
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "ttwid": "private-one",
                    "odin_tt": "private-two",
                    "passport_csrf_token": "private-three",
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("douyin_knowledge.platform_ops.subprocess.run", fake_run)

    result = login(tmp_path)

    assert result == {"authenticated": True, "cookie_keys_validated": True}
    assert (tmp_path / "config" / "cookies.json").is_file()
    assert not (tmp_path / "config" / "cookie-login.blocked").exists()
    assert "private" not in str(result)


def test_login_maps_security_preflight_to_stable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "douyin_knowledge.platform_ops.harden_private_project_directory",
        lambda _root, _path: None,
    )

    def reject(_root):
        raise GateError("private diagnostic")

    monkeypatch.setattr("douyin_knowledge.platform_ops.login_preflight", reject)

    with pytest.raises(CliError) as error:
        login(tmp_path)
    assert error.value.code == "login_preflight_failed"
    assert "private diagnostic" not in error.value.message


def test_login_maps_browser_failure_without_exposing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("config", "data", "output", "logs"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(
        "douyin_knowledge.platform_ops.harden_private_project_directory",
        lambda _root, _path: None,
    )
    monkeypatch.setattr("douyin_knowledge.platform_ops.login_preflight", lambda _root: None)
    monkeypatch.setattr(
        "douyin_knowledge.platform_ops.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="private browser output", stderr="private error"
        ),
    )

    with pytest.raises(CliError) as error:
        login(tmp_path)
    assert error.value.code == "login_failed"
    assert "private" not in error.value.message


def test_sync_wrapper_runs_only_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("douyin_knowledge.platform_ops.sync_preflight", lambda _root: None)

    async def fake_sync(_root):
        return {"snapshot_items": 2, "new_snapshot_items": 1, "downloaded": 0}

    monkeypatch.setattr("douyin_knowledge.platform_ops._sync_collection", fake_sync)
    assert sync(tmp_path)["downloaded"] == 0


def test_sync_maps_preflight_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(_root):
        raise GateError("private sync diagnostic")

    monkeypatch.setattr("douyin_knowledge.platform_ops.sync_preflight", reject)
    with pytest.raises(CliError) as error:
        sync(tmp_path)
    assert error.value.code == "sync_preflight_failed"
    assert "private sync diagnostic" not in error.value.message


def test_collection_sync_consumes_all_pages_without_media_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "cookies.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "douyin_knowledge.platform_ops.load_cookie_values", lambda _path: {"cookie": "private"}
    )
    monkeypatch.setattr(
        "douyin_knowledge.platform_ops.begin_snapshot",
        lambda _database: SimpleNamespace(snapshot_id="snapshot-one"),
    )
    observed: list[tuple[str, int]] = []

    def ingest(_database, **kwargs):
        observed.append((kwargs["cursor"], len(kwargs["items"])))
        return len(kwargs["items"])

    monkeypatch.setattr("douyin_knowledge.platform_ops.ingest_snapshot_page", ingest)
    monkeypatch.setattr("douyin_knowledge.platform_ops.complete_snapshot", lambda *_args: 3)

    async def fetch(_cookies, position, page_handler):
        assert position == 1_000_000_000
        await page_handler(
            {"aweme_list": [{"aweme_id": "one"}], "cursor": "next", "has_more": True},
            "0",
        )
        await page_handler(
            {
                "aweme_list": [{"aweme_id": "two"}, {"aweme_id": "three"}],
                "cursor": "end",
                "has_more": False,
            },
            "next",
        )
        return SimpleNamespace(reason="position_not_available", has_more=False)

    monkeypatch.setattr("douyin_knowledge.platform_ops.fetch_one_collected_video", fetch)

    result = asyncio.run(_sync_collection(tmp_path))

    assert result == {
        "snapshot_items": 3,
        "new_snapshot_items": 3,
        "downloaded": 0,
        "favorite_state_sync": "not_configured",
        "favorite_state_updates": 0,
    }
    assert observed == [("0", 1), ("next", 2)]


def test_optional_favorite_projection_updates_or_blocks_without_failing_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    monkeypatch.setattr("app.obsidian_publish.configured_vault", lambda _root: vault)
    monkeypatch.setattr("app.obsidian_publish.sync_favorite_states", lambda _root, _vault: 2)

    assert _sync_favorite_projection(tmp_path) == {
        "favorite_state_sync": "completed",
        "favorite_state_updates": 2,
    }

    def fail_projection(_root, _vault):
        raise OSError("private vault failure")

    monkeypatch.setattr("app.obsidian_publish.sync_favorite_states", fail_projection)
    assert _sync_favorite_projection(tmp_path) == {
        "favorite_state_sync": "blocked",
        "favorite_state_updates": 0,
    }


def test_sqlite_integrity_and_private_subprocess_error_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sqlite_integrity(tmp_path) is True
    database = tmp_path / "data" / "knowledge.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fixture(value TEXT)")
    assert sqlite_integrity(tmp_path) is True

    monkeypatch.setattr(
        "douyin_knowledge.operations.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=4),
    )
    with pytest.raises(CliError) as error:
        _run_private(["private-command"], stage="analysis", timeout=1)
    assert error.value.code == "analysis_failed"


def test_private_subprocess_uses_utf8_and_discards_input_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="private output", stderr="private error")

    monkeypatch.setattr("douyin_knowledge.operations.subprocess.run", fake_run)

    assert _run_private(["private-command"], stage="analysis", timeout=7) is None
    assert observed["command"] == ["private-command"]
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"
    assert observed["check"] is False
    assert observed["timeout"] == 7


@pytest.mark.parametrize("exception", [OSError("private path"), TimeoutError("private wait")])
def test_private_subprocess_maps_runtime_exceptions_without_exposing_details(
    monkeypatch: pytest.MonkeyPatch, exception: Exception
) -> None:
    def fail(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr("douyin_knowledge.operations.subprocess.run", fail)

    with pytest.raises(CliError) as error:
        _run_private(["private-command"], stage="download", timeout=1)

    assert error.value.code == "download_failed"
    assert error.value.retryable is True
    assert "private" not in error.value.message


def test_default_instance_root_honors_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured"
    monkeypatch.setenv("DOUYIN_KNOWLEDGE_ROOT", str(configured))
    assert default_instance_root() == configured.resolve()
    monkeypatch.delenv("DOUYIN_KNOWLEDGE_ROOT")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert default_instance_root() == (tmp_path / "local" / "douyin-knowledge").resolve()
    assert repository_root().name == "douyin-knowledge-public"


def test_run_job_reuses_bound_download_and_stops_exactly_there(tmp_path: Path) -> None:
    job_ref = "aweme-0123456789abcdefabcd"
    database = tmp_path / "data" / "knowledge.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE collection_items("
            "source_id TEXT, job_id TEXT, status TEXT, last_position INTEGER)"
        )
        connection.execute(
            "INSERT INTO collection_items VALUES ('private-source', ?, 'downloaded', 1)",
            (job_ref,),
        )
    source = tmp_path / "data" / "jobs" / job_ref / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"local media")

    result = run_job(tmp_path, job_ref=job_ref, stop_after="download")

    assert result["status"] == "downloaded"
    assert result["download_reused"] is True
    assert result["model_calls"] == 0
    assert result["publish"] is False
    checkpoint = json.loads(
        (tmp_path / "data" / "tasks" / job_ref / "run-checkpoint.json").read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["status"] == "stopped"
    assert checkpoint["stop_after"] == "download"
    assert checkpoint["stages"]["download"]["status"] == "completed"


def test_run_job_rejects_active_lease_and_quarantines_stale_lock(
    tmp_path: Path,
) -> None:
    job_ref = "aweme-0123456789abcdefabcd"
    database = tmp_path / "data" / "knowledge.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE collection_items("
            "source_id TEXT, job_id TEXT, status TEXT, last_position INTEGER)"
        )
        connection.execute(
            "INSERT INTO collection_items VALUES ('private-source', ?, 'downloaded', 1)",
            (job_ref,),
        )
    source = tmp_path / "data" / "jobs" / job_ref / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"local media")
    lock = tmp_path / "data" / "tasks" / job_ref / "run.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("{}", encoding="utf-8")

    with pytest.raises(CliError) as active:
        run_job(tmp_path, job_ref=job_ref, stop_after="download")
    assert active.value.code == "run_locked"

    old = lock.stat().st_mtime - 3 * 60 * 60
    os.utime(lock, (old, old))
    assert run_job(tmp_path, job_ref=job_ref, stop_after="download")["status"] == "downloaded"
    assert len(list((tmp_path / "quarantine" / "run-locks" / job_ref).glob("*.lock"))) == 1


def test_model_install_verifies_bounded_local_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "douyin_knowledge.platform_ops.harden_private_project_directory",
        lambda _root, path: path.mkdir(parents=True, exist_ok=True),
    )
    snapshot = tmp_path / "downloaded-model"

    def fake_download(**kwargs):
        assert kwargs["repo_id"] == "Systran/faster-whisper-small"
        assert kwargs["local_files_only"] is False
        snapshot.mkdir()
        for name in ("config.json", "model.bin", "tokenizer.json"):
            (snapshot / name).write_text("fixture", encoding="utf-8")
        return str(snapshot)

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_download)

    assert install_asr_model(tmp_path, name="small") == {
        "name": "small",
        "installed": True,
        "local_only_ready": True,
    }
    with pytest.raises(CliError) as error:
        install_asr_model(tmp_path, name="large-v3")
    assert error.value.code == "asr_model_invalid"
