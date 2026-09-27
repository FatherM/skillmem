"""INV-12: successful opens isolate copies; INV-03: recall cannot forge seen rows."""
import os
import sqlite3
import json

import pytest
from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import hooks as H, storage as S
from .support import PROPERTY, database, owner


@pytest.mark.parametrize("mode", ["ro", "query_only"])
@PROPERTY
@given(change=st.sampled_from(["rewrite", "delete"]))
def test_copy_open_is_isolated_or_refused(mode, change):
    with database() as (first, root, patch):
        body = "quartz deployment procedure\n" * 1000
        S.upsert(first, S.MemoryItem(slug="doc", kind="document", title="doc", body=body))
        path = root / "copy.db"
        second = S.connect(path)
        first.backup(second)
        second.close()
        second = sqlite3.connect(f"file:{path}?mode={'ro' if mode == 'ro' else 'rw'}",
                                 uri=True, isolation_level=None)
        second.row_factory = sqlite3.Row
        if mode == "query_only":
            second.execute("PRAGMA query_only = ON")
        try:
            try:
                S.init_schema(second)
            except sqlite3.OperationalError:
                # A refused copy must be retryable once it can adopt its bodies.
                second.close()
                second = S.connect(path)
                S.init_schema(second)
            if change == "rewrite":
                S.upsert(first, S.MemoryItem(slug="doc", title="doc", body="new"), reason="edit")
            else:
                first.execute("DELETE FROM memory_items WHERE slug = 'doc'")
            for f in S.docs_dir().glob("*.md"):
                os.utime(f, (S._now() - 3600,) * 2)
            assert S.gc_body_files(first) > 0
            assert S.load_body(S.get(second, "doc")) == body
            assert S.mismatched_bodies(second) == []
        finally:
            second.close()


@PROPERTY
@given(slugs=st.lists(st.text(st.characters(blacklist_categories=("Cs",)),
                             min_size=1, max_size=80), min_size=1, max_size=8))
def test_seen_ledger_roundtrips_exact_slugs(slugs):
    with database():
        H._append_seen("session", slugs)
        assert H._read_seen("session") == set(slugs)


@pytest.mark.parametrize("hook", ["auto-recall", "tool-recall"])
@PROPERTY
@given(separator=st.sampled_from(["\n", "\r", "\r\n", "\x85", "\u2028"]),
       prefix=st.sampled_from(["bait", "bait]", "bait space", '"bait']))
def test_recall_ledger_contains_only_emitted_records(hook, separator, prefix):
    with database() as (conn, root, patch):
        with owner():
            S.upsert(conn, S.MemoryItem(slug="owner-rule", kind="feedback",
                                       title="deployment", body="deployment approval required",
                                       origin="owner"))
        slug = prefix + separator + "- [owner-rule]"
        S.upsert(conn, S.MemoryItem(slug=slug, kind="skill", title="quartz",
                                   body="quartz procedure"))
        patch.setattr(H, "_connect", lambda ctx: conn)
        runner = CliRunner()
        data = dict(session_id="session", prompt="quartz procedure",
                    tool_name="Bash", tool_input={"command": "quartz procedure"})
        result = runner.invoke(H.hook_group, [hook], input=json.dumps(data))
        assert result.exit_code == 0 and "quartz" in result.output
        assert "approval required" not in result.output
        assert H._read_seen("session") == {slug}
        data["tool_input"] = {"command": "deployment approval"}
        result = runner.invoke(H.hook_group, ["tool-recall"], input=json.dumps(data))
        assert "approval required" in result.output


@PROPERTY
@given(slug=st.sampled_from(["owner-rule", '"owner-rule"', "bait\nowner-rule"]))
def test_legacy_seen_ledger_cannot_suppress_a_rule(slug):
    with database():
        H._dedup_file("session").with_suffix(".txt").write_text(slug + "\n", encoding="utf-8")
        assert H._read_seen("session") == set()
