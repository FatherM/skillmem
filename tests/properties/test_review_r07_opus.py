"""INV-14: the r07 review of 3aa988e."""
from hypothesis import given, strategies as st

from skillmem import storage as S
from skillmem.migrate import import_dir
from skillmem.vault import import_vault
from .support import PROPERTY, database, owner

KEPT = {"visibility": "public", "tags": ["ops"], "topics": ["infra"], "project": "web",
        "ttl_days": 30}


# INV-14: `metadata.node_type: memory` alone made a file a dump, and Claude Code's
# auto-memories carry it too: import-vault cleared every field such a file does not
# state and raised its origin to owner, sealed. Only an export (`exported_at`) is a dump.
@PROPERTY
@given(fields=st.sets(st.sampled_from(sorted(KEPT)), min_size=1),
       reinforced=st.integers(0, 3), text_changed=st.booleans())
def test_an_auto_memory_is_not_a_dump(fields, reinforced, text_changed):
    with database() as (conn, root, _):
        memory = root / ".claude" / "projects" / "p" / "memory"
        memory.mkdir(parents=True)
        (memory / "deploy.md").write_text(
            "---\nname: deploy\ndescription: Deploy\nmetadata:\n"
            "  node_type: memory\n  type: skill\n---\n\nRun make deploy.\n")
        assert import_dir(conn, memory).inserted == 1
        set_ = {k: KEPT[k] for k in fields}
        S.upsert(conn, S.MemoryItem(slug="deploy", kind="skill", title="Deploy",
                                    body="Run make deploy.", **set_),
                 surface="mcp", explicit=set(set_))
        for _ in range(reinforced):
            S.reinforce(conn, "deploy", evidence="test_passed")
        before = S.get(conn, "deploy")
        if text_changed:
            (memory / "deploy.md").write_text(
                "---\nname: deploy\ndescription: Deploy\nmetadata:\n"
                "  node_type: memory\n  type: skill\n---\n\nRun make release.\n")
        with owner():
            report = import_vault(conn, memory, skip_auto_memories=False)
        assert not report.failed and report.updated == 1
        after = S.get(conn, "deploy")
        # the owner importing new text makes it theirs (section 2: note, origin chg);
        # the seal follows INV-02: the owner's import at a terminal mints it
        ownership = () if text_changed else ("origin",)
        for key in (*KEPT, "strength", "pinned", *ownership):
            assert getattr(after, key) == getattr(before, key), \
                f"INV-14: {key} {getattr(before, key)!r} -> {getattr(after, key)!r}"
