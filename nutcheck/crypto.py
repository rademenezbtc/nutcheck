"""Cashu crypto, written from the NUT specs: NUT-00 BDHKE, NUT-02 keyset IDs, NUT-12 DLEQ."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from . import secp
from .secp import N, G

DOMAIN_SEPARATOR = b"Secp256k1_HashToCurve_Cashu_"


def hash_to_curve(msg: bytes) -> secp.Point:
    msg_hash = hashlib.sha256(DOMAIN_SEPARATOR + msg).digest()
    for counter in range(2**16):
        h = hashlib.sha256(msg_hash + counter.to_bytes(4, "little")).digest()
        try:
            return secp.decode(b"\x02" + h)
        except secp.InvalidPoint:
            continue
    raise ValueError("no point found")


@dataclass
class Blinded:
    secret: str
    r: int
    B_: secp.Point

    @property
    def B_hex(self) -> str:
        return secp.encode_hex(self.B_)


def blind(secret: str, r: int | None = None) -> Blinded:
    r = r if r is not None else secrets.randbelow(N - 1) + 1
    Y = hash_to_curve(secret.encode("utf-8"))
    return Blinded(secret, r, secp.add(Y, secp.mul(r, G)))


def unblind(C_: secp.Point, r: int, K: secp.Point) -> secp.Point:
    return secp.add(C_, secp.neg(secp.mul(r, K)))


def hash_e(*points: secp.Point) -> int:
    s = "".join(secp.encode_hex(p, compressed=False) for p in points)
    return int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest(), "big")


def verify_dleq_blind(A: secp.Point, B_: secp.Point, C_: secp.Point, e: int, s: int) -> bool:
    """NUT-12: proves C_ = a*B_ for the same a as A = a*G."""
    R1 = secp.add(secp.mul(s, G), secp.neg(secp.mul(e, A)))
    R2 = secp.add(secp.mul(s, B_), secp.neg(secp.mul(e, C_)))
    if R1 is None or R2 is None:
        return False
    return hash_e(R1, R2, A, C_) == e


def verify_dleq_proof(A: secp.Point, secret: str, C: secp.Point, e: int, s: int, r: int) -> bool:
    """NUT-12 Carol-side check: rebuild B_ and C_ from the unblinded proof and r."""
    Y = hash_to_curve(secret.encode("utf-8"))
    B_ = secp.add(Y, secp.mul(r, G))
    C_ = secp.add(C, secp.mul(r, A))
    return verify_dleq_blind(A, B_, C_, e, s)


def keyset_id_v1(keys: dict[int, str]) -> str:
    """Deprecated '00' keyset IDs."""
    blob = b"".join(bytes.fromhex(keys[a]) for a in sorted(keys))
    return "00" + hashlib.sha256(blob).hexdigest()[:14]


def keyset_id_v2(keys: dict[int, str], unit: str, input_fee_ppk: int | None, final_expiry: int | None) -> str:
    blob = b",".join(f"{a}:{keys[a].lower()}".encode() for a in sorted(keys))
    blob += f"|unit:{unit}".encode()
    if input_fee_ppk:
        blob += f"|input_fee_ppk:{input_fee_ppk}".encode()
    if final_expiry:
        blob += f"|final_expiry:{final_expiry}".encode()
    return "01" + hashlib.sha256(blob).hexdigest()


def fee_for(inputs_keyset_fees: list[int]) -> int:
    """NUT-02 input fee: sum of per-input ppk, rounded up."""
    return (sum(inputs_keyset_fees) + 999) // 1000


def split_amount(amount: int) -> list[int]:
    return [1 << i for i in range(amount.bit_length()) if amount >> i & 1]


def spread_amounts(amount: int) -> list[int]:
    """Wallet-like denominations: one each of 1, 2, 4, ... while they fit, then the binary split of the rest."""
    out, d = [], 1
    while amount >= d:
        out.append(d)
        amount -= d
        d <<= 1
    return out + split_amount(amount)


def random_secret() -> str:
    return secrets.token_hex(32)
