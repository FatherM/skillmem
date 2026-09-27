"""INV-08: the r06 review of e86c018."""
import os
import sys

import pytest
from hypothesis import example, given, strategies as st

from skillmem import vault
from skillmem.migrate import discover_claude_memory_dirs, import_dirs
from skillmem.packs import import_pack
from .support import PROPERTY, database

pytestmark = pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                                reason="POSIX modes, which root ignores")

FOLDERS = ["a", "a/b", "c"]
PROJECTS = ["a", "b", "c/memory"]    # migrate: a project, or its memory folder
# readable; nothing; search only; list only (r, no x: 0o544 is r-x to its owner)
MODES = [0o755, 0o000, 0o311, 0o644]


def _accounted(rel, names):
    """``rel`` or a folder above it is named among ``names``."""
    return any(n in (".", rel) or rel.startswith(n.rstrip("/") + "/") for n in names)


# INV-08: rglob and glob pass over a folder they cannot read, and import-vault,
# migrate and skills add reported the rest imported and exited 0
@PROPERTY
@example(importer="import-vault", modes=[0o755, 0o755, 0o000])
@example(importer="skills add", modes=[0o755, 0o755, 0o000])
@example(importer="migrate", modes=[0o755, 0o755, 0o000])
@example(importer="import-vault", modes=[0o644, 0o755, 0o755])
@example(importer="skills add", modes=[0o755, 0o644, 0o755])
@given(importer=st.sampled_from(["import-vault", "migrate", "skills add"]),
       modes=st.lists(st.sampled_from(MODES), min_size=3, max_size=3))
def test_every_file_is_imported_or_it_or_its_folder_is_reported(importer, modes):
    with database() as (conn, root, patch):
        base = {"import-vault": root / "vault", "skills add": root / "pack" / "skills",
                "migrate": root / ".claude" / "projects"}[importer]
        files = {}
        folders = PROJECTS if importer == "migrate" else FOLDERS
        for folder in folders:
            where = base / folder
            if importer == "migrate" and not folder.endswith("memory"):
                where /= "memory"
            where.mkdir(parents=True)
            word = "word" + folder.replace("/", "x")
            name = "SKILL.md" if importer == "skills add" else f"{word}.md"
            (where / name).write_text(f"---\nname: {word}\ndescription: {word}.\n---\n{word}\n")
            files[word] = (where / name).relative_to(base).as_posix()
        # the deepest first: a folder that loses its search bit hides its children
        chmods = sorted(zip(folders, modes), key=lambda fm: -fm[0].count("/"))
        try:
            for folder, mode in chmods:
                (base / folder).chmod(mode)
            if importer == "import-vault":
                report = vault.import_vault(conn, base)
                names = [n for n, _ in report.failed]
            elif importer == "skills add":
                report = import_pack(conn, str(base.parent))
                names = [n.removeprefix("skills/") for n, _ in report.skipped]
            else:
                names = []
                for source, report in import_dirs(conn, discover_claude_memory_dirs(root)):
                    if report.failed:
                        names.append(source.relative_to(base).as_posix())
        finally:
            for folder, _ in reversed(chmods):
                (base / folder).chmod(0o755)
        bodies = " ".join(r[0] for r in conn.execute("SELECT body FROM memory_items"))
        for word, rel in files.items():
            assert word in bodies or _accounted(rel, names), (rel, names)


KINDS = ["note", "feedback", "skill"]


# INV-01: approval covers the kind the owner saw; the owner's write at a
# terminal over an agent's `feedback` approved that kind, unnamed and unshown,
# and the owner's own words were injected as a rule
@PROPERTY
@example(agent_kind="feedback", trust=False, relabel=None, new_text=True, owner_kind=None)
@example(agent_kind="note", trust=True, relabel="feedback", new_text=False, owner_kind=None)
@given(agent_kind=st.sampled_from(KINDS), trust=st.booleans(),
       relabel=st.none() | st.sampled_from(KINDS), new_text=st.booleans(),
       owner_kind=st.none() | st.sampled_from(KINDS))
def test_an_owner_write_approves_only_a_kind_the_owner_saw(agent_kind, trust, relabel,
                                                           new_text, owner_kind):
    from skillmem import storage as S
    from .support import owner

    def write(kind, body, surface, named):
        return S.upsert(conn, S.MemoryItem(slug="deploy-notes", title="Deploy notes",
                                           body=body, kind=kind), surface=surface,
                        explicit=named, reason="mine", force=True)

    with database() as (conn, root, patch):
        write(agent_kind, "agent deploy notes", "mcp", {"kind"})
        seen = set()
        if trust:
            with owner():
                S.set_trust(conn, "deploy-notes", trusted=True, expect_kind=agent_kind)
            seen.add(agent_kind)
        if relabel:
            try:
                write(relabel, "agent deploy notes", "mcp", {"kind"})
            except S.SealedRecord:
                pass
        with owner():
            write(owner_kind or "note", "my own deploy notes" if new_text else "agent deploy notes",
                  "cli", {"kind"} if owner_kind else set())
        if owner_kind:
            seen.add(owner_kind)
        row = S.get(conn, "deploy-notes")
        assert row.trusted_at is None or row.kind in seen, (row.kind, seen)
