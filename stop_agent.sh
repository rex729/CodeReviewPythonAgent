#!/usr/bin/env bash
# Stops every running copy of the review agent (python -m review_agent) on macOS or Linux.
# It sends Ctrl+C (SIGINT) first, so the agent stops the same way it does by hand. Its progress is
# saved after each merge request, so stopping mid-round loses at most the review in progress.
# Anything still running after 30 seconds, e.g. waiting on a long model call, is terminated.
set -euo pipefail

pattern='-m review_agent'
pids=$(pgrep -f -- "$pattern" || true)
if [ -z "$pids" ]; then
  echo "No review agent is running."
  exit 0
fi

echo "Stopping review agent (PID $(echo $pids | tr '\n' ' '))..."
kill -INT $pids
for _ in $(seq 1 30); do
  sleep 1
  remaining=$(pgrep -f -- "$pattern" || true)
  if [ -z "$remaining" ]; then
    echo "Stopped."
    exit 0
  fi
done

echo "Still running after 30 seconds; terminating PID $(echo $remaining | tr '\n' ' ')."
kill -TERM $remaining
