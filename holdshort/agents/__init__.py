"""Agent processes. Agents never act: each one only inserts intents into intents_<agent>.

- safety_agent:     transmissions -> rule engine -> stop intents
- procedures_agent: executed crossing stop -> propose_instruction (needs a controller);
                    also ``rogue()`` for the demo's misbehaving-agent beat
- weather_agent:    METAR below 1 sm -> set_rule_set low_vis_v1
"""


def rogue(db):
    """API hook (``POST /rogue``): see holdshort.agents.procedures_agent.rogue."""
    from holdshort.agents.procedures_agent import rogue as _rogue

    return _rogue(db)
