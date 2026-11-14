"""uv run python -m holdshort.listener <audio-or-dir> [--text] [--db NAME] [--dry-run] [--check]

Audio mode (VOICE_ENABLED=true + ELEVENLABS_API_KEY): each clip is cleaned (voice
isolation), transcribed (ElevenLabs STT), parsed (LLM) and inserted.
Text mode (--text, or voice disabled): the lines of scenarios/laguardia/script.json
are used instead, with simulated confidence (lines marked "unclear" get 0.55). If
<audio-or-dir> names clips (t01.wav ...), only those script lines are used.

--check compares every produced transmission with the script line's `expect` and
requires the deliberately unclear line to be flagged; exits 1 on any mismatch.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from holdshort.contracts import Transmission
from holdshort.listener import voice
from holdshort.listener.agent import (
    SCRIPT,
    Listener,
    load_script,
    script_time,
    text_confidence,
)
from holdshort.listener.parse import get_labeller

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}


def _clips(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.suffix.lower() in AUDIO_EXT)
    return [path] if path.exists() else []


def _fmt(tx: Transmission) -> str:
    where = f" rwy {tx.runway}" if tx.runway else ""
    where += f" at {tx.at}" if tx.at else ""
    flag = f"  FLAG: {tx.flag}" if tx.flag else ""
    return (
        f"{tx.t:%H:%M:%S} {tx.from_:>7} -> {tx.to:<7} {tx.kind:<9} {tx.instruction:<10}"
        f"{where:<12} readback={tx.readback:<14} conf={tx.confidence:.2f} "
        f"verified={tx.verified}{flag}"
    )


def check(results: list[tuple[dict, Transmission]]) -> list[str]:
    """Differences between produced transmissions and each script line's expect."""
    problems = []
    for event, tx in results:
        exp = event.get("expect") or {}
        if event.get("unclear"):
            if not tx.flag or tx.verified:
                problems.append(f"{event['id']}: unclear line not flagged/unverified")
            continue
        got = {"kind": tx.kind, "instruction": tx.instruction, "runway": tx.runway, "at": tx.at}
        diff = {k: (v, got.get(k)) for k, v in exp.items() if got.get(k) != v}
        if diff:
            problems.append(f"{event['id']}: expected vs got {diff}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hold Short listener agent")
    ap.add_argument("path", nargs="?", default=None, help="audio clip or directory of clips")
    ap.add_argument("--text", action="store_true", help="force text mode (script lines)")
    ap.add_argument("--script", type=Path, default=SCRIPT, help="scenario script.json")
    ap.add_argument("--db", default=None, help="database name (default: MONGODB_DB)")
    ap.add_argument("--dry-run", action="store_true", help="parse only, do not insert")
    ap.add_argument("--check", action="store_true", help="compare with script `expect`")
    ap.add_argument("--labeller", choices=["auto", "llm", "rules"], default="auto",
                    help="auto = Anthropic LLM if LLM_API_KEY is set, else offline rules")
    ap.add_argument("--all", action="store_true", help="also process clips not in the script")
    args = ap.parse_args(argv)

    start, lines = load_script(args.script)
    by_id = {e["id"]: e for e in lines if "id" in e}
    clips = _clips(Path(args.path)) if args.path else []
    if args.path and not clips:
        print(f"listener: no audio found at {args.path}")

    text_mode = args.text or not voice.voice_enabled() or not clips
    db = client = None
    if not args.dry_run:
        from holdshort.db import get_client
        from holdshort.env import mongodb_db_name

        client = get_client()
        db = client[args.db or mongodb_db_name()]
    listener = Listener(db=db, llm=get_labeller(args.labeller))
    results: list[tuple[dict, Transmission]] = []

    try:
        if text_mode:
            ids = [c.stem for c in clips if c.stem in by_id]
            events = [by_id[i] for i in ids] if ids else lines
            why = "--text" if args.text else "voice disabled" if clips else "no audio"
            print(f"listener: text mode ({why}), {len(events)} script lines")
            for e in events:
                tx = listener.handle_text(
                    e["text"], t=script_time(start, e), confidence=text_confidence(e),
                    speaker=e.get("speaker"),
                )
                results.append((e, tx))
                print(f"{e.get('id', '?'):>4} {_fmt(tx)}")
        else:
            print(f"listener: audio mode, {len(clips)} clips")
            for clip in clips:
                e = by_id.get(clip.stem)
                if e is None and not args.all:
                    print(f"  skip {clip.name} (not a script line; use --all)")
                    continue
                t = script_time(start, e) if e else datetime.now(UTC)
                tx = listener.handle_audio(clip, t=t)
                if e:
                    results.append((e, tx))
                print(f"{clip.stem:>4} {_fmt(tx)}")
                print(f"       \"{tx.transcript}\"")
    finally:
        if client is not None:
            client.close()

    where = "not inserted (--dry-run)" if args.dry_run else f"inserted into {db.name}.transmissions"
    print(f"listener: {len(listener.history)} transmissions {where}")
    if args.check:
        problems = check(results)
        for p in problems:
            print(f"CHECK FAIL {p}")
        print("CHECK PASS" if not problems else f"CHECK FAIL ({len(problems)})")
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
