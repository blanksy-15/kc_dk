# KCDK

KCDK is a local Python project for importing weekly DraftKings contest exports, identifying private KCDK members, and building persistent season and player-usage analysis. Leaderboard graphics, generated commentary, Discord posting, and OpenAI API calls are intentionally out of scope for the current phase.

## Official leaderboard rule

The official season ranking is **average weekly KCDK finish**, ascending. A member who finishes 1st, 4th, 2nd, and 3rd has an `average_finish` of 2.5. Weekly KCDK finish is ranked by DraftKings fantasy points; tied scores share the same KCDK finish.

The provisional deterministic tiebreakers are isolated in `kcdk.analytics.LEADERBOARD_TIEBREAKERS`:

1. Lower average finish
2. More wins
3. More podium finishes
4. Higher average DraftKings fantasy points
5. Display name alphabetically

## Architecture

- `src/kcdk/dk_import.py`: alias-based CSV header normalization, numeric parsing, member matching, tournament percentile calculation, and tied weekly standings.
- `src/kcdk/lineups.py`: tolerant lineup parsing and side-by-side player ownership/FPTS metadata lookup. Unknown lineup formats produce warnings and do not block result import.
- `src/kcdk/persistence.py`: SQLite schema initialization and the high-level normalize, match, rank, parse, and atomic import workflow.
- `src/kcdk/analytics.py`: official season leaderboard, member/group player usage, and factual selection-pattern helpers.
- `src/kcdk/members.py`: editable active/inactive member configuration.
- `src/kcdk/config.py` and `src/kcdk/models.py`: portable project paths.
- `data/mock/`: version-controlled fictional members and contest fixtures.
- `notebooks/season_analysis.ipynb`: end-to-end demonstration using a temporary database.

SQLite uses normalized `seasons`, `members`, `season_members`, `contests`, `member_results`, `players`, and `lineup_players` tables. Integer primary keys link records internally. The editable member CSV has a stable `member_key`; keep that key unchanged if a member changes their DraftKings or display name.

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
5. Persist contest metadata, member results, and player usage atomically.
6. Return an `ImportSummary` with season, week, source, tournament size, matched/stored row counts, and warnings.

The source SHA-256 fingerprint and `(season, week_label)` are unique. Importing the same file again is a no-op. A changed source for an existing week raises an error unless `replace=True` is explicitly passed; an explicit replacement removes that contest's dependent result/lineup rows within the same transaction before inserting the replacement.

Lineup parsing is deliberately separate from standings. Player-level information may arrive as a lineup string plus an unusual side-by-side ownership table. If the lineup format is unavailable or unknown, member results are still saved and the summary clearly reports that no lineup-player records were imported.

## Statistics currently tracked

The season leaderboard contains weeks played, average finish, wins, podiums, last-place finishes, average/high/low DraftKings fantasy points, average tournament percentile, and best/worst tournament rank.

Per-member player usage contains selection count, percentage of that member's played weeks, positions used, average/total player fantasy points, average field ownership, average KCDK finish when rostered, and wins/podiums/last places with the player.

Group usage contains total KCDK selections, unique member count, weeks appeared, average field ownership, and average player fantasy points. Helpers identify consecutive use, unanimous weekly selections, unique weekly selections, each member's most-used players, and highest/lowest average-ownership selections. These outputs are factual and do not assign qualitative labels.

Tournament percentile is 0–100, where rank 1 is 100 and the final rank is 0. A one-entry contest is assigned 100.

## Mock multi-week season

`data/mock/season_week_1.csv` through `season_week_4.csv` contain four fictional weeks and five fictional active KCDK members. The fixtures include four different winners, repeated podiums, a four-week last-place streak, an average-finish tie resolved by the configured tiebreakers, recurring player choices, one unanimous weekly player, unique weekly players, and consecutive selections. Optional player/FPTS/ownership columns model the side-by-side metadata pattern without coupling it to entrant rows.

## First real export validation

Before production use, validate the first real export's delimiter, encoding, header spelling, duplicate-column behavior, contest ID/date availability, multi-entry member behavior, tie representation, lineup string grammar, and the exact relationship between entrant rows and side-by-side player ownership/FPTS rows. The persistence schema should not need to change if only lineup parsing needs another format adapter.

## Planned phases

1. Validate and adapt parsing against the first real DraftKings export.
2. Build leaderboard graphics from the persisted facts.
3. Add OpenAI-generated commentary constrained to Python-calculated facts.
4. Add Discord integration.
