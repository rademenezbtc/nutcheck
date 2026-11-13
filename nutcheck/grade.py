"""SSL-Labs-style grading: the worst failure caps the grade."""
from __future__ import annotations

from .model import Result, Severity as S, Status as St

CAPS = {S.CRITICAL: "F", S.HIGH: "C", S.MEDIUM: "B", S.LOW: "A-", S.INFO: "A"}
ORDER = ["A+", "A", "A-", "B", "C", "D", "F"]


def grade(results: list[Result]) -> dict:
    g = "A"
    reasons = []
    for r in results:
        cap = None
        if r.status == St.FAIL:
            cap = CAPS[r.severity]
        elif r.status == St.WARN and r.severity != S.INFO:
            cap = "A-"
        if cap and ORDER.index(cap) > ORDER.index(g):
            g = cap
        if cap and cap != "A":
            reasons.append(f"{r.title} ({r.severity.value}) → capped at {cap}")
    funded = any(r.funded and r.status in (St.PASS, St.FAIL) for r in results)
    errors = [r.id for r in results if r.status == St.ERROR]
    return {
        "grade": g,
        "partial": not funded or bool(errors),
        "reasons": reasons,
        "counts": {s.value: sum(1 for r in results if r.status == s) for s in St},
        "errors": errors,
    }
