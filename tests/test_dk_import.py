from pathlib import Path

import pandas as pd
import pytest

from kcdk.dk_import import (
    import_contest,
    match_kcdk_members,
    normalize_contest,
    normalize_headers,
    percentile_from_rank,
    weekly_standings,
)
from kcdk.members import load_members

ROOT = Path(__file__).parents[1]


def test_header_normalization_and_mock_import():
    assert normalize_headers(["Entry ID", "EntryName", "% Drafted"])["Entry ID"] == "entry_id"
    contest = import_contest(ROOT / "data" / "mock" / "contest.csv")
    assert {"rank", "entry_id", "entry_name", "points", "lineup"}.issubset(contest.columns)
    assert contest["drafted_pct"].iloc[1] == pytest.approx(22.1)


def test_missing_required_columns_reports_found_headers():
    with pytest.raises(ValueError, match="entry_id.*lineup") as error:
        normalize_contest(pd.DataFrame({"Rank": [1], "Points": [10]}))
    assert "Headers found" in str(error.value)


def test_numeric_parsing_handles_commas_blanks_and_percentages():
    contest = normalize_contest(
        pd.DataFrame(
            {
                "Rank": ["1", "2"],
                "Entry ID": ["1,001", "1,002"],
                "Entry Name": ["A", "B"],
                "Points": ["1,234.5", ""],
                "Lineup": ["x", "y"],
                "% Drafted": ["12.5%", "-"],
            }
        )
    )
    assert contest.loc[0, "entry_id"] == 1001
    assert contest.loc[0, "points"] == pytest.approx(1234.5)
    assert pd.isna(contest.loc[1, "points"])
    assert contest.loc[0, "drafted_pct"] == pytest.approx(12.5)
    assert pd.isna(contest.loc[1, "drafted_pct"])


def test_matching_active_members_only():
    contest = import_contest(ROOT / "data" / "mock" / "contest.csv")
    members = load_members(ROOT / "data" / "members.csv")
    matched = match_kcdk_members(contest, members)
    assert matched["display_name"].tolist() == [
        "Casey North",
        "Jordan Vale",
        "Alex Rowan",
        "Sam Ellis",
        "Taylor Quinn",
    ]


def test_standings_ordering_and_tied_points():
    contest = pd.DataFrame(
        {
            "rank": [10, 2, 4],
            "entry_id": [1, 2, 3],
            "entry_name": ["A", "B", "C"],
            "points": [100.0, 120.0, 120.0],
            "lineup": ["x", "y", "z"],
        }
    )
    members = pd.DataFrame(
        {
            "draftkings_name": ["A", "B", "C"],
            "display_name": ["A display", "B display", "C display"],
            "nickname": ["", "", ""],
            "active": [True, True, True],
            "notes": ["", "", ""],
        }
    )
    standings = weekly_standings(contest, members)
    assert standings["draftkings_entry_name"].tolist() == ["B", "C", "A"]
    assert standings["kcdk_rank"].tolist() == [1, 1, 3]


def test_percentile_is_best_to_worst_and_large_field():
    assert percentile_from_rank(1, 100) == pytest.approx(100)
    assert percentile_from_rank(100, 100) == pytest.approx(0)
    assert percentile_from_rank(1, 100_000) == pytest.approx(100)
    assert percentile_from_rank(100_000, 100_000) == pytest.approx(0)
    assert percentile_from_rank(50_000, 100_000) == pytest.approx(50.0005)
