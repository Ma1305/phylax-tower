"""Audit agent: deterministic timelines and grounded explanations (read-only).

    from holdshort.audit import timeline, explain
    timeline("TRUCK1")                       # list of timeline items, scenario order
    explain("Why was TRUCK1 held?")          # {"timeline": [...], "narrative": "..."}

This package never writes to MongoDB: it only runs find/aggregate/distinct.
"""

from holdshort.audit.core import KINDS, answer, explain, timeline

__all__ = ["KINDS", "answer", "explain", "timeline"]
