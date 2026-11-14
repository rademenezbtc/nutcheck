from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"    # not applicable (e.g. NUT not claimed) or not funded
    ERROR = "error"  # NutCheck itself could not complete the check


class Severity(str, Enum):
    CRITICAL = "critical"  # funds can be lost, forged or stolen
    HIGH = "high"          # privacy break or user funds withheld
    MEDIUM = "medium"      # spec violation with limited impact
    LOW = "low"            # honesty / hygiene
    INFO = "info"


# Free checks first (they cost nothing and run on every mint), then the funded ones.
FREE_CATEGORIES = ["Keys", "Forgery", "Info"]
FUNDED_CATEGORIES = ["DLEQ", "Issuing", "Spending", "Locks", "Payouts"]
CATEGORIES = FREE_CATEGORIES + FUNDED_CATEGORIES

_CATEGORY_RULES = [  # first matching prefix wins
    ("swap.rejects_forgery", "Forgery"), ("dleq.", "DLEQ"),
    ("keys.", "Keys"), ("mint.", "Issuing"), ("swap.", "Spending"), ("checkstate.", "Spending"),
    ("p2pk.", "Locks"), ("htlc.", "Locks"), ("locks.", "Locks"), ("melt.", "Payouts"),
    ("info.", "Info"), ("transport.", "Info"),
]


def category_of(check_id: str) -> str:
    for prefix, cat in _CATEGORY_RULES:
        if check_id.startswith(prefix):
            return cat
    return "Info"


@dataclass
class Result:
    id: str
    title: str
    nut: str
    severity: Severity
    status: Status
    summary: str
    funded: bool = False
    evidence: dict = field(default_factory=dict)

    @property
    def category(self) -> str:
        return category_of(self.id)

    def to_dict(self):
        d = asdict(self)
        d["severity"] = self.severity.value
        d["status"] = self.status.value
        d["category"] = self.category
        return d


# ---- evidence that is safe to publish ------------------------------------------

_PROOF_LISTS = {"inputs", "proofs"}
_OUTPUT_LISTS = {"outputs"}
_SIG_LISTS = {"signatures", "change", "promises"}


def _scrub(obj, depth=0):
    """Replace proofs, outputs and signatures with counts. A published report must never contain a
    secret+C pair: if the mint refused a valid spend, that proof is still live and anyone could take it."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _PROOF_LISTS and isinstance(v, list):
                out[k] = f"[{len(v)} proofs, {sum(p.get('amount', 0) for p in v if isinstance(p, dict))} sat]"
            elif k in _OUTPUT_LISTS and isinstance(v, list):
                out[k] = f"[{len(v)} outputs]"
            elif k in _SIG_LISTS and isinstance(v, list):
                out[k] = f"[{len(v)} signatures]" if v else []
            elif k in ("secret", "C", "witness", "preimage", "payment_preimage"):
                out[k] = "[redacted]"
            else:
                out[k] = _scrub(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_scrub(v, depth + 1) for v in obj[:20]]
    if isinstance(obj, str) and len(obj) > 80:
        return obj[:40] + "…" + obj[-12:]
    return obj


def publishable_exchange(ex) -> dict:
    return {"method": ex.method, "path": ex.path, "status": ex.status,
            "request": _scrub(ex.request), "response": _scrub(ex.response), "ms": ex.ms}
