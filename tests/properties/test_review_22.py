"""Twenty-second review: INV-01 (a kind is part of what was approved), INV-08
(a note or skill left out is reported), INV-12 (one database's jobs and its
upgrade backup are its own), INV-14 (a frontmatter string is a string) and
INV-16 (a stored token goes to its repository alone)."""
import contextlib
import plistlib
import shlex
import sqlite3
from subprocess import CompletedProcess

import pytest
from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import cli, migrate, packs, schedule, storage as S, vault
from .support import PROPERTY, database, owner

KINDS = ["note", "feedback", "skill", "reference", "user", "document"]
NAMING_KIND = sorted(s for s, p in S.SURFACES.items() if "kind" in p["names"])


@pytest.mark.parametrize("surface", NAMING_KIND)
@PROPERTY
@given(old=st.sampled_from(KINDS), new=st.sampled_from(KINDS),
       owner_now=st.booleans(), sealed=st.booleans())
def test_approval_is_bound_to_the_kind_approved(surface, old, new, owner_now, sealed):
    """A same-text write that changes the kind keeps approval only when it is
    itself the owner's approving write: the owner's routine `migrate` of an
    agent's file turned an approved note into an injected rule."""
    with database() as (conn, root, patch):
        with owner():
            S.upsert(conn, S.MemoryItem(slug="deploy", kind=old, title="Deploy note",
                                        body="use the staging gate before deploys",
                                        origin="owner" if sealed else "agent"), surface="cli")
        assert S.get(conn, "deploy").trusted_at is not None
        with owner() if owner_now else contextlib.nullcontext():
            try:
                S.upsert(conn, S.MemoryItem(slug="deploy", kind=new, title="Deploy note",
                                            body="use the staging gate before deploys"),
                         surface=surface, explicit={"kind"})
            except (S.SealedRecord, S.MemoryConflict):
                pass
        row = S.get(conn, "deploy")
        if row.trusted_at is not None:
            assert row.kind == old or (S.SURFACES[surface].get("approves") and owner_now), \
                "INV-01: approval kept across a kind nobody approved"


def test_an_owner_migrate_of_a_relabelled_file_drops_approval():
    with database() as (conn, root, patch):
        with owner():
            S.upsert(conn, S.MemoryItem(slug="n1", kind="note", title="Deploy note",
                                        body="use the staging gate before deploys"), surface="cli")
            note = root / "n1.md"
            note.write_text("---\nname: n1\ndescription: Deploy note\nmetadata:\n  type: feedback\n"
                            "---\n\nuse the staging gate before deploys\n", encoding="utf-8")
            migrate.import_file(conn, note)
        row = S.get(conn, "n1")
        assert (row.kind, row.trusted_at) == ("feedback", None)


# --------------------------------------------------------------------------- #
# INV-12: each database's scheduled jobs
# --------------------------------------------------------------------------- #

def _jobs_for(backend, db, root, crontab):
    if backend == "cron":
        return [l for l in crontab if f"SKILLMEM_DB={shlex.quote(db)} " in l]
    if backend == "systemd":
        # Environment= escapes a backslash: a Windows path is written doubled
        return [p for p in schedule._systemd_unit_dir().glob("*.service")
                if f"SKILLMEM_DB={db}".replace("\\", "\\\\") in p.read_text(encoding="utf-8")]
    return [p for p in (root / "Library" / "LaunchAgents").glob("*.plist")
            if plistlib.loads(p.read_bytes()).get("EnvironmentVariables", {}).get("SKILLMEM_DB") == db]


@pytest.mark.parametrize("backend", ["cron", "systemd", "launchd"])
@PROPERTY
@given(names=st.lists(st.sampled_from(["work.db", "personal.db", "a b.db", "memory.db", "x/memory.db"]),
                      min_size=2, max_size=4, unique=True), removed=st.integers(0, 3))
def test_each_database_keeps_its_own_jobs(backend, names, removed):
    with database() as (_, root, patch):
        patch.setenv("XDG_CONFIG_HOME", str(root / "config"))
        patch.setattr(schedule.shutil, "which", lambda _: "/usr/bin/crontab" if backend == "cron" else None)
        crontab: list[str] = []   # a stand-in for the user's crontab

        def run(cmd, *a, input=None, **kw):
            if cmd[:2] == ["crontab", "-l"]:
                return CompletedProcess(cmd, 0, stdout="\n".join(crontab), stderr="")
            if cmd[:2] == ["crontab", "-"]:
                crontab[:] = input.splitlines()
            return CompletedProcess(cmd, 0, stdout="", stderr="")
        patch.setattr(schedule.subprocess, "run", run)
        install, remove = {"cron": (schedule._cron_install, schedule._cron_remove),
                           "systemd": (schedule._systemd_install, schedule._systemd_remove),
                           "launchd": (schedule._launchd_install, schedule._launchd_remove)}[backend]
        dbs = [str(root / n) for n in names]
        for db in dbs:
            patch.setenv("SKILLMEM_DB", db)
            install()
        for db in dbs:
            assert len(_jobs_for(backend, db, root, crontab)) == 2, "INV-12: another database's install replaced these jobs"
        gone = dbs[removed % len(dbs)]
        patch.setenv("SKILLMEM_DB", gone)
        remove()
        for db in dbs:
            assert len(_jobs_for(backend, db, root, crontab)) == (0 if db == gone else 2), \
                "INV-12: remove touched another database's jobs"


# --------------------------------------------------------------------------- #
# INV-12: the pre-v10 backup is the database before the upgrade
# --------------------------------------------------------------------------- #

@PROPERTY
@given(kind=st.sampled_from(["How/To", " Feedback ", "a  b", "k" * 40, "note"]),
       visibility=st.sampled_from(["team", "Private ", "../x", "public"]),
       strength=st.sampled_from([-1.0, 10.0, 1.0, 2.5]),
       ttl=st.sampled_from([0, 5000, 7, None, "7"]))
def test_the_pre_v10_backup_holds_the_values_before_any_repair(kind, visibility, strength, ttl):
    with database() as (conn, root, patch):
        conn.execute("INSERT INTO memory_items(slug, kind, title, body, content_hash, visibility, "
                     "strength, ttl_days, created_at, updated_at) VALUES ('r1', ?, 't', 'b', 'h', ?, ?, ?, 1, 1)",
                     (kind, visibility, strength, ttl))
        for column in ("owner_seal", "origin", "trusted_at", "trusted_by"):
            conn.execute(f"ALTER TABLE memory_items DROP COLUMN {column}")
        conn.execute("DELETE FROM meta WHERE key = 'owner_seal_backfill_done'")
        before = tuple(conn.execute("SELECT kind, visibility, strength, ttl_days FROM memory_items").fetchone())
        conn.close()
        reopened = S.connect(root / "memory.db")
        S.init_schema(reopened)
        reopened.close()
        backups = list((root / "backups").glob("pre-v10-*"))
        assert len(backups) == 1
        copy = sqlite3.connect(backups[0])
        try:
            assert tuple(copy.execute("SELECT kind, visibility, strength, ttl_days FROM memory_items")
                         .fetchone()) == before, "INV-12: the backup holds repaired values"
        finally:
            copy.close()


# --------------------------------------------------------------------------- #
# INV-08: a note or skill left out is reported
# --------------------------------------------------------------------------- #

@PROPERTY
@given(links=st.lists(st.sampled_from(["out-file", "out-dir", "in-note", "in-dir"]), max_size=4))
def test_a_note_left_out_of_a_vault_is_reported(links):
    with database() as (conn, root, patch):
        v, elsewhere = root / "vault", root / "elsewhere"
        (elsewhere / "sub").mkdir(parents=True)
        (elsewhere / "far.md").write_text("far away note", encoding="utf-8")
        (elsewhere / "sub" / "deep.md").write_text("deep note", encoding="utf-8")
        (v / "real").mkdir(parents=True)
        (v / "real" / "note.md").write_text("real note", encoding="utf-8")
        outside = 0
        for i, link in enumerate(links):
            target = {"out-file": elsewhere / "far.md", "out-dir": elsewhere / "sub",
                      "in-note": v / "real" / "note.md", "in-dir": v / "real"}[link]
            (v / (f"l{i}.md" if link.endswith("note") or link.endswith("file") else f"d{i}")).symlink_to(target)
            outside += link.startswith("out")
        with owner():
            report = vault.import_vault(conn, v)
        assert report.inserted == 1
        assert len(report.failed) == outside + links.count("in-dir"), \
            "INV-08: a note or untraversed directory link was counted nowhere"
        # a pack: a SKILL.md link out of it is reported, a per-agent copy in it is not
        pack = root / "pack"
        (pack / "skills" / "x").mkdir(parents=True)
        (pack / "skills" / "x" / "SKILL.md").write_text("---\nname: x\ndescription: X.\n---\nUse x.\n",
                                                        encoding="utf-8")
        for i, link in enumerate(links):
            (pack / f".agent{i}" / "skills" / "x").mkdir(parents=True)
            (pack / f".agent{i}" / "skills" / "x" / "SKILL.md").symlink_to(
                elsewhere / "far.md" if link.startswith("out") else pack / "skills" / "x" / "SKILL.md")
        skipped: list = []
        assert [s.name for s in packs.read_pack(pack, skipped)] == ["x"]
        assert len(skipped) == outside, "INV-08: a skill left out was counted nowhere"


@PROPERTY
@given(bad=st.sampled_from([b"\xe9", b"\xff", b"\xc3(", b"\xed\xa0\x80"]),
       where=st.sampled_from(["description", "body", "name"]))
def test_a_skill_that_is_not_utf8_is_refused_not_replaced(bad, where):
    with database() as (conn, root, patch):
        skill = root / "pk" / "skills" / "x" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        parts = {"name": b"x", "description": b"Cafe rule.", "body": b"Use the cafe build."}
        parts[where] += bad
        skill.write_bytes(b"---\nname: " + parts["name"] + b"\ndescription: " + parts["description"]
                          + b"\n---\n" + parts["body"] + b"\n")
        skipped: list = []
        assert packs.read_pack(root / "pk", skipped) == [], "INV-08: undecodable text was stored replaced"
        assert len(skipped) == 1


# --------------------------------------------------------------------------- #
# INV-14: a frontmatter string field is a string or a number as written
# --------------------------------------------------------------------------- #

TAGGED = st.sampled_from(["!!binary aGk=", "!!set {x: null}", "!!set {}", "!!binary ''",
                          "!!omap [{a: 1}]", "!!pairs [[a, 1]]", "{a: 1}", "[a]", "yes"])


@pytest.mark.parametrize("field", ["project", "agent", "title", "name"])
@PROPERTY
@given(value=TAGGED)
def test_a_value_that_is_not_a_string_fails_the_note(field, value):
    with database() as (conn, root, patch):
        v = root / "vault"
        v.mkdir()
        (v / "c.md").write_text(f"---\n{field}: {value}\n---\nbody c\n", encoding="utf-8")
        with owner():
            report = vault.import_vault(conn, v)
        assert (report.inserted, len(report.failed)) == (0, 1), \
            f"INV-14: {field} {value!r} was stored as its repr"


@pytest.mark.parametrize("field", ["name", "description"])
@PROPERTY
@given(value=TAGGED)
def test_a_value_that_is_not_a_string_fails_a_migrated_file_and_a_skill(field, value):
    with database() as (conn, root, patch):
        note = root / "c.md"
        note.write_text(f"---\n{field}: {value}\n---\nbody c\n", encoding="utf-8")
        with pytest.raises(ValueError):
            migrate.import_file(conn, note)
        skill = root / "pk" / "skills" / "c" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\n{field}: {value}\n---\nUse c.\n", encoding="utf-8")
        skipped: list = []
        assert packs.read_pack(root / "pk", skipped) == [] and len(skipped) == 1


# --------------------------------------------------------------------------- #
# INV-16: a stored token goes only to the repository it was stored for
# --------------------------------------------------------------------------- #

REPOS = st.sampled_from(["me/private", "Me/Private", "someone-else/any-repo", "me/private2",
                         S.APP_NAME + "/x"])


@PROPERTY
@given(stored=REPOS, asked=REPOS)
def test_a_stored_token_goes_only_to_its_repository(stored, asked):
    with database() as (conn, root, patch):
        patch.delenv("SKILLMEM_GITHUB_TOKEN", raising=False)
        patch.delenv("SKILLMEM_GITHUB_REPO", raising=False)
        patch.setattr("shutil.which", lambda _: None)      # no `gh`
        result = CliRunner().invoke(cli.main, ["token", "set", "--repo", stored, "ghp_SECRET"])
        assert result.exit_code == 0, result.output
        sent = []

        def fake_get(url, token, **kw):
            sent.append((url, token))
            return b'{"tag_name": "v0.0.1"}'
        patch.setattr(cli, "_gh_get", fake_get)
        cli._upgrade_via_github(True, asked, None)
        assert sent[0][1] == ("ghp_SECRET" if stored.casefold() == asked.casefold() else None), \
            "INV-16: the stored token went to another repository"
