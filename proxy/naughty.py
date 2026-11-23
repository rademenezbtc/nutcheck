"""Naughty proxy: put it in front of an honest mint and switch misbehaviours on cue.

    python proxy/naughty.py --upstream http://127.0.0.1:3338 --port 3339 [--modes bad_dleq,keep_change]

Change modes live (for the demo):
    curl -X POST localhost:3339/__naughty -d '{"modes": ["lock_bypass"]}'
    curl localhost:3339/__naughty

"Accepting" faults (double_spend, overspend, lock_bypass, forgery, quote_replay) work by
minting the client's requested outputs from a fresh quote that the upstream FakeWallet
marks as paid. The client receives real, valid signatures, exactly what it would get
from a broken mint. Only use this against a FakeWallet test mint.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

MODES = {
    "bad_dleq": "corrupt DLEQ proofs on every blind signature",
    "no_dleq": "strip DLEQ proofs while still claiming NUT-12",
    "keyset_tamper": "serve a substituted key for amount 1 (a tagging attack)",
    "fee_lie": "advertise 0 ppk input fees while the mint charges its real fee",
    "short_mint": "drop one signature when issuing paid ecash",
    "quote_replay": "issue ecash again for an already-minted quote",
    "overspend": "accept swaps whose outputs exceed inputs",
    "double_spend": "accept already-spent and duplicated proofs",
    "forgery": "accept proofs with invalid signatures",
    "lock_bypass": "ignore P2PK/HTLC spending conditions",
    "keep_change": "pay the invoice but keep the overpaid fee change",
    "info_lie": "claim NUT-07 checkstate but answer 404",
    "ws_dead": "keep advertising NUT-17 but answer 404 on /v1/ws",
    "ws_silent": "accept WebSocket subscriptions but never send the current state",
    "htlc_no_refund": "refuse the HTLC refund path (refund signature without a preimage), like CDK 0.18.x",
    "sigall_ignore": "accept SIG_ALL spends whose signature does not cover the outputs",
    "quote_lock_ignore": "ignore NUT-20: drop the quote pubkey, then mint the quote without a signature",
    "sigall_legacy": "accept SIG_ALL spends only when signed over the pre-Nov-2025 message (secrets and B_ only)",
    "payout_fail": "refuse every melt with 'Payment failed', like a mint whose Lightning node can't route out",
    "slow_melt": "answer melts only after a 4 s delay (the payment itself completes at once), like a slow Lightning payout",
    "await_payment": "report mint quotes unpaid until POST /__paid marks the invoice paid (for wallet tests)",
    "rate_limit_burst": "answer 429 to every 8th request, twice in a row (a mint with a strict nginx limit)",
    "rate_limit_hard": "answer 429 to everything after the first 25 requests (a mint that throttles hard)",
}

state = {"modes": set(), "paid": set(), "count": 0, "burst": 0}
upstream: httpx.AsyncClient


def on(mode):
    return mode in state["modes"]


async def forge_signatures(outputs: list[dict]) -> list[dict]:
    """Get real signatures on the client's outputs by minting them from a fake-paid quote."""
    total = sum(o["amount"] for o in outputs)
    if total == 0:
        return []
    q = (await upstream.post("/v1/mint/quote/bolt11", json={"amount": total, "unit": "sat"})).json()
    for _ in range(50):
        st = (await upstream.get(f"/v1/mint/quote/bolt11/{q['quote']}")).json()
        if st.get("state") == "PAID" or st.get("paid"):
            break
        await asyncio.sleep(0.1)
    r = await upstream.post("/v1/mint/bolt11", json={"quote": q["quote"], "outputs": outputs})
    return r.json()["signatures"]


def tamper_sigs(sigs: list[dict]) -> list[dict]:
    for s in sigs:
        if s.get("dleq"):
            if on("no_dleq"):
                s.pop("dleq")
            elif on("bad_dleq"):
                s["dleq"]["s"] = format((int(s["dleq"]["s"], 16) + 1) % (2**256), "064x")
    return sigs


SUBSTITUTE_KEY = None


async def substitute_key() -> str:
    """A key the honest mint never published (proxy keeps it so it is stable per run)."""
    global SUBSTITUTE_KEY
    if SUBSTITUTE_KEY is None:
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from nutcheck.secp import PrivateKey
        SUBSTITUTE_KEY = PrivateKey().pubkey_hex
    return SUBSTITUTE_KEY


async def legacy_sig_all(req: dict):
    """Judge a SIG_ALL swap the way older mints did: one signature over every secret, then every B_."""
    import hashlib, sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from nutcheck import secp
    ins, outs = req["inputs"], req["outputs"]
    msg = ("".join(i["secret"] for i in ins) + "".join(o["B_"] for o in outs)).encode()
    pub = bytes.fromhex(json.loads(ins[0]["secret"])[1]["data"])[1:]
    sigs = json.loads(ins[0].get("witness") or "{}").get("signatures", [])
    if any(secp.schnorr_verify(hashlib.sha256(msg).digest(), pub, bytes.fromhex(s)) for s in sigs):
        return {"signatures": await forge_signatures(outs)}, 200
    return {"detail": "signature threshold not met. 0 < 1.", "code": 11000}, 400


async def relay_ws(client: WebSocket):
    """Pass NUT-17 WebSocket traffic through to the mint (or misbehave, per mode)."""
    import websockets
    if on("ws_dead"):
        try:  # answer the upgrade with a plain 404, like a mint whose reverse proxy has no /v1/ws route
            await client.send_denial_response(JSONResponse({"detail": "Not Found"}, 404))
        except RuntimeError:  # server without the denial-response extension: refused with 403 instead
            await client.close(code=1008)
        return
    await client.accept()
    url = str(upstream.base_url).rstrip("/").replace("http", "ws", 1) + "/v1/ws"
    async with websockets.connect(url) as mint:
        async def up():
            try:
                while True:
                    await mint.send(await client.receive_text())
            except WebSocketDisconnect:
                await mint.close()

        async def down():
            async for msg in mint:
                if on("ws_silent") and json.loads(msg).get("method") == "subscribe":
                    continue  # swallow notifications
                await client.send_text(msg)

        done, pending = await asyncio.wait([asyncio.create_task(up()), asyncio.create_task(down())],
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()


async def handle(request: Request):
    path = request.url.path
    body = await request.body()
    req_json = json.loads(body) if body else None

    if path == "/__naughty":
        if request.method == "POST":
            modes = set(req_json.get("modes", []))
            unknown = modes - MODES.keys()
            if unknown:
                return JSONResponse({"error": f"unknown modes {sorted(unknown)}", "available": MODES}, 400)
            state["modes"] = modes
            state["count"] = state["burst"] = 0
        return JSONResponse({"modes": sorted(state["modes"]), "available": MODES})

    if path.startswith("/v1/"):
        state["count"] += 1
        if on("rate_limit_hard") and state["count"] > 25:
            return Response("<html><body><h1>429 Too Many Requests</h1></body></html>", 429, media_type="text/html")
        if on("rate_limit_burst"):
            if state["burst"] > 0:
                state["burst"] -= 1
                return Response("<html><body><h1>429 Too Many Requests</h1></body></html>", 429, media_type="text/html")
            if state["count"] % 8 == 0:
                state["burst"] = 1
                return Response("<html><body><h1>429 Too Many Requests</h1></body></html>", 429, media_type="text/html")

    if path == "/__paid":
        state["paid"].add(req_json["invoice"])
        return JSONResponse({"ok": True})

    if on("info_lie") and path == "/v1/checkstate":
        return JSONResponse({"detail": "Not Found"}, 404)

    if path == "/v1/swap" and req_json and on("htlc_no_refund"):
        for i in req_json.get("inputs", []):
            if i["secret"].startswith('["HTLC"') and "preimage" not in json.loads(i.get("witness") or "{}"):
                return JSONResponse({"detail": "Secret is not a HTLC secret", "code": 10000}, 400)

    if path == "/v1/melt/bolt11" and on("payout_fail"):
        return JSONResponse({"detail": "Payment failed", "code": 20004}, 400)

    if path == "/v1/swap" and req_json and on("sigall_legacy") and any(
            '"SIG_ALL"' in i["secret"] for i in req_json.get("inputs", [])):
        return JSONResponse(*await legacy_sig_all(req_json))

    if path == "/v1/mint/quote/bolt11" and request.method == "POST" and on("quote_lock_ignore") and req_json:
        req_json.pop("pubkey", None)
        body = json.dumps(req_json).encode()

    r = await upstream.request(request.method, path, content=body,
                               headers={"content-type": request.headers.get("content-type", "application/json")})
    try:
        data = r.json()
    except ValueError:
        return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))
    status = r.status_code
    code = data.get("code") if isinstance(data, dict) else None

    # ---- read side ----------------------------------------------------------
    if path.startswith("/v1/keys") and status == 200:
        for ks in data.get("keysets", []):
            if on("fee_lie") and "input_fee_ppk" in ks:
                ks["input_fee_ppk"] = 0
            if on("keyset_tamper") and "keys" in ks and "1" in ks["keys"]:
                ks["keys"]["1"] = await substitute_key()
    if on("await_payment") and path.startswith("/v1/mint/quote/bolt11") and status == 200 \
            and data.get("request") not in state["paid"]:
        data["state"], data["paid"] = "UNPAID", False
    if path == "/v1/info" and on("info_lie"):
        data["nuts"]["7"] = {"supported": True}
    if path.startswith("/v1/mint/quote/bolt11") and on("quote_lock_ignore") and status == 200:
        if request.method == "POST" and json.loads(await request.body() or b"{}").get("pubkey"):
            state.setdefault("locked_quotes", {})[data["quote"]] = json.loads(await request.body())["pubkey"]
        if data.get("quote") in state.get("locked_quotes", {}):
            data["pubkey"] = state["locked_quotes"][data["quote"]]  # pretend the lock was kept

    # ---- issuing ------------------------------------------------------------
    if path == "/v1/mint/bolt11":
        if status == 200 and on("short_mint") and len(data["signatures"]) > 1:
            data["signatures"] = data["signatures"][:-1]
        if status >= 400 and on("quote_replay"):
            data, status = {"signatures": await forge_signatures(req_json["outputs"])}, 200

    if path == "/v1/swap" and status >= 400:
        inputs = req_json.get("inputs", [])
        locked = any(i["secret"].startswith(('["P2PK"', '["HTLC"')) for i in inputs)
        sig_all = any('"SIG_ALL"' in i["secret"] for i in inputs)
        override = (
            (on("lock_bypass") and locked)
            or (on("sigall_ignore") and sig_all)
            or (on("double_spend") and code in (11001, 11007))
            # spec says 11005; Nutshell 0.21 sends the generic 11000 with "not balanced"
            or (on("overspend") and not locked and (code == 11005 or "balanced" in str(data.get("detail"))))
            or (on("forgery") and code in (10001, 10003) and not locked)
        )
        if override:
            data, status = {"signatures": await forge_signatures(req_json["outputs"])}, 200

    if path == "/v1/melt/bolt11" and on("slow_melt"):
        await asyncio.sleep(4)

    if path.startswith("/v1/melt/") and status == 200 and on("keep_change") and data.get("change"):
        data["change"] = []

    if status == 200 and isinstance(data, dict):
        for k in ("signatures", "change"):
            if isinstance(data.get(k), list):
                tamper_sigs(data[k])

    return JSONResponse(data, status)


def main():
    global upstream
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default="http://127.0.0.1:3338")
    ap.add_argument("--port", type=int, default=3339)
    ap.add_argument("--modes", default="")
    a = ap.parse_args()
    state["modes"] = {m for m in a.modes.split(",") if m}
    upstream = httpx.AsyncClient(base_url=a.upstream, timeout=30)
    app = Starlette(routes=[WebSocketRoute("/v1/ws", relay_ws),
                            Route("/{path:path}", handle, methods=["GET", "POST"])])
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
