"""INV-08: exceptional exits undo their writes and leave connections reusable."""
import asyncio
from contextlib import ExitStack

import pytest
from hypothesis import given, strategies as st

from skillmem import storage as S
from .support import PROPERTY, database, put


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit, GeneratorExit,
                                   asyncio.CancelledError, RuntimeError])
@pytest.mark.parametrize("nested", [False, True])
@PROPERTY
@given(depth=st.integers(min_value=1, max_value=4),
       writes=st.integers(min_value=1, max_value=4))
def test_exception_rolls_back_its_scope_and_later_writes_persist(error, nested, depth, writes):
    with database() as (conn, root, _):
        put(conn, slug="baseline")
        interruption = error("interrupted")

        def interrupt():
            with pytest.raises(error) as caught:
                with ExitStack() as scopes:
                    for level in range(depth):
                        scopes.enter_context(S.tx(conn))
                        for index in range(writes):
                            put(conn, slug=f"failed-{level}-{index}")
                    raise interruption
            assert caught.value is interruption

        if nested:
            with S.tx(conn):
                put(conn, slug="before")
                interrupt()
                assert conn.in_transaction
                assert conn.execute("SELECT count(*) FROM memory_items WHERE slug LIKE 'failed-%'").fetchone()[0] == 0
                put(conn, slug="after")
        else:
            interrupt()

        # An interrupted outer scope must not make later writes mere savepoints.
        put(conn, slug="next")
        assert not conn.in_transaction
        assert id(conn) not in S._deferred_embeddings
        expected = {"baseline", "next"} | ({"before", "after"} if nested else set())
        other = S.connect(root / "memory.db")
        try:
            assert {row[0] for row in other.execute("SELECT slug FROM memory_items")} == expected
            conn.close()
            assert {row[0] for row in other.execute("SELECT slug FROM memory_items")} == expected
        finally:
            other.close()


class _InterruptedAfter:
    """A connection whose `statement` runs, then is interrupted before the next
    line: a Ctrl-C delivered while BEGIN IMMEDIATE waited for another writer."""

    def __init__(self, conn, statement):
        self._conn, self._statement = conn, statement

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        cursor = self._conn.execute(sql, *args)
        if sql == self._statement:
            self._statement = None
            raise KeyboardInterrupt
        return cursor


@pytest.mark.parametrize("scope, statement", [(S.tx, "BEGIN IMMEDIATE"), (S.snapshot, "BEGIN")])
def test_an_interrupt_as_a_scope_opens_leaves_no_transaction(scope, statement):
    # r09 opus review: the opener ran before `try:`, so the interrupt left the
    # transaction open, and every later tx() was a savepoint inside it, its
    # writes acknowledged and lost when the connection closed.
    with database() as (conn, root, _):
        interrupted = _InterruptedAfter(conn, statement)
        with pytest.raises(KeyboardInterrupt):
            with scope(interrupted):
                pass
        assert not conn.in_transaction
        put(interrupted, slug="after")
        assert not conn.in_transaction
        other = S.connect(root / "memory.db")
        try:
            assert [row[0] for row in other.execute("SELECT slug FROM memory_items")] == ["after"]
        finally:
            other.close()


class _FailsAt:
    """A connection whose `statement` raises instead of running: a COMMIT that
    fails (or a Ctrl-C delivered as the scope closes, before COMMIT runs)."""

    def __init__(self, conn, statement, error):
        self._conn, self._statement, self._error = conn, statement, error

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        if sql == self._statement:
            self._statement = None
            raise self._error
        return self._conn.execute(sql, *args)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), S.sqlite3.OperationalError("disk I/O error")])
@pytest.mark.parametrize("scope", [S.tx, S.snapshot])
def test_a_failed_commit_leaves_no_transaction(scope, error):
    # r16 opus review: COMMIT sat outside tx()'s rollback handler, the twin of
    # the r09 BEGIN fix; a failed COMMIT kept the write lock, and every later
    # write was a savepoint inside it, acknowledged and lost on close.
    with database() as (conn, root, _):
        failing = _FailsAt(conn, "COMMIT", error)
        with pytest.raises(type(error)):
            with scope(failing):
                put(failing, slug="unacknowledged")
        assert not conn.in_transaction
        put(conn, slug="after")
        assert not conn.in_transaction
        assert id(conn) not in S._deferred_embeddings
        other = S.connect(root / "memory.db")
        try:
            assert [row[0] for row in other.execute("SELECT slug FROM memory_items")] == ["after"]
        finally:
            other.close()
