"""Static scorecard: turns saved NutCheck reports into a public site.

    nutcheck site scorecard.json -o site/

Writes index.html, one page per mint, badge SVGs, api/mints.json and the latest report per mint.
No JavaScript is required to read it; a few lines add grade filters.

Disclosure: a critical or high failure is shown as a grade only, with details withheld, until
`embargo_days` (default 14) after it was first seen, unless the mint's config sets "disclosed": true.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from .cli import load_config, mint_slug
from .model import CATEGORIES, FREE_CATEGORIES, FUNDED_CATEGORIES, category_of

# grade → (tile background, tile text, soft background, soft text, history bar)
GRADE_STYLE = {
    "A": ("#0E7A5A", "#FFFFFF", "#DDF1E8", "#0B5D45", "#2E9E78"),
    "B": ("#2563A8", "#FFFFFF", "#E1ECF8", "#1F5596", "#4D86C6"),
    "C": ("#A85E00", "#FFFFFF", "#FBEBD0", "#8A4F00", "#D39A3A"),
    "D": ("#A85E00", "#FFFFFF", "#FBEBD0", "#8A4F00", "#D39A3A"),
    "F": ("#B8321A", "#FFFFFF", "#F9DDD6", "#A32A12", "#D2553B"),
    "—": ("#E2E2DC", "#2A2D35", "#ECECE7", "#4A4F5B", "#D9D9D3"),
}
SEVERE = {"critical", "high"}
FAIL_PHRASE = {
    "swap.rejects_forgery": "Accepts forged ecash",
    "swap.no_overspend": "Signs more than it receives",
    "swap.double_spend": "Accepts double spends",
    "swap.duplicate_input": "Counts one proof twice",
    "swap.fee_exact": "Charges a different fee than it advertises",
    "mint.issue": "Short-changes paid quotes",
    "mint.quote_replay": "Mints the same quote twice",
    "keys.keyset_id": "Keyset IDs don't match their keys",
    "keys.unique": "Reuses keys across amounts",
    "keys.valid_points": "Publishes invalid keys",
    "melt.fee_return": "Keeps overpaid Lightning fees",
    "melt.pays": "Fails to pay invoices",
    "checkstate.spent": "Reports spent proofs as unspent",
    "info.claims_answer": "Advertises features that don't work",
    "transport.https": "Served without HTTPS",
    "p2pk.refund": "Refund keys don't work after locktime",
    "p2pk.malformed": "Accepts P2PK locks with duplicated tags",
}
BLURB = {
    "Keys": "Valid keys, one per amount, keyset IDs re-derive",
    "Forgery": "Ecash with a made-up signature is refused",
    "DLEQ": "DLEQ proofs on every signature (mint, swap, change)",
    "Issuing": "Paid quotes issue the full amount, once",
    "Spending": "No overspend or double spend, advertised fee is the real fee",
    "Locks": "P2PK and HTLC conditions enforced",
    "Payouts": "Melt pays, overpaid fees returned",
    "Info": "HTTPS, info page honest, claimed features work",
}


def base_grade(g: str) -> str:
    return g[0] if g and g[0] in GRADE_STYLE else "—"


def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%-d %b %Y, %H:%M UTC")


def fmt_day(dt: datetime) -> str:
    return dt.strftime("%-d %b")


def _lc(s: str) -> str:
    """Lower-case only the first letter, so names like Lightning and NUT-12 survive."""
    return s[:1].lower() + s[1:]


def phrase_for(r: dict) -> str:
    rid = r["id"]
    if rid in FAIL_PHRASE:
        return FAIL_PHRASE[rid]
    if rid.startswith("dleq."):
        return "Invalid DLEQ proofs" if r["severity"] == "critical" else "No DLEQ proofs despite claiming NUT-12"
    if rid.startswith("p2pk."):
        return "Ignores P2PK spending conditions"
    if rid.startswith("htlc."):
        return "Ignores HTLC spending conditions"
    return f"Failed: {r['title']}"


SEV_ORDER = ["critical", "high", "medium", "low", "info"]


def _version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def known_issue(cfg: dict, report: dict, check_id: str) -> dict | None:
    """A config entry explaining a failure that comes from the mint's software, e.g. a bug fixed upstream.
    Matches on check id, a software name fragment, and optionally "below": versions older than a fix."""
    version = (report.get("mint_info", {}).get("version") or "").lower()
    for k in cfg.get("known_issues", []):
        if k.get("check") != check_id or k.get("software", "").lower() not in version:
            continue
        if k.get("below"):
            found = _version_tuple(version.split("/")[-1])
            if not found or found >= _version_tuple(k["below"]):
                continue
        return k
    return None


@dataclass
class MintView:
    cfg: dict
    slug: str
    reports: list[dict]               # oldest → newest
    latest: dict
    withheld: bool = False
    first_seen: datetime | None = None
    publish_on: datetime | None = None
    cats: dict = field(default_factory=dict)
    site_cfg: dict = field(default_factory=dict)

    @property
    def name(self):
        name = self.cfg.get("name") or self.latest["mint_info"].get("name") or self.slug
        return name.strip().strip('"\'“”').strip() or self.slug

    @property
    def url(self):
        return self.cfg["url"]

    @property
    def grade(self) -> str:
        if not self.latest.get("reachable", True):
            return "—"
        return self.latest["grade"] + ("*" if self.latest.get("partial") else "")

    @property
    def band(self) -> str:
        g = self.grade
        if g == "—":
            return "off"
        return {"A": "a", "B": "bc", "C": "bc", "D": "bc", "F": "f"}[g[0]]

    @property
    def fails(self) -> list[dict]:
        rs = [r for r in self.latest["results"] if r["status"] in ("fail", "warn", "error")]
        return sorted(rs, key=lambda r: (r["status"] != "fail", SEV_ORDER.index(r["severity"])))

    @property
    def software(self) -> str:
        v = self.latest["mint_info"].get("version") or "unknown software"
        return v.replace("/", " ")

    @property
    def fee(self) -> str:
        ks = [k for k in self.latest["mint_info"].get("keysets", []) if k.get("active")]
        if not ks:
            return ""
        f = ks[0]["input_fee_ppk"]
        return f"fee {f} ppk" if f else "no input fee"

    @property
    def meta(self) -> str:
        if self.grade == "—":
            return "Unreachable in the last run"
        partial = " · free checks only" if self.latest.get("partial") else ""
        return f"{self.software} · {self.fee}{partial}"

    def tip(self, cat: str, state: str) -> str:
        """Tooltip for one category mark: what was checked, and what went wrong if anything."""
        rs = [r for r in self.latest["results"] if category_of(r["id"]) == cat]
        if state == "p":
            return f"{cat}: all {sum(r['status'] == 'pass' for r in rs)} checks passed"
        bad = [r for r in rs if r["status"] in ("fail", "warn", "error")]
        if not bad:
            why = "funded checks not run this time" if self.latest.get("partial") else "not applicable"
            return f"{cat}: {why}"
        lines = [f"{cat}:"]
        for r in bad:
            first = r["summary"].removeprefix("Run incomplete: ").split(". ")[0].rstrip(".")
            first = first[:1].upper() + first[1:]
            if len(first) > 150:
                first = first[:147].rstrip() + "…"
            label = {"fail": "Failed", "warn": "Warning", "error": "Inconclusive"}[r["status"]]
            line = f"{label}: {first}"
            k = known_issue(self.site_cfg, self.latest, r["id"]) if r["status"] == "fail" else None
            if k:
                line += f" ({k.get('label', 'known upstream issue, fix pending')})"
            lines.append(line)
        return "\n".join(lines)


def build_views(cfg: dict, base: Path, now: datetime, embargo_days: int) -> list[MintView]:
    reports_dir = base / cfg.get("reports_dir", "reports")
    views = []
    for m in cfg["mints"]:
        slug = mint_slug(m["url"])
        files = sorted((reports_dir / slug).glob("*.json"))
        if not files:
            continue
        reports = [json.loads(f.read_text()) for f in files]
        v = MintView(m, slug, reports, reports[-1], site_cfg=cfg)
        latest_severe = {r["id"] for r in v.latest["results"] if r["status"] == "fail" and r["severity"] in SEVERE}
        if not v.latest.get("reachable", True):
            latest_severe = set()  # being offline is not a vulnerability to disclose
        if latest_severe:
            first = v.latest["started"]
            for rep in reversed(reports):
                failing = {r["id"] for r in rep["results"] if r["status"] == "fail"}
                if failing & latest_severe:
                    first = rep["started"]
                else:
                    break
            v.first_seen = datetime.fromisoformat(first)
            v.publish_on = v.first_seen + timedelta(days=embargo_days)
            v.withheld = now < v.publish_on and not m.get("disclosed", False)
        for cat in CATEGORIES:
            rs = [r for r in v.latest["results"] if category_of(r["id"]) == cat]
            sts = {r["status"] for r in rs}
            v.cats[cat] = ("f" if "fail" in sts else "w" if ("warn" in sts or "error" in sts)
                           else "p" if "pass" in sts else "s")
        views.append(v)
    order = {"a": 0, "bc": 1, "f": 2, "off": 3}
    grade_rank = ["A+", "A", "A-", "A*", "A-*", "B", "B*", "C", "C*", "D", "F", "F*", "—"]
    views.sort(key=lambda v: (order[v.band], grade_rank.index(v.grade) if v.grade in grade_rank else 99, v.name.lower()))
    return views


# ---- HTML ---------------------------------------------------------------------------------------

ICONS = {
    "p": '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3 7.3l2.6 2.6L11 4.4" fill="none" stroke="#0B5D45" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "f": '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M4 4l6 6M10 4l-6 6" fill="none" stroke="#A32A12" stroke-width="2" stroke-linecap="round"/></svg>',
    "w": '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M7 3.5v4.5M7 10.4v.1" fill="none" stroke="#8A4F00" stroke-width="2" stroke-linecap="round"/></svg>',
    "s": '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M4 7h6" fill="none" stroke="#8A8F99" stroke-width="2" stroke-linecap="round"/></svg>',
}
LOCK = '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><rect x="2.5" y="6" width="9" height="6.5" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M4.5 6V4.5a2.5 2.5 0 015 0V6" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>'
LOGO = ('<svg width="36" height="36" viewBox="0 0 120 120" aria-hidden="true"><g transform="rotate(-28 58 60)">'
        '<path d="M58 12 C71 12 82 22 82 36 C82 46 74 49 74 56 C74 63 86 68 86 80 C86 96 73 108 58 108 C43 108 30 96 30 80 C30 68 42 63 42 56 C42 49 34 46 34 36 C34 22 45 12 58 12 Z" fill="#C98A3E"/></g>'
        '<circle cx="90" cy="88" r="27" fill="#FFFFFF"/><circle cx="90" cy="88" r="22" fill="#0E7A5A"/>'
        '<path d="M79 88 L87 96 L102 79" fill="none" stroke="#FFFFFF" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/></svg>')
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"><g transform="rotate(-28 58 60)">'
           '<path d="M58 12 C71 12 82 22 82 36 C82 46 74 49 74 56 C74 63 86 68 86 80 C86 96 73 108 58 108 C43 108 30 96 30 80 C30 68 42 63 42 56 C42 49 34 46 34 36 C34 22 45 12 58 12 Z" fill="#C98A3E"/></g>'
           '<circle cx="90" cy="88" r="29" fill="#F6F6F3"/><circle cx="90" cy="88" r="24" fill="#0E7A5A"/>'
           '<path d="M78 88 L87 97 L103 78" fill="none" stroke="#FFFFFF" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/></svg>\n')
WORD = {"p": "passed", "f": "failed", "w": "warning", "s": "not run"}

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#F6F6F3;color:#15171C;font:15px/1.5 Inter,system-ui,sans-serif}
a{color:#2E3FB8}a:hover{color:#1F2C8A}
h1,h2,h3,.disp{font-family:'Space Grotesk',Inter,sans-serif;letter-spacing:-.01em}
code,pre,.mono{font-family:'JetBrains Mono',ui-monospace,monospace}
.wrap{max-width:1280px;margin:0 auto;padding:0 32px}
@media (max-width:640px){.wrap{padding:0 16px}}
header.top{background:#fff;border-bottom:1px solid #E3E3DE}
header.top .wrap{min-height:64px;display:flex;flex-wrap:wrap;align-items:center;gap:24px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:#15171C;font-weight:700;font-size:20px}
nav.main{display:flex;flex-wrap:wrap;gap:4px;flex-grow:1}
nav.main a{padding:10px 12px;border-radius:8px;color:#3F4450;font-weight:500;font-size:14px;text-decoration:none}
nav.main a[aria-current]{background:#EEF0FB;color:#2E3FB8;font-weight:600}
.btn{display:inline-flex;align-items:center;min-height:44px;padding:0 16px;border-radius:10px;font-weight:600;font-size:14px;text-decoration:none}
.btn.dark{background:#15171C;color:#fff}.btn.dark:hover{color:#fff;background:#2A2D35}
.btn.line{border:1px solid #D6D6D0;color:#15171C;background:#fff}
main{padding:40px 0 64px;display:flex;flex-direction:column;gap:28px}
.card{background:#fff;border:1px solid #E3E3DE;border-radius:16px}
.muted{color:#5A5F6B}.small{font-size:13px}
.hero{display:flex;flex-wrap:wrap;gap:32px;align-items:flex-end}
.hero h1{margin:0 0 12px;font-size:44px;line-height:1.05;letter-spacing:-.02em}
.hero p{margin:0;font-size:17px;color:#4A4F5B;max-width:680px}
.stats{flex:1 1 440px;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.stat{background:#fff;border:1px solid #E3E3DE;border-radius:12px;padding:14px 16px}
.stat b{display:block;font-family:'Space Grotesk',sans-serif;font-size:30px}
@media (max-width:640px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.hero h1{font-size:34px}}
.note{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:flex-start;background:#FFF8E6;border:1px solid #F0DDA8;border-radius:12px;padding:14px 18px;font-size:14px;color:#4D3A00}
.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:16px 20px;border-bottom:1px solid #E3E3DE}
.pill{font:inherit;font-size:14px;font-weight:600;min-height:44px;padding:0 14px;border-radius:10px;cursor:pointer;background:#fff;color:#3F4450;border:1px solid #D6D6D0}
.pill[aria-pressed=true]{background:#15171C;color:#fff;border-color:#15171C}
.scroll{overflow-x:auto}
table{border-collapse:collapse}
table.mints{width:100%;min-width:1080px;font-size:14px}
table.mints th{text-align:left;color:#5A5F6B;font-size:12px;text-transform:uppercase;letter-spacing:.04em;font-weight:600;padding:12px 4px}
table.mints td{padding:14px 4px;border-top:1px solid #ECECE7;vertical-align:middle}
table.mints .c{text-align:center}
table.mints .gstart{border-left:1px solid #E3E3DE}
table.mints tr.groups th{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#3F4450;text-align:center;padding:12px 4px 0;border-top:0}
table.mints tr.groups td{border-top:0;padding:0}
table.mints thead tr:last-child th{padding-top:6px}
table.mints td:first-child,table.mints th:first-child{padding-left:20px}
table.mints td:last-child,table.mints th:last-child{padding-right:20px;text-align:right}
.gt{display:inline-flex;align-items:center;justify-content:center;min-width:44px;height:44px;padding:0 6px;border-radius:10px;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:20px}
.mname{font-weight:600;color:#15171C;text-decoration:none;font-size:15px}
.chip{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:8px;position:relative;cursor:default}
.chip:focus{outline:2px solid #2E3FB8;outline-offset:2px}
.tip{display:none;position:fixed;z-index:50;width:max-content;max-width:300px;background:#15171C;color:#F6F6F3;font-size:12.5px;line-height:1.45;font-weight:400;text-align:left;white-space:pre-line;padding:8px 11px;border-radius:8px;box-shadow:0 6px 20px rgba(21,23,28,.18);pointer-events:none}
.chip:hover .tip,.chip:focus .tip{display:block}
.chip.p{background:#E7F4EE}.chip.f{background:#FBE3DC}.chip.w{background:#FCEFD6}.chip.s{background:#F1F1EE}
.hist{display:flex;gap:3px}.hist span{width:8px;height:22px;border-radius:2px}
.withheld{display:inline-flex;gap:8px;align-items:center;background:#F4F4F1;border:1px dashed #C9C9C2;border-radius:8px;padding:8px 12px;font-size:13px;color:#3F4450}
.legend{display:flex;flex-wrap:wrap;gap:20px;align-items:center;padding:14px 20px;border-top:1px solid #E3E3DE;background:#FAFAF8;font-size:13px;color:#4A4F5B}
.three{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.three .card{padding:20px}.three h2{margin:0 0 8px;font-size:18px}.three p{margin:0;font-size:14px;color:#4A4F5B}
pre.code,code.block{display:block;background:#15171C;color:#E8E8E3;border-radius:8px;padding:12px 14px;font-size:12.5px;line-height:1.6;overflow-x:auto;white-space:pre;margin:0}
.head{display:flex;flex-wrap:wrap;gap:28px;align-items:center;padding:28px}
.bigg{width:120px;height:120px;border-radius:22px;display:flex;align-items:center;justify-content:center;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:72px;flex-shrink:0}
.tag{background:#F1F1EE;border-radius:6px;padding:4px 9px;font-size:13px;color:#3F4450}
.cols{display:flex;flex-wrap:wrap;gap:24px;align-items:flex-start}
.colmain{flex:999 1 640px;min-width:0;display:flex;flex-direction:column;gap:24px}
.colside{flex:1 1 320px;display:flex;flex-direction:column;gap:16px}
.colside .card{padding:20px}.colside h2{margin:0 0 8px;font-size:17px}
.why{border-radius:14px;padding:20px 22px}.why h2{margin:0 0 6px;font-size:20px}.why p{margin:0}
.sect h2{margin:0;padding:18px 22px;font-size:18px;border-bottom:1px solid #E3E3DE}
.fail{padding:22px;display:flex;flex-direction:column;gap:16px;border-top:1px solid #ECECE7}
.fail:first-of-type{border-top:0}
.fail h3{margin:0;font-size:17px;flex-grow:1}
.frow{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.sev{font-size:12px;font-weight:600;border-radius:6px;padding:4px 8px}
.ev{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.ev div{background:#FAFAF8;border:1px solid #ECECE7;border-radius:10px;padding:10px 14px}
.ev small{display:block;font-size:12px;color:#5A5F6B}.ev b{font-family:'JetBrains Mono',monospace;font-weight:500;font-size:18px}
details{border-top:1px solid #ECECE7}
details>summary{cursor:pointer;padding:14px 22px;display:flex;align-items:center;gap:10px;font-weight:600;list-style:none}
details>summary::-webkit-details-marker{display:none}
details.ex{border:1px solid #ECECE7;border-radius:10px}details.ex>summary{padding:12px 14px;font-size:14px}
details.ex pre{border-radius:0 0 10px 10px}
table.checks{width:100%;font-size:14px;margin-bottom:8px}
table.checks td{padding:10px 8px;border-top:1px solid #F1F1EE;vertical-align:top}
table.checks td:first-child{padding-left:22px;width:38%;font-weight:500}
table.checks td:last-child{padding-right:22px;color:#4A4F5B}
.hgrid{display:grid;grid-template-columns:repeat(15,minmax(0,1fr));gap:3px}.hgrid span{height:18px;border-radius:3px}
ol.tl{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:12px;font-size:14px}
ol.tl li{display:flex;gap:10px}ol.tl i{width:10px;height:10px;border-radius:50%;margin-top:6px;flex-shrink:0}
footer{padding:24px 0 48px;font-size:13px;color:#5A5F6B}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&amp;'
         'family=Space+Grotesk:wght@500;600;700&amp;family=JetBrains+Mono:wght@400;500&amp;display=swap" rel="stylesheet">')


def page(title: str, body: str, root: str, cfg: dict, current: str = "scorecard") -> str:
    repo = cfg.get("repo_url", "https://github.com/rademenezbtc/nutcheck")
    nav = [("scorecard", f"{root}index.html", "Scorecard"),
           ("method", f"{repo}#checks", "Methodology"),
           ("run", f"{repo}#install-and-run", "Run it yourself")]
    links = "".join(
        f'<a href="{h}"{" aria-current=page" if k == current else ""}>{t}</a>' for k, h, t in nav)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>{FONTS}<link rel="stylesheet" href="{root}style.css">
<link rel="icon" type="image/svg+xml" href="{root}favicon.svg">
<link rel="alternate" type="application/json" href="{root}api/mints.json"></head>
<body><header class="top"><div class="wrap">
<a class="brand disp" href="{root}index.html">{LOGO}<span>{escape(cfg.get('title', 'NutCheck'))}</span></a>
<nav class="main" aria-label="Main">{links}</nav>
<a class="btn dark" href="{repo}">Test your mint</a></div></header>
<div class="wrap">{body}</div>
<footer class="wrap">Built {escape(cfg['_built'])} by NutCheck · results reproducible with <code>pip install nutcheck</code> · <a href="{root}api/mints.json">JSON feed</a></footer>
</body></html>"""


def grade_tile(g: str, cls="gt") -> str:
    bg, fg, sbg, sfg, _ = GRADE_STYLE[base_grade(g)]
    if cls == "gt":
        return f'<span class="gt" style="background:{sbg};color:{sfg}" aria-label="Grade {escape(g)}">{escape(g)}</span>'
    return f'<div class="bigg" role="img" aria-label="Grade {escape(g)}" style="background:{bg};color:{fg}">{escape(g)}</div>'


def history_bars(v: MintView, n: int, cls="hist") -> str:
    reps = v.reports[-n:]
    spans = []
    for r in reps:
        g = "—" if not r.get("reachable", True) else r["grade"]
        spans.append(f'<span style="background:{GRADE_STYLE[base_grade(g)][4]}" title="{escape(fmt_time(r["started"]))}: {escape(g)}"></span>')
    label = "Grades over the last {} runs: {}".format(len(reps), " ".join(
        "—" if not r.get("reachable", True) else r["grade"] for r in reps))
    return f'<div class="{cls}" role="img" aria-label="{escape(label)}">{"".join(spans)}</div>'


def render_index(views: list[MintView], cfg: dict) -> str:
    n = len(views)
    a = sum(1 for v in views if v.band == "a")
    attn = sum(1 for v in views if v.band in ("bc", "f"))
    last = max((v.latest["started"] for v in views), default=None)
    last_s = datetime.fromisoformat(last).astimezone(timezone.utc).strftime("%H:%M") if last else "—"
    counts = {b: sum(1 for v in views if v.band == b) for b in ("a", "bc", "f", "off")}
    pills = [("all", "All", n), ("a", "A", counts["a"]), ("bc", "B–C", counts["bc"]), ("f", "F", counts["f"]),
             ("off", "Unreachable", counts["off"])]
    pill_html = "".join(
        f'<button type="button" class="pill" data-band="{b}" aria-pressed="{"true" if b == "all" else "false"}">'
        f'{t} <span style="opacity:.7">{c}</span></button>' for b, t, c in pills if c or b == "all")
    def _head(c):
        cls = "c gstart" if c == FUNDED_CATEGORIES[0] else "c"
        return f'<th scope="col" class="{cls}" title="{escape(BLURB[c])}">{c}</th>'
    heads = "".join(_head(c) for c in CATEGORIES)
    group_row = (f'<tr class="groups"><td colspan="2"></td>'
                 f'<th scope="colgroup" colspan="{len(FREE_CATEGORIES)}">Free checks</th>'
                 f'<th scope="colgroup" colspan="{len(FUNDED_CATEGORIES)}" class="gstart">Funded checks</th>'
                 f'<td colspan="2"></td></tr>')
    rows = []
    for v in views:
        if v.withheld:
            told = ", reported to the operator" if v.cfg.get("notified") else ""
            cells = (f'<td colspan="{len(CATEGORIES)}"><span class="withheld">{LOCK} Serious failure first seen '
                     f'{fmt_day(v.first_seen)}{told}. Details published '
                     f'{fmt_day(v.publish_on)}, or when fixed.</span></td>')
        else:
            cells = "".join(
                f'<td class="{"c gstart" if c == FUNDED_CATEGORIES[0] else "c"}"><span class="chip {s}" tabindex="0" aria-label="{escape(v.tip(c, s))}">{ICONS[s]}'
                f'<span class="tip" aria-hidden="true">{escape(v.tip(c, s))}</span></span></td>'
                for c, s in v.cats.items())
        rows.append(f"""<tr data-band="{v.band}">
<td>{grade_tile(v.grade)}</td>
<td><a class="mname" href="mints/{v.slug}.html">{escape(v.name)}</a>
<div class="mono small muted">{escape(v.url)}</div><div class="small muted" style="margin-top:4px">{escape(v.meta)}</div></td>
{cells}
<td>{history_bars(v, 14)}</td>
<td class="muted" style="white-space:nowrap">{escape(fmt_time(v.latest['started']))}</td></tr>""")
    body = f"""<main>
<section class="hero"><div style="flex:999 1 520px;min-width:0">
<h1>Do Cashu mints follow the rules?</h1>
<p>NutCheck runs protocol checks against each mint — DLEQ proofs, keyset IDs, P2PK and HTLC locks, double spends, payouts and fee refunds — and grades what it sees, with the evidence.</p></div>
<div class="stats"><div class="stat"><b>{n}</b><span class="small muted">mints tested</span></div>
<div class="stat"><b style="color:#0B5D45">{a}</b><span class="small muted">grade A</span></div>
<div class="stat"><b style="color:#A32A12">{attn}</b><span class="small muted">need attention</span></div>
<div class="stat"><b style="font-size:22px;line-height:1.6">{last_s}</b><span class="small muted">last run, UTC</span></div></div></section>
<div class="note" role="note"><strong style="white-space:nowrap">Behaviour, not solvency.</strong>
<span>An A means the mint followed the protocol in our last test. It says nothing about whether the mint holds the bitcoin behind its ecash. For uptime and payout history, see the Cashu round-robin auditor.</span></div>
<section class="card" aria-label="Mints" style="overflow:hidden">
<div class="toolbar"><div role="group" aria-label="Filter by grade" style="display:flex;flex-wrap:wrap;gap:6px">{pill_html}</div>
<span class="small muted" style="margin-left:auto">* free checks only · — unreachable</span></div>
<div class="scroll"><table class="mints"><thead>{group_row}<tr><th scope="col">Grade</th><th scope="col">Mint</th>{heads}
<th scope="col">Last 14 runs</th><th scope="col">Tested</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<div class="legend"><b style="color:#15171C">How grades work</b><span>The worst failure caps the grade:</span>
<span><b style="color:#A32A12">F</b> funds can be forged or stolen</span><span><b style="color:#8A4F00">C</b> funds withheld or privacy broken</span>
<span><b style="color:#1F5596">B</b> spec violation, limited impact</span><span><b style="color:#0B5D45">A−</b> warnings only</span></div></section>
<section class="three">
<div class="card"><h2>Check it yourself</h2><p style="margin-bottom:12px">Every grade can be reproduced with the same open-source tool, including on a mint you are about to launch.</p>
<code class="block">pip install nutcheck
nutcheck https://your.mint</code></div>
<div class="card"><h2>Evidence, not opinions</h2><p>Each mint page shows the exact request and response behind every failure. Proofs are reduced to counts so published evidence can never be spent.</p></div>
<div class="card"><h2>Responsible disclosure</h2><p>Critical and high failures are reported to the operator first. Details appear here {cfg['_embargo']} days after the failure was first seen, or once it is fixed.</p></div>
</section></main>
<script>
document.querySelectorAll('.chip').forEach(function(c){{var t=c.querySelector('.tip');if(!t)return;
function place(){{t.style.display='block';var r=c.getBoundingClientRect(),w=t.offsetWidth,h=t.offsetHeight;
var left=Math.min(Math.max(8,r.left+r.width/2-w/2),window.innerWidth-w-8);
var top=r.top-h-8<8?r.bottom+8:r.top-h-8;t.style.left=left+'px';t.style.top=top+'px';t.style.display='';}}
c.addEventListener('mouseenter',place);c.addEventListener('focus',place);}});
document.querySelectorAll('.pill').forEach(function(b){{b.addEventListener('click',function(){{
document.querySelectorAll('.pill').forEach(function(x){{x.setAttribute('aria-pressed',x===b?'true':'false')}});
var band=b.dataset.band;document.querySelectorAll('tr[data-band]').forEach(function(r){{
r.hidden=!(band==='all'||r.dataset.band===band)}})}})}});
</script>"""
    return page(cfg.get("title", "NutCheck") + " — Cashu mint scorecard", body, "", cfg)


def _evidence_tiles(ev: dict) -> str:
    items = [(k, v) for k, v in ev.items() if k != "exchange" and isinstance(v, (int, float, str)) and len(str(v)) < 40]
    if not items:
        return ""
    tiles = "".join(f'<div><small>{escape(k.replace("_", " ").capitalize())}</small><b>{escape(str(v))}</b></div>'
                    for k, v in items)
    return f'<div class="ev">{tiles}</div>'


def _known_box(k: dict | None) -> str:
    if not k:
        return ""
    link = f' <a href="{escape(k["url"])}">{escape(k.get("link_text", "Upstream fix"))}</a>' if k.get("url") else ""
    return (f'<div style="background:#EEF0FB;border:1px solid #CBD1F2;border-radius:10px;padding:12px 14px;font-size:14px;color:#22306E">'
            f'<b>Known issue in the mint software.</b> {escape(k["text"])}{link}</div>')


def _exchange(ev: dict) -> str:
    ex = ev.get("exchange")
    if not ex:
        return ""
    txt = (f"{ex['method']} {ex['path']}\n{json.dumps(ex['request'], indent=2) if ex['request'] else ''}\n\n"
           f"{ex['status']}  ({ex['ms']} ms)\n{json.dumps(ex['response'], indent=2)}")
    return (f'<details class="ex" open><summary>Request and response</summary>'
            f'<pre class="code">{escape(txt)}</pre></details>')


SEV_STYLE = {"critical": ("#FBE3DC", "#A32A12"), "high": ("#FBE3DC", "#A32A12"),
             "medium": ("#E1ECF8", "#1F5596"), "low": ("#FCEFD6", "#8A4F00"), "info": ("#F1F1EE", "#3F4450")}
CAP = {"critical": "F", "high": "C", "medium": "B", "low": "A−"}


def render_mint(v: MintView, cfg: dict) -> str:
    L = v.latest
    mi = L["mint_info"]
    g = v.grade
    bg, fg, sbg, sfg, _ = GRADE_STYLE[base_grade(g)]
    active = [k for k in mi.get("keysets", []) if k.get("active")]
    tags = [f'<span class="tag">{escape(v.software)}</span>']
    if active:
        tags.append(f'<span class="tag">{escape(active[0]["unit"])} · {escape(v.fee)}</span>')
        tags.append(f'<span class="tag mono">keyset {escape(active[0]["id"][:12])}…</span>')
    fails = v.fails
    passes = [r for r in L["results"] if r["status"] == "pass"]
    skips = [r for r in L["results"] if r["status"] == "skip"]

    # why
    if g == "—":
        why = ('<section class="why" style="background:#ECECE7"><h2>Unreachable</h2>'
               f'<p>The mint did not answer in the last run ({escape(fmt_time(L["started"]))}).</p></section>')
    elif v.withheld:
        why = (f'<section class="why" style="background:#F4F4F1;border:1px dashed #C9C9C2"><h2>Details withheld until {fmt_day(v.publish_on)}</h2>'
               f'<p>NutCheck saw a serious failure on {fmt_day(v.first_seen)} and {"reported it to the operator" if v.cfg.get("notified") else "is contacting the operator"}. '
               f'The grade is public now; what failed is published on {fmt_day(v.publish_on)}, or as soon as it is fixed.</p></section>')
    elif [r for r in fails if r["status"] == "fail"]:
        f0 = [r for r in fails if r["status"] == "fail"]
        worst = f0[0]["severity"]
        n = len([r for r in f0 if r["severity"] == worst])
        others = "" if len(f0) == n else f" There {'is' if len(f0) - n == 1 else 'are'} {len(f0) - n} less severe failure{'s' if len(f0) - n > 1 else ''} below."
        why = (f'<section class="why" style="background:{sbg}"><h2 style="color:{sfg}">Why {escape(g)}</h2>'
               f'<p>{n} {worst}-severity failure{"s cap" if n > 1 else " caps"} the grade at {CAP.get(worst, g)}: '
               f'<strong>{escape(_lc(phrase_for(f0[0])) if n == 1 else _lc(", ".join(sorted({phrase_for(r) for r in f0 if r["severity"] == worst}))))}</strong>.{others}</p></section>')
    elif fails:
        why = (f'<section class="why" style="background:{sbg}"><h2 style="color:{sfg}">Why {escape(g)}</h2>'
               f'<p>No failures, but {len(fails)} warning{"s" if len(fails) > 1 else ""} below.</p></section>')
    else:
        why = (f'<section class="why" style="background:{sbg}"><h2 style="color:{sfg}">Every check passed</h2>'
               f'<p>{len(passes)} checks passed' + (', but the funded checks were not run, so this grade is partial.' if L.get("partial") else '.') + '</p></section>')

    # failures
    fail_html = ""
    if fails and not v.withheld:
        blocks = []
        for r in fails:
            sb, sf = SEV_STYLE[r["severity"]]
            icon = ICONS["f" if r["status"] == "fail" else "w"]
            cap = f" · caps at {CAP[r['severity']]}" if r["status"] == "fail" and r["severity"] in CAP else ""
            blocks.append(f"""<div class="fail"><div class="frow"><span class="chip {'f' if r['status']=='fail' else 'w'}">{icon}</span>
<h3>{escape(r['title'])}</h3><span class="tag small">NUT-{escape(r['nut'])}</span>
<span class="sev" style="background:{sb};color:{sf}">{escape(r['severity'].capitalize())}{cap}</span></div>
<p style="margin:0;color:#3F4450">{escape(r['summary'])}</p>{_known_box(known_issue(cfg, L, r['id']))}{_evidence_tiles(r.get('evidence', {}))}{_exchange(r.get('evidence', {}))}</div>""")
        n_fail = sum(1 for r in fails if r["status"] == "fail")
        n_warn = len(fails) - n_fail
        title = " · ".join(x for x in [f'Failed <span style="color:#A32A12">{n_fail}</span>' if n_fail else "",
                                       f'Warnings <span style="color:#8A4F00">{n_warn}</span>' if n_warn else ""] if x)
        fail_html = f'<section class="card sect" style="overflow:hidden"><h2>{title}</h2>{"".join(blocks)}</section>'

    # passes by category
    groups = []
    for cat in CATEGORIES:
        rs = [r for r in passes if category_of(r["id"]) == cat]
        if not rs:
            continue
        trs = "".join(f'<tr><td>{escape(r["title"])}</td><td style="width:80px"><span class="tag small" style="white-space:nowrap">'
                      f'{"NUT-" + escape(r["nut"]) if r["nut"] != "-" else "—"}</span></td><td>{escape(r["summary"])}</td></tr>' for r in rs)
        groups.append(f'<details><summary>{ICONS["p"]}<span style="flex-grow:1">{cat}</span>'
                      f'<span class="muted" style="font-weight:400;font-size:14px">{len(rs)} passed</span></summary>'
                      f'<table class="checks"><tbody>{trs}</tbody></table></details>')
    skip_note = ""
    if skips:
        trs = "".join(f'<tr><td>{escape(r["title"])}</td><td style="width:80px"></td><td>{escape(r["summary"])}</td></tr>' for r in skips)
        groups.append(f'<details><summary>{ICONS["s"]}<span style="flex-grow:1">Not run or not applicable</span>'
                      f'<span class="muted" style="font-weight:400;font-size:14px">{len(skips)}</span></summary>'
                      f'<table class="checks"><tbody>{trs}</tbody></table></details>')
    pass_html = (f'<section class="card sect" style="overflow:hidden"><h2>Passed <span style="color:#0B5D45">{len(passes)}</span>'
                 f'{skip_note}</h2>{"".join(groups)}</section>') if groups and not v.withheld else ""

    # sidebar
    hist_n = min(30, len(v.reports))
    first_d = datetime.fromisoformat(v.reports[-hist_n]["started"])
    side = [f"""<section class="card"><h2>Grade history</h2><p class="small muted" style="margin:0 0 12px">Last {hist_n} run{'s' if hist_n > 1 else ''}</p>
{history_bars(v, 30, 'hgrid')}<div class="small muted" style="display:flex;justify-content:space-between;margin-top:6px">
<span>{fmt_day(first_d)}</span><span>{fmt_day(datetime.fromisoformat(L['started']))}</span></div></section>"""]
    if v.first_seen:
        notified = v.cfg.get("notified")
        steps = [("#A85E00", f"<b>{fmt_day(v.first_seen)}</b> · serious failure first seen")]
        if notified:
            steps.append(("#A85E00", f"<b>{escape(notified)}</b> · operator notified"))
        if v.withheld:
            steps.append(("#C9C9C2", f"<b>{fmt_day(v.publish_on)}</b> · details published, or earlier if fixed"))
        elif v.cfg.get("disclosed"):
            steps.append(("#15171C", "<b>Published</b> · disclosed early (fixed or agreed with the operator)"))
        else:
            steps.append(("#15171C", f"<b>{fmt_day(v.publish_on)}</b> · details published after {cfg['_embargo']} days"))
        lis = "".join(f'<li><i style="background:{c}"></i><span>{t}</span></li>' for c, t in steps)
        side.append(f'<section class="card"><h2>Disclosure</h2><ol class="tl">{lis}</ol></section>')
    side.append(f"""<section class="card"><h2>Reproduce this result</h2><code class="block">nutcheck {escape(v.url)}</code>
<p class="small muted" style="margin:10px 0 0">The funded checks spend a few sats; the rest of the budget comes back as a token.</p></section>""")
    side.append("""<section class="card" style="background:#F1F1EE;border:0"><h2>What this grade can't tell you</h2>
<ul style="margin:0;padding-left:18px;font-size:14px;color:#3F4450"><li>Whether the mint holds the bitcoin behind its ecash</li>
<li>Whether it will still be online tomorrow</li><li>How it treats users other than our test wallet</li></ul></section>""")

    report_link = "" if v.withheld else f'<a class="btn line" href="../reports/{v.slug}.json">Download JSON</a>'
    body = f"""<main style="padding-top:28px;gap:24px">
<nav aria-label="Breadcrumb" class="small muted"><a href="../index.html">Scorecard</a> <span aria-hidden="true">/</span> {escape(v.url)}</nav>
<section class="card head">{grade_tile(g, 'big')}
<div style="flex:999 1 420px;min-width:0"><h1 style="margin:0;font-size:34px">{escape(v.name)}</h1>
<div class="mono muted" style="font-size:14px;margin-top:4px">{escape(v.url)}</div>
<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:14px">{''.join(tags)}</div></div>
<div style="flex:1 1 280px;display:flex;flex-direction:column;gap:8px;font-size:14px;color:#4A4F5B">
<div><b style="color:#15171C">Tested</b> {escape(fmt_time(L['started']))}</div>
<div>{len(L['results'])} checks · {L.get('requests', 0)} requests · {L['duration_s']} s{f" · {L['spent_sat']} sat spent" if L.get('spent_sat') else ''}</div>
<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:4px">{report_link}
<a class="btn line" href="../badge/{v.slug}.svg">Badge</a></div></div></section>
<div class="cols"><div class="colmain">{why}{fail_html}{pass_html}</div><aside class="colside">{''.join(side)}</aside></div></main>"""
    return page(f"{v.name} — NutCheck grade {g}", body, "../", cfg)


def render_badge(v: MintView) -> str:
    if v.withheld:
        right, bg, fg = "under review", "#E2E2DC", "#2A2D35"
    else:
        right = v.grade if v.grade != "—" else "offline"
        bg, fg = GRADE_STYLE[base_grade(v.grade)][:2]
    lw, rw = 70, max(30, 8 + 7 * len(right))
    w = lw + rw
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="22" role="img" aria-label="NutCheck: {escape(right)}">
<title>NutCheck: {escape(right)}</title><clipPath id="r"><rect width="{w}" height="22" rx="5"/></clipPath>
<g clip-path="url(#r)"><rect width="{lw}" height="22" fill="#15171C"/><rect x="{lw}" width="{rw}" height="22" fill="{bg}"/></g>
<g font-family="Inter,Verdana,sans-serif" font-size="12" font-weight="600" text-anchor="middle">
<text x="{lw / 2}" y="15" fill="#FFFFFF">NutCheck</text><text x="{lw + rw / 2}" y="15" fill="{fg}">{escape(right)}</text></g></svg>"""


def redacted(report: dict) -> dict:
    """What may be published while details are embargoed: the grade, not what failed."""
    keep = {k: report[k] for k in ("nutcheck", "mint", "slug", "started", "grade", "partial", "reachable")}
    keep["withheld"] = True
    return keep


def feed(views: list[MintView], base_url: str) -> dict:
    out = []
    for v in views:
        e = {"url": v.url, "name": v.name, "grade": v.latest["grade"], "partial": v.latest.get("partial", False),
             "reachable": v.latest.get("reachable", True), "tested": v.latest["started"], "withheld": v.withheld,
             "page": f"{base_url}mints/{v.slug}.html", "badge": f"{base_url}badge/{v.slug}.svg"}
        if v.withheld:
            e["details_published"] = v.publish_on.isoformat()
        else:
            e["categories"] = {c: WORD[s] for c, s in v.cats.items()}
            e["failed"] = [r["id"] for r in v.latest["results"] if r["status"] == "fail"]
            ki = [known_issue(v.site_cfg, v.latest, rid) for rid in e["failed"]]
            if any(ki):
                e["known_issues"] = [{"check": rid, "url": k.get("url"), "text": k["text"]}
                                     for rid, k in zip(e["failed"], ki) if k]
            e["report"] = f"{base_url}reports/{v.slug}.json"
        out.append(e)
    return {"generator": "nutcheck", "mints": out}


def cmd_site(argv) -> int:
    ap = argparse.ArgumentParser(prog="nutcheck site", description="Build the static scorecard from saved reports.")
    ap.add_argument("config")
    ap.add_argument("-o", "--out", default="site")
    ap.add_argument("--now", help="pretend the build happens at this ISO time (for testing the embargo)")
    ap.add_argument("--alerts", metavar="FILE",
                    help="write one line per withheld failure whose operator has not been notified yet")
    a = ap.parse_args(argv)
    cfg, base = load_config(a.config)
    now = datetime.fromisoformat(a.now) if a.now else datetime.now(timezone.utc)
    embargo = int(cfg.get("embargo_days", 14))
    cfg["_embargo"] = embargo
    cfg["_built"] = now.astimezone(timezone.utc).strftime("%-d %b %Y, %H:%M UTC")
    views = build_views(cfg, base, now, embargo)
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    for d in ("mints", "badge", "reports", "api"):
        (out / d).mkdir(parents=True)
    (out / "style.css").write_text(CSS)
    (out / "favicon.svg").write_text(FAVICON)
    (out / "index.html").write_text(render_index(views, cfg))
    for v in views:
        (out / "mints" / f"{v.slug}.html").write_text(render_mint(v, cfg))
        (out / "badge" / f"{v.slug}.svg").write_text(render_badge(v))
        (out / "reports" / f"{v.slug}.json").write_text(json.dumps(redacted(v.latest) if v.withheld else v.latest, indent=2))
    (out / "api" / "mints.json").write_text(json.dumps(feed(views, cfg.get("base_url", "../")), indent=2))
    pending = [v for v in views if v.withheld and not v.cfg.get("notified")]
    if a.alerts:
        Path(a.alerts).write_text("".join(
            f"{v.url}: serious failure first seen {fmt_day(v.first_seen)}, details go public {fmt_day(v.publish_on)}. "
            f"Notify the operator, then set \"notified\" for this mint in the config.\n" for v in pending))
    print(f"Built {len(views)} mint pages into {out}/ ({sum(v.withheld for v in views)} with details withheld, "
          f"{len(pending)} awaiting operator notification)")
    return 0
