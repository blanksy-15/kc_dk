from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from kcdk.analytics import (
    consecutive_player_use,
    group_player_usage,
    member_ownership_extremes,
    member_player_usage,
    most_used_players_by_member,
    season_leaderboard,
    sort_leaderboard,
    sort_tournament_leaderboard,
    tournament_performance_leaderboard,
    unanimous_weekly_selections,
    unique_weekly_selections,
)
from kcdk.persistence import (
    connect_database,
    import_week,
    initialize_database,
    weekly_results,
)
from kcdk.lineups import parse_lineup_text


ROOT = Path(__file__).parents[1]
MOCK = ROOT / "data" / "mock"
MEMBERS = MOCK / "members.csv"


def import_mock_season(connection):
    summaries = []
    for week in range(1, 5):
        summaries.append(
            import_week(
                connection,
                MOCK / f"season_week_{week}.csv",
                MEMBERS,
                season_name="Mock 2026",
                season_identifier="mock-2026",
                season_year=2026,
                week_number=week,
                contest_name=f"Fictional Week {week}",
                contest_date=f"2026-09-{week:02d}",
            )
        )
    return summaries


@pytest.fixture
def season_db(tmp_path):
    connection = connect_database(tmp_path / "season.sqlite")
    import_mock_season(connection)
    yield connection
    connection.close()


def test_database_initialization_has_normalized_schema(tmp_path):
    connection = connect_database(tmp_path / "schema.sqlite")
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "seasons",
        "members",
        "season_members",
        "contests",
        "member_results",
        "players",
        "lineup_players",
    }.issubset(tables)
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    result_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(member_results)")
    }
    assert "prize_cents" in result_columns
    connection.close()


def test_schema_migrates_an_existing_member_results_table(tmp_path):
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE member_results (
            id INTEGER PRIMARY KEY,
            contest_id INTEGER,
            member_id INTEGER
        )
        """
    )
    initialize_database(connection)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(member_results)")}
    versions = {
        row[0] for row in connection.execute("SELECT version FROM schema_versions")
    }
    assert "prize_cents" in columns
    assert 2 in versions
    connection.close()


def test_import_week_and_idempotent_reimport(tmp_path):
    connection = connect_database(tmp_path / "import.sqlite")
    arguments = dict(
        season_name="Mock 2026",
        season_identifier="mock-2026",
        week_number=1,
    )
    first = import_week(connection, MOCK / "season_week_1.csv", MEMBERS, **arguments)
    second = import_week(connection, MOCK / "season_week_1.csv", MEMBERS, **arguments)

    assert first.total_tournament_entries == 10
    assert first.matched_kcdk_members == 5
    assert first.member_result_rows_stored == 5
    assert first.lineup_player_rows_stored == 15
    assert not first.warnings
    assert second.already_imported
    assert second.member_result_rows_stored == 0
    assert second.lineup_player_rows_stored == 0
    assert connection.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM member_results").fetchone()[0] == 5
    assert connection.execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0] == 15
    connection.close()


def test_changed_source_requires_explicit_replacement(tmp_path):
    connection = connect_database(tmp_path / "replacement.sqlite")
    import_week(
        connection,
        MOCK / "season_week_1.csv",
        MEMBERS,
        season_name="Mock 2026",
        season_identifier="mock-2026",
        week_number=1,
    )
    with pytest.raises(ValueError, match="replace=True"):
        import_week(
            connection,
            MOCK / "season_week_2.csv",
            MEMBERS,
            season_name="Mock 2026",
            season_identifier="mock-2026",
            week_number=1,
        )
    replacement = import_week(
        connection,
        MOCK / "season_week_2.csv",
        MEMBERS,
        season_name="Mock 2026",
        season_identifier="mock-2026",
        week_number=1,
        replace=True,
    )
    assert replacement.member_result_rows_stored == 5
    assert connection.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 1
    assert weekly_results(connection, "mock-2026", "Week 1").iloc[0][
        "display_name"
    ] == "Jordan Vale"
    connection.close()


def test_multiple_week_leaderboard_aggregates_required_statistics(season_db):
    leaderboard = season_leaderboard(season_db, "mock-2026")
    assert leaderboard["display_name"].tolist() == [
        "Casey North",
        "Alex Rowan",
        "Jordan Vale",
        "Sam Ellis",
        "Taylor Quinn",
    ]

    casey = leaderboard.set_index("display_name").loc["Casey North"]
    assert casey["weeks_played"] == 4
    assert casey["average_finish"] == pytest.approx(2.25)
    assert casey["wins"] == 1
    assert casey["podium_finishes"] == 3
    assert casey["last_place_finishes"] == 0
    assert casey["average_draftkings_fantasy_points"] == pytest.approx(186.75)
    assert casey["highest_draftkings_fantasy_score"] == pytest.approx(190)
    assert casey["lowest_draftkings_fantasy_score"] == pytest.approx(178)
    assert casey["average_tournament_percentile"] == pytest.approx(86.111111)
    assert casey["best_tournament_rank"] == 1
    assert casey["worst_tournament_rank"] == 4

    taylor = leaderboard.set_index("display_name").loc["Taylor Quinn"]
    assert taylor["last_place_finishes"] == 4


def test_leaderboard_tiebreakers_are_applied_in_configured_order():
    candidates = pd.DataFrame(
        {
            "display_name": ["Epsilon", "Delta", "Charlie", "Bravo", "Alpha"],
            "average_finish": [2.0] * 5,
            "wins": [1, 1, 1, 1, 2],
            "podium_finishes": [3, 3, 3, 4, 1],
            "average_draftkings_fantasy_points": [200, 200, 300, 100, 50],
        }
    )
    ranked = sort_leaderboard(candidates)
    assert ranked["display_name"].tolist() == [
        "Alpha",
        "Bravo",
        "Charlie",
        "Delta",
        "Epsilon",
    ]
    assert ranked["season_rank"].tolist() == [1, 2, 3, 4, 5]


def test_tournament_leaderboard_money_aggregation_and_ordering(season_db):
    leaderboard = tournament_performance_leaderboard(season_db, "mock-2026")
    assert leaderboard["display_name"].tolist() == [
        "Alex Rowan",
        "Casey North",
        "Jordan Vale",
        "Sam Ellis",
        "Taylor Quinn",
    ]

    casey = leaderboard.set_index("display_name").loc["Casey North"]
    assert casey["weeks_played"] == 4
    assert casey["weeks_with_prize_data"] == 4
    assert bool(casey["prize_data_complete"])
    assert casey["total_money_won"] == pytest.approx(125.50)
    assert casey["average_money_won_per_known_week"] == pytest.approx(31.375)
    assert casey["cashes"] == 2
    assert casey["cash_rate"] == pytest.approx(50)
    assert casey["largest_single_tournament_win"] == pytest.approx(100)
    assert casey["average_draftkings_fantasy_points"] == pytest.approx(186.75)
    assert casey["highest_draftkings_fantasy_score"] == pytest.approx(190)
    assert casey["best_tournament_rank"] == 1
    assert casey["average_tournament_rank"] == pytest.approx(2.25)

    # Casey and Jordan both won $125.50; Casey wins the fantasy-points tiebreak.
    casey_tied, jordan = leaderboard.set_index("display_name").loc[
        ["Casey North", "Jordan Vale"]
    ].itertuples()
    assert casey_tied.total_money_won == jordan.total_money_won == pytest.approx(125.50)
    assert (
        casey_tied.average_draftkings_fantasy_points
        > jordan.average_draftkings_fantasy_points
    )

    taylor = leaderboard.set_index("display_name").loc["Taylor Quinn"]
    assert taylor["weeks_with_prize_data"] == 3
    assert not bool(taylor["prize_data_complete"])
    assert taylor["total_money_won"] == pytest.approx(0)
    assert taylor["cashes"] == 0
    assert taylor["cash_rate"] == pytest.approx(0)


def test_tournament_leaderboard_secondary_and_later_tiebreakers():
    candidates = pd.DataFrame(
        {
            "display_name": ["Delta", "Charlie", "Bravo", "Alpha"],
            "total_money_won": [50, 50, 50, 100],
            "average_draftkings_fantasy_points": [200, 200, 210, 100],
            "average_tournament_percentile": [80, 90, 70, 50],
        }
    )
    ranked = sort_tournament_leaderboard(candidates)
    assert ranked["display_name"].tolist() == [
        "Alpha",
        "Bravo",
        "Charlie",
        "Delta",
    ]


def test_weekly_results_are_available_after_import(season_db):
    week = weekly_results(season_db, "mock-2026", "Week 3")
    assert week["display_name"].tolist() == [
        "Alex Rowan",
        "Casey North",
        "Sam Ellis",
        "Jordan Vale",
        "Taylor Quinn",
    ]
    by_member = week.set_index("display_name")
    assert by_member.loc["Casey North", "prize_cents"] == 2550
    assert by_member.loc["Casey North", "money_won"] == pytest.approx(25.50)


def test_known_zero_and_missing_prize_are_distinct(season_db):
    week = weekly_results(season_db, "mock-2026", "Week 4").set_index("display_name")
    assert week.loc["Casey North", "prize_cents"] == 0
    assert week.loc["Casey North", "money_won"] == 0
    assert pd.isna(week.loc["Taylor Quinn", "prize_cents"])
    assert pd.isna(week.loc["Taylor Quinn", "money_won"])


def test_member_level_missing_prize_is_reported(tmp_path):
    connection = connect_database(tmp_path / "missing-prize.sqlite")
    summary = import_week(
        connection,
        MOCK / "season_week_4.csv",
        MEMBERS,
        season_name="Missing Prize",
        season_identifier="missing-prize",
        week_number=4,
    )
    assert any(
        "Prize/winnings data was unknown for: Taylor Quinn" in warning
        for warning in summary.warnings
    )
    connection.close()


def test_member_player_usage_counts_percentages_and_result_context(season_db):
    usage = member_player_usage(season_db, "mock-2026", "casey-north")
    comet = usage.set_index("player_name").loc["Comet Core"]
    assert comet["times_rostered"] == 4
    assert comet["usage_percentage"] == pytest.approx(100)
    assert comet["positions_used"] == "QB, RB"
    assert comet["average_player_fantasy_points"] == pytest.approx(26)
    assert comet["total_player_fantasy_points"] == pytest.approx(104)
    assert comet["average_field_ownership"] == pytest.approx(18.5)
    assert comet["average_kcdk_finish_when_rostered"] == pytest.approx(2.25)
    assert comet["wins_with_player"] == 1
    assert comet["podiums_with_player"] == 3
    assert comet["last_place_finishes_with_player"] == 0
    assert comet["weeks_with_prize_data_when_rostered"] == 4
    assert comet["total_money_won_when_rostered"] == pytest.approx(125.50)
    assert comet["cashes_with_player"] == 2
    assert comet["cash_rate_with_player"] == pytest.approx(50)
    assert comet[
        "average_member_draftkings_fantasy_points_when_rostered"
    ] == pytest.approx(186.75)


def test_group_usage_and_factual_usage_helpers(season_db):
    group = group_player_usage(season_db, "mock-2026")
    river = group.set_index("player_name").loc["River Rush"]
    assert river["kcdk_selections"] >= 10
    assert river["unique_members"] >= 4
    assert river["weeks_appeared"] == 4

    streaks = consecutive_player_use(season_db, "mock-2026")
    comet = streaks.loc[
        (streaks["member_key"] == "casey-north")
        & (streaks["player_name"] == "Comet Core")
    ].iloc[0]
    assert comet["consecutive_weeks"] == 4
    assert (comet["start_week"], comet["end_week"]) == ("Week 1", "Week 4")

    unanimous = unanimous_weekly_selections(season_db, "mock-2026")
    assert (
        (unanimous["week_label"] == "Week 2")
        & (unanimous["player_name"] == "Atlas Common")
    ).any()

    unique = unique_weekly_selections(season_db, "mock-2026")
    zephyr = unique.loc[
        (unique["week_label"] == "Week 1")
        & (unique["player_name"] == "Zephyr Solo")
    ].iloc[0]
    assert zephyr["members"] == "Casey North"

    most_used = most_used_players_by_member(season_db, "mock-2026")
    assert (
        (most_used["member_key"] == "casey-north")
        & (most_used["player_name"] == "Comet Core")
    ).any()
    extremes = member_ownership_extremes(season_db, "mock-2026")
    assert set(extremes["extreme"]) == {"highest", "lowest"}


def test_result_import_succeeds_without_player_level_data(tmp_path):
    csv_path = tmp_path / "no_lineups.csv"
    csv_path.write_text(
        "Rank,Entry ID,Entry Name,Points\n"
        "1,1,NorthStarDFS,180\n"
        "2,2,CourtVision88,170\n",
        encoding="utf-8",
    )
    connection = connect_database(tmp_path / "no_lineups.sqlite")
    summary = import_week(
        connection,
        csv_path,
        MEMBERS,
        season_name="No Lineups",
        season_identifier="no-lineups",
        week_number=1,
    )
    assert summary.member_result_rows_stored == 2
    assert summary.lineup_player_rows_stored == 0
    assert any("No lineup-player records" in warning for warning in summary.warnings)
    assert any("No prize/winnings field" in warning for warning in summary.warnings)
    results = weekly_results(connection, "no-lineups")
    assert results["prize_cents"].isna().all()
    assert results["money_won"].isna().all()
    tournament = tournament_performance_leaderboard(
        connection, "no-lineups"
    ).set_index("display_name")
    assert tournament["weeks_with_prize_data"].eq(0).all()
    assert tournament["total_money_won"].isna().all()
    assert tournament["cashes"].isna().all()
    assert not tournament["prize_data_complete"].any()
    connection.close()


def test_lineup_parser_supports_delimited_and_inline_formats():
    assert parse_lineup_text("QB Comet Core | RB Nova North") == [
        ("QB", "Comet Core"),
        ("RB", "Nova North"),
    ]
    assert parse_lineup_text("QB Comet Core RB Nova North WR River Rush") == [
        ("QB", "Comet Core"),
        ("RB", "Nova North"),
        ("WR", "River Rush"),
    ]
    assert parse_lineup_text("an unknown export format") == []
