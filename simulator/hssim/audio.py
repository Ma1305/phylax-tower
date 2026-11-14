"""Radio audio: turn (speaker, text) into a cached clip that sounds like aviation radio.

The speech comes from ElevenLabs, from macOS ``say``, or from a built-in keyed tone
that needs no tools at all. When ffmpeg is available the speech is then pushed through
a band-pass, compressor, noise and squelch chain so it sounds like a VHF radio. Every
clip is cached on disk, so a line is only ever generated once.

    box = Voicebox()
    clip = box.synth("TOWER", "Air Canada eight six four six, hold short runway four")
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import uuid
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from hssim import phrases

__all__ = ["PROVIDERS", "RADIO_LEVELS", "Clip", "Voicebox"]

log = logging.getLogger(__name__)

PROVIDERS = ("elevenlabs", "say", "tone")
RADIO_LEVELS = ("clean", "mild", "medium", "brutal")
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "audio"
MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg"}
SUFFIX = {"elevenlabs": ".mp3", "say": ".wav", "tone": ".wav"}

# level -> high-pass Hz, low-pass Hz, compressor threshold, ratio, makeup, noise, squelch, drive
RADIO_PARAMS = {
    "mild": (300, 3000, 0.125, 3, 2, 0.06, 0.15, 1.0),
    "medium": (350, 2800, 0.0625, 6, 4, 0.2, 0.4, 1.6),
    "brutal": (450, 2400, 0.03125, 12, 8, 0.55, 0.9, 3.0),
}
RADIO_RATE = 8000
FFMPEG_QUIET = ("-hide_banner", "-loglevel", "error", "-y")
TOOL_TIMEOUT_S = 60

ELEVEN_API = "https://api.elevenlabs.io"
ELEVEN_TIMEOUT_S = 60.0
ELEVEN_MODEL = "eleven_multilingual_v2"
ELEVEN_BITRATE = 128_000  # of the mp3_44100_128 format we ask for
ELEVEN_STATION_VOICES = {
    "TOWER": "21m00Tcm4TlvDq8ikWAM",  # Rachel: calm, even
    "GROUND": "onwK4e9ZLuTAKqWW03F9",  # Daniel: steady, flat
    "OPS": "pNInz6obpgDQGcFmaJgB",  # Adam: deeper, rougher
}
# Premade voices for pilots and drivers when the account's own list cannot be read.
ELEVEN_SPARE_VOICES = (
    "nPczCjzI2devNBz1zQrb",  # Brian
    "JBFqnCBsd6RMkjVDRZzb",  # George
    "IKne3meq5aSn9XLyUdCD",  # Charlie
    "N2lVS1w4EtoT3dr4eOWO",  # Callum
    "TX3LPaxmHKxFdv7VOQHJ",  # Liam
    "iP95p4xoKVk53GoZ742B",  # Chris
    "cjVigY5qzO86Huf0OWal",  # Eric
    "pqHfZKP75CvOlQylNhV4",  # Bill
    "bIHbv24MWmeRgasZH58o",  # Will
    "CwhRBWXzGAHq8TQ4Fs17",  # Roger
    "EXAVITQu4vr4xnSDxMaL",  # Sarah
    "XrExE9yKIg1WjnnlVkGX",  # Matilda
)

SAY_RATE = 185  # words per minute
SAY_WAV_RATE = 22050
# First installed voice in each list wins; a station never shares its voice with another.
SAY_STATION_VOICES = {
    "TOWER": ("Samantha", "Karen", "Moira", "Tessa"),
    "GROUND": ("Daniel", "Rishi", "Fred", "Ralph"),
    "OPS": ("Karen", "Moira", "Tessa", "Kathy"),
}
SAY_NOVELTY = frozenset({
    "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Deranged",
    "Good News", "Hysterical", "Jester", "Organ", "Pipe Organ", "Superstar", "Trinoids",
    "Whisper", "Wobble", "Zarvox",
})  # fmt: skip
_SAY_VOICE_LINE = re.compile(r"^(.+?)\s+([A-Za-z]{2,3}[_-]\S+)\s+#")

TONE_RATE = 8000
TONE_LEVEL = 0.35  # of full scale
TONE_RAMP_S = 0.03
# All inside the narrowest radio band-pass (450-2400 Hz) so the effect never mutes a tone.
TONE_STATION_HZ = {"TOWER": 523, "GROUND": 587, "OPS": 659}
TONE_OTHER_HZ = tuple(round(740 * 2 ** (i / 12)) for i in range(12))  # 740 .. 1397 Hz

_TOOL_RUNS: dict[str, bool] = {}


@dataclass(frozen=True)
class Clip:
    path: Path
    mime: str  # "audio/wav" or "audio/mpeg"
    duration_s: float
    provider: str  # "elevenlabs" | "say" | "tone" (what actually produced the speech)
    text: str


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _say_available() -> bool:
    return sys.platform == "darwin" and shutil.which("say") is not None


def _resolve_provider(provider: str, env: Mapping[str, str]) -> str:
    wanted = (provider or "auto").strip().lower()
    if wanted in PROVIDERS:
        return wanted
    if wanted != "auto":
        raise ValueError(f"unknown TTS provider {provider!r}; use auto, {', '.join(PROVIDERS)}")
    forced = (env.get("SIM_TTS") or "").strip().lower()
    if forced in PROVIDERS:
        return forced
    if (env.get("ELEVENLABS_API_KEY") or "").strip() and _truthy(env.get("VOICE_ENABLED") or ""):
        return "elevenlabs"
    return "say" if _say_available() else "tone"


def _level(radio: str) -> str:
    level = (radio or "").strip().lower()
    if level not in RADIO_LEVELS:
        raise ValueError(f"unknown radio level {radio!r}; use {', '.join(RADIO_LEVELS)}")
    return level


def _key(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _pick(name: str, count: int) -> int:
    """A stable index for ``name`` (the built-in ``hash`` changes from run to run)."""
    return int(hashlib.sha1(name.encode("utf-8")).hexdigest(), 16) % count


def _working_tool(name: str) -> str | None:
    """Path of ``name`` when it is on PATH and really starts; a broken install counts as absent."""
    path = shutil.which(name)
    if path is None:
        return None
    runs = _TOOL_RUNS.get(path)
    if runs is None:
        try:
            done = subprocess.run([path, "-version"], stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=15)
            runs = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            runs = False
        _TOOL_RUNS[path] = runs
        if not runs:
            log.warning("audio: %s is on PATH but does not run; carrying on without it", name)
    return path if runs else None


def _need(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise RuntimeError(f"{tool} not found")
    return path


def _run(cmd: list[str], stdin: bytes | None = None) -> bytes:
    """Run a tool and return its stdout; raise with the last line of stderr when it fails."""
    feed: dict[str, Any] = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
    done = subprocess.run(cmd, capture_output=True, timeout=TOOL_TIMEOUT_S, **feed)
    if done.returncode != 0:
        last = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        raise RuntimeError(f"{Path(cmd[0]).name} exited {done.returncode}: {' '.join(last)}")
    return done.stdout


def _tone_hz(who: str) -> int:
    return TONE_STATION_HZ.get(who) or TONE_OTHER_HZ[_pick(who, len(TONE_OTHER_HZ))]


def _write_tone(dest: Path, text: str, hz: float) -> None:
    """A keyed tone as long as the line takes to say: 8 kHz mono 16-bit with soft edges."""
    count = max(1, round(phrases.estimate_duration_s(text) * TONE_RATE))
    ramp = round(TONE_RAMP_S * TONE_RATE)
    step = 2 * math.pi * hz / TONE_RATE
    peak = TONE_LEVEL * 32767
    samples = []
    for i in range(count):
        edge = min(i, count - 1 - i)
        gain = 0.5 - 0.5 * math.cos(math.pi * edge / ramp) if edge < ramp else 1.0
        samples.append(round(peak * gain * math.sin(step * i)))
    with wave.open(str(dest), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(TONE_RATE)
        out.writeframes(struct.pack(f"<{count}h", *samples))


def _radio_cmd(ffmpeg: str, src: Path, duration_s: float, level: str, dest: Path) -> list[str]:
    """The radio effect: band-pass, compressor, pink noise, squelch at key-up and release."""
    hp, lp, threshold, ratio, makeup, noise, squelch, drive = RADIO_PARAMS[level]
    # Voice padded 0.15 s each side; noise runs the full length; the squelch bursts are
    # louder noise on the first and last 0.12 s.
    total = f"{duration_s + 0.3:.3f}"
    tail = f"{duration_s + 0.18:.3f}"
    graph = (
        "[0:a]aformat=sample_rates=22050:channel_layouts=mono,adelay=150,apad=pad_dur=0.15,"
        f"highpass=f={hp}:poles=2,highpass=f={hp}:poles=2,"
        f"lowpass=f={lp}:poles=2,lowpass=f={lp}:poles=2,volume={drive},"
        f"acompressor=threshold={threshold}:ratio={ratio}:attack=5:release=80:makeup={makeup},"
        "alimiter=limit=0.9[v];"
        f"[1:a]atrim=0:{total},highpass=f={hp},lowpass=f={lp}[n];"
        f"[2:a]atrim=0:{total},highpass=f={hp},lowpass=f={lp},"
        f"volume='if(lt(t,0.12)+gt(t,{tail}),1,0)':eval=frame[s];"
        "[v][n][s]amix=inputs=3:duration=first:normalize=0,alimiter=limit=0.95,"
        f"aresample={RADIO_RATE},aformat=sample_fmts=s16:channel_layouts=mono[out]"
    )
    return [
        ffmpeg, *FFMPEG_QUIET, "-i", str(src),
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude={noise}:sample_rate=22050:seed=7",
        "-f", "lavfi", "-i", f"anoisesrc=color=white:amplitude={squelch}:sample_rate=22050:seed=11",
        "-filter_complex", graph, "-map", "[out]", "-f", "wav", str(dest),
    ]  # fmt: skip


def _overlap_cmd(ffmpeg: str, a: Path, b: Path, delay_ms: int, dest: Path) -> list[str]:
    mono = f"aformat=sample_rates={RADIO_RATE}:channel_layouts=mono"
    graph = (
        f"[0:a]{mono}[a];[1:a]{mono},adelay={delay_ms},volume=0.9[b];"
        "[a][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[out]"
    )
    return [
        ffmpeg, *FFMPEG_QUIET, "-i", str(a), "-i", str(b), "-filter_complex", graph,
        "-map", "[out]", "-ac", "1", "-ar", str(RADIO_RATE), "-sample_fmt", "s16",
        "-f", "wav", str(dest),
    ]  # fmt: skip


class Voicebox:
    """Makes the audio clip for each radio call. One instance can be shared between threads."""

    def __init__(self, cache_dir: Path | None = None, provider: str = "auto",
                 radio: str = "mild", env: Mapping[str, str] | None = None,
                 http: httpx.Client | None = None):
        self._env = os.environ if env is None else env
        self._http = http
        self.cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.provider = _resolve_provider(provider, self._env)
        self.radio = _level(radio)
        self._ffmpeg = _working_tool("ffmpeg")
        self._ffprobe = _working_tool("ffprobe")
        if self._ffmpeg is None and self.radio != "clean":
            log.info("audio: no working ffmpeg, so clips stay clean and overlaps are not mixed")
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._clips: dict[str, Clip] = {}
        self._gave_up: set[str] = set()  # lines and effects that failed once in this run
        self._casting = threading.Lock()
        self._eleven_pool: list[str] | None = None
        self._say_cast: tuple[dict[str, str], list[str]] | None = None

    def synth(self, speaker: str, text: str, *, radio: str | None = None) -> Clip:
        """The clip for ``speaker`` saying ``text``; blocks while it is made, then it is cached.

        A provider that fails costs one warning and the line comes back as a tone instead.
        That line then stays a tone for as long as this Voicebox lives, so asking for it
        again gives the same clip; the next run tries the provider again.
        """
        level = self.radio if radio is None else _level(radio)
        who = speaker.strip().upper()
        line = _key("line", who, text)
        with self._lock(line):  # the same line asked for twice at once is still made only once
            if self.provider != "tone" and text.strip() and line not in self._gave_up:
                try:
                    return self._clip(self.provider, who, text, level)
                except Exception as exc:
                    self._gave_up.add(line)
                    log.warning("audio: %s failed for %s (%s); using a tone instead",
                                self.provider, who, self._safe(exc))
            return self._clip("tone", who, text, level)

    def overlap(self, a: Clip, b: Clip, delay_s: float = 1.2) -> Clip:
        """One clip in which ``b`` keys up ``delay_s`` after ``a`` and both talk at once."""
        delay_s = max(0.0, delay_s)
        text = f"{a.text} // {b.text}"
        unmixed = replace(a, duration_s=max(a.duration_s, delay_s + b.duration_s), text=text)
        ffmpeg = self._ffmpeg
        if ffmpeg is None:
            return unmixed
        delay_ms = round(delay_s * 1000)
        try:
            sounds = [hashlib.sha1(c.path.read_bytes()).hexdigest() for c in (a, b)]
            return self._cached(
                _key("overlap", *sounds, str(delay_ms), text), a.provider, text, ".wav",
                lambda dest: _run(_overlap_cmd(ffmpeg, a.path, b.path, delay_ms, dest)),
            )
        except Exception as exc:
            log.warning("audio: could not mix the overlap (%s); using the first clip alone",
                        self._safe(exc))
            return unmixed

    # -- one clip -----------------------------------------------------------------

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _clip(self, provider: str, who: str, text: str, level: str) -> Clip:
        voice = self._voice(provider, who)
        clean = self._cached(
            _key(provider, voice, "clean", text), provider, text, SUFFIX[provider],
            lambda dest: self._speak(provider, voice, text, dest),
        )
        ffmpeg = self._ffmpeg
        key = _key(provider, voice, level, text)
        if level == "clean" or ffmpeg is None or key in self._gave_up:
            return clean
        try:
            return self._cached(
                key, provider, text, ".wav",
                lambda dest: _run(_radio_cmd(ffmpeg, clean.path, clean.duration_s, level, dest)),
            )
        except Exception as exc:
            self._gave_up.add(key)
            log.warning("audio: radio effect failed (%s); using the clean speech", self._safe(exc))
            return clean

    def _cached(self, key: str, provider: str, text: str, suffix: str,
                write: Callable[[Path], Any]) -> Clip:
        """The clip stored under ``key``; ``write`` makes its file first when it is missing."""
        with self._lock(key):  # two speakers can share a file, e.g. tones of the same pitch
            clip = self._clips.get(key)
            if clip is not None and clip.path.is_file():
                return clip
            path = self.cache_dir / f"{key}{suffix}"
            if not (path.is_file() and path.stat().st_size):
                tmp = self.cache_dir / f".tmp-{uuid.uuid4().hex}{suffix}"
                try:
                    write(tmp)
                    os.replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
            clip = Clip(path, MIME[suffix], self._duration(path), provider, text)
            self._clips[key] = clip
            return clip

    def _duration(self, path: Path) -> float:
        if path.suffix == ".wav":
            with wave.open(str(path), "rb") as src:
                return src.getnframes() / src.getframerate()
        if self._ffprobe is not None:
            try:
                return float(_run([self._ffprobe, "-v", "error", "-show_entries",
                                   "format=duration", "-of", "csv=p=0", str(path)]))
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                pass
        return path.stat().st_size * 8 / ELEVEN_BITRATE

    def _voice(self, provider: str, who: str) -> str:
        if provider == "elevenlabs":
            return self._eleven_voice(who)
        if provider == "say":
            return self._say_voice(who)
        return str(_tone_hz(who))

    def _speak(self, provider: str, voice: str, text: str, dest: Path) -> None:
        if provider == "elevenlabs":
            self._eleven(voice, text, dest)
        elif provider == "say":
            self._say(voice, text, dest)
        else:
            _write_tone(dest, text, float(voice))

    def _getenv(self, name: str) -> str:
        return (self._env.get(name) or "").strip()

    def _safe(self, error: object) -> str:
        """A short one-line description of ``error`` with the API key taken out."""
        text = f"{type(error).__name__}: {error}" if isinstance(error, Exception) else str(error)
        text = " ".join(text.split())
        key = self._getenv("ELEVENLABS_API_KEY")
        if key:
            text = text.replace(key, "<key>")
        return text[:200]

    # -- ElevenLabs ---------------------------------------------------------------

    def _eleven_key(self) -> str:
        key = self._getenv("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")
        return key

    def _eleven_voice(self, who: str) -> str:
        key = self._eleven_key()
        override = self._getenv("ELEVENLABS_VOICE_" + re.sub(r"[^A-Z0-9]+", "_", who))
        if override:
            return override
        if who in ELEVEN_STATION_VOICES:
            return ELEVEN_STATION_VOICES[who]
        with self._casting:
            if self._eleven_pool is None:
                self._eleven_pool = self._eleven_account_pool(key)
            pool = self._eleven_pool
        return pool[_pick(who, len(pool))]

    def _eleven_account_pool(self, key: str) -> list[str]:
        """The account's voices minus the stations'. Read once, so a speaker keeps its voice."""
        taken = set(ELEVEN_STATION_VOICES.values())
        taken |= {self._getenv(f"ELEVENLABS_VOICE_{station}") for station in ELEVEN_STATION_VOICES}
        try:
            reply = (self._http or httpx).get(
                f"{ELEVEN_API}/v1/voices", headers={"xi-api-key": key}, timeout=ELEVEN_TIMEOUT_S)
            if reply.status_code != 200:
                raise RuntimeError(f"ElevenLabs HTTP {reply.status_code}")
            mine = {str(v["voice_id"]) for v in reply.json()["voices"] if v.get("voice_id")}
        except Exception as exc:  # e.g. a key that may speak but not list voices
            log.info("audio: could not list ElevenLabs voices (%s); using built-in ones",
                     self._safe(exc))
            mine = set()
        spare = [v for v in ELEVEN_SPARE_VOICES if v not in taken] or list(ELEVEN_SPARE_VOICES)
        return sorted(mine - taken) or spare

    def _eleven(self, voice: str, text: str, dest: Path) -> None:
        reply = (self._http or httpx).post(
            f"{ELEVEN_API}/v1/text-to-speech/{voice}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self._eleven_key(), "accept": "audio/mpeg"},
            json={"text": text,
                  "model_id": self._getenv("ELEVENLABS_MODEL") or ELEVEN_MODEL,
                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
            timeout=ELEVEN_TIMEOUT_S,
        )
        if reply.status_code != 200:
            raise RuntimeError(f"ElevenLabs HTTP {reply.status_code}: {self._safe(reply.text)}")
        if not reply.content:
            raise RuntimeError("ElevenLabs sent no audio")
        dest.write_bytes(reply.content)

    # -- macOS say ----------------------------------------------------------------

    def _say_voice(self, who: str) -> str:
        with self._casting:
            if self._say_cast is None:
                self._say_cast = self._say_installed_cast()
            stations, others = self._say_cast
        if who in stations:
            return stations[who]
        return others[_pick(who, len(others))] if others else ""

    def _say_installed_cast(self) -> tuple[dict[str, str], list[str]]:
        """(voice of each station, voices for everyone else) from the installed English voices."""
        listing = _run([_need("say"), "-v", "?"]).decode("utf-8", "replace")
        found = (_SAY_VOICE_LINE.match(line) for line in listing.splitlines())
        english = sorted({m.group(1) for m in found
                          if m and m.group(2).lower().startswith("en")} - SAY_NOVELTY)
        stations: dict[str, str] = {}
        for station, wanted in SAY_STATION_VOICES.items():
            free = [v for v in english if v not in stations.values()]
            stations[station] = next((v for v in wanted if v in free), free[0] if free else "")
        return stations, [v for v in english if v not in stations.values()] or english

    def _say(self, voice: str, text: str, dest: Path) -> None:
        aiff = dest.with_suffix(".aiff")
        try:
            # The text goes in on stdin so a line starting with "-" is never read as an option.
            _run([_need("say"), *(["-v", voice] if voice else []), "-r", str(SAY_RATE),
                  "-o", str(aiff)], stdin=text.encode("utf-8"))
            afconvert = shutil.which("afconvert")
            if afconvert is not None:
                _run([afconvert, "-f", "WAVE", "-d", f"LEI16@{SAY_WAV_RATE}", "-c", "1",
                      str(aiff), str(dest)])
            elif self._ffmpeg is not None:
                _run([self._ffmpeg, *FFMPEG_QUIET, "-i", str(aiff), "-ac", "1",
                      "-ar", str(SAY_WAV_RATE), "-sample_fmt", "s16", "-f", "wav", str(dest)])
            else:
                raise RuntimeError("neither afconvert nor ffmpeg to turn the speech into WAV")
        finally:
            aiff.unlink(missing_ok=True)
