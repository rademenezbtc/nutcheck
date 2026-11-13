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
from starlette.routing import Route

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
}

state = {"modes": set()}
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
        return JSONResponse({"modes": sorted(state["modes"]), "available": MODES})

    if on("info_lie") and path == "/v1/checkstate":
        return JSONResponse({"detail": "Not Found"}, 404)

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
    if path == "/v1/info" and on("info_lie"):
        data["nuts"]["7"] = {"supported": True}

    # ---- issuing ------------------------------------------------------------
    if path == "/v1/mint/bolt11":
        if status == 200 and on("short_mint") and len(data["signatures"]) > 1:
            data["signatures"] = data["signatures"][:-1]
        if status >= 400 and on("quote_replay"):
            data, status = {"signatures": await forge_signatures(req_json["outputs"])}, 200

    if path == "/v1/swap" and status >= 400:
        inputs = req_json.get("inputs", [])
        locked = any(i["secret"].startswith(('["P2PK"', '["HTLC"')) for i in inputs)
        override = (
            (on("lock_bypass") and locked)
            or (on("double_spend") and code in (11001, 11007))
            # spec says 11005; Nutshell 0.21 sends the generic 11000 with "not balanced"
            or (on("overspend") and not locked and (code == 11005 or "balanced" in str(data.get("detail"))))
            or (on("forgery") and code in (10001, 10003) and not locked)
        )
        if override:
            data, status = {"signatures": await forge_signatures(req_json["outputs"])}, 200

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
    app = Starlette(routes=[Route("/{path:path}", handle, methods=["GET", "POST"])])
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
