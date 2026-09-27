"""INV-08/12: discovery must account for links; publication keeps ownership."""
import json
from pathlib import Path

import pytest
from hypothesis import example, given, strategies as st

from skillmem import export as E, packs, storage as S, vault
from .support import PROPERTY, database, put


@pytest.mark.parametrize("failure", [KeyboardInterrupt, OSError])
@pytest.mark.parametrize("asset", [False, True])
@PROPERTY
@given(after=st.booleans(), again=st.booleans())
def test_interrupted_publication_keeps_every_published_file_listed(failure, asset, after, again):
    with database() as (conn, root, patch):
        attachments = []
        if asset:
            assets = S.default_data_dir() / "assets"
            assets.mkdir(parents=True, exist_ok=True)
            (assets / "pic.png").write_bytes(b"backup asset")
            attachments = ["assets/pic.png"]
        put(conn, slug="record", attachments=attachments)
        out = root / "out"
        if again:
            E.export_all(conn, out)
        real = E.os.replace
        interrupted = False

        def replace(src, dst):
            nonlocal interrupted
            target = Path(dst)
            if not interrupted and target.suffix == (".png" if asset else ".md"):
                interrupted = True
                if after:
                    real(src, dst)
                raise failure("interrupted publication")
            return real(src, dst)

        with patch.context() as fault:
            fault.setattr(E.os, "replace", replace)
            with pytest.raises(failure):
                E.export_all(conn, out)
        assert interrupted
        files = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*")
                 if p.is_file() and not p.name.startswith(".")}
        listed = {f for entry in json.loads((out / ".skillmem-export.json").read_text())["dbs"].values()
                  for f in entry}
        assert files.keys() <= listed, "INV-12: published files lost their manifest ownership"
        other = S.connect(root / "other.db")
        try:
            S.init_schema(other)
            put(other, slug="record", body="other database", created_at=9999)
            if any(name.endswith(".md") for name in files):
                with pytest.raises(ValueError):
                    E.export_all(other, out)
                assert all((out / name).read_bytes() == data for name, data in files.items())
        finally:
            other.close()
        E.export_all(conn, out)
        assert len(list(out.rglob("*.md"))) == 1


@pytest.mark.parametrize("surface", ["pack", "vault"])
@PROPERTY
@example(suffix=".txt", excluded=False, copies=1, missing=False)
@given(suffix=st.sampled_from([".md", ".txt", ".data"]),
       excluded=st.booleans(), copies=st.integers(1, 3), missing=st.booleans())
def test_in_tree_links_are_imported_or_reported(surface, suffix, excluded, copies, missing):
    with database() as (conn, root, patch):
        tree = root / "source"
        target = tree / ("tests" if excluded else "procedures") / ("procedure" + suffix)
        target.parent.mkdir(parents=True)
        target.write_text("---\nname: deploy\ndescription: Deploy procedure.\n---\nDeploy quartz safely.\n")
        for i in range(copies):
            link = tree / f"alias{i}" / ("SKILL.md" if surface == "pack" else "deploy.md")
            link.parent.mkdir()
            link.symlink_to(target)
        if missing:
            target.unlink()
        if surface == "pack":
            skipped = []
            skills = packs.read_pack(tree, skipped)
            assert any("Deploy quartz safely." in s.body for s in skills) or len(skipped) >= copies
        else:
            report = vault.import_vault(conn, tree)
            assert report.inserted or len(report.failed) >= copies, "INV-08: links silently omitted"
