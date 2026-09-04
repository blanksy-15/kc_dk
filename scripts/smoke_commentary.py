"""Manual KCDK commentary smoke test.

The default is free and local: it prints the exact dry-run request. Pass
``--live`` explicitly to make exactly one Responses API request.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kcdk.commentary import (  # noqa: E402
    generate_weekly_commentary,
    render_discord_markdown,
)
from kcdk.facts import build_weekly_fact_report  # noqa: E402
from kcdk.persistence import connect_database, import_week  # noqa: E402


def _mock_report():
    temporary_directory = tempfile.TemporaryDirectory(prefix="kcdk-commentary-")
    connection = connect_database(Path(temporary_directory.name) / "smoke.sqlite")
    for week in range(1, 5):
        import_week(
            connection,
            ROOT / "data" / "mock" / f"season_week_{week}.csv",
            ROOT / "data" / "mock" / "members.csv",
            season_name="Mock 2026",
            season_identifier="mock-2026",
            season_year=2026,
            week_number=week,
            contest_name=f"Fictional Week {week}",
            contest_date=f"2026-09-{week:02d}",
        )
    report = build_weekly_fact_report(connection, "mock-2026", max_facts=6)
    return temporary_directory, connection, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Make exactly one real OpenAI Responses API request.",
    )
    parser.add_argument(
        "--tone",
        choices=("mild", "normal", "ruthless"),
        default="normal",
    )
    arguments = parser.parse_args()

    temporary_directory, connection, report = _mock_report()
    try:
        result = generate_weekly_commentary(
            report,
            tone=arguments.tone,
            dry_run=not arguments.live,
        )
        print(result.to_json())
        if result.commentary is not None:
            print("\n--- Discord Markdown preview ---\n")
            print(render_discord_markdown(result.commentary))
    finally:
        connection.close()
        temporary_directory.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
