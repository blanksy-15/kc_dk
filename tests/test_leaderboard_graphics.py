from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw

from kcdk.analytics import (
    MOVEMENT_DOWN,
    MOVEMENT_NEW,
    MOVEMENT_SAME,
    MOVEMENT_UP,
    add_rank_movement,
    current_last_place_streaks,
    season_leaderboard,
    season_leaderboard_with_movement,
    tournament_leaderboard_with_movement,
)
from kcdk.leaderboard_graphics import (
    BrandingAssetPaths,
    KCDK_COLUMN_HEADERS,
    TOURNAMENT_COLUMN_HEADERS,
    LeaderboardCallout,
    LeaderboardVisualConfig,
    _composite_header_artwork,
    build_kcdk_visual_rows,
    centered_text_x,
    format_currency,
    format_movement,
    render_kcdk_standings_png,
    render_tournament_performance_png,
    resolve_fonts,
    text_width,
    truncate_text_to_width,
)
from kcdk.leaderboard_preview import build_preview_frames
from kcdk.persistence import connect_database, import_week


ROOT = Path(__file__).parents[1]
MOCK = ROOT / "data" / "mock"
MEMBERS = MOCK / "members.csv"


def _import_weeks(tmp_path, count):
    connection = connect_database(tmp_path / "season.sqlite")
    contest_ids = []
    for week in range(1, count + 1):
        import_week(
            connection,
            MOCK / f"season_week_{week}.csv",
            MEMBERS,
            season_name="Mock 2026",
            season_identifier="mock-2026",
            season_year=2026,
            week_number=week,
            contest_date=f"2026-09-{week:02d}",
        )
        contest_ids.append(
            int(
                connection.execute(
                    "SELECT id FROM contests WHERE week_number = ?", (week,)
                ).fetchone()[0]
            )
        )
    return connection, contest_ids


def _rows(data, count):
    if count <= len(data):
        return data.iloc[:count].copy()
    records = data.to_dict("records")
    while len(records) < count:
        record = dict(records[-1])
        rank = len(records) + 1
        record["display_name"] = f"Additional Preview Member {rank}"
        if "season_rank" in record:
            record["season_rank"] = rank
            record["average_finish"] = 8.0 + rank / 10
        if "tournament_rank" in record:
            record["tournament_rank"] = rank
        record["movement_status"] = MOVEMENT_NEW
        record["movement_delta"] = pd.NA
        records.append(record)
    return pd.DataFrame.from_records(records)


def test_rank_movement_up_down_same_and_new():
    previous = pd.DataFrame(
        {
            "display_name": ["Up", "Down", "Same", "Gone"],
            "season_rank": [5, 2, 3, 1],
        }
    )
    current = pd.DataFrame(
        {
            "display_name": ["Up", "Down", "Same", "New"],
            "season_rank": [2, 6, 3, 4],
        }
    )
    result = add_rank_movement(current, previous, rank_column="season_rank")
    by_name = result.set_index("display_name")

    assert by_name.loc["Up", "movement_status"] == MOVEMENT_UP
    assert by_name.loc["Up", "movement_delta"] == -3
    assert by_name.loc["Down", "movement_status"] == MOVEMENT_DOWN
    assert by_name.loc["Down", "movement_delta"] == 4
    assert by_name.loc["Same", "movement_status"] == MOVEMENT_SAME
    assert by_name.loc["New", "movement_status"] == MOVEMENT_NEW
    assert pd.isna(by_name.loc["New", "movement_delta"])
    assert format_movement(MOVEMENT_UP, 3) == "▲ 3"
    assert format_movement(MOVEMENT_DOWN, -4) == "▼ 4"
    assert format_movement(MOVEMENT_SAME, 0) == "—"
    assert format_movement(MOVEMENT_NEW, pd.NA) == "NEW"


def test_tied_rank_movement_uses_supplied_ranks():
    previous = pd.DataFrame(
        {"display_name": ["A", "B", "C"], "season_rank": [2, 2, 4]}
    )
    current = pd.DataFrame(
        {"display_name": ["A", "B", "C"], "season_rank": [1, 2, 2]}
    )
    result = add_rank_movement(current, previous, rank_column="season_rank")
    assert result["movement_delta"].tolist() == [-1, 0, -2]
    assert result["movement_status"].tolist() == [MOVEMENT_UP, MOVEMENT_SAME, MOVEMENT_UP]


def test_movement_uses_immediately_previous_contest_not_later_week(tmp_path):
    connection, contest_ids = _import_weeks(tmp_path, 3)
    try:
        through_two = season_leaderboard_with_movement(
            connection, "mock-2026", contest_ids[1]
        )
        expected = add_rank_movement(
            season_leaderboard(connection, "mock-2026", contest_ids[1]),
            season_leaderboard(connection, "mock-2026", contest_ids[0]),
            rank_column="season_rank",
        )
        later = season_leaderboard_with_movement(
            connection, "mock-2026", contest_ids[2]
        )
    finally:
        connection.close()

    pd.testing.assert_series_equal(
        through_two["movement_delta"], expected["movement_delta"]
    )
    assert through_two[
        ["display_name", "movement_delta"]
    ].to_dict("records") != later[
        ["display_name", "movement_delta"]
    ].to_dict("records")


def test_first_week_is_new_for_both_independent_leaderboards(tmp_path):
    connection, contest_ids = _import_weeks(tmp_path, 1)
    try:
        kcdk = season_leaderboard_with_movement(
            connection, "mock-2026", contest_ids[0]
        )
        tournament = tournament_leaderboard_with_movement(
            connection, "mock-2026", contest_ids[0]
        )
    finally:
        connection.close()
    assert set(kcdk["movement_status"]) == {MOVEMENT_NEW}
    assert set(tournament["movement_status"]) == {MOVEMENT_NEW}
    assert kcdk["movement_delta"].isna().all()
    assert tournament["movement_delta"].isna().all()


def test_kcdk_last_place_totals_and_current_streak_are_analytics_data(tmp_path):
    connection, contest_ids = _import_weeks(tmp_path, 4)
    try:
        leaderboard = season_leaderboard_with_movement(
            connection, "mock-2026", contest_ids[-1]
        )
        streaks = current_last_place_streaks(
            connection, "mock-2026", contest_ids[-1]
        )
    finally:
        connection.close()
    taylor = leaderboard.set_index("display_name").loc["Taylor Quinn"]
    assert taylor["last_place_finishes"] == 4
    assert streaks.iloc[0].to_dict() == {
        "display_name": "Taylor Quinn",
        "consecutive_weeks": 4,
    }


def test_member_absent_in_prior_contest_returns_as_new(tmp_path):
    week_two = pd.read_csv(MOCK / "season_week_2.csv")
    member_config = pd.read_csv(MEMBERS)
    absent_name = str(member_config.iloc[0]["draftkings_name"])
    week_two = week_two.loc[week_two["Entry Name"] != absent_name]
    changed_week = tmp_path / "week-two-with-absence.csv"
    week_two.to_csv(changed_week, index=False)

    connection = connect_database(tmp_path / "absence.sqlite")
    try:
        for week, source in (
            (1, MOCK / "season_week_1.csv"),
            (2, changed_week),
            (3, MOCK / "season_week_3.csv"),
        ):
            import_week(
                connection,
                source,
                MEMBERS,
                season_name="Mock 2026",
                season_identifier="mock-2026",
                week_number=week,
            )
        result = season_leaderboard_with_movement(connection, "mock-2026")
    finally:
        connection.close()

    member_display = member_config.set_index("draftkings_name").loc[
        absent_name, "display_name"
    ]
    row = result.loc[result["display_name"] == member_display].iloc[0]
    assert row["movement_status"] == MOVEMENT_NEW
    assert pd.isna(row["movement_delta"])


def test_dynamic_height_and_fixed_width_for_10_15_20_rows(tmp_path):
    kcdk, _, _, _ = build_preview_frames()
    expected_heights = {10: 946, 15: 1206, 20: 1466}
    for count, expected_height in expected_heights.items():
        result = render_kcdk_standings_png(
            _rows(kcdk, count),
            tmp_path / f"kcdk-{count}.png",
            season_label="2026 SEASON",
            week_label="Week 4",
            weeks_completed=4,
        )
        assert (result.width, result.height) == (1400, expected_height)
        assert result.last_row_bottom == result.footer_top
        assert result.footer_top + LeaderboardVisualConfig().footer_height <= (
            result.height - LeaderboardVisualConfig().outer_margin
        )


def test_long_name_uses_measured_ellipsis():
    config = LeaderboardVisualConfig()
    fonts = resolve_fonts(config)
    image = Image.new("RGB", (500, 100))
    draw = ImageDraw.Draw(image)
    available = 220
    rendered = truncate_text_to_width(
        draw,
        "This Is An Extremely Long KCDK Display Name That Must Not Push Numbers",
        fonts.bold,
        available,
    )
    assert rendered.endswith("…")
    assert text_width(draw, rendered, fonts.bold) <= available


def test_currency_preserves_zero_unknown_and_incomplete():
    assert format_currency(0.0) == "$0.00"
    assert format_currency(125.5) == "$125.50"
    assert format_currency(None) == "—"
    assert format_currency(0.0, complete=False) == "$0.00*"
    assert format_currency(None, complete=False) == "—"


def test_refined_headers_and_internal_kcdk_visual_model():
    assert KCDK_COLUMN_HEADERS == (
        "RK", "MOVE", "PLAYER", "AVG FIN", "W", "TOP 3", "AVG PTS", "LAST"
    )
    assert "WON" not in KCDK_COLUMN_HEADERS
    assert TOURNAMENT_COLUMN_HEADERS[-2] == "AVG PCTL"
    kcdk, _, _, _ = build_preview_frames()
    style = LeaderboardVisualConfig()
    rows, incomplete = build_kcdk_visual_rows(kcdk, style)
    assert rows[0]["values"][-1] == str(int(kcdk.iloc[0]["last_place_finishes"]))
    assert incomplete is False
    assert style.kcdk_column_widths[2] == 540
    assert style.tournament_column_widths[2] == 430
    assert sum(style.kcdk_column_widths) == 1312
    assert sum(style.tournament_column_widths) == 1312


def test_top_three_tints_do_not_change_row_height():
    style = LeaderboardVisualConfig()
    assert len(
        {
            style.first_place_row_color,
            style.second_place_row_color,
            style.third_place_row_color,
        }
    ) == 3
    assert style.image_height(4) - style.image_height(3) == style.row_height


def test_complete_movement_labels_center_within_rebalanced_column():
    style = LeaderboardVisualConfig()
    fonts = resolve_fonts(style)
    image = Image.new("RGB", (style.canvas_width, 100))
    draw = ImageDraw.Draw(image)
    bounds = (
        style.outer_margin + style.kcdk_column_widths[0],
        style.outer_margin
        + style.kcdk_column_widths[0]
        + style.kcdk_column_widths[1],
    )
    expected_center = sum(bounds) / 2
    for label in ("▲ 12", "▼ 12", "—", "NEW"):
        left = centered_text_x(draw, bounds, label, fonts.rank)
        assert left > bounds[0]
        assert left + text_width(draw, label, fonts.rank) < bounds[1]
        assert abs(
            left + text_width(draw, label, fonts.rank) / 2 - expected_center
        ) < 0.01


def test_renderers_work_without_authored_assets_and_produce_valid_png(tmp_path):
    kcdk, tournament, kcdk_callouts, tournament_callouts = build_preview_frames()
    missing_assets = BrandingAssetPaths(tmp_path / "missing-assets")
    first = render_kcdk_standings_png(
        kcdk,
        tmp_path / "kcdk.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        callouts=kcdk_callouts,
        assets=missing_assets,
    )
    second = render_tournament_performance_png(
        tournament,
        tmp_path / "tournament.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        callouts=tournament_callouts,
        assets=missing_assets,
    )
    assert first.loaded_assets == ()
    assert second.loaded_assets == ()
    assert first.procedural_skyline_used
    assert second.procedural_skyline_used
    assert first.header_artwork is None
    assert second.header_artwork is None
    assert not first.incomplete_prize_data
    assert second.incomplete_prize_data
    assert first.callout_count == 3
    assert second.callout_count == 3
    for result in (first, second):
        with Image.open(result.path) as image:
            assert image.format == "PNG"
            assert image.size == (1400, 1206)


def test_renderer_loads_mock_branding_assets_without_stretching(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logo = Image.new("RGBA", (300, 100), (0, 0, 0, 0))
    ImageDraw.Draw(logo).rectangle((20, 20, 280, 80), fill=(243, 113, 33, 255))
    logo.save(assets / "kcdk_logo.png")
    Image.new("RGB", (400, 900), (30, 32, 34)).save(
        assets / "leaderboard_background.png"
    )
    header = Image.new("RGBA", (900, 360), (0, 0, 0, 0))
    ImageDraw.Draw(header).rectangle((360, 80, 899, 300), fill=(55, 181, 111, 160))
    header.save(assets / "leaderboard_header.png")
    kcdk, _, _, _ = build_preview_frames()
    result = render_kcdk_standings_png(
        kcdk,
        tmp_path / "branded.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        assets=BrandingAssetPaths(assets),
    )
    assert set(result.loaded_assets) == {
        "kcdk_logo.png",
        "leaderboard_background.png",
        "leaderboard_header.png",
    }
    source_ratio = 300 / 100
    rendered_ratio = result.logo_rendered_size[0] / result.logo_rendered_size[1]
    assert abs(rendered_ratio - source_ratio) < 0.05
    assert result.logo_rendered_size == (126, 42)
    assert logo.getchannel("A").getextrema() == (0, 255)
    assert not result.procedural_skyline_used
    assert result.header_artwork is not None
    assert result.header_artwork.has_alpha
    source_header_ratio = 900 / 360
    rendered_header_ratio = (
        result.header_artwork.scaled_size[0]
        / result.header_artwork.scaled_size[1]
    )
    assert abs(rendered_header_ratio - source_header_ratio) < 0.01
    assert (
        result.header_artwork.destination[0]
        + result.header_artwork.scaled_size[0]
        == LeaderboardVisualConfig().canvas_width
        - LeaderboardVisualConfig().outer_margin
    )
    with Image.open(result.path) as image:
        assert image.size == (1400, 1206)


def test_header_artwork_preserves_transparent_pixels_when_composited():
    style = LeaderboardVisualConfig(
        canvas_width=200,
        outer_margin=20,
        header_height=80,
        header_artwork_scale=1.0,
        header_artwork_y_offset=0,
        header_artwork_opacity=1.0,
        kcdk_column_widths=(10, 10, 80, 10, 10, 10, 10, 20),
        tournament_column_widths=(10, 10, 70, 10, 10, 10, 20, 20),
    )
    base = Image.new("RGBA", (200, 120), (12, 13, 14, 255))
    source = Image.new("RGBA", (160, 80), (0, 0, 0, 0))
    ImageDraw.Draw(source).rectangle((80, 0, 159, 79), fill=(243, 113, 33, 255))

    placement = _composite_header_artwork(base, source, style)

    assert placement.has_alpha
    assert base.getpixel((40, 40)) == (12, 13, 14, 255)
    assert base.getpixel((160, 40)) == (243, 113, 33, 255)


def test_invalid_header_artwork_uses_procedural_fallback(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "leaderboard_header.png").write_text("not a PNG", encoding="utf-8")
    kcdk, _, _, _ = build_preview_frames()

    result = render_kcdk_standings_png(
        kcdk,
        tmp_path / "fallback.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        assets=BrandingAssetPaths(assets),
    )

    assert result.header_artwork is None
    assert result.procedural_skyline_used
    assert "leaderboard_header.png" not in result.loaded_assets


def test_authored_project_logo_loads_at_preserved_aspect_ratio(tmp_path):
    kcdk, _, _, _ = build_preview_frames()
    result = render_kcdk_standings_png(
        kcdk,
        tmp_path / "authored-logo.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        assets=BrandingAssetPaths(ROOT / "assets" / "branding"),
    )
    assert "kcdk_logo.png" in result.loaded_assets
    assert result.logo_rendered_size == (112, 110)
    source_ratio = 1545 / 1513
    rendered_ratio = result.logo_rendered_size[0] / result.logo_rendered_size[1]
    assert abs(rendered_ratio - source_ratio) < 0.01


def test_authored_project_header_renders_on_both_leaderboards(tmp_path):
    kcdk, tournament, kcdk_callouts, tournament_callouts = build_preview_frames()
    project_assets = BrandingAssetPaths(ROOT / "assets" / "branding")
    first = render_kcdk_standings_png(
        kcdk,
        tmp_path / "kcdk-header.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        callouts=kcdk_callouts,
        assets=project_assets,
    )
    second = render_tournament_performance_png(
        tournament,
        tmp_path / "tournament-header.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        callouts=tournament_callouts,
        assets=project_assets,
    )

    for result in (first, second):
        assert "leaderboard_header.png" in result.loaded_assets
        assert result.header_artwork is not None
        assert result.header_artwork.source_size == (1942, 809)
        assert not result.procedural_skyline_used
        assert result.path.is_file()


def test_missing_optional_footer_callout_renders_cleanly(tmp_path):
    kcdk, _, callouts, _ = build_preview_frames()
    result = render_kcdk_standings_png(
        kcdk,
        tmp_path / "two-callouts.png",
        season_label="2026 SEASON",
        week_label="Week 4",
        weeks_completed=4,
        callouts=callouts[:2],
    )
    assert result.callout_count == 2


def test_footer_callout_models_are_factual_and_currency_ready():
    _, _, kcdk_callouts, tournament_callouts = build_preview_frames()
    assert [item.label for item in kcdk_callouts] == [
        "Most Wins",
        "Most Podiums",
        "Last-Place Streak",
    ]
    assert [item.label for item in tournament_callouts] == [
        "Money Leader",
        "Most Cashes",
        "Best Cash",
    ]
    assert tournament_callouts[0].value_kind == "currency"
    assert tournament_callouts[2].value_kind == "currency"
    LeaderboardCallout("Optional", "Member", "No metric", "text")
