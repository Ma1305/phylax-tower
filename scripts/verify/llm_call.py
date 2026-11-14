"""Check 6: one LLM call through holdshort.llm (the provider chosen by LLM_PROVIDER)."""
import sys
from pathlib import Path

import _env  # noqa: F401  (loads .env)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from holdshort import llm  # noqa: E402

st = llm.status()
if st["provider"] == "none":
    print("BLOCKED 6: no LLM provider configured (set LLM_PROVIDER / VERTEX_PROJECT)")
    sys.exit(2)
text = llm.text("parse", "Follow the instruction exactly.", "Reply with exactly: HOLD SHORT OK", 30)
if not text:
    print(f"FAIL 6: no reply from {st['provider']} {st['tasks']['parse']['model']}")
    sys.exit(1)
print(f"PASS 6: {st['provider']} {st['tasks']['parse']['model']} replied: {text!r}")
