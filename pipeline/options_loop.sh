#!/usr/bin/env bash
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-OPTIONS
#  >>> FILE PATH:   pipeline/options_loop.sh
#
#  z148 - OPTION CHAINS AS OFTEN AS THE INTRADAY TAPE. During the regular session (0935-1605 ET) a pass
#  runs every 15 minutes (:05 / :20 / :35 / :50): the CORE universe (seed list + requests + portfolio
#  holdings + the bot's open positions) with fetch_options.py's oldest-first rotation and a 9-minute
#  budget per pass, so every core chain - including each contract the bot holds - is re-read every
#  15-30 minutes. Each pass commits on its own. One job runs up to LOOP_BUDGET_MIN; hourly triggers
#  queue the next (concurrency group options-data, shared with the after-close broad run and compact).
#
#  z196 - THE BROAD PASS RUNS HERE, after 1605 ET, before the night bridge: every optionable US equity / ETF in
#  the five books (~2,300; fetch_options.py OPTIONS_TIER=broad, full ladder for the core list + 400 largest, lite
#  for the rest), oldest first, skipping chains fetched this evening. Why: options.yml's 1630 ET broad run waited
#  in the shared concurrency group behind this loop's night bridge, and the bridge's own queued next run replaced
#  it as the group's pending run - GitHub cancels a pending run when a newer one queues - so Monday-Thursday the
#  broad chains never refreshed (on file: 11-07 and 11-13, both Fridays). data/options/_state.json says whether
#  tonight's pass is complete; an incomplete one (time budget, Yahoo refusing) continues in this job or the next.
#  LOOP_BROAD=0 turns it off (options.yml's broad run is still the backstop).
# ============================================================
set -u
BUDGET=$(( ${LOOP_BUDGET_MIN:-170} * 60 ))
PASS=${PASS_BUDGET_SEC:-540}
START=$(date +%s)
BRANCH="${GITHUB_REF_NAME:-main}"
OPEN=575     # 09:35 ET - after the opening rotation settles
CLOSE=965    # 16:05 ET
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

et_min() { echo $(( 10#$(TZ=America/New_York date +%H) * 60 + 10#$(TZ=America/New_York date +%M) )); }
weekday() { [ "$(TZ=America/New_York date +%u)" -le 5 ]; }
latest() { git fetch -q origin "$BRANCH" && { git merge -q --ff-only FETCH_HEAD 2>/dev/null || git reset -q --hard FETCH_HEAD; }; }
stage() { for f in data/options data/surfaces data/requests.txt; do
            if [ -e "$f" ] || git ls-files --error-unmatch "$f" >/dev/null 2>&1; then git add -A "$f"; fi; done; }   # z116
push_retry() {
  for i in 1 2 3 4 5; do
    if git push -q origin "HEAD:$BRANCH"; then echo "  pushed (attempt $i)"; return 0; fi
    git fetch -q origin "$BRANCH"
    git merge -X ours "origin/$BRANCH" --no-edit -q 2>/dev/null || true
    if [ -f .git/MERGE_HEAD ]; then git checkout --ours -- data/options data/surfaces 2>/dev/null || true; stage; git commit -q --no-edit 2>/dev/null || true; fi
    sleep $(( i * 4 ))
  done
  echo "::warning::push failed after 5 attempts"; return 1
}
one_pass() {
  latest
  OPTIONS_TIER=core TIME_BUDGET_SEC="$PASS" python pipeline/fetch_options.py || echo "::warning::options pass failed"
  stage
  if git diff --staged --quiet; then echo "  no changes"; return 0; fi
  git commit -q -m "Options chains (core, intraday) $(date -u +%Y-%m-%dT%H:%M:%SZ) [skip ci]"
  push_retry
}
next_wait() {   # seconds to the next :05 / :20 / :35 / :50, at least 60 s away
  local now m s w
  now=$(date +%s); m=$(( (now / 60) % 60 )); s=$(( now % 60 ))
  w=$(( ( (5 - m % 15 + 15) % 15 ) * 60 - s ))
  [ "$w" -lt 60 ] && w=$(( w + 900 ))
  echo "$w"
}
# z161: when this job stops for its TIME BUDGET while its window is still open, it queues the next run of its own
# workflow - GitHub's scheduler went hours without starting anything (2026-10-26: no intraday run by 0900 ET).
# Needs `actions: write` and GH_TOKEN (the job's own token) in the workflow; the concurrency group queues the new
# run behind this one. GitHub's schedule and the Supabase heartbeat (supabase/heartbeat.sql) stay as backstops.
chain_next() {
  [ -n "${GH_TOKEN:-}" ] || { echo "::notice::GH_TOKEN not set - not chaining"; return 0; }
  local wf="${GITHUB_WORKFLOW_REF:-}"
  wf="${wf%@*}"; wf="${wf##*/}"
  { [ -n "$wf" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; } || return 0
  if gh workflow run "$wf" --ref "${GITHUB_REF_NAME:-main}" -R "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
    echo "chained: the next run of $wf is queued"
  else
    echo "::warning::could not queue the next run of $wf (does the workflow grant actions: write?)"
  fi
}

left() { echo $(( BUDGET - ( $(date +%s) - START ) )); }

broad_due() {   # z196: tonight's broad pass not done yet? (no state file / another date / incomplete)
  python - <<'PY'
import json, sys, datetime
from zoneinfo import ZoneInfo
try:
    b = (json.load(open("data/options/_state.json")) or {}).get("broad") or {}
except Exception:
    b = {}
today = datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
sys.exit(0 if (b.get("date") != today or b.get("complete") is not True) else 1)
PY
}
BROAD_RUNS=0
broad_pass() {   # z196: one broad pass inside this job's remaining budget, committed on its own
  latest
  local l budget
  l=$(left); budget=$(( l - 480 ))
  BROAD_RUNS=$((BROAD_RUNS + 1))
  echo "── broad pass $BROAD_RUNS · $(TZ=America/New_York date +'%a %H:%M') ET · budget $(( budget / 60 )) min"
  OPTIONS_TIER=broad TIME_BUDGET_SEC="$budget" python pipeline/fetch_options.py || echo "::warning::broad pass failed"
  stage
  if git diff --staged --quiet; then echo "  no changes"; return 0; fi
  git commit -q -m "Options chains (broad - every optionable name in the books) $(date -u +%Y-%m-%dT%H:%M:%SZ) [skip ci]"
  push_retry
}

i=0
while :; do
  weekday || { echo "weekend - no session"; break; }
  m=$(et_min)
  if [ "$m" -ge "$CLOSE" ]; then
    # z196: tonight's broad pass first (at most 3 tries in one job - a pass that keeps failing waits for the next job)
    if [ "${LOOP_BROAD:-1}" = "1" ] && [ "$BROAD_RUNS" -lt 3 ] && broad_due; then
      if [ "$(left)" -gt 900 ]; then broad_pass; continue; fi
      echo "tonight's broad pass is due but under 15 min are left in this job - queueing the next run for it"; chain_next; break
    fi
    # z168: Monday-Thursday the loop bridges the night (GitHub started few of its morning triggers on 10-26..10-30):
    # wait to the end of this job and queue the next, which waits for tomorrow's 0935 ET; Friday it stops
    if [ "$(TZ=America/New_York date +%u)" -le 4 ] && [ "${LOOP_BRIDGE:-1}" = "1" ]; then
      l=$(left); echo "after 1605 ET - bridging the night: sleeping $(( (l - 180) / 60 )) min, then queueing the next run"
      [ "$l" -gt 180 ] && sleep $(( l - 180 )); chain_next; break
    fi
    echo "after 1605 ET - Friday: no bridge (the broad pass is done or retried by options.yml's backstop)"; break
  fi
  if [ "$m" -lt "$OPEN" ]; then
    w=$(( (OPEN - m) * 60 ))
    if [ $(( w + 600 )) -gt "$(left)" ]; then
      l=$(left); echo "the session opens after this job's budget - sleeping $(( (l - 180) / 60 )) min, then queueing the next run"
      [ "$l" -gt 180 ] && sleep $(( l - 180 )); chain_next; break
    fi
    echo "waiting $(( w / 60 )) min for 0935 ET"; sleep "$w"; continue
  fi
  i=$((i + 1))
  echo "── pass $i · $(TZ=America/New_York date +'%a %H:%M') ET"
  one_pass
  w=$(next_wait)
  [ $(( w + PASS + 120 )) -gt "$(left)" ] && { echo "budget reached after $i pass(es) - queueing the next run"; chain_next; break; }
  echo "  next pass in $(( w / 60 )) min"
  sleep "$w"
done
