import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from app.pipeline import _playwright_platform_layouts, doctor, inventory, status
from app.security import (
    GateError,
    block_unexpected_cookie_candidates,
    login_preflight,
    sync_preflight,
    validate_downloader_config,
)
from app.sync_entry import download_without_relogin
from app.sync_entry import main as sync_main


def write_config(root: Path) -> None:
    config = root / "config"
    config.mkdir()
    (config / "downloader.yml").write_text(
        """
link:
  - https://www.douyin.com/user/self?showTab=favorite_collection
path: data/downloads
mode:
  - collect
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
""".strip(),
        encoding="utf-8",
    )


def test_playwright_layouts_cover_current_and_legacy_platform_names() -> None:
    windows = _playwright_platform_layouts("win32", "nt")
    linux = _playwright_platform_layouts("linux", "posix")
    macos = _playwright_platform_layouts("darwin", "posix")

    assert ("chrome-win64", "chrome.exe") in windows
    assert ("chrome-win", "chrome.exe") in windows
    assert ("chrome-linux64", "chrome") in linux
    assert ("chrome-linux", "chrome") in linux
    assert any(parts[0] == "chrome-mac-arm64" for parts in macos)
    assert any(parts[0] == "chrome-mac-x64" for parts in macos)
    assert all(parts[-1] == "Google Chrome for Testing" for parts in macos[:2])


def test_inventory_discovers_video_and_is_idempotent(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "one.mp4").write_bytes(b"video")
    (downloads / "ignore.txt").write_text("not media", encoding="utf-8")
    db_path = tmp_path / "knowledge.db"

    assert inventory(downloads, db_path) == {"found": 1, "inserted": 1, "updated": 0, "missing": 0}
    assert inventory(downloads, db_path) == {"found": 1, "inserted": 0, "updated": 0, "missing": 0}
    assert status(db_path) == {"total": 1, "by_status": {"discovered": 1}}


def test_inventory_marks_changed_video_for_reprocessing_and_clears_artifacts(
    tmp_path: Path,
) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    media = downloads / "one.mp4"
    media.write_bytes(b"first")
    db_path = tmp_path / "knowledge.db"
    inventory(downloads, db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE media_jobs SET status='done', transcript_path='old.txt', summary_path='old.md'"
        )

    media.write_bytes(b"second version")

    assert inventory(downloads, db_path) == {"found": 1, "inserted": 0, "updated": 1, "missing": 0}
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status, transcript_path, summary_path FROM media_jobs"
        ).fetchone()
    assert row == ("discovered", None, None)


def test_inventory_detects_same_size_changed_content_and_missing(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    media = downloads / "one.mp4"
    media.write_bytes(b"aaaa")
    db_path = tmp_path / "knowledge.db"
    inventory(downloads, db_path)

    original_stat = media.stat()
    media.write_bytes(b"bbbb")
    os.utime(media, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert inventory(downloads, db_path)["updated"] == 1

    media.unlink()
    assert inventory(downloads, db_path) == {"found": 0, "inserted": 0, "updated": 0, "missing": 1}
    assert status(db_path) == {"total": 1, "by_status": {"missing": 1}}


def test_status_does_not_create_schema_for_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()
    assert status(db_path) == {"total": 0, "by_status": {}}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_config_rejects_inline_cookie_and_auto_cookie(tmp_path: Path) -> None:
    write_config(tmp_path)
    validate_downloader_config(tmp_path)
    config_path = tmp_path / "config" / "downloader.yml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\ncookies:\n  sessionid: fixture-secret\n",
        encoding="utf-8",
    )
    with pytest.raises(GateError):
        validate_downloader_config(tmp_path)


def test_login_preflight_wraps_config_access_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path)
    for relative in (
        "app/pipeline.py",
        "scripts/login.ps1",
        "vendor/douyin-downloader/run.py",
    ):
        required = tmp_path / relative
        required.parent.mkdir(parents=True, exist_ok=True)
        required.touch()

    config_path = tmp_path / "config" / "downloader.yml"
    original_exists = Path.exists

    def deny_config_access(path: Path) -> bool:
        if path == config_path:
            raise PermissionError("access denied")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", deny_config_access)

    with pytest.raises(GateError, match="无法访问 config/downloader.yml"):
        login_preflight(tmp_path)


def test_blocks_unexpected_cookie_candidates_without_reading(tmp_path: Path) -> None:
    write_config(tmp_path)
    for relative in (
        "app/pipeline.py",
        "scripts/login.ps1",
        "vendor/douyin-downloader/run.py",
    ):
        required = tmp_path / relative
        required.parent.mkdir(parents=True, exist_ok=True)
        required.touch()
    unexpected = tmp_path / "vendor" / "douyin-downloader"
    unexpected.mkdir(parents=True, exist_ok=True)
    (unexpected / ".cookies.json").write_text(
        json.dumps({"secret": "do-not-read"}),
        encoding="utf-8",
    )
    for gate in (
        block_unexpected_cookie_candidates,
        login_preflight,
        sync_preflight,
    ):
        with pytest.raises(GateError, match="存在非预期 Cookie 候选路径"):
            gate(tmp_path)


def test_sync_login_required_stops_without_relogin() -> None:
    class LoginRequiredError(Exception):
        pass

    async def failing_download(*args, **kwargs):
        raise LoginRequiredError("expired")

    with pytest.raises(GateError, match="不会自动打开浏览器"):
        asyncio.run(
            download_without_relogin(
                failing_download,
                login_required_error=LoginRequiredError,
                url="https://example.invalid/video",
                config=object(),
                cookie_manager=object(),
                database=None,
                progress_reporter=None,
            )
        )


def test_sync_main_contains_unexpected_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail_sync(root: Path) -> int:
        raise RuntimeError("sensitive vendor details")

    monkeypatch.setattr("app.sync_entry.sync_preflight", lambda root: None)
    monkeypatch.setattr("app.sync_entry.run_sync", fail_sync)
    monkeypatch.setattr(sys, "argv", ["sync_entry", "--root", "."])

    assert sync_main() == 3
    captured = capsys.readouterr()
    assert "同步运行异常" in captured.err
    assert "RuntimeError" in captured.err
    assert "sensitive vendor details" not in captured.err
    assert "Traceback" not in captured.err
def test_doctor_default_does_not_read_cookie_or_launch_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path)
    data = tmp_path / "data" / "downloads"
    data.mkdir(parents=True)
    (tmp_path / "output").mkdir()
    (tmp_path / "logs").mkdir()
    cookies = tmp_path / "config" / "cookies.json"
    cookies.write_text("not-json-but-doctor-must-not-read", encoding="utf-8")

    acl_paths = [tmp_path / name for name in ("config", "data", "output", "logs")]
    acl_paths.append(cookies)
    security_metadata = {
        "acl": [
            {
                "path": str(path),
                "exists": True,
                "platform": "win32",
                "acl_check_returncode": 0,
                "access_rules_protected": True,
                "broad_acl_identities": [],
            }
            for path in acl_paths
        ]
    }

    def check_browser_without_launch(*, active_probe: bool = False) -> str:
        assert active_probe is False
        return "browser-path"

    def fail_if_cookie_is_read(self, *args, **kwargs):
        if self == cookies:
            raise AssertionError("doctor must not read cookie content")
        return original_read_text(self, *args, **kwargs)

    original_read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", fail_if_cookie_is_read)
    monkeypatch.setattr("app.pipeline.metadata_report", lambda root: security_metadata)
    monkeypatch.setattr("app.pipeline._check_playwright_chromium", check_browser_without_launch)

    result, ready = doctor(tmp_path)

    assert result["login_ready"] is True
    assert result["sync_ready"] is True
    assert result["transcribe_ready"] is True
    assert result["cookie_size_bytes"] > 0
    assert "cookie_key_count" not in result
    assert ready is True
