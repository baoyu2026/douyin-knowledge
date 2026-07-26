from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from app.collection_registry import ensure_collection_schema
from app.security import (
    GateError,
    allowed_cookie_path,
    login_block_path,
    metadata_report,
    unexpected_cookie_candidates,
    validate_downloader_config,
    windows_acl_metadata,
)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".flv"}


def path_metadata(path: Path) -> dict[str, object]:
    try:
        path_stat = path.stat()
    except FileNotFoundError:
        return {"exists": False, "is_file": False, "size_bytes": 0}
    except OSError as exc:
        return {
            "exists": False,
            "is_file": False,
            "size_bytes": 0,
            "access_error": str(exc),
        }
    return {
        "exists": True,
        "is_file": stat.S_ISREG(path_stat.st_mode),
        "size_bytes": path_stat.st_size,
    }


def connect(db_path: Path, *, initialize: bool = True) -> sqlite3.Connection:
    if initialize:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    if initialize:
        ensure_schema(connection)
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS media_jobs (
            id INTEGER PRIMARY KEY,
            media_path TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL,
            modified_ns INTEGER NOT NULL,
            content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'discovered',
            transcript_path TEXT,
            summary_path TEXT,
            error TEXT,
            discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_scan TEXT
        )
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(media_jobs)")}
    for name, definition in {
        "content_hash": "TEXT",
        "last_seen_scan": "TEXT",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE media_jobs ADD COLUMN {name} {definition}")
    ensure_collection_schema(connection)
    connection.commit()


def iter_videos(download_dir: Path) -> Iterable[Path]:
    if not download_dir.exists():
        return []
    return (
        path
        for path in download_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(download_dir: Path, db_path: Path) -> dict[str, int]:
    found = 0
    inserted = 0
    updated = 0
    missing = 0
    scan_id = datetime.now(UTC).isoformat()
    with connect(db_path) as connection:
        for media_path in iter_videos(download_dir):
            found += 1
            stat = media_path.stat()
            normalized = str(media_path.resolve())
            content_hash = file_hash(media_path)
            existing = connection.execute(
                """
                SELECT size_bytes, modified_ns, content_hash
                FROM media_jobs WHERE media_path = ?
                """,
                (normalized,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO media_jobs (
                        media_path, size_bytes, modified_ns, content_hash, last_seen_scan
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (normalized, stat.st_size, stat.st_mtime_ns, content_hash, scan_id),
                )
                inserted += 1
            elif existing != (stat.st_size, stat.st_mtime_ns, content_hash):
                connection.execute(
                    """
                    UPDATE media_jobs
                    SET size_bytes = ?, modified_ns = ?, content_hash = ?,
                        status = 'discovered', transcript_path = NULL,
                        summary_path = NULL, error = NULL,
                        last_seen_scan = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE media_path = ?
                    """,
                    (stat.st_size, stat.st_mtime_ns, content_hash, scan_id, normalized),
                )
                updated += 1
            else:
                connection.execute(
                    """
                    UPDATE media_jobs
                    SET last_seen_scan = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE media_path = ?
                    """,
                    (scan_id, normalized),
                )
        missing = connection.execute(
            """
            UPDATE media_jobs
            SET status = 'missing', error = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE (last_seen_scan IS NULL OR last_seen_scan != ?) AND status != 'missing'
            """,
            (scan_id,),
        ).rowcount
        connection.commit()
    return {"found": found, "inserted": inserted, "updated": updated, "missing": missing}


def status(db_path: Path) -> dict[str, object]:
    if not db_path.exists():
        return {"total": 0, "by_status": {}}
    with connect(db_path, initialize=False) as connection:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_jobs'"
        ).fetchone()
        if table_exists is None:
            return {"total": 0, "by_status": {}}
        rows = connection.execute(
            "SELECT status, COUNT(*) FROM media_jobs GROUP BY status ORDER BY status"
        ).fetchall()
    counts = Counter({name: count for name, count in rows})
    return {"total": sum(counts.values()), "by_status": dict(counts)}


def _check_ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run(
            [ffmpeg_path, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=10,
        )
        return ffmpeg_path
    except Exception as exc:  # pragma: no cover - environment dependent
        return f"unavailable: {exc}"


def _playwright_package_root() -> Path:
    import playwright

    return Path(playwright.__file__).resolve().parent / "driver" / "package"


def _playwright_chromium_revision() -> str:
    browsers_json = _playwright_package_root() / "browsers.json"
    metadata = json.loads(browsers_json.read_text(encoding="utf-8"))
    for browser in metadata.get("browsers", []):
        if browser.get("name") == "chromium":
            return str(browser["revision"])
    raise RuntimeError("Playwright browsers.json does not list chromium")


def _default_playwright_registry() -> Path:
    env_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_root == "0":
        return _playwright_package_root() / ".local-browsers"
    if env_root:
        return Path(env_root).expanduser()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        return Path(local_app_data) / "ms-playwright"
    cache_root = os.environ.get("XDG_CACHE_HOME")
    if cache_root:
        return Path(cache_root) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _playwright_platform_layouts(
    platform: str, operating_system: str
) -> tuple[tuple[str, ...], ...]:
    if platform == "darwin":
        executable = (
            "Google Chrome for Testing.app",
            "Contents",
            "MacOS",
            "Google Chrome for Testing",
        )
        return (
            ("chrome-mac-arm64", *executable),
            ("chrome-mac-x64", *executable),
            ("chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
        )
    if operating_system == "nt":
        return (
            ("chrome-win64", "chrome.exe"),
            ("chrome-win", "chrome.exe"),
            ("chrome-win32", "chrome.exe"),
        )
    return (("chrome-linux64", "chrome"), ("chrome-linux", "chrome"))


def _playwright_browser_candidates() -> list[Path]:
    registry = _default_playwright_registry()
    revision = _playwright_chromium_revision()
    platform_layouts = _playwright_platform_layouts(sys.platform, os.name)
    browser_root = registry / f"chromium-{revision}"
    return [browser_root / Path(*parts) for parts in platform_layouts]


def _check_playwright_chromium(*, active_probe: bool = False) -> str:
    try:
        import playwright  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment dependent
        return f"unavailable: {exc}"

    candidates = [path for path in _playwright_browser_candidates() if path.exists()]
    if not candidates:
        return "package_available_browser_missing"
    if not active_probe:
        return str(candidates[0])

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:  # pragma: no cover - environment dependent
        return f"unavailable: {exc}"
    return "available_active_probe"


def doctor(root: Path, *, active_probe: bool = False) -> tuple[dict[str, object], bool]:
    config_path = root / "config" / "downloader.yml"
    cookie_path = allowed_cookie_path(root)
    download_dir = root / "data" / "downloads"
    unexpected = unexpected_cookie_candidates(root)
    config_metadata = path_metadata(config_path)
    cookie_metadata = path_metadata(cookie_path)
    download_metadata = path_metadata(download_dir)
    login_block_metadata = path_metadata(login_block_path(root))
    checks: dict[str, object] = {
        "root": str(root),
        "config": config_metadata["exists"],
        "config_access_error": config_metadata.get("access_error"),
        "cookies": cookie_metadata["exists"],
        "cookie_is_file": cookie_metadata["is_file"],
        "cookie_size_bytes": cookie_metadata["size_bytes"],
        "cookie_access_error": cookie_metadata.get("access_error"),
        "download_dir": download_metadata["exists"],
        "download_dir_access_error": download_metadata.get("access_error"),
        "unexpected_cookie_candidates": [str(path) for path in unexpected],
        "login_blocked": login_block_metadata["exists"],
        "login_block_access_error": login_block_metadata.get("access_error"),
        "active_probe": active_probe,
    }

    config_ok = False
    transcript_enabled = False
    try:
        config = validate_downloader_config(root)
        config_ok = True
        transcript_enabled = bool((config.get("transcript") or {}).get("enabled"))
        checks["config_parseable"] = True
    except GateError as exc:
        checks["config_parseable"] = False
        checks["config_error"] = str(exc)

    checks["cookie_acl"] = windows_acl_metadata(cookie_path)
    checks["playwright_chromium"] = _check_playwright_chromium(active_probe=active_probe)
    checks["ffmpeg"] = _check_ffmpeg() if transcript_enabled else "not_required"

    security = metadata_report(root)
    acl_checks = security["acl"]
    acl_ready = all(
        item.get("exists")
        and (
            item.get("platform") != "win32"
            or (
                item.get("acl_check_returncode") == 0
                and item.get("access_rules_protected") is True
                and not item.get("broad_acl_identities")
            )
        )
        for item in acl_checks
    )
    checks["acl_ready"] = acl_ready

    login_ready = bool(
        config_ok
        and acl_ready
        and not unexpected
        and not str(checks["playwright_chromium"]).startswith("unavailable:")
        and checks["playwright_chromium"] != "package_available_browser_missing"
    )
    sync_ready = bool(
        config_ok
        and checks["cookies"]
        and checks["cookie_is_file"]
        and checks["cookie_size_bytes"]
        and acl_ready
        and not checks["login_blocked"]
        and not unexpected
        and download_dir.exists()
    )
    transcribe_ready = bool(
        sync_ready
        and (not transcript_enabled or not str(checks["ffmpeg"]).startswith("unavailable:"))
    )
    checks["login_ready"] = login_ready
    checks["sync_ready"] = sync_ready
    checks["transcribe_ready"] = transcribe_ready
    checks["security_metadata"] = security
    ready = login_ready and (sync_ready or not cookie_metadata["exists"]) and transcribe_ready
    return checks, ready


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Douyin knowledge pipeline")
    parser.add_argument(
        "command", choices=("doctor", "inventory", "status"), help="operation to run"
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1], help="project root"
    )
    parser.add_argument(
        "--active-probe",
        action="store_true",
        help="launch browser for doctor probe",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    db_path = root / "data" / "knowledge.db"

    if args.command == "doctor":
        result, _ready = doctor(root, active_probe=args.active_probe)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "inventory":
        result = inventory(root / "data" / "downloads", db_path)
    else:
        result = status(db_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
