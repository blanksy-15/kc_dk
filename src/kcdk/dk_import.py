"""DraftKings CSV normalization and KCDK contest calculations."""

from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Iterable

import pandas as pd

REQUIRED_FIELDS = ("rank", "entry_id", "entry_name", "points")
_FIELD_ALIASES = {
    "rank": {"rank", "place", "contest_rank"},
    "entry_id": {"entryid", "entry_id", "entry_number", "entrynumber"},
    "entry_name": {"entryname", "entry_name", "username", "user_name"},
    "time_remaining": {"timeremaining", "time_remaining"},
    "points": {"points", "fantasy_points", "score"},
    "lineup": {"lineup", "roster"},
    "player": {"player", "player_name"},
    "roster_position": {"rosterposition", "roster_position", "position"},
    "drafted_pct": {"drafted", "drafted_pct", "percent_drafted", "drafted_percentage"},
    "fpts": {"fpts", "player_points", "fantasy_points_player"},
    "prize": {"prize", "winnings", "prize_amount", "amount_won"},
}


def _header_key(header: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(header).strip().lower()).strip("_")


def _parse_numeric(values: pd.Series, *, percentage: bool = False) -> pd.Series:
    cleaned = values.astype("string").str.strip().str.replace(",", "", regex=False)
    if percentage:
        cleaned = cleaned.str.replace("%", "", regex=False)
    return pd.to_numeric(cleaned.replace({"": pd.NA, "-": pd.NA}), errors="coerce")


def parse_money_to_cents(value: object) -> int | None:
    """Parse an optional currency value into exact integer cents.

    Missing or unrecognized values remain unknown rather than becoming zero.
    """
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if text.casefold() in {"", "-", "--", "n/a", "na", "null", "none"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if negative:
        amount = -amount
    if amount < 0:
        return None
    cents = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def normalize_headers(headers: Iterable[object]) -> dict[str, str]:
    """Return source-header to internal-name mappings for known headers."""
    aliases = {alias: name for name, values in _FIELD_ALIASES.items() for alias in values}
    mapping: dict[str, str] = {}
    for header in headers:
        key = _header_key(header)
        if key in aliases:
            mapping[str(header)] = aliases[key]
    return mapping


def normalize_contest(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a DraftKings-shaped DataFrame without changing the source file."""
    mapping = normalize_headers(data.columns)
    missing = [field for field in REQUIRED_FIELDS if field not in mapping.values()]
    if missing:
        found = list(data.columns)
        raise ValueError(
            f"Missing required DraftKings fields: {missing}. Headers found: {found}"
        )

    normalized = data.rename(columns=mapping).copy()
    normalized = normalized.loc[:, ~normalized.columns.duplicated()]
    normalized["rank"] = _parse_numeric(normalized["rank"]).astype("Int64")
    normalized["entry_id"] = _parse_numeric(normalized["entry_id"]).astype("Int64")
    normalized["points"] = _parse_numeric(normalized["points"])
    if "drafted_pct" in normalized:
        normalized["drafted_pct"] = _parse_numeric(normalized["drafted_pct"], percentage=True)
    if "fpts" in normalized:
        normalized["fpts"] = _parse_numeric(normalized["fpts"])
    if "prize" in normalized:
        normalized["prize_cents"] = normalized["prize"].map(
            parse_money_to_cents
        ).astype("Int64")
    return normalized


def contest_result_rows(contest: pd.DataFrame) -> pd.DataFrame:
    """Return one standings row per DraftKings entry.

    Some DraftKings exports repeat entrant values alongside a player ownership
    table.  Keeping this collapse separate lets lineup parsing evolve without
    changing the season persistence model.
    """
    return contest.drop_duplicates(subset=["entry_id"], keep="first").copy()


def import_contest(path: Path) -> pd.DataFrame:
    """Read and normalize one DraftKings contest CSV."""
    return normalize_contest(pd.read_csv(path))


def percentile_from_rank(rank: float, total_entries: int) -> float:
    """Return a 0-100 percentile where rank 1 is the best result."""
    if total_entries < 1:
        raise ValueError("total_entries must be at least 1")
    if pd.isna(rank):
        return float("nan")
    if total_entries == 1:
        return 100.0
    return (total_entries - float(rank)) / (total_entries - 1) * 100.0


def match_kcdk_members(contest: pd.DataFrame, members: pd.DataFrame) -> pd.DataFrame:
    """Return contest rows whose entry names match active configured members."""
    active_members = members.loc[members["active"]].copy()
    lookup = active_members.assign(
        _match_name=active_members["draftkings_name"].str.strip().str.casefold()
    )
    results = contest_result_rows(contest)
    matched = results.assign(
        _match_name=results["entry_name"].astype("string").str.strip().str.casefold()
    )
    return matched.merge(
        lookup[["_match_name", "display_name", "nickname", "notes"]],
        on="_match_name",
        how="inner",
    ).drop(columns="_match_name")


def weekly_standings(contest: pd.DataFrame, members: pd.DataFrame) -> pd.DataFrame:
    """Build KCDK-only weekly standings from contest rank and fantasy points."""
    matched = match_kcdk_members(contest, members).copy()
    total_entries = len(contest_result_rows(contest))
    matched["overall_percentile"] = matched["rank"].map(
        lambda rank: percentile_from_rank(rank, total_entries)
    )
    matched = matched.sort_values(
        ["points", "rank", "entry_name"], ascending=[False, True, True], kind="stable"
    )
    matched["kcdk_rank"] = matched["points"].rank(method="min", ascending=False).astype("Int64")
    standings = matched.rename(
        columns={"rank": "overall_rank", "entry_name": "draftkings_entry_name"}
    )
    return standings[
        [
            "kcdk_rank",
            "display_name",
            "draftkings_entry_name",
            "overall_rank",
            "points",
            "overall_percentile",
        ]
    ].reset_index(drop=True)
