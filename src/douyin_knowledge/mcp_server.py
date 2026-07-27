from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from douyin_knowledge.agent_gateway import AgentGateway, safe_gateway_call
from douyin_knowledge.contracts import CliError
from douyin_knowledge.paths import default_instance_root


def create_server(gateway: AgentGateway):
    from mcp.server.fastmcp import FastMCP, Image

    server = FastMCP(
        "douyin-knowledge",
        instructions=(
            "Candidate-only gateway for one isolated Douyin knowledge assignment. "
            "Read the manifest, every text resource, and open every visual before "
            "submitting one schema-valid candidate. The local CLI remains authoritative."
        ),
    )

    @server.tool(name="douyin_capabilities", structured_output=True)
    def capabilities() -> dict[str, Any]:
        """Report the gateway protocol, limits, and supported release mode."""
        return gateway.capabilities()

    @server.tool(name="douyin_doctor", structured_output=True)
    def doctor() -> dict[str, Any]:
        """Run safe local capability checks without reading credential contents."""
        return safe_gateway_call("gateway_doctor", gateway.doctor)

    @server.tool(name="douyin_status", structured_output=True)
    def status() -> dict[str, Any]:
        """Return safe collection, resource, and publication status."""
        return safe_gateway_call("gateway_status", gateway.status)

    @server.tool(name="douyin_plan", structured_output=True)
    def plan(limit: int = 1, status: str | None = None) -> dict[str, Any]:
        """Plan one to five stable job references without changing local state."""
        return safe_gateway_call(
            "gateway_plan", lambda: gateway.plan(limit=limit, status=status)
        )

    @server.tool(name="douyin_prepare_handoff", structured_output=True)
    def prepare_handoff(job_ref: str, confirmed: bool = False) -> dict[str, Any]:
        """Create one isolated semantic assignment for an already prepared packet."""
        return safe_gateway_call(
            "gateway_prepare_handoff",
            lambda: gateway.prepare_handoff(job_ref, confirmed=confirmed),
        )

    @server.tool(name="douyin_get_manifest", structured_output=True)
    def get_manifest(assignment_ref: str) -> dict[str, Any]:
        """Read the immutable manifest and ordered assignment inventory."""
        return safe_gateway_call(
            "gateway_get_manifest", lambda: gateway.get_manifest(assignment_ref)
        )

    @server.tool(name="douyin_read_text", structured_output=True)
    def read_text(assignment_ref: str, handle: str) -> dict[str, Any]:
        """Read one verified UTF-8 packet, schema, instruction, or evidence chunk."""
        return safe_gateway_call(
            "gateway_read_text", lambda: gateway.read_text(assignment_ref, handle)
        )

    @server.tool(name="douyin_open_visual")
    def open_visual(assignment_ref: str, handle: str):
        """Open one verified keyframe as real MCP image content."""
        try:
            visual = gateway.open_visual(assignment_ref, handle)
        except CliError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from None
        except Exception:
            raise ValueError("gateway_internal_error: visual could not be opened") from None
        image_format = "jpeg" if visual.mime_type == "image/jpeg" else "png"
        return Image(data=visual.content, format=image_format)

    @server.tool(name="douyin_assignment_status", structured_output=True)
    def assignment_status(assignment_ref: str) -> dict[str, Any]:
        """Report missing text and visual reads before candidate submission."""
        return safe_gateway_call(
            "gateway_assignment_status",
            lambda: gateway.assignment_status(assignment_ref),
        )

    @server.tool(name="douyin_submit_candidate", structured_output=True)
    def submit_candidate(
        assignment_ref: str, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        """Atomically write and deterministically ingest one candidate JSON object."""
        return safe_gateway_call(
            "gateway_submit_candidate",
            lambda: gateway.submit_candidate(assignment_ref, candidate),
        )

    @server.tool(name="douyin_cleanup_assignment", structured_output=True)
    def cleanup_assignment(
        assignment_ref: str, confirmed: bool = False
    ) -> dict[str, Any]:
        """Remove one verified isolated handoff and release its semantic slot."""
        return safe_gateway_call(
            "gateway_cleanup_assignment",
            lambda: gateway.cleanup_assignment(assignment_ref, confirmed=confirmed),
        )

    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="douyin-knowledge-mcp")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--workspace", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gateway = AgentGateway(
        instance_root=(args.root or default_instance_root()),
        workspace_root=args.workspace,
    )
    create_server(gateway).run(transport="stdio")
    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
