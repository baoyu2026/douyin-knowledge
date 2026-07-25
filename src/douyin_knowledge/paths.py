from __future__ import annotations

import os
from pathlib import Path


def default_instance_root() -> Path:
    configured = os.environ.get("DOUYIN_KNOWLEDGE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return (Path(os.environ["LOCALAPPDATA"]) / "douyin-knowledge").resolve()
    data_root = os.environ.get("XDG_DATA_HOME")
    if data_root:
        return (Path(data_root) / "douyin-knowledge").expanduser().resolve()
    return (Path.home() / ".local" / "share" / "douyin-knowledge").resolve()


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]
