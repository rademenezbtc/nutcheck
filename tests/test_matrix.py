"""End-to-end: every naughty-proxy fault must be caught, and the honest mint must get an A.

Needs a FakeWallet mint on :3338 and proxy/naughty.py on :3339 (see README). Skipped otherwise.
"""
import httpx
import pytest

from nutcheck import checks_free, checks_funded
from nutcheck.client import MintClient
from nutcheck.grade import grade
from nutcheck.wallet import Wallet

PROXY = "http://127.0.0.1:3339"

EXPECT = {  # mode -> (checks that must fail, worst acceptable grade)
    "none": (set(), "A"),
    "bad_dleq": ({"dleq.mint", "dleq.swap"}, "F"),
    "no_dleq": ({"dleq.mint", "dleq.swap"}, "B"),
    "keyset_tamper": ({"keys.keyset_id"}, "F"),
    "fee_lie": ({"keys.keyset_id", "swap.fee_exact"}, "C"),
    "short_mint": ({"mint.issue"}, "F"),
    "quote_replay": ({"mint.quote_replay"}, "C"),
    "overspend": ({"swap.no_overspend"}, "F"),
    "double_spend": ({"swap.double_spend", "swap.duplicate_input"}, "F"),
    "forgery": ({"swap.rejects_forgery"}, "F"),
    "lock_bypass": ({"p2pk.basic", "p2pk.multisig", "p2pk.locktime", "p2pk.malformed", "p2pk.sig_all",
                     "htlc.hashlock", "htlc.receiver_sig", "htlc.locktime"}, "F"),
    "sigall_ignore": ({"p2pk.sig_all"}, "C"),
    "htlc_no_refund": ({"htlc.refund"}, "B"),
    "quote_lock_ignore": ({"mint.quote_lock"}, "C"),
    "ws_dead": ({"ws.subscribe"}, "A-"),
    "ws_silent": ({"ws.subscribe"}, "A-"),
    "sigall_legacy": (set(), "A-"),  # a warning: the lock holds, but only with the old message
    "keep_change": ({"melt.fee_return"}, "C"),
    "info_lie": ({"info.claims_answer"}, "B"),
}


def _up():
    try:
        return httpx.get(PROXY + "/__naughty", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _up(), reason="naughty proxy not running on :3339")
@pytest.mark.parametrize("mode", list(EXPECT))
def test_fault_is_caught(mode):
    httpx.post(PROXY + "/__naughty", json={"modes": [] if mode == "none" else [mode]})
    try:
        c = MintClient(PROXY)
        w = Wallet(c)
        res, info = checks_free.run(c, w)
        res += checks_funded.run(c, w, info, 64, 10, lambda *a: None)
    finally:
        httpx.post(PROXY + "/__naughty", json={"modes": []})
    failed = {r.id for r in res if r.status.value == "fail"}
    must_fail, g = EXPECT[mode]
    assert must_fail <= failed, f"missed: {must_fail - failed}"
    if mode == "none":
        assert not failed
    assert grade(res)["grade"] == g
