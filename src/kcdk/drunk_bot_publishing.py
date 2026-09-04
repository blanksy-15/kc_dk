"""Optional persona publication, with durable intent before network side effects."""

from dataclasses import dataclass, field
import json
from typing import Callable

from .commentary import CommentaryError
from .discord import DiscordError, DiscordTransport, RejectedDiscordMessageError
from .drunk_bot import DrunkBotConfig, DrunkBotEligibility, select_drunk_bot_facts
from .drunk_bot_commentary import (
    generate_drunk_bot_commentary, parse_drunk_bot_commentary, render_drunk_bot_message,
)
from .facts import WeeklyFactReport


DRUNK_BOT_STATUSES = frozenset({
    "disabled", "not_eligible", "missing_webhook", "generating", "generation_failed",
    "ready", "posting", "post_failed", "post_uncertain", "posted",
})


@dataclass
class DrunkBotRecord:
    status: str
    eligibility: dict[str, object] = field(default_factory=dict)
    generation: dict[str, object] | None = None
    message_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "eligibility": self.eligibility,
                "generation": self.generation, "message_id": self.message_id, "error": self.error}

    @classmethod
    def from_dict(cls, value: object) -> "DrunkBotRecord":
        if (not isinstance(value, dict) or value.get("status") not in DRUNK_BOT_STATUSES
                or not isinstance(value.get("eligibility", {}), dict)
                or (value.get("generation") is not None and not isinstance(value["generation"], dict))
                or (value.get("message_id") is not None and not isinstance(value["message_id"], str))
                or (value.get("error") is not None and not isinstance(value["error"], str))):
            raise ValueError("Drunk Bot publication state is malformed.")
        if value["status"] == "posted" and not value.get("message_id"):
            raise ValueError("Posted Drunk Bot state requires a message ID.")
        return cls(**value)


@dataclass
class DrunkBotPlan:
    eligibility: DrunkBotEligibility
    status: str
    would_generate_commentary: bool = False
    would_post_to_discord: bool = False
    message_id: str | None = None
    usage: dict[str, object] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return dict(self.eligibility.to_dict(), status=self.status,
                    would_generate_commentary=self.would_generate_commentary,
                    would_post_to_discord=self.would_post_to_discord,
                    message_id=self.message_id, usage=self.usage, error=self.error)


def plan_drunk_bot(
    report: WeeklyFactReport, config: DrunkBotConfig, *, destination_available: bool,
    record: DrunkBotRecord | None, commissioner_already_posted: bool, recover: bool,
) -> DrunkBotPlan:
    eligibility = select_drunk_bot_facts(report, config)
    plan = DrunkBotPlan(eligibility, "planned")
    if record:
        plan.message_id = record.message_id
        plan.usage = record.generation.get("usage") if record.generation else None
    if record and (record.message_id or record.status == "posted"):
        plan.status = "already_posted"
    elif record and record.status in {"posting", "post_uncertain"}:
        plan.status = "reconciliation_required"
        plan.error = "Inspect Discord and reconcile the stored Drunk Bot message before recovery."
    elif commissioner_already_posted and not recover:
        plan.status = "recovery_required" if not record else record.status
    elif not config.enabled:
        plan.status = "disabled"
    elif not eligibility.eligible:
        plan.status = "not_eligible"
    elif not destination_available:
        plan.status = "missing_webhook"
    else:
        plan.would_generate_commentary = not (record and record.generation)
        plan.would_post_to_discord = True
    return plan


def publish_drunk_bot(
    report: WeeklyFactReport, config: DrunkBotConfig, plan: DrunkBotPlan, *,
    record: DrunkBotRecord | None, webhook_url: str | None, client: DiscordTransport,
    save: Callable[[DrunkBotRecord], None],
) -> DrunkBotPlan:
    if not plan.would_post_to_discord:
        if record is None and plan.status in {"disabled", "not_eligible", "missing_webhook"}:
            save(DrunkBotRecord(plan.status, plan.eligibility.to_dict()))
        return plan
    current = record or DrunkBotRecord("generating", plan.eligibility.to_dict())
    if current.generation is None:
        current.status = "generating"
        save(current)
        try:
            generation = generate_drunk_bot_commentary(report, config=config)
            if generation.commentary is None:
                raise CommentaryError("No Drunk Bot commentary was generated.")
            current.generation = generation.to_dict()
        except (CommentaryError, ValueError) as exc:
            current.status, current.error = "generation_failed", str(exc)
            save(current)
            plan.status, plan.error = current.status, current.error
            return plan
        current.status = "ready"
        save(current)
    try:
        # Revalidate cached structured output under current size/member limits.
        commentary = parse_drunk_bot_commentary(
            json.dumps(current.generation["commentary"]),
            request=current.generation["request"], config=config,
        )
        message = render_drunk_bot_message(commentary)
    except (CommentaryError, DiscordError, KeyError, TypeError, ValueError):
        current.status, current.error = "generation_failed", "Stored Drunk Bot output is invalid."
        current.generation = None
        save(current)
        plan.status, plan.error = current.status, current.error
        return plan
    current.status, current.error = "posting", None
    save(current)  # A crash after POST must never trigger an automatic duplicate.
    try:
        current.message_id = client.create_message(webhook_url, message)
    except DiscordError as exc:
        current.status = "post_failed" if isinstance(exc, RejectedDiscordMessageError) else "post_uncertain"
        current.error = str(exc)
    else:
        current.status = "posted"
    save(current)
    plan.status, plan.message_id, plan.error = current.status, current.message_id, current.error
    plan.usage = current.generation.get("usage")
    return plan
