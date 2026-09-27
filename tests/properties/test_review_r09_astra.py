"""INV-08: an unresolved attachment must fail its note, never vanish silently."""
from pathlib import Path

import pytest
from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import cli, storage as S
from .support import PROPERTY, database, owner


@pytest.mark.parametrize("fault", ["unreadable", "missing", "unsupported", "outside"])
@PROPERTY
@given(existing=st.booleans(), listed=st.booleans(),
       payload=st.binary(min_size=1, max_size=30))
def test_unresolved_attachments_fail_without_changing_the_note(fault, existing, listed, payload):
    with database() as (conn, root, patch):
        vault = root / "vault"
        vault.mkdir()
        target = "diagram.mp4" if fault == "unsupported" else "diagram.png"
        if fault == "outside":
            target = "../diagram.png"
        asset = vault / target
        if fault != "missing":
            asset.write_bytes(payload)
        if fault == "unreadable":
            read_bytes = Path.read_bytes

            def denied(path):
                if path == asset:
                    raise PermissionError("attachment cannot be read")
                return read_bytes(path)

            patch.setattr(Path, "read_bytes", denied)
        if existing:
            S.upsert(conn, S.MemoryItem(slug="note", title="Original", body="Original text"),
                     explicit=set())
        before = S.get(conn, "note")
        prefix = f"---\nattachments: ['{target}']\n---\n" if listed else ""
        (vault / "note.md").write_text(prefix + f"# Changed\n![[{target}|diagram]]")
        (vault / "good.md").write_text("# A separate valid note\nKeep this text.")
        with owner():
            result = CliRunner().invoke(cli.main, ["import-vault", str(vault)])
        assert result.exit_code == 1, result.output
        assert "failed=1" in result.output and target in result.output
        assert S.get(conn, "note") == before
        assert S.get(conn, "good") is not None


@PROPERTY
@given(named=st.sampled_from(["[]", "null", "[present.png]"]),
       payload=st.binary(min_size=1, max_size=30))
def test_an_explicit_attachment_list_overrides_unresolved_embeds(named, payload):
    with database() as (conn, root, _):
        vault = root / "vault"
        vault.mkdir()
        (vault / "note.md").write_text(f"---\nattachments: {named}\n---\n# Note\n![[missing.png]]")
        (vault / "present.png").write_bytes(payload)
        with owner():
            result = CliRunner().invoke(cli.main, ["import-vault", str(vault)])
        assert result.exit_code == 0, result.output
        attachments = S.get(conn, "note").attachments
        assert len(attachments) == (1 if named == "[present.png]" else 0)
        for attachment in attachments:
            assert (S.default_data_dir() / attachment).read_bytes() == payload
