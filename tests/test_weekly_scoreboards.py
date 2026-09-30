from pathlib import Path

from kcdk.discord import DiscordConfig
from kcdk.leaderboard_delivery import LeaderboardConfig
from kcdk.persistence import connect_database, import_h2h
from kcdk.publishing import DiscordStateStore, publish_weekly_report
from kcdk.tournaments import import_tournament
from test_tournaments import sources


def test_historical_recap_uses_only_requested_week_and_replaces_text(sources, tmp_path, monkeypatch):
    import kcdk.weekly_scoreboards as scoreboards
    db = connect_database(':memory:')
    common = dict(season_name='Test', season_identifier='test')
    try:
        import_h2h(db, sources[3], sources[0], dk_fee_cents=100, week_number=1, **common)
        import_tournament(db, sources[1], sources[0], payout_path=sources[2], week_number=1, **common)
        sources[3].write_text(sources[3].read_text().replace(',90,', ',190,').replace(',80,', ',180,'))
        sources[1].write_text(sources[1].read_text().replace(',80.000004,', ',180.000004,').replace(',70,', ',170,'))
        import_h2h(db, sources[3], sources[0], dk_fee_cents=100, week_number=2, **common)
        import_tournament(db, sources[1], sources[0], payout_path=sources[2], week_number=2, **common)
        captured = []
        original = scoreboards._render
        def render(frame, path, **kwargs):
            captured.append((frame.copy(), kwargs['week_label']))
            return original(frame, path, **kwargs)
        monkeypatch.setattr(scoreboards, '_render', render)
        result = publish_weekly_report(db, season_identifier='test', week_label='Week 1',
            dry_run=True, discord_config=DiscordConfig(),
            state_store=DiscordStateStore(tmp_path / 'state.json'),
            leaderboard_config=LeaderboardConfig(output_directory=tmp_path / 'images'))
        assert len(captured) == 2
        h2h, tournament = [item[0] for item in captured]
        assert h2h.draftkings_fantasy_points.tolist() == [90, 80]
        assert h2h.top_lineup_player.str.contains(r"\d", regex=True).all()
        assert h2h.bottom_lineup_player.str.contains(r"\d", regex=True).all()
        assert tournament.entry_count.tolist() == [2]
        assert tournament.average_draftkings_fantasy_points.tolist() == [75]
        assert all(week == 'Week 1' for _, week in captured)
        assert 'Updated through Week 2' in result.kcdk_leaderboard_message.content
        message = result.weekly_message
        assert [attachment.filename for attachment in message.files] == ['weekly_h2h.png', 'weekly_tournament.png']
        fields = message.embeds[0]['fields']
        assert all(field['name'] == 'Data notes' for field in fields)
        assert message.embeds[1]['image']['url'] == 'attachment://weekly_h2h.png'
        assert message.embeds[2]['image']['url'] == 'attachment://weekly_tournament.png'
        assert message.replace_attachments
        assert not (tmp_path / 'state.json').exists()
    finally:
        db.close()
