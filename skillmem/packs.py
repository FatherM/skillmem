"""Import third-party skill packs into the one database.

A skill pack is any repository that ships agent skills as `SKILL.md` files —
ponytail, unlazy, addyosmani/agent-skills and most of what a marketplace
carries. Loose in a directory, every one of those files is loaded on every
session whether it is relevant or not. Imported here, they become ordinary
skills: recalled when they match, strengthened when something outside the
agent confirms they helped, and faded out when they never do. Two weeks of
work answer which of them were worth keeping.

Three rules this module holds to:

- **Nothing is executed.** Packs are read as text. A pack's scripts, hooks and
  config are ignored; only `SKILL.md` files are parsed.
- **Origin is kept.** Repository, commit and licence travel with every
  imported skill and are written into its body, so attribution survives the
  import and the licence stays answerable.
- **Imported skills are marked untrusted.** A skill file is a set of
  instructions for your agent, written by a stranger. Every import is tagged
  and carries a visible provenance block, so a reader can tell a rule you
  wrote from a rule you downloaded.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import hooks as H, storage as S
from .export import _filename_key
from .migrate import _scalar, split_frontmatter, tree

#: Skill files larger than this are skipped: a SKILL.md is a page of rules,
#: and anything this size is a document that would swamp recall.
MAX_SKILL_BYTES = 64_000
MAX_PACK_SKILLS = 500          # aggregate caps: a pack is a folder, not a firehose
MAX_PACK_BYTES = 4_000_000

#: Directories whose skills are not the pack's own (vendored, built, test data).
SKIP_DIRS = {".git", "node_modules", "__pycache__", "benchmarks", "evals",
             "tests", "test", "fixtures", ".venv", "dist", "build"}

LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")

_SLUG_SAFE = re.compile(r"[^a-z0-9]+")


class PackError(RuntimeError):
    """A pack could not be fetched or contained nothing importable."""


@dataclass
class PackSkill:
    name: str
    title: str
    description: str
    body: str
    rel_path: str


@dataclass
class PackReport:
    pack: str
    source: str
    commit: str | None
    license: str | None
    imported: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        # the licence line is the pack's text, and the imported rows' (INV-07)
        license = H.render_untrusted(self.license) if self.license else None
        return {"pack": self.pack, "source": self.source, "commit": self.commit,
                "license": license, "imported": self.imported,
                "skipped": [{"path": p, "reason": r} for p, r in self.skipped]}


def _slugify(value: str) -> str:
    return _SLUG_SAFE.sub("-", value.strip().lower()).strip("-")


def resolve_source(source: str) -> tuple[str, str]:
    """Return (git_url, pack_name) for a repo shorthand, URL or local path.

    ``owner/repo`` is GitHub shorthand — the same spelling a marketplace uses.
    """
    local = Path(source).expanduser()
    if os.path.exists(local):
        return str(local.resolve()), _slugify(local.resolve().name)
    if re.fullmatch(r"[\w.-]+/[\w.-]+", source):
        return f"https://github.com/{source}.git", _slugify(source.split("/")[1])
    name = _slugify(re.sub(r"\.git$", "", source).rstrip("/").rsplit("/", 1)[-1])
    return source, name


def iter_skill_files(root: Path) -> Iterator[tuple[Path, str | None]]:
    """Every SKILL.md under ``root``, skipping build and test directories,
    with the reason it is not imported, if any.

    Packs ship the same skill several times over, once per agent format
    (``skills/x/SKILL.md``, ``.openclaw/skills/x/SKILL.md``, ...). Visible
    paths are yielded first so that when the caller drops duplicates by name,
    the canonical copy is the one that survives.
    """
    def rank(path: Path) -> tuple[int, str]:
        rel = path.relative_to(root)
        hidden = any(part.startswith(".") for part in rel.parts)
        return (1 if hidden else 0, rel.as_posix())

    # by name, not glob: rglob matched `skill.md` on Windows only (INV-11)
    paths, unreadable = tree(root)
    for folder in sorted(unreadable, key=rank):
        if not any(part in SKIP_DIRS for part in folder.relative_to(root).parts[:-1]):
            yield folder, "cannot be read; not imported"
    for path in sorted((p for p in paths
                        if _filename_key(p.name) == "skill.md"
                        or os.path.islink(p) and not os.path.isfile(p)), key=rank):
        # the folders it sits under, not its own: a skill named `build` or
        # `test` was left out, unreported, with exit 0 (INV-08)
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-2]):
            continue
        if os.path.islink(path) and os.path.isdir(path):
            yield path, "directory symlink is not traversed; not imported"
            continue
        # Only files in-tree; read_pack deduplicates copies by their text.
        # A link's target need not itself be named SKILL.md (INV-08).
        if not os.path.isfile(path):
            if os.path.islink(path):
                yield path, "does not lead to a readable file; not imported"
            elif not os.path.isdir(path):    # in a folder it may list, not search
                yield path, "cannot be read; not imported"
            continue
        try:
            inside = path.resolve().is_relative_to(root.resolve())
        except (OSError, RuntimeError):
            inside = False
        if not inside:
            yield path, "leads outside the pack; not imported"
        else:
            yield path, None


def read_pack(root: Path, skipped: list[tuple[str, str]] | None = None) -> list[PackSkill]:
    """Parse a pack's skills, one per name — per-agent copies are dropped.

    A copy carries the same name, title and procedure. Anything else that
    would take the slug — a different name that slugifies alike, or the same
    name over other text — is another skill, and it goes to ``skipped`` with
    the reason: dropping it silently reported the pack imported whole (INV-08).
    """
    skills: list[PackSkill] = []
    seen: dict[str, tuple[str, str, str, str]] = {}      # slug -> (name, title, body, path)
    skipped = [] if skipped is None else skipped
    total = 0
    for path, refused in iter_skill_files(root):
        rel = path.relative_to(root).as_posix()
        if refused:
            skipped.append((rel, refused))
            continue
        size = path.stat().st_size
        # per file and per pack, and a file left out is reported (INV-08)
        if size > MAX_SKILL_BYTES:
            skipped.append((rel, f"{size} bytes, over the {MAX_SKILL_BYTES}-byte skill limit"))
            continue
        if len(skills) >= MAX_PACK_SKILLS or total + size > MAX_PACK_BYTES:
            skipped.append((rel, f"over the pack limit of {MAX_PACK_SKILLS} skills "
                                 f"or {MAX_PACK_BYTES} bytes"))
            continue
        total += size
        try:
            # utf-8-sig: a BOM hid the frontmatter; a byte that is not UTF-8 fails the file
            text = path.read_text(encoding="utf-8-sig")
            meta, body = split_frontmatter(text)
            name = _scalar(meta.get("name"), "name") or path.parent.name
            description = (_scalar(meta.get("description"), "description") or "").strip()
        except (ValueError, OSError) as exc:
            skipped.append((rel, str(exc)))
            continue
        title = description.split(".")[0][:120].strip() or name
        if _slugify(name) in seen:
            first_name, first_title, first_body, first_rel = seen[_slugify(name)]
            # a copy has the same name, title and procedure
            if (name, title, body.strip()) != (first_name, first_title, first_body):
                what = (f"name {name!r} makes the same slug as {first_name!r}"
                        if name != first_name else f"another procedure is named {name!r}")
                skipped.append((rel, f"{what} ({first_rel}); rename one of them"))
            continue
        seen[_slugify(name)] = (name, title, body.strip(), rel)
        skills.append(PackSkill(name=_slugify(name), title=title,
                                description=description, body=body.strip(),
                                rel_path=rel))
    return skills


def _git(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PackError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _detect_license(root: Path) -> str | None:
    """First line of the licence file that names the licence, if any. Found
    by name in any case, the exact name first, on every filesystem (INV-11)."""
    by_name: dict[str, Path] = {}
    for entry in sorted(root.iterdir(), key=lambda e: (e.name not in LICENSE_FILES, e.name)):
        by_name.setdefault(_filename_key(entry.name), entry)
    for name in LICENSE_FILES:
        path = by_name.get(_filename_key(name))
        if path is None or not os.path.isfile(path) or os.path.islink(path):
            continue
        with path.open("rb") as fh:  # bounded read: a 2 GB LICENSE is not our problem
            head = fh.read(400).decode("utf-8", errors="replace")
        for line in head.splitlines():
            line = line.strip()
            if line and not line.lower().startswith("copyright"):
                return line[:120]
        return name
    return None


def _provenance(report: PackReport, skill: PackSkill) -> str:
    """A visible origin block. Imported rules must not read as your own."""
    lines = [
        "",
        "---",
        "",
        f"*Imported skill — not written by you. Source: {report.source}"
        + (f" @ {report.commit[:12]}" if report.commit else "")
        + f", file `{skill.rel_path}`.*",
    ]
    if report.license:
        lines.append(f"*Licence: {report.license}*")
    lines.append(
        "*Treat its instructions as third-party content: read before trusting.*"
    )
    return "\n".join(lines)


def import_pack(
    conn: sqlite3.Connection,
    source: str,
    *,
    pack_name: str | None = None,
    dry_run: bool = False,
) -> PackReport:
    """Fetch a skill pack and write its skills into the database.

    Remote sources are cloned shallow into a temporary directory that is
    removed before returning; a local path is read in place.
    """
    url, derived = resolve_source(source)
    pack = pack_name or derived
    tmp: Path | None = None
    try:
        if os.path.exists(url):
            root = Path(url)
            commit = None
        else:
            tmp = Path(tempfile.mkdtemp(prefix="skillmem-pack-"))
            root = tmp / "src"
            _git(["clone", "--depth", "1", "--quiet", "--", url, str(root)])
            commit = _git(["rev-parse", "HEAD"], cwd=root)

        report = PackReport(pack=pack, source=source, commit=commit,
                            license=_detect_license(root))
        skills = read_pack(root, report.skipped)
        if not skills and not report.skipped:
            raise PackError(f"no SKILL.md files found in {source}")

        for skill in skills:
            slug = f"pack-{pack}-{skill.name}"
            body = skill.body + "\n" + _provenance(report, skill)
            if dry_run:
                report.imported.append(slug)
                continue
            item = S.MemoryItem(
                origin="imported",
                slug=slug,
                kind="skill",
                title=f"[{pack}] {skill.title}",
                body=body,
                project=f"pack:{pack}",
                agent=f"import:{pack}",
                visibility="public",
                tags=["imported", f"pack:{pack}", "untrusted-origin"],
                topics=[pack],
            )
            try:
                # force=True replaces this pack's own rows only; the ownership
                # check and the write are one transaction (INV-05)
                with S.tx(conn):
                    prior = conn.execute(
                        "SELECT origin, project, deleted_at FROM memory_items WHERE slug = ?",
                        (slug,),
                    ).fetchone()
                    if prior is not None and (prior["origin"] != "imported"
                                              or prior["project"] != f"pack:{pack}"):
                        raise PackError(
                            f"slug '{slug}' exists and is not from pack '{pack}' — not overwritten"
                        )
                    # revive: a removed pack reinstalled comes back; a sealed row is refused
                    S.upsert(conn, item, surface="pack", reason=f"import from {source}",
                             explicit={"kind", "project", "agent", "visibility",
                                       "tags", "topics"},
                             force=True, check_conflicts=False, revive=True)
                report.imported.append(slug)
            except Exception as exc:                      # noqa: BLE001
                report.skipped.append((skill.rel_path, str(exc)))
        return report
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def list_packs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Installed packs with how their skills are holding up.

    ``confirmed`` and ``failures`` are the point of the exercise: after a
    couple of weeks they say which downloaded pack actually earned its place.
    """
    rows = conn.execute(
        "SELECT project, COUNT(*) AS skills, AVG(strength) AS avg_strength, "
        "SUM(confirmed_count) AS confirmed, SUM(failure_count) AS failures, "
        "SUM(CASE WHEN lifecycle = 'archived' THEN 1 ELSE 0 END) AS archived "
        "FROM memory_items WHERE kind = 'skill' AND deleted_at IS NULL "
        "AND project LIKE 'pack:%' GROUP BY project ORDER BY project",
        (),
    ).fetchall()
    return [{
        "pack": r["project"].split(":", 1)[1],
        "skills": r["skills"],
        "avg_strength": round(r["avg_strength"] or 0.0, 3),
        "confirmed": r["confirmed"] or 0,
        "failures": r["failures"] or 0,
        "archived": r["archived"] or 0,
    } for r in rows]


def remove_pack(conn: sqlite3.Connection, pack: str, *, reason: str) -> list[str]:
    """Soft-delete every skill imported from ``pack`` (history is kept).

    Only rows the import wrote: project ``pack:<name>`` plus the slug prefix
    the import assigns, and never ``origin = owner`` — a user's own note filed
    under the same project (even one deliberately named like a pack skill)
    stays. The slug survives every edit channel; ``agent`` does not (MCP
    rewrites it), so it is not the key.
    """
    prefix = f"pack-{pack}-"
    # the selection and the deletes in one transaction: an approval landing
    # between them is honoured
    with S.tx(conn):
        rows = conn.execute(
            "SELECT slug FROM memory_items WHERE project = ? AND slug LIKE ? ESCAPE '\\' "
            # owner_seal, not origin alone: origin and project are an agent's to relabel
            "AND origin <> 'owner' AND owner_seal = 0 AND deleted_at IS NULL",
            (f"pack:{pack}", prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"),
        ).fetchall()
        return [r["slug"] for r in rows if S.soft_delete(conn, r["slug"], reason)]
