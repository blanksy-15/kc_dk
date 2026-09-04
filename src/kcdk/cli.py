"""Command-line entry points for routine KCDK workflows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .commentary import CommentaryError
from .discord import DiscordError
from .leaderboard_preview import preview_result_dict, render_leaderboard_previews
from .persistence import connect_database
from .publishing import DiscordStateStore, PublishingError, run_weekly_workflow
from .weekly_runner import WeeklyRunnerError, run_interactive_weekly


def _weekly_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "weekly", help="Import, render, and optionally publish one KCDK week."
    )
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--season", required=True, dest="season_identifier")
    parser.add_argument("--season-name", required=True)
    parser.add_argument("--week", required=True, dest="week_label")
    parser.add_argument("--week-number", type=int)
    parser.add_argument("--year", dest="season_year", type=int)
    parser.add_argument("--contest-name")
    parser.add_argument("--contest-date")
    parser.add_argument(
        "--members", type=Path, default=Path("data/members.csv")
    )
    parser.add_argument(
        "--database", type=Path, default=Path("data/processed/kcdk.sqlite")
    )
    parser.add_argument(
        "--state", type=Path, default=Path("data/processed/discord_state.json")
    )
    parser.add_argument(
        "--tone", choices=("mild", "normal", "ruthless"), default="normal"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Render without contacting Discord."
    )
    parser.add_argument(
        "--generate-commentary",
        action="store_true",
        help="During a dry run, explicitly allow one paid OpenAI request.",
    )
    parser.add_argument(
        "--repost",
        action="store_true",
        help="Explicitly post a weekly recap even if state says it was published.",
    )
    parser.set_defaults(handler=_run_weekly)


def _weekly_runner_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "weekly-runner", help="Safely preflight and interactively publish one week."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        help="DraftKings CSV path; omit to use the Windows file picker.",
    )
    parser.add_argument("--week-number", type=int)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("data/processed/local_config.json"),
        help="Ignored non-secret local defaults file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview using a temporary database; never contact OpenAI or Discord.",
    )
    parser.add_argument(
        "--repost",
        action="store_true",
        help="Allow the confirmation flow to repost an already-published recap.",
    )
    parser.set_defaults(handler=_run_weekly_runner)


def _leaderboard_preview_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "leaderboard-preview",
        help="Render both branded leaderboard PNGs from mock presentation data.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output/preview")
    )
    parser.add_argument(
        "--assets", type=Path, default=Path("assets/branding")
    )
    parser.set_defaults(handler=_run_leaderboard_preview)


def _run_weekly(arguments: argparse.Namespace) -> int:
    connection = connect_database(arguments.database)
    try:
        result = run_weekly_workflow(
            connection,
            csv_path=arguments.csv,
            members_path=arguments.members,
            season_name=arguments.season_name,
            season_identifier=arguments.season_identifier,
            week_label=arguments.week_label,
            week_number=arguments.week_number,
            season_year=arguments.season_year,
            contest_name=arguments.contest_name,
            contest_date=arguments.contest_date,
            tone=arguments.tone,
            dry_run=arguments.dry_run,
            repost=arguments.repost,
            generate_commentary_in_dry_run=arguments.generate_commentary,
            state_store=DiscordStateStore(arguments.state),
        )
    finally:
        connection.close()
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, allow_nan=False))
    return 0


def _run_weekly_runner(arguments: argparse.Namespace) -> int:
    result = run_interactive_weekly(
        csv_path=arguments.csv,
        week_number=arguments.week_number,
        config_path=arguments.config,
        dry_run=arguments.dry_run,
        repost=arguments.repost,
    )
    return 1 if result.status == "blocked" else 0


def _run_leaderboard_preview(arguments: argparse.Namespace) -> int:
    results = render_leaderboard_previews(
        arguments.output_dir, asset_directory=arguments.assets
    )
    print(
        json.dumps(
            [preview_result_dict(result) for result in results],
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m kcdk")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _weekly_parser(subparsers)
    _weekly_runner_parser(subparsers)
    _leaderboard_preview_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (
        PublishingError,
        CommentaryError,
        DiscordError,
        WeeklyRunnerError,
        ValueError,
        OSError,
    ) as exc:
        print(f"KCDK weekly workflow failed: {exc}", file=sys.stderr)
        return 1


__all__ = ["build_parser", "main"]
