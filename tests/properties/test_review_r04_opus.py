"""INV-01/03/11/12: the r04 review of e6654e5."""
import json
import os
import tempfile

from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import cli, hooks as H, schedule, storage as S
from skillmem.export import _record_in, export_all
from .support import PROPERTY, database, owner


# INV-12: a file that no skillmem initialised (purge's own connect created it)
# was taken for the database from before 0.12.0 that was at its path, and
# `uninstall --purge-db` deleted a moved legacy database's body files. The r04
# fix covered the default path; r11 found any `--db` path still exposed
@PROPERTY
@given(at_default=st.sampled_from(["nothing", "empty file"]),
       where=st.sampled_from(["default", "--db path"]))
def test_purging_nothing_keeps_a_moved_legacy_databases_body_files(at_default, where):
    with database() as (conn, root, patch):
        conn.close()
        default = (S.default_data_dir() / "memory.db" if where == "default"
                   else root / "work" / "work.db")
        default.parent.mkdir(parents=True, exist_ok=True)
        text = "important text. " * 1000
        c = S.connect(default); S.init_schema(c)
        S.upsert(c, S.MemoryItem(slug="big", kind="document", title="Big doc", body=text))
        c.execute("DELETE FROM meta WHERE key IN ('db_id', 'db_file')")
        c.close()
        c = S.connect(default); S.init_schema(c)    # reopened: a default database from before 0.12.0
        assert S._db_namespace(c) == ("" if where == "default"
                                      else S._identity(str(default.resolve()), ""))
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()
        moved = root / "archive" / "old.db"
        moved.parent.mkdir()
        os.rename(default, moved)
        for suffix in ("-wal", "-shm"):
            default.with_name(default.name + suffix).unlink(missing_ok=True)
        if at_default == "empty file":
            default.touch()
        patch.setattr(schedule, "_backend", lambda: (None, lambda: [], None))
        with owner():
            result = CliRunner().invoke(cli.main, [
                "--db", str(default), "uninstall", "--purge-db",
                "--no-claude-code", "--no-codex", "--no-editors"])
        assert result.exit_code == 0, result.output
        c = S.connect(moved)
        try:
            S.init_schema(c)
            assert S.load_body(S.get(c, "big")) == text
        finally:
            c.close()


# INV-12: the ownership check covered planned dump files only, and an
# attachment was published over the file another database's export lists
@PROPERTY
@given(clash=st.sampled_from(["its attachment", "its dump"]),
       name=st.sampled_from(["pic.md", "PIC.md"]), same_bytes=st.booleans())
def test_an_export_never_overwrites_an_attachment_file_another_database_lists(
        clash, name, same_bytes):
    with database() as (conn, root, patch):
        out = root / "vault"
        (S.default_data_dir() / "assets").mkdir(parents=True)
        (S.default_data_dir() / "assets" / "pic.md").write_bytes(b"DB1")
        if clash == "its attachment":
            S.upsert(conn, S.MemoryItem(slug="p1", kind="note", title="t", body="b",
                                       attachments=["assets/pic.md"]))
        else:
            S.upsert(conn, S.MemoryItem(slug="pic", kind="assets", title="t", body="b"))
        export_all(conn, out)
        first = {p: p.read_bytes() for p in out.rglob("*") if p.is_file() and p.suffix == ".md"}
        patch.setenv("SKILLMEM_HOME", str(root / "home2"))
        other = S.connect(root / "two.db")
        try:
            S.init_schema(other)
            (S.default_data_dir() / "assets").mkdir(parents=True)
            mine = b"DB1" if same_bytes and clash == "its attachment" else b"DB2"
            (S.default_data_dir() / "assets" / name).write_bytes(mine)
            S.upsert(other, S.MemoryItem(slug="p2", kind="note", title="t2", body="b2",
                                        attachments=[f"assets/{name}"]))
            try:
                export_all(other, out)
                refused = False
            except ValueError:
                refused = True
        finally:
            other.close()
        for path, data in first.items():
            assert path.read_bytes() == data, f"INV-12: {path.name} was overwritten"
        assert refused == (mine != b"DB1"), "identical bytes share one file"
        patch.setenv("SKILLMEM_HOME", str(root / "home"))
        export_all(conn, out)     # the first database still exports


# INV-11: the names an export allocated for dumps left out its attachments, so
# a record of kind `assets` dumped over the attachment `assets/X.md` (APFS/NTFS)
@PROPERTY
@given(name=st.sampled_from(["x.md", "X.md"]), kind=st.sampled_from(["assets", "Assets"]))
def test_a_dump_never_takes_an_attachments_name(name, kind):
    with database() as (conn, root, patch):
        (S.default_data_dir() / "assets").mkdir(parents=True)
        (S.default_data_dir() / "assets" / name).write_bytes(b"ATTACHMENT BYTES\n")
        S.upsert(conn, S.MemoryItem(slug="x", kind=kind, title="record x", body="record body"))
        S.upsert(conn, S.MemoryItem(slug="n", kind="note", title="note", body="b",
                                   attachments=[f"assets/{name}"]))
        out = root / "vault"
        for _ in range(2):
            assert export_all(conn, out) == 2
            assert (out / "assets" / name).read_bytes() == b"ATTACHMENT BYTES\n"
            held = {_record_in(p) for p in out.rglob("*.md")}
            assert {("x", S.get(conn, "x").created_at), ("n", S.get(conn, "n").created_at)} <= held


# INV-01/03: opening a database repaired a stored kind (`Feedback`) with no
# owner present and kept its approval, and an approved note became an
# approved, injected rule: approval is bound to the kind
@PROPERTY
@given(kind=st.sampled_from(["Feedback", "FEEDBACK", " rule", "User  x", "how/to", "Note"]),
       sealed=st.booleans())
def test_opening_never_keeps_approval_on_a_kind_it_changes(kind, sealed):
    with database() as (conn, root, patch):
        with owner():
            S.upsert(conn, S.MemoryItem(slug="r", kind="note", title="Use tabs",
                                       body="always use tabs", origin="owner"), surface="cli")
        conn.execute("UPDATE memory_items SET kind = ?, owner_seal = ?", (kind, int(sealed)))
        conn.close()
        c = S.connect(root / "memory.db")
        try:
            S.init_schema(c)
            row = c.execute("SELECT kind, trusted_at FROM memory_items").fetchone()
            assert row["kind"] == kind or row["trusted_at"] is None
        finally:
            c.close()


# INV-03: with the state dir unusable, the seen ledger fell back to the shared
# temp dir, where anyone can pre-create it and hide an approved rule
@PROPERTY
@given(session=st.sampled_from(["S1", "abc-def", ""]))
def test_the_seen_ledger_is_never_outside_the_state_dir(session):
    with database() as (conn, root, patch):
        (root / "notadir").write_text("")
        patch.setenv("SKILLMEM_STATE_DIR", str(root / "notadir" / "state"))
        shared = root / "shared-tmp"
        shared.mkdir()
        patch.setattr(tempfile, "tempdir", str(shared))
        (shared / f"skillmem-injected-{H._safe_session(session)}.jsonl").write_text(
            '"owner-rule"\n', encoding="utf-8")
        with owner():
            S.upsert(conn, S.MemoryItem(slug="owner-rule", kind="feedback", title="deployment",
                                       body="deployment approval required", origin="owner"),
                     surface="cli")
        patch.setattr(H, "_connect", lambda ctx: conn)
        data = dict(session_id=session, tool_name="Bash",
                    tool_input={"command": "deployment approval"})
        result = CliRunner().invoke(H.hook_group, ["tool-recall"], input=json.dumps(data))
        assert result.exit_code == 0
        assert "approval required" in result.output
        assert H._dedup_file(session).is_relative_to(H._state_dir())
