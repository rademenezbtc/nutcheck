"""Checks that need a few sats of real ecash. Most of the budget comes back as leftover ecash."""
from __future__ import annotations

import json
import secrets
import time
from hashlib import sha256

from . import crypto
from .checks_free import claimed
from .client import Inconclusive, MintClient, MintError
from .model import Result, Severity as S, Status as St, publishable_exchange
from .secp import PrivateKey
from .wallet import Output, Proof, Wallet

LOCK_AMOUNT = 2  # big enough that 1 sat survives the input fee


class Ctx:
    def __init__(self, client: MintClient, wallet: Wallet, info: dict):
        self.client, self.wallet, self.info = client, wallet, info
        self.results: list[Result] = []

    def add(self, id, title, nut, sev, status, summary, **evidence):
        if status in (St.FAIL, St.WARN) and self.client.log:
            # the request that exposed the problem is the most recent one in every funded check
            evidence["exchange"] = publishable_exchange(self.client.log[-1])
        self.results.append(Result(id, title, nut, sev, status, summary, funded=True, evidence=evidence))

    def try_swap(self, inputs: list[Proof], outputs: list[Output]):
        """Returns (signatures | None, error | None)."""
        try:
            return self.client.swap([p.wire() for p in inputs], [o.wire() for o in outputs]), None
        except MintError as e:
            return None, e

    def spend_outputs(self, inputs: list[Proof], extra: int = 0) -> list[Output]:
        amt = sum(p.amount for p in inputs) - self.wallet.fee(inputs) + extra
        return self.wallet.outputs_for(amt) if amt > 0 else []


def _dleq_verdict(ctx: Ctx, proofs: list[Proof], where: str, nut12: bool):
    bad = [p.amount for p in proofs if p.dleq_ok is False]
    missing = [p.amount for p in proofs if p.dleq_ok is None]
    if bad:
        ctx.add(f"dleq.{where}", f"DLEQ proofs valid on {where}", "12", S.CRITICAL, St.FAIL,
                f"{len(bad)} of {len(proofs)} signatures carry a DLEQ proof that does not verify against the "
                "advertised key. The mint signed with a key it did not publish: the ecash may be unredeemable "
                "or tagged to identify you.", bad_amounts=bad)
    elif missing and nut12:
        ctx.add(f"dleq.{where}", f"DLEQ proofs valid on {where}", "12", S.MEDIUM, St.FAIL,
                f"Mint claims NUT-12 but sent {len(missing)} signature(s) without a DLEQ proof")
    elif missing:
        ctx.add(f"dleq.{where}", f"DLEQ proofs valid on {where}", "12", S.MEDIUM, St.WARN,
                "No DLEQ proofs: wallets cannot check the mint signed with its published keys")
    else:
        ctx.add(f"dleq.{where}", f"DLEQ proofs valid on {where}", "12", S.CRITICAL, St.PASS,
                f"{len(proofs)} DLEQ proof(s) verified")


def run(client: MintClient, wallet: Wallet, info: dict, budget: int, wait_s: float, on_invoice,
        expect_payment: bool = False) -> list[Result]:
    """expect_payment: the mint should be on real Lightning, so a quote must stay unpaid until NutCheck pays it.
    False for test mints and local FakeWallet mints, which mark every invoice paid by themselves."""
    ctx = Ctx(client, wallet, info)
    nut12 = claimed(info, "12")
    floor = mint_min(info, wallet.unit)
    if floor > 8:  # e.g. "Amount must be between 100 and 100000": fund enough for the internal melt too
        budget = max(budget, floor + 64)

    # ---------------------------------------------------------------- mint (NUT-04)
    try:
        q = client.mint_quote(budget, wallet.unit)
    except MintError as e:
        ctx.add("mint.quote", "Mint quote issued", "04", S.INFO, St.ERROR,
                f"Run incomplete: the mint could not create a Lightning invoice ({e}). Its Lightning backend may be "
                "down; that is an availability problem, which NutCheck does not grade. Funded checks were not run.")
        return ctx.results
    deadline = time.monotonic() + wait_s
    announced = False
    while True:
        st = client.mint_quote_state(q["quote"])
        if st.get("state") in ("PAID", "ISSUED") or st.get("paid") is True:
            if expect_payment and not announced:
                ctx.add("mint.requires_payment", "Issues ecash only for paid invoices", "04", S.CRITICAL, St.FAIL,
                        "The mint marked NutCheck's funding invoice as paid before anyone paid it, so it would issue "
                        "ecash for nothing. Its Lightning backend is fake or broken: anyone can mint ecash here for "
                        "free, so the ecash isn't backed by bitcoin. Fine for a test mint, never for one that holds "
                        "real funds. Paid checks were not run.")
                return ctx.results
            break
        if not announced:
            try:
                on_invoice(q["request"], budget)
            except Inconclusive:
                raise
            except Exception as e:  # noqa: BLE001  funding problems are not the mint's fault
                ctx.add("mint.funded", "Funding invoice paid", "04", S.INFO, St.SKIP,
                        f"Could not fund this run ({e}); funded checks skipped")
                return ctx.results
            announced = True
        if time.monotonic() > deadline:
            ctx.add("mint.funded", "Funding invoice paid", "04", S.INFO, St.SKIP,
                    "Funding invoice not paid in time; funded checks skipped")
            return ctx.results
        time.sleep(2)
    if expect_payment:
        ctx.add("mint.requires_payment", "Issues ecash only for paid invoices", "04", S.CRITICAL, St.PASS,
                "The funding quote stayed unpaid until NutCheck paid the invoice")
    outs = wallet.outputs(crypto.spread_amounts(budget))
    try:
        sigs = client.mint(q["quote"], [o.wire() for o in outs])
    except MintError as e:
        ctx.add("mint.issue", "Paid quote issues ecash", "04", S.CRITICAL, St.FAIL,
                f"Invoice was paid but the mint refused to issue ecash: {e}", quote=q["quote"])
        return ctx.results
    minted = wallet.unblind(sigs, outs)
    wrong = [(o.amount, s.get("amount")) for o, s in zip(outs, sigs) if o.amount != s.get("amount")]
    got = sum(p.amount for p in minted)
    ctx.add("mint.issue", "Paid quote issues the full amount", "04", S.CRITICAL,
            St.PASS if got == budget and not wrong and len(sigs) == len(outs) else St.FAIL,
            f"Paid {budget}, received {got} in {len(sigs)} signatures" + (f"; amount changed {wrong}" if wrong else ""))
    wallet.proofs += minted
    _dleq_verdict(ctx, minted, "mint", nut12)

    # replaying the quote must not issue twice
    try:
        client.mint(q["quote"], [o.wire() for o in wallet.outputs_for(budget)])
        ctx.add("mint.quote_replay", "Quote cannot be minted twice", "04", S.HIGH, St.FAIL,
                "The same paid quote issued ecash a second time: the mint's books cannot balance")
    except MintError as e:
        ctx.add("mint.quote_replay", "Quote cannot be minted twice", "04", S.HIGH, St.PASS,
                f"Second mint on the same quote refused ({e.code or e.status})")

    # ---------------------------------------------------------------- swap (NUT-03, NUT-02 fees)
    inputs = wallet.take(8)
    fee = wallet.fee(inputs)
    total = sum(p.amount for p in inputs)
    advertised = wallet.active.input_fee_ppk

    _, over_err = ctx.try_swap(inputs, wallet.outputs_for(total - fee + 1))
    ctx.add("swap.no_overspend", "Rejects outputs worth more than inputs", "03", S.CRITICAL,
            St.PASS if over_err else St.FAIL,
            f"Swap creating 1 sat out of nothing refused ({over_err.code or over_err.status})" if over_err
            else "Mint signed outputs worth more than the inputs minus fees: it can be drained by inflation")

    exact_outs = wallet.outputs_for(total - fee)
    sigs, err = ctx.try_swap(inputs, exact_outs)
    if err:
        ctx.add("swap.fee_exact", "Swap at the advertised fee succeeds", "02", S.HIGH, St.FAIL,
                f"Swap paying exactly the advertised fee ({advertised} ppk → {fee} sat for {len(inputs)} inputs) "
                f"was refused: {err}. Either the mint charges a different fee than it advertises, or it refuses "
                "to redeem valid ecash.")
        wallet.proofs += inputs  # might still be unspent
        return ctx.results
    swapped = wallet.unblind(sigs, exact_outs)
    ctx.add("swap.fee_exact", "Swap at the advertised fee succeeds", "02", S.HIGH, St.PASS,
            f"{len(inputs)} inputs, {advertised} ppk → {fee} sat fee, accepted")
    wallet.proofs += swapped
    _dleq_verdict(ctx, swapped, "swap", nut12)

    # double spend: re-submit the inputs we just spent
    _, ds_err = ctx.try_swap(inputs, wallet.outputs_for(total - fee))
    ctx.add("swap.double_spend", "Rejects double spends", "03", S.CRITICAL,
            St.PASS if ds_err else St.FAIL,
            f"Re-spending used proofs refused ({ds_err.code or ds_err.status})" if ds_err
            else "Mint accepted proofs it had already redeemed")

    # same proof twice in one request
    p = wallet.take(4)
    dup = [p[0], p[0]]
    _, dup_err = ctx.try_swap(dup, wallet.outputs_for(2 * p[0].amount - wallet.fee(dup)))
    ctx.add("swap.duplicate_input", "Rejects the same proof twice in one request", "03", S.CRITICAL,
            St.PASS if dup_err else St.FAIL,
            f"Duplicated input refused ({dup_err.code or dup_err.status})" if dup_err
            else "Mint counted one proof twice in a single swap")
    wallet.proofs += p if dup_err else []

    if claimed(info, "7"):
        try:
            states = client.checkstate([x.Y for x in inputs])
            ok = all(s.get("state") == "SPENT" for s in states)
            ctx.add("checkstate.spent", "Spent proofs are reported as spent", "07", S.MEDIUM,
                    St.PASS if ok else St.FAIL,
                    "All spent proofs report SPENT" if ok else f"States after spending: {[s.get('state') for s in states]}")
        except MintError as e:
            ctx.add("checkstate.spent", "Spent proofs are reported as spent", "07", S.MEDIUM, St.FAIL, str(e))

    # ---------------------------------------------------------------- P2PK (NUT-11) and HTLC (NUT-14)
    _locks(ctx)

    # ---------------------------------------------------------------- melt + fee return (NUT-05, NUT-08)
    _melt(ctx)
    return ctx.results


# ------------------------------------------------------------------------------------------------
def _secret(kind: str, data: str, tags: list[list[str]]) -> str:
    return json.dumps([kind, {"nonce": secrets.token_hex(16), "data": data, "tags": tags}], separators=(",", ":"))


def sig_all_message(inputs: list[Proof], outputs: list[Output], quote_id: str = "") -> str:
    """NUT-11 SIG_ALL message: secret and C of every input, then amount and B_ of every output (+ quote for melt)."""
    return ("".join(p.secret + p.C for p in inputs)
            + "".join(str(o.amount) + o.blinded.B_hex for o in outputs) + quote_id)


def sig_all_message_legacy(inputs: list[Proof], outputs: list[Output]) -> str:
    """SIG_ALL message before cashubtc/nuts#302 (Nov 2025): every input's secret, then every output's B_."""
    return "".join(p.secret for p in inputs) + "".join(o.blinded.B_hex for o in outputs)


def _lock_view(secret: str) -> str:
    """The lock as evidence: its kind and tags, with the nonce removed and keys shortened.
    Without the nonce and the mint's signature it identifies and spends nothing."""
    kind, body = json.loads(secret)
    short = lambda x: x[:8] + "…" + x[-6:] if len(x) > 20 else x
    return json.dumps([kind, {"nonce": "…", "data": short(body["data"]),
                              "tags": [[t[0]] + [short(x) for x in t[1:]] for t in body.get("tags", [])]}])


def _witness(sigs: list[str] | None = None, preimage: str | None = None) -> str:
    w = {}
    if preimage is not None:
        w["preimage"] = preimage
    w["signatures"] = sigs or []
    return json.dumps(w, separators=(",", ":"))


def _locks(ctx: Ctx):
    w = ctx.wallet
    p2pk, htlc = claimed(ctx.info, "11"), claimed(ctx.info, "14")
    if not (p2pk or htlc):
        ctx.add("p2pk.enforced", "P2PK locks enforced", "11", S.INFO, St.SKIP, "Mint does not claim NUT-11")
        return
    A, B, R = PrivateKey(), PrivateKey(), PrivateKey()
    now = int(time.time())
    preimage = secrets.token_hex(32)
    hashlock = sha256(bytes.fromhex(preimage)).hexdigest()

    specs = {}
    if p2pk:
        specs |= {
            "basic": _secret("P2PK", A.pubkey_hex, []),
            "multisig": _secret("P2PK", A.pubkey_hex, [["pubkeys", B.pubkey_hex], ["n_sigs", "2"]]),
            "active_lock": _secret("P2PK", A.pubkey_hex, [["locktime", str(now + 86400)], ["refund", R.pubkey_hex]]),
            "expired_lock": _secret("P2PK", A.pubkey_hex, [["locktime", str(now - 86400)], ["refund", R.pubkey_hex]]),
            "malformed": _secret("P2PK", A.pubkey_hex, [["locktime", str(now + 86400)], ["locktime", str(now + 1)]]),
            # SIG_ALL: two inputs with the same data and tags; one signature in the first covers the whole swap
            "sig_all_0": _secret("P2PK", A.pubkey_hex, [["sigflag", "SIG_ALL"]]),
            "sig_all_1": _secret("P2PK", A.pubkey_hex, [["sigflag", "SIG_ALL"]]),
        }
    if htlc:
        specs |= {
            "htlc_plain": _secret("HTLC", hashlock, []),
            "htlc_sig": _secret("HTLC", hashlock, [["pubkeys", A.pubkey_hex]]),
            # sender pathway (NUT-14): the refund key alone, no preimage, only once the locktime has passed
            "htlc_active": _secret("HTLC", hashlock, [["pubkeys", A.pubkey_hex], ["locktime", str(now + 86400)],
                                                      ["refund", R.pubkey_hex]]),
            "htlc_expired": _secret("HTLC", hashlock, [["pubkeys", A.pubkey_hex], ["locktime", str(now - 86400)],
                                                       ["refund", R.pubkey_hex]]),
        }
    names = list(specs)
    try:
        need = LOCK_AMOUNT * len(names)
        ins = w.take(need + 1)
        fee = w.fee(ins)
        if sum(p.amount for p in ins) - fee < need:
            w.proofs += ins
            ins = w.take(need + fee)
            fee = w.fee(ins)
        lock_outs = w.outputs([LOCK_AMOUNT] * len(names), [specs[n] for n in names])
        change = w.outputs_for(sum(p.amount for p in ins) - fee - need)
        sigs = ctx.client.swap([p.wire() for p in ins], [o.wire() for o in lock_outs + change])
        proofs = w.unblind(sigs, lock_outs + change)
        locked = dict(zip(names, proofs[: len(names)]))
        w.proofs += proofs[len(names):]
    except MintError as e:
        ctx.add("locks.create", "Locked ecash can be created", "10", S.HIGH, St.FAIL,
                f"Mint refused outputs with NUT-10 secrets: {e}")
        return

    def attempt(proof: Proof, witness: str | None):
        q = Proof(proof.amount, proof.id, proof.secret, proof.C, witness)
        outs = ctx.spend_outputs([q])
        sigs, err = ctx.try_swap([q], outs)
        if sigs:
            w.proofs += w.unblind(sigs, outs)
        return err is None, err

    def attempt_many(proofs: list[Proof], witnesses: list[str | None], outs: list[Output] | None = None):
        """Spend several locked proofs in one swap (default outputs: fresh ones worth inputs minus fee)."""
        qs = [Proof(p.amount, p.id, p.secret, p.C, wit) for p, wit in zip(proofs, witnesses)]
        outs = outs if outs is not None else ctx.spend_outputs(qs)
        sigs, err = ctx.try_swap(qs, outs)
        if sigs:
            w.proofs += w.unblind(sigs, outs)
        return err is None, err

    def scenario(id, title, nut, reject_cases, accept_case, proof: Proof, leak_severity=S.CRITICAL, leak_summary=None):
        """reject_cases: list of (label, witness). accept_case: (label, witness) control that must succeed.
        leak_severity/leak_summary: what it means if a reject case is accepted (default: anyone could take it)."""
        leaked = []
        for label, wit in reject_cases:
            ok, _ = attempt(proof, wit)
            if ok:
                leaked.append(label)
                break  # proof is gone
        if leaked:
            ctx.add(id, title, nut, leak_severity, St.FAIL,
                    leak_summary or f"Locked ecash was spent with: {leaked[0]}. Anyone could have taken it.",
                    lock=_lock_view(proof.secret))
            return
        if accept_case is None:
            ctx.add(id, title, nut, leak_severity, St.PASS, f"Refused: {', '.join(l for l, _ in reject_cases)}")
            return
        ok, err = attempt(proof, accept_case[1])
        if ok:
            ctx.add(id, title, nut, S.CRITICAL, St.PASS,
                    f"Refused: {', '.join(l for l, _ in reject_cases)}; accepted: {accept_case[0]}")
        else:
            ctx.add(id, title, nut, S.MEDIUM, St.FAIL,
                    f"Refused the invalid spends, but also refused the valid one ({accept_case[0]}): {err}. "
                    "Locked ecash may be unspendable.")

    if p2pk:
        s = locked["basic"].secret
        scenario("p2pk.basic", "P2PK lock enforced", "11", [
            ("no signature", None), ("empty witness", _witness([])),
            ("signature from the wrong key", _witness([B.sign(s.encode())])),
        ], ("signature from the locking key", _witness([A.sign(s.encode())])), locked["basic"])

        s = locked["multisig"].secret
        scenario("p2pk.multisig", "P2PK 2-of-2 multisig enforced", "11", [
            ("1 of 2 signatures", _witness([A.sign(s.encode())])),
            ("the same key signing twice", _witness([A.sign(s.encode()), A.sign(s.encode())])),
        ], ("2 of 2 signatures", _witness([A.sign(s.encode()), B.sign(s.encode())])), locked["multisig"])

        s = locked["active_lock"].secret
        scenario("p2pk.locktime", "Refund key blocked before locktime", "11", [
            ("refund key before locktime", _witness([R.sign(s.encode())])),
        ], ("locking key", _witness([A.sign(s.encode())])), locked["active_lock"])

        s = locked["expired_lock"].secret
        ok, err = attempt(locked["expired_lock"], _witness([R.sign(s.encode())]))
        ctx.add("p2pk.refund", "Refund key works after locktime", "11", S.MEDIUM,
                St.PASS if ok else St.FAIL,
                "Refund key spent the expired lock" if ok
                else f"Refund key could not spend an expired lock: {err}. Senders cannot reclaim timed-out payments.")

        s = locked["malformed"].secret
        # The spend carries a valid signature from the locking key, so accepting it is not theft. The risk is
        # ambiguity: NUT-11 says duplicate tags make the secret unspendable, and implementations that pick
        # different copies disagree on when the refund path opens.
        scenario("p2pk.malformed", "Malformed lock (duplicate tag) rejected", "11", [
            ("valid signature on a secret with two locktime tags", _witness([A.sign(s.encode())])),
        ], None, locked["malformed"], leak_severity=S.MEDIUM, leak_summary=(
            "Accepted a P2PK secret with two locktime tags. NUT-11 says a duplicated tag makes the secret malformed "
            "and it MUST be rejected. Wallets and mints that read different copies of the tag disagree on when the "
            "refund key can spend, so a refund could open earlier than the receiver expects."))

        # SIG_ALL (NUT-11): the signature must commit to every input and every output of the swap.
        ins = [locked["sig_all_0"], locked["sig_all_1"]]
        outs = ctx.spend_outputs(ins)
        sig_all = lambda o: A.sign(sig_all_message(ins, o).encode())
        tampered = ctx.spend_outputs(ins)  # same amounts, different blinded messages
        cases = [
            ("a separate signature on each input's secret (SIG_INPUTS style)",
             [_witness([A.sign(p.secret.encode())]) for p in ins], None),
            ("a SIG_ALL signature whose outputs were swapped after signing",
             [_witness([sig_all(outs)]), None], tampered),
        ]
        leaked = None
        for label, wits, o in cases:
            ok, _ = attempt_many(ins, wits, o)
            if ok:
                leaked = label
                break
        if leaked:
            ctx.add("p2pk.sig_all", "SIG_ALL signature covers the outputs", "11", S.HIGH, St.FAIL,
                    f"Accepted SIG_ALL inputs spent with {leaked}. The signature did not commit to the outputs, so "
                    "anyone who sees a signed SIG_ALL transaction (a relay, a coordinator, a payment server) could "
                    "redirect it to their own outputs.", lock=_lock_view(ins[0].secret))
        else:
            ok, err = attempt_many(ins, [_witness([sig_all(outs)]), None], outs)
            if ok:
                ctx.add("p2pk.sig_all", "SIG_ALL signature covers the outputs", "11", S.HIGH, St.PASS,
                        f"Refused: {'; '.join(c[0] for c in cases)}; accepted: one SIG_ALL signature over all inputs "
                        "and outputs, in the first input")
            elif (legacy := ctx.spend_outputs(ins)) and attempt_many(
                    ins, [_witness([A.sign(sig_all_message_legacy(ins, legacy).encode())]), None], legacy)[0]:
                ctx.add("p2pk.sig_all", "SIG_ALL signature covers the outputs", "11", S.LOW, St.WARN,
                        "The SIG_ALL lock is enforced, but the mint only accepts the message format from before "
                        "November 2025 (secrets and B_ only) and refused the current one, which adds each input's C "
                        f"and each output's amount ({err}). Wallets following the current NUT-11 can't spend SIG_ALL "
                        "ecash here.", lock=_lock_view(ins[0].secret))
            else:
                ctx.add("p2pk.sig_all", "SIG_ALL signature covers the outputs", "11", S.MEDIUM, St.FAIL,
                        "Refused the invalid spends, but also refused a correct SIG_ALL spend (one signature over "
                        f"all inputs and outputs, in the first input): {err}. SIG_ALL ecash may be unspendable.",
                        lock=_lock_view(ins[0].secret))

    if htlc:
        scenario("htlc.hashlock", "HTLC hash lock enforced", "14", [
            ("no preimage", _witness([])),
            ("wrong preimage", _witness([], secrets.token_hex(32))),
        ], ("correct preimage", _witness([], preimage)), locked["htlc_plain"])
        s = locked["htlc_sig"].secret
        scenario("htlc.receiver_sig", "HTLC receiver signature enforced", "14", [
            ("preimage without the receiver's signature", _witness([], preimage)),
            ("preimage with the wrong signature", _witness([B.sign(s.encode())], preimage)),
        ], ("preimage and receiver signature", _witness([A.sign(s.encode())], preimage)), locked["htlc_sig"])

        s = locked["htlc_active"].secret
        scenario("htlc.locktime", "HTLC refund key blocked before locktime", "14", [
            ("refund key before locktime, no preimage", _witness([R.sign(s.encode())])),
        ], ("preimage and receiver signature", _witness([A.sign(s.encode())], preimage)), locked["htlc_active"])

        # Sender pathway: once the locktime has passed, the refund key alone spends the HTLC (no preimage).
        p = locked["htlc_expired"]
        s = p.secret
        ok, err = attempt(p, _witness([R.sign(s.encode())]))
        if ok:
            ctx.add("htlc.refund", "HTLC refund works after locktime", "14", S.MEDIUM, St.PASS,
                    "Refund key alone (no preimage) spent the expired HTLC")
        else:
            # The receiver pathway stays open after the locktime, so take the sats back that way.
            back, _ = attempt(p, _witness([A.sign(s.encode())], preimage))
            ctx.add("htlc.refund", "HTLC refund works after locktime", "14", S.MEDIUM, St.FAIL,
                    f"Refund key could not spend an expired HTLC without a preimage: {err}. NUT-14 lets the sender "
                    "reclaim with the refund signature alone once the locktime passes; on this mint a sender whose "
                    "payment was never claimed cannot get it back."
                    + ("" if back else " The receiver pathway (preimage and signature) also failed."),
                    lock=_lock_view(s))


def _melt(ctx: Ctx):
    w, c = ctx.wallet, ctx.client
    if not claimed(ctx.info, "5"):
        return
    amount = max(8, mint_min(ctx.info, w.unit))  # the smallest invoice this mint will issue
    # NUT-20: lock this internal quote to a key, so claiming it back below doubles as the quote-lock test
    qkey = PrivateKey() if claimed(ctx.info, "20") else None
    try:
        # internal invoice: the mint pays itself, Lightning fee is 0
        mq = c.mint_quote(amount, w.unit, qkey.pubkey_hex if qkey else None)
        melt_q = c.melt_quote(mq["request"], w.unit)
    except MintError as e:
        ctx.add("melt.quote", "Melt quote issued", "05", S.INFO, St.ERROR,
                f"Run incomplete: the mint could not quote paying an invoice ({e}). Melt checks were not run.")
        return
    reserve = melt_q.get("fee_reserve", 0) or 0
    q_amount = melt_q["amount"]
    # deliberately overpay by 8 sat so change is owed even when fee_reserve is 0
    inputs = w.take(q_amount + reserve + 8 + 2)
    in_fee = w.fee(inputs)
    paid_in = sum(p.amount for p in inputs)
    overpay = paid_in - in_fee - q_amount
    blanks = w.outputs([1] * max(overpay.bit_length(), 1))  # enough blanks to hold any change up to overpay
    try:
        res = c.melt(melt_q["quote"], [p.wire() for p in inputs], [o.wire() for o in blanks])
    except MintError as e:
        ctx.add("melt.pays", "Melt pays the invoice", "05", S.HIGH, St.FAIL, f"Melt refused: {e}")
        w.proofs += inputs
        return
    state = res.get("state") or ("PAID" if res.get("paid") else "UNPAID")
    ctx.add("melt.pays", "Melt pays the invoice", "05", S.HIGH,
            St.PASS if state == "PAID" else St.FAIL, f"Melt state: {state} (fee_reserve {reserve})")

    change_sigs = res.get("change") or []
    change = w.unblind(change_sigs, blanks[: len(change_sigs)])
    got = sum(p.amount for p in change)
    owed = paid_in - in_fee - q_amount  # internal payment: Lightning fee is zero
    w.proofs += change
    ctx.add("melt.fee_return", "Overpaid fees are returned", "08", S.HIGH,
            St.PASS if got == owed else (St.WARN if got > 0 else St.FAIL),
            f"Overpaid by {owed} sat (internal payment, no Lightning fee); {got} sat returned as change"
            + ("" if got == owed else f"; {owed - got} sat kept by the mint"),
            fee_reserve=reserve, inputs=paid_in, input_fee=in_fee, quote_amount=q_amount, returned=got)
    if change:
        _dleq_verdict(ctx, change, "melt_change", claimed(ctx.info, "12"))

    # the internal invoice should now be paid: claim it back
    try:
        st = c.mint_quote_state(mq["quote"])
        paid = st.get("state") == "PAID" or st.get("paid")
    except MintError:
        paid = False
    if not paid:
        return
    if qkey:
        _quote_lock(ctx, mq, qkey, amount)
        return
    try:
        outs = w.outputs_for(amount)
        w.proofs += w.unblind(c.mint(mq["quote"], [o.wire() for o in outs]), outs)
    except MintError:
        pass


def mint_min(info: dict, unit: str) -> int:
    """The smallest bolt11 mint quote this mint accepts in `unit` (NUT-04 min_amount), 0 if it sets none."""
    for m in info.get("nuts", {}).get("4", {}).get("methods", []):
        if m.get("method") == "bolt11" and m.get("unit") == unit:
            return int(m.get("min_amount") or 0)
    return 0


def nut20_message(quote: str, outputs: list[Output]) -> bytes:
    """NUT-20 message (v1, May 2026): domain tag, then length-prefixed quote id, amounts and B_ bytes."""
    lp = lambda b: len(b).to_bytes(4, "big") + b
    msg = b"Cashu_MintQuoteSig_v1" + lp(quote.encode())
    for o in outputs:
        amt = o.amount.to_bytes((o.amount.bit_length() + 7) // 8, "big") if o.amount else b""
        msg += lp(amt) + lp(bytes.fromhex(o.blinded.B_hex))
    return msg


def nut20_message_legacy(quote: str, outputs: list[Output]) -> bytes:
    """NUT-20 message before May 2026: the quote id followed by every B_ as hex text."""
    return (quote + "".join(o.blinded.B_hex for o in outputs)).encode()


def _quote_lock(ctx: Ctx, mq: dict, key: PrivateKey, amount: int):
    """NUT-20: a quote created with a pubkey must only mint with a signature from that key."""
    w, c = ctx.wallet, ctx.client
    title, quote = "Locked mint quote needs its signature", mq["quote"]
    echoed = mq.get("pubkey")

    def mint(sig):
        outs = w.outputs_for(amount)
        try:
            w.proofs += w.unblind(c.mint(quote, [o.wire() for o in outs], sig(outs) if sig else None), outs)
            return True, None
        except MintError as e:
            return False, e

    for label, sig in [("no signature", None),
                       ("a signature from the wrong key", lambda o: PrivateKey().sign(nut20_message(quote, o)))]:
        ok, _ = mint(sig)
        if ok:
            ctx.add("mint.quote_lock", title, "20", S.HIGH, St.FAIL,
                    f"Mint claims NUT-20 but issued a paid, key-locked quote with {label}"
                    + ("" if echoed else " (its quote response also dropped the pubkey)")
                    + ". Anyone who learns a quote ID, from logs, a shared screen or a leaky wallet, can mint the "
                    "payment for themselves.", quote_pubkey_echoed=bool(echoed))
            return
    ok, err = mint(lambda o: key.sign(nut20_message(quote, o)))
    if ok:
        ctx.add("mint.quote_lock", title, "20", S.HIGH, St.PASS,
                "Refused: no signature, a signature from the wrong key; accepted: the quote key's signature")
        return
    ok, _ = mint(lambda o: key.sign(nut20_message_legacy(quote, o)))
    if ok:
        ctx.add("mint.quote_lock", title, "20", S.LOW, St.WARN,
                "The lock is enforced, but the mint only accepts the pre-May-2026 signature format and refused the "
                f"current one ({err}). Up-to-date wallets cannot mint their locked quotes here.")
    else:
        ctx.add("mint.quote_lock", title, "20", S.MEDIUM, St.FAIL,
                f"Refused minting without a valid signature, but also refused the quote key's own signature ({err}), "
                f"in both the current and the old format. A paid, locked quote cannot be redeemed ({amount} sat).")
