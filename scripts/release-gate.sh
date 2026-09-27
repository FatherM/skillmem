#!/usr/bin/env bash
# Run before tagging a release. Every check here is a mistake that has
# actually shipped from this repo: a version string left behind in one of six
# files, a CHANGELOG entry that claimed what the code did not do, a test that
# was green on the bug it was named for, a container that never started.
#
#   scripts/release-gate.sh            # gate the working tree against the last tag
#   scripts/release-gate.sh v0.11.2    # ...against an explicit base
#   REQUIRE_DOCKER=1 scripts/release-gate.sh   # a missing docker daemon FAILS
#
# Exit 0 only when every check passes. Prints one line per check.
set -u
cd "$(dirname "$0")/.."
PY=${PY:-./.venv/bin/python}
BASE=${1:-$(git describe --tags --abbrev=0 2>/dev/null)}
fail=0
ok()   { printf 'PASS  %s\n' "$*"; }
# coreutils timeout is absent on stock macOS; run unbounded rather than fail
tmo()  { if command -v timeout >/dev/null 2>&1; then timeout "$@"; elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$@"; else shift; "$@"; fi; }
bad()  { printf 'FAIL  %s\n' "$*"; fail=1; }
skip() { printf 'SKIP  %s\n' "$*"; }

# 1. Clean tree — the gate must judge what will be tagged, not what is in
#    the editor.
if [ -z "$(git status --porcelain)" ]; then ok "working tree clean"
else bad "working tree has uncommitted changes"; fi

# 2. One version everywhere. pyproject is the source of truth; the release
#    that missed the root plugin.json was caught by a test — after the commit.
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
declare -a where=(skillmem/__init__.py plugin.json .claude-plugin/plugin.json server.json)
vbad=0
for f in "${where[@]}"; do
  if ! grep -q "\"$version\"\|'$version'" "$f"; then
    bad "$f does not carry version $version"; vbad=1
  fi
done
[ $vbad -eq 0 ] && ok "version $version in pyproject and ${#where[@]} other files"
if [ "$(grep -c "\"version\": \"$version\"" server.json)" -ne 2 ]; then
  bad "server.json must carry $version twice (server and package)"
fi

# 3. Tag not taken, and CHANGELOG has a section for it.
if git rev-parse -q --verify "refs/tags/v$version" >/dev/null; then
  bad "tag v$version already exists"
else ok "tag v$version is free"; fi
if grep -q "^## $version\$" CHANGELOG.md; then ok "CHANGELOG has a $version section"
else bad "CHANGELOG has no '## $version' section"; fi

# 4. Suite green.
# "N passed, M errors" has no "failed" in it and is still a red suite.
if out=$($PY -m pytest -q 2>&1 | tail -1) && [[ "$out" == *passed* && "$out" != *failed* && "$out" != *error* ]]; then
  ok "pytest: $out"
else bad "pytest: $out"; fi

# 5. A new regression test must fail on the parent of the commit that added
#    it. A test that is green on the code it claims to guard proves nothing;
#    this repo shipped one named for an invariant it never checked. Judged per
#    function, on the tree it was written against, not on the base: a test for
#    a defect made and fixed since the base is green there and still guards
#    it. tests/properties/** guard invariants and are skipped. The rules are in
#    scripts/gate_new_tests.py.
if [ -n "$BASE" ] && git rev-parse -q --verify "$BASE^{commit}" >/dev/null; then
  err=$(mktemp)
  if ! verdict=$("$PY" scripts/gate_new_tests.py "$BASE" 2>"$err") || [ -z "$verdict" ]; then
    bad "could not judge the new tests: $(tail -3 "$err" | tr '\n' ' ')"
  else
    while IFS= read -r line; do
      case "$line" in
        "ok "*)   ok "${line#ok }" ;;
        "bad "*)  bad "${line#bad }" ;;
        "skip "*) skip "${line#skip }" ;;
        *)        bad "unexpected verdict line: $line" ;;
      esac
    done <<<"$verdict"
  fi
  rm -f "$err"
else
  skip "no base tag to compare tests against"
fi

# 6. The container starts and answers initialize with stdin held open — the
#    way a real MCP client talks to it. Two releases shipped an image that did
#    not.
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  img="skillmem-gate:$version"
  if docker build -q -t "$img" . >/dev/null 2>&1; then
    init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"gate","version":"0"}}}'
    reply=$( (printf '%s\n' "$init"; sleep 4) | tmo 30 docker run -i --rm "$img" 2>/dev/null | head -c 2000)
    if [[ "$reply" == *'"serverInfo"'* ]]; then ok "docker image answers initialize"
    else bad "docker image did not answer initialize"; fi
  else bad "docker build failed"; fi
elif [ "${REQUIRE_DOCKER:-0}" = "1" ]; then
  bad "docker daemon not available and REQUIRE_DOCKER=1"
else
  skip "docker daemon not available — image NOT verified (set REQUIRE_DOCKER=1 to fail)"
fi

# 7. Manifests parse.
if $PY - <<'EOF'
import json
for f in ("plugin.json", ".claude-plugin/plugin.json", "server.json", ".claude-plugin/marketplace.json"):
    try:
        json.load(open(f))
    except FileNotFoundError:
        pass
EOF
then ok "manifests parse"; else bad "a manifest does not parse"; fi

echo
if [ $fail -eq 0 ]; then echo "GATE PASS — safe to tag v$version"; else echo "GATE FAIL — do not tag"; fi
exit $fail
