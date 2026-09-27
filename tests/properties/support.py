from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import Phase, settings

from skillmem import storage as S

PROPERTY = settings(deadline=None, max_examples=50, derandomize=True, database=None, report_multiple_bugs=False,
                    phases=(Phase.explicit, Phase.generate, Phase.shrink))


@contextmanager
def database():
    # Per-example isolation also covers Hypothesis replay and shrinking.
    with TemporaryDirectory(prefix="skillmem-property-") as directory:
        root = Path(directory)
        with pytest.MonkeyPatch.context() as patch:
            for name, value in {
                "HOME": root, "USERPROFILE": root, "SKILLMEM_HOME": root / "home",
                "SKILLMEM_DB": root / "memory.db", "XDG_STATE_HOME": root / "state",
                "SKILLMEM_STATE_DIR": root / "state", "MEM_SEMANTIC": "0",
            }.items():
                patch.setenv(name, str(value))
            patch.setattr(S, "owner_present", lambda: False)
            conn = S.connect(root / "memory.db")
            S.init_schema(conn)
            try:
                yield conn, root, patch
            finally:
                conn.close()


@contextmanager
def owner():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(S, "owner_present", lambda: True)
        yield


def put(conn, slug="record", body="quartz deployment procedure", **kwargs):
    # created_at is an insert value, not a field a write names (INV-14)
    return S.upsert(conn, S.MemoryItem(slug=slug, title="quartz", body=body,
                                     kind="skill", **kwargs), explicit=set(kwargs) - {"created_at"})


# What renders as nothing, independently of skillmem's predicate: Unicode's
# Default_Ignorable_Code_Point (DerivedCoreProperties.txt, 15.0), assigned or
# not, and the blank Braille cell. The frame skips these before a marker, and
# the `trust` preview escapes them (*nineteenth review:* variation selectors,
# U+2065 and the Hangul fillers were missed).
DEFAULT_IGNORABLE = [chr(c) for lo, hi in (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF), (0x2800, 0x2800)) for c in range(lo, hi + 1)]
