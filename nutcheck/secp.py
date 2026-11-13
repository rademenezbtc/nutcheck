"""Minimal secp256k1 arithmetic and BIP-340 Schnorr signing, in pure Python.

NutCheck deliberately does not reuse any Cashu library's crypto: an auditor
that shares code with the thing it audits can share its bugs too. Speed is not
a concern here; a full test run needs a few hundred point multiplications.
"""
from __future__ import annotations

import hashlib
import secrets

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

Point = tuple[int, int] | None  # None is the point at infinity
G: Point = (GX, GY)


class InvalidPoint(ValueError):
    pass


def is_on_curve(pt: Point) -> bool:
    if pt is None:
        return True
    x, y = pt
    return (y * y - x * x * x - 7) % P == 0


def add(a: Point, b: Point) -> Point:
    if a is None:
        return b
    if b is None:
        return a
    x1, y1 = a
    x2, y2 = b
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if a == b:
        lam = 3 * x1 * x1 * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def neg(a: Point) -> Point:
    return None if a is None else (a[0], (-a[1]) % P)


def mul(k: int, pt: Point) -> Point:
    k %= N
    result: Point = None
    addend = pt
    while k:
        if k & 1:
            result = add(result, addend)
        addend = add(addend, addend)
        k >>= 1
    return result


def lift_x(x: int, even: bool = True) -> Point:
    if not 0 <= x < P:
        raise InvalidPoint("x out of range")
    y2 = (pow(x, 3, P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    if y * y % P != y2:
        raise InvalidPoint("x is not on the curve")
    if (y % 2 == 0) != even:
        y = P - y
    return (x, y)


def decode(hex_or_bytes: str | bytes) -> Point:
    """Parse a SEC1 compressed or uncompressed point. Raises InvalidPoint."""
    try:
        b = bytes.fromhex(hex_or_bytes) if isinstance(hex_or_bytes, str) else hex_or_bytes
    except ValueError as e:
        raise InvalidPoint(f"not hex: {e}") from None
    if len(b) == 33 and b[0] in (2, 3):
        return lift_x(int.from_bytes(b[1:], "big"), even=b[0] == 2)
    if len(b) == 65 and b[0] == 4:
        pt = (int.from_bytes(b[1:33], "big"), int.from_bytes(b[33:], "big"))
        if not is_on_curve(pt):
            raise InvalidPoint("uncompressed point not on curve")
        return pt
    raise InvalidPoint(f"bad point encoding (len {len(b)})")


def encode(pt: Point, compressed: bool = True) -> bytes:
    if pt is None:
        raise InvalidPoint("cannot encode point at infinity")
    x, y = pt
    if compressed:
        return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def encode_hex(pt: Point, compressed: bool = True) -> str:
    return encode(pt, compressed).hex()


# ---- BIP-340 Schnorr ---------------------------------------------------------

def _tagged(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def schnorr_sign(msg32: bytes, seckey: int, aux: bytes | None = None) -> bytes:
    assert len(msg32) == 32
    d0 = seckey % N
    if d0 == 0:
        raise ValueError("invalid secret key")
    Pk = mul(d0, G)
    d = d0 if Pk[1] % 2 == 0 else N - d0
    aux = aux if aux is not None else secrets.token_bytes(32)
    t = (d ^ int.from_bytes(_tagged("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    px = Pk[0].to_bytes(32, "big")
    k0 = int.from_bytes(_tagged("BIP0340/nonce", t + px + msg32), "big") % N
    R = mul(k0, G)
    k = k0 if R[1] % 2 == 0 else N - k0
    rx = R[0].to_bytes(32, "big")
    e = int.from_bytes(_tagged("BIP0340/challenge", rx + px + msg32), "big") % N
    return rx + ((k + e * d) % N).to_bytes(32, "big")


def schnorr_verify(msg32: bytes, pubkey_x: bytes, sig: bytes) -> bool:
    try:
        Pk = lift_x(int.from_bytes(pubkey_x, "big"))
    except InvalidPoint:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if r >= P or s >= N:
        return False
    e = int.from_bytes(_tagged("BIP0340/challenge", sig[:32] + pubkey_x + msg32), "big") % N
    R = add(mul(s, G), neg(mul(e, Pk)))
    return R is not None and R[1] % 2 == 0 and R[0] == r


class PrivateKey:
    def __init__(self, k: int | None = None):
        self.k = k if k is not None else (secrets.randbelow(N - 1) + 1)
        self.point = mul(self.k, G)

    @property
    def pubkey_hex(self) -> str:
        return encode_hex(self.point)

    def sign(self, msg: bytes) -> str:
        """NUT-11: Schnorr signature over SHA256(msg)."""
        return schnorr_sign(hashlib.sha256(msg).digest(), self.k).hex()
