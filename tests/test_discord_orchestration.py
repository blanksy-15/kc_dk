import asyncio
from dataclasses import asdict, replace
import importlib.util
import json
from pathlib import Path
import random
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from kcdk.cli import main
from kcdk.discord import DiscordMessage, DiscordAttachment, DiscordTransportError
from kcdk.discord_orchestration import DiscordOrchestrator, H2H, TOURNAMENT, contest_urls, update_persistent_leaderboard
from kcdk.discord_service import BotTransport
from kcdk.general_drunk import ChannelMessage, ConversationGate, general_instructions, generate_general_reply
from kcdk.orchestration_config import GeneralDrunkConfig, OrchestrationConfig
from kcdk.orchestration_state import OrchestrationState
from kcdk.persistence import connect_database
from kcdk.result_orchestration import prepare_result, publish_result

spec = importlib.util.spec_from_file_location('discord_smoke', Path(__file__).parents[1] / 'scripts/smoke_discord_orchestration.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
MockTransport = smoke.MockTransport


@pytest.fixture
def setup(tmp_path):
    return smoke.fixtures(tmp_path)


def event(**kwargs):
    return replace(ChannelMessage('1', '1', '10', '99', 'My lineup is absolutely unbeatable.'), **kwargs)


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize('channel,label', [('20', 'H2H'), ('30', 'tournament')])
def test_authorized_announcement_persists_and_deduplicates(setup, channel, label):
    config, _, _ = setup
    transport = MockTransport()
    generator = Mock(side_effect=AssertionError('No model needed for announcement'))
    msg = event(channel_id=channel, content='Join https://www.draftkings.com/draft/contest/123')
    state = OrchestrationState(config.state_path)
    listener = DiscordOrchestrator(config, state, transport, generator)
    assert run(listener.on_message(msg)) == 'announced'
    state.close()
    state = OrchestrationState(config.state_path)
    listener = DiscordOrchestrator(config, state, transport, generator)
    assert run(listener.on_message(msg)) == 'duplicate'
    assert len(transport.sent) == 1 and transport.sent[0][0] == '10'
    assert label.lower() in transport.sent[0][2].content.lower()
    stored = state.get('announcement:1:1')
    assert stored['message_id'] == transport.sent[0][1]
    assert json.loads(stored['details'])['source_channel_id'] == channel
    assert not transport.edited
    generator.assert_not_called()
    state.close()


@pytest.mark.parametrize('changes', [
    {'author_id': '98'}, {'is_bot': True}, {'is_webhook': True}, {'guild_id': '2'},
    {'content': 'https://example.com/contest/123'},
    {'content': 'https://draftkings.com.evil.example/contest/123'},
    {'content': 'https://draftkings.com/contest/1 https://draftkings.com/contest/2'},
])
def test_announcement_rejections(setup, changes):
    config, _, _ = setup
    state = OrchestrationState()
    transport = MockTransport()
    generate = Mock()
    listener = DiscordOrchestrator(config, state, transport, generate)
    run(listener.on_message(replace(event(channel_id='20', content='https://draftkings.com/contest/123'), **changes)))
    assert not transport.sent
    generate.assert_not_called()
    state.close()


def test_optional_followup_once(setup):
    config, _, _ = setup
    config = replace(config, general_drunk=replace(config.general_drunk, announcement_followups=True))
    state = OrchestrationState()
    transport = MockTransport()
    generate = Mock(return_value='Try finding the enter button this time.')
    listener = DiscordOrchestrator(config, state, transport, generate, random_provider=lambda: 0)
    msg = event(channel_id='20', content='https://draftkings.com/contest/123')
    run(listener.on_message(msg))
    run(listener.on_message(msg))
    assert len(transport.sent) == 2
    assert transport.sent[1][3] == transport.sent[0][1]
    assert generate.call_count == 1
    state.close()


@pytest.mark.parametrize('settings,changes,rng', [
    ({'enabled': False}, {}, 0), ({}, {'is_bot': True}, 0), ({}, {'is_webhook': True}, 0),
    ({}, {'author_id': '555'}, 0), ({}, {'content': 'https://example.com/long-link'}, 0),
    ({}, {'content': 'ha'}, 0), ({}, {'content': '!show all my contest results'}, 0),
    ({'probability': .08}, {}, .5), ({}, {'channel_id': '88'}, 0),
])
def test_general_local_gates_never_generate(setup, settings, changes, rng):
    config, _, _ = setup
    config = replace(config, general_drunk=replace(config.general_drunk, **settings))
    generate = Mock(side_effect=AssertionError('Must not call OpenAI'))
    state = OrchestrationState()
    transport = MockTransport()
    listener = DiscordOrchestrator(config, state, transport, generate, own_user_id='555', random_provider=lambda: rng)
    run(listener.on_message(event(**changes)))
    generate.assert_not_called()
    assert not transport.sent
    state.close()


@pytest.mark.parametrize('settings,second_time,second_user', [
    ({'global_cooldown_seconds': 720}, 719, '100'),
    ({'per_user_cooldown_seconds': 1800}, 900, '99'),
    ({'max_replies_per_hour': 1}, 3599, '100'),
])
def test_cooldown_and_hourly_cap_before_generation(setup, settings, second_time, second_user):
    config, _, _ = setup
    config = replace(config, general_drunk=replace(config.general_drunk, **settings))
    now = [0]
    generate = Mock(return_value='Here comes the confidence again.')
    state = OrchestrationState()
    transport = MockTransport()
    listener = DiscordOrchestrator(config, state, transport, generate, clock=lambda: now[0], random_provider=lambda: 0)
    assert run(listener.on_message(event())) == 'replied'
    now[0] = second_time
    run(listener.on_message(event(id='2', author_id=second_user)))
    assert generate.call_count == 1
    state.close()


def test_general_context_bounds_reply_and_loop_prevention(setup):
    config, _, _ = setup
    config = replace(config, general_drunk=replace(config.general_drunk, context_message_count=3, max_context_chars_per_message=30))
    rng = iter([1, 1, 1, 1, 0])
    generate = Mock(return_value='That is certainly a lot of confidence.')
    state = OrchestrationState()
    transport = MockTransport()
    listener = DiscordOrchestrator(config, state, transport, generate, random_provider=lambda: next(rng))
    for i in range(5):
        run(listener.on_message(event(id=str(i), content='Some long conversation ' * 40)))
    context = generate.call_args.args[0]
    assert [m.id for m in context] == ['2', '3', '4']
    assert all(len(m.content) == 30 for m in context)
    assert transport.sent[0][3] == '4'
    run(listener.on_message(event(id='6', is_bot=True)))
    assert generate.call_count == 1
    state.close()


def test_seeded_rng_and_window_expiration():
    config = GeneralDrunkConfig(probability=.5, global_cooldown_seconds=0, per_user_cooldown_seconds=0, max_replies_per_hour=60)
    gates = [ConversationGate(config, random.Random(41).random, lambda: 0) for _ in range(2)]
    outcomes = [[g.reserve(event(id=str(i))) for i in range(20)] for g in gates]
    assert outcomes[0] == outcomes[1] and any(outcomes[0]) and not all(outcomes[0])
    now = [0]
    gate = ConversationGate(replace(config, probability=1, max_replies_per_hour=1), lambda: 0, lambda: now[0])
    assert gate.reserve(event())
    assert not gate.reserve(event())
    now[0] = 3600
    assert gate.reserve(event())


def test_general_prompt_and_bounded_provider_request(monkeypatch):
    from kcdk.commentary import CommentaryConfig
    monkeypatch.setattr(CommentaryConfig, 'from_env', lambda **kwargs: CommentaryConfig(api_key='test'))
    config = GeneralDrunkConfig(context_message_count=2, max_context_chars_per_message=20, max_output_tokens=100)
    client = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=SimpleNamespace(output_text='Confidence has entered the chat.', status='completed'))))
    generate_general_reply([event(id=str(i), content='x' * 1000) for i in range(8)], event(id='7'), config, client=client)
    args = client.responses.create.call_args.kwargs
    assert args['max_output_tokens'] == 100 and args['store'] is False
    assert 'Never invent KCDK statistics' in args['instructions']
    messages = json.loads(args['input'])['messages']
    assert len(messages) == 2 and all(len(m['content']) == 20 for m in messages)


def test_failed_general_attempt_is_charged_and_not_repeated(setup):
    config, _, _ = setup
    config = replace(config, general_drunk=replace(config.general_drunk, max_replies_per_hour=1))
    state = OrchestrationState()
    transport = MockTransport()
    generate = Mock(side_effect=ValueError('offline provider failure'))
    listener = DiscordOrchestrator(config, state, transport, generate, random_provider=lambda: 0)
    with pytest.raises(ValueError):
        run(listener.on_message(event()))
    run(listener.on_message(event()))
    run(listener.on_message(event(id='2')))
    assert generate.call_count == 1
    assert state.get('general-drunk:1:1')['status'] == 'uncertain'
    state.close()


@pytest.mark.parametrize('kind,history,board', [(H2H, '20', '40'), (TOURNAMENT, '30', '50')])
def test_independent_results_new_history_scoped_board_and_fact_isolation(setup, tmp_path, kind, history, board, monkeypatch):
    config, paths, payout = setup
    config = replace(config, results_drunk_enabled=True)
    db = connect_database()
    state = OrchestrationState()
    transport = MockTransport()
    # Force eligibility to exercise the handoff to both result personas.
    monkeypatch.setattr('kcdk.drunk_bot.select_drunk_bot_facts', lambda *a: SimpleNamespace(eligible=True))
    from kcdk.drunk_bot import DrunkBotConfig
    commissioner = Mock(return_value='Verified Commissioner recap.')
    drunk = Mock(return_value='A verified results joke.')
    outcomes = []
    try:
        for week in (1, 2):
            bundle = prepare_result(db, config, kind, paths[kind, week], week, tmp_path / 'images', payout_path=payout if kind == TOURNAMENT else None)
            for f in bundle.report.candidate_facts:
                assert (f.category == 'tournament') == (kind == TOURNAMENT)
            outcomes.append(run(publish_result(bundle, config, state, transport, commissioner=commissioner, results_drunk=drunk, drunk_config=DrunkBotConfig())))
        assert [x[0] for x in transport.sent] == [history, history, board, history, history]
        assert len(transport.edited) == 1 and transport.edited[0][0] == board
        assert outcomes[0]['leaderboard_message_id'] == outcomes[1]['leaderboard_message_id']
        assert outcomes[0]['historical_message_id'] != outcomes[1]['historical_message_id']
        assert commissioner.call_count == drunk.call_count == 2
        assert commissioner.call_args.args[0] is drunk.call_args.args[0]
        assert transport.sent[0][2].files[0].data.startswith(b'\x89PNG')
        rerun = run(publish_result(bundle, config, state, transport, commissioner=commissioner, results_drunk=drunk, drunk_config=DrunkBotConfig()))
        assert rerun['historical_action'] == 'skipped'
        assert commissioner.call_count == drunk.call_count == 2
        assert all(edit[0] == board for edit in transport.edited)
    finally:
        db.close()
        state.close()


def test_board_deleted_replaced_and_state_scopes(setup):
    config, _, _ = setup
    state = OrchestrationState()
    transport = MockTransport()
    message = DiscordMessage(content='Current standings')
    a = run(update_persistent_leaderboard(state, transport, config, H2H, message))
    b = run(update_persistent_leaderboard(state, transport, config, TOURNAMENT, message))
    del transport.messages['40', a]
    c = run(update_persistent_leaderboard(state, transport, config, H2H, message))
    assert c != a and state.leaderboard('1', '40', 'offline', H2H)['message_id'] == c
    assert state.leaderboard('1', '50', 'offline', TOURNAMENT)['message_id'] == b
    for scope in [('2', '40', 'offline', H2H), ('1', '41', 'offline', H2H), ('1', '40', 'next', H2H), ('1', '40', 'offline', TOURNAMENT)]:
        assert state.leaderboard(*scope) is None
    state.close()


def test_ambiguous_board_create_not_repeated(setup):
    config, _, _ = setup
    state = OrchestrationState()
    transport = MockTransport()
    async def fail(*args, **kwargs):
        raise DiscordTransportError('timeout after POST')
    transport.send = fail
    with pytest.raises(DiscordTransportError):
        run(update_persistent_leaderboard(state, transport, config, H2H, DiscordMessage(content='board')))
    with pytest.raises(ValueError, match='reconciliation'):
        run(update_persistent_leaderboard(state, MockTransport(), config, H2H, DiscordMessage(content='board')))
    state.close()


def test_shared_state_atomic_claim(setup):
    config, _, _ = setup
    a = OrchestrationState(config.state_path)
    b = OrchestrationState(config.state_path)
    assert a.claim('source', {})
    assert not b.claim('source', {})
    a.finish('source', '123')
    assert b.get('source')['message_id'] == '123'
    a.close()
    b.close()


@pytest.mark.parametrize('selected', [(), (H2H,), (TOURNAMENT,), (H2H, TOURNAMENT)])
def test_cli_dry_run_and_neither_no_live_writes(setup, tmp_path, selected, monkeypatch, capsys):
    config, paths, payout = setup
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(asdict(config)))
    monkeypatch.setattr('kcdk.discord_service.bot_token', Mock(side_effect=AssertionError('No network configuration in dry run')))
    args = ['results', '--config', str(config_path), '--week-number', '1', '--dry-run']
    for kind in selected:
        args += ['--h2h-csv' if kind == H2H else '--tournament-csv', str(paths[kind, 1])]
    if TOURNAMENT in selected:
        args += ['--tournament-payout', str(payout)]
    assert main(args) == 0
    assert not Path(config.database_path).exists()
    assert not Path(config.state_path).exists()
    text = capsys.readouterr().out
    if not selected:
        assert 'nothing to do' in text
    else:
        output = json.loads(text)
        assert [r['contest_type'] for r in output['results']] == list(selected)


def test_cli_live_cancel_before_writes(setup, tmp_path, monkeypatch):
    config, paths, _ = setup
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(asdict(config)))
    monkeypatch.setattr('builtins.input', lambda prompt: 'no')
    monkeypatch.setattr('kcdk.discord_service.bot_token', Mock(side_effect=AssertionError('No token before confirmation')))
    assert main(['results', '--config', str(path), '--h2h-csv', str(paths[H2H, 1]), '--week-number', '1', '--live']) == 0
    assert not Path(config.database_path).exists() and not Path(config.state_path).exists()


def test_bot_transport_payload_and_ownership(setup):
    config, _, _ = setup
    requests = []
    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == '/channels/40':
            return httpx.Response(200, json={'guild_id': '1', 'type': 0})
        if path == '/users/@me':
            return httpx.Response(200, json={'id': '999'})
        return httpx.Response(200, json={'id': '123', 'channel_id': '40', 'author': {'id': '999'}})
    async def scenario():
        async with httpx.AsyncClient(base_url='https://offline.invalid', transport=httpx.MockTransport(handler)) as client:
            transport = BotTransport(config, 'unused', client=client)
            message = DiscordMessage(content='board', files=(DiscordAttachment('test.png', b'png'),), replace_attachments=True)
            assert await transport.send('40', message) == '123'
            assert await transport.exists('40', '123')
            await transport.edit('40', '123', message)
            with pytest.raises(ValueError, match='Only persistent'):
                await transport.edit('20', '123', message)
    run(scenario())
    writes = [r for r in requests if r.method in {'POST', 'PATCH'}]
    assert len(writes) == 2
    for request in writes:
        body = request.content.decode()
        assert '"allowed_mentions": {"parse": [], "replied_user": false}' in body
        assert '"attachments": [{"id": 0' in body
        assert 'files[0]' in body


@pytest.mark.parametrize('status,code,missing', [(404, 10008, True), (404, 10003, False), (403, 50001, False), (429, 0, False), (500, 0, False)])
def test_transport_only_confirmed_missing_can_recreate(setup, status, code, missing):
    config, _, _ = setup
    def handler(request):
        if request.url.path == '/channels/40':
            return httpx.Response(200, json={'guild_id': '1', 'type': 0})
        return httpx.Response(status, json={'code': code})
    async def scenario():
        async with httpx.AsyncClient(base_url='https://offline.invalid', transport=httpx.MockTransport(handler)) as client:
            transport = BotTransport(config, 'unused', client=client)
            if missing:
                assert await transport.exists('40', '123') is False
            else:
                with pytest.raises(DiscordTransportError):
                    await transport.exists('40', '123')
    run(scenario())


def test_offline_smoke(tmp_path):
    result = run(smoke.simulate(tmp_path))
    assert result['status'] == 'passed' and result['persistent_edits'] == 2


@pytest.mark.parametrize('url,accepted', [
    ('https://www.draftkings.com/contest/draftteam/123?ref=abc', True),
    ('https://draftkings.com/contest/123', True),
    ('https://www.draftkings.com:443/draft/contest/123', True),
    ('https://draftkings.com:444/contest/123', False),
    ('https://draftkings.com:bad/contest/123', False),
    ('https://[broken/contest/123', False),
    ('https://other@draftkings.com/contest/123', False),
    ('https://sportsbook.draftkings.com/contest/123', False),
    ('https://draftkings.com/promotions/123', False),
])
def test_contest_url_validation(url, accepted):
    assert bool(contest_urls(url)) is accepted


@pytest.mark.parametrize('setting,value', [('probability', 'bad'), ('probability', True), ('probability', float('nan')),
    ('max_output_tokens', 99999), ('model', ''), ('context_message_count', 100), ('enabled', 'true')])
def test_general_configuration_bounds(setting, value):
    with pytest.raises(ValueError):
        GeneralDrunkConfig(**{setting: value})


def test_preflight_existing_databases_unchanged(setup, tmp_path, capsys):
    config, paths, payout = setup
    db = connect_database(config.database_path)
    prepare_result(db, config, H2H, paths[H2H, 1], 1, tmp_path / 'initial')
    db.close()
    state = OrchestrationState(config.state_path)
    state.save_leaderboard('1', '50', 'offline', TOURNAMENT, '456')
    state.close()
    before = {p: Path(p).read_bytes() for p in (config.database_path, config.state_path)}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(asdict(config)))
    assert main(['results', '--config', str(path), '--tournament-csv', str(paths[TOURNAMENT, 1]),
                 '--tournament-payout', str(payout), '--week-number', '1']) == 0
    assert all(Path(p).read_bytes() == data for p, data in before.items())
    assert json.loads(capsys.readouterr().out)['results'][0]['stored_leaderboard_message_id'] == '456'


def test_old_week_replay_uses_latest_board_and_filters_coexisting_tournament(setup, tmp_path):
    config, paths, payout = setup
    db = connect_database()
    try:
        for week in (1, 2):
            prepare_result(db, config, TOURNAMENT, paths[TOURNAMENT, week], week, tmp_path, payout_path=payout)
            prepare_result(db, config, H2H, paths[H2H, week], week, tmp_path)
        h = prepare_result(db, config, H2H, paths[H2H, 1], 1, tmp_path)
        t = prepare_result(db, config, TOURNAMENT, paths[TOURNAMENT, 1], 1, tmp_path, payout_path=payout)
        assert 'Week 2' in h.persistent_message.content and 'Week 2' in t.persistent_message.content
        assert h.report.week_label == t.report.week_label == 'Week 1'
        assert all(f.category != 'tournament' and 'tournament' not in f.tags for f in h.report.candidate_facts)
        assert all(f.category == 'tournament' for f in t.report.candidate_facts)
    finally:
        db.close()


def test_ambiguous_result_send_skips_repeat_model_and_post(setup, tmp_path):
    config, paths, _ = setup
    db = connect_database()
    state = OrchestrationState()
    transport = MockTransport()
    commissioner = Mock(return_value='A result recap.')
    async def fail(*args, **kwargs):
        raise DiscordTransportError('Ambiguous network outcome')
    transport.send = fail
    try:
        bundle = prepare_result(db, config, H2H, paths[H2H, 1], 1, tmp_path)
        with pytest.raises(DiscordTransportError):
            run(publish_result(bundle, config, state, transport, commissioner=commissioner))
        with pytest.raises(ValueError, match='Unresolved'):
            run(publish_result(bundle, config, state, transport, commissioner=commissioner))
        assert commissioner.call_count == 1
    finally:
        db.close()
        state.close()


def test_fetch_error_does_not_create_replacement(setup):
    config, _, _ = setup
    state = OrchestrationState()
    state.save_leaderboard('1', '40', 'offline', H2H, '123')
    transport = MockTransport()
    async def fail(*args):
        raise DiscordTransportError('No access')
    transport.exists = fail
    with pytest.raises(DiscordTransportError):
        run(update_persistent_leaderboard(state, transport, config, H2H, DiscordMessage(content='board')))
    assert not transport.sent and not transport.edited
    assert state.leaderboard('1', '40', 'offline', H2H)['message_id'] == '123'
    state.close()
