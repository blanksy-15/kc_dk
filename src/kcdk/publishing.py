"""High-level weekly orchestration and Discord-facing KCDK renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd

from .analytics import season_leaderboard, tournament_performance_leaderboard
from .commentary import (
    CommentaryRoast,
    StructuredCommentary,
    fact_identifier,
    generate_weekly_commentary,
    render_discord_markdown,
)
from .discord import (
    DISCORD_EMBED_DESCRIPTION_LIMIT,
    DISCORD_EMBED_FIELD_VALUE_LIMIT,
    DiscordConfig,
    DiscordError,
    DiscordMessage,
    DiscordTransport,
    DiscordWebhookClient,
)
from .facts import WeeklyFactReport, build_weekly_fact_report
from .persistence import ImportSummary, import_week, weekly_results


DEFAULT_DISCORD_STATE_PATH = Path("data/processed/discord_state.json")
STATE_VERSION = 1
KCDK_COLOR = 0x2E8B57
TOURNAMENT_COLOR = 0xD4AF37
WEEKLY_COLOR = 0x5865F2
WEEKLY_COMMENTARY_DESCRIPTION_LIMIT = 2600


class PublishingError(RuntimeError):
    """Raised for safe workflow, rendering, or state failures."""


@dataclass(frozen=True)
class PublicationRecord:
    message_ids: tuple[str, ...]
    published_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "message_ids": list(self.message_ids),
            "published_at": self.published_at,
        }


@dataclass
class DiscordState:
    kcdk_leaderboard_message_id: str | None = None
    tournament_leaderboard_message_id: str | None = None
    published_weeks: dict[str, PublicationRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": STATE_VERSION,
            "leaderboards": {
                "kcdk_message_id": self.kcdk_leaderboard_message_id,
                "tournament_message_id": self.tournament_leaderboard_message_id,
            },
            "published_weeks": {
                key: value.to_dict()
                for key, value in sorted(self.published_weeks.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DiscordState":
        if payload.get("version") != STATE_VERSION:
            raise PublishingError("Discord state has an unsupported version.")
        leaderboards = payload.get("leaderboards", {})
        publications = payload.get("published_weeks", {})
        if not isinstance(leaderboards, dict) or not isinstance(publications, dict):
            raise PublishingError("Discord state is malformed.")
        parsed: dict[str, PublicationRecord] = {}
        for key, value in publications.items():
            if not isinstance(value, dict):
                raise PublishingError("Discord publication state is malformed.")
            message_ids = value.get("message_ids", [])
            published_at = value.get("published_at", "")
            if (
                not isinstance(message_ids, list)
                or any(not isinstance(item, str) for item in message_ids)
                or not isinstance(published_at, str)
            ):
                raise PublishingError("Discord publication state is malformed.")
            parsed[str(key)] = PublicationRecord(tuple(message_ids), published_at)
        return cls(
            kcdk_leaderboard_message_id=_optional_id(
                leaderboards.get("kcdk_message_id")
            ),
            tournament_leaderboard_message_id=_optional_id(
                leaderboards.get("tournament_message_id")
            ),
            published_weeks=parsed,
        )


def _optional_id(value: object) -> str | None:
    return str(value) if isinstance(value, (str, int)) and str(value) else None


class DiscordStateStore:
    """Atomic local JSON storage that never contains webhook URLs."""

    def __init__(self, path: str | Path = DEFAULT_DISCORD_STATE_PATH):
        self.path = Path(path)

    def load(self) -> DiscordState:
        if not self.path.exists():
            return DiscordState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PublishingError("Could not read local Discord state.") from exc
        if not isinstance(payload, dict):
            raise PublishingError("Discord state is malformed.")
        return DiscordState.from_dict(payload)

    def save(self, state: DiscordState) -> None:
        if _contains_webhook_material(state.to_dict()):
            raise PublishingError("Refusing to persist webhook material in state.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise PublishingError("Could not persist local Discord state.") from exc


def _contains_webhook_material(payload: object) -> bool:
    text = json.dumps(payload, sort_keys=True).lower()
    return "webhook_url" in text or "/api/webhooks/" in text


def _truncate(text: str, limit: int) -> str:
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    if limit < 2:
        return clean[:limit]
    boundary = clean.rfind("\n", 0, limit - 1)
    if boundary < limit // 2:
        boundary = clean.rfind(" ", 0, limit - 1)
    if boundary <= 0:
        boundary = limit - 1
    return clean[:boundary].rstrip() + "…"


def _is_missing(value: object) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return value is None


def _money(value: object, *, known: bool = True) -> str:
    if not known or _is_missing(value):
        return "unknown"
    return f"${float(value):,.2f}"


def _integer(value: object) -> str:
    if _is_missing(value):
        return "?"
    return str(int(value))


def _weekly_result_lines(results: pd.DataFrame) -> str:
    lines = []
    for row in results.itertuples(index=False):
        lines.append(
            f"**{int(row.kcdk_finish)}. {row.display_name}** — "
            f"{float(row.draftkings_fantasy_points):.2f} pts"
        )
    return _truncate("\n".join(lines), DISCORD_EMBED_FIELD_VALUE_LIMIT)


def _weekly_tournament_lines(results: pd.DataFrame) -> str:
    highlights = results.sort_values(
        ["prize_cents", "draftkings_fantasy_points"],
        ascending=[False, False],
        na_position="last",
    )
    lines = []
    for row in highlights.itertuples(index=False):
        known = not _is_missing(row.prize_cents)
        lines.append(
            f"**{row.display_name}** — DK #{_integer(row.draftkings_overall_rank)} · "
            f"{_money(row.money_won, known=known)}"
        )
    return _truncate("\n".join(lines), DISCORD_EMBED_FIELD_VALUE_LIMIT)


def render_weekly_recap(
    report: WeeklyFactReport,
    commentary: StructuredCommentary,
    results: pd.DataFrame,
) -> DiscordMessage:
    """Render one compact weekly recap embed without public fact IDs."""
    if results.empty:
        raise PublishingError("Cannot render a weekly recap without weekly results.")
    public_commentary = render_discord_markdown(commentary)
    fields: list[dict[str, object]] = [
        {
            "name": "Weekly KCDK results",
            "value": _weekly_result_lines(results),
            "inline": False,
        },
        {
            "name": "Tournament results / earnings",
            "value": _weekly_tournament_lines(results),
            "inline": False,
        },
    ]
    if report.warnings:
        warning_text = "\n".join(f"• {item}" for item in report.warnings)
        fields.append(
            {
                "name": "Data notes",
                "value": _truncate(
                    warning_text, DISCORD_EMBED_FIELD_VALUE_LIMIT
                ),
                "inline": False,
            }
        )
    message = DiscordMessage(
        content=f"# KCDK {report.week_label}",
        embeds=(
            {
                "title": "Commissioner Report",
                "description": _truncate(
                    public_commentary,
                    min(
                        WEEKLY_COMMENTARY_DESCRIPTION_LIMIT,
                        DISCORD_EMBED_DESCRIPTION_LIMIT,
                    ),
                ),
                "color": WEEKLY_COLOR,
                "fields": fields,
                "footer": {"text": report.season_name},
            },
        ),
    )
    message.to_payload()
    return message


def render_kcdk_leaderboard(
    leaderboard: pd.DataFrame, *, season_name: str, through_week: str
) -> DiscordMessage:
    if leaderboard.empty:
        raise PublishingError("Cannot render an empty KCDK leaderboard.")
    lines = []
    for row in leaderboard.itertuples(index=False):
        lines.append(
            f"`{int(row.season_rank):>2}.` **{row.display_name}** — "
            f"avg {float(row.average_finish):.2f} · {int(row.wins)}W · "
            f"{int(row.podium_finishes)}P · {int(row.weeks_played)} wk"
        )
    message = DiscordMessage(
        embeds=(
            {
                "title": "KCDK SEASON STANDINGS",
                "description": _truncate(
                    "\n".join(lines), DISCORD_EMBED_DESCRIPTION_LIMIT
                ),
                "color": KCDK_COLOR,
                "footer": {
                    "text": f"{season_name} · through {through_week} · ranked by average finish"
                },
            },
        )
    )
    message.to_payload()
    return message


def render_tournament_leaderboard(
    leaderboard: pd.DataFrame, *, season_name: str, through_week: str
) -> DiscordMessage:
    if leaderboard.empty:
        raise PublishingError("Cannot render an empty tournament leaderboard.")
    lines = []
    incomplete = False
    for row in leaderboard.itertuples(index=False):
        known_weeks = int(row.weeks_with_prize_data)
        weeks = int(row.weeks_played)
        complete = bool(row.prize_data_complete)
        incomplete = incomplete or not complete
        suffix = "" if complete else "*"
        lines.append(
            f"`{int(row.tournament_rank):>2}.` **{row.display_name}** — "
            f"{_money(row.total_money_won)}{suffix} · {_integer(row.cashes)} cash · "
            f"{float(row.average_draftkings_fantasy_points):.2f} avg pts · "
            f"{known_weeks}/{weeks} prize wk"
        )
    footer = f"{season_name} · through {through_week} · money, then average DK points"
    if incomplete:
        footer += " · * partial known prize data"
    message = DiscordMessage(
        embeds=(
            {
                "title": "TOURNAMENT EARNINGS",
                "description": _truncate(
                    "\n".join(lines), DISCORD_EMBED_DESCRIPTION_LIMIT
                ),
                "color": TOURNAMENT_COLOR,
                "footer": {"text": footer},
            },
        )
    )
    message.to_payload()
    return message


def preview_commentary(report: WeeklyFactReport) -> StructuredCommentary:
    """Build a fact-only, non-OpenAI preview for charge-free Discord dry runs."""
    roasts = tuple(
        CommentaryRoast(
            member=fact.subject_member_name or "League",
            text=fact.summary,
            fact_ids=(fact_identifier(fact),),
        )
        for fact in report.selected_facts[:4]
    )
    return StructuredCommentary(
        headline=f"{report.week_label} preview",
        intro="Dry-run preview using selected deterministic facts; no OpenAI request was made.",
        roasts=roasts,
        closing="Live publishing will replace this preview with the Commissioner Report.",
    )


@dataclass(frozen=True)
class DiscordOperation:
    target: str
    action: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"target": self.target, "action": self.action, "reason": self.reason}


@dataclass(frozen=True)
class WeeklyPublishResult:
    season_identifier: str
    week_label: str
    publication_key: str
    already_published: bool
    repost_requested: bool
    dry_run: bool
    commentary_source: str
    operations: tuple[DiscordOperation, ...]
    weekly_message: DiscordMessage
    kcdk_leaderboard_message: DiscordMessage
    tournament_leaderboard_message: DiscordMessage
    weekly_message_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "season_identifier": self.season_identifier,
            "week_label": self.week_label,
            "publication_key": self.publication_key,
            "already_published": self.already_published,
            "repost_requested": self.repost_requested,
            "dry_run": self.dry_run,
            "commentary_source": self.commentary_source,
            "operations": [item.to_dict() for item in self.operations],
            "rendered": {
                "weekly_recap": self.weekly_message.to_payload(),
                "kcdk_leaderboard": self.kcdk_leaderboard_message.to_payload(),
                "tournament_leaderboard": self.tournament_leaderboard_message.to_payload(),
            },
            "weekly_message_ids": list(self.weekly_message_ids),
        }


def _contest_id_for_week(
    connection: sqlite3.Connection, season_identifier: str, week_label: str
) -> int:
    row = connection.execute(
        """
        SELECT c.id
        FROM contests c JOIN seasons s ON s.id = c.season_id
        WHERE s.identifier = ? AND c.week_label = ?
        """,
        (season_identifier, week_label),
    ).fetchone()
    if row is None:
        raise PublishingError(
            f"No imported contest exists for {season_identifier} / {week_label}."
        )
    return int(row["id"])


def _publication_key(season_identifier: str, contest_id: int) -> str:
    return f"{season_identifier}:{contest_id}"


def _planned_operations(
    state: DiscordState, *, already_published: bool, repost: bool
) -> tuple[DiscordOperation, ...]:
    return (
        DiscordOperation(
            "kcdk_leaderboard",
            "edit" if state.kcdk_leaderboard_message_id else "create",
            "refresh persistent standings",
        ),
        DiscordOperation(
            "tournament_leaderboard",
            "edit" if state.tournament_leaderboard_message_id else "create",
            "refresh persistent tournament earnings",
        ),
        DiscordOperation(
            "weekly_recap",
            "create" if (not already_published or repost) else "skip",
            (
                "explicit repost requested"
                if already_published and repost
                else "week already published"
                if already_published
                else "new weekly publication"
            ),
        ),
    )


def _save_state(store: DiscordStateStore, state: DiscordState) -> None:
    store.save(state)


def publish_weekly_report(
    connection: sqlite3.Connection,
    *,
    season_identifier: str,
    week_label: str,
    tone: str = "normal",
    dry_run: bool = False,
    repost: bool = False,
    generate_commentary_in_dry_run: bool = False,
    commentary: StructuredCommentary | None = None,
    state_store: DiscordStateStore | None = None,
    discord_config: DiscordConfig | None = None,
    transport: DiscordTransport | None = None,
) -> WeeklyPublishResult:
    """Refresh both leaderboards and idempotently publish a weekly recap."""
    store = state_store or DiscordStateStore()
    state = store.load()
    contest_id = _contest_id_for_week(connection, season_identifier, week_label)
    report = build_weekly_fact_report(
        connection, season_identifier, contest_id, max_facts=8
    )
    key = _publication_key(season_identifier, contest_id)
    already_published = key in state.published_weeks
    should_publish_week = not already_published or repost
    known_weekly_message_ids = (
        state.published_weeks[key].message_ids if already_published else ()
    )

    resolved_discord_config: DiscordConfig | None = None
    if not dry_run:
        resolved_discord_config = discord_config or DiscordConfig.from_env()
        if (
            resolved_discord_config.weekly_webhook_url is None
            or resolved_discord_config.leaderboard_webhook_url is None
        ):
            raise PublishingError(
                "Both Discord webhooks are required for live publishing."
            )

    if commentary is not None:
        resolved_commentary = commentary
        commentary_source = "provided"
    elif dry_run and not generate_commentary_in_dry_run:
        resolved_commentary = preview_commentary(report)
        commentary_source = "deterministic_preview"
    elif not should_publish_week:
        resolved_commentary = preview_commentary(report)
        commentary_source = "not_generated_already_published"
    else:
        generation = generate_weekly_commentary(report, tone=tone)
        if generation.commentary is None:
            raise PublishingError("OpenAI returned no commentary for publication.")
        resolved_commentary = generation.commentary
        commentary_source = "openai"

    week_results = weekly_results(connection, season_identifier, week_label)
    kcdk = season_leaderboard(connection, season_identifier, contest_id)
    tournament = tournament_performance_leaderboard(
        connection, season_identifier, contest_id
    )
    weekly_message = render_weekly_recap(report, resolved_commentary, week_results)
    kcdk_message = render_kcdk_leaderboard(
        kcdk, season_name=report.season_name, through_week=report.week_label
    )
    tournament_message = render_tournament_leaderboard(
        tournament, season_name=report.season_name, through_week=report.week_label
    )
    operations = _planned_operations(
        state, already_published=already_published, repost=repost
    )

    if dry_run:
        return WeeklyPublishResult(
            season_identifier=season_identifier,
            week_label=week_label,
            publication_key=key,
            already_published=already_published,
            repost_requested=repost,
            dry_run=True,
            commentary_source=commentary_source,
            operations=operations,
            weekly_message=weekly_message,
            kcdk_leaderboard_message=kcdk_message,
            tournament_leaderboard_message=tournament_message,
            weekly_message_ids=known_weekly_message_ids,
        )

    config = resolved_discord_config
    if config is None:
        raise PublishingError("Discord configuration was not resolved.")
    if config.weekly_webhook_url is None or config.leaderboard_webhook_url is None:
        raise PublishingError("Both Discord webhooks are required for live publishing.")
    client = transport or DiscordWebhookClient(
        timeout_seconds=config.timeout_seconds
    )

    try:
        if state.kcdk_leaderboard_message_id:
            client.edit_message(
                config.leaderboard_webhook_url,
                state.kcdk_leaderboard_message_id,
                kcdk_message,
            )
        else:
            state.kcdk_leaderboard_message_id = client.create_message(
                config.leaderboard_webhook_url, kcdk_message
            )
            _save_state(store, state)
    except DiscordError as exc:
        raise PublishingError(f"KCDK leaderboard update failed: {exc}") from exc

    try:
        if state.tournament_leaderboard_message_id:
            client.edit_message(
                config.leaderboard_webhook_url,
                state.tournament_leaderboard_message_id,
                tournament_message,
            )
        else:
            state.tournament_leaderboard_message_id = client.create_message(
                config.leaderboard_webhook_url, tournament_message
            )
            _save_state(store, state)
    except DiscordError as exc:
        raise PublishingError(
            f"Tournament leaderboard update failed: {exc}"
        ) from exc

    weekly_message_ids = known_weekly_message_ids
    if should_publish_week:
        try:
            weekly_message_id = client.create_message(
                config.weekly_webhook_url, weekly_message
            )
        except DiscordError as exc:
            raise PublishingError(f"Weekly recap publish failed: {exc}") from exc
        weekly_message_ids = known_weekly_message_ids + (weekly_message_id,)
        state.published_weeks[key] = PublicationRecord(
            message_ids=weekly_message_ids,
            published_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        _save_state(store, state)

    return WeeklyPublishResult(
        season_identifier=season_identifier,
        week_label=week_label,
        publication_key=key,
        already_published=already_published,
        repost_requested=repost,
        dry_run=False,
        commentary_source=commentary_source,
        operations=operations,
        weekly_message=weekly_message,
        kcdk_leaderboard_message=kcdk_message,
        tournament_leaderboard_message=tournament_message,
        weekly_message_ids=weekly_message_ids,
    )


@dataclass(frozen=True)
class WeeklyWorkflowResult:
    import_summary: ImportSummary
    publication: WeeklyPublishResult

    def to_dict(self) -> dict[str, object]:
        return {
            "import": self.import_summary.to_dict(),
            "publication": self.publication.to_dict(),
        }


def run_weekly_workflow(
    connection: sqlite3.Connection,
    *,
    csv_path: str | Path,
    members_path: str | Path,
    season_name: str,
    season_identifier: str,
    week_label: str,
    week_number: int | None = None,
    season_year: int | None = None,
    contest_name: str | None = None,
    contest_date: str | None = None,
    tone: str = "normal",
    dry_run: bool = False,
    repost: bool = False,
    generate_commentary_in_dry_run: bool = False,
    state_store: DiscordStateStore | None = None,
) -> WeeklyWorkflowResult:
    """Import one CSV, then render or publish the complete weekly package."""
    summary = import_week(
        connection,
        csv_path,
        members_path,
        season_name=season_name,
        season_identifier=season_identifier,
        season_year=season_year,
        week_number=week_number,
        week_label=week_label,
        contest_name=contest_name,
        contest_date=contest_date,
    )
    publication = publish_weekly_report(
        connection,
        season_identifier=season_identifier,
        week_label=week_label,
        tone=tone,
        dry_run=dry_run,
        repost=repost,
        generate_commentary_in_dry_run=generate_commentary_in_dry_run,
        state_store=state_store,
    )
    return WeeklyWorkflowResult(summary, publication)


__all__ = [
    "DEFAULT_DISCORD_STATE_PATH",
    "DiscordOperation",
    "DiscordState",
    "DiscordStateStore",
    "PublicationRecord",
    "PublishingError",
    "WeeklyPublishResult",
    "WeeklyWorkflowResult",
    "preview_commentary",
    "publish_weekly_report",
    "render_kcdk_leaderboard",
    "render_tournament_leaderboard",
    "render_weekly_recap",
    "run_weekly_workflow",
]
