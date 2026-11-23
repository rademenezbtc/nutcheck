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


def _out(amount, B_):
    from types import SimpleNamespace
    return SimpleNamespace(amount=amount, blinded=SimpleNamespace(B_hex=B_))


def test_sig_all_message():
    # NUT-11 SIG_ALL vector (tests/11-test.md)
    from nutcheck.checks_funded import sig_all_message
    from nutcheck.wallet import Proof
    secret = ('["P2PK",{"nonce":"c7f280eb55c1e8564e03db06973e94bc9b666d9e1ca42ad278408fe625950303","data":'
              '"030d8acedfe072c9fa449a1efe0817157403fbec460d8e79f957966056e5dd76c1","tags":[["sigflag","SIG_ALL"]]}]')
    p = Proof(2, "00bfa73302d12ffd", secret, "02c97ee3d1db41cf0a3ddb601724be8711a032950811bf326f8219c50c4808d3cd")
    msg = sig_all_message([p], [_out(2, "038ec853d65ae1b79b5cdbc2774150b2cb288d6d26e12958a16fb33c32d9a86c39")])
    assert hashlib.sha256(msg.encode()).hexdigest() == "de7f9e3ca0fcc5ed3258fcf83dbf1be7fa78a5ed6da7bf2aa60d61e9dc6eb09a"
    sig = bytes.fromhex("ce017ca25b1b97df2f72e4b49f69ac26a240ce14b3690a8fe619d41ccc42d3c1"
                        "282e073f85acd36dc50011638906f35b56615f24e4d03e8effe8257f6a808538")
    pub = bytes.fromhex("030d8acedfe072c9fa449a1efe0817157403fbec460d8e79f957966056e5dd76c1")[1:]
    assert secp.schnorr_verify(hashlib.sha256(msg.encode()).digest(), pub, sig)


def test_nut20_message():
    # NUT-20 vector (tests/20-test.md): pubkey of secret key 1
    from nutcheck.checks_funded import nut20_message
    quote = "0192d3c0-7e8a-7c3d-8e9f-1a2b3c4d5e6f"
    outs = [_out(1, "036d6caac248af96f6afa7f904f550253a0f3ef3f5aa2fe6838a95b216691468e2"),
            _out(1, "021f8a566c205633d029094747d2e18f44e05993dda7a5f88f496078205f656e59")]
    msg = nut20_message(quote, outs)
    assert msg.hex() == (
        "43617368755f4d696e7451756f74655369675f76310000002430313932643363302d376538612d376333642d386539662d3161"
        "32623363346435653666000000010100000021036d6caac248af96f6afa7f904f550253a0f3ef3f5aa2fe6838a95b216691468"
        "e2000000010100000021021f8a566c205633d029094747d2e18f44e05993dda7a5f88f496078205f656e59")
    digest = hashlib.sha256(msg).digest()
    assert digest.hex() == "c164fd384879f74ab6ea2e7cf13d90ed42e6df9d5de607eeb5c9cc7d36fb1c21"
    sig = bytes.fromhex("4881093a332ff7c79f3e598ce5b249d64978b47165a0b19c18adf0ced0246228"
                        "e61e702f0abaf1bf27b92be4336bdbabacfbe4c914076386b3c66fdcd0b3480e")
    assert secp.schnorr_verify(digest, bytes.fromhex("79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"), sig)
    # and our signer produces signatures that verify against the same digest
    k = secp.PrivateKey(1)
    assert secp.schnorr_verify(digest, bytes.fromhex(k.pubkey_hex)[1:], bytes.fromhex(k.sign(msg)))


def test_legacy_keyset_id():
    # Independent value: computed with the browser's WebCrypto SHA-256 and btoa, not with Python
    keys = {1: "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
            2: "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5",
            4: "02f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"}
    assert crypto.keyset_id_legacy(keys) == "CSp9IXskZ64l"


def test_legacy_alias_is_not_key_reuse():
    """mint.mjex.me (Nutshell 0.20.0) lists each keyset twice: its 00… ID and its pre-2024 base64 ID."""
    from nutcheck import checks_free
    from nutcheck.wallet import Keyset, Wallet

    keys = {1: secp.encode_hex(secp.mul(11, secp.G)), 2: secp.encode_hex(secp.mul(12, secp.G))}
    v1 = crypto.keyset_id_v1(keys)
    legacy = crypto.keyset_id_legacy(keys)

    class Client:
        url = "https://mint.example"
        log = []
        def info(self): return {"nuts": {}, "name": "x", "version": "Nutshell/0.20.0"}
        def swap(self, *a): raise checks_free.MintError(400, {"code": 10001, "detail": "invalid proof"})

    w = Wallet(Client())
    w.load_keysets = lambda: None
    w.keysets = {k: Keyset(k, "sat", True, 0, dict(keys)) for k in (v1, legacy)}
    res = {r.id: r for r in checks_free.run(w.client, w)[0]}
    assert res["keys.unique"].status.value == "pass", res["keys.unique"].summary
    assert res["keys.keyset_id"].status.value == "pass", res["keys.keyset_id"].summary
    assert "legacy base64" in res["keys.keyset_id"].summary
    # a base64 ID that does NOT derive from its keys is still a mismatch
    w.keysets = {k: Keyset(k, "sat", True, 0, dict(keys)) for k in (v1, "AAAAAAAAAAAA")}
    res = {r.id: r for r in checks_free.run(w.client, w)[0]}
    assert res["keys.keyset_id"].status.value == "fail"
