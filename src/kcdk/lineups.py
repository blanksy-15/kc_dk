"""Tolerant, isolated parsing of player selections from DraftKings lineups."""

from __future__ import annotations

from dataclasses import dataclass
import re

import pandas as pd


_POSITION = r"D/ST|FLEX|UTIL|CPT|DST|QB|RB|WR|TE|K|G|F|C"
_SEPARATED_PLAYER = re.compile(
    rf"^\s*(?P<position>{_POSITION})\s*[:\-]?\s+(?P<player>.+?)\s*$",
    re.IGNORECASE,
)
_INLINE_PLAYER = re.compile(
    rf"(?:^|\s)(?P<position>{_POSITION})\s+(?P<player>.+?)"
    rf"(?=\s+(?:{_POSITION})\s+|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedLineupPlayer:
    player_name: str
    roster_position: str
    fantasy_points: float | None = None
    ownership_percentage: float | None = None


def player_key(player_name: str) -> str:
    """Return a stable comparison key while preserving display spelling elsewhere."""
    return re.sub(r"\s+", " ", player_name.strip()).casefold()


def parse_lineup_text(lineup: object) -> list[tuple[str, str]]:
    """Parse common delimited or inline DraftKings lineup strings.

    Unknown formats deliberately return no players: inability to interpret a
    lineup must never block standings import.
    """
    if lineup is None or pd.isna(lineup):
        return []
    text = str(lineup).strip()
    if not text:
        return []

    if re.search(r"[|;/]", text):
        parsed: list[tuple[str, str]] = []
        for segment in re.split(r"\s*[|;/]\s*", text):
            match = _SEPARATED_PLAYER.match(segment)
            if not match:
                return []
            name = re.sub(r"\s+", " ", match.group("player").strip())
            if not name:
                return []
            parsed.append((match.group("position").upper(), name))
        return parsed

    matches = list(_INLINE_PLAYER.finditer(text))
    if not matches:
        return []
    return [
        (
            match.group("position").upper(),
            re.sub(r"\s+", " ", match.group("player").strip()),
        )
        for match in matches
    ]


def _optional_float(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def player_metadata(contest: pd.DataFrame) -> dict[str, tuple[float | None, float | None]]:
    """Index the optional side-by-side player table by normalized player name."""
    if "player" not in contest.columns:
        return {}
    metadata: dict[str, tuple[float | None, float | None]] = {}
    for row in contest.to_dict("records"):
        name = row.get("player")
        if name is None or pd.isna(name) or not str(name).strip():
            continue
        metadata[player_key(str(name))] = (
            _optional_float(row.get("fpts")),
            _optional_float(row.get("drafted_pct")),
        )
    return metadata


def parse_member_lineup(
    lineup: object,
    metadata: dict[str, tuple[float | None, float | None]] | None = None,
) -> list[ParsedLineupPlayer]:
    """Parse a member lineup and enrich selections from optional field metadata."""
    metadata = metadata or {}
    players: list[ParsedLineupPlayer] = []
    seen: set[str] = set()
    for position, name in parse_lineup_text(lineup):
        key = player_key(name)
        if key in seen:
            continue
        seen.add(key)
        fantasy_points, ownership = metadata.get(key, (None, None))
        players.append(
            ParsedLineupPlayer(name, position, fantasy_points, ownership)
        )
    return players
