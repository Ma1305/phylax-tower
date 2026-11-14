"""holdshort.llm and the rules-first listener path. No real model calls: the layer is stubbed."""

import importlib
import json
import time

from holdshort import llm

P = importlib.import_module("holdshort.listener.parse")


F = {"tower_id": "LGA", "stream_id": "LGA_GND", "controller_id": "ctl_ground"}


def _fail(*a, **k):
    raise AssertionError("model must not be called")


def test_rules_first_skips_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "vertex")
    monkeypatch.setattr(llm, "structured", _fail)
    tx = P.parse("Truck one, cross runway four at Delta", {"station": "GROUND", "fields": F},
                 llm=P.get_labeller())
    assert tx.kind == "clearance" and tx.instruction == "cross" and tx.to == "TRUCK1"
    assert tx.verified and tx.flag is None


def test_nonstandard_call_uses_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "vertex")
    seen = []

    def fake(task, system, user, schema, timeout_s):
        seen.append((task, timeout_s))
        return schema.model_validate({"intelligible": True, "from": "TRUCK1", "to": "TOWER",
                                      "kind": "request", "instruction": "cross",
                                      "runway": "4", "at": "Delta"})

    monkeypatch.setattr(llm, "structured", fake)
    text = "Tower, uh, Truck one with two more behind, we'd like to get across four at Delta"
    tx = P.parse(text, {"fields": F}, llm=P.get_labeller())
    assert seen == [("parse", 6.0)]
    assert (tx.kind, tx.instruction, tx.to) == ("request", "cross", "TOWER")
    assert tx.runway == P.normalize_runway("4") and tx.at == P.normalize_taxiway("Delta")


def test_model_timeout_falls_back_to_rules_or_unclear(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "vertex")
    monkeypatch.setattr(llm, "structured", lambda *a, **k: None)  # timeout -> None
    tx = P.parse("Tower, uh, Truck one, uh... say again", {"fields": F}, llm=P.get_labeller())
    assert tx.flag == P.FLAG_UNCLEAR and not tx.verified


def test_layer_timeout_returns_none(monkeypatch):
    class Slow:
        def invoke(self, msgs):
            time.sleep(2)

    monkeypatch.setattr(llm, "chat_model", lambda task, timeout_s=60: Slow())
    t0 = time.monotonic()
    assert llm.text("parse", "s", "u", timeout_s=0.2) is None
    assert time.monotonic() - t0 < 1.5


def test_layer_errors_never_raise(monkeypatch):
    class Boom:
        def invoke(self, msgs):
            raise RuntimeError("boom")

        def with_structured_output(self, schema):
            return self

    monkeypatch.setattr(llm, "chat_model", lambda task, timeout_s=60: Boom())
    assert llm.text("audit", "s", "u", 1) is None
    assert llm.structured("audit", "s", "u", P.ParseLabel, 1) is None


def test_provider_none_returns_none():
    assert llm.provider() == "none"  # conftest
    assert llm.text("advisor", "s", "u", 1) is None


def test_status_has_no_secrets(monkeypatch):
    secret = "sk-secret-value-123456"
    monkeypatch.setenv("LLM_PROVIDER", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT", "proj-secret-xyz")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/secret-key-file.json")
    monkeypatch.setenv("LLM_API_KEY", secret)
    monkeypatch.setattr(llm, "_credentials_resolve", lambda: True)
    s = json.dumps(llm.status())
    for bad in (secret, "proj-secret-xyz", "secret-key-file"):
        assert bad not in s
    st = llm.status()
    assert st["provider"] == "vertex" and st["tasks"]["parse"]["model"] == "gemini-4-argon"
    assert st["tasks"]["advisor"]["thinking"] == "medium"
