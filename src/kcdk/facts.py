"""Deterministic, factual talking points for future commentary generation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from itertools import combinations
import json
import math
import sqlite3
from typing import Any, Iterable

from .analytics import season_leaderboard, tournament_performance_leaderboard


FACT_PRIORITY_BASES: dict[str, int] = {
    "weekly_winner": 84,
    "weekly_last_place": 72,
    "margin_of_victory": 70,
    "closest_weekly_finish": 68,
    "biggest_weekly_gap": 64,
    "weekly_tie": 82,
    "new_personal_best_finish": 68,
    "new_personal_worst_finish": 64,
    "score_above_personal_average": 70,
    "score_below_personal_average": 66,
    "winner_tournament_result": 66,
    "last_place_tournament_result": 58,
    "consecutive_wins": 92,
    "consecutive_podiums": 78,
    "consecutive_last_places": 90,
    "consecutive_improvement": 74,
    "consecutive_decline": 72,
    "consecutive_cashes": 82,
    "consecutive_known_zeroes": 84,
    "consecutive_player_use": 84,
    "kcdk_standings_leader": 82,
    "tournament_earnings_leader": 84,
    "different_leaderboard_leaders": 96,
    "most_wins_not_leader": 78,
    "best_average_score_not_leader": 76,
    "standings_rank_contrast": 72,
    "season_winnings": 52,
    "largest_single_cash": 80,
    "first_cash": 72,
    "first_cash_above_threshold": 82,
    "known_zero_season_winnings": 82,
    "earnings_gap": 76,
    "tied_earnings_score_tiebreak": 78,
    "winner_did_not_cash": 82,
    "last_place_member_cashed": 80,
    "most_earnings_fewer_wins": 80,
    "most_used_player": 62,
    "unanimous_player": 88,
    "unique_player": 74,
    "player_result_history": 64,
    "player_money_history": 66,
    "lowest_owned_weekly_selection": 70,
    "highest_owned_weekly_selection": 64,
    "member_lowest_average_ownership": 60,
    "member_highest_average_ownership": 58,
    "unique_low_owned_selection": 84,
    "unanimous_high_owned_selection": 86,
    "repeated_high_ownership": 70,
    "repeated_low_ownership": 76,
    "head_to_head_record": 62,
    "head_to_head_streak": 82,
    "season_high_score_record": 94,
    "season_low_score_record": 90,
    "season_victory_margin_record": 94,
    "season_closest_margin_record": 90,
    "season_percentile_record": 90,
    "season_cash_record": 92,
    "season_last_place_streak_record": 92,
    "season_player_streak_record": 90,
}

PRIORITY_BONUSES = {
    "per_streak_week": 2,
    "record": 6,
    "cross_leaderboard": 4,
    "maximum": 100,
}


@dataclass(frozen=True)
class FactEngineConfig:
    """Central thresholds controlling generation volume and selection balance."""

    min_win_streak: int = 2
    min_podium_streak: int = 3
    min_last_place_streak: int = 3
    min_direction_streak: int = 3
    min_cash_streak: int = 2
    min_zero_streak: int = 3
    min_player_streak: int = 3
    min_head_to_head_streak: int = 3
    min_player_uses_for_history: int = 3
    score_deviation_points: float = 10.0
    cash_threshold_dollars: float = 100.0
    low_ownership_percentage: float = 5.0
    high_ownership_percentage: float = 60.0
    standings_rank_gap: int = 2
    default_max_facts: int = 12
    max_facts_per_member: int = 3


@dataclass(frozen=True)
class Fact:
    """A JSON-ready fact with machine values kept separate from its summary."""

    fact_type: str
    category: str
    summary: str
    priority: int
    season_identifier: str
    contest_id: int | None = None
    week_label: str | None = None
    subject_member_id: int | None = None
    subject_member_name: str | None = None
    related_member_id: int | None = None
    related_member_name: str | None = None
    related_player: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    completeness: str = "complete"
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tags"] = list(self.tags)
        return payload

    def to_text(self) -> str:
        return f"[{self.priority:03d}] {self.summary}"


@dataclass(frozen=True)
class WeeklyFactReport:
    season_id: int
    season_identifier: str
    season_name: str
    contest_id: int
    week_label: str
    generated_candidate_count: int
    candidate_facts: tuple[Fact, ...]
    selected_facts: tuple[Fact, ...]
    warnings: tuple[str, ...]

    def to_dict(self, *, include_candidates: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "season": {
                "id": self.season_id,
                "identifier": self.season_identifier,
                "name": self.season_name,
            },
            "contest": {"id": self.contest_id, "week_label": self.week_label},
            "generated_candidate_count": self.generated_candidate_count,
            "selected_facts": [fact.to_dict() for fact in self.selected_facts],
            "warnings": list(self.warnings),
        }
        if include_candidates:
            payload["candidate_facts"] = [
                fact.to_dict() for fact in self.candidate_facts
            ]
        return payload

    def to_json(self, *, include_candidates: bool = False, indent: int = 2) -> str:
        return json.dumps(
            self.to_dict(include_candidates=include_candidates),
            allow_nan=False,
            indent=indent,
            sort_keys=True,
        )

    def debug_lines(self, *, selected_only: bool = True) -> list[str]:
        facts = self.selected_facts if selected_only else self.candidate_facts
        return [fact.to_text() for fact in facts]


def score_fact(
    fact_type: str,
    *,
    streak_length: int = 0,
    magnitude_bonus: int = 0,
    record: bool = False,
) -> int:
    """Return a transparent deterministic priority using centralized weights."""
    base = FACT_PRIORITY_BASES[fact_type]
    streak_bonus = min(
        streak_length * PRIORITY_BONUSES["per_streak_week"], 12
    )
    record_bonus = PRIORITY_BONUSES["record"] if record else 0
    return min(
        PRIORITY_BONUSES["maximum"],
        base + streak_bonus + magnitude_bonus + record_bonus,
    )


def _fetch_dicts(
    connection: sqlite3.Connection, query: str, parameters: Iterable[Any] = ()
) -> list[dict[str, Any]]:
    cursor = connection.execute(query, tuple(parameters))
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _season_context(
    connection: sqlite3.Connection,
    season_reference: str | int,
    contest_id: int | None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if isinstance(season_reference, int):
        season_rows = _fetch_dicts(
            connection, "SELECT * FROM seasons WHERE id = ?", [season_reference]
        )
    else:
        season_rows = _fetch_dicts(
            connection,
            "SELECT * FROM seasons WHERE identifier = ?",
            [season_reference],
        )
    if not season_rows:
        raise ValueError(f"Unknown season: {season_reference}")
    season = season_rows[0]
    contests = _fetch_dicts(
        connection,
        """
        SELECT * FROM contests WHERE season_id = ?
        ORDER BY COALESCE(week_number, 2147483647), contest_date, id
        """,
        [season["id"]],
    )
    if not contests:
        raise ValueError(f"Season {season['identifier']} has no imported contests")
    if contest_id is None:
        target_index = len(contests) - 1
    else:
        matches = [index for index, row in enumerate(contests) if row["id"] == contest_id]
        if not matches:
            raise ValueError(
                f"Contest {contest_id} does not belong to season {season['identifier']}"
            )
        target_index = matches[0]
    relevant = contests[: target_index + 1]
    return season, relevant[-1], relevant


def _load_results(
    connection: sqlite3.Connection, contests: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    contest_ids = [row["id"] for row in contests]
    placeholders = ",".join("?" for _ in contest_ids)
    results = _fetch_dicts(
        connection,
        f"""
        SELECT r.*, c.week_number, c.week_label, m.display_name, m.member_key
        FROM member_results r
        JOIN contests c ON c.id = r.contest_id
        JOIN members m ON m.id = r.member_id
        WHERE r.contest_id IN ({placeholders})
        ORDER BY COALESCE(c.week_number, 2147483647), c.contest_date, c.id,
                 r.kcdk_finish, r.draftkings_overall_rank, m.display_name
        """,
        contest_ids,
    )
    last_finish = {
        contest["id"]: max(
            row["kcdk_finish"]
            for row in results
            if row["contest_id"] == contest["id"]
        )
        for contest in contests
    }
    for row in results:
        row["is_last_place"] = row["kcdk_finish"] == last_finish[row["contest_id"]]
    return results


def _load_lineups(
    connection: sqlite3.Connection, contests: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    contest_ids = [row["id"] for row in contests]
    placeholders = ",".join("?" for _ in contest_ids)
    return _fetch_dicts(
        connection,
        f"""
        SELECT lp.*, c.week_number, c.week_label, m.display_name, m.member_key,
               p.display_name AS player_name, r.kcdk_finish,
               r.draftkings_fantasy_points AS member_points, r.prize_cents
        FROM lineup_players lp
        JOIN contests c ON c.id = lp.contest_id
        JOIN members m ON m.id = lp.member_id
        JOIN players p ON p.id = lp.player_id
        JOIN member_results r
          ON r.contest_id = lp.contest_id AND r.member_id = lp.member_id
        WHERE lp.contest_id IN ({placeholders})
        ORDER BY COALESCE(c.week_number, 2147483647), c.contest_date, c.id,
                 m.display_name, p.display_name
        """,
        contest_ids,
    )


def _make_fact(
    *,
    fact_type: str,
    category: str,
    summary: str,
    season_identifier: str,
    contest: dict[str, Any] | None = None,
    subject: dict[str, Any] | None = None,
    related: dict[str, Any] | None = None,
    related_player: str | None = None,
    values: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    completeness: str = "complete",
    tags: tuple[str, ...] = (),
    streak_length: int = 0,
    magnitude_bonus: int = 0,
    record: bool = False,
) -> Fact:
    return Fact(
        fact_type=fact_type,
        category=category,
        summary=summary,
        priority=score_fact(
            fact_type,
            streak_length=streak_length,
            magnitude_bonus=magnitude_bonus,
            record=record,
        ),
        season_identifier=season_identifier,
        contest_id=None if contest is None else int(contest["id"]),
        week_label=None if contest is None else str(contest["week_label"]),
        subject_member_id=None if subject is None else int(subject["member_id"]),
        subject_member_name=None if subject is None else str(subject["display_name"]),
        related_member_id=None if related is None else int(related["member_id"]),
        related_member_name=None if related is None else str(related["display_name"]),
        related_player=related_player,
        values=values or {},
        evidence=evidence or {},
        completeness=completeness,
        tags=tags,
    )


def _money(value: int | None) -> str:
    return "unknown" if value is None else f"${value / 100:,.2f}"


def _weekly_result_facts(
    season_identifier: str,
    target: dict[str, Any],
    results: list[dict[str, Any]],
    config: FactEngineConfig,
) -> list[Fact]:
    facts: list[Fact] = []
    current = [row for row in results if row["contest_id"] == target["id"]]
    ordered = sorted(
        current,
        key=lambda row: (
            -row["draftkings_fantasy_points"],
            row["draftkings_overall_rank"] or math.inf,
            row["display_name"],
        ),
    )
    winners = [row for row in current if row["kcdk_finish"] == 1]
    last_rows = [row for row in current if row["is_last_place"]]
    for winner in winners:
        facts.append(
            _make_fact(
                fact_type="weekly_winner",
                category="weekly_result",
                summary=(
                    f"{winner['display_name']} finished 1st in KCDK in "
                    f"{target['week_label']} with {winner['draftkings_fantasy_points']:.2f} "
                    "DraftKings points."
                ),
                season_identifier=season_identifier,
                contest=target,
                subject=winner,
                values={
                    "kcdk_finish": 1,
                    "draftkings_points": winner["draftkings_fantasy_points"],
                },
                evidence={"entry_id": winner["entry_id"]},
                tags=("weekly", "kcdk", "winner"),
            )
        )
        facts.append(
            _make_fact(
                fact_type="winner_tournament_result",
                category="weekly_result",
                summary=(
                    f"{winner['display_name']}'s KCDK-winning entry ranked "
                    f"{winner['draftkings_overall_rank']:,} overall at the "
                    f"{winner['tournament_percentile']:.2f} percentile."
                ),
                season_identifier=season_identifier,
                contest=target,
                subject=winner,
                values={
                    "draftkings_overall_rank": winner["draftkings_overall_rank"],
                    "tournament_percentile": winner["tournament_percentile"],
                },
                tags=("weekly", "tournament", "winner"),
            )
        )
    for last in last_rows:
        facts.append(
            _make_fact(
                fact_type="weekly_last_place",
                category="weekly_result",
                summary=(
                    f"{last['display_name']} finished last in KCDK in "
                    f"{target['week_label']} with {last['draftkings_fantasy_points']:.2f} "
                    "DraftKings points."
                ),
                season_identifier=season_identifier,
                contest=target,
                subject=last,
                values={
                    "kcdk_finish": last["kcdk_finish"],
                    "draftkings_points": last["draftkings_fantasy_points"],
                },
                tags=("weekly", "kcdk", "last_place"),
            )
        )
        facts.append(
            _make_fact(
                fact_type="last_place_tournament_result",
                category="weekly_result",
                summary=(
                    f"{last['display_name']}'s last-place KCDK entry ranked "
                    f"{last['draftkings_overall_rank']:,} overall at the "
                    f"{last['tournament_percentile']:.2f} percentile."
                ),
                season_identifier=season_identifier,
                contest=target,
                subject=last,
                values={
                    "draftkings_overall_rank": last["draftkings_overall_rank"],
                    "tournament_percentile": last["tournament_percentile"],
                },
                tags=("weekly", "tournament", "last_place"),
            )
        )

    adjacent = [
        (left, right, left["draftkings_fantasy_points"] - right["draftkings_fantasy_points"])
        for left, right in zip(ordered, ordered[1:])
    ]
    if adjacent:
        top, runner_up, victory_margin = adjacent[0]
        facts.append(
            _make_fact(
                fact_type="margin_of_victory",
                category="weekly_result",
                summary=(
                    f"{top['display_name']} finished {victory_margin:.2f} points ahead "
                    f"of {runner_up['display_name']} in {target['week_label']}."
                ),
                season_identifier=season_identifier,
                contest=target,
                subject=top,
                related=runner_up,
                values={"point_margin": victory_margin},
                tags=("weekly", "margin", "winner"),
                magnitude_bonus=min(int(victory_margin // 5), 5),
            )
        )
        closest = min(adjacent, key=lambda item: (item[2], item[0]["display_name"]))
        biggest = max(adjacent, key=lambda item: (item[2], item[0]["display_name"]))
        for fact_type, pair, descriptor in [
            ("closest_weekly_finish", closest, "closest adjacent finish"),
            ("biggest_weekly_gap", biggest, "largest adjacent gap"),
        ]:
            left, right, gap = pair
            facts.append(
                _make_fact(
                    fact_type=fact_type,
                    category="weekly_result",
                    summary=(
                        f"The {descriptor} in {target['week_label']} was "
                        f"{gap:.2f} points between {left['display_name']} and "
                        f"{right['display_name']}."
                    ),
                    season_identifier=season_identifier,
                    contest=target,
                    subject=left,
                    related=right,
                    values={"point_gap": gap},
                    tags=("weekly", "margin"),
                )
            )

    tied_groups: dict[int, list[dict[str, Any]]] = {}
    for row in current:
        tied_groups.setdefault(row["kcdk_finish"], []).append(row)
    for finish, rows in tied_groups.items():
        if len(rows) < 2:
            continue
        names = sorted(row["display_name"] for row in rows)
        facts.append(
            _make_fact(
                fact_type="weekly_tie",
                category="weekly_result",
                summary=(
                    f"{', '.join(names)} tied for KCDK finish {finish} in "
                    f"{target['week_label']} with "
                    f"{rows[0]['draftkings_fantasy_points']:.2f} points."
                ),
                season_identifier=season_identifier,
                contest=target,
                subject=rows[0],
                related=rows[1],
                values={"kcdk_finish": finish, "member_names": names},
                tags=("weekly", "tie", "kcdk"),
            )
        )

    for row in current:
        history = [
            item
            for item in results
            if item["member_id"] == row["member_id"]
            and item["contest_id"] != target["id"]
        ]
        if not history:
            continue
        prior_finishes = [item["kcdk_finish"] for item in history]
        if row["kcdk_finish"] < min(prior_finishes):
            facts.append(
                _make_fact(
                    fact_type="new_personal_best_finish",
                    category="weekly_result",
                    summary=(
                        f"{row['display_name']} set a season-best KCDK finish of "
                        f"{row['kcdk_finish']} in {target['week_label']}."
                    ),
                    season_identifier=season_identifier,
                    contest=target,
                    subject=row,
                    values={"finish": row["kcdk_finish"], "prior_best": min(prior_finishes)},
                    tags=("weekly", "personal_record", "kcdk"),
                    record=True,
                )
            )
        if row["kcdk_finish"] > max(prior_finishes):
            facts.append(
                _make_fact(
                    fact_type="new_personal_worst_finish",
                    category="weekly_result",
                    summary=(
                        f"{row['display_name']} recorded a new season-worst KCDK finish of "
                        f"{row['kcdk_finish']} in {target['week_label']}."
                    ),
                    season_identifier=season_identifier,
                    contest=target,
                    subject=row,
                    values={"finish": row["kcdk_finish"], "prior_worst": max(prior_finishes)},
                    tags=("weekly", "personal_record", "kcdk"),
                    record=True,
                )
            )
        prior_average = sum(item["draftkings_fantasy_points"] for item in history) / len(history)
        difference = row["draftkings_fantasy_points"] - prior_average
        if abs(difference) >= config.score_deviation_points:
            above = difference > 0
            facts.append(
                _make_fact(
                    fact_type=(
                        "score_above_personal_average"
                        if above
                        else "score_below_personal_average"
                    ),
                    category="weekly_result",
                    summary=(
                        f"{row['display_name']} scored {abs(difference):.2f} points "
                        f"{'above' if above else 'below'} their prior season average "
                        f"in {target['week_label']}."
                    ),
                    season_identifier=season_identifier,
                    contest=target,
                    subject=row,
                    values={
                        "current_points": row["draftkings_fantasy_points"],
                        "prior_average_points": prior_average,
                        "difference": difference,
                    },
                    tags=("weekly", "score", "personal_history"),
                    magnitude_bonus=min(int(abs(difference) // 10), 5),
                )
            )
    return facts


def _ending_streak(rows: list[dict[str, Any]], predicate: Any) -> int:
    length = 0
    for row in reversed(rows):
        if not predicate(row):
            break
        length += 1
    return length


def _longest_streak(
    rows: list[dict[str, Any]], predicate: Any
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    best: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for row in rows:
        if predicate(row):
            current.append(row)
            if len(current) > len(best):
                best = current.copy()
        else:
            current = []
    return (
        len(best),
        None if not best else best[0],
        None if not best else best[-1],
    )


def _streak_facts(
    season_identifier: str,
    target: dict[str, Any],
    contests: list[dict[str, Any]],
    results: list[dict[str, Any]],
    lineups: list[dict[str, Any]],
    config: FactEngineConfig,
) -> list[Fact]:
    facts: list[Fact] = []
    member_rows: dict[int, list[dict[str, Any]]] = {}
    for row in results:
        member_rows.setdefault(row["member_id"], []).append(row)
    specifications = [
        (
            "consecutive_wins",
            "wins",
            config.min_win_streak,
            lambda row: row["kcdk_finish"] == 1,
        ),
        (
            "consecutive_podiums",
            "podium finishes",
            config.min_podium_streak,
            lambda row: row["kcdk_finish"] <= 3,
        ),
        (
            "consecutive_last_places",
            "last-place finishes",
            config.min_last_place_streak,
            lambda row: row["is_last_place"],
        ),
        (
            "consecutive_cashes",
            "known cashes",
            config.min_cash_streak,
            lambda row: row["prize_cents"] is not None
            and row["prize_cents"] > 0,
        ),
    ]
    for rows in member_rows.values():
        member = rows[-1]
        for fact_type, label, minimum, predicate in specifications:
            length = _ending_streak(rows, predicate)
            if length >= minimum:
                facts.append(
                    _make_fact(
                        fact_type=fact_type,
                        category="streak",
                        summary=(
                            f"{member['display_name']} has {length} consecutive {label} "
                            f"through {target['week_label']}."
                        ),
                        season_identifier=season_identifier,
                        contest=target,
                        subject=member,
                        values={"consecutive_weeks": length},
                        tags=("streak", "kcdk" if "cash" not in fact_type else "money"),
                        streak_length=length,
                    )
                )

        zero_length, start, end = _longest_streak(
            rows, lambda row: row["prize_cents"] == 0
        )
        if zero_length >= config.min_zero_streak and start and end:
            known = sum(row["prize_cents"] is not None for row in rows)
            completeness = "complete" if known == len(rows) else "partial"
            facts.append(
                _make_fact(
                    fact_type="consecutive_known_zeroes",
                    category="streak",
                    summary=(
                        f"{member['display_name']} recorded $0 in {zero_length} "
                        f"consecutive weeks with known prize data from "
                        f"{start['week_label']} through {end['week_label']}."
                    ),
                    season_identifier=season_identifier,
                    contest=target,
                    subject=member,
                    values={
                        "consecutive_weeks": zero_length,
                        "start_week": start["week_label"],
                        "end_week": end["week_label"],
                        "weeks_with_prize_data": known,
                        "weeks_played": len(rows),
                    },
                    completeness=completeness,
                    tags=("streak", "money", "known_zero"),
                    streak_length=zero_length,
                )
            )

        if len(rows) >= config.min_direction_streak:
            improving = 1
            declining = 1
            for current, previous in zip(reversed(rows[1:]), reversed(rows[:-1])):
                if current["kcdk_finish"] < previous["kcdk_finish"]:
                    improving += 1
                else:
                    break
            for current, previous in zip(reversed(rows[1:]), reversed(rows[:-1])):
                if current["kcdk_finish"] > previous["kcdk_finish"]:
                    declining += 1
                else:
                    break
            for fact_type, length, direction in [
                ("consecutive_improvement", improving, "improved"),
                ("consecutive_decline", declining, "declined"),
            ]:
                if length >= config.min_direction_streak:
                    facts.append(
                        _make_fact(
                            fact_type=fact_type,
                            category="streak",
                            summary=(
                                f"{member['display_name']}'s KCDK finish {direction} "
                                f"in {length} consecutive weeks through {target['week_label']}."
                            ),
                            season_identifier=season_identifier,
                            contest=target,
                            subject=member,
                            values={"consecutive_weeks": length},
                            tags=("streak", "kcdk", "direction"),
                            streak_length=length,
                        )
                    )

    contest_order = {row["id"]: index for index, row in enumerate(contests)}
    grouped_lineups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for row in lineups:
        grouped_lineups.setdefault((row["member_id"], row["player_id"]), []).append(row)
    for rows in grouped_lineups.values():
        positions = sorted({contest_order[row["contest_id"]] for row in rows})
        run: list[int] = [positions[0]]
        runs: list[list[int]] = []
        for position in positions[1:]:
            if position == run[-1] + 1:
                run.append(position)
            else:
                runs.append(run)
                run = [position]
        runs.append(run)
        longest = max(runs, key=len)
        if len(longest) < config.min_player_streak:
            continue
        row = rows[-1]
        start_week = contests[longest[0]]["week_label"]
        end_week = contests[longest[-1]]["week_label"]
        facts.append(
            _make_fact(
                fact_type="consecutive_player_use",
                category="player_usage",
                summary=(
                    f"{row['display_name']} rostered {row['player_name']} in "
                    f"{len(longest)} consecutive weeks from {start_week} through {end_week}."
                ),
                season_identifier=season_identifier,
                contest=target,
                subject=row,
                related_player=row["player_name"],
                values={
                    "consecutive_weeks": len(longest),
                    "start_week": start_week,
                    "end_week": end_week,
                },
                tags=("streak", "player_usage"),
                streak_length=len(longest),
            )
        )
    return facts


def _season_and_money_facts(
    connection: sqlite3.Connection,
    season: dict[str, Any],
    target: dict[str, Any],
    results: list[dict[str, Any]],
    config: FactEngineConfig,
) -> list[Fact]:
    facts: list[Fact] = []
    identifier = season["identifier"]
    kcdk = season_leaderboard(connection, identifier, target["id"])
    tournament = tournament_performance_leaderboard(
        connection, identifier, target["id"]
    )
    member_lookup = {row["display_name"]: row for row in results}
    kcdk_leader = kcdk.iloc[0].to_dict()
    kcdk_member = member_lookup[kcdk_leader["display_name"]]
    facts.append(
        _make_fact(
            fact_type="kcdk_standings_leader",
            category="season_standings",
            summary=(
                f"{kcdk_leader['display_name']} leads the KCDK standings with "
                f"an average finish of {kcdk_leader['average_finish']:.2f}."
            ),
            season_identifier=identifier,
            contest=target,
            subject=kcdk_member,
            values={
                "average_finish": kcdk_leader["average_finish"],
                "wins": int(kcdk_leader["wins"]),
            },
            tags=("leaderboard", "kcdk"),
        )
    )
    known_tournament = tournament.dropna(subset=["total_money_won"])
    if not known_tournament.empty:
        tournament_leader = known_tournament.iloc[0].to_dict()
        tournament_member = member_lookup[tournament_leader["display_name"]]
        facts.append(
            _make_fact(
                fact_type="tournament_earnings_leader",
                category="season_standings",
                summary=(
                    f"{tournament_leader['display_name']} leads known tournament earnings "
                    f"with ${tournament_leader['total_money_won']:,.2f}."
                ),
                season_identifier=identifier,
                contest=target,
                subject=tournament_member,
                values={
                    "total_money_won": tournament_leader["total_money_won"],
                    "weeks_with_prize_data": int(
                        tournament_leader["weeks_with_prize_data"]
                    ),
                    "weeks_played": int(tournament_leader["weeks_played"]),
                },
                completeness=(
                    "complete"
                    if bool(tournament_leader["prize_data_complete"])
                    else "partial"
                ),
                tags=("leaderboard", "money"),
            )
        )
        if kcdk_leader["display_name"] != tournament_leader["display_name"]:
            facts.append(
                _make_fact(
                    fact_type="different_leaderboard_leaders",
                    category="season_contrast",
                    summary=(
                        f"{kcdk_leader['display_name']} leads the KCDK standings, while "
                        f"{tournament_leader['display_name']} leads known tournament earnings."
                    ),
                    season_identifier=identifier,
                    contest=target,
                    subject=kcdk_member,
                    related=tournament_member,
                    values={
                        "kcdk_leader_average_finish": kcdk_leader[
                            "average_finish"
                        ],
                        "earnings_leader_total_money_won": tournament_leader[
                            "total_money_won"
                        ],
                    },
                    tags=("leaderboard", "contrast", "money", "kcdk"),
                    magnitude_bonus=PRIORITY_BONUSES["cross_leaderboard"],
                )
            )

    most_wins = kcdk["wins"].max()
    win_leaders = kcdk.loc[kcdk["wins"].eq(most_wins)]
    if len(win_leaders) == 1 and win_leaders.iloc[0]["display_name"] != kcdk_leader["display_name"]:
        row = win_leaders.iloc[0].to_dict()
        member = member_lookup[row["display_name"]]
        facts.append(
            _make_fact(
                fact_type="most_wins_not_leader",
                category="season_contrast",
                summary=(
                    f"{row['display_name']} has the most KCDK weekly wins ({int(most_wins)}), "
                    f"while {kcdk_leader['display_name']} leads by average finish."
                ),
                season_identifier=identifier,
                contest=target,
                subject=member,
                related=kcdk_member,
                values={"wins": int(most_wins)},
                tags=("leaderboard", "contrast", "wins"),
            )
        )

    best_score_row = kcdk.sort_values(
        ["average_draftkings_fantasy_points", "display_name"],
        ascending=[False, True],
    ).iloc[0]
    if best_score_row["display_name"] != kcdk_leader["display_name"]:
        member = member_lookup[best_score_row["display_name"]]
        facts.append(
            _make_fact(
                fact_type="best_average_score_not_leader",
                category="season_contrast",
                summary=(
                    f"{best_score_row['display_name']} has the highest average DraftKings "
                    f"score ({best_score_row['average_draftkings_fantasy_points']:.2f}), "
                    f"while {kcdk_leader['display_name']} leads the KCDK standings."
                ),
                season_identifier=identifier,
                contest=target,
                subject=member,
                related=kcdk_member,
                values={
                    "average_draftkings_points": best_score_row[
                        "average_draftkings_fantasy_points"
                    ]
                },
                tags=("leaderboard", "contrast", "score"),
            )
        )

    tournament_by_name = tournament.set_index("display_name")
    percentile_order = tournament.sort_values(
        ["average_tournament_percentile", "display_name"], ascending=[False, True]
    )
    percentile_ranks = {
        name: index + 1 for index, name in enumerate(percentile_order["display_name"])
    }
    for _, row in kcdk.iterrows():
        name = row["display_name"]
        gap = abs(int(row["season_rank"]) - percentile_ranks[name])
        if gap >= config.standings_rank_gap:
            member = member_lookup[name]
            facts.append(
                _make_fact(
                    fact_type="standings_rank_contrast",
                    category="season_contrast",
                    summary=(
                        f"{name} ranks {int(row['season_rank'])} in KCDK average finish "
                        f"and {percentile_ranks[name]} in average tournament percentile."
                    ),
                    season_identifier=identifier,
                    contest=target,
                    subject=member,
                    values={
                        "kcdk_rank": int(row["season_rank"]),
                        "tournament_percentile_rank": percentile_ranks[name],
                        "average_finish": row["average_finish"],
                        "average_tournament_percentile": tournament_by_name.loc[
                            name, "average_tournament_percentile"
                        ],
                    },
                    tags=("leaderboard", "contrast", "percentile"),
                    magnitude_bonus=min(gap, 5),
                )
            )

    result_by_member: dict[int, list[dict[str, Any]]] = {}
    for row in results:
        result_by_member.setdefault(row["member_id"], []).append(row)
    for rows in result_by_member.values():
        member = rows[-1]
        known = [row for row in rows if row["prize_cents"] is not None]
        total_cents = sum(row["prize_cents"] for row in known)
        completeness = "complete" if len(known) == len(rows) else "partial"
        if known:
            facts.append(
                _make_fact(
                    fact_type="season_winnings",
                    category="money",
                    summary=(
                        f"{member['display_name']} has {_money(total_cents)} in known "
                        f"tournament winnings across {len(known)} prize-reported weeks."
                    ),
                    season_identifier=identifier,
                    contest=target,
                    subject=member,
                    values={
                        "total_money_won": total_cents / 100,
                        "weeks_with_prize_data": len(known),
                        "weeks_played": len(rows),
                    },
                    completeness=completeness,
                    tags=("money", "season_total"),
                )
            )
        if known and total_cents == 0 and len(known) >= config.min_zero_streak:
            facts.append(
                _make_fact(
                    fact_type="known_zero_season_winnings",
                    category="money",
                    summary=(
                        f"{member['display_name']} has $0 in known tournament winnings "
                        f"across {len(known)} weeks with prize data."
                    ),
                    season_identifier=identifier,
                    contest=target,
                    subject=member,
                    values={
                        "total_money_won": 0,
                        "weeks_with_prize_data": len(known),
                        "weeks_played": len(rows),
                    },
                    completeness=completeness,
                    tags=("money", "known_zero"),
                    streak_length=len(known),
                )
            )

        positive = [row for row in known if row["prize_cents"] > 0]
        if positive:
            largest = max(positive, key=lambda row: row["prize_cents"])
            facts.append(
                _make_fact(
                    fact_type="largest_single_cash",
                    category="money",
                    summary=(
                        f"{member['display_name']}'s largest known single cash is "
                        f"{_money(largest['prize_cents'])} in {largest['week_label']}."
                    ),
                    season_identifier=identifier,
                    contest=target,
                    subject=member,
                    values={
                        "largest_cash": largest["prize_cents"] / 100,
                        "cash_week": largest["week_label"],
                    },
                    completeness=completeness,
                    tags=("money", "cash"),
                    magnitude_bonus=min(largest["prize_cents"] // 10000, 5),
                )
            )
            current = rows[-1]
            prior_positive = [row for row in positive if row["contest_id"] != target["id"]]
            if current["prize_cents"] is not None and current["prize_cents"] > 0:
                if not prior_positive:
                    facts.append(
                        _make_fact(
                            fact_type="first_cash",
                            category="money",
                            summary=(
                                f"{member['display_name']} recorded their first known cash "
                                f"of the season in {target['week_label']}: "
                                f"{_money(current['prize_cents'])}."
                            ),
                            season_identifier=identifier,
                            contest=target,
                            subject=member,
                            values={"money_won": current["prize_cents"] / 100},
                            tags=("money", "first", "cash"),
                        )
                    )
                threshold_cents = int(round(config.cash_threshold_dollars * 100))
                prior_above = [
                    row for row in prior_positive if row["prize_cents"] >= threshold_cents
                ]
                if current["prize_cents"] >= threshold_cents and not prior_above:
                    facts.append(
                        _make_fact(
                            fact_type="first_cash_above_threshold",
                            category="money",
                            summary=(
                                f"{member['display_name']} recorded their first known cash "
                                f"of at least ${config.cash_threshold_dollars:,.2f} in "
                                f"{target['week_label']}: {_money(current['prize_cents'])}."
                            ),
                            season_identifier=identifier,
                            contest=target,
                            subject=member,
                            values={
                                "money_won": current["prize_cents"] / 100,
                                "threshold": config.cash_threshold_dollars,
                            },
                            tags=("money", "first", "threshold"),
                        )
                    )

    current = [row for row in results if row["contest_id"] == target["id"]]
    for row in current:
        if row["prize_cents"] is None:
            continue
        if row["kcdk_finish"] == 1 and row["prize_cents"] == 0:
            facts.append(
                _make_fact(
                    fact_type="winner_did_not_cash",
                    category="money",
                    summary=(
                        f"{row['display_name']} won KCDK in {target['week_label']} and "
                        "recorded $0 in tournament winnings."
                    ),
                    season_identifier=identifier,
                    contest=target,
                    subject=row,
                    values={"kcdk_finish": 1, "money_won": 0},
                    tags=("money", "weekly", "contrast"),
                )
            )
        if row["is_last_place"] and row["prize_cents"] > 0:
            facts.append(
                _make_fact(
                    fact_type="last_place_member_cashed",
                    category="money",
                    summary=(
                        f"{row['display_name']} finished last in KCDK in "
                        f"{target['week_label']} and won {_money(row['prize_cents'])} "
                        "in the tournament."
                    ),
                    season_identifier=identifier,
                    contest=target,
                    subject=row,
                    values={
                        "kcdk_finish": row["kcdk_finish"],
                        "money_won": row["prize_cents"] / 100,
                    },
                    tags=("money", "weekly", "contrast"),
                )
            )

    if len(known_tournament) >= 2:
        first, second = known_tournament.iloc[0], known_tournament.iloc[1]
        gap = first["total_money_won"] - second["total_money_won"]
        facts.append(
            _make_fact(
                fact_type="earnings_gap",
                category="money",
                summary=(
                    f"{first['display_name']} leads {second['display_name']} in known "
                    f"tournament winnings by ${gap:,.2f}."
                ),
                season_identifier=identifier,
                contest=target,
                subject=member_lookup[first["display_name"]],
                related=member_lookup[second["display_name"]],
                values={"earnings_gap": gap},
                tags=("money", "leaderboard", "gap"),
                magnitude_bonus=min(int(gap // 25), 5),
            )
        )
    for (_, left), (_, right) in combinations(known_tournament.iterrows(), 2):
        if left["total_money_won"] != right["total_money_won"]:
            continue
        if left["average_draftkings_fantasy_points"] == right[
            "average_draftkings_fantasy_points"
        ]:
            continue
        higher, lower = sorted(
            [left, right],
            key=lambda row: row["average_draftkings_fantasy_points"],
            reverse=True,
        )
        facts.append(
            _make_fact(
                fact_type="tied_earnings_score_tiebreak",
                category="money",
                summary=(
                    f"{higher['display_name']} and {lower['display_name']} are tied at "
                    f"${higher['total_money_won']:,.2f} in known winnings; "
                    f"{higher['display_name']} has the higher average DraftKings score."
                ),
                season_identifier=identifier,
                contest=target,
                subject=member_lookup[higher["display_name"]],
                related=member_lookup[lower["display_name"]],
                values={
                    "total_money_won": higher["total_money_won"],
                    "subject_average_points": higher[
                        "average_draftkings_fantasy_points"
                    ],
                    "related_average_points": lower[
                        "average_draftkings_fantasy_points"
                    ],
                },
                completeness=(
                    "complete"
                    if bool(higher["prize_data_complete"])
                    and bool(lower["prize_data_complete"])
                    else "partial"
                ),
                tags=("money", "tie", "tiebreak"),
            )
        )
    return facts


def _player_and_ownership_facts(
    connection: sqlite3.Connection,
    season: dict[str, Any],
    target: dict[str, Any],
    contests: list[dict[str, Any]],
    lineups: list[dict[str, Any]],
    config: FactEngineConfig,
) -> list[Fact]:
    facts: list[Fact] = []
    identifier = season["identifier"]
    active_count = connection.execute(
        "SELECT COUNT(*) FROM season_members WHERE season_id = ? AND active = 1",
        [season["id"]],
    ).fetchone()[0]
    by_contest_player: dict[tuple[int, int], list[dict[str, Any]]] = {}
    by_member_player: dict[tuple[int, int], list[dict[str, Any]]] = {}
    by_member: dict[int, list[dict[str, Any]]] = {}
    for row in lineups:
        by_contest_player.setdefault((row["contest_id"], row["player_id"]), []).append(row)
        by_member_player.setdefault((row["member_id"], row["player_id"]), []).append(row)
        by_member.setdefault(row["member_id"], []).append(row)

    for (contest_id, _), rows in by_contest_player.items():
        member_ids = {row["member_id"] for row in rows}
        row = rows[0]
        contest = next(item for item in contests if item["id"] == contest_id)
        if len(member_ids) == active_count:
            ownership = row["draftkings_ownership_percentage"]
            facts.append(
                _make_fact(
                    fact_type="unanimous_player",
                    category="player_usage",
                    summary=(
                        f"Every active KCDK member rostered {row['player_name']} in "
                        f"{contest['week_label']}."
                    ),
                    season_identifier=identifier,
                    contest=contest,
                    related_player=row["player_name"],
                    values={
                        "member_count": len(member_ids),
                        "field_ownership": ownership,
                    },
                    tags=("player_usage", "unanimous"),
                )
            )
            if ownership is not None and ownership >= config.high_ownership_percentage:
                facts.append(
                    _make_fact(
                        fact_type="unanimous_high_owned_selection",
                        category="ownership",
                        summary=(
                            f"Every active KCDK member rostered {row['player_name']} in "
                            f"{contest['week_label']}; field ownership was {ownership:.2f}%."
                        ),
                        season_identifier=identifier,
                        contest=contest,
                        related_player=row["player_name"],
                        values={
                            "member_count": len(member_ids),
                            "field_ownership": ownership,
                        },
                        tags=("ownership", "unanimous", "high_ownership"),
                    )
                )
        if len(member_ids) == 1:
            facts.append(
                _make_fact(
                    fact_type="unique_player",
                    category="player_usage",
                    summary=(
                        f"{row['display_name']} was the only KCDK member to roster "
                        f"{row['player_name']} in {contest['week_label']}."
                    ),
                    season_identifier=identifier,
                    contest=contest,
                    subject=row,
                    related_player=row["player_name"],
                    values={
                        "field_ownership": row["draftkings_ownership_percentage"]
                    },
                    tags=("player_usage", "unique"),
                )
            )
            ownership = row["draftkings_ownership_percentage"]
            if ownership is not None and ownership <= config.low_ownership_percentage:
                facts.append(
                    _make_fact(
                        fact_type="unique_low_owned_selection",
                        category="ownership",
                        summary=(
                            f"{row['display_name']} uniquely rostered {row['player_name']} "
                            f"in {contest['week_label']} at {ownership:.2f}% field ownership."
                        ),
                        season_identifier=identifier,
                        contest=contest,
                        subject=row,
                        related_player=row["player_name"],
                        values={"field_ownership": ownership},
                        tags=("ownership", "unique", "low_ownership"),
                    )
                )

    for rows in by_member_player.values():
        selection = rows[-1]
        count = len({row["contest_id"] for row in rows})
        if count < config.min_player_uses_for_history:
            continue
        finishes = [row["kcdk_finish"] for row in rows]
        known_money = [row["prize_cents"] for row in rows if row["prize_cents"] is not None]
        cashes = sum(value > 0 for value in known_money)
        wins = sum(value == 1 for value in finishes)
        podiums = sum(value <= 3 for value in finishes)
        facts.append(
            _make_fact(
                fact_type="player_result_history",
                category="player_usage",
                summary=(
                    f"{selection['display_name']} averaged a {sum(finishes) / count:.2f} "
                    f"KCDK finish in {count} weeks with {selection['player_name']}, "
                    f"including {wins} wins and {podiums} podiums."
                ),
                season_identifier=identifier,
                contest=target,
                subject=selection,
                related_player=selection["player_name"],
                values={
                    "times_rostered": count,
                    "average_kcdk_finish": sum(finishes) / count,
                    "wins": wins,
                    "podiums": podiums,
                },
                tags=("player_usage", "result_history"),
            )
        )
        if known_money:
            completeness = "complete" if len(known_money) == count else "partial"
            facts.append(
                _make_fact(
                    fact_type="player_money_history",
                    category="player_usage",
                    summary=(
                        f"{selection['display_name']} has {_money(sum(known_money))} and "
                        f"{cashes} cashes in {len(known_money)} prize-reported weeks with "
                        f"{selection['player_name']} rostered."
                    ),
                    season_identifier=identifier,
                    contest=target,
                    subject=selection,
                    related_player=selection["player_name"],
                    values={
                        "times_rostered": count,
                        "weeks_with_prize_data": len(known_money),
                        "total_money_won": sum(known_money) / 100,
                        "cashes": cashes,
                    },
                    completeness=completeness,
                    tags=("player_usage", "money", "history"),
                )
            )
        ownership = [
            row["draftkings_ownership_percentage"]
            for row in rows
            if row["draftkings_ownership_percentage"] is not None
        ]
        if ownership:
            average = sum(ownership) / len(ownership)
            fact_type = None
            label = None
            if average >= config.high_ownership_percentage:
                fact_type, label = "repeated_high_ownership", "at least"
            elif average <= config.low_ownership_percentage:
                fact_type, label = "repeated_low_ownership", "at most"
            if fact_type:
                threshold = (
                    config.high_ownership_percentage
                    if fact_type == "repeated_high_ownership"
                    else config.low_ownership_percentage
                )
                facts.append(
                    _make_fact(
                        fact_type=fact_type,
                        category="ownership",
                        summary=(
                            f"{selection['display_name']} rostered {selection['player_name']} "
                            f"{count} times with average field ownership of {average:.2f}% "
                            f"({label} the configured {threshold:.2f}% threshold)."
                        ),
                        season_identifier=identifier,
                        contest=target,
                        subject=selection,
                        related_player=selection["player_name"],
                        values={
                            "times_rostered": count,
                            "average_field_ownership": average,
                        },
                        tags=("ownership", "repeated", "numeric_threshold"),
                    )
                )

    for rows in by_member.values():
        counts: dict[int, int] = {}
        for row in rows:
            counts[row["player_id"]] = counts.get(row["player_id"], 0) + 1
        maximum = max(counts.values())
        for player_id, count in counts.items():
            if count != maximum:
                continue
            row = next(item for item in rows if item["player_id"] == player_id)
            facts.append(
                _make_fact(
                    fact_type="most_used_player",
                    category="player_usage",
                    summary=(
                        f"{row['display_name']}'s most-used player is {row['player_name']} "
                        f"with {count} selections."
                    ),
                    season_identifier=identifier,
                    contest=target,
                    subject=row,
                    related_player=row["player_name"],
                    values={"times_rostered": count},
                    tags=("player_usage", "most_used"),
                )
            )
        ownership_rows = [
            row for row in rows if row["draftkings_ownership_percentage"] is not None
        ]
        if ownership_rows:
            player_averages: dict[int, float] = {}
            for player_id in {row["player_id"] for row in ownership_rows}:
                values = [
                    row["draftkings_ownership_percentage"]
                    for row in ownership_rows
                    if row["player_id"] == player_id
                ]
                player_averages[player_id] = sum(values) / len(values)
            for fact_type, player_id, descriptor in [
                (
                    "member_lowest_average_ownership",
                    min(player_averages, key=lambda key: (player_averages[key], key)),
                    "lowest",
                ),
                (
                    "member_highest_average_ownership",
                    max(player_averages, key=lambda key: (player_averages[key], -key)),
                    "highest",
                ),
            ]:
                row = next(item for item in rows if item["player_id"] == player_id)
                facts.append(
                    _make_fact(
                        fact_type=fact_type,
                        category="ownership",
                        summary=(
                            f"{row['display_name']}'s {descriptor} average-ownership player "
                            f"selection is {row['player_name']} at "
                            f"{player_averages[player_id]:.2f}%."
                        ),
                        season_identifier=identifier,
                        contest=target,
                        subject=row,
                        related_player=row["player_name"],
                        values={"average_field_ownership": player_averages[player_id]},
                        tags=("ownership", "member_extreme"),
                    )
                )

    current_owned = [
        row
        for row in lineups
        if row["contest_id"] == target["id"]
        and row["draftkings_ownership_percentage"] is not None
    ]
    if current_owned:
        low = min(
            current_owned,
            key=lambda row: (
                row["draftkings_ownership_percentage"],
                row["display_name"],
                row["player_name"],
            ),
        )
        high = max(
            current_owned,
            key=lambda row: (
                row["draftkings_ownership_percentage"],
                row["display_name"],
                row["player_name"],
            ),
        )
        for fact_type, row, descriptor in [
            ("lowest_owned_weekly_selection", low, "lowest-owned"),
            ("highest_owned_weekly_selection", high, "highest-owned"),
        ]:
            facts.append(
                _make_fact(
                    fact_type=fact_type,
                    category="ownership",
                    summary=(
                        f"{row['display_name']}'s {row['player_name']} selection was the "
                        f"{descriptor} KCDK-rostered player in {target['week_label']} at "
                        f"{row['draftkings_ownership_percentage']:.2f}%."
                    ),
                    season_identifier=identifier,
                    contest=target,
                    subject=row,
                    related_player=row["player_name"],
                    values={
                        "field_ownership": row[
                            "draftkings_ownership_percentage"
                        ]
                    },
                    tags=("ownership", "weekly_extreme"),
                )
            )
    return facts


def _head_to_head_facts(
    season_identifier: str,
    target: dict[str, Any],
    results: list[dict[str, Any]],
    config: FactEngineConfig,
) -> list[Fact]:
    facts: list[Fact] = []
    by_member: dict[int, list[dict[str, Any]]] = {}
    for row in results:
        by_member.setdefault(row["member_id"], []).append(row)
    for left_id, right_id in combinations(sorted(by_member), 2):
        left_by_week = {row["contest_id"]: row for row in by_member[left_id]}
        right_by_week = {row["contest_id"]: row for row in by_member[right_id]}
        shared_ids = [
            row["contest_id"]
            for row in by_member[left_id]
            if row["contest_id"] in right_by_week
        ]
        if not shared_ids:
            continue
        comparisons: list[int] = []
        point_differences: list[float] = []
        finish_differences: list[float] = []
        money_differences: list[int] = []
        for contest_id in shared_ids:
            left = left_by_week[contest_id]
            right = right_by_week[contest_id]
            if left["kcdk_finish"] < right["kcdk_finish"]:
                comparisons.append(1)
            elif left["kcdk_finish"] > right["kcdk_finish"]:
                comparisons.append(-1)
            else:
                comparisons.append(0)
            point_differences.append(
                left["draftkings_fantasy_points"]
                - right["draftkings_fantasy_points"]
            )
            finish_differences.append(
                right["kcdk_finish"] - left["kcdk_finish"]
            )
            if left["prize_cents"] is not None and right["prize_cents"] is not None:
                money_differences.append(left["prize_cents"] - right["prize_cents"])
        left = left_by_week[shared_ids[-1]]
        right = right_by_week[shared_ids[-1]]
        left_ahead = comparisons.count(1)
        right_ahead = comparisons.count(-1)
        ties = comparisons.count(0)
        if left_ahead >= right_ahead:
            subject, related = left, right
            subject_ahead, related_ahead = left_ahead, right_ahead
            signed_point_difference = sum(point_differences) / len(point_differences)
            signed_finish_difference = sum(finish_differences) / len(
                finish_differences
            )
            signed_money_difference = (
                sum(money_differences) / 100 if money_differences else None
            )
        else:
            subject, related = right, left
            subject_ahead, related_ahead = right_ahead, left_ahead
            signed_point_difference = -sum(point_differences) / len(point_differences)
            signed_finish_difference = -sum(finish_differences) / len(
                finish_differences
            )
            signed_money_difference = (
                -sum(money_differences) / 100 if money_differences else None
            )
        facts.append(
            _make_fact(
                fact_type="head_to_head_record",
                category="head_to_head",
                summary=(
                    f"{subject['display_name']} finished ahead of {related['display_name']} "
                    f"in {subject_ahead} of {len(shared_ids)} shared weeks; "
                    f"{related['display_name']} led {related_ahead} times with {ties} ties."
                ),
                season_identifier=season_identifier,
                contest=target,
                subject=subject,
                related=related,
                values={
                    "shared_weeks": len(shared_ids),
                    "subject_finished_ahead": subject_ahead,
                    "related_finished_ahead": related_ahead,
                    "tied_finishes": ties,
                    "average_point_difference": signed_point_difference,
                    "average_finish_advantage": signed_finish_difference,
                    "total_money_difference_known_shared_weeks": signed_money_difference,
                    "closest_point_difference": min(abs(value) for value in point_differences),
                    "known_money_comparison_weeks": len(money_differences),
                },
                completeness=(
                    "complete"
                    if len(money_differences) == len(shared_ids)
                    else "partial"
                ),
                tags=("head_to_head", "record"),
            )
        )
        final = comparisons[-1]
        if final != 0:
            streak = 0
            for comparison in reversed(comparisons):
                if comparison != final:
                    break
                streak += 1
            if streak >= config.min_head_to_head_streak:
                streak_subject = left if final == 1 else right
                streak_related = right if final == 1 else left
                facts.append(
                    _make_fact(
                        fact_type="head_to_head_streak",
                        category="head_to_head",
                        summary=(
                            f"{streak_subject['display_name']} has finished ahead of "
                            f"{streak_related['display_name']} in {streak} consecutive "
                            f"shared weeks through {target['week_label']}."
                        ),
                        season_identifier=season_identifier,
                        contest=target,
                        subject=streak_subject,
                        related=streak_related,
                        values={"consecutive_shared_weeks": streak},
                        tags=("head_to_head", "streak"),
                        streak_length=streak,
                    )
                )
    return facts


def _record_facts(
    season_identifier: str,
    target: dict[str, Any],
    contests: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[Fact]:
    facts: list[Fact] = []
    current = [row for row in results if row["contest_id"] == target["id"]]
    prior = [row for row in results if row["contest_id"] != target["id"]]
    if not prior:
        return facts

    record_specs = [
        (
            "season_high_score_record",
            max,
            "draftkings_fantasy_points",
            "high DraftKings score",
            lambda current_value, prior_value: current_value >= prior_value,
        ),
        (
            "season_low_score_record",
            min,
            "draftkings_fantasy_points",
            "low DraftKings score",
            lambda current_value, prior_value: current_value <= prior_value,
        ),
        (
            "season_percentile_record",
            max,
            "tournament_percentile",
            "high tournament percentile",
            lambda current_value, prior_value: current_value >= prior_value,
        ),
    ]
    for fact_type, chooser, field_name, label, comparison in record_specs:
        current_row = chooser(current, key=lambda row: row[field_name])
        prior_value = chooser(row[field_name] for row in prior)
        current_value = current_row[field_name]
        if comparison(current_value, prior_value):
            status = "tied" if current_value == prior_value else "set"
            facts.append(
                _make_fact(
                    fact_type=fact_type,
                    category="record",
                    summary=(
                        f"{current_row['display_name']} {status} the season {label} "
                        f"at {current_value:.2f} in {target['week_label']}."
                    ),
                    season_identifier=season_identifier,
                    contest=target,
                    subject=current_row,
                    values={
                        "record_value": current_value,
                        "prior_record": prior_value,
                        "record_status": status,
                    },
                    tags=("record", "season", "new" if status == "set" else "tied"),
                    record=True,
                )
            )

    def margins_for(contest_id: int) -> tuple[float, float]:
        rows = sorted(
            [row for row in results if row["contest_id"] == contest_id],
            key=lambda row: -row["draftkings_fantasy_points"],
        )
        gaps = [
            left["draftkings_fantasy_points"] - right["draftkings_fantasy_points"]
            for left, right in zip(rows, rows[1:])
        ]
        return gaps[0], min(gaps)

    current_victory, current_closest = margins_for(target["id"])
    prior_margins = [margins_for(contest["id"]) for contest in contests[:-1]]
    for fact_type, current_value, prior_value, label, comparison in [
        (
            "season_victory_margin_record",
            current_victory,
            max(value[0] for value in prior_margins),
            "largest KCDK victory margin",
            current_victory.__ge__,
        ),
        (
            "season_closest_margin_record",
            current_closest,
            min(value[1] for value in prior_margins),
            "closest adjacent KCDK margin",
            current_closest.__le__,
        ),
    ]:
        if comparison(prior_value):
            status = "tied" if current_value == prior_value else "set"
            facts.append(
                _make_fact(
                    fact_type=fact_type,
                    category="record",
                    summary=(
                        f"{target['week_label']} {status} the season {label} at "
                        f"{current_value:.2f} points."
                    ),
                    season_identifier=season_identifier,
                    contest=target,
                    values={
                        "record_value": current_value,
                        "prior_record": prior_value,
                        "record_status": status,
                    },
                    tags=("record", "season", "margin"),
                    record=True,
                )
            )

    known_current_cash = [row for row in current if row["prize_cents"] is not None]
    known_prior_cash = [row for row in prior if row["prize_cents"] is not None]
    if known_current_cash and known_prior_cash:
        current_cash = max(known_current_cash, key=lambda row: row["prize_cents"])
        prior_cash = max(row["prize_cents"] for row in known_prior_cash)
        if current_cash["prize_cents"] >= prior_cash:
            status = "tied" if current_cash["prize_cents"] == prior_cash else "set"
            facts.append(
                _make_fact(
                    fact_type="season_cash_record",
                    category="record",
                    summary=(
                        f"{current_cash['display_name']} {status} the season single-cash "
                        f"record at {_money(current_cash['prize_cents'])} in "
                        f"{target['week_label']}."
                    ),
                    season_identifier=season_identifier,
                    contest=target,
                    subject=current_cash,
                    values={
                        "record_money_won": current_cash["prize_cents"] / 100,
                        "prior_record_money_won": prior_cash / 100,
                        "record_status": status,
                    },
                    tags=("record", "season", "money"),
                    record=True,
                )
            )
    return facts


def generate_weekly_facts(
    connection: sqlite3.Connection,
    season_reference: str | int,
    contest_id: int | None = None,
    *,
    config: FactEngineConfig | None = None,
) -> list[Fact]:
    """Generate deterministic factual candidates through a target contest."""
    config = config or FactEngineConfig()
    season, target, contests = _season_context(connection, season_reference, contest_id)
    results = _load_results(connection, contests)
    lineups = _load_lineups(connection, contests)
    identifier = season["identifier"]
    facts: list[Fact] = []
    facts.extend(_weekly_result_facts(identifier, target, results, config))
    facts.extend(
        _streak_facts(identifier, target, contests, results, lineups, config)
    )
    facts.extend(
        _season_and_money_facts(connection, season, target, results, config)
    )
    facts.extend(
        _player_and_ownership_facts(
            connection, season, target, contests, lineups, config
        )
    )
    facts.extend(_head_to_head_facts(identifier, target, results, config))
    facts.extend(_record_facts(identifier, target, contests, results))
    return sorted(facts, key=_fact_sort_key)


def _fact_sort_key(fact: Fact) -> tuple[Any, ...]:
    return (
        -fact.priority,
        fact.category,
        fact.fact_type,
        fact.subject_member_name or "",
        fact.related_member_name or "",
        fact.related_player or "",
        fact.week_label or "",
        fact.summary,
    )


def _dedupe_key(fact: Fact) -> tuple[Any, ...]:
    return (
        fact.fact_type,
        fact.subject_member_id,
        fact.related_member_id,
        fact.related_player,
        fact.week_label,
        fact.summary,
    )


def select_facts(
    facts: Iterable[Fact],
    *,
    max_count: int = 12,
    config: FactEngineConfig | None = None,
) -> list[Fact]:
    """Select a deterministic, category-diverse, member-balanced shortlist."""
    if max_count < 0:
        raise ValueError("max_count cannot be negative")
    if max_count == 0:
        return []
    config = config or FactEngineConfig()
    unique: dict[tuple[Any, ...], Fact] = {}
    for fact in facts:
        key = _dedupe_key(fact)
        existing = unique.get(key)
        if existing is None or _fact_sort_key(fact) < _fact_sort_key(existing):
            unique[key] = fact
    ordered = sorted(unique.values(), key=_fact_sort_key)
    selected: list[Fact] = []
    selected_keys: set[tuple[Any, ...]] = set()
    member_counts: dict[int, int] = {}
    categories: set[str] = set()

    def can_add(fact: Fact, *, enforce_member_limit: bool) -> bool:
        if _dedupe_key(fact) in selected_keys:
            return False
        if not enforce_member_limit or fact.subject_member_id is None:
            return True
        return member_counts.get(fact.subject_member_id, 0) < config.max_facts_per_member

    def add(fact: Fact) -> None:
        selected.append(fact)
        selected_keys.add(_dedupe_key(fact))
        categories.add(fact.category)
        if fact.subject_member_id is not None:
            member_counts[fact.subject_member_id] = (
                member_counts.get(fact.subject_member_id, 0) + 1
            )

    for fact in ordered:
        if len(selected) >= max_count:
            break
        if fact.category not in categories and can_add(fact, enforce_member_limit=True):
            add(fact)
    for fact in ordered:
        if len(selected) >= max_count:
            break
        if can_add(fact, enforce_member_limit=True):
            add(fact)
    for fact in ordered:
        if len(selected) >= max_count:
            break
        if can_add(fact, enforce_member_limit=False):
            add(fact)
    return sorted(selected, key=_fact_sort_key)


def _completeness_warnings(
    connection: sqlite3.Connection,
    season: dict[str, Any],
    target: dict[str, Any],
    contests: list[dict[str, Any]],
    results: list[dict[str, Any]],
    lineups: list[dict[str, Any]],
) -> list[str]:
    warnings: list[str] = []
    known_prizes = sum(row["prize_cents"] is not None for row in results)
    if known_prizes < len(results):
        warnings.append(
            f"Prize data is incomplete: {known_prizes} of {len(results)} "
            "member-results have known prize values through this contest."
        )
    current_results = [row for row in results if row["contest_id"] == target["id"]]
    current_lineups = [row for row in lineups if row["contest_id"] == target["id"]]
    members_with_lineups = {row["member_id"] for row in current_lineups}
    if len(members_with_lineups) < len(current_results):
        warnings.append(
            f"Player lineup data is incomplete: {len(members_with_lineups)} of "
            f"{len(current_results)} member-results have parsed players in "
            f"{target['week_label']}."
        )
    known_ownership = sum(
        row["draftkings_ownership_percentage"] is not None for row in current_lineups
    )
    if known_ownership < len(current_lineups):
        warnings.append(
            f"Ownership data is incomplete: {known_ownership} of "
            f"{len(current_lineups)} lineup-player rows have ownership values in "
            f"{target['week_label']}."
        )
    if not current_lineups:
        warnings.append(
            f"No player-level lineup rows are available for {target['week_label']}."
        )
    return warnings


def build_weekly_fact_report(
    connection: sqlite3.Connection,
    season_reference: str | int,
    contest_id: int | None = None,
    *,
    max_facts: int | None = None,
    config: FactEngineConfig | None = None,
) -> WeeklyFactReport:
    """Generate, balance, and package facts for one week as a future LLM payload."""
    config = config or FactEngineConfig()
    selection_limit = (
        config.default_max_facts if max_facts is None else max_facts
    )
    season, target, contests = _season_context(connection, season_reference, contest_id)
    results = _load_results(connection, contests)
    lineups = _load_lineups(connection, contests)
    candidates = generate_weekly_facts(
        connection,
        season["id"],
        target["id"],
        config=config,
    )
    selected = select_facts(
        candidates, max_count=selection_limit, config=config
    )
    warnings = _completeness_warnings(
        connection, season, target, contests, results, lineups
    )
    return WeeklyFactReport(
        season_id=int(season["id"]),
        season_identifier=str(season["identifier"]),
        season_name=str(season["name"]),
        contest_id=int(target["id"]),
        week_label=str(target["week_label"]),
        generated_candidate_count=len(candidates),
        candidate_facts=tuple(candidates),
        selected_facts=tuple(selected),
        warnings=tuple(warnings),
    )
