"""Project path configuration."""

from pathlib import Path

from .models import ProjectPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PATHS = ProjectPaths.from_root(PROJECT_ROOT)
