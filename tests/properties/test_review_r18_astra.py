"""INV-08/14: nested release failures and canonical import provenance."""
import asyncio

import pytest
from hypothesis import given, strategies as st

from skillmem import storage as S
from skillmem.migrate import import_dir
from skillmem.vault import import_vault
from .support import PROPERTY, database, owner, put


class FailRelease:
    def __init__(self, conn, error):
        self.conn, self.error = conn, error

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *args):
        if sql.startswith("RELEASE ") and self.error is not None:
            error, self.error = self.error, None
            raise error
        return self.conn.execute(sql, *args)


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit, GeneratorExit,
                                   asyncio.CancelledError, S.sqlite3.OperationalError])
@PROPERTY
@given(writes=st.integers(1, 4))
def test_failed_release_undoes_only_its_scope(error, writes):
    with database() as (conn, root, _):
        for i in range(writes):
            put(conn, slug=f"record-{i}")
        with S.tx(conn):
            put(conn, slug="before")
            failure = error("cancelled release")
            with pytest.raises(error) as caught:
                with S.tx(FailRelease(conn, failure)):
                    for i in range(writes):
                        S.soft_delete(conn, f"record-{i}", reason="cancelled deletion")
            assert caught.value is failure
            assert conn.in_transaction
            for i in range(writes):
                assert S.get(conn, f"record-{i}") is not None
            assert conn.execute("SELECT count(*) FROM memory_history").fetchone()[0] == 0
            put(conn, slug="after")
        other = S.connect(root / "memory.db")
        try:
            assert {r[0] for r in other.execute("SELECT slug FROM memory_items WHERE deleted_at IS NULL")} == {
                "before", "after", *[f"record-{i}" for i in range(writes)]}
            assert other.execute("SELECT count(*) FROM memory_history").fetchone()[0] == 0
        finally:
            other.close()


@pytest.mark.parametrize("importer", ["migrate", "vault"])
@PROPERTY
@given(letters=st.tuples(*(st.sampled_from([c, c.upper()]) for c in "note")),
       padding=st.sampled_from(["", " ", "\t"]), existing=st.booleans(),
       named=st.booleans())
def test_note_spelling_cannot_raise_origin_or_seal(importer, letters, padding, existing, named):
    with database() as (conn, root, _):
        if existing:
            S.upsert(conn, S.MemoryItem(slug="recap", kind="note", body="old"),
                     surface="mcp", explicit={"kind"})
        folder = root / "notes"
        folder.mkdir()
        kind = padding + "".join(letters) + padding
        # Quoted YAML preserves whitespace. Migrate has no --kind default.
        front = f'---\nmetadata:\n  type: "{kind}"\n---\n' if named or importer == "migrate" else ""
        (folder / "recap.md").write_text(front + "Recap\n\nnew text\n")
        with owner():
            report = (import_dir(conn, folder) if importer == "migrate"
                      else import_vault(conn, folder, kind=kind))
        assert not report.failed
        row = S.get(conn, "recap")
        assert row.kind == "note"
        assert row.origin == "derived"
        assert row.owner_seal == 0
