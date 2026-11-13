#!/bin/bash
# usage: start_mint.sh <name>   (reads run/<name>.env, logs to run/<name>.log)
cd "$(dirname "$0")" && mkdir -p "$1" && cd "$1" && set -a && . "../$1.env" && set +a
exec ../../.mintenv/bin/mint > "../$1.log" 2>&1
