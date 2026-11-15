"""Publish NutCheck grades on Nostr as NIP-87 mint reviews (kind 38000).

One replaceable event per mint, signed by NutCheck's own key, so wallets can show a mint's grade
without trusting nutcheck.org. The content follows cashu.me's review format ("[4/5] text"), and a
"rating" tag carries the same number for clients such as Sovran. Extra tags hold the letter grade,
the evidence page and the test time.

A review is only sent to a relay when its text changed or the copy there is over a day old, so
four runs a day don't flood relays with identical events.

    nutcheck nostr keygen                       # new key: prints npub and nsec (keep the nsec safe)
    nutcheck nostr pubkey                       # npub of $NOSTR_SECRET_KEY
    nutcheck nostr publish scorecard.json       # publish (needs $NOSTR_SECRET_KEY, hex or nsec)
    nutcheck nostr publish scorecard.json --dry-run
    nutcheck nostr publish scorecard.json --with-profile   # also set the kind 0 profile (normally set in an app)
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone

from . import nostr, secp

DEFAULT_RELAYS = ["wss://relay.damus.io", "wss://nos.lol", "wss://relay.primal.net"]
REFRESH_S = 20 * 3600
RATING = {"A": 5, "B": 4, "C": 3, "D": 2, "F": 1}

# ---- bech32 (NIP-19) ---------------------------------------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data, frombits, tobits, pad):
    acc = bits = 0
    out = []
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & ((1 << tobits) - 1))
    if pad and bits:
        out.append((acc << (tobits - bits)) & ((1 << tobits) - 1))
    elif not pad and (bits >= frombits or (acc << (tobits - bits)) & ((1 << tobits) - 1)):
        raise ValueError("invalid padding")
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    d = _convertbits(data, 8, 5, True)
    pm = _polymod(_hrp_expand(hrp) + d + [0] * 6) ^ 1
    return hrp + "1" + "".join(_CHARSET[x] for x in d + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


def bech32_decode(s: str) -> tuple[str, bytes]:
    s = s.lower()
    hrp, _, rest = s.rpartition("1")
    d = [_CHARSET.index(c) for c in rest]
    if not hrp or len(d) < 6 or _polymod(_hrp_expand(hrp) + d) != 1:
        raise ValueError("bad bech32 checksum")
    return hrp, bytes(_convertbits(d[:-6], 5, 8, False))


def npub(pub_hex: str) -> str:
    return bech32_encode("npub", bytes.fromhex(pub_hex))


def load_seckey(value: str) -> int:
    """A secret key given as 64 hex characters or as an nsec."""
    value = value.strip()
    if value.startswith("nsec1"):
        hrp, raw = bech32_decode(value)
        if hrp != "nsec":
            raise ValueError("not an nsec")
    else:
        raw = bytes.fromhex(value)
    k = int.from_bytes(raw, "big")
    if len(raw) != 32 or not 0 < k < secp.N:
        raise ValueError("not a valid secret key")
    return k


# ---- events ------------------------------------------------------------------------------------

def rating_for(report: dict) -> int:
    r = RATING[report["grade"][0]]
    return min(r, 4) if report.get("partial") else r  # an untested half can't earn full marks


def publishable(v) -> bool:
    """Skip mints that didn't answer or whose run broke off: the last review stays until a real result."""
    L = v.latest
    return L.get("reachable", True) and not any(r["id"] == "nutcheck.inconclusive" for r in L["results"])


def review(v, cfg: dict, seckey: int, created_at: int | None = None) -> dict:
    from .site import run_tip
    L = v.latest
    grade = L["grade"] + ("*" if L.get("partial") else "")
    findings = run_tip(v, L).split("\n")[1:]
    reason = v.partial_reason.removeprefix(" · ")
    if reason and findings == ["All checks passed (free checks only)"]:
        findings = [f"Free checks passed; {reason}"]
    page = cfg.get("base_url", "https://nutcheck.org/") + f"mints/{v.slug}.html"
    rating = rating_for(L)
    content = f"[{rating}/5] NutCheck grade {grade}. {'. '.join(f.rstrip('.') for f in findings)}. Evidence: {page}"
    d = (L.get("mint_info") or {}).get("pubkey") or v.url
    tags = [["d", d], ["k", "38172"], ["u", v.url, "cashu"], ["rating", str(rating)], ["grade", grade],
            ["r", page], ["t", "nutcheck"], ["tested", L["started"]],
            ["alt", f"NutCheck review of the Cashu mint {v.url}: grade {grade}"]]
    return nostr.sign_event(seckey, 38000, content, tags, created_at)


def profile(cfg: dict, seckey: int, created_at: int | None = None) -> dict:
    base = cfg.get("base_url", "https://nutcheck.org/")
    meta = {"name": "NutCheck", "display_name": "NutCheck",
            "about": ("Independent protocol tests for Cashu mints: DLEQ proofs, keyset IDs, P2PK/HTLC locks, "
                      "double spends, payouts and fee refunds. Grades A–F with evidence, re-tested every 6 hours. "
                      "Reviews are NIP-87 events: A = 5/5, B = 4, C = 3, D = 2, F = 1; a partial run (*) gets at most 4. "
                      "Measures behaviour, not solvency."),
            "picture": base + "logo.png", "website": base,
            "lud16": cfg.get("lightning", "nutcheck@rizful.com")}
    host = base.split("//", 1)[-1].strip("/")
    if cfg.get("nostr_npub") and "/" not in host:
        meta["nip05"] = f"_@{host}"
    return nostr.sign_event(seckey, 0, json.dumps(meta, ensure_ascii=False), [], created_at)


# ---- relays ------------------------------------------------------------------------------------

def _stale(existing: dict | None, ev: dict) -> bool:
    if not existing:
        return True
    if existing["content"] != ev["content"]:
        return True
    return ev["created_at"] - existing["created_at"] > REFRESH_S


def sync_relay(relay: str, events: list[dict], pub: str, timeout: float = 15.0) -> dict:
    """Send the events this relay doesn't have yet (or has an outdated copy of). Returns counts."""
    from .nwc import _connect
    out = {"relay": relay, "sent": 0, "ok": 0, "unchanged": 0, "errors": []}
    with _connect(relay, timeout) as ws:
        sub = secrets.token_hex(8)
        ws.send(json.dumps(["REQ", sub, {"authors": [pub], "kinds": [0, 38000]}]))
        have: dict[tuple, dict] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            except TimeoutError:
                break
            if msg[0] == "EOSE":
                break
            if msg[0] == "EVENT" and msg[1] == sub:
                e = msg[2]
                key = (e["kind"], next((t[1] for t in e["tags"] if t[0] == "d"), ""))
                if key not in have or e["created_at"] > have[key]["created_at"]:
                    have[key] = e
        ws.send(json.dumps(["CLOSE", sub]))
        pending = {}
        for ev in events:
            key = (ev["kind"], next((t[1] for t in ev["tags"] if t[0] == "d"), ""))
            if not _stale(have.get(key), ev):
                out["unchanged"] += 1
                continue
            ws.send(json.dumps(["EVENT", ev]))
            pending[ev["id"]] = ev
            out["sent"] += 1
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            try:
                msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            except TimeoutError:
                break
            if msg[0] == "OK" and msg[1] in pending:
                pending.pop(msg[1])
                if msg[2]:
                    out["ok"] += 1
                else:
                    out["errors"].append(str(msg[3])[:120])
        if pending:
            out["errors"].append(f"{len(pending)} not acknowledged")
    return out


# ---- commands ----------------------------------------------------------------------------------

def _env_key() -> int:
    raw = os.environ.get("NOSTR_SECRET_KEY", "")
    if not raw.strip():
        raise SystemExit("NOSTR_SECRET_KEY is not set")
    return load_seckey(raw)


def cmd_publish(argv) -> int:
    from .site import build_views
    from .cli import load_config
    ap = argparse.ArgumentParser(prog="nutcheck nostr publish")
    ap.add_argument("config")
    ap.add_argument("--dry-run", action="store_true", help="print the events instead of sending them")
    ap.add_argument("--relay", action="append", help="relay URL (repeatable; default from config)")
    ap.add_argument("--with-profile", action="store_true",
                    help="also publish NutCheck's kind 0 profile (overwrites a profile edited in a Nostr app)")
    a = ap.parse_args(argv)
    cfg, base = load_config(a.config)
    seckey = _env_key()
    pub = nostr.xonly_pub(seckey)
    want = cfg.get("nostr_npub")
    if want and want != npub(pub):
        raise SystemExit(f"NOSTR_SECRET_KEY belongs to {npub(pub)}, but scorecard.json says nostr_npub is {want}")
    now = datetime.now(timezone.utc)
    views = build_views(cfg, base, now, int(cfg.get("embargo_days", 14)))
    t = int(now.timestamp())
    events = [review(v, cfg, seckey, t) for v in views if v.reports and publishable(v)]
    if a.with_profile:
        events.insert(0, profile(cfg, seckey, t))
    skipped = [v.url for v in views if v.reports and not publishable(v)]
    if a.dry_run:
        for ev in events:
            print(json.dumps(ev, ensure_ascii=False))
        print(f"{sum(e['kind'] == 38000 for e in events)} reviews as {npub(pub)}; skipped (no usable result): {', '.join(skipped) or 'none'}",
              file=sys.stderr)
        return 0
    relays = a.relay or cfg.get("nostr_relays") or DEFAULT_RELAYS
    reached = 0
    for relay in relays:
        try:
            r = sync_relay(relay, events, pub)
        except Exception as e:  # one bad relay must not stop the others
            print(f"::warning::{relay}: {type(e).__name__}: {e}"[:300])
            continue
        reached += 1
        print(f"{relay}: sent {r['sent']}, accepted {r['ok']}, unchanged {r['unchanged']}"
              + (f", errors: {'; '.join(r['errors'])}" if r["errors"] else ""))
    print(f"Published as {npub(pub)}; skipped: {', '.join(skipped) or 'none'}")
    if not reached:
        print("::warning::No Nostr relay could be reached; grades were not published this run")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd == "keygen":
        k = int.from_bytes(secrets.token_bytes(32), "big") % (secp.N - 1) + 1
        print(f"npub (public, share it):  {npub(nostr.xonly_pub(k))}")
        print(f"nsec (secret, keep it):   {bech32_encode('nsec', k.to_bytes(32, 'big'))}")
        return 0
    if cmd == "pubkey":
        print(npub(nostr.xonly_pub(_env_key())))
        return 0
    if cmd == "publish":
        return cmd_publish(argv[1:])
    print(__doc__.split("\n\n")[-1])
    return 2


if __name__ == "__main__":
    sys.exit(main())
