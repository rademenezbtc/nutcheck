"""Thin HTTP client for the Cashu v1 mint API, recording every exchange as evidence."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class Exchange:
    method: str
    path: str
    status: int
    request: object
    response: object
    ms: int


class Inconclusive(Exception):
    """The mint did not give a usable answer (rate limited, overloaded, gateway error).

    Deliberately NOT a MintError: checks treat a MintError as "the mint refused", so a rate-limit
    reply must never be mistaken for a refusal (a false pass) or for misbehaviour (a false fail)."""


class RateLimited(Inconclusive):
    pass


class MintUnavailable(Inconclusive):
    pass


class MintError(Exception):
    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        detail = body.get("detail") if isinstance(body, dict) else body
        code = body.get("code") if isinstance(body, dict) else None
        super().__init__(f"HTTP {status} code={code} {detail}")

    @property
    def code(self):
        return self.body.get("code") if isinstance(self.body, dict) else None


@dataclass
class MintClient:
    url: str
    timeout: float = 20.0
    log: list[Exchange] = field(default_factory=list)
    # Pacing (plain class attributes so tests can speed them up)
    min_interval = 0.2       # pace requests so we look like a wallet, not a load test
    slow_interval = 3.3      # POST spacing once a mint limits us (~18/min, under Nutshell's default 20/min)
    backoff = (65, 65, 65)   # waits after a 429. Nutshell's limiter uses elastic expiry: every request
                             # sent while limited extends the lockout, so wait out a full quiet minute
    gateway_backoff = (2, 5)  # waits after 502/503/504 on GETs

    def __post_init__(self):
        self.url = self.url.rstrip("/")
        self._http = httpx.Client(timeout=self.timeout, headers={"User-Agent": "Mozilla/5.0"})
        self._last = 0.0
        self.rate_limited = 0      # how many 429s we absorbed
        self.slow = False          # switched on by the first 429, or up front for mints known to limit hard

    _sleep = staticmethod(time.sleep)  # tests replace this to skip real waiting

    def _send(self, method, path, body):
        interval = self.slow_interval if (self.slow and method == "POST") else self.min_interval
        wait = interval - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        return self._http.request(method, self.url + path, json=body)

    def _call(self, method: str, path: str, body=None):
        retryable = {429} | ({502, 503, 504} if method == "GET" else set())
        attempt = 0
        while True:
            t0 = time.monotonic()
            try:
                r = self._send(method, path, body)
            except httpx.TimeoutException:
                if method == "GET" and attempt < 1:
                    attempt += 1
                    continue
                raise
            ms = int((time.monotonic() - t0) * 1000)
            waits = self.backoff if r.status_code == 429 else self.gateway_backoff
            if r.status_code in retryable and attempt < len(waits):
                if r.status_code == 429:
                    self.rate_limited += 1
                    self.slow = True
                ra = r.headers.get("retry-after", "")
                hinted = float(ra) if ra.replace(".", "", 1).isdigit() else 0
                self._sleep(min(max(hinted, waits[attempt]), 120))
                attempt += 1
                continue
            break
        try:
            data = r.json()
        except json.JSONDecodeError:
            data = r.text[:300]
        self.log.append(Exchange(method, path, r.status_code, body, data, ms))
        if r.status_code == 429:
            raise RateLimited(f"{method} {path}: still rate limited (HTTP 429) after backing off")
        if r.status_code >= 500:
            raise MintUnavailable(f"{method} {path}: HTTP {r.status_code}")
        if r.status_code >= 400:
            raise MintError(r.status_code, data)
        return data

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, body)

    # convenience wrappers
    def info(self):
        return self.get("/v1/info")

    def keysets(self):
        return self.get("/v1/keysets")["keysets"]

    def keys(self, keyset_id: str | None = None):
        return self.get(f"/v1/keys/{keyset_id}" if keyset_id else "/v1/keys")["keysets"]

    def mint_quote(self, amount: int, unit: str = "sat", pubkey: str | None = None):
        body = {"amount": amount, "unit": unit}
        if pubkey:
            body["pubkey"] = pubkey  # NUT-20: lock the quote to this key
        return self.post("/v1/mint/quote/bolt11", body)

    def mint_quote_state(self, quote: str):
        return self.get(f"/v1/mint/quote/bolt11/{quote}")

    def mint(self, quote: str, outputs: list[dict], signature: str | None = None):
        body = {"quote": quote, "outputs": outputs}
        if signature is not None:
            body["signature"] = signature
        return self.post("/v1/mint/bolt11", body)["signatures"]

    def swap(self, inputs: list[dict], outputs: list[dict]):
        return self.post("/v1/swap", {"inputs": inputs, "outputs": outputs})["signatures"]

    def melt_quote(self, request: str, unit: str = "sat"):
        return self.post("/v1/melt/quote/bolt11", {"request": request, "unit": unit})

    def melt(self, quote: str, inputs: list[dict], outputs: list[dict] | None = None):
        body = {"quote": quote, "inputs": inputs}
        if outputs is not None:
            body["outputs"] = outputs
        return self.post("/v1/melt/bolt11", body)

    def checkstate(self, Ys: list[str]):
        return self.post("/v1/checkstate", {"Ys": Ys})["states"]
