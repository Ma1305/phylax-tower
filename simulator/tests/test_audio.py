"""hssim.audio: speech providers, cache, radio effect and overlap. No network is used."""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import struct
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from hssim import audio, phrases
from hssim.audio import Clip, Voicebox

LINE = "LaGuardia tower, Air Canada eight six four six, holding short runway four"
OTHER_LINE = "Air Canada eight six four six, LaGuardia tower, cross runway four"
KEY = "sk_test_0123456789abcdef_not_a_real_key"
MP3 = b"\xff\xfb\x90\x00" + bytes(15996)  # 16 000 bytes: one second at 128 kbit/s
ACCOUNT_VOICES = ["voiceA", "voiceB", "voiceC", "voiceD", "voiceE"]


REAL_WORKING_TOOL = audio._working_tool


@pytest.fixture(autouse=True)
def no_tools(request, monkeypatch):
    """Every test runs as if ffmpeg and ffprobe were missing, unless it asks for one."""
    if "real_ffmpeg" not in request.fixturenames:
        monkeypatch.setattr(audio, "_working_tool", lambda name: None)


@pytest.fixture
def real_ffmpeg():
    if audio._working_tool("ffmpeg") is None:
        pytest.skip("no working ffmpeg on PATH")


class FakeFfmpeg:
    """Stands in for ffmpeg: notes each command and writes one second of 8 kHz silence."""

    path = "/fake/bin/ffmpeg"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.broken = False

    def run(self, cmd: list[str], stdin: bytes | None = None) -> bytes:
        assert cmd[0] == self.path and all(isinstance(arg, str) for arg in cmd)
        self.calls.append(cmd[1:])
        if self.broken:
            raise RuntimeError("ffmpeg exited 1: told to fail")
        with wave.open(cmd[-1], "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(bytes(16000))
        return b""


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    fake = FakeFfmpeg()
    monkeypatch.setattr(audio, "_working_tool",
                        lambda name: fake.path if name == "ffmpeg" else None)
    monkeypatch.setattr(audio, "_run", fake.run)
    return fake


def tone_box(tmp_path: Path, radio: str = "clean") -> Voicebox:
    return Voicebox(cache_dir=tmp_path / "cache", provider="tone", radio=radio, env={})


def wav_info(path: Path) -> tuple[int, int, int, float]:
    """(sample rate, channels, bytes per sample, seconds) from the header."""
    with wave.open(str(path), "rb") as src:
        return (src.getframerate(), src.getnchannels(), src.getsampwidth(),
                src.getnframes() / src.getframerate())


def wav_samples(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as src:
        count = src.getnframes()
        return list(struct.unpack(f"<{count}h", src.readframes(count)))


def pitch_hz(path: Path) -> float:
    """Pitch of a steady tone, from how often it crosses zero."""
    samples = wav_samples(path)
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return crossings * wav_info(path)[0] / (2 * len(samples))


# -- tone provider ------------------------------------------------------------------


def test_tone_clip_is_a_valid_wav_of_the_estimated_length(tmp_path):
    box = tone_box(tmp_path)
    clip = box.synth("TOWER", LINE)

    assert isinstance(clip, Clip)
    assert clip.path.is_file() and clip.path.parent == tmp_path / "cache"
    assert (clip.mime, clip.provider, clip.text) == ("audio/wav", "tone", LINE)
    rate, channels, width, seconds = wav_info(clip.path)
    assert (rate, channels, width) == (8000, 1, 2)
    assert clip.duration_s == seconds
    assert abs(clip.duration_s - phrases.estimate_duration_s(LINE)) < 0.2
    with pytest.raises(dataclasses.FrozenInstanceError):
        clip.text = "changed"


def test_tone_has_soft_edges_and_does_not_clip(tmp_path):
    samples = wav_samples(tone_box(tmp_path).synth("TOWER", LINE).path)
    assert abs(samples[0]) < 300 and abs(samples[-1]) < 300
    assert 8000 < max(abs(s) for s in samples) < 20000


def test_second_call_returns_the_cached_file_without_regenerating(tmp_path, monkeypatch):
    made = []
    write_tone = audio._write_tone
    monkeypatch.setattr(audio, "_write_tone",
                        lambda dest, text, hz: (made.append(text), write_tone(dest, text, hz)))
    box = tone_box(tmp_path)
    first = box.synth("TOWER", LINE)
    stamp = first.path.stat().st_mtime_ns

    assert box.synth("TOWER", LINE).path == first.path
    assert tone_box(tmp_path).synth("TOWER", LINE) == first  # a new process finds it on disk
    assert made == [LINE]
    assert first.path.stat().st_mtime_ns == stamp
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [first.path.name]

    assert box.synth("TOWER", OTHER_LINE).path != first.path
    assert made == [LINE, OTHER_LINE]


def test_each_speaker_has_its_own_pitch(tmp_path):
    box = tone_box(tmp_path)
    clips = {who: box.synth(who, LINE) for who in ("TOWER", "GROUND", "OPS", "AC8646")}

    assert len({clip.path for clip in clips.values()}) == 4
    pitches = {who: pitch_hz(clip.path) for who, clip in clips.items()}
    assert pitches["TOWER"] == pytest.approx(523, rel=0.02)
    assert pitches["GROUND"] == pytest.approx(587, rel=0.02)
    assert pitches["OPS"] == pytest.approx(659, rel=0.02)
    assert pitches["AC8646"] > 700
    again = box.synth("ac8646 ", OTHER_LINE)  # same aircraft, however the name is typed
    assert pitch_hz(again.path) == pytest.approx(pitches["AC8646"], rel=0.01)


def test_same_line_from_many_threads_is_made_once(tmp_path, monkeypatch):
    made = []
    write_tone = audio._write_tone
    monkeypatch.setattr(audio, "_write_tone",
                        lambda dest, text, hz: (made.append(text), write_tone(dest, text, hz)))
    box = tone_box(tmp_path)
    with ThreadPoolExecutor(8) as pool:
        clips = list(pool.map(lambda _: box.synth("TOWER", LINE), range(16)))

    assert len({clip.path for clip in clips}) == 1
    assert made == [LINE]
    assert not list((tmp_path / "cache").glob(".tmp-*"))


def test_default_cache_dir_is_created(tmp_path, monkeypatch):
    assert audio.DEFAULT_CACHE_DIR == Path(audio.__file__).resolve().parents[1] / ".cache" / "audio"
    monkeypatch.setattr(audio, "DEFAULT_CACHE_DIR", tmp_path / "deep" / ".cache" / "audio")
    box = Voicebox(provider="tone", env={})
    assert box.cache_dir == tmp_path / "deep" / ".cache" / "audio" and box.cache_dir.is_dir()


# -- provider resolution ------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "say_installed", "expected"),
    [
        ({}, False, "tone"),
        ({}, True, "say"),
        ({"SIM_TTS": "tone"}, True, "tone"),
        ({"SIM_TTS": "say"}, False, "say"),
        ({"SIM_TTS": " ElevenLabs "}, True, "elevenlabs"),
        ({"SIM_TTS": "tone", "ELEVENLABS_API_KEY": KEY, "VOICE_ENABLED": "true"}, True, "tone"),
        ({"SIM_TTS": "festival", "ELEVENLABS_API_KEY": KEY, "VOICE_ENABLED": "1"}, True,
         "elevenlabs"),
        ({"SIM_TTS": "festival"}, True, "say"),
        ({"SIM_TTS": ""}, False, "tone"),
        ({"ELEVENLABS_API_KEY": KEY, "VOICE_ENABLED": "true"}, True, "elevenlabs"),
        ({"ELEVENLABS_API_KEY": KEY, "VOICE_ENABLED": "1"}, False, "elevenlabs"),
        ({"ELEVENLABS_API_KEY": KEY, "VOICE_ENABLED": "Yes"}, False, "elevenlabs"),
        ({"ELEVENLABS_API_KEY": KEY, "VOICE_ENABLED": "on"}, False, "elevenlabs"),
        ({"ELEVENLABS_API_KEY": KEY, "VOICE_ENABLED": "false"}, True, "say"),
        ({"ELEVENLABS_API_KEY": KEY, "VOICE_ENABLED": "0"}, False, "tone"),
        ({"ELEVENLABS_API_KEY": KEY}, False, "tone"),
        ({"ELEVENLABS_API_KEY": "  ", "VOICE_ENABLED": "true"}, False, "tone"),
        ({"VOICE_ENABLED": "true"}, True, "say"),
    ],
)
def test_auto_provider_follows_the_environment(tmp_path, monkeypatch, env, say_installed, expected):
    monkeypatch.setattr(audio, "_say_available", lambda: say_installed)
    assert Voicebox(cache_dir=tmp_path, env=env).provider == expected


def test_explicit_provider_beats_the_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "_say_available", lambda: True)
    env = {"SIM_TTS": "elevenlabs", "ELEVENLABS_API_KEY": KEY, "VOICE_ENABLED": "true"}
    assert Voicebox(cache_dir=tmp_path, provider="tone", env=env).provider == "tone"
    assert Voicebox(cache_dir=tmp_path, provider="say", env=env).provider == "say"


def test_env_defaults_to_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_TTS", "tone")
    assert Voicebox(cache_dir=tmp_path).provider == "tone"


def test_unknown_provider_or_radio_level_is_refused(tmp_path):
    with pytest.raises(ValueError):
        Voicebox(cache_dir=tmp_path, provider="festival", env={})
    with pytest.raises(ValueError):
        Voicebox(cache_dir=tmp_path, provider="tone", radio="loud", env={})
    with pytest.raises(ValueError):
        tone_box(tmp_path).synth("TOWER", LINE, radio="loud")


# -- ElevenLabs ---------------------------------------------------------------------


class FakeEleven:
    """Answers like api.elevenlabs.io and keeps every request it saw."""

    def __init__(self, speech_status: int = 200, voices_status: int = 200):
        self.speech_status = speech_status
        self.voices_status = voices_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/voices":
            ids = [*ACCOUNT_VOICES, *audio.ELEVEN_STATION_VOICES.values()]
            return httpx.Response(self.voices_status, json={"voices": [
                {"voice_id": voice, "name": voice, "category": "premade"} for voice in ids]})
        if self.speech_status != 200:
            # A server that echoes the key back, to prove it is scrubbed on our side.
            sent = request.headers.get("xi-api-key", "")
            return httpx.Response(self.speech_status, json={
                "detail": {"status": "invalid_api_key", "message": f"Invalid API key {sent}"}})
        return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})

    def speech(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith("/v1/text-to-speech/")]

    def voice_lists(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/v1/voices"]

    def spoken_by(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[1] for r in self.speech()]


def eleven_box(tmp_path: Path, api, radio: str = "clean", **env: str) -> Voicebox:
    settings = {"ELEVENLABS_API_KEY": KEY, "VOICE_ENABLED": "true", **env}
    return Voicebox(cache_dir=tmp_path / "cache", radio=radio, env=settings,
                    http=httpx.Client(transport=httpx.MockTransport(api)))


def test_elevenlabs_request_and_clip(tmp_path):
    api = FakeEleven()
    box = eleven_box(tmp_path, api)
    assert box.provider == "elevenlabs"
    clip = box.synth("TOWER", LINE)

    (request,) = api.requests  # a station voice needs no look at the account's voices
    assert request.method == "POST"
    assert str(request.url) == ("https://api.elevenlabs.io/v1/text-to-speech/"
                                "21m00Tcm4TlvDq8ikWAM?output_format=mp3_44100_128")
    assert request.headers["xi-api-key"] == KEY
    assert json.loads(request.content) == {
        "text": LINE,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    assert (clip.provider, clip.mime, clip.text) == ("elevenlabs", "audio/mpeg", LINE)
    assert clip.path.suffix == ".mp3" and clip.path.read_bytes() == MP3
    assert clip.duration_s == pytest.approx(len(MP3) * 8 / 128000)

    assert box.synth("TOWER", LINE) == clip  # cached: no second request
    assert len(api.requests) == 1


def test_elevenlabs_station_voices_and_env_overrides(tmp_path):
    api = FakeEleven()
    box = eleven_box(tmp_path, api, ELEVENLABS_MODEL="eleven_flash_v2_5",
                     ELEVENLABS_VOICE_OPS="opsOverride", ELEVENLABS_VOICE_AC8646="pilotOverride")
    for who in ("TOWER", "GROUND", "OPS", "AC8646"):
        box.synth(who, LINE)

    assert api.spoken_by() == ["21m00Tcm4TlvDq8ikWAM", "onwK4e9ZLuTAKqWW03F9", "opsOverride",
                               "pilotOverride"]
    assert not api.voice_lists()
    assert json.loads(api.speech()[0].content)["model_id"] == "eleven_flash_v2_5"


def test_elevenlabs_same_speaker_always_gets_the_same_voice(tmp_path):
    api = FakeEleven()
    box = eleven_box(tmp_path, api)
    for who, text in (("AC8646", LINE), ("DL2210", LINE), ("AC8646", OTHER_LINE),
                      ("DL2210", OTHER_LINE), ("ac8646", "Air Canada eight six four six, roger")):
        assert box.synth(who, text).provider == "elevenlabs"

    first, second, third, fourth, fifth = api.spoken_by()
    assert first == third == fifth and second == fourth
    assert {first, second} <= set(ACCOUNT_VOICES)  # from the account, never a station's voice
    assert len(api.voice_lists()) == 1  # fetched once, kept in memory
    assert api.voice_lists()[0].headers["xi-api-key"] == KEY

    again = FakeEleven()
    eleven_box(tmp_path / "second", again).synth("AC8646", LINE)
    assert again.spoken_by() == [first]  # and the same in the next run


def test_elevenlabs_uses_builtin_voices_when_the_account_list_fails(tmp_path, caplog):
    api = FakeEleven(voices_status=500)
    box = eleven_box(tmp_path, api)
    with caplog.at_level(logging.WARNING, logger="hssim.audio"):
        clips = [box.synth("AC8646", text) for text in (LINE, OTHER_LINE)]

    assert [clip.provider for clip in clips] == ["elevenlabs", "elevenlabs"]
    first, second = api.spoken_by()
    assert first == second and first in audio.ELEVEN_SPARE_VOICES
    assert first not in audio.ELEVEN_STATION_VOICES.values()
    assert len(api.voice_lists()) == 1
    assert not caplog.records


def test_elevenlabs_401_falls_back_to_tone_without_leaking_the_key(tmp_path, caplog):
    api = FakeEleven(speech_status=401, voices_status=401)
    box = eleven_box(tmp_path, api)
    with caplog.at_level(logging.DEBUG, logger="hssim.audio"):
        clip = box.synth("AC8646", LINE)
        again = box.synth("AC8646", LINE)

    assert again == clip  # the line stays a tone: no second attempt, no second warning
    assert len(api.speech()) == 1 and len(api.voice_lists()) == 1
    assert (clip.provider, clip.mime, clip.text) == ("tone", "audio/wav", LINE)
    assert wav_info(clip.path)[:3] == (8000, 1, 2)
    assert abs(clip.duration_s - phrases.estimate_duration_s(LINE)) < 0.2
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "401" in warnings[0].getMessage() and "AC8646" in warnings[0].getMessage()
    assert KEY not in caplog.text
    for path in tmp_path.rglob("*"):
        assert path.is_dir() or KEY.encode() not in path.read_bytes()
    assert not list((tmp_path / "cache").glob("*.mp3"))


def test_elevenlabs_outage_only_costs_the_lines_it_hit(tmp_path, caplog):
    api = FakeEleven(speech_status=503)
    box = eleven_box(tmp_path, api)
    with caplog.at_level(logging.WARNING, logger="hssim.audio"):
        during = box.synth("TOWER", LINE)
        api.speech_status = 200
        after = box.synth("TOWER", OTHER_LINE)
        same_line = box.synth("TOWER", LINE)

    assert (during.provider, after.provider) == ("tone", "elevenlabs")
    assert same_line == during  # what was already on the air does not change voice mid-run
    assert len(api.speech()) == 2 and len(caplog.records) == 1
    restarted = eleven_box(tmp_path, api)  # a new run shares the disk cache and tries again
    assert restarted.synth("TOWER", LINE).provider == "elevenlabs"


def test_nothing_to_say_is_a_tone_without_asking_the_provider(tmp_path, caplog):
    api = FakeEleven()
    box = eleven_box(tmp_path, api)
    with caplog.at_level(logging.WARNING, logger="hssim.audio"):
        clip = box.synth("TOWER", "  ")

    assert clip.provider == "tone" and clip.duration_s == phrases.estimate_duration_s("")
    assert not api.requests and not caplog.records


def test_elevenlabs_network_error_falls_back_to_tone(tmp_path, caplog):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    box = eleven_box(tmp_path, unreachable)
    with caplog.at_level(logging.WARNING, logger="hssim.audio"):
        clip = box.synth("TOWER", LINE)

    assert clip.provider == "tone" and clip.path.is_file()
    assert len(caplog.records) == 1 and KEY not in caplog.text


def test_elevenlabs_without_a_key_falls_back_without_calling_out(tmp_path, caplog):
    api = FakeEleven()
    box = Voicebox(cache_dir=tmp_path, provider="elevenlabs", radio="clean", env={},
                   http=httpx.Client(transport=httpx.MockTransport(api)))
    with caplog.at_level(logging.WARNING, logger="hssim.audio"):
        clip = box.synth("AC8646", LINE)

    assert box.provider == "elevenlabs" and clip.provider == "tone"
    assert not api.requests and len(caplog.records) == 1


def test_elevenlabs_same_line_from_many_threads_is_bought_once(tmp_path):
    api = FakeEleven()
    box = eleven_box(tmp_path, api)
    with ThreadPoolExecutor(8) as pool:
        clips = list(pool.map(lambda _: box.synth("AC8646", LINE), range(16)))

    assert len({clip.path for clip in clips}) == 1
    assert len(api.speech()) == 1 and len(api.voice_lists()) == 1


# -- overlap ------------------------------------------------------------------------


def test_overlap_without_ffmpeg_keeps_the_first_clip_and_stretches_it(tmp_path):
    box = tone_box(tmp_path)
    a = box.synth("TRUCK1", LINE)
    b = box.synth("AC8646", OTHER_LINE)
    mixed = box.overlap(a, b)

    assert mixed == dataclasses.replace(a, duration_s=1.2 + b.duration_s,
                                        text=f"{LINE} // {OTHER_LINE}")
    assert box.overlap(a, b, delay_s=0.4).duration_s == pytest.approx(0.4 + b.duration_s)
    short = box.synth("OPS", "roger")
    inside = box.overlap(a, short, delay_s=0.5)  # b is over before a ends
    assert inside.duration_s == a.duration_s and inside.text == f"{LINE} // roger"


def test_overlap_mixes_with_ffmpeg_and_caches(tmp_path, fake_ffmpeg):
    box = tone_box(tmp_path)
    a = box.synth("TRUCK1", LINE)
    b = box.synth("AC8646", OTHER_LINE)
    mixed = box.overlap(a, b, delay_s=0.8)

    assert mixed.path not in (a.path, b.path) and mixed.path.parent == box.cache_dir
    assert (mixed.mime, mixed.provider, mixed.text) == ("audio/wav", "tone",
                                                        f"{LINE} // {OTHER_LINE}")
    assert mixed.duration_s == 1.0  # read from the file the stand-in wrote
    (args,) = fake_ffmpeg.calls
    assert args.index(str(a.path)) < args.index(str(b.path))
    graph = args[args.index("-filter_complex") + 1]
    assert "adelay=800" in graph and "amix=inputs=2:duration=longest" in graph
    assert args[args.index("-ar") + 1] == "8000" and args[args.index("-ac") + 1] == "1"

    assert box.overlap(a, b, delay_s=0.8) == mixed
    assert len(fake_ffmpeg.calls) == 1
    assert box.overlap(a, b, delay_s=1.2).path != mixed.path


def test_overlap_falls_back_when_ffmpeg_fails(tmp_path, fake_ffmpeg, caplog):
    box = tone_box(tmp_path)
    a = box.synth("TRUCK1", LINE)
    b = box.synth("AC8646", OTHER_LINE)
    fake_ffmpeg.broken = True
    with caplog.at_level(logging.WARNING, logger="hssim.audio"):
        mixed = box.overlap(a, b)

    assert mixed.path == a.path and mixed.duration_s == pytest.approx(1.2 + b.duration_s)
    assert len(caplog.records) == 1


def test_overlap_with_real_ffmpeg(tmp_path, real_ffmpeg):
    box = tone_box(tmp_path)
    a = box.synth("TRUCK1", LINE)
    b = box.synth("AC8646", OTHER_LINE)
    mixed = box.overlap(a, b, delay_s=1.2)

    assert mixed.path not in (a.path, b.path)
    rate, channels, width, seconds = wav_info(mixed.path)
    assert (rate, channels, width) == (8000, 1, 2)
    assert mixed.duration_s == seconds
    assert mixed.duration_s == pytest.approx(max(a.duration_s, 1.2 + b.duration_s), abs=0.1)
    assert mixed.text == f"{LINE} // {OTHER_LINE}"
    assert box.overlap(a, b, delay_s=1.2) == mixed


# -- radio effect -------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("true") is None or shutil.which("false") is None,
                    reason="needs the true and false commands")
def test_a_tool_that_is_missing_or_does_not_run_counts_as_absent():
    assert REAL_WORKING_TOOL("true") == shutil.which("true")
    assert REAL_WORKING_TOOL("false") is None  # on PATH, but exits non-zero
    assert REAL_WORKING_TOOL("no-such-tool-hssim") is None


def test_radio_level_without_ffmpeg_returns_the_clean_speech(tmp_path):
    box = tone_box(tmp_path, radio="brutal")
    clip = box.synth("TOWER", LINE)
    assert clip == box.synth("TOWER", LINE, radio="clean")
    assert len(list(box.cache_dir.iterdir())) == 1


def test_radio_effect_uses_the_level_and_caches(tmp_path, fake_ffmpeg):
    box = tone_box(tmp_path, radio="mild")
    clean = box.synth("TOWER", LINE, radio="clean")
    assert not fake_ffmpeg.calls
    mild = box.synth("TOWER", LINE)  # the instance default
    brutal = box.synth("TOWER", LINE, radio="brutal")

    assert len({clean.path, mild.path, brutal.path}) == 3
    assert (brutal.mime, brutal.provider, brutal.text) == ("audio/wav", "tone", LINE)
    assert brutal.duration_s == 1.0  # read from the file the stand-in wrote
    mild_args, brutal_args = fake_ffmpeg.calls
    for args, (hp, lp, ratio, noise, squelch) in ((mild_args, (300, 3000, 3, 0.06, 0.15)),
                                                  (brutal_args, (450, 2400, 12, 0.55, 0.9))):
        assert args[args.index("-i") + 1] == str(clean.path)
        graph = args[args.index("-filter_complex") + 1]
        assert f"highpass=f={hp}:poles=2" in graph and f"lowpass=f={lp}:poles=2" in graph
        assert f"ratio={ratio}:" in graph and "aresample=8000" in graph
        assert f"atrim=0:{clean.duration_s + 0.3:.3f}" in graph
        assert f"anoisesrc=color=pink:amplitude={noise}:sample_rate=22050:seed=7" in args
        assert f"anoisesrc=color=white:amplitude={squelch}:sample_rate=22050:seed=11" in args

    assert box.synth("TOWER", LINE, radio="brutal") == brutal
    assert len(fake_ffmpeg.calls) == 2


def test_radio_effect_failure_returns_the_clean_speech(tmp_path, fake_ffmpeg, caplog):
    fake_ffmpeg.broken = True
    box = tone_box(tmp_path, radio="medium")
    with caplog.at_level(logging.WARNING, logger="hssim.audio"):
        clip = box.synth("TOWER", LINE)
        again = box.synth("TOWER", LINE)

    assert clip == again == box.synth("TOWER", LINE, radio="clean")
    assert len(caplog.records) == 1 and len(fake_ffmpeg.calls) == 1  # not retried every call


@pytest.mark.parametrize("level", ["mild", "medium", "brutal"])
def test_radio_effect_with_real_ffmpeg(tmp_path, real_ffmpeg, level):
    box = tone_box(tmp_path, radio=level)
    clean = box.synth("TOWER", LINE, radio="clean")
    clip = box.synth("TOWER", LINE)

    assert clip.path != clean.path and clip.provider == "tone"
    rate, channels, width, seconds = wav_info(clip.path)
    assert (rate, channels, width) == (8000, 1, 2)
    assert clip.duration_s == seconds
    assert clip.duration_s == pytest.approx(clean.duration_s + 0.3, abs=0.1)  # squelch padding
    assert max(abs(s) for s in wav_samples(clip.path)) > 1000
    assert box.synth("TOWER", LINE) == clip


# -- macOS say ----------------------------------------------------------------------

SAY_LISTING = """\
Albert              en_US    # Hello! My name is Albert.
Alice               it_IT    # Ciao! Mi chiamo Alice.
Daniel              en_GB    # Hello! My name is Daniel.
Eddy (German (Germany)) de_DE    # Hallo! Ich heiße Eddy.
Eddy (English (US)) en_US    # Hello! My name is Eddy.
Karen               en_AU    # Hi! My name is Karen.
Bad News            en_US    # Hello! My name is Bad News.
Moira               en_IE    # Hello! My name is Moira.
Rishi               en_IN    # Hello! My name is Rishi.
"""


def test_say_voices_come_from_the_installed_english_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "_need", lambda tool: tool)
    monkeypatch.setattr(audio, "_run", lambda cmd, stdin=None: SAY_LISTING.encode())
    box = Voicebox(cache_dir=tmp_path, provider="say", env={})
    voices = {who: box._voice("say", who) for who in ("TOWER", "GROUND", "OPS", "AC8646", "DL1")}

    # No Samantha here, so the tower takes its next choice and ops moves on from Karen.
    assert (voices["TOWER"], voices["GROUND"], voices["OPS"]) == ("Karen", "Daniel", "Moira")
    assert {voices["AC8646"], voices["DL1"]} <= {"Eddy (English (US))", "Rishi"}
    assert box._voice("say", "AC8646") == voices["AC8646"]


needs_say = pytest.mark.skipif(sys.platform != "darwin" or shutil.which("say") is None,
                               reason="needs macOS say")


@needs_say
def test_say_speaks_into_a_wav(tmp_path):
    box = Voicebox(cache_dir=tmp_path, provider="say", radio="clean", env={})
    clip = box.synth("TOWER", "radio check")

    assert (clip.provider, clip.mime, clip.text) == ("say", "audio/wav", "radio check")
    rate, channels, width, seconds = wav_info(clip.path)
    assert (channels, width) == (1, 2) and rate >= 8000
    assert clip.duration_s == seconds and 0.3 < seconds < 5
    assert max(abs(s) for s in wav_samples(clip.path)) > 500  # somebody is talking
    assert box.synth("TOWER", "radio check") == clip
    assert [p.suffix for p in tmp_path.iterdir()] == [".wav"]  # no AIFF left behind

    stations = {box._voice("say", who) for who in ("TOWER", "GROUND", "OPS")}
    assert len(stations) == 3 and "" not in stations
    assert box._voice("say", "AC8646") == box._voice("say", "AC8646") not in stations


def test_say_failure_falls_back_to_tone(tmp_path, monkeypatch, caplog):
    def missing(tool: str) -> str:
        raise RuntimeError(f"{tool} not found")

    monkeypatch.setattr(audio, "_need", missing)
    box = Voicebox(cache_dir=tmp_path, provider="say", radio="clean", env={})
    with caplog.at_level(logging.WARNING, logger="hssim.audio"):
        clip = box.synth("TOWER", LINE)

    assert box.provider == "say" and clip.provider == "tone" and clip.path.is_file()
    assert len(caplog.records) == 1 and "say not found" in caplog.text
