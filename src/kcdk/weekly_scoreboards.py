"""Week-specific scoreboards for recap attachments, independent of season totals."""
from io import BytesIO

from PIL import Image

from .analytics import MOVEMENT_SAME
from .discord import DiscordAttachment
from .leaderboard_graphics import _render, build_tournament_visual_rows, format_currency


def h2h_rows(frame, config):
    rows = []
    for row in frame.itertuples(index=False):
        rows.append({
            'rank': int(row.kcdk_finish), 'player': row.display_name,
            'movement': f'{row.draftkings_fantasy_points:.2f}',
            'movement_status': MOVEMENT_SAME,
            'values': (
                row.top_lineup_player,
                row.bottom_lineup_player,
                format_currency(row.prize_cents / 100),
            ),
            'green_column': None,
        })
    return rows, False


def tournament_rows(frame, config):
    rows, incomplete = build_tournament_visual_rows(frame, config)
    for row in rows:
        row['movement'] = row['values'][0]
        row['movement_status'] = MOVEMENT_SAME
        row['values'] = row['values'][1:]
    return rows, incomplete


def prepare_weekly_scoreboards(h2h, tournament, *, h2h_lineups, season_name, week_label, config):
    """Inputs must already be filtered to the requested week. Text mode is supported."""
    if (config.mode != 'image' or h2h.empty or 'contest_type' not in h2h
            or not h2h.contest_type.eq('KCDK_H2H').all()):
        return ()
    from dataclasses import replace
    style = replace(config.visual, column_font_size=17, row_font_size=20)
    h2h = h2h.merge(
        h2h_lineups[["display_name", "top_lineup_player", "bottom_lineup_player"]],
        on="display_name", validate="one_to_one",
    )
    h2h[["top_lineup_player", "bottom_lineup_player"]] = h2h[
        ["top_lineup_player", "bottom_lineup_player"]
    ].fillna("—")
    specifications = [(
        h2h, 'weekly_h2h.png', 'KCDK WEEKLY H2H SCOREBOARD',
        ('RK', 'POINTS', 'MEMBER', 'TOP PLAYER', 'BOTTOM PLAYER', 'PAYOUT'),
        (55, 105, 230, 330, 440, 152), h2h_rows,
    )]
    if not tournament.empty:
        specifications.append((
            tournament, 'weekly_tournament.png', 'KCDK WEEKLY TOURNAMENT',
            ('RK', 'ENT', 'PLAYER', 'GROSS', 'FEES', 'NET', 'AVG PTS', 'AVG PCTL', 'BEST RK', 'CASH %'),
            (55, 85, 300, 135, 100, 135, 140, 130, 130, 102), tournament_rows,
        ))
    attachments = []
    for frame, filename, title, headers, widths, builder in specifications:
        path = config.output_directory / 'weekly' / filename
        _render(frame, path, title=title, headers=headers, widths=widths,
                row_builder=builder, season_label=season_name, week_label=week_label,
                weeks_completed=1, config=style, assets=config.assets, weekly_only=True)
        data = path.read_bytes()
        if len(data) > config.max_file_bytes:
            raise ValueError('Weekly scoreboard exceeds the configured attachment limit')
        with Image.open(BytesIO(data)) as png:
            if png.format != 'PNG' or png.size != (style.canvas_width, style.image_height(len(frame))):
                raise ValueError('Invalid weekly scoreboard image')
            png.verify()
        description = f'{title} - {week_label} only'
        attachments.append(DiscordAttachment(filename, data, description))
    if sum(len(item.data) for item in attachments) > config.max_file_bytes:
        raise ValueError('Combined weekly scoreboards exceed the configured attachment limit')
    return tuple(attachments)
