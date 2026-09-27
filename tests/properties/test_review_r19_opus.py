"""INV-11: an export's files carry the spelling its manifest lists."""
import json
import unicodedata

from hypothesis import given, strategies as st

from skillmem import storage as S
from skillmem.export import export_all
from .support import PROPERTY, database, put


# ASCII letters change case, Hangul decomposes: both keep a clean file name.
SLUG = st.text(st.sampled_from("abcXYZ가힣"), min_size=1, max_size=4)
SPELLINGS = [lambda s: s, str.swapcase, lambda s: unicodedata.normalize("NFD", s),
             lambda s: unicodedata.normalize("NFD", s.swapcase())]


@PROPERTY
@given(slug=SLUG, old=st.sampled_from(SPELLINGS), new=st.sampled_from(SPELLINGS))
def test_a_renamed_record_is_dumped_under_its_new_spelling(slug, old, new):
    # APFS and NTFS kept the replaced file's spelling (r19 opus review): on a
    # case-sensitive copy the stale name is in no manifest and never pruned
    with database() as (conn, root, _):
        destination = root / "dump"
        put(conn, old(slug), body="old text")
        export_all(conn, destination)
        if new(slug) != old(slug):
            S.soft_delete(conn, old(slug), "renamed")
            put(conn, new(slug), body="new text")
        export_all(conn, destination)
        manifest = json.loads((destination / ".skillmem-export.json").read_text("utf-8"))
        listed = {f for files in manifest["dbs"].values() for f in files}
        on_disk = {p.relative_to(destination).as_posix()
                   for p in destination.rglob("*") if p.is_file() and not p.name.startswith(".")}
        assert on_disk == listed == {f"skill/{new(slug)}.md"}
