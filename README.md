# KCDK

KCDK is a local Python project for importing weekly DraftKings contest exports, identifying private KCDK members, and building season-long analysis over time. Discord posting and OpenAI calls are intentionally not implemented yet.

## Current architecture

- `src/kcdk/dk_import.py`: alias-based CSV header normalization, numeric parsing, member matching, percentile calculation, and weekly standings.
- `src/kcdk/members.py`: loads editable active/inactive member configuration.
- `src/kcdk/config.py` and `src/kcdk/models.py`: portable project paths.
- `data/members.example.csv`: fictional template for the editable member mapping. Copy it to the local, ignored `data/members.csv` and replace the sample values. CSV was chosen because it is easy to edit in a spreadsheet.
- `data/mock/`: fictional fixture data for development. Production exports will go in `data/raw/`; raw files are never rewritten.
- `data/processed/` and `output/`: reserved for future derived tables and graphics.

The importer requires rank, entry ID, entry name, points, and lineup. It accepts common header variants and preserves recognized optional fields when present, including player, roster position, drafted percentage, and player fantasy points.

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

Open `notebooks/exploration.ipynb` in VS Code or Jupyter and run the cells. The notebook calls package code rather than duplicating business logic.

## Data workflow

Place each unchanged DraftKings CSV export in `data/raw/`. That directory is intentionally excluded from Git because real exports may contain 100,000+ entries. The mock contest at `data/mock/contest.csv` is fictional, version controlled, and exists only to exercise the importer. Copy `data/members.example.csv` to the ignored local `data/members.csv`, then edit it to add or deactivate KCDK members; matching is case-insensitive after trimming whitespace. `.env` is also local-only and must never contain committed secrets.

Percentiles use a 0-100 scale where rank 1 is 100 and the last rank is 0. For a one-entry contest, the only entry is treated as the 100th percentile. Weekly KCDK rank is based on points, with ties sharing a rank; standings are displayed by points, overall rank, then entry name.

## Planned phases

1. Persistent season database
2. Weekly and season statistical analysis
3. Leaderboard rendering
4. OpenAI-generated commentary based only on Python-calculated statistics
5. Discord integration

## First real export checklist

Validate the actual delimiter, encoding, header spelling, duplicate column behavior, whether rank/points contain ties or special placeholders, and the units/format of any ownership percentages. Confirm how multi-row player data relates to entrant rows before using optional athlete columns for analysis.
