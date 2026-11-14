"""Load the gitignored .env and report BLOCKED cleanly.

Lookup order: $HOLDSHORT_ENV, then <repo root>/.env, then ~/plane-management/.env
(so git worktrees of the repo find the shared secrets file without a copy).
Importing this module loads the file into os.environ.
"""

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def _env_file() -> Path:
    if os.environ.get("HOLDSHORT_ENV"):
        return Path(os.environ["HOLDSHORT_ENV"])
    for p in (ROOT / ".env", Path.home() / "plane-management" / ".env"):
        if p.is_file():
            return p
    return ROOT / ".env"


ENV_FILE = _env_file()
load_dotenv(ENV_FILE)

_URI_RE = re.compile(r"mongodb(\+srv)?://[^\s'\"]+")
_SECRET_NAMES = ("MONGODB_URI", "LLM_API_KEY", "ELEVENLABS_API_KEY", "LANGSMITH_API_KEY")


def redact(text: object) -> str:
    """Strip connection strings and known secret values from text before printing."""
    text = _URI_RE.sub("<redacted-uri>", str(text))
    for name in _SECRET_NAMES:
        val = os.environ.get(name, "").strip()
        if len(val) >= 8:
            text = text.replace(val, f"<redacted-{name}>")
    return text


def require(name: str, where: str) -> str:
    """Return env var `name`, or print BLOCKED and exit with code 2 if it is unset."""
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"BLOCKED: {name} is not set. {where}")
        sys.exit(2)
    return value


def mongodb_db_name() -> str:
    """Database name from MONGODB_DB (default "holdshort")."""
    return os.environ.get("MONGODB_DB", "").strip() or "holdshort"


URI_HELP = (
    "Put the Atlas Sandbox connection string (mongodb+srv://...) in "
    f"{ENV_FILE} as MONGODB_URI=..."
)
