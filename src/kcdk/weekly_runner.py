"""Guarded, interactive Windows workflow for publishing one KCDK week."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Callable

import pandas as pd

from .commentary import CommentaryConfig, CommentaryError
from .discord import DiscordConfig, DiscordError
from .dk_import import (
    contest_result_rows,
    import_contest,
    match_kcdk_members,
    weekly_standings,
)
from .members import load_members
from .persistence import connect_database
from .publishing import (
    DEFAULT_DISCORD_STATE_PATH,
    DiscordStateStore,
    PublishingError,
    WeeklyWorkflowResult,
    run_weekly_workflow,
)


DEFAULT_LOCAL_CONFIG_PATH = Path("data/processed/local_config.json")
DEFAULT_DATABASE_PATH = Path("data/processed/kcdk.sqlite")
ALLOWED_TONES = ("mild", "normal", "ruthless")


class WeeklyRunnerError(RuntimeError):
    """A safe, user-facing interactive-runner failure."""


@dataclass(frozen=True)
class LocalWeeklyConfig:
    """Non-secret operator defaults stored outside version control."""

    season_identifier: str
    season_name: str
    members_path: str = "data/members.csv"
    default_tone: str = "normal"
    season_year: int | None = None
    database_path: str = str(DEFAULT_DATABASE_PATH)
    state_path: str = str(DEFAULT_DISCORD_STATE_PATH)

    def __post_init__(self) -> None:
        text_fields = {
            "season_identifier": self.season_identifier,
            "season_name": self.season_name,
            "members_path": self.members_path,
            "default_tone": self.default_tone,
            "database_path": self.database_path,
            "state_path": self.state_path,
        }
        if any(not isinstance(value, str) for value in text_fields.values()):
            raise WeeklyRunnerError("Local runner text fields must be strings.")
        if not self.season_identifier.strip():
            raise WeeklyRunnerError("Season identifier cannot be empty.")
        if not self.season_name.strip():
            raise WeeklyRunnerError("Season name cannot be empty.")
        if self.default_tone not in ALLOWED_TONES:
            raise WeeklyRunnerError(
                f"Default tone must be one of: {', '.join(ALLOWED_TONES)}."
            )
        if self.season_year is not None:
            if not isinstance(self.season_year, int) or isinstance(
                self.season_year, bool
            ):
                raise WeeklyRunnerError("Season year must be a whole number.")
            if not 2000 <= self.season_year <= 2200:
                raise WeeklyRunnerError("Season year must be between 2000 and 2200.")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "LocalWeeklyConfig":
        if not isinstance(payload, dict):
            raise WeeklyRunnerError("Local runner configuration must be a JSON object.")
        _reject_secret_material(payload)
        allowed = {
            "season_identifier",
            "season_name",
            "members_path",
            "default_tone",
            "season_year",
            "database_path",
            "state_path",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise WeeklyRunnerError(
                "Local runner configuration has unsupported fields: "
                + ", ".join(unknown)
            )
        try:
            return cls(**payload)
        except TypeError as exc:
            raise WeeklyRunnerError("Local runner configuration is incomplete.") from exc


class LocalConfigStore:
    """Atomic JSON storage that refuses credentials and webhook material."""

    def __init__(self, path: str | Path = DEFAULT_LOCAL_CONFIG_PATH):
        self.path = Path(path)

    def load(self) -> LocalWeeklyConfig | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WeeklyRunnerError("Could not read local runner configuration.") from exc
        return LocalWeeklyConfig.from_dict(payload)

    def save(self, config: LocalWeeklyConfig) -> None:
        payload = config.to_dict()
        _reject_secret_material(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise WeeklyRunnerError("Could not save local runner configuration.") from exc


def _reject_secret_material(payload: object) -> None:
    text = json.dumps(payload, sort_keys=True).casefold()
    forbidden = (
        "api_key",
        "webhook",
        "password",
        "secret",
        "/api/webhooks/",
    )
    if any(marker in text for marker in forbidden):
        raise WeeklyRunnerError(
            "Refusing to store secret or webhook material in local runner configuration."
        )


@dataclass(frozen=True)
class StandingPreview:
    kcdk_rank: int
    display_name: str
    overall_rank: int | None
    points: float


@dataclass(frozen=True)
class WeeklyPreflight:
    csv_path: Path
    season_identifier: str
    season_name: str
    week_number: int
    week_label: str
    total_entries: int
    matched_members: int
    active_members: int
    unmatched_members: tuple[str, ...]
    standings: tuple[StandingPreview, ...]
    prize_column_available: bool
    known_prize_entries: int
    player_data_available: bool
    ownership_data_available: bool
    week_exists: bool
    exact_source_already_imported: bool
    already_published: bool
    openai_configured: bool
    discord_configured: bool
    configured_model: str | None
    blocking_errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def can_continue(self) -> bool:
        return not self.blocking_errors

    def render(self) -> str:
        lines = [
            "",
            "KCDK weekly preflight",
            "---------------------",
            f"CSV: {self.csv_path.name} ({self.csv_path})",
            f"Season/week: {self.season_name} [{self.season_identifier}] / {self.week_label}",
            f"Tournament entries parsed: {self.total_entries}",
            f"KCDK members matched: {self.matched_members}/{self.active_members}",
            "Unmatched configured members: "
            + (", ".join(self.unmatched_members) if self.unmatched_members else "none"),
            "Prize/money data: "
            + (
                f"available ({self.known_prize_entries} known entrant values)"
                if self.prize_column_available
                else "unavailable"
            ),
            f"Player data: {'available' if self.player_data_available else 'unavailable'}",
            f"Ownership data: {'available' if self.ownership_data_available else 'unavailable'}",
            f"Week already in database: {'yes' if self.week_exists else 'no'}",
            "Exact source already imported: "
            + ("yes" if self.exact_source_already_imported else "no"),
            f"Week already published: {'yes' if self.already_published else 'no'}",
            f"OpenAI configured: {'yes' if self.openai_configured else 'no'}",
            f"Discord configured: {'yes' if self.discord_configured else 'no'}",
        ]
        if self.configured_model:
            lines.append(f"OpenAI model: {self.configured_model}")
        lines.append("Standings preview:")
        if self.standings:
            lines.extend(
                f"  {row.kcdk_rank}. {row.display_name} - {row.points:.2f} pts "
                f"(DK rank {row.overall_rank if row.overall_rank is not None else 'unknown'})"
                for row in self.standings
            )
        else:
            lines.append("  none")
        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"  - {warning}" for warning in self.warnings)
        if self.blocking_errors:
            lines.append("BLOCKED:")
            lines.extend(f"  - {error}" for error in self.blocking_errors)
        return "\n".join(lines)


@dataclass(frozen=True)
class InteractiveRunResult:
    status: str
    preflight: WeeklyPreflight | None = None
    workflow: WeeklyWorkflowResult | None = None


def select_csv_file() -> Path | None:
    """Open the standard Windows file picker; return None on cancellation."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(
            title="Select DraftKings contest CSV",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
        )
        root.destroy()
    except Exception as exc:
        raise WeeklyRunnerError(
            "The Windows file picker could not open. Pass --csv with a file path instead."
        ) from exc
    return Path(selected) if selected else None


def confirmation_is_yes(answer: str) -> bool:
    """Require an explicit yes; blank and every other value are safe noes."""
    return answer.strip().casefold() in {"y", "yes"}


def _database_read_only(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise WeeklyRunnerError("Could not inspect the local KCDK database.") from exc


def suggest_next_week(database_path: str | Path, season_identifier: str) -> int:
    """Read the latest persisted week number without creating or modifying a DB."""
    connection = _database_read_only(Path(database_path))
    if connection is None:
        return 1
    try:
        tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contests'"
        ).fetchone()
        if tables is None:
            return 1
        row = connection.execute(
            """
            SELECT MAX(c.week_number) AS latest
            FROM contests c JOIN seasons s ON s.id = c.season_id
            WHERE s.identifier = ?
            """,
            (season_identifier,),
        ).fetchone()
        latest = row["latest"] if row else None
        return int(latest) + 1 if latest is not None else 1
    except sqlite3.Error as exc:
        raise WeeklyRunnerError("Could not inspect existing contest weeks.") from exc
    finally:
        connection.close()


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_week(
    database_path: Path, season_identifier: str, week_label: str
) -> tuple[int | None, str | None]:
    connection = _database_read_only(database_path)
    if connection is None:
        return None, None
    try:
        has_schema = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contests'"
        ).fetchone()
        if has_schema is None:
            return None, None
        row = connection.execute(
            """
            SELECT c.id, c.source_fingerprint
            FROM contests c JOIN seasons s ON s.id = c.season_id
            WHERE s.identifier = ? AND c.week_label = ?
            """,
            (season_identifier, week_label),
        ).fetchone()
        if row is None:
            return None, None
        return int(row["id"]), str(row["source_fingerprint"])
    except sqlite3.Error as exc:
        raise WeeklyRunnerError("Could not inspect the selected database week.") from exc
    finally:
        connection.close()


def _configuration_status() -> tuple[bool, bool, str | None, tuple[str, ...]]:
    errors: list[str] = []
    model: str | None = None
    openai_ready = False
    discord_ready = False
    try:
        commentary = CommentaryConfig.from_env(require_api_key=True)
        model = commentary.model
        openai_ready = True
    except CommentaryError as exc:
        errors.append(str(exc))
    try:
        DiscordConfig.from_env(require_webhooks=True)
        discord_ready = True
    except DiscordError as exc:
        errors.append(str(exc))
    return openai_ready, discord_ready, model, tuple(errors)


def build_preflight(
    *,
    csv_path: str | Path,
    config: LocalWeeklyConfig,
    week_number: int,
    dry_run: bool,
) -> WeeklyPreflight:
    """Parse and inspect a week without creating DB rows or contacting services."""
    source = Path(csv_path)
    members_path = Path(config.members_path)
    errors: list[str] = []
    warnings: list[str] = []
    empty = dict(
        total_entries=0,
        matched_members=0,
        active_members=0,
        unmatched_members=(),
        standings=(),
        prize_column_available=False,
        known_prize_entries=0,
        player_data_available=False,
        ownership_data_available=False,
    )
    if week_number < 1:
        errors.append("Week number must be at least 1.")
    if not source.is_file():
        errors.append(f"CSV file does not exist: {source}")
    if not members_path.is_file():
        errors.append(f"Member configuration does not exist: {members_path}")

    metrics: dict[str, object] = empty
    if not errors:
        try:
            contest = import_contest(source)
            results = contest_result_rows(contest)
            members = load_members(members_path, active_only=False)
            active = members.loc[members["active"]].copy()
            normalized_names = active["draftkings_name"].str.strip().str.casefold()
            if normalized_names.duplicated().any():
                errors.append("Active member configuration has duplicate DraftKings names.")
            matched = match_kcdk_members(contest, active)
            standings_frame = weekly_standings(contest, active)
            if standings_frame["display_name"].duplicated().any():
                errors.append("Multiple entries matched the same KCDK display name.")
            match_column = (
                "draftkings_entry_name"
                if "draftkings_entry_name" in matched
                else "entry_name"
            )
            matched_names = {
                str(value).strip().casefold() for value in matched[match_column]
            }
            unmatched = tuple(
                str(row["display_name"])
                for _, row in active.iterrows()
                if str(row["draftkings_name"]).strip().casefold() not in matched_names
            )
            standings = tuple(
                StandingPreview(
                    kcdk_rank=int(row["kcdk_rank"]),
                    display_name=str(row["display_name"]),
                    overall_rank=(
                        None if pd.isna(row["overall_rank"]) else int(row["overall_rank"])
                    ),
                    points=float(row["points"]),
                )
                for _, row in standings_frame.iterrows()
            )
            prize_available = "prize_cents" in results.columns
            known_prizes = (
                int(results["prize_cents"].notna().sum()) if prize_available else 0
            )
            metrics = dict(
                total_entries=len(results),
                matched_members=len(standings_frame),
                active_members=len(active),
                unmatched_members=unmatched,
                standings=standings,
                prize_column_available=prize_available,
                known_prize_entries=known_prizes,
                player_data_available=("lineup" in contest or "player" in contest),
                ownership_data_available="drafted_pct" in contest,
            )
            if len(results) == 0:
                errors.append("The CSV did not contain any tournament entries.")
            if len(standings_frame) == 0:
                errors.append("Zero active KCDK members matched the selected CSV.")
            if unmatched:
                warnings.append(
                    f"{len(unmatched)} active configured member(s) did not match."
                )
            if not prize_available:
                warnings.append("Prize/money data is unavailable in this export.")
            elif known_prizes == 0:
                warnings.append("Prize/money column exists but contains no known values.")
            if not metrics["player_data_available"]:
                warnings.append("Player/lineup data is unavailable in this export.")
            if not metrics["ownership_data_available"]:
                warnings.append("Ownership data is unavailable in this export.")
        except (OSError, ValueError, pd.errors.ParserError, UnicodeError) as exc:
            errors.append(f"CSV/member validation failed: {exc}")

    label = f"Week {week_number}"
    contest_id: int | None = None
    stored_fingerprint: str | None = None
    try:
        contest_id, stored_fingerprint = _existing_week(
            Path(config.database_path), config.season_identifier, label
        )
    except WeeklyRunnerError as exc:
        errors.append(str(exc))
    exact = False
    if contest_id is not None and source.is_file():
        exact = stored_fingerprint == _fingerprint(source)
        if not exact:
            errors.append(
                f"{label} already exists with a different CSV. The interactive runner "
                "will not replace it."
            )

    already_published = False
    if contest_id is not None:
        try:
            state = DiscordStateStore(config.state_path).load()
            already_published = (
                f"{config.season_identifier}:{contest_id}" in state.published_weeks
            )
        except PublishingError as exc:
            errors.append(str(exc))

    openai_ready, discord_ready, model, configuration_errors = _configuration_status()
    if not dry_run:
        errors.extend(configuration_errors)
    elif configuration_errors:
        warnings.append(
            "Live credentials were not required or used by this dry run."
        )

    return WeeklyPreflight(
        csv_path=source,
        season_identifier=config.season_identifier,
        season_name=config.season_name,
        week_number=week_number,
        week_label=label,
        week_exists=contest_id is not None,
        exact_source_already_imported=exact,
        already_published=already_published,
        openai_configured=openai_ready,
        discord_configured=discord_ready,
        configured_model=model,
        blocking_errors=tuple(errors),
        warnings=tuple(warnings),
        **metrics,
    )


def _prompt_config(
    store: LocalConfigStore,
    input_func: Callable[[str], str],
    output_func: Callable[[str], None],
) -> LocalWeeklyConfig:
    existing = store.load()
    if existing is not None:
        return existing
    output_func("First run: enter non-secret local defaults. They will be saved locally.")
    identifier = input_func("Season identifier: ").strip()
    name = input_func(f"Season name [{identifier}]: ").strip() or identifier
    year_text = input_func("Season year [blank for unknown]: ").strip()
    members = input_func("Members CSV [data/members.csv]: ").strip() or "data/members.csv"
    tone = input_func("Default tone [normal]: ").strip().casefold() or "normal"
    try:
        year = int(year_text) if year_text else None
    except ValueError as exc:
        raise WeeklyRunnerError("Season year must be a whole number.") from exc
    config = LocalWeeklyConfig(
        season_identifier=identifier,
        season_name=name,
        season_year=year,
        members_path=members,
        default_tone=tone,
    )
    store.save(config)
    output_func(f"Saved local defaults to {store.path} (no secrets).")
    return config


def _copy_database_for_dry_run(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    read_connection = _database_read_only(source)
    if read_connection is None:
        return
    write_connection = sqlite3.connect(destination)
    try:
        read_connection.backup(write_connection)
    finally:
        write_connection.close()
        read_connection.close()


def run_dry_run(
    *, csv_path: Path, config: LocalWeeklyConfig, week_number: int
) -> WeeklyWorkflowResult:
    """Execute the real pipeline against a disposable DB with no network calls."""
    with tempfile.TemporaryDirectory(prefix="kcdk-weekly-") as temporary:
        database = Path(temporary) / "kcdk.sqlite"
        _copy_database_for_dry_run(Path(config.database_path), database)
        connection = connect_database(database)
        try:
            return run_weekly_workflow(
                connection,
                csv_path=csv_path,
                members_path=config.members_path,
                season_name=config.season_name,
                season_identifier=config.season_identifier,
                week_label=f"Week {week_number}",
                week_number=week_number,
                season_year=config.season_year,
                tone=config.default_tone,
                dry_run=True,
                state_store=DiscordStateStore(config.state_path),
            )
        finally:
            connection.close()


def _success_summary(result: WeeklyWorkflowResult) -> str:
    operations = {
        operation.target: operation.action
        for operation in result.publication.operations
    }
    return "\n".join(
        (
            "",
            "KCDK weekly run completed successfully.",
            f"Imported: {result.import_summary.matched_kcdk_members} matched member(s), "
            f"{result.import_summary.lineup_player_rows_stored} lineup-player row(s)",
            f"KCDK leaderboard: {operations.get('kcdk_leaderboard', 'unknown')}",
            "Tournament leaderboard: "
            f"{operations.get('tournament_leaderboard', 'unknown')}",
            f"Weekly recap: {operations.get('weekly_recap', 'unknown')}",
            f"Commentary source: {result.publication.commentary_source}",
            *(f"{item.target}: {item.mode}"
              + (f" ({item.reason})" if item.reason else "")
              for item in result.publication.leaderboards),
        )
    )


def run_interactive_weekly(
    *,
    csv_path: str | Path | None = None,
    week_number: int | None = None,
    config_path: str | Path = DEFAULT_LOCAL_CONFIG_PATH,
    dry_run: bool = False,
    repost: bool = False,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
    file_selector: Callable[[], Path | None] = select_csv_file,
) -> InteractiveRunResult:
    """Guide a human through guarded preflight, confirmation, and publication."""
    selected = Path(csv_path) if csv_path is not None else file_selector()
    if selected is None:
        output_func("No CSV selected. Nothing was imported or published.")
        return InteractiveRunResult("cancelled")

    config = _prompt_config(LocalConfigStore(config_path), input_func, output_func)
    if week_number is None:
        suggestion = suggest_next_week(config.database_path, config.season_identifier)
        answer = input_func(f"Week number [{suggestion}]: ").strip()
        try:
            week_number = int(answer) if answer else suggestion
        except ValueError as exc:
            raise WeeklyRunnerError("Week number must be a whole number.") from exc

    preflight = build_preflight(
        csv_path=selected,
        config=config,
        week_number=week_number,
        dry_run=dry_run,
    )
    output_func(preflight.render())
    if not preflight.can_continue:
        output_func("Preflight failed. Nothing was imported or published.")
        return InteractiveRunResult("blocked", preflight)

    if dry_run:
        result = run_dry_run(csv_path=selected, config=config, week_number=week_number)
        output_func(json.dumps(result.to_dict(), indent=2, sort_keys=True, allow_nan=False))
        output_func(_success_summary(result))
        output_func("Dry run only: production data, OpenAI, and Discord were untouched.")
        return InteractiveRunResult("dry_run", preflight, result)

    if preflight.already_published and not repost:
        if not confirmation_is_yes(
            input_func("This week is already published. Repost the weekly recap? [y/N] ")
        ):
            output_func("Repost declined. Nothing was imported or published.")
            return InteractiveRunResult("already_published", preflight)
        repost = True

    if not confirmation_is_yes(input_func("Publish this week live? [y/N] ")):
        output_func("Live publication cancelled. Nothing was imported or published.")
        return InteractiveRunResult("declined", preflight)

    connection = connect_database(config.database_path)
    try:
        result = run_weekly_workflow(
            connection,
            csv_path=selected,
            members_path=config.members_path,
            season_name=config.season_name,
            season_identifier=config.season_identifier,
            week_label=preflight.week_label,
            week_number=week_number,
            season_year=config.season_year,
            tone=config.default_tone,
            dry_run=False,
            repost=repost,
            state_store=DiscordStateStore(config.state_path),
        )
    finally:
        connection.close()
    output_func(_success_summary(result))
    return InteractiveRunResult("published", preflight, result)


__all__ = [
    "DEFAULT_LOCAL_CONFIG_PATH",
    "InteractiveRunResult",
    "LocalConfigStore",
    "LocalWeeklyConfig",
    "StandingPreview",
    "WeeklyPreflight",
    "WeeklyRunnerError",
    "build_preflight",
    "confirmation_is_yes",
    "run_dry_run",
    "run_interactive_weekly",
    "select_csv_file",
    "suggest_next_week",
]
