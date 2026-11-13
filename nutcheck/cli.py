"""nutcheck <mint-url> — grade a Cashu mint on whether it behaves as the NUTs require."""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from . import __version__, checks_free, checks_funded
from .client import MintClient
from .grade import grade
from .model import Result, Severity, Status
from .wallet import Wallet

ICON = {Status.PASS: "✔", Status.FAIL: "✘", Status.WARN: "!", Status.SKIP: "·", Status.ERROR: "?"}
COLOR = {Status.PASS: "32", Status.FAIL: "31", Status.WARN: "33", Status.SKIP: "90", Status.ERROR: "35"}


def _c(code, s, on):
    return f"\033[{code}m{s}\033[0m" if on else s


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nutcheck", description=__doc__)
    ap.add_argument("mint", help="mint URL, e.g. https://mint.example.com")
    ap.add_argument("--fund", type=int, default=64, metavar="SATS",
                    help="sats to mint for funded checks (default 64; most comes back as leftover ecash)")
    ap.add_argument("--free-only", action="store_true", help="run only checks that cost nothing")
    ap.add_argument("--wait", type=float, default=600, help="seconds to wait for the funding invoice")
    ap.add_argument("--json", metavar="FILE", help="write the full report (with evidence) as JSON")
    ap.add_argument("--leftover", metavar="FILE", default="nutcheck-leftover.token",
                    help="where to save remaining ecash (default: nutcheck-leftover.token)")
    ap.add_argument("--no-color", action="store_true")
    a = ap.parse_args(argv)
    color = sys.stdout.isatty() and not a.no_color

    client = MintClient(a.mint)
    wallet = Wallet(client)
    started = time.time()
    results: list[Result] = []

    def on_invoice(invoice, amount):
        print(f"\nPay this invoice ({amount} sat) to run the funded checks:\n\n{invoice}\n", flush=True)

    try:
        free, info = checks_free.run(client, wallet)
        results += free
        if not a.free_only and wallet.keysets:
            results += checks_funded.run(client, wallet, info, a.fund, a.wait, on_invoice)
    except Exception as e:  # noqa: BLE001
        results.append(Result("nutcheck.crash", "NutCheck completed", "-", Severity.INFO, Status.ERROR,
                              f"{type(e).__name__}: {e}", evidence={"trace": traceback.format_exc()[-2000:]}))

    g = grade(results)
    print(f"\nNutCheck {__version__} · {a.mint}\n")
    for r in results:
        line = f"  {_c(COLOR[r.status], ICON[r.status], color)} {r.title:<46} "
        nut = f"NUT-{r.nut}" if r.nut != "-" else ""
        line += _c("90", f"{nut:<7} {r.severity.value:<8}", color)
        print(line)
        if r.status in (Status.FAIL, Status.WARN, Status.ERROR):
            print(f"      {r.summary}")
    gcol = "32" if g["grade"].startswith("A") else "33" if g["grade"] in ("B", "C") else "31"
    print(f"\n  Grade: {_c(gcol + ';1', g['grade'], color)}"
          + ("  (partial: funded checks not run)" if g["partial"] else ""))
    for reason in g["reasons"]:
        print(f"    - {reason}")

    if wallet.proofs:
        Path(a.leftover).write_text(wallet.export_token() + "\n")
        print(f"\n  {wallet.balance} sat of leftover ecash saved to {a.leftover}")

    if a.json:
        Path(a.json).write_text(json.dumps({
            "nutcheck": __version__, "mint": a.mint, "started": int(started), "duration_s": round(time.time() - started, 1),
            **g, "results": [r.to_dict() for r in results],
            "exchanges": len(client.log),
        }, indent=2))
        print(f"  Report written to {a.json}")
    return 0 if g["grade"].startswith("A") else 1


if __name__ == "__main__":
    sys.exit(main())
