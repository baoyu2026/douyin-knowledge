from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.probe_one import CollectResult, stable_job_id, write_job_state
from app.publish_library import PublicationError, safe_component

SENSITIVE_RELATIVE_PATH = Path("config/cookies.json")


class MigrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MigrationPlan:
    job_id: str
    source: Path
    analysis: Path | None
    archive_sources: tuple[Path, ...]

    def public_payload(self, root: Path, *, mode: str) -> dict[str, Any]:
        return {
            "status": "planned" if mode == "plan" else "applied",
            "mode": mode,
            "job_id": self.job_id,
            "correct_source": str(self.source.relative_to(root)),
            "analysis_included": self.analysis is not None,
            "archive_count": len(self.archive_sources),
        }


def _inside_root(root: Path, value: Path, *, must_exist: bool = True) -> Path:
    candidate = value if value.is_absolute() else root / value
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise MigrationError("path_outside_root", "迁移路径必须位于项目根目录内") from exc
    if relative == SENSITIVE_RELATIVE_PATH:
        raise MigrationError("sensitive_path_blocked", "该路径不允许作为迁移输入")
    if must_exist and not resolved.exists():
        raise MigrationError("migration_input_missing", f"迁移输入不存在：{relative}")
    return resolved


def build_plan(
    root: Path,
    *,
    aweme_id: str,
    correct_source: Path,
    correct_analysis: Path | None,
    archive_old: list[Path],
) -> MigrationPlan:
    root = root.resolve()
    source = _inside_root(root, correct_source)
    if not source.is_file() or source.suffix.lower() != ".mp4" or source.stat().st_size <= 0:
        raise MigrationError("correct_source_invalid", "正确样本必须是非空 MP4")
    analysis = _inside_root(root, correct_analysis) if correct_analysis else None
    if analysis is not None and not analysis.is_dir():
        raise MigrationError("correct_analysis_invalid", "分析样本必须是目录")
    archive_sources = tuple(_inside_root(root, path) for path in archive_old)
    try:
        job_id = stable_job_id({"aweme_id": aweme_id})
    except ValueError as exc:
        raise MigrationError("aweme_id_required", "迁移正确样本需要 aweme ID") from exc
    return MigrationPlan(job_id, source, analysis, archive_sources)


def apply_plan(
    root: Path,
    plan: MigrationPlan,
    *,
    aweme_id: str,
    position: int,
    cursor: str | None,
    title: str,
    author: str,
) -> None:
    root = root.resolve()
    job_dir = root / "data" / "jobs" / plan.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan.source, job_dir / "source.mp4")
    if plan.analysis is not None:
        shutil.copytree(plan.analysis, job_dir / "analysis", dirs_exist_ok=True)
    write_job_state(
        job_dir,
        CollectResult(
            {
                "aweme_id": aweme_id,
                "desc": title,
                "author": {"nickname": author},
            },
            position=position,
            cursor=cursor,
        ),
    )
    archive_root = root / "archive" / "旧错误样本"
    for source in plan.archive_sources:
        safe_name = safe_component(source.name, field="归档文件名", slug=True)
        target = archive_root / safe_name
        if target.exists():
            raise MigrationError("archive_collision", f"归档目标已存在：{target.relative_to(root)}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), target)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply migration of the existing probe samples"
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--mode", choices=("plan", "apply"), default="plan")
    parser.add_argument("--aweme-id", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--position", type=int, default=1)
    parser.add_argument("--cursor", help=argparse.SUPPRESS)
    parser.add_argument("--title", default="现有正确样本")
    parser.add_argument("--author", default="")
    parser.add_argument("--correct-source", type=Path, required=True)
    parser.add_argument("--correct-analysis", type=Path)
    parser.add_argument("--archive-old", type=Path, action="append", default=[])
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    try:
        if args.position < 1:
            raise MigrationError("position_invalid", "position 必须从 1 开始")
        plan = build_plan(
            root,
            aweme_id=args.aweme_id,
            correct_source=args.correct_source,
            correct_analysis=args.correct_analysis,
            archive_old=args.archive_old,
        )
        if args.mode == "apply":
            apply_plan(
                root,
                plan,
                aweme_id=args.aweme_id,
                position=args.position,
                cursor=args.cursor,
                title=args.title,
                author=args.author,
            )
        print(json.dumps(plan.public_payload(root, mode=args.mode), ensure_ascii=False))
        return 0
    except (MigrationError, PublicationError, OSError) as exc:
        code = getattr(exc, "code", "migration_failed")
        print(
            json.dumps(
                {"status": "controlled_failure", "code": code, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
