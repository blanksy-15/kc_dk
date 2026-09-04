"""Local, network-free preview generation for branded leaderboard PNGs."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import tempfile

import pandas as pd

from .analytics import (
    MOVEMENT_DOWN,
    MOVEMENT_NEW,
    MOVEMENT_SAME,
    MOVEMENT_UP,
    season_leaderboard_with_movement,
    tournament_leaderboard_with_movement,
)
from .leaderboard_graphics import (
    BrandingAssetPaths,
    RenderedLeaderboard,
    render_kcdk_standings_png,
    render_tournament_performance_png,
)
from .persistence import connect_database, import_week


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MOCK_DIRECTORY = PROJECT_ROOT / "data" / "mock"
DEFAULT_PREVIEW_DIRECTORY = PROJECT_ROOT / "output" / "preview"


def _import_mock_season(database: Path) -> tuple[object, int]:
    connection = connect_database(database)
    members = MOCK_DIRECTORY / "members.csv"
    for week in range(1, 5):
        import_week(
            connection,
            MOCK_DIRECTORY / f"season_week_{week}.csv",
            members,
            season_name="Mock 2026",
            season_identifier="mock-2026",
            season_year=2026,
            week_number=week,
            contest_name=f"Fictional Week {week}",
            contest_date=f"2026-09-{week:02d}",
        )
    contest_id = int(
        connection.execute(
            "SELECT id FROM contests WHERE week_number = 4"
        ).fetchone()[0]
    )
    return connection, contest_id


_SYNTHETIC_NAMES = (
    "Patrick Mahomes' Extremely Long Film-Room Alias",
    "West Bottoms Blitz",
    "Arrowhead Auditor",
    "Boulevard Fourth Down",
    "Truman Sports Complex",
    "Crossroads Commissioner",
    "Fountain City Flex",
    "Burnt Ends Playbook",
    "Liberty Memorial Lineup",
    "River Market Red Zone",
)
_MOVEMENTS = (
    (MOVEMENT_UP, -2),
    (MOVEMENT_DOWN, 2),
    (MOVEMENT_SAME, 0),
    (MOVEMENT_NEW, pd.NA),
    (MOVEMENT_UP, -1),
    (MOVEMENT_DOWN, 1),
    (MOVEMENT_SAME, 0),
    (MOVEMENT_NEW, pd.NA),
    (MOVEMENT_UP, -3),
    (MOVEMENT_DOWN, 3),
)


def _extend_kcdk(data: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for offset, (name, movement) in enumerate(zip(_SYNTHETIC_NAMES, _MOVEMENTS), start=6):
        records.append(
            {
                "season_rank": offset,
                "display_name": name,
                "weeks_played": 4 if offset != 9 else 1,
                "average_finish": 5.5 + ((offset - 6) // 2) * 0.55,
                "wins": 0,
                "podium_finishes": 1 if offset < 9 else 0,
                "average_draftkings_fantasy_points": 168.25 - offset * 1.8,
                "total_money_won": 0.0 if offset % 3 else None,
                "prize_data_complete": offset % 4 != 0,
                "movement_status": movement[0],
                "movement_delta": movement[1],
            }
        )
    return pd.concat([data, pd.DataFrame.from_records(records)], ignore_index=True)


def _extend_tournament(data: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for offset, (name, movement) in enumerate(zip(_SYNTHETIC_NAMES, _MOVEMENTS), start=6):
        records.append(
            {
                "tournament_rank": offset,
                "display_name": name,
                "weeks_played": 4 if offset != 9 else 1,
                "weeks_with_prize_data": 4 if offset % 4 else 3,
                "prize_data_complete": offset % 4 != 0,
                "total_money_won": 0.0 if offset % 3 else None,
                "cashes": 0 if offset % 3 else pd.NA,
                "largest_single_tournament_win": 0.0 if offset % 3 else None,
                "average_draftkings_fantasy_points": 168.25 - offset * 1.8,
                "average_tournament_percentile": max(12.0, 72.0 - offset * 3.1),
                "movement_status": movement[0],
                "movement_delta": movement[1],
            }
        )
    return pd.concat([data, pd.DataFrame.from_records(records)], ignore_index=True)


def build_preview_frames() -> tuple[pd.DataFrame, pd.DataFrame, tuple[str, ...]]:
    """Use real mock analytics, then add presentation-only rows to reach 15."""
    with tempfile.TemporaryDirectory(prefix="kcdk-graphics-preview-") as temporary:
        connection, contest_id = _import_mock_season(Path(temporary) / "preview.sqlite")
        try:
            kcdk = season_leaderboard_with_movement(
                connection, "mock-2026", contest_id
            )
            tournament = tournament_leaderboard_with_movement(
                connection, "mock-2026", contest_id
            )
        finally:
            connection.close()

    money = tournament[
        ["display_name", "total_money_won", "prize_data_complete"]
    ]
    kcdk = kcdk.merge(money, on="display_name", how="left")
    most_wins = kcdk.sort_values(
        ["wins", "display_name"], ascending=[False, True], kind="stable"
    ).iloc[0]
    earnings_leader = tournament.iloc[0]
    callouts = (
        f"MOST WINS: {most_wins['display_name']} ({int(most_wins['wins'])})",
        f"EARNINGS LEADER: {earnings_leader['display_name']} "
        f"(${float(earnings_leader['total_money_won']):,.0f})",
    )
    return _extend_kcdk(kcdk), _extend_tournament(tournament), callouts


def render_leaderboard_previews(
    output_directory: str | Path = DEFAULT_PREVIEW_DIRECTORY,
    *,
    asset_directory: str | Path = PROJECT_ROOT / "assets" / "branding",
) -> tuple[RenderedLeaderboard, RenderedLeaderboard]:
    output = Path(output_directory)
    kcdk, tournament, callouts = build_preview_frames()
    assets = BrandingAssetPaths(Path(asset_directory))
    kcdk_result = render_kcdk_standings_png(
        kcdk,
        output / "kcdk_standings_preview.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        callouts=callouts,
        assets=assets,
    )
    tournament_result = render_tournament_performance_png(
        tournament,
        output / "tournament_performance_preview.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        callouts=callouts,
        assets=assets,
    )
    return kcdk_result, tournament_result


def preview_result_dict(result: RenderedLeaderboard) -> dict[str, object]:
    payload = asdict(result)
    payload["path"] = str(result.path)
    return payload


__all__ = [
    "DEFAULT_PREVIEW_DIRECTORY",
    "build_preview_frames",
    "preview_result_dict",
    "render_leaderboard_previews",
]
