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


def test_known_issue_note(tmp_path):
    slug = _setup(tmp_path, disclosed=True)
    cfg = json.loads((tmp_path / "scorecard.json").read_text())
    cfg["known_issues"] = [{"check": "p2pk.basic", "software": "nutshell", "text": "Fixed upstream in X.",
                            "url": "https://example.com/pr/1", "link_text": "PR #1"}]
    (tmp_path / "scorecard.json").write_text(json.dumps(cfg))
    out = _build(tmp_path, T0 + timedelta(days=7))
    page = (out / "mints" / f"{slug}.html").read_text()
    assert "Known issue in the mint software." in page and "https://example.com/pr/1" in page
    assert "known upstream issue" in (out / "index.html").read_text()
    feed = json.loads((out / "api" / "mints.json").read_text())["mints"][0]
    assert feed["known_issues"][0]["url"] == "https://example.com/pr/1"
    # no match for other software
    cfg["known_issues"][0]["software"] = "cdk-mintd"
    (tmp_path / "scorecard.json").write_text(json.dumps(cfg))
    out = _build(tmp_path, T0 + timedelta(days=7))
    assert "Known issue" not in (out / "mints" / f"{slug}.html").read_text()


def test_problems_live_in_tooltips_not_under_the_name(tmp_path):
    slug = _setup(tmp_path, disclosed=True)
    out = _build(tmp_path, T0 + timedelta(days=7))
    html = (out / "index.html").read_text()
    row = html[html.index('data-band='):]
    meta = row[row.index('class="small muted" style="margin-top:4px">'):][:200]
    assert "SECRET-DETAIL" not in meta and "Failed" not in meta
    assert 'class="tip"' in html and "Failed: SECRET-DETAIL p2pk.basic" in html
    assert "Keys: all 1 checks passed" in html


def test_known_issue_version_bound():
    from nutcheck.site import known_issue
    cfg = {"known_issues": [{"check": "p2pk.malformed", "software": "nutshell", "below": "0.21.0", "text": "x"}]}
    rep = lambda v: {"mint_info": {"version": v}}
    assert known_issue(cfg, rep("Nutshell/0.20.3"), "p2pk.malformed")
    assert not known_issue(cfg, rep("Nutshell/0.21.0"), "p2pk.malformed")
    assert not known_issue(cfg, rep("Nutshell/0.22.1"), "p2pk.malformed")
    assert not known_issue(cfg, rep("cdk-mintd/0.18.1"), "p2pk.malformed")


def _free_only(slug, url, when, version="Nutshell/0.21.0"):
    r = _report(slug, url, when)
    r["results"].append({"id": "p2pk.malformed", "title": "Malformed lock rejected", "nut": "11", "severity": "medium",
                         "status": "skip", "summary": "not funded", "funded": True, "evidence": {}, "category": "Locks"})
    r["partial"] = True
    r["mint_info"]["version"] = version
    return r


def _feed(tmp_path, reports):
    url, slug = "http://127.0.0.1:3399", "127.0.0.1_3399"
    d = tmp_path / "reports" / slug
    d.mkdir(parents=True)
    for i, mk in enumerate(reports):
        (d / f"{i}.json").write_text(json.dumps(mk(slug, url, T0 + timedelta(hours=6 * i))))
    (tmp_path / "scorecard.json").write_text(json.dumps({"mints": [{"url": url}], "embargo_days": 14}))
    out = _build(tmp_path, T0 + timedelta(days=1))
    return json.loads((out / "api" / "mints.json").read_text())["mints"][0], out


def _funded_b(slug, url, when):
    r = _report(slug, url, when, [("p2pk.malformed", "medium")])
    r["grade"] = "B"
    return r


def test_free_run_keeps_latest_funded_results(tmp_path):
    feed, out = _feed(tmp_path, [_funded_b, _free_only, _free_only])
    assert feed["grade"] == "B" and not feed["partial"]
    assert feed["funded_tested"] == T0.isoformat() and feed["tested"] == (T0 + timedelta(hours=12)).isoformat()
    assert "Grades over the last 3 runs: B B B" in (out / "index.html").read_text()


def test_funded_results_not_carried_across_upgrade(tmp_path):
    upgraded = lambda s, u, w: _free_only(s, u, w, version="Nutshell/0.22.0")
    feed, _ = _feed(tmp_path, [_funded_b, upgraded])
    assert feed["grade"] == "A" and feed["partial"] and feed["funded_tested"] is None


def test_failed_funding_attempt_stops_carry(tmp_path):
    def no_invoice(s, u, w):
        r = _free_only(s, u, w)
        r["results"][-1].update(status="error", summary="Run incomplete: the mint could not create a Lightning invoice")
        return r
    feed, out = _feed(tmp_path, [_funded_b, no_invoice, _free_only])
    assert feed["grade"] == "A" and feed["partial"] and feed["funded_tested"] is None
    assert "Incomplete: the mint could not create a Lightning invoice" in (out / "index.html").read_text()


def test_phrases_say_which_way_a_lock_check_failed():
    from nutcheck.site import phrase_for
    assert phrase_for({"id": "p2pk.sig_all", "severity": "high"}) == "SIG_ALL signatures don't protect the outputs"
    assert phrase_for({"id": "p2pk.sig_all", "severity": "medium"}) == "Refuses valid SIG_ALL spends"
    assert phrase_for({"id": "htlc.refund", "severity": "medium"}) == "HTLC refunds don't work after locktime"
    assert phrase_for({"id": "htlc.hashlock", "severity": "medium"}) == "Refuses valid HTLC spends"
    assert phrase_for({"id": "htlc.hashlock", "severity": "critical"}) == "Ignores HTLC spending conditions"
    assert phrase_for({"id": "mint.quote_lock", "severity": "medium"}) == "Refuses correctly signed locked quotes"
