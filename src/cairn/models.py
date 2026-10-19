"""Request and response models shared by REST, MCP and federation (a local server talks REST to its remote)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EntryType = Literal["general", "gotcha", "decision", "reference", "open_question", "practice"]
ENTRY_TYPES: tuple[str, ...] = EntryType.__args__  # type: ignore[attr-defined]


class SearchRequest(BaseModel):
    concepts: list[str] = Field(min_length=1, max_length=8)
    stores: list[str] | None = None
    types: list[EntryType] | None = None
    within: str | None = None  # node or project ref
    limit: int = Field(12, ge=1, le=50)


class HitOut(BaseModel):
    store: str
    ref: str
    type: str
    title: str
    verified_at: str
    node_ref: str
    node_title: str
    node_summary: str
    node_path: str
    team: str
    project: str
    matched: list[str]
    features: tuple[int, int, int, int, int]  # C, E, S, V, depth — comparable across stores


class SearchResponse(BaseModel):
    hits: list[HitOut]
    unreachable: list[str] = []


class GetRequest(BaseModel):
    refs: list[str] = Field(min_length=1, max_length=20)
    history: bool = False


class Brief(BaseModel):
    ref: str
    title: str
    type: str | None = None
    summary: str | None = None


class Revision(BaseModel):
    rev: int
    title: str
    body: str
    at: str


class Item(BaseModel):
    ref: str
    status: Literal["ok", "archived", "not_found"]
    tombstone: str | None = None
    type: str | None = None
    title: str | None = None
    body: str | None = None
    keywords: list[str] = []
    rev: int | None = None
    inject: bool = False
    verified_at: str | None = None
    node_ref: str | None = None
    path: str | None = None
    summary: str | None = None
    children: list[Brief] = []
    entries: list[Brief] = []
    counts: dict[str, int] = {}
    history: list[Revision] = []


class GetResponse(BaseModel):
    items: list[Item]
    unreachable: list[str] = []


class MapRequest(BaseModel):
    store: str | None = None
    root: str | None = None
    depth: int = Field(2, ge=1, le=8)
    filter: str | None = None


class MapNode(BaseModel):
    ref: str
    title: str
    summary: str
    entries: int
    children: list[MapNode] = []


class MapProject(BaseModel):
    ref: str
    team: str
    name: str
    role: str
    nodes: list[MapNode] = []


class MapStore(BaseModel):
    store: str
    user: str
    projects: list[MapProject] = []


class MapResponse(BaseModel):
    stores: list[MapStore]
    unreachable: list[str] = []


class EntryFields(BaseModel):
    type: EntryType | None = None
    title: str | None = None
    body: str | None = None
    keywords: list[str] | None = None


class NodeFields(BaseModel):
    slug: str | None = None
    title: str | None = None
    summary: str | None = None
    keywords: list[str] | None = None


WriteOp = Literal["add", "update", "archive", "unarchive", "verify", "move", "create_node", "update_node"]


class WriteRequest(BaseModel):
    op: WriteOp
    ref: str | None = None  # the entry or node being changed
    node: str | None = None  # target node: `add`, `move`, parent for `create_node`
    project: str | None = None  # target project for a top-level `create_node` or `move`
    base_revision: int | None = None
    entry: EntryFields | None = None
    node_fields: NodeFields | None = None
    reason: str | None = None
    distinct_from: list[str] = []


class Duplicate(BaseModel):
    ref: str
    title: str
    score: float


class WriteResponse(BaseModel):
    ok: bool
    ref: str | None = None
    rev: int | None = None
    message: str = ""
    duplicates: list[Duplicate] = []


class AdminRequest(BaseModel):
    op: str
    store: str | None = None
    args: dict[str, Any] = {}


class AdminResponse(BaseModel):
    result: dict[str, Any]


class InjectRequest(BaseModel):
    budget: int = Field(8000, ge=500, le=20000)
    render: bool = True


class InjectItem(BaseModel):
    ref: str
    type: str
    title: str
    body: str
    verified_at: str


class InjectGroup(BaseModel):
    label: str  # "<store>" or "<store> · <team>"
    items: list[InjectItem]


class InjectResponse(BaseModel):
    text: str = ""
    groups: list[InjectGroup] = []


class BootstrapRequest(BaseModel):
    token: str
    email: str
    name: str


class ErrorBody(BaseModel):
    code: str
    message: str
    duplicates: list[Duplicate] = []
