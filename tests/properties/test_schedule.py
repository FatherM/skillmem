"""INV-16: scheduler refreshes publish complete files or keep the old bytes."""
import errno
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import schedule
from .support import PROPERTY, database


@pytest.mark.parametrize("backend,index", [("launchd", i) for i in range(2)] +
                         [("systemd", i) for i in range(4)])
@pytest.mark.parametrize("failure", ["write", "rename"])
@PROPERTY
@given(old=st.binary(min_size=65, max_size=200), cut=st.integers(0, 64))
def test_scheduler_refresh_preserves_complete_files(backend, index, failure, old, cut):
    with database() as (_, root, patch):
        patch.setenv("XDG_CONFIG_HOME", str(root / "config"))
        patch.setattr(schedule.shutil, "which", lambda _: None)
        patch.setattr(schedule.subprocess, "run",
                      lambda *a, **kw: CompletedProcess(a[0], 0, stdout="", stderr=""))
        install = getattr(schedule, f"_{backend}_install")
        patch.setattr(schedule, "_backend", lambda: (install, None, None))
        install()
        folder = (schedule._launchd_plist_path("unused").parent if backend == "launchd"
                  else schedule._systemd_unit_dir())
        paths = (list(map(schedule._launchd_plist_path, schedule._launchd_labels().values()))
                 if backend == "launchd" else
                 [folder / f"{unit}.{suffix}" for unit in schedule._systemd_units().values()
                  for suffix in ("service", "timer")])
        expected = {p: p.read_bytes() for p in paths}
        for p in paths:
            p.write_bytes(old)
        real_bytes, real_text, real_replace = Path.write_bytes, Path.write_text, schedule.os.replace
        calls = 0

        def fail_write(path, data, *args, **kwargs):
            nonlocal calls
            ordinal = calls
            calls += 1
            if ordinal == index:
                real_bytes(path, (data.encode("utf-8") if isinstance(data, str) else data)[:cut])
                raise OSError(errno.ENOSPC, "injected partial write")
            return (real_text if isinstance(data, str) else real_bytes)(path, data, *args, **kwargs)

        def fail_rename(src, dst):
            if Path(dst) == paths[index]:
                raise OSError(errno.EIO, "injected rename failure")
            return real_replace(src, dst)

        with pytest.MonkeyPatch.context() as fault:
            if failure == "write":
                fault.setattr(Path, "write_bytes", fail_write)
                fault.setattr(Path, "write_text", fail_write)
            else:
                fault.setattr(schedule.os, "replace", fail_rename)
            result = CliRunner().invoke(schedule.schedule_group, ["install"])
        assert result.exit_code != 0, "INV-16: failed publication was acknowledged"
        assert paths[index].read_bytes() == old, "INV-16: failed refresh destroyed the old file"
        assert all(p.read_bytes() in (old, expected[p]) for p in paths)
        assert set(folder.iterdir()) == set(paths), "scratch file survived failure"
        install()
        assert {p: p.read_bytes() for p in paths} == expected


@PROPERTY
@given(home=st.sampled_from([None, "data", "./d/../data", "sub/home"]),
       db=st.sampled_from([None, "m.db", "./x/../m.db", "sub/other.db"]))
def test_scheduled_jobs_name_the_install_database_from_any_cwd(home, db):
    """INV-12: a job runs from another working directory (launchd: /, cron:
    $HOME), so every path it gets names the database the install meant (*r10
    review:* a relative SKILLMEM_DB/SKILLMEM_HOME was copied into the job, and
    the weekly backup exported a new empty database)."""
    with database() as (_, root, patch):
        work, elsewhere = root / "work", root / "elsewhere"
        work.mkdir()
        elsewhere.mkdir()
        patch.chdir(work)
        for name, value in (("SKILLMEM_HOME", home), ("SKILLMEM_DB", db)):
            if value is None:
                patch.delenv(name, raising=False)
            else:
                patch.setenv(name, value)
        from skillmem import storage as S
        meant = S.default_db_path().resolve()
        data = S.default_data_dir().resolve()
        env, jobs, logs = schedule._job_env(), schedule._jobs(), schedule._log_dir()
        assert all(Path(v).is_absolute() for v in env.values()), env
        assert Path(jobs["export"][-1]).is_absolute() and logs.is_absolute()
        patch.chdir(elsewhere)
        for name in ("SKILLMEM_HOME", "SKILLMEM_DB"):
            patch.delenv(name, raising=False)
        for name, value in env.items():
            patch.setenv(name, value)
        assert S.default_db_path().resolve() == meant
        if home is not None:
            assert S.default_data_dir().resolve() == data
