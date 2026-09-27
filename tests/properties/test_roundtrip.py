"""INV-06/11/15: compare dump bytes, not YAML's lossy interpretation."""
import dataclasses
import hashlib
import re
from contextlib import closing

import pytest

from hypothesis import example, given, strategies as st

from skillmem import storage as S
from skillmem.export import export_all
from skillmem.vault import import_vault
from .support import PROPERTY, database, owner

# NEL is a C1 control and a YAML line break: excluding Cc hid that a dump
# folded it to a space (thirteenth review)
TEXT = st.text(st.characters(exclude_categories=("Cs", "Cc"), include_characters="\x85"),
               max_size=32)
BODY = st.one_of(TEXT, st.sampled_from([
    "", "\r", "\r\n", " leading\r\nbody\rtrailing \n", "\n---\n",
    "\u2028\u00a0\u2029", "long document\r\n" * 650,
    # past the excerpt, under the document threshold: a note keeps it inline
    "mid-sized note\n" * 400,
]))


SLUGS = ("quartz", "Quartz", "qu\x85artz")


def dump_bytes(root):
    return {p.relative_to(root).as_posix(): re.sub(
        rb"(?m)^exported_at:.*\n", b"", p.read_bytes())
        for p in root.rglob("*") if p.is_file() and not p.name.startswith(".skillmem")}


@pytest.mark.parametrize("destination", ["fresh", "same", "edited", "drifted", "independent",
                                         "rekinded"])
@PROPERTY
@given(title=st.one_of(TEXT, st.sampled_from([" padded ", "\n", "title\r\nsecond", "\u00a0"])), body=BODY, tags=st.lists(TEXT, max_size=2),
       archived=st.booleans(), pinned=st.booleans(), attachment=st.booleans(),
       strength=st.sampled_from([1.0, 0.2]), named=st.sampled_from(["value", "", "val\x85ue"]),
       recalled=st.sampled_from([None, "recalled", "decayed"]),
       deadline=st.sampled_from(["derived", "cleared", "moved"]), earned=st.booleans(),
       epoch=st.booleans(), owned=st.booleans(), approved=st.booleans(),
       kind=st.sampled_from(["document", "note"]))
# a note past the excerpt: generation seldom pairs its body with its kind
@example(title="t", body="mid-sized note\n" * 400, tags=[], archived=False, pinned=False,
         attachment=False, strength=1.0, named="value", recalled=None, deadline="derived",
         earned=False, epoch=False, owned=False, approved=False, kind="note")
def test_export_import_export(title, body, tags, archived, pinned, attachment, strength, named, recalled,
                              deadline, earned, epoch, owned, approved, kind, destination):
    with database() as (conn, root, patch):
        patch.setattr(S, "_now", lambda: 1000000)
        attachments = []
        if attachment:
            data = b'<svg xmlns="http://www.w3.org/2000/svg"/>\r\n'
            asset = root / "home" / "assets" / hashlib.sha256(data).hexdigest()[:2] / (hashlib.sha256(data).hexdigest() + ".svg")
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(data)
            attachments = [asset.relative_to(root / "home").as_posix()]
        for slug in SLUGS:
            S.upsert(conn, S.MemoryItem(slug=slug, kind=kind, title=title,
                body=body, tags=tags, topics=["ops", "日本語"], attachments=attachments,
                # "" too: a caller clears a field with it (INV-14)
                project=named and "project", agent=named and "writer",
                source_session=named and "session", ttl_days=7,
                strength=strength, visibility="public" if earned else "private",
                # an owner record a library call wrote with no terminal is
                # unsealed, and a restore at one kept its origin but sealed it
                # (*nineteenth review*)
                origin="owner" if owned else "derived" if earned else "agent",
                confirmed_count=3 * earned, failure_count=earned),
                explicit={"confirmed_count", "failure_count"} if earned else set())
            if deadline != "derived":
                # a deadline cleared or moved since is not the TTL's
                S.upsert(conn, S.MemoryItem(slug=slug, title=title, body=body, freshness_until={
                    "cleared": None, "moved": 5000000}[deadline]), explicit={"freshness_until"})
            if epoch:
                # a time the write accepts is restored, the least one too: a
                # fresh restore took 0 for none and stamped the current time
                S.upsert(conn, S.MemoryItem(slug=slug, title=title, body=body, created_at=0,
                                            updated_at=0), explicit={"created_at", "updated_at"})
            if recalled:
                S.reinforce(conn, slug)
            if recalled == "decayed":
                patch.setattr(S, "_now", lambda: 1000000 + 30 * 86400)
                S.decay_stale(conn, kind=kind)
                patch.setattr(S, "_now", lambda: 1000000)
            with owner():
                if approved:
                    S.set_trust(conn, slug, trusted=True, expect_hash=S.get(conn, slug).content_hash)
                S.set_pinned(conn, slug, pinned)
                if archived and not pinned:
                    S.set_archived(conn, slug, True)
        originals = {slug: S.get(conn, slug) for slug in SLUGS}
        before = conn.total_changes
        export_all(conn, root / "first")
        assert conn.total_changes == before, "INV-06: export must not mutate the DB"
        first = dump_bytes(root / "first")
        assert len({n.casefold() for n in first}) == len(first), "INV-11: filename alias"
        patch.setattr(S, "_now", lambda: 1000010)
        target = S.connect(root / "fresh.db") if destination in ("fresh", "independent") else conn
        try:
            if target is not conn:
                S.init_schema(target)
                if destination == "independent":
                    # another database's records under the same slugs, born later
                    for slug in originals:
                        S.upsert(target, S.MemoryItem(slug=slug, title="other", body="other",
                                                      created_at=2000000))
            elif destination == "edited":
                with owner():
                    for slug in originals:
                        # K1: a write lands only on a visible row; the dump re-archives it
                        S.set_archived(conn, slug, False)
                        S.upsert(conn, S.MemoryItem(slug=slug, title="edited", body="changed"),
                                 reason="intervening edit", explicit=set())
            elif destination == "drifted":
                # same text, moved state: recalled since the export (counters,
                # not the age) and archived or restored by the owner
                conn.execute("UPDATE memory_items SET access_count = access_count + 1")
                with owner():
                    for slug in originals:
                        if not pinned:
                            S.set_archived(conn, slug, not archived)
            elif destination == "rekinded":
                # the same text under the other kind: a body file kept on the
                # same text left a note restored over a document an excerpt
                # (*r12 opus review*)
                with owner():
                    for slug in originals:
                        S.set_archived(conn, slug, False)
                        S.upsert(conn, S.MemoryItem(
                            slug=slug, kind={"note": "document"}.get(kind, "note"),
                            title=title, body=body), explicit={"kind"})
            with owner():
                report = import_vault(target, root / "first", skip_auto_memories=False)
            assert not report.failed, f"INV-06: {report.failed}"
            for slug, old in originals.items():
                row = S.get(target, slug)
                assert row is not None, "INV-06: lost row"
                assert (row.title, S.load_body(row)) == (old.title, S.load_body(old)), "INV-06/15: text"
                # every field but the database's own (row id, file name) and
                # approval, which never travels: a field added later is
                # restored or named here (INV-06)
                fields = {f.name for f in dataclasses.fields(S.MemoryItem)} - UNCOMPARED
                assert {k: getattr(row, k) for k in fields} == {k: getattr(old, k) for k in fields}, "INV-06: fields"
                assert (row.trusted_at is not None) == (approved and destination in ("same", "drifted")), \
                    "INV-01: approval must not travel"
            export_all(target, root / "second")
            assert dump_bytes(root / "second") == first, "INV-06: dump bytes changed"
        finally:
            if target is not conn:
                target.close()


# Every field but the database's own (row id, file name), approval, which
# never travels, and the tombstone, which no dump holds. The body (the whole
# text or its excerpt) is compared: it was not, and a restore over a record
# rekinded since left a note stored as an excerpt (*r12 opus review*). The property above
# moves each of the rest off its default: a field it never moved was compared
# equal to itself (seventh review: a stored confidence and a cleared deadline).
UNCOMPARED = {"id", "body_path", "trusted_at", "trusted_by", "deleted_at"}
MOVED = {"slug", "kind", "title", "body", "project", "tags", "topics", "visibility", "agent",
         "source_session", "attachments", "ttl_days", "freshness_until", "strength", "pinned",
         "lifecycle", "owner_seal", "confirmed_count", "failure_count", "access_count",
         "last_accessed_at", "last_decayed_at", "origin", "content_hash", "wordcount",
         "created_at", "updated_at"}


def test_the_round_trip_moves_every_field():
    """INV-06: a MemoryItem field is restored and moved by the property, or named here."""
    assert {f.name for f in dataclasses.fields(S.MemoryItem)} - UNCOMPARED == MOVED


@pytest.mark.parametrize("destination", ["same", "fresh"])
@pytest.mark.parametrize("lost", ["asset", "body", "dumped asset", "asset everywhere", "damaged asset",
                                  "body, then a case twin", "damaged asset everywhere"])
def test_a_copy_the_store_lost_survives_in_its_backup(lost, destination):
    """INV-06: an export over an earlier one never replaces what the store
    lost with less (*thirteenth review:* a lost attachment's backup was pruned),
    and a restore takes each listed attachment from the dump or the store, or
    fails the file (it cleared the list while the store held the file).
    *Fourteenth review:* damaged bytes are lost bytes (INV-16: a stored name is
    the hash of its bytes), and a record whose file a case twin moved still
    finds its earlier dump."""
    with database() as (conn, root, patch):
        data = b"picture bytes"
        digest = hashlib.sha256(data).hexdigest()
        rel = f"assets/{digest[:2]}/{digest}.png"
        (root / "home" / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / "home" / rel).write_bytes(data)
        full = "quartz deployment procedure\n" + "gate step\n" * 2000
        S.upsert(conn, S.MemoryItem(slug="quartz", kind="document", title="quartz", body=full,
                                    attachments=[rel]))
        original = S.get(conn, "quartz")
        backup = root / "backup"
        export_all(conn, backup)
        if lost in ("asset", "asset everywhere"):
            (root / "home" / rel).unlink()
        if lost in ("damaged asset", "damaged asset everywhere"):
            (root / "home" / rel).write_bytes(b"damaged bytes")
        if lost in ("dumped asset", "asset everywhere"):
            (backup / rel).unlink()
        if lost == "damaged asset everywhere":
            (backup / rel).write_bytes(b"damaged bytes")
        if lost.startswith("body"):
            (S.docs_dir() / original.body_path).unlink()
        if lost.endswith("twin"):
            # "Quartz" sorts first and takes the name "quartz.md" had
            S.upsert(conn, S.MemoryItem(slug="Quartz", kind="document", title="twin", body="twin"))
        if lost in ("asset", "body", "damaged asset", "body, then a case twin"):
            export_all(conn, backup)      # the weekly export, into the same directory
            assert (backup / rel).read_bytes() == data, "INV-06: a lost asset's backup pruned or replaced"
            dumps = [p.read_bytes() for p in (backup / "document").glob("*.md")]
            assert any(full.encode() in d for d in dumps) and not any(b"truncated: true" in d for d in dumps), \
                "INV-06: a lost body's backup replaced by its excerpt"
        target = S.connect(root / "fresh.db") if destination == "fresh" else conn
        try:
            S.init_schema(target)
            with owner():
                report = import_vault(target, backup, skip_auto_memories=False)
            row = S.get(target, "quartz")
            if lost in ("asset everywhere", "damaged asset everywhere"):
                assert report.failed, "INV-14: a listed attachment found nowhere was dropped"
                assert row is None or row.attachments == [rel]
                return
            assert not report.failed, report.failed
            assert row.attachments == [rel], "INV-06: attachment list"
            assert (root / "home" / rel).read_bytes() == data
            if destination == "fresh":
                assert S.load_body(row) == full, "INV-06: body"
        finally:
            if target is not conn:
                target.close()


@PROPERTY
@given(strength=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False),
       ttl=st.one_of(st.none(), st.integers(-10, 10000), st.floats(-10, 10000, allow_nan=False)),
       kind=st.one_of(st.sampled_from(["skill", "how/to", "../x", "", " ", "Reference", " a  b ",
                                       "-" * 40, "\u0130", "caf\u00e9"]), st.text(max_size=40)),
       empty=st.sets(st.sampled_from(["project", "agent", "source_session"])))
def test_a_value_an_earlier_version_stored_restores(strength, ttl, kind, empty):
    """INV-06: 0.11 stored a strength above STRENGTH_CAP (`strength: 10` in a
    note or dump), 0.10 any strength and TTL; the export carried them and
    upsert refused them, so the record's own dump could not be restored
    (*twentieth review*). 0.10 stored any kind, too (*twenty-first review*),
    and 0.11.3 an empty project, agent or source session as "", which came
    back NULL (*r11 opus review*)."""
    with database() as (conn, root, patch):
        S.upsert(conn, S.MemoryItem(slug="gate", kind="skill", title="deploy gate", body="run the gate"))
        conn.execute("UPDATE memory_items SET strength = ?, ttl_days = ?, kind = ?",
                     (strength, ttl, kind))   # as stored before
        for name in empty:
            conn.execute(f"UPDATE memory_items SET {name} = ''")
        conn.close()
        conn = S.connect(root / "memory.db")
        S.init_schema(conn)   # the next open, by this release
        export_all(conn, root / "dump")
        opened = S.get(conn, "gate")
        conn.close()
        fresh = S.connect(root / "fresh.db")
        try:
            S.init_schema(fresh)
            with owner():
                report = import_vault(fresh, root / "dump", skip_auto_memories=False)
            assert not report.failed, f"INV-06: {report.failed}"
            row = S.get(fresh, "gate")
            assert row.strength == min(max(strength, 0), S.STRENGTH_CAP)
            assert row.ttl_days == (None if ttl is None or int(ttl) < 1 else min(int(ttl), 3650))
            assert row.kind == S._valid_kind(row.kind)
            for name in ("project", "agent", "source_session"):
                assert getattr(row, name) == getattr(opened, name), f"INV-06: {name}"
            try:
                assert row.kind == S._valid_kind(kind), "INV-06: a valid kind was rewritten"
            except ValueError:
                pass
        finally:
            fresh.close()


@PROPERTY
@given(kind=st.sampled_from(["note", "document", "skill"]),
       size=st.sampled_from([10, 6000, 9000]), deleted=st.booleans())
def test_a_body_an_earlier_version_placed_restores(kind, size, deleted):
    """INV-06: whether a body is kept in a file is the kind's and the length's,
    as a restore decides it. Until 0.12.0 a body file was kept on the same text
    (a note once a document was an excerpt), and a document from before body
    files was inline: `get().body` was not what its own dump restored (*r12
    opus review*). The next open by this release moves each body."""
    with database() as (conn, root, patch):
        body = ("word " * size)[:size]
        S.upsert(conn, S.MemoryItem(slug="doc", kind=kind, title="t", body=body))
        row = conn.execute("SELECT * FROM memory_items").fetchone()
        if row["body_path"]:     # as stored before: inline
            conn.execute("UPDATE memory_items SET body = ?, body_path = NULL", (body,))
        else:                    # or kept in a file
            S._stage_body_file(conn, "doc", body, row["content_hash"])
            conn.execute("UPDATE memory_items SET body = ?, body_path = ?", (
                S._make_excerpt(body), S._body_filename(
                    "doc", ns=S._db_namespace(conn), content_hash=row["content_hash"])))
        if deleted:
            conn.execute("UPDATE memory_items SET deleted_at = 1")
        conn.execute("UPDATE meta SET value = '11' WHERE key = 'schema_version'")
        conn.close()
        with closing(S.connect(root / "memory.db")) as conn:
            S.init_schema(conn)   # the next open, by this release
            stored = conn.execute("SELECT body, body_path FROM memory_items").fetchone()
            opened = S.get(conn, "doc") if not deleted else None
            export_all(conn, root / "dump")
            assert S.load_body(S.MemoryItem.from_row(conn.execute(
                "SELECT * FROM memory_items").fetchone())) == body, "INV-06: text"
        fresh = S.connect(root / "fresh.db")
        try:
            S.init_schema(fresh)
            S.upsert(fresh, S.MemoryItem(slug="doc", kind=kind, title="t", body=body))
            placed = fresh.execute("SELECT body, body_path IS NOT NULL FROM memory_items").fetchone()
            assert (stored[0], stored[1] is not None) == tuple(placed), "INV-06: body placement"
            if opened is not None:
                with owner():
                    import_vault(fresh, root / "dump", skip_auto_memories=False)
                assert S.get(fresh, "doc").body == opened.body, "INV-06: get().body"
        finally:
            fresh.close()
