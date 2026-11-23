"""Paying for funded checks with a real wallet, and paying the leftovers back.

Safety rules:
- a hard cap on sats paid out per sweep (`nwc_max_sat_per_run`, default 1000);
- a pay-back is retried; if it still fails, the leftover ecash is stashed, encrypted with a key derived
  from the NWC secret, so it is never lost with the CI runner. The next funded sweep pays it back first;
- after a wallet error, a pay-back that could not be stashed, or MAX_PAYBACK_FAILURES pay-backs that failed in
  an unexpected way (a likely NutCheck bug), no further mint is funded in that sweep. A mint that simply can't pay
  the invoice ("Payment failed", a 5xx, a timeout) is that mint's problem: its ecash is stashed and the rest go on;
- a mint still holding stashed ecash from a failed pay-back is not funded again until that ecash comes back,
  so a mint that can't pay out ties up at most one run's budget;
- mints may be tested in parallel: wallet calls and the counters are serialised by one lock;
- every problem becomes a funding alert, which fails the workflow so the owner is emailed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import nostr
from .client import Inconclusive, MintClient, MintError
from .nwc import NWC, NWCError
from .wallet import Proof, Wallet

MIN_PAYBACK_SAT = 3
RECOVER_NOW_SAT = 20  # smaller stashes are folded into that mint's next funded run instead
PAYBACK_MELT_TIMEOUT = 90  # a melt answers only once its Lightning payment settles, which can take a while
PAYBACK_SETTLE_WAIT = 120  # after a timeout or PENDING, keep asking the mint how the payment ended
MAX_PAYBACK_FAILURES = 3  # more than this many mints failing to pay back in one run looks like our bug: stop
_RETRYABLE = (NWCError, MintError, Inconclusive, OSError, TimeoutError, ConnectionError, httpx.TransportError)


class FundingStopped(Exception):
    pass


class Funder:
    _sleep = staticmethod(time.sleep)  # tests replace this to skip real waiting

    def __init__(self, nwc: NWC, max_sat_per_run: int = 1000, stash_dir: Path | None = None,
                 retry_waits: tuple = (30, 60)):
        self.nwc = nwc
        self.max = max_sat_per_run
        self.stash_dir = Path(stash_dir) if stash_dir else None
        self.retry_waits = retry_waits
        self.paid_out = 0
        self.paid_back = 0
        self.stopped: str | None = None
        self.alerts: list[str] = []
        self.payback_failures = 0
        self._lock = threading.RLock()

    def stop(self, reason: str):
        with self._lock:
            if not self.stopped:
                self.stopped = reason
            self.alerts.append(reason)

    # ---- paying in --------------------------------------------------------------------------------

    def pay(self, invoice: str, amount: int) -> int:
        """Used as the funded checks' on_invoice callback. Returns the sats it cost, fees included."""
        with self._lock:  # one payment at a time, so the cap holds when mints run in parallel
            if self.stopped:
                raise FundingStopped(f"funding stopped earlier in this run: {self.stopped}")
            if self.paid_out + amount > self.max:
                raise FundingStopped(f"per-run cap of {self.max} sat reached")
            try:
                res = self.nwc.pay_invoice(invoice)
            except (NWCError, OSError, TimeoutError, ConnectionError) as e:
                self.stop(f"wallet could not pay a funding invoice: {e}")
                raise FundingStopped(str(e)) from None
            cost = amount + (res.get("fees_paid") or 0) // 1000
            self.paid_out += cost
            return cost

    # ---- paying back ------------------------------------------------------------------------------

    def pay_back(self, wallet: Wallet, label: str, stash_on_failure: bool = True) -> dict:
        """Melt all leftover ecash to an invoice from the funding wallet, retrying before giving up."""
        out = {"leftover": wallet.balance, "returned": 0, "melt_fee": 0, "dust": wallet.balance, "ok": True}
        if wallet.balance < MIN_PAYBACK_SAT:
            return out
        last = None
        for attempt in range(len(self.retry_waits) + 1):
            if attempt:
                self._sleep(self.retry_waits[attempt - 1])
            try:
                res = self._melt_back(wallet, label)
                if res is not None:
                    out.update(res)
                    with self._lock:
                        self.paid_back += res["returned"]
                if wallet.balance and self.stash_dir:
                    self.stash(wallet, label)  # change too small to pay back: reused next run, not lost
                    out["change_kept"] = wallet.balance
                return out
            except Exception as e:  # noqa: BLE001  any failure here must end in a stash, never in lost sats
                last = e
                if not isinstance(e, _RETRYABLE):
                    break  # a bug, not a busy mint: retrying won't help
        out["ok"] = False
        why = f"{label}: pay-back failed after {len(self.retry_waits) + 1} attempts ({last})"
        if stash_on_failure and self.stash_dir:
            path = self.stash(wallet, label)
            out["stashed"] = True
            why += f"; {wallet.balance} sat stashed encrypted in {path.name}, the next funded run pays it back"
        elif not stash_on_failure:
            why += f"; {wallet.balance} sat stay in the stash for the next run"
        else:
            why += f"; {wallet.balance} sat left as ecash on the mint"
        mint_side = isinstance(last, (MintError, Inconclusive, httpx.TransportError)) and not isinstance(last, NWCError)
        with self._lock:
            if not mint_side:
                self.payback_failures += 1
            too_many = self.payback_failures >= MAX_PAYBACK_FAILURES
        unsafe = stash_on_failure and not out.get("stashed")  # the ecash is nowhere but on the mint
        if isinstance(last, NWCError) or unsafe or too_many:
            self.stop(why + ("" if not too_many else f"; {self.payback_failures} pay-backs failed this run"))
        else:
            with self._lock:
                self.alerts.append(why)  # this mint's problem: its ecash is safe, the other mints go on
        return out

    def _melt_back(self, wallet: Wallet, label: str) -> dict | None:
        c = wallet.client
        proofs = list(wallet.proofs)
        bal = sum(p.amount for p in proofs)
        in_fee = wallet.fee(proofs)
        target = bal - in_fee - max(2, bal // 50)
        for _ in range(4):
            if target < MIN_PAYBACK_SAT:
                return None  # too little to be worth paying back
            with self._lock:
                invoice = self.nwc.make_invoice(target, f"NutCheck leftover from {label}")
            q = c.melt_quote(invoice, wallet.unit)
            need = q["amount"] + (q.get("fee_reserve") or 0) + in_fee
            if need > bal:
                target -= need - bal
                continue
            blanks = wallet.outputs([1] * max((bal - q["amount"] - in_fee).bit_length(), 1))
            try:
                res = c.melt(q["quote"], [p.wire() for p in proofs], [o.wire() for o in blanks],
                             timeout=PAYBACK_MELT_TIMEOUT)
            except httpx.TimeoutException:
                # No answer yet is not a failure: the mint may still be paying. Retrying now would try to spend
                # the same proofs twice, so ask the mint how this payment ended instead.
                res = {"state": "PENDING"}
            state = res.get("state") or ("PAID" if res.get("paid") else "UNPAID")
            deadline = time.monotonic() + PAYBACK_SETTLE_WAIT
            while state == "PENDING" and time.monotonic() < deadline:
                time.sleep(3)
                res = c.get(f"/v1/melt/quote/bolt11/{q['quote']}")
                state = res.get("state", state)
            if state != "PAID":
                raise MintError(200, {"detail": f"pay-back melt ended {state}"})
            wallet.proofs = wallet.unblind(res.get("change") or [], blanks)
            return {"returned": q["amount"], "melt_fee": bal - q["amount"] - wallet.balance, "dust": wallet.balance}
        return None

    # ---- the stash: leftover ecash that could not be paid back yet ---------------------------------

    def _key(self) -> bytes:
        return hmac.new(b"nutcheck-stash-v1", self.nwc._secret.to_bytes(32, "big"), hashlib.sha256).digest()

    def stash(self, wallet: Wallet, label: str) -> Path:
        self.stash_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        body = {"mint": wallet.client.url, "unit": wallet.unit, "created": now.isoformat(timespec="seconds"),
                "amount": wallet.balance, "proofs": [p.wire() for p in wallet.proofs]}
        path = self.stash_dir / f"{label}-{now.strftime('%Y%m%dT%H%M%SZ')}.nutstash"
        path.write_text(nostr.nip44_encrypt(self._key(), json.dumps(body)) + "\n")
        return path

    def _open(self, path: Path):
        body = json.loads(nostr.nip44_decrypt(self._key(), path.read_text().strip()))
        wallet = Wallet(MintClient(body["mint"]), unit=body.get("unit", "sat"))
        wallet.load_keysets()
        proofs = [Proof(p["amount"], p["id"], p["secret"], p["C"]) for p in body["proofs"]]
        states = wallet.client.checkstate([p.Y for p in proofs])
        wallet.proofs = [p for p, st in zip(proofs, states) if st.get("state") == "UNSPENT"]
        return body, wallet

    def pending(self, mint_url: str) -> int:
        """Sats of this mint's ecash still waiting to be paid back (stashes too big to fold into a run)."""
        total = 0
        if not self.stash_dir or not self.stash_dir.exists():
            return 0
        for path in self.stash_dir.glob("*.nutstash"):
            try:
                body = json.loads(nostr.nip44_decrypt(self._key(), path.read_text().strip()))
            except Exception:  # noqa: BLE001
                continue
            if body["mint"].rstrip("/") == mint_url.rstrip("/") and body.get("amount", 0) >= RECOVER_NOW_SAT:
                total += body["amount"]
        return total

    def take_stash(self, mint_url: str) -> list[Proof]:
        """Unspent stashed proofs for this mint, removed from the stash (the caller now holds them)."""
        out = []
        if not self.stash_dir or not self.stash_dir.exists():
            return out
        for path in sorted(self.stash_dir.glob("*.nutstash")):
            try:
                body = json.loads(nostr.nip44_decrypt(self._key(), path.read_text().strip()))
                if body["mint"].rstrip("/") != mint_url.rstrip("/"):
                    continue
                _, wallet = self._open(path)
            except Exception:  # noqa: BLE001  leave it for recover() to report
                continue
            out += wallet.proofs
            path.unlink()
        return out

    def recover(self) -> list[str]:
        """Pay back every stashed token. Returns human-readable lines; deletes stash files that are settled."""
        lines = []
        if not self.stash_dir or not self.stash_dir.exists():
            return lines
        for path in sorted(self.stash_dir.glob("*.nutstash")):
            try:
                body = json.loads(nostr.nip44_decrypt(self._key(), path.read_text().strip()))
            except Exception as e:  # noqa: BLE001  e.g. the wallet secret was rotated
                lines.append(f"{path.name}: cannot decrypt ({e}); left in place")
                continue
            if body.get("amount", 0) < RECOVER_NOW_SAT:
                continue  # small change: folded into that mint's next funded run
            try:
                body, wallet = self._open(path)
            except _RETRYABLE as e:
                lines.append(f"{path.name}: mint not usable yet ({e}); will retry next run")
                continue
            if not wallet.proofs:
                path.unlink()
                lines.append(f"{path.name}: nothing left to recover (already spent); removed")
                continue
            res = self.pay_back(wallet, path.stem.split("-", 1)[0], stash_on_failure=False)
            if res["ok"]:
                path.unlink()
                lines.append(f"{path.name}: recovered {res['returned']} sat")
            else:
                lines.append(f"{path.name}: still failing; will retry next run")
        return lines
