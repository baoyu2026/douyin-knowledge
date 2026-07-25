from __future__ import annotations

import argparse
import importlib.resources
import importlib.util
import json
import locale
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REQUIRED_COOKIE_KEYS = {"ttwid", "odin_tt", "passport_csrf_token"}
ALLOWED_COOKIE_RELATIVE = Path("config/cookies.json")
LOGIN_BLOCK_RELATIVE = Path("config/cookie-login.blocked")
UNEXPECTED_COOKIE_RELATIVES = (
    Path(".cookies.json"),
    Path("config/.cookies.json"),
    Path("vendor/douyin-downloader/.cookies.json"),
    Path("vendor/douyin-downloader/config/cookies.json"),
)
ALLOWED_CONFIG_KEYS = {
    "link",
    "path",
    "mode",
    "number",
    "increase",
    "music",
    "cover",
    "avatar",
    "json",
    "folderstyle",
    "author_dir",
    "video_quality",
    "thread",
    "rate_limit",
    "retry_times",
    "proxy",
    "database",
    "database_path",
    "auto_cookie",
    "progress",
    "transcript",
    "browser_fallback",
    "comments",
    "notifications",
}
BROAD_ACL_IDENTITIES = (
    "Everyone",
    "BUILTIN\\Users",
    "NT AUTHORITY\\Authenticated Users",
    "Authenticated Users",
)


class GateError(RuntimeError):
    def __init__(self, message: str, *, reason: str = "security_gate_failed") -> None:
        super().__init__(message)
        self.reason = reason


def project_root_from(path: Path | None = None) -> Path:
    return (path or Path(__file__).resolve().parents[1]).resolve()


def resolve_project_path(root: Path, relative: Path, field: str) -> Path:
    if relative.is_absolute():
        raise GateError(f"{field} 必须是项目内相对路径")
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise GateError(f"{field} 解析后越出项目根目录: {relative}") from exc
    return candidate


def allowed_cookie_path(root: Path) -> Path:
    return resolve_project_path(root, ALLOWED_COOKIE_RELATIVE, "Cookie 路径")


def login_block_path(root: Path) -> Path:
    return (root / LOGIN_BLOCK_RELATIVE).resolve()


def validate_project_structure(root: Path) -> None:
    installation = Path(__file__).resolve().parents[1]
    code_root = root if (root / "app" / "pipeline.py").is_file() else installation
    required = [
        code_root / "app" / "pipeline.py",
        code_root / "vendor" / "douyin-downloader" / "run.py",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing and importlib.util.find_spec("tools.cookie_fetcher") is not None:
        missing = []
    if missing:
        raise GateError("项目结构不完整: " + ", ".join(missing))


def unexpected_cookie_candidates(root: Path) -> list[Path]:
    resolved_root = root.resolve()
    unexpected: list[Path] = []
    for relative in UNEXPECTED_COOKIE_RELATIVES:
        candidate = resolved_root / relative
        if os.path.lexists(candidate):
            unexpected.append(candidate)
    return unexpected


def block_unexpected_cookie_candidates(root: Path) -> None:
    unexpected = unexpected_cookie_candidates(root)
    if unexpected:
        joined = ", ".join(str(path) for path in unexpected)
        raise GateError("存在非预期 Cookie 候选路径（未读取内容）: " + joined)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact parser errors vary
        raise GateError(f"YAML 不可解析: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GateError("downloader.yml 必须解析为映射")
    return data


def _ensure_inside_root(path_value: Any, root: Path, field: str) -> None:
    if not isinstance(path_value, str) or not path_value:
        raise GateError(f"{field} 必须是非空路径字符串")
    resolved_root = root.resolve()
    resolved = Path(path_value).expanduser()
    if not resolved.is_absolute():
        resolved = resolved_root / resolved
    try:
        resolved.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise GateError(f"{field} 越出项目根目录: {path_value}") from exc


def validate_downloader_config(root: Path) -> dict[str, Any]:
    config_path = root / "config" / "downloader.yml"
    try:
        config_exists = config_path.exists()
    except OSError as exc:
        raise GateError(f"无法访问 config/downloader.yml: {exc}") from exc
    if not config_exists:
        raise GateError("缺少 config/downloader.yml")
    data = _load_yaml_mapping(config_path)

    unknown = set(data) - ALLOWED_CONFIG_KEYS
    if unknown:
        raise GateError("downloader.yml 含未知顶层键: " + ", ".join(sorted(unknown)))
    if {"cookie", "cookies"} & set(data):
        raise GateError("downloader.yml 不得包含内联 Cookie")
    if data.get("auto_cookie") is not False:
        raise GateError("auto_cookie 必须为 false，由项目侧唯一 Cookie 入口显式加载")
    if data.get("mode") != ["collect"]:
        raise GateError("mode 必须为 ['collect']")
    if (data.get("number") or {}).get("collect") != 20:
        raise GateError("number.collect 必须为 20")
    if data.get("video_quality") != "720p":
        raise GateError("video_quality 必须为 720p")
    if data.get("thread") != 2:
        raise GateError("thread 必须为 2")
    if data.get("rate_limit") != 1:
        raise GateError("rate_limit 必须为 1")
    for section in ("transcript", "browser_fallback", "comments", "notifications"):
        value = data.get(section) or {}
        if not isinstance(value, dict) or value.get("enabled") is not False:
            raise GateError(f"{section}.enabled 必须为 false")
    _ensure_inside_root(data.get("path"), root, "path")
    _ensure_inside_root(data.get("database_path"), root, "database_path")
    return data


def validate_cookie_file(path: Path) -> set[str]:
    if not path.exists():
        raise GateError(f"缺少 Cookie 文件: {path}")
    if not path.is_file():
        raise GateError(f"Cookie 路径不是普通文件: {path}")
    if path.stat().st_size <= 0:
        raise GateError(f"Cookie 文件为空: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GateError(f"Cookie 文件不是有效 JSON: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise GateError("Cookie JSON 必须是对象")
    keys = {str(key) for key, value in raw.items() if value}
    missing = REQUIRED_COOKIE_KEYS - keys
    if missing:
        raise GateError("Cookie 缺少必需键: " + ", ".join(sorted(missing)))
    return keys


def load_cookie_values(path: Path) -> dict[str, str]:
    validate_cookie_file(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in raw.items() if value}


def windows_acl_metadata(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"path": str(path), "platform": sys.platform}
    try:
        info["exists"] = path.exists()
    except OSError as exc:
        info["exists"] = False
        info["acl_error"] = str(exc)
        return info
    if not info["exists"] or os.name != "nt":
        return info
    try:
        completed = subprocess.run(
            ["icacls", str(path)],
            text=True,
            encoding=locale.getencoding(),
            errors="replace",
            capture_output=True,
            check=False,
            timeout=10,
        )
    except Exception as exc:  # pragma: no cover - host dependent
        info["acl_error"] = str(exc)
        return info
    acl_text = (completed.stdout or "") + (completed.stderr or "")
    info["acl_check_returncode"] = completed.returncode
    info["access_rules_protected"] = "(I)" not in acl_text
    info["broad_acl_identities"] = [name for name in BROAD_ACL_IDENTITIES if name in acl_text]
    return info


def assert_private_windows_acl(path: Path) -> None:
    metadata = windows_acl_metadata(path)
    if not metadata.get("exists"):
        raise GateError(f"ACL 检查路径不存在: {path}", reason="acl_path_missing")
    if os.name != "nt":
        return
    if metadata.get("acl_check_returncode") != 0:
        raise GateError(f"ACL 检查失败: {path}", reason="acl_check_failed")
    if metadata.get("access_rules_protected") is not True:
        raise GateError(f"ACL 仍启用继承: {path}", reason="acl_inheritance_enabled")
    broad = metadata.get("broad_acl_identities") or []
    if broad:
        raise GateError(
            f"ACL 仍包含宽泛主体 {broad}: {path}",
            reason="acl_broad_access",
        )


def harden_private_project_directory(root: Path, path: Path) -> None:
    root = root.resolve()
    path = path.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise GateError("私有目录必须位于项目根目录内", reason="acl_path_outside_root") from exc
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
        return
    script = root / "scripts" / "harden-acl.ps1"
    if not script.is_file():
        script = Path(__file__).resolve().parents[1] / "scripts" / "harden-acl.ps1"
    if not script.is_file():
        resource = importlib.resources.files("douyin_knowledge").joinpath(
            "resources", "harden-acl.ps1"
        )
        script = Path(str(resource))
    if not script.is_file():
        raise GateError("缺少 ACL 加固脚本", reason="acl_hardening_script_missing")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Root",
            str(root),
            "-Paths",
            str(relative),
        ],
        text=True,
        encoding=locale.getencoding(),
        errors="replace",
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise GateError("私有目录 ACL 加固失败", reason="acl_hardening_failed")
    assert_private_windows_acl(path)


def assert_sensitive_directories_private(root: Path) -> None:
    for name in ("config", "data", "output", "logs"):
        assert_private_windows_acl(root / name)


def login_preflight(root: Path) -> None:
    block_unexpected_cookie_candidates(root)
    validate_project_structure(root)
    validate_downloader_config(root)
    assert_sensitive_directories_private(root)


def sync_preflight(root: Path) -> None:
    login_preflight(root)
    if login_block_path(root).exists():
        raise GateError("上次登录未成功完成，旧 Cookie 已被同步门禁禁用")
    cookie_path = allowed_cookie_path(root)
    validate_cookie_file(cookie_path)
    assert_private_windows_acl(cookie_path)


def metadata_report(root: Path) -> dict[str, Any]:
    config_ok = True
    config_error = None
    try:
        config = validate_downloader_config(root)
    except GateError as exc:
        config_ok = False
        config_error = str(exc)
        config = {}
    paths = [root / name for name in ("config", "data", "output", "logs")]
    cookie = allowed_cookie_path(root)
    if windows_acl_metadata(cookie).get("exists"):
        paths.append(cookie)
    return {
        "config_ok": config_ok,
        "config_error": config_error,
        "transcript_enabled": bool((config.get("transcript") or {}).get("enabled")),
        "allowed_cookie_path": str(cookie),
        "unexpected_cookie_candidates": [str(path) for path in unexpected_cookie_candidates(root)],
        "acl": [windows_acl_metadata(path) for path in paths],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project security gates")
    parser.add_argument(
        "command",
        choices=(
            "login-preflight",
            "sync-preflight",
            "validate-cookie",
            "config",
            "metadata",
        ),
    )
    parser.add_argument("--root", type=Path, default=project_root_from())
    parser.add_argument("--path", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    try:
        if args.command == "login-preflight":
            login_preflight(root)
            result = {"ok": True, "allowed_cookie_path": str(allowed_cookie_path(root))}
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "sync-preflight":
            sync_preflight(root)
            result = {"ok": True, "allowed_cookie_path": str(allowed_cookie_path(root))}
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "validate-cookie":
            if args.path is None:
                raise GateError("validate-cookie 需要 --path")
            keys = validate_cookie_file(args.path.resolve())
            print(json.dumps({"ok": True, "key_count": len(keys)}, ensure_ascii=False))
        elif args.command == "config":
            validate_downloader_config(root)
            print(json.dumps({"ok": True}, ensure_ascii=False))
        else:
            print(json.dumps(metadata_report(root), ensure_ascii=False, indent=2))
    except GateError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
