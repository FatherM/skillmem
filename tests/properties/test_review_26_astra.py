"""INV-08/14: directory links and named nulls cannot disappear on import."""
import json

import pytest
from click.testing import CliRunner
from hypothesis import example, given, strategies as st

from skillmem import storage as S, vault
from skillmem.cli import main
from .support import PROPERTY, database, owner, put


@pytest.mark.parametrize("surface", ["pack", "vault"])
@PROPERTY
@example(destination="outside", nested=False)
@given(destination=st.sampled_from(["inside", "outside", "missing", "cycle"]),
       nested=st.booleans())
def test_directory_links_are_reported(surface, destination, nested):
    with database() as (conn, root, patch):
        tree = root / "source"
        tree.mkdir()
        target = (tree if destination == "inside" else root) / "shared"
        target.mkdir()
        (target / "SKILL.md").write_text("---\nname: shared\n---\nShared procedure.\n")
        (tree / "SKILL.md").write_text("---\nname: local\n---\nLocal procedure.\n")
        parent = tree / "nested" if nested else tree
        parent.mkdir(exist_ok=True)
        link = parent / "linked"
        link.symlink_to(link if destination == "cycle" else
                        root / "missing" if destination == "missing" else target,
                        target_is_directory=True)
        if surface == "pack":
            result = CliRunner().invoke(main, ["skills", "add", str(tree), "--name", "demo"])
            assert result.exit_code == 1, result.output
            report, _ = json.JSONDecoder().raw_decode(result.output)
            assert any(entry["path"] == link.relative_to(tree).as_posix()
                       for entry in report["skipped"])
        else:
            report = vault.import_vault(conn, tree)
            assert any(path == link.relative_to(tree).as_posix() for path, _ in report.failed)


@pytest.mark.parametrize("key", ["access_count", "confirmed_count", "failure_count",
                                "created_at", "updated_at"])
@PROPERTY
@example(existing=True, changed=False, value=7)
@given(existing=st.booleans(), changed=st.booleans(), value=st.integers(1, 10000))
def test_required_dump_numbers_refuse_null_without_writing(key, existing, changed, value):
    with database() as (conn, root, _):
        if existing:
            put(conn)
            with S.tx(conn):
                conn.execute(f"UPDATE memory_items SET {key} = ?", (value,))
        before = [tuple(row) for row in conn.execute("SELECT * FROM memory_items")]
        notes = root / "notes"
        notes.mkdir()
        body = "changed procedure" if changed else "quartz deployment procedure"
        (notes / "record.md").write_text(
            f"---\nname: record\ndescription: quartz\nexported_at: 1\nmetadata: {{node_type: memory, type: skill}}\n"
            f"{key}: null\n---\n\n{body}\n")
        with owner():
            report = vault.import_vault(conn, notes, skip_auto_memories=False)
        assert report.failed, f"INV-14: {key}: null was silently accepted"
        assert report.inserted == report.updated == 0
        assert [tuple(row) for row in conn.execute("SELECT * FROM memory_items")] == before
        assert S.history(conn, "record") == []
