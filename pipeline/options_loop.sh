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

i=0
while :; do
  weekday || { echo "weekend - no session"; break; }
  m=$(et_min)
  if [ "$m" -ge "$CLOSE" ]; then echo "after 1605 ET - the after-close broad run takes it from here"; break; fi
  if [ "$m" -lt "$OPEN" ]; then
    w=$(( (OPEN - m) * 60 ))
    [ $(( w + 600 )) -gt "$(left)" ] && { echo "the session opens after this job's budget - the next job takes it"; break; }
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
