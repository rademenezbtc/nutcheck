"""NIP-87 publishing: key formats, review events, and only re-sending what changed."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from nutcheck import nostr, publish
from nutcheck.site import build_views
from .test_site import T0, _free_only, _funded_b, _report

# NIP-19 test vectors
NPUB = "npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg"
NPUB_HEX = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
NSEC = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"
NSEC_HEX = "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa"
KEY = 0x1D2C3B4A5F6E7D8C9BAAB9C8D7E6F5041322314F5E6D7C8B9AA9B8C7D6E5F403


def test_nip19_vectors():
    assert publish.npub(NPUB_HEX) == NPUB
    assert publish.bech32_decode(NSEC) == ("nsec", bytes.fromhex(NSEC_HEX))
    assert publish.load_seckey(NSEC) == publish.load_seckey(NSEC_HEX) == int(NSEC_HEX, 16)
    with pytest.raises(ValueError):
        publish.load_seckey(NSEC[:-1] + "q")


def _views(tmp_path, makers):
    url, slug = "http://127.0.0.1:3399", "127.0.0.1_3399"
    d = tmp_path / "reports" / slug
    d.mkdir(parents=True)
    for i, mk in enumerate(makers):
        r = mk(slug, url, T0 + timedelta(hours=6 * i))
        r["mint_info"]["pubkey"] = "02" + "ab" * 32
        (d / f"{i}.json").write_text(json.dumps(r))
    cfg = {"mints": [{"url": url}], "base_url": "https://nutcheck.org/", "reports_dir": "reports"}
    return cfg, build_views(cfg, tmp_path, T0 + timedelta(days=1), 14)


def test_review_event(tmp_path):
    cfg, (v,) = _views(tmp_path, [_funded_b, _free_only])
    ev = publish.review(v, cfg, KEY)
    assert nostr.verify_event(ev) and ev["kind"] == 38000
    tags = {t[0]: t[1:] for t in ev["tags"]}
    assert tags["d"] == ["02" + "ab" * 32] and tags["k"] == ["38172"] and tags["u"] == [v.url, "cashu"]
    assert tags["rating"] == ["4"] and tags["grade"] == ["B"]
    assert ev["content"].startswith("[4/5] NutCheck grade B. Accepts P2PK locks with duplicated tags")  # cashu.me's "[n/5] text" format
    assert ev["content"].endswith("https://nutcheck.org/mints/127.0.0.1_3399.html")


def test_partial_run_never_gets_full_marks(tmp_path):
    cfg, (v,) = _views(tmp_path, [lambda s, u, w: _report(s, u, w)])
    v.latest["partial"] = True
    assert publish.rating_for(v.latest) == 4


def test_only_changed_reviews_are_resent(tmp_path):
    from .fake_nwc import FakeRelayWallet
    cfg, (v,) = _views(tmp_path, [_funded_b])
    pub = nostr.xonly_pub(KEY)
    t = int(datetime.now(timezone.utc).timestamp())
    first = [publish.profile(cfg, KEY, t), publish.review(v, cfg, KEY, t)]
    with FakeRelayWallet(47123, proxy_url="", mint_url="") as relay:
        url = "ws://127.0.0.1:47123"
        assert publish.sync_relay(url, first, pub)["ok"] == 2
        again = [publish.profile(cfg, KEY, t + 60), publish.review(v, cfg, KEY, t + 60)]
        r = publish.sync_relay(url, again, pub)
        assert (r["sent"], r["unchanged"]) == (0, 2)
        v.latest["grade"] = "F"
        r = publish.sync_relay(url, [publish.review(v, cfg, KEY, t + 120)], pub)
        assert (r["sent"], r["ok"]) == (1, 1)
        day_later = [publish.profile(cfg, KEY, t + 86400)]
        assert publish.sync_relay(url, day_later, pub)["sent"] == 1
    assert sum(e["kind"] == 38000 for e in relay.events) == 2


def test_no_review_until_the_paid_checks_have_run():
    from nutcheck.publish import publishable
    class V:  # a reachable, complete run with no paid results
        grade = "?"
        latest = {"reachable": True, "results": []}
    assert not publishable(V())
