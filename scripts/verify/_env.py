"""Shared helper: load the gitignored .env and report BLOCKED cleanly.

Lookup order: $HOLDSHORT_ENV, then <repo root>/.env, then ~/plane-management/.env
(so git worktrees of the repo find the shared secrets file without a copy).
"""
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


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


def redact(text) -> str:
    """Strip connection strings and known secret values from text before printing."""
    text = _URI_RE.sub("<redacted-uri>", str(text))
    for name in ("MONGODB_URI", "LLM_API_KEY", "ELEVENLABS_API_KEY"):
        val = os.environ.get(name, "").strip()
        if len(val) >= 8:
            text = text.replace(val, f"<redacted-{name}>")
    return text


def require(name: str, where: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"BLOCKED: {name} is not set. {where}")
        sys.exit(2)
    return value


URI_HELP = ("Put the Atlas Sandbox connection string (mongodb+srv://...) in "
            f"{ENV_FILE} as MONGODB_URI=...")
