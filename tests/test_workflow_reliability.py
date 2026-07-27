from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from douyin_knowledge.contracts import CliError
from douyin_knowledge.operations import _analyze, run_job
from douyin_knowledge.protocol import export_packet, import_candidate
from douyin_knowledge.publishing import _registered_vault_note, publish_staged_job
from douyin_knowledge.review import approved_candidate, record_review
from tests.test_public_cli import invoke
from tests.test_structured_content import _fixture, _payload


@pytest.fixture(autouse=True)
def private_directory_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.structured_content.harden_private_project_directory",
        lambda _root, path: path.mkdir(parents=True, exist_ok=True),
    )


def test_publish_rejects_a_second_concurrent_publisher(tmp_path: Path) -> None:
    lock = tmp_path / "data" / "run-leases" / "publisher.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("{}", encoding="utf-8")

    with pytest.raises(CliError) as error:
        publish_staged_job(tmp_path, job_ref="aweme-0123456789abcdefabcd")

    assert error.value.code == "publisher_capacity_reached"
    assert error.value.retryable is True
    assert lock.is_file()


def _write_candidate(root: Path, job_ref: str, packet_hash: str, *, title: str) -> Path:
    payload = _payload()
    payload["title"] = title
    path = root / "data" / "tasks" / job_ref / "semantic-v1" / f"{title}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "schema_version": 2,
                "job_ref": job_ref,
                "packet_sha256": packet_hash,
                "content": payload,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _doctor_prerequisites(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = (
        root
        / "data"
        / "models"
        / "huggingface"
        / "hub"
        / "models--Systran--faster-whisper-small"
        / "snapshots"
        / "fixture"
    )
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"fixture")
    monkeypatch.setattr(
        "app.pipeline._check_playwright_chromium",
        lambda **_kwargs: str(root / "browser" / "chrome.exe"),
    )
    monkeypatch.setattr("app.analyze_video.resolve_ffmpeg", lambda: "private-ffmpeg-path")
    monkeypatch.setattr(
        "app.security.windows_acl_metadata",
        lambda _path: {
            "exists": True,
            "acl_check_returncode": 0,
            "access_rules_protected": True,
            "broad_acl_identities": [],
        },
    )


def test_publication_reuses_registered_vault_note_path(tmp_path: Path) -> None:
    database = tmp_path / "data" / "knowledge.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE collection_items(source_id TEXT PRIMARY KEY, job_id TEXT)"
        )
        connection.execute(
            "CREATE TABLE obsidian_publications("
            "source_id TEXT PRIMARY KEY, note_path TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO collection_items VALUES ('private-source', 'safe-job-ref')"
        )
        connection.execute(
            "INSERT INTO obsidian_publications VALUES "
            "('private-source', '40-Resources/existing-note.md')"
        )

    assert _registered_vault_note(database, "safe-job-ref") == Path(
        "40-Resources/existing-note.md"
    )
    assert _registered_vault_note(database, "unpublished-job") is None


def test_doctor_accepts_installed_browser_path_and_validates_cookie(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke(["--root", str(tmp_path), "init", "--json"], capsys)[0] == 0
    _doctor_prerequisites(tmp_path, monkeypatch)
    cookie = tmp_path / "config" / "cookies.json"
    cookie.write_text(
        json.dumps(
            {
                "ttwid": "private-one",
                "odin_tt": "private-two",
                "passport_csrf_token": "private-three",
            }
        ),
        encoding="utf-8",
    )

    code, ready = invoke(["--root", str(tmp_path), "doctor", "--json"], capsys)
    assert code == 0
    assert ready["data"]["checks"]["chromium_runtime"] is True
    assert ready["data"]["ready_for_login"] is True
    assert ready["data"]["ready_for_sync"] is True

    cookie.write_text("not-json", encoding="utf-8")
    code, invalid = invoke(["--root", str(tmp_path), "doctor", "--json"], capsys)
    assert code == 0
    assert invalid["data"]["checks"]["cookie_valid"] is False
    assert invalid["data"]["ready_for_sync"] is False
    assert "run_confirmed_login" in invalid["data"]["repair_actions"]


def test_rejected_candidate_can_be_replaced_without_reusing_old_approval(
    tmp_path: Path,
) -> None:
    job_ref = _fixture(tmp_path)
    packet_hash = export_packet(tmp_path, job_ref)["packet_sha256"]
    first = _write_candidate(tmp_path, job_ref, packet_hash, title="first candidate")
    imported = import_candidate(tmp_path, job_ref, first)
    first_hash = imported["candidate_sha256"]
    database = tmp_path / "data" / "knowledge.db"
    record_review(tmp_path, database, job_ref=job_ref, decision="reject", note="revise")

    second = _write_candidate(tmp_path, job_ref, packet_hash, title="second candidate")
    replaced = import_candidate(tmp_path, job_ref, second)

    assert replaced["replaced"] is True
    assert replaced["candidate_sha256"] != first_hash
    assert approved_candidate(tmp_path, database, job_ref) is False
    history = tmp_path / "quarantine" / "candidates" / job_ref / f"{first_hash}.json"
    assert history.is_file()
    record_review(tmp_path, database, job_ref=job_ref, decision="approve", note="")
    assert approved_candidate(tmp_path, database, job_ref) is True


def test_rejected_candidate_can_be_replaced_at_official_output_handle(tmp_path: Path) -> None:
    job_ref = _fixture(tmp_path)
    packet = export_packet(tmp_path, job_ref)
    official = tmp_path / packet["candidate_output_handle"]
    first = _write_candidate(tmp_path, job_ref, packet["packet_sha256"], title="official first")
    official.write_bytes(first.read_bytes())
    imported = import_candidate(tmp_path, job_ref, official)
    database = tmp_path / "data" / "knowledge.db"
    record_review(tmp_path, database, job_ref=job_ref, decision="reject", note="revise")

    second = _write_candidate(tmp_path, job_ref, packet["packet_sha256"], title="official second")
    official.write_bytes(second.read_bytes())
    replaced = import_candidate(tmp_path, job_ref, official)

    assert replaced["replaced"] is True
    assert replaced["reused"] is False
    history = (
        tmp_path
        / "quarantine"
        / "candidates"
        / job_ref
        / f"{imported['candidate_sha256']}.json"
    )
    assert history.is_file()


def test_packet_refresh_makes_candidate_review_stale_for_staging_and_publish(
    tmp_path: Path,
) -> None:
    job_ref = _fixture(tmp_path)
    packet_hash = export_packet(tmp_path, job_ref)["packet_sha256"]
    candidate = _write_candidate(tmp_path, job_ref, packet_hash, title="fresh candidate")
    import_candidate(tmp_path, job_ref, candidate)
    database = tmp_path / "data" / "knowledge.db"
    record_review(tmp_path, database, job_ref=job_ref, decision="approve", note="")
    assert approved_candidate(tmp_path, database, job_ref) is True

    transcript = tmp_path / "data" / "jobs" / job_ref / "analysis" / "transcript.md"
    transcript.write_text(
        transcript.read_text(encoding="utf-8") + "\nnew evidence\n",
        encoding="utf-8",
    )
    export_packet(tmp_path, job_ref)

    assert approved_candidate(tmp_path, database, job_ref) is False
    with pytest.raises(CliError) as staging:
        run_job(tmp_path, job_ref=job_ref, stop_after="staging")
    assert staging.value.code == "candidate_stale"
    with pytest.raises(CliError) as publishing:
        publish_staged_job(tmp_path, job_ref=job_ref)
    assert publishing.value.code == "candidate_stale"


def test_invalid_analysis_manifest_is_rebuilt_instead_of_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_ref = "aweme-0123456789abcdefabcd"
    source = tmp_path / "data" / "jobs" / job_ref / "source.mp4"
    analysis = source.parent / "analysis"
    analysis.mkdir(parents=True)
    source.write_bytes(b"local media")
    (analysis / "manifest.json").write_text("{}", encoding="utf-8")
    calls: list[str] = []

    def rebuild(_command: list[str], *, stage: str, timeout: int) -> None:
        calls.append(stage)
        (analysis / "manifest.json").write_text('{"rebuilt": true}', encoding="utf-8")

    monkeypatch.setattr("douyin_knowledge.operations._run_private", rebuild)

    assert _analyze(tmp_path, job_ref) is False
    assert calls == ["analysis"]


def test_run_failure_budget_blocks_third_attempt_and_success_clears_stage_failures(
    tmp_path: Path,
) -> None:
    job_ref = "aweme-0123456789abcdefabcd"
    database = tmp_path / "data" / "knowledge.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE collection_items(source_id TEXT, job_id TEXT, status TEXT, "
            "last_position INTEGER)"
        )
        connection.execute(
            "INSERT INTO collection_items VALUES ('private-source', ?, 'downloaded', 1)",
            (job_ref,),
        )
    source = tmp_path / "data" / "jobs" / job_ref / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"local media")
    checkpoint = tmp_path / "data" / "tasks" / job_ref / "run-checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_ref": job_ref,
                "status": "paused",
                "current_stage": "download",
                "error": "download_failed",
                "same_failure_count": 2,
                "stages": {},
                "failures": {"download:download_failed": 2},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CliError) as blocked:
        run_job(tmp_path, job_ref=job_ref, stop_after="download")
    assert blocked.value.code == "run_failure_limit_reached"

    assert (
        run_job(
            tmp_path,
            job_ref=job_ref,
            stop_after="download",
            retry_after_fix=True,
        )["status"]
        == "downloaded"
    )
    completed = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert "download:download_failed" not in completed["failures"]
    assert completed["same_failure_count"] == 0
    assert completed["failure_limit_reset_at"]


def test_status_reports_active_job_and_stage(tmp_path: Path, capsys) -> None:
    job_ref = "aweme-0123456789abcdefabcd"
    task = tmp_path / "data" / "tasks" / job_ref
    task.mkdir(parents=True)
    (task / "run.lock").write_text("{}", encoding="utf-8")
    (task / "run-checkpoint.json").write_text(
        json.dumps({"job_ref": job_ref, "status": "running", "current_stage": "analysis"}),
        encoding="utf-8",
    )

    code, payload = invoke(["--root", str(tmp_path), "status", "--json"], capsys)
    assert code == 0
    assert payload["data"]["active_stage"] == "analysis"
    assert payload["data"]["active_job_ref"] == job_ref


def test_batch_plan_requires_successful_canary(tmp_path: Path, capsys) -> None:
    job_ref = _fixture(tmp_path)
    code, blocked = invoke(["--root", str(tmp_path), "plan", "--limit", "2", "--json"], capsys)
    assert code == 2
    assert blocked["error"]["code"] == "canary_required"

    code, canary = invoke(
        [
            "--root",
            str(tmp_path),
            "canary",
            "--limit",
            "1",
            "--no-publish",
            "--confirm",
            "--json",
        ],
        capsys,
    )
    assert code == 0
    assert canary["data"]["job_ref"] == job_ref
    assert (tmp_path / "data" / "safety" / "canary-v1.json").is_file()
    code, planned = invoke(["--root", str(tmp_path), "plan", "--limit", "2", "--json"], capsys)
    assert code == 0
    assert planned["data"]["limit"] == 2


def test_canary_status_filter_selects_a_new_item(tmp_path: Path, capsys) -> None:
    analyzed_job_ref = _fixture(tmp_path)
    new_job_ref = "aweme-11111111111111111111"
    database = tmp_path / "data" / "knowledge.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE collection_items SET status = 'analyzed' WHERE job_id = ?",
            (analyzed_job_ref,),
        )
        existing = connection.execute(
            "SELECT * FROM collection_items WHERE job_id = ?", (analyzed_job_ref,)
        ).fetchone()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(collection_items)")]
        values = list(existing)
        values[columns.index("source_id")] = "private-new-source"
        values[columns.index("aweme_id")] = "private-new-aweme"
        values[columns.index("job_id")] = new_job_ref
        values[columns.index("status")] = "new"
        values[columns.index("last_position")] = 2
        connection.execute(
            f"INSERT INTO collection_items({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
    shutil.copytree(
        tmp_path / "data" / "jobs" / analyzed_job_ref,
        tmp_path / "data" / "jobs" / new_job_ref,
    )

    code, payload = invoke(
        [
            "--root",
            str(tmp_path),
            "canary",
            "--limit",
            "1",
            "--status",
            "new",
            "--no-publish",
            "--confirm",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    assert payload["data"]["job_ref"] == new_job_ref
