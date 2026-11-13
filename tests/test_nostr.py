"""Nostr crypto checked against the official NIP-44 test vectors (paulmillr/nip44) before it touches a wallet."""
import json
import pathlib

import pytest

from nutcheck import nostr

VEC = json.loads((pathlib.Path(__file__).parent / "vectors" / "nip44.vectors.json").read_text())["v2"]


def test_conversation_keys():
    for v in VEC["valid"]["get_conversation_key"]:
        pub = v["pub2"]
        assert nostr.nip44_conversation_key(int(v["sec1"], 16), pub).hex() == v["conversation_key"]


def test_message_keys():
    mk = VEC["valid"]["get_message_keys"]
    ck = bytes.fromhex(mk["conversation_key"])
    for k in mk["keys"]:
        c, n, h = nostr.nip44_message_keys(ck, bytes.fromhex(k["nonce"]))
        assert (c.hex(), n.hex(), h.hex()) == (k["chacha_key"], k["chacha_nonce"], k["hmac_key"])


def test_padded_len():
    for n, want in VEC["valid"]["calc_padded_len"]:
        assert nostr.nip44_padded_len(n) == want


def test_encrypt_decrypt():
    for v in VEC["valid"]["encrypt_decrypt"]:
        sec1 = int(v["sec1"], 16)
        pub2 = nostr.xonly_pub(int(v["sec2"], 16))
        ck = nostr.nip44_conversation_key(sec1, pub2)
        assert ck.hex() == v["conversation_key"]
        assert nostr.nip44_encrypt(ck, v["plaintext"], bytes.fromhex(v["nonce"])) == v["payload"]
        assert nostr.nip44_decrypt(ck, v["payload"]) == v["plaintext"]


def test_invalid_payloads_rejected():
    for v in VEC["invalid"]["decrypt"]:
        with pytest.raises(Exception):
            nostr.nip44_decrypt(bytes.fromhex(v["conversation_key"]), v["payload"])


def test_nip04_roundtrip_both_sides():
    a, b = 0x1234, 0x5678
    msg = '{"method":"pay_invoice","params":{"invoice":"lnbc1..."}}'
    ct = nostr.nip04_encrypt(a, nostr.xonly_pub(b), msg)
    assert nostr.nip04_decrypt(b, nostr.xonly_pub(a), ct) == msg


def test_event_signature():
    ev = nostr.sign_event(0xABC, 23194, "x", [["p", "00" * 32]], created_at=1700000000)
    assert nostr.verify_event(ev)
    ev["content"] = "y"
    assert not nostr.verify_event(ev)
