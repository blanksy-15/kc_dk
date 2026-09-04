import json
from pathlib import Path

import pytest

from kcdk.commentary import CommentaryRoast, StructuredCommentary
from kcdk.discord import (
    DISCORD_CONTENT_LIMIT,
    DISCORD_EMBED_DESCRIPTION_LIMIT,
    DISCORD_EMBED_FIELDS_LIMIT,
    DISCORD_EMBED_FIELD_VALUE_LIMIT,
    DISCORD_EMBED_TOTAL_LIMIT,
    DiscordConfig,
    DiscordConfigurationError,
    DiscordMessage,
    DiscordMessageError,
    DiscordTransportError,
    DiscordWebhookClient,
    StaleDiscordMessageError,
    validate_discord_message,
)
from kcdk.persistence import connect_database, import_week
from kcdk.publishing import (
    DiscordState,
    DiscordStateStore,
    PublicationRecord,
    PublishingError,
    publish_weekly_report,
    render_kcdk_leaderboard,
    render_tournament_leaderboard,
)


ROOT = Path(__file__).parents[1]
MOCK = ROOT / "data" / "mock"
VALID_WEEKLY_URL = "https://discord.com/api/webhooks/111/test-weekly-token"
VALID_LEADERBOARD_URL = "https://discord.com/api/webhooks/222/test-board-token"


@pytest.fixture
def season_db(tmp_path):
    connection = connect_database(tmp_path / "discord.sqlite")
    for week in range(1, 5):
        import_week(
            connection,
            MOCK / f"season_week_{week}.csv",
            MOCK / "members.csv",
            season_name="Mock 2026",
            season_identifier="mock-2026",
            season_year=2026,
            week_number=week,
            contest_name=f"Fictional Week {week}",
            contest_date=f"2026-09-{week:02d}",
        )
    yield connection
    connection.close()


@pytest.fixture
def commentary():
    return StructuredCommentary(
        headline="Week 4 recap",
        intro="The slate was noisy.",
        roasts=(
            CommentaryRoast(
                member="Sam Ellis",
                text="Sam set the scoring pace.",
                fact_ids=("fact_private_1",),
            ),
            CommentaryRoast(
                member="Taylor Quinn",
                text="Taylor's standings rut continued.",
                fact_ids=("fact_private_2",),
            ),
        ),
        closing="On to next week.",
    )


class FakeTransport:
    def __init__(self, *, stale_on_edit=False, fail_on_create_number=None):
        self.created = []
        self.edited = []
        self.deleted = []
        self.stale_on_edit = stale_on_edit
        self.fail_on_create_number = fail_on_create_number

    def create_message(self, webhook_url, message):
        message.to_payload()
        call_number = len(self.created) + 1
        if self.fail_on_create_number == call_number:
            raise DiscordTransportError("Discord webhook request failed (HTTP 503).")
        message_id = f"message-{call_number}"
        self.created.append((webhook_url, message, message_id))
        return message_id

    def edit_message(self, webhook_url, message_id, message):
        message.to_payload()
        if self.stale_on_edit:
            raise StaleDiscordMessageError(
                "The persisted Discord message no longer exists; no replacement was created."
            )
        self.edited.append((webhook_url, message_id, message))

    def delete_message(self, webhook_url, message_id):
        self.deleted.append((webhook_url, message_id))


def _discord_config():
    return DiscordConfig(
        weekly_webhook_url=VALID_WEEKLY_URL,
        leaderboard_webhook_url=VALID_LEADERBOARD_URL,
    )


def test_missing_weekly_webhook(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCORD_WEEKLY_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_LEADERBOARD_WEBHOOK_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DISCORD_LEADERBOARD_WEBHOOK_URL=" + VALID_LEADERBOARD_URL,
        encoding="utf-8",
    )
    with pytest.raises(DiscordConfigurationError, match="WEEKLY"):
        DiscordConfig.from_env(env_file=env_file)


def test_missing_leaderboard_webhook(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCORD_WEEKLY_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_LEADERBOARD_WEBHOOK_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DISCORD_WEEKLY_WEBHOOK_URL=" + VALID_WEEKLY_URL,
        encoding="utf-8",
    )
    with pytest.raises(DiscordConfigurationError, match="LEADERBOARD"):
        DiscordConfig.from_env(env_file=env_file)


def test_configuration_repr_does_not_expose_webhooks():
    config = _discord_config()
    assert VALID_WEEKLY_URL not in repr(config)
    assert VALID_LEADERBOARD_URL not in repr(config)


def test_transport_error_does_not_expose_webhook(monkeypatch):
    secret_url = "https://discord.com/api/webhooks/333/SENSITIVE_WEBHOOK_TOKEN"

    class FailingClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, json=None):
            raise RuntimeError(f"could not reach {url}")

    monkeypatch.setattr("kcdk.discord.httpx.Client", FailingClient)
    client = DiscordWebhookClient()
    with pytest.raises(DiscordTransportError) as captured:
        client.create_message(secret_url, DiscordMessage(content="test"))
    assert secret_url not in str(captured.value)
    assert "SENSITIVE_WEBHOOK_TOKEN" not in str(captured.value)


def test_webhook_transport_uses_wait_create_edit_and_delete(monkeypatch):
    calls = []
    responses = [
        type("Response", (), {"status_code": 200, "json": lambda self: {"id": "42"}})(),
        type("Response", (), {"status_code": 200})(),
        type("Response", (), {"status_code": 204})(),
    ]

    class RecordingClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, json=None):
            calls.append((method, url, json))
            return responses.pop(0)

    monkeypatch.setattr("kcdk.discord.httpx.Client", RecordingClient)
    client = DiscordWebhookClient()
    message = DiscordMessage(content="transport test")

    assert client.create_message(VALID_WEEKLY_URL, message) == "42"
    client.edit_message(VALID_WEEKLY_URL, "42", message)
    client.delete_message(VALID_WEEKLY_URL, "42")

    assert calls[0][0] == "POST" and calls[0][1].endswith("?wait=true")
    assert calls[1][0] == "PATCH" and calls[1][1].endswith("/messages/42")
    assert calls[2][0] == "DELETE" and calls[2][1].endswith("/messages/42")
    assert calls[0][2]["allowed_mentions"] == {"parse": []}


def test_webhook_transport_maps_edit_404_to_stale_error(monkeypatch):
    class MissingClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, url, json=None):
            return type("Response", (), {"status_code": 404})()

    monkeypatch.setattr("kcdk.discord.httpx.Client", MissingClient)
    with pytest.raises(StaleDiscordMessageError, match="no replacement"):
        DiscordWebhookClient().edit_message(
            VALID_WEEKLY_URL, "missing", DiscordMessage(content="test")
        )


@pytest.mark.parametrize(
    "message",
    [
        DiscordMessage(content="x" * (DISCORD_CONTENT_LIMIT + 1)),
        DiscordMessage(
            embeds=(
                {
                    "description": "x" * (DISCORD_EMBED_DESCRIPTION_LIMIT + 1),
                },
            )
        ),
        DiscordMessage(
            embeds=(
                {
                    "fields": [
                        {"name": "field", "value": "x"}
                        for _ in range(DISCORD_EMBED_FIELDS_LIMIT + 1)
                    ]
                },
            )
        ),
        DiscordMessage(
            embeds=(
                {
                    "fields": [
                        {
                            "name": "field",
                            "value": "x" * (DISCORD_EMBED_FIELD_VALUE_LIMIT + 1),
                        }
                    ]
                },
            )
        ),
        DiscordMessage(
            embeds=(
                {"description": "x" * 3500},
                {"description": "x" * (DISCORD_EMBED_TOTAL_LIMIT - 3499)},
            )
        ),
    ],
)
def test_message_and_embed_limits_are_enforced(message):
    with pytest.raises(DiscordMessageError):
        validate_discord_message(message)


def test_weekly_dry_run_renders_all_sections_without_transport(
    season_db, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "kcdk.publishing.generate_weekly_commentary",
        lambda *args, **kwargs: pytest.fail("dry run called OpenAI"),
    )
    transport = FakeTransport()
    result = publish_weekly_report(
        season_db,
        season_identifier="mock-2026",
        week_label="Week 4",
        dry_run=True,
        state_store=DiscordStateStore(tmp_path / "state.json"),
        transport=transport,
    )
    payload = result.to_dict()
    serialized = json.dumps(payload)

    assert result.commentary_source == "deterministic_preview"
    assert [item.action for item in result.operations] == [
        "create",
        "create",
        "create",
    ]
    assert "Weekly KCDK results" in serialized
    assert "Tournament results / earnings" in serialized
    assert "KCDK SEASON STANDINGS" in serialized
    assert "TOURNAMENT EARNINGS" in serialized
    assert "fact_" not in serialized
    assert "webhook" not in serialized.lower()
    assert transport.created == []
    assert transport.edited == []


def test_partial_prize_warning_is_rendered_only_when_relevant(season_db, tmp_path):
    result = publish_weekly_report(
        season_db,
        season_identifier="mock-2026",
        week_label="Week 4",
        dry_run=True,
        state_store=DiscordStateStore(tmp_path / "state.json"),
    )
    payload = result.weekly_message.to_payload()
    fields = payload["embeds"][0]["fields"]
    notes = next(item for item in fields if item["name"] == "Data notes")
    assert "Prize data is incomplete" in notes["value"]


def test_initial_leaderboard_creation_and_publication_state(
    season_db, commentary, tmp_path
):
    store = DiscordStateStore(tmp_path / "state.json")
    transport = FakeTransport()
    result = publish_weekly_report(
        season_db,
        season_identifier="mock-2026",
        week_label="Week 4",
        commentary=commentary,
        state_store=store,
        discord_config=_discord_config(),
        transport=transport,
    )
    state = store.load()

    assert len(transport.created) == 3
    assert state.kcdk_leaderboard_message_id == "message-1"
    assert state.tournament_leaderboard_message_id == "message-2"
    assert result.publication_key in state.published_weeks
    assert state.published_weeks[result.publication_key].message_ids == (
        "message-3",
    )
    state_text = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "api/webhooks" not in state_text


def test_subsequent_run_edits_leaderboards_and_skips_weekly_recap(
    season_db, commentary, tmp_path
):
    store = DiscordStateStore(tmp_path / "state.json")
    first_transport = FakeTransport()
    publish_weekly_report(
        season_db,
        season_identifier="mock-2026",
        week_label="Week 4",
        commentary=commentary,
        state_store=store,
        discord_config=_discord_config(),
        transport=first_transport,
    )
    second_transport = FakeTransport()
    result = publish_weekly_report(
        season_db,
        season_identifier="mock-2026",
        week_label="Week 4",
        commentary=commentary,
        state_store=store,
        discord_config=_discord_config(),
        transport=second_transport,
    )

    assert result.already_published is True
    assert len(second_transport.edited) == 2
    assert second_transport.created == []
    assert result.operations[-1].action == "skip"


def test_explicit_repost_edits_leaderboards_and_creates_one_recap(
    season_db, commentary, tmp_path
):
    store = DiscordStateStore(tmp_path / "state.json")
    publish_weekly_report(
        season_db,
        season_identifier="mock-2026",
        week_label="Week 4",
        commentary=commentary,
        state_store=store,
        discord_config=_discord_config(),
        transport=FakeTransport(),
    )
    transport = FakeTransport()
    result = publish_weekly_report(
        season_db,
        season_identifier="mock-2026",
        week_label="Week 4",
        repost=True,
        commentary=commentary,
        state_store=store,
        discord_config=_discord_config(),
        transport=transport,
    )

    assert len(transport.edited) == 2
    assert len(transport.created) == 1
    assert result.operations[-1].reason == "explicit repost requested"
    assert len(store.load().published_weeks[result.publication_key].message_ids) == 2


def test_stale_leaderboard_message_fails_without_creating_duplicate(
    season_db, commentary, tmp_path
):
    store = DiscordStateStore(tmp_path / "state.json")
    store.save(
        DiscordState(
            kcdk_leaderboard_message_id="deleted-message",
            tournament_leaderboard_message_id="existing-message",
        )
    )
    transport = FakeTransport(stale_on_edit=True)

    with pytest.raises(PublishingError, match="no replacement was created"):
        publish_weekly_report(
            season_db,
            season_identifier="mock-2026",
            week_label="Week 4",
            commentary=commentary,
            state_store=store,
            discord_config=_discord_config(),
            transport=transport,
        )
    assert transport.created == []
    assert store.load().kcdk_leaderboard_message_id == "deleted-message"


def test_weekly_failure_does_not_mark_publication_successful(
    season_db, commentary, tmp_path
):
    store = DiscordStateStore(tmp_path / "state.json")
    transport = FakeTransport(fail_on_create_number=3)

    with pytest.raises(PublishingError, match="Weekly recap publish failed"):
        publish_weekly_report(
            season_db,
            season_identifier="mock-2026",
            week_label="Week 4",
            commentary=commentary,
            state_store=store,
            discord_config=_discord_config(),
            transport=transport,
        )
    assert store.load().published_weeks == {}


def test_existing_publication_dry_run_reports_skip(season_db, tmp_path):
    store = DiscordStateStore(tmp_path / "state.json")
    store.save(
        DiscordState(
            published_weeks={
                "mock-2026:4": PublicationRecord(
                    message_ids=("weekly-existing",),
                    published_at="2026-09-04T00:00:00+00:00",
                )
            }
        )
    )
    result = publish_weekly_report(
        season_db,
        season_identifier="mock-2026",
        week_label="Week 4",
        dry_run=True,
        state_store=store,
    )
    assert result.already_published is True
    assert result.operations[-1].action == "skip"
    assert result.weekly_message_ids == ("weekly-existing",)


def test_existing_live_publication_skips_openai_generation(
    season_db, tmp_path, monkeypatch
):
    store = DiscordStateStore(tmp_path / "state.json")
    store.save(
        DiscordState(
            kcdk_leaderboard_message_id="kcdk-existing",
            tournament_leaderboard_message_id="tournament-existing",
            published_weeks={
                "mock-2026:4": PublicationRecord(
                    message_ids=("weekly-existing",),
                    published_at="2026-09-04T00:00:00+00:00",
                )
            },
        )
    )
    monkeypatch.setattr(
        "kcdk.publishing.generate_weekly_commentary",
        lambda *args, **kwargs: pytest.fail("published week called OpenAI"),
    )
    transport = FakeTransport()
    result = publish_weekly_report(
        season_db,
        season_identifier="mock-2026",
        week_label="Week 4",
        state_store=store,
        discord_config=_discord_config(),
        transport=transport,
    )
    assert result.commentary_source == "not_generated_already_published"
    assert len(transport.edited) == 2
    assert transport.created == []


def test_live_missing_urls_fails_before_openai(season_db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kcdk.publishing.generate_weekly_commentary",
        lambda *args, **kwargs: pytest.fail("missing webhook called OpenAI"),
    )
    with pytest.raises(PublishingError, match="Both Discord webhooks"):
        publish_weekly_report(
            season_db,
            season_identifier="mock-2026",
            week_label="Week 4",
            state_store=DiscordStateStore(tmp_path / "state.json"),
            discord_config=DiscordConfig(),
            transport=FakeTransport(),
        )


def test_independent_leaderboard_renderers_are_compact(season_db):
    from kcdk.analytics import (
        season_leaderboard,
        tournament_performance_leaderboard,
    )

    kcdk = render_kcdk_leaderboard(
        season_leaderboard(season_db, "mock-2026"),
        season_name="Mock 2026",
        through_week="Week 4",
    ).to_payload()
    tournament = render_tournament_leaderboard(
        tournament_performance_leaderboard(season_db, "mock-2026"),
        season_name="Mock 2026",
        through_week="Week 4",
    ).to_payload()

    assert "avg 2.25" in kcdk["embeds"][0]["description"]
    assert "$160.00" in tournament["embeds"][0]["description"]
    assert "partial known prize data" in tournament["embeds"][0]["footer"][
        "text"
    ]
