"""INV-05/06/12/14/16: the r01 review of 005540e."""
import hashlib
import http.server
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import cli, export as E, schedule, storage as S
from skillmem.migrate import iter_notes
from skillmem.vault import SUPPORTED_TEXT, import_vault
from .support import PROPERTY, database, owner


# INV-06/08: every exported file is one the restore reads (`..` was `...md`,
# which has no `.md` suffix, and its record was left out with exit 0)
@PROPERTY
@given(slugs=st.sets(st.text(".-_/aA", min_size=1, max_size=6), min_size=1, max_size=5))
def test_every_slug_comes_back_from_its_dump(slugs):
    with database() as (conn, root, _):
        for slug in slugs:
            S.upsert(conn, S.MemoryItem(slug=slug, kind="note", title=slug, body="text"))
        E.export_all(conn, root / "dump")
        assert all(refused is None for _, refused in iter_notes(root / "dump", SUPPORTED_TEXT))
        fresh = S.connect(root / "fresh.db")
        S.init_schema(fresh)
        try:
            with owner():
                report = import_vault(fresh, root / "dump", skip_auto_memories=False)
            assert not report.failed and report.inserted == len(slugs)
            assert {slug: S.get(fresh, slug) and S.get(fresh, slug).body for slug in slugs} \
                == dict.fromkeys(slugs, "text")
        finally:
            fresh.close()


# INV-12: a pre-0.11 body file name carries no namespace and may be a copy's
# original's; purging the copy deleted it
@pytest.mark.parametrize("purged", ["copy", "original"])
def test_purging_one_database_keeps_a_legacy_body_file_the_other_serves(purged):
    with database() as (conn, root, patch):
        text = "legacy runbook " + "x" * 9000
        S.upsert(conn, S.MemoryItem(slug="runbook", kind="document", title="Runbook", body=text))
        legacy = f"runbook__{hashlib.sha256(b'runbook').hexdigest()[:8]}.md"
        (S.docs_dir() / legacy).write_bytes(text.encode())
        conn.execute("UPDATE memory_items SET body_path = ? WHERE slug = 'runbook'", (legacy,))
        conn.close()
        shutil.copy(root / "memory.db", root / "copy.db")
        for name in ("memory.db", "copy.db"):
            other = S.connect(root / name)
            S.init_schema(other)
            other.close()
        patch.setattr(schedule, "_backend", lambda: (None, lambda: [], None))
        with owner():
            result = CliRunner().invoke(cli.main, [
                "--db", str(root / ("copy.db" if purged == "copy" else "memory.db")),
                "uninstall", "--no-claude-code", "--no-codex", "--no-editors", "--purge-db"])
        assert result.exit_code == 0, result.output
        kept = S.connect(root / ("memory.db" if purged == "copy" else "copy.db"))
        try:
            assert S.load_body(S.get(kept, "runbook")) == text
        finally:
            kept.close()


# INV-05: an excerpt's row is read again under the write lock; a busy lock
# fails the export instead of writing the stale row's excerpt over the backup
@PROPERTY
@given(busy=st.booleans())
def test_a_busy_write_lock_never_puts_an_excerpt_over_the_backup(busy):
    with database() as (conn, root, patch):
        patch.setenv("SKILLMEM_BUSY_TIMEOUT_MS", "50")
        other = S.connect(root / "memory.db")

        def doc(c, text):
            S.upsert(c, S.MemoryItem(slug="runbook", kind="document", title="Runbook", body=text),
                     reason="edit")

        doc(conn, "v1 " + "a" * 9000)
        dump = root / "dump"
        E.export_all(conn, dump)
        before = (dump / "document" / "runbook.md").read_bytes()
        doc(conn, "v2 " + "b" * 9000)
        real = E._iter_all

        def plan_then_race(c):
            items = list(real(c))
            doc(other, "v3 " + "c" * 9000)      # an edit, and GC of v2's file
            for p in S.docs_dir().glob("*.md"):
                os.utime(p, (time.time() - 3600,) * 2)
            assert S.gc_body_files(other)
            if busy:
                other.execute("BEGIN IMMEDIATE")
            return items

        patch.setattr(E, "_iter_all", plan_then_race)
        try:
            if busy:
                with pytest.raises(sqlite3.OperationalError):
                    E.export_all(conn, dump)
                assert (dump / "document" / "runbook.md").read_bytes() == before
            else:
                E.export_all(conn, dump)
                assert ("v3 " + "c" * 9000).encode() in (dump / "document" / "runbook.md").read_bytes()
        finally:
            if busy:
                other.rollback()
            other.close()


# INV-06/14: a dump's recorded state is a value or the file fails; a string
# `owner_seal` sealed, a misspelt lifecycle came back active, an unknown
# origin became the importer's default
@PROPERTY
@given(key=st.sampled_from(["owner_seal", "origin", "lifecycle"]),
       value=st.sampled_from(["'false'", '"no"', "1", "null", "bogus", "Archived",
                              "archivedd", "OWNER", "[agent]"]))
def test_a_dump_with_an_invalid_state_fails_the_file(key, value):
    with database() as (conn, root, _):
        fields = {"owner_seal": "false", "origin": "agent", "lifecycle": "active", key: value}
        (root / "dump" / "note").mkdir(parents=True)
        (root / "dump" / "note" / "r.md").write_text(
            "---\nname: r\ndescription: t\nexported_at: 1\nmetadata:\n  node_type: memory\n  type: note\n"
            f"  origin: {fields['origin']}\n  owner_seal: {fields['owner_seal']}\n"
            f"created_at: 100\nupdated_at: 100\nstrength: 1.0\npinned: false\n"
            f"lifecycle: {fields['lifecycle']}\n---\n\nbody\n", encoding="utf-8")
        with owner():
            report = import_vault(conn, root / "dump", skip_auto_memories=False)
        assert report.failed and report.inserted == 0 and S.get(conn, "r") is None


# INV-12: each database's weekly export has its own directory; a second
# database's job was refused every week by the first one's dump
@PROPERTY
@given(names=st.lists(st.sampled_from([None, "work.db", "other.db", "nested/x.db"]),
                      min_size=2, max_size=3, unique=True))
def test_every_databases_scheduled_export_is_written(names):
    with database() as (_, root, patch):
        patch.setattr(S, "user_data_dir", lambda *a, **k: str(root / "home"))
        for i, name in enumerate(names):
            if name is None:
                patch.delenv("SKILLMEM_DB")
            else:
                patch.setenv("SKILLMEM_DB", str(root / "home" / name))
            conn = S.connect(S.default_db_path())
            S.init_schema(conn)
            S.upsert(conn, S.MemoryItem(slug="deploy", kind="note", title="t", body=f"db {i}"))
            conn.close()
            argv = schedule._jobs()["export"][1:]
            result = CliRunner().invoke(cli.main, argv)
            assert result.exit_code == 0, result.output
            dump = Path(argv[-1]) / "note" / "deploy.md"
            assert f"db {i}" in dump.read_text(encoding="utf-8")


# INV-16: the stored token goes to the origin it was sent to, never to the
# host a redirect names
@pytest.mark.parametrize("target", ["same", "other-port", "other-host"])
def test_a_redirect_carries_the_token_only_to_the_same_origin(target):
    seen = {}

    def server(handle):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                handle(self)

            def log_message(self, *a):
                pass
        s = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        return s

    def asset(h):
        seen[h.path] = h.headers.get("Authorization")
        h.send_response(200)
        h.end_headers()
        h.wfile.write(b"asset")

    def redirect(h):
        if h.path == "/asset":
            return asset(h)
        seen[h.path] = h.headers.get("Authorization")
        h.send_response(302)
        h.send_header("Location", {"same": f"http://127.0.0.1:{a.server_port}/asset",
                                   "other-port": f"http://127.0.0.1:{b.server_port}/asset",
                                   "other-host": f"http://localhost:{b.server_port}/asset"}[target])
        h.end_headers()

    b = server(asset)
    a = server(redirect)
    try:
        assert cli._gh_get(f"http://127.0.0.1:{a.server_port}/release", "ghp_SECRET",
                           accept="application/octet-stream") == b"asset"
    finally:
        a.shutdown()
        b.shutdown()
    assert seen["/release"] == "Bearer ghp_SECRET"
    assert seen["/asset"] == ("Bearer ghp_SECRET" if target == "same" else None)
