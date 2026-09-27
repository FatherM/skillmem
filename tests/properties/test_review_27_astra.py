"""INV-12/14: copy isolation must finish; named visibility is never omission."""
import os

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, strategies as st

from skillmem import storage as S
from skillmem.server import TokenStore, build_app
from .support import PROPERTY, database


@PROPERTY
@given(existing=st.booleans(), prior=st.sampled_from(["public", "shared", "private"]))
def test_storage_refuses_null_visibility(existing, prior):
    with database() as (conn, _, __):
        if existing:
            S.upsert(conn, S.MemoryItem(slug="record", title="Document", body="body",
                                       visibility=prior), explicit={"visibility"})
        before = [tuple(r) for r in conn.execute("SELECT * FROM memory_items")]
        with pytest.raises(ValueError, match="visibility"):
            S.upsert(conn, S.MemoryItem(slug="record", title="Document", body="body",
                                       visibility=None), explicit={"visibility"})
        assert [tuple(r) for r in conn.execute("SELECT * FROM memory_items")] == before


@pytest.mark.parametrize("timing", ["before_read", "after_read"])
@PROPERTY
@given(damage=st.sampled_from(["gc", "corrupt", "encoding"]),
       count=st.integers(1, 3))
def test_copy_open_preserves_every_body_or_refuses(timing, damage, count):
    with database() as (source, root, patch):
        body = "original document line\n" * 600
        for index in range(count):
            S.upsert(source, S.MemoryItem(slug=f"doc-{index}", title="Document",
                                        kind="document", body=body))
        copied = S.connect(root / "copy.db")
        source.backup(copied)
        before = [tuple(r) for r in copied.execute("SELECT * FROM memory_items")]
        victim = S.get(source, f"doc-{count - 1}")
        path = S.docs_dir() / victim.body_path
        verify = S.verified_body_file
        hit = False

        def interleave(title, body_path, content_hash):
            nonlocal hit
            if body_path != victim.body_path or hit:
                return verify(title, body_path, content_hash)
            hit = True
            text = verify(title, body_path, content_hash) if timing == "after_read" else None
            if damage == "gc":
                S.upsert(source, S.MemoryItem(slug=victim.slug, title="Document",
                                             kind="document", body="replacement"), reason="edit")
                os.utime(path, (1, 1))
                assert S.gc_body_files(source) == 1
            else:
                path.write_bytes(b"corrupt" if damage == "corrupt" else b"\xff")
            return text if timing == "after_read" else verify(title, body_path, content_hash)

        patch.setattr(S, "verified_body_file", interleave)
        try:
            try:
                S.init_schema(copied)
            except OSError:
                assert timing == "before_read"
                assert [tuple(r) for r in copied.execute("SELECT * FROM memory_items")] == before
            else:
                for index in range(count):
                    row = S.get(copied, f"doc-{index}")
                    assert S.load_body(row) == body, "INV-12: successful open lost a copied body"
                    assert row.body_path != S.get(source, f"doc-{index}").body_path
            assert hit, "the competing write must run during isolation"
        finally:
            copied.close()


@pytest.mark.parametrize("endpoint", ["/write", "/learn"])
@PROPERTY
@given(existing=st.booleans(), visibility=st.sampled_from([None, "", "invalid"]),
       prior=st.sampled_from(["public", "shared", "private"]))
def test_http_create_refuses_invalid_named_visibility(endpoint, existing, visibility, prior):
    with database() as (conn, root, _):
        tokens = root / "tokens.yaml"
        tokens.write_text("alice:\n  token: test-token\n  scope: master\n")
        payload = {"slug": "record", "title": "Document", "check_conflicts": False}
        payload.update({"body": "body"} if endpoint == "/write" else
                       {"trigger": "deploy", "steps": "verify", "outcome": "done"})
        with TestClient(build_app(TokenStore(tokens), root / "memory.db")) as client:
            client.headers["Authorization"] = "Bearer test-token"
            if existing:
                assert client.post(endpoint, json={**payload, "visibility": prior}).status_code == 200
            before = [tuple(r) for r in conn.execute("SELECT * FROM memory_items")]
            response = client.post(endpoint, json={**payload, "visibility": visibility})
            assert response.status_code == 422, response.text
            assert [tuple(r) for r in conn.execute("SELECT * FROM memory_items")] == before
            # Omission still preserves existing visibility and uses each insert default.
            assert client.post(endpoint, json=payload).status_code == 200
            assert S.get(conn, "record").visibility == (
                prior if existing else "private" if endpoint == "/write" else "public")
