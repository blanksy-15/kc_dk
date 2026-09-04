"""Season leaderboard and factual player-usage analytics."""

from __future__ import annotations

import sqlite3

import pandas as pd


# Kept as data so future official tiebreak changes stay localized.
LEADERBOARD_TIEBREAKERS = (
    ("average_finish", True),
    ("wins", False),
    ("podium_finishes", False),
    ("average_draftkings_fantasy_points", False),
    ("display_name", True),
)


def sort_leaderboard(leaderboard: pd.DataFrame) -> pd.DataFrame:
    """Apply the configurable official ordering and assign deterministic ranks."""
    sort_columns = [item[0] for item in LEADERBOARD_TIEBREAKERS]
    ascending = [item[1] for item in LEADERBOARD_TIEBREAKERS]
    ranked = leaderboard.sort_values(
        sort_columns, ascending=ascending, kind="stable"
    ).reset_index(drop=True)
    ranked.insert(0, "season_rank", range(1, len(ranked) + 1))
    return ranked


def _season_results(
    connection: sqlite3.Connection, season_identifier: str
) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT c.id AS contest_id, c.week_number, c.week_label,
               m.id AS member_id, m.member_key, m.display_name,
               r.kcdk_finish, r.draftkings_fantasy_points,
               r.tournament_percentile, r.draftkings_overall_rank
        FROM member_results r
        JOIN contests c ON c.id = r.contest_id
        JOIN seasons s ON s.id = c.season_id
        JOIN members m ON m.id = r.member_id
        WHERE s.identifier = ?
        """,
        connection,
        params=[season_identifier],
    )


def _with_last_place(results: pd.DataFrame) -> pd.DataFrame:
    enriched = results.copy()
    if enriched.empty:
        enriched["is_last_place"] = pd.Series(dtype=bool)
        return enriched
    last_finish = enriched.groupby("contest_id")["kcdk_finish"].transform("max")
    enriched["is_last_place"] = enriched["kcdk_finish"].eq(last_finish)
    return enriched


def season_leaderboard(
    connection: sqlite3.Connection, season_identifier: str
) -> pd.DataFrame:
    """Build the official average-weekly-finish season leaderboard."""
    results = _with_last_place(_season_results(connection, season_identifier))
    columns = [
        "season_rank",
        "display_name",
        "weeks_played",
        "average_finish",
        "wins",
        "podium_finishes",
        "last_place_finishes",
        "average_draftkings_fantasy_points",
        "highest_draftkings_fantasy_score",
        "lowest_draftkings_fantasy_score",
        "average_tournament_percentile",
        "best_tournament_rank",
        "worst_tournament_rank",
    ]
    if results.empty:
        return pd.DataFrame(columns=columns)

    grouped = results.groupby(["member_id", "display_name"], sort=False, dropna=False)
    leaderboard = grouped.agg(
        weeks_played=("contest_id", "nunique"),
        average_finish=("kcdk_finish", "mean"),
        wins=("kcdk_finish", lambda values: int(values.eq(1).sum())),
        podium_finishes=("kcdk_finish", lambda values: int(values.le(3).sum())),
        last_place_finishes=("is_last_place", "sum"),
        average_draftkings_fantasy_points=("draftkings_fantasy_points", "mean"),
        highest_draftkings_fantasy_score=("draftkings_fantasy_points", "max"),
        lowest_draftkings_fantasy_score=("draftkings_fantasy_points", "min"),
        average_tournament_percentile=("tournament_percentile", "mean"),
        best_tournament_rank=("draftkings_overall_rank", "min"),
        worst_tournament_rank=("draftkings_overall_rank", "max"),
    ).reset_index()
    leaderboard = sort_leaderboard(leaderboard)
    leaderboard["last_place_finishes"] = leaderboard["last_place_finishes"].astype(int)
    return leaderboard[columns]


def _usage_rows(connection: sqlite3.Connection, season_identifier: str) -> pd.DataFrame:
    usage = pd.read_sql_query(
        """
        SELECT c.id AS contest_id, c.week_number, c.week_label,
               m.id AS member_id, m.member_key, m.display_name,
               p.id AS player_id, p.display_name AS player_name,
               lp.roster_position, lp.player_fantasy_points,
               lp.draftkings_ownership_percentage,
               r.kcdk_finish
        FROM lineup_players lp
        JOIN contests c ON c.id = lp.contest_id
        JOIN seasons s ON s.id = c.season_id
        JOIN members m ON m.id = lp.member_id
        JOIN players p ON p.id = lp.player_id
        JOIN member_results r
          ON r.contest_id = lp.contest_id AND r.member_id = lp.member_id
        WHERE s.identifier = ?
        """,
        connection,
        params=[season_identifier],
    )
    return _with_last_place_for_usage(connection, season_identifier, usage)


def _with_last_place_for_usage(
    connection: sqlite3.Connection, season_identifier: str, usage: pd.DataFrame
) -> pd.DataFrame:
    if usage.empty:
        usage["is_last_place"] = pd.Series(dtype=bool)
        return usage
    results = _with_last_place(_season_results(connection, season_identifier))[
        ["contest_id", "member_id", "is_last_place"]
    ]
    return usage.merge(results, on=["contest_id", "member_id"], how="left")


def member_player_usage(
    connection: sqlite3.Connection,
    season_identifier: str,
    member_key: str | None = None,
) -> pd.DataFrame:
    """Return per-member/player selection counts and result context."""
    usage = _usage_rows(connection, season_identifier)
    if member_key is not None:
        usage = usage.loc[usage["member_key"] == member_key].copy()
    columns = [
        "member_key",
        "display_name",
        "player_name",
        "times_rostered",
        "usage_percentage",
        "positions_used",
        "average_player_fantasy_points",
        "total_player_fantasy_points",
        "average_field_ownership",
        "average_kcdk_finish_when_rostered",
        "wins_with_player",
        "podiums_with_player",
        "last_place_finishes_with_player",
    ]
    if usage.empty:
        return pd.DataFrame(columns=columns)

    results = _season_results(connection, season_identifier)
    weeks_by_member = results.groupby("member_id")["contest_id"].nunique().to_dict()
    grouped = usage.groupby(
        ["member_id", "member_key", "display_name", "player_id", "player_name"],
        sort=False,
    )
    report = grouped.agg(
        times_rostered=("contest_id", "nunique"),
        positions_used=("roster_position", lambda values: ", ".join(sorted(set(values)))),
        average_player_fantasy_points=("player_fantasy_points", "mean"),
        total_player_fantasy_points=(
            "player_fantasy_points",
            lambda values: values.sum(min_count=1),
        ),
        average_field_ownership=("draftkings_ownership_percentage", "mean"),
        average_kcdk_finish_when_rostered=("kcdk_finish", "mean"),
        wins_with_player=("kcdk_finish", lambda values: int(values.eq(1).sum())),
        podiums_with_player=("kcdk_finish", lambda values: int(values.le(3).sum())),
        last_place_finishes_with_player=("is_last_place", "sum"),
    ).reset_index()
    report["usage_percentage"] = report.apply(
        lambda row: row["times_rostered"] / weeks_by_member[row["member_id"]] * 100.0,
        axis=1,
    )
    report["last_place_finishes_with_player"] = report[
        "last_place_finishes_with_player"
    ].astype(int)
    return report.sort_values(
        ["display_name", "times_rostered", "player_name"],
        ascending=[True, False, True],
    )[columns].reset_index(drop=True)


def group_player_usage(
    connection: sqlite3.Connection, season_identifier: str
) -> pd.DataFrame:
    """Return group-wide selection, reach, ownership, and player scoring facts."""
    usage = _usage_rows(connection, season_identifier)
    columns = [
        "player_name",
        "kcdk_selections",
        "unique_members",
        "weeks_appeared",
        "average_field_ownership",
        "average_player_fantasy_points",
    ]
    if usage.empty:
        return pd.DataFrame(columns=columns)
    report = usage.groupby(["player_id", "player_name"], sort=False).agg(
        kcdk_selections=("contest_id", "size"),
        unique_members=("member_id", "nunique"),
        weeks_appeared=("contest_id", "nunique"),
        average_field_ownership=("draftkings_ownership_percentage", "mean"),
        average_player_fantasy_points=("player_fantasy_points", "mean"),
    ).reset_index()
    return report.sort_values(
        ["kcdk_selections", "player_name"], ascending=[False, True]
    )[columns].reset_index(drop=True)


def consecutive_player_use(
    connection: sqlite3.Connection,
    season_identifier: str,
    *,
    minimum_weeks: int = 2,
) -> pd.DataFrame:
    """Identify player selections across consecutive imported season contests."""
    if minimum_weeks < 2:
        raise ValueError("minimum_weeks must be at least 2")
    usage = _usage_rows(connection, season_identifier)
    columns = [
        "member_key",
        "display_name",
        "player_name",
        "consecutive_weeks",
        "start_week",
        "end_week",
    ]
    if usage.empty:
        return pd.DataFrame(columns=columns)
    contests = pd.read_sql_query(
        """
        SELECT c.id, c.week_label
        FROM contests c JOIN seasons s ON s.id = c.season_id
        WHERE s.identifier = ?
        ORDER BY COALESCE(c.week_number, 2147483647), c.contest_date, c.id
        """,
        connection,
        params=[season_identifier],
    )
    order = {int(row.id): index for index, row in enumerate(contests.itertuples())}
    labels = {int(row.id): row.week_label for row in contests.itertuples()}
    streaks: list[dict[str, object]] = []
    for keys, rows in usage.groupby(
        ["member_key", "display_name", "player_name"], sort=False
    ):
        positions = sorted({order[int(value)] for value in rows["contest_id"]})
        run = [positions[0]]
        runs: list[list[int]] = []
        for position in positions[1:]:
            if position == run[-1] + 1:
                run.append(position)
            else:
                runs.append(run)
                run = [position]
        runs.append(run)
        for item in runs:
            if len(item) >= minimum_weeks:
                contest_ids = contests.iloc[item]["id"].astype(int).tolist()
                streaks.append(
                    {
                        "member_key": keys[0],
                        "display_name": keys[1],
                        "player_name": keys[2],
                        "consecutive_weeks": len(item),
                        "start_week": labels[contest_ids[0]],
                        "end_week": labels[contest_ids[-1]],
                    }
                )
    return pd.DataFrame(streaks, columns=columns).sort_values(
        ["consecutive_weeks", "display_name", "player_name"],
        ascending=[False, True, True],
    ).reset_index(drop=True)


def unanimous_weekly_selections(
    connection: sqlite3.Connection, season_identifier: str
) -> pd.DataFrame:
    """Return players used by every active season member in a week."""
    return _weekly_selection_patterns(connection, season_identifier, unanimous=True)


def unique_weekly_selections(
    connection: sqlite3.Connection, season_identifier: str
) -> pd.DataFrame:
    """Return players used by exactly one KCDK member in a week."""
    return _weekly_selection_patterns(connection, season_identifier, unanimous=False)


def _weekly_selection_patterns(
    connection: sqlite3.Connection, season_identifier: str, *, unanimous: bool
) -> pd.DataFrame:
    usage = _usage_rows(connection, season_identifier)
    columns = ["week_label", "player_name", "member_count", "members"]
    if usage.empty:
        return pd.DataFrame(columns=columns)
    active_count = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM season_members sm JOIN seasons s ON s.id = sm.season_id
            WHERE s.identifier = ? AND sm.active = 1
            """,
            (season_identifier,),
        ).fetchone()["count"]
    )
    report = usage.groupby(
        ["contest_id", "week_label", "player_id", "player_name"], sort=False
    ).agg(
        member_count=("member_id", "nunique"),
        members=("display_name", lambda values: ", ".join(sorted(set(values)))),
    ).reset_index()
    target = active_count if unanimous else 1
    report = report.loc[report["member_count"] == target]
    return report.sort_values(["week_label", "player_name"])[columns].reset_index(drop=True)


def most_used_players_by_member(
    connection: sqlite3.Connection, season_identifier: str
) -> pd.DataFrame:
    """Return all tied most-frequently rostered players for every member."""
    usage = member_player_usage(connection, season_identifier)
    if usage.empty:
        return usage
    maximum = usage.groupby("member_key")["times_rostered"].transform("max")
    return usage.loc[usage["times_rostered"].eq(maximum)].reset_index(drop=True)


def member_ownership_extremes(
    connection: sqlite3.Connection, season_identifier: str
) -> pd.DataFrame:
    """Return each member's highest and lowest average-ownership players."""
    usage = member_player_usage(connection, season_identifier).dropna(
        subset=["average_field_ownership"]
    )
    columns = [
        "member_key",
        "display_name",
        "extreme",
        "player_name",
        "average_field_ownership",
    ]
    if usage.empty:
        return pd.DataFrame(columns=columns)
    items: list[pd.DataFrame] = []
    for _, group in usage.groupby("member_key", sort=False):
        high = group.loc[
            group["average_field_ownership"].eq(
                group["average_field_ownership"].max()
            )
        ].copy()
        high["extreme"] = "highest"
        low = group.loc[
            group["average_field_ownership"].eq(
                group["average_field_ownership"].min()
            )
        ].copy()
        low["extreme"] = "lowest"
        items.extend([high, low])
    return pd.concat(items, ignore_index=True)[columns].sort_values(
        ["display_name", "extreme", "player_name"]
    ).reset_index(drop=True)
