"""Import Claude Code auto-memory .md files into skillmem.

Frontmatter shape we see in the wild:

    ---
    name: feedback-no-hallucinations
    description: "..."
    metadata:
      node_type: memory
      type: feedback
      originSessionId: 70da1c82-...
    ---
    body...

Some files have looser frontmatter; we fall back gracefully.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

from . import storage as _storage
from .storage import MemoryItem, upsert


def discover_claude_memory_dirs(home: Path | None = None) -> list[Path]:
    """Find every ``~/.claude/projects/*/memory`` directory on this machine.

    Skips dead symlinks; keeps directories it cannot read, so that the import
    reports it (INV-08) instead of passing it over as glob did.
    """
    home = home or Path.home()
    projects_root = home / ".claude" / "projects"
    try:
        projects_root.stat()  # exists() also hides permission errors on Python 3.14
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        return [projects_root]  # import_dir reports it
    projects, unreadable = tree(projects_root, recursive=False)
    out: list[Path] = unreadable  # import_dir reports discovery failures too
    for p in projects:
        try:
            if stat.S_ISDIR((p / "memory").stat().st_mode):
                out.append(p / "memory")
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            out.append(p / "memory")      # import_dir reports it
    return sorted(out)


def tree(root: Path, *, recursive: bool = True) -> tuple[list[Path], list[Path]]:
    """Every path under ``root``, and every directory it could not list.

    The one walk the importers share: rglob, glob and os.walk pass over a
    folder they cannot read, and every note in it went unreported (INV-08).
    Directory symlinks are listed, not traversed.
    """
    paths: list[Path] = []
    unreadable: list[Path] = []
    stack = [root]
    while stack:
        top = stack.pop()
        try:
            with os.scandir(top) as it:
                entries = list(it)
        except OSError:
            unreadable.append(top)
            continue
        for entry in entries:
            paths.append(Path(entry.path))
            try:
                if recursive and entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
            except OSError:
                unreadable.append(Path(entry.path))
    return paths, unreadable

def iter_notes(root: Path, suffixes: Iterable[str], *,
               recursive: bool = True) -> Iterator[tuple[Path, str | None]]:
    """Every note under ``root``, with the reason it is not imported, if any
    (INV-08): a link that leads outside the tree (`zshrc.md -> ~/.zshrc`),
    nowhere, or to a directory is reported, never read or skipped in silence;
    so is a subfolder a flat walk (``recursive=False``) does not search.
    Each in-tree file is read once, under its own name when that is a note:
    known by its inode, not its resolved path, which keeps the spelling the
    link used (`link.md -> NOTE.md` is `note.md` on APFS, nothing on ext4; INV-11).
    """
    base = root.resolve()
    seen: set[tuple[int, int]] = set()
    paths, unreadable = tree(root, recursive=recursive)
    for folder in sorted(unreadable):
        yield folder, "cannot be read; not imported"
    # os.path's tests, not Path's: before Python 3.14 Path.is_dir() and the
    # rest raise PermissionError in a folder that may be listed, not searched,
    # and the import stopped with a traceback instead of reporting it (INV-08)
    for path in sorted(paths, key=lambda p: (os.path.islink(p), p)):
        note = path.suffix.lower() in suffixes
        if not (os.path.islink(path) and not os.path.isfile(path)
                or note and not os.path.isdir(path) or not recursive and os.path.isdir(path)):
            continue
        try:
            inside = path.resolve().is_relative_to(base)
        except (OSError, RuntimeError):
            inside = False
        if not inside:
            yield path, "leads outside the tree; not imported"
        elif os.path.islink(path) and not os.path.exists(path):
            yield path, "does not lead to a readable file; not imported"
        elif os.path.islink(path) and os.path.isdir(path):
            yield path, "directory symlink is not traversed; not imported"
        elif os.path.isdir(path):           # a flat walk: its notes are not searched
            yield path, "subfolder is not searched; not imported"
        elif note and os.path.isfile(path):
            st = path.stat()
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                yield path, None
        elif not os.path.isfile(path):      # in a folder it may list, not search
            yield path, "cannot be read; not imported"


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


@dataclass
class ImportReport:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (file, error)


def desurrogate(value: Any) -> Any:
    """Repair lone/paired UTF-16 surrogates left behind by YAML \\uXXXX escapes.

    Frontmatter written as a JSON-escaped scalar (``json.dumps`` with the
    default ``ensure_ascii=True``) encodes an emoji as an escaped surrogate
    PAIR. PyYAML decodes each half into a separate lone surrogate instead of
    recombining them, and the resulting str cannot be encoded back to UTF-8 —
    every downstream write raises ``UnicodeEncodeError: surrogates not
    allowed`` and the record is lost. Recombine valid pairs, drop the rest.
    """
    if isinstance(value, str):
        if not any("\ud800" <= ch <= "\udfff" for ch in value):
            return value
        # 'surrogatepass' lets the pair round-trip through UTF-16, which joins it
        # back into the real character; anything still broken is dropped.
        try:
            return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        except UnicodeError:
            return "".join(ch for ch in value if not "\ud800" <= ch <= "\udfff")
    if isinstance(value, dict):
        return {desurrogate(k): desurrogate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [desurrogate(v) for v in value]
    return value


class _Loader(yaml.SafeLoader):
    """YAML's own reading, except that a number or a timestamp keeps the text
    the file wrote: `1.10` was 1.1, `12:30` 750, `0x1F` 31 and `010` 8, and a
    string field stored that (INV-14, *twentieth review*). A number field
    still gets the value; its text is what `str()` gives."""


def _as_written(cls: type, construct):
    typed = type(cls.__name__, (cls,), {"__str__": lambda self: self.written})

    def build(loader: yaml.SafeLoader, node: yaml.ScalarNode):
        value = typed(construct(loader, node))
        value.written = node.value
        return value
    return build


_Loader.add_constructor("tag:yaml.org,2002:int", _as_written(int, yaml.SafeLoader.construct_yaml_int))
_Loader.add_constructor("tag:yaml.org,2002:float", _as_written(float, yaml.SafeLoader.construct_yaml_float))
# no field takes a date: a timestamp is the text as written
_Loader.add_constructor("tag:yaml.org,2002:timestamp", yaml.SafeLoader.construct_scalar)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """(frontmatter, body) of a file, the body unstripped; ({}, text) without
    frontmatter. Frontmatter that is not a YAML mapping fails the file: it was
    read as none, and the file imported under its file name with every key it
    stated dropped (INV-14). All frontmatter readers parse through this."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.load(match.group(1), Loader=_Loader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid frontmatter: {exc}") from None
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError(f"invalid frontmatter: not a mapping ({type(meta).__name__})")
    if not isinstance(meta.get("metadata", {}), (dict, type(None))):   # `metadata: feedback` became a note
        raise ValueError(f"invalid metadata {meta['metadata']!r}")
    return meta, match.group(2)


def parse_file(path: Path) -> tuple[dict[str, Any], str]:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8-sig"))   # a BOM hid the frontmatter
    return desurrogate(meta), desurrogate(body.strip())


def _slug_from(meta: dict[str, Any], path: Path) -> str:
    raw = _scalar(meta.get("name"), "name") or path.stem
    raw = raw.strip().lower()
    raw = re.sub(r"[^a-z0-9а-яё\-]+", "-", raw, flags=re.IGNORECASE)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    return raw or path.stem


def _file_kind(meta: dict[str, Any]) -> str | None:
    """The kind a file's `metadata.type` key names, or None without the key.
    The key names it by being there: "" or null is an invalid kind that
    upsert refuses, not a default (INV-14). Both importers ask this."""
    md = meta.get("metadata") or {}
    if "type" in md:
        return _scalar(md["type"], "type") or ""
    return None


def _kind_from(meta: dict[str, Any], path: Path) -> str | None:
    """The kind the file names — `metadata.type`, else a recognised file name
    prefix — or None: the `note` fallback is an insert default, and applying it
    to an existing row relabelled a rule and dropped it from the briefing."""
    if _file_kind(meta) is not None:
        return _file_kind(meta)
    prefix = path.stem.split("_", 1)[0]
    if prefix in {"feedback", "project", "reference", "user"}:
        return prefix
    return None


def _title_from(meta: dict[str, Any], body: str, slug: str) -> str:
    if "description" in meta:
        # the key names the title: "" or null clears it (INV-14)
        return (_scalar(meta["description"], "description") or "").strip()
    return _first_line(body, slug)


def _first_line(body: str, fallback: str) -> str:
    """The title a file that names none gets: its first heading or line."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
        if line:
            return line[:120]
    return fallback


# A file can claim anything in its frontmatter, so a claimed origin is accepted
# only when it is NOT a claim of ownership: re-importing a file must never be a
# way to launder an imported or model-written memory into a trusted one. Trust
# itself is never imported — only the owner grants it.
_CLAIMABLE_ORIGINS = ("agent", "imported", "derived")


def _origin_from(conn, slug: str, meta: dict[str, Any], named_kind: str | None,
                 default_kind: str, default: str = "owner") -> str:
    """The caller declares provenance; the file may only lower it, never raise it.

    `default` is what the importer knows about the directory it is reading (the
    owner's own memory dir → owner). A file claiming `origin: owner` is ignored,
    or re-importing a pack would be a way to launder it into a trusted rule.
    Call it inside the transaction that writes the row: an unnamed kind is the
    row's, and `default_kind` only an insert default (INV-14).
    """
    md = meta.get("metadata") or {}
    claimed = str(md.get("origin") or "").strip().lower()
    if claimed in _CLAIMABLE_ORIGINS:
        return claimed
    kind = named_kind
    if kind is None:
        row = conn.execute("SELECT kind FROM memory_items WHERE slug = ?", (slug,)).fetchone()
        kind = row["kind"] if row else default_kind
    # A session recap is a model's summary of a transcript, whatever the file says.
    return "derived" if _storage._valid_kind(kind) == "note" else default


# what the recap hook writes, then what older exports wrote
_SESSION_KEYS = ("source_session", "originSessionId", "sessionId")


def _scalar(value: Any, key: str) -> str | None:
    """A frontmatter string field: null or empty is none; a number is its text
    as written; anything else (`yes`, `!!binary`, `!!set`) is refused, not
    stored as its repr (INV-14)."""
    if value is None or value == "":
        return None                    # `project: 0` is "0", not none
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"invalid {key} {value!r}")
    return str(value)


def _source_session(meta: dict[str, Any]) -> str | None:
    md = meta.get("metadata") or {}
    # the first key present names the field, whatever its value
    key = next((k for k in _SESSION_KEYS if k in md), None)
    return None if key is None else _scalar(md[key], key)


def import_file(conn, path: Path, *, force: bool = True,
                default_origin: str = "agent",
                claimed: dict[str, tuple[Path, tuple]] | None = None) -> str:
    """Import a single .md file. Returns 'inserted' | 'updated' | 'skipped'."""
    meta, body = parse_file(path)
    slug = _slug_from(meta, path)
    named_kind = _kind_from(meta, path)
    kind = "note" if named_kind is None else named_kind
    title = _title_from(meta, body, slug)
    session = _source_session(meta)
    # the row's kind decides the origin, and the existence read the reason and
    # the status: under the write's lock (INV-05)
    with _storage.tx(conn):
        origin = _origin_from(conn, slug, meta, named_kind, kind, default_origin)
        md = meta.get("metadata") or {}
        explicit = {k for k, v in (("kind", named_kind is not None),
                                  ("source_session", _SESSION_KEYS & md.keys()))
                    if v}
        if claimed is not None:
            # A copy must carry the same text, metadata and named fields:
            # omitted metadata preserves a value that an explicit null clears.
            file_key = (title, body, kind, session, origin, frozenset(explicit))
            first, first_key = claimed.setdefault(slug, (path, file_key))
            if first != path:      # slugs are not per project or per file
                if first_key == file_key:
                    return "skipped"
                raise ValueError(f"slug '{slug}' was already imported from {first}; "
                                 f"rename one, or import it alone with --source")
        item = MemoryItem(
            slug=slug,
            kind=kind,
            title=title,
            body=body,
            source_session=session,
            visibility="private",
            origin=origin,
        )
        existed = conn.execute(
            "SELECT id FROM memory_items WHERE slug = ?", (slug,)
        ).fetchone()
        upsert(
            conn, item, surface="migrate",
            explicit=explicit,     # what the file states; the row keeps the rest (INV-14)
            reason="migrated from .md" if existed else None,
            force=force,
        )
    return "updated" if existed else "inserted"


def import_dirs(conn, sources: Iterable[Path]) -> Iterator[tuple[Path, ImportReport]]:
    """Import several memory directories; a slug a later directory shares with
    an earlier one is reported, not written over it."""
    claimed: dict[str, tuple[Path, tuple]] = {}
    for source in sources:
        yield source, import_dir(conn, source, claimed=claimed)


def import_dir(
    conn,
    source: Path,
    *,
    skip_index: bool = True,
    default_origin: str = "agent",
    claimed: dict[str, tuple[Path, tuple]] | None = None,
) -> ImportReport:
    report = ImportReport()
    if not os.path.isdir(source):   # a file, or in a folder it cannot search
        report.failed.append((str(source), "not a readable directory"))
        return report
    claimed = {} if claimed is None else claimed   # one directory is a run too

    with _storage.tx(conn):
        # by name, not glob: glob matched `Rule.MD` on Windows only (INV-11)
        for path, refused in iter_notes(source, {".md"}, recursive=False):
            if refused:
                report.failed.append((path.name, refused))
                continue
            if skip_index and path.name.upper() == "MEMORY.MD":
                report.skipped += 1
                continue
            try:
                action = import_file(conn, path, default_origin=default_origin,
                                     claimed=claimed)
                if action == "inserted":
                    report.inserted += 1
                elif action == "skipped":
                    report.skipped += 1
                else:
                    report.updated += 1
            except Exception as exc:  # noqa: BLE001 - report and continue
                report.failed.append((path.name, repr(exc)))
    return report
