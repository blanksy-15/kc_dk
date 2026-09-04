# KCDK

KCDK is a local Python project for importing weekly DraftKings contest exports, identifying private KCDK members, and building persistent season, tournament, and player-usage analysis. Leaderboard graphics, generated commentary, Discord posting, and OpenAI API calls are intentionally out of scope for the current phase.

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
- `src/kcdk/analytics.py`: official season leaderboard, member/group player usage, and factual selection-pattern helpers.
- `src/kcdk/facts.py`: structured fact models, deterministic fact generators, transparent priority scoring, balanced selection, and weekly JSON-ready reports.
- `src/kcdk/members.py`: editable active/inactive member configuration.
- `src/kcdk/config.py` and `src/kcdk/models.py`: portable project paths.
- `data/mock/`: version-controlled fictional members and contest fixtures.
- `notebooks/season_analysis.ipynb`: end-to-end demonstration using a temporary database.
- `notebooks/fact_engine.ipynb`: candidate facts, selected talking points, priorities/tags, completeness warnings, and the serialized payload for a fictional week.

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
Copy-Item data\members.example.csv data\members.csv
```

Run tests:

```powershell
python -m pytest
```

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

Current limitations: thresholds are provisional; lineup/ownership and prize aliases still need validation against a real DraftKings export; player identity currently depends on normalized names; and the first fact set intentionally favors transparent rules over statistical anomaly modeling. The next phase is OpenAI commentary generation constrained to the selected JSON facts. No OpenAI or Discord integration exists yet.

## Mock multi-week season

`data/mock/season_week_1.csv` through `season_week_4.csv` contain four fictional weeks and five fictional active KCDK members. The fixtures include four different winners, repeated podiums, a four-week last-place streak, an average-finish tie, close and large score margins, recurring player choices, one unanimous weekly player, unique weekly players, consecutive selections, head-to-head streaks, and latest-week season records. Prize data includes known zeroes, a three-week known-zero streak followed by missing data, small cashes, a larger win, equal total winnings resolved by average fantasy points, and different KCDK-versus-earnings leaders. Optional player/FPTS/ownership columns model the side-by-side metadata pattern without coupling it to entrant rows.

## First real export validation

Before production use, validate the first real export's delimiter, encoding, header spelling, actual prize/winnings column and formatting, duplicate-column behavior, contest ID/date availability, multi-entry member behavior, tie representation, lineup string grammar, and the exact relationship between entrant rows and side-by-side player ownership/FPTS rows. The persistence schema should not need to change if only header aliases or lineup parsing need another format adapter.

## Planned phases

1. Validate and adapt parsing and fact thresholds against the first real DraftKings export.
2. Add OpenAI-generated commentary constrained to selected Python-calculated facts.
3. Build leaderboard graphics from the persisted facts.
4. Add Discord integration.
