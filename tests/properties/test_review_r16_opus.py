"""r16 opus review: one database file is one database, however its path is spelled (INV-12)."""
import os
import unicodedata

import pytest
from hypothesis import given, strategies as st

from skillmem import schedule, storage as S
from .support import PROPERTY, database, owner

FOLDER, FILE = "Café Data", "Memory.db"


def _spell(name, flips, form):
    name = unicodedata.normalize(form, name)
    return "".join(c.swapcase() if flip else c for c, flip in zip(name, flips + [False] * len(name)))


@PROPERTY
@given(folder=st.lists(st.booleans(), max_size=12), file=st.lists(st.booleans(), max_size=12),
       form=st.sampled_from(["NFC", "NFD"]))
def test_every_spelling_of_the_file_is_one_database(folder, file, form):
    # _db_path used Path.resolve(), which keeps the caller's case on APFS: the
    # file opened as `M.db` got another namespace and export key than `m.db`,
    # its body files were copied as a copy's, and once one was gone it would
    # not open under the other spelling.
    with database() as (conn, root, patch):
        path = root / FOLDER / FILE
        path.parent.mkdir()
        first = S.connect(path)
        S.init_schema(first)
        try:
            item = S.upsert(first, S.MemoryItem(slug="doc", kind="document", title="doc",
                                                body="document body " * 800), explicit={"kind"})
            names = (S._db_path(first), S._db_namespace(first), S._db_identity(first))
            patch.setenv("SKILLMEM_DB", str(path))
            tag = schedule._database_tag()
        finally:
            first.close()
        other = root / _spell(FOLDER, folder, form) / _spell(FILE, file, form)
        if not other.exists() or not os.path.samefile(other, path):
            pytest.skip("this file system tells the spellings apart")
        again = S.connect(other)
        try:
            S.init_schema(again)
            assert (S._db_path(again), S._db_namespace(again), S._db_identity(again)) == names
            assert S.get(again, "doc").body_path == item.body_path
            assert sorted(p.name for p in S.docs_dir().iterdir()) == [item.body_path]
            patch.setenv("SKILLMEM_DB", str(other))
            assert schedule._database_tag() == tag
        finally:
            again.close()


DUMP_KEYS = {"type": "kind", "origin": "origin", "originSessionId": "source_session",
             "owner_seal": "owner_seal", "strength": "strength"}


@PROPERTY
@given(kind=st.sampled_from(["feedback", "skill", "note", "document"]),
       origin=st.sampled_from(["agent", "derived", "imported"]),
       dropped=st.sets(st.sampled_from(sorted(DUMP_KEYS)), min_size=1),
       default=st.sampled_from(["document", "note", "reference"]),
       present=st.booleans(), edited=st.booleans())
def test_a_key_a_dump_leaves_out_keeps_the_row_value(kind, origin, dropped, default, present,
                                                     edited):
    # A dump restore named every field, so a dump without `metadata.type` made
    # a stored feedback rule a `--kind` document, and one without
    # `metadata.origin` made an agent's record `owner`, and the owner sealed it.
    # r17 opus review: that held on same text only; with the record's text
    # changed after the backup, the restore still wrote the importer's `owner`.
    import yaml
    from skillmem.export import export_all
    from skillmem.vault import import_vault
    with database() as (conn, root, patch):
        patch.setattr(S, "owner_present", lambda: present)
        S.upsert(conn, S.MemoryItem(slug="rule", kind=kind, title="Rule", body="rule body",
                                    origin=origin, source_session="sess", strength=0.7),
                 explicit={"kind", "origin", "source_session", "strength"})
        before = S.get(conn, "rule")
        export_all(conn, root / "dump")
        (path,) = (root / "dump").rglob("rule.md")
        _, head, body = path.read_text(encoding="utf-8").split("---\n", 2)
        meta = yaml.safe_load(head)
        for key in dropped:
            (meta if key == "strength" else meta["metadata"]).pop(key)
        path.write_text("---\n" + yaml.safe_dump(meta, allow_unicode=True) + "---\n" + body,
                        encoding="utf-8")
        if edited:
            S.upsert(conn, S.MemoryItem(slug="rule", kind=kind, title="Rule", body="edited",
                                        origin="agent"), surface="mcp", reason="edit")
        kept = S.get(conn, "rule")
        report = import_vault(conn, root / "dump", kind=default)
        assert (report.updated, report.failed) == (1, [])
        after = S.get(conn, "rule")
        assert S.load_body(after) == "rule body"
        assert {f: getattr(after, f) for f in DUMP_KEYS.values()} == \
            {f: getattr(kept if key in dropped else before, f) for key, f in DUMP_KEYS.items()}


@PROPERTY
@given(surface=st.sampled_from(sorted(s for s, p in S.SURFACES.items() if "origin" in p["names"])),
       stored=st.sampled_from(sorted(S.ORIGINS)), given_=st.sampled_from(sorted(S.ORIGINS)),
       present=st.booleans(), seal=st.booleans())
def test_a_text_change_that_does_not_name_the_origin_keeps_it(surface, stored, given_, present,
                                                              seal):
    # upsert set the item's origin on every text change, so a surface that can
    # name the origin and did not (a dump without the key, a library call)
    # replaced the row's with a default (INV-14). Kept, the item's unnamed
    # origin still minted the seal: an agent's row sealed as the owner's. An
    # origin the write does not state mints none, the row's kept one included.
    with database() as (conn, root, patch):
        S.upsert(conn, S.MemoryItem(slug="rule", kind="note", title="Rule", body="one",
                                    origin=stored), explicit={"kind", "origin"})
        sealed = S.get(conn, "rule").owner_seal
        patch.setattr(S, "owner_present", lambda: present)
        S.upsert(conn, S.MemoryItem(slug="rule", kind="note", title="Rule", body="two",
                                    origin=given_, owner_seal=seal), surface=surface,
                 explicit={"owner_seal"} if seal else (), reason="edit")
        after = S.get(conn, "rule")
        assert (S.load_body(after), after.origin) == ("two", stored)
        assert bool(after.owner_seal) == bool(sealed or present and seal)


def _tree(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes()
            for p in sorted(directory.rglob("*")) if p.is_file()}


@PROPERTY
@given(ours=st.dictionaries(st.sampled_from(["deploy", "Deploy", "build", "rule"]),
                            st.sampled_from(["skill", "note", "feedback"]), min_size=1),
       theirs=st.dictionaries(st.sampled_from(["deploy", "Deploy", "build", "other"]),
                              st.sampled_from(["skill", "note", "feedback"]), min_size=1))
def test_a_shared_export_directory_restores_each_database(ours, theirs):
    # Two databases exported one slug into one directory under different kind
    # folders, both exports succeeded, and a restore of that directory kept the
    # file that sorted first: A's skill became B's unapproved note.
    from skillmem.export import export_all
    from skillmem.vault import import_vault
    with database() as (conn, root, _):
        for n, (slug, kind) in enumerate(sorted(ours.items())):
            S.upsert(conn, S.MemoryItem(slug=slug, kind=kind, title="ours", body=f"our {slug}",
                                        created_at=1 + n), explicit={"kind", "created_at"})
        other = S.connect(root / "other.db")
        S.init_schema(other)
        try:
            for n, (slug, kind) in enumerate(sorted(theirs.items())):
                S.upsert(other, S.MemoryItem(slug=slug, kind=kind, title="theirs",
                                             body=f"their {slug}", created_at=1000 + n),
                         explicit={"kind", "created_at"})
            out = root / "out"
            export_all(conn, out)
            before = _tree(out)
            try:
                export_all(other, out)
            except ValueError as refused:
                assert "another database" in str(refused)
                assert _tree(out) == before, "INV-12: a refused export wrote"
                # a case twin's file is one of ours where case is ignored (INV-11)
                assert {s.casefold() for s in ours} & {s.casefold() for s in theirs}
            else:
                assert not ours.keys() & theirs.keys(), "INV-12: one slug from two databases"
        finally:
            other.close()
        kept = {slug: S.get(conn, slug) for slug in ours}
        with owner():
            assert not import_vault(conn, out, skip_auto_memories=False).failed
        for slug, item in kept.items():
            again = S.get(conn, slug)
            assert (again.kind, again.title, S.load_body(again), again.created_at) == \
                (item.kind, item.title, S.load_body(item), item.created_at), \
                "INV-06: a record did not come back from its own backup"
