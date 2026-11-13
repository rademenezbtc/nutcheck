#!/bin/bash
# Builds a demo scorecard from real NutCheck runs against naughty-proxy instances.
# Needs a FakeWallet mint on :3338 (see README) and the .mintenv/.venv environments.
set -e
cd "$(dirname "$0")/.."
PY=.venv/bin/python; MPY=.mintenv/bin/python
declare -A MODES=([3340]="" [3346]="" [3342]=no_dleq [3341]=keep_change [3343]="" [3345]=double_spend [3344]=lock_bypass)
for port in "${!MODES[@]}"; do
  $MPY proxy/naughty.py --port "$port" --modes "${MODES[$port]}" > "demo/proxy-$port.log" 2>&1 &
done
trap 'kill $(jobs -p) 2>/dev/null' EXIT
sleep 3
rm -rf demo/reports demo/wallet
for i in 1 2 3 4 5 6; do
  # Macadamia regresses halfway through: honest for 3 runs, then lies about its fee
  [ "$i" = 4 ] && curl -s -X POST localhost:3343/__naughty -d '{"modes":["fee_lie"]}' >/dev/null
  echo "== sweep $i"; $PY -m nutcheck sweep demo/scorecard.json
done
$PY -m nutcheck site demo/scorecard.json -o demo/site
