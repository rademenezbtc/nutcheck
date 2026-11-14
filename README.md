<p><img src="assets/logo-lockup.svg" alt="NutCheck" width="320"></p>

# NutCheck

**Get your nuts checked.** SSL Labs for Cashu mints.

[![Tests](https://github.com/rademenezbtc/nutcheck/actions/workflows/tests.yml/badge.svg)](https://github.com/rademenezbtc/nutcheck/actions/workflows/tests.yml)
[![Scorecard](https://github.com/rademenezbtc/nutcheck/actions/workflows/scorecard.yml/badge.svg)](https://github.com/rademenezbtc/nutcheck/actions/workflows/scorecard.yml)

**Live scorecard: [nutcheck.org](https://nutcheck.org)**, re-tested every day, with real sats on Mondays.

Point NutCheck at a mint and it checks whether the mint *behaves* as the NUTs require: valid DLEQ proofs, keyset IDs that match their keys, P2PK and HTLC locks actually enforced, double spends refused, payouts that work, overpaid fees returned. You get an A–F grade with the evidence behind it: the request and response that exposed each failure.

Wallet users get a way to compare mints on something other than uptime. Mint operators get a test they can run before launch and a badge once they pass. Implementers get an independent conformance suite that runs against production.

[![The NutCheck scorecard at nutcheck.org](docs/scorecard.png)](https://nutcheck.org)

NutCheck measures behaviour, not solvency. An A means the mint followed the rules in the last test. It says nothing about whether the mint holds the bitcoin backing its ecash, or will still be there tomorrow.

## Install and run

No install needed if you have [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/rademenezbtc/nutcheck nutcheck https://mint.example.com --free-only
```

Or install it:

```bash
pip install git+https://github.com/rademenezbtc/nutcheck   # needs only httpx
nutcheck <mint-url>                  # free checks, then asks you to pay a 64-sat invoice for the rest
nutcheck <mint-url> --free-only      # nothing paid, partial grade (shown as A*)
nutcheck <mint-url> --fund 128 --json report.json
```

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

A funded run costs about 11–15 sat: input fees, plus 2 sat deliberately burned on a malformed lock. Everything else comes back as a `cashuA` token you can receive into any wallet.

## What it found on live mints

First sweeps across ten public mints (CDK and Nutshell, 0.17 to 0.22):

| Finding | Mints | Status |
|---|---|---|
| **Duplicated P2PK tags accepted.** A lock with two `locktime` tags should be rejected as malformed (NUT-11). CDK keeps the first copy instead, so a wallet that reads the other copy disagrees on when the refund key can spend. | All four CDK mints tested (Minibits, Macadamia, WesternBTC, testnut) | Graded B (medium). Fix in review: [cashubtc/cdk#2888](https://github.com/cashubtc/cdk/pull/2888) |
| Same issue in Nutshell before 0.21.0 | Voltz (Nutshell 0.20.3) | Graded B. Fixed upstream in [cashubtc/nutshell#1008](https://github.com/cashubtc/nutshell/pull/1008); upgrading clears it |
| Mint quote fails: the Lightning backend can't create invoices | Agorist | Free checks only (A*), flagged on the scorecard |
| Minting disabled | 8333.space | Free checks only (A*) |
| Everything passes, including locks, fee return and DLEQ on change | Chorus, Coinos, Cuba Bitcoin | A |

The scorecard explains known issues in place, with a link to the upstream fix, so an operator sees what to upgrade rather than just a letter.

Lessons for anyone writing a wallet or tool against live mints:

- **Nutshell's rate limiter punishes retries.** Its defaults (20 transactions/min, 60 requests/min per IP) use an elastic window: every request made while limited extends the lockout. NutCheck spaces POSTs about 3.3 s apart on Nutshell, waits 65 s on a 429, and never retries a POST on a 5xx.
- **A refusal is not proof of correct behaviour.** A 429 or 5xx counts as *inconclusive*, never as "mint refused the bad proof". Every must-refuse test is paired with a must-accept control built the same way, so a mint that refuses everything can't pass.
- **Error codes differ between implementations,** so NutCheck grades what the mint did (accepted or refused), not which code it returned.

## Checks

| Free | Funded |
|---|---|
| HTTPS · info well formed, clock skew · keys are valid points · no key reuse · keyset ID re-derivation (v1 and v2) · active keyset · **forged proof refused** · claimed NUT endpoints answer | paid quote issues full amount · quote can't be minted twice · DLEQ on mint/swap/change · outputs > inputs refused · advertised fee is the real fee · double spend and duplicate input refused · checkstate shows SPENT · P2PK: no sig / wrong key / 1-of-2 / refund-before-locktime / duplicate tag all refused, valid spends accepted · HTLC: wrong/no preimage, missing receiver sig refused · melt pays · overpaid fees returned |

The fee-return test melts against an invoice from the same mint (an internal payment, so the Lightning fee is 0) and expects every overpaid sat back.

**Grading:** the worst failure caps the grade. Critical (funds can be forged or stolen) → F, high (funds withheld, privacy broken) → C, medium (spec violation, limited impact) → B, low or a warning → A−. An asterisk means only the free checks ran.

## Get your mint listed

Open an [issue](https://github.com/rademenezbtc/nutcheck/issues/new) with your mint URL, or a pull request adding it to `scorecard.json`:

```json
{"url": "https://mint.example.com", "funded": "nwc"}
```

`"funded": "nwc"` lets the weekly run spend about 15 sat of NutCheck's own money on your mint and pay the rest back; `false` runs the free checks only. Then add your badge:

```markdown
[![NutCheck](https://nutcheck.org/badge/mint.example.com.svg)](https://nutcheck.org/mints/mint.example.com.html)
```

Wallets can read every grade from the JSON feed at [`nutcheck.org/api/mints.json`](https://nutcheck.org/api/mints.json).

Funded checks are paid from a small Lightning wallet. Zaps to ⚡ `nutcheck@rizful.com` keep them running.

## How it works

- **Independent crypto.** NutCheck's secp256k1, BDHKE, DLEQ, BIP-340, NIP-44 and keyset-ID code is its own pure-Python implementation of the spec text. It doesn't reuse any mint's library, so it can't share that library's bugs. `tests/test_vectors.py` checks it against the official vectors in `cashubtc/nuts`, BIP-340 and `paulmillr/nip44`.
- **The naughty proxy.** `proxy/naughty.py` sits in front of an honest Nutshell mint and misbehaves on cue, one fault per mode. `tests/test_matrix.py` asserts each one is caught. [CI](.github/workflows/tests.yml) runs this against a real mint on every push.

  ```bash
  python proxy/naughty.py --upstream http://127.0.0.1:3338 --port 3339
  curl -X POST localhost:3339/__naughty -d '{"modes": ["lock_bypass"]}'
  nutcheck http://127.0.0.1:3339          # → F, every P2PK/HTLC check fails
  ```

  Modes: `bad_dleq no_dleq keyset_tamper fee_lie short_mint quote_replay overspend double_spend forgery lock_bypass keep_change info_lie`. For the "accepts what it shouldn't" modes, the proxy mints the client's outputs from a fake-paid quote, so the client gets real signatures.
- **Funding over Nostr Wallet Connect.** For each funded mint the sweep pays a 64-sat invoice from an NWC wallet, runs the funded checks, then melts every leftover sat back to the same wallet. It pays out at most `nwc_max_sat_per_run` (default 1000) per run. If a pay-back fails, the ecash is kept encrypted (NIP-44, keyed from the wallet secret) on the `data` branch and recovered on the next run.
- **Responsible disclosure is built in.** A critical or high failure shows as a grade only, with details withheld from the pages, the JSON report, the feed and the badge ("under review"), until `embargo_days` (14) after it was first seen. Set `"disclosed": true` to publish early, for example once the operator has fixed it.
- **Published evidence can't be spent.** Failed checks include the request and response that exposed them, with proofs, outputs, witnesses and preimages reduced to counts or redacted.

## Run your own scorecard

Test a list of mints on a schedule and publish the results as a static site: a scorecard, a page per mint with the evidence behind every failure, badge SVGs and a JSON feed.

```bash
nutcheck sweep scorecard.json            # test every mint, save reports/<mint>/<timestamp>.json
nutcheck sweep scorecard.json --nwc      # also run funded checks on "funded": "nwc" mints (needs NWC_URL)
nutcheck site scorecard.json -o site/    # build the site from all saved reports
```

`.github/workflows/scorecard.yml` runs the sweep daily at 05:17 UTC on GitHub's free runners, keeps the report history on a separate `data` branch, and publishes to GitHub Pages. No server needed.

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
2. For funded checks, store an NWC connection string (send and receive) as the repository secret `NWC_URL`. Only the sweep step can read it, and it never appears in logs or reports. Give NutCheck a wallet that only holds what you're willing to spend.
3. **Actions → Scorecard → Run workflow** for the first run. Tick **funded** to run the paid checks now (they otherwise run on Mondays), and optionally name one mint under **only**.
4. Optional: a custom domain (Settings → Pages → Custom domain; set `base_url` in `scorecard.json` to match).

**When the run fails with "NutCheck needs your attention",** either NutCheck found a critical or high failure, or funding went wrong. GitHub emails you about the failed run, and the run summary says which. For a mint failure, contact the operator privately, add `"notified": "<date>"` to that mint in `scorecard.json`, and push. The alert stops, and the site then says the operator was told.

Notes:
- `testnut.cashu.space` runs on fake Lightning, so it gets the full funded run every day for free.
- GitHub turns off scheduled workflows in public repos after 60 days without activity. The daily commit to `data` should count. If the workflow ever shows as disabled, re-enable it in the Actions tab.
- GitHub runners use well-known cloud IP ranges, so a mint could recognise NutCheck and behave differently. That's an open problem.
- `demo/run_demo.sh` builds a demo scorecard from real runs: one naughty-proxy instance per fault mode, swept six times, with one mint that regresses halfway through and one still inside its disclosure window.

## Local test mint

```bash
uv venv -p 3.12 .mintenv
VIRTUAL_ENV=.mintenv uv pip install cashu==0.21.0 --override overrides.txt   # Nutshell 0.21.0 with pin fixes
run/start_mint.sh mint1     # FakeWallet on :3338, 100 ppk input fee
pip install -e ".[dev,nwc,proxy]" && python proxy/naughty.py --upstream http://127.0.0.1:3338 --port 3339 &
pytest                      # 51 tests; the end-to-end ones skip if the mint or proxy isn't running
```

## Built for Nut November 2026

NutCheck was started for [Nut November](https://nutnovember.org/). Everything in this repository was written in November 2026; the first commit is from 13 November.

- **13 Nov:** grading CLI with its own crypto, the naughty proxy and the fault matrix; scorecard site and daily GitHub sweep; first live findings (the duplicate-tag issue, traced to CDK PR #2888 and Nutshell PR #1008); funded checks on live mints over Nostr Wallet Connect.
- **14 Nov:** rate-limit-safe client after Nutshell's limiter caused false results; encrypted recovery of leftover ecash; scorecard tooltips and known-issue notes; live at [nutcheck.org](https://nutcheck.org); CI running the full suite against a real mint.
- **Next:** publish signed grades on Nostr (NIP-87 recommendations) so wallets can read them without trusting this website; more mints; NUT-17 and NUT-20 checks.

The [design brief](docs/nutcheck-brief.md) has the reasoning, the test catalogue and the ethics.

## License

MIT
