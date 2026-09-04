"""Traceable structured generation and minimal Discord presentation for Drunk Bot."""

from dataclasses import dataclass, field, replace
import json
import os
import re

from .commentary import (
    CommentaryConfig, CommentaryConfigurationError, CommentaryResponseError,
    CommentaryUsage, _compact_fact, _new_openai_client, _response_usage,
    _safe_api_error, _neutralize_discord_mentions,
)
from .discord import DiscordMessage
from .drunk_bot import DrunkBotConfig, DrunkBotEligibility, roast_subject, select_drunk_bot_facts
from .drunk_bot_prompt import DRUNK_BOT_PROMPT_VERSION, drunk_bot_instructions, drunk_bot_schema
from .facts import WeeklyFactReport


@dataclass(frozen=True)
class DrunkBotInterjection:
    subject: str
    text: str
    fact_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"subject": self.subject, "text": self.text, "fact_ids": list(self.fact_ids)}


@dataclass(frozen=True)
class DrunkBotCommentary:
    interjections: tuple[DrunkBotInterjection, ...]

    def to_dict(self) -> dict[str, object]:
        return {"interjections": [item.to_dict() for item in self.interjections]}


@dataclass(frozen=True)
class DrunkBotGeneration:
    eligibility: DrunkBotEligibility
    request: dict[str, object] | None = None
    commentary: DrunkBotCommentary | None = None
    usage: CommentaryUsage = field(default_factory=CommentaryUsage)
    response_id: str | None = None
    response_model: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "eligibility": self.eligibility.to_dict(), "request": self.request,
            "commentary": self.commentary.to_dict() if self.commentary else None,
            "usage": self.usage.to_dict(), "response_id": self.response_id,
            "response_model": self.response_model, "prompt_version": DRUNK_BOT_PROMPT_VERSION,
        }


def build_drunk_bot_request(
    report: WeeklyFactReport, eligibility: DrunkBotEligibility,
    config: DrunkBotConfig, *, model: str,
) -> dict[str, object]:
    if not config.enabled or not eligibility.eligible or not eligibility.selected_facts:
        raise ValueError("Drunk Bot requires deterministic eligibility before a request.")
    facts = [dict(_compact_fact(fact), roast_subject=roast_subject(fact))
             for fact in eligibility.selected_facts]
    payload = {
        "season": report.season_identifier, "week": report.week_label,
        "selected_facts": facts, "subjects": list(eligibility.subjects),
        "maximum_members_targeted": config.maximum_members_targeted,
    }
    return {
        "model": model,
        "instructions": drunk_bot_instructions(
            tone=config.default_tone, allow_profanity=config.allow_profanity,
            maximum=config.maximum_interjections_per_week,
        ),
        "input": json.dumps(payload, sort_keys=True, allow_nan=False),
        "text": {"format": {
            "type": "json_schema", "name": "kcdk_drunk_bot", "strict": True,
            "schema": drunk_bot_schema(config.maximum_interjections_per_week),
        }},
        "store": False, "max_output_tokens": 1200,
        "metadata": {"persona": "drunk_bot", "prompt_version": DRUNK_BOT_PROMPT_VERSION},
    }


def parse_drunk_bot_commentary(
    response_text: str, *, request: dict[str, object], config: DrunkBotConfig,
) -> DrunkBotCommentary:
    try:
        payload = json.loads(response_text)
    except (ValueError, TypeError):
        raise CommentaryResponseError("Drunk Bot returned malformed JSON.") from None
    if not isinstance(payload, dict) or set(payload) != {"interjections"}:
        raise CommentaryResponseError("Drunk Bot returned unexpected fields.")
    raw = payload["interjections"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= config.maximum_interjections_per_week:
        raise CommentaryResponseError("Drunk Bot exceeded the interjection limit or returned none.")
    facts = {fact["fact_id"]: fact for fact in json.loads(request["input"])["selected_facts"]}
    parsed = []
    used_facts = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"subject", "text", "fact_ids"}:
            raise CommentaryResponseError("Drunk Bot returned an invalid interjection.")
        subject, text, ids = item["subject"], item["text"], item["fact_ids"]
        if (not isinstance(subject, str) or not subject.strip()
                or not isinstance(text, str) or not 1 <= len(text.strip()) <= 600
                or re.search(r"fact_[A-Za-z0-9_]+", text)):
            raise CommentaryResponseError("Drunk Bot returned invalid public text or subject.")
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(key, str) or key not in facts for key in ids)):
            raise CommentaryResponseError("Drunk Bot returned missing or unknown fact IDs.")
        if any(facts[key]["roast_subject"] != subject for key in ids):
            raise CommentaryResponseError("Drunk Bot assigned a fact to an unsupported subject.")
        if used_facts.intersection(ids):
            raise CommentaryResponseError("Drunk Bot repeated a fact across interjections.")
        used_facts.update(ids)
        parsed.append(DrunkBotInterjection(subject, text.strip(), tuple(dict.fromkeys(ids))))
    if len({item.subject for item in parsed}) > config.maximum_members_targeted:
        raise CommentaryResponseError("Drunk Bot exceeded the member limit.")
    return DrunkBotCommentary(tuple(parsed))


def generate_drunk_bot_commentary(
    report: WeeklyFactReport, *, config: DrunkBotConfig | None = None,
    openai_config: CommentaryConfig | None = None, client: object | None = None,
    dry_run: bool = False,
) -> DrunkBotGeneration:
    settings = config or DrunkBotConfig.from_env()
    eligibility = select_drunk_bot_facts(report, settings)
    # Check eligibility before resolving an API key or constructing any client.
    if not eligibility.eligible:
        return DrunkBotGeneration(eligibility)
    provider = openai_config or CommentaryConfig.from_env(require_api_key=not dry_run)
    provider = replace(provider, model=os.getenv("DRUNK_BOT_OPENAI_MODEL") or provider.model,
                       max_retries=0)
    request = build_drunk_bot_request(report, eligibility, settings, model=provider.model)
    if dry_run:
        return DrunkBotGeneration(eligibility, request)
    if client is None and not provider.api_key:
        raise CommentaryConfigurationError("OPENAI_API_KEY is required for Drunk Bot generation.")
    resolved_client = client or _new_openai_client(provider)
    try:
        response = resolved_client.responses.create(**request)
    except Exception as exc:
        raise _safe_api_error(exc) from None
    if getattr(response, "status", "completed") not in (None, "completed"):
        raise CommentaryResponseError("Drunk Bot returned an incomplete response.")
    result = parse_drunk_bot_commentary(
        getattr(response, "output_text", None), request=request, config=settings,
    )
    return DrunkBotGeneration(
        eligibility, request, result, _response_usage(response),
        getattr(response, "id", None), getattr(response, "model", None),
    )


def render_drunk_bot_message(commentary: DrunkBotCommentary) -> DiscordMessage:
    message = DiscordMessage(content=_neutralize_discord_mentions(
        "\n\n".join(item.text for item in commentary.interjections)
    ))
    message.to_payload()
    return message
