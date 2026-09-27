"""Dump every memory back to .md with YAML frontmatter.

Vendor-lock defense: if skillmem ever dies, you keep your data as
ordinary markdown files. The export is round-trip-safe — re-importing the dump
via ``vault.import_vault`` yields the same slug/kind/title/body plus metadata
(project/tags/topics/visibility/agent/strength/ttl/freshness).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import yaml

from . import storage as S
from .migrate import FRONTMATTER_RE, split_frontmatter


class _Dumper(yaml.SafeDumper):
    pass


# PyYAML writes NEL (U+0085) raw in a plain scalar and reads it back as a
# folded line break; double quotes escape it as "\N".
_Dumper.add_representer(str, lambda dumper, s: dumper.represent_scalar(
    "tag:yaml.org,2002:str", s, style='"' if "\x85" in s else None))


def dump_yaml(data) -> str:
    """YAML that loads back as ``data``: every file skillmem writes uses it."""
    return yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False)


_SAFE_FN = re.compile(r"[^\w.\-]+", re.UNICODE)


def _filename_key(name: str) -> str:
    """One filename comparison for imports, exports and GC on every filesystem."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def _safe_filename(slug: str) -> str:
    """Filesystem-safe name, collision-proof: a slug sanitising changed ("a/b"
    and "a-b"), one that could be such a name ("a-b__<hash>") and one too long
    for a file name get a slug-hash suffix; clean slugs keep pretty names.
    A leading dot goes too: `...md` has no `.md` suffix, and the restore
    never found the record (INV-06).
    """
    name = _SAFE_FN.sub("-", slug).lstrip(".-").rstrip("-")
    if not name:
        name = "untitled"
    if name != slug or "__" in name or len(name.encode("utf-8")) > 200:
        name = (name.encode("utf-8")[:200].decode("utf-8", "ignore")
                + "__" + hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8])
    return name


def _frontmatter(item: S.MemoryItem, *, truncated: bool = False) -> str:
    meta = {
        "name": item.slug,
        "description": item.title,
        "metadata": {
            "node_type": "memory",
            "type": item.kind,
            "originSessionId": item.source_session,
            # Provenance and the seal travel with the file; approval never
            # does, only the owner grants it.
            "origin": item.origin,
            "owner_seal": bool(getattr(item, "owner_seal", 0)),
        },
        "exported_at": dt.datetime.fromtimestamp(int(time.time()), tz=dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
    if item.project:
        meta["project"] = item.project
    if item.tags:
        meta["tags"] = item.tags
    if item.topics:
        meta["topics"] = item.topics
    if item.agent:
        meta["agent"] = item.agent
    if item.attachments:
        meta["attachments"] = [Path(a).as_posix() for a in item.attachments]
    if item.ttl_days:
        meta["ttl_days"] = item.ttl_days
    # null too: a deadline the owner cleared is not one derived from the TTL
    meta["freshness_until"] = item.freshness_until
    if item.visibility and item.visibility != "private":
        meta["visibility"] = item.visibility
    meta["strength"] = item.strength   # always: a restore must be able to say "1.0"
    # the rest of what a row earned, so a restore is a restore
    meta["pinned"] = bool(item.pinned)
    if item.lifecycle == "archived":   # 'stale' the sweep derives from recency
        meta["lifecycle"] = "archived"
    meta["access_count"] = item.access_count
    meta["confirmed_count"] = item.confirmed_count
    meta["failure_count"] = item.failure_count
    # null too: "never" is a value, and the key tells it from a pre-0.12 dump
    meta["last_accessed_at"] = item.last_accessed_at
    meta["last_decayed_at"] = item.last_decayed_at
    if truncated:      # only the excerpt follows
        meta["truncated"] = True
    return dump_yaml(meta).strip()


def _iter_all(conn) -> list[S.MemoryItem]:
    """Every record, deleted ones too, in one statement: what an export dumps
    and what it calls its own are one snapshot (INV-05)."""
    rows = conn.execute("SELECT * FROM memory_items ORDER BY kind, slug").fetchall()
    return [S.MemoryItem.from_row(r) for r in rows]


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


def _wrote(key: str, path: Path) -> bool:
    """Whether the database at ``path`` is the one whose id gives ``key``: a
    newcomer at a moved database's old path is not. Unreadable: assume it is."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("SELECT 1 FROM meta").fetchone()
            db_id = S._db_id(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return True
    return key == S._identity(str(path), db_id)


def _record_in(path: Path) -> tuple[str, int] | None:
    """(slug, created_at) of the record a dump file holds, or None."""
    try:
        # read as the restore reads it: `name: 1.10` is the slug "1.10", and a
        # dump a Windows checkout (git autocrlf) gave "\r\n" is still ours
        text = path.read_bytes().decode("utf-8-sig")
        meta, _ = split_frontmatter(text.replace("\r\n", "\n").replace("\r", "\n"))
        return str(meta["name"]), int(meta["created_at"])
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        return None


@contextmanager
def _locked(destination: Path, *, lock_name: str = ".skillmem-export.lock"):
    """One exporter per directory from manifest read to manifest write: two
    databases that both read the manifest before either wrote it each saw a
    free directory, and the second overwrote the first one's backup."""
    with open(destination / lock_name, "a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # raises after ~10 s
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        yield  # closing the file releases the lock, also if the process dies


def export_all(conn, destination: Path) -> int:
    """Write every memory as ``<destination>/<kind>/<slug>.md``. Returns count."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with _locked(destination):
        return _export_locked(conn, destination)


def _export_locked(conn, destination: Path) -> int:
    root = destination.resolve()
    # The files each database's exporter wrote last time, keyed by database:
    # only these are ever pruned, and only its own.
    manifest = destination / ".skillmem-export.json"
    data: dict = {}
    try:
        loaded = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    dbs = data.get("dbs") if isinstance(data.get("dbs"), dict) else {}
    # the database each entry was written from: records alone cannot tell a
    # moved database from a second live copy restored from the same dump
    paths = data.get("paths") if isinstance(data.get("paths"), dict) else {}
    ns, here = S._db_identity(conn), S._db_path(conn)
    previous: set[str] = set(dbs.pop(ns, []) or [])
    # a 0.11 pre-release manifest ("files", "default") names no writer: it
    # goes through the same ownership checks as every other entry
    for legacy in (data.get("files"), dbs.pop("default", None)):
        if isinstance(legacy, list):
            dbs.setdefault("default", []).extend(map(str, legacy))
    # every path is decided and checked before the first file is written
    plan: list[tuple[S.MemoryItem, Path]] = []
    items: list[S.MemoryItem] = []
    assets: dict[str, Path] = {}
    bodies: dict[int, str] = {}
    data_dir = S.default_data_dir().resolve()
    everything = _iter_all(conn)
    for item in everything:
        if item.deleted_at is not None:
            continue
        body = S.load_body(item)
        if S.is_excerpt(item, body):
            current = _current(conn, item)
            if current is None:
                continue    # deleted since: not in the dump, as in the next one
            item, body = current
        bodies[item.id] = body     # frozen before its path and attachments (INV-06)
        items.append(item)
        # attachments travel under their (content-addressed) store path
        for rel in item.attachments:
            src = (data_dir / rel).resolve()
            if (src.is_relative_to(data_dir / "assets")
                    and (destination / rel).resolve().is_relative_to(root)):
                assets[Path(rel).as_posix()] = src
    # Case and canonical Unicode variants can name one file, an attachment's
    # too: a record of kind `assets` dumped over `assets/X.md` (INV-11)
    taken = {_filename_key((destination / rel).as_posix()) for rel in assets}
    for item in items:
        # kind is writer-controlled text: sanitised, and refused if it still leads outside
        folder = destination / _safe_filename(item.kind).replace("..", "-")
        path = folder / f"{_safe_filename(item.slug)}.md"
        if _filename_key(path.as_posix()) in taken:
            h = hashlib.sha256(item.slug.encode("utf-8")).hexdigest()[:8]
            path = path.with_name(f"{path.stem}__{h}.md")
        taken.add(_filename_key(path.as_posix()))
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"refusing to export {item.slug!r} outside {root}")
        plan.append((item, path))
    # Another manifest entry is another database, or this one moved, restored
    # or rebuilt from this very dump. A file names the record it holds (slug
    # and created_at, which dump and restore keep, a deleted row's too): an
    # entry whose files hold our records is ours, and its stale files are
    # pruned; one about to be overwritten holding another's refuses the export.
    planned = {_filename_key(path.relative_to(destination).as_posix()): item
               for item, path in plan}
    ours = {(item.slug, item.created_at) for item in everything}
    slugs = {item.slug for item in items}
    attached = {_filename_key(rel): src for rel, src in assets.items()}
    for key, files in list(dbs.items()):
        if not isinstance(files, list):
            continue
        mine = unproved = clash = False
        others: list[str] = []     # files no record of ours proves ours
        for rel in map(str, files):
            held = _record_in(destination / rel)
            item = planned.get(_filename_key(rel))
            # an attachment is published over whatever is there: only the same bytes may be
            if _filename_key(rel) in attached:
                data = intact_asset(attached[_filename_key(rel)])
                clash = clash or (data is not None and os.path.isfile(destination / rel)
                                  and (destination / rel).read_bytes() != data)
            if held not in ours and item is None:
                others.append(rel)
            if held is None:     # that database's file, until the entry is proved ours
                unproved = unproved or (item is not None and os.path.exists(destination / rel))
                continue
            if item is not None and held != (item.slug, item.created_at):
                raise ValueError(f"{destination / rel} belongs to another database's "
                                 "export — give each database its own directory")
            # a restore knows a record by its slug: another database's under
            # one of ours, in another kind's folder, replaced ours (INV-06)
            if held not in ours and held[0] in slugs:
                raise ValueError(f"{destination / rel} holds another database's "
                                 f"{held[0]!r} — give each database its own directory")
            mine = mine or held in ours
        if unproved and not mine:
            raise ValueError(f"{destination} holds files another database's export lists "
                             "— give each database its own directory")
        there = paths.get(key)
        # its writer is this database: its id at the entry's path gives the key
        whole = isinstance(there, str) and key == S._identity(there, S._db_id(conn))
        if clash and not whole:
            raise ValueError(f"{destination} holds an attachment another database's export "
                             "lists — give each database its own directory")
        if mine and not isinstance(there, str) and not S.owner_present():
            raise ValueError(f"{destination} holds an export with no writer identity; "
                             "run export-all yourself once to take it over")
        if mine and isinstance(there, str):
            if (os.path.exists(there) and not _same_file(Path(there), Path(here))
                    and _wrote(key, Path(there))):     # a live copy of our records (K5)
                raise ValueError(f"{destination} holds the export of {there}, a live "
                                 "database with the same records — give each database "
                                 "its own directory")
            # Its path is gone or is ours: this database, moved, wrote it if its
            # id at that path gives the key; else a moved one may live on (INV-12)
            if not whole and not S.owner_present():
                raise ValueError(f"{destination} holds the export of the database that "
                                 f"was at {there}. If it was deleted, not moved, run "
                                 f"`skillmem export-all {destination}` yourself once to "
                                 "take its export over; else give this database its own "
                                 "directory")
        if mine:
            # An entry whose writer is this database is ours whole. Any other
            # is ours file by file: the rest stays reserved to it, never pruned
            # (a legacy one lost them all)
            if whole:
                others = []
            previous |= set(map(str, files)) - set(others)
            if others:
                dbs[key] = others
            else:
                del dbs[key]
                paths.pop(key, None)
    paths = {k: v for k, v in paths.items() if k in dbs}
    paths[ns] = here

    listed: dict = {}

    def save(files: set[str]) -> None:
        dbs[ns] = sorted(files)
        _publish(manifest, json.dumps({"dbs": dbs, "paths": paths}, indent=1).encode("utf-8"),
                 listed)

    written: list[str] = []
    kept = _kept_bodies(plan, [destination / rel for rel in sorted(previous)], bodies)
    # Reserved before the first is written, and kept on failure: an export
    # killed halfway (no `except` runs) otherwise leaves files in no manifest.
    save(previous | set(assets) | {path.relative_to(destination).as_posix() for _, path in plan})
    for rel, src in assets.items():
        data = intact_asset(src)
        if data is not None:
            (destination / rel).parent.mkdir(parents=True, exist_ok=True)
            _publish(destination / rel, data, listed)
        elif not os.path.isfile(destination / rel):
            continue
        # else lost or damaged in the store: the backup's copy is kept (INV-06)
        written.append(rel)
    _write_planned(plan, destination, written, kept, bodies, listed)
    # a stale name is pruned unless the filesystem says it IS a file just
    # written (one file on APFS/NTFS, two on ext4: INV-11)
    same_name: dict[str, list[str]] = {}
    for w in written:
        same_name.setdefault(_filename_key(w), []).append(w)
    # an asset another database lists is its file too
    theirs = {_filename_key(str(f)) for k, v in dbs.items() if k != ns and isinstance(v, list) for f in v}
    for rel in previous - set(written):
        stale = destination / rel
        if any(_same_file(stale, destination / w) for w in same_name.get(_filename_key(rel), ())):
            continue
        if stale.resolve().is_relative_to(root) and _filename_key(rel) not in theirs:
            stale.unlink(missing_ok=True)
    save(set(written))
    return len(plan)


def intact_asset(path: Path) -> bytes | None:
    """A file's bytes, or None when it cannot be read or is content-addressed
    (named for a sha256, in any case) and its bytes do not hash to its name.
    Every reader that takes a stored attachment asks here (INV-16)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if (re.fullmatch(r"[0-9a-fA-F]{64}", path.stem)
            and hashlib.sha256(data).hexdigest() != path.stem.lower()):
        return None
    return data


def _kept_bodies(plan: list[tuple[S.MemoryItem, Path]], earlier: list[Path],
                 bodies: dict[int, str]) -> dict[str, str]:
    """The text of each record captured as an excerpt, from an earlier
    dump of that record: the file this export replaces, or any other the
    manifest lists. Read before the first write, which may overwrite it."""
    lost = {(item.slug, item.created_at): item for item, _ in plan
            if S.is_excerpt(item, bodies[item.id])}
    kept: dict[str, str] = {}
    for path in [*(p for _, p in plan), *earlier] if lost else ():
        item = lost.get(_record_in(path))
        if item is not None and item.slug not in kept:
            body = _dumped_body(path)
            if body is not None and not S.is_excerpt(item, body):
                kept[item.slug] = body
    return kept


def _dumped_body(path: Path) -> str | None:
    """The body a dump file at ``path`` holds, as ``_write_planned`` wrote it."""
    try:
        match = FRONTMATTER_RE.match(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return match.group(2)[1:-1] if match else None


def _publish(path: Path, data: bytes, listed: dict | None = None) -> None:
    """Write bytes beside ``path`` and rename into place, so a crash halfway
    never leaves the last complete copy torn (INV-16). ``listed`` caches each
    directory's names by filename key across one export's writes."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        # APFS and NTFS keep the spelling on disk when a file is replaced under
        # a case or canonical Unicode variant: it takes the planned one first (INV-11)
        listed = {} if listed is None else listed
        if path.parent not in listed:
            listed[path.parent] = {_filename_key(n): n for n in os.listdir(path.parent)}
        names, key = listed[path.parent], _filename_key(path.name)
        if names.get(key, path.name) != path.name and _same_file(path.parent / names[key], path):
            os.rename(path.parent / names[key], path)
        names[key] = path.name
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _current(conn, item: S.MemoryItem) -> tuple[S.MemoryItem, str] | None:
    """The record and its body, both read under the write lock GC deletes
    body files under (INV-05), or None if it was deleted since; the path is
    planned from the row returned here. A read-only database: the row as read.
    A busy one fails the export: the stale row's excerpt replaced the last
    good dump (INV-05)."""
    try:
        with S.tx(conn):
            row = conn.execute("SELECT * FROM memory_items WHERE id = ? AND deleted_at IS NULL",
                               (item.id,)).fetchone()
            if row is None:
                return None
            fresh = S.MemoryItem.from_row(row)
            return fresh, S.load_body(fresh)
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode & 0xFF != sqlite3.SQLITE_READONLY:
            raise
    return item, S.load_body(item)


def _write_planned(plan: list[tuple[S.MemoryItem, Path]], destination: Path,
                   written: list[str], kept: dict[str, str], bodies: dict[int, str],
                   listed: dict) -> None:
    for item, path in plan:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = bodies[item.id]
        # decided on the body read, never on a second read of the file (INV-15);
        # an earlier dump may hold the last copy of the text the store lost
        truncated = S.is_excerpt(item, body)
        if truncated:
            old = kept[item.slug] if item.slug in kept else _dumped_body(path)
            if old is not None and not S.is_excerpt(item, old):
                body, truncated = old, False
        # body verbatim, plus the one newline the file needs: its hash survives
        content = ("---\n" + _frontmatter(item, truncated=truncated)
                   + "\n---\n\n" + body + "\n")
        _publish(path, content.encode("utf-8"), listed)
        written.append(path.relative_to(destination).as_posix())
