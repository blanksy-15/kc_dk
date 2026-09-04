"""Prepare fresh leaderboard images and safely update owned Discord messages."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from io import BytesIO
import os
from pathlib import Path

from dotenv import dotenv_values
import pandas as pd
from PIL import Image

from .discord import (
    DiscordAttachment, DiscordConfigurationError, DiscordError, DiscordMessage,
    DiscordTransport, StaleDiscordMessageError,
    RejectedDiscordMessageError,
)
from .leaderboard_graphics import (
    BrandingAssetPaths, LeaderboardVisualConfig,
    render_kcdk_standings_png, render_tournament_performance_png,
)


@dataclass(frozen=True)
class LeaderboardConfig:
    mode: str = "image"
    max_file_bytes: int = 8 * 1024 * 1024
    output_directory: Path = Path("output/leaderboards")
    assets: BrandingAssetPaths = field(default_factory=BrandingAssetPaths)
    visual: LeaderboardVisualConfig = field(default_factory=LeaderboardVisualConfig)

    def __post_init__(self) -> None:
        if self.mode not in {"image", "text"}:
            raise DiscordConfigurationError("KCDK_LEADERBOARD_MODE must be image or text.")
        if self.max_file_bytes <= 0:
            raise DiscordConfigurationError("KCDK_LEADERBOARD_MAX_FILE_BYTES must be positive.")

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "LeaderboardConfig":
        values = dotenv_values(env_file or Path.cwd() / ".env")
        mode = (os.getenv("KCDK_LEADERBOARD_MODE")
                or values.get("KCDK_LEADERBOARD_MODE") or "image").strip().lower()
        try:
            limit = int(os.getenv("KCDK_LEADERBOARD_MAX_FILE_BYTES")
                        or values.get("KCDK_LEADERBOARD_MAX_FILE_BYTES") or 8 * 1024 * 1024)
        except ValueError:
            raise DiscordConfigurationError(
                "KCDK_LEADERBOARD_MAX_FILE_BYTES must be a whole number."
            ) from None
        return cls(mode=mode, max_file_bytes=limit)


@dataclass
class PreparedLeaderboard:
    target: str
    message: DiscordMessage
    fallback: DiscordMessage
    image_path: Path
    rendering_attempted: bool
    mode: str = "text"
    dimensions: tuple[int, int] | None = None
    file_size_bytes: int | None = None
    reason: str | None = None
    message_id: str | None = None
    attachment_count: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "mode": self.mode,
            "rendering_attempted": self.rendering_attempted,
            "image_path": str(self.image_path),
            "dimensions": list(self.dimensions) if self.dimensions else None,
            "file_size_bytes": self.file_size_bytes,
            "reason": self.reason,
            "message_id": self.message_id,
            "attachment_count": self.attachment_count,
        }


def prepare_leaderboard(
    target: str,
    frame: pd.DataFrame,
    fallback: DiscordMessage,
    *,
    season_name: str,
    through_week: str,
    weeks_completed: int,
    config: LeaderboardConfig,
    message_id: str | None = None,
) -> PreparedLeaderboard:
    """Always render anew; validate and snapshot the exact bytes sent to Discord."""
    filename, title, renderer = {
        "kcdk_leaderboard": ("kcdk_standings.png", "KCDK Season Standings",
                             render_kcdk_standings_png),
        "tournament_leaderboard": ("tournament_performance.png", "KCDK Tournament Performance",
                                   render_tournament_performance_png),
    }[target]
    fallback = replace(fallback, replace_attachments=True)
    prepared = PreparedLeaderboard(
        target, fallback, fallback, config.output_directory / filename,
        config.mode == "image", message_id=message_id,
    )
    if config.mode == "text":
        prepared.reason = "text mode configured"
        return prepared
    try:
        renderer(
            frame, prepared.image_path, season_label=season_name,
            week_label=through_week, weeks_completed=weeks_completed,
            config=config.visual, assets=config.assets,
        )
        if not prepared.image_path.is_file():
            prepared.reason = "rendered PNG is missing"
            return prepared
        prepared.file_size_bytes = prepared.image_path.stat().st_size
        if prepared.file_size_bytes == 0:
            prepared.reason = "rendered PNG is empty"
            return prepared
        # Inspect dimensions even when the file is too large, for dry-run diagnostics.
        with Image.open(prepared.image_path) as png:
            prepared.dimensions = png.size
            if png.format != "PNG" or png.size != (
                config.visual.canvas_width, config.visual.image_height(len(frame))
            ):
                prepared.reason = "PNG format or dimensions do not match the renderer"
                return prepared
        if prepared.file_size_bytes > config.max_file_bytes:
            prepared.reason = f"PNG exceeds configured limit of {config.max_file_bytes} bytes"
            return prepared
        with prepared.image_path.open("rb") as source:
            data = source.read(config.max_file_bytes + 1)
        if not data or len(data) > config.max_file_bytes:
            prepared.reason = "PNG changed size after validation"
            return prepared
        with Image.open(BytesIO(data)) as png:
            if png.format != "PNG" or png.size != prepared.dimensions:
                prepared.reason = "PNG changed format or dimensions after validation"
                return prepared
            png.verify()
        prepared.file_size_bytes = len(data)
        prepared.message = DiscordMessage(
            content=f"{title}\nUpdated through {through_week}",
            files=(DiscordAttachment(filename, data, f"{title}, through {through_week}"),),
            replace_attachments=True,
        )
        prepared.message.to_payload()
        prepared.mode = "image"
    except Exception as exc:
        # Renderer/file errors can contain arbitrary paths or configuration values.
        prepared.message = prepared.fallback
        prepared.reason = f"PNG rendering or validation failed ({type(exc).__name__})"
    return prepared


def deliver_leaderboard(
    client: DiscordTransport, webhook_url: str, prepared: PreparedLeaderboard,
) -> str:
    """Fall back once; never repeat a create whose outcome is ambiguous."""
    if prepared.message_id:
        try:
            client.edit_message(webhook_url, prepared.message_id, prepared.message)
        except StaleDiscordMessageError:
            # Never erase stale IDs or automatically create replacements.
            raise
        except DiscordError as exc:
            if prepared.mode != "image":
                raise
            prepared.reason = f"PNG edit failed: {exc}; attempting text fallback once"
            prepared.mode = "text"
            prepared.message = prepared.fallback
            try:
                client.edit_message(webhook_url, prepared.message_id, prepared.fallback)
            except DiscordError as fallback_error:
                raise DiscordError(
                    f"{prepared.reason}; text fallback failed: {fallback_error}"
                ) from None
    else:
        # A timeout/5xx may mean POST succeeded. Never risk a second create.
        try:
            prepared.message_id = client.create_message(webhook_url, prepared.message)
        except RejectedDiscordMessageError as exc:
            if prepared.mode != "image":
                raise
            prepared.reason = f"PNG create rejected: {exc}; attempting text fallback once"
            prepared.mode = "text"
            prepared.message = prepared.fallback
            try:
                prepared.message_id = client.create_message(webhook_url, prepared.fallback)
            except DiscordError as fallback_error:
                raise DiscordError(
                    f"{prepared.reason}; text fallback failed: {fallback_error}"
                ) from None
    prepared.attachment_count = getattr(client, "attachment_counts", {}).get(prepared.message_id)
    return prepared.message_id
