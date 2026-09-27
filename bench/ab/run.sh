#!/usr/bin/env bash
# A/B: the same task with recall on and with recall off, measured by outcome.
#
# This exists because a proxy lied. "The record reached the prompt" went from
# 37% to 62% when bodies were cut to 50 chars — and the agent's correct
# answers went from 18/18 to 15/18, because 50 chars carried a rule's tone
# and not its detail. The only honest measure of a change to recall is what
# the agent then gets right.
#
#   bench/ab/run.sh                      # 3 repeats per task and arm
#   REPS=1 BODY=250 bench/ab/run.sh      # quick pass, SKILLMEM_BODY_CHARS=250
#
# Arms differ in one thing: arm A has the skillmem hooks in its Claude Code
# settings, arm B has none. Neither gets a CLAUDE.md — that file carries much
# of the same knowledge and would leak it into the control. Tasks come from
# bench/ab/tasks.txt: `id|prompt|expected-regex`, read-only questions whose
# answer lives in YOUR memory and would otherwise need digging. Write your
# own; the shipped ones are examples from the author's corpus and will not
# match yours.
#
# Read bench/ab/README.md before trusting a number: run the same arm twice
# first to learn the noise, then compare.
set -u
cd "$(dirname "$0")"
# coreutils timeout is absent on stock macOS — the very machine the README targets
tmo() { if command -v timeout >/dev/null 2>&1; then timeout "$@"; elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$@"; else shift; "$@"; fi; }
DB=${SKILLMEM_DB:?set SKILLMEM_DB to the memory database to recall from}
SM=${SM:-$(command -v skillmem)}
REPS=${REPS:-3}
BODY=${BODY:-400}
OUT=${OUT:-results-$(date +%Y%m%d-%H%M).jsonl}
TASKS=${TASKS:-tasks.txt}
W=$(mktemp -d)
mkdir -p "$W/work" "$W/armA" "$W/armB"

# Built by json/shlex, not string interpolation: the default database path
# on macOS has a space in it, and an interpolated command broke on it.
SM="$SM" DB="$DB" python3 - "$W/armA/settings.json" <<'PY'
import json, os, shlex, sys
sm, db = os.environ["SM"], os.environ["DB"]
cmd = lambda hook: f"{shlex.quote(sm)} --db {shlex.quote(db)} hook {hook}"
json.dump({"hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd("auto-recall")}]}],
    "PreToolUse": [{"matcher": "Bash|Edit|Write|Read|Grep|Glob",
                    "hooks": [{"type": "command", "command": cmd("tool-recall")}]}]}},
    open(sys.argv[1], "w"), indent=1)
PY
echo '{}' > "$W/armB/settings.json"

run_one() {
  local arm=$1 id=$2 expect=$3 prompt=$4 rep=$5 t0 t1 json
  t0=$(date +%s)
  # stdin closed on purpose: claude inside a `while read` loop otherwise eats
  # the remaining task lines — the first run of this harness did exactly one task
  json=$(cd "$W/work" && env SKILLMEM_BODY_CHARS="$BODY" CLAUDE_CONFIG_DIR="$W/arm$arm" \
         tmo 420 claude -p "$prompt" --output-format json \
         --permission-mode bypassPermissions 2>/dev/null </dev/null)
  t1=$(date +%s)
  printf '%s' "$json" | python3 parse.py "$OUT" "$arm" "$id" "$rep" "$expect" "$((t1-t0))"
}

for rep in $(seq 1 "$REPS"); do
  while IFS='|' read -r id prompt expect; do   # expect LAST: it is a regex and may contain |
    [ -z "$id" ] || [ "${id:0:1}" = "#" ] && continue
    for arm in A B; do run_one "$arm" "$id" "$expect" "$prompt" "$rep"; done
  done < "$TASKS"
done
rm -rf "$W"
echo "wrote $(wc -l < "$OUT") runs to $OUT"
python3 parse.py --report "$OUT"
