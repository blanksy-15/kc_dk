from dataclasses import replace
import json

import pytest

from kcdk.commentary import StructuredCommentary
from kcdk.discord import DiscordConfig, DiscordTransportError
from kcdk.drunk_bot import DrunkBotConfig
from kcdk.leaderboard_delivery import LeaderboardConfig
from kcdk.persistence import connect_database, import_h2h
from kcdk.publishing import DiscordStateStore, PublishingError, publish_weekly_report
from kcdk.tournaments import import_tournament


def hook(name):
    return f"https://discord.com/api/webhooks/123/{name}"


class Transport:
    def __init__(self, fail=None):
        self.created = []
        self.edited = []
        self.fail = fail

    def create_message(self, url, message):
        message.to_payload()
        if url == self.fail:
            raise DiscordTransportError("Request timed out")
        self.created.append((url, message))
        return str(len(self.created))

    def edit_message(self, url, message_id, message):
        message.to_payload()
        self.edited.append((url, message_id))


@pytest.fixture
def workflow(tmp_path):
    members = tmp_path / 'members.csv'
    members.write_text('draftkings_name,display_name,active\na,A,true\nb,B,true\n')
    h2h = tmp_path / 'h2h.csv'
    h2h.write_text('Rank,EntryId,EntryName,Points\n1,1,a,100\n2,2,b,90\n')
    tournament = tmp_path / 'tournament.csv'
    tournament.write_text('Rank,EntryId,EntryName,Points\n1,3,outsider,120\n2,4,b,80\n')
    payouts = tmp_path / 'payouts.csv'
    payouts.write_text('place_from,place_to,payout_cents\n1,1,1000\n')
    db = connect_database(':memory:')
    common = dict(season_name='Test', season_identifier='test', week_number=1)
    import_h2h(db, h2h, members, dk_fee_cents=100, **common)
    import_tournament(db, tournament, members, payout_path=payouts, **common)
    config = DiscordConfig(weekly_webhook_url=hook('commissioner'),
        leaderboard_webhook_url=hook('boards'), h2h_webhook_url=hook('h2h'),
        tournament_webhook_url=hook('tournament'))
    store = DiscordStateStore(tmp_path / 'state.json')
    def run(transport, **kwargs):
        return publish_weekly_report(db, season_identifier='test', week_label='Week 1',
            discord_config=kwargs.pop('discord_config', config), state_store=store,
            transport=transport, commentary=StructuredCommentary('Week 1', 'Praise', (), 'Check the leaderboards'),
            drunk_bot_config=DrunkBotConfig(enabled=False),
            leaderboard_config=LeaderboardConfig(mode='text'), **kwargs)
    yield run, store, config
    db.close()


def test_destinations_content_and_rerun(workflow):
    run, store, _ = workflow
    client = Transport()
    result = run(client)
    assert [url for url, _ in client.created] == [hook('h2h'), hook('tournament'),
        hook('commissioner'), hook('boards'), hook('boards')]
    h2h = json.dumps(client.created[0][1].to_payload())
    tournament = json.dumps(client.created[1][1].to_payload())
    assert '100.00 pts' in h2h and '$9.00' in h2h
    assert '80.00 avg pts' in tournament and '-$3.00' in tournament
    assert '100.00 pts' not in tournament
    assert set(result.contest_result_messages) == {'h2h_results', 'tournament_results'}
    assert '/api/webhooks/' not in store.path.read_text()
    run(client)
    assert len(client.created) == 5
    assert len(client.edited) == 2


def test_dry_run_lists_posts_without_sending_or_saving(workflow):
    run, store, _ = workflow
    client = Transport()
    result = run(client, dry_run=True)
    assert set(result.to_dict()['contest_results']) == {'h2h_results', 'tournament_results'}
    assert not client.created and not store.path.exists()


def test_partial_send_blocks_ambiguous_retry_and_preserves_completed_post(workflow):
    run, store, _ = workflow
    client = Transport(fail=hook('tournament'))
    with pytest.raises(PublishingError, match='inspect Discord'):
        run(client)
    saved = store.load()
    assert saved.result_posts['test:1:h2h_results']['status'] == 'posted'
    assert saved.result_posts['test:1:tournament_results']['status'] == 'posting'
    with pytest.raises(PublishingError, match='unresolved send'):
        run(client)
    assert len(client.created) == 1
    # Simulate operator reconciliation after confirming no tournament post exists.
    del saved.result_posts['test:1:tournament_results']
    store.save(saved)
    client.fail = None
    run(client)
    assert [url for url, _ in client.created].count(hook('h2h')) == 1
    assert len(client.created) == 5


def test_absent_optional_routes_preserve_existing_flow(workflow):
    run, _, config = workflow
    client = Transport()
    run(client, discord_config=replace(config, h2h_webhook_url=None, tournament_webhook_url=None))
    assert [url for url, _ in client.created] == [hook('commissioner'), hook('boards'), hook('boards')]


def test_optional_webhooks_load_without_exposing_secrets(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    path.write_text('DISCORD_H2H_WEBHOOK_URL=' + hook('h2h') + '\n'
                    'DISCORD_TOURNAMENT_WEBHOOK_URL=' + hook('tournament') + '\n')
    monkeypatch.delenv('DISCORD_H2H_WEBHOOK_URL', raising=False)
    monkeypatch.delenv('DISCORD_TOURNAMENT_WEBHOOK_URL', raising=False)
    config = DiscordConfig.from_env(env_file=path, require_webhooks=False)
    assert config.h2h_webhook_url == hook('h2h')
    assert config.tournament_webhook_url == hook('tournament')
    assert '/api/webhooks/' not in repr(config)
