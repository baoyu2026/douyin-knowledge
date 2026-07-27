from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path
from types import ModuleType

import pytest


def load_builder() -> ModuleType:
    repository = Path(__file__).resolve().parents[1]
    path = repository / "scripts" / "build-workbuddy-bundle.py"
    spec = importlib.util.spec_from_file_location("build_workbuddy_bundle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workbuddy_bundle_is_uploadable_and_uses_local_stdio(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    skill_source = repository / "skills" / "workbuddy" / "douyin-knowledge"
    launcher = tmp_path / "douyin-knowledge-mcp.ps1"
    launcher.write_text("exit 0\n", encoding="utf-8")
    output = tmp_path / "upload"

    result = load_builder().build_bundle(skill_source, launcher, output)

    assert result == {
        "ok": True,
        "created": ["douyin-knowledge.zip", "douyin-knowledge.mcp.json"],
        "skill_files": 2,
    }
    with zipfile.ZipFile(output / "douyin-knowledge.zip") as archive:
        assert archive.namelist() == [
            "douyin-knowledge/SKILL.md",
            "douyin-knowledge/references/gateway-workflow.md",
        ]
        skill = archive.read("douyin-knowledge/SKILL.md").decode("utf-8")
        assert "agent_created: true" in skill
        assert "allowed-tools: mcp__douyin-knowledge" in skill

    config_text = (output / "douyin-knowledge.mcp.json").read_text(encoding="utf-8")
    config = json.loads(config_text)
    server = config["mcpServers"]["douyin-knowledge"]
    assert server["type"] == "stdio"
    assert server["command"] == "powershell.exe"
    assert server["args"][-1] == str(launcher.resolve())
    assert "runtime.local.json" not in config_text
    assert "instance_root" not in config_text


def test_workbuddy_bundle_rejects_private_runtime_artifacts(tmp_path: Path) -> None:
    skill_source = tmp_path / "douyin-knowledge"
    skill_source.mkdir()
    (skill_source / "SKILL.md").write_text(
        "---\n"
        "name: douyin-knowledge\n"
        "description: candidate gateway\n"
        "agent_created: true\n"
        "allowed-tools: mcp__douyin-knowledge\n"
        "---\n\n"
        "Read references/contract.md.\n",
        encoding="utf-8",
    )
    references = skill_source / "references"
    references.mkdir()
    (references / "contract.md").write_text("contract\n", encoding="utf-8")
    (skill_source / "runtime.local.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="private runtime artifact"):
        load_builder().validate_skill(skill_source)
