"""Free persona eligibility/payload preview, or explicitly opt-in generation/posting."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from scripts.smoke_commentary import _mock_report  # noqa: E402
from kcdk.commentary import CommentaryError  # noqa: E402
from kcdk.discord import DiscordConfig, DiscordError, DiscordWebhookClient  # noqa: E402
from kcdk.drunk_bot import DrunkBotConfig  # noqa: E402
from kcdk.drunk_bot_commentary import generate_drunk_bot_commentary  # noqa: E402
from kcdk.drunk_bot_publishing import plan_drunk_bot, publish_drunk_bot, DrunkBotRecord  # noqa: E402
from kcdk.publishing import DiscordStateStore, PublishingError  # noqa: E402

DEFAULT_GENERATION = ROOT / "output" / "preview" / "drunk_bot_generation.json"
SMOKE_STATE = ROOT / "data" / "processed" / "drunk_bot_smoke_state.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Make ONE paid OpenAI request; do not post.")
    mode.add_argument("--post", action="store_true", help="Post a saved generation to the optional Drunk Bot webhook; no API generation.")
    parser.add_argument("--generation", type=Path, default=DEFAULT_GENERATION)
    parser.add_argument("--recover", action="store_true", help="Recover a definitely rejected smoke post using saved text.")
    args = parser.parse_args(argv)
    temporary, connection, report = _mock_report()
    try:
        config = DrunkBotConfig.from_env()
        if not args.post:
            result = generate_drunk_bot_commentary(report, config=config, dry_run=not args.live)
            if args.live and result.commentary:
                args.generation.parent.mkdir(parents=True, exist_ok=True)
                args.generation.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
            print(json.dumps(result.to_dict(), indent=2))
            return 0

        # Rebuild the request from verified mock facts, never trust IDs/facts in a file.
        preview = generate_drunk_bot_commentary(report, config=config, dry_run=True)
        discord = DiscordConfig.from_env(require_webhooks=False)
        store = DiscordStateStore(SMOKE_STATE)
        state = store.load()
        key = "drunk-bot-mock-2026:4"
        record = state.drunk_bot_weeks.get(key)
        plan = plan_drunk_bot(
            report, config, destination_available=bool(discord.drunk_webhook_url),
            record=record, commissioner_already_posted=record is not None, recover=args.recover,
        )
        if plan.would_post_to_discord and not (record and record.generation):
            saved = json.loads(args.generation.read_text(encoding="utf-8"))
            saved["request"] = preview.request
            record = DrunkBotRecord("ready", preview.eligibility.to_dict(), saved)

        def save(value):
            state.drunk_bot_weeks[key] = value
            store.save(state)

        class SmokeTransport:
            def create_message(self, url, message):
                return DiscordWebhookClient(timeout_seconds=discord.timeout_seconds).create_message(
                    url, replace(message, content="[SMOKE TEST]\n" + message.content),
                )

        result = publish_drunk_bot(
            report, config, plan, record=record, webhook_url=discord.drunk_webhook_url,
            client=SmokeTransport(), save=save,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.status in {"posted", "already_posted"} else 1
    except (CommentaryError, DiscordError, PublishingError, ValueError, OSError):
        print("Drunk Bot smoke failed; check configuration or the saved generation file.", file=sys.stderr)
        return 1
    finally:
        connection.close()
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
