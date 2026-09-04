"""Pillow-based branded leaderboard graphics, isolated from delivery code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import random
from typing import Iterable, Sequence

import pandas as pd
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .analytics import MOVEMENT_DOWN, MOVEMENT_NEW, MOVEMENT_SAME, MOVEMENT_UP


KCDK_COLUMN_HEADERS = (
    "RK",
    "MOVE",
    "PLAYER",
    "AVG FIN",
    "W",
    "TOP 3",
    "AVG PTS",
    "LAST",
)
TOURNAMENT_COLUMN_HEADERS = (
    "RK",
    "MOVE",
    "PLAYER",
    "WON",
    "CASHES",
    "AVG PTS",
    "AVG PCTL",
    "BEST CASH",
)


@dataclass(frozen=True)
class LeaderboardVisualConfig:
    canvas_width: int = 1400
    outer_margin: int = 44
    header_height: int = 158
    column_header_height: int = 56
    row_height: int = 52
    footer_height: int = 124
    title_font_size: int = 48
    subtitle_font_size: int = 25
    column_font_size: int = 19
    row_font_size: int = 24
    rank_font_size: int = 23
    footer_font_size: int = 19
    callout_label_font_size: int = 14
    callout_value_font_size: int = 20
    background_color: tuple[int, int, int] = (18, 19, 20)
    header_color: tuple[int, int, int] = (25, 26, 27)
    orange: tuple[int, int, int] = (243, 113, 33)
    green: tuple[int, int, int] = (55, 181, 111)
    decline_color: tuple[int, int, int] = (222, 93, 79)
    text_color: tuple[int, int, int] = (244, 244, 240)
    muted_text_color: tuple[int, int, int] = (166, 169, 170)
    row_even_color: tuple[int, int, int] = (29, 30, 31)
    row_odd_color: tuple[int, int, int] = (24, 25, 26)
    first_place_row_color: tuple[int, int, int] = (45, 39, 23)
    second_place_row_color: tuple[int, int, int] = (32, 34, 36)
    third_place_row_color: tuple[int, int, int] = (42, 31, 24)
    callout_panel_color: tuple[int, int, int] = (29, 30, 31)
    gold: tuple[int, int, int] = (212, 172, 55)
    silver: tuple[int, int, int] = (171, 180, 187)
    bronze: tuple[int, int, int] = (184, 115, 51)
    separator_color: tuple[int, int, int, int] = (255, 255, 255, 25)
    movement_new_label: str = "NEW"
    movement_same_label: str = "—"
    movement_up_symbol: str = "▲"
    movement_down_symbol: str = "▼"
    logo_max_width: int = 126
    logo_max_height: int = 110
    logo_left: int = 58
    logo_top: int = 68
    skyline_opacity: int = 34
    background_art_opacity: float = 0.28
    cell_padding: int = 12
    player_text_padding: int = 16
    callout_panel_gap: int = 14
    callout_accent_height: int = 3
    regular_font_path: str | None = None
    bold_font_path: str | None = None
    kcdk_column_widths: tuple[int, ...] = (65, 110, 540, 125, 65, 90, 170, 147)
    tournament_column_widths: tuple[int, ...] = (
        65,
        110,
        430,
        155,
        110,
        150,
        165,
        127,
    )

    def __post_init__(self) -> None:
        content_width = self.canvas_width - 2 * self.outer_margin
        if sum(self.kcdk_column_widths) != content_width:
            raise ValueError("KCDK column widths must fill the configured content width.")
        if sum(self.tournament_column_widths) != content_width:
            raise ValueError(
                "Tournament column widths must fill the configured content width."
            )
        if self.row_height < self.row_font_size + 8:
            raise ValueError("Row height is too small for the configured row font.")
        if not 0 <= self.skyline_opacity <= 255:
            raise ValueError("Skyline opacity must be between 0 and 255.")

    def image_height(self, row_count: int) -> int:
        if row_count < 1 or row_count > 20:
            raise ValueError("Leaderboard graphics support between 1 and 20 rows.")
        return (
            2 * self.outer_margin
            + self.header_height
            + self.column_header_height
            + row_count * self.row_height
            + self.footer_height
        )


@dataclass(frozen=True)
class BrandingAssetPaths:
    root: Path = Path("assets/branding")
    logo_filename: str = "kcdk_logo.png"
    background_filename: str = "leaderboard_background.png"
    skyline_filename: str = "skyline.png"

    @property
    def logo(self) -> Path:
        return self.root / self.logo_filename

    @property
    def background(self) -> Path:
        return self.root / self.background_filename

    @property
    def skyline(self) -> Path:
        return self.root / self.skyline_filename


@dataclass(frozen=True)
class RenderedLeaderboard:
    path: Path
    width: int
    height: int
    row_count: int
    font_name: str
    loaded_assets: tuple[str, ...]
    logo_rendered_size: tuple[int, int]
    callout_count: int
    incomplete_prize_data: bool
    last_row_bottom: int
    footer_top: int


@dataclass(frozen=True)
class LeaderboardCallout:
    """An explicitly supplied factual footer metric."""

    label: str
    subject: str
    value: object
    value_kind: str = "count"

    def __post_init__(self) -> None:
        if self.value_kind not in {"count", "currency", "weeks", "text"}:
            raise ValueError("Unsupported leaderboard callout value kind.")


@dataclass(frozen=True)
class _FontSet:
    regular_path: str | None
    bold_path: str | None
    name: str
    regular: ImageFont.FreeTypeFont | ImageFont.ImageFont
    bold: ImageFont.FreeTypeFont | ImageFont.ImageFont
    title: ImageFont.FreeTypeFont | ImageFont.ImageFont
    subtitle: ImageFont.FreeTypeFont | ImageFont.ImageFont
    column: ImageFont.FreeTypeFont | ImageFont.ImageFont
    rank: ImageFont.FreeTypeFont | ImageFont.ImageFont
    footer: ImageFont.FreeTypeFont | ImageFont.ImageFont
    callout_label: ImageFont.FreeTypeFont | ImageFont.ImageFont
    callout_value: ImageFont.FreeTypeFont | ImageFont.ImageFont


_REGULAR_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/bahnschrift.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)
_BOLD_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("C:/Windows/Fonts/bahnschrift.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)


def _first_font(configured: str | None, candidates: Sequence[Path]) -> str | None:
    paths = ([Path(configured)] if configured else []) + list(candidates)
    return str(next((path for path in paths if path.is_file()), "")) or None


def _font(path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if path:
        return ImageFont.truetype(path, size=size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def resolve_fonts(config: LeaderboardVisualConfig) -> _FontSet:
    regular_path = _first_font(config.regular_font_path, _REGULAR_FONT_CANDIDATES)
    bold_path = _first_font(config.bold_font_path, _BOLD_FONT_CANDIDATES)
    name = (
        Path(regular_path).stem
        if regular_path
        else "Pillow embedded default"
    )
    return _FontSet(
        regular_path=regular_path,
        bold_path=bold_path,
        name=name,
        regular=_font(regular_path, config.row_font_size),
        bold=_font(bold_path or regular_path, config.row_font_size),
        title=_font(bold_path or regular_path, config.title_font_size),
        subtitle=_font(regular_path, config.subtitle_font_size),
        column=_font(bold_path or regular_path, config.column_font_size),
        rank=_font(bold_path or regular_path, config.rank_font_size),
        footer=_font(regular_path, config.footer_font_size),
        callout_label=_font(
            bold_path or regular_path, config.callout_label_font_size
        ),
        callout_value=_font(
            bold_path or regular_path, config.callout_value_font_size
        ),
    )


def text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def truncate_text_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> str:
    """Ellipsize using actual Pillow glyph measurements, never character counts."""
    clean = " ".join(str(text).split())
    if text_width(draw, clean, font) <= max_width:
        return clean
    ellipsis = "…"
    if text_width(draw, ellipsis, font) > max_width:
        return ""
    low, high = 0, len(clean)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = clean[:middle].rstrip() + ellipsis
        if text_width(draw, candidate, font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return clean[:low].rstrip() + ellipsis


def format_currency(value: object, *, complete: bool = True) -> str:
    if value is None or pd.isna(value):
        return "—"
    amount = float(value)
    formatted = f"${amount:,.2f}"
    return formatted if complete else formatted + "*"


def format_movement(
    status: object,
    delta: object,
    config: LeaderboardVisualConfig | None = None,
) -> str:
    style = config or LeaderboardVisualConfig()
    normalized = str(status).casefold()
    if normalized == MOVEMENT_NEW:
        return style.movement_new_label
    if normalized == MOVEMENT_SAME:
        return style.movement_same_label
    amount = 0 if delta is None or pd.isna(delta) else abs(int(delta))
    if normalized == MOVEMENT_UP:
        return f"{style.movement_up_symbol} {amount}"
    if normalized == MOVEMENT_DOWN:
        return f"{style.movement_down_symbol} {amount}"
    return style.movement_new_label


def _movement_color(status: object, config: LeaderboardVisualConfig) -> tuple[int, int, int]:
    normalized = str(status).casefold()
    if normalized == MOVEMENT_UP:
        return config.green
    if normalized == MOVEMENT_DOWN:
        return config.decline_color
    if normalized == MOVEMENT_NEW:
        return config.orange
    return config.muted_text_color


def _draw_fallback_skyline(
    image: Image.Image, config: LeaderboardVisualConfig
) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    baseline = config.outer_margin + config.header_height - 9
    color = (*config.orange, config.skyline_opacity)
    x = config.canvas_width - 575
    buildings = (84, 52, 112, 68, 42, 96, 58, 126, 72, 48, 91, 61)
    widths = (38, 54, 42, 64, 46, 39, 60, 44, 52, 62, 43, 55)
    for height, width in zip(buildings, widths):
        draw.rectangle((x, baseline - height, x + width, baseline), fill=color)
        x += width - 5
    draw.line(
        (config.canvas_width - 610, baseline, config.canvas_width - 24, baseline),
        fill=(*config.orange, min(90, config.skyline_opacity * 2)),
        width=2,
    )
    image.alpha_composite(overlay)


def _draw_fallback_logo(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    config: LeaderboardVisualConfig,
    fonts: _FontSet,
) -> None:
    left, top = config.logo_left, config.logo_top
    size = min(config.logo_max_height, 100)
    draw.rounded_rectangle(
        (left, top, left + size, top + size),
        radius=18,
        outline=config.orange,
        width=4,
        fill=(29, 30, 31),
    )
    mark_font = _font(fonts.bold_path or fonts.regular_path, 31)
    label = "KCDK"
    width = text_width(draw, label, mark_font)
    box = draw.textbbox((0, 0), label, font=mark_font)
    height = box[3] - box[1]
    draw.text(
        (left + (size - width) / 2, top + (size - height) / 2 - box[1]),
        label,
        font=mark_font,
        fill=config.text_color,
    )


def _apply_branding(
    image: Image.Image,
    config: LeaderboardVisualConfig,
    assets: BrandingAssetPaths,
    fonts: _FontSet,
) -> tuple[tuple[str, ...], tuple[int, int]]:
    loaded: list[str] = []
    if assets.background.is_file():
        with Image.open(assets.background) as source:
            art = ImageOps.fit(source.convert("RGB"), image.size, method=Image.Resampling.LANCZOS)
        base = image.convert("RGB")
        image.paste(Image.blend(base, art, config.background_art_opacity).convert("RGBA"))
        loaded.append(assets.background.name)

    draw = ImageDraw.Draw(image)
    if assets.skyline.is_file():
        with Image.open(assets.skyline) as source:
            skyline = source.convert("RGBA")
            skyline.thumbnail((630, config.header_height - 18), Image.Resampling.LANCZOS)
        alpha = skyline.getchannel("A").point(
            lambda value: value * config.skyline_opacity // 255
        )
        skyline.putalpha(alpha)
        image.alpha_composite(
            skyline,
            (config.canvas_width - config.outer_margin - skyline.width,
             config.outer_margin + config.header_height - skyline.height),
        )
        loaded.append(assets.skyline.name)
    else:
        _draw_fallback_skyline(image, config)

    if assets.logo.is_file():
        with Image.open(assets.logo) as source:
            logo = source.convert("RGBA")
            logo.thumbnail(
                (config.logo_max_width, config.logo_max_height),
                Image.Resampling.LANCZOS,
            )
        image.alpha_composite(logo, (config.logo_left, config.logo_top))
        loaded.append(assets.logo.name)
        logo_size = logo.size
    else:
        _draw_fallback_logo(image, draw, config, fonts)
        size = min(config.logo_max_height, 100)
        logo_size = (size, size)
    return tuple(loaded), logo_size


def _new_canvas(
    height: int,
    config: LeaderboardVisualConfig,
    assets: BrandingAssetPaths,
    fonts: _FontSet,
) -> tuple[Image.Image, tuple[str, ...], tuple[int, int]]:
    image = Image.new("RGBA", (config.canvas_width, height), (*config.background_color, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (
            config.outer_margin,
            config.outer_margin,
            config.canvas_width - config.outer_margin,
            config.outer_margin + config.header_height,
        ),
        fill=(*config.header_color, 255),
    )
    rng = random.Random(1503)
    for _ in range(620):
        x = rng.randrange(config.canvas_width)
        y = rng.randrange(height)
        shade = rng.choice((24, 31, 37, 43))
        draw.point((x, y), fill=(shade, shade, shade, rng.randrange(18, 42)))
    loaded, logo_size = _apply_branding(image, config, assets, fonts)
    return image, loaded, logo_size


def _draw_header(
    draw: ImageDraw.ImageDraw,
    *,
    title: str,
    season_label: str,
    week_label: str,
    config: LeaderboardVisualConfig,
    fonts: _FontSet,
) -> None:
    text_left = config.logo_left + config.logo_max_width + 32
    draw.text(
        (text_left, config.outer_margin + 34),
        title,
        font=fonts.title,
        fill=config.text_color,
    )
    draw.text(
        (text_left + 2, config.outer_margin + 98),
        f"{season_label.upper()}  •  {week_label.upper()}",
        font=fonts.subtitle,
        fill=config.orange,
    )
    draw.rectangle(
        (
            config.outer_margin,
            config.outer_margin + config.header_height - 5,
            config.canvas_width - config.outer_margin,
            config.outer_margin + config.header_height,
        ),
        fill=config.orange,
    )


def _column_positions(config: LeaderboardVisualConfig, widths: Sequence[int]) -> list[tuple[int, int]]:
    positions: list[tuple[int, int]] = []
    x = config.outer_margin
    for width in widths:
        positions.append((x, x + width))
        x += width
    return positions


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int],
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    draw.text(
        (centered_text_x(draw, bounds, text, font), y),
        text,
        font=font,
        fill=fill,
    )


def centered_text_x(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> float:
    """Return the measured left coordinate that centers text in a cell."""
    width = text_width(draw, text, font)
    return (bounds[0] + bounds[1] - width) / 2


def _draw_right(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int],
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
    padding: int,
) -> None:
    width = text_width(draw, text, font)
    draw.text((bounds[1] - padding - width, y), text, font=font, fill=fill)


def _draw_column_headers(
    draw: ImageDraw.ImageDraw,
    headers: Sequence[str],
    positions: Sequence[tuple[int, int]],
    y: int,
    config: LeaderboardVisualConfig,
    fonts: _FontSet,
) -> None:
    draw.rectangle(
        (config.outer_margin, y, config.canvas_width - config.outer_margin, y + config.column_header_height),
        fill=(34, 35, 36),
    )
    text_y = y + (config.column_header_height - config.column_font_size) // 2 - 2
    for index, (header, bounds) in enumerate(zip(headers, positions)):
        if index == 2:
            draw.text(
                (bounds[0] + config.player_text_padding, text_y),
                header,
                font=fonts.column,
                fill=config.muted_text_color,
            )
        else:
            _draw_centered(
                draw, bounds, text_y, header, fonts.column, config.muted_text_color
            )


def _rank_color(rank: int, config: LeaderboardVisualConfig) -> tuple[int, int, int] | None:
    return {1: config.gold, 2: config.silver, 3: config.bronze}.get(rank)


def _draw_rows(
    draw: ImageDraw.ImageDraw,
    rows: Sequence[dict[str, object]],
    positions: Sequence[tuple[int, int]],
    y: int,
    config: LeaderboardVisualConfig,
    fonts: _FontSet,
) -> int:
    for row_index, row in enumerate(rows):
        top = y + row_index * config.row_height
        bottom = top + config.row_height
        base_color = {
            1: config.first_place_row_color,
            2: config.second_place_row_color,
            3: config.third_place_row_color,
        }.get(
            int(row["rank"]),
            config.row_even_color if row_index % 2 == 0 else config.row_odd_color,
        )
        draw.rectangle(
            (config.outer_margin, top, config.canvas_width - config.outer_margin, bottom),
            fill=base_color,
        )
        rank = int(row["rank"])
        medal = _rank_color(rank, config)
        if medal:
            draw.rectangle((config.outer_margin, top, config.outer_margin + 6, bottom), fill=medal)
        draw.line(
            (config.outer_margin, bottom - 1, config.canvas_width - config.outer_margin, bottom - 1),
            fill=config.separator_color,
            width=1,
        )
        text_y = top + (config.row_height - config.row_font_size) // 2 - 2
        _draw_centered(
            draw,
            positions[0],
            text_y,
            str(rank),
            fonts.rank,
            medal or config.text_color,
        )
        _draw_centered(
            draw,
            positions[1],
            text_y,
            str(row["movement"]),
            fonts.rank,
            _movement_color(row["movement_status"], config),
        )
        player_bounds = positions[2]
        max_player_width = (
            player_bounds[1] - player_bounds[0] - 2 * config.player_text_padding
        )
        player = truncate_text_to_width(
            draw, str(row["player"]), fonts.bold, max_player_width
        )
        draw.text(
            (player_bounds[0] + config.player_text_padding, text_y),
            player,
            font=fonts.bold,
            fill=config.text_color,
        )
        for column_index, value in enumerate(row["values"], start=3):
            fill = config.green if row.get("green_column") == column_index else config.text_color
            _draw_right(
                draw,
                positions[column_index],
                text_y,
                str(value),
                fonts.regular,
                fill,
                config.cell_padding,
            )
    return y + len(rows) * config.row_height


def _draw_footer(
    draw: ImageDraw.ImageDraw,
    *,
    top: int,
    week_label: str,
    member_count: int,
    weeks_completed: int,
    callouts: Sequence[LeaderboardCallout],
    incomplete: bool,
    config: LeaderboardVisualConfig,
    fonts: _FontSet,
) -> None:
    bottom = top + config.footer_height
    draw.rectangle(
        (config.outer_margin, top, config.canvas_width - config.outer_margin, bottom),
        fill=(20, 21, 22),
    )
    summary = (
        f"{week_label.upper()}   •   {member_count} KCDK MEMBERS   •   "
        f"{weeks_completed} WEEK{'S' if weeks_completed != 1 else ''} COMPLETED"
    )
    draw.text(
        (config.outer_margin + config.cell_padding, top + 16),
        summary,
        font=fonts.footer,
        fill=config.muted_text_color,
    )
    if callouts:
        panels = tuple(callouts[:3])
        content_left = config.outer_margin + config.cell_padding
        content_right = config.canvas_width - config.outer_margin - config.cell_padding
        total_gap = config.callout_panel_gap * (len(panels) - 1)
        panel_width = (content_right - content_left - total_gap) // len(panels)
        panel_top = top + 43
        panel_bottom = bottom - 10
        for index, callout in enumerate(panels):
            left = content_left + index * (panel_width + config.callout_panel_gap)
            right = content_right if index == len(panels) - 1 else left + panel_width
            draw.rounded_rectangle(
                (left, panel_top, right, panel_bottom),
                radius=6,
                fill=config.callout_panel_color,
            )
            accent = config.green if callout.value_kind == "currency" else config.orange
            draw.rectangle(
                (
                    left,
                    panel_top,
                    right,
                    panel_top + config.callout_accent_height,
                ),
                fill=accent,
            )
            draw.text(
                (left + 12, panel_top + 9),
                callout.label.upper(),
                font=fonts.callout_label,
                fill=config.muted_text_color,
            )
            value = _format_callout_value(callout)
            value = truncate_text_to_width(
                draw, value, fonts.callout_value, panel_width - 24
            )
            draw.text(
                (left + 12, panel_top + 30),
                value,
                font=fonts.callout_value,
                fill=config.text_color,
            )
    elif incomplete:
        draw.text(
            (config.outer_margin + config.cell_padding, top + 49),
            "* Winnings include incomplete known prize data.",
            font=fonts.callout_value,
            fill=config.muted_text_color,
        )


def _format_callout_value(callout: LeaderboardCallout) -> str:
    if callout.value_kind == "currency":
        metric = format_currency(callout.value)
    elif callout.value_kind == "weeks":
        count = _as_int(callout.value)
        metric = f"{count} Week{'s' if count != 1 else ''}"
    elif callout.value_kind == "text":
        metric = str(callout.value)
    else:
        metric = str(_as_int(callout.value))
    return f"{callout.subject} · {metric}"


def _as_int(value: object, default: int = 0) -> int:
    return default if value is None or pd.isna(value) else int(value)


def _as_float(value: object, default: float = 0.0) -> float:
    return default if value is None or pd.isna(value) else float(value)


def _prize_complete(value: object) -> bool:
    return False if value is None or pd.isna(value) else bool(value)


def build_kcdk_visual_rows(
    data: pd.DataFrame, config: LeaderboardVisualConfig
) -> tuple[list[dict[str, object]], bool]:
    rows: list[dict[str, object]] = []
    for record in data.to_dict("records"):
        rows.append(
            {
                "rank": _as_int(record.get("season_rank")),
                "movement": format_movement(
                    record.get("movement_status"), record.get("movement_delta"), config
                ),
                "movement_status": record.get("movement_status", MOVEMENT_NEW),
                "player": record.get("display_name", ""),
                "values": (
                    f"{_as_float(record.get('average_finish')):.2f}",
                    str(_as_int(record.get("wins"))),
                    str(_as_int(record.get("podium_finishes"))),
                    f"{_as_float(record.get('average_draftkings_fantasy_points')):.2f}",
                    str(_as_int(record.get("last_place_finishes"))),
                ),
                "green_column": None,
            }
        )
    return rows, False


def build_tournament_visual_rows(
    data: pd.DataFrame, config: LeaderboardVisualConfig
) -> tuple[list[dict[str, object]], bool]:
    rows: list[dict[str, object]] = []
    incomplete = False
    for record in data.to_dict("records"):
        complete = _prize_complete(record.get("prize_data_complete", True))
        incomplete = incomplete or not complete
        rows.append(
            {
                "rank": _as_int(record.get("tournament_rank")),
                "movement": format_movement(
                    record.get("movement_status"), record.get("movement_delta"), config
                ),
                "movement_status": record.get("movement_status", MOVEMENT_NEW),
                "player": record.get("display_name", ""),
                "values": (
                    format_currency(record.get("total_money_won"), complete=complete),
                    (
                        "—"
                        if record.get("cashes") is None or pd.isna(record.get("cashes"))
                        else str(int(record["cashes"]))
                    ),
                    f"{_as_float(record.get('average_draftkings_fantasy_points')):.2f}",
                    f"{_as_float(record.get('average_tournament_percentile')):.1f}%",
                    format_currency(
                        record.get("largest_single_tournament_win"), complete=complete
                    ),
                ),
                "green_column": 3,
            }
        )
    return rows, incomplete


def _render(
    data: pd.DataFrame,
    output_path: str | Path,
    *,
    title: str,
    headers: Sequence[str],
    widths: Sequence[int],
    row_builder,
    season_label: str,
    week_label: str,
    weeks_completed: int,
    callouts: Iterable[LeaderboardCallout] = (),
    config: LeaderboardVisualConfig | None = None,
    assets: BrandingAssetPaths | None = None,
) -> RenderedLeaderboard:
    style = config or LeaderboardVisualConfig()
    asset_paths = assets or BrandingAssetPaths()
    if data.empty:
        raise ValueError("Cannot render an empty leaderboard.")
    resolved_callouts = tuple(callouts)[:3]
    height = style.image_height(len(data))
    fonts = resolve_fonts(style)
    image, loaded, logo_size = _new_canvas(height, style, asset_paths, fonts)
    draw = ImageDraw.Draw(image)
    _draw_header(
        draw,
        title=title,
        season_label=season_label,
        week_label=week_label,
        config=style,
        fonts=fonts,
    )
    positions = _column_positions(style, widths)
    table_top = style.outer_margin + style.header_height
    _draw_column_headers(draw, headers, positions, table_top, style, fonts)
    rows, incomplete = row_builder(data, style)
    row_top = table_top + style.column_header_height
    last_row_bottom = _draw_rows(draw, rows, positions, row_top, style, fonts)
    footer_top = last_row_bottom
    _draw_footer(
        draw,
        top=footer_top,
        week_label=week_label,
        member_count=len(rows),
        weeks_completed=weeks_completed,
        callouts=resolved_callouts,
        incomplete=incomplete,
        config=style,
        fonts=fonts,
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(destination, format="PNG", optimize=True)
    return RenderedLeaderboard(
        path=destination,
        width=style.canvas_width,
        height=height,
        row_count=len(rows),
        font_name=fonts.name,
        loaded_assets=loaded,
        logo_rendered_size=logo_size,
        callout_count=len(resolved_callouts),
        incomplete_prize_data=incomplete,
        last_row_bottom=last_row_bottom,
        footer_top=footer_top,
    )


def render_kcdk_standings_png(
    leaderboard: pd.DataFrame,
    output_path: str | Path,
    *,
    season_label: str,
    week_label: str,
    weeks_completed: int,
    callouts: Iterable[LeaderboardCallout] = (),
    config: LeaderboardVisualConfig | None = None,
    assets: BrandingAssetPaths | None = None,
) -> RenderedLeaderboard:
    style = config or LeaderboardVisualConfig()
    return _render(
        leaderboard,
        output_path,
        title="KCDK SEASON STANDINGS",
        headers=KCDK_COLUMN_HEADERS,
        widths=style.kcdk_column_widths,
        row_builder=build_kcdk_visual_rows,
        season_label=season_label,
        week_label=week_label,
        weeks_completed=weeks_completed,
        callouts=callouts,
        config=style,
        assets=assets,
    )


def render_tournament_performance_png(
    leaderboard: pd.DataFrame,
    output_path: str | Path,
    *,
    season_label: str,
    week_label: str,
    weeks_completed: int,
    callouts: Iterable[LeaderboardCallout] = (),
    config: LeaderboardVisualConfig | None = None,
    assets: BrandingAssetPaths | None = None,
) -> RenderedLeaderboard:
    style = config or LeaderboardVisualConfig()
    return _render(
        leaderboard,
        output_path,
        title="KCDK TOURNAMENT PERFORMANCE",
        headers=TOURNAMENT_COLUMN_HEADERS,
        widths=style.tournament_column_widths,
        row_builder=build_tournament_visual_rows,
        season_label=season_label,
        week_label=week_label,
        weeks_completed=weeks_completed,
        callouts=callouts,
        config=style,
        assets=assets,
    )


__all__ = [
    "BrandingAssetPaths",
    "KCDK_COLUMN_HEADERS",
    "LeaderboardCallout",
    "LeaderboardVisualConfig",
    "RenderedLeaderboard",
    "TOURNAMENT_COLUMN_HEADERS",
    "build_kcdk_visual_rows",
    "build_tournament_visual_rows",
    "centered_text_x",
    "format_currency",
    "format_movement",
    "render_kcdk_standings_png",
    "render_tournament_performance_png",
    "resolve_fonts",
    "truncate_text_to_width",
]
