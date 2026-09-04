"""Small typed models used by the KCDK package."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    """Important filesystem locations for a local KCDK checkout."""

    root: Path
    raw_data: Path
    mock_data: Path
    processed_data: Path
    output: Path
    members: Path

    @classmethod
    def from_root(cls, root: Path) -> "ProjectPaths":
        return cls(
            root=root,
            raw_data=root / "data" / "raw",
            mock_data=root / "data" / "mock",
            processed_data=root / "data" / "processed",
            output=root / "output",
            members=root / "data" / "members.csv",
        )
