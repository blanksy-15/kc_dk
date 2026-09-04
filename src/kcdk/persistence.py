"""SQLite persistence and the high-level weekly contest import workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import re
import sqlite3

import pandas as pd

from .dk_import import contest_result_rows, import_contest, weekly_standings
from .lineups import parse_member_lineup, player_key, player_metadata
from .members import load_members


SCHEMA_VERSION = 1
SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    identifier TEXT NOT NULL UNIQUE,
    year INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    member_key TEXT NOT NULL UNIQUE,
    draftkings_name TEXT NOT NULL,
    match_name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS season_members (
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    PRIMARY KEY (season_id, member_id)
);

CREATE TABLE IF NOT EXISTS contests (
    id INTEGER PRIMARY KEY,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    week_number INTEGER,
    week_label TEXT NOT NULL,
    draftkings_contest_id TEXT,
    contest_name TEXT,
    contest_date TEXT,
    total_tournament_entries INTEGER NOT NULL CHECK (total_tournament_entries > 0),
    source_filename TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (season_id, week_label),
    UNIQUE (season_id, source_fingerprint)
);

CREATE TABLE IF NOT EXISTS member_results (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id),
    kcdk_finish INTEGER NOT NULL CHECK (kcdk_finish > 0),
    draftkings_overall_rank INTEGER,
    draftkings_fantasy_points REAL NOT NULL,
    tournament_percentile REAL,
    entry_id TEXT,
    entry_name TEXT NOT NULL,
    UNIQUE (contest_id, member_id)
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    player_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lineup_players (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id),
    player_id INTEGER NOT NULL REFERENCES players(id),
    roster_position TEXT NOT NULL,
    player_fantasy_points REAL,
    draftkings_ownership_percentage REAL,
    UNIQUE (contest_id, member_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_results_season_contest ON member_results(contest_id);
CREATE INDEX IF NOT EXISTS idx_lineup_member_player ON lineup_players(member_id, player_id);
CREATE INDEX IF NOT EXISTS idx_lineup_contest_player ON lineup_players(contest_id, player_id);
"""


@dataclass(frozen=True)
class ImportSummary:
    season: str
    week: str
    source_file: str
    total_tournament_entries: int
    matched_kcdk_members: int
    member_result_rows_stored: int
    lineup_player_rows_stored: int
    warnings: tuple[str, ...]
    already_imported: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect_database(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a SQLite database and ensure the current schema exists."""
    database_target = str(path)
    if database_target != ":memory:":
        Path(database_target).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_target)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    initialize_database(connection)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create normalized season-history tables idempotently."""
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT OR IGNORE INTO schema_versions(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, _utc_now()),
    )
    connection.commit()


def _normalized_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _member_key(row: pd.Series) -> str:
    configured = str(row.get("member_key", "")).strip()
    if configured:
        return configured
    return "dk:" + _normalized_name(str(row["draftkings_name"]))


def _ensure_season(
    connection: sqlite3.Connection,
    *,
    name: str,
    identifier: str,
    year: int | None,
) -> int:
    connection.execute(
        """
        INSERT INTO seasons(name, identifier, year, active, created_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(identifier) DO UPDATE SET
            name=excluded.name,
            year=COALESCE(excluded.year, seasons.year)
        """,
        (name, identifier, year, _utc_now()),
    )
    return int(
        connection.execute(
            "SELECT id FROM seasons WHERE identifier = ?", (identifier,)
        ).fetchone()["id"]
    )


def _sync_members(
    connection: sqlite3.Connection, season_id: int, members: pd.DataFrame
) -> dict[str, int]:
    now = _utc_now()
    member_ids: dict[str, int] = {}
    for _, row in members.iterrows():
        key = _member_key(row)
        match_name = _normalized_name(str(row["draftkings_name"]))
        connection.execute(
            """
            INSERT INTO members(
                member_key, draftkings_name, match_name, display_name, nickname,
                active, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(member_key) DO UPDATE SET
                draftkings_name=excluded.draftkings_name,
                match_name=excluded.match_name,
                display_name=excluded.display_name,
                nickname=excluded.nickname,
                active=excluded.active,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (
                key,
                str(row["draftkings_name"]).strip(),
                match_name,
                str(row["display_name"]).strip(),
                str(row.get("nickname", "")).strip(),
                int(bool(row["active"])),
                str(row.get("notes", "")).strip(),
                now,
                now,
            ),
        )
        member_id = int(
            connection.execute(
                "SELECT id FROM members WHERE member_key = ?", (key,)
            ).fetchone()["id"]
        )
        member_ids[match_name] = member_id
        connection.execute(
            """
            INSERT INTO season_members(season_id, member_id, active)
            VALUES (?, ?, ?)
            ON CONFLICT(season_id, member_id) DO UPDATE SET active=excluded.active
            """,
            (season_id, member_id, int(bool(row["active"]))),
        )
    return member_ids


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_summary(
    connection: sqlite3.Connection, contest: sqlite3.Row, season_name: str
) -> ImportSummary:
    contest_id = int(contest["id"])
    result_count = connection.execute(
        "SELECT COUNT(*) AS count FROM member_results WHERE contest_id = ?", (contest_id,)
    ).fetchone()["count"]
    return ImportSummary(
        season=season_name,
        week=str(contest["week_label"]),
        source_file=str(contest["source_filename"]),
        total_tournament_entries=int(contest["total_tournament_entries"]),
        matched_kcdk_members=int(result_count),
        member_result_rows_stored=0,
        lineup_player_rows_stored=0,
        warnings=("This exact source was already imported; no rows were changed.",),
        already_imported=True,
    )


def import_week(
    connection: sqlite3.Connection,
    csv_path: str | Path,
    members_path: str | Path,
    *,
    season_name: str,
    season_identifier: str,
    week_number: int | None = None,
    week_label: str | None = None,
    season_year: int | None = None,
    draftkings_contest_id: str | None = None,
    contest_name: str | None = None,
    contest_date: str | None = None,
    replace: bool = False,
) -> ImportSummary:
    """Normalize, match, rank, parse, and atomically persist one contest week.

    An identical file is an idempotent no-op.  A changed file for an existing
    week raises unless ``replace=True`` is explicitly supplied.
    """
    source_path = Path(csv_path)
    label = week_label or (f"Week {week_number}" if week_number is not None else None)
    if not label:
        raise ValueError("week_number or week_label is required")

    contest = import_contest(source_path)
    results = contest_result_rows(contest)
    members = load_members(Path(members_path), active_only=False)
    active_members = members.loc[members["active"]].copy()
    standings = weekly_standings(contest, active_members)
    fingerprint = _fingerprint(source_path)
    warnings: list[str] = []

    if standings["display_name"].duplicated().any():
        duplicates = standings.loc[
            standings["display_name"].duplicated(keep=False), "display_name"
        ].tolist()
        raise ValueError(f"Multiple entries matched the same KCDK member: {duplicates}")

    with connection:
        known_season = connection.execute(
            "SELECT id FROM seasons WHERE identifier = ?", (season_identifier,)
        ).fetchone()
        if known_season:
            existing = connection.execute(
                """
                SELECT * FROM contests
                WHERE season_id = ? AND (week_label = ? OR source_fingerprint = ?)
                """,
                (known_season["id"], label, fingerprint),
            ).fetchone()
            if existing and existing["source_fingerprint"] == fingerprint:
                return _existing_summary(connection, existing, season_name)
        season_id = _ensure_season(
            connection,
            name=season_name,
            identifier=season_identifier,
            year=season_year,
        )
        member_ids = _sync_members(connection, season_id, members)
        existing = connection.execute(
            """
            SELECT * FROM contests
            WHERE season_id = ? AND (week_label = ? OR source_fingerprint = ?)
            """,
            (season_id, label, fingerprint),
        ).fetchone()
        if existing and not replace:
            raise ValueError(
                f"{label} already exists with a different source. "
                "Pass replace=True for an explicit replacement."
            )
        if existing:
            connection.execute("DELETE FROM contests WHERE id = ?", (existing["id"],))

        cursor = connection.execute(
            """
            INSERT INTO contests(
                season_id, week_number, week_label, draftkings_contest_id,
                contest_name, contest_date, total_tournament_entries,
                source_filename, source_fingerprint, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                season_id,
                week_number,
                label,
                draftkings_contest_id,
                contest_name,
                contest_date,
                len(results),
                source_path.name,
                fingerprint,
                _utc_now(),
            ),
        )
        contest_id = int(cursor.lastrowid)

        result_by_entry = {
            _normalized_name(str(row["entry_name"])): row
            for _, row in results.iterrows()
        }
        metadata = player_metadata(contest)
        lineup_count = 0
        unparsed_members: list[str] = []
        for standing in standings.to_dict("records"):
            match_name = _normalized_name(str(standing["draftkings_entry_name"]))
            source_row = result_by_entry[match_name]
            member_id = member_ids[match_name]
            entry_id = source_row.get("entry_id")
            connection.execute(
                """
                INSERT INTO member_results(
                    contest_id, member_id, kcdk_finish, draftkings_overall_rank,
                    draftkings_fantasy_points, tournament_percentile,
                    entry_id, entry_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contest_id,
                    member_id,
                    int(standing["kcdk_rank"]),
                    None
                    if pd.isna(standing["overall_rank"])
                    else int(standing["overall_rank"]),
                    float(standing["points"]),
                    None
                    if pd.isna(standing["overall_percentile"])
                    else float(standing["overall_percentile"]),
                    None if pd.isna(entry_id) else str(int(entry_id)),
                    str(standing["draftkings_entry_name"]),
                ),
            )
            parsed = parse_member_lineup(source_row.get("lineup"), metadata)
            if not parsed:
                unparsed_members.append(str(standing["display_name"]))
            for selection in parsed:
                key = player_key(selection.player_name)
                connection.execute(
                    """
                    INSERT INTO players(player_key, display_name) VALUES (?, ?)
                    ON CONFLICT(player_key) DO UPDATE SET display_name=excluded.display_name
                    """,
                    (key, selection.player_name),
                )
                player_id = int(
                    connection.execute(
                        "SELECT id FROM players WHERE player_key = ?", (key,)
                    ).fetchone()["id"]
                )
                connection.execute(
                    """
                    INSERT INTO lineup_players(
                        contest_id, member_id, player_id, roster_position,
                        player_fantasy_points, draftkings_ownership_percentage
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contest_id,
                        member_id,
                        player_id,
                        selection.roster_position,
                        selection.fantasy_points,
                        selection.ownership_percentage,
                    ),
                )
                lineup_count += 1

        if unparsed_members:
            warnings.append(
                "No parseable lineup players for: " + ", ".join(sorted(unparsed_members))
            )
        if lineup_count == 0:
            warnings.append("No lineup-player records were imported; standings were saved.")
        if standings.empty:
            warnings.append("No active KCDK members matched this contest.")

    return ImportSummary(
        season=season_name,
        week=label,
        source_file=source_path.name,
        total_tournament_entries=len(results),
        matched_kcdk_members=len(standings),
        member_result_rows_stored=len(standings),
        lineup_player_rows_stored=lineup_count,
        warnings=tuple(warnings),
    )


def weekly_results(
    connection: sqlite3.Connection, season_identifier: str, week_label: str | None = None
) -> pd.DataFrame:
    """Return persisted weekly standings for a season."""
    parameters: list[object] = [season_identifier]
    week_clause = ""
    if week_label is not None:
        week_clause = "AND c.week_label = ?"
        parameters.append(week_label)
    query = f"""
        SELECT c.week_number, c.week_label, m.display_name, r.kcdk_finish,
               r.draftkings_overall_rank, r.draftkings_fantasy_points,
               r.tournament_percentile, r.entry_id, r.entry_name
        FROM member_results r
        JOIN contests c ON c.id = r.contest_id
        JOIN seasons s ON s.id = c.season_id
        JOIN members m ON m.id = r.member_id
        WHERE s.identifier = ? {week_clause}
        ORDER BY COALESCE(c.week_number, 2147483647), c.week_label,
                 r.kcdk_finish, r.draftkings_overall_rank, m.display_name
    """
    return pd.read_sql_query(query, connection, params=parameters)
