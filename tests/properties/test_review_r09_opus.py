"""INV-06 and INV-14: the r09 opus review of f011fdc."""
from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import cli, storage as S
from skillmem.export import export_all
from skillmem.migrate import import_dir
from skillmem.vault import import_vault
from .support import PROPERTY, database, owner


# INV-06: every dump carries `metadata.node_type: memory`, so skipping Claude Code
# auto-memories (the library default, and `--skip-frontmatter-memories`) skipped
# every dump too, and the restore INV-06 names restored nothing with success.
@PROPERTY
@given(surface=st.sampled_from(["library-default", "library", "cli"]),
       kind=st.sampled_from(["note", "feedback", "skill", "document"]))
def test_skipping_auto_memories_never_skips_a_dump(surface, kind):
    with database() as (conn, root, _):
        S.upsert(conn, S.MemoryItem(slug="kept", kind=kind, title="t", body="kept body"),
                 explicit={"kind"})
        export_all(conn, root / "e1")
        auto = root / "e1" / "auto.md"      # an auto-memory: node_type, no exported_at
        auto.write_text("---\nname: auto\nmetadata:\n  node_type: memory\n---\n\nauto body\n")
        fresh_db = root / "fresh.db"
        fresh = S.connect(fresh_db)
        S.init_schema(fresh)
        try:
            with owner():
                if surface == "cli":
                    result = CliRunner().invoke(cli.main, [
                        "--db", str(fresh_db), "import-vault",
                        "--skip-frontmatter-memories", str(root / "e1")])
                    assert result.exit_code == 0, result.output
                elif surface == "library":
                    assert not import_vault(fresh, root / "e1", skip_auto_memories=True).failed
                else:
                    assert not import_vault(fresh, root / "e1").failed
            restored = S.get(fresh, "kept")
            assert restored is not None and restored.kind == kind
            assert S.get(fresh, "auto") is None
        finally:
            fresh.close()


# INV-14: a `metadata` key that is not a mapping was dropped, and the file
# imported with the default kind: `metadata: feedback` became a note, never a rule.
@PROPERTY
@given(importer=st.sampled_from(["migrate", "vault"]),
       value=st.sampled_from(["feedback", "[type, feedback]", "1", "true", "''"]))
def test_a_metadata_key_that_is_not_a_mapping_fails_the_file(importer, value):
    with database() as (conn, root, _):
        folder = root / "notes"
        folder.mkdir()
        (folder / "rule.md").write_text(f"---\nname: rule\nmetadata: {value}\n---\n\nAlways test.\n")
        (folder / "other.md").write_text("---\nname: other\nmetadata:\n---\n\nFine.\n")
        with owner():
            report = import_dir(conn, folder) if importer == "migrate" else import_vault(conn, folder)
        assert [name for name, _ in report.failed] and "rule.md" in str(report.failed)
        assert S.get(conn, "rule") is None
        assert S.get(conn, "other") is not None      # null metadata states nothing


# INV-14: a null inside a file's list (`tags: [ops, null]`) was dropped, while
# the library and HTTP refuse the same value; every surface refuses it.
@PROPERTY
@given(key=st.sampled_from(["tags", "topics", "attachments"]),
       items=st.lists(st.sampled_from(["ops", None]), min_size=1, max_size=3))
def test_a_null_in_a_file_list_is_refused_like_the_library(key, items):
    with database() as (conn, root, _):
        if key != "attachments":
            try:
                S.upsert(conn, S.MemoryItem(slug="lib", title="t", body="b", **{key: items}),
                         explicit={key})
                library_refused = False
            except ValueError:
                library_refused = True
            assert library_refused == (None in items)
        folder = root / "vault"
        folder.mkdir()
        (folder / "ops.png").write_bytes(b"png")
        listed = ", ".join("null" if v is None else ("ops.png" if key == "attachments" else v)
                           for v in items)
        (folder / "note.md").write_text(f"---\nname: note\n{key}: [{listed}]\n---\n\nbody\n")
        with owner():
            report = import_vault(conn, folder)
        assert bool(report.failed) == (None in items), report.failed
        assert (S.get(conn, "note") is None) == (None in items)
