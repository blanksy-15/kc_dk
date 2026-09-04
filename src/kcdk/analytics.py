"""Season leaderboard and factual player-usage analytics."""

from __future__ import annotations

import sqlite3

import pandas as pd


# Kept as data so future official tiebreak changes stay localized.
KCDK_LEADERBOARD_TIEBREAKERS = (
    ("average_finish", True),
    ("wins", False),
    ("podium_finishes", False),
    ("average_draftkings_fantasy_points", False),
    ("display_name", True),
)

# Backward-compatible name from the original single-leaderboard implementation.
LEADERBOARD_TIEBREAKERS = KCDK_LEADERBOARD_TIEBREAKERS

TOURNAMENT_LEADERBOARD_TIEBREAKERS = (
    ("total_money_won", False),
    ("average_draftkings_fantasy_points", False),
    ("average_tournament_percentile", False),
    ("display_name", True),
)

MOVEMENT_UP = "up"
MOVEMENT_DOWN = "down"
MOVEMENT_SAME = "same"
MOVEMENT_NEW = "new"


def sort_leaderboard(leaderboard: pd.DataFrame) -> pd.DataFrame:
    """Apply the configurable official ordering and assign deterministic ranks."""
    sort_columns = [item[0] for item in KCDK_LEADERBOARD_TIEBREAKERS]
    ascending = [item[1] for item in KCDK_LEADERBOARD_TIEBREAKERS]
    ranked = leaderboard.sort_values(
        sort_columns, ascending=ascending, kind="stable"
    ).reset_index(drop=True)
    ranked.insert(0, "season_rank", range(1, len(ranked) + 1))
    return ranked


def sort_tournament_leaderboard(leaderboard: pd.DataFrame) -> pd.DataFrame:
    """Apply the separately configurable tournament-performance ordering."""
    sort_columns = [item[0] for item in TOURNAMENT_LEADERBOARD_TIEBREAKERS]
    ascending = [item[1] for item in TOURNAMENT_LEADERBOARD_TIEBREAKERS]
    ranked = leaderboard.sort_values(
        sort_columns, ascending=ascending, kind="stable", na_position="last"
    ).reset_index(drop=True)
    ranked.insert(0, "tournament_rank", range(1, len(ranked) + 1))
    return ranked


def add_rank_movement(
    current: pd.DataFrame,
    previous: pd.DataFrame | None,
    *,
    rank_column: str,
    member_column: str = "display_name",
) -> pd.DataFrame:
    """Annotate an already-ranked table with movement from one prior table.

    ``movement_delta`` is current rank minus previous rank, so a negative value
    means improvement. The renderer owns only presentation; it never
    recalculates or changes leaderboard order.
    """
    required = {rank_column, member_column}
    missing = required - set(current.columns)
    if missing:
        raise ValueError(f"Current leaderboard is missing columns: {sorted(missing)}")
    if previous is not None:
        missing = required - set(previous.columns)
        if missing:
            raise ValueError(
                f"Previous leaderboard is missing columns: {sorted(missing)}"
            )
        if previous[member_column].duplicated().any():
            raise ValueError("Previous leaderboard contains duplicate member names.")

    prior_ranks = (
        {}
        if previous is None
        else previous.set_index(member_column)[rank_column].to_dict()
    )
    deltas: list[object] = []
    statuses: list[str] = []
    previous_values: list[object] = []
    for _, row in current.iterrows():
        previous_rank = prior_ranks.get(row[member_column])
        if previous_rank is None or pd.isna(previous_rank):
            previous_values.append(pd.NA)
            deltas.append(pd.NA)
            statuses.append(MOVEMENT_NEW)
            continue
        current_rank = int(row[rank_column])
        prior_rank = int(previous_rank)
        delta = current_rank - prior_rank
        previous_values.append(prior_rank)
        deltas.append(delta)
        statuses.append(
            MOVEMENT_UP if delta < 0 else MOVEMENT_DOWN if delta > 0 else MOVEMENT_SAME
        )

    result = current.copy()
    result["previous_rank"] = pd.array(previous_values, dtype="Int64")
    result["movement_delta"] = pd.array(deltas, dtype="Int64")
    result["movement_status"] = statuses
    return result


def _current_and_previous_contest_ids(
    connection: sqlite3.Connection,
    season_identifier: str,
    through_contest_id: int | None,
) -> tuple[int, int | None]:
    contests = connection.execute(
        """
        SELECT c.id
        FROM contests c JOIN seasons s ON s.id = c.season_id
        WHERE s.identifier = ?
        ORDER BY COALESCE(c.week_number, 2147483647), c.contest_date, c.id
        """,
        (season_identifier,),
    ).fetchall()
    contest_ids = [int(row[0]) for row in contests]
    if not contest_ids:
        raise ValueError(f"No contests exist for season {season_identifier}")
    current_id = through_contest_id or contest_ids[-1]
    if current_id not in contest_ids:
        raise ValueError(
            f"Contest {current_id} does not belong to season {season_identifier}"
        )
    current_index = contest_ids.index(current_id)
    previous_id = contest_ids[current_index - 1] if current_index else None
    return current_id, previous_id


def _contest_member_names(
    connection: sqlite3.Connection, contest_id: int
) -> set[str]:
    rows = connection.execute(
        """
        SELECT m.display_name
        FROM member_results r JOIN members m ON m.id = r.member_id
        WHERE r.contest_id = ?
        """,
        (contest_id,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _mark_current_returnees_new(
    connection: sqlite3.Connection,
    movement: pd.DataFrame,
    current_id: int,
    previous_id: int | None,
) -> pd.DataFrame:
    if previous_id is None:
        return movement
    current_members = _contest_member_names(connection, current_id)
    previous_members = _contest_member_names(connection, previous_id)
    returning = current_members - previous_members
    if not returning:
        return movement
    result = movement.copy()
    mask = result["display_name"].isin(returning)
    result.loc[mask, "previous_rank"] = pd.NA
    result.loc[mask, "movement_delta"] = pd.NA
    result.loc[mask, "movement_status"] = MOVEMENT_NEW
    return result


def season_leaderboard_with_movement(
    connection: sqlite3.Connection,
    season_identifier: str,
    through_contest_id: int | None = None,
) -> pd.DataFrame:
    """Return official KCDK standings plus prior-contest rank movement."""
    current_id, previous_id = _current_and_previous_contest_ids(
        connection, season_identifier, through_contest_id
    )
    current = season_leaderboard(connection, season_identifier, current_id)
    previous = (
        season_leaderboard(connection, season_identifier, previous_id)
        if previous_id is not None
        else None
    )
    movement = add_rank_movement(
        current, previous, rank_column="season_rank"
    )
    return _mark_current_returnees_new(
        connection, movement, current_id, previous_id
    )


def tournament_leaderboard_with_movement(
    connection: sqlite3.Connection,
    season_identifier: str,
    through_contest_id: int | None = None,
) -> pd.DataFrame:
    """Return official tournament standings plus independent rank movement."""
    current_id, previous_id = _current_and_previous_contest_ids(
        connection, season_identifier, through_contest_id
    )
    current = tournament_performance_leaderboard(
        connection, season_identifier, current_id
    )
    previous = (
        tournament_performance_leaderboard(
            connection, season_identifier, previous_id
        )
        if previous_id is not None
        else None
    )
    movement = add_rank_movement(
        current, previous, rank_column="tournament_rank"
    )
    return _mark_current_returnees_new(
        connection, movement, current_id, previous_id
    )


def _season_results(
    connection: sqlite3.Connection,
    season_identifier: str,
    through_contest_id: int | None = None,
) -> pd.DataFrame:
    results = pd.read_sql_query(
        """
        SELECT c.id AS contest_id, c.week_number, c.week_label,
               m.id AS member_id, m.member_key, m.display_name,
               r.kcdk_finish, r.draftkings_fantasy_points,
               r.tournament_percentile, r.draftkings_overall_rank,
               r.prize_cents
        FROM member_results r
        JOIN contests c ON c.id = r.contest_id
        JOIN seasons s ON s.id = c.season_id
        JOIN members m ON m.id = r.member_id
        WHERE s.identifier = ?
        """,
        connection,
        params=[season_identifier],
    )
    if through_contest_id is None:
        return results
    contests = pd.read_sql_query(
        """
        SELECT c.id
        FROM contests c JOIN seasons s ON s.id = c.season_id
        WHERE s.identifier = ?
        ORDER BY COALESCE(c.week_number, 2147483647), c.contest_date, c.id
        """,
        connection,
        params=[season_identifier],
    )
    matching = contests.index[contests["id"].eq(through_contest_id)].tolist()
    if not matching:
        raise ValueError(
            f"Contest {through_contest_id} does not belong to season {season_identifier}"
        )
    allowed_ids = set(contests.iloc[: matching[0] + 1]["id"])
    return results.loc[results["contest_id"].isin(allowed_ids)].copy()


def _with_last_place(results: pd.DataFrame) -> pd.DataFrame:
    enriched = results.copy()
    if enriched.empty:
        enriched["is_last_place"] = pd.Series(dtype=bool)
        return enriched
    last_finish = enriched.groupby("contest_id")["kcdk_finish"].transform("max")
    enriched["is_last_place"] = enriched["kcdk_finish"].eq(last_finish)
    return enriched


def season_leaderboard(
    connection: sqlite3.Connection,
    season_identifier: str,
    through_contest_id: int | None = None,
) -> pd.DataFrame:
    """Build the official average-weekly-finish season leaderboard."""
    results = _with_last_place(
        _season_results(connection, season_identifier, through_contest_id)
    )
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


def _money_sum(values: pd.Series) -> float:
    cents = values.sum(min_count=1)
    return cents / 100.0


def _money_mean(values: pd.Series) -> float:
    cents = values.mean()
    return cents / 100.0


def _known_cash_count(values: pd.Series) -> object:
    if values.count() == 0:
        return pd.NA
    return int(values.gt(0).sum())


def _known_cash_rate(values: pd.Series) -> float:
    known = int(values.count())
    if known == 0:
        return float("nan")
    return float(values.gt(0).sum()) / known * 100.0


def tournament_performance_leaderboard(
    connection: sqlite3.Connection,
    season_identifier: str,
    through_contest_id: int | None = None,
) -> pd.DataFrame:
    """Aggregate the distinct money-first DraftKings tournament leaderboard."""
    results = _season_results(connection, season_identifier, through_contest_id)
    columns = [
        "tournament_rank",
        "display_name",
        "weeks_played",
        "weeks_with_prize_data",
        "prize_data_complete",
        "total_money_won",
        "average_money_won_per_known_week",
        "cashes",
        "cash_rate",
        "largest_single_tournament_win",
        "average_draftkings_fantasy_points",
        "highest_draftkings_fantasy_score",
        "average_tournament_percentile",
        "best_tournament_rank",
        "average_tournament_rank",
    ]
    if results.empty:
        return pd.DataFrame(columns=columns)

    grouped = results.groupby(["member_id", "display_name"], sort=False)
    leaderboard = grouped.agg(
        weeks_played=("contest_id", "nunique"),
        weeks_with_prize_data=("prize_cents", "count"),
        total_money_won=("prize_cents", _money_sum),
        average_money_won_per_known_week=("prize_cents", _money_mean),
        cashes=("prize_cents", _known_cash_count),
        cash_rate=("prize_cents", _known_cash_rate),
        largest_single_tournament_win=("prize_cents", "max"),
        average_draftkings_fantasy_points=("draftkings_fantasy_points", "mean"),
        highest_draftkings_fantasy_score=("draftkings_fantasy_points", "max"),
        average_tournament_percentile=("tournament_percentile", "mean"),
        best_tournament_rank=("draftkings_overall_rank", "min"),
        average_tournament_rank=("draftkings_overall_rank", "mean"),
    ).reset_index()
    leaderboard["largest_single_tournament_win"] = (
        leaderboard["largest_single_tournament_win"] / 100.0
    )
    leaderboard["prize_data_complete"] = leaderboard[
        "weeks_with_prize_data"
    ].eq(leaderboard["weeks_played"])
    leaderboard["cashes"] = leaderboard["cashes"].astype("Int64")
    leaderboard = sort_tournament_leaderboard(leaderboard)
    return leaderboard[columns]


def _usage_rows(connection: sqlite3.Connection, season_identifier: str) -> pd.DataFrame:
    usage = pd.read_sql_query(
        """
        SELECT c.id AS contest_id, c.week_number, c.week_label,
               m.id AS member_id, m.member_key, m.display_name,
               p.id AS player_id, p.display_name AS player_name,
               lp.roster_position, lp.player_fantasy_points,
               lp.draftkings_ownership_percentage,
               r.kcdk_finish, r.prize_cents,
               r.draftkings_fantasy_points AS member_draftkings_fantasy_points
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
        "weeks_with_prize_data_when_rostered",
        "total_money_won_when_rostered",
        "cashes_with_player",
        "cash_rate_with_player",
        "average_member_draftkings_fantasy_points_when_rostered",
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
        weeks_with_prize_data_when_rostered=("prize_cents", "count"),
        total_money_won_when_rostered=("prize_cents", _money_sum),
        cashes_with_player=("prize_cents", _known_cash_count),
        cash_rate_with_player=("prize_cents", _known_cash_rate),
        average_member_draftkings_fantasy_points_when_rostered=(
            "member_draftkings_fantasy_points",
            "mean",
        ),
    ).reset_index()
    report["usage_percentage"] = report.apply(
        lambda row: row["times_rostered"] / weeks_by_member[row["member_id"]] * 100.0,
        axis=1,
    )
    report["last_place_finishes_with_player"] = report[
        "last_place_finishes_with_player"
    ].astype(int)
    report["cashes_with_player"] = report["cashes_with_player"].astype("Int64")
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
