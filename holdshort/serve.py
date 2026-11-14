"""Run the whole tower in one container: a small process supervisor.

    python -m holdshort.serve [--only a,b] [--db NAME] [--port 8000] [--host 0.0.0.0]

Starts each process (api, gate, listener, state, memory, safety, procedures, weather,
speaker; see docs/v2/PLAN.md section 4) as a child, prefixes its log lines with its name,
restarts a child 2 s after it exits and gives up after 5 failures inside 60 s. SIGTERM /
SIGINT stops every child. Exits non-zero if the api child cannot start. ``--port`` defaults
to $PORT (Cloud Run), then 8000. Workers get ``--db NAME``; the api gets MONGODB_DB.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

# name -> module run with `python -m` ("api" is special: uvicorn)
PROCESSES: dict[str, str] = {
    "api": "holdshort.api.app",
    "gate": "holdshort.gate",
    "listener": "holdshort.listener",
    "state": "holdshort.state",
    "memory": "holdshort.memory",
    "safety": "holdshort.agents.safety_agent",
    "procedures": "holdshort.agents.procedures_agent",
    "weather": "holdshort.agents.weather_agent",
    "speaker": "holdshort.speaker",
}
RESTART_DELAY = 2.0
MAX_FAILURES = 5
FAILURE_WINDOW = 60.0
STOP_TIMEOUT = 10.0

_print_lock = threading.Lock()


def say(name: str, line: str) -> None:
    with _print_lock:
        sys.stdout.write(f"[{name}] {line.rstrip()}\n")
        sys.stdout.flush()


def runnable(module: str) -> bool:
    """True if `python -m module` can run: the module exists (packages need __main__)."""
    try:
        spec = importlib.util.find_spec(module)
        if spec is None:
            return False
        if spec.submodule_search_locations is not None:  # a package
            return importlib.util.find_spec(f"{module}.__main__") is not None
        return True
    except (ImportError, ValueError):
        return False


@dataclass
class Child:
    name: str
    argv: list[str]
    env: dict[str, str]
    proc: subprocess.Popen | None = None
    failures: deque = field(default_factory=deque)
    restart_at: float | None = None
    given_up: bool = False

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True,
        )
        say("serve", f"started {self.name} (pid {self.proc.pid})")
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            say(self.name, line)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)


def build_children(only: list[str] | None, db: str | None, host: str,
                   port: int) -> list[Child]:
    children: list[Child] = []
    for name in only or list(PROCESSES):
        module = PROCESSES[name]
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if name == "api":
            if db:
                env["MONGODB_DB"] = db
            argv = [sys.executable, "-m", "uvicorn", f"{module}:app",
                    "--host", host, "--port", str(port)]
        else:
            if not runnable(module):
                say("serve", f"WARNING: skipping {name}: {module} is not importable yet")
                continue
            argv = [sys.executable, "-m", module] + (["--db", db] if db else [])
        children.append(Child(name, argv, env))
    return children


def supervise(children: list[Child]) -> int:
    stopping = threading.Event()

    def on_signal(signum, _frame) -> None:
        say("serve", f"received {signal.Signals(signum).name}; stopping")
        stopping.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    for c in children:
        c.start()
    code = 0
    while not stopping.is_set():
        now = time.monotonic()
        for c in children:
            if c.given_up:
                continue
            if c.restart_at is not None:
                if now >= c.restart_at:
                    c.restart_at = None
                    c.start()
                continue
            rc = c.proc.poll() if c.proc else None
            if rc is None:
                continue
            c.failures.append(now)
            while c.failures and now - c.failures[0] > FAILURE_WINDOW:
                c.failures.popleft()
            if len(c.failures) >= MAX_FAILURES:
                c.given_up = True
                say("serve", f"ERROR: giving up on {c.name}: {MAX_FAILURES} failures in "
                    f"{FAILURE_WINDOW:.0f}s (last exit code {rc})")
                if c.name == "api":
                    code = 1
                    stopping.set()
            else:
                say("serve", f"{c.name} exited with code {rc}; restarting in "
                    f"{RESTART_DELAY:.0f}s")
                c.restart_at = now + RESTART_DELAY
        if children and all(c.given_up for c in children):
            code = 1
            break
        stopping.wait(0.2)

    for c in children:
        c.stop()
    deadline = time.monotonic() + STOP_TIMEOUT
    for c in children:
        if c.proc is None:
            continue
        try:
            c.proc.wait(max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            say("serve", f"{c.name} did not stop in {STOP_TIMEOUT:.0f}s; killing")
            c.proc.kill()
            c.proc.wait()
    given_up = [c.name for c in children if c.given_up]
    if given_up:
        say("serve", f"given up on: {', '.join(given_up)}")
    say("serve", f"stopped (exit {code})")
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m holdshort.serve", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", help="comma-separated subset of: " + ",".join(PROCESSES))
    p.add_argument("--db", help="database name passed to every process")
    p.add_argument("--port", type=int, default=None, help="api port (default $PORT or 8000)")
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args(argv)
    only = None
    if args.only:
        only = [n.strip() for n in args.only.split(",") if n.strip()]
        unknown = [n for n in only if n not in PROCESSES]
        if unknown:
            p.error(f"unknown process(es): {', '.join(unknown)}")
    port = args.port if args.port is not None else int(os.environ.get("PORT") or 8000)
    children = build_children(only, args.db, args.host, port)
    if not children:
        say("serve", "nothing to run")
        return 1
    say("serve", f"Phylax Tower: starting {len(children)} process(es) on port {port}")
    return supervise(children)


if __name__ == "__main__":
    sys.exit(main())
