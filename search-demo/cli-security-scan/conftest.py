import sys
from pathlib import Path

# cli-security-scan/ isn't an importable package name (hyphen), so make its
# modules importable by path for `uv run pytest cli-security-scan/` from the
# repo root as well as `cd cli-security-scan && pytest`.
sys.path.insert(0, str(Path(__file__).resolve().parent))
