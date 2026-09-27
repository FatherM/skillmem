"""INV-01/03/13/15: unambiguous text identity and honest excerpt notices."""
import hashlib
import json

import pytest
from hypothesis import example, given, strategies as st

from skillmem import mcp_server as M, storage as S
from .support import PROPERTY, database, owner


def legacy_hash(title, body):
    return hashlib.sha256((title + "\n\0\n" + body).encode()).hexdigest()


@pytest.mark.parametrize("surface", ["mcp", "cli", "dump"])
@PROPERTY
@given(reverse=st.booleans(), sealed=st.booleans(),
       legacy=st.booleans(), middle=st.text(alphabet="abc Ω", min_size=1, max_size=30))
@example(reverse=False, sealed=True, legacy=True, middle="owner text")
def test_hash_alias_cannot_replace_a_body_file(surface, reverse, sealed, legacy, middle):
    delimiter = "\n\0\n"
    tail = "procedure step\n" * 800
    pairs = [("policy", middle + delimiter + tail),
             ("policy" + delimiter + middle, tail)]
    if reverse:
        pairs.reverse()
    (title, body), (replacement_title, replacement_body) = pairs
    assert legacy_hash(title, body) == legacy_hash(replacement_title, replacement_body)
    with database() as (conn, root, patch):
        with owner(), patch.context() as initial:
            if legacy:
                initial.setattr(S, "_hash", legacy_hash)
            row = S.upsert(conn, S.MemoryItem(slug="policy", kind="document",
                           title=title, body=body, origin="owner" if sealed else "agent"),
                           surface="cli" if sealed else "mcp")
        before = dict(conn.execute("SELECT * FROM memory_items").fetchone())
        path = S.docs_dir() / row.body_path
        original = path.read_bytes()
        if surface == "mcp":
            patch.setattr(M, "_shared_conn", lambda: conn)
            result = M._tool_update({"slug": "policy", "title": replacement_title,
                                     "body": replacement_body, "reason": "edit"})
            refused = not json.loads(result[0].text).get("ok", False)
        else:
            try:
                S.upsert(conn, S.MemoryItem(slug="policy", kind="document",
                         title=replacement_title, body=replacement_body),
                         surface=surface, reason="edit")
            except (S.MemoryConflict, S.SealedRecord):
                refused = True
            else:
                refused = False
        assert path.read_bytes() == original, "INV-03: an alias overwrote the body file"
        if sealed:
            assert refused, "a sealed text change must be refused"
            assert dict(conn.execute("SELECT * FROM memory_items").fetchone()) == before
            assert S.load_body(S.get(conn, "policy")) == body
            assert S.history(conn, "policy") == []
        else:
            assert not refused
            updated = S.get(conn, "policy")
            assert updated.title == replacement_title
            assert S.load_body(updated) == replacement_body
            assert updated.content_hash != row.content_hash
            assert updated.trusted_at is None
            assert S.history(conn, "policy")[0]["old_body"] == body


@PROPERTY
@given(legacy=st.booleans(), middle=st.text(alphabet="abc Ω", min_size=1, max_size=30))
def test_hash_alias_cannot_satisfy_a_pending_approval(legacy, middle):
    """Hash CAS must reject an alias even after an intervening edit."""
    with database() as (conn, root, patch):
        with patch.context() as initial:
            if legacy:
                initial.setattr(S, "_hash", legacy_hash)
            row = S.upsert(conn, S.MemoryItem(slug="policy", title="policy",
                           body=middle + "\n\0\ntail"))
        S.upsert(conn, S.MemoryItem(slug="policy", title="intermediate", body="edit"), reason="edit")
        S.upsert(conn, S.MemoryItem(slug="policy", title="policy\n\0\n" + middle,
                                   body="tail"), reason="edit")
        with owner(), pytest.raises(S.MemoryConflict):
            S.set_trust(conn, "policy", trusted=True, expect_hash=row.content_hash)


@PROPERTY
@given(title=st.sampled_from(["policy", "policy\n\0\npart"]), missing=st.booleans())
def test_legacy_hash_reads_and_repairs_preserve_approval(title, missing):
    with database() as (conn, root, patch):
        body = "full procedure\n" * 1000
        with owner(), patch.context() as initial:
            initial.setattr(S, "_hash", legacy_hash)
            row = S.upsert(conn, S.MemoryItem(slug="policy", title=title, body=body,
                           kind="document", origin="owner"), surface="cli")
        assert S.served_body(row) == body
        before = dict(conn.execute("SELECT * FROM memory_items").fetchone())
        if missing:
            (S.docs_dir() / row.body_path).unlink()
        S.upsert(conn, S.MemoryItem(slug="policy", title=title, body=body), surface="mcp")
        assert dict(conn.execute("SELECT * FROM memory_items").fetchone()) == before
        assert S.served_body(S.get(conn, "policy")) == body
        assert S.history(conn, "policy") == []


@PROPERTY
@given(padding=st.integers(min_value=50, max_value=300),
       damage=st.sampled_from(["delete", "replace"]), approved=st.booleans())
@example(padding=100, damage="delete", approved=True)
def test_search_snippets_keep_the_excerpt_notice(padding, damage, approved):
    with database() as (conn, root, patch):
        with owner():
            row = S.upsert(conn, S.MemoryItem(slug="policy", kind="document", title="policy",
                           body="intro " * padding + "needle diagnostic " + "tail " * 2000),
                           surface="cli" if approved else "mcp")
        path = S.docs_dir() / row.body_path
        if damage == "delete":
            path.unlink()
        else:
            path.write_bytes(b"damaged")
        patch.setattr(M, "_shared_conn", lambda: conn)
        hit, = S.search(conn, "needle")
        assert hit["snippet"].startswith(S.EXCERPT_NOTICE)
        assert "needle" in hit["snippet"]
        result = json.loads(M._tool_search({"query": "needle"})[0].text)
        assert "skillmem: excerpt only" in result["results"][0]["snippet"]


@pytest.mark.parametrize("action", ["edit", "delete", "archive"])
def test_empty_document_history_is_verified(action):
    with database() as (conn, root, patch):
        S.upsert(conn, S.MemoryItem(slug="empty", title="empty", body="", kind="document"))
        if action == "edit":
            S.upsert(conn, S.MemoryItem(slug="empty", title="empty", body="new"), reason="edit")
        elif action == "delete":
            S.soft_delete(conn, "empty", reason="gone")
        else:
            S.set_archived(conn, "empty", True)
        assert S.history(conn, "empty")[0]["old_body"] == ""
