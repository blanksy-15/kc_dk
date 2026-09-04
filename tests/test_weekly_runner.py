from pathlib import Path
from types import SimpleNamespace

import pytest

from kcdk.persistence import connect_database, import_week
from kcdk.publishing import DiscordState, DiscordStateStore, PublicationRecord
from kcdk.weekly_runner import (
    LocalConfigStore,
    LocalWeeklyConfig,
    WeeklyRunnerError,
    build_preflight,
    confirmation_is_yes,
    run_interactive_weekly,
    suggest_next_week,
)


ROOT = Path(__file__).parents[1]
MOCK = ROOT / "data" / "mock"
MEMBERS = MOCK / "members.csv"
WEEK_ONE = MOCK / "season_week_1.csv"


def _config(tmp_path, **overrides):
    values = {
        "season_identifier": "mock-2026",
        "season_name": "Mock 2026",
        "season_year": 2026,
        "members_path": str(MEMBERS),
        "default_tone": "normal",
        "database_path": str(tmp_path / "kcdk.sqlite"),
        "state_path": str(tmp_path / "state.json"),
    }
    values.update(overrides)
    return LocalWeeklyConfig(**values)


def _save_config(tmp_path, config):
    path = tmp_path / "local_config.json"
    LocalConfigStore(path).save(config)
    return path


def test_first_run_config_creation_and_reload(tmp_path):
    path = tmp_path / "local_config.json"
    answers = iter(("mock-2026", "Mock 2026", "2026", str(MEMBERS), "normal"))
    output = []

    result = run_interactive_weekly(
        csv_path=WEEK_ONE,
        week_number=1,
        config_path=path,
        dry_run=True,
        input_func=lambda prompt: next(answers),
        output_func=output.append,
    )

    loaded = LocalConfigStore(path).load()
    assert result.status == "dry_run"
    assert loaded == LocalWeeklyConfig(
        season_identifier="mock-2026",
        season_name="Mock 2026",
        members_path=str(MEMBERS),
        default_tone="normal",
        season_year=2026,
    )
    assert "api_key" not in path.read_text(encoding="utf-8").casefold()
    assert "webhook" not in path.read_text(encoding="utf-8").casefold()


def test_config_refuses_secret_or_webhook_fields(tmp_path):
    path = tmp_path / "local_config.json"
    path.write_text(
        '{"season_identifier":"x","season_name":"x","api_key":"never"}',
        encoding="utf-8",
    )
    with pytest.raises(WeeklyRunnerError, match="Refusing to store secret"):
        LocalConfigStore(path).load()


def test_next_week_suggestion_uses_latest_import(tmp_path):
    database = tmp_path / "season.sqlite"
    connection = connect_database(database)
    try:
        import_week(
            connection,
            WEEK_ONE,
            MEMBERS,
            season_name="Mock 2026",
            season_identifier="mock-2026",
            season_year=2026,
            week_number=1,
        )
    finally:
        connection.close()

    assert suggest_next_week(database, "mock-2026") == 2
    assert suggest_next_week(tmp_path / "missing.sqlite", "mock-2026") == 1
    assert not (tmp_path / "missing.sqlite").exists()


def test_file_picker_cancel_has_no_local_side_effects(tmp_path):
    config_path = tmp_path / "local_config.json"
    output = []
    result = run_interactive_weekly(
        config_path=config_path,
        file_selector=lambda: None,
        input_func=lambda prompt: pytest.fail("cancel prompted for input"),
        output_func=output.append,
    )

    assert result.status == "cancelled"
    assert not config_path.exists()
    assert "Nothing was imported or published" in output[-1]


def test_missing_member_file_blocks_preflight(tmp_path):
    result = build_preflight(
        csv_path=WEEK_ONE,
        config=_config(tmp_path, members_path=str(tmp_path / "missing.csv")),
        week_number=1,
        dry_run=True,
    )
    assert not result.can_continue
    assert "Member configuration does not exist" in result.blocking_errors[0]


def test_zero_member_matches_blocks_preflight(tmp_path):
    members = tmp_path / "members.csv"
    members.write_text(
        "member_key,draftkings_name,display_name,nickname,active,notes\n"
        "outsider,not-in-contest,Outsider,,true,\n",
        encoding="utf-8",
    )
    result = build_preflight(
        csv_path=WEEK_ONE,
        config=_config(tmp_path, members_path=str(members)),
        week_number=1,
        dry_run=True,
    )
    assert not result.can_continue
    assert any(
        "Zero active KCDK members matched" in message
        for message in result.blocking_errors
    )


def test_missing_required_csv_columns_block_preflight(tmp_path):
    invalid = tmp_path / "invalid.csv"
    invalid.write_text("Rank,EntryId\n1,100\n", encoding="utf-8")
    result = build_preflight(
        csv_path=invalid,
        config=_config(tmp_path),
        week_number=1,
        dry_run=True,
    )
    assert not result.can_continue
    assert any(
        "Missing required DraftKings fields" in item
        for item in result.blocking_errors
    )


def test_preflight_reports_existing_and_published_week(tmp_path):
    config = _config(tmp_path)
    connection = connect_database(config.database_path)
    try:
        import_week(
            connection,
            WEEK_ONE,
            MEMBERS,
            season_name=config.season_name,
            season_identifier=config.season_identifier,
            season_year=config.season_year,
            week_number=1,
        )
        contest_id = connection.execute("SELECT id FROM contests").fetchone()[0]
    finally:
        connection.close()
    DiscordStateStore(config.state_path).save(
        DiscordState(
            published_weeks={
                f"mock-2026:{contest_id}": PublicationRecord(
                    message_ids=("weekly-1",),
                    published_at="2026-09-04T00:00:00+00:00",
                )
            }
        )
    )

    result = build_preflight(
        csv_path=WEEK_ONE, config=config, week_number=1, dry_run=True
    )
    assert result.can_continue
    assert result.week_exists
    assert result.exact_source_already_imported
    assert result.already_published


def test_existing_week_with_changed_source_is_blocked(tmp_path):
    config = _config(tmp_path)
    connection = connect_database(config.database_path)
    try:
        import_week(
            connection,
            WEEK_ONE,
            MEMBERS,
            season_name=config.season_name,
            season_identifier=config.season_identifier,
            week_number=1,
        )
    finally:
        connection.close()

    result = build_preflight(
        csv_path=MOCK / "season_week_2.csv",
        config=config,
        week_number=1,
        dry_run=True,
    )
    assert not result.can_continue
    assert any("different CSV" in message for message in result.blocking_errors)


def test_blank_confirmation_is_no():
    assert not confirmation_is_yes("")
    assert not confirmation_is_yes("n")
    assert confirmation_is_yes("YES")


def test_live_default_no_never_opens_database_or_calls_workflow(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    config_path = _save_config(tmp_path, config)
    monkeypatch.setattr(
        "kcdk.weekly_runner._configuration_status",
        lambda: (True, True, "test-model", ()),
    )
    monkeypatch.setattr(
        "kcdk.weekly_runner.connect_database",
        lambda *args, **kwargs: pytest.fail("database opened before confirmation"),
    )
    monkeypatch.setattr(
        "kcdk.weekly_runner.run_weekly_workflow",
        lambda *args, **kwargs: pytest.fail("workflow ran before confirmation"),
    )

    result = run_interactive_weekly(
        csv_path=WEEK_ONE,
        week_number=1,
        config_path=config_path,
        input_func=lambda prompt: "",
        output_func=lambda message: None,
    )
    assert result.status == "declined"
    assert not Path(config.database_path).exists()


def test_already_published_week_requires_explicit_repost(tmp_path, monkeypatch):
    config = _config(tmp_path)
    connection = connect_database(config.database_path)
    try:
        import_week(
            connection,
            WEEK_ONE,
            MEMBERS,
            season_name=config.season_name,
            season_identifier=config.season_identifier,
            week_number=1,
        )
        contest_id = connection.execute("SELECT id FROM contests").fetchone()[0]
    finally:
        connection.close()
    DiscordStateStore(config.state_path).save(
        DiscordState(
            published_weeks={
                f"mock-2026:{contest_id}": PublicationRecord(
                    message_ids=("weekly-1",),
                    published_at="2026-09-04T00:00:00+00:00",
                )
            }
        )
    )
    config_path = _save_config(tmp_path, config)
    monkeypatch.setattr(
        "kcdk.weekly_runner._configuration_status",
        lambda: (True, True, "test-model", ()),
    )
    monkeypatch.setattr(
        "kcdk.weekly_runner.run_weekly_workflow",
        lambda *args, **kwargs: pytest.fail("published week reposted without approval"),
    )

    result = run_interactive_weekly(
        csv_path=WEEK_ONE,
        week_number=1,
        config_path=config_path,
        input_func=lambda prompt: "",
        output_func=lambda message: None,
    )
    assert result.status == "already_published"


def test_dry_run_has_no_network_or_production_database_side_effects(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    config_path = _save_config(tmp_path, config)
    monkeypatch.setattr(
        "kcdk.publishing.generate_weekly_commentary",
        lambda *args, **kwargs: pytest.fail("dry run called OpenAI"),
    )
    monkeypatch.setattr(
        "kcdk.publishing.DiscordWebhookClient",
        lambda *args, **kwargs: pytest.fail("dry run created Discord transport"),
    )

    result = run_interactive_weekly(
        csv_path=WEEK_ONE,
        week_number=1,
        config_path=config_path,
        dry_run=True,
        input_func=lambda prompt: pytest.fail("dry run requested live confirmation"),
        output_func=lambda message: None,
    )
    assert result.status == "dry_run"
    assert result.workflow.publication.commentary_source == "deterministic_preview"
    assert not Path(config.database_path).exists()
    assert not Path(config.state_path).exists()


def test_confirmed_live_run_invokes_pipeline_once(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config_path = _save_config(tmp_path, config)
    monkeypatch.setattr(
        "kcdk.weekly_runner._configuration_status",
        lambda: (True, True, "test-model", ()),
    )
    calls = []

    class FakeConnection:
        def close(self):
            return None

    fake_result = SimpleNamespace(
        import_summary=SimpleNamespace(
            matched_kcdk_members=5, lineup_player_rows_stored=15
        ),
        publication=SimpleNamespace(
            operations=(
                SimpleNamespace(target="kcdk_leaderboard", action="edit"),
                SimpleNamespace(target="tournament_leaderboard", action="edit"),
                SimpleNamespace(target="weekly_recap", action="create"),
            ),
            commentary_source="openai",
        ),
    )
    monkeypatch.setattr(
        "kcdk.weekly_runner.connect_database", lambda path: FakeConnection()
    )

    def fake_workflow(connection, **kwargs):
        calls.append(kwargs)
        return fake_result

    monkeypatch.setattr("kcdk.weekly_runner.run_weekly_workflow", fake_workflow)
    result = run_interactive_weekly(
        csv_path=WEEK_ONE,
        week_number=1,
        config_path=config_path,
        input_func=lambda prompt: "yes",
        output_func=lambda message: None,
    )
    assert result.status == "published"
    assert len(calls) == 1
    assert calls[0]["dry_run"] is False
    assert calls[0]["repost"] is False


def test_launcher_prefers_project_virtual_environment():
    launcher = (ROOT / "Run KCDK Weekly.bat").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in launcher
    assert '-c "import kcdk"' in launcher
    assert "-m kcdk weekly-runner" in launcher
    assert "pip install -r requirements.txt" in launcher
    assert "pip install" not in launcher.split('if not exist "%KCDK_PYTHON%" (')[0]
