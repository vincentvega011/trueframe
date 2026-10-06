#!/usr/bin/env bash
# Stops the validator started by localnet_up.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PID_FILE="$ROOT/.localnet/validator.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  kill "$(cat "$PID_FILE")"
  for _ in $(seq 1 30); do kill -0 "$(cat "$PID_FILE")" 2>/dev/null || break; sleep 0.5; done
  echo "validator stopped"
else
  echo "no validator started by localnet_up.sh is running"
fi
rm -f "$PID_FILE"
