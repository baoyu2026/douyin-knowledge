from __future__ import annotations

import argparse
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

import yaml

FORBIDDEN_NAMES = {
    "PROJECT-MEMORY.local.md",
    "runtime.local.json",
    "gateway-state-v1.json",
    "cookies.json",
    "candidate-v1.json",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".mp3",
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".wav",
}
FRONTMATTER_PATTERN = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)


def _frontmatter(skill_file: Path) -> dict[str, Any]:
    content = skill_file.read_text(encoding="utf-8-sig")
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        raise ValueError("SKILL.md must start with YAML frontmatter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md frontmatter must be an object")
    return metadata


def validate_skill(skill_source: Path) -> list[Path]:
    if not skill_source.is_dir() or skill_source.is_symlink():
        raise ValueError("skill source must be a real directory")
    skill_file = skill_source / "SKILL.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        raise ValueError("SKILL.md is missing or unsafe")

    metadata = _frontmatter(skill_file)
    if metadata.get("name") != skill_source.name:
        raise ValueError("skill name must match its directory")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("skill description is required")
    if metadata.get("agent_created") is not True:
        raise ValueError("WorkBuddy skills must set agent_created: true")
    if metadata.get("allowed-tools") != "mcp__douyin-knowledge":
        raise ValueError("WorkBuddy skill must be restricted to the Douyin MCP server")

    files: list[Path] = []
    for path in sorted(skill_source.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError(f"skill package must not contain symlinks: {path.name}")
        if not path.is_file():
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise ValueError(f"private runtime artifact is not allowed: {path.name}")
        files.append(path)
    if files == [skill_file] or not files:
        raise ValueError("skill package is incomplete")
    return files


def _write_zip(destination: Path, skill_source: Path, files: list[Path]) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(skill_source.parent).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    os.replace(temporary, destination)


def _write_json(destination: Path, value: dict[str, Any]) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, destination)


def build_bundle(skill_source: Path, launcher: Path, output_directory: Path) -> dict[str, Any]:
    skill_source = skill_source.resolve()
    launcher = launcher.resolve()
    output_directory = output_directory.resolve()
    if not launcher.is_file() or launcher.is_symlink() or launcher.suffix.casefold() != ".ps1":
        raise ValueError("MCP launcher must be a real PowerShell script")
    if output_directory == skill_source or skill_source in output_directory.parents:
        raise ValueError("output directory must be outside the skill source")

    files = validate_skill(skill_source)
    output_directory.mkdir(parents=True, exist_ok=True)
    skill_archive = output_directory / "douyin-knowledge.zip"
    mcp_config = output_directory / "douyin-knowledge.mcp.json"
    _write_zip(skill_archive, skill_source, files)
    _write_json(
        mcp_config,
        {
            "mcpServers": {
                "douyin-knowledge": {
                    "type": "stdio",
                    "command": "powershell.exe",
                    "args": [
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(launcher),
                    ],
                    "description": "抖音收藏知识库候选生成网关",
                }
            }
        },
    )
    return {
        "ok": True,
        "created": [skill_archive.name, mcp_config.name],
        "skill_files": len(files),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build-workbuddy-bundle")
    parser.add_argument("--skill-source", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_bundle(args.skill_source, args.launcher, args.output)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
