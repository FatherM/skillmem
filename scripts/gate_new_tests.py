"""Release gate check 5: every new regression test fails on the parent of the
commit that added it.

    python scripts/gate_new_tests.py BASE [HEAD]

A test that is green on the code it claims to guard proves nothing; this repo
shipped one named for an invariant it never checked. Judged per function, not
per file: a file that only gained a skipif marker or was reformatted is not
checked. A function is new when HEAD defines it and BASE does not (file::name
or file::Class::name; parametrised cases are one function).

Each new function is run where it was written: in a tree made of the package
of the parent of the commit that introduced it, and that commit's own tests.
Judging it against BASE instead would call every test that guards a defect
introduced and fixed between BASE and HEAD green, though it failed on the
defect when it was written. It must fail or error there in at least one case.

A test committed apart from the fix it guards (after it, or before a later fix
for the defect its own commit made) is green on its own commit's parent. It is
judged on the parent of that fix instead when tests/gate-fixes.txt names the
fix, which must be in BASE..HEAD; it must fail there (an import error is not
enough).

tests/properties/** are excluded: they guard invariants, not a regression, and
are expected to be green wherever the invariant already held.

Prints one verdict per line, "ok ...", "bad ..." or "skip ...", for
release-gate.sh to report. Exit status 0 means the verdicts are complete;
anything else means the check itself broke.
"""
from __future__ import annotations

import ast
import fnmatch
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path

INVARIANT_GUARDS = "tests/properties/"
FIXES = "tests/gate-fixes.txt"
TIMEOUT = int(os.environ.get("GATE_TEST_TIMEOUT", "1800"))   # seconds per commit

# Selected by a plugin, not by node ids on the command line: one id that
# pytest cannot find stops the whole run with nothing reported.
PLUGIN = r'''
import json, os, re

WANT = set(open(os.environ["GATE_IDS"]).read().split())
OUT = open(os.environ["GATE_OUT"], "a")

def _function(nodeid):
    return re.sub(r"\[.*\]$", "", nodeid)

def pytest_collection_modifyitems(config, items):
    keep = [i for i in items if _function(i.nodeid) in WANT]
    config.hook.pytest_deselected(items=[i for i in items if _function(i.nodeid) not in WANT])
    items[:] = keep

def pytest_collectreport(report):
    if report.failed:
        OUT.write(json.dumps({"file": report.nodeid.split("::")[0], "outcome": "collect-error"}) + "\n")
        OUT.flush()

def pytest_runtest_logreport(report):
    if report.when != "call" and report.outcome == "passed":
        return                  # a passed setup says nothing: the call may skip
    OUT.write(json.dumps({"id": _function(report.nodeid), "when": report.when,
                          "outcome": report.outcome}) + "\n")
    OUT.flush()
'''


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


_parsed: dict[str, frozenset[str]] = {}


def functions(rev: str, strict: bool = False) -> set[str]:
    """The test functions `rev` defines, read from the source (ast), not
    collected: collecting would need `rev` importable, and a module that
    fails to import there would hide what it defines."""
    found: set[str] = set()
    for line in git("ls-tree", "-r", "-z", rev, "--", "tests").split(b"\0"):
        if not line:
            continue
        meta, path = line.decode().split("\t", 1)
        blob = meta.split()[2]
        if not fnmatch.fnmatch(path.rsplit("/", 1)[-1], "test_*.py"):
            continue
        key = f"{blob}:{path}"
        if key not in _parsed:
            try:
                tree = ast.parse(git("cat-file", "blob", blob), path)
            except SyntaxError:
                if strict:
                    raise
                tree = ast.Module(body=[], type_ignores=[])   # defines nothing here
            names: set[str] = set()

            def walk(body, prefix):
                for node in body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                        names.add(f"{path}::{prefix}{node.name}")
                    elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                        walk(node.body, f"{prefix}{node.name}::")
            walk(tree.body, "")
            _parsed[key] = frozenset(names)
        found |= _parsed[key]
    return found


def introduced(base: str, head: str, ids: set[str]) -> dict[str, list[str]]:
    """Commit -> the ids it introduced: the first commit in BASE..HEAD, parents
    before children, whose tree defines each id."""
    left = set(ids)
    by_commit: dict[str, list[str]] = {}
    for commit in git("rev-list", "--reverse", "--topo-order", f"{base}..{head}").decode().split():
        if not left:
            break
        here = functions(commit) & left
        if here:
            by_commit[commit] = sorted(here)
            left -= here
    if left:        # HEAD is in the range, so only a BASE that is not an ancestor
        by_commit[""] = sorted(left)
    return by_commit


def extract(rev: str, dest: Path, *paths: str) -> None:
    data = git("archive", "--format=tar", rev, *paths)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="tar")


def run(package: str, tests: str, ids: list[str], tmp: Path) -> tuple[list[dict], str]:
    """Run `ids` in a tree of `package`'s package and `tests`'s tests. Returns
    what the plugin recorded and pytest's output."""
    key = f"{package}-{tests}".replace("^", "p")
    # real path: on macOS the temp dir is /var/... -> /private/var/..., pytest
    # names tests relative to the resolved rootdir, and no id matched
    tree = tmp.resolve() / key
    tree.mkdir()
    extract(package, tree)
    shutil.rmtree(tree / "tests", ignore_errors=True)
    extract(tests, tree, "tests")
    want, report, log = tmp / f"{key}.ids", tmp / f"{key}.jsonl", tmp / f"{key}.log"
    want.write_text("\n".join(ids) + "\n")
    report.touch()
    files = sorted({i.split("::")[0] for i in ids})
    env = dict(os.environ, GATE_IDS=str(want), GATE_OUT=str(report),
               PYTHONPATH=os.pathsep.join([str(tree), str(tmp)]))
    # the tree's own package, never an installed one; the run's exit status is
    # expected to be non-zero, and what counts is what the plugin recorded
    with open(log, "w") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "_gate_select",
             "--rootdir", str(tree), "--continue-on-collection-errors", *files],
            cwd=tree, env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            proc.wait(TIMEOUT)
        except subprocess.TimeoutExpired:
            if os.name == "nt":                      # no process groups: the tree by parent
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                os.killpg(proc.pid, signal.SIGKILL)  # the whole group: a test's children too
            proc.wait()
            sink.write(f"\ntimed out after {TIMEOUT}s\n")
    shutil.rmtree(tree)
    return ([json.loads(line) for line in report.read_text().splitlines() if line],
            log.read_text(errors="replace"))


def judge(where: str, ids: list[str], records: list[dict], log: str,
          declared: bool) -> tuple[list[str], int, int]:
    seen: dict[str, set[str]] = defaultdict(set)
    broken: set[str] = set()
    for r in records:
        if r["outcome"] == "collect-error":
            broken.add(r["file"])
        else:
            seen[r["id"]].add(r["outcome"])
    lines, failing, erroring = [], 0, 0
    for f in ids:
        outcomes = seen.get(f, set())
        if "failed" in outcomes:
            failing += 1
        elif f.split("::")[0] in broken and not declared:
            erroring += 1                 # its module does not import there
        elif "passed" in outcomes or f.split("::")[0] in broken:
            what = "does not import" if f.split("::")[0] in broken else "is GREEN"
            lines.append(f"bad {f} {what} on {where} — it guards nothing")
        elif outcomes:
            lines.append(f"skip {f} was skipped on {where} — not verified")
        else:
            tail = " ".join(log.strip().splitlines()[-2:])
            lines.append(f"bad {f} did not run on {where}: {tail}")
    return lines, failing, erroring


def declared_fixes(head: str) -> tuple[dict[str, list[str]], list[str]]:
    """FIXES in `head`: id -> the fix commits it guards (any one of them must
    fail on its parent: a defect can be fixed at different commits on
    different filesystems), or ["guard"] for a test that checks a fix kept
    something working and so is green on the parent by design."""
    try:
        text = git("show", f"{head}:{FIXES}").decode()
    except subprocess.CalledProcessError:
        return {}, []
    fixes, errors = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        try:
            if len(words) < 2:
                raise ValueError
            if words[1:] == ["guard"]:
                fixes[words[0]] = ["guard"]
                continue
            fixes[words[0]] = [git("rev-parse", "--verify", "-q", f"{c}^{{commit}}").decode().strip()
                               for c in words[1:]]
        except (ValueError, subprocess.CalledProcessError):
            errors.append(f"bad {FIXES}:{n} is not '<test id> <fix commit>... | guard': {line.strip()}")
    return fixes, errors


def main(base: str, head: str = "HEAD") -> int:
    new = functions(head, strict=True) - functions(base)
    guards = {i for i in new if i.startswith(INVARIANT_GUARDS)}
    new -= guards
    if guards:
        print(f"skip {len(guards)} new functions under {INVARIANT_GUARDS} are invariant guards, "
              "not checked against a parent")
    fixes, lines = declared_fixes(head)
    lines += [f"bad {FIXES} names {i}, which is not a new test function outside {INVARIANT_GUARDS}"
              for i in sorted(set(fixes) - new)]
    guarded = {i for i in new if fixes.get(i) == ["guard"]}
    new -= guarded
    if guarded:
        lines.append(f"skip {len(guarded)} new functions {FIXES} marks as guards "
                     "(they check a fix kept something working), not checked against a parent")
    if not new:
        lines.append(f"skip no regression test functions added since {base}")
    # (package, tests) -> ids: a function runs on the parent of the commit
    # that added it or, when FIXES names the fix it guards, on the parent of
    # that fix. The fix must be in BASE..HEAD: a defect the base shipped fixed
    # is no evidence.
    runs: dict[tuple[str, str], list[str]] = defaultdict(list)
    window = set(git("rev-list", f"{base}..{head}").decode().split())
    candidates: dict[str, int] = {}       # id -> how many parents it is tried on
    for commit, ids in introduced(base, head, new).items():
        if not commit or not git("rev-list", "--parents", "-n1", commit).split()[1:]:
            lines += [f"bad {i} has no parent commit in {base}..{head} to run on" for i in ids]
            continue
        for i in ids:
            named = fixes.get(i, [])
            outside = [f for f in named if f not in window]
            if outside:
                lines += [f"bad {FIXES}: {f[:12]}, named for {i}, is not in {base}..{head}" for f in outside]
                continue
            for fix in named or [commit]:
                runs[(f"{fix}^", commit)].append(i)
            candidates[i] = len(named or [commit])
    # a test named with several fixes passes when it fails on ANY of their
    # parents; its other runs' complaints are kept only if none failed
    verdicts: dict[str, list[tuple[bool, list[str], bool, bool]]] = defaultdict(list)
    with tempfile.TemporaryDirectory(prefix="gate-") as t:
        tmp = Path(t)
        (tmp / "_gate_select.py").write_text(PLUGIN)
        for (package, tests), ids in runs.items():
            declared = package != f"{tests}^"
            where = f"the parent of {package[:12]}" + (f", the fix {FIXES} names" if declared else "")
            records, log = run(package, tests, ids, tmp)
            for i in ids:
                got, f, e = judge(where, [i], records, log, declared)
                verdicts[i].append((bool(f), got, bool(e), declared))
    failing = erroring = earlier = 0
    for i, runs_of_i in verdicts.items():
        hit = next((r for r in runs_of_i if r[0]), None)
        if hit:
            failing += 1
            earlier += 1 if hit[3] else 0
        elif any(r[2] for r in runs_of_i):
            erroring += 1
        else:
            for r in runs_of_i:
                lines += r[1]
    for line in lines:
        print(line)
    if failing or erroring:
        named = f" ({earlier} of them on the parent of the fix {FIXES} names)" if earlier else ""
        print(f"ok {failing} new test functions fail on the parent of the commit that added them"
              f"{named}, {erroring} error at import there")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
