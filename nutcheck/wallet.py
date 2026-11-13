"""A deliberately small wallet: just enough to mint, swap, lock and melt for the tests.

It verifies every signature it receives (DLEQ) and records failures rather than
raising, so checks can grade them.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field

from . import crypto, secp
from .client import MintClient


@dataclass
class Proof:
    amount: int
    id: str
    secret: str
    C: str
    witness: str | None = None
    dleq_ok: bool | None = None  # None = mint sent no DLEQ

    def wire(self) -> dict:
        d = {"amount": self.amount, "id": self.id, "secret": self.secret, "C": self.C}
        if self.witness is not None:
            d["witness"] = self.witness
        return d

    @property
    def Y(self) -> str:
        return secp.encode_hex(crypto.hash_to_curve(self.secret.encode()))


@dataclass
class Output:
    amount: int
    keyset_id: str
    blinded: crypto.Blinded

    def wire(self) -> dict:
        return {"amount": self.amount, "id": self.keyset_id, "B_": self.blinded.B_hex}


@dataclass
class Keyset:
    id: str
    unit: str
    active: bool
    input_fee_ppk: int
    keys: dict[int, str]  # amount -> hex pubkey
    final_expiry: int | None = None


class FundingTimeout(Exception):
    pass


@dataclass
class Wallet:
    client: MintClient
    unit: str = "sat"
    keysets: dict[str, Keyset] = field(default_factory=dict)
    proofs: list[Proof] = field(default_factory=list)
    signature_problems: list[str] = field(default_factory=list)

    def load_keysets(self):
        meta = {k["id"]: k for k in self.client.keysets()}
        for ks in self.client.keys():
            m = meta.get(ks["id"], {})
            self.keysets[ks["id"]] = Keyset(
                id=ks["id"], unit=ks.get("unit", m.get("unit")),
                active=m.get("active", ks.get("active", True)),
                input_fee_ppk=m.get("input_fee_ppk", ks.get("input_fee_ppk", 0)) or 0,
                keys={int(a): v for a, v in ks["keys"].items()},
                final_expiry=m.get("final_expiry", ks.get("final_expiry")),
            )
        return self.keysets

    @property
    def active(self) -> Keyset:
        for ks in self.keysets.values():
            if ks.active and ks.unit == self.unit:
                return ks
        raise RuntimeError(f"mint has no active {self.unit} keyset")

    def fee(self, proofs: list[Proof]) -> int:
        return crypto.fee_for([self.keysets[p.id].input_fee_ppk if p.id in self.keysets else 0 for p in proofs])

    # ---- outputs / signatures ------------------------------------------------

    def outputs(self, amounts: list[int], secrets: list[str] | None = None) -> list[Output]:
        ks = self.active
        secrets = secrets or [crypto.random_secret() for _ in amounts]
        return [Output(a, ks.id, crypto.blind(s)) for a, s in zip(amounts, secrets)]

    def outputs_for(self, total: int) -> list[Output]:
        return self.outputs(crypto.split_amount(total))

    def unblind(self, sigs: list[dict], outs: list[Output]) -> list[Proof]:
        """Turn BlindSignatures into Proofs, checking DLEQ for each. Order must match outputs."""
        proofs = []
        for sig, out in zip(sigs, outs):
            ks = self.keysets.get(sig["id"])
            if ks is None:
                self.signature_problems.append(f"signature uses unknown keyset {sig['id']}")
                continue
            amount = sig["amount"]
            if amount not in ks.keys:
                self.signature_problems.append(f"signature amount {amount} has no key")
                continue
            K = secp.decode(ks.keys[amount])
            C_ = secp.decode(sig["C_"])
            dleq_ok = None
            if sig.get("dleq"):
                dleq_ok = crypto.verify_dleq_blind(
                    K, out.blinded.B_, C_, int(sig["dleq"]["e"], 16), int(sig["dleq"]["s"], 16))
            C = crypto.unblind(C_, out.blinded.r, K)
            proofs.append(Proof(amount, sig["id"], out.blinded.secret, secp.encode_hex(C), dleq_ok=dleq_ok))
        return proofs

    # ---- funding -------------------------------------------------------------

    def fund(self, amount: int, wait_s: float = 600, on_invoice=None) -> list[Proof]:
        q = self.client.mint_quote(amount, self.unit)
        deadline = time.monotonic() + wait_s
        announced = False
        while True:
            st = self.client.mint_quote_state(q["quote"])
            if st.get("state") == "PAID" or st.get("paid") is True:
                break
            if not announced and on_invoice:
                on_invoice(q["request"], amount)
                announced = True
            if time.monotonic() > deadline:
                raise FundingTimeout(q["quote"])
            time.sleep(2)
        outs = self.outputs_for(amount)
        sigs = self.client.mint(q["quote"], [o.wire() for o in outs])
        proofs = self.unblind(sigs, outs)
        self.proofs += proofs
        return proofs

    # ---- selection -----------------------------------------------------------

    def take(self, amount: int) -> list[Proof]:
        """Remove and return proofs covering at least `amount` (greedy, largest first)."""
        chosen, total = [], 0
        for p in sorted(self.proofs, key=lambda p: -p.amount):
            if total >= amount:
                break
            chosen.append(p)
            total += p.amount
        if total < amount:
            raise RuntimeError(f"insufficient balance: need {amount}, have {self.balance}")
        for p in chosen:
            self.proofs.remove(p)
        return chosen

    def make_exact(self, amounts: list[int]) -> list[Proof]:
        """Swap to obtain proofs of exactly these amounts (returned, not kept in the wallet)."""
        need = sum(amounts)
        # fee depends on number of inputs; iterate until stable
        fee = 1
        for _ in range(5):
            inputs = self.take(need + fee)
            real_fee = self.fee(inputs)
            if real_fee <= fee:
                break
            self.proofs += inputs
            fee = real_fee
        change_amt = sum(p.amount for p in inputs) - real_fee - need
        outs = self.outputs(amounts) + self.outputs(crypto.split_amount(change_amt))
        sigs = self.client.swap([p.wire() for p in inputs], [o.wire() for o in outs])
        proofs = self.unblind(sigs, outs)
        self.proofs += proofs[len(amounts):]
        return proofs[: len(amounts)]

    @property
    def balance(self) -> int:
        return sum(p.amount for p in self.proofs)

    def export_token(self) -> str:
        """cashuA (v3) token of the remaining proofs, so leftover funds are never lost."""
        tok = {"token": [{"mint": self.client.url, "proofs": [p.wire() for p in self.proofs]}], "unit": self.unit}
        return "cashuA" + base64.urlsafe_b64encode(json.dumps(tok).encode()).decode().rstrip("=")
