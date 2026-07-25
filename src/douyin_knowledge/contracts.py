from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CliError(Exception):
    code: str
    message: str
    user_action: str
    retryable: bool = False
    preserved_checkpoint: bool = True
    exit_code: int = 2


def success(
    operation: str,
    data: dict[str, Any],
    *,
    summary: str,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ok": True,
        "operation": operation,
        "data": data,
        "error": None,
        "warnings": warnings or [],
        "safe_summary": summary,
    }


def failure(operation: str, error: CliError) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ok": False,
        "operation": operation,
        "data": {},
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "preserved_checkpoint": error.preserved_checkpoint,
            "user_action": error.user_action,
        },
        "warnings": [],
        "safe_summary": error.message,
    }
