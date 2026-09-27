"""Exercise the actual Claude Code command boundary, including process startup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

PYTHON = sys.executable   # the interpreter running the suite, on any machine
ROOT = Path(__file__).resolve().parents[1]
# Derive the matrix from the shipped wiring, retaining both recap events.
HOOKS = [
    (event, shlex.split(hook["command"])[1:], hook["timeout"])
    for event, groups in json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"].items()
    for group in groups for hook in group["hooks"]
]
HOOK_IDS = [f"{event}-{args[-1] if args[0] == 'hook' else 'inject'}"
            for event, args, _ in HOOKS]
WRONG = [None, [], ["nested"], {}, {"nested": []}, 42, True]
FIELDS = ["prompt", "session_id", "transcript_path", "tool_name", "hook_event_name", "force"]
INPUTS = [
    ("empty", b""), ("non-json", b"not json"), ("invalid-utf8", b'\xff\xfe'),
    ("huge", json.dumps({"prompt": "a" * 5_000_000}).encode()),
    ("deep", b'[' * 2000 + b']' * 2000),
    ("unknown-user", b'{"transcript_path":"~nosuchuser/session.jsonl","session_id":"fuzz"}'),
    ("nul-path", b'{"transcript_path":"\\u0000","session_id":"fuzz"}'),
]
INPUTS += [(f"top-{i}", json.dumps(value).encode())
           for i, value in enumerate([[], "string", 1, None, True])]
INPUTS += [(f"{key}-{i}", json.dumps({key: value}).encode())
           for key in FIELDS for i, value in enumerate(WRONG)]
INPUTS += [(f"tool-input-{i}", json.dumps({"tool_name": "Bash", "tool_input": value}).encode())
           for i, value in enumerate([*WRONG, "string"])]
INPUTS += [(f"{key}-{i}", json.dumps({"tool_name": tool, "tool_input": {key: value}}).encode())
           for tool, key in [("Bash", "command"), ("Edit", "file_path"),
                             ("Write", "file_path"), ("NotebookEdit", "notebook_path")]
           for i, value in enumerate(WRONG)]


@pytest.fixture
def hook_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SKILLMEM_", "MEM_", "XDG_"))}
    env.update(HOME=str(home), USERPROFILE=str(home),
               SKILLMEM_HOME=str(home / "store"), SKILLMEM_DB=str(home / "store" / "memory.db"),
               SKILLMEM_STATE_DIR=str(home / "state"), MEM_SEMANTIC="0",
               PYTHONPATH=str(ROOT), PATH=str(home / "empty-bin"))
    return env


def run_hook(hook, payload, env, *, limit=None):
    # Bounded by the shipped hook timeout, the limit Claude Code enforces; the
    # watchdog's own bound is asked by the tests that pass `limit`. A fixed 4 s
    # wall failed a different fast hook on each Windows run: process start-up
    # there, not the hook, went past it.
    event, args, timeout = hook
    limit = limit or timeout
    start = time.monotonic()
    result = subprocess.run([PYTHON, "-P", "-m", "skillmem.cli", *args],
                            input=payload, capture_output=True, env=env,
                            cwd=ROOT, timeout=limit)
    elapsed = time.monotonic() - start
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"Traceback" not in result.stderr
    assert elapsed < min(limit, timeout)
    output = result.stdout.decode("utf-8")
    args = args[2:] if args[0] == "--db" else args
    if args[0] == "inject":
        assert not output or output.startswith("# skillmem briefing\n")
    elif args[-1] == "session-recap":
        assert output == ""
    elif output:
        response = json.loads(output)
        assert set(response) == {"hookSpecificOutput"}
        assert set(response["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}
        assert response["hookSpecificOutput"]["hookEventName"] == event
        assert isinstance(response["hookSpecificOutput"]["additionalContext"], str)
    return result


@pytest.mark.parametrize("hook", HOOKS, ids=HOOK_IDS)
@pytest.mark.parametrize("payload", [p for _, p in INPUTS], ids=[n for n, _ in INPUTS])
def test_hook_stdin_matrix(hook, payload, hook_env):
    # Keep sibling fields valid so a malformed nested field actually reaches
    # its consumer instead of returning early for a missing session/tool.
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        value = None
    if isinstance(value, dict):
        payload = json.dumps({"session_id": "fuzz", "prompt": "latest version of deployment",
                              "tool_name": "Bash", "tool_input": {"command": "run deployment tests"},
                              "transcript_path": str(Path(hook_env["HOME"]) / "absent.jsonl"),
                              "hook_event_name": hook[0], **value}).encode()
    run_hook(hook, payload, hook_env)


DB_CASES = ["missing-db", "unreadable-db", "corrupt-db", "directory-db", "dangling-db",
            "loop-db", "unknown-user-db"]
# Every database state twice: named by SKILLMEM_DB, and by `--db`, which the
# group parses before the hook's own fail-open guard runs.
ENV_CASES = [*DB_CASES, *(f"{case}-option" for case in DB_CASES),
             "unknown-user-home", "unknown-user-state", "missing-home",
             "unreadable-files", "non-utf8-files", "directory-files", "dangling-files",
             "wrong-transcript-shapes", "wrong-mcp-shapes"]


@pytest.mark.parametrize("hook", HOOKS, ids=HOOK_IDS)
@pytest.mark.parametrize("case", ENV_CASES)
def test_hook_environment_matrix(hook, case, hook_env):
    home = Path(hook_env["HOME"])
    db = Path(hook_env["SKILLMEM_DB"])
    db.parent.mkdir()
    claude = home / ".claude"
    claude.mkdir()
    conf = home / ".claude.json"
    conf.write_text('{"mcpServers":{}}')
    baseline = claude / "mcp-baseline.txt"
    baseline.write_text("skillmem\n")
    project = home / "project"
    project.mkdir()
    transcript = project / "session.jsonl"
    transcript.write_text((json.dumps({"type": "user", "message": {
        "content": [{"type": "text", "text": "Please fix the deployment tests"}]}}) + "\n") * 25)
    memory = project / "memory"
    memory.mkdir()
    note = memory / "session-fuzz.md"
    note.write_text("Previous session's deployment work")
    files = [baseline, note, transcript]
    option = case.endswith("-option")
    case = case.removesuffix("-option")
    if case == "unreadable-db":
        db.touch(mode=0)
    elif case == "corrupt-db":
        db.write_bytes(b"not a sqlite database" * 100)
    elif case == "directory-db":
        db.mkdir()
    elif case == "dangling-db":
        db.symlink_to(home / "absent" / "memory.db")
    elif case == "loop-db":
        (home / "loop").symlink_to("loop")
        hook_env["SKILLMEM_DB"] = str(home / "loop" / "memory.db")
    elif case.startswith("unknown-user-"):
        key = {"db": "SKILLMEM_DB", "home": "SKILLMEM_HOME", "state": "SKILLMEM_STATE_DIR"}[case.rsplit("-", 1)[1]]
        hook_env[key] = "~nosuchuser/skillmem"
        if key == "SKILLMEM_HOME":
            hook_env.pop("SKILLMEM_DB")
    elif case == "missing-home":
        hook_env.pop("HOME")
        hook_env.pop("USERPROFILE")
    elif case == "unreadable-files":
        for path in files:
            path.chmod(0)
    elif case == "non-utf8-files":
        for path in files:
            path.write_bytes(b"\xff\xfe\n" * 25)
    elif case in ("directory-files", "dangling-files"):
        for path in files:
            path.unlink()
            if case == "directory-files":
                path.mkdir()
            else:
                path.symlink_to(home / "absent-file")
    elif case == "wrong-transcript-shapes":
        records = [None, [], "string", 42, {"type": "user", "message": "bad"},
                   *({"type": "assistant", "message": {"content": value}} for value in WRONG),
                   *({"type": "user", "message": {"content": [{"type": "text", "text": value}]}}
                     for value in WRONG)]
        transcript.write_text(("\n".join(json.dumps(r) for r in records) + "\n") * 3)
    elif case == "wrong-mcp-shapes":
        conf.write_text('{"mcpServers":[null]}')
    if option:
        hook = (hook[0], ["--db", hook_env["SKILLMEM_DB"], *hook[1]], hook[2])
    payload = json.dumps({"prompt": "What is the latest version of deployment?", "session_id": "fuzz",
                          "tool_name": "Bash", "tool_input": {"command": "run deployment tests"},
                          "transcript_path": str(transcript), "hook_event_name": hook[0]}).encode()
    try:
        run_hook(hook, payload, hook_env)
    finally:
        for path in [db, *files]:
            if path.exists() and not path.is_symlink():
                path.chmod(0o700)


def test_guard_discards_partial_output(hook_env):
    # Exercise the same command class when failure happens *after* an emit.
    script = '''
import click
from skillmem.hooks import HookCommand
@click.command(cls=HookCommand)
def command():
    click.echo("partial response")
    raise ValueError("bad nested data")
command()
'''
    result = subprocess.run([PYTHON, "-P", "-c", script], env=hook_env,
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""


@pytest.mark.skipif(sys.platform == "win32", reason=(
    "no SIGALRM on Windows, and a regex holds the GIL the watchdog thread needs: "
    "Claude Code's hook timeout bounds it there (CHANGELOG 0.12.0 Known issues)"))
def test_guard_bounds_a_stalled_hook(hook_env):
    # A user-supplied regex can otherwise hold verify-gate forever.
    hook_env["SKILLMEM_VERIFY_PATTERN"] = "(a+)+$"
    hook = next(h for h in HOOKS if h[1][-1] == "verify-gate")
    run_hook(hook, json.dumps({"prompt": "a" * 32 + "!"}).encode(), hook_env, limit=7)


def test_transcript_skips_bad_records_without_losing_good_turns(tmp_path):
    from skillmem.hooks import _filter_transcript
    transcript = tmp_path / "transcript.jsonl"
    records = [[], {"type": "user", "message": 42},
               {"type": "assistant", "message": {"content": [{"type": "text", "text": []}]}},
               {"type": "user", "message": {"content": "Keep this useful deployment instruction"}}]
    transcript.write_text("\n".join(map(json.dumps, records)))
    assert _filter_transcript(transcript) == "user: Keep this useful deployment instruction"


def test_full_init_wires_the_tested_commands(hook_env):
    result = subprocess.run([PYTHON, "-P", "-m", "skillmem.cli", "init", "--claude-code",
                             "--hooks", "full", "--skip-migrate"], env=hook_env,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    settings_file = Path(hook_env["HOME"]) / ".claude" / "settings.json"
    configured = json.loads(settings_file.read_text())["hooks"]
    commands = [(event, shlex.split(h["command"])[1:])
                for event, groups in configured.items() for group in groups for h in group["hooks"]]
    assert commands == [(event, args) for event, args, _ in HOOKS]


@pytest.mark.parametrize("args", [["inject"], ["hook", "auto-recall"], ["hook", "tool-recall"]])
def test_locked_database_does_not_outlive_hook_timeout(hook_env, args):
    import sqlite3
    db = Path(hook_env["SKILLMEM_DB"])
    db.parent.mkdir()
    connection = sqlite3.connect(db)
    connection.execute("create table held (value)")
    connection.execute("begin exclusive")
    hook_env["SKILLMEM_BUSY_TIMEOUT_MS"] = "60000"
    payload = json.dumps({"prompt": "Remember deployment instructions", "tool_name": "Bash",
                          "tool_input": {"command": "run deployment tests"}}).encode()
    try:
        run_hook(("PreToolUse" if args[-1] == "tool-recall" else "UserPromptSubmit", args, 10),
                 payload, hook_env, limit=7)
    finally:
        connection.close()


@pytest.mark.parametrize("hook", HOOKS, ids=HOOK_IDS)
def test_a_hook_answers_the_same_on_a_non_utf8_stdout(hook, hook_env):
    """UTF-8 on any stdout: `inject` alone did not reconfigure it, and on a
    cp1252 pipe (Windows) the briefing failed to encode and came out empty."""
    seed = ("from skillmem import storage as S\nS.owner_present = lambda: True\n"
            "c = S.connect(); S.init_schema(c)\n"
            "S.upsert(c, S.MemoryItem(slug='fb-rule', kind='feedback', origin='owner', "
            "title='Всегда запускай тесты ✅', body='run the deployment tests'), surface='cli')\n")
    subprocess.run([PYTHON, "-P", "-c", seed], env=hook_env, check=True, timeout=10)
    payload = json.dumps({"prompt": "run the deployment tests", "session_id": "enc",
                          "tool_name": "Bash", "tool_input": {"command": "run deployment tests"},
                          "hook_event_name": hook[0]}).encode()
    outputs = []
    for encoding in ("cp1252", "utf-8"):
        env = {**hook_env, "PYTHONIOENCODING": encoding,
               "SKILLMEM_STATE_DIR": str(Path(hook_env["HOME"]) / f"state-{encoding}")}
        outputs.append(run_hook(hook, payload, env).stdout)
    assert outputs[0] == outputs[1]
    if hook[1][0] == "inject":
        assert "Всегда".encode() in outputs[1], "test premise: the briefing names the rule"
