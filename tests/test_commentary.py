import json
from types import SimpleNamespace

import pytest

from kcdk.commentary import (
    COMMENTARY_PROMPT_VERSION,
    CommentaryAPIError,
    CommentaryConfig,
    CommentaryConfigurationError,
    CommentaryResponseError,
    CommentaryRoast,
    MemberCommentaryContext,
    StructuredCommentary,
    build_commentary_request,
    chunk_discord_markdown,
    generate_weekly_commentary,
    parse_structured_commentary,
    render_discord_markdown,
)
from kcdk.facts import Fact, WeeklyFactReport


def _fact(
    summary="Casey North won Week 4 with 198.0 fantasy points.",
    *,
    completeness="complete",
):
    return Fact(
        fact_type="weekly_winner",
        category="weekly_result",
        summary=summary,
        priority=90,
        season_identifier="mock-2026",
        contest_id=4,
        week_label="Week 4",
        subject_member_id=1,
        subject_member_name="Casey North",
        values={"kcdk_finish": 1, "draftkings_points": 198.0},
        evidence={"source": "member_results"},
        completeness=completeness,
        tags=("winner", "weekly"),
    )


def _report(*, selected=None, candidate=None, warnings=()):
    selected_facts = tuple(selected or [_fact()])
    candidate_facts = tuple(candidate or selected_facts)
    return WeeklyFactReport(
        season_id=1,
        season_identifier="mock-2026",
        season_name="Mock 2026",
        contest_id=4,
        week_label="Week 4",
        generated_candidate_count=len(candidate_facts),
        candidate_facts=candidate_facts,
        selected_facts=selected_facts,
        warnings=tuple(warnings),
    )


def _valid_output(fact_id):
    return {
        "headline": "Week 4 Whiplash",
        "intro": "The slate produced one clean winner.",
        "roasts": [
            {
                "member": "Casey North",
                "text": "Casey took first and made the standings look temporary.",
                "fact_ids": [fact_id],
            }
        ],
        "closing": "Back to the lineup lab.",
    }


class FakeResponses:
    def __init__(self, output_builder=None, error=None):
        self.output_builder = output_builder
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        output = self.output_builder(kwargs)
        return SimpleNamespace(
            id="resp_test",
            model=kwargs["model"],
            status="completed",
            output_text=json.dumps(output),
            usage=SimpleNamespace(
                input_tokens=321,
                output_tokens=87,
                total_tokens=408,
            ),
        )


class FakeClient:
    def __init__(self, output_builder=None, error=None):
        self.responses = FakeResponses(output_builder=output_builder, error=error)


def _config():
    return CommentaryConfig(api_key="test-only-key", model="test-model")


def test_absent_key_has_clear_configuration_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(CommentaryConfigurationError, match="OPENAI_API_KEY"):
        generate_weekly_commentary(_report())


def test_dry_run_needs_no_key_and_makes_no_request(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = FakeClient(lambda kwargs: {})

    result = generate_weekly_commentary(_report(), client=client, dry_run=True)

    assert result.dry_run is True
    assert result.commentary is None
    assert client.responses.calls == []
    assert result.request.to_dict()["api_arguments"]["store"] is False
    assert "api_key" not in json.dumps(result.to_dict()).lower()


def test_request_contains_only_selected_compact_facts_and_safe_context():
    selected = _fact()
    candidate = _fact("DO NOT SEND THIS CANDIDATE")
    report = _report(selected=[selected], candidate=[selected, candidate])
    context = {
        "Casey North": MemberCommentaryContext(
            display_name="Casey North",
            nickname="North Star",
            commentary_notes="Loves a good space pun.\nKeep it light.",
        ),
        "Not In Report": {
            "display_name": "Not In Report",
            "commentary_notes": "Must not be sent",
        },
    }

    request = build_commentary_request(report, member_context=context)
    payload = json.loads(request.input_json)

    assert len(payload["selected_facts"]) == 1
    assert "candidate_facts" not in payload
    assert "DO NOT SEND THIS CANDIDATE" not in request.input_json
    assert payload["member_context"] == [
        {
            "display_name": "Casey North",
            "nickname": "North Star",
            "commentary_notes": "Loves a good space pun. Keep it light.",
        }
    ]
    assert request.input_bytes == len(request.input_json.encode("utf-8"))


def test_prompt_has_fact_causality_partial_and_safety_restrictions():
    instructions = build_commentary_request(_report()).instructions.lower()

    for phrase in (
        "use only claims",
        "do not invent",
        "every roast must cite",
        'completeness="partial"',
        "do not imply that a player choice caused",
        "no attacks involving protected traits",
        "fantasy-sports performance",
    ):
        assert phrase in instructions


@pytest.mark.parametrize("tone", ["mild", "normal", "ruthless"])
def test_commissioner_prompt_explicitly_frames_weekly_dfs(tone):
    instructions = " ".join(build_commentary_request(_report(), tone=tone).instructions.split())
    assert "KCDK is daily fantasy sports" in instructions
    assert "Each week is a separate DraftKings DFS contest with a newly constructed lineup" in instructions
    assert "in separate weekly DFS lineups N times" in instructions
    assert "Consecutive usage means a new selection" in instructions
    assert "Season standings aggregate those separate contests" in instructions
    assert "finished last in N separate KCDK contests" in instructions
    assert "Field ownership describes entrants selecting a player on a slate" in instructions
    forbidden = instructions.split("Do not use language implying persistent teams or player ownership", 1)[1].split("Prefer lineup", 1)[0]
    for phrase in ("drafted Player X", "kept Player X", "held Player X", "traded for",
                   "waiver pickup", "bench", "own Player X", "your team all season", "rostered all season"):
        assert phrase in forbidden
    assert "NO persistent fantasy rosters, season-long owned players, trades, waivers, bench decisions, keeper decisions" in instructions


@pytest.mark.parametrize("tone", ["mild", "normal", "ruthless"])
def test_supported_tones_are_explicit_in_prompt(tone):
    request = build_commentary_request(_report(), tone=tone)
    assert request.tone == tone
    assert f"TONE PROFILE: {tone}" in request.instructions


def test_unknown_tone_is_rejected():
    with pytest.raises(ValueError, match="Unknown commentary tone"):
        build_commentary_request(_report(), tone="radioactive")


def test_partial_fact_and_warning_are_preserved_for_model():
    request = build_commentary_request(
        _report(
            selected=[_fact(completeness="partial")],
            warnings=("Week 4 prize data is incomplete.",),
        )
    )
    payload = json.loads(request.input_json)
    assert payload["selected_facts"][0]["completeness"] == "partial"
    assert payload["data_warnings"] == ["Week 4 prize data is incomplete."]


def test_responses_api_structured_request_parses_and_tracks_usage():
    client = FakeClient(
        lambda kwargs: _valid_output(
            json.loads(kwargs["input"])["selected_facts"][0]["fact_id"]
        )
    )

    result = generate_weekly_commentary(
        _report(), config=_config(), client=client
    )

    assert result.commentary.headline == "Week 4 Whiplash"
    assert result.commentary.roasts[0].fact_ids == result.request.fact_ids
    assert result.response_id == "resp_test"
    assert result.usage.total_tokens == 408
    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["strict"] is True
    assert call["metadata"]["prompt_version"] == COMMENTARY_PROMPT_VERSION


def test_official_sdk_serializes_request_with_mock_transport():
    import httpx
    from openai import OpenAI

    captured = {}

    def handler(request):
        request_payload = json.loads(request.content)
        captured["request_payload"] = request_payload
        fact_id = json.loads(request_payload["input"])["selected_facts"][0][
            "fact_id"
        ]
        response_payload = {
            "id": "resp_sdk_mock",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": request_payload["model"],
            "output": [
                {
                    "id": "msg_sdk_mock",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(_valid_output(fact_id)),
                            "annotations": [],
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        }
        return httpx.Response(200, json=response_payload, request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with OpenAI(api_key="test-only", http_client=http_client) as client:
        result = generate_weekly_commentary(
            _report(), config=_config(), client=client
        )

    assert result.response_id == "resp_sdk_mock"
    assert result.usage.total_tokens == 15
    assert captured["request_payload"]["text"]["format"]["type"] == "json_schema"
    assert captured["request_payload"]["store"] is False


def test_generation_metadata_and_output_are_json_serializable():
    client = FakeClient(
        lambda kwargs: _valid_output(
            json.loads(kwargs["input"])["selected_facts"][0]["fact_id"]
        )
    )
    result = generate_weekly_commentary(
        _report(), config=_config(), client=client
    )

    payload = json.loads(result.to_json())
    assert payload["dry_run"] is False
    assert payload["prompt_version"] == COMMENTARY_PROMPT_VERSION
    assert payload["fact_ids"] == list(result.request.fact_ids)
    assert payload["usage"] == {
        "input_tokens": 321,
        "output_tokens": 87,
        "total_tokens": 408,
    }


@pytest.mark.parametrize(
    "response_text",
    ["not json", "[]", '{"headline":"Only one field"}'],
)
def test_malformed_structured_response_is_rejected(response_text):
    with pytest.raises(CommentaryResponseError):
        parse_structured_commentary(response_text, allowed_fact_ids=["fact_ok"])


def test_unknown_or_missing_fact_ids_are_rejected():
    unknown = _valid_output("fact_made_up")
    with pytest.raises(CommentaryResponseError, match="unknown fact ID"):
        parse_structured_commentary(
            json.dumps(unknown), allowed_fact_ids=["fact_real"]
        )

    missing = _valid_output("fact_real")
    missing["roasts"][0]["fact_ids"] = []
    with pytest.raises(CommentaryResponseError, match="without valid fact IDs"):
        parse_structured_commentary(
            json.dumps(missing), allowed_fact_ids=["fact_real"]
        )


def test_roast_member_must_be_supported_by_its_fact_ids():
    output = _valid_output("fact_real")
    output["roasts"][0]["member"] = "Taylor Quinn"

    with pytest.raises(CommentaryResponseError, match="unsupported member"):
        parse_structured_commentary(
            json.dumps(output),
            allowed_fact_ids=["fact_real"],
            allowed_members_by_fact_id={"fact_real": ("Casey North",)},
        )


def test_api_errors_are_sanitized_and_one_call_only():
    secret = "SENSITIVE_TEST_VALUE_THAT_MUST_NOT_ESCAPE"
    client = FakeClient(error=RuntimeError(f"transport failed for {secret}"))

    with pytest.raises(CommentaryAPIError) as captured:
        generate_weekly_commentary(
            _report(), config=_config(), client=client
        )

    assert secret not in str(captured.value)
    assert "RuntimeError" in str(captured.value)
    assert len(client.responses.calls) == 1


def test_discord_markdown_renderer_and_chunking():
    commentary = StructuredCommentary(
        headline="Week @everyone",
        intro="A long night for the league.",
        roasts=(
            CommentaryRoast(
                member="Casey North",
                text="First place, no committee meeting required. " * 5,
                fact_ids=("fact_123",),
            ),
            CommentaryRoast(
                member="Taylor Quinn",
                text="The standings remain stubborn.",
                fact_ids=("fact_456",),
            ),
        ),
        closing="See you next week, @here.",
    )

    markdown = render_discord_markdown(commentary)
    debug_markdown = render_discord_markdown(commentary, include_fact_ids=True)
    chunks = chunk_discord_markdown(markdown, max_chars=120)

    assert markdown.startswith("## Week")
    assert "**Casey North:**" in markdown
    assert "fact_123" not in markdown
    assert "*Facts: fact_123*" in debug_markdown
    assert "@everyone" not in markdown
    assert "@here" not in markdown
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 120 for chunk in chunks)


def test_config_repr_and_validation_do_not_expose_secret():
    config = CommentaryConfig(api_key="private-test-value")
    assert "private-test-value" not in repr(config)
    with pytest.raises(CommentaryConfigurationError):
        CommentaryConfig(api_key="x", max_retries=6)
