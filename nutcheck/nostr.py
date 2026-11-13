"""Just enough Nostr for Nostr Wallet Connect: keys, signed events, NIP-04 and NIP-44 v2 encryption.

Reuses NutCheck's own secp256k1 code. Symmetric primitives (AES-CBC, ChaCha20) come from `cryptography`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from . import secp


# ---- keys and events -------------------------------------------------------------------------

def xonly_pub(seckey: int) -> str:
    return secp.mul(seckey, secp.G)[0].to_bytes(32, "big").hex()


def _point_from_xonly(pub_hex: str) -> secp.Point:
    return secp.lift_x(int(pub_hex, 16), even=True)


def event_id(ev: dict) -> str:
    ser = json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                     separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(ser.encode()).hexdigest()


def sign_event(seckey: int, kind: int, content: str, tags: list[list[str]], created_at: int | None = None) -> dict:
    ev = {"pubkey": xonly_pub(seckey), "created_at": created_at or int(time.time()), "kind": kind,
          "tags": tags, "content": content}
    ev["id"] = event_id(ev)
    ev["sig"] = secp.schnorr_sign(bytes.fromhex(ev["id"]), seckey).hex()
    return ev


def verify_event(ev: dict) -> bool:
    return event_id(ev) == ev.get("id") and secp.schnorr_verify(
        bytes.fromhex(ev["id"]), bytes.fromhex(ev["pubkey"]), bytes.fromhex(ev["sig"]))


def _shared_x(seckey: int, pub_hex: str) -> bytes:
    return secp.mul(seckey, _point_from_xonly(pub_hex))[0].to_bytes(32, "big")


# ---- NIP-04 (deprecated, still common) -------------------------------------------------------

def nip04_encrypt(seckey: int, pub_hex: str, plaintext: str, iv: bytes | None = None) -> str:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    iv = iv or os.urandom(16)
    padder = padding.PKCS7(128).padder()
    data = padder.update(plaintext.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(_shared_x(seckey, pub_hex)), modes.CBC(iv)).encryptor()
    ct = enc.update(data) + enc.finalize()
    return base64.b64encode(ct).decode() + "?iv=" + base64.b64encode(iv).decode()


def nip04_decrypt(seckey: int, pub_hex: str, payload: str) -> str:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    ct_b64, iv_b64 = payload.split("?iv=")
    dec = Cipher(algorithms.AES(_shared_x(seckey, pub_hex)), modes.CBC(base64.b64decode(iv_b64))).decryptor()
    data = dec.update(base64.b64decode(ct_b64)) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(data) + unpadder.finalize()).decode()


# ---- NIP-44 v2 -----------------------------------------------------------------------------

def nip44_conversation_key(seckey: int, pub_hex: str) -> bytes:
    return hmac.new(b"nip44-v2", _shared_x(seckey, pub_hex), hashlib.sha256).digest()  # HKDF-extract


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


def nip44_message_keys(conversation_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    k = _hkdf_expand(conversation_key, nonce, 76)
    return k[:32], k[32:44], k[44:76]


def nip44_padded_len(n: int) -> int:
    if n <= 32:
        return 32
    next_power = 1 << ((n - 1).bit_length())
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((n - 1) // chunk + 1)


def _chacha(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    c = Cipher(algorithms.ChaCha20(key, b"\x00\x00\x00\x00" + nonce12), mode=None).encryptor()
    return c.update(data) + c.finalize()


def nip44_encrypt(conversation_key: bytes, plaintext: str, nonce: bytes | None = None) -> str:
    nonce = nonce or os.urandom(32)
    pt = plaintext.encode()
    if not 1 <= len(pt) <= 65535:
        raise ValueError("plaintext length must be 1..65535 bytes")
    padded = len(pt).to_bytes(2, "big") + pt + bytes(nip44_padded_len(len(pt)) - len(pt))
    ck, cn, hk = nip44_message_keys(conversation_key, nonce)
    ct = _chacha(ck, cn, padded)
    mac = hmac.new(hk, nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(b"\x02" + nonce + ct + mac).decode()


def nip44_decrypt(conversation_key: bytes, payload: str) -> str:
    if not payload or payload[0] == "#":
        raise ValueError("unknown NIP-44 version")
    raw = base64.b64decode(payload)
    if len(raw) < 99 or raw[0] != 2:
        raise ValueError("invalid NIP-44 payload")
    nonce, ct, mac = raw[1:33], raw[33:-32], raw[-32:]
    ck, cn, hk = nip44_message_keys(conversation_key, nonce)
    if not hmac.compare_digest(mac, hmac.new(hk, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("invalid MAC")
    padded = _chacha(ck, cn, ct)
    n = int.from_bytes(padded[:2], "big")
    if n == 0 or len(padded) != 2 + nip44_padded_len(n):
        raise ValueError("invalid padding")
    return padded[2:2 + n].decode()
