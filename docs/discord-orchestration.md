# Discord orchestration

This phase adds a bot above the existing import, analytics, fact, commentary,
and PNG rendering layers. The original `weekly`, `weekly-runner`, webhook
transport and their publication state remain available. Use `results` for the
new independent channel flow; do not publish the same contest through both flows.

## Process and boundaries

`python -m kcdk discord-bot` is the long-running listener. `discord.py` receives
gateway events. `DiscordOrchestrator` accepts plain `ChannelMessage` objects and
an injected `ChannelTransport`, RNG, clock and general-reply generator. It has no
analytics or import responsibilities. The listener never processes result files.

`python -m kcdk results` is a separate, manually invoked process. It uses the
same channel configuration and orchestration SQLite state. It does not open a
gateway session. `prepare_result` imports locally and builds a fact report and
images for exactly one contest type; `publish_result` handles delivery.

The bot REST adapter uses existing `DiscordMessage` validation and attachment
payloads, a 15-second timeout, disabled mentions and no automatic network retries.
Channel guild/type and message ownership are checked before persistent edits.
Only configured persistent leaderboard channels accept edits. The adapter follows
the [Discord message API](https://docs.discord.com/developers/resources/message).
Gateway setup uses the [discord.py client API](https://discordpy.readthedocs.io/en/stable/api.html).

Run one listener and one result operator at a time. The listener can coexist with
the operator. SQLite reservations prevent duplicate operation claims across
processes, but concurrent result jobs could render different database snapshots;
serialize result jobs to keep current standings current.

## Local configuration

Copy `data/discord_config.example.json` to ignored `data/discord_config.json`.
All sample snowflakes are placeholders. Supply:

| Setting | Purpose |
| --- | --- |
| `guild_id` | Your Discord server |
| `general_channel_id` | Announcements and conversation |
| `h2h_channel_id` | Official H2H links and historical H2H results |
| `tournament_channel_id` | Official tournament links and historical tournament results |
| `h2h_leaderboard_channel_id` | Current H2H standings |
| `tournament_leaderboard_channel_id` | Current tournament standings |
| `authorized_poster_ids` | Your user ID and any other official link posters; empty denies all |
| `season_identifier`, `season_name`, `season_year` | Match your existing analytics season |
| `announcement_week` | Optional short label such as `Week 2`; no automatic DK lookup |
| `members_path`, `database_path` | Existing roster and analytics SQLite |
| `state_path` | Separate orchestration SQLite; never point at the old JSON state |
| `h2h_dk_fee_cents` | Actual total H2H contest rake in integer cents; required for H2H import |
| `commissioner_enabled` | Live result generation; false uses deterministic fact-based recap |
| `results_drunk_enabled` | Additional switch above existing `DRUNK_BOT_ENABLED` |

H2H, tournament and general channels must be distinct. Leaderboard channels must
be separate from those three; the two leaderboard types may share a leaderboard
channel. State still stores separate type-scoped IDs. Local file paths are
relative to the working directory, so run commands from the repository root.

Put `DISCORD_BOT_TOKEN` in the environment or ignored `.env`. Use the **same bot
account** for the listener and results command. Existing `OPENAI_API_KEY`,
`OPENAI_MODEL`, timeouts and `DRUNK_BOT_*` settings remain in effect. Never put
secrets in JSON. No token or model key is needed for dry runs, config checks or
the offline smoke test. The new bot does not reuse webhook-owned message IDs;
existing webhooks cannot edit bot-owned messages and vice versa.

Install the optional gateway dependency with `python -m pip install -e ".[discord]"`.
In the Discord developer portal, enable **Message Content Intent**, invite the
application as a bot, and grant View Channel, Send Messages, Read Message History,
Embed Links and Attach Files in the configured text channels. No Administrator,
Manage Messages or privileged member-list access is needed. Validate IDs locally
with `python -m kcdk discord-bot --config data/discord_config.json --check-config`.
Starting without `--check-config` connects to Discord and may call OpenAI when
conversation is eligible. Stop with Ctrl+C.

## Announcements

Only allowlisted human messages in the official H2H/tournament channels qualify.
One HTTPS DraftKings contest link is required. Supported paths include
`/contest/123`, `/draft/contest/123` and `/contest/draftteam/123` on
`draftkings.com` or `www.draftkings.com`; query strings are preserved. Shortened,
marketing, sportsbook, spoofed host and ambiguous multi-contest links are ignored.
There is no scraping, redirect-following or DraftKings login.

The source channel determines type. Commissioner-style copy includes type, $5
single-entry H2H or $3 multi-entry tournament terms, configured week when present,
and the official channel/link. `announcement_templates` is editable; placeholders
are `{week}`, `{label}`, `{fee}`, `{channel_id}`, `{url}`. Template choice is stable
for a source message and never costs an OpenAI request.

The source post is never edited/deleted. `announcement:guild:source_message_id`
records source ID/channel, type, URL, week, output ID and timestamp. Duplicate
events or service restarts cannot announce that source again. A new manually
posted source message is a new announcement even if it repeats a URL.

`general_drunk.announcement_followups` defaults to false. When enabled, one
optional conversation-only reply can target the Commissioner announcement. It
shares the conversation gate and has a separate durable operation linked to the
source. It cannot loop on bot messages or repeat on a duplicate source event.

## GENERAL_DRUNK

This mode receives only recent human conversation from configured general chat,
not analytics, standings or member history. Its versioned prompt forbids invented
statistics, scores, money, records and streaks, and requires attribution when
riffing on a member's unverified claim. Message bodies are untrusted quoted input.
This prompt constraint is not a semantic proof; review early replies and disable
the mode if the tone or grounding is unsuitable.

Defaults are 8% probability, 720-second global cooldown, 1,800-second per-user
cooldown, three attempts per rolling hour, last 15 messages, 500 characters per
message, 200 output tokens and a 400-character reply cap. Each is bounded and
configurable in `general_drunk`; model defaults to `OPENAI_MODEL`. Tone is normal
or ruthless. Profanity must be permitted by both this mode's configuration and
the existing `DRUNK_BOT_ALLOW_PROFANITY` setting.

The listener ignores bots, itself, webhooks, other guilds/channels, command-like
messages, URL-only messages and messages below the length threshold. Probability,
cooldowns and hourly budget are checked **before** generation. An eligible attempt
reserves both budget and publication state before the model call; failures consume
budget. No automatic model retries. Replies reference the triggering Discord
message and suppress reply pings. RNG and clock injection make checks testable.

Recent context and cooldowns are memory-only and reset on restart. Durable source
operation IDs prevent repeating a reply already attempted before restart. Run one
listener: multiple listeners have separate in-memory cooldown budgets.

## Results and persona isolation

H2H and tournament can run separately, together, or neither. Sources are explicit:

```powershell
# All commands below are offline dry runs.
python -m kcdk results --config data/discord_config.json --week-number 2 --h2h-csv data/raw/week2_h2h.csv --dry-run
python -m kcdk results --config data/discord_config.json --week-number 2 --tournament-csv data/test_tourney_results.csv --tournament-payout data/test_tourney_payout.csv --dry-run
python -m kcdk results --config data/discord_config.json --week-number 2 --h2h-csv data/raw/week2_h2h.csv --tournament-csv data/test_tourney_results.csv --tournament-payout data/test_tourney_payout.csv --dry-run
python -m kcdk results
```

Replace week/source filenames with the actual contest. A tournament payout is
required unless `--allow-unknown-tournament-payouts` is explicit. Unknown gross/net
stays unknown. H2H uses the existing strict $5 one-entry model and fee validation.
Tournament uses the existing $3-per-entry imports, payouts and member net results.

Preflight copies any existing databases into memory, performs imports/migrations
there, and renders PNGs in a temporary directory. It prints selected types,
source/payout paths, import summaries, historical destination, persistent
destination/saved ID, persona switches, fact types and deterministic recap text.
Dry run is the default; it never calls OpenAI or Discord and never writes the
production analytics/state paths. Preview PNGs are temporary.

To publish later, replace `--dry-run` with `--live`. Preflight still runs first.
Only after typing `PUBLISH` does the command import into the real analytics DB,
generate live commentary and write to Discord. Source/member/payout hashes are
checked again after confirmation. Cancellation leaves production untouched.
The two selected types are processed separately, H2H first. Errors stop the
command; rerun the affected type after reconciliation. Earlier completed work is
not rolled back, and exact reruns skip already-posted historical recaps.

The H2H report excludes tournament facts before Commissioner and RESULTS_DRUNK
receive it. A tournament report comes solely from tournament facts, without any
H2H import dependency. Both personas use the existing structured generation and
fact-reference validation. When both types run, they get separate reports and
separate generation/publication operations. GENERAL_DRUNK never receives them.

RESULTS_DRUNK retains its existing opt-in switches, priority/streak/margin
thresholds, target limits, profanity and model controls. The present tournament
fact types do not satisfy that existing selector, so tournament RESULTS_DRUNK is
silent until tournament-specific qualifying rules are deliberately added. The
isolated handoff is implemented/tested; it does not force an ordinary result to
qualify just to create a joke.

## Historical posts and current boards

Each new completed H2H contest creates a normal H2H result message with its recap
and historical PNG; tournament does the equivalent in its own channel. These
posts are never edited. The historical image is calculated through the selected
week, while the persistent image represents the latest imported standings.
Exact reruns skip historical generation/send and refresh only the current board.

Current leaderboard IDs are scoped by `(guild, channel, season, type)`, with an
updated timestamp. Stored IDs are fetched directly, not discovered by searching
recent history. A confirmed existing bot-owned message is edited with a replacement
attachment set. No ID or Discord's explicit Unknown Message response creates a
new message and saves its ID. Permission failures, missing channels, HTTP 429 and
network failures do **not** masquerade as a deleted message. An ownership failure
stops the operation. Prior historical posts cannot be used as update targets.

## Publication state and recovery

The new SQLite file is separate from analytics and old webhook JSON state:

* `operations`: unique operation key, status (`pending`, `posted`, `uncertain`),
  metadata JSON, message ID and updated timestamp. Announcements, conversational
  replies, result recaps, RESULTS_DRUNK and temporary leaderboard locks each have
  separate keys. Result keys include guild, channel, season, type, contest ID and
  source/payout/economics revision.
* `leaderboards`: guild, channel, season, type, message ID and updated timestamp.

Every create is reserved before the network call (and before paid generation).
An interruption or ambiguous failure remains pending/uncertain and is never
automatically resent. A failed leaderboard fetch/edit releases its lock because
an edit is repeatable; an ambiguous leaderboard create keeps the reservation.
SQLite does not provide an atomic transaction with Discord: a process can stop
after Discord accepted a post but before its ID was saved.

Recovery is deliberate and local, not an automatic retry policy:

1. Stop the listener/result job, back up the state SQLite, and inspect the relevant
   `operations` row and its destination channel in Discord.
2. If the post exists, save its ID with `OrchestrationState.finish(key, id)`.
   For an ambiguous leaderboard create also call
   `save_leaderboard(guild, channel, season, type, id)` and `release(lock_key)`.
3. Only after confirming **no** post was created, use `release(key)` to allow an
   explicit retry. Do not erase the whole state file or unrelated operation keys.
4. Rerun the selected results command. It skips posted historical operations and
   retries the persistent edit. Announcement/general events are not backfilled;
   the listener processes new incoming messages only. Do not release a source
   reservation expecting a historical gateway event to replay automatically.

Errors deliberately omit request bodies, tokens and third-party exception text.
Listener logs report safe exception classes. State retains publication metadata,
not conversational message bodies. This phase adds no scheduler, dashboard,
Discord result command, production deployment or external infrastructure.

## Offline verification

```powershell
python -m pytest -q -p no:cacheprovider
python scripts/smoke_discord_orchestration.py
python -m kcdk discord-bot --config data/discord_config.example.json --check-config
```

The smoke creates temporary fixture CSVs and databases, uses mock Discord and
stub model generators, and simulates two official link announcements, a duplicate,
a conversational reply, and two weeks of each result type. It asserts four distinct
historical posts, two persistent creates followed by two edits of those same IDs,
and no cross-posting. It cannot publish live and leaves production paths untouched.
