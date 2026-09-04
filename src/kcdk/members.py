"""Load and match editable KCDK member configuration."""

from pathlib import Path

import pandas as pd

REQUIRED_MEMBER_COLUMNS = {"draftkings_name", "display_name", "active"}
OPTIONAL_MEMBER_COLUMNS = {"member_key": "", "nickname": "", "notes": ""}


def load_members(path: Path, active_only: bool = True) -> pd.DataFrame:
    """Load member configuration from CSV and optionally keep active members."""
    members = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = REQUIRED_MEMBER_COLUMNS - set(members.columns)
    if missing:
        raise ValueError(f"Member configuration is missing columns: {sorted(missing)}")

    for column, default in OPTIONAL_MEMBER_COLUMNS.items():
        if column not in members:
            members[column] = default

    members["active"] = members["active"].str.strip().str.lower().isin(
        {"true", "1", "yes", "y"}
    )
    if active_only:
        members = members.loc[members["active"]].copy()
    return members.reset_index(drop=True)
