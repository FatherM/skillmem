"""HTTP API for shared multi-agent access.

Mirrors the MCP tools as REST endpoints with bearer-token auth and visibility
scoping. Bound to 127.0.0.1 by default; expose with care.

Token file format (YAML)::

    admin:                        # master — sees everything
      token: <random>
      scope: master
    researcher:
      token: <random>
      permissions: [write_public]               # may write/edit public rows
      topics: [research, docs]                  # 'shared' rows must match
    analyst:
      token: <random>
      topics: [research, metrics]

Visibility rules:
- ``public`` — everyone.
- ``shared`` — its author, and agents whose ``topics`` intersect the row's topics.
- ``private`` — only the author (``agent`` column).
- ``master`` scope — bypasses all of the above.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

import uvicorn
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StrictInt

from . import storage as S
from . import __version__
from .hooks import frame_for_model, frame_history, frame_title


# --------------------------------------------------------------------------- #
# token loading
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgentIdentity:
    name: str
    token: str
    scope: str = "agent"  # 'master' | 'agent'
    topics: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()  # e.g. {'write_public'}

    @property
    def is_master(self) -> bool:
        return self.scope == "master"

    def can(self, permission: str) -> bool:
        return self.is_master or permission in self.permissions


class TokenStore:
    """In-memory bearer-token → agent lookup, reloadable on SIGHUP."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._by_token: dict[str, AgentIdentity] = {}
        self.reload()

    def reload(self) -> None:
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("tokens file must be a YAML mapping of agent → config")
        bucket: dict[str, AgentIdentity] = {}
        for name, cfg in raw.items():
            if not isinstance(name, str) or not name:   # "" is stored as no author: 404 on its own write
                raise ValueError(f"agent name {name!r} must be a non-empty string")
            if not isinstance(cfg, dict) or "token" not in cfg:
                raise ValueError(f"agent '{name}' missing 'token'")
            scope = cfg.get("scope", "agent")
            topics = tuple(cfg.get("topics", []) or [])
            perms = frozenset(cfg.get("permissions", []) or [])
            ident = AgentIdentity(
                name=name, token=cfg["token"],
                scope=scope, topics=topics, permissions=perms,
            )
            if ident.token in bucket:
                raise ValueError(f"duplicate token for agents {bucket[ident.token].name} and {name}")
            bucket[ident.token] = ident
        self._by_token = bucket

    def resolve(self, token: str) -> AgentIdentity | None:
        return self._by_token.get(token)


# --------------------------------------------------------------------------- #
# visibility filter — applied to every result row in the HTTP layer
# --------------------------------------------------------------------------- #


def _predicate(agent: "AgentIdentity"):
    """The visibility predicate for the storage rankers — or None for master,
    which takes the unfiltered path and ranks as the CLI and MCP do."""
    if agent.is_master:
        return None
    return lambda m: _visible_to(m, agent)


def _visible_to(row: dict[str, Any] | S.MemoryItem, agent: AgentIdentity) -> bool:
    if agent.is_master:
        return True
    visibility, author = _owner_of(row)
    topics = row.topics if isinstance(row, S.MemoryItem) else row.get("topics") or []
    if visibility == "public":
        return True
    if visibility == "shared":     # its author too (INV-08)
        return author == agent.name or any(t in agent.topics for t in topics)
    if visibility == "private":
        return author == agent.name
    return False


def _owner_of(row: dict[str, Any] | S.MemoryItem) -> tuple[str, str | None]:
    """(visibility, author) of a row or an item."""
    if isinstance(row, S.MemoryItem):
        return row.visibility, row.agent
    return row.get("visibility", "private"), row.get("agent")


def _may_write(row: dict[str, Any] | S.MemoryItem, agent: AgentIdentity) -> bool:
    """Write permission for an EXISTING record — stricter than _visible_to.

    Read access answers "may I see this"; this answers "may I overwrite it".
    Public records are team-wide, so they need the same 'write_public'
    permission /write demands; everything else belongs to its author.
    """
    if agent.is_master:
        return True
    visibility, author = _owner_of(row)
    if visibility == "public":
        return agent.can("write_public")
    return author == agent.name


@contextmanager
def _refusals():
    """A storage refusal as its HTTP status: the owner's record 403, a
    conflict 409, an invalid field 422."""
    try:
        yield
    except S.SealedRecord as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except S.MemoryConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# --------------------------------------------------------------------------- #
# request / response shapes
# --------------------------------------------------------------------------- #


class SearchRequest(BaseModel):
    query: str
    kind: str | None = None
    project: str | None = None
    limit: int = Field(10, ge=1, le=100)


class CreateRequest(BaseModel):
    # Defaults are not validated: only omission gets the internal None sentinel.
    # A supplied value must be one of these strings, including on same-text retries.
    visibility: Literal["public", "shared", "private"] = None


class WriteRequest(CreateRequest):
    slug: str
    title: str
    body: str
    kind: str = "note"
    project: str | None = None
    tags: list[str] | None = None       # null clears, as over MCP and /update
    topics: list[str] | None = None
    ttl_days: StrictInt | None = None   # a lax int stored `true` as one day (INV-14)
    check_conflicts: bool = True


class UpdateRequest(BaseModel):
    body: str
    reason: str
    title: str | None = None
    kind: str | None = None
    project: str | None = None
    topics: list[str] | None = None
    tags: list[str] | None = None


class ListRequest(BaseModel):
    kind: str | None = None
    project: str | None = None
    limit: int = Field(50, ge=1, le=500)


class LearnRequest(CreateRequest):
    slug: str
    title: str
    trigger: str
    steps: str
    outcome: str
    lessons: str | None = None
    project: str | None = None
    tags: list[str] | None = None       # null clears, as over MCP and /update
    topics: list[str] | None = None
    ttl_days: StrictInt | None = None   # a lax int stored `true` as one day (INV-14)
    check_conflicts: bool = True


class RecallRequest(BaseModel):
    query: str
    limit: int = Field(5, ge=1, le=50)
    auto_reinforce: bool = True


# --------------------------------------------------------------------------- #
# server build
# --------------------------------------------------------------------------- #


def build_app(token_store: TokenStore, db_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="skillmem", version=__version__)
    bearer = HTTPBearer(auto_error=True)

    # Every write endpoint makes its permission check and its write inside one
    # S.tx: a check before the lock acted on a stale answer (INV-05).

    @contextmanager
    def get_conn() -> Iterator[sqlite3.Connection]:
        # One connection per request, closed on the thread that opened it. A
        # per-thread connection kept for reuse was never closed, and Windows
        # cannot delete, move or purge a database file while a handle is open.
        conn = S.connect(db_path)
        try:
            S.init_schema(conn)
            yield conn
        finally:
            conn.close()

    def get_agent(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> AgentIdentity:
        ident = token_store.resolve(credentials.credentials)
        if ident is None:
            raise HTTPException(status_code=401, detail="invalid bearer token")
        return ident

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.post("/whoami")
    def whoami(agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        return {"agent": agent.name, "scope": agent.scope, "topics": list(agent.topics)}

    @app.post("/search")
    def search(req: SearchRequest, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        with get_conn() as conn:
            # the filter runs inside the ranking; the post-check stays as a belt
            hits = S.search(conn, req.query, kind=req.kind, project=req.project, limit=req.limit,
                            visible=_predicate(agent))
            filtered = [frame_for_model(h, dict(h)) for h in hits if _visible_to(h, agent)]
            return {"count": len(filtered), "results": filtered, "agent": agent.name}

    @app.get("/get/{slug:path}")
    def get_one(slug: str, include_history: bool = False,
                agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        with get_conn() as conn:
            # the visibility check and the history read see one state (INV-04)
            record = S.read_record(conn, slug, with_history=include_history)
            if not record or not _visible_to(record["item"], agent):
                raise HTTPException(status_code=404, detail="not found")
            item = record["item"]
            payload = item.to_dict()
            payload["body"] = record["body"]
            payload["links_out"] = record["links_out"]
            frame_for_model(item, payload)
            # a backlink names its source: only the ones the caller could read
            payload["links_in"] = [row.slug for row in record["links_in"] if _visible_to(row, agent)]
            if include_history:
                payload["history"] = [frame_history(h) for h in record["history"]]
            return payload

    @app.post("/list")
    def list_(req: ListRequest, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        with get_conn() as conn:
            items = S.list_items(conn, kind=req.kind, project=req.project, limit=req.limit,
                                 visible=_predicate(agent))
            visible = [i for i in items if _visible_to(i, agent)]
            return {
                "count": len(visible),
                "items": [
                    {"slug": i.slug, "kind": i.kind, "title": frame_title(i),
                     "project": i.project, "updated_at": i.updated_at,
                     "visibility": i.visibility, "origin": i.origin,
                     "trusted": bool(i.trusted_at)}
                    for i in visible
                ],
            }

    def _gate_create(conn, item: S.MemoryItem, agent: AgentIdentity,
                     default_visibility: str = "private") -> bool:
        """One authorization for every create path (/write, /learn). A create
        on an existing slug, a tombstone's included, is an update in disguise:
        it keeps the row's visibility and needs the permission /update does."""
        row = conn.execute(
            "SELECT * FROM memory_items WHERE slug = ?", (item.slug,)
        ).fetchone()
        if row is None:
            if item.visibility is None:
                item.visibility = default_visibility
            _require_visibility_perm(item.visibility, agent)
            return True        # authorised on a free slug: the write must create
        existing = S.MemoryItem.from_row(row)
        if row["deleted_at"] is not None:
            raise HTTPException(
                status_code=409,
                detail="slug belongs to a deleted record; pick another slug",
            )
        if not _may_write(existing, agent):
            raise HTTPException(
                status_code=403,
                detail="slug exists and belongs to another agent or is public; "
                       "use /update with the right permission",
            )
        if item.visibility is None:
            item.visibility = existing.visibility   # omitted → keep
        elif item.visibility != existing.visibility:
            raise HTTPException(
                status_code=409,
                detail=f"record is {existing.visibility}; a create call cannot "
                       "change visibility",
            )
        _require_visibility_perm(item.visibility, agent)
        return False

    def _require_visibility_perm(visibility: str, agent: AgentIdentity) -> None:
        if visibility == "public" and not agent.can("write_public"):
            raise HTTPException(
                status_code=403,
                detail="agent lacks 'write_public' permission (or master scope)",
            )

    @app.post("/write")
    def write(req: WriteRequest, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        with get_conn() as conn:
            with S.tx(conn):
                item = S.MemoryItem(
                    slug=req.slug, kind=req.kind, title=req.title, body=req.body,
                    project=req.project, tags=req.tags or [], topics=req.topics or [],
                    visibility=req.visibility, agent=agent.name, ttl_days=req.ttl_days,
                    origin="agent",
                )
                fresh = _gate_create(conn, item, agent)
                with _refusals():
                    result = S.upsert(
                        conn, item, surface="http",
                        check_conflicts=req.check_conflicts,
                        conflict_filter=_predicate(agent),
                        create_only=fresh,
                        explicit=req.model_fields_set & {"kind", "project", "tags", "topics",
                                                         "ttl_days", "visibility"},
                    )
                return {"ok": True, "slug": result.slug, "id": result.id, "agent": agent.name}

    @app.post("/update/{slug:path}")
    def update(slug: str, req: UpdateRequest,
               agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        with get_conn() as conn:
            with S.tx(conn):
                existing = S.get(conn, slug)
                if not existing or not _visible_to(existing, agent):
                    raise HTTPException(status_code=404, detail="not found")
                if not _may_write(existing, agent):
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "agent lacks permission to update this record "
                            "(public requires 'write_public'; otherwise only the author or master)"
                        ),
                    )
                item = S.MemoryItem(
                    slug=slug, body=req.body,
                    title=(req.title or "") if "title" in req.model_fields_set else existing.title,
                    kind=req.kind if "kind" in req.model_fields_set else existing.kind,
                    project=req.project, topics=req.topics or [],
                    tags=req.tags or [],
                    origin="agent",     # the words are an agent's now
                )
                with _refusals():
                    result = S.upsert(
                        conn, item, surface="http", reason=req.reason,
                        actor=f"http:{agent.name}",     # the surface stamps itself
                        explicit=req.model_fields_set & {"kind", "project", "tags", "topics"},
                    )
                return {"ok": True, "slug": result.slug}

    @app.post("/learn")
    def learn(req: LearnRequest, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        with get_conn() as conn:
            with S.tx(conn):
                item = S.MemoryItem(
                    slug=req.slug, kind="skill", title=req.title,
                    body=S.skill_body(req.trigger, req.steps, req.outcome, req.lessons),
                    project=req.project,
                    tags=req.tags or [], topics=req.topics or [],
                    visibility=req.visibility, agent=agent.name,
                    ttl_days=req.ttl_days, origin="agent",
                )
                fresh = _gate_create(conn, item, agent, default_visibility="public")
                with _refusals():
                    result = S.upsert_skill(
                        conn, item, surface="http",
                        check_conflicts=req.check_conflicts,
                        conflict_filter=_predicate(agent),
                        create_only=fresh,
                        explicit=req.model_fields_set & {"project", "tags", "topics",
                                                         "ttl_days", "visibility"},
                    )
                return {"ok": True, "slug": result.slug, "id": result.id, "kind": "skill", "agent": agent.name}

    @app.post("/recall")
    def recall(req: RecallRequest, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        with get_conn() as conn:
            # reinforced after the visibility filter, and asked again under its lock:
            # a skill the caller may not see is never touched
            results = S.recall_skills(conn, req.query, limit=req.limit, auto_reinforce=False,
                                      visible=_predicate(agent))
            visible = [r for r in results if _visible_to(r, agent)]
            if req.auto_reinforce:
                bumped = S.reinforce_retrieved(conn, [r["slug"] for r in visible],
                                               visible=_predicate(agent), surface="http")
                for r in visible:
                    if r["slug"] in bumped:
                        r.update(strength=bumped[r["slug"]]["strength"],
                                 access_count=bumped[r["slug"]]["access_count"])
            for r in visible:
                frame_for_model(r, r)
            return {"count": len(visible), "skills": visible, "agent": agent.name}

    @app.post("/reinforce/{slug:path}")
    def reinforce(slug: str, evidence: str = "self_report",
                  agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        with get_conn() as conn:
            # visibility asked inside S.reinforce, under its lock; one 404 for
            # hidden and missing
            if evidence not in S.EVIDENCE_WEIGHTS:
                raise HTTPException(status_code=422, detail=f"unknown evidence: {evidence}")
            result = S.reinforce(conn, slug, evidence=evidence, visible=_predicate(agent),
                                 surface="http")
            if not result:
                raise HTTPException(status_code=404, detail="not found")
            return result

    @app.post("/decay")
    def decay(days: int = Query(14, ge=1, le=3650),
              agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        if not agent.is_master:
            raise HTTPException(status_code=403, detail="master scope required")
        with get_conn() as conn:
            decayed = S.decay_stale(conn, days_threshold=days)
            sweep = S.sweep_lifecycle(conn)  # same maintenance the CLI run does
            gc = S.gc_body_files(conn)
            return {"decayed": len(decayed), "details": decayed, "lifecycle": sweep,
                    "gc_body_files": gc}

    @app.post("/reload-tokens")
    def reload_tokens(agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        if not agent.is_master:
            raise HTTPException(status_code=403, detail="master scope required")
        token_store.reload()
        return {"ok": True}

    return app


# --------------------------------------------------------------------------- #
# entry point (`skillmem-server`)
# --------------------------------------------------------------------------- #


def _default_tokens_path() -> Path:
    return S.default_data_dir() / "agent_tokens.yaml"


def run() -> None:
    """CLI entry: skillmem-server [--host 127.0.0.1] [--port 7000] [--tokens PATH]"""
    import argparse

    parser = argparse.ArgumentParser(prog="skillmem-server")
    parser.add_argument("--host", default=os.environ.get("SKILLMEM_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SKILLMEM_PORT", "7000")))
    parser.add_argument("--tokens", type=Path,
                        default=Path(os.environ.get("SKILLMEM_TOKENS", _default_tokens_path())))
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()

    if not args.tokens.exists():
        raise SystemExit(
            f"tokens file not found: {args.tokens}\n"
            f"create it via: skillmem tokens-init {args.tokens}"
        )

    store = TokenStore(args.tokens)
    app = build_app(store, db_path=args.db)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    run()
