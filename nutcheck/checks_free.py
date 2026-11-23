"""Checks that cost nothing: they read public endpoints or submit invalid data the mint must refuse."""
from __future__ import annotations

import json
import time
import uuid
from urllib.parse import urlparse

from . import crypto, secp
from .client import Inconclusive, MintClient, MintError
from .model import Result, Severity as S, Status as St, publishable_exchange
from .wallet import Wallet

KNOWN_NUT_ENDPOINTS = {
    "7": ("POST", "/v1/checkstate", lambda: {"Ys": [secp.encode_hex(crypto.hash_to_curve(b"nutcheck-probe"))]}),
    "9": ("POST", "/v1/restore", lambda: {"outputs": []}),
}


def claimed(info: dict, nut: str) -> bool:
    n = info.get("nuts", {}).get(nut)
    if n is None:
        return False
    if isinstance(n, dict):
        if "supported" in n:
            return bool(n["supported"])
        return not n.get("disabled", False)
    return bool(n)


def run(client: MintClient, wallet: Wallet) -> tuple[list[Result], dict]:
    out: list[Result] = []
    info: dict = {}

    # -- transport --------------------------------------------------------------
    u = urlparse(client.url)
    local = u.hostname in ("localhost", "127.0.0.1", "::1") or (u.hostname or "").endswith(".onion")
    out.append(Result(
        "transport.https", "Mint is served over HTTPS", "-", S.MEDIUM,
        St.PASS if u.scheme == "https" else (St.SKIP if local else St.FAIL),
        "HTTPS in use" if u.scheme == "https" else
        ("Local or onion address; HTTPS not required" if local else "Plain HTTP exposes proofs in transit"),
    ))

    # -- NUT-06 info ------------------------------------------------------------
    try:
        info = client.info()
        problems = []
        if not isinstance(info.get("nuts"), dict):
            problems.append("no `nuts` object")
        if info.get("pubkey"):
            try:
                secp.decode(info["pubkey"])
            except secp.InvalidPoint:
                problems.append("`pubkey` is not a valid secp256k1 point")
        skew = None
        if isinstance(info.get("time"), int):
            skew = info["time"] - int(time.time())
            if abs(skew) > 120:
                problems.append(f"mint clock is off by {skew}s (affects P2PK/HTLC locktimes)")
        out.append(Result(
            "info.wellformed", "Info endpoint is well formed", "06", S.LOW,
            St.WARN if problems else St.PASS,
            "; ".join(problems) or f"{info.get('name')!r} running {info.get('version')}",
            evidence={"name": info.get("name"), "version": info.get("version"), "clock_skew_s": skew,
                      "nuts": sorted(info.get("nuts", {}), key=lambda x: int(x) if x.isdigit() else 999)},
        ))
    except Inconclusive:
        raise
    except Exception as e:  # noqa: BLE001
        out.append(Result("info.wellformed", "Info endpoint is well formed", "06", S.HIGH, St.FAIL,
                          f"GET /v1/info failed: {e}"))
        return out, info

    # -- NUT-01/02 keys ----------------------------------------------------------
    try:
        wallet.load_keysets()
    except Inconclusive:
        raise
    except Exception as e:  # noqa: BLE001
        out.append(Result("keys.load", "Keys and keysets load", "01", S.CRITICAL, St.FAIL, str(e)))
        return out, info

    bad_points, non_pow2, reused = [], [], []
    seen: dict[str, str] = {}
    for ks in wallet.keysets.values():
        for amt, pk in ks.keys.items():
            try:
                secp.decode(pk)
            except secp.InvalidPoint:
                bad_points.append(f"{ks.id[:12]}…/{amt}")
            if amt & (amt - 1):
                non_pow2.append(amt)
            if pk in seen:
                reused.append(f"{pk[:16]}… used for {seen[pk]} and {ks.id[:12]}…/{amt}")
            seen[pk] = f"{ks.id[:12]}…/{amt}"
    out.append(Result(
        "keys.valid_points", "All public keys are valid curve points", "01", S.HIGH,
        St.FAIL if bad_points else St.PASS,
        f"invalid keys: {bad_points}" if bad_points else f"{len(seen)} keys across {len(wallet.keysets)} keysets",
    ))
    out.append(Result(
        "keys.unique", "Every amount has its own key", "01", S.HIGH,
        St.FAIL if reused else St.PASS,
        ("Key reuse lets the mint mislabel amounts and can link users: " + "; ".join(reused[:5]))
        if reused else "No key is reused across amounts or keysets",
    ))
    if non_pow2:
        out.append(Result("keys.amounts", "Denominations are powers of two", "01", S.INFO, St.WARN,
                          f"non power-of-two amounts: {sorted(set(non_pow2))[:10]}"))

    # keyset ID derivation (NUT-02)
    mismatches, checked = [], 0
    for ks in wallet.keysets.values():
        if ks.id.startswith("00") and len(ks.id) == 16:
            expect = crypto.keyset_id_v1(ks.keys)
        elif ks.id.startswith("01") and len(ks.id) == 66:
            expect = crypto.keyset_id_v2(ks.keys, ks.unit, ks.input_fee_ppk, ks.final_expiry)
        else:
            mismatches.append({"id": ks.id, "problem": "unknown keyset ID version/length"})
            continue
        checked += 1
        if expect != ks.id:
            mismatches.append({"id": ks.id, "derived": expect, "unit": ks.unit, "input_fee_ppk": ks.input_fee_ppk})
    out.append(Result(
        "keys.keyset_id", "Keyset IDs match their keys", "02", S.HIGH,
        St.FAIL if mismatches else St.PASS,
        (f"{len(mismatches)} keyset(s) do not derive to their advertised ID. Wallets cannot verify which keys "
         "signed their ecash, and v2 IDs also commit to the advertised fee.") if mismatches
        else f"{checked} keyset ID(s) re-derived and matched",
        evidence={"mismatches": mismatches},
    ))

    actives = [k for k in wallet.keysets.values() if k.active]
    out.append(Result(
        "keys.active", "At least one active keyset", "02", S.HIGH,
        St.PASS if actives else St.FAIL,
        ", ".join(f"{k.unit}: {k.id[:12]}… fee {k.input_fee_ppk} ppk" for k in actives) or "no active keyset",
    ))

    # -- forged proof must be rejected (free) -----------------------------------
    if actives:
        ks = actives[0]
        amt = min(ks.keys)
        forged = {"amount": amt, "id": ks.id, "secret": crypto.random_secret(),
                  "C": secp.encode_hex(secp.mul(7, secp.G))}
        outs = wallet.outputs([amt])
        try:
            client.swap([forged], [o.wire() for o in outs])
            out.append(Result("swap.rejects_forgery", "Rejects forged ecash", "03", S.CRITICAL, St.FAIL,
                              "Mint signed new ecash in exchange for a proof with a made-up signature",
                              evidence={"exchange": publishable_exchange(client.log[-1])}))
        except MintError as e:
            out.append(Result("swap.rejects_forgery", "Rejects forged ecash", "03", S.CRITICAL, St.PASS,
                              f"Forged proof refused ({e.code or e.status})"))

    # -- claimed NUTs answer ----------------------------------------------------
    dead = []
    for nut, (method, path, body) in KNOWN_NUT_ENDPOINTS.items():
        if not claimed(info, nut):
            continue
        try:
            client._call(method, path, body())
        except MintError as e:
            if e.status in (404, 405, 501):
                dead.append(f"NUT-{nut} ({path} → {e.status})")
        except Inconclusive:
            raise
        except Exception as e:  # noqa: BLE001
            dead.append(f"NUT-{nut} ({path} → {e})")
    if claimed(info, "7"):
        try:
            st = client.checkstate([secp.encode_hex(crypto.hash_to_curve(crypto.random_secret().encode()))])
            if not st or st[0].get("state") != "UNSPENT":
                dead.append(f"NUT-07 reports {st} for a never-seen secret (expected UNSPENT)")
        except Inconclusive:
            raise
        except Exception:  # noqa: BLE001
            pass
    out.append(Result(
        "info.claims_answer", "Claimed NUT endpoints respond", "06", S.LOW,
        St.FAIL if dead else St.PASS,
        ("Info page advertises features that do not work: " + "; ".join(dead)) if dead
        else "Every probed claim answered",
    ))

    if claimed(info, "17"):
        out.append(websocket_check(client, info))
    return out, info


def ws_commands(info: dict) -> set[str]:
    sup = info.get("nuts", {}).get("17", {}).get("supported", [])
    return {c for m in sup if isinstance(m, dict) for c in m.get("commands", [])} if isinstance(sup, list) else set()


def websocket_check(client: MintClient, info: dict, timeout: float = 8.0) -> Result:
    """NUT-17: the advertised websocket answers, and a new subscription first gets the current state.
    Subscribes to a proof that has never existed: the mint must reply OK, then notify UNSPENT."""
    rid, title = "ws.subscribe", "WebSocket subscriptions work"
    url = client.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/v1/ws"

    def result(status, summary, **ev):
        return Result(rid, title, "17", S.LOW, status, summary, evidence={"url": url, **ev})

    try:
        from websockets.exceptions import InvalidStatus, WebSocketException
        from websockets.sync.client import connect
    except ImportError:
        return result(St.SKIP, "Needs the `websockets` package")
    y = secp.encode_hex(crypto.hash_to_curve(crypto.random_secret().encode()))
    sub_id = str(uuid.uuid4())
    req = {"jsonrpc": "2.0", "id": 0, "method": "subscribe",
           "params": {"kind": "proof_state", "filters": [y], "subId": sub_id}}
    seen: list = []
    try:
        with connect(url, open_timeout=timeout, close_timeout=2,
                     additional_headers={"User-Agent": "Mozilla/5.0"}) as ws:
            if "proof_state" not in ws_commands(info):
                return result(St.PASS, "WebSocket connects (proof_state is not advertised, so subscriptions "
                                       "were not tested)")
            ws.send(json.dumps(req))
            deadline = time.monotonic() + timeout
            acked = False
            while time.monotonic() < deadline:
                try:
                    msg = json.loads(ws.recv(timeout=max(deadline - time.monotonic(), 0.1)))
                except TimeoutError:
                    break
                seen.append(msg)
                if msg.get("id") == 0 and "error" in msg:
                    return result(St.FAIL, f"Subscription refused: {msg['error']}", messages=seen)
                if msg.get("id") == 0 and (msg.get("result") or {}).get("status") == "OK":
                    acked = True
                if msg.get("method") == "subscribe" and (msg.get("params") or {}).get("subId") == sub_id:
                    state = (msg["params"].get("payload") or {}).get("state")
                    if state == "UNSPENT":
                        return result(St.PASS, "Subscribed to a never-seen proof: got OK, then its current state "
                                               "(UNSPENT)" + ("" if acked else ", though without the OK first"))
                    return result(St.FAIL, f"First notification for a never-seen proof says {state!r}, "
                                           "expected UNSPENT", messages=seen)
            if not acked:
                return result(St.FAIL, f"No answer to a proof_state subscription within {timeout:.0f}s",
                              messages=seen)
            return result(St.FAIL, "Subscription accepted, but the mint never sent the current state. NUT-17 says it "
                                   "MUST notify the current state first (UNSPENT for a never-seen proof); wallets "
                                   "waiting for it hang until they fall back to polling.", messages=seen)
    except InvalidStatus as e:
        code = e.response.status_code
        return result(St.FAIL, f"Info page advertises NUT-17, but the WebSocket answers HTTP {code}", http_status=code)
    except (OSError, TimeoutError, WebSocketException) as e:
        return result(St.ERROR, f"Could not reach the WebSocket: {e}")
