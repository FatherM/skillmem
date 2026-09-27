"""INV-05/06: the r11 review of 50fb10d."""
import os
import shutil

from hypothesis import given, strategies as st

from skillmem import storage as S
from skillmem.export import _record_in, export_all
from skillmem.vault import import_vault
from .support import PROPERTY, database, owner


# INV-05: export read the records it dumps and the records it calls its own in
# two statements; a restore committed between them made it take another
# database's dump of that record for its own and prune it, writing none
@PROPERTY
@given(at=st.integers(min_value=0, max_value=4))
def test_a_restore_during_an_export_never_loses_a_dump(at):
    with database() as (conn, root, patch):
        out, gone = root / "shared", root / "gone.db"
        b = S.connect(gone); S.init_schema(b)
        for slug in ("a", "b"):
            S.upsert(b, S.MemoryItem(slug=slug, kind="note", title=slug, body=f"{slug} body"))
        export_all(b, out)
        b.close()
        for suffix in ("", "-wal", "-shm"):
            gone.with_name(gone.name + suffix).unlink(missing_ok=True)
        dump = {}
        for slug in ("a", "b"):
            (root / slug / "note").mkdir(parents=True)
            shutil.copy(out / "note" / f"{slug}.md", root / slug / "note" / f"{slug}.md")
            dump[slug] = _record_in(out / "note" / f"{slug}.md")
        with owner():
            assert import_vault(conn, root / "a").inserted == 1
        other = S.connect(root / "memory.db"); S.init_schema(other)
        seen = []

        def restore(sql):
            if (len(seen) <= at and "memory_items" in sql and not conn.in_transaction
                    and sql.lstrip().upper().startswith("SELECT")):
                seen.append(sql)
                if len(seen) > at:
                    assert import_vault(other, root / "b").inserted == 1
        try:
            with owner():
                conn.set_trace_callback(restore)
                try:
                    export_all(conn, out)
                finally:
                    conn.set_trace_callback(None)
                if len(seen) <= at:
                    import_vault(other, root / "b")    # after the export: a serial order
        finally:
            other.close()
        assert _record_in(out / "note" / "b.md") == dump["b"], \
            "INV-05: the only dump of b was pruned"
        assert os.path.exists(out / "note" / "a.md")
