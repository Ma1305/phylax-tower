"""Command line for the Phylax Tower simulator.

    python -m hssim list
    python -m hssim run crossing --api http://localhost:8000
    python -m hssim run normal --loop
    python -m hssim run crossing --stub --auto-stop 6 --speed 20 --no-audio   # no tower needed
    python -m hssim warm all                 # synthesise and cache every line of speech
    python -m hssim stub --port 8000         # a stand-in tower for development
    python -m hssim panel                    # a web page with a Start button per scenario
    python -m hssim serve                    # normal traffic forever + inject incidents
    python -m hssim inject crossing          # ... into a running `serve`
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import secrets
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from hssim.client import TowerClient
from hssim.engine import Outcome, Sim
from hssim.scenarios import SCENARIOS
from hssim.world import World

log = logging.getLogger("hssim")
ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / ".cache" / "control-token"
TOKEN_HEADER = "X-Sim-Token"


def new_control_token() -> str:
    """The secret `serve` requires on POST /inject. $SIM_CONTROL_TOKEN if set, else a random
    one, saved (owner-only) where `hssim inject` on the same machine can read it. Without it
    any web page open in a browser on this machine could trigger an incident."""
    token = os.environ.get("SIM_CONTROL_TOKEN") or secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.touch(mode=0o600, exist_ok=True)
    TOKEN_FILE.chmod(0o600)
    TOKEN_FILE.write_text(token)
    return token


def read_control_token() -> str | None:
    if os.environ.get("SIM_CONTROL_TOKEN"):
        return os.environ["SIM_CONTROL_TOKEN"]
    return TOKEN_FILE.read_text().strip() if TOKEN_FILE.is_file() else None


def load_env() -> None:
    """Pick up TOWER_API, INGEST_KEY, ELEVENLABS_API_KEY, VOICE_ENABLED from a .env file
    (never printed). Values already in the environment win."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    candidates = [os.environ.get("SIM_ENV"), Path.cwd() / ".env", ROOT.parent / ".env",
                  Path.home() / "plane-management-2" / ".env"]
    for p in candidates:
        if p and Path(p).is_file():
            load_dotenv(p, override=False)


def _voicebox(args: argparse.Namespace) -> Any:
    if getattr(args, "no_audio", False):
        return None
    from hssim.audio import Voicebox

    return Voicebox(provider=args.tts, radio=args.radio)


def _client(args: argparse.Namespace) -> TowerClient:
    if getattr(args, "stub", False):
        from hssim.stub_tower import create_stub_app

        app = create_stub_app(auto_stop_after_s=args.auto_stop)
        return TowerClient("http://stub", transport=httpx.ASGITransport(app=app))
    return TowerClient(args.api, ingest_key=os.environ.get("INGEST_KEY") or None)


async def _world(client: TowerClient) -> World:
    try:
        return World(await client.world())
    except Exception as e:
        raise SystemExit(
            f"Cannot reach the tower's /ingest/world ({e}).\n"
            "Start the tower, pass --api URL, or use --stub to run against the built-in "
            "stand-in tower.") from e


def _opts(args: argparse.Namespace) -> dict[str, Any]:
    opts = {"lead_s": args.lead_s, "intruder": args.intruder, "cycles": args.cycles,
            "loop": args.loop, "short": args.short, "resume_wait_s": args.resume_wait_s,
            "crew_reaction_s": args.crew_reaction_s, "final_nm": args.final_nm}
    if getattr(args, "real_audio", None):
        opts["real_audio"] = Path(args.real_audio).expanduser()
        opts["real_audio_offset"] = real_audio_offset(args)
    return opts


def real_audio_offset(args: argparse.Namespace) -> float:
    """Where in the recording the landing clearance (23:35:07 local, 03:35:07 UTC) is."""
    from hssim.realaudio import parse_clock, start_seconds_from_name
    from hssim.scenarios.laguardia import T0_UTC_S, T_CLEARANCE

    if args.real_audio_clearance_at:
        offset = parse_clock(args.real_audio_clearance_at) - T_CLEARANCE
    else:
        start = (parse_clock(args.real_audio_start) if args.real_audio_start
                 else start_seconds_from_name(Path(args.real_audio)))
        if start is None:
            raise SystemExit(
                "Cannot tell when the recording starts. Pass --real-audio-start HH:MM:SS (the "
                "UTC time the file begins) or --real-audio-clearance-at MM:SS (where in the "
                "file the controller says 'Truck 1 and company, cross runway 4').")
        offset = T0_UTC_S - start
    return offset + args.real_audio_shift


async def collect_lines(name: str, opts: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Every line a scenario speaks, found by a fast silent dry run against the stub."""
    from hssim.stub_tower import create_stub_app

    scenario = SCENARIOS[name]
    client = TowerClient("http://stub", transport=httpx.ASGITransport(
        app=create_stub_app(auto_stop_after_s=3.0)))
    try:
        sim = Sim(client, World.load_default(), speed=300.0, warm=True)
        quiet = logging.getLogger("hssim").level
        logging.getLogger("hssim").setLevel(logging.ERROR)
        try:
            await sim.run(lambda s: scenario.run(s, {**opts, "loop": False, "cycles": 1,
                                                     "real_audio": None}),
                          scenario=name, kind=scenario.kind, announce=False)
        finally:
            logging.getLogger("hssim").setLevel(quiet)
    finally:
        await client.aclose()
    lines = list(sim.warm_lines)
    module = sys.modules[scenario.run.__module__]
    for const in ("HOLDING", "RESUME_READBACK", "CLEAR"):
        text = getattr(module, const, None)
        if text and ("TRUCK1", text, None) not in lines:
            lines.append(("TRUCK1", text, None))
    return lines


async def warm(names: list[str], box: Any, opts: dict[str, Any]) -> int:
    """Synthesise and cache every line of the named scenarios so a live run never waits
    for speech. Returns the number of lines."""
    jobs: list[tuple[Any, ...]] = []
    for name in names:
        for line in await collect_lines(name, opts):
            if line not in jobs:
                jobs.append(line)
    sem = asyncio.Semaphore(4)

    async def one(job: tuple[Any, ...]) -> None:
        async with sem:
            if job[0] == "__overlap__":
                _, first, second, delay = job
                a = await asyncio.to_thread(box.synth, first[0], first[1])
                b = await asyncio.to_thread(box.synth, second[0], second[1])
                await asyncio.to_thread(box.overlap, a, b, delay)
            else:
                speaker, text, radio = job
                await asyncio.to_thread(box.synth, speaker, text, radio=radio)

    await asyncio.gather(*(one(j) for j in jobs))
    return len(jobs)


async def run_scenario(args: argparse.Namespace) -> Outcome:
    scenario = SCENARIOS[args.scenario]
    opts = _opts(args)
    box = _voicebox(args)
    if box is not None and not args.no_warm and getattr(box, "provider", "tone") != "tone":
        log.info("preparing speech (%s) ...", box.provider)
        n = await warm([args.scenario], box, opts)
        log.info("speech ready: %d lines cached", n)
    client = _client(args)
    try:
        world = await _world(client)
        sim = Sim(client, world, voicebox=box, audio=box is not None, speed=args.speed)
        log.info("scenario %s (%s) against %s", scenario.name, scenario.kind,
                 "the built-in stub" if args.stub else args.api)
        log.info("expect: %s", scenario.expect)
        return await sim.run(lambda s: scenario.run(s, opts), scenario=scenario.name,
                             kind=scenario.kind, note=scenario.summary,
                             reset=not args.no_reset)
    finally:
        await client.aclose()


async def serve(args: argparse.Namespace) -> None:
    """Normal traffic forever, with a small control API to inject incidents on demand."""
    import uvicorn
    from fastapi import FastAPI, Header, HTTPException

    token = new_control_token()
    box = _voicebox(args)
    opts = {**_opts(args), "loop": True}
    if box is not None and not args.no_warm and getattr(box, "provider", "tone") != "tone":
        log.info("preparing speech (%s) ...", box.provider)
        log.info("speech ready: %d lines cached", await warm(list(SCENARIOS), box, opts))
    client = _client(args)
    world = await _world(client)
    sim = Sim(client, world, voicebox=box, audio=box is not None, speed=args.speed)
    queue: asyncio.Queue[str] = asyncio.Queue()
    sim.shared["inject_queue"] = queue
    sim.shared["status"] = "normal traffic"

    control = FastAPI(title="Phylax Tower simulator control")

    @control.get("/status")
    def status() -> dict[str, Any]:
        return {"status": sim.shared.get("status"), "queued": queue.qsize(),
                "objects": sorted(sim.actors), "stats": dict(sim.stats),
                "outbound_calls": len(sim.outbound_calls),
                "scenarios": [n for n, s in SCENARIOS.items() if s.kind == "incident"]}

    @control.post("/inject/{name}")
    def inject(name: str, x_sim_token: str | None = Header(default=None)) -> dict[str, Any]:
        if not x_sim_token or not hmac.compare_digest(x_sim_token, token):
            raise HTTPException(403, f"missing or wrong {TOKEN_HEADER} header "
                                     "(use `python -m hssim inject <scenario>`)")
        if name not in SCENARIOS or name == "normal":
            raise HTTPException(404, f"unknown scenario {name!r}")
        queue.put_nowait(name)
        return {"queued": name, "note": "starts as soon as the traffic in progress is done"}

    server = uvicorn.Server(uvicorn.Config(control, host=args.control_host,
                                           port=args.control_port, log_level="warning"))
    log.info("control API on http://%s:%d  (GET /status; inject with "
             "`python -m hssim inject crossing`)", args.control_host, args.control_port)
    api = asyncio.ensure_future(server.serve())
    traffic = asyncio.ensure_future(
        sim.run(lambda s: SCENARIOS["normal"].run(s, opts), scenario="normal", kind="normal",
                note="continuous normal traffic", reset=not args.no_reset))
    try:
        # uvicorn takes Ctrl-C / SIGTERM for itself and just returns, so whichever of the
        # two ends first ends the other: stopping the control API stops the traffic too
        await asyncio.wait({api, traffic}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True
        traffic.cancel()
        await asyncio.gather(api, traffic, return_exceptions=True)
        await client.aclose()
        log.info("stopped: traffic removed from the tower and the run marker closed")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--api", default=os.environ.get("TOWER_API", "http://localhost:8000"),
                   help="tower base URL (default: $TOWER_API or http://localhost:8000)")
    p.add_argument("--stub", action="store_true",
                   help="run against the built-in stand-in tower instead of a real one")
    p.add_argument("--auto-stop", type=float, default=None, metavar="S",
                   help="with --stub: the stand-in tower issues a stop S seconds after it "
                        "hears a crossing clearance that conflicts with a landing")
    p.add_argument("--speed", type=float, default=1.0,
                   help="run faster than real time (timestamps stay real); default 1")
    p.add_argument("--no-audio", action="store_true",
                   help="send text only (POST /ingest/transcripts)")
    p.add_argument("--tts", default="auto", choices=["auto", "elevenlabs", "say", "tone"],
                   help="speech engine (auto: ElevenLabs if configured, else macOS say, "
                        "else tones)")
    p.add_argument("--radio", default="mild", choices=["clean", "mild", "medium", "brutal"],
                   help="how degraded the radio sounds")
    p.add_argument("--no-warm", action="store_true",
                   help="do not pre-generate speech before starting")
    p.add_argument("--no-reset", action="store_true",
                   help="do not clear the tower's live state first")
    p.add_argument("--lead-s", type=float, default=25.0,
                   help="crossing: seconds from the crossing clearance to the conflict")
    p.add_argument("--final-nm", type=float, default=4.0,
                   help="crossing: how far out the landing aircraft starts (2 makes a "
                        "run of about a minute and a half)")
    p.add_argument("--intruder", default="person", choices=["person", "vehicle"])
    p.add_argument("--cycles", type=int, default=1, help="normal: how many traffic cycles")
    p.add_argument("--loop", action="store_true", help="normal: run until interrupted")
    p.add_argument("--short", action="store_true",
                   help="end once the runway part is over (skip the taxi to the stand)")
    p.add_argument("--crew-reaction-s", type=float, default=2.0,
                   help="laguardia: seconds between Truck 1 hearing a stop addressed to it "
                        "and braking")
    p.add_argument("--real-audio", metavar="FILE",
                   help="laguardia: play this recording of the tower frequency instead of "
                        "generated speech (the file stays outside the repository)")
    p.add_argument("--real-audio-start", metavar="HH:MM:SS",
                   help="UTC time the recording begins (read from a name like ...-0330Z.mp3 "
                        "when omitted)")
    p.add_argument("--real-audio-clearance-at", metavar="MM:SS",
                   help="instead: where in the recording the crossing clearance is spoken")
    p.add_argument("--real-audio-shift", type=float, default=0.0, metavar="S",
                   help="nudge the recording later (+) or earlier (-) by S seconds")
    p.add_argument("--resume-wait-s", type=float, default=45.0,
                   help="crossing: how long the held truck waits for an approved instruction "
                        "to cross")


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="python -m hssim", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the scenarios and what each should make the tower do")
    p_run = sub.add_parser("run", help="play one scenario")
    p_run.add_argument("scenario", choices=sorted(SCENARIOS))
    _common(p_run)
    p_warm = sub.add_parser("warm", help="synthesise and cache the speech of scenarios")
    p_warm.add_argument("scenario", choices=[*sorted(SCENARIOS), "all"])
    _common(p_warm)
    p_stub = sub.add_parser("stub", help="serve the stand-in tower")
    p_stub.add_argument("--port", type=int, default=8000)
    p_stub.add_argument("--host", default="127.0.0.1")
    p_stub.add_argument("--auto-stop", type=float, default=None, metavar="S")
    p_serve = sub.add_parser("serve", help="normal traffic forever + inject incidents")
    _common(p_serve)
    p_serve.add_argument("--control-port", type=int, default=8200)
    p_serve.add_argument("--control-host", default="127.0.0.1")
    p_panel = sub.add_parser("panel", help="a web page with a Start button per scenario")
    _common(p_panel)
    p_panel.add_argument("--control-port", type=int, default=8200)
    p_panel.add_argument("--control-host", default="127.0.0.1")
    p_panel.add_argument("--visualizer-url", default="http://localhost:5180",
                         help="where the page's link to the visualizer points")
    p_inject = sub.add_parser("inject", help="queue an incident in a running `serve`")
    p_inject.add_argument("scenario", choices=sorted(n for n in SCENARIOS if n != "normal"))
    p_inject.add_argument("--control", default="http://127.0.0.1:8200")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "list":
        for s in SCENARIOS.values():
            print(f"{s.name}  [{s.kind}]\n  {s.summary}\n  expect: {s.expect}\n")
        return 0
    if args.cmd == "stub":
        import uvicorn

        from hssim.stub_tower import create_stub_app

        uvicorn.run(create_stub_app(ingest_key=os.environ.get("INGEST_KEY") or None,
                                    auto_stop_after_s=args.auto_stop),
                    host=args.host, port=args.port, log_level="info")
        return 0
    if args.cmd == "inject":
        token = read_control_token()
        if not token:
            print("no control token found: is `python -m hssim serve` running on this machine? "
                  "(or set SIM_CONTROL_TOKEN to the one it was started with)")
            return 1
        r = httpx.post(f"{args.control.rstrip('/')}/inject/{args.scenario}",
                       headers={TOKEN_HEADER: token}, timeout=10)
        print(r.status_code, r.text)
        return 0 if r.status_code == 200 else 1
    try:
        if args.cmd == "warm":
            box = _voicebox(args)
            if box is None:
                print("nothing to do with --no-audio")
                return 0
            names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
            n = asyncio.run(warm(names, box, _opts(args)))
            print(f"{n} lines cached with the {box.provider} voice engine")
            return 0
        if args.cmd == "serve":
            asyncio.run(serve(args))
            return 0
        if args.cmd == "panel":
            from hssim.panel import serve_panel

            asyncio.run(serve_panel(args))
            return 0
        outcome = asyncio.run(run_scenario(args))
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
    print(json.dumps(asdict(outcome), indent=2, default=str))
    return 0 if outcome.result.get("ok", True) else 3
