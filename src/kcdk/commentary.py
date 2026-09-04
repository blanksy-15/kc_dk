"""OpenAI-backed phrasing for deterministic KCDK weekly facts.

This module deliberately knows nothing about raw DraftKings CSV files, database
persistence, or Discord delivery. It accepts an already-built WeeklyFactReport,
sends only a compact selected-fact payload, and validates traceable structured
output before rendering it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping

from dotenv import load_dotenv

from .commentary_prompt import (
    COMMENTARY_OUTPUT_SCHEMA,
    COMMENTARY_PROMPT_VERSION,
    TONE_PROFILES,
    commentary_instructions,
)
from .facts import Fact, WeeklyFactReport


DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_OUTPUT_TOKENS = 1200
DEFAULT_DISCORD_CHUNK_SIZE = 1900
MAX_COMMENTARY_NOTE_CHARS = 240


class CommentaryError(RuntimeError):
    """Base error for commentary configuration, transport, and validation."""


class CommentaryConfigurationError(CommentaryError):
    """Raised for missing or invalid local OpenAI configuration."""


class CommentaryAPIError(CommentaryError):
    """Raised when the OpenAI request cannot be completed."""


class CommentaryResponseError(CommentaryError):
    """Raised when structured commentary is missing or invalid."""


@dataclass(frozen=True)
class CommentaryConfig:
    """OpenAI request configuration with the secret excluded from repr."""

    api_key: str | None = field(default=None, repr=False)
    model: str = DEFAULT_OPENAI_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise CommentaryConfigurationError("OPENAI_MODEL cannot be empty.")
        if self.timeout_seconds <= 0:
            raise CommentaryConfigurationError(
                "OPENAI_TIMEOUT_SECONDS must be greater than zero."
            )
        if not 0 <= self.max_retries <= 5:
            raise CommentaryConfigurationError(
                "OPENAI_MAX_RETRIES must be between zero and five."
            )

    @classmethod
    def from_env(
        cls,
        *,
        require_api_key: bool = True,
        env_file: str | Path | None = None,
    ) -> "CommentaryConfig":
        """Load supported settings without ever exposing the key in errors."""
        dotenv_path = Path(env_file) if env_file is not None else Path.cwd() / ".env"
        load_dotenv(dotenv_path=dotenv_path, override=False)

        api_key = (os.getenv("OPENAI_API_KEY") or "").strip() or None
        if require_api_key and api_key is None:
            raise CommentaryConfigurationError(
                "OPENAI_API_KEY is not configured. Add it to the ignored .env file."
            )

        try:
            timeout_seconds = float(
                os.getenv("OPENAI_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
            )
            max_retries = int(
                os.getenv("OPENAI_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))
            )
        except ValueError as exc:
            raise CommentaryConfigurationError(
                "OpenAI timeout and retry settings must be numeric."
            ) from exc

        return cls(
            api_key=api_key,
            model=(os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL).strip(),
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )


@dataclass(frozen=True)
class MemberCommentaryContext:
    """Optional, explicitly safe phrasing context for one member."""

    display_name: str
    nickname: str = ""
    commentary_notes: str = ""


@dataclass(frozen=True)
class CommentaryRoast:
    member: str
    text: str
    fact_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "member": self.member,
            "text": self.text,
            "fact_ids": list(self.fact_ids),
        }


@dataclass(frozen=True)
class StructuredCommentary:
    headline: str
    intro: str
    roasts: tuple[CommentaryRoast, ...]
    closing: str

    def to_dict(self) -> dict[str, object]:
        return {
            "headline": self.headline,
            "intro": self.intro,
            "roasts": [roast.to_dict() for roast in self.roasts],
            "closing": self.closing,
        }


@dataclass(frozen=True)
class CommentaryUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class CommentaryRequest:
    """Secret-free exact request content for inspection and dry runs."""

    model: str
    tone: str
    instructions: str
    payload: dict[str, object]
    input_json: str
    fact_ids: tuple[str, ...]
    input_characters: int
    input_bytes: int
    prompt_characters: int
    prompt_bytes: int

    def api_arguments(self) -> dict[str, object]:
        return {
            "model": self.model,
            "instructions": self.instructions,
            "input": self.input_json,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "kcdk_weekly_commentary",
                    "strict": True,
                    "schema": COMMENTARY_OUTPUT_SCHEMA,
                }
            },
            "store": False,
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "metadata": {
                "prompt_version": COMMENTARY_PROMPT_VERSION,
                "tone": self.tone,
                "season": str(self.payload["season"]["identifier"]),
                "week": str(self.payload["contest"]["week_label"]),
            },
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "prompt_version": COMMENTARY_PROMPT_VERSION,
            "model": self.model,
            "tone": self.tone,
            "fact_ids": list(self.fact_ids),
            "fact_count": len(self.fact_ids),
            "input_characters": self.input_characters,
            "input_bytes": self.input_bytes,
            "prompt_characters": self.prompt_characters,
            "prompt_bytes": self.prompt_bytes,
            "api_arguments": self.api_arguments(),
        }


@dataclass(frozen=True)
class CommentaryGeneration:
    """Structured output and persistence-friendly request metadata."""

    request: CommentaryRequest
    commentary: StructuredCommentary | None
    dry_run: bool
    generated_at: str
    response_id: str | None = None
    response_model: str | None = None
    usage: CommentaryUsage = field(default_factory=CommentaryUsage)

    def to_dict(self) -> dict[str, object]:
        return {
            "prompt_version": COMMENTARY_PROMPT_VERSION,
            "tone": self.request.tone,
            "requested_model": self.request.model,
            "response_model": self.response_model,
            "response_id": self.response_id,
            "generated_at": self.generated_at,
            "dry_run": self.dry_run,
            "fact_ids": list(self.request.fact_ids),
            "usage": self.usage.to_dict(),
            "commentary": (
                self.commentary.to_dict() if self.commentary is not None else None
            ),
            "request": self.request.to_dict(),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)


def fact_identifier(fact: Fact) -> str:
    """Return a stable content-derived ID for model-output traceability."""
    canonical = json.dumps(
        fact.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"fact_{digest}"


def _compact_fact(fact: Fact) -> dict[str, object]:
    return {
        "fact_id": fact_identifier(fact),
        "type": fact.fact_type,
        "category": fact.category,
        "summary": fact.summary,
        "priority": fact.priority,
        "subject_member": fact.subject_member_name,
        "related_member": fact.related_member_name,
        "player": fact.related_player,
        "values": fact.values,
        "completeness": fact.completeness,
        "tags": list(fact.tags),
    }


def _clean_context_text(value: object, *, limit: int) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _member_names(report: WeeklyFactReport) -> set[str]:
    names: set[str] = set()
    for fact in report.selected_facts:
        if fact.subject_member_name:
            names.add(fact.subject_member_name)
        if fact.related_member_name:
            names.add(fact.related_member_name)
    return names


def _compact_member_context(
    report: WeeklyFactReport,
    member_context: Mapping[str, MemberCommentaryContext | Mapping[str, object]] | None,
) -> list[dict[str, str]]:
    if not member_context:
        return []

    relevant_names = _member_names(report)
    compact: list[dict[str, str]] = []
    for key in sorted(member_context):
        value = member_context[key]
        if isinstance(value, MemberCommentaryContext):
            display_name = value.display_name
            nickname = value.nickname
            notes = value.commentary_notes
        elif isinstance(value, Mapping):
            display_name = str(value.get("display_name") or key)
            nickname = str(value.get("nickname") or "")
            notes = str(value.get("commentary_notes") or "")
        else:
            raise TypeError(
                "Member context values must be MemberCommentaryContext objects or mappings."
            )

        display_name = _clean_context_text(display_name, limit=100)
        if display_name not in relevant_names:
            continue
        item = {"display_name": display_name}
        safe_nickname = _clean_context_text(nickname, limit=80)
        safe_notes = _clean_context_text(notes, limit=MAX_COMMENTARY_NOTE_CHARS)
        if safe_nickname:
            item["nickname"] = safe_nickname
        if safe_notes:
            item["commentary_notes"] = safe_notes
        compact.append(item)
    return compact


def build_commentary_request(
    report: WeeklyFactReport,
    *,
    tone: str = "normal",
    model: str = DEFAULT_OPENAI_MODEL,
    member_context: Mapping[
        str, MemberCommentaryContext | Mapping[str, object]
    ]
    | None = None,
) -> CommentaryRequest:
    """Build the exact, compact, secret-free Responses API request."""
    instructions = commentary_instructions(tone)
    if not model.strip():
        raise CommentaryConfigurationError("OPENAI_MODEL cannot be empty.")
    if not report.selected_facts:
        raise CommentaryConfigurationError(
            "The weekly fact report has no selected facts to narrate."
        )

    compact_facts = [_compact_fact(fact) for fact in report.selected_facts]
    fact_ids = tuple(str(fact["fact_id"]) for fact in compact_facts)
    payload: dict[str, object] = {
        "prompt_version": COMMENTARY_PROMPT_VERSION,
        "tone": tone,
        "season": {
            "identifier": report.season_identifier,
            "name": report.season_name,
        },
        "contest": {
            "id": report.contest_id,
            "week_label": report.week_label,
        },
        "selected_facts": compact_facts,
        "data_warnings": list(report.warnings),
        "member_context": _compact_member_context(report, member_context),
    }
    input_json = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return CommentaryRequest(
        model=model.strip(),
        tone=tone,
        instructions=instructions,
        payload=payload,
        input_json=input_json,
        fact_ids=fact_ids,
        input_characters=len(input_json),
        input_bytes=len(input_json.encode("utf-8")),
        prompt_characters=len(instructions) + len(input_json),
        prompt_bytes=len(instructions.encode("utf-8"))
        + len(input_json.encode("utf-8")),
    )


def _required_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise CommentaryResponseError(
            f"OpenAI returned invalid structured commentary: {field_name} is missing."
        )
    return value.strip()


def parse_structured_commentary(
    response_text: str,
    *,
    allowed_fact_ids: tuple[str, ...] | list[str],
    allowed_members_by_fact_id: Mapping[str, tuple[str, ...]] | None = None,
) -> StructuredCommentary:
    """Parse and validate model JSON, including every fact reference."""
    try:
        payload = json.loads(response_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CommentaryResponseError(
            "OpenAI returned malformed structured commentary."
        ) from exc
    if not isinstance(payload, dict):
        raise CommentaryResponseError(
            "OpenAI returned malformed structured commentary."
        )
    if set(payload) != {"headline", "intro", "roasts", "closing"}:
        raise CommentaryResponseError(
            "OpenAI returned structured commentary with unexpected fields."
        )

    raw_roasts = payload.get("roasts")
    if not isinstance(raw_roasts, list):
        raise CommentaryResponseError(
            "OpenAI returned invalid structured commentary: roasts is missing."
        )

    allowed = set(allowed_fact_ids)
    roasts: list[CommentaryRoast] = []
    for raw_roast in raw_roasts:
        if not isinstance(raw_roast, dict):
            raise CommentaryResponseError(
                "OpenAI returned invalid structured commentary: a roast is malformed."
            )
        if set(raw_roast) != {"member", "text", "fact_ids"}:
            raise CommentaryResponseError(
                "OpenAI returned a roast with unexpected fields."
            )
        member = _required_text(raw_roast, "member")
        roast_text = _required_text(raw_roast, "text")
        raw_ids = raw_roast.get("fact_ids")
        if (
            not isinstance(raw_ids, list)
            or not raw_ids
            or any(not isinstance(item, str) or not item for item in raw_ids)
        ):
            raise CommentaryResponseError(
                "OpenAI returned a roast without valid fact IDs."
            )
        unknown = sorted(set(raw_ids) - allowed)
        if unknown:
            raise CommentaryResponseError(
                "OpenAI returned commentary referencing an unknown fact ID."
            )
        if allowed_members_by_fact_id is not None and member != "League":
            supported_members = {
                supported_member
                for fact_id in raw_ids
                for supported_member in allowed_members_by_fact_id.get(fact_id, ())
            }
            if member not in supported_members:
                raise CommentaryResponseError(
                    "OpenAI returned commentary assigning a fact to an unsupported member."
                )
        roasts.append(
            CommentaryRoast(
                member=member,
                text=roast_text,
                fact_ids=tuple(dict.fromkeys(raw_ids)),
            )
        )

    return StructuredCommentary(
        headline=_required_text(payload, "headline"),
        intro=_required_text(payload, "intro"),
        roasts=tuple(roasts),
        closing=_required_text(payload, "closing"),
    )


def _usage_value(usage: object, name: str) -> int | None:
    if isinstance(usage, Mapping):
        value = usage.get(name)
    else:
        value = getattr(usage, name, None)
    return int(value) if isinstance(value, int) else None


def _response_usage(response: object) -> CommentaryUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return CommentaryUsage()
    return CommentaryUsage(
        input_tokens=_usage_value(usage, "input_tokens"),
        output_tokens=_usage_value(usage, "output_tokens"),
        total_tokens=_usage_value(usage, "total_tokens"),
    )


def _safe_api_error(exc: Exception) -> CommentaryAPIError:
    """Create a useful error that never interpolates exception text or secrets."""
    error_kind = type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    suffix = f", HTTP {status_code}" if isinstance(status_code, int) else ""
    return CommentaryAPIError(f"OpenAI request failed ({error_kind}{suffix}).")


def _new_openai_client(config: CommentaryConfig) -> object:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise CommentaryConfigurationError(
            "The OpenAI Python SDK is not installed; install requirements.txt."
        ) from exc
    return OpenAI(
        api_key=config.api_key,
        timeout=config.timeout_seconds,
        max_retries=config.max_retries,
    )


def generate_weekly_commentary(
    report: WeeklyFactReport,
    *,
    tone: str = "normal",
    member_context: Mapping[
        str, MemberCommentaryContext | Mapping[str, object]
    ]
    | None = None,
    config: CommentaryConfig | None = None,
    client: object | None = None,
    dry_run: bool = False,
) -> CommentaryGeneration:
    """Build a dry run or make one Responses API request and validate it."""
    resolved_config = config or CommentaryConfig.from_env(
        require_api_key=not dry_run
    )
    request = build_commentary_request(
        report,
        tone=tone,
        model=resolved_config.model,
        member_context=member_context,
    )
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    if dry_run:
        return CommentaryGeneration(
            request=request,
            commentary=None,
            dry_run=True,
            generated_at=generated_at,
        )

    if not (resolved_config.api_key or "").strip():
        raise CommentaryConfigurationError(
            "OPENAI_API_KEY is not configured. Add it to the ignored .env file."
        )
    resolved_client = client or _new_openai_client(resolved_config)
    try:
        response = resolved_client.responses.create(**request.api_arguments())
    except Exception as exc:
        raise _safe_api_error(exc) from None

    status = getattr(response, "status", "completed")
    if status not in (None, "completed"):
        raise CommentaryResponseError(
            f"OpenAI returned a non-completed response (status={status})."
        )
    response_text = getattr(response, "output_text", None)
    if not isinstance(response_text, str) or not response_text.strip():
        raise CommentaryResponseError(
            "OpenAI returned no structured commentary text."
        )
    commentary = parse_structured_commentary(
        response_text,
        allowed_fact_ids=request.fact_ids,
        allowed_members_by_fact_id={
            fact_identifier(fact): tuple(
                name
                for name in (
                    fact.subject_member_name,
                    fact.related_member_name,
                )
                if name
            )
            for fact in report.selected_facts
        },
    )
    return CommentaryGeneration(
        request=request,
        commentary=commentary,
        dry_run=False,
        generated_at=generated_at,
        response_id=getattr(response, "id", None),
        response_model=getattr(response, "model", None),
        usage=_response_usage(response),
    )


def _neutralize_discord_mentions(text: str) -> str:
    return (
        text.replace("@everyone", "@\u200beveryone")
        .replace("@here", "@\u200bhere")
        .replace("<@", "<@\u200b")
    )


def render_discord_markdown(commentary: StructuredCommentary) -> str:
    """Render validated commentary without posting it anywhere."""
    sections = [
        f"## {commentary.headline}",
        commentary.intro,
    ]
    if commentary.roasts:
        sections.append(
            "\n".join(
                f"- **{roast.member}:** {roast.text}\n  *Facts: {', '.join(roast.fact_ids)}*"
                for roast in commentary.roasts
            )
        )
    sections.append(commentary.closing)
    return _neutralize_discord_mentions("\n\n".join(sections))


def _split_long_block(block: str, limit: int) -> list[str]:
    pieces: list[str] = []
    remaining = block.strip()
    while len(remaining) > limit:
        split_at = max(
            remaining.rfind("\n", 0, limit + 1),
            remaining.rfind(" ", 0, limit + 1),
        )
        if split_at <= 0:
            split_at = limit
        pieces.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        pieces.append(remaining)
    return pieces


def chunk_discord_markdown(
    markdown: str, *, max_chars: int = DEFAULT_DISCORD_CHUNK_SIZE
) -> list[str]:
    """Split Markdown into Discord-safe chunks, preferring paragraph boundaries."""
    if not 20 <= max_chars <= 2000:
        raise ValueError("max_chars must be between 20 and Discord's 2000 limit.")
    blocks = [block.strip() for block in re.split(r"\n{2,}", markdown) if block.strip()]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        for piece in _split_long_block(block, max_chars):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


__all__ = [
    "COMMENTARY_PROMPT_VERSION",
    "TONE_PROFILES",
    "DEFAULT_OPENAI_MODEL",
    "CommentaryAPIError",
    "CommentaryConfig",
    "CommentaryConfigurationError",
    "CommentaryGeneration",
    "CommentaryRequest",
    "CommentaryResponseError",
    "CommentaryRoast",
    "CommentaryUsage",
    "MemberCommentaryContext",
    "StructuredCommentary",
    "build_commentary_request",
    "chunk_discord_markdown",
    "fact_identifier",
    "generate_weekly_commentary",
    "parse_structured_commentary",
    "render_discord_markdown",
]
