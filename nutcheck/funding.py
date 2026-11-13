"""Paying for funded checks with a real wallet, and paying the leftovers back.

Safety rules:
- a hard cap on sats paid out per sweep (`nwc_max_sat_per_run`, default 1000);
- after any failed pay-back or wallet error, no further mint is funded in that sweep;
- every problem becomes a funding alert, which fails the workflow so the owner is emailed.
"""
from __future__ import annotations

import time

from .client import MintError
from .nwc import NWC, NWCError
from .wallet import Wallet

MIN_PAYBACK_SAT = 3


class FundingStopped(Exception):
    pass


class Funder:
    def __init__(self, nwc: NWC, max_sat_per_run: int = 1000):
        self.nwc = nwc
        self.max = max_sat_per_run
        self.paid_out = 0
        self.paid_back = 0
        self.stopped: str | None = None
        self.alerts: list[str] = []

    def stop(self, reason: str):
        if not self.stopped:
            self.stopped = reason
        self.alerts.append(reason)

    def pay(self, invoice: str, amount: int):
        """Used as the funded checks' on_invoice callback."""
        if self.stopped:
            raise FundingStopped(f"funding stopped earlier in this run: {self.stopped}")
        if self.paid_out + amount > self.max:
            raise FundingStopped(f"per-run cap of {self.max} sat reached")
        try:
            res = self.nwc.pay_invoice(invoice)
        except (NWCError, OSError, TimeoutError, ConnectionError) as e:
            self.stop(f"wallet could not pay a funding invoice: {e}")
            raise FundingStopped(str(e)) from None
        self.paid_out += amount + (res.get("fees_paid") or 0) // 1000

    def pay_back(self, wallet: Wallet, label: str) -> dict:
        """Melt all leftover ecash to an invoice from the funding wallet."""
        bal = wallet.balance
        out = {"leftover": bal, "returned": 0, "melt_fee": 0, "dust": bal, "ok": True}
        if bal < MIN_PAYBACK_SAT:
            return out
        c = wallet.client
        proofs = list(wallet.proofs)
        in_fee = wallet.fee(proofs)
        target = bal - in_fee - max(2, bal // 50)
        for _ in range(4):
            if target < MIN_PAYBACK_SAT:
                break
            try:
                invoice = self.nwc.make_invoice(target, f"NutCheck leftover from {label}")
                q = c.melt_quote(invoice, wallet.unit)
            except (NWCError, MintError, OSError, TimeoutError, ConnectionError) as e:
                return self._failed(out, label, f"could not prepare pay-back: {e}")
            need = q["amount"] + (q.get("fee_reserve") or 0) + in_fee
            if need > bal:
                target -= need - bal
                continue
            blanks = wallet.outputs([1] * max((bal - q["amount"] - in_fee).bit_length(), 1))
            try:
                res = c.melt(q["quote"], [p.wire() for p in proofs], [o.wire() for o in blanks])
            except MintError as e:
                return self._failed(out, label, f"mint refused the pay-back melt: {e}")
            state = res.get("state") or ("PAID" if res.get("paid") else "UNPAID")
            deadline = time.monotonic() + 60
            while state == "PENDING" and time.monotonic() < deadline:
                time.sleep(3)
                try:
                    res = c.get(f"/v1/melt/quote/bolt11/{q['quote']}")
                    state = res.get("state", state)
                except MintError:
                    pass
            if state != "PAID":
                return self._failed(out, label, f"pay-back melt ended {state}")
            change = wallet.unblind(res.get("change") or [], blanks)
            wallet.proofs = change
            out.update(returned=q["amount"], melt_fee=bal - q["amount"] - wallet.balance, dust=wallet.balance)
            self.paid_back += q["amount"]
            return out
        return out  # too little to be worth paying back

    def _failed(self, out: dict, label: str, why: str) -> dict:
        self.stop(f"{label}: {why} ({out['leftover']} sat left on the mint as ecash)")
        out["ok"] = False
        return out
