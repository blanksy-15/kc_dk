"""Deterministic scarcity rules for an optional second commentary persona."""

from dataclasses import dataclass, fields
import math
import os
from pathlib import Path

from dotenv import dotenv_values

from .commentary import fact_identifier
from .facts import Fact, WeeklyFactReport


@dataclass(frozen=True)
class DrunkBotConfig:
    enabled: bool = True
    minimum_fact_priority: int = 90
    minimum_streak_length: int = 3
    maximum_interjections_per_week: int = 2
    maximum_members_targeted: int = 2
    minimum_number_of_eligible_facts: int = 1
    allow_profanity: bool = True
    default_tone: str = "ruthless"
    large_margin_points: float = 40.0
    close_margin_points: float = 0.5
    poor_average_finish: float = 5.0

    def __post_init__(self) -> None:
        for name in ("enabled", "allow_profanity"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"Drunk Bot {name} must be true or false.")
        limits = {
            "minimum_fact_priority": (0, 100), "minimum_streak_length": (2, 100),
            "maximum_interjections_per_week": (1, 2), "maximum_members_targeted": (1, 3),
            "minimum_number_of_eligible_facts": (1, 3),
        }
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Drunk Bot {name} must be between {low} and {high}.")
        if self.default_tone not in {"normal", "ruthless"}:
            raise ValueError("Drunk Bot default_tone must be normal or ruthless.")
        for name in ("large_margin_points", "close_margin_points", "poor_average_finish"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Drunk Bot {name} must be finite and positive.")

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "DrunkBotConfig":
        values = dotenv_values(env_file or Path.cwd() / ".env")
        defaults = cls()
        settings = {}
        for item in fields(cls):
            name = "DRUNK_BOT_" + item.name.upper()
            raw = os.getenv(name, values.get(name))
            if raw is None or not raw.strip():
                continue
            baseline = getattr(defaults, item.name)
            try:
                if type(baseline) is bool:
                    if raw.strip().lower() not in {"true", "false", "1", "0"}:
                        raise ValueError
                    settings[item.name] = raw.strip().lower() in {"true", "1"}
                else:
                    settings[item.name] = type(baseline)(raw.strip())
            except ValueError:
                raise ValueError(f"Invalid {name} setting.") from None
        return cls(**settings)


@dataclass(frozen=True)
class DrunkBotEligibility:
    enabled: bool
    eligible: bool
    score: int
    reason: str
    selected_facts: tuple[Fact, ...] = ()
    subjects: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled, "eligible": self.eligible,
            "score": self.score, "reason": self.reason,
            "selected_fact_ids": [fact_identifier(fact) for fact in self.selected_facts],
            "subjects": list(self.subjects), "triggering_facts": len(self.selected_facts),
        }


def roast_subject(fact: Fact) -> str | None:
    # Head-to-head facts describe the winner first; the losing friend is the target.
    if fact.fact_type in {"head_to_head_streak", "margin_of_victory", "biggest_weekly_gap", "closest_weekly_finish"}:
        return fact.related_member_name
    return fact.subject_member_name


def _qualifies(fact: Fact, config: DrunkBotConfig) -> bool:
    if fact.priority < config.minimum_fact_priority or not roast_subject(fact):
        return False
    values = fact.values
    kind = fact.fact_type
    if kind in {"consecutive_last_places", "consecutive_known_zeroes", "consecutive_decline"}:
        return values.get("consecutive_weeks", 0) >= config.minimum_streak_length
    if kind == "head_to_head_streak":
        return values.get("consecutive_shared_weeks", 0) >= config.minimum_streak_length
    if kind == "season_low_score_record":
        return values.get("record_status") == "set"
    if kind == "known_zero_season_winnings":
        return (values.get("total_money_won") == 0
                and values.get("weeks_with_prize_data", 0) >= config.minimum_streak_length)
    if kind in {"margin_of_victory", "biggest_weekly_gap"}:
        return values.get("point_margin", values.get("point_gap", 0)) >= config.large_margin_points
    if kind == "closest_weekly_finish":
        gap = values.get("point_gap")
        return gap is not None and 0 < gap <= config.close_margin_points
    if kind == "score_below_personal_average":
        return values.get("difference", 0) <= -config.large_margin_points
    if kind == "player_result_history":
        return (values.get("times_rostered", 0) >= config.minimum_streak_length
                and values.get("average_kcdk_finish", 0) >= config.poor_average_finish
                and values.get("podiums") == 0)
    # High priority alone is insufficient: positive records and ordinary wins
    # should never manufacture a reason for this persona to appear.
    return False


def select_drunk_bot_facts(
    report: WeeklyFactReport, config: DrunkBotConfig | None = None,
) -> DrunkBotEligibility:
    config = config or DrunkBotConfig()
    if not config.enabled:
        return DrunkBotEligibility(False, False, 0, "disabled")
    candidates = {
        fact_identifier(fact): fact for fact in report.candidate_facts
        if _qualifies(fact, config)
    }
    ordered = sorted(candidates.values(), key=lambda fact: (
        -fact.priority, roast_subject(fact) or "", fact.fact_type, fact_identifier(fact),
    ))
    selected = []
    subjects = []
    # One strongest fact per target first, with stable tie breaks and no randomness.
    for fact in ordered:
        subject = roast_subject(fact)
        if subject not in subjects and len(subjects) < config.maximum_members_targeted:
            selected.append(fact)
            subjects.append(subject)
        if len(selected) == 3:
            break
    # A second distinct fact for the same target is justified only when a minimum
    # fact count was explicitly configured, and remains within the three-fact cap.
    for fact in ordered:
        if len(selected) >= config.minimum_number_of_eligible_facts:
            break
        if fact not in selected and roast_subject(fact) in subjects:
            selected.append(fact)
    score = max((fact.priority for fact in ordered), default=0)
    if len(selected) < config.minimum_number_of_eligible_facts:
        return DrunkBotEligibility(True, False, score, "not enough qualifying facts")
    return DrunkBotEligibility(
        True, True, score, selected[0].summary, tuple(selected), tuple(subjects),
    )
