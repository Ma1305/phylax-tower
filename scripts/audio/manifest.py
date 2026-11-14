"""Write scenarios/laguardia/audio/manifest.json (called by degrade.sh).

Lists every clip (one per transmission plus the stepped-on overlap) at the
clean level and every degraded level, with paths relative to audio/, durations
and sample rates read with ffprobe. Fails if any expected file is missing.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import AUDIO, CLEAN, LEVELS, transmissions  # noqa: E402

LEVEL_INFO = {
    "clean": "TTS output, 22.05 kHz mono",
    "mild": "band-pass 300-3000 Hz, 3:1 compression, light pink noise (~26 dB SNR), squelch, 8 kHz",
    "medium": "band-pass 350-2800 Hz, 6:1 compression, pink noise (~18 dB SNR), squelch, 8 kHz",
    "brutal": "band-pass 450-2400 Hz, 12:1 compression + overdrive, heavy pink noise "
              "(~11 dB SNR), loud squelch, 8 kHz",
}


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=sample_rate",
         "-of", "json", str(path)], check=True, capture_output=True, text=True).stdout
    info = json.loads(out)
    return {"file": str(path.relative_to(AUDIO)),
            "duration_s": round(float(info["format"]["duration"]), 3),
            "sample_rate": int(info["streams"][0]["sample_rate"])}


def main() -> int:
    levels = ("clean",) + LEVELS
    overlap_id = os.environ.get("OVERLAP_ID", "overlap_t06_t07")
    entries = [{"id": t["id"], "kind": "transmission", "speaker": t["speaker"],
                "offset_s": t["offset_s"], "text": t["text"]} for t in transmissions()]
    entries.append({"id": overlap_id, "kind": "overlap",
                    "stepped_on": [os.environ.get("OVER_A", "t06"),
                                   os.environ.get("OVER_B", "t07")],
                    "second_starts_after_ms": int(os.environ.get("OVER_DELAY_MS", "1200"))})
    missing = []
    for e in entries:
        e["files"] = {}
        for lvl in levels:
            p = AUDIO / lvl / f"{e['id']}.wav"
            if p.is_file():
                e["files"][lvl] = probe(p)
            else:
                missing.append(str(p.relative_to(AUDIO)))
    if missing:
        print("manifest.py: missing clips: " + ", ".join(missing))
        return 1
    src = CLEAN / "source.json"
    engine = json.loads(src.read_text())["engine"] if src.is_file() else "unknown"
    manifest = {"scenario": "laguardia", "tts_engine": engine,
                "levels": {lvl: LEVEL_INFO[lvl] for lvl in levels},
                "clips": entries}
    (AUDIO / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"manifest.py: {len(entries)} clips x {len(levels)} levels -> {AUDIO / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
