import json
from pathlib import Path

import pytest

from kcdk.facts import (
    Fact,
    FactEngineConfig,
    build_weekly_fact_report,
    score_fact,
    select_facts,
)
from kcdk.persistence import connect_database, import_week


ROOT = Path(__file__).parents[1]
MOCK = ROOT / "data" / "mock"
MEMBERS = MOCK / "members.csv"


@pytest.fixture
def fact_db(tmp_path):
    connection = connect_database(tmp_path / "facts.sqlite")
    for week in range(1, 5):
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
    yield connection
    connection.close()


def facts_by_type(report, fact_type):
    return [fact for fact in report.candidate_facts if fact.fact_type == fact_type]


def test_weekly_winner_last_place_and_margin_facts(fact_db):
    report = build_weekly_fact_report(fact_db, "mock-2026")

    winner = facts_by_type(report, "weekly_winner")[0]
    assert winner.subject_member_name == "Sam Ellis"
    assert winner.values == {"kcdk_finish": 1, "draftkings_points": 198.0}

    last = facts_by_type(report, "weekly_last_place")[0]
    assert last.subject_member_name == "Taylor Quinn"
    assert last.values["kcdk_finish"] == 5

    victory = facts_by_type(report, "margin_of_victory")[0]
    closest = facts_by_type(report, "closest_weekly_finish")[0]
    biggest = facts_by_type(report, "biggest_weekly_gap")[0]
    assert victory.values["point_margin"] == pytest.approx(9)
    assert closest.values["point_gap"] == pytest.approx(3)
    assert biggest.values["point_gap"] == pytest.approx(13)


def test_result_player_and_known_zero_streaks(fact_db):
    report = build_weekly_fact_report(fact_db, "mock-2026")

    last_streak = facts_by_type(report, "consecutive_last_places")[0]
    assert last_streak.subject_member_name == "Taylor Quinn"
    assert last_streak.values["consecutive_weeks"] == 4

    player_streak = next(
        fact
        for fact in facts_by_type(report, "consecutive_player_use")
        if fact.subject_member_name == "Casey North"
        and fact.related_player == "Comet Core"
    )
    assert player_streak.values["consecutive_weeks"] == 4

    zero_streak = next(
        fact
        for fact in facts_by_type(report, "consecutive_known_zeroes")
        if fact.subject_member_name == "Taylor Quinn"
    )
    assert zero_streak.values["consecutive_weeks"] == 3
    assert zero_streak.values["end_week"] == "Week 3"
    assert zero_streak.completeness == "partial"
    assert "Week 4" not in zero_streak.summary


def test_cross_leaderboard_contrast_and_historical_cutoff(fact_db):
    latest = build_weekly_fact_report(fact_db, "mock-2026")
    contrast = facts_by_type(latest, "different_leaderboard_leaders")[0]
    assert contrast.subject_member_name == "Casey North"
    assert contrast.related_member_name == "Alex Rowan"

    week_two_id = fact_db.execute(
        "SELECT id FROM contests WHERE week_label = 'Week 2'"
    ).fetchone()[0]
    week_two = build_weekly_fact_report(fact_db, "mock-2026", week_two_id)
    earnings_leader = facts_by_type(
        week_two, "tournament_earnings_leader"
    )[0]
    assert earnings_leader.subject_member_name == "Jordan Vale"
    assert earnings_leader.values["total_money_won"] == pytest.approx(125.50)


def test_unanimous_unique_and_ownership_facts(fact_db):
    report = build_weekly_fact_report(fact_db, "mock-2026")

    unanimous = next(
        fact
        for fact in facts_by_type(report, "unanimous_player")
        if fact.related_player == "Atlas Common"
    )
    assert unanimous.week_label == "Week 2"
    assert unanimous.values["member_count"] == 5

    unique = next(
        fact
        for fact in facts_by_type(report, "unique_player")
        if fact.related_player == "Zephyr Solo" and fact.week_label == "Week 1"
    )
    assert unique.subject_member_name == "Casey North"

    low = facts_by_type(report, "lowest_owned_weekly_selection")[0]
    assert low.related_player == "Anchor Low"
    assert low.values["field_ownership"] == pytest.approx(3)


def test_head_to_head_record_and_current_streak(fact_db):
    report = build_weekly_fact_report(fact_db, "mock-2026")
    record = next(
        fact
        for fact in facts_by_type(report, "head_to_head_record")
        if {fact.subject_member_name, fact.related_member_name}
        == {"Casey North", "Taylor Quinn"}
    )
    assert record.subject_member_name == "Casey North"
    assert record.values["shared_weeks"] == 4
    assert record.values["subject_finished_ahead"] == 4
    assert record.values["tied_finishes"] == 0

    streak = next(
        fact
        for fact in facts_by_type(report, "head_to_head_streak")
        if fact.subject_member_name == "Casey North"
        and fact.related_member_name == "Taylor Quinn"
    )
    assert streak.values["consecutive_shared_weeks"] == 4


def test_latest_week_establishes_season_records(fact_db):
    report = build_weekly_fact_report(fact_db, "mock-2026")
    high = facts_by_type(report, "season_high_score_record")[0]
    assert high.subject_member_name == "Sam Ellis"
    assert high.values == {
        "record_value": 198.0,
        "prior_record": 195.0,
        "record_status": "set",
    }
    assert facts_by_type(report, "season_victory_margin_record")[0].values[
        "record_value"
    ] == pytest.approx(9)
    assert facts_by_type(report, "season_closest_margin_record")[0].values[
        "record_value"
    ] == pytest.approx(3)


def _selection_fact(
    fact_type, category, priority, subject_id, subject_name, summary
):
    return Fact(
        fact_type=fact_type,
        category=category,
        summary=summary,
        priority=priority,
        season_identifier="selection-test",
        subject_member_id=subject_id,
        subject_member_name=subject_name,
        tags=(category,),
    )


def test_priority_scoring_and_balanced_selection_are_deterministic():
    assert score_fact("consecutive_last_places", streak_length=4) == 98
    assert score_fact("season_high_score_record", record=True) == 100

    candidates = [
        _selection_fact(
            "weekly_winner", "weekly_result", 100 - index, 1, "Member A", f"A {index}"
        )
        for index in range(5)
    ]
    candidates.extend(
        [
            _selection_fact("season_winnings", "money", 90, 2, "Member B", "Money"),
            _selection_fact("unique_player", "player_usage", 89, 3, "Member C", "Player"),
            _selection_fact("head_to_head_record", "head_to_head", 88, 4, "Member D", "H2H"),
        ]
    )
    config = FactEngineConfig(max_facts_per_member=2)
    first = select_facts(candidates, max_count=5, config=config)
    second = select_facts(reversed(candidates), max_count=5, config=config)

    assert [fact.to_dict() for fact in first] == [fact.to_dict() for fact in second]
    assert {fact.category for fact in first} == {
        "weekly_result",
        "money",
        "player_usage",
        "head_to_head",
    }
    assert sum(fact.subject_member_id == 1 for fact in first) <= 2


def test_report_json_serialization_and_completeness_warnings(fact_db):
    report = build_weekly_fact_report(fact_db, "mock-2026", max_facts=12)
    payload = json.loads(report.to_json())

    assert payload["generated_candidate_count"] == len(report.candidate_facts)
    assert len(payload["selected_facts"]) == 12
    assert all("priority" in fact and "tags" in fact for fact in payload["selected_facts"])
    assert any("Prize data is incomplete" in warning for warning in report.warnings)
    complete_payload = json.loads(report.to_json(include_candidates=True))
    assert len(complete_payload["candidate_facts"]) == report.generated_candidate_count

    latest_id = report.contest_id
    member_id = fact_db.execute(
        "SELECT member_id FROM member_results WHERE contest_id = ? LIMIT 1",
        (latest_id,),
    ).fetchone()[0]
    fact_db.execute(
        """
        UPDATE lineup_players
        SET draftkings_ownership_percentage = NULL
        WHERE id = (SELECT MIN(id) FROM lineup_players WHERE contest_id = ?)
        """,
        (latest_id,),
    )
    fact_db.execute(
        "DELETE FROM lineup_players WHERE contest_id = ? AND member_id = ?",
        (latest_id, member_id),
    )
    fact_db.commit()
    incomplete = build_weekly_fact_report(fact_db, "mock-2026", latest_id)
    assert any("Player lineup data is incomplete" in warning for warning in incomplete.warnings)
    assert any("Ownership data is incomplete" in warning for warning in incomplete.warnings)


def test_report_with_no_prize_or_lineup_data_does_not_invent_money(tmp_path):
    source = tmp_path / "minimal.csv"
    source.write_text(
        "Rank,Entry ID,Entry Name,Points\n"
        "1,1,NorthStarDFS,180\n"
        "2,2,CourtVision88,170\n",
        encoding="utf-8",
    )
    connection = connect_database(tmp_path / "minimal.sqlite")
    import_week(
        connection,
        source,
        MEMBERS,
        season_name="Minimal",
        season_identifier="minimal",
        week_number=1,
    )
    report = build_weekly_fact_report(connection, "minimal")
    types = {fact.fact_type for fact in report.candidate_facts}

    assert "tournament_earnings_leader" not in types
    assert "season_winnings" not in types
    assert any("Prize data is incomplete" in warning for warning in report.warnings)
    assert any("No player-level lineup rows" in warning for warning in report.warnings)
    assert report.selected_facts
    connection.close()
