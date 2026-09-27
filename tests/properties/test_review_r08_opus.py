"""INV-08 and INV-14: the r08 review of 16a1bef."""
from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import cli, storage as S
from skillmem.migrate import import_dir
from skillmem.vault import import_vault
from .support import PROPERTY, database, owner


# INV-08: `migrate --source` reads one folder, and a note in a real subfolder was
# passed over with exit 0 while the same folder behind a link was reported.
@PROPERTY
@given(depths=st.lists(st.integers(0, 2), min_size=1, max_size=4))
def test_migrate_imports_or_reports_every_note_under_its_source(depths):
    with database() as (conn, root, _):
        source = root / "mem"
        for i, depth in enumerate(depths):
            folder = source.joinpath(*["sub"] * depth)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"rule{i}.md").write_text(f"Rule {i}\n\nbody {i}\n")
        result = CliRunner().invoke(cli.main, ["migrate", "--source", str(source)])
        missing = [i for i in range(len(depths)) if S.get(conn, f"rule{i}") is None]
        assert result.exit_code == (1 if missing else 0), result.output
        if missing:
            assert "sub" in result.output and "failed=0" not in result.output


# INV-14: `--kind` and migrate's `note` fallback are insert defaults, yet on a text
# change they decided the stored record's origin: a note became `owner` (and sealed),
# a feedback rule `derived`. The origin follows the kind the row keeps.
@PROPERTY
@given(importer=st.sampled_from(["migrate", "vault"]),
       stored=st.sampled_from(["note", "feedback", "document", "skill"]),
       default_kind=st.sampled_from(["note", "document"]))
def test_an_unnamed_kind_does_not_decide_a_stored_records_origin(importer, stored,
                                                                  default_kind):
    with database() as (conn, root, _):
        S.upsert(conn, S.MemoryItem(slug="recap", kind=stored, title="Recap",
                                    body="first text"), surface="mcp", explicit={"kind"})
        folder = root / "notes"
        folder.mkdir()
        (folder / "recap.md").write_text("Recap\n\nsecond text\n")
        with owner():
            if importer == "migrate":
                report, default = import_dir(conn, folder), "agent"
            else:
                report, default = import_vault(conn, folder, kind=default_kind), "owner"
        assert not report.failed and report.updated == 1
        after = S.get(conn, "recap")
        assert after.kind == stored
        assert after.origin == ("derived" if stored == "note" else default)
