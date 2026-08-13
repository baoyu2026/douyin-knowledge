from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from app.collection_registry import (
    PIPELINE_VERSION,
    CollectionRegistry,
    begin_snapshot,
    claim_item,
    complete_snapshot,
    ingest_snapshot_page,
    pause_snapshot,
    stable_job_id_for_source,
    update_item_by_job,
)
from app.security import (
    GateError,
    allowed_cookie_path,
    load_cookie_values,
    resolve_project_path,
    sync_preflight,
    validate_downloader_config,
)

CONTROLLED_FAILURE_EXIT = 4
COLLECT_VIDEO_SOURCE = "collect-video"
MAX_MEDIA_BYTES = 1024 * 1024 * 1024
MAX_REDIRECTS = 3
DOWNLOAD_CHUNK_BYTES = 256 * 1024
PROBE_RELATIVE_DIR = Path("data/probe-collect")
JOBS_RELATIVE_DIR = Path("data/jobs")
COLLECTION_PAGE_URL = "https://www.douyin.com/user/self?showTab=favorite_collection"
COLLECTION_API_PATH = "/aweme/v1/web/aweme/listcollection/"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

# Direct play_addr URLs currently use these Douyin/ByteDance CDN families. A URL
# outside them is rejected before any media request, including after redirects.
ALLOWED_MEDIA_HOST_SUFFIXES = (
    "amemv.com",
    "bytecdn.cn",
    "byteimg.com",
    "bytedance.com",
    "bytedance.net",
    "douyin.com",
    "douyinpic.com",
    "douyinvod.com",
    "ixigua.com",
    "pstatp.com",
    "snssdk.com",
    "zijieapi.com",
    "zjcdn.com",
)
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MANIFEST_CONTENT_TYPES = {
    "application/dash+xml",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
}
PROTECTED_MEDIA_MARKERS = (b"pssh", b"sinf", b"schm", b"encv", b"enca")
MIN_PROTECTED_BOX_SIZES = {
    b"pssh": 32,
    b"sinf": 8,
    b"schm": 20,
    b"encv": 8,
    b"enca": 8,
}


@dataclass(frozen=True)
class CollectResult:
    item: dict[str, Any] | None
    reason: str | None = None
    position: int | None = None
    cursor: int | str | None = None
    has_more: bool | None = None


@dataclass(frozen=True)
class FetchResult:
    http_status: int | None
    readable: bool
    saved: bool
    reason: str | None = None
    size_bytes: int = 0
    signature: str | None = None
    reused: bool = False


class PreflightDiagnosticError(RuntimeError):
    def __init__(self, reason: str, exception_type: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exception_type = exception_type


class Reporter:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout

    def emit(self, stage: str, status: str, **details: object) -> None:
        payload = {"stage": stage, "status": status, **details}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=self.stream, flush=True)


def _emit_local_diagnostic(stage: str, reason: str, exception_type: str) -> None:
    payload = {"stage": stage, "reason": reason, "exception_type": exception_type}
    print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)


def _sync_preflight_with_diagnostics(root: Path) -> None:
    try:
        sync_preflight(root)
    except GateError:
        raise
    except UnicodeError as exc:
        raise PreflightDiagnosticError("acl_decode_failed", type(exc).__name__) from exc
    except TypeError as exc:
        raise PreflightDiagnosticError("acl_output_invalid", type(exc).__name__) from exc
    except subprocess.SubprocessError as exc:
        raise PreflightDiagnosticError("acl_command_failed", type(exc).__name__) from exc
    except OSError as exc:
        raise PreflightDiagnosticError("preflight_io_failed", type(exc).__name__) from exc
    except Exception as exc:
        raise PreflightDiagnosticError("preflight_internal_error", type(exc).__name__) from exc


def _collection_page_result(data: Any, position: int = 1) -> CollectResult:
    if position < 1:
        return CollectResult(None, "position_invalid", position=position)
    if not isinstance(data, dict):
        return CollectResult(None, "collect_page_invalid", position=position)
    if data.get("status_code") not in (None, 0, "0"):
        return CollectResult(None, "collect_page_denied", position=position)
    items = data.get("aweme_list")
    if not isinstance(items, list):
        return CollectResult(None, "collect_page_invalid", position=position)
    entries = [item for item in items if isinstance(item, dict)]
    if len(entries) < position:
        reason = "position_not_available" if entries else "no_collected_video"
        return CollectResult(
            None,
            reason,
            position=position,
            cursor=data.get("cursor"),
            has_more=bool(data.get("has_more")),
        )
    return CollectResult(
        entries[position - 1],
        position=position,
        cursor=data.get("cursor"),
        has_more=bool(data.get("has_more")),
    )


async def _scroll_collection_container(page: Any) -> bool:
    return bool(
        await page.evaluate(
            """
            () => {
              const candidates = Array.from(document.querySelectorAll('*'))
                .filter((element) => {
                  const style = getComputedStyle(element);
                  const rect = element.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0
                    && (style.overflowY === 'auto' || style.overflowY === 'scroll')
                    && element.scrollHeight > element.clientHeight + 100;
                });
              for (const target of candidates) {
                target.scrollTop = target.scrollHeight;
                target.dispatchEvent(new Event('scroll', {bubbles: true}));
                target.dispatchEvent(new WheelEvent('wheel', {
                  bubbles: true,
                  cancelable: true,
                  deltaY: Math.max(1200, target.clientHeight),
                }));
              }
              return candidates.length > 0;
            }
            """
        )
    )


async def fetch_one_collected_video(
    cookies: Mapping[str, str],
    position: int = 1,
    page_handler: Callable[[dict[str, Any], str], Awaitable[None]] | None = None,
) -> CollectResult:
    if position < 1:
        return CollectResult(None, "position_invalid", position=position)

    from playwright.async_api import async_playwright

    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[CollectResult] = loop.create_future()
    response_tasks: list[asyncio.Task[None]] = []
    response_lock = asyncio.Lock()
    collected_items: list[dict[str, Any]] = []
    seen_aweme_ids: set[str] = set()
    expected_request_cursor = "0"

    async def inspect_response(response: Any) -> None:
        nonlocal expected_request_cursor
        if urlparse(response.url).path != COLLECTION_API_PATH:
            return
        async with response_lock:
            if result_future.done():
                return
            try:
                data = await response.json()
            except Exception:
                result = CollectResult(None, "collect_page_invalid", position=position)
            else:
                page = _collection_page_result(data)
                if page.item is None and page.reason not in {"no_collected_video"}:
                    result = page
                else:
                    query_cursor = parse_qs(urlparse(response.url).query).get("cursor")
                    request_cursor = query_cursor[0] if query_cursor else expected_request_cursor
                    next_cursor = str(data.get("cursor") or "")
                    has_more = bool(data.get("has_more"))
                    if has_more and (not next_cursor or next_cursor == request_cursor):
                        result = CollectResult(
                            None,
                            "collect_cursor_not_advanced",
                            position=position,
                        )
                    else:
                        if page_handler is not None:
                            await page_handler(data, request_cursor)
                        expected_request_cursor = next_cursor if has_more else request_cursor
                        raw_items = data.get("aweme_list", []) if isinstance(data, dict) else []
                        for item in raw_items:
                            if not isinstance(item, dict):
                                continue
                            aweme_id = str(item.get("aweme_id") or "").strip()
                            if aweme_id and aweme_id in seen_aweme_ids:
                                continue
                            if aweme_id:
                                seen_aweme_ids.add(aweme_id)
                            collected_items.append(item)
                        selected = (
                            collected_items[position - 1]
                            if len(collected_items) >= position
                            else None
                        )
                        if page_handler is not None and has_more:
                            return
                        if selected is not None:
                            result = CollectResult(
                                selected,
                                position=position,
                                cursor=data.get("cursor"),
                                has_more=has_more,
                            )
                        elif not has_more:
                            result = CollectResult(
                                None,
                                "position_not_available",
                                position=position,
                                cursor=data.get("cursor"),
                                has_more=False,
                            )
                        else:
                            return
            if not result_future.done():
                result_future.set_result(result)

    def on_response(response: Any) -> None:
        response_tasks.append(asyncio.create_task(inspect_response(response)))

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            context = await browser.new_context(
                user_agent=BROWSER_USER_AGENT,
                locale="zh-CN",
                viewport={"width": 1600, "height": 900},
            )
            await context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "domain": ".douyin.com",
                        "path": "/",
                        "secure": True,
                    }
                    for name, value in cookies.items()
                ]
            )
            page = await context.new_page()
            page.on("response", on_response)
            try:
                await page.goto(
                    COLLECTION_PAGE_URL,
                    wait_until="domcontentloaded",
                    timeout=120_000,
                )
            except Exception:
                pass
            try:
                for _attempt in range(400):
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(result_future), timeout=1.5
                        )
                        break
                    except TimeoutError:
                        scrolled = await _scroll_collection_container(page)
                        if not scrolled:
                            await page.mouse.wheel(0, 2400)
                        await page.wait_for_timeout(500)
                else:
                    result = CollectResult(
                        None,
                        "collect_page_timeout",
                        position=position,
                    )
            finally:
                if response_tasks:
                    await asyncio.gather(*response_tasks, return_exceptions=True)
                await context.close()
                await browser.close()
            return result
    except Exception:
        return CollectResult(None, "collect_page_error", position=position)


def stable_job_id(item: Mapping[str, Any]) -> str:
    aweme_id = str(item.get("aweme_id") or "").strip()
    if not aweme_id:
        raise ValueError("aweme_id_missing")
    return stable_job_id_for_source(aweme_id)


def write_job_state(job_dir: Path, result: CollectResult) -> None:
    if result.item is None or result.position is None:
        raise ValueError("selected collection item is required")
    item = result.item
    author_value = item.get("author")
    author = author_value if isinstance(author_value, dict) else {}
    video_value = item.get("video")
    video = video_value if isinstance(video_value, dict) else {}
    duration_ms = item.get("duration")
    payload = {
        "schema_version": 1,
        "job_id": stable_job_id(item),
        "source": {
            "aweme_id": str(item.get("aweme_id") or ""),
            "position": result.position,
            "cursor": result.cursor,
        },
        "title": str(item.get("desc") or "").strip(),
        "author": str(author.get("nickname") or "").strip(),
        "expected_media": {
            "duration_seconds": (
                round(float(duration_ms) / 1000, 3)
                if isinstance(duration_ms, (int, float)) and duration_ms > 0
                else None
            ),
            "width": int(video.get("width") or 0) or None,
            "height": int(video.get("height") or 0) or None,
        },
    }
    _atomic_json(job_dir / "job.json", payload)


def _atomic_json(target: Path, payload: Mapping[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _https_url(play_addr: Mapping[str, Any]) -> str | None:
    urls = _https_urls(play_addr)
    return urls[0] if urls else None


def _https_urls(play_addr: Mapping[str, Any]) -> list[str]:
    urls = play_addr.get("url_list")
    if not isinstance(urls, list):
        return []
    result: list[str] = []
    for value in urls:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        try:
            if (
                candidate
                and urlparse(candidate).scheme.lower() == "https"
                and candidate not in result
            ):
                result.append(candidate)
        except ValueError:
            continue
    return result


def _quality_score(entry: Mapping[str, Any]) -> tuple[int, int, int] | None:
    play_addr_value = entry.get("play_addr")
    play_addr = play_addr_value if isinstance(play_addr_value, dict) else {}
    try:
        width = int(play_addr.get("width") or entry.get("width") or 0)
        height = int(play_addr.get("height") or entry.get("height") or 0)
        bit_rate = int(entry.get("bit_rate") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or bit_rate <= 0 or _https_url(play_addr) is None:
        return None
    return min(width, height), width * height, bit_rate


def extract_standard_play_url(item: Mapping[str, Any]) -> str | None:
    urls = extract_standard_play_urls(item)
    return urls[0] if urls else None


def extract_standard_play_urls(item: Mapping[str, Any]) -> list[str]:
    video_value = item.get("video")
    video = video_value if isinstance(video_value, dict) else {}
    candidates: list[str] = []
    bit_rates = video.get("bit_rate")
    if isinstance(bit_rates, list):
        ranked = [
            (score, entry)
            for entry in bit_rates
            if isinstance(entry, dict) and (score := _quality_score(entry)) is not None
        ]
        for _score, selected in sorted(ranked, key=lambda item: item[0], reverse=True):
            selected_addr = selected.get("play_addr")
            if isinstance(selected_addr, dict):
                for selected_url in _https_urls(selected_addr):
                    if selected_url not in candidates:
                        candidates.append(selected_url)
    play_addr = video.get("play_addr")
    if isinstance(play_addr, dict):
        for fallback_url in _https_urls(play_addr):
            if fallback_url not in candidates:
                candidates.append(fallback_url)
    return candidates


def _is_allowed_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return False
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in ALLOWED_MEDIA_HOST_SUFFIXES
    )


def _header(headers: Any, name: str) -> str:
    if not headers:
        return ""
    value = headers.get(name)
    return str(value or "").strip()


def _media_signature(prefix: bytes) -> tuple[str, str] | None:
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return "iso-bmff", ".mp4"
    if prefix.startswith(b"\x1a\x45\xdf\xa3"):
        return "ebml", ".webm"
    if prefix.startswith(b"FLV"):
        return "flv", ".flv"
    if prefix.startswith(b"OggS"):
        return "ogg", ".ogg"
    if len(prefix) >= 377 and prefix[0] == prefix[188] == prefix[376] == 0x47:
        return "mpeg-ts", ".ts"
    return None


def existing_job_source(destination: Path) -> FetchResult | None:
    source = destination / "source.mp4"
    try:
        if not source.is_file() or source.stat().st_size <= 0:
            return None
        with source.open("rb") as handle:
            signature = _media_signature(handle.read(512))
        if not signature or signature[0] != "iso-bmff":
            return None
        return FetchResult(
            200,
            True,
            True,
            size_bytes=source.stat().st_size,
            signature="iso-bmff",
            reused=True,
        )
    except OSError:
        return None


def _content_length(headers: Any) -> int | None:
    if _header(headers, "Content-Encoding"):
        return None
    raw = _header(headers, "Content-Length")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _contains_protected_box_header(data: bytes) -> bool:
    """Recognize DRM box headers without matching arbitrary compressed payload bytes."""
    for marker in PROTECTED_MEDIA_MARKERS:
        start = 4
        while True:
            index = data.find(marker, start)
            if index < 0:
                break
            declared_size = int.from_bytes(data[index - 4 : index], "big")
            payload = data[index + 4 :]
            plausible_size = MIN_PROTECTED_BOX_SIZES[marker] <= declared_size <= MAX_MEDIA_BYTES
            full_box = marker in {b"pssh", b"schm"} and len(payload) >= 4
            full_box = full_box and payload[0] in {0, 1} and payload[1:4] == b"\x00\x00\x00"
            sample_entry = marker in {b"encv", b"enca"} and len(payload) >= 8
            sample_entry = sample_entry and payload[:6] == b"\x00" * 6
            sample_entry = sample_entry and int.from_bytes(payload[6:8], "big") > 0
            container = marker == b"sinf" and len(payload) >= 8
            if container:
                child_size = int.from_bytes(payload[:4], "big")
                container = 8 <= child_size <= declared_size - 8
                container = container and payload[4:8] in {b"frma", b"schm", b"schi"}
            if plausible_size and (full_box or sample_entry or container):
                return True
            start = index + 1
    return False


async def download_sample(
    session: Any,
    url: str,
    output_dir: Path,
    *,
    proxy: str | None = None,
    final_name: str | None = None,
) -> FetchResult:
    if not _is_allowed_media_url(url):
        return FetchResult(None, False, False, "media_url_not_allowed")

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_dir = output_dir.resolve()
    except OSError:
        return FetchResult(None, False, False, "probe_output_unavailable")
    if final_name is not None and (Path(final_name).name != final_name or not final_name):
        return FetchResult(None, False, False, "output_name_invalid")
    target_basename = final_name or "collect-probe-sample"
    temp_path = output_dir / f".{target_basename}.{uuid.uuid4().hex}.tmp"
    current_url = url

    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            async with session.get(
                current_url,
                allow_redirects=False,
                proxy=proxy or None,
            ) as response:
                status = int(response.status)
                if status in REDIRECT_STATUSES:
                    if redirect_count >= MAX_REDIRECTS:
                        return FetchResult(status, False, False, "redirect_limit")
                    location = _header(response.headers, "Location")
                    next_url = urljoin(current_url, location) if location else ""
                    if not next_url or not _is_allowed_media_url(next_url):
                        return FetchResult(status, False, False, "redirect_not_allowed")
                    current_url = next_url
                    continue

                if status == 403:
                    return FetchResult(status, False, False, "http_403_platform_denied")
                if status != 200:
                    return FetchResult(status, False, False, "http_not_readable")

                content_type = _header(response.headers, "Content-Type")
                normalized_type = content_type.split(";", 1)[0].strip().lower()
                if normalized_type in MANIFEST_CONTENT_TYPES:
                    return FetchResult(status, True, False, "manifest_stream")

                expected_size = _content_length(response.headers)
                if expected_size is not None and expected_size > MAX_MEDIA_BYTES:
                    return FetchResult(status, True, False, "media_too_large")

                written = 0
                prefix = bytearray()
                scan_tail = b""
                protected_media = False
                with temp_path.open("xb") as handle:
                    async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_BYTES):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_MEDIA_BYTES:
                            return FetchResult(status, True, False, "media_too_large")
                        if len(prefix) < 512:
                            prefix.extend(chunk[: 512 - len(prefix)])
                        scan_window = scan_tail + chunk
                        if _contains_protected_box_header(scan_window):
                            protected_media = True
                        scan_tail = scan_window[-19:]
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())

                if written <= 0:
                    return FetchResult(status, True, False, "empty_media")
                if expected_size is not None and written != expected_size:
                    return FetchResult(status, True, False, "content_length_mismatch")
                if protected_media:
                    return FetchResult(status, True, False, "protected_media_box")

                signature = _media_signature(bytes(prefix))
                if signature is None:
                    return FetchResult(status, True, False, "invalid_media_signature")
                signature_name, suffix = signature
                if final_name is not None:
                    if signature_name != "iso-bmff" or Path(final_name).suffix.lower() != ".mp4":
                        return FetchResult(status, True, False, "unsupported_media_container")
                    final_path = output_dir / final_name
                else:
                    final_path = output_dir / f"collect-probe-sample{suffix}"
                os.replace(temp_path, final_path)
                return FetchResult(status, True, True, size_bytes=written, signature=signature_name)
    except Exception:
        return FetchResult(None, False, False, "http_read_error")
    finally:
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)

    return FetchResult(None, False, False, "redirect_limit")


async def execute_probe(
    *,
    source_fetcher: Callable[[], Awaitable[CollectResult]],
    fetcher: Callable[[str, Path], Awaitable[FetchResult]],
    output_dir: Path,
    reporter: Reporter,
    use_job_layout: bool = False,
    should_process: Callable[[dict[str, Any], Path], tuple[bool, str]] | None = None,
    handoff_path: Path | None = None,
) -> int:
    try:
        collected_video = await source_fetcher()
    except Exception:
        reporter.emit(
            "collect_page", "controlled_failure", entry_returned=False, reason="collect_page_error"
        )
        return CONTROLLED_FAILURE_EXIT

    if collected_video.item is None:
        if collected_video.reason == "no_processing_required":
            reporter.emit("collect_page", "ok", entry_returned=False)
            reporter.emit("sample", "skipped", reason="no_processing_required")
            return 0
        reporter.emit(
            "collect_page",
            "controlled_failure",
            entry_returned=False,
            reason=collected_video.reason,
        )
        return CONTROLLED_FAILURE_EXIT
    reporter.emit("collect_page", "ok", entry_returned=True)

    job_id: str | None = None
    destination = output_dir
    if use_job_layout:
        try:
            job_id = stable_job_id(collected_video.item)
            destination = output_dir / job_id
            write_job_state(destination, collected_video)
        except (OSError, ValueError):
            reporter.emit(
                "job_state",
                "controlled_failure",
                reason="job_state_unavailable",
            )
            return CONTROLLED_FAILURE_EXIT

    process_item = True
    decision_reason = "new_or_incomplete"
    if should_process is not None:
        try:
            process_item, decision_reason = should_process(collected_video.item, destination)
        except Exception:
            reporter.emit("registry", "controlled_failure", reason="registry_unavailable")
            return CONTROLLED_FAILURE_EXIT
    if handoff_path is not None and job_id is not None:
        try:
            _atomic_json(
                handoff_path,
                {
                    "job_id": job_id,
                    "aweme_id": str(collected_video.item.get("aweme_id") or ""),
                    "observed_position": collected_video.position,
                    "author": str(
                        (collected_video.item.get("author") or {}).get("nickname") or ""
                    )
                    if isinstance(collected_video.item.get("author"), dict)
                    else "",
                    "title": str(collected_video.item.get("desc") or "").strip(),
                    "should_process": process_item,
                    "reason": decision_reason,
                },
            )
        except OSError:
            reporter.emit("handoff", "controlled_failure", reason="handoff_unavailable")
            return CONTROLLED_FAILURE_EXIT
    if not process_item:
        reporter.emit("sample", "skipped", reason=decision_reason)
        return 0

    play_urls = extract_standard_play_urls(collected_video.item)
    if not play_urls:
        reporter.emit(
            "standard_play_stream",
            "controlled_failure",
            available=False,
            reason="standard_play_stream_missing",
        )
        return CONTROLLED_FAILURE_EXIT
    reporter.emit("standard_play_stream", "ok", available=True, source="play_addr")

    fetched = FetchResult(None, False, False, "http_read_error")
    for play_url in play_urls:
        try:
            fetched = await fetcher(play_url, destination)
        except Exception:
            fetched = FetchResult(None, False, False, "http_read_error")
        if fetched.saved:
            break
    reporter.emit(
        "http_read",
        "ok" if fetched.readable else "controlled_failure",
        readable=fetched.readable,
        http_status=fetched.http_status,
        **({"reason": fetched.reason} if not fetched.readable else {}),
    )
    if not fetched.saved:
        reporter.emit(
            "sample",
            "controlled_failure",
            saved=False,
            reason=fetched.reason,
        )
        return CONTROLLED_FAILURE_EXIT

    reporter.emit(
        "sample",
        "ok",
        saved=True,
        size_bytes=fetched.size_bytes,
        media_format=fetched.signature,
        reused=fetched.reused,
    )
    return 0


def _fixed_identity_conflict(
    *,
    db_path: Path,
    row: sqlite3.Row,
    item: Mapping[str, Any],
    expected_job_id: str,
    expected_aweme_id: str,
) -> bool:
    if (
        row["source_id"] != expected_aweme_id
        or row["aweme_id"] != expected_aweme_id
        or stable_job_id(item) != expected_job_id
    ):
        return True
    root = db_path.parents[1]
    job_dir = root / "data" / "jobs" / expected_job_id
    job_state_path = job_dir / "job.json"
    if job_state_path.is_file():
        try:
            job_state = json.loads(job_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        source = job_state.get("source") if isinstance(job_state.get("source"), dict) else {}
        if source.get("aweme_id") and source.get("aweme_id") != expected_aweme_id:
            return True
        old_author = str(job_state.get("author") or "").strip()
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        new_author = str(author.get("nickname") or "").strip()
        if old_author and new_author and old_author != new_author:
            return True
    source_path = job_dir / "source.mp4"
    if row["media_sha256"] and source_path.is_file():
        try:
            if sha256_file(source_path) != row["media_sha256"]:
                return True
        except OSError:
            return True
    return False


def select_snapshot_item(
    result: CollectResult,
    *,
    expected_job_id: str | None,
    expected_aweme_id: str | None,
    position: int,
    snapshot_items: Mapping[str, dict[str, Any]],
    db_path: Path,
    snapshot_id: str,
) -> CollectResult:
    if expected_job_id is not None:
        if not expected_aweme_id or stable_job_id_for_source(expected_aweme_id) != expected_job_id:
            return CollectResult(None, "fixed_item_binding_invalid", position=position)
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT item.*, snapshot_item.position AS observed_position
                FROM collection_items AS item
                LEFT JOIN collection_snapshot_items AS snapshot_item
                  ON snapshot_item.snapshot_id = ?
                 AND snapshot_item.source_id = item.source_id
                WHERE item.job_id = ?
                """,
                (snapshot_id, expected_job_id),
            ).fetchone()
        if row is None:
            return CollectResult(None, "fixed_item_not_found", position=position)
        if not row["currently_collected"]:
            return CollectResult(None, "fixed_item_not_current", position=position)
        if row["observed_position"] is None:
            return CollectResult(None, "fixed_item_not_in_snapshot", position=position)
        if row["status"] == "completed":
            return CollectResult(None, "fixed_item_already_completed", position=position)
        if row["status"] not in {"new", "downloaded", "analyzed", "failed", "incomplete"}:
            return CollectResult(None, "fixed_item_status_conflict", position=position)
        selected = snapshot_items.get(expected_aweme_id)
        if selected is None:
            return CollectResult(None, "fixed_item_details_missing", position=position)
        try:
            conflict = _fixed_identity_conflict(
                db_path=db_path,
                row=row,
                item=selected,
                expected_job_id=expected_job_id,
                expected_aweme_id=expected_aweme_id,
            )
        except ValueError:
            conflict = True
        if conflict:
            return CollectResult(None, "fixed_item_identity_conflict", position=position)
        return CollectResult(
            selected,
            position=int(row["observed_position"]),
            cursor=result.cursor,
            has_more=False,
        )

    next_item = CollectionRegistry(db_path, root=db_path.parents[1]).next_item(
        snapshot_id,
        pipeline_version=PIPELINE_VERSION,
    )
    if next_item is None:
        return CollectResult(None, "no_processing_required", has_more=False)
    selected = snapshot_items.get(next_item.source_id)
    if selected is None:
        pause_snapshot(db_path, snapshot_id, "snapshot_item_details_missing")
        return CollectResult(None, "snapshot_item_details_missing")
    return CollectResult(selected, position=next_item.last_position, has_more=False)


async def run_live(
    root: Path,
    reporter: Reporter,
    position: int = 1,
    handoff_path: Path | None = None,
    expected_job_id: str | None = None,
    expected_aweme_id: str | None = None,
) -> int:
    root = root.resolve()
    _sync_preflight_with_diagnostics(root)
    config = validate_downloader_config(root)
    output_dir = resolve_project_path(root, JOBS_RELATIVE_DIR, "job 输出目录")
    db_path = root / "data" / "knowledge.db"
    snapshot = begin_snapshot(db_path, pipeline_version=PIPELINE_VERSION)
    cookies = load_cookie_values(allowed_cookie_path(root))
    reporter.emit("preflight", "ok", snapshot_resumed=snapshot.resumed)

    import aiohttp

    media_headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Referer": "https://www.douyin.com/",
        "Accept": "video/*,application/octet-stream;q=0.9,*/*;q=0.1",
    }
    timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_read=60)
    media_session = aiohttp.ClientSession(headers=media_headers, timeout=timeout)
    async with media_session as session:
        snapshot_items: dict[str, dict[str, Any]] = {}

        async def page_handler(data: dict[str, Any], request_cursor: str) -> None:
            raw_items = data.get("aweme_list")
            items = [item for item in raw_items if isinstance(item, dict)]
            for item in items:
                source_id = str(item.get("aweme_id") or "").strip()
                if source_id and source_id not in snapshot_items:
                    snapshot_items[source_id] = item
            ingest_snapshot_page(
                db_path,
                snapshot_id=snapshot.snapshot_id,
                cursor=request_cursor,
                next_cursor=data.get("cursor"),
                has_more=bool(data.get("has_more")),
                items=items,
            )

        async def source_fetcher() -> CollectResult:
            result = await fetch_one_collected_video(
                cookies,
                position,
                page_handler=page_handler,
            )
            if result.item is not None or (
                result.reason in {"no_collected_video", "position_not_available"}
                and result.has_more is False
            ):
                complete_snapshot(db_path, snapshot.snapshot_id)
                from app.obsidian_publish import configured_vault, sync_favorite_states

                vault = configured_vault(root)
                if vault is not None:
                    sync_favorite_states(root, vault)
                return select_snapshot_item(
                    result,
                    expected_job_id=expected_job_id,
                    expected_aweme_id=expected_aweme_id,
                    position=position,
                    snapshot_items=snapshot_items,
                    db_path=db_path,
                    snapshot_id=snapshot.snapshot_id,
                )
            pause_snapshot(
                db_path,
                snapshot.snapshot_id,
                result.reason or "collection_snapshot_interrupted",
            )
            return result

        def should_process_item(item: dict[str, Any], destination: Path) -> tuple[bool, str]:
            source_id = str(item.get("aweme_id") or "").strip()
            existing = existing_job_source(destination)
            observed_hash = sha256_file(destination / "source.mp4") if existing else ""
            decision = claim_item(
                db_path,
                source_id,
                pipeline_version=PIPELINE_VERSION,
                observed_media_sha256=observed_hash,
            )
            return decision.should_process, decision.reason

        async def fetcher(url: str, destination: Path) -> FetchResult:
            existing = existing_job_source(destination)
            if existing is not None:
                result = existing
            else:
                result = await download_sample(
                    session,
                    url,
                    destination,
                    proxy=config.get("proxy") or None,
                    final_name="source.mp4",
                )
            if result.saved:
                update_item_by_job(
                    db_path,
                    destination.name,
                    status="downloaded",
                    media_sha256=sha256_file(destination / "source.mp4"),
                )
            else:
                update_item_by_job(
                    db_path,
                    destination.name,
                    status="failed",
                    error=result.reason or "download_failed",
                )
            return result

        return await execute_probe(
            source_fetcher=source_fetcher,
            fetcher=fetcher,
            output_dir=output_dir,
            reporter=reporter,
            use_job_layout=True,
            should_process=should_process_item,
            handoff_path=handoff_path,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safe single collected-video download feasibility probe"
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--source",
        choices=(COLLECT_VIDEO_SOURCE,),
        default=COLLECT_VIDEO_SOURCE,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--position",
        type=int,
        default=1,
        help="1-based absolute position in the collected-video list",
    )
    parser.add_argument(
        "--job-id",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--aweme-id",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--handoff",
        type=Path,
        help=argparse.SUPPRESS,
    )
    return parser


def resolve_handoff_path(root: Path, supplied_path: Path | None) -> Path | None:
    if supplied_path is None:
        return None
    resolved_root = root.resolve()
    candidate = supplied_path if supplied_path.is_absolute() else resolved_root / supplied_path
    try:
        relative = candidate.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise GateError("handoff 路径必须位于项目内") from exc
    handoff_path = resolve_project_path(resolved_root, relative, "handoff 路径")
    jobs_root = resolve_project_path(resolved_root, JOBS_RELATIVE_DIR, "job 输出目录")
    try:
        handoff_path.relative_to(jobs_root)
    except ValueError as exc:
        raise GateError("handoff 路径必须位于 data/jobs") from exc
    return handoff_path


def main() -> int:
    args = build_parser().parse_args()
    reporter = Reporter()
    try:
        if args.position < 1:
            reporter.emit("preflight", "controlled_failure", reason="position_invalid")
            return 2
        handoff_path = resolve_handoff_path(args.root, args.handoff)
        return asyncio.run(
            run_live(
                args.root,
                reporter,
                args.position,
                handoff_path,
                expected_job_id=args.job_id,
                expected_aweme_id=args.aweme_id,
            )
        )
    except PreflightDiagnosticError as exc:
        reporter.emit("preflight", "controlled_failure", reason=exc.reason)
        _emit_local_diagnostic("preflight", exc.reason, exc.exception_type)
        return 2
    except GateError as exc:
        reporter.emit("preflight", "controlled_failure", reason=exc.reason)
        _emit_local_diagnostic("preflight", exc.reason, type(exc).__name__)
        return 2
    except Exception as exc:
        reporter.emit("probe", "controlled_failure", reason="internal_error")
        _emit_local_diagnostic("probe", "internal_error", type(exc).__name__)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
