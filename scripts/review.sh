#!/usr/bin/env bash
# Two independent reviewers, alternating with fixes, until two consecutive
# rounds find nothing above P3. This is how 0.11.0–0.11.3 were reviewed; the
# loop used to live on a server as an ad-hoc file, and the rule that makes it
# work — a finding counts only with a pasted command and its output — lived in
# a prompt nobody could see. Both live here now.
#
#   scripts/review.sh                 # review HEAD once with both reviewers
#   scripts/review.sh --loop          # review → fix → review, until converged
#
# Reviewer A is `codex exec` (a different model family than the one that
# wrote the code). Reviewer B is `claude -p`. Reports land in ./.review/.
# The fixer is `claude -p` in bypass mode on the checkout; it may commit,
# never push. HARD LIMITS the fixer is told: no push, no tags, no publishing,
# no contacting anyone; tests green before every commit; every fix adds a
# test that FAILS on the parent commit.
set -u
cd "$(dirname "$0")/.."
OUT=./.review; mkdir -p "$OUT"
PY=${PY:-./.venv/bin/python}
MAX_ROUNDS=${MAX_ROUNDS:-20}
CLEAN_NEEDED=2
loop=0; [ "${1:-}" = "--loop" ] && loop=1
say() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$OUT/loop.log"; }
# The fixer commits its own work and the reviewers may write anywhere in the
# checkout; a review must start from a state git can restore. An earlier
# version cleaned the tree after each round and would have destroyed
# uncommitted work — so refuse instead.
if [ -n "$(git status --porcelain | grep -v '^?? .review/')" ]; then
  echo "review.sh: working tree has uncommitted changes — commit or stash first" >&2
  exit 3
fi

review_prompt() {
  local sha=$1
  cat <<EOF
Adversarial correctness review of the Python package in this directory (skillmem, commit $sha). Disposable checkout: write scratch files under ./.review/scratch/ only; do not modify package source; never push, tag, publish or contact anyone. Virtualenv at ./.venv.

THE RULE: a finding counts only with a pasted command and its output. Reasoning without reproduction is not a finding.

Read CHANGELOG.md's top entry first and test every claim in it — a claim the code does not deliver is a finding at the severity of the gap. Then attack, in this order: (1) hooks._recall_sections and hooks.plan_budget — budget never exceeded, no slug twice, never fewer approved records than the v0.11.1 in-order fill (transcribe it yourself from \`git show v0.11.1:skillmem/hooks.py\`), unapproved rows always inside a balanced frame, approved rows never beyond their limit, and all of that under a concurrent writer using a trace callback; (2) the trust boundary — any path by which text the owner never approved reaches a model without its frame, or an approved rule is kept out of the candidate set; (3) every reader of an externalised body file degrades on OSError and UnicodeDecodeError; (4) the shipped Dockerfile; (5) version strings across pyproject.toml, skillmem/__init__.py, plugin.json, .claude-plugin/plugin.json, server.json. Run ./.venv/bin/python -m pytest -q and report the exact line.

Report each finding as: P1|P2|P3 - file:line - what - evidence (command + output). Then a section Untested. Then a final line Verdict: SHIP or HOLD. If you find nothing above P3, say so plainly; do not manufacture findings.
EOF
}

fix_prompt() {
  local report=$1
  cat <<EOF
You are fixing findings in the skillmem checkout in this directory. Read the review report at $report and fix every P1 and P2 it lists; leave P3s unless the fix is one line. Two rules matter more than speed: put a guard inside the function every caller passes through, never in one caller; and any read followed by a write must be one transaction. For each fix add a test that FAILS on the parent commit — verify it by running the test against \`git archive HEAD\` in a temp dir. Run ./.venv/bin/python -m pytest -q and make it fully green BEFORE committing. Update CHANGELOG.md so it claims nothing the code does not do. Then make ONE commit explaining what was wrong and why. If a finding is wrong, do not change code to match it — say so in the commit message. HARD LIMITS: never git push, never create tags, never publish anything, never contact anyone.
EOF
}

count_p12() {
  # grep -c prints 0 and exits 1 on no match — never chain `|| echo 0` after it
  # `**P1 - `, `P1 — `, `- **P1** - `, `### P2 - ` are all real report lines
  local n; n=$(grep -cE '^(#+ *)?(- *)?\**P[12]\** *[-—:]' "$1" 2>/dev/null); echo "${n:-0}"
}

run_round() {
  local n=$1 sha; sha=$(git rev-parse --short HEAD)
  local a="$OUT/round-$n-A-$sha.md" b="$OUT/round-$n-B-$sha.md"
  say "round $n: reviewing $sha"
  ( codex exec -s workspace-write --skip-git-repo-check -o "$a" "$(review_prompt "$sha")" ) >>"$OUT/loop.log" 2>&1 &
  local pa=$!
  claude -p "$(review_prompt "$sha")" --permission-mode bypassPermissions </dev/null >"$b" 2>>"$OUT/loop.log"
  wait $pa
  rm -rf "$OUT/scratch"          # only the reviewers' own scratch, never the tree
  local fa fb; fa=$(count_p12 "$a"); fb=$(count_p12 "$b")
  say "round $n: reviewer A: $fa P1/P2 ($(grep -oE 'Verdict: \w+' "$a" | tail -1)); reviewer B: $fb P1/P2 ($(grep -oE 'Verdict: \w+' "$b" | tail -1))"
  [ -s "$a" ] && [ -s "$b" ] || { say "a reviewer produced nothing — stopping"; return 2; }
  [ "$fa" = 0 ] && [ "$fb" = 0 ]
}

clean=0; round=0
while [ $round -lt "$MAX_ROUNDS" ]; do
  round=$((round+1))
  run_round $round; rc=$?
  if [ $rc -eq 0 ]; then
    clean=$((clean+1)); say "round $round: CLEAN ($clean of $CLEAN_NEEDED)"
    [ $clean -ge $CLEAN_NEEDED ] && { say "CONVERGED at $(git rev-parse --short HEAD)"; exit 0; }
    [ $loop -eq 1 ] || exit 0
    continue
  fi
  [ $rc -eq 2 ] && exit 2
  clean=0
  [ $loop -eq 1 ] || { say "findings above P3 — see $OUT/"; exit 1; }
  say "round $round: fixing"
  for r in "$OUT"/round-$round-*.md; do
    claude -p "$(fix_prompt "$r")" --permission-mode bypassPermissions </dev/null >>"$OUT/loop.log" 2>&1
  done
  t=$($PY -m pytest -q 2>&1 | tail -1); say "after fix: HEAD=$(git rev-parse --short HEAD) | $t"
  case "$t" in *failed*) say "TESTS RED after the fix — stopping for a human"; exit 1;; esac
done
say "gave up after $MAX_ROUNDS rounds"; exit 1
