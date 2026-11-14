"""The only place that talks to a language model.

Provider (LLM_PROVIDER): "vertex" (Gemini on Vertex AI via langchain-google-genai; default
when VERTEX_PROJECT is set), "anthropic" (when LLM_API_KEY is set), or "none" (callers use
their rule/template fallback). LangChain chat models keep LangGraph/LangSmith tracing.

Per-task settings (task = "parse" | "advisor" | "audit"):
  LLM_MODEL_<TASK>     model name (default gemini-4-argon on vertex)
  LLM_THINKING_<TASK>  thinking level: off|minimal|low|medium|high|xhigh

Vertex credentials: GOOGLE_APPLICATION_CREDENTIALS if set, else application default
credentials (Cloud Run service account). Calls never raise into the caller: structured()
and text() return None on timeout, refusal, validation or API failure.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import os
import time
from typing import Any

from pydantic import BaseModel

from holdshort import env  # noqa: F401  (loads .env)
from holdshort.env import redact

log = logging.getLogger("holdshort.llm")

TASKS = ("parse", "advisor", "audit")
VERTEX_DEFAULT_MODEL = "gemini-4-argon"
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
# Defaults measured 2026-11-14 on gemini-4-argon, advisor prompt: "minimal" rejected
# (unsupported), low 3.7 s, medium 3.4 s, high 15.1 s. So parse = low (lowest accepted),
# advisor = medium (highest under ~12 s), audit = high.
DEFAULT_THINKING = {"parse": "low", "advisor": "medium", "audit": "high"}

_POOL = cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")
_MODELS: dict[tuple, Any] = {}


def provider() -> str:
    p = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if p in ("vertex", "anthropic", "none"):
        return p
    if os.environ.get("VERTEX_PROJECT", "").strip():
        return "vertex"
    if os.environ.get("LLM_API_KEY", "").strip():
        return "anthropic"
    return "none"


def settings(task: str) -> dict[str, str]:
    t = task.upper()
    default_model = VERTEX_DEFAULT_MODEL if provider() == "vertex" else ANTHROPIC_DEFAULT_MODEL
    model = (os.environ.get(f"LLM_MODEL_{t}", "").strip()
             or (os.environ.get("LLM_MODEL", "").strip() if provider() == "anthropic" else "")
             or default_model)
    thinking = (os.environ.get(f"LLM_THINKING_{t}", "").strip().lower()
                or DEFAULT_THINKING.get(task, "low"))
    return {"model": model, "thinking": thinking}


_CRED_CACHE: dict[str, bool] = {}


def _credentials_resolve() -> bool:
    p = provider()
    if p in _CRED_CACHE:
        return _CRED_CACHE[p]
    _CRED_CACHE[p] = ok = _credentials_check(p)
    return ok


def _credentials_check(p: str) -> bool:
    if p == "anthropic":
        return bool(os.environ.get("LLM_API_KEY", "").strip())
    if p != "vertex":
        return False
    try:
        import google.auth

        google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return True
    except Exception:  # noqa: BLE001
        return False


def status() -> dict:
    """Provider, models, thinking levels and whether credentials resolve. No secrets."""
    p = provider()
    out: dict[str, Any] = {"provider": p,
                           "tasks": {t: settings(t) for t in TASKS} if p != "none" else {}}
    if p == "vertex":
        out["region"] = os.environ.get("VERTEX_REGION", "").strip() or "global"
        out["project_set"] = bool(os.environ.get("VERTEX_PROJECT", "").strip())
        out["key_file"] = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip())
    out["credentials"] = _credentials_resolve() if p != "none" else False
    return out


def describe() -> str:
    """One line naming the active provider (for startup logs)."""
    p = provider()
    if p == "none":
        return "llm: provider none (rule/template fallbacks only)"
    return "llm: provider " + p + " (" + ", ".join(
        f"{t}={s['model']}/{s['thinking']}" for t, s in
        ((t, settings(t)) for t in TASKS)) + ")"


def chat_model(task: str, timeout_s: float = 60):
    """A LangChain chat model for `task`, or None when the provider is "none"."""
    p = provider()
    s = settings(task)
    # The caller's hard limit is enforced by _run (thread future). The HTTP/server deadline
    # gets headroom: Vertex answers 504 DEADLINE_EXCEEDED well before a tight deadline.
    timeout_s = max(30.0, 2 * timeout_s)
    key = (p, s["model"], s["thinking"], round(timeout_s))
    if key in _MODELS:
        return _MODELS[key]
    if p == "vertex":
        import warnings

        warnings.filterwarnings("ignore", category=SyntaxWarning, module="google.genai")
        from langchain_google_genai import ChatGoogleGenerativeAI

        kw: dict[str, Any] = {}
        if s["thinking"] not in ("", "off", "none", "default"):
            kw["thinking_config"] = {"thinking_level": s["thinking"]}
        m = ChatGoogleGenerativeAI(
            model=s["model"], vertexai=True,
            project=os.environ.get("VERTEX_PROJECT", "").strip() or None,
            location=os.environ.get("VERTEX_REGION", "").strip() or "global",
            timeout=timeout_s, max_retries=0, temperature=0, **kw)
    elif p == "anthropic":
        from langchain_anthropic import ChatAnthropic

        m = ChatAnthropic(model=s["model"], api_key=os.environ.get("LLM_API_KEY", ""),
                          max_tokens=4096, timeout=timeout_s, max_retries=0)
    else:
        return None
    _MODELS[key] = m
    return m


def _run(task: str, fn, timeout_s: float):
    t0 = time.monotonic()
    fut = _POOL.submit(fn)
    try:
        out = fut.result(timeout=timeout_s)
    except cf.TimeoutError:
        log.warning("llm %s: timed out after %.1fs", task, timeout_s)
        return None
    except Exception as e:  # noqa: BLE001 - never raise into the caller
        log.warning("llm %s failed: %s", task, redact(f"{type(e).__name__}: {e}")[:300])
        return None
    log.info("llm %s: %.2fs", task, time.monotonic() - t0)
    return out


def structured(task: str, system: str, user: str, schema: type[BaseModel],
               timeout_s: float) -> BaseModel | None:
    model = chat_model(task, timeout_s)
    if model is None:
        return None

    def call():
        runner = model.with_structured_output(schema)
        out = runner.invoke([("system", system), ("human", user)])
        if isinstance(out, dict):
            out = schema.model_validate(out)
        return out if isinstance(out, schema) else None

    return _run(task, call, timeout_s)


def text(task: str, system: str, user: str, timeout_s: float) -> str | None:
    model = chat_model(task, timeout_s)
    if model is None:
        return None

    def call():
        msg = model.invoke([("system", system), ("human", user)])
        c = msg.content
        if isinstance(c, list):
            c = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
        return (c or "").strip() or None

    return _run(task, call, timeout_s)
