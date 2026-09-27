"""INV-11: Unicode filename aliases keep imported assets and pack files."""
from pathlib import Path

from hypothesis import example, given, strategies as st

from skillmem import storage as S
from skillmem.export import export_all
from skillmem.packs import _detect_license, iter_skill_files
from skillmem.vault import _named, import_vault
from .support import PROPERTY, database


ALIASES = st.sampled_from([
    ("Σ.png", "ς.png"), ("STRASSE.png", "straße.png"),
    ("é.png", "e\u0301.png"), ("가.png", "가.png"),
    ("image.svg", "image.ſvg"),
])


@PROPERTY
@example(pair=("Σ.png", "ς.png"), location="beside", reverse=False)
@given(pair=ALIASES, location=st.sampled_from(["beside", "elsewhere", "listed", "store"]),
       reverse=st.booleans())
def test_unicode_attachment_alias_survives_import_and_export(pair, location, reverse):
    requested, actual = pair[::-1] if reverse else pair
    with database() as (conn, root, _):
        vault = root / "vault"
        vault.mkdir()
        folder = (S.default_data_dir() / "assets" if location == "store" else
                  vault / "pictures" if location == "elsewhere" else vault)
        folder.mkdir(parents=True, exist_ok=True)
        payload = b"attachment bytes must survive"
        (folder / actual).write_bytes(payload)
        if location in {"listed", "store"}:
            target = f"assets/{requested}" if location == "store" else requested
            text = f'---\nattachments: ["{target}"]\n---\nNote'
        else:
            text = f"Note ![[{requested}]]"
        (vault / "note.md").write_text(text, encoding="utf-8")
        report = import_vault(conn, vault)
        assert not report.failed
        assert report.inserted == 1
        item = S.get(conn, "note")
        assert len(item.attachments) == 1, "successful import silently omitted an attachment"
        destination = root / "dump"
        assert export_all(conn, destination) == 1
        assert (destination / item.attachments[0]).read_bytes() == payload


@PROPERTY
@given(pair=ALIASES, reverse=st.booleans())
def test_unicode_lookup_prefers_exact_spelling(pair, reverse):
    # Synthetic paths exercise case twins even on filesystems that alias them.
    paths = [Path("assets") / name for name in pair]
    if reverse:
        paths.reverse()
    for exact in paths:
        matches = _named(paths, lambda p: p.parts, exact.parts)
        assert matches[0] == exact
        assert set(matches) == set(paths)


@PROPERTY
@given(skill=st.sampled_from(["ſkill.md", "SKILL.MD", "skill.md"]),
       license=st.sampled_from(["LICENſE", "license", "LICENSE"]))
def test_pack_discovery_matches_unicode_case_aliases(skill, license):
    with database() as (_, root, __):
        pack = root / "pack"
        pack.mkdir()
        path = pack / skill
        path.write_text("procedure", encoding="utf-8")
        (pack / license).write_text("MIT", encoding="utf-8")
        assert list(iter_skill_files(pack)) == [(path, None)]
        assert _detect_license(pack) == "MIT"
