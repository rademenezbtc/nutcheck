"""Scorecard generator: embargo, redaction and the safety of published evidence."""
import json
from datetime import datetime, timedelta, timezone

from nutcheck.client import Exchange
from nutcheck.model import publishable_exchange
from nutcheck.site import cmd_site

T0 = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)


def _report(slug, url, when, fails=(), reachable=True):
    results = [{"id": "keys.keyset_id", "title": "Keyset IDs match their keys", "nut": "02", "severity": "high",
                "status": "pass", "summary": "ok", "funded": False, "evidence": {}, "category": "Keys"}]
    for fid, sev in fails:
        results.append({"id": fid, "title": fid, "nut": "11", "severity": sev, "status": "fail",
                        "summary": f"SECRET-DETAIL {fid}", "funded": True, "category": "Locks",
                        "evidence": {"exchange": {"method": "POST", "path": "/v1/swap", "status": 200,
                                                  "request": {}, "response": {}, "ms": 1}}})
    grade = "F" if any(s == "critical" for _, s in fails) else "A"
    return {"nutcheck": "0.1.0", "mint": url, "slug": slug, "started": when.isoformat(), "duration_s": 1.0,
            "reachable": reachable, "grade": grade, "partial": False, "reasons": [], "counts": {}, "errors": [],
            "spent_sat": 11, "requests": 39,
            "mint_info": {"name": "Test", "version": "Nutshell/0.21.0", "nuts": [], "keysets": []},
            "results": results}


def _setup(tmp_path, disclosed=False):
    url = "http://127.0.0.1:3399"
    slug = "127.0.0.1_3399"
    d = tmp_path / "reports" / slug
    d.mkdir(parents=True)
    # clean for two runs, then a critical lock failure from day 3 onwards
    for i in range(6):
        fails = [("p2pk.basic", "critical")] if i >= 2 else []
        (d / f"{i}.json").write_text(json.dumps(_report(slug, url, T0 + timedelta(days=i), fails)))
    cfg = {"mints": [{"url": url, "disclosed": disclosed}], "embargo_days": 14}
    (tmp_path / "scorecard.json").write_text(json.dumps(cfg))
    return slug


def _build(tmp_path, now):
    out = tmp_path / "site"
    assert cmd_site([str(tmp_path / "scorecard.json"), "-o", str(out), "--now", now.isoformat()]) == 0
    return out


def test_details_withheld_inside_window(tmp_path):
    slug = _setup(tmp_path)
    out = _build(tmp_path, T0 + timedelta(days=7))  # first seen day 2 → published day 16
    everything = "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    assert "SECRET-DETAIL" not in everything
    assert "p2pk.basic" not in everything
    assert "17 Nov" in (out / "index.html").read_text()  # 3 Nov + 14 days
    assert "under review" in (out / "badge" / f"{slug}.svg").read_text()
    feed = json.loads((out / "api" / "mints.json").read_text())["mints"][0]
    assert feed["withheld"] and feed["grade"] == "F" and "failed" not in feed


def test_details_published_after_window(tmp_path):
    slug = _setup(tmp_path)
    out = _build(tmp_path, T0 + timedelta(days=20))
    page = (out / "mints" / f"{slug}.html").read_text()
    assert "SECRET-DETAIL p2pk.basic" in page
    assert json.loads((out / "api" / "mints.json").read_text())["mints"][0]["failed"] == ["p2pk.basic"]


def test_disclosed_flag_publishes_early(tmp_path):
    slug = _setup(tmp_path, disclosed=True)
    out = _build(tmp_path, T0 + timedelta(days=7))
    assert "SECRET-DETAIL" in (out / "mints" / f"{slug}.html").read_text()


def test_published_exchange_never_contains_spendable_proofs():
    ex = Exchange("POST", "/v1/swap", 400,
                  {"inputs": [{"amount": 2, "id": "01ab", "secret": "s3cr3t", "C": "02cc", "witness": "{}"}],
                   "outputs": [{"amount": 1, "id": "01ab", "B_": "02bb"}]},
                  {"detail": "witness invalid", "code": 10001}, 5)
    pub = json.dumps(publishable_exchange(ex))
    assert "s3cr3t" not in pub and "02cc" not in pub and "02bb" not in pub
    assert "[1 proofs, 2 sat]" in pub


def test_alert_until_operator_notified(tmp_path):
    _setup(tmp_path)
    alerts = tmp_path / "alerts.txt"
    out = tmp_path / "site"
    cmd_site([str(tmp_path / "scorecard.json"), "-o", str(out), "--now", (T0 + timedelta(days=7)).isoformat(),
              "--alerts", str(alerts)])
    assert "Notify the operator" in alerts.read_text()
    assert ", reported to the operator" not in (out / "index.html").read_text()
    cfg = json.loads((tmp_path / "scorecard.json").read_text())
    cfg["mints"][0]["notified"] = "4 Nov"
    (tmp_path / "scorecard.json").write_text(json.dumps(cfg))
    cmd_site([str(tmp_path / "scorecard.json"), "-o", str(out), "--now", (T0 + timedelta(days=7)).isoformat(),
              "--alerts", str(alerts)])
    assert alerts.read_text() == ""
    assert ", reported to the operator" in (out / "index.html").read_text()
