# NutCheck

**SSL Labs for Cashu mints.** Point it at a mint and it checks whether the mint *behaves* as the NUTs require: valid DLEQ proofs, keyset IDs that match their keys, P2PK and HTLC locks actually enforced, double spends refused, payouts that work, overpaid fees returned. You get an A–F grade with the evidence behind it.

NutCheck measures behaviour, not solvency. An A means the mint follows the rules today. It says nothing about whether the mint holds the bitcoin backing its ecash, or will still be there tomorrow.

```
$ nutcheck https://mint.example.com --json report.json

  ✔ Keyset IDs match their keys                    NUT-02  high
  ✔ Rejects forged ecash                           NUT-03  critical
  ✔ DLEQ proofs valid on mint                      NUT-12  critical
  ✔ Rejects double spends                          NUT-03  critical
  ✔ P2PK lock enforced                             NUT-11  critical
  ✔ Overpaid fees are returned                     NUT-08  high
  ...
  Grade: A
  53 sat of leftover ecash saved to nutcheck-leftover.token
```

## Install and run

```bash
pip install -e .            # needs only httpx
nutcheck <mint-url>                  # free checks, then asks you to pay a 64-sat invoice for the rest
nutcheck <mint-url> --free-only      # nothing paid, partial grade
nutcheck <mint-url> --fund 128 --json report.json
```

The funded run costs about 11 sat at a 100 ppk input fee: input fees, plus 2 sat deliberately burned on a malformed lock. The rest is saved as a `cashuA` token you can receive into any wallet.

## Checks

| Free | Funded |
|---|---|
| HTTPS · info well formed, clock skew · keys are valid points · no key reuse · keyset ID re-derivation (v1 and v2) · active keyset · **forged proof refused** · claimed NUT endpoints answer | paid quote issues full amount · quote can't be minted twice · DLEQ on mint/swap/change · outputs > inputs refused · advertised fee is the real fee · double spend and duplicate input refused · checkstate shows SPENT · P2PK: no sig / wrong key / 1-of-2 / refund-before-locktime / duplicate tag all refused, valid spends accepted · HTLC: wrong/no preimage, missing receiver sig refused · melt pays · overpaid fees returned |

Every "must refuse" test is paired with a "must accept" control built the same way, so a mint that refuses everything can't pass by accident. The fee-return test melts against an invoice from the same mint (an internal payment, so the Lightning fee is 0) and expects every overpaid sat back.

**Grading:** the worst failure caps the grade. Critical → F, high → C, medium → B, low or a warning → A-.

## Independence

NutCheck's secp256k1, BDHKE, DLEQ, BIP-340 and keyset-ID code is its own pure-Python implementation of the NUT text. It does not reuse any mint's library. `tests/test_vectors.py` checks it against the official test vectors in `cashubtc/nuts`.

## The naughty proxy (demo and regression)

`proxy/naughty.py` sits in front of an honest FakeWallet mint and misbehaves on cue:

```bash
python proxy/naughty.py --upstream http://127.0.0.1:3338 --port 3339
curl -X POST localhost:3339/__naughty -d '{"modes": ["lock_bypass"]}'
nutcheck http://127.0.0.1:3339          # → F, every P2PK/HTLC check fails
```

Modes: `bad_dleq no_dleq keyset_tamper fee_lie short_mint quote_replay overspend double_spend forgery lock_bypass keep_change info_lie`. For the "accepts what it shouldn't" modes, the proxy mints the client's outputs from a fake-paid quote, so the client gets real signatures. `tests/test_matrix.py` asserts each mode is caught.

## Local test mint

```bash
uv venv -p 3.12 .mintenv
printf "breez-sdk-spark>=0.21.1\nmarshmallow<4\nlimits<4\n" > overrides.txt   # Nutshell 0.21.0 pin fixes
VIRTUAL_ENV=.mintenv uv pip install cashu==0.21.0 --override overrides.txt
run/start_mint.sh mint1     # FakeWallet on :3338, 100 ppk input fee
```
