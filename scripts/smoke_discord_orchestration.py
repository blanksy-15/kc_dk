"""Offline end-to-end orchestration: temporary data, mock Discord, stub models."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from kcdk.discord_orchestration import DiscordOrchestrator, H2H, TOURNAMENT
from kcdk.general_drunk import ChannelMessage
from kcdk.orchestration_config import GeneralDrunkConfig, OrchestrationConfig
from kcdk.orchestration_state import OrchestrationState
from kcdk.persistence import connect_database
from kcdk.result_orchestration import prepare_result, publish_result


class MockTransport:
    def __init__(self):
        self.messages = {}
        self.sent = []
        self.edited = []
        self.next_id = 1000

    async def send(self, channel_id, message, *, reply_to=None):
        message.to_payload()
        self.next_id += 1
        message_id = str(self.next_id)
        self.messages[channel_id, message_id] = message
        self.sent.append((channel_id, message_id, message, reply_to))
        return message_id

    async def exists(self, channel_id, message_id):
        return (channel_id, message_id) in self.messages

    async def edit(self, channel_id, message_id, message):
        assert (channel_id, message_id) in self.messages
        message.to_payload()
        self.messages[channel_id, message_id] = message
        self.edited.append((channel_id, message_id, message))


def fixtures(directory):
    directory = Path(directory)
    members = directory / 'members.csv'
    members.write_text('draftkings_name,display_name,active\nbschobs,Brian,true\nother,Other,true\n')
    payout = directory / 'payout.csv'
    payout.write_text('place_from,place_to,payout_cents\n1,1,1000\n2,2,500\n')
    paths = {}
    for week in (1, 2):
        h2h = directory / f'h2h{week}.csv'
        h2h.write_text('Rank,EntryId,EntryName,Points,Lineup\n'
                      f'1,{week}01,other,90,QB Alpha\n2,{week}02,bschobs,80,QB Beta\n')
        tournament = directory / f'tournament{week}.csv'
        tournament.write_text('Rank,EntryId,EntryName,Points,Lineup\n'
            f'1,{week}11,outsider,100,CPT Alpha FLEX Beta\n'
            f'2,{week}12,bschobs (1/2),90,CPT Alpha FLEX Beta\n'
            f'3,{week}13,bschobs (2/2),80,CPT Beta FLEX Alpha\n')
        paths[H2H, week] = h2h
        paths[TOURNAMENT, week] = tournament
    config = OrchestrationConfig('1', '10', '20', '30', '40', '50', ('99',),
        season_identifier='offline', season_name='Offline KCDK', members_path=str(members),
        database_path=str(directory / 'analytics.sqlite'), state_path=str(directory / 'state.sqlite'),
        h2h_dk_fee_cents=100, results_drunk_enabled=False,
        general_drunk=GeneralDrunkConfig(probability=1, global_cooldown_seconds=0, per_user_cooldown_seconds=0))
    return config, paths, payout


async def simulate(directory):
    config, paths, payout = fixtures(directory)
    db = connect_database(config.database_path)
    state = OrchestrationState(config.state_path)
    transport = MockTransport()
    model_calls = []
    def general(context, trigger, settings):
        model_calls.append(('GENERAL_DRUNK', trigger.id))
        return 'That confidence deserves its own parking space.'
    def commissioner(report):
        categories = {f.category for f in report.candidate_facts}
        kind = TOURNAMENT if categories == {'tournament'} else H2H
        assert (categories == {'tournament'}) if kind == TOURNAMENT else 'tournament' not in categories
        model_calls.append((kind, report.week_label))
        return f'Offline Commissioner: {kind}, {report.week_label}.'
    listener = DiscordOrchestrator(config, state, transport, general, random_provider=lambda: 0)
    try:
        h = ChannelMessage('1', '1', '20', '99', 'https://www.draftkings.com/draft/contest/123')
        assert await listener.on_message(h) == 'announced'
        assert await listener.on_message(h) == 'duplicate'
        assert await listener.on_message(replace(h, id='2', channel_id='30')) == 'announced'
        assert await listener.on_message(replace(h, id='3', channel_id='10', content='This lineup is absolutely unbeatable.')) == 'replied'
        assert transport.sent[-1][3] == '3'
        publications = []
        for kind in (H2H, TOURNAMENT):
            for week in (1, 2):
                bundle = prepare_result(db, config, kind, paths[kind, week], week, Path(directory) / 'images',
                                        payout_path=payout if kind == TOURNAMENT else None)
                publications.append(await publish_result(bundle, config, state, transport, commissioner=commissioner))
        assert [x[0] for x in transport.sent] == ['10', '10', '10', '20', '40', '20', '30', '50', '30']
        assert [x[0] for x in transport.edited] == ['40', '50']
        assert publications[0]['leaderboard_message_id'] == publications[1]['leaderboard_message_id']
        assert publications[2]['leaderboard_message_id'] == publications[3]['leaderboard_message_id']
        assert len({p['historical_message_id'] for p in publications}) == 4
        return {'status': 'passed', 'mock_messages_created': len(transport.sent),
                'persistent_edits': len(transport.edited), 'stub_model_calls': len(model_calls),
                'real_discord_calls': 0, 'real_openai_calls': 0, 'publications': publications}
    finally:
        db.close()
        state.close()


def main():
    with TemporaryDirectory(prefix='kcdk-discord-smoke-') as directory:
        print(json.dumps(asyncio.run(simulate(directory)), indent=2))


if __name__ == '__main__':
    main()
