"""Minimal Nostr Wallet Connect (NIP-47) client: get_info, pay_invoice, make_invoice.

The connection URI is a spending credential. It is read from the NWC_URL environment variable,
never logged, and never written into reports.
"""
from __future__ import annotations

import json
import secrets
import time
from urllib.parse import parse_qs, urlparse

from . import nostr

KIND_INFO, KIND_REQUEST, KIND_RESPONSE = 13194, 23194, 23195


class NWCError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def parse_uri(uri: str) -> tuple[str, list[str], int]:
    u = urlparse(uri.strip())
    if u.scheme not in ("nostr+walletconnect", "nostrwalletconnect"):
        raise ValueError("not a nostr+walletconnect:// URI")
    wallet_pub = (u.netloc or u.path.lstrip("/")).lower()
    q = parse_qs(u.query)
    relays, secret = q.get("relay", []), q.get("secret", [None])[0]
    if len(wallet_pub) != 64 or not relays or not secret:
        raise ValueError("NWC URI must contain a wallet pubkey, at least one relay and a secret")
    return wallet_pub, relays, int(secret, 16)


def _connect(relay: str, timeout: float):
    from websockets.sync.client import connect
    host = urlparse(relay).hostname or ""
    kwargs = {"open_timeout": timeout, "close_timeout": 5}
    if host in ("localhost", "127.0.0.1", "::1"):
        kwargs["proxy"] = None
    try:
        return connect(relay, **kwargs)
    except TypeError:  # older websockets without the proxy argument
        kwargs.pop("proxy", None)
        return connect(relay, **kwargs)


class NWC:
    def __init__(self, uri: str, timeout: float = 60.0):
        self.wallet_pub, self.relays, self._secret = parse_uri(uri)
        self.client_pub = nostr.xonly_pub(self._secret)
        self.timeout = timeout
        self.encryption: str | None = None
        self.methods: set[str] = set()

    def __repr__(self):  # never show the secret
        return f"NWC(wallet={self.wallet_pub[:8]}…, relays={self.relays})"

    # ---- transport ----------------------------------------------------------------------------

    def _with_relay(self, fn):
        last = None
        for relay in self.relays:
            try:
                with _connect(relay, self.timeout) as ws:
                    return fn(ws)
            except NWCError:
                raise
            except Exception as e:  # noqa: BLE001  try the next relay
                last = e
        raise ConnectionError(f"no NWC relay reachable ({type(last).__name__}: {last})")

    def _recv(self, ws, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("NWC wallet did not answer in time")
        return json.loads(ws.recv(timeout=remaining))

    # ---- protocol -----------------------------------------------------------------------------

    def get_info(self) -> dict:
        def run(ws):
            sub = secrets.token_hex(8)
            ws.send(json.dumps(["REQ", sub, {"kinds": [KIND_INFO], "authors": [self.wallet_pub], "limit": 1}]))
            deadline = time.monotonic() + self.timeout
            found = None
            while True:
                msg = self._recv(ws, deadline)
                if msg[0] == "EVENT" and msg[1] == sub and nostr.verify_event(msg[2]):
                    found = msg[2]
                elif msg[0] == "EOSE" and msg[1] == sub:
                    break
            ws.send(json.dumps(["CLOSE", sub]))
            return found
        ev = self._with_relay(run)
        enc = ""
        if ev:
            self.methods = set(ev["content"].split())
            enc = " ".join(t[1] for t in ev["tags"] if len(t) > 1 and t[0] == "encryption")
        self.encryption = "nip44_v2" if "nip44_v2" in enc.split() else "nip04"
        return {"methods": sorted(self.methods), "encryption": self.encryption, "found": ev is not None}

    def _encrypt(self, text: str) -> str:
        if self.encryption == "nip44_v2":
            return nostr.nip44_encrypt(nostr.nip44_conversation_key(self._secret, self.wallet_pub), text)
        return nostr.nip04_encrypt(self._secret, self.wallet_pub, text)

    def _decrypt(self, payload: str) -> str:
        if "?iv=" in payload:
            return nostr.nip04_decrypt(self._secret, self.wallet_pub, payload)
        return nostr.nip44_decrypt(nostr.nip44_conversation_key(self._secret, self.wallet_pub), payload)

    def request(self, method: str, params: dict) -> dict:
        if self.encryption is None:
            self.get_info()
        tags = [["p", self.wallet_pub]]
        if self.encryption == "nip44_v2":
            tags.append(["encryption", "nip44_v2"])
        ev = nostr.sign_event(self._secret, KIND_REQUEST, self._encrypt(json.dumps({"method": method, "params": params})), tags)

        def run(ws):
            sub = secrets.token_hex(8)
            ws.send(json.dumps(["REQ", sub, {"kinds": [KIND_RESPONSE], "authors": [self.wallet_pub], "#e": [ev["id"]]}]))
            ws.send(json.dumps(["EVENT", ev]))
            deadline = time.monotonic() + self.timeout
            while True:
                msg = self._recv(ws, deadline)
                if msg[0] == "OK" and msg[1] == ev["id"] and not msg[2]:
                    raise NWCError("RELAY_REJECTED", str(msg[3] if len(msg) > 3 else ""))
                if msg[0] != "EVENT" or msg[1] != sub:
                    continue
                resp = msg[2]
                if resp.get("pubkey") != self.wallet_pub or not nostr.verify_event(resp):
                    continue
                if ["e", ev["id"]] not in [t[:2] for t in resp["tags"]]:
                    continue
                body = json.loads(self._decrypt(resp["content"]))
                ws.send(json.dumps(["CLOSE", sub]))
                if body.get("error"):
                    raise NWCError(body["error"].get("code", "OTHER"), body["error"].get("message", ""))
                return body.get("result") or {}
        return self._with_relay(run)

    def pay_invoice(self, invoice: str) -> dict:
        return self.request("pay_invoice", {"invoice": invoice})

    def make_invoice(self, amount_sat: int, description: str) -> str:
        res = self.request("make_invoice", {"amount": amount_sat * 1000, "description": description})
        if not res.get("invoice"):
            raise NWCError("OTHER", "wallet returned no invoice")
        return res["invoice"]
