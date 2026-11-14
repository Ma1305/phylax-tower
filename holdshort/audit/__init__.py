"""Audit agent: deterministic timelines and grounded explanations over the v2 log (read-only).

    from holdshort.audit import timeline, explain
    timeline(db, "LGA", entity="TRUCK1")        # list of timeline items, time order
    explain(db, "Why was TRUCK1 held?")         # {"timeline", "narrative", "cited", ...}

This package never writes to MongoDB: it only runs find / aggregate / distinct.
"""

from holdshort.audit.core import KINDS, answer, explain, parse_question, timeline

__all__ = ["KINDS", "answer", "explain", "parse_question", "timeline"]
