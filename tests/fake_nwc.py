"""A stand-in Nostr relay with an NWC wallet service built in, for testing NutCheck's funding loop.

- pay_invoice: records the payment and tells the naughty proxy (await_payment mode) the invoice is paid.
- make_invoice: returns a real bolt11 from the local test mint, so the pay-back melt is a real melt.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading

import httpx

from nutcheck import nostr

_MULT = {"": 100_000_000, "m": 100_000, "u": 100, "n": 0.1, "p": 0.0001}


def bolt11_sats(inv: str) -> int:
    m = re.match(r"ln(?:bcrt|bc|tbs|tb)(\d+)([munp]?)1", inv.lower())
    return int(int(m.group(1)) * _MULT[m.group(2)]) if m else 0


def _match(f: dict, ev: dict) -> bool:
    if "kinds" in f and ev["kind"] not in f["kinds"]:
        return False
    if "authors" in f and ev["pubkey"] not in f["authors"]:
        return False
    for key, vals in f.items():
        if key.startswith("#") and not any(t[0] == key[1:] and t[1] in vals for t in ev["tags"] if len(t) > 1):
            return False
    return True


class FakeRelayWallet:
    def __init__(self, port: int, *, proxy_url: str, mint_url: str, encryption: str | None = "nip44_v2 nip04",
                 fail_pay: bool = False, fail_make: bool = False):
        self.port, self.proxy_url, self.mint_url = port, proxy_url, mint_url
        self.wallet_sec = 0xB0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0B0 + port
        self.wallet_pub = nostr.xonly_pub(self.wallet_sec)
        self.client_sec = 0xC11E47C11E47C11E47C11E47C11E47C11E47C11E47C11E47C11E47C11E47 + port
        self.encryption, self.fail_pay, self.fail_make = encryption, fail_pay, fail_make
        self.events: list[dict] = []
        self.subs: dict = {}
        self.paid: list[tuple[str, int]] = []
        self.invoices: list[tuple[str, int]] = []
        tags = [["encryption", encryption]] if encryption else []
        self.events.append(nostr.sign_event(self.wallet_sec, 13194, "pay_invoice make_invoice get_info", tags))
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()

    @property
    def uri(self) -> str:
        return f"nostr+walletconnect://{self.wallet_pub}?relay=ws://127.0.0.1:{self.port}&secret={self.client_sec:064x}"

    # ---- relay ----------------------------------------------------------------------------------
    async def _send(self, ws, msg):
        await ws.send(json.dumps(msg))

    async def _publish(self, ev: dict):
        self.events.append(ev)
        for ws, subs in list(self.subs.items()):
            for sid, filters in subs.items():
                if any(_match(f, ev) for f in filters):
                    await self._send(ws, ["EVENT", sid, ev])

    async def _handler(self, ws):
        self.subs[ws] = {}
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg[0] == "REQ":
                    sid, filters = msg[1], msg[2:]
                    self.subs[ws][sid] = filters
                    for ev in list(self.events):
                        if any(_match(f, ev) for f in filters):
                            await self._send(ws, ["EVENT", sid, ev])
                    await self._send(ws, ["EOSE", sid])
                elif msg[0] == "CLOSE":
                    self.subs[ws].pop(msg[1], None)
                elif msg[0] == "EVENT":
                    ev = msg[1]
                    ok = nostr.verify_event(ev)
                    await self._send(ws, ["OK", ev["id"], ok, "" if ok else "invalid: bad signature"])
                    if ok:
                        await self._publish(ev)
                        if ev["kind"] == 23194 and ["p", self.wallet_pub] in [t[:2] for t in ev["tags"]]:
                            await self._publish(await asyncio.to_thread(self._serve, ev))
        finally:
            self.subs.pop(ws, None)

    # ---- wallet service ---------------------------------------------------------------------------
    def _serve(self, req: dict) -> dict:
        nip44 = ["encryption", "nip44_v2"] in [t[:2] for t in req["tags"]]
        ck = nostr.nip44_conversation_key(self.wallet_sec, req["pubkey"])
        body = json.loads(nostr.nip44_decrypt(ck, req["content"]) if nip44
                          else nostr.nip04_decrypt(self.wallet_sec, req["pubkey"], req["content"]))
        method, params = body["method"], body.get("params", {})
        result, error = None, None
        if method == "pay_invoice":
            if self.fail_pay:
                error = {"code": "PAYMENT_FAILED", "message": "test: payment failed"}
            else:
                inv = params["invoice"]
                self.paid.append((inv, bolt11_sats(inv)))
                httpx.post(self.proxy_url + "/__paid", json={"invoice": inv})
                result = {"preimage": "00" * 32, "fees_paid": 0}
        elif method == "make_invoice":
            if self.fail_make:
                error = {"code": "INTERNAL", "message": "test: cannot make invoices"}
            else:
                sat = params["amount"] // 1000
                q = httpx.post(self.mint_url + "/v1/mint/quote/bolt11", json={"amount": sat, "unit": "sat"}).json()
                self.invoices.append((q["request"], sat))
                result = {"type": "incoming", "invoice": q["request"], "amount": params["amount"]}
        else:
            error = {"code": "NOT_IMPLEMENTED", "message": method}
        text = json.dumps({"result_type": method, "error": error, "result": result})
        content = nostr.nip44_encrypt(ck, text) if nip44 else nostr.nip04_encrypt(self.wallet_sec, req["pubkey"], text)
        return nostr.sign_event(self.wallet_sec, 23195, content, [["p", req["pubkey"]], ["e", req["id"]]])

    # ---- lifecycle ----------------------------------------------------------------------------------
    def __enter__(self):
        from websockets.asyncio.server import serve

        async def main():
            async with serve(self._handler, "127.0.0.1", self.port):
                self._ready.set()
                self._stop = asyncio.Event()
                await self._stop.wait()

        self._thread = threading.Thread(target=self._loop.run_until_complete, args=(main(),), daemon=True)
        self._thread.start()
        self._ready.wait(10)
        return self

    def __exit__(self, *exc):
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)
