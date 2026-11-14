"""Weather agent: ask the gate for the rule set the visibility calls for.

    uv run python -m holdshort.agents.weather_agent [--db NAME]

It watches ``weather`` (written through /ingest/weather). Visibility under ``LOW_VIS_SM``
while low_vis_v1 is not active -> a ``set_rule_set`` intent with proposal_text
"low_vis_v1"; visibility back to ``NORMAL_VIS_SM`` or more while low_vis_v1 is active ->
"normal_v1". Between the two thresholds nothing changes (hysteresis). The same request is
not repeated while an earlier one for the same rule set is less than ``REPEAT_S`` old.
Every read and write goes through ``scoped_db(db, "weather")``; the gate decides.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from typing import Any

from pymongo import DESCENDING

from holdshort.access import ScopedDb, scoped_db
from holdshort.agents._stream import StreamAgent, main, now
from holdshort.contracts import TOWER_ID, Intent, Weather, to_mongo

LOW_VIS_SM = 1.0
NORMAL_VIS_SM = 3.0
LOW_VIS_RULE_SET = "low_vis_v1"
NORMAL_RULE_SET = "normal_v1"
REPEAT_S = 120


def wanted_rule_set(visibility_sm: float, active_version: str | None) -> str | None:
    """The rule set to ask for, or None if nothing should change."""
    if visibility_sm < LOW_VIS_SM and active_version != LOW_VIS_RULE_SET:
        return LOW_VIS_RULE_SET
    if visibility_sm >= NORMAL_VIS_SM and active_version == LOW_VIS_RULE_SET:
        return NORMAL_RULE_SET
    return None


def rule_set_intent(wx: Weather, active_version: str | None) -> Intent | None:
    version = wanted_rule_set(wx.visibility_sm, active_version)
    if version is None:
        return None
    if version == LOW_VIS_RULE_SET:
        reason = f"visibility {wx.visibility_sm:g} sm below {LOW_VIS_SM:g} sm"
        detail = (f"Visibility is {wx.visibility_sm:g} sm (wind {wx.wind or 'n/a'}): switch to "
                  f"low-visibility rules, one movement per runway.")
    else:
        reason = f"visibility {wx.visibility_sm:g} sm back to {NORMAL_VIS_SM:g} sm or more"
        detail = f"Visibility is back to {wx.visibility_sm:g} sm: return to normal rules."
    return Intent(t=now(), tower_id=wx.tower_id or TOWER_ID, agent="weather",
                  action="set_rule_set", severity="caution", reason=reason,
                  proposal_text=version, detail=detail)


def active_version(sdb: ScopedDb) -> str | None:
    doc = sdb.rules.find_one({"active": True}, sort=[("version", 1)])
    return doc["version"] if doc else None


class WeatherAgent(StreamAgent):
    name = "weather_agent"
    collection = "weather"

    def __init__(self, db, max_await_ms: int = 300):
        super().__init__(db, max_await_ms)
        self.sdb = scoped_db(db, "weather")

    def handle(self, doc: dict[str, Any]) -> Intent | None:
        wx = Weather.model_validate(doc)
        intent = rule_set_intent(wx, active_version(self.sdb))
        if intent is None:
            return None
        recent = self.sdb.intents_weather.find_one(
            {"action": "set_rule_set", "tower_id": intent.tower_id,
             "t": {"$gte": now() - timedelta(seconds=REPEAT_S)}},
            sort=[("t", DESCENDING)])
        if recent and recent.get("proposal_text") == intent.proposal_text:
            return None
        iid = self.sdb.intents_weather.insert_one(to_mongo(intent)).inserted_id
        self.log.info("intent %s: set_rule_set %s (%s)", iid, intent.proposal_text, intent.reason)
        return intent


if __name__ == "__main__":
    sys.exit(main(WeatherAgent))
