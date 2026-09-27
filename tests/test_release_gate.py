"""Release gate check 5 (scripts/gate_new_tests.py), on throwaway repositories:
a new regression test must fail on the parent of the commit that added it."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOOD = "def add(a, b):\n    return a + b\n"
BUG = "def add(a, b):\n    return a - b\n"
TEST_ADD = "from pkg import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@t")
        self.git("init", "-q", "-b", "main")
        self.write("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        self.write("pkg/__init__.py", GOOD)
        self.write("tests/__init__.py", "")
        self.write("tests/test_base.py", "def test_old():\n    pass\n")
        self.commit("base")
        self.git("tag", "v0")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.path, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, rel: str, text: str) -> None:
        (self.path / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.path / rel).write_text(text)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def gate(self, **env: str) -> list[str]:
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "gate_new_tests.py"), "v0"],
                                cwd=self.path, env={**self.env, **env}, capture_output=True,
                                text=True, timeout=300)
        assert result.returncode == 0, result.stderr
        return result.stdout.splitlines()


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def bad(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("bad ")]


def test_a_test_for_a_defect_made_and_fixed_since_the_base_passes(repo: Repo) -> None:
    """Green on the base (the defect never shipped), red on its parent: the
    rule this gate enforces, where judging against the base called it green."""
    repo.write("pkg/__init__.py", BUG)
    repo.commit("break add")
    repo.write("pkg/__init__.py", GOOD)
    repo.write("tests/test_add.py", TEST_ADD)
    repo.commit("fix add, with its test")
    lines = repo.gate()
    assert bad(lines) == [], lines
    assert lines[-1].startswith("ok 1 new test functions fail on the parent"), lines


def test_a_test_green_on_its_parent_fails_naming_its_commit(repo: Repo) -> None:
    repo.write("pkg/__init__.py", BUG)
    repo.commit("break add")
    repo.write("pkg/__init__.py", GOOD)
    repo.write("tests/test_add.py", TEST_ADD)
    repo.commit("fix add, with its test")
    repo.write("tests/test_add.py", TEST_ADD + "\n\ndef test_zero():\n    assert add(0, 0) == 0\n")
    green = repo.commit("a test that guards nothing")
    lines = repo.gate()
    assert bad(lines) == [f"bad tests/test_add.py::test_zero is GREEN on the parent of "
                          f"{green[:12]} — it guards nothing"], lines
    assert any(line.startswith("ok 1 ") for line in lines), lines   # test_add, on its own parent


def test_invariant_guards_are_skipped_and_counted(repo: Repo) -> None:
    repo.write("tests/properties/__init__.py", "")
    repo.write("tests/properties/test_inv.py",
               "def test_a():\n    pass\n\n\nclass TestB:\n    def test_b(self):\n        pass\n")
    repo.commit("invariants")
    lines = repo.gate()
    assert lines[0] == ("skip 2 new functions under tests/properties/ are invariant guards, "
                        "not checked against a parent"), lines
    assert bad(lines) == [], lines


def test_a_test_on_a_merged_branch_is_judged_on_its_own_commit(repo: Repo) -> None:
    repo.git("checkout", "-q", "-b", "side")
    repo.write("tests/test_side.py", "def test_side():\n    pass\n")
    side = repo.commit("green test on a branch")
    repo.git("checkout", "-q", "main")
    repo.write("pkg/other.py", "X = 1\n")
    repo.commit("unrelated")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    assert bad(repo.gate()) == [f"bad tests/test_side.py::test_side is GREEN on the parent of "
                                f"{side[:12]} — it guards nothing"]


def test_a_module_that_does_not_import_on_the_parent_counts_as_an_error(repo: Repo) -> None:
    repo.write("pkg/__init__.py", GOOD + "\n\ndef mul(a, b):\n    return a * b\n")
    repo.write("tests/test_mul.py", "from pkg import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
    repo.commit("add mul")
    lines = repo.gate()
    assert bad(lines) == [] and lines[-1].endswith("1 error at import there"), lines


def test_a_function_pytest_does_not_run_is_not_a_pass(repo: Repo) -> None:
    repo.write("pkg/__init__.py", BUG)
    repo.commit("break add")
    repo.write("pkg/__init__.py", GOOD)
    repo.write("tests/test_add.py", "__test__ = False\n" + TEST_ADD)
    repo.commit("fix add, with a test pytest ignores")
    lines = repo.gate()
    head = repo.git("rev-parse", "HEAD")
    assert len(bad(lines)) == 1 and bad(lines)[0].startswith(
        f"bad tests/test_add.py::test_add did not run on the parent of {head[:12]}: "), lines
    assert not any(line.startswith("ok ") for line in lines), lines


def test_a_run_that_outlives_its_timeout_is_not_a_pass(repo: Repo) -> None:
    repo.write("tests/test_slow.py", "import time\n\n\ndef test_slow():\n    time.sleep(120)\n")
    repo.commit("slow")
    lines = repo.gate(GATE_TEST_TIMEOUT="3")
    assert len(bad(lines)) == 1 and "did not run" in lines[0] and "timed out" in lines[0], lines


def test_a_test_committed_after_its_fix_is_judged_on_the_fix_it_names(repo: Repo) -> None:
    repo.write("pkg/__init__.py", BUG)
    repo.commit("break add")
    repo.write("pkg/__init__.py", GOOD)
    fix = repo.commit("fix add")
    repo.write("tests/test_add.py", TEST_ADD)
    late = repo.commit("its test, one commit late")
    assert bad(repo.gate()) == [f"bad tests/test_add.py::test_add is GREEN on the parent of "
                                f"{late[:12]} — it guards nothing"]

    repo.write("tests/gate-fixes.txt", f"# comment\ntests/test_add.py::test_add {fix[:7]}  # why\n")
    repo.commit("name the fix")
    lines = repo.gate()
    assert bad(lines) == [], lines
    assert lines[-1].startswith("ok 1 new test functions fail on the parent of the commit that "
                                "added them (1 of them on the parent of the fix"), lines


def test_a_named_fix_is_verified_not_trusted(repo: Repo) -> None:
    repo.write("tests/test_add.py", TEST_ADD)
    repo.commit("a test that guards nothing")
    other = repo.commit("an empty commit, named as its fix")
    base = repo.git("rev-parse", "v0")
    repo.write("tests/gate-fixes.txt", f"tests/test_add.py::test_add {other}\n")
    repo.commit("name a fix it does not guard")
    assert bad(repo.gate()) == [f"bad tests/test_add.py::test_add is GREEN on the parent of "
                                f"{other[:12]}, the fix tests/gate-fixes.txt names — it guards nothing"]

    repo.write("tests/gate-fixes.txt",
               f"tests/test_add.py::test_add {base}\n"            # the base: shipped fixed
               "tests/test_base.py::test_old v0\n"                # not a new function
               "tests/test_add.py::test_add\n")                   # no commit
    repo.commit("three bad entries")
    lines = bad(repo.gate())
    assert f"bad tests/gate-fixes.txt: {base[:12]}, named for tests/test_add.py::test_add, is not in v0..HEAD" in lines
    assert "bad tests/gate-fixes.txt names tests/test_base.py::test_old, which is not a new test function outside tests/properties/" in lines
    assert ("bad tests/gate-fixes.txt:3 is not '<test id> <fix commit>... | guard': "
            "tests/test_add.py::test_add") in lines


def test_a_test_naming_several_fixes_passes_when_one_parent_fails(repo: Repo) -> None:
    """One defect, fixed at a different commit on each filesystem: the test
    names both and must fail on the parent of at least one."""
    unrelated = repo.commit("an empty commit")
    repo.write("pkg/__init__.py", BUG)
    repo.commit("break add")
    repo.write("pkg/__init__.py", GOOD)
    fix = repo.commit("fix add")
    repo.write("tests/test_add.py", TEST_ADD)
    repo.write("tests/gate-fixes.txt", f"tests/test_add.py::test_add {unrelated} {fix}\n")
    repo.commit("its test, naming two fixes")
    lines = repo.gate()
    assert bad(lines) == [], lines
    assert any(line.startswith("ok 1 ") for line in lines), lines

    repo.write("tests/gate-fixes.txt", f"tests/test_add.py::test_add {unrelated}\n")
    repo.commit("name only the unrelated one")
    assert len(bad(repo.gate())) == 1


def test_a_test_marked_guard_is_skipped_and_counted(repo: Repo) -> None:
    repo.write("tests/test_keep.py", "def test_keep():\n    pass\n")
    repo.write("tests/gate-fixes.txt", "tests/test_keep.py::test_keep guard   # a fix kept this\n")
    repo.commit("a guard")
    lines = repo.gate()
    assert bad(lines) == [], lines
    assert any("1 new functions tests/gate-fixes.txt marks as guards" in line for line in lines), lines


def test_a_named_fix_needs_a_failure_not_an_import_error(repo: Repo) -> None:
    fix = repo.commit("empty")
    repo.write("pkg/__init__.py", GOOD + "\n\ndef mul(a, b):\n    return a * b\n")
    repo.write("tests/test_mul.py", "from pkg import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
    repo.write("tests/gate-fixes.txt", f"tests/test_mul.py::test_mul {fix}\n")
    repo.commit("add mul, and name an unrelated fix")
    assert bad(repo.gate()) == [f"bad tests/test_mul.py::test_mul does not import on the parent of "
                                f"{fix[:12]}, the fix tests/gate-fixes.txt names — it guards nothing"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.skipif(sys.platform == "win32", reason=(
    "release-gate.sh is a POSIX shell script: Windows resolves `bash` to the WSL "
    "launcher in System32, and the docker stand-in is a #!/bin/sh file"))
@pytest.mark.parametrize("green", [False, True])
def test_release_gate_reports_check_5(repo: Repo, tmp_path: Path, green: bool) -> None:
    """The whole gate on a repository that carries its scripts: PASS, and a
    green test FAILs it."""
    for name in ("release-gate.sh", "gate_new_tests.py"):
        repo.write(f"scripts/{name}", (ROOT / "scripts" / name).read_text())
    repo.write("pyproject.toml", 'version = "1.0.0"\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    repo.write("skillmem/__init__.py", '__version__ = "1.0.0"\n')
    for name in ("plugin.json", ".claude-plugin/plugin.json"):
        repo.write(name, '{"version": "1.0.0"}\n')
    repo.write("server.json", '{"version": "1.0.0",\n "packages": [{"version": "1.0.0"}]}\n')
    repo.write("CHANGELOG.md", "## 1.0.0\n")
    repo.write("pkg/__init__.py", BUG)
    repo.commit("break add")
    repo.write("pkg/__init__.py", GOOD)
    repo.write("tests/test_add.py", TEST_ADD)
    repo.commit("fix add, with its test")
    if green:
        repo.write("tests/test_green.py", "def test_green():\n    pass\n")
        repo.commit("green")
    nodocker = tmp_path / "bin"
    nodocker.mkdir()
    (nodocker / "docker").write_text("#!/bin/sh\nexit 1\n")
    (nodocker / "docker").chmod(0o755)
    result = subprocess.run(
        ["bash", "scripts/release-gate.sh", "v0"], cwd=repo.path, capture_output=True, text=True,
        env={**repo.env, "PY": sys.executable, "PATH": f"{nodocker}{os.pathsep}{os.environ['PATH']}"},
        timeout=300)
    out = result.stdout
    assert "PASS  pytest:" in out, out
    if green:
        assert result.returncode == 1 and "FAIL  tests/test_green.py::test_green is GREEN" in out, out
    else:
        assert result.returncode == 0 and "GATE PASS" in out, out
        assert "PASS  1 new test functions fail on the parent" in out, out
