"""NutCheck — grade Cashu mints on whether they behave as the NUTs require.

    nutcheck <mint-url> [options]          test one mint
    nutcheck sweep scorecard.json          test every mint in a config, saving dated reports
    nutcheck site scorecard.json -o site/  build the static scorecard from saved reports
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, checks_free, checks_funded
from .client import Inconclusive, MintClient
from .grade import grade
from .model import Result, Severity, Status
from .wallet import Wallet

ICON = {Status.PASS: "✔", Status.FAIL: "✘", Status.WARN: "!", Status.SKIP: "·", Status.ERROR: "?"}
COLOR = {Status.PASS: "32", Status.FAIL: "31", Status.WARN: "33", Status.SKIP: "90", Status.ERROR: "35"}


def _c(code, s, on):
    return f"\033[{code}m{s}\033[0m" if on else s


def mint_slug(url: str) -> str:
    """Stable filesystem name for a mint: host[_port][_path]."""
    u = urlparse(url if "://" in url else "https://" + url)
    s = u.hostname or url
    if u.port:
        s += f"_{u.port}"
    if u.path.strip("/"):
        s += "_" + u.path.strip("/").replace("/", "_")
    return s.lower()


def audit(url: str, *, free_only=False, fund=64, wait=600.0, on_invoice=None, preload=None):
    """Run every check against one mint. Returns (report dict, wallet holding leftover proofs)."""
    client = MintClient(url)
    wallet = Wallet(client)
    started = time.time()
    results: list[Result] = []
    info: dict = {}
    on_invoice = on_invoice or (lambda inv, amt: None)
    try:
        free, info = checks_free.run(client, wallet)
        results += free
        if "nutshell" in str(info.get("version", "")).lower():
            client.slow = True  # Nutshell limits transactions to 20/min per IP by default
        if preload and wallet.keysets:
            wallet.proofs += preload  # change kept from an earlier run; paid back with this run's leftovers
        if not free_only and wallet.keysets:
            results += checks_funded.run(client, wallet, info, fund, wait, on_invoice)
    except Inconclusive as e:
        results.append(Result(
            "nutcheck.inconclusive", "Mint answered every request", "-", Severity.INFO, Status.ERROR,
            f"Run incomplete: {e}. The remaining checks were not run. This is not a mark against the mint; "
            "it usually means the mint was busy or limits how fast one client may send requests."))
    except Exception as e:  # noqa: BLE001
        results.append(Result("nutcheck.crash", "NutCheck completed", "-", Severity.INFO, Status.ERROR,
                              f"{type(e).__name__}: {e}", evidence={"trace": traceback.format_exc()[-2000:]}))
    g = grade(results)
    issued = any(r.id == "mint.issue" for r in results)  # ecash was actually minted from a paid quote
    preloaded = sum(p.amount for p in (preload or []))
    reachable = not any(r.id == "info.wellformed" and r.summary.startswith("GET /v1/info failed") for r in results)
    report = {
        "nutcheck": __version__,
        "mint": url,
        "slug": mint_slug(url),
        "started": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
        "duration_s": round(time.time() - started, 1),
        "reachable": reachable,
        **g,
        "spent_sat": (fund + preloaded - wallet.balance) if issued else 0,
        "requests": len(client.log),
        "rate_limited": client.rate_limited,
        "mint_info": {
            "name": info.get("name"), "version": info.get("version"),
            "description": info.get("description"), "contact": info.get("contact"),
            "nuts": sorted(info.get("nuts", {}), key=lambda x: int(x) if str(x).isdigit() else 999),
            "keysets": [{"id": k.id, "unit": k.unit, "active": k.active, "input_fee_ppk": k.input_fee_ppk}
                        for k in wallet.keysets.values()],
        },
        "results": [r.to_dict() for r in results],
    }
    return report, wallet


def print_report(report: dict, color: bool):
    print(f"\nNutCheck {report['nutcheck']} · {report['mint']}\n")
    for r in report["results"]:
        st = Status(r["status"])
        line = f"  {_c(COLOR[st], ICON[st], color)} {r['title']:<46} "
        nut = f"NUT-{r['nut']}" if r["nut"] != "-" else ""
        line += _c("90", f"{nut:<7} {r['severity']:<8}", color)
        print(line)
        if st in (Status.FAIL, Status.WARN, Status.ERROR):
            print(f"      {r['summary']}")
    g = report["grade"]
    gcol = "32" if g.startswith("A") else "33" if g in ("B", "C") else "31"
    print(f"\n  Grade: {_c(gcol + ';1', g, color)}" + ("  (partial: funded checks not run)" if report["partial"] else ""))
    for reason in report["reasons"]:
        print(f"    - {reason}")


def save_report(report: dict, reports_dir: Path) -> Path:
    d = reports_dir / report["slug"]
    d.mkdir(parents=True, exist_ok=True)
    stamp = report["started"].replace(":", "").replace("-", "").replace("+0000", "Z")
    p = d / f"{stamp}.json"
    p.write_text(json.dumps(report, indent=2))
    return p


def save_leftover(wallet: Wallet, path: Path):
    """Leftover ecash is spendable by whoever holds the file: never inside a published directory."""
    if wallet.proofs:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(wallet.export_token() + "\n")


# ---- commands -------------------------------------------------------------------------------

def cmd_check(argv):
    ap = argparse.ArgumentParser(prog="nutcheck", description="Grade one Cashu mint.")
    ap.add_argument("mint", help="mint URL, e.g. https://mint.example.com")
    ap.add_argument("--fund", type=int, default=64, metavar="SATS",
                    help="sats to mint for funded checks (default 64; most comes back as leftover ecash)")
    ap.add_argument("--free-only", action="store_true", help="run only checks that cost nothing")
    ap.add_argument("--wait", type=float, default=600, help="seconds to wait for the funding invoice")
    ap.add_argument("--json", metavar="FILE", help="write the full report (with evidence) as JSON")
    ap.add_argument("--save", metavar="DIR", help="also save the report under DIR/<mint>/<timestamp>.json")
    ap.add_argument("--leftover", metavar="FILE", default="nutcheck-leftover.token",
                    help="where to save remaining ecash (default: nutcheck-leftover.token)")
    ap.add_argument("--no-color", action="store_true")
    a = ap.parse_args(argv)

    def on_invoice(invoice, amount):
        print(f"\nPay this invoice ({amount} sat) to run the funded checks:\n\n{invoice}\n", flush=True)

    report, wallet = audit(a.mint, free_only=a.free_only, fund=a.fund, wait=a.wait, on_invoice=on_invoice)
    print_report(report, sys.stdout.isatty() and not a.no_color)
    if wallet.proofs and a.leftover != "/dev/null":
        Path(a.leftover).write_text(wallet.export_token() + "\n")
        print(f"\n  {wallet.balance} sat of leftover ecash saved to {a.leftover}")
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2))
        print(f"  Report written to {a.json}")
    if a.save:
        print(f"  Saved to {save_report(report, Path(a.save))}")
    return 0 if report["grade"].startswith("A") else 1


def load_config(path: str) -> tuple[dict, Path]:
    p = Path(path)
    cfg = json.loads(p.read_text())
    return cfg, p.parent


def cmd_sweep(argv):
    import os
    ap = argparse.ArgumentParser(prog="nutcheck sweep", description="Test every mint in a scorecard config.")
    ap.add_argument("config")
    ap.add_argument("--only", metavar="SLUG", action="append", help="test only these mints")
    ap.add_argument("--nwc", action="store_true",
                    help='fund mints marked "funded": "nwc" from the wallet in the NWC_URL environment variable')
    ap.add_argument("--funding-alerts", metavar="FILE", help="write funding problems here (one per line)")
    a = ap.parse_args(argv)
    cfg, base = load_config(a.config)
    reports_dir = base / cfg.get("reports_dir", "reports")
    wallet_dir = base / cfg.get("wallet_dir", "wallet")
    funder = None
    if a.nwc:
        from .funding import Funder
        from .nwc import NWC
        uri = os.environ.get("NWC_URL", "")
        if not uri:
            print("--nwc given but NWC_URL is not set", file=sys.stderr)
            return 2
        funder = Funder(NWC(uri), int(cfg.get("nwc_max_sat_per_run", 1000)),
                        stash_dir=base / cfg.get("stash_dir", "stash"))
        for line in funder.recover():
            print(f"stash: {line}")
            if "recovered" not in line and "nothing left" not in line:
                funder.alerts.append(f"stash: {line}")
    for m in cfg["mints"]:
        slug = mint_slug(m["url"])
        if a.only and slug not in a.only:
            continue
        mode = m.get("funded", False)
        if mode == "nwc":
            use_nwc = funder is not None and not funder.stopped
            free_only = not use_nwc
            pay = funder.pay if use_nwc else None
        else:
            use_nwc, free_only, pay = False, not mode, None
        on_invoice = pay or (lambda inv, amt, s=slug: print(f"  [{s}] invoice for {amt} sat not paid automatically", flush=True))
        preload = funder.take_stash(m["url"]) if (funder and mode == "nwc") else []
        paid_before = funder.paid_out if funder else 0
        report, wallet = audit(m["url"], free_only=free_only, fund=m.get("fund", 64), wait=m.get("wait", 60),
                               on_invoice=on_invoice, preload=preload)
        if funder and mode == "nwc":  # never pay a test mint's fake ecash back into the real wallet
            paid_in = funder.paid_out - paid_before
            report["funding"] = {"source": "nwc", "paid_in": paid_in, "funded": paid_in > 0,
                                 "carried_in": sum(p.amount for p in preload), **funder.pay_back(wallet, slug)}
        path = save_report(report, reports_dir)
        save_leftover(wallet, wallet_dir / f"{slug}.tokens")
        fund_note = ""
        if report.get("funding"):
            f = report["funding"]
            fund_note = f" [nwc: returned {f['returned']} of {f['leftover']} sat{'' if f['ok'] else ', PAY-BACK FAILED'}]"
        print(f"{report['grade']:<3} {slug:<40} {'partial ' if report['partial'] else ''}→ {path}{fund_note}")
    if funder:
        print(f"NWC: paid out {funder.paid_out} sat, paid back {funder.paid_back} sat"
              + (f"; funding stopped: {funder.stopped}" if funder.stopped else ""))
        if a.funding_alerts:
            Path(a.funding_alerts).write_text("".join(f"Funding: {x}\n" for x in funder.alerts))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "sweep":
        return cmd_sweep(argv[1:])
    if argv and argv[0] == "site":
        from .site import cmd_site
        return cmd_site(argv[1:])
    return cmd_check(argv)


if __name__ == "__main__":
    sys.exit(main())
