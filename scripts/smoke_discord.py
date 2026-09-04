"""Explicit Discord webhook smoke test using fictional KCDK data.

Without ``--live`` this renders a safe preview. With ``--live`` it posts and
deletes one clearly labeled weekly test message, then creates or edits two
clearly labeled persistent test leaderboard messages. No OpenAI call is made.
With ``--png-leaderboards``, only the two persistent leaderboards are updated;
no weekly message is posted or deleted.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kcdk.discord import (  # noqa: E402
    DiscordConfig,
    DiscordError,
    DiscordMessage,
    DiscordWebhookClient,
)
from kcdk.leaderboard_delivery import LeaderboardConfig, deliver_leaderboard  # noqa: E402
from kcdk.persistence import connect_database, import_week  # noqa: E402
from kcdk.publishing import (  # noqa: E402
    DiscordStateStore,
    PublishingError,
    publish_weekly_report,
)


SMOKE_STATE = ROOT / "data" / "processed" / "discord_smoke_state.json"


def _mock_database():
    temporary = tempfile.TemporaryDirectory(prefix="kcdk-discord-smoke-")
    connection = connect_database(Path(temporary.name) / "smoke.sqlite")
    for week in range(1, 5):
        import_week(
            connection,
            ROOT / "data" / "mock" / f"season_week_{week}.csv",
            ROOT / "data" / "mock" / "members.csv",
            season_name="Mock 2026",
            season_identifier="mock-2026",
            season_year=2026,
            week_number=week,
            contest_name=f"Fictional Week {week}",
            contest_date=f"2026-09-{week:02d}",
        )
    return temporary, connection


def _label_message(message: DiscordMessage, label: str) -> DiscordMessage:
    embeds = []
    for embed in message.embeds:
        labeled = dict(embed)
        labeled["title"] = f"[SMOKE TEST] {labeled.get('title', label)}"
        embeds.append(labeled)
    return replace(message, content=f"[SMOKE TEST] {message.content}" if message.content else "",
                   embeds=tuple(embeds))


def _upsert_test_leaderboard(
    client: DiscordWebhookClient,
    webhook_url: str,
    message: DiscordMessage,
    message_id: str | None,
) -> tuple[str, str]:
    if message_id:
        client.edit_message(webhook_url, message_id, message)
        return message_id, "edited"
    return client.create_message(webhook_url, message), "created"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Perform the explicitly controlled webhook smoke sequence.",
    )
    parser.add_argument(
        "--png-leaderboards", action="store_true",
        help="Render and update only the two persistent PNG test leaderboards; no recap.",
    )
    arguments = parser.parse_args(argv)
    temporary, connection = _mock_database()
    try:
        preview = publish_weekly_report(
            connection,
            season_identifier="mock-2026",
            week_label="Week 4",
            dry_run=True,
            state_store=DiscordStateStore(SMOKE_STATE),
            leaderboard_config=replace(
                LeaderboardConfig.from_env(ROOT / ".env"),
                mode="image" if arguments.png_leaderboards else "text",
                output_directory=ROOT / "output" / "leaderboards",
            ),
        )
        if not arguments.live:
            print(json.dumps(preview.to_dict(), indent=2, sort_keys=True))
            print("Live Discord smoke test skipped; rerun with --live intentionally.")
            return 0

        config = DiscordConfig.from_env(require_webhooks=not arguments.png_leaderboards)
        client = DiscordWebhookClient(timeout_seconds=config.timeout_seconds)
        weekly_url = config.weekly_webhook_url
        leaderboard_url = config.leaderboard_webhook_url
        if leaderboard_url is None:
            raise DiscordError("The leaderboard webhook is required.")

        if arguments.png_leaderboards:
            store = DiscordStateStore(SMOKE_STATE)
            state = store.load()
            for item, attribute in zip(preview.leaderboards, (
                "kcdk_leaderboard_message_id", "tournament_leaderboard_message_id",
            )):
                item.message = _label_message(item.message, item.target)
                item.fallback = _label_message(item.fallback, item.target)
                message_id = deliver_leaderboard(client, leaderboard_url, item)
                setattr(state, attribute, message_id)
                store.save(state)
            print(json.dumps({
                "leaderboards": [item.to_dict() for item in preview.leaderboards],
                "weekly_recap_posted": False, "openai_called": False,
            }, indent=2, sort_keys=True))
            return 0 if all(
                item.mode == "image" and item.attachment_count in (None, 1)
                for item in preview.leaderboards
            ) else 1

        if weekly_url is None:
            raise DiscordError("The weekly webhook is required.")

        temporary_message = DiscordMessage(
            content=(
                "**[KCDK DISCORD SMOKE TEST]** Temporary weekly-results message. "
                "This message should be deleted automatically."
            )
        )
        temporary_id = client.create_message(weekly_url, temporary_message)
        client.delete_message(weekly_url, temporary_id)

        store = DiscordStateStore(SMOKE_STATE)
        state = store.load()
        kcdk_message = _label_message(
            preview.kcdk_leaderboard_message, "KCDK standings"
        )
        tournament_message = _label_message(
            preview.tournament_leaderboard_message, "Tournament earnings"
        )
        kcdk_id, kcdk_action = _upsert_test_leaderboard(
            client,
            leaderboard_url,
            kcdk_message,
            state.kcdk_leaderboard_message_id,
        )
        state.kcdk_leaderboard_message_id = kcdk_id
        store.save(state)
        tournament_id, tournament_action = _upsert_test_leaderboard(
            client,
            leaderboard_url,
            tournament_message,
            state.tournament_leaderboard_message_id,
        )
        state.tournament_leaderboard_message_id = tournament_id
        store.save(state)
        print(
            json.dumps(
                {
                    "temporary_weekly_message": "created_and_deleted",
                    "kcdk_leaderboard": kcdk_action,
                    "kcdk_message_id": kcdk_id,
                    "tournament_leaderboard": tournament_action,
                    "tournament_message_id": tournament_id,
                    "openai_called": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (DiscordError, PublishingError) as exc:
        print(f"Discord smoke test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
