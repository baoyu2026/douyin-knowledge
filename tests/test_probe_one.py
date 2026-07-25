import asyncio
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from app.collection_registry import PIPELINE_VERSION, CollectionRegistry
from app.probe_one import (
    COLLECT_VIDEO_SOURCE,
    CONTROLLED_FAILURE_EXIT,
    PROBE_RELATIVE_DIR,
    FetchResult,
    Reporter,
    _collection_page_result,
    build_parser,
    download_sample,
    execute_probe,
    extract_standard_play_url,
    resolve_handoff_path,
    select_snapshot_item,
    stable_job_id,
)
from app.probe_one import (
    main as probe_main,
)
from app.security import GateError
from tests.publication_helpers import accept_item_for_test


class FakeAPI:
    def __init__(self, page, *, collections_page=None):
        self.page = page
        self.collections_page = collections_page
        self.cookie = "private-cookie"

    async def fetch(self):
        return _collection_page_result(
            {
                "status_code": 0,
                "aweme_list": self.page.get("items", []) if isinstance(self.page, dict) else [],
            }
        )


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status, *, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_collection_page_result_consumes_only_first_video() -> None:
    result = _collection_page_result(
        {
            "status_code": 0,
            "aweme_list": [{"aweme_id": "first"}, {"aweme_id": "second"}],
            "cursor": 8,
            "has_more": True,
        }
    )

    assert result.item == {"aweme_id": "first"}


def test_collection_page_result_rejects_wrong_shape() -> None:
    result = _collection_page_result({"status_code": 0, "items": [{"aweme_id": "wrong"}]})

    assert result.item is None
    assert result.reason == "collect_page_invalid"


def test_collection_page_result_handles_denial_without_fallback() -> None:
    result = _collection_page_result({"status_code": 8, "aweme_list": []})

    assert result.item is None
    assert result.reason == "collect_page_denied"


def test_collect_cli_uses_the_bounded_collection_source() -> None:
    args = build_parser().parse_args(["--source", COLLECT_VIDEO_SOURCE])

    assert args.source == "collect-video"
    assert PROBE_RELATIVE_DIR == Path("data/probe-collect")


@pytest.mark.skipif(os.name != "nt", reason="Windows path contract regression")
def test_resolve_handoff_path_accepts_windows_jobs_path_and_rejects_task_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    handoff = (
        root
        / "data"
        / "jobs"
        / "aweme-0123456789abcdefabcd"
        / "single-item-download-handoff.json"
    )

    assert resolve_handoff_path(root, handoff) == handoff.resolve()
    assert "\\" in str(handoff.resolve())
    with pytest.raises(GateError, match="data/jobs"):
        resolve_handoff_path(root, root / "task" / "artifacts" / "handoff.json")


def _complete_snapshot(root: Path, items: list[dict]) -> tuple[Path, str]:
    db_path = root / "data" / "knowledge.db"
    registry = CollectionRegistry(db_path, root=root)
    snapshot_id = registry.begin_snapshot(pipeline_version=PIPELINE_VERSION)
    registry.record_snapshot_page(snapshot_id, items)
    registry.complete_snapshot(snapshot_id, pipeline_version=PIPELINE_VERSION)
    return db_path, snapshot_id


def test_fixed_job_selection_never_calls_next_item(tmp_path: Path, monkeypatch) -> None:
    item = {"aweme_id": "fixture-aweme-id", "author": {"nickname": "作者甲"}}
    expected_job_id = stable_job_id(item)
    db_path, snapshot_id = _complete_snapshot(tmp_path, [item])

    def unexpected_next_item(*_args, **_kwargs):
        raise AssertionError("fixed selection must not call next_item")

    monkeypatch.setattr(
        "app.probe_one.CollectionRegistry.next_item",
        unexpected_next_item,
    )
    selected = select_snapshot_item(
        _collection_page_result(
            {"status_code": 0, "aweme_list": [item]},
            position=1,
        ),
        expected_job_id=expected_job_id,
        expected_aweme_id=item["aweme_id"],
        position=1,
        snapshot_items={item["aweme_id"]: item},
        db_path=db_path,
        snapshot_id=snapshot_id,
    )

    assert selected.item == item
    assert selected.position == 1
    assert selected.reason is None


def test_fixed_job_follows_stable_id_when_position_drifts(tmp_path: Path, monkeypatch) -> None:
    target = {"aweme_id": "fixture-aweme-id", "author": {"nickname": "作者甲"}}
    items = [{"aweme_id": f"head-{index}"} for index in range(1, 10)] + [target]
    expected_job_id = stable_job_id(target)
    db_path, snapshot_id = _complete_snapshot(tmp_path, items)
    monkeypatch.setattr(
        "app.probe_one.CollectionRegistry.next_item",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fixed selection must not call next_item")
        ),
    )

    selected = select_snapshot_item(
        _collection_page_result(
            {"status_code": 0, "aweme_list": items},
            position=6,
        ),
        expected_job_id=expected_job_id,
        expected_aweme_id=target["aweme_id"],
        position=6,
        snapshot_items={item["aweme_id"]: item for item in items},
        db_path=db_path,
        snapshot_id=snapshot_id,
    )

    assert selected.item == target
    assert selected.position == 10
    assert selected.item != items[5]


def test_fixed_job_stops_when_stable_id_disappears(tmp_path: Path) -> None:
    target = {"aweme_id": "fixture-aweme-id"}
    expected_job_id = stable_job_id(target)
    _db_path, _first_snapshot = _complete_snapshot(tmp_path, [target])
    other = {"aweme_id": "other-current-item"}
    db_path, latest_snapshot = _complete_snapshot(tmp_path, [other])

    selected = select_snapshot_item(
        _collection_page_result(
            {"status_code": 0, "aweme_list": [other]}, position=1
        ),
        expected_job_id=expected_job_id,
        expected_aweme_id=target["aweme_id"],
        position=1,
        snapshot_items={other["aweme_id"]: other},
        db_path=db_path,
        snapshot_id=latest_snapshot,
    )

    assert selected.item is None
    assert selected.reason == "fixed_item_not_current"


def test_fixed_job_stops_on_identity_conflict_or_other_owner_completion(tmp_path: Path) -> None:
    target = {"aweme_id": "fixture-aweme-id", "author": {"nickname": "新作者"}}
    expected_job_id = stable_job_id(target)
    db_path, snapshot_id = _complete_snapshot(tmp_path, [target])
    job_dir = tmp_path / "data" / "jobs" / expected_job_id
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "source": {"aweme_id": target["aweme_id"]},
                "author": "旧作者",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    kwargs = {
        "result": _collection_page_result(
            {"status_code": 0, "aweme_list": [target]}, position=1
        ),
        "expected_job_id": expected_job_id,
        "expected_aweme_id": target["aweme_id"],
        "position": 1,
        "snapshot_items": {target["aweme_id"]: target},
        "db_path": db_path,
        "snapshot_id": snapshot_id,
    }

    conflict = select_snapshot_item(**kwargs)
    assert conflict.item is None
    assert conflict.reason == "fixed_item_identity_conflict"

    (job_dir / "job.json").unlink()
    source = job_dir / "source.mp4"
    source.write_bytes(b"accepted fixture")
    library = tmp_path / "library" / expected_job_id
    library.mkdir(parents=True)
    registry = CollectionRegistry(db_path, root=tmp_path)
    accept_item_for_test(
        registry,
        target["aweme_id"],
        pipeline_version=PIPELINE_VERSION,
        media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        job_path=job_dir,
        library_path=library,
    )
    completed = select_snapshot_item(**kwargs)
    assert completed.item is None
    assert completed.reason == "fixed_item_already_completed"


def test_fixed_job_rejects_aweme_job_id_mismatch(tmp_path: Path) -> None:
    target = {"aweme_id": "fixture-aweme-id"}
    db_path, snapshot_id = _complete_snapshot(tmp_path, [target])
    selected = select_snapshot_item(
        _collection_page_result(
            {"status_code": 0, "aweme_list": [target]}, position=1
        ),
        expected_job_id=stable_job_id(target),
        expected_aweme_id="different-aweme-id",
        position=1,
        snapshot_items={target["aweme_id"]: target},
        db_path=db_path,
        snapshot_id=snapshot_id,
    )
    assert selected.item is None
    assert selected.reason == "fixed_item_binding_invalid"


@pytest.mark.parametrize(
    ("exception", "reason"),
    [
        (
            UnicodeDecodeError("utf-8", b"\\xd2", 0, 1, "private decode detail"),
            "acl_decode_failed",
        ),
        (TypeError("private Cookie and icacls output"), "acl_output_invalid"),
        (OSError("private Cookie and filesystem detail"), "preflight_io_failed"),
        (
            GateError("private ACL path", reason="acl_check_failed"),
            "acl_check_failed",
        ),
    ],
)
def test_preflight_error_classification_is_precise_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exception: Exception,
    reason: str,
) -> None:
    def fail_preflight(_root: Path) -> None:
        raise exception

    monkeypatch.setattr("app.probe_one.sync_preflight", fail_preflight)
    monkeypatch.setattr(
        "sys.argv",
        [
            "probe-one",
            "--root",
            str(tmp_path),
            "--job-id",
            "aweme-0123456789abcdefabcd",
            "--position",
            "6",
        ],
    )

    assert probe_main() == 2
    captured = capsys.readouterr()
    public_payload = json.loads(captured.out)
    local_payload = json.loads(captured.err)
    assert public_payload == {
        "reason": reason,
        "stage": "preflight",
        "status": "controlled_failure",
    }
    assert local_payload["stage"] == "preflight"
    assert local_payload["reason"] == reason
    assert local_payload["exception_type"] == type(exception).__name__
    combined = captured.out + captured.err
    assert "private" not in combined
    assert "Cookie" not in combined
    assert "icacls output" not in combined


def test_extract_standard_play_url_selects_first_https_url() -> None:
    item = {
        "video": {
            "bit_rate": [
                {
                    "play_addr": {
                        "url_list": ["https://v.douyinvod.com/nonstandard-bitrate"]
                    }
                }
            ],
            "play_addr": {
                "url_list": [
                    "http://v.douyinvod.com/plain-http",
                    "https://v.douyinvod.com/first-https",
                    "https://v.douyinvod.com/second-https",
                ]
            }
        }
    }

    assert (
        extract_standard_play_url(item)
        == "https://v.douyinvod.com/first-https"
    )


def test_extract_standard_play_url_prefers_highest_valid_quality() -> None:
    item = {
        "video": {
            "bit_rate": [
                {
                    "bit_rate": 800_000,
                    "play_addr": {
                        "width": 720,
                        "height": 1280,
                        "url_list": ["https://v.douyinvod.com/720p"],
                    },
                },
                {
                    "bit_rate": 1_600_000,
                    "play_addr": {
                        "width": 1080,
                        "height": 1920,
                        "url_list": ["https://v.douyinvod.com/1080p"],
                    },
                },
            ],
            "play_addr": {"url_list": ["https://v.douyinvod.com/fallback"]},
        }
    }

    assert extract_standard_play_url(item) == "https://v.douyinvod.com/1080p"


def test_execute_probe_uses_play_addr_and_keeps_private_values_out_of_output(
    tmp_path: Path,
) -> None:
    private_collect_name = "private-collect-name"
    private_collects_id = "private-collects-id"
    private_aweme_id = "private-aweme-id"
    private_title = "private-title"
    private_author = "private-author"
    private_download_url = "https://v.douyinvod.com/private-download"
    play_url = "https://v.douyinvod.com/direct-play"
    api = FakeAPI(
        {
            "items": [
                {
                    "aweme_id": private_aweme_id,
                    "desc": private_title,
                    "author": {"nickname": private_author},
                    "video": {
                        "play_addr": {"url_list": [play_url]},
                        "download_addr": {"url_list": [private_download_url]},
                    },
                }
            ]
        },
        collections_page={
            "items": [
                {
                    "collects_id_str": private_collects_id,
                    "collects_name": private_collect_name,
                }
            ]
        },
    )
    selected = []

    async def fetcher(url, output_dir):
        selected.append((url, output_dir))
        return FetchResult(200, True, True, size_bytes=32, signature="iso-bmff")

    stream = io.StringIO()
    result = asyncio.run(
        execute_probe(
            source_fetcher=api.fetch,
            fetcher=fetcher,
            output_dir=tmp_path,
            reporter=Reporter(stream),
        )
    )

    assert result == 0
    assert selected == [(play_url, tmp_path)]
    output = stream.getvalue()
    for private_value in (
        "private-cookie",
        private_collect_name,
        private_collects_id,
        private_aweme_id,
        private_title,
        private_author,
        private_download_url,
        play_url,
    ):
        assert private_value not in output
    assert '"entry_returned": true' in output
    assert '"stage": "collect_page"' in output
    assert '"source": "play_addr"' in output


def test_execute_probe_does_not_fall_back_to_download_addr(tmp_path: Path) -> None:
    api = FakeAPI(
        {
            "items": [
                {
                    "video": {
                        "download_addr": {
                            "url_list": ["https://v.douyinvod.com/download-only"]
                        }
                    }
                }
            ]
        },
    )

    async def fetcher(_url, _output_dir):
        raise AssertionError("HTTP must not be attempted without a standard play stream")

    stream = io.StringIO()
    result = asyncio.run(
        execute_probe(
            source_fetcher=api.fetch,
            fetcher=fetcher,
            output_dir=tmp_path,
            reporter=Reporter(stream),
        )
    )

    assert result == CONTROLLED_FAILURE_EXIT
    assert "standard_play_stream_missing" in stream.getvalue()


def test_execute_probe_contains_transport_exception(tmp_path: Path) -> None:
    api = FakeAPI(
        {"items": [{"video": {"play_addr": {"url_list": ["https://v.douyinvod.com/x"]}}}]},
    )

    async def fetcher(_url, _output_dir):
        raise RuntimeError("private transport detail")

    stream = io.StringIO()
    result = asyncio.run(
        execute_probe(
            source_fetcher=api.fetch,
            fetcher=fetcher,
            output_dir=tmp_path,
            reporter=Reporter(stream),
        )
    )

    output = stream.getvalue()
    assert result == CONTROLLED_FAILURE_EXIT
    assert '"reason": "http_read_error"' in output
    assert "private transport detail" not in output


def test_download_sample_atomically_saves_valid_mp4(tmp_path: Path) -> None:
    body = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
    session = FakeSession(
        FakeResponse(
            200,
            headers={"Content-Type": "video/mp4", "Content-Length": str(len(body))},
            chunks=(body[:13], body[13:]),
        )
    )

    result = asyncio.run(
        download_sample(session, "https://v.douyinvod.com/sample", tmp_path)
    )

    assert result == FetchResult(
        200,
        True,
        True,
        size_bytes=len(body),
        signature="iso-bmff",
    )
    assert (tmp_path / "collect-probe-sample.mp4").read_bytes() == body
    assert list(tmp_path.glob(".collect-probe-sample.*.tmp")) == []
    assert session.calls[0][1]["allow_redirects"] is False


def test_download_sample_treats_403_as_final_and_leaves_no_partial(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse(403))

    result = asyncio.run(
        download_sample(session, "https://v.douyinvod.com/forbidden", tmp_path)
    )

    assert result.reason == "http_403_platform_denied"
    assert result.saved is False
    assert len(session.calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_download_sample_rejects_invalid_signature_and_cleans_temp(tmp_path: Path) -> None:
    body = b"<html>not media</html>"
    session = FakeSession(
        FakeResponse(
            200,
            headers={"Content-Type": "text/html", "Content-Length": str(len(body))},
            chunks=(body,),
        )
    )

    result = asyncio.run(
        download_sample(session, "https://v.douyinvod.com/not-media", tmp_path)
    )

    assert result.reason == "invalid_media_signature"
    assert result.readable is True
    assert result.saved is False
    assert list(tmp_path.iterdir()) == []


def test_download_sample_rejects_common_drm_box_marker(tmp_path: Path) -> None:
    body = b"\x00\x00\x00\x18ftypisom" + b"\x00\x00\x00\x20pssh" + b"\x00" * 32
    session = FakeSession(
        FakeResponse(
            200,
            headers={"Content-Type": "video/mp4", "Content-Length": str(len(body))},
            chunks=(body[:17], body[17:]),
        )
    )

    result = asyncio.run(
        download_sample(session, "https://v.douyinvod.com/protected", tmp_path)
    )

    assert result.reason == "manifest_or_protected_stream"
    assert result.saved is False
    assert list(tmp_path.iterdir()) == []


def test_download_sample_rejects_redirect_outside_platform_hosts(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse(302, headers={"Location": "https://example.com/media"}))

    result = asyncio.run(
        download_sample(session, "https://www.douyin.com/play", tmp_path)
    )

    assert result.reason == "redirect_not_allowed"
    assert len(session.calls) == 1
    assert list(tmp_path.iterdir()) == []
