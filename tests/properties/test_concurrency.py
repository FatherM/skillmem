"""INV-05 §3: real second writers, with no sleeps or scheduler assumptions.

A cursor proxy pauses *after* the decisive read. Protected sites must exclude
an actual BEGIN IMMEDIATE on the other connection. Pre-lock reads and CAS sites
have separate tests that let the other writer commit and check the outcome.
File-only sites at the end use explicit interleavings rather than SQLite locks.
"""
import sqlite3
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from skillmem import cli, embed, export as E, hooks as H, mcp_server as M, storage as S
from skillmem.migrate import import_dir
from skillmem.packs import import_pack, remove_pack
from skillmem.vault import import_vault
from .support import database, owner, put


class Cursor:
    def __init__(self, cursor, callback):
        self.cursor, self.callback = cursor, callback

    def fetchone(self):
        result = self.cursor.fetchone()
        self.callback()
        return result

    def fetchall(self):
        result = self.cursor.fetchall()
        self.callback()
        return result

    def __iter__(self):
        for row in self.cursor:
            self.callback()
            yield row

    def __getattr__(self, key):
        return getattr(self.cursor, key)


class PauseConnection:
    def __init__(self, conn, predicate, callback):
        self.conn, self.predicate, self.callback = conn, predicate, callback
        self.fired = False

    def execute(self, sql, *args):
        cursor = self.conn.execute(sql, *args)
        if not self.fired and self.predicate(sql):
            def pause():
                if not self.fired:
                    self.fired = True
                    self.callback()
            return Cursor(cursor, pause)
        return cursor

    def __getattr__(self, key):
        return getattr(self.conn, key)


@contextmanager
def pair():
    with database() as (conn, root, patch):
        other = S.connect(root / "memory.db")
        other.execute("PRAGMA busy_timeout=0")
        try:
            yield conn, other, root, patch
        finally:
            other.close()


LOCKED = ["restem", "gc", "trust", "same_text", "update", "learn", "delete",
          "reinforce", "pin", "decay", "sweep", "archive", "restore", "recall",
          "mcp_write", "mcp_update", "mcp_learn", "cli_write", "cli_learn",
          "migrate", "vault", "pack", "remove_pack", "migration_seal", "migration_v10", "migration_history"]


@pytest.mark.parametrize("site", LOCKED)
def test_decisive_read_excludes_other_writer(site):
    with pair() as (conn, other, root, patch):
        put(conn, created_at=1)
        S.upsert(conn, S.MemoryItem(slug="doc", kind="document", title="doc", body="document"))
        pack = root / "pack"
        pack.mkdir()
        (pack / "SKILL.md").write_text("---\nname: demo\ndescription: quartz\n---\nquartz procedure")
        import_pack(conn, str(pack), pack_name="local")
        notes = root / "notes"
        notes.mkdir()
        (notes / "note.md").write_text("# quartz\nquartz procedure")
        E.export_all(conn, root / "dump")
        if site == "migration_seal":
            conn.execute("DELETE FROM meta WHERE key='owner_seal_backfill_done'")
        if site == "migration_history":
            conn.execute("DROP TABLE memory_history")
            conn.execute("CREATE TABLE memory_history (id INTEGER PRIMARY KEY, slug TEXT, old_title TEXT, old_body TEXT, changed_at INTEGER, changed_by TEXT, reason TEXT, prev_hash TEXT)")
            conn.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
        def blocked():
            try:
                other.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc), str(exc)
            else:
                other.rollback()
                pytest.fail(f"INV-05: {site} read without write lock")
        # upsert has an advisory pre-read; its *decisive* re-read is in tx.
        proxy = PauseConnection(conn, lambda sql: conn.in_transaction and
            (sql.lstrip().upper().startswith("SELECT") or sql.startswith("PRAGMA table_info")), blocked)
        patch.setattr(M, "_shared_conn", lambda: proxy)
        patch.setattr(cli, "_conn", lambda _: proxy)
        item = lambda body="quartz deployment procedure": S.MemoryItem(
            slug="record", title="quartz", body=body, kind="skill")
        actions = {
            "restem": lambda: S.restem_all(proxy), "gc": lambda: S.gc_body_files(proxy),
            "trust": lambda: S.set_trust(proxy, "record", trusted=True),
            "same_text": lambda: S.upsert(proxy, item(), explicit={"topics"}),
            "update": lambda: S.upsert(proxy, item("edited"), reason="edit", explicit=set()),
            "learn": lambda: S.upsert_skill(proxy, item(), explicit=set()),
            "delete": lambda: S.soft_delete(proxy, "record", "delete"),
            "reinforce": lambda: S.reinforce(proxy, "record"),
            "pin": lambda: S.set_pinned(proxy, "record", True),
            "decay": lambda: S.decay_stale(proxy), "sweep": lambda: S.sweep_lifecycle(proxy),
            "archive": lambda: S.set_archived(proxy, "record"),
            "restore": lambda: S.restore_skill(proxy, "record"),
            "recall": lambda: S.recall_skills(proxy, "quartz"),
            "mcp_write": lambda: M._tool_write(dict(slug="record", title="quartz", body="quartz deployment procedure", check_conflicts=False)),
            "mcp_update": lambda: M._tool_update(dict(slug="record", body="edited", reason="edit")),
            "mcp_learn": lambda: M._tool_learn(dict(slug="new", title="quartz", trigger="quartz", steps="run", outcome="success", check_conflicts=False)),
            "cli_write": lambda: CliRunner().invoke(cli.main, ["write", "--slug", "record", "--title", "quartz", "--body", "edited", "--reason", "edit"]),
            "cli_learn": lambda: CliRunner().invoke(cli.main, ["learn", "new", "--title", "quartz", "--trigger", "event", "--steps", "run", "--outcome", "success"]),
            "migrate": lambda: import_dir(proxy, notes),
            "vault": lambda: import_vault(proxy, root / "dump", skip_auto_memories=False),
            "pack": lambda: import_pack(proxy, str(pack), pack_name="local"),
            "remove_pack": lambda: remove_pack(proxy, "local", reason="remove"),
            "migration_seal": lambda: S._migrate_owner_seal(proxy),
            "migration_v10": lambda: S._migrate_v10(proxy),
            "migration_history": lambda: S._migrate(proxy),
        }
        if site == "trust":
            with owner():
                result = actions[site]()
        else:
            result = actions[site]()
        assert proxy.fired, f"test did not reach {site}'s decisive read"
        if hasattr(result, "exit_code"):
            assert result.exit_code == 0, result.output
        if hasattr(result, "failed"):
            assert not result.failed, result.failed


@pytest.mark.parametrize("site", ["kind", "visibility"])
def test_migration_does_not_overwrite_concurrent_edit(site):
    with pair() as (conn, other, _, __):
        put(conn)
        if site == "kind":
            conn.execute("UPDATE memory_items SET kind=' Skill '")
            predicate = lambda sql: "SELECT id, kind" in sql
            update = "UPDATE memory_items SET kind='reference'"
        else:
            conn.execute("UPDATE memory_items SET visibility='broken'")
            predicate = lambda sql: "SELECT id FROM memory_items WHERE LOWER" in sql
            update = "UPDATE memory_items SET visibility='public'"
        proxy = PauseConnection(conn, predicate, lambda: other.execute(update))
        S._migrate(proxy)
        assert proxy.fired, "test did not reach migration read"
        row = conn.execute("SELECT * FROM memory_items").fetchone()
        assert row[site] == {"kind": "reference", "visibility": "public"}[site], \
            "INV-05: migration overwrote newer decision"


@pytest.mark.parametrize("step", [None, "CREATE TRIGGER mem_stem_ai", "UPDATE memory_items SET stemmed"])
@pytest.mark.parametrize("write", ["insert", "tags"])
def test_a_write_during_the_versioned_migration_is_indexed(step, write):
    """INV-05, INV-08 (*sixteenth review*): a second opener arriving at any
    statement of the versioned migration either waits for all of it or has
    its write indexed. The FTS table was published before its triggers, and
    the stem backfill replaced a tag edit's stems with older ones. Alone, too:
    the backfill's update trigger deleted rows the new index never held, which
    SQLite reports as a malformed database."""
    with pair() as (conn, other, _, __):
        put(conn, tags=["kept"])
        put(conn, slug="legacy", body="legacy runbook", tags=["legacytag"])
        # a pre-v5 database: no stem index, no stems
        for ddl in ("DROP TRIGGER mem_stem_ai", "DROP TRIGGER mem_stem_ad",
                    "DROP TRIGGER mem_stem_au", "DROP TABLE mem_fts_stem"):
            conn.execute(ddl)
        conn.execute("UPDATE memory_items SET stemmed=''")
        conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        slug = "record" if write == "tags" else "arrived"
        def second_opener():
            S.init_schema(other)
            S.upsert(other, S.MemoryItem(slug=slug, kind="skill", title="quartz",
                     body="quartz deployment procedure", tags=["zephyr"]), explicit={"tags"})
        outcome = []
        def trace(sql):
            if step and not outcome and sql.lstrip().startswith(step):
                try:
                    second_opener()
                    outcome.append("wrote")
                except sqlite3.OperationalError as exc:
                    outcome.append(str(exc))
        conn.set_trace_callback(trace)
        S.init_schema(conn)
        conn.set_trace_callback(None)
        assert outcome or not step, f"test did not reach {step}"
        assert outcome[:1] in ([], ["wrote"], ["database is locked"]), outcome
        if outcome != ["wrote"]:
            second_opener()   # it waited for the lock, then wrote
        assert [r["slug"] for r in S.search(conn, "legacytag")] == ["legacy"], \
            "INV-05: a row from before the index is missing from it"
        assert [r["slug"] for r in S.search(conn, "zephyr")] == [slug], \
            "INV-05: an acknowledged write is missing from the index"
        S.upsert(conn, S.MemoryItem(slug=slug, kind="skill", title="quartz", body="edited"),
                 reason="edit", explicit=set())
        assert [r["slug"] for r in S.search(conn, "edited")] == [slug], "INV-08: the edit was not indexed"


@pytest.mark.parametrize("same_text", [False, True])
def test_revive_rechecks_seal_after_lock(same_text):
    with pair() as (conn, other, _, patch):
        put(conn)
        S.soft_delete(conn, "record", "removed")
        real_tx, fired = S.tx, []
        @contextmanager
        def interleave(c):
            if c is conn and not fired:
                fired.append(True)
                with owner(), real_tx(other):
                    S.upsert(other, S.MemoryItem(slug="record", kind="skill", title="quartz",
                        body="quartz deployment procedure"), revive=True, reason="owner restore")
                    S.set_trust(other, "record", trusted=True)
                    S.soft_delete(other, "record", "owner removes sealed record")
            with real_tx(c):
                yield c
        patch.setattr(S, "tx", interleave)
        try:
            S.upsert(conn, S.MemoryItem(slug="record", kind="skill", title="quartz",
                body="quartz deployment procedure" if same_text else "changed"),
                revive=True, reason="restore", explicit=set())
        except (S.SealedRecord, S.MemoryConflict):
            pass
        assert fired, "test did not reach lock"
        assert S.get(conn, "record") is None, "INV-03/05: revived a concurrently sealed tombstone"


@pytest.mark.parametrize("site", ["set_embedding", "reindex"])
def test_embedding_compare_and_set(site):
    with pair() as (conn, other, _, patch):
        row = put(conn)
        patch.setattr(embed, "available", lambda: True)
        patch.setattr(embed, "semantic_enabled", lambda: True)
        def compute(_):
            # Also proves INV-09: computation does not hold the write lock.
            with S.tx(other):
                other.execute("UPDATE memory_items SET body='new', content_hash='new', embedding=NULL")
            return b"old vector"
        patch.setattr(embed, "embed_text", compute)
        if site == "set_embedding":
            S._set_embedding(conn, row.id, row.title, row.body, row.content_hash)
        else:
            S.reindex_embeddings(conn)
        assert conn.execute("SELECT embedding FROM memory_items").fetchone()[0] is None, "INV-09: stale vector"


@pytest.mark.parametrize("site", ["set_embedding", "reindex", "upsert", "search", "recall"])
def test_no_embedding_under_a_callers_transaction(site):
    """INV-09: inside a caller's transaction the model waits for its commit;
    a query is not embedded there at all (the read ranks by BM25 alone)."""
    with pair() as (conn, other, _, patch):
        row = put(conn)
        patch.setattr(embed, "available", lambda: True)
        patch.setattr(embed, "semantic_enabled", lambda: True)
        held = []

        def compute(_):
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
                held.append(False)
            except sqlite3.OperationalError:
                held.append(True)
            return b"vector"
        patch.setattr(embed, "embed_text", compute)
        conn.execute("UPDATE memory_items SET embedding = NULL")
        with S.tx(conn):
            if site == "set_embedding":
                S._set_embedding(conn, row.id, row.title, row.body, row.content_hash)
            elif site == "reindex":
                S.reindex_embeddings(conn)
            elif site == "search":
                assert S.search(conn, "quartz"), "INV-09: a read in a transaction lost its BM25 hits"
            elif site == "recall":
                assert S.recall_skills(conn, "quartz", auto_reinforce=False), \
                    "INV-09: a read in a transaction lost its BM25 hits"
            else:
                S.upsert(conn, S.MemoryItem(slug="record", kind="skill", title="quartz", body="new"),
                         reason="edit", explicit=set())
        if site in ("search", "recall"):
            held.append(False)   # the rows keep their vectors: nothing is deferred
        assert held and not any(held), "INV-09: the model ran under the write lock"


def test_trust_display_hash_compare_and_set():
    with pair() as (conn, other, _, __):
        row = put(conn)
        S.upsert(other, S.MemoryItem(slug="record", title="new", body="new"), reason="edit", explicit=set())
        with owner(), pytest.raises(S.MemoryConflict):
            S.set_trust(conn, "record", trusted=True, expect_hash=row.content_hash)
        assert S.get(conn, "record").trusted_at is None, "INV-01/05: approved changed text"


def test_conflicts_refetch_only_live_rows():
    with pair() as (conn, other, _, __):
        put(conn, body="quartz deployment procedure extra distinct words")
        def delete():
            S.soft_delete(other, "record", "concurrent removal")
        proxy = PauseConnection(conn, lambda sql: "JOIN memory_items m ON" in sql, delete)
        result = S.find_conflicts(proxy, "quartz", "quartz deployment procedure extra distinct words")
        assert proxy.fired, "test did not reach conflict scan"
        assert not result, "INV-04: conflicts quote a deleted row"


@pytest.mark.parametrize("site", ["write", "update", "learn", "recall", "reinforce"])
def test_http_decision_holds_write_lock(site):
    from skillmem import server as W
    with pair() as (conn, other, root, patch):
        put(conn, visibility="public")
        def blocked():
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        proxy = PauseConnection(conn, lambda sql: conn.in_transaction and sql.startswith("SELECT"), blocked)
        patch.setattr(S, "connect", lambda _: proxy)
        tokens = root / "tokens.yaml"
        tokens.write_text("boss:\n  token: test\n  scope: master\n")
        app = W.build_app(W.TokenStore(tokens), db_path=root / "memory.db")
        endpoint = next(r.endpoint for r in app.routes if r.path == {
            "update": "/update/{slug:path}", "reinforce": "/reinforce/{slug:path}"}.get(site, "/" + site))
        agent = W.AgentIdentity("boss", "test", scope="master")
        if site == "write":
            endpoint(W.WriteRequest(slug="new", title="new", body="new", check_conflicts=False), agent)
        elif site == "update":
            endpoint("record", W.UpdateRequest(body="new", reason="edit"), agent)
        elif site == "learn":
            endpoint(W.LearnRequest(slug="new", title="new", trigger="event", steps="run", outcome="success", check_conflicts=False), agent)
        elif site == "recall":
            endpoint(W.RecallRequest(query="quartz"), agent)
        else:
            endpoint("record", "self_report", agent)
        assert proxy.fired, f"test did not exercise HTTP {site}"


@pytest.mark.parametrize("surface", ["cli", "mcp", "http"])
def test_get_history_uses_same_snapshot(surface):
    from skillmem import server as W
    with pair() as (conn, other, root, patch):
        put(conn)
        def change():
            S.upsert(other, S.MemoryItem(slug="record", title="new", body="new"), reason="racing edit", explicit=set())
        proxy = PauseConnection(conn, lambda sql: "SELECT * FROM memory_items WHERE slug" in sql, change)
        patch.setattr(M, "_shared_conn", lambda: proxy)
        patch.setattr(cli, "_conn", lambda _: proxy)
        if surface == "cli":
            result = CliRunner().invoke(cli.main, ["cat", "record", "--history"])
            assert result.exit_code == 0, result.output
            output = result.output
        elif surface == "mcp":
            output = M._tool_get(dict(slug="record", include_history=True))[0].text
        else:
            patch.setattr(S, "connect", lambda _: proxy)
            tokens = root / "tokens.yaml"
            tokens.write_text("boss:\n  token: test\n  scope: master\n")
            app = W.build_app(W.TokenStore(tokens), db_path=root / "memory.db")
            endpoint = next(r.endpoint for r in app.routes if r.path == "/get/{slug:path}")
            output = str(endpoint("record", True, W.AgentIdentity("boss", "test", scope="master")))
        assert proxy.fired, "test did not reach by-slug read"
        assert "racing edit" not in output, "INV-04/05: old text with newer history"


@pytest.mark.parametrize("reader", ["search", "recall", "list"])
@pytest.mark.parametrize("mutation", ["archive", "delete", "private", "unapprove", "rewrite", "kind"])
def test_ranked_read_rechecks_fetched_state(reader, mutation):
    with pair() as (conn, other, _, patch):
        put(conn, visibility="public")
        with owner():
            S.set_trust(conn, "record", trusted=True)
        changes = {
            "archive": "lifecycle='archived'", "delete": "deleted_at=1",
            "private": "visibility='private'", "unapprove": "trusted_at=NULL",
            "rewrite": "body='newzephyr'", "kind": "kind='note'",
        }
        original = S._fetch_live
        fired = []
        def fetch(c, ids, *recheck, **filters):
            if not fired:
                fired.append(True)
                if mutation == "rewrite":
                    # A valid owner edit: the new text has its own hash and
                    # approval, so this case checks fresh text, not revocation.
                    # The owner approves by writing at a terminal through the
                    # CLI surface, not by a trusted_at on the item (INV-01/02).
                    with owner():
                        item = S.get(other, "record")
                        item.body = "newzephyr"
                        S.upsert(other, item, surface="cli", reason="owner edit", explicit=set())
                else:
                    other.execute("UPDATE memory_items SET " + changes[mutation])
            return original(c, ids, *recheck, **filters)
        patch.setattr(S, "_fetch_live", fetch)
        visible = lambda row: row["visibility"] == "public" and row.get("trusted_at") is not None
        # list's visibility payload omits trusted_at; test its visibility contract only.
        if reader == "list":
            visible = lambda row: row["visibility"] == "public"
            rows = S.list_items(conn, kind="skill", visible=visible)
        elif reader == "search":
            rows = S.search(conn, "quartz", visible=visible, exclude_kinds=("note",))
        else:
            rows = S.recall_skills(conn, "quartz", visible=visible, auto_reinforce=False)
        assert fired, "test did not reach final fetch"
        for result in rows:
            row = result if isinstance(result, dict) else result.to_dict()
            assert not row.get("deleted_at") and row.get("lifecycle") != "archived", "INV-04: not live"
            assert visible(row), "INV-04: fetched row fails ranking permission"
            # every reader here asks for skills: the kind is a ranking filter too
            assert row["kind"] == "skill", "INV-04: fetched row fails the ranking's kind filter"
            assert row["body"] == S.get(conn, row["slug"]).body, "INV-04: stale text"


@pytest.mark.parametrize("mutation", ["archive", "delete", "private", "unapprove", "kind"])
def test_recall_reinforces_only_what_the_caller_still_sees(mutation):
    """INV-05: the bump recall makes is a write, decided under its lock on the
    caller's predicate too, not only on liveness and kind."""
    with pair() as (conn, other, _, patch):
        put(conn, visibility="public")
        with owner():
            S.set_trust(conn, "record", trusted=True)
        changes = {
            "archive": "lifecycle='archived'", "delete": "deleted_at=1",
            "private": "visibility='private'", "unapprove": "trusted_at=NULL", "kind": "kind='note'",
        }
        original = S._fetch_live
        def fetch(*args, **kwargs):
            rows = original(*args, **kwargs)
            other.execute("UPDATE memory_items SET " + changes[mutation])
            return rows
        patch.setattr(S, "_fetch_live", fetch)
        rows = S.recall_skills(conn, "quartz", auto_reinforce=True, visible=lambda row:
                               row["visibility"] == "public" and row.get("trusted_at") is not None)
        assert [r["slug"] for r in rows] == ["record"], "test did not reach the bump"
        count = conn.execute("SELECT access_count FROM memory_items WHERE slug = 'record'").fetchone()[0]
        assert count == 0, "INV-05: reinforced a record the caller can no longer see"


def test_export_refetch_does_not_mix_ownership_snapshots():
    with pair() as (conn, other, root, __):
        put(conn)
        E.export_all(conn, root / "dump")
        # Rows loaded before the second ownership read still have to be live
        # at the statement returning their text (INV-04), not forever after.
        proxy = PauseConnection(conn, lambda sql: "SELECT * FROM memory_items ORDER BY" in sql,
                                lambda: S.soft_delete(other, "record", "after snapshot"))
        assert E.export_all(proxy, root / "dump") == 1
        assert proxy.fired
        assert "quartz deployment procedure" in (root / "dump" / "skill" / "record.md").read_text(), "INV-04/12: incoherent export"


def test_recap_publish_rechecks_basis():
    with pair() as (_, __, root, ___):
        note = root / "session.md"
        note.write_text("transcript_bytes: 10\nold")
        basis = H._note_basis(note)  # slow recap reads first
        assert H._publish_note(note, 20, "transcript_bytes: 20\nnew") == ""
        assert H._publish_note(note, basis, "transcript_bytes: 10\nstale").startswith("skip:stale"), "INV-05: recap overwrote newer basis"
        assert note.read_text().endswith("new"), "INV-05: recap lost newer text"


def test_the_recap_that_wins_the_file_wins_the_record():
    """INV-05 (*seventeenth review*): the record is indexed under the lock that
    decides the file. Indexed after it, a Stop recap that read its note, then
    lost the file to SessionEnd's longer recap, wrote the shorter one over the
    record last; the final text survived only as a history row."""
    import json
    import threading
    from types import SimpleNamespace
    from skillmem import migrate
    with database() as (conn, root, patch):
        transcript = root / "project" / "session.jsonl"
        transcript.parent.mkdir()

        def say(lines):
            with transcript.open("a", encoding="utf-8") as fh:
                for i in range(lines):
                    fh.write(json.dumps({"type": "user", "message": {"content": f"turn {i} said here"}}) + "\n")

        say(25)
        patch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
        patch.setattr(H.subprocess, "run", lambda *a, input=b"", **kw: SimpleNamespace(
            stdout=f"## DONE\n- built from {len(input)} bytes\n{'x' * 120}".encode(),
            returncode=0, stderr=b""))
        payload = {"session_id": "abcdef12-3456-7890", "transcript_path": str(transcript)}
        session_end = threading.Thread(target=H.run_recap, args=({**payload, "hook_event_name": "SessionEnd"},))
        parse = migrate.parse_file

        def then_session_end(path):
            parsed = parse(path)                  # the Stop recap has read its note
            if not session_end.is_alive() and session_end.ident is None:
                say(40)
                session_end.start()               # SessionEnd recaps the longer transcript
                session_end.join(0.5)
            return parsed

        patch.setattr(migrate, "parse_file", then_session_end)
        H.run_recap(payload)                      # Stop
        session_end.join()
        note, = (transcript.parent / "memory").glob("session-*.md")
        body = note.read_text(encoding="utf-8").split("---\n", 2)[2].strip()
        assert H._note_basis(note) == transcript.stat().st_size, "INV-05: the final recap was lost"
        assert S.get(conn, note.stem).body == body, "INV-05: the record holds another recap than the file"


def test_seen_reset_does_not_lose_concurrent_append():
    # The reset reads nothing: it is the boundary between two prompt cycles,
    # and an entry written before it belongs to the cycle it ends. What it
    # must not do is lose an entry written after it, e.g. by rewriting the
    # file from a copy (INV-05 §3, seen-ledger row).
    with pair() as (_, __, root, patch):
        ledger = H._dedup_file("session")
        ledger.write_text("previous-cycle\n")
        original = type(ledger).write_text
        fired = []
        def reset(path, text, *args, **kwargs):
            result = original(path, text, *args, **kwargs)
            if path == ledger and text == "" and not fired:
                fired.append(True)
                H._append_seen("session", ["current-cycle"])
            return result
        patch.setattr(type(ledger), "write_text", reset)
        result = CliRunner().invoke(cli.main, ["hook", "auto-recall"], input='{"session_id":"session","prompt":"short"}')
        assert result.exit_code == 0 and fired
        assert H._read_seen("session") == {"current-cycle"}, "INV-05: reset lost concurrent seen entry"


@pytest.mark.parametrize("patcher", ["settings", "claude", "mcp", "opencode", "deny", "codex"])
def test_config_patch_preserves_concurrent_editor(patcher):
    import json
    with pair() as (_, __, root, patch):
        path = root / ("config.toml" if patcher == "codex" else "config.json")
        path.write_text("" if patcher == "codex" else "{}")
        read_method = "read_bytes" if patcher == "codex" else "read_text"
        original = getattr(type(path), read_method)
        fired = []
        def read(p, *args, **kwargs):
            result = original(p, *args, **kwargs)
            if p == path and not fired:
                fired.append(True)
                path.write_text("editor_setting = true\n" if patcher == "codex" else '{"editor_setting": true}')
            return result
        patch.setattr(type(path), read_method, read)
        if patcher == "settings":
            cli._patch_settings_hook(path, root / "skillmem", event="SessionStart", args=["inject"])
        elif patcher == "claude":
            cli._patch_claude_json(path, root / "skillmem")
        elif patcher == "mcp":
            cli._patch_mcp_servers_json(path, root / "skillmem", agent="property")
        elif patcher == "opencode":
            cli._patch_opencode_json(path, root / "skillmem")
        elif patcher == "deny":
            cli._patch_settings_deny(path, "Read(secret)")
        else:
            cli._patch_codex_config(path, root / "skillmem")
        assert fired
        import tomllib
        data = tomllib.loads(path.read_text()) if patcher == "codex" else json.loads(path.read_text())
        assert data.get("editor_setting") is True, "INV-05: config editor update lost"


def test_recap_slot_is_never_taken_from_a_live_holder():
    # A slot's holder is whoever holds its OS lock, not whoever wrote the file
    # last: an ancient mtime on a held slot must not let a second process in,
    # and a holder that exits frees the slot without anyone deleting a file.
    import os
    with pair() as (_, __, root, patch):
        patch.setattr(H, "RECAP_MAX_PARALLEL", 1)
        holder = H._acquire_recap_slot()
        assert holder is not None
        slot = root / "state" / "recap-slots" / "slot0.lock"
        os.utime(slot, (1, 1))
        assert H._acquire_recap_slot() is None, "INV-05: reclaimed another process's live slot"
        assert slot.exists()
        holder.close()
        taken = H._acquire_recap_slot()
        assert taken is not None, "a released slot stayed busy"
        taken.close()


@pytest.mark.parametrize("create_only", [False, True])
def test_upsert_rechecks_new_row_after_pre_read(create_only):
    with pair() as (conn, other, _, patch):
        real_tx, fired = S.tx, []
        @contextmanager
        def interleave(c):
            if c is conn and not fired:
                fired.append(True)
                put(other, body="other writer")
            with real_tx(c):
                yield c
        patch.setattr(S, "tx", interleave)
        try:
            S.upsert(conn, S.MemoryItem(slug="record", title="quartz", body="request"), create_only=create_only)
        except S.MemoryConflict:
            pass
        assert fired
        assert S.get(conn, "record").body == "other writer", "INV-05/08: stale absence permitted overwrite"


def test_advisory_conflict_scan_does_not_authorize_overwrite():
    with pair() as (conn, other, _, patch):
        real_scan = S.find_conflicts
        fired = []
        def scan(c, *args, **kwargs):
            result = real_scan(c, *args, **kwargs)
            if c is conn and not fired:
                fired.append(True)
                put(other, slug="other", body="quartz deployment procedure extra distinct words")
            return result
        patch.setattr(S, "find_conflicts", scan)
        S.upsert(conn, S.MemoryItem(slug="record", title="quartz",
                 body="quartz deployment procedure extra distinct words"), check_conflicts=True)
        assert fired
        # The spec deliberately permits duplicates across this advisory window.
        assert {r.slug for r in S.list_items(conn)} == {"record", "other"}, "INV-08: advisory scan overwrote another slug"


def test_recap_session_lock_closes_debounce_window():
    with pair() as (_, __, root, patch):
        transcript = root / "transcript.jsonl"
        transcript.write_text("{}\n" * 20)
        data = {"session_id": "session", "transcript_path": str(transcript)}
        patch.delenv("SKILLMEM_RECAP_FORCE", raising=False)
        patch.setattr(H.shutil, "which", lambda _: "/not-executed/claude")
        patch.setattr(H, "_filter_transcript", lambda _: "a sufficiently long transcript")
        original = H._recap_stamp
        lock = original("session").with_suffix(".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        # First worker passed debounce but has not yet stamped its attempt.
        held = H._try_lock(lock)
        assert held is not None
        invoked = []
        def no_process(*args, **kwargs):
            invoked.append(True)
            raise AssertionError("a competing recap must not launch a process")
        patch.setattr(H.subprocess, "run", no_process)
        H.run_recap(data)
        assert not invoked and H._try_lock(lock) is None, "INV-05: concurrent same-session recap escaped lock"
        held.close()


def test_a_direct_file_import_decides_existence_under_the_write_lock():
    """INV-05: `migrate.import_file`'s existence read picks the history reason
    and the status reported. The recap hook calls it on a connection holding
    no lock, so a record created between the read and the write was reported
    inserted (*nineteenth review*)."""
    from skillmem.migrate import import_file
    with pair() as (conn, other, root, _):
        note = root / "session-x.md"
        note.write_text("---\nname: session-x\ndescription: recap\n---\n\nrecap text\n",
                        encoding="utf-8")
        def blocked():
            try:
                other.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc), str(exc)
            else:
                other.rollback()
                pytest.fail("INV-05: import_file read existence without the write lock")
        proxy = PauseConnection(conn, lambda sql: "SELECT id FROM memory_items WHERE slug" in sql,
                                blocked)
        import_file(proxy, note)
        assert proxy.fired, "test did not reach the existence read"


def test_export_reads_a_body_the_gc_cannot_remove_underneath():
    """INV-05: the export read a row, then its body file, with no lock. An
    edit and a GC (`decay`, HTTP `/decay`) in between removed the file, and
    the excerpt, marked truncated, replaced the last good dump — a dump no
    restore accepts (*twentieth review*)."""
    import os
    import time
    with pair() as (conn, other, root, _):
        def doc(c, text):
            S.upsert(c, S.MemoryItem(slug="runbook", kind="document", title="Runbook", body=text),
                     reason="edit")
        doc(conn, "v1 " + "a" * 9000)
        E.export_all(conn, root / "dump")
        doc(conn, "v2 " + "b" * 9000)
        def edit_and_collect():
            doc(other, "v3 " + "c" * 9000)
            for path in S.docs_dir().glob("*.md"):   # past the GC's grace
                os.utime(path, (time.time() - 3600, time.time() - 3600))
            assert S.gc_body_files(other) >= 1
        proxy = PauseConnection(conn, lambda sql: "SELECT * FROM memory_items ORDER BY" in sql,
                                edit_and_collect)
        assert E.export_all(proxy, root / "dump") == 1
        assert proxy.fired
        dump = (root / "dump" / "document" / "runbook.md").read_text(encoding="utf-8")
        assert "truncated" not in dump and "v3 " + "c" * 9000 in dump, "INV-05: excerpt replaced the dump"
