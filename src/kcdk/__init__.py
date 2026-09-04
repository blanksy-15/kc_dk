"""KCDK weekly DraftKings contest analysis package."""

from .dk_import import (
    REQUIRED_FIELDS,
    import_contest,
    match_kcdk_members,
    percentile_from_rank,
    weekly_standings,
)
from .analytics import (
    consecutive_player_use,
    group_player_usage,
    member_player_usage,
    season_leaderboard,
    tournament_performance_leaderboard,
    unanimous_weekly_selections,
    unique_weekly_selections,
)
from .members import load_members
from .persistence import connect_database, import_week, weekly_results
from .facts import (
    Fact,
    FactEngineConfig,
    WeeklyFactReport,
    build_weekly_fact_report,
    generate_weekly_facts,
    select_facts,
)

__all__ = [
    "REQUIRED_FIELDS",
    "import_contest",
    "load_members",
    "match_kcdk_members",
    "percentile_from_rank",
    "weekly_standings",
    "connect_database",
    "import_week",
    "weekly_results",
    "season_leaderboard",
    "tournament_performance_leaderboard",
    "member_player_usage",
    "group_player_usage",
    "consecutive_player_use",
    "unanimous_weekly_selections",
    "unique_weekly_selections",
    "Fact",
    "FactEngineConfig",
    "WeeklyFactReport",
    "build_weekly_fact_report",
    "generate_weekly_facts",
    "select_facts",
]
