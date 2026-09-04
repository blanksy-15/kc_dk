from email.parser import BytesParser
from email.policy import default
import importlib.util
import json
from pathlib import Path

import httpx
from PIL import Image
import pytest

from kcdk.analytics import season_leaderboard_with_movement
from kcdk.discord import (
    DiscordAttachment, DiscordConfigurationError, DiscordMessage,
    DiscordTransportError, DiscordWebhookClient, RejectedDiscordMessageError,
)
from kcdk.leaderboard_delivery import LeaderboardConfig, deliver_leaderboard
from kcdk.publishing import DiscordState, DiscordStateStore, PublishingError, publish_weekly_report
from test_discord import (
    FakeTransport, VALID_LEADERBOARD_URL, _discord_config, commentary, season_db,
)


def preview(connection, tmp_path, **kwargs):
    return publish_weekly_report(
        connection, season_identifier="mock-2026", week_label="Week 4",
        dry_run=True, state_store=DiscordStateStore(tmp_path / "state.json"),
        leaderboard_config=kwargs.pop("leaderboard_config", LeaderboardConfig(
            output_directory=tmp_path / "images")), **kwargs,
    )


def test_default_image_and_environment_text_configuration(tmp_path, monkeypatch):
    for name in ("KCDK_LEADERBOARD_MODE", "KCDK_LEADERBOARD_MAX_FILE_BYTES"):
        monkeypatch.delenv(name, raising=False)
    assert LeaderboardConfig().mode == "image"
    assert LeaderboardConfig.from_env(tmp_path / "missing").mode == "image"
    settings = tmp_path / ".env"
    settings.write_text("KCDK_LEADERBOARD_MODE=text\nKCDK_LEADERBOARD_MAX_FILE_BYTES=1000\n")
    assert LeaderboardConfig.from_env(settings).mode == "text"
    assert LeaderboardConfig.from_env(settings).max_file_bytes == 1000
    monkeypatch.setenv("KCDK_LEADERBOARD_MODE", "image")
    assert LeaderboardConfig.from_env(settings).mode == "image"


@pytest.mark.parametrize("setting,value", [
    ("KCDK_LEADERBOARD_MODE", "video"),
    ("KCDK_LEADERBOARD_MAX_FILE_BYTES", "zero"),
    ("KCDK_LEADERBOARD_MAX_FILE_BYTES", "0"),
])
def test_bad_configuration_fails_early(tmp_path, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(DiscordConfigurationError):
        LeaderboardConfig.from_env(tmp_path / "missing")


def test_fresh_current_analytics_and_respective_images(season_db, tmp_path, monkeypatch):
    import kcdk.leaderboard_delivery as delivery
    original = delivery.render_kcdk_standings_png
    captured = []

    def capture(frame, *args, **kwargs):
        captured.append(frame.copy())
        return original(frame, *args, **kwargs)

    monkeypatch.setattr(delivery, "render_kcdk_standings_png", capture)
    output = tmp_path / "images"
    output.mkdir()
    (output / "kcdk_standings.png").write_bytes(b"obsolete preview must be replaced")
    first = preview(season_db, tmp_path)
    assert captured[0].equals(season_leaderboard_with_movement(season_db, "mock-2026"))
    assert [item.message.files[0].filename for item in first.leaderboards] == [
        "kcdk_standings.png", "tournament_performance.png",
    ]
    for item in first.leaderboards:
        assert item.mode == "image"
        assert item.image_path.read_bytes() == item.message.files[0].data
        assert item.dimensions == (1400, 686)
        assert item.file_size_bytes > 0
        assert not item.message.embeds
        assert len(item.message.content.splitlines()) == 2
    season_db.execute("UPDATE members SET display_name = 'Changed Current Member' WHERE id = 1")
    season_db.commit()
    # Rerunning an older recap must still publish the latest standings and week label.
    second = publish_weekly_report(
        season_db, season_identifier="mock-2026", week_label="Week 1", dry_run=True,
        state_store=DiscordStateStore(tmp_path / "state.json"),
        leaderboard_config=LeaderboardConfig(output_directory=output),
    )
    assert "Changed Current Member" in captured[1].display_name.tolist()
    assert second.leaderboards[0].message.files[0].data != first.leaderboards[0].message.files[0].data
    assert "Updated through Week 4" in second.kcdk_leaderboard_message.content
    assert "Week 1" in second.weekly_message.content


def test_dry_run_reports_pngs_ids_and_never_uses_network(season_db, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr("kcdk.publishing.generate_weekly_commentary",
                        lambda *a, **k: pytest.fail("OpenAI"))
    store = DiscordStateStore(tmp_path / "state.json")
    store.save(DiscordState(kcdk_leaderboard_message_id="10", tournament_leaderboard_message_id="20"))
    before = store.path.read_bytes()
    result = preview(season_db, tmp_path).to_dict()
    assert [item["message_id"] for item in result["leaderboards"]] == ["10", "20"]
    assert [item["action"] for item in result["operations"][2:]] == ["edit", "edit"]
    for item in result["leaderboards"]:
        assert item["rendering_attempted"] is True
        assert item["dimensions"] == [1400, 686]
        assert item["file_size_bytes"] > 0
        assert item["mode"] == "image"
        assert item["image_path"].endswith(".png")
    assert store.path.read_bytes() == before
    assert "api/webhooks" not in json.dumps(result)


def test_explicit_text_mode_skips_rendering(season_db, tmp_path, monkeypatch):
    monkeypatch.setattr("kcdk.leaderboard_delivery.render_kcdk_standings_png",
                        lambda *a, **k: pytest.fail("render"))
    result = preview(season_db, tmp_path, leaderboard_config=LeaderboardConfig(mode="text"))
    for item in result.leaderboards:
        assert not item.rendering_attempted
        assert item.message.embeds and not item.message.files
        assert item.message.to_payload()["attachments"] == []


def test_render_failure_falls_back_independently(season_db, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private renderer exception contents")
    monkeypatch.setattr("kcdk.leaderboard_delivery.render_kcdk_standings_png", fail)
    result = preview(season_db, tmp_path)
    assert result.leaderboards[0].mode == "text"
    assert result.leaderboards[0].message.embeds
    assert "RuntimeError" in result.leaderboards[0].reason
    assert "private" not in result.leaderboards[0].reason
    assert result.leaderboards[1].mode == "image"


@pytest.mark.parametrize("kind", ["missing", "empty", "dimensions", "corrupt"])
def test_invalid_rendered_files_fall_back(season_db, tmp_path, monkeypatch, kind):
    def invalid(frame, path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "empty":
            path.write_bytes(b"")
        elif kind == "dimensions":
            Image.new("RGB", (20, 20)).save(path)
        elif kind == "corrupt":
            path.write_bytes(b"not a PNG")
    monkeypatch.setattr("kcdk.leaderboard_delivery.render_kcdk_standings_png", invalid)
    item = preview(season_db, tmp_path).leaderboards[0]
    assert item.mode == "text" and item.reason and not item.message.files


def test_file_too_large_is_not_uploaded(season_db, commentary, tmp_path):
    client = FakeTransport()
    result = publish_weekly_report(
        season_db, season_identifier="mock-2026", week_label="Week 4",
        commentary=commentary, discord_config=_discord_config(), transport=client,
        state_store=DiscordStateStore(tmp_path / "state.json"),
        leaderboard_config=LeaderboardConfig(max_file_bytes=1, output_directory=tmp_path),
    )
    assert len(client.created) == 3
    assert all(not message.files for _, message, _ in client.created)
    for item in result.leaderboards:
        assert "exceeds configured limit" in item.reason
        assert item.dimensions == (1400, 686)


def test_initial_image_create_and_repeated_edits_preserve_ids(season_db, commentary, tmp_path):
    store = DiscordStateStore(tmp_path / "state.json")
    client = FakeTransport()
    for _ in range(3):
        result = publish_weekly_report(
            season_db, season_identifier="mock-2026", week_label="Week 4",
            commentary=commentary, discord_config=_discord_config(), transport=client,
            state_store=store, leaderboard_config=LeaderboardConfig(output_directory=tmp_path),
        )
    assert len(client.created) == 3  # two boards and one recap only
    assert len(client.edited) == 4
    assert [item.message_id for item in result.leaderboards] == ["message-2", "message-3"]
    assert all(message.files for _, message, _ in client.created[1:])
    assert not client.created[0][1].files
    for _, _, message in client.edited:
        assert len(message.to_payload()["attachments"]) == 1
        assert message.to_payload()["attachments"][0]["id"] == 0


@pytest.mark.parametrize("fallback_fails", [False, True])
def test_image_edit_fallback_once_preserves_state(season_db, commentary, tmp_path, fallback_fails):
    store = DiscordStateStore(tmp_path / "state.json")
    store.save(DiscordState(kcdk_leaderboard_message_id="10", tournament_leaderboard_message_id="20"))

    class FailingImageTransport(FakeTransport):
        def edit_message(self, url, message_id, message):
            self.edited.append((url, message_id, message))
            if message.files or fallback_fails:
                raise DiscordTransportError("Discord webhook request failed (HTTP 503).")

    client = FailingImageTransport()
    kwargs = dict(
        season_identifier="mock-2026", week_label="Week 4", commentary=commentary,
        discord_config=_discord_config(), transport=client, state_store=store,
        leaderboard_config=LeaderboardConfig(output_directory=tmp_path),
    )
    if fallback_fails:
        with pytest.raises(PublishingError, match="text fallback failed"):
            publish_weekly_report(season_db, **kwargs)
        assert len(client.edited) == 2
        assert len(client.created) == 1  # Commissioner already posted.
        assert not client.created[0][1].files
    else:
        result = publish_weekly_report(season_db, **kwargs)
        assert len(client.edited) == 4
        assert len(client.created) == 1  # only weekly recap
        assert all(item.mode == "text" and "PNG edit failed" in item.reason for item in result.leaderboards)
    assert store.load().kcdk_leaderboard_message_id == "10"
    assert store.load().tournament_leaderboard_message_id == "20"
    assert client.edited[0][2].files
    assert client.edited[1][2].to_payload()["attachments"] == []


@pytest.mark.parametrize("rejected", [False, True])
def test_create_failure_never_retries_ambiguous_outcome(season_db, tmp_path, rejected):
    item = preview(season_db, tmp_path).leaderboards[0]
    calls = []

    class Transport(FakeTransport):
        def create_message(self, url, message):
            calls.append(message)
            if len(calls) == 1:
                error = RejectedDiscordMessageError if rejected else DiscordTransportError
                raise error("upload failed")
            return "42"

    if rejected:
        assert deliver_leaderboard(Transport(), VALID_LEADERBOARD_URL, item) == "42"
        assert len(calls) == 2 and not calls[1].files
    else:
        with pytest.raises(DiscordTransportError):
            deliver_leaderboard(Transport(), VALID_LEADERBOARD_URL, item)
        assert len(calls) == 1 and item.message_id is None


def test_real_multipart_encoding_and_attachment_replacement(monkeypatch):
    requests = []
    attachments = []

    def respond(request):
        nonlocal attachments
        content_type = request.headers["content-type"]
        if content_type.startswith("multipart/form-data"):
            mime = BytesParser(policy=default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
                + request.content
            )
            parts = {part.get_param("name", header="content-disposition"): part
                     for part in mime.iter_parts()}
            payload = json.loads(parts["payload_json"].get_payload(decode=True))
            assert parts["files[0]"].get_payload(decode=True) == b"PNG test bytes"
            assert parts["files[0]"].get_content_type() == "image/png"
            assert parts["files[0]"].get_filename() == "board.png"
        else:
            payload = json.loads(request.content)
        if "attachments" in payload:
            attachments = payload["attachments"]
        requests.append((request, payload, list(attachments)))
        return httpx.Response(200, json={"id": "42", "attachments": attachments})

    real_client = httpx.Client
    monkeypatch.setattr("kcdk.discord.httpx.Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    client = DiscordWebhookClient(timeout_seconds=3)
    message = DiscordMessage(content="Board", files=(DiscordAttachment("board.png", b"PNG test bytes"),))
    url = VALID_LEADERBOARD_URL + "?thread_id=7&wait=false"
    assert client.create_message(url, message) == "42"
    for _ in range(3):
        client.edit_message(url, "42", message)
        assert client.attachment_counts["42"] == 1
    client.edit_message(url, "42", DiscordMessage(embeds=({"title": "fallback"},), replace_attachments=True))
    assert client.attachment_counts["42"] == 0
    assert requests[0][0].url.params["wait"] == "true"
    for request, payload, retained in requests[:-1]:
        assert request.url.params["thread_id"] == "7"
        assert payload["allowed_mentions"] == {"parse": []}
        assert len(retained) == 1 and retained[0]["id"] == 0
    assert requests[1][0].url.path.endswith("/messages/42")
    assert requests[1][1]["embeds"] == []
    assert requests[-1][1]["content"] == ""
    assert requests[-1][1]["attachments"] == []


def test_multipart_errors_are_secret_safe_without_retries(monkeypatch):
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout(f"private URL: {request.url}")
    real_client = httpx.Client
    monkeypatch.setattr("kcdk.discord.httpx.Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(fail), **kwargs))
    with pytest.raises(DiscordTransportError) as error:
        DiscordWebhookClient().create_message(VALID_LEADERBOARD_URL, DiscordMessage(
            content="board", files=(DiscordAttachment("board.png", b"png"),)))
    assert "test-board-token" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [400, 413, 429, 503])
def test_http_create_failure_policy(status, season_db, tmp_path, monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status if len(requests) == 1 else 200,
                              json={"id": "42", "attachments": []})
    real_client = httpx.Client
    monkeypatch.setattr("kcdk.discord.httpx.Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    item = preview(season_db, tmp_path).leaderboards[0]
    if status in (400, 413):
        assert deliver_leaderboard(DiscordWebhookClient(), VALID_LEADERBOARD_URL, item) == "42"
        assert len(requests) == 2 and item.mode == "text"
        assert json.loads(requests[1].content)["attachments"] == []
    else:
        with pytest.raises(DiscordTransportError):
            deliver_leaderboard(DiscordWebhookClient(), VALID_LEADERBOARD_URL, item)
        assert len(requests) == 1


@pytest.mark.parametrize("response", [{}, {"id": None}, {"id": ""}, {"id": "invalid"}])
def test_unusable_create_id_is_an_ambiguous_error(response, monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=response)
    real_client = httpx.Client
    monkeypatch.setattr("kcdk.discord.httpx.Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(DiscordTransportError, match="no usable message ID"):
        DiscordWebhookClient().create_message(VALID_LEADERBOARD_URL, DiscordMessage(content="board"))
    assert len(requests) == 1


def test_png_smoke_edits_existing_ids_without_openai_or_recap(tmp_path, monkeypatch, capsys):
    script = Path(__file__).parents[1] / "scripts" / "smoke_discord.py"
    spec = importlib.util.spec_from_file_location("smoke_discord_test", script)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    store = DiscordStateStore(tmp_path / "smoke.json")
    store.save(DiscordState(kcdk_leaderboard_message_id="10", tournament_leaderboard_message_id="20"))
    client = FakeTransport()
    client.attachment_counts = {"10": 1, "20": 1}
    monkeypatch.setattr(smoke, "SMOKE_STATE", store.path)
    monkeypatch.setattr(smoke, "DiscordWebhookClient", lambda **kwargs: client)
    monkeypatch.setattr(smoke.DiscordConfig, "from_env", lambda **kwargs: _discord_config())
    monkeypatch.setattr("kcdk.publishing.generate_weekly_commentary",
                        lambda *a, **k: pytest.fail("OpenAI"))
    assert smoke.main(["--live", "--png-leaderboards"]) == 0
    assert len(client.edited) == 2
    assert not client.created and not client.deleted
    assert all(message.files and "SMOKE TEST" in message.content for _, _, message in client.edited)
    result = json.loads(capsys.readouterr().out)
    assert result["openai_called"] is False and result["weekly_recap_posted"] is False
    assert [item["attachment_count"] for item in result["leaderboards"]] == [1, 1]
    assert [item["message_id"] for item in result["leaderboards"]] == ["10", "20"]
