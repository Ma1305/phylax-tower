# Hold Short

AI agents that listen to simulated tower radio, store every call in MongoDB Atlas,
and stop a runway conflict before it happens. The AI can stop traffic, never clear it.

## Setup

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22.12+ (for `web/`),
and an Atlas cluster you can reach.

1. Copy `.env.example` to `.env` (gitignored, never commit it) and fill in `MONGODB_URI`.
   `MONGODB_DB` defaults to `holdshort`. On the shared VM the file lives at
   `~/plane-management/.env`; `holdshort/env.py` finds it from any worktree
   (lookup order: `$HOLDSHORT_ENV`, `<repo>/.env`, `~/plane-management/.env`).
2. Install dependencies (the `holdshort` package is installed in editable mode):

   ```bash
   uv sync
   ```

3. Create the collections, `$jsonSchema` validators, indexes and seed data
   (idempotent, safe to re-run; it never resets live permission switches or the
   active rule set):

   ```bash
   uv run python scripts/setup_atlas.py
   ```

4. Check everything:

   ```bash
   uv run ruff check .
   uv run pytest          # validator tests use the throwaway "<MONGODB_DB>_test" database
   ```

`holdshort/contracts.py` is the single source of truth for documents; the validators
in `scripts/setup_atlas.py` read their enums from it. `SETUP_STATUS.md` covers the
VM setup checks in `scripts/verify/`.

## Listener agent

```bash
uv run python -m holdshort.listener scenarios/laguardia/audio/medium/          # insert into MONGODB_DB
uv run python -m holdshort.listener scenarios/laguardia/audio/medium/ --check  # + compare with script `expect`
uv run python -m holdshort.listener --text --dry-run                           # script lines, no insert
```

- Audio mode (VOICE_ENABLED=true + ELEVENLABS_API_KEY): `clean()` (ElevenLabs voice isolation,
  passthrough on failure) -> `transcribe()` (ElevenLabs STT, confidence = mean word probability)
  -> `parse()` -> readback check -> `insert()` (insert-only).
- Text mode (`--text`, voice disabled, or no clips on disk): lines from `scenarios/laguardia/script.json`,
  simulated confidence 0.95 (0.55 for lines marked `"unclear": true`).
- `parse()` labels with the Anthropic LLM (`LLM_API_KEY`, `LLM_MODEL`, default `claude-opus-5-5`,
  structured JSON output). Without a key it falls back to an offline phraseology labeller
  (`holdshort/listener/rules.py`); force one with `--labeller llm|rules`.
  Values are always normalised in code: "Truck one and company" -> TRUCK1, "the jet" -> AC8646, "4" -> "04", "Delta" -> "D".
- Confidence < 0.8 or an unparseable call -> kind `other`, `verified: false`, transcript kept,
  `flag: "unclear - say again"`. A "stop" with no callsign -> kind `stop`, `to: "UNKNOWN"`, flagged.
- `readback`: clearance -> `missing` (awaiting readback), readback -> `matched`/`mismatch` vs the last
  clearance to that callsign, everything else -> `not_applicable`.
