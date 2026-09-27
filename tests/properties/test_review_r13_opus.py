"""INV-08/INV-05: the manual recap reports what happened; the debounce stamp is
decided under the per-session lock."""
import json
import threading
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from skillmem import cli, hooks as H, migrate, storage as S
from .support import database


def _transcript(root, lines=25):
    transcript = root / "project" / "abcdef12-3456-7890.jsonl"
    transcript.parent.mkdir(exist_ok=True)
    with transcript.open("w", encoding="utf-8") as fh:
        for i in range(lines):
            fh.write(json.dumps({"type": "user", "message": {"content": f"turn {i} said here"}}) + "\n")
    return transcript


def _model(calls, rc=0):
    def run(*args, input=b"", **kwargs):
        calls.append(True)
        return SimpleNamespace(stdout=f"## DONE\n- {len(input)} bytes\n{'x' * 120}".encode(),
                               returncode=rc, stderr=b"")
    return run


@pytest.mark.parametrize("outcome", ["written", "no-claude", "model-failed",
                                     "index-refused", "short", "debounced"])
def test_manual_recap_exits_zero_only_when_the_record_was_written(outcome):
    """INV-08 (*r13 review*): `skillmem recap` printed "recap run" and exited 0
    with no claude on PATH or with the index write refused; failures went only
    to hooks.log."""
    with database() as (conn, root, patch):
        transcript = _transcript(root, 5 if outcome == "short" else 25)
        calls = []
        patch.setattr(H.shutil, "which", lambda *_: None if outcome == "no-claude" else "/fake/claude")
        patch.setattr(H.subprocess, "run", _model(calls, rc=1 if outcome == "model-failed" else 0))
        if outcome == "index-refused":
            def refuse(*args, **kwargs):
                raise S.MemoryConflict("refused")
            patch.setattr(migrate, "import_file", refuse)
        args = ["recap", str(transcript)]
        if outcome == "debounced":
            stamp = H._recap_stamp(transcript.stem)
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text("1")
            args.append("--no-force")
        result = CliRunner().invoke(cli.main, args)
        written = [i for i in S.list_items(conn) if i.slug.startswith("session-")]
        if outcome == "written":
            assert result.exit_code == 0 and written, result.output
        else:
            assert result.exit_code != 0, f"INV-08: {outcome} acknowledged: {result.output}"
            assert not written


def test_second_stop_rereads_the_debounce_stamp_under_the_session_lock():
    """INV-05 (*r13 review*): the stamp was read before the per-session lock
    and not again after it, so a Stop that passed the check while another ran
    start to finish made a second model call within RECAP_MIN_INTERVAL."""
    with database() as (conn, root, patch):
        transcript = _transcript(root)
        patch.delenv("SKILLMEM_RECAP_FORCE", raising=False)
        calls = []
        patch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
        patch.setattr(H.subprocess, "run", _model(calls))
        payload = {"session_id": transcript.stem, "transcript_path": str(transcript)}
        first = threading.Thread(target=H.run_recap, args=(payload,))
        real_filter = H._filter_transcript

        def then_the_other_stop(path):
            if first.ident is None:           # this Stop has passed the debounce check
                first.start()
                first.join()
            return real_filter(path)

        patch.setattr(H, "_filter_transcript", then_the_other_stop)
        H.run_recap(payload)
        assert len(calls) == 1, "INV-05: a second recap ran within RECAP_MIN_INTERVAL"
