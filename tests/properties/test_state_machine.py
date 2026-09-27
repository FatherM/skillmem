"""An independent small model of text, deletion, visibility and approval."""
from dataclasses import dataclass
from hashlib import sha256
import json

import pytest
from click.testing import CliRunner
from hypothesis import given, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from skillmem import cli, mcp_server as M, storage as S
from skillmem.export import export_all
from skillmem.packs import import_pack, remove_pack
from skillmem.vault import import_vault
from .support import PROPERTY, database, owner, put

SLUG = st.sampled_from(["a", "b", "c"])
TEXT = st.text(alphabet="abc 日本語\r\n", min_size=1, max_size=20).map(lambda s: "quartz " + s)


@dataclass
class Record:
    title: str
    body: str
    kind: str
    live: bool = True
    archived: bool = False
    approved: str | None = None
    sealed: bool = False


class MemoryMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.scope = database()
        self.conn, self.root, self.patch = self.scope.__enter__()
        self.patch.setattr(M, "_shared_conn", lambda: self.conn)
        self.model = {}
        self.serial = 0

    @initialize()
    def seed(self):
        # One live row makes lifecycle/approval transitions useful immediately;
        # the other two slugs remain available for create/tombstone sequences.
        put(self.conn, slug="a", body="quartz a")
        self.model["a"] = Record("quartz", "quartz a", "skill")

    def teardown(self):
        self.scope.__exit__(None, None, None)

    @rule(slug=SLUG, body=TEXT, surface=st.sampled_from(["storage", "cli", "mcp"]),
          action=st.sampled_from(["write", "update", "learn"]))
    def write(self, slug, body, surface, action):
        old = self.model.get(slug)
        title = "quartz"
        expected = S.skill_body(body, body, "success") if action == "learn" else body
        kind = "skill" if action == "learn" else (old.kind if old else "note")
        if surface == "storage":
            try:
                fn = S.upsert_skill if action == "learn" else S.upsert
                fn(self.conn, S.MemoryItem(slug=slug, title=title, body=expected, kind=kind),
                   reason="edit" if action == "update" else None, explicit=set())
                success = True
            except (S.MemoryConflict, S.SealedRecord):
                success = False
        elif surface == "cli":
            args = ["learn", slug, "--title", title, "--trigger", body, "--steps", body,
                    "--outcome", "success"] if action == "learn" else [
                    "write", "--slug", slug, "--title", title, "--body", body]
            if action == "update":
                args += ["--reason", "edit"]
            result = CliRunner().invoke(cli.main, args)
            success = result.exit_code == 0
            if not success:
                assert isinstance(result.exception, (SystemExit, S.MemoryConflict, S.SealedRecord)), result.exception
        else:
            args = dict(slug=slug, title=title, body=body, trigger=body, steps=body,
                        outcome="success", reason="edit", check_conflicts=False)
            result = getattr(M, "_tool_" + action)(args)
            success = not isinstance(result, M._Err)
        if success:
            row = S.get(self.conn, slug)
            assert row and S.load_body(row) == expected, "INV-08: acknowledged text invisible"
            assert row.lifecycle != "archived", "INV-08: acknowledged write remains archived"
            assert not (old and old.sealed and (old.title, old.body) != (title, expected)), "INV-03: agent changed sealed text"
            approved = old.approved if old and (old.title, old.body) == (title, expected) else None
            self.model[slug] = Record(title, expected, kind, approved=approved,
                                      sealed=bool(old and old.sealed))

    @rule(slug=SLUG)
    def owner_approve(self, slug):
        old = self.model.get(slug)
        if old and old.live and not old.archived:
            expected = sha256(json.dumps([old.title, old.body], ensure_ascii=False).encode()).hexdigest()
            with owner():
                S.set_trust(self.conn, slug, trusted=True, expect_hash=expected)
            old.approved, old.sealed = expected, True

    @rule(slug=SLUG, action=st.sampled_from(["archive", "restore", "delete", "pin", "reinforce"]),
          as_owner=st.booleans())
    def lifecycle(self, slug, action, as_owner):
        old = self.model.get(slug)
        if not old or not old.live:
            return
        def apply():
            if action == "delete":
                return S.soft_delete(self.conn, slug, "property")
            if action == "pin":
                return S.set_pinned(self.conn, slug, True)
            if action == "reinforce":
                return S.reinforce(self.conn, slug, evidence="test_passed")
            return S.set_archived(self.conn, slug, action == "archive")
        before = S.get(self.conn, slug)
        try:
            if as_owner:
                with owner():
                    result = apply()
            else:
                result = apply()
        except (S.SealedRecord, S.MemoryConflict, ValueError):
            return
        after = S.get(self.conn, slug)
        if old.sealed and not as_owner:
            assert (after is not None and (after.pinned, after.lifecycle) ==
                    (before.pinned, before.lifecycle)), "INV-03: agent changed sealed lifecycle/pin"
        if result and action == "delete":
            old.live = False
        elif result and action in {"archive", "restore"}:
            old.archived = action == "archive"

    @rule(slug=SLUG)
    def owner_restore_deleted(self, slug):
        old = self.model.get(slug)
        if not old or old.live:
            return
        with owner():
            S.upsert(self.conn, S.MemoryItem(slug=slug, kind=old.kind, title=old.title, body=old.body),
                     revive=True, reason="owner restore", explicit=set())
        old.live = True

    @rule(slug=SLUG)
    def owner_revoke(self, slug):
        old = self.model.get(slug)
        if old and old.live:
            with owner():
                S.set_trust(self.conn, slug, trusted=False)
            old.approved = None

    @rule()
    def owner_vault_roundtrip(self):
        self.serial += 1
        path = self.root / f"dump-{self.serial}"
        export_all(self.conn, path)
        with owner():
            report = import_vault(self.conn, path, skip_auto_memories=False)
        assert not report.failed, f"INV-06: {report.failed}"

    @rule(body=TEXT)
    def pack_import(self, body):
        path = self.root / "pack"
        path.mkdir(exist_ok=True)
        (path / "SKILL.md").write_text("---\nname: demo\ndescription: quartz\n---\n" + body, encoding="utf-8")
        report = import_pack(self.conn, str(path), pack_name="local")
        for slug in report.imported:
            row = S.get(self.conn, slug)
            assert row and row.lifecycle != "archived", "INV-08: imported row invisible"
            assert row.trusted_at is None and not row.owner_seal, "INV-02: pack minted trust"
            # The importer adds a provenance trailer. Model that surface's format.
            from skillmem.packs import _provenance, read_pack
            skill = read_pack(path)[0]
            self.model[slug] = Record("[local] quartz", skill.body + "\n" + _provenance(report, skill), "skill")

    @invariant()
    def model_matches(self):
        for slug, expected in self.model.items():
            row = S.get(self.conn, slug)
            assert (row is not None) == expected.live, "INV-04: by-slug liveness"
            if row:
                assert (row.title, S.load_body(row)) == (expected.title, expected.body), "INV-08: model text"
                assert (row.lifecycle == "archived") == expected.archived, "INV-04: model lifecycle"
                assert row.trusted_at is None or row.content_hash == expected.approved, "INV-01: approval hash"
                assert bool(row.owner_seal) == expected.sealed, "INV-02: owner-only seal"
                tip = self.conn.execute("SELECT reason FROM memory_history WHERE slug = ? "
                                        "ORDER BY id DESC LIMIT 1", (slug,)).fetchone()
                assert not (tip and tip[0].startswith("deleted")), "INV-13: a revive left no history row"
                assert sha256(json.dumps([row.title, S.load_body(row)], ensure_ascii=False).encode()).hexdigest() == row.content_hash, "INV-15: verified body"
        for rows in (S.list_items(self.conn, limit=100), S.search(self.conn, "quartz", limit=100),
                     S.recall_skills(self.conn, "quartz", limit=100, auto_reinforce=False)):
            for row in rows:
                slug = row["slug"] if isinstance(row, dict) else row.slug
                expected = self.model[slug]
                assert expected.live and not expected.archived, "INV-04: ranked liveness"


TestMemoryMachine = MemoryMachine.TestCase
TestMemoryMachine.settings = settings(PROPERTY, stateful_step_count=20)


@pytest.mark.parametrize("route", ["item_insert", "item_same", "item_changed", "set_trust"])
@PROPERTY
@given(stamp=st.integers(min_value=1, max_value=1000))
def test_agent_cannot_mint_approval(stamp, route):
    with database() as (conn, _, __):
        try:
            if route.startswith("item_"):
                if route != "item_insert":
                    put(conn)
                S.upsert(conn, S.MemoryItem(slug="record", kind="skill", title="quartz",
                    body="changed" if route == "item_changed" else "quartz deployment procedure",
                    trusted_at=stamp, trusted_by="forged"),
                    reason="edit" if route == "item_changed" else None, explicit=set())
            else:
                put(conn)
                S.set_trust(conn, "record", trusted=True)
        except (S.SealedRecord, S.MemoryConflict, ValueError):
            pass
        row = S.get(conn, "record")
        assert row is None or (row.trusted_at is None and not row.owner_seal), "INV-01/02: caller minted trust/seal"


@pytest.mark.parametrize("surface", ["storage", "cli", "mcp", "vault", "pack"])
@pytest.mark.parametrize("change", ["text", "metadata"])
@PROPERTY
@given(body=TEXT)
def test_sealed_record_is_immutable_to_agents(surface, change, body):
    with database() as (conn, root, patch):
        slug = "pack-local-demo" if surface == "pack" else "record"
        pack = root / "pack"
        pack.mkdir()
        (pack / "SKILL.md").write_text("---\nname: demo\ndescription: quartz\n---\noriginal")
        if surface == "pack":
            import_pack(conn, str(pack), pack_name="local")
        else:
            put(conn, slug=slug, body="original")
        with owner():
            S.set_trust(conn, slug, trusted=True)
        patch.setattr(M, "_shared_conn", lambda: conn)
        before = S.get(conn, slug).to_dict()
        try:
            if surface == "storage":
                item = S.MemoryItem(slug=slug, kind="skill", title="quartz",
                    body=body if change == "text" else "original", project="changed")
                S.upsert(conn, item, reason="edit", explicit={"project"})
            elif surface == "mcp":
                M._tool_update(dict(slug=slug, body=body if change == "text" else "original",
                                    project="changed", reason="edit"))
            elif surface == "cli":
                result = CliRunner().invoke(cli.main, ["write", "--slug", slug, "--title", "quartz",
                    "--body", body if change == "text" else "original", "--project", "changed", "--reason", "edit"])
                assert result.exit_code in (0, 1, 2), result.output  # 2: write's refusal
            elif surface == "vault":
                export_all(conn, root / "dump")
                path = next((root / "dump").rglob("*.md"))
                content = path.read_bytes().decode("utf-8")
                if change == "text":
                    # Keep exact dump frontmatter and replace only body bytes.
                    content = content.rsplit("\n---\n\n", 1)[0] + "\n---\n\n" + body + "\n"
                else:
                    content = content.replace("\ncreated_at:", "\nproject: changed\ncreated_at:")
                path.write_bytes(content.encode("utf-8"))
                import_vault(conn, root / "dump", skip_auto_memories=False)
            else:
                (pack / "SKILL.md").write_text("---\nname: demo\ndescription: " +
                    ("changed" if change == "metadata" else "quartz") + "\n---\n" + body)
                import_pack(conn, str(pack), pack_name="local")
        except (S.SealedRecord, S.MemoryConflict):
            pass
        after = S.get(conn, slug).to_dict()
        protected = ("title", "body", "kind", "project", "visibility", "tags", "topics", "ttl_days",
                     "agent", "attachments", "source_session", "pinned", "lifecycle", "owner_seal")
        assert {k: after[k] for k in protected} == {k: before[k] for k in protected}, "INV-03: non-owner changed sealed record"


@pytest.mark.parametrize("action", ["pin", "restore", "archive", "delete", "sweep", "failure", "decay"])
@PROPERTY
@given(body=TEXT)
def test_sealed_lifecycle_changes_need_owner(action, body):
    with database() as (conn, _, __):
        # idle past the stale cut, so the sweep has something to decide
        put(conn, body=body, created_at=S._now() - (S.STALE_AFTER_DAYS + 1) * 86400)
        with owner():
            S.set_trust(conn, "record", trusted=True)
            if action == "restore":
                S.set_archived(conn, "record", True)
        before = S.get(conn, "record")
        try:
            if action == "pin":
                S.set_pinned(conn, "record", True)
            elif action == "sweep":
                S.sweep_lifecycle(conn)
            elif action == "decay":
                # the nightly job, idle past its threshold
                S.decay_stale(conn)
            elif action == "failure":
                # an agent's evidence walked the rule below tool-recall's floor
                for _ in range(5):
                    S.reinforce(conn, "record", evidence="failure")
            elif action == "delete":
                S.soft_delete(conn, "record", "agent deletion")
            else:
                S.set_archived(conn, "record", action == "archive")
        except (S.SealedRecord, S.MemoryConflict):
            pass
        after = S.get(conn, "record")
        assert after is not None and (after.pinned, after.lifecycle, after.strength) == (before.pinned, before.lifecycle, before.strength), "INV-03: sealed lifecycle changed"


@pytest.mark.parametrize("command", [["rm", "record", "--reason", "agent"],
                                     ["uninstall", "--purge-db", "--no-claude-code", "--no-codex",
                                      "--no-editors"]])
def test_no_cli_command_deletes_a_sealed_record_without_the_owner(command):
    """INV-03: `uninstall --purge-db` deleted the database file, sealed rows
    and their body files with them, with no terminal."""
    from skillmem import schedule
    with database() as (conn, root, patch):
        patch.setattr(schedule, "_backend", lambda: (None, lambda: []))   # no real scheduler
        put(conn, body="quartz deployment procedure\n" * 1000)
        with owner():
            S.set_trust(conn, "record", trusted=True)
        body = S.docs_dir() / S.get(conn, "record").body_path
        result = CliRunner().invoke(cli.main, ["--db", str(root / "memory.db"), *command])
        assert result.exit_code != 0 and "no TTY" in result.output, result.output
        assert (root / "memory.db").exists() and body.exists(), "INV-03: a sealed record was deleted"
        conn.close()     # the file --purge-db deletes: Windows refuses one held open
        with owner():
            result = CliRunner().invoke(cli.main, ["--db", str(root / "memory.db"), *command])
        assert result.exit_code == 0, result.output


@PROPERTY
@given(body=TEXT)
def test_agent_cannot_forge_owner_call_to_revive(body):
    with database() as (conn, _, __):
        put(conn)
        with owner():
            S.set_trust(conn, "record", trusted=True)
            S.soft_delete(conn, "record", "owner removed")
        try:
            # owner_call= is gone (INV-02); what a caller can still claim is on the item
            S.upsert(conn, S.MemoryItem(slug="record", kind="skill", title="quartz", body=body,
                                        origin="owner", trusted_at=1, trusted_by="forged"),
                     revive=True, reason="forged owner", explicit=set())
        except (S.SealedRecord, S.MemoryConflict):
            pass
        assert S.get(conn, "record") is None, "INV-02/03: caller flag revived sealed tombstone"


@pytest.mark.parametrize("surface", ["storage", "cli", "mcp"])
@pytest.mark.parametrize("action", ["write", "update", "learn"])
@PROPERTY
@given(body=TEXT)
def test_acknowledged_archived_write_is_visible(surface, action, body):
    with database() as (conn, _, patch):
        initial = S.skill_body(body, body, "success") if action == "learn" else body
        S.upsert(conn, S.MemoryItem(slug="record", kind="skill", title="quartz", body=initial))
        S.set_archived(conn, "record", True)
        patch.setattr(M, "_shared_conn", lambda: conn)
        expected = body + " edited" if action == "update" else initial
        try:
            if surface == "storage":
                fn = S.upsert_skill if action == "learn" else S.upsert
                fn(conn, S.MemoryItem(slug="record", kind="skill", title="quartz", body=expected),
                   reason="edit" if action == "update" else None, explicit=set())
                success = True
            elif surface == "mcp":
                result = getattr(M, "_tool_" + action)(dict(slug="record", title="quartz", body=expected,
                    trigger=body, steps=body, outcome="success", reason="edit", check_conflicts=False))
                success = not isinstance(result, M._Err)
            else:
                args = ["learn", "record", "--title", "quartz", "--trigger", body, "--steps", body,
                        "--outcome", "success"] if action == "learn" else ["write", "--slug", "record",
                        "--title", "quartz", "--body", expected, "--no-check-conflicts"]
                if action == "update":
                    args += ["--reason", "edit"]
                result = CliRunner().invoke(cli.main, args)
                assert result.exit_code in (0, 1, 2), result.output  # 2: write's refusal
                success = result.exit_code == 0
        except (S.MemoryConflict, S.SealedRecord):
            success = False
        if success:
            row = S.get(conn, "record")
            assert row and row.lifecycle != "archived" and S.load_body(row) == expected, "INV-08: acknowledged archived write is invisible"


@pytest.mark.parametrize("removed", [False, True])
@PROPERTY
@given(body=TEXT)
def test_pack_acknowledgement_is_visible(body, removed):
    with database() as (conn, root, _):
        pack = root / "pack"
        pack.mkdir()
        (pack / "SKILL.md").write_text("---\nname: demo\ndescription: quartz\n---\n" + body)
        import_pack(conn, str(pack), pack_name="local")
        S.set_archived(conn, "pack-local-demo", True)
        if removed:
            # the tombstone of an archived row: a revive left it archived
            remove_pack(conn, "local", reason="uninstall")
        report = import_pack(conn, str(pack), pack_name="local")
        for slug in report.imported:
            row = S.get(conn, slug)
            assert row and row.lifecycle != "archived", "INV-08: pack acknowledged invisible import"


@pytest.mark.parametrize("surface", ["storage", "vault", "pack"])
@pytest.mark.parametrize("same_text", [True, False])
@PROPERTY
@given(body=TEXT)
def test_a_revive_appends_its_history_row(surface, same_text, body):
    """INV-13: bringing a tombstone back is a transition; a live record's
    chain never ends in its deletion."""
    with database() as (conn, root, patch):
        pack = root / "pack"
        pack.mkdir()
        skill = pack / "SKILL.md"
        skill.write_text("---\nname: demo\ndescription: quartz\n---\n" + body, encoding="utf-8")
        if surface == "pack":
            slug = import_pack(conn, str(pack), pack_name="local").imported[0]
        else:
            slug = put(conn, body=body).slug
            export_all(conn, root / "dump")
        S.soft_delete(conn, slug, "gone")
        with owner():
            if surface == "storage":
                S.upsert(conn, S.MemoryItem(slug=slug, kind="skill", title="quartz",
                                            body=body if same_text else body + " edited"),
                         revive=True, reason="restore", explicit=set())
            elif surface == "vault":
                if not same_text:
                    dump = next((root / "dump").rglob("*.md"))
                    dump.write_bytes(dump.read_bytes().rstrip(b"\n") + b" edited\n")
                assert not import_vault(conn, root / "dump", skip_auto_memories=False).failed
            else:
                if not same_text:
                    skill.write_text(skill.read_text(encoding="utf-8") + " edited", encoding="utf-8")
                import_pack(conn, str(pack), pack_name="local")
        assert S.get(conn, slug) is not None, "test premise: revived"
        reasons = [r[0] for r in conn.execute(
            "SELECT reason FROM memory_history WHERE slug = ? ORDER BY id", (slug,))]
        assert not reasons[-1].startswith("deleted"), f"INV-13: revive left no history row: {reasons}"
        assert S.verify_history(conn)[1] == [], "INV-13: chain broken"


@pytest.mark.parametrize("surface,action", [("mcp", "pin"), ("mcp", "failure"), ("http", "failure")])
@PROPERTY
@given(body=TEXT)
def test_an_agent_surface_is_never_the_owner(surface, action, body):
    """INV-03: a server started in the owner's terminal has a TTY, and every
    request it serves is still an agent's."""
    from skillmem import server as W
    with database() as (conn, root, patch):
        put(conn, body=body, visibility="public")
        with owner():
            S.set_trust(conn, "record", trusted=True)
        before = S.get(conn, "record")
        patch.setattr(S, "owner_present", lambda: True)
        patch.setattr(M, "_shared_conn", lambda: conn)
        tokens = root / "tokens.yaml"
        tokens.write_text("boss:\n  token: test\n  scope: master\n")
        app = W.build_app(W.TokenStore(tokens), db_path=root / "memory.db")
        endpoint = next(r.endpoint for r in app.routes if r.path == "/reinforce/{slug:path}")
        for _ in range(5):
            try:
                if surface == "http":
                    endpoint("record", "failure", W.AgentIdentity("boss", "test", scope="master"))
                elif action == "pin":
                    M._tool_pin({"slug": "record", "pinned": not before.pinned})
                else:
                    M._tool_reinforce({"slug": "record", "evidence": "failure"})
            except S.SealedRecord:
                pass
        after = S.get(conn, "record")
        assert (after.pinned, after.strength) == (before.pinned, before.strength), "INV-03: agent surface acted as the owner"


@pytest.mark.parametrize("source", ["note", "library"])
@PROPERTY
@given(strength=st.one_of(st.floats(), st.sampled_from([0.0, S.STRENGTH_CAP, 10.0])),
       evidence=st.sampled_from(sorted(S.EVIDENCE_WEIGHTS)))
def test_agent_evidence_never_lowers_a_sealed_rule(source, strength, evidence):
    """INV-03: strength lives in 0..STRENGTH_CAP, so no agent's evidence moves
    a sealed rule down (a value above the cap was "capped" by a self_report)."""
    with database() as (conn, root, _):
        in_domain = 0 <= strength <= S.STRENGTH_CAP
        with owner():
            if source == "note":
                (root / "vault").mkdir()
                (root / "vault" / "record.md").write_text(
                    f"---\nname: record\nmetadata:\n  type: skill\nstrength: {strength!r}\n---\n"
                    "quartz deployment procedure\n", encoding="utf-8")
                report = import_vault(conn, root / "vault")
                assert bool(report.failed) != in_domain, f"INV-03: strength {strength!r} {report}"
            else:
                try:
                    put(conn, strength=strength)
                except ValueError:
                    assert not in_domain, "INV-03: a valid strength was refused"
            if S.get(conn, "record") is None:
                return
            S.set_trust(conn, "record", trusted=True)
        before = S.get(conn, "record")
        assert 0 <= before.strength <= S.STRENGTH_CAP, "INV-03: strength outside its domain"
        for _ in range(3):
            S.reinforce(conn, "record", evidence=evidence)
        assert S.get(conn, "record").strength >= before.strength, "INV-03: agent evidence lowered a sealed rule"


@PROPERTY
@given(strength=st.floats(0, S.STRENGTH_CAP), evidence=st.sampled_from(sorted(S.EVIDENCE_WEIGHTS)))
def test_evidence_moves_strength_only_its_way(strength, evidence):
    """Failure never raises strength, outside evidence never lowers it and a
    self_report leaves it alone. A failure raised a strength below the decay
    floor up to the floor (*ninth review:* 0.01 became 0.05)."""
    with database() as (conn, _, _):
        put(conn, strength=strength)
        before = S.get(conn, "record").strength
        S.reinforce(conn, "record", evidence=evidence)
        after = S.get(conn, "record").strength
        weight = S.EVIDENCE_WEIGHTS[evidence]
        if evidence == "failure":
            assert after <= before, f"failure raised strength {before} -> {after}"
        else:
            assert (after >= before) if weight > 0 else (after == before), f"{evidence}: {before} -> {after}"


@pytest.mark.parametrize("action", ["edit", "archive", "delete", "sweep", "revive"])
@PROPERTY
@given(damaged=st.booleans())
def test_history_records_the_whole_replaced_text(action, damaged):
    """INV-13: every history row's old_body is the version replaced: an
    externalised body's verified file, or its excerpt when the file does not
    verify, headed by the excerpt notice (never unverified bytes)."""
    with database() as (conn, root, _):
        full = "quartz long procedure line\n" * 800
        row = put(conn, body=full, created_at=S._now() - (S.STALE_AFTER_DAYS + 1) * 86400)
        assert row.body_path, "test premise: the body is externalised"
        if damaged:
            (S.docs_dir() / row.body_path).write_text("tampered", encoding="utf-8")
        if action == "edit":
            S.upsert(conn, S.MemoryItem(slug="record", kind="skill", title="quartz", body="new"),
                     reason="edit", explicit=set())
        elif action == "archive":
            S.set_archived(conn, "record", True)
        elif action == "delete":
            S.soft_delete(conn, "record", "gone")
        elif action == "sweep":
            S.sweep_lifecycle(conn)
        else:
            S.soft_delete(conn, "record", "gone")
            S.upsert(conn, S.MemoryItem(slug="record", kind="skill", title="quartz", body=full),
                     revive=True, explicit=set())
        entries = S.history(conn, "record")
        assert entries, f"test premise: {action} appends a history row"
        # the verified text, or the excerpt only when the file does not verify,
        # headed by the notice that says so (INV-15)
        assert entries[0]["old_body"] == full or (
            damaged and entries[0]["old_body"] == S.EXCERPT_NOTICE + row.body), \
            "INV-13: history recorded an excerpt of an intact file, or unverified bytes"
