"""INV-12: concurrent cron mutations preserve every other database's jobs."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from skillmem import schedule


WORKER = r'''
import os
from pathlib import Path
import sys
import time
from skillmem import schedule

root, who, operation = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def read():
    lines = (root / "crontab").read_text().splitlines()
    (root / (who + "-read")).touch()
    if who == "a":
        deadline = time.monotonic() + 15
        while not (root / "release").exists():
            assert time.monotonic() < deadline, "parent did not release first reader"
            time.sleep(.01)
    return lines

def write(lines):
    staged = root / (who + "-staged")
    staged.write_text("\n".join(lines) + "\n")
    os.replace(staged, root / "crontab")

schedule._cron_read = read
schedule._cron_write = write
(root / (who + "-ready")).touch()
getattr(schedule, "_cron_" + operation)()
'''


def _wait(path, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    return path.exists()


@pytest.mark.skipif(os.name == "nt", reason="cron is a POSIX backend")
@pytest.mark.parametrize("first", ["install", "remove"])
@pytest.mark.parametrize("second", ["install", "remove"])
@pytest.mark.parametrize("separate_data_dirs", [False, True])
def test_concurrent_cron_mutations_preserve_other_jobs(
        tmp_path, monkeypatch, first, second, separate_data_dirs):
    """Exhaust the operation pairs, including removal during systemd migration.

    Hold A after its read and start B. Without a lock B completes before A
    writes its stale snapshot; with a lock B cannot read until A is released.
    The crontab stand-in publishes atomically like the external utility.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    envs, marks = {}, {}
    for who in ("a", "b"):
        monkeypatch.setenv("SKILLMEM_DB", str(tmp_path / (who + ".db")))
        marks[who] = set(schedule._cron_marks().values())
        envs[who] = dict(os.environ, MEM_SEMANTIC="0",
                         PYTHONPATH=str(Path(schedule.__file__).resolve().parent.parent),
                         SKILLMEM_HOME=str(tmp_path / (who if separate_data_dirs else "data")))
    unrelated = "0 1 * * * echo unrelated"
    initial = [unrelated]
    for who, operation in (("a", first), ("b", second)):
        if operation == "remove":
            initial += ["0 0 * * * true " + mark for mark in sorted(marks[who])]
    (tmp_path / "crontab").write_text("\n".join(initial) + "\n")
    processes = []
    try:
        for who, operation in (("a", first), ("b", second)):
            processes.append(subprocess.Popen(
                [sys.executable, "-P", "-c", WORKER, str(tmp_path), who, operation],
                env=envs[who], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            assert _wait(tmp_path / (who + "-ready"))
            if who == "a":
                assert _wait(tmp_path / "a-read")
        if _wait(tmp_path / "b-read", timeout=1):
            # On the parent, guarantee B's write precedes A's stale write.
            out, err = processes[1].communicate(timeout=10)
            assert processes[1].returncode == 0, out + err
        (tmp_path / "release").touch()
        for process in processes:
            out, err = process.communicate(timeout=10)
            assert process.returncode == 0, out + err
    finally:
        (tmp_path / "release").touch()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
    lines = (tmp_path / "crontab").read_text().splitlines()
    assert unrelated in lines
    for who, operation in (("a", first), ("b", second)):
        actual = [mark for line in lines for mark in marks[who]
                  if line.endswith(" " + mark)]
        assert sorted(actual) == (sorted(marks[who]) if operation == "install" else []), \
            "INV-12: a concurrent cron mutation lost or resurrected another database's jobs"
