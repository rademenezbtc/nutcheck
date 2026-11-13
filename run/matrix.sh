#!/bin/bash
# Runs NutCheck through the naughty proxy once per mode and prints the grade + failed checks.
cd "$(dirname "$0")/.."
for mode in none bad_dleq no_dleq keyset_tamper fee_lie short_mint quote_replay overspend double_spend forgery lock_bypass keep_change info_lie; do
  m=$([ "$mode" = none ] && echo '[]' || echo "[\"$mode\"]")
  curl -s -X POST localhost:3339/__naughty -d "{\"modes\": $m}" >/dev/null
  .venv/bin/python -m nutcheck http://127.0.0.1:3339 --no-color --json run/report-$mode.json --leftover /dev/null >/dev/null 2>&1
  python3 - "$mode" <<'PY'
import json, sys
mode = sys.argv[1]; d = json.load(open(f"run/report-{mode}.json"))
fails = [f"{r['id']}({r['status']})" for r in d["results"] if r["status"] in ("fail", "warn", "error")]
print(f"{mode:<14} grade {d['grade']:<3} {'partial ' if d['partial'] else ''}{', '.join(fails) or '-'}")
PY
done
curl -s -X POST localhost:3339/__naughty -d '{"modes": []}' >/dev/null
