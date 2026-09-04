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
    current_last_place_streaks,
    season_leaderboard_with_movement,
    tournament_leaderboard_with_movement,
)
from .leaderboard_graphics import (
    BrandingAssetPaths,
    LeaderboardCallout,
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
                "last_place_finishes": max(0, offset - 12),
                "average_draftkings_fantasy_points": 168.25 - offset * 1.8,
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


def build_preview_frames() -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    tuple[LeaderboardCallout, ...],
    tuple[LeaderboardCallout, ...],
]:
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
            last_place_streaks = current_last_place_streaks(
                connection, "mock-2026", contest_id
            )
        finally:
            connection.close()

    most_wins = kcdk.sort_values(
        ["wins", "display_name"], ascending=[False, True], kind="stable"
    ).iloc[0]
    most_podiums = kcdk.sort_values(
        ["podium_finishes", "display_name"],
        ascending=[False, True],
        kind="stable",
    ).iloc[0]
    kcdk_callouts = [
        LeaderboardCallout("Most Wins", str(most_wins["display_name"]), most_wins["wins"]),
        LeaderboardCallout(
            "Most Podiums",
            str(most_podiums["display_name"]),
            most_podiums["podium_finishes"],
        ),
    ]
    if not last_place_streaks.empty:
        streak = last_place_streaks.iloc[0]
        if int(streak["consecutive_weeks"]) >= 2:
            kcdk_callouts.append(
                LeaderboardCallout(
                    "Last-Place Streak",
                    str(streak["display_name"]),
                    streak["consecutive_weeks"],
                    "weeks",
                )
            )

    money_leader = tournament.loc[tournament["total_money_won"].notna()].iloc[0]
    most_cashes = tournament.loc[tournament["cashes"].notna()].sort_values(
        ["cashes", "display_name"], ascending=[False, True], kind="stable"
    ).iloc[0]
    best_cash = tournament.loc[
        tournament["largest_single_tournament_win"].idxmax()
    ]
    tournament_callouts = (
        LeaderboardCallout(
            "Money Leader",
            str(money_leader["display_name"]),
            money_leader["total_money_won"],
            "currency",
        ),
        LeaderboardCallout(
            "Most Cashes",
            str(most_cashes["display_name"]),
            most_cashes["cashes"],
        ),
        LeaderboardCallout(
            "Best Cash",
            str(best_cash["display_name"]),
            best_cash["largest_single_tournament_win"],
            "currency",
        ),
    )
    return (
        _extend_kcdk(kcdk),
        _extend_tournament(tournament),
        tuple(kcdk_callouts),
        tournament_callouts,
    )


def render_leaderboard_previews(
    output_directory: str | Path = DEFAULT_PREVIEW_DIRECTORY,
    *,
    asset_directory: str | Path = PROJECT_ROOT / "assets" / "branding",
) -> tuple[RenderedLeaderboard, RenderedLeaderboard]:
    output = Path(output_directory)
    kcdk, tournament, kcdk_callouts, tournament_callouts = build_preview_frames()
    assets = BrandingAssetPaths(Path(asset_directory))
    kcdk_result = render_kcdk_standings_png(
        kcdk,
        output / "kcdk_standings_preview.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        callouts=kcdk_callouts,
        assets=assets,
    )
    tournament_result = render_tournament_performance_png(
        tournament,
        output / "tournament_performance_preview.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        callouts=tournament_callouts,
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
