# NutCheck — brief

*"SSL Labs for Cashu mints." Rebuilt 13 Nov 2026: the original `claude/nutcheck-brief.md` never reached this project, so this version is reconstructed from the project summary and updated with the day-one spike results and the first live runs.*

## The gap

Mint *availability* is already watched:

- **The Cashu round-robin auditor** holds donated ecash, sends random Lightning payments between mints and marks mints that fail to pay. It rotates IP addresses so mints can't single it out.
- **Sovran wallet** pulls uptime and success rates from that auditor and mixes in Nostr community ratings.
- **cashumints.space** probes availability and response time and keeps a history of each mint's info page. Its docs don't mention DLEQ, fees or melt data.

Nobody checks whether a live mint **follows the protocol**. NutCheck does: valid DLEQ proofs, keyset IDs matching keys, P2PK/HTLC locks actually enforced, double spends refused, payouts working, overpaid fees returned, and an info page that tells the truth. Each mint gets an A–F grade with evidence.

**Honest limit:** NutCheck measures behaviour, not solvency. An A says the mint follows the rules today, not that it holds the sats backing its ecash.

## Outputs

1. **CLI** (`nutcheck <url>`). Anyone can point it at a mint, including operators testing their own before launch. *Working as of 13 Nov.*
2. **Daemon.** Runs the suite on a schedule against a list of mints and publishes signed results as Nostr events, so wallets like Sovran can consume them.
3. **Static scorecard.** A page listing each mint's grade, date of last test and per-check evidence.

## Test catalogue

**Free** (no ecash spent): HTTPS · info well formed and clock skew · keys are valid points · no key reused across amounts · keyset ID re-derivation (v1 and v2; v2 commits to `input_fee_ppk`, so a fee lie also breaks the ID; legacy base64 IDs that alias a current keyset are recognised, not counted as key reuse) · active keyset exists · forged proof refused · claimed NUT endpoints answer · NUT-17 WebSocket answers and sends the current state of a never-seen proof (UNSPENT) first.

**Funded** (64 sat budget, about 15 sat actually spent per run at 100 ppk): the funding invoice must stay unpaid until NutCheck pays it (a quote marked paid by itself means fake Lightning: critical, and no paid checks) · paid quote issues full amount · quote can't be minted twice · DLEQ on mint, swap and melt change · overspend refused · advertised fee is the real fee · double spend and duplicate input refused · checkstate reports SPENT · P2PK (missing/wrong signature, 1-of-2 multisig, refund key before locktime, duplicate tag all refused; valid spends accepted; refund works after locktime) · SIG_ALL (per-input signatures and outputs swapped after signing refused; one signature over all inputs and outputs accepted; if the current message is refused, the pre-November-2025 one (secrets and B_ only) is tried, and accepting only that is a low warning) · HTLC (missing/wrong preimage, missing receiver signature, refund key before locktime refused; refund key alone works after locktime) · melt pays · overpaid fees returned (melting against the mint's *own* invoice, where the true Lightning fee is 0, so every overpaid sat must come back) · NUT-20: the melt's internal quote is locked to a key; claiming it back without a signature or with the wrong key must fail, with the right key succeed (current message format first, the pre-May-2026 one as a fallback that only earns a warning).

Every "must refuse" test has a "must accept" control built the same way, so a mint that refuses everything can't pass.

**Grading:** the worst failure caps the grade. Critical (funds forged, stolen or lost) → F; high (privacy break, funds withheld) → C; medium → B; low or warning → A-. Each mint shows one grade, from the latest free and paid results together, with a ✓/✗ line for each half saying when it ran and how it went, or why the paid checks didn't run. A mint gets a letter only once it has paid results from the last 8 days; until then it shows **?** and gets no Nostr review (a free-check failure still shows and is reviewed).

## Naughty proxy

A reverse proxy in front of an honest FakeWallet mint that misbehaves on cue, so the demo needs no changes to CDK or Nutshell. Eighteen grading modes: `bad_dleq`, `no_dleq`, `keyset_tamper`, `fee_lie`, `short_mint`, `quote_replay`, `overspend`, `double_spend`, `forgery`, `lock_bypass`, `keep_change`, `info_lie`, plus (23 Nov) `sigall_ignore`, `sigall_legacy`, `htlc_no_refund`, `quote_lock_ignore`, `ws_dead`, `ws_silent`; and helper modes for the funding tests (`await_payment`, `payout_fail`, `slow_melt`, two rate-limit modes). Without `await_payment` the FakeWallet behind the proxy pays every invoice itself, which the fake-Lightning check must catch. The proxy also relays the NUT-17 WebSocket. For the "accepts what it shouldn't" faults, the proxy mints the client's outputs from a fake-paid quote, so the client receives real signatures, exactly as from a broken mint. Modes switch live via `POST /__naughty`.

## Day-one spike results (13 Nov)

- CLI, crypto core, 27 checks and the proxy are built. **All 12 faults are caught. The honest mint grades A with no false alarms.** 22 automated tests pass, including the official NUT-00/02/12 and BIP-340 vectors.
- Crypto is NutCheck's own pure-Python implementation of the spec text, so it doesn't share bugs with the mints it audits.
- **Live mints were not reachable from the build workspace** (network allowlist). All testing so far is against local Nutshell 0.21.0 with FakeWallet. The first live runs must happen from a normal machine.
- Finding: Nutshell 0.21.0 answers an unbalanced swap with generic error code `11000`, not the spec's `11005`. That's minor, but a good case for a low-severity "error codes" check, and a polite first disclosure.
- Installing Nutshell 0.21.0 needed three dependency overrides (`breez-sdk-spark`, `marshmallow<4`, `limits<4`), documented in the README.

## First live runs (13 Nov)

The scorecard went live at https://rademenezbtc.github.io/nutcheck/ (now https://nutcheck.org), rebuilt daily by a GitHub Actions workflow (free runners, report history on the `data` branch, GitHub Pages).

- **10 mints tested:** 6 run Nutshell (0.17.0 to 0.22.1), 4 run CDK (0.17.6 to 0.18.3).
- **9 live mints, free checks only:** all A* (keys, keyset IDs, forged ecash refused, HTTPS, honest info page).
- **testnut.cashu.space, full funded run (cdk-mintd 0.18.1): B.** 26 of 27 checks pass, including DLEQ, double spend, overspend, every P2PK/HTLC lock and fee refunds. It accepts a P2PK secret with a duplicated `locktime` tag, which NUT-11 says MUST be rejected. CDK does this by design: current `master` keeps the first copy of a duplicated tag, and has a test asserting it, so it likely applies to every CDK mint. Already public in CDK's code, so no embargo; to raise openly with CDK and the spec authors.
- **Lesson:** the first live run graded testnut F because NutCheck labelled this check critical ("anyone could take it"), although the spend carried a valid signature from the locking key. Reclassified as medium and the wrong report withdrawn from the history. Severity must describe the actual consequence of the specific spend, not the category of check.

## Status (14 Nov)

Live at **https://nutcheck.org**: all checks, free and funded, once a day.

- **Funded checks on live mints work.** A Rizful wallet over Nostr Wallet Connect pays each mint's 64-sat invoice; NutCheck runs the funded checks and melts the rest back. About 15 sat per mint per run. Leftovers that can't be paid back are stored NIP-44-encrypted on the `data` branch and recovered next run.
- **Results (9 live mints + testnut):** A for Chorus, Coinos and Cuba Bitcoin. B for every CDK mint (Minibits, Macadamia, WesternBTC, testnut) and for Voltz (Nutshell 0.20.3), all for accepting duplicated P2PK tags. CDK's fix was merged on 16 Nov ([cdk#2888](https://github.com/cashubtc/cdk/pull/2888)); Nutshell fixed it in 0.21.0 ([nutshell#1008](https://github.com/cashubtc/nutshell/pull/1008)). Agorist (Lightning backend can't create invoices) and 8333.space (minting disabled) are free checks only (A*).
- **Lesson: rate limits create false results.** Nutshell's limiter (20 tx/min, elastic window) turned a funded run into a string of 429s, which early NutCheck read as "refused" (false pass) or "error" (false fail), and stranded 53 sat. Now: 429 and 5xx are *inconclusive*, POSTs are paced at 3.3 s on Nutshell, and leftover ecash is never lost.
- **Scorecard:** free checks grouped before funded ones, problems in hover tooltips, known upstream issues explained in place with links, badges and a JSON feed.
- **Nostr (15 Nov):** each run publishes one signed NIP-87 review per mint (kind 38000, cashu.me's `[n/5]` format plus a `rating` tag for Sovran; A=5 … F=1, partial runs capped at 4), re-sent only when it changes. Profile and NIP-05 at `_@nutcheck.org`.
- **CI** runs all 60 tests on every push, including the full fault matrix against a real Nutshell mint behind the naughty proxy.

## Plan to the 17 Nov show-and-tell

| Day | Work |
|---|---|
| Fri 13 | ✅ Spike: CLI, checks, proxy, fault matrix |
| Fri 13 | ✅ Static scorecard, daily GitHub workflow, first live runs (10 mints, CDK and Nutshell) |
| Fri 13 | ✅ Funded checks on live mints via Nostr Wallet Connect (leftover sats paid back each run); duplicate-tag issue traced to CDK PR #2888 and Nutshell PR #1008 |
| Sat 14 | ✅ Rate-limit-safe client, encrypted leftover recovery, scorecard polish, nutcheck.org, CI |
| Sun 15 | ✅ Signed Nostr publishing: NIP-87 kind 38000 reviews |
| Mon 16 | Error-code check, `SIG_ALL` tests, keep testing after a `fee_lie` failure instead of stopping; rehearse the demo |
| Tue 17 | Show-and-tell: honest mint → A, then flip proxy modes live and watch the grade fall with evidence |

**Demo script:** run against the honest mint (A) → `lock_bypass` (F, every lock check red) → `keep_change` (C, "23 sat kept by the mint") → `keyset_tamper` (F, ID mismatch plus DLEQ failures) → back to honest.

## Open questions

1. **Nostr format.** Reuse an existing mint-review kind (NIP-87 recommendations) with structured tags, or a dedicated kind with the full JSON report? What does Sovran already parse?
2. **Fingerprinting.** Mints could spot NutCheck (deliberately invalid requests, fixed 64-sat budget) and behave well only for it. Copy the round-robin auditor's IP rotation? Randomise budget and test order?
3. **External melts.** The fee-return test uses the mint's own invoice. Paying a *different* mint's invoice (like the auditor) tests real Lightning payouts, but the true routing fee is then unknown. Report change in relation to `fee_reserve` instead?
4. **CDK vs Nutshell differences.** Which behaviours differ legitimately (error codes, empty outputs, rate limits), and how do we keep from punishing a mint for its implementation's choices?
5. **Grading weights.** Is "missing DLEQ while claiming NUT-12" really a B? Should a stale clock affect the grade at all?

## Ethics and responsible disclosure

- Funded tests are ordinary wallet operations, and invalid requests are refused as normal. NutCheck never tries to overload a mint and keeps to a handful of requests per run (39 in the spike).
- Before publishing a **critical or high** failure for a live mint, contact the operator privately (info-page contact, or the implementation's security channel if the bug is in CDK/Nutshell) and wait 14 days, or until it's fixed. Implementation-level bugs go upstream first, since they affect every mint running that software.
- Results are signed, dated and carry evidence, so operators can reproduce them with the same CLI.

## Costs

About 15 sat per funded run per mint (input fees plus 2 sat burned on purpose). Funded runs are daily: measured on 14 Nov at about 107 sat a day for the 7 mints that can be funded (Minibits only 4 sat, having no input fee), so about 3,200 sat a month; testnut is free. Each extra funded mint adds about 500 sat a month. Hosting is free (GitHub Actions and Pages); the domain is the only fixed cost.

## Status (23 Nov)

- **Submitted** to Nut November; updates count until 27 Nov 23:59 UTC, winners on 30 Nov.
- **Four new checks**, prompted by Mintprobe (another Nut November mint tester, which renamed itself from "Nutcheck" on 22 Nov) finding CDK issues NutCheck didn't test:
  - `htlc.refund` / `htlc.locktime`: NUT-14 sender pathway. The refund key alone (no preimage) must spend an expired HTLC, and must not spend an active one. On failure NutCheck takes the sats back through the receiver pathway. CDK 0.18.x is expected to fail (cdk#2980; fixed on main by #2641, unreleased).
  - `p2pk.sig_all`: a SIG_ALL signature must commit to the outputs. Per-input signatures and outputs swapped after signing must be refused; one signature over everything in the first input must be accepted. High if it leaks (whoever relays a signed transaction could redirect it), medium if a valid spend is refused.
  - `ws.subscribe` (free): a claimed NUT-17 WebSocket must answer, and a proof_state subscription to a never-seen proof must get OK and then UNSPENT. Checked by hand on 23 Nov from a browser: all four CDK mints reply OK and then stay silent (cdk#2983 in review); all five Nutshell mints send UNSPENT. Low severity.
  - `mint.quote_lock`: NUT-20 on the melt's internal 8-sat quote, so it costs nothing extra. No signature or a wrong key must be refused (high if not); current message format first, legacy as a fallback that earns a low warning. All ten mints claim NUT-20.
- **Cost:** about 3 sat more per funded run (two extra lock spends at the input fee).
- **One grade, two lines.** The asterisk is gone. Each mint shows one grade (latest free + latest paid results, as before) and two lines under it: "Free checks · date · passed" and "Paid checks · date · 1 failed", or "not run: mint can't issue ecash / funding failed / free checks only for this mint". Nostr reviews say the same in words; a mint without paid results still gets at most 4/5.
- **39 mints** (was 10), every one able to run the paid checks on real Lightning: the reachable mints listed by MintRadar's API or cashumints.space on 23 Nov, deduplicated by mint pubkey (satscribe.me/cashu = mint.satscribe.me, nostrodomo = sharegap). After the first 49-mint run (#45) we kept only mints that can do paid checks, dropping: 8333.space:3338, mint.unclerics.cash and an LNbits mint on bb.mountainlake.io (didn't answer); hashu.io and lekmint.cash (no sat Lightning minting); Agorist (can't create invoices) and Voltz (bolt11 minting disabled); mint.lndiscs.duckdns.org (HTTP 500 on swaps, so runs stop before the paid checks). After run #46 the fake-Lightning mints went too: testnut.cashu.space (a test mint by design) and one directory-listed mint whose invoices are marked paid by themselves (not named here: a critical finding, so the 14-day disclosure rule applies).
- **Schedule:** free checks on every mint at 05:17 and 17:17 UTC, paid checks in the 05:17 run. Sweeps test six mints at a time (`--jobs 6`); wallet calls are serialised so the per-run cap (now 4,000 sat paid out) holds. One mint failing to pay back no longer stops funding for the rest, as long as its ecash is stashed; a wallet error, an unstashable leftover or three unexpected pay-back errors still do (see "Fixes after #46").
- **Expected cost:** about 15–18 sat per paid mint per day, so roughly 550–650 sat a day (~18,000 sat a month) for 35 mints, to be tuned down after the hackathon.
- **Run #45 (first 49-mint run) and its fixes.** The red step was the deliberate alert, for two reasons. (1) Three pay-back melts timed out at NutCheck's 20 s request limit while their Lightning payments were still settling (Coinos, cashu.centurymetadata.org, mint.103100.xyz); three failures stop funding, so about 30 mints ran free checks only. Fix: pay-back melts get 90 s, and after a timeout NutCheck asks the mint how the payment ended (up to 120 s) instead of retrying with the same proofs. (2) A false high-severity failure on mint.mjex.me (C): Nutshell 0.20.0 also lists each keyset under its pre-2024 base64 ID (`base64(sha256(hex keys))[:12]`), an alias of the same keys, which NutCheck read as key reuse and a keyset-ID mismatch. Fix: legacy aliases that derive from the same keys are recognised and excluded from the key-reuse check. Do not contact that operator.
- **New checks live (run #45):** every CDK mint with paid checks fails `htlc.refund` ("Secret is not a HTLC secret") and `ws.subscribe`, as Mintprobe reported.
- **Run #46 (first run with all fixes, 41 mints):** 16 A, 9 A−, 16 B; mjex back to A. Funding stopped again after five CDK mints answered "Payment failed" to the pay-back (cashu.centurymetadata.org, mint.103100.xyz, mint.hanbitkorea.org, mint.lemonfizz.st, mint.lnw.cash): a routing or liquidity problem on their side, the first two failing in #45 too. 13 mints got free checks only; about 310 sat sit stashed as ecash on those five. Other findings: CDK 0.15–0.17 and Nutshell 0.20.2 accept only the old NUT-20 signature (warning); Nutshell 0.20.1–0.20.2 refused a current-format SIG_ALL spend; 21mint.me's clock is 35 minutes off; one mint issued ecash for an unpaid invoice (fake Lightning).
- **Fixes after #46:** mint-side pay-back failures ("Payment failed", 5xx, timeouts) are that mint's problem: its ecash stays stashed, the others keep being funded, and it isn't funded again while a stash is pending (its page says "earlier pay-back still pending" and keeps its last paid results for up to 8 days). Only wallet errors, unstashable leftovers or three unexpected pay-back errors stop a run. New `mint.requires_payment` check (critical) for fake Lightning; `SIG_ALL` legacy fallback; testnut and the fake mint removed. The scorecard shows the last 7 runs (mint pages up to 30).
- **Run #47 (39 mints):** 15 A, 11 A−, 12 B, 1 C; 25 mints got paid checks. Funding stopped once more, this time because the wallet couldn't pay one mint's invoice (mint.mjex.me, NWC `PAYMENT_FAILED`), which NutCheck treated as the wallet failing; the 13 mints after it, always the same tail of the list, got free checks only. The two mints with pending stashes were correctly skipped. New finding: mint.mintlnd.org (Nutshell 0.22.1) refused a swap of its own fresh ecash with "keyset balance is insufficient for redemption" and couldn't pay the leftover back either: high, graded C, details withheld for 14 days; contact the operator privately. Its 64 sat are stashed.
- **Fixes after #47:** a failed funding payment skips only that mint (the wallet is considered down on `INSUFFICIENT_BALANCE`, `QUOTA_EXCEEDED`, `UNAUTHORIZED`, `RESTRICTED`, `RATE_LIMITED`, relay errors, or three failures in a row); mints left out say why ("funding stopped earlier in this run", "NutCheck's wallet couldn't pay this mint", "not run yet"); paid runs start with the mints whose last paid run is oldest; a low-only failure shows "1 failed (low)" with an amber mark; the fee-check failure now says the mint charges a different fee *or refuses valid ecash*.
- **Run #48 (39 mints, first with all fixes):** funding never stopped. 30 mints got paid checks (16 A, 14 B, 9 A− including carried results); the mints left out in #47 went first. Not funded: four with pending stashes (centurymetadata, hanbitkorea, lemonfizz, mintlnd, whose retry failed again), two the wallet has no route to (mjex, sortug), nuts.cash (its Lightning backend can't make invoices), sovran.money (minimum mint 100 sat), sharegap (NutCheck crashed on a read timeout). Four more CDK mints failed to pay back (103100, lnw, nostrzap, ucash), so eight mints now hold about 500 sat, retried every run. The 05:17 UTC scheduled run didn't start on 23 Nov; GitHub skipped it.
- **Fixes after #48:** request timeouts and connection failures are "mint busy, run incomplete" (`MintTimeout`), never a crash; a POST that times out is never repeated. Mints with a NUT-04 `min_amount` are funded with that minimum + 64 sat and the internal melt uses the minimum (proxy mode `min_mint_100`).
- **24 Nov:** the scheduled paid run started ~10½ h late (run #50, 15:55 UTC) and the 17:17 free run never started, so the paid run is started by hand until judging. Mints without paid results looked like an A (sharegap: free checks passed, its invoice requests time out; mjex: one-channel Lightning node, no route from Rizful). Now: no letter without paid results (**?**, no Nostr review); a timeout during the paid checks belongs to the paid line; mjex, sortug, nuts.cash and sharegap dropped: **35 mints**. False clock warning on 21mint.me: its nginx caches `/v1/info`, so the `time` field was as old as the cached copy (19 s with a plain request, 0 s cache-busted; 19–35 min in runs). The clock check now re-reads `/v1/info?nocache=…` before warning and records `info_cached_s`.
- **24 Nov (later):** asked CDK on #2888 to backport the duplicate-tag fix; the maintainer added it to the 0.18.4 milestone and cdk-bot opened the backport (#2995). The HTLC refund backport (#2985) is already merged into v0.18.x, so a 0.18.4 should move every CDK mint from B towards A. Scorecard notes for both issues now say "fix coming in CDK 0.18.4" and link the backports.
- **Tests:** 93 (68 after the new checks, then parallel sweeps, free-only sweeps, the two-line display, the schedule guard, slow pay-backs, legacy keyset IDs, fake Lightning, payout failures, the SIG_ALL fallback, the 7-run history, single failed payments, run order, the amber mark, timeouts, minimum mint amounts, the "?" grade and cached info pages), including the SIG_ALL and NUT-20 spec vectors and a 20-case fault matrix (18 faults, a 100-sat-minimum mint and the honest mint) against Nutshell 0.21.0.

## Submission checklist

- [x] Repo public with README, licence (MIT) and the test suites (github.com/rademenezbtc/nutcheck)
- [x] Demo video at https://nutcheck.org/demo/ (scorecard, passing and failing mint, naughty-proxy proof, wallet showing the Nostr review)
- [x] At least 5 live mints graded with the funded checks (28 in run #46; all failures so far are public upstream issues, except the fake-Lightning mint)
- [x] Nostr events visible in a client (first review seen on relay.primal.net, 15 Nov)
- [x] Scorecard page live at nutcheck.org
- [x] CI running the fault matrix against a real mint
- [x] This brief updated with what changed
- [x] Comment on CDK PR #2888 with NutCheck's evidence (14 Nov)
- [x] Ask CDK to backport the duplicate-tag fix (24 Nov): on the 0.18.4 milestone, backport #2995 open
- [ ] Contact the fake-Lightning mint's operator privately (critical, first seen 23 Nov; it is off the scorecard)
- [ ] Contact mint.mintlnd.org's operator privately ("keyset balance is insufficient for redemption", high, first seen 23 Nov), then set `"notified"` for it in `scorecard.json`
- [x] ~~Tell the Voltz operator about Nutshell 0.21.0~~ (Voltz dropped from the scorecard: bolt11 minting disabled)
- [x] Submit at nutnovember.org/form, saying all work is new since 13 Nov (updates count until 27 Nov 23:59 UTC)
