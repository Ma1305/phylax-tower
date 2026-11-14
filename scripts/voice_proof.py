"""Prove the real ElevenLabs speech path once, with four API calls, on a throwaway database.

    uv run python scripts/voice_proof.py [--db holdshort_v2_t36_voice]

1. One smoke-feed line is synthesised by real text-to-speech (cached in tools/fixtures/audio/,
   so a rerun spends nothing), stored in GridFS as a queued inbound clip, and the listener
   (scoped to its own access list) runs real voice isolation + speech-to-text on it. The
   transmission must parse the same as the truth text (from, to, instruction, runway, at).
2. One safety stop intent goes through the gate (autonomous -> executed) and the speaker
   synthesises it with real text-to-speech (audio/mpeg, not the placeholder wav).
Needs VOICE_ENABLED=true and ELEVENLABS_API_KEY; prints BLOCKED otherwise. Drops the db.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "scripts"))

import gridfs  # noqa: E402

from holdshort import contracts as c  # noqa: E402
from holdshort import gate  # noqa: E402
from holdshort.db import get_client  # noqa: E402
from holdshort.env import redact  # noqa: E402
from holdshort.listener import voice  # noqa: E402
from holdshort.listener.service import ClipProcessor  # noqa: E402
from holdshort.speaker.core import speak_decision  # noqa: E402

LINE = {"t": 25.0, "dur": 3.0, "stream": "LGA_GND", "speaker": "GROUND",
        "text": "Truck one, cross runway four at Delta."}
FIELDS = ("from_", "to", "kind", "instruction", "runway", "at")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="holdshort_v2_t36_voice")
    args = ap.parse_args()
    assert args.db not in {"holdshort", "holdshort_v2"}
    if not voice.voice_enabled():
        print("BLOCKED: VOICE_ENABLED is not true or ELEVENLABS_API_KEY is missing")
        return 2
    import setup_atlas
    import smoke_feed

    client = get_client()
    db = client[args.db]
    ok = True
    try:
        setup_atlas.setup(db, log=lambda *_: None, reset_live=True)
        # 1. real TTS -> GridFS clip -> listener (real isolation + STT) -> transmission
        voices = smoke_feed.Voices(smoke_feed.load_scenario("incident").get("voices", {}), False)
        data, mime, dur = voices.clip(LINE)
        print(f"tts: {len(data)} bytes {mime}, {dur:.1f} s")
        if mime != "audio/mpeg":
            print("FAIL  text-to-speech fell back to a tone")
            return 1
        now = datetime.now(UTC)
        audio_id = gridfs.GridFSBucket(db, bucket_name="audio").upload_from_stream(
            "voice-proof", data, metadata={"mime": mime})
        clip = c.to_mongo(c.AudioClip(
            tower_id="LGA", stream_id=LINE["stream"], direction="in", t_start=now,
            t_end=now + timedelta(seconds=dur), duration_s=dur, audio_id=audio_id, mime=mime,
            truth_text=LINE["text"], speaker=LINE["speaker"], status="queued", created_at=now))
        clip["_id"] = db.audio_clips.insert_one(clip).inserted_id
        proc = ClipProcessor(db)  # stt="auto": real ElevenLabs because voice is enabled
        tx_id = proc.process(clip)
        tx = db.transmissions.find_one({"_id": tx_id}) if tx_id else None
        heard, conf = (tx or {}).get("transcript"), (tx or {}).get("confidence", 0)
        # the truth-text fallback stores the exact truth text at confidence 0.95
        real = bool(tx) and (heard != LINE["text"].strip() or conf != 0.95)
        print(f"stt: {heard!r} (confidence {conf:.2f}, {'real' if real else 'TRUTH FALLBACK'})")
        truth = ClipProcessor(db, stt=None).build(clip)
        got = c.Transmission.model_validate(tx) if tx else None
        diff = {f: (getattr(got, f, None), getattr(truth, f)) for f in FIELDS
                if got is None or getattr(got, f) != getattr(truth, f)}
        good = real and got is not None and not diff
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  real STT gave the right transmission: "
              f"{got and {f: getattr(got, f) for f in FIELDS}}" + (f" diff {diff}" if diff else ""))
        denied = db.security_events.count_documents({"agent": "listener"})
        ok &= denied == 0
        print(f"{'PASS' if denied == 0 else 'FAIL'}  listener read audio.files/audio.chunks "
              f"through its own access list ({denied} denials)")

        # 2. gate (autonomous stop) -> speaker -> real TTS
        intent = c.to_mongo(c.Intent(
            t=datetime.now(UTC), tower_id="LGA", agent="safety", action="stop",
            severity="critical", target=["TRUCK1"], object_ids=["TRUCK1"], lane_id="RWY_04_22",
            runway="04", stream_id=None, reason="runway_incursion", conflicts_with="AC8646",
            proposal_text="Truck one, hold position, hold short of runway four, traffic landing.",
            detail="voice proof"))
        intent["_id"] = db.intents_safety.insert_one(intent).inserted_id
        d = gate.decide(db, "safety", intent)
        print(f"gate: {d['outcome']} [{d['rule']}] -> {d['controller_id']} on {d['stream_id']}")
        out = speak_decision(db, d)
        oclip = out and db.audio_clips.find_one({"_id": out["clip_id"]})
        good = bool(d["outcome"] == "executed" and out and oclip and oclip["mime"] == "audio/mpeg"
                    and oclip["duration_s"] > 1)
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  speaker synthesised the executed stop with real "
              f"TTS: {oclip and (oclip['mime'], oclip['duration_s'])}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  {type(e).__name__}: {redact(e)}")
        ok = False
    finally:
        client.drop_database(args.db)
        client.close()
    print("voice proof:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
