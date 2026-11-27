#!/usr/bin/env bash
# Hourly paper-trade loop: run bot.py every 5 minutes for ~45–50 minutes,
# then exit before the next hourly cron (:02) starts. Used by trade.yml.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

SINGLE_SHOT="${SINGLE_SHOT:-false}"
TICKS_RAW="${TICKS:-10}"
# GitHub boolean inputs arrive as the strings "true" / "false".
if [[ "${SINGLE_SHOT}" == "true" || "${SINGLE_SHOT}" == "True" || "${SINGLE_SHOT}" == "1" ]]; then
  TICKS=1
else
  TICKS="${TICKS_RAW}"
fi
# Clamp to 1–11 ticks (~0–50 minutes of spacing).
if ! [[ "${TICKS}" =~ ^[0-9]+$ ]]; then
  TICKS=10
fi
if (( TICKS < 1 )); then TICKS=1; fi
if (( TICKS > 11 )); then TICKS=11; fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

START_EPOCH="$(date +%s)"
# Hard stop ~50 minutes after start, and at least 8 minutes before the next :02 cron.
# Next cron is the next UTC hour at minute 02.
NOW_UTC_MIN=$((10#$(date -u +%M)))
NOW_UTC_SEC=$((10#$(date -u +%S)))
# Seconds until the next HH:02:00 UTC (if we are at or past :02 this hour, that's next hour).
if (( NOW_UTC_MIN < 2 )); then
  SECS_TO_NEXT_CRON=$(( (2 - NOW_UTC_MIN) * 60 - NOW_UTC_SEC ))
else
  SECS_TO_NEXT_CRON=$(( (60 - NOW_UTC_MIN + 2) * 60 - NOW_UTC_SEC ))
fi
DEADLINE_FROM_CRON=$(( START_EPOCH + SECS_TO_NEXT_CRON - 8 * 60 ))
DEADLINE_FROM_START=$(( START_EPOCH + 50 * 60 ))
if (( DEADLINE_FROM_CRON < DEADLINE_FROM_START )); then
  DEADLINE="${DEADLINE_FROM_CRON}"
else
  DEADLINE="${DEADLINE_FROM_START}"
fi
# Always allow at least the first tick even if we started late.
if (( DEADLINE <= START_EPOCH )); then
  DEADLINE=$(( START_EPOCH + 60 ))
fi

echo "Paper-trade loop: ticks=${TICKS} single_shot=${SINGLE_SHOT} deadline_in=$(( DEADLINE - START_EPOCH ))s"

push_with_retry() {
  local i
  for i in 1 2 3 4 5 6; do
    if ! git pull --rebase origin main; then
      echo "Rebase failed (attempt $i); aborting and retrying."
      git rebase --abort || true
      sleep $((i * 8))
      continue
    fi
    if [[ -f learn.py ]]; then
      python3 learn.py --merge-dashboard || true
      git add docs/data.json
      if ! git diff --staged --quiet; then
        git commit -m "chore: keep learning payload on dashboard $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      fi
    fi
    if git push origin HEAD:main; then
      echo "Push succeeded on attempt $i."
      return 0
    fi
    echo "Push rejected (attempt $i); retrying."
    sleep $((i * 8))
  done
  echo "Failed to push paper-book commit after retries."
  return 1
}

run_tick() {
  local tick_no="$1"
  echo "======== tick ${tick_no}/${TICKS} at $(date -u +%Y-%m-%dT%H:%M:%SZ) ========"
  # Pull first so we never overwrite a concurrent learner (or a previous tick) commit.
  if ! git pull --rebase origin main; then
    echo "Pre-tick rebase failed; aborting rebase and continuing with local tree."
    git rebase --abort || true
  fi
  python3 bot.py

  git add data/state.json data/backtest.json docs/data.json
  if [[ -f data/strategy_config.json ]]; then
    git add data/strategy_config.json
  fi
  # Commit + push only when the live paper book or dashboard actually changed.
  if git diff --staged --quiet; then
    echo "No paper-book changes to commit on tick ${tick_no}."
    return 0
  fi
  git commit -m "chore: update Kalshi 15m paper book $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  push_with_retry
}

for (( i=1; i<=TICKS; i++ )); do
  NOW="$(date +%s)"
  if (( i > 1 && NOW >= DEADLINE )); then
    echo "Deadline reached before tick ${i}; exiting cleanly so the next hourly run can start."
    break
  fi
  TICK_START="$(date +%s)"
  run_tick "$i"
  if (( i >= TICKS )); then
    break
  fi
  NOW="$(date +%s)"
  NEXT_START=$(( TICK_START + 5 * 60 ))
  if (( NEXT_START >= DEADLINE )); then
    echo "Next tick would pass the deadline; exiting after tick ${i}."
    break
  fi
  SLEEP_FOR=$(( NEXT_START - NOW ))
  if (( SLEEP_FOR < 1 )); then
    SLEEP_FOR=1
  fi
  echo "Sleeping ${SLEEP_FOR}s until the next 5-minute tick…"
  sleep "${SLEEP_FOR}"
done

echo "Paper-trade loop finished at $(date -u +%Y-%m-%dT%H:%M:%SZ)."
