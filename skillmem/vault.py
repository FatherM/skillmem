"""Obsidian vault importer.

Recursively walks a directory tree, imports every .md as a memory of
``kind=document`` by default, derives the ``project`` tag from the top-level
folder name (frontmatter ``project:`` wins when present), copies attached
images / PDFs to the assets directory, and resolves ``[[wikilinks]]`` into
``mem_links``. Frontmatter written by export.py (tags/topics/visibility/agent/
strength/ttl/freshness) is rehydrated, making export -> import a full
metadata round-trip.

The migrator from ``migrate.py`` handles single .md files with frontmatter
shaped like Claude Code auto-memory; this module handles arbitrary trees with
no frontmatter or with loose Obsidian frontmatter.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import storage as S
from .export import _filename_key, _publish, intact_asset
from .migrate import (ImportReport, _file_kind, _first_line, _scalar, desurrogate,
                      iter_notes, split_frontmatter)
from .migrate import _origin_from as _migrate_origin


SUPPORTED_TEXT = {".md", ".markdown"}
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".svg"}

_SLUG_TRIM = re.compile(r"[^a-z0-9а-яё\-]+", re.IGNORECASE)


def _slug_from_meta_or_path(meta: dict, root: Path, path: Path) -> str:
    """Prefer frontmatter ``name:`` so an export/import round-trip is lossless."""
    raw = _scalar(meta.get("name"), "name")
    if raw and _is_dump(meta):
        return raw               # a skillmem dump: the slug is exact, or a_b and a-b merge
    if raw:
        slug = _SLUG_TRIM.sub("-", raw.lower())
    else:
        rel = path.relative_to(root).with_suffix("")
        joined = "-".join(p.lower() for p in rel.parts)
        slug = _SLUG_TRIM.sub("-", joined)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "untitled"


def _project_from_path(root: Path, path: Path) -> str | None:
    rel = path.relative_to(root)
    parts = rel.parts
    if len(parts) > 1:
        return parts[0]
    return None


def _parse_md(text: str) -> tuple[dict, str]:
    meta, body = split_frontmatter(text)
    if _is_dump(meta):
        # a skillmem dump wrote "\n---\n\n<body>\n": undo exactly that, keep the
        # body's own whitespace so its content hash (and approval) survives
        if body.startswith("\n"):
            body = body[1:]
        if body.endswith("\n"):
            body = body[:-1]
    else:
        body = body.strip()
    return desurrogate(meta), desurrogate(body)


def _str_list(value, key: str = "list") -> list[str]:
    """Frontmatter list coercion: Obsidian allows both ``tags: [a, b]`` and ``tags: a``.
    Null and "" are empty; anything else that is not one is refused (INV-14): a mapping
    became [] and cleared the field, and a null item was dropped (*r09 opus review*)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list) and all(isinstance(v, (str, int, float)) and not isinstance(v, bool)
                                       for v in value):   # `true` became the tag "True"
        return [str(v) for v in value]
    raise ValueError(f"invalid {key} {value!r}")


def _number(value, cast):
    """A frontmatter number, exactly: ``int(1.5)`` was 1, ``int(True)`` 1 and
    ``float('nan')`` a strength (INV-14)."""
    if isinstance(value, bool):
        raise ValueError
    number = cast(value) if isinstance(value, str) else value
    if not isinstance(number, (int, float)) or not math.isfinite(number) \
            or (cast is int and not isinstance(number, int)):
        raise ValueError
    return cast(number)


def _restore_meta(meta: dict) -> dict:
    """Rehydrate metadata the exporter (export.py) writes to frontmatter, so an
    export -> import round-trip is lossless. A plain Obsidian vault without
    these keys yields an empty dict — the MemoryItem defaults stay in force."""
    out: dict = {}
    for key in ("strength", "visibility", "access_count", "confirmed_count",
                "failure_count", "created_at", "updated_at"):
        if key in meta and meta[key] is None:     # no "none" to clear to (INV-14)
            raise ValueError(f"invalid {key} None")
    for key in ("tags", "topics"):
        if key in meta:
            out[key] = _str_list(meta[key], key)
    if meta.get("visibility") is not None:
        out["visibility"] = str(meta["visibility"])     # "" is refused, not "private"
    if _scalar(meta.get("agent"), "agent"):
        out["agent"] = _scalar(meta["agent"], "agent")
    for key, cast in (
        ("strength", float), ("ttl_days", int), ("freshness_until", int),
        ("access_count", int), ("confirmed_count", int), ("failure_count", int),
        ("created_at", int), ("updated_at", int),      # a dump's age too (INV-06)
        ("last_accessed_at", int), ("last_decayed_at", int),
    ):
        if meta.get(key) is not None:     # a value that is not one fails the file (INV-14)
            try:
                out[key] = _number(meta[key], cast)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"invalid {key} {meta[key]!r}") from None
    return out


def _is_auto_memory(meta: dict) -> bool:
    """A Claude Code auto-memory: node_type, which a dump carries too, without
    exported_at (skipping auto-memories skipped every dump, *r09 opus review*)."""
    return (meta.get("metadata") or {}).get("node_type") == "memory" and "exported_at" not in meta


def _is_dump(meta: dict) -> bool:
    """A skillmem dump, which names every field: export also stamps exported_at.
    An auto-memory is a note, or import-vault cleared what it does not state (INV-14)."""
    return (meta.get("metadata") or {}).get("node_type") == "memory" and "exported_at" in meta


def _title_from(meta: dict, body: str, fallback: str) -> str:
    if _is_dump(meta) and isinstance(meta.get("description"), str):
        return meta["description"]     # exact: it is in the content hash
    for key in ("title", "description"):
        if key in meta:     # the key names the title: "" or null clears it (INV-14)
            return (_scalar(meta[key], key) or "").strip()
    if meta.get("name"):
        return _scalar(meta["name"], "name").strip()
    return _first_line(body, fallback)


def _store_asset(asset: Path, assets_root: Path) -> str:
    # one read: the name is the hash of the bytes published, or an editor's
    # save between hashing and copying filed new bytes under the old hash
    data = asset.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    sub = assets_root / digest[:2]
    sub.mkdir(parents=True, exist_ok=True)
    dest = sub / (digest + _filename_key(asset.suffix))
    # content-addressed, so a file under this name is complete only if its bytes
    # hash to it: a copy that died halfway left one a retry took as done (INV-16)
    if intact_asset(dest) is None:
        _publish(dest, data)
    return dest.relative_to(assets_root.parent).as_posix()   # stored: one spelling on every OS


_ATTACHMENT_RE = re.compile(r"!\[\[([^\]\n]+?)\]\]")


def _named(pool: list[Path], parts, name: tuple[str, ...]) -> list[Path]:
    """The paths in ``pool`` whose ``parts`` spell ``name`` in any case, the
    exact spelling first, including canonical Unicode aliases (INV-11)."""
    folded = tuple(_filename_key(part) for part in name)
    return sorted((c for c in pool if tuple(_filename_key(part) for part in parts(c)) == folded),
                  key=lambda c: parts(c) != name)


def _collect_attachments(root: Path, current_dir: Path, body: str,
                         listed: Iterable[str] = (), assets_root: Path | None = None) -> list[Path]:
    out: list[Path] = []
    # `listed`: the paths a skillmem dump records (export writes each asset
    # under its stored name, which the body's `![[name]]` no longer matches)
    targets = [m.group(1).split("|", 1)[0].strip() for m in _ATTACHMENT_RE.finditer(body)]
    listed = list(listed)
    files = sorted(root.rglob("*"))
    store = sorted(assets_root.rglob("*")) if assets_root and listed else []
    for n, target in enumerate([*targets, *listed]):
        if not target:
            continue
        # Beside the note first, then anywhere in the vault, then (a listed
        # path) the store's own copy of one the dump lost. Matched by name
        # (_named), never by glob or by asking the file system (INV-11). A
        # note is untrusted text: nothing from beyond the vault or the store.
        exact, tail = Path(os.path.normpath(current_dir / target)).parts, Path(target).parts
        for base, candidate in (*((root, c) for c in _named(files, lambda c: c.parts, exact)),
                                *((root, c) for c in _named(files, lambda c: c.parts[-len(tail):], tail)),
                                *((assets_root, c) for c in (_named(
                                    store, lambda c: c.relative_to(assets_root.parent).parts, tail)
                                    if n >= len(targets) else ()))):
            # a stored name whose bytes hash elsewhere is not that attachment
            if (not os.path.isfile(candidate) or os.path.islink(candidate)
                    or intact_asset(candidate) is None):
                continue
            try:
                candidate.resolve().relative_to(base.resolve())
            except ValueError:
                continue
            if _filename_key(candidate.suffix) in ASSET_EXTS:
                out.append(candidate)
                break
        else:
            raise ValueError(f"attachment {target!r} has no readable, supported copy in {root} or the store")
    return out


@dataclass
class VaultReport(ImportReport):
    # slugs a dump asked to retire that only the owner may retire
    skipped_archive: list[str] = field(default_factory=list)


def import_vault(
    conn,
    root: Path,
    *,
    kind: str = "document",
    project_override: str | None = None,
    skip_auto_memories: bool = True,
    default_origin: str = "owner",
) -> VaultReport:
    """Bulk-import an Obsidian vault. All upserts run inside a single outer
    transaction so a 1000-file vault is one commit, not 1000 (with per-file
    SAVEPOINTs so individual failures still roll back cleanly)."""
    report = VaultReport()
    root = Path(root)
    assets_root = S.default_data_dir() / "assets"

    with S.tx(conn):
        _run_import(conn, root, assets_root, kind, project_override,
                    skip_auto_memories, report, default_origin)
    return report


def _run_import(conn, root, assets_root, kind, project_override,
                skip_auto_memories, report, default_origin="owner") -> None:
    claimed: dict[str, Path] = {}    # slug -> the file that wrote it in this run
    for path, refused in iter_notes(root, SUPPORTED_TEXT):
        rel = path.relative_to(root).as_posix()   # one spelling on every OS, as packs report
        if refused:
            report.failed.append((rel, refused))
            continue
        try:
            # One file is all-or-nothing: a refusal partway through left the
            # row written with its lifecycle, pin and counters unapplied. tx()
            # nests as a SAVEPOINT inside the importer's own transaction.
            with S.tx(conn):
                raw = path.read_bytes().decode("utf-8-sig")   # a BOM hid the frontmatter
                meta, body = _parse_md(raw.replace("\r\n", "\n").replace("\r", "\n"))
                if _is_dump(meta) and raw.startswith("---\n"):
                    meta, body = _parse_md(raw)    # a dump's body keeps its line endings
                if skip_auto_memories and _is_auto_memory(meta):
                    report.skipped += 1
                    continue
                slug = _slug_from_meta_or_path(meta, root, path)
                if meta.get("truncated"):     # an excerpt, not the record
                    report.failed.append(
                        (rel,
                         "truncated dump: body is an excerpt, not the record; "
                         "restore the body file first"))
                    continue
                if claimed.setdefault(slug, path) != path:   # `Build Steps.md`, `Build-Steps.md`
                    report.failed.append(
                        (rel,
                         f"{slug!r} was already imported from "
                         f"{claimed[slug].relative_to(root).as_posix()}; rename one"))
                    continue
                # Frontmatter or --project names the project, null and "" too; the
                # folder (not a dump's: that is its kind) is only the insert default (INV-14)
                named_project = project_override is not None or "project" in meta
                project = (
                    _scalar(project_override if project_override is not None
                            else meta.get("project"), "project")
                    if named_project or _is_dump(meta)
                    else _project_from_path(root, path)
                )
                title = _title_from(meta, body, slug)
                md = meta.get("metadata") or {}
                dump = _is_dump(meta)
                named_kind = _file_kind(meta)
                item_kind = kind if named_kind is None else named_kind
                extras = _restore_meta(meta)
                if _scalar(md.get("originSessionId"), "originSessionId"):
                    extras["source_session"] = _scalar(md["originSessionId"], "originSessionId")
                # only a dump that RECORDS the pin may change it (a pre-0.11 dump has no key)
                pinned = None
                if dump and "pinned" in meta:
                    if not isinstance(meta["pinned"], bool):   # INV-14
                        raise ValueError(f"invalid pinned {meta['pinned']!r}")
                    pinned = bool(meta["pinned"])
                # and every other state it records: `owner_seal: 'false'` sealed,
                # `lifecycle: Archived` came back active, `origin: bogus` the default
                if dump:
                    if not isinstance(md.get("owner_seal", False), bool):
                        raise ValueError(f"invalid owner_seal {md['owner_seal']!r}")
                    if "origin" in md and md["origin"] not in S.ORIGINS:
                        raise ValueError(f"invalid origin {md['origin']!r}")
                    if meta.get("lifecycle", "active") not in ("active", "stale", "archived"):
                        raise ValueError(f"invalid lifecycle {meta['lifecycle']!r}")

                attachments: list[str] = []
                # the key names the list; embeds stand in only without it (INV-14)
                listed = _str_list(meta.get("attachments"), "attachments")
                embeds = "" if "attachments" in meta or dump else body
                for asset in _collect_attachments(root, path.parent, embeds, listed,
                                                  assets_root):
                    stored = _store_asset(asset, assets_root)
                    if stored not in attachments:
                        attachments.append(stored)

                dump_origin = str(md.get("origin") or "") if dump else ""
                item = S.MemoryItem(
                    # A dump restores its recorded origin exactly, but `owner` only
                    # with a person at the terminal (it mints the seal); a plain
                    # file may lower its origin, never raise it (_origin_from).
                    origin=(dump_origin if dump_origin in S.ORIGINS
                            and (dump_origin != "owner" or S.owner_present())
                            else _migrate_origin(conn, slug, meta, named_kind, kind, default_origin)),
                    slug=slug,
                    kind=item_kind,
                    title=title,
                    body=body,
                    project=project,
                    attachments=attachments,
                    # upsert mints it only with the owner at the terminal (INV-02)
                    owner_seal=1 if dump and md.get("owner_seal") else 0,
                    **extras,
                )
                existed = conn.execute(
                    "SELECT lifecycle, deleted_at FROM memory_items WHERE slug = ?", (slug,)
                ).fetchone()
                if dump:
                    # A dump is the whole record and names every field; earned
                    # counters and ages only when it has them, the recency clocks
                    # and the deadline by their key (null is "never").
                    by_key = {"last_accessed_at", "last_decayed_at", "freshness_until"}
                    earned = {"access_count", "confirmed_count", "failure_count",
                              "updated_at", "created_at"} | by_key
                    named = S.SURFACES["dump"]["names"] - earned | (earned & extras.keys()) \
                        | (by_key & meta.keys())
                    # a key every export writes names its field by being there: without
                    # it `--kind` and the importer's origin are insert defaults (INV-14)
                    named -= {f for f, key in (("kind", "type"), ("origin", "origin"),
                                               ("source_session", "originSessionId"))
                              if key not in md}
                    # a dump from before the seal lets the origin it gives decide
                    # (INV-06); the default origin a row keeps its own over does not
                    if "owner_seal" not in md and ("origin" in md or not existed):
                        named -= {"owner_seal"}
                    named -= {"strength"} - meta.keys()
                else:
                    # a plain note names what its frontmatter states; --kind and
                    # visibility are insert defaults
                    named = ({"visibility", "tags", "topics", "agent", "ttl_days",
                              "strength"} & (meta.keys() | extras.keys())) \
                        | {k for k, v in (("kind", named_kind is not None),
                                          ("attachments", attachments)) if v} \
                        | {k for k, v in (("project", named_project),
                                          ("attachments", "attachments" in meta),
                                          ("source_session", "originSessionId" in md)) if v}
                want_archived = dump and meta.get("lifecycle") == "archived"
                if (dump and not want_archived and existed and existed["deleted_at"] is None
                        and existed["lifecycle"] == "archived"):
                    # restored before the write, which then puts back the dump's
                    # own strength over set_archived's floor (INV-06)
                    S.set_archived(conn, slug, False, by="import")
                S.upsert(
                    conn, item, surface="dump" if dump else "note",
                    explicit=named,
                    reason="vault import" if existed else None,
                    force=True,
                    actor="import",
                    revive=dump,        # a dump restores a deleted slug too
                )
                # a dump of an archived record restores it archived: unpin (set_archived
                # refuses a pinned row), archive, then pin as the dump or the row says
                if want_archived:
                    was_pinned = conn.execute(
                        "SELECT pinned FROM memory_items WHERE slug = ? AND deleted_at IS NULL",
                        (slug,)).fetchone()
                    if was_pinned and was_pinned["pinned"]:
                        S.set_pinned(conn, slug, False)
                    if S.owner_present():
                        S.set_archived(conn, slug, True, by="import")
                    else:
                        report.skipped_archive.append(slug)
                    if pinned is None and was_pinned and was_pinned["pinned"]:
                        S.set_pinned(conn, slug, True)     # the row's own flag, untouched
                if pinned is not None:
                    S.set_pinned(conn, slug, pinned)
                if existed:
                    report.updated += 1
                else:
                    report.inserted += 1
        except Exception as exc:  # noqa: BLE001
            report.failed.append((rel, repr(exc)))
