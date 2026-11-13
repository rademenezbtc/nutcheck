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

    def to_dict(self):
        d = asdict(self)
        d["severity"] = self.severity.value
        d["status"] = self.status.value
        return d
