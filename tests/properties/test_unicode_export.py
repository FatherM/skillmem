"""INV-06/11/12: canonical filename aliases never lose a backup."""
import json
import unicodedata

import pytest
from hypothesis import given, strategies as st

from skillmem import storage as S
from skillmem.export import export_all
from skillmem.migrate import split_frontmatter
from .support import PROPERTY, database, put


# Hangul decomposes into letters: both spellings survive filename sanitising.
SLUG = st.text(st.characters(min_codepoint=0xAC00, max_codepoint=0xD7A3),
               min_size=1, max_size=4)


@PROPERTY
@given(slug=SLUG, reverse=st.booleans())
def test_canonical_twins_keep_distinct_dumps(slug, reverse):
    twins = [slug, unicodedata.normalize("NFD", slug)]
    if reverse:
        twins.reverse()
    with database() as (conn, root, _):
        for i, name in enumerate(twins):
            put(conn, name, body=f"record {i}")
        destination = root / "dump"
        for _ in range(2):
            assert export_all(conn, destination) == 2
            paths = list(destination.rglob("*.md"))
            assert len(paths) == 2, "export acknowledged two records but lost a dump"
            assert len({unicodedata.normalize("NFD", p.name.casefold()) for p in paths}) == 2
            documents = dict((meta["name"], body) for meta, body in
                             (split_frontmatter(p.read_text()) for p in paths))
            for i, name in enumerate(twins):
                assert documents[name].strip() == f"record {i}"
        # Removing the first twin changes which record gets the pretty name.
        S.soft_delete(conn, twins[0], reason="remove twin")
        assert export_all(conn, destination) == 1
        paths = list(destination.rglob("*.md"))
        assert len(paths) == 1
        assert split_frontmatter(paths[0].read_text())[0]["name"] == twins[1]


@PROPERTY
@given(slug=SLUG, reverse=st.booleans(), unreadable=st.booleans())
def test_canonical_alias_cannot_overwrite_another_database(slug, reverse, unreadable):
    twins = [slug, unicodedata.normalize("NFD", slug)]
    if reverse:
        twins.reverse()
    with database() as (conn, root, _):
        put(conn, twins[0])
        destination = root / "dump"
        export_all(conn, destination)
        if unreadable:
            next(destination.rglob("*.md")).write_text("unreadable frontmatter")
        before = {p.relative_to(destination): p.read_bytes()
                  for p in destination.rglob("*") if p.is_file()}
        other = S.connect(root / "other.db")
        try:
            S.init_schema(other)
            put(other, twins[1], body="other database")
            with pytest.raises(ValueError, match="another database"):
                export_all(other, destination)
            assert before == {p.relative_to(destination): p.read_bytes()
                              for p in destination.rglob("*") if p.is_file()}
        finally:
            other.close()


@PROPERTY
@given(slug=SLUG, reverse=st.booleans())
def test_pruning_a_canonical_alias_keeps_the_new_dump(slug, reverse):
    twins = [slug, unicodedata.normalize("NFD", slug)]
    if reverse:
        twins.reverse()
    with database() as (conn, root, _):
        put(conn, twins[0])
        destination = root / "dump"
        export_all(conn, destination)
        with S.tx(conn):
            conn.execute("UPDATE memory_items SET slug = ? WHERE slug = ?", twins[::-1])
        assert export_all(conn, destination) == 1
        paths = list(destination.rglob("*.md"))
        assert len(paths) == 1, "pruning the old spelling removed the new dump"
        assert split_frontmatter(paths[0].read_text())[0]["name"] == twins[1]
        manifest = json.loads((destination / ".skillmem-export.json").read_text())
        assert len(manifest["dbs"][S._db_identity(conn)]) == 1
