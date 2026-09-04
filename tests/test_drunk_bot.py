from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kcdk.commentary import CommentaryConfig, CommentaryResponseError, CommentaryAPIError, fact_identifier
from kcdk.commentary_prompt import commentary_instructions
from kcdk.drunk_bot_prompt import drunk_bot_instructions, DRUNK_BOT_PROMPT_VERSION
from kcdk.discord import DiscordConfig, DiscordTransportError, RejectedDiscordMessageError
from kcdk.drunk_bot import DrunkBotConfig, select_drunk_bot_facts
from kcdk.drunk_bot_commentary import (
    generate_drunk_bot_commentary, parse_drunk_bot_commentary, render_drunk_bot_message,
)
from kcdk.drunk_bot_publishing import DrunkBotRecord
from kcdk.facts import Fact, build_weekly_fact_report
from kcdk.leaderboard_delivery import LeaderboardConfig
from kcdk.publishing import DiscordState, DiscordStateStore, PublicationRecord, publish_weekly_report
from kcdk.weekly_runner import LocalWeeklyConfig, build_preflight
from test_discord import FakeTransport, _discord_config, commentary, season_db, MOCK

DRUNK_URL = "https://discord.com/api/webhooks/333/test-drunk-token"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("unexpected network"))
    monkeypatch.setattr("kcdk.drunk_bot_commentary._new_openai_client",
                        lambda *a, **k: pytest.fail("unexpected paid client"))
    monkeypatch.delenv("DRUNK_BOT_OPENAI_MODEL", raising=False)


@pytest.fixture
def report(season_db):
    return build_weekly_fact_report(season_db, "mock-2026", max_facts=8)


def with_facts(report, *facts):
    return replace(report, candidate_facts=tuple(facts), selected_facts=tuple(facts))


def negative_fact(**overrides):
    values = dict(
        fact_type="consecutive_last_places", category="streak", priority=98,
        summary="Taylor Quinn has 4 consecutive last-place finishes through Week 4.",
        season_identifier="mock-2026", contest_id=4, week_label="Week 4",
        subject_member_name="Taylor Quinn", values={"consecutive_weeks": 4},
    )
    values.update(overrides)
    return Fact(**values)


class ModelClient:
    def __init__(self, payload=None):
        self.responses = self
        self.calls = []
        self.payload = payload

    def create(self, **kwargs):
        self.calls.append(kwargs)
        facts = json.loads(kwargs["input"])["selected_facts"]
        payload = self.payload or {"interjections": [{
            "subject": facts[0]["roast_subject"], "text": facts[0]["summary"],
            "fact_ids": [facts[0]["fact_id"]],
        }]}
        return SimpleNamespace(output_text=json.dumps(payload), status="completed", id="response-test",
                               model=kwargs["model"], usage={"input_tokens": 200, "output_tokens": 30, "total_tokens": 230})


def generated(report, config=None, client=None):
    return generate_drunk_bot_commentary(
        report, config=config or DrunkBotConfig(), client=client or ModelClient(),
        openai_config=CommentaryConfig(api_key="fake", model="configured-commissioner-model"),
    )


def test_disabled_or_mild_never_constructs_client(report, monkeypatch):
    monkeypatch.setattr(CommentaryConfig, "from_env", lambda **k: pytest.fail("read API configuration"))
    disabled = generate_drunk_bot_commentary(report, config=DrunkBotConfig(enabled=False))
    assert not disabled.eligibility.eligible and disabled.request is None
    mild = negative_fact(priority=72, values={"consecutive_weeks": 1})
    assert not generate_drunk_bot_commentary(with_facts(report, mild), config=DrunkBotConfig()).eligibility.eligible


def test_strong_streak_deterministic_existing_fact_ids(report):
    first = select_drunk_bot_facts(report)
    second = select_drunk_bot_facts(replace(report, candidate_facts=tuple(reversed(report.candidate_facts))))
    assert first == second
    assert first.eligible and first.score == 98
    assert first.subjects == ("Taylor Quinn",)
    assert first.selected_facts[0].fact_type == "consecutive_last_places"
    assert first.selected_facts[0].values["consecutive_weeks"] == 4
    assert set(first.to_dict()["selected_fact_ids"]) <= {fact_identifier(f) for f in report.candidate_facts}
    assert not select_drunk_bot_facts(report, DrunkBotConfig(minimum_fact_priority=100)).eligible
    assert not select_drunk_bot_facts(
        with_facts(report, first.selected_facts[0]), DrunkBotConfig(minimum_streak_length=5)
    ).eligible


def test_new_negative_record_qualifies_but_positive_or_tied_does_not(report):
    record = negative_fact(fact_type="season_low_score_record", priority=96, category="record",
                           values={"record_status": "set", "record_value": 80, "prior_record": 90})
    assert select_drunk_bot_facts(with_facts(report, record)).eligible
    assert not select_drunk_bot_facts(with_facts(report, replace(record, fact_type="season_high_score_record"))).eligible
    assert not select_drunk_bot_facts(with_facts(report, replace(record, values={"record_status": "tied"}))).eligible


def test_subject_diversity_and_minimum_evidence(report):
    a = negative_fact()
    b = negative_fact(subject_member_name="Alex Rowan", priority=96)
    c = negative_fact(subject_member_name="Sam Ellis", priority=95)
    selection = select_drunk_bot_facts(with_facts(report, a, a, b, c))
    assert selection.subjects == ("Taylor Quinn", "Alex Rowan")
    assert len(selection.selected_facts) == 2
    assert not select_drunk_bot_facts(with_facts(report, a), DrunkBotConfig(minimum_number_of_eligible_facts=2)).eligible


def test_head_to_head_targets_loser_and_unknown_money_is_not_zero(report):
    head = negative_fact(fact_type="head_to_head_streak", subject_member_name="Alex Rowan",
                         related_member_name="Taylor Quinn", values={"consecutive_shared_weeks": 4})
    assert select_drunk_bot_facts(with_facts(report, head)).subjects == ("Taylor Quinn",)
    money = negative_fact(fact_type="known_zero_season_winnings",
                          values={"total_money_won": None, "weeks_with_prize_data": 3})
    assert not select_drunk_bot_facts(with_facts(report, money)).eligible


def test_environment_config(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("DRUNK_BOT_"):
            monkeypatch.delenv(key)
    settings = tmp_path / ".env"
    settings.write_text("DRUNK_BOT_ENABLED=false\nDRUNK_BOT_MINIMUM_FACT_PRIORITY=95\n")
    assert not DrunkBotConfig.from_env(settings).enabled
    monkeypatch.setenv("DRUNK_BOT_ENABLED", "true")
    assert DrunkBotConfig.from_env(settings).enabled
    assert DrunkBotConfig.from_env(settings).minimum_fact_priority == 95
    monkeypatch.setenv("DRUNK_BOT_MAXIMUM_INTERJECTIONS_PER_WEEK", "9")
    with pytest.raises(ValueError):
        DrunkBotConfig.from_env(settings)


def test_one_generation_call_structured_parsing_traceability_usage(report, monkeypatch):
    client = ModelClient()
    result = generated(report, client=client)
    assert len(client.calls) == 1
    assert result.request["model"] == "configured-commissioner-model"
    assert result.usage.total_tokens == 230
    assert result.commentary.interjections[0].fact_ids == tuple(result.eligibility.to_dict()["selected_fact_ids"])
    assert result.request["text"]["format"]["strict"] is True
    assert not result.request["store"]
    monkeypatch.setenv("DRUNK_BOT_OPENAI_MODEL", "persona-override")
    assert generated(report).request["model"] == "persona-override"


def test_prompts_are_separate_and_profanity_fact_rules_remain(report):
    prompt = generated(report).request["instructions"]
    assert "Profanity is allowed and encouraged" in prompt
    assert "Never treat missing prize data as $0" in prompt
    assert "Never invent or change scores, ranks, winnings, player selections, streaks" in prompt
    assert "Never estimate entry fees, net losses" in prompt
    assert "No protected-characteristic attacks" in prompt
    assert "Avoid profanity" in commentary_instructions("ruthless")
    assert "Do not use profanity" in generated(report, DrunkBotConfig(allow_profanity=False)).request["instructions"]


@pytest.mark.parametrize("tone", ["normal", "ruthless"])
@pytest.mark.parametrize("allow_profanity", [False, True])
def test_drunk_bot_prompt_explicitly_disallows_persistent_rosters(tone, allow_profanity):
    prompt = " ".join(drunk_bot_instructions(tone=tone, allow_profanity=allow_profanity, maximum=2).split())
    assert f"PROMPT VERSION: {DRUNK_BOT_PROMPT_VERSION}" in prompt
    assert "KCDK is daily fantasy sports" in prompt
    assert "Each week is a separate DraftKings DFS contest with a newly constructed lineup" in prompt
    assert "in separate weekly DFS lineups N times" in prompt
    assert "Consecutive usage means a new selection" in prompt
    assert "finished last in N separate KCDK contests" in prompt
    assert "Field ownership describes entrants selecting a player on a slate" in prompt
    assert "NO persistent fantasy rosters, season-long owned players, trades, waivers, bench decisions, keeper decisions" in prompt
    forbidden = prompt.split("Do not use language implying persistent teams or player ownership", 1)[1].split("Prefer lineup", 1)[0]
    for phrase in ("drafted Player X", "kept Player X", "held Player X", "traded for",
                   "waiver pickup", "bench", "own Player X", "your team all season", "rostered all season"):
        assert phrase in forbidden
    for phrase in ("lineup", "slate", "entry", "weekly selection", "player exposure",
                   "field ownership", "chalk", "contrarian selection", "tournament finish",
                   "percentile", "cash / no cash", "entry fee", "lineup construction"):
        assert phrase in prompt


@pytest.mark.parametrize("mutation", ["unknown_id", "wrong_subject", "too_many", "too_long", "public_id", "repeat", "no_ids", "extra"])
def test_structured_output_limits(report, mutation):
    result = generated(report)
    payload = result.commentary.to_dict()
    first = payload["interjections"][0]
    if mutation == "unknown_id":
        first["fact_ids"] = ["fact_unknown"]
    elif mutation == "wrong_subject":
        first["subject"] = "Alex Rowan"
    elif mutation == "too_many":
        payload["interjections"] *= 3
    elif mutation == "too_long":
        first["text"] = "x" * 601
    elif mutation == "public_id":
        first["text"] = first["fact_ids"][0]
    elif mutation == "repeat":
        payload["interjections"] *= 2
    elif mutation == "no_ids":
        first["fact_ids"] = []
    else:
        first["extra"] = "unexpected"
    with pytest.raises(CommentaryResponseError):
        parse_drunk_bot_commentary(json.dumps(payload), request=result.request, config=DrunkBotConfig())


def test_render_minimal_with_no_ids_or_mentions(report):
    result = generated(report)
    item = replace(result.commentary.interjections[0], text="@everyone <@123> Four finishes.")
    message = render_drunk_bot_message(replace(result.commentary, interjections=(item,)))
    payload = message.to_payload()
    assert not message.embeds and "fact_" not in message.content
    assert "@everyone" not in message.content and "<@123>" not in message.content
    assert payload["allowed_mentions"] == {"parse": []}


def publish(connection, tmp_path, commentary, client, **kwargs):
    return publish_weekly_report(
        connection, season_identifier="mock-2026", week_label="Week 4", commentary=commentary,
        state_store=DiscordStateStore(tmp_path / "state.json"),
        discord_config=kwargs.pop("discord_config", replace(_discord_config(), drunk_webhook_url=DRUNK_URL)),
        leaderboard_config=LeaderboardConfig(mode="text"), drunk_bot_config=DrunkBotConfig(),
        transport=client, **kwargs,
    )


def test_separate_webhook_order_and_rerun_idempotency(report, season_db, commentary, tmp_path, monkeypatch):
    calls = []
    def generate(*args, **kwargs):
        calls.append(1)
        return generated(report)
    monkeypatch.setattr("kcdk.drunk_bot_publishing.generate_drunk_bot_commentary", generate)
    client = FakeTransport()
    result = publish(season_db, tmp_path, commentary, client)
    assert [url for url, _, _ in client.created] == [
        _discord_config().weekly_webhook_url, DRUNK_URL,
        _discord_config().leaderboard_webhook_url, _discord_config().leaderboard_webhook_url,
    ]
    assert len(calls) == 1 and result.drunk_bot.status == "posted"
    assert "fact_" not in client.created[1][1].content
    store = DiscordStateStore(tmp_path / "state.json")
    assert store.load().drunk_bot_weeks[result.publication_key].message_id == "message-2"
    rerun = FakeTransport()
    second = publish(season_db, tmp_path, commentary, rerun)
    assert not rerun.created and len(rerun.edited) == 2
    assert len(calls) == 1 and second.drunk_bot.status == "already_posted"
    # Even explicitly reposting the Commissioner must not duplicate the second voice.
    publish(season_db, tmp_path, commentary, rerun, repost=True)
    assert len(rerun.created) == 1 and len(calls) == 1


def test_missing_drunk_webhook_is_nonfatal_and_no_generation(season_db, commentary, tmp_path, monkeypatch):
    monkeypatch.setattr("kcdk.drunk_bot_publishing.generate_drunk_bot_commentary",
                        lambda *a, **k: pytest.fail("unnecessary generation"))
    client = FakeTransport()
    result = publish(season_db, tmp_path, commentary, client, discord_config=_discord_config())
    assert result.drunk_bot.status == "missing_webhook"
    assert len(client.created) == 3
    assert result.publication_key in DiscordStateStore(tmp_path / "state.json").load().published_weeks


def test_failed_post_explicit_recovery_reuses_generation_without_commissioner(report, season_db, commentary, tmp_path, monkeypatch):
    calls = []
    def generate(*a, **k):
        calls.append(1)
        return generated(report)
    monkeypatch.setattr("kcdk.drunk_bot_publishing.generate_drunk_bot_commentary", generate)
    class RejectPersona(FakeTransport):
        def create_message(self, url, message):
            if url == DRUNK_URL:
                raise RejectedDiscordMessageError("Discord rejected the message payload (HTTP 400).")
            return super().create_message(url, message)
    first = publish(season_db, tmp_path, commentary, RejectPersona())
    assert first.drunk_bot.status == "post_failed"
    ordinary = FakeTransport()
    publish(season_db, tmp_path, commentary, ordinary)
    assert not ordinary.created
    recovered = FakeTransport()
    result = publish(season_db, tmp_path, commentary, recovered, recover_drunk_bot=True)
    assert len(recovered.created) == 1 and recovered.created[0][0] == DRUNK_URL
    assert result.drunk_bot.status == "posted" and len(calls) == 1


def test_ambiguous_post_and_crash_intent_block_duplicate(report, season_db, commentary, tmp_path, monkeypatch):
    monkeypatch.setattr("kcdk.drunk_bot_publishing.generate_drunk_bot_commentary", lambda *a, **k: generated(report))
    class UncertainPersona(FakeTransport):
        def create_message(self, url, message):
            if url == DRUNK_URL:
                raise DiscordTransportError("Discord request failed (ReadTimeout).")
            return super().create_message(url, message)
    first = publish(season_db, tmp_path, commentary, UncertainPersona())
    assert first.drunk_bot.status == "post_uncertain"
    retry = FakeTransport()
    result = publish(season_db, tmp_path, commentary, retry, recover_drunk_bot=True)
    assert not retry.created and result.drunk_bot.status == "reconciliation_required"
    store = DiscordStateStore(tmp_path / "state.json")
    state = store.load()
    state.drunk_bot_weeks[first.publication_key].status = "posting"
    store.save(state)
    assert publish(season_db, tmp_path, commentary, retry).drunk_bot.status == "reconciliation_required"


def test_generation_failure_does_not_fail_commissioner_or_leaderboards(season_db, commentary, tmp_path, monkeypatch):
    def fail(*a, **k):
        raise CommentaryAPIError("OpenAI request failed (HTTP 500).")
    monkeypatch.setattr("kcdk.drunk_bot_publishing.generate_drunk_bot_commentary", fail)
    client = FakeTransport()
    result = publish(season_db, tmp_path, commentary, client)
    assert result.drunk_bot.status == "generation_failed" and len(client.created) == 3
    monkeypatch.setattr("kcdk.drunk_bot_publishing.generate_drunk_bot_commentary", lambda *a, **k: pytest.fail("auto retry"))
    assert publish(season_db, tmp_path, commentary, FakeTransport()).drunk_bot.status == "generation_failed"


def test_disabled_and_ineligible_states_saved(season_db, commentary, tmp_path):
    store = DiscordStateStore(tmp_path / "state.json")
    result = publish_weekly_report(
        season_db, season_identifier="mock-2026", week_label="Week 4", commentary=commentary,
        state_store=store, discord_config=_discord_config(), transport=FakeTransport(),
        leaderboard_config=LeaderboardConfig(mode="text"), drunk_bot_config=DrunkBotConfig(enabled=False),
    )
    assert store.load().drunk_bot_weeks[result.publication_key].status == "disabled"
    assert not result.drunk_bot.eligibility.eligible
    first_week = publish_weekly_report(
        season_db, season_identifier="mock-2026", week_label="Week 1", commentary=commentary,
        state_store=store, discord_config=_discord_config(), transport=FakeTransport(),
        leaderboard_config=LeaderboardConfig(mode="text"), drunk_bot_config=DrunkBotConfig(),
    )
    assert first_week.drunk_bot.status == "not_eligible"
    assert store.load().drunk_bot_weeks[first_week.publication_key].status == "not_eligible"


def test_explicit_generation_recovery_does_not_regenerate_commissioner(report, season_db, commentary, tmp_path, monkeypatch):
    def fail(*a, **k):
        raise CommentaryAPIError("OpenAI request failed (ReadTimeout).")
    monkeypatch.setattr("kcdk.drunk_bot_publishing.generate_drunk_bot_commentary", fail)
    publish(season_db, tmp_path, commentary, FakeTransport())
    monkeypatch.setattr("kcdk.publishing.generate_weekly_commentary", lambda *a, **k: pytest.fail("Commissioner called"))
    monkeypatch.setattr("kcdk.drunk_bot_publishing.generate_drunk_bot_commentary", lambda *a, **k: generated(report))
    client = FakeTransport()
    recovered = publish(season_db, tmp_path, None, client, recover_drunk_bot=True)
    assert recovered.drunk_bot.status == "posted"
    assert len(client.created) == 1 and client.created[0][0] == DRUNK_URL


def test_paid_smoke_makes_one_mocked_request_and_never_posts(tmp_path, monkeypatch, capsys):
    from scripts import smoke_drunk_bot as smoke
    client = ModelClient()
    monkeypatch.setattr("kcdk.drunk_bot_commentary._new_openai_client", lambda config: client)
    monkeypatch.setattr(CommentaryConfig, "from_env", lambda **k: CommentaryConfig(api_key="fake"))
    monkeypatch.setattr(smoke, "DiscordWebhookClient", lambda **k: pytest.fail("Discord client"))
    destination = tmp_path / "generation.json"
    assert smoke.main(["--live", "--generation", str(destination)]) == 0
    assert len(client.calls) == 1
    assert json.loads(destination.read_text())["commentary"]["interjections"]


def test_dry_run_has_eligibility_without_generation_or_state_changes(season_db, commentary, tmp_path, monkeypatch):
    monkeypatch.setattr("kcdk.drunk_bot_publishing.generate_drunk_bot_commentary", lambda *a, **k: pytest.fail("generation"))
    client = FakeTransport()
    result = publish(season_db, tmp_path, commentary, client, dry_run=True)
    assert result.drunk_bot.eligibility.score == 98
    assert result.drunk_bot.would_generate_commentary and result.drunk_bot.would_post_to_discord
    assert not client.created and not client.edited
    assert not (tmp_path / "state.json").exists()


def test_version_one_state_loads_without_reposting_old_weeks(tmp_path):
    store = DiscordStateStore(tmp_path / "old.json")
    store.path.write_text(json.dumps({"version": 1, "leaderboards": {"kcdk_message_id": "10", "tournament_message_id": "20"},
        "published_weeks": {"mock-2026:4": {"message_ids": ["30"], "published_at": "previous"}}}))
    state = store.load()
    assert state.kcdk_leaderboard_message_id == "10" and state.drunk_bot_weeks == {}
    assert state.published_weeks["mock-2026:4"].message_ids == ("30",)
    store.save(state)
    assert json.loads(store.path.read_text())["version"] == 2


def test_preflight_shows_eligibility_using_temporary_database(season_db, tmp_path):
    database = Path(season_db.execute("PRAGMA database_list").fetchone()[2])
    before = database.read_bytes()
    config = LocalWeeklyConfig(season_identifier="mock-2026", season_name="Mock 2026",
        members_path=str(MOCK / "members.csv"), database_path=str(database), state_path=str(tmp_path / "state.json"))
    result = build_preflight(csv_path=MOCK / "season_week_4.csv", config=config, week_number=4, dry_run=True)
    assert result.can_continue and result.drunk_bot.eligible
    assert "score=98" in result.render()
    assert database.read_bytes() == before
    disabled = build_preflight(csv_path=MOCK / "season_week_4.csv", config=replace(config, drunk_bot_enabled=False), week_number=4, dry_run=True)
    assert not disabled.drunk_bot.enabled


def test_smoke_preview_and_post_are_separate_opt_ins(tmp_path, report, monkeypatch, capsys):
    from scripts import smoke_drunk_bot as smoke
    monkeypatch.setattr(smoke, "SMOKE_STATE", tmp_path / "smoke.json")
    assert smoke.main([]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["eligibility"]["eligible"] and preview["commentary"] is None
    saved = tmp_path / "generation.json"
    saved.write_text(json.dumps(generated(report).to_dict()))
    client = FakeTransport()
    monkeypatch.setattr(smoke.DiscordConfig, "from_env", lambda **k: replace(_discord_config(), drunk_webhook_url=DRUNK_URL))
    monkeypatch.setattr(smoke, "DiscordWebhookClient", lambda **k: client)
    assert smoke.main(["--post", "--generation", str(saved)]) == 0
    assert len(client.created) == 1 and client.created[0][0] == DRUNK_URL
    assert client.created[0][1].content.startswith("[SMOKE TEST]")
    assert smoke.main(["--post", "--generation", str(saved)]) == 0
    assert len(client.created) == 1
