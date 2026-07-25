from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.security import (
    GateError,
    allowed_cookie_path,
    load_cookie_values,
    sync_preflight,
    validate_downloader_config,
)


@contextlib.contextmanager
def vendor_imports(root: Path):
    vendor_root = root / "vendor" / "douyin-downloader"
    if not vendor_root.is_dir():
        vendor_root = Path(__file__).resolve().parents[1] / "vendor" / "douyin-downloader"
    previous_cwd = Path.cwd()
    old_path = list(sys.path)
    if vendor_root.is_dir():
        sys.path.insert(0, str(vendor_root))
        os.chdir(vendor_root)
    try:
        yield
    finally:
        os.chdir(previous_cwd)
        sys.path[:] = old_path


async def download_without_relogin(
    download: Callable[..., Awaitable[Any]],
    *,
    login_required_error: type[Exception],
    url: str,
    config: Any,
    cookie_manager: Any,
    database: Any,
    progress_reporter: Any,
) -> Any:
    try:
        return await download(
            url,
            config,
            cookie_manager,
            database,
            progress_reporter=progress_reporter,
        )
    except login_required_error as exc:
        raise GateError("登录态失效；同步已停止，不会自动打开浏览器或写入新 Cookie") from exc


async def run_sync(root: Path) -> int:
    root = root.resolve()
    sync_preflight(root)
    config_path = root / "config" / "downloader.yml"
    config_data = validate_downloader_config(root)
    download_path = (root / config_data["path"]).resolve()
    database_path = (root / config_data["database_path"]).resolve()
    cookie_values = load_cookie_values(allowed_cookie_path(root))

    with vendor_imports(root):
        from auth import CookieManager
        from cli import main as vendor_main
        from core import LoginRequiredError
        from storage import Database

        from config import ConfigLoader

        config = ConfigLoader(str(config_path))
        config.update(
            auto_cookie=False,
            cookie=None,
            cookies=cookie_values,
            path=str(download_path),
            database_path=str(database_path),
            thread=config_data["thread"],
            rate_limit=config_data["rate_limit"],
            retry_times=config_data["retry_times"],
            proxy=config_data["proxy"],
        )
        cookie_manager = CookieManager(str(allowed_cookie_path(root)))
        cookie_manager.cookies = cookie_values
        if not cookie_manager.validate_cookies():
            raise GateError("Cookie 缺少上游必需键")

        database = None
        if config.get("database"):
            database = Database(db_path=str(database_path))
            await database.initialize()
            vendor_main.display.print_success("Database initialized")

        urls = config.get_links()
        vendor_main.display.print_info(f"Found {len(urls)} URL(s) to process")
        all_results: list[Any] = []
        vendor_main.display.start_download_session(len(urls))
        try:
            for index, url in enumerate(urls, 1):
                vendor_main.display.start_url(index, len(urls), url)
                result = await download_without_relogin(
                    vendor_main.download_url,
                    login_required_error=LoginRequiredError,
                    url=url,
                    config=config,
                    cookie_manager=cookie_manager,
                    database=database,
                    progress_reporter=vendor_main.display,
                )
                if result:
                    all_results.append(result)
                    vendor_main.display.complete_url(result)
                else:
                    vendor_main.display.fail_url("下载失败或链接无效")
        finally:
            vendor_main.display.stop_download_session()
            if database is not None:
                await database.close()

        return 0 if all_results else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project-side non-interactive sync entry")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        root = args.root.resolve()
        sync_preflight(root)
        if args.preflight_only:
            print(f"Preflight OK. Cookie path: {allowed_cookie_path(root)}")
            return 0
        return asyncio.run(run_sync(root))
    except GateError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[ERROR] 同步运行异常 ({type(exc).__name__})；已安全停止", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
