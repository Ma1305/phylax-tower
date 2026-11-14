"""Play a real recording of a frequency instead of generated speech.

A recording of a radio frequency is one long file. This module finds the individual
transmissions in it (the stretches that are not silence), cuts each one out as its own clip
and tells the scenario when, relative to the start of the file, each was spoken. The scenario
then posts every clip to the tower at the matching moment, with no text attached: the tower
has to transcribe the real audio itself.

The recording never goes into the repository: pass its path on the command line. Clips are
cut into the simulator's cache directory (git-ignored).

WAV files are read directly. Anything else (mp3, m4a, ...) is converted with ffmpeg first.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / ".cache" / "real"
RATE = 8000


class RealAudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class Burst:
    """One transmission found in the recording."""

    offset_s: float      # seconds from the start of the recording
    duration_s: float
    path: Path
    mime: str = "audio/wav"


def _to_wav(path: Path) -> Path:
    if path.suffix.lower() == ".wav":
        return path
    if not shutil.which("ffmpeg"):
        raise RealAudioError(f"{path.name} is not a WAV file and ffmpeg is not installed to "
                             "convert it")
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{path.resolve()}:{path.stat().st_size}".encode()).hexdigest()[:16]
    out = CACHE / f"{key}.wav"
    if not out.is_file():
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i",
                            str(path), "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16",
                            str(out)], capture_output=True, text=True, check=False)
        if r.returncode != 0 or not out.is_file():
            raise RealAudioError(f"ffmpeg could not read {path.name}: {r.stderr.strip()[:200]}")
    return out


def load_pcm(path: Path) -> tuple[array, int]:
    """Mono 16-bit samples and the sample rate."""
    with wave.open(str(_to_wav(path)), "rb") as w:
        if w.getsampwidth() != 2:
            raise RealAudioError("only 16-bit WAV is supported")
        rate, channels = w.getframerate(), w.getnchannels()
        samples = array("h")
        samples.frombytes(w.readframes(w.getnframes()))
    if channels > 1:
        samples = array("h", (sum(samples[i:i + channels]) // channels
                              for i in range(0, len(samples) - channels + 1, channels)))
    return samples, rate


def find_bursts(samples: array, rate: int, *, frame_ms: int = 20, min_silence_s: float = 0.6,
                min_burst_s: float = 0.35, threshold: float | None = None
                ) -> list[tuple[float, float]]:
    """(start, end) in seconds of every stretch louder than the background."""
    n = max(1, rate * frame_ms // 1000)
    energy = [sum(abs(x) for x in samples[i:i + n]) / n for i in range(0, len(samples), n)]
    if not energy:
        return []
    if threshold is None:
        floor = sorted(energy)[len(energy) // 5]        # the quiet fifth is the background
        threshold = max(floor * 3.0, 250.0)
    frame_s = n / rate
    bursts: list[list[float]] = []
    for i, e in enumerate(energy):
        if e < threshold:
            continue
        t0, t1 = i * frame_s, (i + 1) * frame_s
        if bursts and t0 - bursts[-1][1] < min_silence_s:
            bursts[-1][1] = t1
        else:
            bursts.append([t0, t1])
    return [(a, b) for a, b in bursts if b - a >= min_burst_s]


def cut_bursts(path: Path, start_s: float = 0.0, end_s: float | None = None,
               pad_s: float = 0.12, **kw: float) -> list[Burst]:
    """Every transmission between ``start_s`` and ``end_s`` of the recording, as clips."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise RealAudioError(f"recording not found: {path}")
    samples, rate = load_pcm(path)
    total = len(samples) / rate
    end_s = total if end_s is None else min(end_s, total)
    start_s = max(0.0, start_s)
    window = samples[int(start_s * rate):int(end_s * rate)]
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{path.resolve()}:{path.stat().st_size}:{start_s}:{end_s}"
                       .encode()).hexdigest()[:12]
    out: list[Burst] = []
    for i, (a, b) in enumerate(find_bursts(window, rate, **kw)):
        a, b = max(0.0, a - pad_s), min(len(window) / rate, b + pad_s)
        clip = CACHE / f"{key}-{i:03d}.wav"
        with wave.open(str(clip), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(window[int(a * rate):int(b * rate)].tobytes())
        out.append(Burst(round(start_s + a, 2), round(b - a, 2), clip))
    return out


def start_seconds_from_name(path: Path) -> int | None:
    """Seconds after midnight UTC that an archive file starts at, from a name such as
    ``KLGA-Twr-Mar-23-2026-0330Z.mp3``. None if the name does not say."""
    m = re.search(r"(?<!\d)([01]\d|2[0-3])([0-5]\d)Z", Path(path).name)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 if m else None


def parse_clock(text: str) -> float:
    """'03:30:00' or '5:07' or '307' -> seconds."""
    parts = [float(p) for p in str(text).split(":")]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"not a time: {text!r}")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds
