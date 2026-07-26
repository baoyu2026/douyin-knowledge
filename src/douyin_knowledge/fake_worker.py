from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any


class FakeWorkerError(RuntimeError):
    pass


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FakeWorkerError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise FakeWorkerError(f"invalid {label}")
    return value


def run_fake_worker(
    packet_path: Path,
    schema_path: Path,
    content_path: Path,
    output_path: Path,
) -> str:
    packet_path = packet_path.resolve()
    packet = _object(packet_path, "packet")
    schema = _object(schema_path.resolve(), "schema")
    content = _object(content_path.resolve(), "fixture content")
    required = schema.get("required")
    expected = [
        "protocol_version",
        "schema_version",
        "job_ref",
        "packet_sha256",
        "content",
    ]
    if required != expected or not isinstance(packet.get("job_ref"), str):
        raise FakeWorkerError("unsupported protocol schema")
    schema_version = schema.get("properties", {}).get("schema_version", {}).get("const")
    if not isinstance(schema_version, int):
        raise FakeWorkerError("unsupported content schema")
    packet_hash = hashlib.sha256(packet_path.read_bytes()).hexdigest()
    candidate = {
        "protocol_version": 1,
        "schema_version": schema_version,
        "job_ref": packet["job_ref"],
        "packet_sha256": packet_hash,
        "content": content,
    }
    encoded = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(encoded).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic protocol test worker")
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--content", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    run_fake_worker(args.packet, args.schema, args.content, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
