"""INV-06/11: captured export bodies and filesystem-independent hook discovery."""
import json
import re

from click.testing import CliRunner
from hypothesis import given, strategies as st

from skillmem import export as E, hooks, storage as S
from skillmem.cli import main
from skillmem.vault import import_vault
from .support import PROPERTY, database, owner


@PROPERTY
@given(slug=st.sampled_from(["quartz", "café", "σigma"]), repair=st.booleans())
def test_captured_excerpt_keeps_earlier_backup_after_repair(slug, repair):
    with database() as (conn, root, patch):
        body = "full original document\r\n" * 500
        item = S.MemoryItem(slug=slug, title="original", kind="document", body=body)
        S.upsert(conn, item)
        out = root / "backup"
        E.export_all(conn, out)
        row = S.get(conn, slug)
        (S.docs_dir() / row.body_path).unlink()
        S.upsert(conn, S.MemoryItem(slug=slug.upper(), title="twin", kind="document", body="twin"))
        other = S.connect(root / "memory.db")
        original = E._kept_bodies
        called = []

        def recover(*args, **kwargs):
            called.append(True)
            if repair:
                S.upsert(other, S.MemoryItem(slug=slug, title="original", kind="document", body=body))
            return original(*args, **kwargs)

        patch.setattr(E, "_kept_bodies", recover)
        try:
            assert E.export_all(conn, out) == 2
        finally:
            other.close()
        assert called
        restored = S.connect(root / "restored.db")
        S.init_schema(restored)
        try:
            with owner():
                report = import_vault(restored, out)
            assert not report.failed
            assert S.load_body(S.get(restored, slug)) == body
        finally:
            restored.close()


@PROPERTY
@given(prefix=st.sampled_from(["session-", "SESSION-", "Session-"]),
       suffix=st.sampled_from(["md", "MD", "mD", "Md"]))
def test_session_history_discovers_every_filename_case(prefix, suffix):
    with database() as (_, root, patch):
        memory = root / "project" / "memory"
        memory.mkdir(parents=True)
        (memory / f"{prefix}review.{suffix}").write_text("recall sentinel", encoding="utf-8")
        result = CliRunner().invoke(main, ["hook", "session-history"], input=json.dumps({
            "transcript_path": str(memory.parent / "session.jsonl")}))
        assert result.exit_code == 0
        assert "recall sentinel" in result.output


@PROPERTY
@given(suffix=st.sampled_from(["jsonl", "JSONL", "Jsonl", "jSoNl"]))
def test_transcript_discovery_ignores_extension_case(suffix):
    with database() as (_, root, patch):
        cwd = root / "project"
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
        project = root / ".claude" / "projects" / slug
        project.mkdir(parents=True)
        transcript = project / f"session.{suffix}"
        transcript.write_text("{}", encoding="utf-8")
        assert hooks.newest_transcript_for_cwd(cwd) == transcript
