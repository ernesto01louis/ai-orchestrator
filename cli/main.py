"""orchestrator CLI — verify-run and verify-campaign."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.paths import CAMPAIGN_TEMPLATES_DIR, PROJECTS_DIR
from manifest import verify_campaign_merkle, verify_run_manifest


def _resolve_run_dir(run_id: str, projects_dir: Path) -> Path:
    """Find <projects_dir>/<project>/runs/<run_id>/ by scanning project subdirs.

    Errors with sys.exit(1) if zero or >1 matches.
    """
    matches: list[Path] = []
    if not projects_dir.is_dir():
        print(f"error: projects directory {projects_dir} does not exist", file=sys.stderr)
        sys.exit(1)
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / "runs" / run_id
        if candidate.is_dir():
            matches.append(candidate)
    if len(matches) == 0:
        print(f"error: run_id {run_id!r} not found under {projects_dir}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(
            f"error: run_id {run_id!r} matches multiple projects: {[str(m) for m in matches]}",
            file=sys.stderr,
        )
        sys.exit(1)
    return matches[0]


def _cmd_verify_run(args: argparse.Namespace) -> int:
    projects_dir = Path(args.projects_dir) if args.projects_dir else Path(PROJECTS_DIR)
    run_dir = _resolve_run_dir(args.run_id, projects_dir)
    result = verify_run_manifest(run_dir)
    if result.valid:
        print(f"OK: {args.run_id} (manifest verified, {len(result.expected or {})} files)")
        return 0
    print(f"FAIL: {args.run_id} status={result.status}", file=sys.stderr)
    for m in result.mismatches:
        print(f"  - {m}", file=sys.stderr)
    return 1


def _cmd_verify_campaign(args: argparse.Namespace) -> int:
    campaigns_dir = Path(args.campaigns_dir) if args.campaigns_dir else Path(CAMPAIGN_TEMPLATES_DIR)
    projects_dir = Path(args.projects_dir) if args.projects_dir else Path(PROJECTS_DIR)
    campaign_dir = campaigns_dir / args.campaign_id
    if not campaign_dir.is_dir():
        print(
            f"error: campaign_id {args.campaign_id!r} not found under {campaigns_dir}",
            file=sys.stderr,
        )
        return 1
    result = verify_campaign_merkle(campaign_dir, projects_dir)
    if result.valid:
        print(f"OK: campaign {args.campaign_id} (Merkle root verified)")
        return 0
    print(f"FAIL: campaign {args.campaign_id} status={result.status}", file=sys.stderr)
    for m in result.mismatches:
        print(f"  - {m}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="AI Orchestrator CLI — verify run and campaign integrity.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("verify-run", help="verify a single run's SHA256 manifest")
    p_run.add_argument("run_id")
    p_run.add_argument(
        "--projects-dir",
        default=None,
        help=f"override projects directory (default: {PROJECTS_DIR})",
    )
    p_run.set_defaults(func=_cmd_verify_run)

    p_camp = sub.add_parser("verify-campaign", help="verify a campaign's Merkle root")
    p_camp.add_argument("campaign_id")
    p_camp.add_argument(
        "--campaigns-dir",
        default=None,
        help=f"override campaigns directory (default: {CAMPAIGN_TEMPLATES_DIR})",
    )
    p_camp.add_argument(
        "--projects-dir",
        default=None,
        help=f"override projects directory (default: {PROJECTS_DIR})",
    )
    p_camp.set_defaults(func=_cmd_verify_campaign)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
