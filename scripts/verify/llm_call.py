"""Check 6: one LLM call with LLM_API_KEY (assumes an Anthropic API key; model overridable via LLM_MODEL)."""
import os
import sys

import anthropic

from _env import ROOT, require

key = require("LLM_API_KEY", f"Put an Anthropic API key in {ROOT}/.env as LLM_API_KEY=...")
model = os.environ.get("LLM_MODEL", "claude-opus-5-5")
client = anthropic.Anthropic(api_key=key)
try:
    msg = client.messages.create(
        model=model,
        max_tokens=256,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": "Reply with exactly: HOLD SHORT OK"}],
    )
except anthropic.AuthenticationError as e:
    print(f"FAIL 6: authentication error (is LLM_API_KEY an Anthropic key?): {e}")
    sys.exit(1)
except anthropic.APIStatusError as e:
    print(f"FAIL 6: HTTP {e.status_code}: {e.message}")
    sys.exit(1)
except anthropic.APIConnectionError as e:
    print(f"FAIL 6: connection error: {e}")
    sys.exit(1)
if msg.stop_reason == "refusal":
    print(f"FAIL 6: model refused: {msg.stop_details}")
    sys.exit(1)
text = "".join(b.text for b in msg.content if b.type == "text").strip()
print(f"PASS 6: {model} replied: {text!r}")
