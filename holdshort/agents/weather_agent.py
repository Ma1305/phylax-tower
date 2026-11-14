"""Weather agent: METAR below 1 sm visibility -> ask the gate for the low-visibility rule set.

    uv run python -m holdshort.agents.weather_agent [--db NAME]

It watches the metar collection (written by the scenario runner). When visibility
drops below ``LOW_VIS_SM`` and low_vis_v1 is not already active, it inserts a
``set_rule_set`` intent (proposal_text "low_vis_v1") into intents_weather. The gate
decides; if the weather tier allows it the gate activates the rule set.
"""

from __future__ import annotations

import sys
from typing import Any

from pymongo.database import Database

from holdshort.agents._stream import StreamAgent, main
from holdshort.contracts import Intent, Metar, to_mongo

LOW_VIS_SM = 1.0
LOW_VIS_RULE_SET = "low_vis_v1"


def low_vis_intent(metar: Metar, active_version: str | None) -> Intent | None:
    """The set_rule_set intent for this observation, or None if nothing should change."""
    if metar.visibility_sm >= LOW_VIS_SM or active_version == LOW_VIS_RULE_SET:
        return None
    return Intent(
        t=metar.t,
        agent="weather",
        action="set_rule_set",
        reason=f"visibility {metar.visibility_sm:g} sm below {LOW_VIS_SM:g} sm "
        f"(wind {metar.wind})",
        proposal_text=LOW_VIS_RULE_SET,
    )


def _active_version(db: Database) -> str | None:
    doc = db.rules.find_one({"active": True}, sort=[("version", 1)])
    return doc["version"] if doc else None


class WeatherAgent(StreamAgent):
    name = "weather"
    collection = "metar"

    def handle(self, doc: dict[str, Any]) -> Intent | None:
        metar = Metar.model_validate(doc)
        intent = low_vis_intent(metar, _active_version(self.db))
        if intent is not None:
            iid = self.db.intents_weather.insert_one(to_mongo(intent)).inserted_id
            self.log.info("intent %s: set_rule_set %s (%s)", iid, intent.proposal_text,
                          intent.reason)
        return intent


if __name__ == "__main__":
    sys.exit(main(WeatherAgent))
