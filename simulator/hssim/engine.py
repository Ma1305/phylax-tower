"""The simulation loop: moves actors, reports positions once a second, keeps each radio
stream to one speaker at a time, posts every utterance when it ends, and listens for what
Hold Short tells traffic so the traffic can obey.

All timestamps sent to the tower are the real current UTC time (see the contract). ``speed``
only shortens the waits; it never changes the timestamps.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from hssim import phrases
from hssim.actors import Actor
from hssim.client import TowerClient, iso
from hssim.geo import LonLat
from hssim.world import World

log = logging.getLogger("hssim")
SUBSTEP_S = 0.2


@dataclass
class Said:
    """One utterance as the simulator spoke it."""

    stream_id: str
    speaker: str
    text: str
    t_start: datetime
    duration_s: float
    radio: str | None = None


@dataclass
class Outcome:
    scenario: str
    kind: str
    run_id: str | None
    seconds: float
    stats: dict[str, int]
    result: dict[str, Any] = field(default_factory=dict)


class Sim:
    def __init__(self, client: TowerClient, world: World, *, voicebox: Any = None,
                 audio: bool = True, speed: float = 1.0, tick_s: float = 1.0, seed: int = 7,
                 clock: Callable[[], datetime] | None = None, warm: bool = False):
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.client = client
        self.world = world
        # warm: a fast dry run that only collects the lines to synthesise (see cli.warm)
        self.warm_lines: list[tuple[Any, ...]] = []
        self._warm = warm
        self.voicebox = voicebox if audio and not warm else None
        self.speed = speed
        self.tick_s = tick_s
        self.rng = random.Random(seed)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.actors: dict[str, Actor] = {}
        self.sim_t = 0.0
        self.stats: Counter[str] = Counter()
        self.said: list[Said] = []
        self.outbound_calls: list[dict[str, Any]] = []
        self.outbound_handlers: list[Callable[[dict[str, Any]], None]] = [self._obey]
        self.shared: dict[str, Any] = {}
        self._stream_locks: dict[str, asyncio.Lock] = {}
        self._flows: set[asyncio.Task] = set()
        self._daemons: set[asyncio.Task] = set()
        self._posts: set[asyncio.Task] = set()
        self._pos_in_flight = 0
        self._running = False
        self._since: str | None = None

    # ---------------------------------------------------------------- time
    def utcnow(self) -> datetime:
        return self.clock()

    async def sleep(self, sim_seconds: float) -> None:
        if sim_seconds > 0:
            await asyncio.sleep(sim_seconds / self.speed)

    async def until(self, cond: Callable[[], bool], timeout: float | None = None,
                    poll: float = 0.2) -> bool:
        """Wait (in simulated seconds) until ``cond()`` is true. False on timeout."""
        waited = 0.0
        while not cond():
            if timeout is not None and waited >= timeout:
                return False
            await self.sleep(poll)
            waited += poll
        return True

    # -------------------------------------------------------------- actors
    def spawn(self, object_id: str, kind: str, *, position: LonLat, **kw: Any) -> Actor:
        actor = Actor(object_id, kind, position=position, **kw)
        self.actors[object_id] = actor
        return actor

    async def remove(self, actor: Actor, reason: str = "") -> None:
        actor.removed = True
        self.actors.pop(actor.object_id, None)
        self._background(self._safe(self.client.remove_object(actor.object_id, reason),
                                    "remove"))

    def by_callsign(self, callsign: str) -> Actor | None:
        return next((a for a in self.actors.values() if a.callsign == callsign), None)

    def separation_m(self, a: Actor, b: Actor) -> float:
        return self.world.frame.distance_m(a.position, b.position)

    # --------------------------------------------------------------- radio
    async def duration(self, speaker: str, text: str, radio: str | None = None) -> float:
        """How long this line will take on the air (synthesises and caches it now)."""
        clip = await self._clip(speaker, text, radio)
        return clip.duration_s if clip else phrases.estimate_duration_s(text)

    async def _clip(self, speaker: str, text: str, radio: str | None) -> Any:
        if self._warm and (speaker, text, radio) not in self.warm_lines:
            self.warm_lines.append((speaker, text, radio))
        if self.voicebox is None:
            return None
        try:
            return await asyncio.to_thread(self.voicebox.synth, speaker, text, radio=radio)
        except Exception as e:  # the voicebox should never raise; stay alive if it does
            log.warning("speech failed for %s (%s); sending text only", speaker,
                        type(e).__name__)
            self.stats["speech_errors"] += 1
            return None

    async def say(self, stream_id: str, speaker: str, text: str, *, radio: str | None = None,
                  gap_s: float = 0.7) -> Said:
        """Speak one call on a stream. Waits for the frequency to be free, takes as long as
        the call lasts, and posts it to the tower when it ends."""
        clip = await self._clip(speaker, text, radio)
        duration = clip.duration_s if clip else phrases.estimate_duration_s(text)
        lock = self._stream_locks.setdefault(stream_id, asyncio.Lock())
        async with lock:
            t_start = self.utcnow()
            log.info("%-8s %-7s %s", stream_id, speaker, text)
            await asyncio.sleep(duration / self.speed)
            self._post_utterance(stream_id, speaker, text, t_start, duration, clip)
            await asyncio.sleep(gap_s / self.speed)
        said = Said(stream_id, speaker, text, t_start, duration, radio)
        self.said.append(said)
        return said

    async def atc(self, stream_id: str, text: str, **kw: Any) -> Said:
        """A call from the controller working that stream (TOWER / GROUND / OPS)."""
        return await self.say(stream_id, self.world.station(stream_id), text, **kw)

    async def say_overlap(self, stream_id: str, first: tuple[str, str],
                          second: tuple[str, str], delay_s: float = 1.2) -> Said:
        """Two stations transmit on top of each other (a stepped-on call): one garbled clip."""
        text = f"{first[1]} // {second[1]}"
        a = await self._clip(first[0], first[1], None)
        b = await self._clip(second[0], second[1], None)
        if self._warm:
            self.warm_lines.append(("__overlap__", first, second, delay_s))
        clip = None
        if a is not None and b is not None:
            try:
                clip = await asyncio.to_thread(self.voicebox.overlap, a, b, delay_s)
            except Exception:
                clip = a
        duration = clip.duration_s if clip else max(
            phrases.estimate_duration_s(first[1]),
            delay_s + phrases.estimate_duration_s(second[1]))
        lock = self._stream_locks.setdefault(stream_id, asyncio.Lock())
        async with lock:
            t_start = self.utcnow()
            log.info("%-8s %-7s %s", stream_id, "BLOCKED", text)
            await asyncio.sleep(duration / self.speed)
            self._post_utterance(stream_id, None, text, t_start, duration, clip)
            await asyncio.sleep(0.7 / self.speed)
        said = Said(stream_id, "BLOCKED", text, t_start, duration)
        self.said.append(said)
        return said

    def _post_utterance(self, stream_id: str, speaker: str | None, text: str,
                        t_start: datetime, duration: float, clip: Any) -> None:
        if clip is not None:
            coro = self.client.audio(stream_id=stream_id, t_start=t_start, duration_s=duration,
                                     path=clip.path, mime=clip.mime, speaker=speaker,
                                     truth_text=text)
            self.stats["clips"] += 1
        else:
            coro = self.client.transcript(stream_id=stream_id, t_start=t_start, text=text,
                                          duration_s=duration, speaker=speaker)
            self.stats["transcripts"] += 1
        self.stats[f"utterances:{stream_id}"] += 1
        self._background(self._safe(coro, "utterance"))

    async def weather(self, visibility_sm: float, wind: str = "03012KT",
                      raw: str | None = None) -> None:
        log.info("WEATHER  vis %g sm wind %s", visibility_sm, wind)
        self.stats["weather"] += 1
        await self._safe(self.client.weather(visibility_sm, wind, raw), "weather")

    # --------------------------------------------------------------- flows
    def flow(self, coro: Awaitable[Any], name: str | None = None,
             daemon: bool = False) -> asyncio.Task:
        """Start an actor's script alongside the others. The scenario ends when every flow
        has ended; a ``daemon`` flow (background life that loops forever) is cancelled then."""
        task = asyncio.ensure_future(coro)
        if name:
            task.set_name(name)
        (self._daemons if daemon else self._flows).add(task)
        task.add_done_callback(self._flow_done)
        return task

    def _flow_done(self, task: asyncio.Task) -> None:
        self._flows.discard(task)
        self._daemons.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.stats["flow_errors"] += 1
            log.error("flow %s failed: %r", task.get_name(), task.exception())

    def _background(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._posts.add(task)
        task.add_done_callback(self._posts.discard)

    async def _safe(self, coro: Awaitable[Any], what: str) -> Any:
        try:
            return await coro
        except Exception as e:
            self.stats[f"errors:{what}"] += 1
            if self.stats[f"errors:{what}"] <= 3:
                log.warning("%s failed: %s", what, e)
            return None

    # ---------------------------------------------------------- background loops
    async def _ticker(self) -> None:
        interval = self.tick_s / self.speed
        next_at = time.monotonic()
        while self._running:
            steps = max(1, round(self.tick_s / SUBSTEP_S))
            for _ in range(steps):
                for actor in list(self.actors.values()):
                    actor.step(self.tick_s / steps)
            self.sim_t += self.tick_s
            self._report_positions()
            next_at += interval
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    def _report_positions(self) -> None:
        objects = [a.payload() for a in self.actors.values() if not a.removed]
        if not objects:
            return
        if self._pos_in_flight >= 3:  # the tower is slow: skip a beat rather than pile up
            self.stats["position_batches_skipped"] += 1
            return
        self._background(self._send_positions(objects, self.utcnow()))

    async def _send_positions(self, objects: list[dict], t: datetime) -> None:
        self._pos_in_flight += 1
        try:
            out = await self.client.positions(objects, t)
            self.stats["position_batches"] += 1
            for object_id, lane in ((out or {}).get("lanes") or {}).items():
                actor = self.actors.get(object_id)
                if actor is not None:
                    actor.lane_id = lane
        except Exception as e:
            self.stats["errors:positions"] += 1
            if self.stats["errors:positions"] <= 3:
                log.warning("positions failed: %s", e)
        finally:
            self._pos_in_flight -= 1

    async def _outbound_poller(self) -> None:
        interval = max(0.05, 1.0 / self.speed)
        while self._running:
            try:
                out = await self.client.outbound(self._since)
                self._since = (out or {}).get("now") or self._since
                for call in (out or {}).get("calls") or []:
                    self._heard(call)
            except Exception as e:
                self.stats["errors:outbound"] += 1
                if self.stats["errors:outbound"] <= 3:
                    log.warning("outbound poll failed: %s", e)
            await asyncio.sleep(interval)

    def _heard(self, call: dict[str, Any]) -> None:
        key = call.get("decision_id") or (call.get("t"), call.get("text"))
        if any((c.get("decision_id") or (c.get("t"), c.get("text"))) == key
               for c in self.outbound_calls):
            return
        call = {**call, "heard_at": self.utcnow(), "heard_sim_t": self.sim_t}
        self.outbound_calls.append(call)
        self.stats["outbound_calls"] += 1
        log.info("HOLDSHORT %s -> %s: %s", call.get("action"), call.get("target"),
                 call.get("text"))
        for handler in list(self.outbound_handlers):
            handler(call)

    def _obey(self, call: dict[str, Any]) -> None:
        """Default reaction of traffic to what Hold Short says on the radio."""
        action = call.get("action")
        text = (call.get("text") or "").lower()
        targets = [self.by_callsign(cs) for cs in call.get("target") or []]
        actors = [a for a in targets if a is not None]
        if not actors and action == "stop":
            # addressed to nobody by name ("Vehicle on runway zero four, STOP"): anyone with
            # a radio and no callsign on that lane takes it as theirs
            actors = [a for a in self.actors.values()
                      if a.callsign is None and a.has_radio
                      and (call.get("lane_id") is None or a.lane_id == call.get("lane_id"))]
        for actor in actors:
            actor.calls.append(call)
            if "go around" in text and actor.kind == "aircraft":
                actor.go_around = True
            elif action == "stop" or "hold short" in text or "hold position" in text:
                policy = getattr(actor, "stop_policy", None)
                if policy is not None:
                    policy(actor, call)
                elif actor.status == "ground":
                    actor.hold()

    # ------------------------------------------------------------------ run
    async def run(self, main: Callable[[Sim], Awaitable[Any]], *, scenario: str, kind: str,
                  note: str = "", reset: bool = True, announce: bool = True) -> Outcome:
        """Play one scenario from start to end."""
        started = time.monotonic()
        if reset:
            await self._safe(self.client.reset(), "reset")
        run_id = None
        if announce:
            run_id = await self._safe(self.client.run_start(scenario, kind, note), "run_start")
        self._since = iso(self.utcnow())
        self._running = True
        loops = [asyncio.ensure_future(self._ticker()),
                 asyncio.ensure_future(self._outbound_poller())]
        result: Any = None
        try:
            result = await main(self)
            while self._flows:
                await asyncio.gather(*list(self._flows), return_exceptions=True)
            await asyncio.sleep(1.2 / self.speed)   # one last position report
        finally:
            self._running = False
            leftovers = [*self._flows, *self._daemons]
            for flow in leftovers:
                flow.cancel()
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops, *leftovers, return_exceptions=True)
            if self._posts:
                await asyncio.gather(*list(self._posts), return_exceptions=True)
            for actor in list(self.actors.values()):
                await self._safe(self.client.remove_object(actor.object_id, "scenario ended"),
                                 "remove")
            self.actors.clear()
            if run_id:
                await self._safe(self.client.run_end(run_id), "run_end")
        return Outcome(scenario, kind, run_id, round(time.monotonic() - started, 1),
                       dict(self.stats), result if isinstance(result, dict) else {})
