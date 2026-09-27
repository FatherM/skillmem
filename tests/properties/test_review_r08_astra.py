"""INV-08: auto-discovery failures must reach every import command."""
import json
import os
import sys

import pytest
from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import cli, storage as S
from .support import PROPERTY, database, owner


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="POSIX modes, which root ignores")
@pytest.mark.parametrize("command", ["migrate", "init"])
@PROPERTY
@given(mode=st.sampled_from([0o755, 0o000, 0o311, 0o544]),
       level=st.sampled_from(["claude", "projects", "project", "memory"]),
       count=st.integers(min_value=1, max_value=3))
def test_auto_discovery_imports_notes_or_reports_failure(command, mode, level, count):
    with database() as (conn, root, _):
        projects = root / ".claude" / "projects"
        folder = projects / "p" / "memory"
        folder.mkdir(parents=True)
        for i in range(count):
            (folder / f"deploy{i}.md").write_text(f"Deployment procedure {i}\n")
        blocked = {"claude": projects.parent, "projects": projects,
                   "project": folder.parent, "memory": folder}[level]
        try:
            blocked.chmod(mode)
            with owner():
                args = (["migrate"] if command == "migrate" else
                        ["init", "--migrate-existing", "--hooks", "none"])
                result = CliRunner().invoke(cli.main, args)
        finally:
            blocked.chmod(0o755)
        imported = sum(S.get(conn, f"deploy{i}") is not None for i in range(count))
        assert result.exit_code == (0 if imported == count else 1), result.output
        if imported != count:
            assert "no ~/.claude/projects" not in result.output
            if command == "init":
                report, _ = json.JSONDecoder().raw_decode(result.output)
                assert any(r["failed"] for r in report["migrations"])
            else:
                assert "failed=1" in result.output
            assert str(blocked) in result.output
        assert all((folder / f"deploy{i}.md").exists() for i in range(count))
