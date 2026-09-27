"""INV-06/14: a dump's assets match its row; packs validate YAML."""
import os
import time

import pytest
from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import export as E, storage as S
from skillmem.cli import main
from skillmem.vault import _store_asset, import_vault
from .support import PROPERTY, database, owner
from .test_concurrency import pair, PauseConnection


@PROPERTY
@given(payload=st.binary(min_size=1, max_size=40), external=st.booleans(),
       late=st.booleans(), change=st.sampled_from(["edit", "kind", "delete"]))
def test_export_refresh_keeps_attachments_with_the_record(payload, external, late, change):
    # The concurrent edit may also move the record to another kind or delete
    # it: the re-read under the lock gave up on those, and the excerpt of a
    # GC'd body file replaced the last good dump (*twenty-first review*).
    with pair() as (conn, other, root, patch):
        source = root / "picture.png"
        source.write_bytes(payload)
        attachment = _store_asset(source, S.default_data_dir() / "assets")
        def write(c, version, attachments, kind="document" if external else "note"):
            return S.upsert(c, S.MemoryItem(slug="runbook", kind=kind,
                            title="Runbook", body=version + "x" * 9000,
                            attachments=attachments), explicit={"attachments", "kind"},
                            reason="edit")
        write(conn, "v1", [])
        E.export_all(conn, root / "dump")
        write(conn, "v2", [])
        def edit_and_collect():
            if change == "kind":
                write(other, "v3", [attachment], kind="reference")
            else:
                write(other, "v3", [attachment])
            if change == "delete":
                assert S.soft_delete(other, "runbook", "gone")
            for path in S.docs_dir().glob("*.md"):
                os.utime(path, (time.time() - 3600, time.time() - 3600))
            S.gc_body_files(other)
        if late:
            publish = E._write_planned
            def edit_before_publish(*args, **kwargs):
                edit_and_collect()
                return publish(*args, **kwargs)
            patch.setattr(E, "_write_planned", edit_before_publish)
        proxy = PauseConnection(conn, lambda sql: "SELECT * FROM memory_items ORDER BY" in sql,
                                (lambda: None) if late else edit_and_collect)
        # deleted before the plan read it: not in the dump, as the next export
        gone = change == "delete" and not late
        assert E.export_all(proxy, root / "dump") == (0 if gone else 1)
        assert proxy.fired
        restored = S.connect(root / "restored.db")
        S.init_schema(restored)
        # The backup must stand alone, without the source store's assets.
        (S.default_data_dir() / attachment).unlink()
        try:
            with owner():
                report = import_vault(restored, root / "dump", skip_auto_memories=False)
            assert not report.failed
            assert report.inserted == (0 if gone else 1)
            if gone:
                return
            row = S.get(restored, "runbook")
            body = S.load_body(row)
            assert body in {v + "x" * 9000 for v in ("v2", "v3")}
            assert row.attachments == ([attachment] if body.startswith("v3") else [])
            if row.attachments:
                assert E.intact_asset(root / "dump" / attachment) == payload
        finally:
            restored.close()


@pytest.mark.parametrize("field", ["name", "description"])
@PROPERTY
@given(value=st.sampled_from(["[unterminated", "{broken", '"unclosed', "[a, b]", "{a: b}", "true"]),
       mixed=st.booleans())
def test_pack_invalid_frontmatter_is_reported_without_writing(field, value, mixed):
    with database() as (conn, root, patch):
        pack = root / "pack"
        pack.mkdir()
        (pack / "SKILL.md").write_text(f"---\n{field}: {value}\n---\nProcedure\n")
        if mixed:
            good = pack / "good"
            good.mkdir()
            (good / "SKILL.md").write_text("---\nname: good\ndescription: Valid\n---\nGood procedure\n")
        result = CliRunner().invoke(main, ["skills", "add", str(pack)])
        assert result.exit_code != 0, result.output
        assert conn.execute("SELECT count(*) FROM memory_items").fetchone()[0] == int(mixed)
