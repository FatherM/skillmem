"""INV-08/11: the r05 review of 207b6b4."""
import os
import unicodedata

from hypothesis import example, given, strategies as st

from skillmem import vault
from skillmem.migrate import import_dir
from .support import PROPERTY, database

NOTE = "café"
# what a link may name: the note in its own, another case or Unicode form;
# nothing; a file or a folder outside the tree; a folder inside it
TARGETS = {
    "same": f"{NOTE}.md",
    "case": f"{NOTE.upper()}.md",
    "nfd": unicodedata.normalize("NFD", f"{NOTE}.md"),
    "nothing": "missing.md",
    "outside file": "../elsewhere/secret.txt",
    "outside folder": "../elsewhere",
    "inside folder": "sub",
}


# INV-08: discovery kept a file once per resolved path, and on APFS resolve()
# keeps the link's spelling, so `link.md -> NOTE.md` stored `note.md` twice
# (on ext4 the link leads nowhere and is reported: INV-11); `migrate` read a
# link out of its directory and passed a directory link over, exit 0
@PROPERTY
@example(importer="import-vault", links=["case", "nfd"])
@example(importer="migrate", links=["same", "outside file"])
@example(importer="migrate", links=["inside folder"])
@given(importer=st.sampled_from(["import-vault", "migrate"]),
       links=st.lists(st.sampled_from(sorted(TARGETS)), min_size=1, max_size=4))
def test_each_file_is_imported_once_and_every_link_left_out_is_reported(importer, links):
    with database() as (conn, root, patch):
        tree, elsewhere = root / "tree", root / "elsewhere"
        (tree / "sub").mkdir(parents=True)
        elsewhere.mkdir()
        (tree / f"{NOTE}.md").write_text("Deploy steps\n\nrun make deploy\n")
        (elsewhere / "secret.txt").write_text("outside secret\n")
        for n, link in enumerate(links):
            os.symlink(TARGETS[link], tree / (f"link{n}.md" if n % 2 else f"link{n}"))
        if importer == "import-vault":
            report = vault.import_vault(conn, tree)
        else:
            report = import_dir(conn, tree)
        failed = {name.rsplit("/", 1)[-1] for name, _ in report.failed}
        rows = conn.execute("SELECT body FROM memory_items").fetchall()
        assert len(rows) == report.inserted == 1, (rows, report)
        assert "run make deploy" in rows[0][0]    # and nothing from outside
        for n, link in enumerate(links):
            path = tree / (f"link{n}.md" if n % 2 else f"link{n}")
            second_name = path.is_file() and path.samefile(tree / f"{NOTE}.md")
            if not second_name and (n % 2 or not path.is_file()):
                assert path.name in failed, (link, report.failed)
