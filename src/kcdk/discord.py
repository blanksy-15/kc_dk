"""Secret-safe Discord incoming-webhook transport and message validation."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

from dotenv import dotenv_values
import httpx


DISCORD_CONTENT_LIMIT = 2000
DISCORD_EMBEDS_PER_MESSAGE = 10
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FIELDS_LIMIT = 25
DISCORD_EMBED_FIELD_NAME_LIMIT = 256
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_EMBED_FOOTER_LIMIT = 2048
DISCORD_EMBED_AUTHOR_LIMIT = 256
DISCORD_EMBED_TOTAL_LIMIT = 6000
DEFAULT_DISCORD_TIMEOUT_SECONDS = 15.0
ALLOWED_DISCORD_HOSTS = frozenset(
    {
        "discord.com",
        "www.discord.com",
        "discordapp.com",
        "canary.discord.com",
        "ptb.discord.com",
    }
)


class DiscordError(RuntimeError):
    """Base error for Discord configuration, validation, and transport."""


class DiscordConfigurationError(DiscordError):
    """Raised when a webhook setting is absent or invalid."""


class DiscordMessageError(DiscordError):
    """Raised before transport when a payload exceeds Discord limits."""


class DiscordTransportError(DiscordError):
    """Raised for a sanitized webhook transport failure."""


class StaleDiscordMessageError(DiscordTransportError):
    """Raised when a persisted webhook message no longer exists."""


@dataclass(frozen=True)
class DiscordConfig:
    weekly_webhook_url: str | None = field(default=None, repr=False)
    leaderboard_webhook_url: str | None = field(default=None, repr=False)
    timeout_seconds: float = DEFAULT_DISCORD_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise DiscordConfigurationError(
                "DISCORD_TIMEOUT_SECONDS must be greater than zero."
            )
        for name, value in (
            ("DISCORD_WEEKLY_WEBHOOK_URL", self.weekly_webhook_url),
            ("DISCORD_LEADERBOARD_WEBHOOK_URL", self.leaderboard_webhook_url),
        ):
            if value:
                _validate_webhook_url(value, name)

    @classmethod
    def from_env(
        cls,
        *,
        require_webhooks: bool = True,
        env_file: str | Path | None = None,
    ) -> "DiscordConfig":
        dotenv_path = Path(env_file) if env_file is not None else Path.cwd() / ".env"
        file_values = dotenv_values(dotenv_path)
        weekly = (
            os.getenv("DISCORD_WEEKLY_WEBHOOK_URL")
            or file_values.get("DISCORD_WEEKLY_WEBHOOK_URL")
            or ""
        ).strip() or None
        leaderboard = (
            os.getenv("DISCORD_LEADERBOARD_WEBHOOK_URL")
            or file_values.get("DISCORD_LEADERBOARD_WEBHOOK_URL")
            or ""
        ).strip() or None
        if require_webhooks and weekly is None:
            raise DiscordConfigurationError(
                "DISCORD_WEEKLY_WEBHOOK_URL is not configured in the ignored .env file."
            )
        if require_webhooks and leaderboard is None:
            raise DiscordConfigurationError(
                "DISCORD_LEADERBOARD_WEBHOOK_URL is not configured in the ignored .env file."
            )
        try:
            timeout = float(
                os.getenv("DISCORD_TIMEOUT_SECONDS")
                or file_values.get("DISCORD_TIMEOUT_SECONDS")
                or str(DEFAULT_DISCORD_TIMEOUT_SECONDS)
            )
        except ValueError as exc:
            raise DiscordConfigurationError(
                "DISCORD_TIMEOUT_SECONDS must be numeric."
            ) from exc
        return cls(
            weekly_webhook_url=weekly,
            leaderboard_webhook_url=leaderboard,
            timeout_seconds=timeout,
        )


def _validate_webhook_url(url: str, setting_name: str) -> None:
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    valid_path = len(path_parts) >= 4 and path_parts[-3] == "webhooks"
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in ALLOWED_DISCORD_HOSTS
        or not valid_path
    ):
        raise DiscordConfigurationError(
            f"{setting_name} is not a valid Discord incoming-webhook URL."
        )


@dataclass(frozen=True)
class DiscordMessage:
    content: str = ""
    embeds: tuple[dict[str, Any], ...] = ()

    def to_payload(self) -> dict[str, object]:
        validate_discord_message(self)
        payload: dict[str, object] = {"allowed_mentions": {"parse": []}}
        if self.content:
            payload["content"] = self.content
        if self.embeds:
            payload["embeds"] = [dict(embed) for embed in self.embeds]
        return payload


def _text_length(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key, "")
    return len(value) if isinstance(value, str) else 0


def validate_discord_message(message: DiscordMessage) -> None:
    if not message.content and not message.embeds:
        raise DiscordMessageError("A Discord message must have content or an embed.")
    if len(message.content) > DISCORD_CONTENT_LIMIT:
        raise DiscordMessageError("Discord message content exceeds 2000 characters.")
    if len(message.embeds) > DISCORD_EMBEDS_PER_MESSAGE:
        raise DiscordMessageError("A Discord message cannot contain more than 10 embeds.")

    combined = 0
    for embed in message.embeds:
        if _text_length(embed, "title") > DISCORD_EMBED_TITLE_LIMIT:
            raise DiscordMessageError("Discord embed title exceeds 256 characters.")
        if _text_length(embed, "description") > DISCORD_EMBED_DESCRIPTION_LIMIT:
            raise DiscordMessageError(
                "Discord embed description exceeds 4096 characters."
            )
        combined += _text_length(embed, "title") + _text_length(
            embed, "description"
        )
        fields = embed.get("fields", [])
        if not isinstance(fields, list) or len(fields) > DISCORD_EMBED_FIELDS_LIMIT:
            raise DiscordMessageError(
                "A Discord embed cannot contain more than 25 fields."
            )
        for item in fields:
            if not isinstance(item, Mapping):
                raise DiscordMessageError("Discord embed fields must be mappings.")
            if _text_length(item, "name") > DISCORD_EMBED_FIELD_NAME_LIMIT:
                raise DiscordMessageError(
                    "Discord embed field name exceeds 256 characters."
                )
            if _text_length(item, "value") > DISCORD_EMBED_FIELD_VALUE_LIMIT:
                raise DiscordMessageError(
                    "Discord embed field value exceeds 1024 characters."
                )
            combined += _text_length(item, "name") + _text_length(item, "value")
        footer = embed.get("footer", {})
        if isinstance(footer, Mapping):
            if _text_length(footer, "text") > DISCORD_EMBED_FOOTER_LIMIT:
                raise DiscordMessageError(
                    "Discord embed footer exceeds 2048 characters."
                )
            combined += _text_length(footer, "text")
        author = embed.get("author", {})
        if isinstance(author, Mapping):
            if _text_length(author, "name") > DISCORD_EMBED_AUTHOR_LIMIT:
                raise DiscordMessageError(
                    "Discord embed author exceeds 256 characters."
                )
            combined += _text_length(author, "name")
    if combined > DISCORD_EMBED_TOTAL_LIMIT:
        raise DiscordMessageError(
            "Combined Discord embed text exceeds 6000 characters."
        )


class DiscordTransport(Protocol):
    def create_message(self, webhook_url: str, message: DiscordMessage) -> str: ...

    def edit_message(
        self, webhook_url: str, message_id: str, message: DiscordMessage
    ) -> None: ...

    def delete_message(self, webhook_url: str, message_id: str) -> None: ...


class DiscordWebhookClient:
    """Minimal no-retry client for incoming webhook messages."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_DISCORD_TIMEOUT_SECONDS):
        if timeout_seconds <= 0:
            raise DiscordConfigurationError("Discord timeout must be positive.")
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, object] | None = None,
        stale_on_404: bool = False,
    ) -> httpx.Response:
        try:
            with httpx.Client(
                timeout=self.timeout_seconds, follow_redirects=False
            ) as client:
                response = client.request(method, url, json=payload)
        except Exception as exc:
            raise DiscordTransportError(
                f"Discord webhook request failed ({type(exc).__name__})."
            ) from None
        if response.status_code == 404 and stale_on_404:
            raise StaleDiscordMessageError(
                "The persisted Discord message no longer exists; no replacement was created."
            )
        if response.status_code >= 400:
            raise DiscordTransportError(
                f"Discord webhook request failed (HTTP {response.status_code})."
            )
        return response

    def create_message(self, webhook_url: str, message: DiscordMessage) -> str:
        _validate_webhook_url(webhook_url, "Discord webhook URL")
        separator = "&" if "?" in webhook_url else "?"
        response = self._request(
            "POST",
            f"{webhook_url}{separator}wait=true",
            payload=message.to_payload(),
        )
        try:
            message_id = str(response.json()["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise DiscordTransportError(
                "Discord created a message but returned no usable message ID."
            ) from None
        if not message_id:
            raise DiscordTransportError(
                "Discord created a message but returned no usable message ID."
            )
        return message_id

    def edit_message(
        self, webhook_url: str, message_id: str, message: DiscordMessage
    ) -> None:
        _validate_webhook_url(webhook_url, "Discord webhook URL")
        self._request(
            "PATCH",
            f"{webhook_url.rstrip('/')}/messages/{message_id}",
            payload=message.to_payload(),
            stale_on_404=True,
        )

    def delete_message(self, webhook_url: str, message_id: str) -> None:
        _validate_webhook_url(webhook_url, "Discord webhook URL")
        self._request(
            "DELETE",
            f"{webhook_url.rstrip('/')}/messages/{message_id}",
            stale_on_404=True,
        )


__all__ = [
    "DISCORD_CONTENT_LIMIT",
    "DISCORD_EMBEDS_PER_MESSAGE",
    "DISCORD_EMBED_TITLE_LIMIT",
    "DISCORD_EMBED_DESCRIPTION_LIMIT",
    "DISCORD_EMBED_FIELDS_LIMIT",
    "DISCORD_EMBED_FIELD_NAME_LIMIT",
    "DISCORD_EMBED_FIELD_VALUE_LIMIT",
    "DISCORD_EMBED_TOTAL_LIMIT",
    "DiscordConfig",
    "DiscordConfigurationError",
    "DiscordError",
    "DiscordMessage",
    "DiscordMessageError",
    "DiscordTransport",
    "DiscordTransportError",
    "DiscordWebhookClient",
    "StaleDiscordMessageError",
    "validate_discord_message",
]
