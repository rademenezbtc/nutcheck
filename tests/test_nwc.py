"""Funding loop end to end: NutCheck pays a mint's funding invoice over NWC, runs the funded checks,
and pays the leftover back. Needs the local FakeWallet mint (:3338) and naughty proxy (:3339)."""
import json

import httpx
import pytest

from nutcheck import nwc
from nutcheck.cli import cmd_sweep

from .fake_nwc import FakeRelayWallet

MINT, PROXY = "http://127.0.0.1:3338", "http://127.0.0.1:3339"


def _up():
    try:
        return httpx.get(PROXY + "/__naughty", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="local mint/proxy not running")


@pytest.fixture(autouse=True)
def await_payment():
    httpx.post(PROXY + "/__naughty", json={"modes": ["await_payment"]})
    yield
    httpx.post(PROXY + "/__naughty", json={"modes": []})


def _sweep(tmp_path, monkeypatch, wallet, mints, cap=1000):
    cfg = {"reports_dir": "reports", "wallet_dir": "wallet", "nwc_max_sat_per_run": cap, "mints": mints}
    (tmp_path / "sc.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("NWC_URL", wallet.uri)
    alerts = tmp_path / "funding-alerts.txt"
    assert cmd_sweep([str(tmp_path / "sc.json"), "--nwc", "--funding-alerts", str(alerts)]) == 0
    reports = sorted((tmp_path / "reports").rglob("*.json"))
    return [json.loads(p.read_text()) for p in reports], alerts.read_text()


def test_uri_parsing_never_exposes_secret():
    w, relays, sec = nwc.parse_uri("nostr+walletconnect://" + "ab" * 32 + "?relay=wss://r.example&secret=" + "11" * 32)
    assert w == "ab" * 32 and relays == ["wss://r.example"] and sec == int("11" * 32, 16)
    client = nwc.NWC("nostr+walletconnect://" + "ab" * 32 + "?relay=wss://r.example&secret=" + "11" * 32)
    assert "11" * 8 not in repr(client)


@pytest.mark.parametrize("encryption", ["nip44_v2 nip04", None])  # None = legacy wallet, NIP-04 only
def test_fund_check_and_pay_back(tmp_path, monkeypatch, encryption):
    with FakeRelayWallet(7701 if encryption else 7702, proxy_url=PROXY, mint_url=MINT, encryption=encryption) as w:
        (rep,), alerts = _sweep(tmp_path, monkeypatch, w, [{"url": PROXY, "funded": "nwc", "wait": 20}])
    assert [amt for _, amt in w.paid] == [64], "exactly one 64-sat funding invoice paid through NWC"
    assert rep["grade"] == "A" and not rep["partial"], rep["reasons"]
    f = rep["funding"]
    assert f["ok"] and f["returned"] > 0 and f["returned"] == w.invoices[0][1]
    assert f["dust"] <= 4, f
    assert alerts == ""


def test_payback_failure_stops_further_funding(tmp_path, monkeypatch):
    with FakeRelayWallet(7703, proxy_url=PROXY, mint_url=MINT, fail_make=True) as w:
        reps, alerts = _sweep(tmp_path, monkeypatch, w, [
            {"url": PROXY, "funded": "nwc", "wait": 20},
            {"url": "http://127.0.0.1:3338", "funded": "nwc", "wait": 20},
        ])
    by = {r["mint"]: r for r in reps}
    assert by[PROXY]["funding"]["ok"] is False
    assert len(w.paid) == 1, "second mint must not be funded after a failed pay-back"
    assert by["http://127.0.0.1:3338"]["partial"] is True
    assert "Funding:" in alerts and "pay-back" in alerts


def test_per_run_cap(tmp_path, monkeypatch):
    with FakeRelayWallet(7704, proxy_url=PROXY, mint_url=MINT) as w:
        (rep,), alerts = _sweep(tmp_path, monkeypatch, w, [{"url": PROXY, "funded": "nwc", "wait": 20}], cap=50)
    assert w.paid == []
    assert rep["partial"] and any(r["id"] == "mint.funded" and r["status"] == "skip" for r in rep["results"])


def test_wallet_refuses_payment(tmp_path, monkeypatch):
    with FakeRelayWallet(7705, proxy_url=PROXY, mint_url=MINT, fail_pay=True) as w:
        (rep,), alerts = _sweep(tmp_path, monkeypatch, w, [{"url": PROXY, "funded": "nwc", "wait": 20}])
    assert rep["partial"]
    assert "could not pay a funding invoice" in alerts
