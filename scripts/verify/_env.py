"""Shared helper: load ~/plane-management/.env (or repo-root .env) and report BLOCKED cleanly."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(os.environ.get("HOLDSHORT_ENV", ROOT / ".env"))


def require(name: str, where: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"BLOCKED: {name} is not set. {where}")
        sys.exit(2)
    return value


URI_HELP = ("Put the Atlas Sandbox connection string (mongodb+srv://...) in "
            f"{ROOT}/.env as MONGODB_URI=...")
