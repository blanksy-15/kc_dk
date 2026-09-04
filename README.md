# KCDK

KCDK is a local Python project for importing weekly DraftKings contest exports, identifying private KCDK members, and building persistent season, tournament, and player-usage analysis. It can turn the deterministic weekly fact report into optional, structured OpenAI commentary and publish a mobile-friendly weekly recap plus persistent standings through Discord incoming webhooks. It also renders branded leaderboard PNG previews for visual review.

## Two distinct leaderboards

### KCDK standings: internal weekly finish

The official internal KCDK season ranking is **average weekly KCDK finish**, ascending. A member who finishes 1st, 4th, 2nd, and 3rd has an `average_finish` of 2.5. Weekly KCDK finish is ranked by DraftKings fantasy points; tied scores share the same KCDK finish.

The provisional deterministic tiebreakers are isolated in `kcdk.analytics.KCDK_LEADERBOARD_TIEBREAKERS`:

1. Lower average finish
2. More wins
3. More podium finishes
4. Higher average DraftKings fantasy points
5. Display name alphabetically

### Tournament standings: DraftKings tournament performance

The separate tournament-performance leaderboard measures results in the larger DraftKings fields. Its ordering is isolated in `kcdk.analytics.TOURNAMENT_LEADERBOARD_TIEBREAKERS`:

1. Higher total money won
2. Higher average DraftKings fantasy points
3. Higher average tournament percentile
4. Display name alphabetically

These standings are not an alternate calculation of the internal KCDK leaderboard. Both are returned independently.

## Architecture

- `src/kcdk/dk_import.py`: alias-based CSV header normalization, Decimal-safe prize parsing, numeric parsing, member matching, tournament percentile calculation, and tied weekly standings.
- `src/kcdk/lineups.py`: tolerant lineup parsing and side-by-side player ownership/FPTS metadata lookup. Unknown lineup formats produce warnings and do not block result import.
- `src/kcdk/persistence.py`: SQLite schema initialization and the high-level normalize, match, rank, parse, and atomic import workflow.
- `src/kcdk/analytics.py`: official season and tournament leaderboards, independent prior-week rank movement, member/group player usage, and factual selection-pattern helpers.
- `src/kcdk/facts.py`: structured fact models, deterministic fact generators, transparent priority scoring, balanced selection, and weekly JSON-ready reports.
- `src/kcdk/commentary.py`: compact fact-payload construction, OpenAI Responses API integration, structured-output validation, generation metadata, and Discord Markdown rendering/chunking.
- `src/kcdk/commentary_prompt.py`: versioned prompt guardrails, tone profiles, and the strict output schema.
- `src/kcdk/discord.py`: secret-safe, no-retry Discord incoming-webhook transport and centralized platform-limit validation.
- `src/kcdk/publishing.py`: Discord renderers, local message/publication state, idempotency, and weekly orchestration.
- `src/kcdk/leaderboard_graphics.py`: isolated Pillow renderer, centralized visual configuration, font and authored-asset fallbacks, measured text truncation, and both PNG layouts.
- `src/kcdk/leaderboard_preview.py`: network-free mock-season and presentation-data preview composition.
- `src/kcdk/leaderboard_delivery.py`: image mode configuration, fresh PNG preparation/validation, and safe text fallback delivery.
- `src/kcdk/cli.py`: normal command-line workflow available through `python -m kcdk weekly`.
- `src/kcdk/members.py`: editable active/inactive member configuration.
- `src/kcdk/config.py` and `src/kcdk/models.py`: portable project paths.
- `data/mock/`: version-controlled fictional members and contest fixtures.
- `notebooks/season_analysis.ipynb`: end-to-end demonstration using a temporary database.
- `notebooks/fact_engine.ipynb`: candidate facts, selected talking points, priorities/tags, completeness warnings, and the serialized payload for a fictional week.
- `notebooks/commentary.ipynb`: latest mock fact report, a no-cost dry run, structured request JSON, and an explicitly opt-in live preview.

SQLite uses normalized `seasons`, `members`, `season_members`, `contests`, `member_results`, `players`, and `lineup_players` tables. Integer primary keys link records internally. Prize amounts are nullable integer cents on `member_results`, avoiding floating-point money storage. The editable member CSV has a stable `member_key`; keep that key unchanged if a member changes their DraftKings or display name.

The normal local database location is `data/processed/kcdk.sqlite`. Everything under `data/processed/` except `.gitkeep` is ignored, so the production database is never committed. Tests and the season notebook use temporary databases.

## Setup on Windows

```powershell
cd Z:\
git clone https://github.com/blanksy-15/kc_dk.git
cd kc_dk
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
Copy-Item data\members.example.csv data\members.csv
Copy-Item .env.example .env
```

Edit the ignored `.env` file and replace its placeholders with your local API key and two Discord incoming-webhook URLs. Never commit `.env` or paste its values into notebooks, logs, screenshots, or issue reports. `OPENAI_MODEL` defaults to `gpt-5.4-mini` and can be overridden in `.env`. Discord uses `DISCORD_WEEKLY_WEBHOOK_URL` for new weekly recaps and `DISCORD_LEADERBOARD_WEBHOOK_URL` for the two persistent leaderboard messages. A Discord bot token is not used or required.

Run tests:

```powershell
python -m pytest
```

## Interactive Windows weekly runner

For the normal weekly operator workflow, double-click `Run KCDK Weekly.bat` in
the repository root. The launcher uses only this project's
`.venv\Scripts\python.exe`. If that environment is missing, it displays setup
commands and exits; it never installs packages automatically.

On first use, the runner asks for non-secret season defaults and stores them in
ignored `data/processed/local_config.json`. The committed
`data/local_config.example.json` shows the supported fields. API keys and
Discord webhook URLs remain exclusively in the ignored `.env` file and are
never copied to runner config or output.

The guided flow opens a standard CSV file picker, suggests the next week from
the existing database, and displays a read-only preflight containing the file,
season/week, entrant and member-match counts, unmatched members, standings,
money/player/ownership availability, duplicate/import status, publication
status, and live-service readiness. Nothing is imported and no network call is
made until the operator explicitly answers yes to:

```text
Publish this week live? [y/N]
```

Blank input is no. A previously published week also requires a separate,
explicit repost confirmation. A changed CSV for an existing week is blocked;
the interactive runner never performs an implicit replacement.

Use the fully offline interactive preview before a live week:

```powershell
python -m kcdk weekly-runner --dry-run
```

You can bypass the picker or proposed week when needed:

```powershell
python -m kcdk weekly-runner `
  --csv C:\path\to\contest.csv `
  --week-number 5 `
  --dry-run
```

Dry-run imports into a temporary copy of the database, renders the same weekly
recap and both leaderboards with deterministic preview commentary, and never
contacts OpenAI or Discord. The existing scriptable `python -m kcdk weekly`
command remains available for advanced automation.

## Branded leaderboard PNG previews

The Pillow renderer produces two matching, fixed-width graphics without
changing analytical rank order:

- `KCDK SEASON STANDINGS`, ranked by the official average-finish rules.
- `KCDK TOURNAMENT PERFORMANCE`, ranked by money, average DraftKings points,
  average tournament percentile, and display name.

Both use a 1,400 px canvas width, 158 px header, 52 px fixed rows, and 124 px
footer. Height grows with the number of entries instead of shrinking
typography: 10, 15, and 20 rows render at 946, 1,206, and 1,466 px respectively.
The intended normal range is 10–20 members, with 20 as the maximum. Player
names are ellipsized using Pillow glyph measurement; the expanded default
player text areas are 508 px for KCDK standings and 398 px for tournament
performance after padding.

Movement is calculated independently for the two leaderboards against the
immediately previous imported contest in the same season. Positive movement is
shown as `▲ N`, decline as `▼ N`, unchanged as `—`, and a first appearance or
first week as `NEW`. Later contests are never included in a historical
comparison. Currency uses two decimal places: known zero winnings remain
`$0.00`, unknown amounts remain `—`, and an asterisk marks incomplete prize
history.

KCDK standings show `RK`, `MOVE`, `PLAYER`, `AVG FIN`, `W`, `TOP 3`, `AVG PTS`,
and cumulative internal `LAST` finishes. Tournament performance shows `RK`,
`MOVE`, `PLAYER`, `WON`, `CASHES`, `AVG PTS`, `AVG PCTL`, and `BEST CASH`.
Three factual broadcast-style footer panels summarize most wins, most podiums,
and a meaningful current last-place streak for KCDK, plus money leader, most
cashes, and best cash for tournament performance. Callout values are supplied
from analytics; the drawing layer does not invent them.

The authored `assets/branding/kcdk_logo.png` is used by default and contained
within the configured logo region without stretching. Its RGBA channel is
preserved. The matching `assets/branding/leaderboard_header.png` is scaled
without distortion, right-anchored, and cropped to the header behind the
logo/title/subtitle. Scale, X/Y offset, opacity, and anchoring are centralized
in `LeaderboardVisualConfig`. A replacement RGBA file will preserve its alpha
automatically; the current RGB artwork is rendered exactly as supplied.
Another optional authored asset can be placed in `assets/branding/`:

- `leaderboard_background.png`

Aspect ratios are preserved through contain/crop operations. Missing or
unreadable header artwork is not an error: the renderer falls back to its
restrained procedural skyline instead, but never draws that skyline underneath
successfully loaded header artwork. It also supplies a KCDK mark and
deterministic broadcast-style texture when needed. Fonts are configurable
through `LeaderboardVisualConfig`; without a project font it searches sensible
system fonts and reports the selected fallback.

Generate both 15-row review images from the real mock-season analytics plus
preview-only synthetic presentation rows:

```powershell
python -m kcdk leaderboard-preview
```

The ignored outputs are:

- `output/preview/kcdk_standings_preview.png`
- `output/preview/tournament_performance_preview.png`

These preview PNGs are review-only. Production Discord publishing renders fresh
PNGs from the current database into `output/leaderboards/`, independently of previews.

Open `notebooks/season_analysis.ipynb` in VS Code or Jupyter and run all cells. The notebook calls package code rather than duplicating business logic.

## Weekly import workflow and idempotency

`kcdk.persistence.import_week` performs the complete workflow:

1. Load and normalize a DraftKings CSV.
2. Match active members from the editable member CSV.
3. Calculate tied weekly KCDK standings.
4. Parse available lineup-player selections.
5. Persist contest metadata, nullable prize cents, member results, and player usage atomically.
6. Return an `ImportSummary` with season, week, source, tournament size, matched/stored row counts, and warnings.

The source SHA-256 fingerprint and `(season, week_label)` are unique. Importing the same file again is a no-op. A changed source for an existing week raises an error unless `replace=True` is explicitly passed; an explicit replacement removes that contest's dependent result/lineup rows within the same transaction before inserting the replacement.

Lineup parsing is deliberately separate from standings. Player-level information may arrive as a lineup string plus an unusual side-by-side ownership table. If the lineup format is unavailable or unknown, member results are still saved and the summary clearly reports that no lineup-player records were imported.

Prize headers currently recognize isolated aliases for `Prize`, `Winnings`, `Prize Amount`, and `Amount Won`. Currency strings are converted with `Decimal` to integer cents (`$125.50` becomes `12550`). A source value of `$0.00` is known zero; a blank, invalid value, or absent prize column is stored as null/unknown. Missing prize data never blocks standings import and always produces an import warning.

## Statistics currently tracked

The internal KCDK leaderboard contains weeks played, average finish, wins, podiums, last-place finishes, average/high/low DraftKings fantasy points, average tournament percentile, and best/worst tournament rank.

The separate tournament leaderboard contains weeks played, weeks with known prize data, a completeness flag, total and average known winnings, cash count and cash rate, largest win, average/high fantasy points, average percentile, and best/average tournament ranks. Totals based on incomplete prize information remain visibly marked incomplete; members with no known prize values have unknown totals rather than fabricated zeroes.

Per-member player usage contains selection count, percentage of that member's played weeks, positions used, average/total player fantasy points, average field ownership, average KCDK finish when rostered, wins/podiums/last places with the player, known prize weeks, money won, cash count/rate, and the member's average DraftKings score when that player was rostered. These are historical correlations and do not claim a player caused the result.

Group usage contains total KCDK selections, unique member count, weeks appeared, average field ownership, and average player fantasy points. Helpers identify consecutive use, unanimous weekly selections, unique weekly selections, each member's most-used players, and highest/lowest average-ownership selections. These outputs are factual and do not assign qualitative labels.

Tournament percentile is 0–100, where rank 1 is 100 and the final rank is 0. A one-entry contest is assigned 100.

## Trash-talk fact engine

The fact engine calculates structured, verifiable talking points for a selected contest. Python owns all rankings, comparisons, streaks, margins, money calculations, and completeness decisions. A future language-model layer will receive the selected facts and may phrase them as commentary; it will not be responsible for calculating or inventing the underlying claims.

Each `Fact` keeps machine-readable values separate from its concise factual summary. It includes a fact type, category, subject and related member IDs/names, optional player and contest context, evidence, completeness, priority, and neutral tags. Facts and `WeeklyFactReport` objects serialize directly to JSON-ready dictionaries. `build_weekly_fact_report` accepts either a stable season identifier or internal season ID plus an optional contest ID; historical reports are restricted to information available through that contest.

Initial generators cover:

- Weekly winners/last places, score margins, ties, tournament results, personal bests/worsts, and score deviations from a member's prior average.
- Win, podium, last-place, direction, cash, known-zero, player-use, and head-to-head streaks.
- KCDK and tournament leaders, cross-leaderboard contrasts, standings-rank contrasts, and differences between wins/scores and internal rank.
- Known season winnings, largest/first/threshold cashes, no-cash facts, earnings gaps/ties, and weekly KCDK-versus-cash outcomes.
- Repeated, unanimous, unique, and most-used players plus factual KCDK, cash, and winnings history when those players were rostered.
- Weekly and member ownership extremes, unique low-owned choices, unanimous high-owned choices, and threshold-backed repeated ownership patterns.
- Pairwise shared-week records, current head-to-head streaks, ties, average score/finish differences, known money differences, and closest score gaps.
- New or tied season records for fantasy score, tournament percentile, victory/closest margins, and cashes.

Priority weights live in `FACT_PRIORITY_BASES` and bonuses in `PRIORITY_BONUSES`; generation thresholds live in `FactEngineConfig`. The score is a visible sum of the fact-type base, bounded streak/magnitude bonuses, and an optional record bonus. There is no randomness. Selection first seeks category coverage, then fills by priority while respecting a configurable per-member cap where possible, then relaxes that cap only when necessary. Duplicate identities are removed and final ordering is deterministic. The default shortlist contains at most 12 facts.

Report warnings make incomplete prize, player-lineup, and ownership data explicit. Missing prize data never becomes a known $0 result. Player/money facts state historical co-occurrence only and do not claim that a selection caused an outcome.

Current limitations: thresholds are provisional; lineup/ownership and prize aliases still need validation against a real DraftKings export; player identity currently depends on normalized names; and the first fact set intentionally favors transparent rules over statistical anomaly modeling. OpenAI commentary requires a separately funded API account and is intentionally opt-in.

## OpenAI weekly commentary

The commentary layer uses the official OpenAI Python SDK and the Responses API with a strict JSON Schema response. Python remains the source of truth: the model receives only the selected compact facts, fact IDs, completeness warnings, the chosen tone, and any explicitly supplied safe member context. It never receives a raw DraftKings CSV, database, candidate-fact set, API key, or unrelated local data.

Three versioned tone profiles are available: `mild`, `normal` (default), and `ruthless`. Even the strongest tone is restricted to good-natured fantasy-sports performance. Prompt guardrails forbid invented statistics, unsupported history, sensitive personal inferences, predictions, and causal claims about player selections. Facts marked `partial` must remain qualified or be omitted. Each generated roast must cite one or more IDs from the supplied fact set, and the parser rejects unknown or missing IDs.

Build and inspect the exact secret-free request without contacting OpenAI:

```powershell
python scripts\smoke_commentary.py
```

The dry-run JSON contains the complete instructions, compact input payload, model, output schema, prompt version, fact IDs, and character/UTF-8 byte sizes. To make exactly one deliberate request using the key in `.env`:

```powershell
python scripts\smoke_commentary.py --live --tone normal
```

Live results include the response ID/model and input, output, and total token usage when returned by the API. Errors expose only a safe exception class and optional HTTP status, never the exception message or key. The SDK client uses a 30-second timeout and at most two SDK-managed transient retries by default; authentication and configuration failures are not manually retried. `OPENAI_TIMEOUT_SECONDS` and `OPENAI_MAX_RETRIES` are optional local overrides.

Application code can pass optional member context explicitly:

```python
from kcdk.commentary import MemberCommentaryContext, generate_weekly_commentary

result = generate_weekly_commentary(
    report,
    tone="normal",
    member_context={
        "Casey North": MemberCommentaryContext(
            display_name="Casey North",
            nickname="North Star",
            commentary_notes="Space puns are welcome.",
        )
    },
)
```

Only context for members appearing in selected facts is sent. Commentary notes are whitespace-cleaned and length-limited; callers are responsible for supplying only non-sensitive, fantasy-relevant notes. `render_discord_markdown` formats validated output, hides internal fact IDs by default, and neutralizes mass mentions. Fact IDs remain in the structured response and can be included only through its explicit debug option. `chunk_discord_markdown` splits text below Discord's 2,000-character message limit. Neither function posts anything.

## Commissioner and occasional Drunk Bot

The Commissioner remains the informative weekly voice, using its existing
`mild`/`normal`/`ruthless` prompt, structured output, and fact-ID checks. Drunk Bot
is a separate, scarce interruption: sharper, profane fantasy-football jokes in
one short message, only when Python finds strong existing facts. It never decides
what happened or whether a week qualifies.

The layers are separate: `facts.py` → `drunk_bot.py` eligibility →
`drunk_bot_prompt.py` (prompt version `1`) → `drunk_bot_commentary.py` structured
generation → `drunk_bot_publishing.py` state/delivery. The Commissioner prompt is
unchanged. Both use the configured OpenAI Responses provider/model; optional
`DRUNK_BOT_OPENAI_MODEL` overrides only the second persona. Structured generation
uses a strict JSON schema, following the
[OpenAI structured output documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

`DrunkBotConfig` defaults:

| Setting | Default |
| --- | --- |
| `enabled` | `true` |
| `minimum_fact_priority` | `90` |
| `minimum_streak_length` | `3` |
| `maximum_interjections_per_week` | `2` (configurable 1–2) |
| `maximum_members_targeted` | `2` (configurable 1–3) |
| `minimum_number_of_eligible_facts` | `1` (configurable 1–3) |
| `allow_profanity` | `true` |
| `default_tone` | `ruthless` (`normal` also supported) |

Environment/ignored `.env` overrides use `DRUNK_BOT_` plus the uppercase field
name, for example `DRUNK_BOT_MINIMUM_FACT_PRIORITY=95`. Set
`DRUNK_BOT_ENABLED=false` to disable it. Existing local runner configuration loads
with `drunk_bot_enabled: true` by default; add `"drunk_bot_enabled": false` to
`data/processed/local_config.json` to disable it there. Either switch can disable
the persona. No extra weekly prompt is added. Preflight computes eligibility from
a disposable database copy that includes the selected CSV, and shows its score
and trigger before live confirmation.

Eligibility examines the existing candidate facts, independently of the
Commissioner's shortlist. Every selected fact must meet the priority threshold
and a negative-event rule: sustained last-place/decline/known-zero streaks,
head-to-head losses, a newly set season-low score, or several known weeks with
zero winnings. Additional rules support large margins, close non-tied finishes,
large score declines, and repeated poor player outcomes when their existing fact
priority clears the configured gate. Their additional thresholds are
`large_margin_points=40`, `close_margin_points=0.5`, and
`poor_average_finish=5`. The default priority gate intentionally excludes many
milder fact types; positive records and routine winners cannot qualify merely by
having high priority.

The eligibility score is the highest qualifying fact's existing priority, with
stable ordering and no randomness. Selection prefers one strongest fact per
target, up to three facts overall. A second distinct fact for the same person is
used only to satisfy an explicitly configured minimum evidence count. Head-to-head
facts target the losing member. The four-week mock season selects Taylor Quinn's
four consecutive last-place finishes at score **98**. A new season-low score also
qualifies at **96**, but does not crowd out that stronger fact for the same person.

The prompt permits clever profanity and rhetorical exaggeration while forbidding
invented scores, ranks, selections, winnings, streaks, entry fees/net losses,
causality, personal-life attacks, protected-group slurs, threats, and sexual
humiliation. Missing prize data never becomes zero. Structured parsing enforces
existing fact IDs, the supported target for each ID, non-repeated evidence,
member/interjection limits, and a 600-character cap per interjection. IDs remain
in local state and diagnostics, never in Discord text. These checks validate
structure and references; the prompt supplies the factual prose constraints.

Configure the optional separate identity with `DISCORD_DRUNK_WEBHOOK_URL=` in
the ignored `.env`. It can be a distinct webhook in the same weekly channel.
If absent, the Commissioner and leaderboards work normally and Drunk Bot reports
`missing_webhook`; no unnecessary persona generation is purchased. Disabled or
ineligible weeks make zero Drunk Bot API calls. Eligible live publishing makes
one persona request with SDK retries disabled and records its usage separately
from Commissioner usage. Persona API/post failures are nonfatal to the rest of
the workflow.

Network posting order is **Commissioner recap → optional single Drunk Bot message
→ both persistent leaderboard updates**. The two voices are never combined.
Drunk Bot uses simple prose and disabled mentions, with identity supplied by its
webhook rather than a large embed or internal label.

State version 2 adds `drunk_bot_weeks` alongside the existing Commissioner
`published_weeks`. Version 1 files load without losing any IDs or recap records.
States distinguish disabled, not eligible, missing destination, generation
failure, ready, posting, rejected/uncertain post, and posted. Generated output,
fact references, request/version/model, usage, and confirmed message ID are saved
locally; credentials are never saved. Intent is saved before a network post.
Ordinary reruns never regenerate/repost the persona, and `--repost` applies only
to the Commissioner. Existing historical recap records do not cause a new persona
message to be retroactively posted on an ordinary rerun.

For explicit recovery, rerun the usual `python -m kcdk weekly ...` command with
`--recover-drunk-bot` and **without** `--repost`. The Commissioner must already
have posted. Missing-destination/generation failures can then be retried, and
definitely rejected posts reuse the saved generation without another paid call.
A timeout, 5xx, missing message ID, or interrupted `posting` state may represent a
successful Discord post. These require reconciliation first: inspect Discord,
then set that record's `message_id` and `status: "posted"` if found; only if it is
confirmed absent, set `status: "post_failed"` before explicit recovery. Automatic
recovery never risks a duplicate after an ambiguous response.

Weekly `--dry-run` reports enabled/eligible status, score, selected fact IDs,
trigger count, and whether generation/posting would occur given the destination
and publication state. It never makes the Drunk Bot request or alters state.
`--generate-commentary` retains its existing Commissioner-only meaning.

Free mock eligibility and exact persona payload preview:

```powershell
python scripts\smoke_drunk_bot.py
```

Deliberately make **one paid generation**, saving its structured result to ignored
`output/preview/drunk_bot_generation.json`, without contacting Discord:

```powershell
python scripts\smoke_drunk_bot.py --live
```

After reviewing the saved prose, deliberately post it once through the Drunk Bot
webhook, with **no OpenAI generation**:

```powershell
python scripts\smoke_drunk_bot.py --post
```

`--generation PATH` chooses the output/input file. Posting uses separate ignored
`data/processed/drunk_bot_smoke_state.json`, labels the message `[SMOKE TEST]`, and
does not post a Commissioner recap or touch leaderboards. Repeated smoke posts
are suppressed; `--recover` permits a definitely rejected smoke post to retry.
The `--live` and `--post` options are mutually exclusive. Tests mock all live calls.

## Discord weekly publishing

The normal publishing flow keeps importing, analytics, fact generation, OpenAI commentary, Discord rendering, webhook transport, and local publication state separate. The weekly-results webhook receives one new recap containing the week label, Commissioner Report, compact internal results, tournament rank/earnings highlights, and completeness notes only when relevant. Normal public output never includes internal fact IDs.

The leaderboard webhook owns exactly two persistent messages:

1. `KCDK Season Standings`, ranked by average weekly KCDK finish.
2. `KCDK Tournament Performance`, ranked by known winnings and then average DraftKings points.

Image mode is the default. Every publishing run calculates current standings and
independent rank movement from the database and uses the existing branded renderer:

- `output/leaderboards/kcdk_standings.png`
- `output/leaderboards/tournament_performance.png`

Each message has one PNG plus its title and `Updated through Week N`. The full
text/embed renderers remain available internally for fallback and debugging. Even
when rerunning an older recap, persistent standings reflect the latest imported
contest; the recap still describes the requested week. Generated PNGs and local
Discord state remain ignored by Git.

Centralized settings in `LeaderboardConfig` read these optional environment or
ignored `.env` values (process environment takes precedence):

```dotenv
KCDK_LEADERBOARD_MODE=image
KCDK_LEADERBOARD_MAX_FILE_BYTES=8388608
```

Set the mode to `text` to skip image rendering and use the existing embeds.
The Windows runner uses this configuration automatically without another prompt.
The 8 MiB default is a conservative local upload budget, not a universal Discord
limit; Discord can reject a file below it. The application verifies the file exists,
is nonempty, is a valid PNG, matches the renderer's expected dimensions, and fits
the configured budget. Current default dimensions are 1400 pixels wide and
`426 + 52 × row count` high for 1–20 rows. Oversized or invalid images immediately
use text fallback without an image upload attempt. Render errors are reported by
safe exception type. Validation snapshots the bytes to avoid uploading a later
overwrite of the generated file.

Branding uses `assets/branding/kcdk_logo.png` when present. Authored
`assets/branding/leaderboard_header.png` is optional; missing/unreadable artwork
uses the renderer's existing procedural fallback. Supplying a visual configuration
with `header_artwork_opacity=0` disables header artwork while retaining the logo.

The webhook client sends `payload_json` and `files[0]` as multipart form data for
both create (`POST`, `wait=true`) and edit (`PATCH`). An image edit supplies
`attachments: [{"id": 0, "filename": "…", "description": "…"}]`, listing only
the new upload. These two application-owned messages intentionally retain no older
attachments. Text fallback supplies `attachments: []`; mode changes also clear old
content/embeds. This prevents attachment accumulation according to
[Discord's webhook edit semantics](https://docs.discord.com/developers/resources/webhook#edit-webhook-message).

If an image edit fails, the existing message ID stays intact and the publisher
attempts one text/embed edit. A stale ID stops without creating a replacement.
If both edits fail, publishing stops with a clear error and preserves state and
the last content Discord accepted. For initial creation, an explicit payload
rejection (HTTP 400/413/415/422) allows one text fallback create. An ambiguous
timeout, 5xx response, or missing returned message ID is never followed by another
create: inspect Discord and repair local state before rerunning if necessary.
The weekly recap's posting and idempotency behavior is unchanged.

Message IDs and published-week records are stored atomically in ignored local state at `data/processed/discord_state.json`. Webhook URLs are never stored there. The first live run creates each leaderboard message; later runs edit those IDs. A stale/deleted message ID causes a clear failure and no automatic replacement, preventing silent duplicate leaderboard spam. Remove or deliberately repair that state entry only after confirming the Discord message is truly gone.

Weekly publication is idempotent. Re-running an already published season/contest refreshes the two leaderboards but skips the recap and avoids another OpenAI request. Use `--repost` only when a duplicate recap is intentional. A week is marked published only after Discord confirms the recap post. Successful initial leaderboard creation is saved immediately, so a later recap failure will not duplicate leaderboard messages on the next run.

Preview a week without contacting Discord or OpenAI:

```powershell
python -m kcdk weekly `
  --csv data\mock\season_week_4.csv `
  --members data\mock\members.csv `
  --database data\processed\kcdk.sqlite `
  --season mock-2026 `
  --season-name "Mock 2026" `
  --week "Week 4" `
  --week-number 4 `
  --year 2026 `
  --tone normal `
  --dry-run
```

The dry run performs the local idempotent CSV import, renders all three Discord payloads, reports whether the week is already published, and lists the create/edit/skip operations that would occur. It also renders fresh PNGs in image mode and reports each path, dimensions, byte size, selected image/text mode, fallback reason, and existing message ID to edit. Text fallback payloads are included only in local diagnostics. It never contacts Discord or changes Discord state, and uses a deterministic fact preview rather than incurring an OpenAI charge. Add `--generate-commentary` only when a paid commentary request during dry run is deliberate. Remove `--dry-run` for live publishing. Use `--repost` to intentionally publish an already-recorded week again.

Discord content, embed, field, and combined embed limits are centralized and validated before transport. All outbound payloads disable mentions. Transport uses a finite timeout and no automatic retries because retrying a successful-but-ambiguous webhook request can create duplicates. Errors include only safe operation/status information and never webhook URLs.

An additional mock-only transport smoke test is available:

```powershell
python scripts\smoke_discord.py
python scripts\smoke_discord.py --live  # only after confirming both destinations are test-safe
python scripts\smoke_discord.py --png-leaderboards  # PNG dry run, no network
python scripts\smoke_discord.py --live --png-leaderboards
```

The default command is network-free. The explicit live form creates and deletes one labeled weekly test message and creates or edits two labeled persistent test leaderboard messages using separate ignored smoke-test state. It never calls OpenAI.

The `--live --png-leaderboards` form renders both current mock standings with the
production renderer and updates only the two labeled smoke leaderboards. It never
posts a weekly recap, never calls OpenAI, and requires only the leaderboard webhook.
It reuses `data/processed/discord_smoke_state.json`, replaces previous attachments,
and reports message IDs, chosen modes, image details, and attachment counts from
Discord's response when available. Missing IDs are created once and saved; stale
IDs follow the same stop-without-replacement policy as production. Text fallback
or an unexpected returned attachment count makes the PNG smoke test exit nonzero.
Live smoke tests are never run by pytest; test coverage uses mocked transport.

## Mock multi-week season

`data/mock/season_week_1.csv` through `season_week_4.csv` contain four fictional weeks and five fictional active KCDK members. The fixtures include four different winners, repeated podiums, a four-week last-place streak, an average-finish tie, close and large score margins, recurring player choices, one unanimous weekly player, unique weekly players, consecutive selections, head-to-head streaks, and latest-week season records. Prize data includes known zeroes, a three-week known-zero streak followed by missing data, small cashes, a larger win, equal total winnings resolved by average fantasy points, and different KCDK-versus-earnings leaders. Optional player/FPTS/ownership columns model the side-by-side metadata pattern without coupling it to entrant rows.

## First real export validation

Before production use, validate the first real export's delimiter, encoding, header spelling, actual prize/winnings column and formatting, duplicate-column behavior, contest ID/date availability, multi-entry member behavior, tie representation, lineup string grammar, and the exact relationship between entrant rows and side-by-side player ownership/FPTS rows. The persistence schema should not need to change if only header aliases or lineup parsing need another format adapter.

## Planned phases

1. Validate and adapt parsing and fact thresholds against the first real DraftKings export.
2. Validate OpenAI commentary tone and prompt behavior with league feedback.
3. Validate the full import/publish workflow against the first real weekly export.
4. Verify the first real weekly image leaderboards and production message destinations.
