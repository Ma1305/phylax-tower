"""A small control page: one button per scenario, a Stop button, and what happened.

    python -m hssim panel --api http://localhost:8000      # then open http://localhost:8200

It runs the scenarios in this process against the tower, one at a time. Starting and
stopping needs a token that only this page has (it is served from here, so it can carry it;
another web page cannot read it), so nothing else in the browser can start an incident.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import secrets
import time
from collections import deque
from dataclasses import asdict
from typing import Any

from hssim.scenarios import SCENARIOS

log = logging.getLogger("hssim")

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Phylax Tower simulator</title>
<style>
  :root { --bg:#0e1116; --panel:#171b22; --line:#2a303b; --text:#e6e9ee; --dim:#98a2b3;
          --go:#2f81f7; --stop:#e5534b; --ok:#3fb950; --bad:#e5534b; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
  main { max-width: 980px; margin: 0 auto; padding: 20px 16px 40px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--dim); margin-bottom: 18px; }
  .sub a { color: var(--go); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
          padding:14px 16px; margin-bottom:10px; display:flex; gap:14px; align-items:center; }
  .card h2 { font-size:16px; margin:0 0 3px; }
  .card p { margin:0; color:var(--dim); font-size:13.5px; }
  .tag { font-size:11px; text-transform:uppercase; letter-spacing:.06em; padding:1px 7px;
         border-radius:999px; border:1px solid var(--line); color:var(--dim); margin-left:8px; }
  .tag.incident { color:#f0883e; border-color:#5a3a1d; }
  .grow { flex:1; min-width:0; }
  button { font:inherit; font-weight:600; color:#fff; background:var(--go); border:0;
           border-radius:8px; padding:9px 18px; cursor:pointer; white-space:nowrap; }
  button:disabled { opacity:.4; cursor:default; }
  button.stop { background:var(--stop); }
  #status { position:sticky; top:0; background:var(--bg); padding:10px 0 12px; z-index:2;
            display:flex; gap:12px; align-items:center; border-bottom:1px solid var(--line);
            margin-bottom:14px; }
  #state { font-weight:600; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--dim); flex:none; }
  .dot.on { background:var(--ok); box-shadow:0 0 0 4px rgba(63,185,80,.18); }
  pre { background:#0a0d12; border:1px solid var(--line); border-radius:10px; padding:12px;
        overflow:auto; font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
        max-height:320px; white-space:pre-wrap; word-break:break-word; }
  h3 { font-size:13px; color:var(--dim); text-transform:uppercase; letter-spacing:.06em;
       margin:18px 0 6px; }
  .result { font-weight:600; }
  .result.ok { color:var(--ok); } .result.bad { color:var(--bad); }
  @media (max-width:640px){ .card{flex-direction:column; align-items:stretch;} }
</style></head><body><main>
<h1>Phylax Tower simulator</h1>
<div class="sub">Pick a simulation, then watch the tower react in the
  <a id="viz" target="_blank" rel="noopener">visualizer</a>. One runs at a time.</div>
<div id="status"><span class="dot" id="dot"></span><span id="state">Idle</span>
  <span class="grow"></span><button class="stop" id="stop" disabled>Stop</button></div>
<div id="cards"></div>
<h3>Last result</h3><div id="result" class="result">Nothing has run yet.</div>
<pre id="outcome" hidden></pre>
<h3>What the simulator is doing</h3><pre id="log">waiting…</pre>
</main><script>
const TOKEN = __TOKEN__, VIZ = __VIZ__;
document.getElementById('viz').href = VIZ;
const post = (path) => fetch(path, {method:'POST', headers:{'X-Sim-Token': TOKEN}})
  .then(async r => { if (!r.ok) alert((await r.json()).detail || r.statusText); });
let built = false;
function build(list) {
  const root = document.getElementById('cards');
  for (const s of list) {
    const card = document.createElement('div'); card.className = 'card';
    const body = document.createElement('div'); body.className = 'grow';
    const h = document.createElement('h2'); h.textContent = s.name.replace('_', ' ');
    const tag = document.createElement('span'); tag.className = 'tag ' + s.kind;
    tag.textContent = s.kind; h.appendChild(tag);
    const p = document.createElement('p'); p.textContent = s.summary;
    const e = document.createElement('p'); e.textContent = 'Expect: ' + s.expect;
    body.append(h, p, e);
    const b = document.createElement('button'); b.textContent = 'Start'; b.dataset.run = s.name;
    b.onclick = () => post('/run/' + s.name);
    card.append(body, b); root.appendChild(card);
  }
  built = true;
}
async function tick() {
  try {
    const s = await (await fetch('/status')).json();
    if (!built) build(s.scenarios);
    document.getElementById('dot').className = 'dot' + (s.running ? ' on' : '');
    document.getElementById('state').textContent = s.running
      ? `Running: ${s.scenario.replace('_',' ')} (${Math.round(s.seconds)} s)` : 'Idle';
    document.getElementById('stop').disabled = !s.running;
    for (const b of document.querySelectorAll('button[data-run]')) b.disabled = s.running;
    const log = document.getElementById('log');
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    log.textContent = s.log.join('\\n') || 'waiting…';
    if (atEnd) log.scrollTop = log.scrollHeight;
    if (s.last) {
      const r = document.getElementById('result'), ok = s.last.ok;
      r.className = 'result ' + (ok ? 'ok' : 'bad');
      r.textContent = `${s.last.scenario.replace('_',' ')}: ${s.last.headline}`;
      const o = document.getElementById('outcome'); o.hidden = false;
      o.textContent = JSON.stringify(s.last.result, null, 2);
    }
  } catch (e) { document.getElementById('state').textContent = 'Simulator not answering'; }
}
document.getElementById('stop').onclick = () => post('/stop');
tick(); setInterval(tick, 1000);
</script></body></html>
"""


class _Tail(logging.Handler):
    def __init__(self, lines: deque[str]):
        super().__init__(level=logging.INFO)
        self.lines = lines
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def headline(result: dict[str, Any]) -> str:
    """One line a person can read for the outcome of a run."""
    outcome = str(result.get("outcome") or ("no stop was needed" if result.get("ok", True)
                                            else "ended in the conflict"))
    text = outcome.replace("_", " ")
    if result.get("stop_heard_after_s") is not None:
        text += f" (stop heard {result['stop_heard_after_s']} s after the clearance"
        if result.get("stop_needed_within_s") is not None:
            text += f", needed within {result['stop_needed_within_s']} s"
        text += ")"
    elif result.get("stop_latency_s") is not None:
        text += f" (stop heard {result['stop_latency_s']} s after the clearance)"
    return text


def create_panel_app(args: argparse.Namespace, token: str | None = None) -> Any:
    from urllib.parse import urlsplit

    from fastapi import FastAPI, Header, HTTPException
    from fastapi.responses import HTMLResponse
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    from hssim.cli import run_scenario

    token = token or secrets.token_urlsafe(24)
    lines: deque[str] = deque(maxlen=160)
    logging.getLogger("hssim").addHandler(_Tail(lines))
    state: dict[str, Any] = {"task": None, "scenario": None, "started": 0.0, "last": None}
    app = FastAPI(title="Phylax Tower simulator")
    app.state.sim_state, app.state.token = state, token
    # Only answer requests addressed to this machine by name. Without this, a web site
    # whose hostname is re-pointed at 127.0.0.1 (DNS rebinding) could load this page and
    # read the token out of it.
    local = {"localhost", "127.0.0.1", "[::1]", str(getattr(args, "control_host", "127.0.0.1"))}
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(local))

    def check(given: str | None, origin: str | None = None) -> None:
        if origin and (urlsplit(origin).hostname or "") not in {h.strip("[]") for h in local}:
            raise HTTPException(403, "requests from other sites are not accepted")
        if not given or not hmac.compare_digest(given, token):
            raise HTTPException(403, "missing or wrong X-Sim-Token (use the buttons on this "
                                     "page)")

    async def run_one(name: str) -> None:
        one = argparse.Namespace(**{**vars(args), "scenario": name, "loop": False})
        try:
            outcome = await run_scenario(one)
            result = outcome.result
            state["last"] = {"scenario": name, "ok": bool(result.get("ok", True)),
                             "headline": headline(result), "result": asdict(outcome)}
            log.info("finished %s: %s", name, headline(result))
        except asyncio.CancelledError:
            state["last"] = {"scenario": name, "ok": True, "headline": "stopped by you",
                             "result": {}}
            log.info("stopped %s", name)
            raise
        except BaseException as e:  # SystemExit from an unreachable tower included
            state["last"] = {"scenario": name, "ok": False,
                             "headline": f"could not run: {e}", "result": {}}
            log.error("could not run %s: %s", name, e)
        finally:
            state["task"] = None

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return (PAGE.replace("__TOKEN__", json.dumps(token))
                .replace("__VIZ__", json.dumps(args.visualizer_url)))

    @app.get("/status")
    def status() -> dict[str, Any]:
        running = state["task"] is not None
        return {"running": running, "scenario": state["scenario"] if running else None,
                "seconds": time.monotonic() - state["started"] if running else 0,
                "log": list(lines)[-60:], "last": state["last"],
                "scenarios": [{"name": s.name, "kind": s.kind, "summary": s.summary,
                               "expect": s.expect} for s in SCENARIOS.values()]}

    @app.post("/run/{name}")
    async def run(name: str, x_sim_token: str | None = Header(default=None),
                  origin: str | None = Header(default=None)) -> dict[str, Any]:
        check(x_sim_token, origin)
        if name not in SCENARIOS:
            raise HTTPException(404, f"unknown scenario {name!r}")
        if state["task"] is not None:
            raise HTTPException(409, f"{state['scenario']} is still running; stop it first")
        lines.clear()
        state["scenario"], state["started"] = name, time.monotonic()
        state["task"] = asyncio.ensure_future(run_one(name))
        return {"started": name}

    @app.post("/stop")
    async def stop(x_sim_token: str | None = Header(default=None),
                   origin: str | None = Header(default=None)) -> dict[str, Any]:
        check(x_sim_token, origin)
        task = state["task"]
        if task is None:
            return {"stopped": False}
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return {"stopped": True}

    return app


async def serve_panel(args: argparse.Namespace) -> None:
    import uvicorn

    app = create_panel_app(args)
    server = uvicorn.Server(uvicorn.Config(app, host=args.control_host, port=args.control_port,
                                           log_level="warning"))
    log.info("simulator page: http://%s:%d   (tower: %s)", args.control_host,
             args.control_port, "built-in stub" if args.stub else args.api)
    try:
        await server.serve()
    finally:
        task = app.state.sim_state["task"]
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
