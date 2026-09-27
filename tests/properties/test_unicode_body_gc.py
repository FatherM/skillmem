"""INV-11/12: GC preserves referenced body files under filename aliases."""
import os
import unicodedata

from hypothesis import given, strategies as st

from skillmem import storage as S
from .support import PROPERTY, database


@PROPERTY
@given(syllables=st.text(st.characters(min_codepoint=0xAC00, max_codepoint=0xD7A3),
                         min_size=1, max_size=4),
       form=st.sampled_from(["NFC", "NFD"]), case_alias=st.booleans())
def test_gc_preserves_live_filename_aliases(syllables, form, case_alias):
    with database() as (conn, _, __):
        body = "A complete document paragraph.\n" * 400
        S.upsert(conn, S.MemoryItem(slug="Doc-" + syllables, title="Document",
                                   kind="document", body=body), explicit=set())
        item = S.get(conn, "Doc-" + syllables)
        original = S.docs_dir() / item.body_path
        # Exercise both directions, including paths retained from older stores.
        stored = unicodedata.normalize(form, original.name)
        alias = unicodedata.normalize("NFD" if form == "NFC" else "NFC", stored)
        if case_alias:
            alias = alias.replace("Doc-", "doc-", 1)
        renamed = original.with_name(alias)
        original.rename(renamed)
        referenced = original.with_name(stored)
        # APFS resolves the spelling itself. On ext4, supply the same inode
        # under both spellings so the full body is readable before GC too.
        if not referenced.exists():
            referenced.hardlink_to(renamed)
        with S.tx(conn):
            conn.execute("UPDATE memory_items SET body_path = ? WHERE slug = ?",
                         (stored, item.slug))
        item = S.get(conn, item.slug)
        assert S.load_body(item) == body
        orphan = S.docs_dir() / S._body_filename(
            "orphan", ns=S._db_namespace(conn), content_hash="1" * 64)
        foreign = S.docs_dir() / S._body_filename(
            "foreign", ns="ffffffff" if S._db_namespace(conn) != "ffffffff" else "eeeeeeee",
            content_hash="2" * 64)
        orphan.write_text("orphan")
        foreign.write_text("another database")
        for path in S.docs_dir().iterdir():
            os.utime(path, (1, 1))
        assert S.gc_body_files(conn) == 1
        assert renamed.exists()
        assert S.load_body(item) == body
        assert not orphan.exists()
        assert foreign.read_text() == "another database"
