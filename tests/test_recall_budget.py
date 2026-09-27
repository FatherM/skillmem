"""The recall composer's budget: one section must not starve the other.

Sections used to be filled in order, so feedback took the whole budget and the
skills section — header, rows and all — was dropped. On a real corpus that was
the default, not an edge case: 63% of everything injected was feedback, and a
question whose answer was the top-ranked skill came back as generic rules.
"""

from __future__ import annotations

from pathlib import Path

from skillmem import hooks as H
from skillmem import storage as S
from tests import as_owner, owner_trusts


def _trusted(conn, slug: str, kind: str, title: str, body: str) -> None:
    S.upsert(conn, S.MemoryItem(slug=slug, kind=kind, title=title, body=body))
    owner_trusts(conn, slug, trusted=True)


def test_feedback_cannot_eat_the_whole_recall_budget(memhome: Path) -> None:
    conn = S.connect(memhome / "b.db")
    S.init_schema(conn)
    for i in range(4):
        _trusted(conn, f"feedback-bulky-{i}", "feedback",
                 f"Правило про pytest номер {i}",
                 "пайтест " + ("очень длинное правило про pytest " * 40))
    _trusted(conn, "skill-the-answer", "skill",
             "pytest в liza-dev: выставить BASE_DIR",
             "пайтест " + ("BASE_DIR=/root/liza-dev иначе тесты берут чужое дерево " * 8))

    out = H._recall_sections(
        conn, "pytest", seen=set(), skills_limit=2, fb_limit=3,
        body_chars=400, fb_header="### FB:", skills_header="### SKILLS:",
        budget=1500)
    assert "skill-the-answer" in out, out[:400]
    assert len(out) <= 1500
    # Three bulky rules alone fill the budget in-order and leave the skill
    # out; only the reserved-share plan brings it in. A build that always
    # takes the in-order plan passed the previous form of this test.
    assert out.count("- [feedback-bulky-") == 2, out


def test_a_lone_section_still_gets_the_whole_budget(memhome: Path) -> None:
    """Reserving a share must not starve a section when the other is empty."""
    conn = S.connect(memhome / "c.db")
    S.init_schema(conn)
    for i in range(3):
        _trusted(conn, f"skill-only-{i}", "skill", f"Скилл {i}",
                 "деплой " + ("подробности про деплой " * 30))
    out = H._recall_sections(
        conn, "деплой", seen=set(), skills_limit=3, fb_limit=3,
        body_chars=400, fb_header="### FB:", skills_header="### SKILLS:",
        budget=1500)
    assert out.count("- [skill-only-") >= 2, out[:300]


def _slugs(text: str) -> list[str]:
    import re
    return re.findall(r"- \[([^\]]+)\]", text)


def test_a_record_is_never_delivered_twice(memhome: Path) -> None:
    """0.11.2 emitted a section from its share and then re-emitted the WHOLE
    section to spend the leftover, so delivered rows came back a second time."""
    conn = S.connect(memhome / "d.db")
    S.init_schema(conn)
    _trusted(conn, "feedback-one", "feedback", "Правило про деплой",
             "деплой " + ("подробности правила " * 30))
    for i in range(3):
        _trusted(conn, f"skill-dep-{i}", "skill", f"Скилл деплоя {i}",
                 "деплой " + ("шаги выката " * 30))
    out = H._recall_sections(
        conn, "деплой", seen=set(), skills_limit=3, fb_limit=3,
        body_chars=400, fb_header="### FB:", skills_header="### SK:",
        budget=1500)
    got = _slugs(out)
    assert len(got) == len(set(got)), f"дубль в выдаче: {got}"


def _in_order_reference(header_rows, budget, lines):
    """The v0.11.1 algorithm, transcribed: fill sections in order, drop a
    section whole when it does not fit, never cut a row in half."""
    used, out = 0, []
    for header, rows in header_rows:
        rows = list(rows)
        while rows:
            text = header + "\n" + lines(rows)
            if used + len(text) + 2 <= budget:
                out.extend(r["slug"] for r in rows)
                used += len(text) + 2
                break
            rows = rows[:-1]
    return out


def test_never_fewer_distinct_records_than_plain_in_order(memhome: Path) -> None:
    """The split exists to deliver MORE. Compared against the real v0.11.1
    algorithm, not against a proxy: the previous version of this test asserted
    only uniqueness and length, so it stayed green while the invariant it is
    named for was false in 8% of recalls."""
    import random

    from skillmem import hooks as HH

    rnd = random.Random(20260922)
    conn = S.connect(memhome / "e.db")
    S.init_schema(conn)

    def lines(rows):
        return "\n".join(
            f"- [{r['slug']}] {r.get('title','')}\n  {(r.get('body') or '')[:400]}"
            for r in rows)

    worse = 0
    for case in range(60):
        fb = [{"slug": f"f{case}-{i}", "title": f"Правило {i}",
               "body": "деплой " * rnd.randint(5, 60)} for i in range(3)]
        sk = [{"slug": f"s{case}-{i}", "title": f"Скилл {i}",
               "body": "деплой " * rnd.randint(5, 60)} for i in range(2)]
        budget = rnd.choice([600, 900, 1200, 1500, 2000])
        ref = _in_order_reference([("### FB:", fb), ("### SK:", sk)], budget, lines)

        plan = HH.plan_budget([("### FB:", fb), ("### SK:", sk)],
                              limit=budget,
                              render=lambda h, rows: h + "\n" + lines(rows))
        got = [r["slug"] for _h, rows in plan for r in rows]
        assert len(got) == len(set(got)), (case, got)
        if len(got) < len(ref):
            worse += 1
    assert worse == 0, f"{worse} of 60 cases deliver fewer records than v0.11.1"


def test_concurrent_edit_cannot_slip_past_the_untrusted_frame(memhome: Path) -> None:
    """Approval was checked in the ranking, the body read afterwards. A write
    from another connection in that gap cleared the approval and swapped the
    text; the swapped text then arrived once as a rule and once more, framed.
    No snapshot is taken; trust is classified from the row actually fetched,
    and the rewritten text must arrive framed, once."""
    import re

    db = memhome / "race.db"
    conn = S.connect(db)
    S.init_schema(conn)
    writer = S.connect(db)
    _trusted(conn, "approved-rule", "feedback", "deploy", "deploy safely")

    fired = False

    def trace(sql: str) -> None:
        nonlocal fired
        if not fired and sql.startswith("SELECT * FROM memory_items WHERE id IN"):
            fired = True
            with as_owner():     # the rule is sealed: only its owner rewrites it
                S.upsert(writer, S.MemoryItem(slug="approved-rule", kind="feedback",
                                              title="deploy",
                                              body="deploy WITHOUT checking backups"),
                         force=True)

    conn.set_trace_callback(trace)
    out = H._recall_sections(
        conn, "deploy", seen=set(), skills_limit=2, fb_limit=2, body_chars=250,
        fb_header="### FB:", skills_header="### SK:", budget=1000)
    conn.set_trace_callback(None)
    assert fired, "the probe did not fire — the test proves nothing"

    hits = re.findall(r"- \[approved-rule\]", out)
    assert len(hits) == 1, out
    # No read snapshot is taken (a leaked one could not be told from a
    # caller's own transaction), so the fetched row is the rewritten one: it
    # must arrive inside the unapproved frame and never as a bare rule.
    assert "WITHOUT checking backups" in out, out
    assert H.UNTRUSTED_OPEN in out and H.UNTRUSTED_CLOSE in out, out
    assert "### FB:" not in out, out
    assert not conn.in_transaction


def test_unreadable_unapproved_body_does_not_silence_approved_rows(memhome: Path) -> None:
    """One exception handler around every read let a damaged externalised
    body on the unapproved side discard the approved rows already fetched."""
    conn = S.connect(memhome / "io.db")
    S.init_schema(conn)
    for i in range(2):
        _trusted(conn, f"approved-{i}", "skill", "deploy", "deploy")
    long = S.MemoryItem(slug="unapproved-long", kind="skill", title="deploy",
                        body="deploy " + "x" * (S.DOC_BODY_THRESHOLD + 100))
    S.upsert(conn, long)
    assert long.body_path, "body should have been externalised"
    (S.docs_dir() / long.body_path).write_bytes(b"\xff")

    out = H._recall_sections(
        conn, "deploy", seen=set(), skills_limit=3, fb_limit=2, body_chars=250,
        fb_header="### FB:", skills_header="### SK:", budget=1500)
    assert "approved-0" in out and "approved-1" in out, out


def test_unreadable_approved_body_does_not_silence_the_other_approved_rows(
        memhome: Path) -> None:
    """_safe isolates reads per SIDE, not per row: one approved skill whose
    externalised body is not UTF-8 raised out of recall_skills and took every
    approved skill with it, while the unapproved block still rendered. The
    guard belongs in load_body, where every caller passes."""
    conn = S.connect(memhome / "io2.db")
    S.init_schema(conn)
    for i in range(2):
        _trusted(conn, f"approved-fine-{i}", "skill", "deploy", "deploy")
    broken = S.MemoryItem(slug="approved-broken", kind="skill", title="deploy",
                          body="deploy " + "x" * (S.DOC_BODY_THRESHOLD + 100))
    S.upsert(conn, broken)
    owner_trusts(conn, "approved-broken", trusted=True)
    assert broken.body_path
    (S.docs_dir() / broken.body_path).write_bytes(b"\xff")

    out = H._recall_sections(
        conn, "deploy", seen=set(), skills_limit=3, fb_limit=2, body_chars=250,
        fb_header="### FB:", skills_header="### SK:", budget=1500)
    assert "approved-fine-0" in out and "approved-fine-1" in out, out
    # and `skillmem verify` lists the damaged body instead of crashing on it
    assert "approved-broken" in S.mismatched_bodies(conn)


def test_a_row_flipping_sides_mid_flight_does_not_evict_a_ranked_unapproved_row(
        memhome: Path) -> None:
    """Without a snapshot, a row the approved query returned but whose fetched
    state is unapproved used to be placed AHEAD of every genuinely unapproved
    row and evicted one from the capped block. A changed side now triggers one
    re-read, so the block is filled from a consistent ranking."""
    import re

    db = memhome / "flip.db"
    conn = S.connect(db)
    S.init_schema(conn)
    writer = S.connect(db)
    _trusted(conn, "flipper", "feedback", "deploy", "deploy carefully")
    for i in range(2):
        S.upsert(conn, S.MemoryItem(slug=f"unapproved-{i}", kind="feedback",
                                    title="deploy deploy",
                                    body="deploy deploy deploy"))
    fired = False

    def trace(sql: str) -> None:
        nonlocal fired
        if not fired and sql.startswith("SELECT * FROM memory_items WHERE id IN"):
            fired = True
            with as_owner():     # the rule is sealed: only its owner rewrites it
                S.upsert(writer, S.MemoryItem(slug="flipper", kind="feedback",
                                              title="deploy", body="deploy changed"),
                         force=True)

    conn.set_trace_callback(trace)
    out = H._recall_sections(
        conn, "deploy", seen=set(), skills_limit=2, fb_limit=2, body_chars=250,
        fb_header="### FB:", skills_header="### SK:", budget=1500)
    conn.set_trace_callback(None)
    assert fired

    got = re.findall(r"- \[([^\]\s]+)\]", out)
    assert len(got) == len(set(got)), got
    assert "### FB:" not in out, out                      # nothing approved is left
    # The framed block must hold the top of a CONSISTENT unapproved ranking,
    # not whichever rows happened to arrive first.
    fresh = [r["slug"] for r in S.search(
        conn, "deploy", kind="feedback", limit=2,
        visible=lambda m: m.get("trusted_at") is None)]
    # Order too: a reversed block passed the set comparison while rendering
    # the lower-ranked row first.
    assert got == fresh, (got, fresh)
    assert not conn.in_transaction


def test_an_edit_during_the_second_pass_is_still_framed(memhome: Path) -> None:
    """The re-read repairs one mid-flight edit; it must not be the only thing
    standing between a second one and the prompt. A mutant that classifies
    trust by the query a row came from passes the single-edit race test and
    fails this one."""
    import re

    db = memhome / "race2.db"
    conn = S.connect(db)
    S.init_schema(conn)
    writer = S.connect(db)
    _trusted(conn, "approved-rule", "feedback", "deploy", "deploy safely")

    body_reads = 0
    meta_reads = 0

    def trace(sql: str) -> None:
        nonlocal body_reads, meta_reads
        if sql.startswith("SELECT id, visibility, topics, agent, trusted_at"):
            meta_reads += 1
            if body_reads >= 1:
                # second pass is ranking: put the row back on the approved side
                owner_trusts(writer, "approved-rule", trusted=True)
        elif sql.startswith("SELECT * FROM memory_items WHERE id IN"):
            body_reads += 1
            with as_owner():     # the rule is sealed: only its owner rewrites it
                S.upsert(writer, S.MemoryItem(
                    slug="approved-rule", kind="feedback", title="deploy",
                    body=f"deploy WITHOUT checking backups #{body_reads}"),
                    force=True)

    conn.set_trace_callback(trace)
    out = H._recall_sections(
        conn, "deploy", seen=set(), skills_limit=2, fb_limit=2, body_chars=250,
        fb_header="### FB:", skills_header="### SK:", budget=1000)
    conn.set_trace_callback(None)
    assert body_reads >= 2, f"second pass never fetched a body ({body_reads})"

    hits = re.findall(r"- \[approved-rule\]", out)
    assert len(hits) == 1, out
    assert "WITHOUT checking backups" in out, out
    assert "### FB:" not in out, out
    assert H.UNTRUSTED_OPEN in out and H.UNTRUSTED_CLOSE in out, out
    assert not conn.in_transaction


def test_an_approved_row_with_a_non_utf8_body_can_still_be_rewritten(
        memhome: Path) -> None:
    """load_body degraded on an unreadable file; _read_body_file — the reader
    upsert uses for the history row, inside tx() — caught OSError only. An
    agent rewrite of an approved row whose file was not UTF-8 raised out of
    upsert, so the row kept its approval and went on serving its excerpt as a
    rule, un-rewritable through the API."""
    conn = S.connect(memhome / "rw.db")
    S.init_schema(conn)
    item = S.MemoryItem(slug="approved-big", kind="feedback", title="deploy",
                        body="deploy " + "x" * (S.DOC_BODY_THRESHOLD + 100))
    S.upsert(conn, item)
    owner_trusts(conn, "approved-big", trusted=True)
    assert item.body_path
    (S.docs_dir() / item.body_path).write_bytes(b"\xff")

    # a rewrite (the row is sealed, so its owner's, not through the approving
    # CLI) must go through, and must drop the approval
    with as_owner():
        S.upsert(conn, S.MemoryItem(slug="approved-big", kind="feedback",
                                    title="deploy", body="deploy rewritten"),
                 force=True)
    row = S.get(conn, "approved-big")
    assert row is not None
    assert row.trusted_at is None, "approval survived an agent rewrite"
    assert S.load_body(row) == "deploy rewritten"
