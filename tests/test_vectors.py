"""NutCheck's crypto must reproduce the official NUT test vectors before it judges any mint."""
import hashlib

from nutcheck import crypto, secp


def test_hash_to_curve():
    for msg, pt in [
        ("00" * 32, "024cce997d3b518f739663b757deaec95bcd9473c30a14ac2fd04023a739d1a725"),
        ("00" * 31 + "01", "022e7158e11c9506f1aa4248bf531298daa7febd6194f003edcd9b93ade6253acf"),
        ("00" * 31 + "02", "026cdbe15362df59cd1dd3c9c11de8aedac2106eca69236ecd9fbe117af897be4f"),
    ]:
        assert secp.encode_hex(crypto.hash_to_curve(bytes.fromhex(msg))) == pt


def test_blinding():
    # NUT-00 vectors: x is a hex-encoded byte array
    for x, r, B_ in [
        ("d341ee4871f1f889041e63cf0d3823c713eea6aff01e80f1719f08f9e5be98f6",
         "99fce58439fc37412ab3468b73db0569322588f62fb3a49182d67e23d877824a",
         "033b1a9737a40cc3fd9b6af4b723632b76a67a36782596304612a6c2bfb5197e6d"),
        ("f1aaf16c2239746f369572c0784d9dd3d032d952c2d992175873fb58fae31a60",
         "f78476ea7cc9ade20f9e05e58a804cf19533f03ea805ece5fee88c8e2874ba50",
         "029bdf2d716ee366eddf599ba252786c1033f47e230248a4612a5670ab931f1763"),
    ]:
        Y = crypto.hash_to_curve(bytes.fromhex(x))
        got = secp.add(Y, secp.mul(int(r, 16), secp.G))
        assert secp.encode_hex(got) == B_


def test_hash_e():
    R =secp.decode("020000000000000000000000000000000000000000000000000000000000000001")
    C_ = secp.decode("02a9acc1e48c25eeeb9289b5031cc57da9fe72f3fe2861d264bdc074209b107ba2")
    assert crypto.hash_e(R, R, R, C_) == int("a4dc034b74338c28c6bc3ea49731f2a24440fc7c4affc08b31a93fc9fbe6401e", 16)


def test_dleq_blind_signature():
    A = secp.decode("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
    B_ = secp.decode("02a9acc1e48c25eeeb9289b5031cc57da9fe72f3fe2861d264bdc074209b107ba2")
    e = int("9818e061ee51d5c8edc3342369a554998ff7b4381c8652d724cdf46429be73d9", 16)
    s = int("9818e061ee51d5c8edc3342369a554998ff7b4381c8652d724cdf46429be73da", 16)
    assert crypto.verify_dleq_blind(A, B_, B_, e, s)  # C_ == B_ because a == 1
    assert not crypto.verify_dleq_blind(A, B_, B_, e, s + 1)


def test_dleq_deterministic_vector():
    A = secp.decode("02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5")
    B_ = secp.decode("02a9acc1e48c25eeeb9289b5031cc57da9fe72f3fe2861d264bdc074209b107ba2")
    C_ = secp.decode("0244eccfc7a348274458bb38044c7f3c389b3c2086c7ec18b5812d2877ab937787")
    e = int("2a16ffee280aff3c429045607f9b8e0bf8b35910c44c1b20b9dfaf01b263d7b3", 16)
    s = int("9df27731238334718d120d4f74611a7c668233f988e687ac3fb188f0a34a2dab", 16)
    assert crypto.verify_dleq_blind(A, B_, C_, e, s)


def test_dleq_proof():
    A = secp.decode("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
    assert crypto.verify_dleq_proof(
        A,
        "daf4dd00a2b68a0858a80450f52c8a7d2ccf87d375e43e216e0c571f089f63e9",
        secp.decode("024369d2d22a80ecf78f3937da9d5f30c1b9f74f0c32684d583cca0fa6a61cdcfc"),
        int("b31e58ac6527f34975ffab13e70a48b6d2b0d35abc4b03f0151f09ee1a9763d4", 16),
        int("8fbae004c59e754d71df67e392b6ae4e29293113ddc2ec86592a0431d16306d8", 16),
        int("a6d13fcd7a18442e6076f5e1e7c887ad5de40a019824bdfa9fe740d302e8d861", 16),
    )


def test_bip340_roundtrip():
    k = secp.PrivateKey(12345)
    msg = hashlib.sha256(b"hello").digest()
    sig = secp.schnorr_sign(msg, k.k)
    assert secp.schnorr_verify(msg, k.point[0].to_bytes(32, "big"), sig)
    assert not secp.schnorr_verify(hashlib.sha256(b"other").digest(), k.point[0].to_bytes(32, "big"), sig)


def test_bip340_official_vector_0():
    # BIP-340 test vector index 0
    sk = 3
    aux = bytes(32)
    msg = bytes(32)
    sig = secp.schnorr_sign(msg, sk, aux)
    assert sig.hex().upper() == (
        "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
        "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"
    )


def _keyset_vectors():
    import json
    import pathlib
    import re
    text = (pathlib.Path(__file__).parent / "vectors/02-tests.md").read_text()
    out = []
    for block in re.split(r"\n(?=Keyset id:|### Vector)", text):
        m = re.search(r"Keyset id: `([0-9a-f]+)`", block)
        j = re.search(r"```json\n(.*?)```", block, re.S)
        if not (m and j):
            continue
        meta = {k: re.search(rf"- {k}: `([^`]*)`", block) for k in ("Unit", "Input fee ppk", "Final expiry")}
        out.append((
            m.group(1),
            {int(a): v for a, v in json.loads(j.group(1)).items()},
            meta["Unit"].group(1) if meta["Unit"] else None,
            int(meta["Input fee ppk"].group(1)) if meta["Input fee ppk"] else None,
            int(meta["Final expiry"].group(1)) if meta["Final expiry"] else None,
        ))
    return out


def test_keyset_ids_from_spec():
    vectors = _keyset_vectors()
    assert len(vectors) >= 4, vectors
    for kid, keys, unit, fee, expiry in vectors:
        if kid.startswith("00"):
            assert crypto.keyset_id_v1(keys) == kid
        else:
            assert crypto.keyset_id_v2(keys, unit, fee, expiry) == kid
