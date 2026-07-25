from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from douyin_knowledge.cli import main

ENVELOPE_KEYS = {
    "schema_version",
    "ok",
    "operation",
    "data",
    "error",
    "warnings",
    "safe_summary",
}


def invoke(args: list[str], capsys) -> tuple[int, dict[str, object]]:
    code = main(args)
    output = capsys.readouterr().out
    return code, json.loads(output)


def all_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in all_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in all_strings(child)]
    return []


def test_init_is_idempotent_and_returns_safe_envelope(tmp_path: Path, capsys) -> None:
    args = ["--root", str(tmp_path), "init", "--json"]
    first_code, first = invoke(args, capsys)
    second_code, second = invoke(args, capsys)

    assert first_code == second_code == 0
    assert set(first) == ENVELOPE_KEYS
    assert first["ok"] is True
    assert first["operation"] == "init"
    assert second["data"]["reused"] is True
    assert (tmp_path / "config" / "config.yml").is_file()
    assert (tmp_path / "data" / "jobs").is_dir()
    assert (tmp_path / "data" / "tasks").is_dir()
    assert (tmp_path / "schemas" / "structured-content-v1.schema.json").is_file()
    assert (tmp_path / "schemas" / "cli-envelope-v1.schema.json").is_file()
    assert (tmp_path / "schemas" / "config-v1.schema.json").is_file()
    assert str(tmp_path).casefold() not in "\n".join(all_strings(first)).casefold()


def test_status_reads_collection_registry_not_legacy_media_jobs(
    tmp_path: Path, capsys
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "knowledge.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE collection_items(status TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO collection_items(status) VALUES (?)",
            [("completed",), ("new",), ("new",), ("analyzed",)],
        )
        connection.execute("CREATE TABLE media_jobs(status TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO media_jobs(status) VALUES (?)", [("ignored",)] * 9
        )

    code, payload = invoke(
        ["--root", str(tmp_path), "status", "--json"], capsys
    )

    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["total"] == 4
    assert payload["data"]["by_status"] == {
        "analyzed": 1,
        "completed": 1,
        "new": 2,
    }


def test_invalid_limit_returns_stable_error_contract(tmp_path: Path, capsys) -> None:
    code, payload = invoke(
        ["--root", str(tmp_path), "plan", "--limit", "0", "--json"], capsys
    )

    assert code == 2
    assert set(payload) == ENVELOPE_KEYS
    assert payload["ok"] is False
    assert payload["error"] == {
        "code": "invalid_limit",
        "message": "limit must be between 1 and 5",
        "retryable": False,
        "preserved_checkpoint": True,
        "user_action": "choose a limit from 1 to 5 after a successful canary",
    }


def test_plan_rejects_batch_larger_than_public_safety_limit(tmp_path: Path, capsys) -> None:
    code, payload = invoke(
        ["--root", str(tmp_path), "plan", "--limit", "6", "--json"], capsys
    )
    assert code == 2
    assert payload["error"]["code"] == "invalid_limit"


def test_argument_errors_use_json_contract(capsys) -> None:
    code, payload = invoke(["unknown-command"], capsys)
    assert code == 2
    assert set(payload) == ENVELOPE_KEYS
    assert payload["operation"] == "cli"
    assert payload["error"]["code"] == "invalid_arguments"


def test_doctor_reports_safe_capability_matrix(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    code, _initialized = invoke(["--root", str(tmp_path), "init", "--json"], capsys)
    assert code == 0
    database = tmp_path / "data" / "knowledge.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fixture(value TEXT)")
    (tmp_path / "config" / "cookies.json").write_text("private", encoding="utf-8")
    model = (
        tmp_path
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
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (tmp_path / "config" / "obsidian.yml").write_text("vault: vault\n", encoding="utf-8")
    monkeypatch.setattr("app.pipeline._check_playwright_chromium", lambda **_kwargs: "available")
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

    code, payload = invoke(["--root", str(tmp_path), "doctor", "--json"], capsys)

    assert code == 0
    data = payload["data"]
    assert data["ready"] is True
    assert data["ready_for_login"] is True
    assert data["ready_for_sync"] is True
    assert data["ready_for_analysis"] is True
    assert data["ready_for_publish"] is True
    assert data["repair_actions"] == []
    assert str(tmp_path).casefold() not in "\n".join(all_strings(payload)).casefold()
