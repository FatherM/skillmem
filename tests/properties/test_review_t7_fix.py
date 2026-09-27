"""INV-12/14: legacy export ownership and exact integer imports."""
import json
import re

import pytest
from hypothesis import given, strategies as st

from skillmem import storage as S
from skillmem.export import export_all
from skillmem.vault import import_vault
from .support import PROPERTY, database, owner, put


@pytest.mark.parametrize("manifest_format", ["files", "default", "both"])
@PROPERTY
@given(action=st.sampled_from(["empty", "disjoint", "collision", "restored"]),
       as_owner=st.booleans())
def test_legacy_exports_keep_another_databases_files(manifest_format, action, as_owner):
    with database() as (conn, root, patch):
        put(conn)
        out = root / "out"
        export_all(conn, out)
        manifest = out / ".skillmem-export.json"
        files = ["skill/record.md"]
        legacy = {}
        if manifest_format in ("files", "both"):
            legacy["files"] = files
        if manifest_format in ("default", "both"):
            legacy["dbs"] = {"default": files}
        manifest.write_text(json.dumps(legacy))
        before = (out / files[0]).read_bytes()
        other = S.connect(root / "other.db")
        S.init_schema(other)
        try:
            if action == "restored":
                with owner():
                    assert not import_vault(other, out, skip_auto_memories=False).failed
            elif action != "empty":
                put(other, slug="different" if action == "disjoint" else "record", created_at=1)
            patch.setattr(S, "owner_present", lambda: as_owner)
            if action == "collision" or (action == "restored" and not as_owner):
                with pytest.raises(ValueError):
                    export_all(other, out)
                assert json.loads(manifest.read_text()) == legacy
            else:
                export_all(other, out)
            after = (out / files[0]).read_bytes()
            if action == "restored" and as_owner:
                assert re.sub(rb"(?m)^exported_at:.*", b"", after) == re.sub(
                    rb"(?m)^exported_at:.*", b"", before)
            else:
                assert after == before
            # Retaining the legacy reservation matters on subsequent exports too.
            if action in ("empty", "disjoint"):
                export_all(other, out)
                assert (out / files[0]).read_bytes() == before
        finally:
            other.close()


@pytest.mark.parametrize("manifest_format", ["files", "default"])
@PROPERTY
@given(deleted=st.booleans())
def test_owner_can_adopt_legacy_export_with_matching_records(manifest_format, deleted):
    with database() as (conn, root, _):
        put(conn)
        out = root / "out"
        export_all(conn, out)
        files = ["skill/record.md"]
        manifest = out / ".skillmem-export.json"
        manifest.write_text(json.dumps({"files": files} if manifest_format == "files"
                                       else {"dbs": {"default": files}}))
        if deleted:
            S.soft_delete(conn, "record", reason="removed")
        with owner():
            assert export_all(conn, out) == (0 if deleted else 1)
        assert (out / files[0]).exists() == (not deleted)
        assert set(json.loads(manifest.read_text())["dbs"]) == {S._db_identity(conn)}


@pytest.mark.parametrize("dump", [False, True])
@pytest.mark.parametrize("key", ["ttl_days", "freshness_until", "created_at", "updated_at",
                                "access_count", "confirmed_count", "failure_count",
                                "last_accessed_at", "last_decayed_at"])
@PROPERTY
@given(value=st.integers(1, 3650), existing=st.booleans())
def test_import_integer_fields_refuse_floats_without_writing(dump, key, value, existing):
    with database() as (conn, root, _):
        if existing:
            put(conn, slug="record", ttl_days=30)
        before = [tuple(row) for row in conn.execute("SELECT * FROM memory_items")]
        notes = root / "notes"
        notes.mkdir()
        metadata = "exported_at: 1\nmetadata: {node_type: memory}\n" if dump else ""
        (notes / "record.md").write_text(
            f"---\nname: record\ndescription: replacement\n{metadata}{key}: {value}.0\n---\nchanged\n")
        report = import_vault(conn, notes, skip_auto_memories=False)
        assert report.failed, f"INV-14: {key}: {value}.0 was accepted"
        assert report.inserted == report.updated == 0
        assert [tuple(row) for row in conn.execute("SELECT * FROM memory_items")] == before
