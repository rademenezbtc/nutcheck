"""Rate limits must never turn into false failures, false passes or lost sats."""
import json

import httpx
import pytest

from nutcheck.cli import audit, cmd_sweep
from nutcheck.client import MintClient
from nutcheck.funding import Funder

from .fake_nwc import FakeRelayWallet

MINT, PROXY = "http://127.0.0.1:3338", "http://127.0.0.1:3339"


def _up():
    try:
        return httpx.get(PROXY + "/__naughty", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="local mint/proxy not running")


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(MintClient, "_sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(Funder, "_sleep", staticmethod(lambda s: None))
    yield
    httpx.post(PROXY + "/__naughty", json={"modes": []})


def _modes(*m):
    httpx.post(PROXY + "/__naughty", json={"modes": list(m)})


def test_bursty_rate_limit_is_absorbed():
    _modes("rate_limit_burst")
    rep, _ = audit(PROXY, fund=64, wait=10)
    assert rep["rate_limited"] > 3, "the proxy should have thrown several 429s"
    assert [r["id"] for r in rep["results"] if r["status"] in ("fail", "error")] == []
    assert rep["grade"] == "A" and not rep["partial"]


def test_hard_rate_limit_is_inconclusive_and_sats_are_stashed_then_recovered(tmp_path, monkeypatch):
    with FakeRelayWallet(7711, proxy_url=PROXY, mint_url=MINT) as w:
        monkeypatch.setenv("NWC_URL", w.uri)
        cfg = {"reports_dir": "reports", "wallet_dir": "wallet", "stash_dir": "stash",
               "mints": [{"url": PROXY, "funded": "nwc", "wait": 10}]}
        (tmp_path / "sc.json").write_text(json.dumps(cfg))

        _modes("await_payment", "rate_limit_hard")
        alerts = tmp_path / "alerts.txt"
        cmd_sweep([str(tmp_path / "sc.json"), "--nwc", "--funding-alerts", str(alerts)])
        rep = json.loads(next((tmp_path / "reports").rglob("*.json")).read_text())
        statuses = {r["id"]: r["status"] for r in rep["results"]}
        assert statuses.get("nutcheck.inconclusive") == "error"
        assert "fail" not in statuses.values(), "a rate limit must not be reported as the mint misbehaving"
        assert rep["partial"]
        assert rep["funding"]["ok"] is False and rep["funding"].get("stashed")
        stash = list((tmp_path / "stash").glob("*.nutstash"))
        assert len(stash) == 1 and "proofs" not in stash[0].read_text(), "stash must be encrypted"
        assert "stashed encrypted" in alerts.read_text()

        # next run: the mint has calmed down; recovery pays the stash back before anything else
        _modes("await_payment")
        cfg["mints"] = []
        (tmp_path / "sc.json").write_text(json.dumps(cfg))
        n_invoices = len(w.invoices)
        cmd_sweep([str(tmp_path / "sc.json"), "--nwc", "--funding-alerts", str(alerts)])
        assert list((tmp_path / "stash").glob("*.nutstash")) == []
        assert len(w.invoices) == n_invoices + 1, "recovery paid the stashed sats back to the wallet"
        assert alerts.read_text() == ""
