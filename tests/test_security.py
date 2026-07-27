from __future__ import annotations

import json
import locale
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import security


def test_windows_acl_metadata_uses_system_encoding_and_handles_empty_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class Result:
        stdout = None
        stderr = "BUILTIN\\Users:(R)"
        returncode = 0

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return Result()

    monkeypatch.setattr(security.os, "name", "nt")
    monkeypatch.setattr(security.subprocess, "run", fake_run)

    metadata = security.windows_acl_metadata(tmp_path)

    assert observed["command"] == ["icacls", str(tmp_path)]
    assert observed["text"] is True
    assert observed["encoding"] == locale.getencoding()
    assert observed["errors"] == "replace"
    assert observed["capture_output"] is True
    assert metadata["acl_check_returncode"] == 0
    assert metadata["broad_acl_identities"] == ["BUILTIN\\Users"]


def test_harden_private_project_directory_prefers_current_acl_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    stale_script = root / "scripts" / "harden-acl.ps1"
    stale_script.parent.mkdir(parents=True)
    stale_script.write_text("# stale fixture\n", encoding="utf-8")
    script = Path(security.__file__).resolve().parents[1] / "scripts" / "harden-acl.ps1"
    target = root / "quarantine" / "content-drafts" / "job"
    observed: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return Result()

    monkeypatch.setattr(security.os, "name", "nt")
    monkeypatch.setattr(security.subprocess, "run", fake_run)
    monkeypatch.setattr(
        security,
        "assert_private_windows_acl",
        lambda path: observed.setdefault("asserted", path),
    )

    security.harden_private_project_directory(root, target)

    command = observed["command"]
    assert command[:7] == [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Root",
    ]
    assert command[-2:] == ["-Paths", str(Path("quarantine/content-drafts/job"))]
    assert observed["encoding"] == locale.getencoding()
    assert observed["errors"] == "replace"
    assert observed["asserted"] == target.resolve()


def test_acl_tool_does_not_depend_on_instance_virtual_environment() -> None:
    repository = Path(__file__).resolve().parents[1]
    for script in (
        repository / "scripts" / "harden-acl.ps1",
        repository / "src" / "douyin_knowledge" / "resources" / "harden-acl.ps1",
    ):
        text = script.read_text(encoding="utf-8")
        assert '.venv\\Scripts\\python.exe' not in text
        assert text.count('"/remove:d"') == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration")
def test_windows_acl_metadata_survives_python_utf8_child_process(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = """
import json
import sys
from pathlib import Path
from app.security import windows_acl_metadata
metadata = windows_acl_metadata(Path(sys.argv[1]))
print(json.dumps({"returncode": metadata.get("acl_check_returncode")}))
"""
    environment = os.environ.copy()
    environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})

    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=project_root,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "UnicodeDecodeError" not in completed.stderr
    assert "TypeError" not in completed.stderr
    payload = json.loads(completed.stdout)
    assert isinstance(payload["returncode"], int)
    assert not list(tmp_path.rglob("source.mp4"))
    assert not list(tmp_path.rglob("single-item-download-handoff.json"))
