"""orchestrator CLI — verify-run, verify-campaign, rotate-logs."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.log_rotation import rotate_logs
from core.paths import CAMPAIGN_TEMPLATES_DIR, LOG_DIR, PROJECTS_DIR
from manifest import verify_campaign_merkle, verify_run_manifest


def _resolve_run_dir(run_id: str, projects_dir: Path) -> Path | None:
    """Find <projects_dir>/<project>/runs/<run_id>/.
    Returns None and prints an error if 0 or >1 matches."""
    if not projects_dir.is_dir():
        print(f"error: projects directory not found: {projects_dir}", file=sys.stderr)
        return None
    matches: list[Path] = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / "runs" / run_id
        if candidate.is_dir():
            matches.append(candidate)
    if not matches:
        print(f"error: run_id {run_id!r} not found under {projects_dir}", file=sys.stderr)
        return None
    if len(matches) > 1:
        print(
            f"error: run_id {run_id!r} matches multiple projects: "
            f"{[str(m) for m in matches]}",
            file=sys.stderr,
        )
        return None
    return matches[0]


def _cmd_verify_run(args: argparse.Namespace) -> int:
    projects_dir = Path(args.projects_dir) if args.projects_dir else Path(PROJECTS_DIR)
    run_dir = _resolve_run_dir(args.run_id, projects_dir)
    if run_dir is None:
        return 1
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


def _cmd_rotate_logs(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir) if args.log_dir else Path(LOG_DIR)
    dry_run: bool = args.dry_run

    if dry_run:
        # Walk without modifying; report what would happen.
        from datetime import datetime  # noqa: PLC0415

        summary = rotate_logs(
            log_dir,
            gzip_after_days=args.gzip_after_days,
            delete_after_days=args.delete_after_days,
            now=datetime.utcnow(),
            dry_run=True,
        )
        for p in summary.gzipped:
            print(f"would gzip: {p}")
        for p in summary.deleted:
            print(f"would delete: {p}")
        for e in summary.errors:
            print(f"error: {e}", file=sys.stderr)
        print(
            f"dry-run complete: {len(summary.gzipped)} would gzip, "
            f"{len(summary.deleted)} would delete"
        )
        return 0

    summary = rotate_logs(
        log_dir,
        gzip_after_days=args.gzip_after_days,
        delete_after_days=args.delete_after_days,
    )
    print(
        f"rotation complete: {len(summary.gzipped)} gzipped, "
        f"{len(summary.deleted)} deleted, {len(summary.errors)} errors"
    )
    for e in summary.errors:
        print(f"  error: {e}", file=sys.stderr)
    return 1 if summary.errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="AI Orchestrator CLI — verify run and campaign integrity.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("verify-run", help="verify a single run's SHA256 manifest")
    p_run.add_argument("run_id", metavar="RUN_ID", help="run ID (UUID) to verify")
    p_run.add_argument(
        "--projects-dir",
        default=None,
        help=f"override projects directory (default: {PROJECTS_DIR})",
    )
    p_run.set_defaults(func=_cmd_verify_run)

    p_camp = sub.add_parser("verify-campaign", help="verify a campaign's Merkle root")
    p_camp.add_argument("campaign_id", metavar="CAMPAIGN_ID", help="campaign ID (UUID) to verify")
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

    p_rotate = sub.add_parser(
        "rotate-logs",
        help="rotate logs in LOG_DIR (gzip >1d, delete >90d)",
    )
    p_rotate.add_argument(
        "--log-dir",
        default=None,
        help=f"override log directory (default: {LOG_DIR})",
    )
    p_rotate.add_argument(
        "--gzip-after-days",
        type=int,
        default=1,
        help="gzip .log files older than this many days (default: 1)",
    )
    p_rotate.add_argument(
        "--delete-after-days",
        type=int,
        default=90,
        help="delete .log.gz files older than this many days (default: 90)",
    )
    p_rotate.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen without modifying any files",
    )
    p_rotate.set_defaults(func=_cmd_rotate_logs)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
