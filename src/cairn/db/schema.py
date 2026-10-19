"""Every table, typed once in SQLAlchemy Core. Alembic migrations are generated from this metadata.

Search columns (`an_*`) hold analyzer output: space-separated `[a-z0-9]` tokens (see core/analyzer.py). Postgres
indexes them with expression GIN indexes, SQLite with FTS5 tables; both are created in migrations, not here.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

# bigint on Postgres; INTEGER on SQLite so it aliases the rowid (required for AUTOINCREMENT).
ID = BigInteger().with_variant(Integer(), "sqlite")
TS = DateTime(timezone=True)
# Byte order on Postgres, as on SQLite: locale collations ignore "_" and "/", so slugs, paths and tokens would sort
# (and LIMIT) differently on the two engines.
SLUG = String(48).with_variant(String(48, collation="C"), "postgresql")
PATH = Text().with_variant(Text(collation="C"), "postgresql")
TERM = String(64).with_variant(String(64, collation="C"), "postgresql")


def _id() -> Column[int]:
    return Column("id", ID, primary_key=True, autoincrement=True)


def _fk(name: str, target: str, primary_key: bool = False, nullable: bool = True) -> Column[int]:
    return Column(name, ID, ForeignKey(target, ondelete="CASCADE"), primary_key=primary_key, nullable=nullable)


users = Table(
    "users",
    metadata,
    _id(),
    Column("email", String(320), nullable=False, unique=True),
    Column("name", String(200), nullable=False),
    Column("is_instance_admin", Boolean, nullable=False, default=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("created_at", TS, nullable=False),
)
# Exactly one instance admin (bootstrap relies on it under concurrency).
Index("uq_users_instance_admin", users.c.is_instance_admin, unique=True,
      postgresql_where=users.c.is_instance_admin, sqlite_where=users.c.is_instance_admin)

teams = Table(
    "teams",
    metadata,
    _id(),
    Column("slug", SLUG, nullable=False, unique=True),
    Column("name", String(200), nullable=False),
    Column("archived_at", TS),
)

team_members = Table(
    "team_members",
    metadata,
    _fk("team_id", "teams.id", primary_key=True),
    _fk("user_id", "users.id", primary_key=True),
    Column("role", String(10), nullable=False),  # admin | member
)

projects = Table(
    "projects",
    metadata,
    _id(),
    _fk("team_id", "teams.id", nullable=False),
    Column("slug", SLUG, nullable=False),
    Column("name", String(200), nullable=False),
    Column("archived_at", TS),
    UniqueConstraint("team_id", "slug"),
)

project_members = Table(
    "project_members",
    metadata,
    _fk("project_id", "projects.id", primary_key=True),
    _fk("user_id", "users.id", primary_key=True),
    Column("role", String(10), nullable=False),  # write | read
)

nodes = Table(
    "nodes",
    metadata,
    _id(),
    _fk("project_id", "projects.id", nullable=False),
    _fk("parent_id", "nodes.id"),
    Column("slug", SLUG, nullable=False),
    Column("path", PATH, nullable=False),  # slugs joined by "/"; a label, not the identity
    Column("depth", Integer, nullable=False),
    Column("title", String(160), nullable=False),
    Column("summary", Text, nullable=False, default=""),
    Column("keywords", JSON, nullable=False, default=list),
    Column("rev", Integer, nullable=False, default=1),
    Column("inject", Boolean, nullable=False, default=False),
    Column("created_at", TS, nullable=False),
    Column("created_by", ID, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("updated_by", ID, nullable=False),
    Column("archived_at", TS),
    Column("archived_by", ID),
    Column("archive_reason", Text),
    Column("is_searchable", Boolean, nullable=False, default=True),
    Column("an_head", Text, nullable=False, default=""),  # title + keywords
    Column("an_desc", Text, nullable=False, default=""),  # summary + path
    UniqueConstraint("project_id", "path"),
    sqlite_autoincrement=True,
)

entries = Table(
    "entries",
    metadata,
    _id(),
    _fk("node_id", "nodes.id", nullable=False),
    _fk("project_id", "projects.id", nullable=False),
    Column("type", String(16), nullable=False),
    Column("title", String(160), nullable=False),
    Column("body", Text, nullable=False, default=""),
    Column("keywords", JSON, nullable=False, default=list),
    Column("rev", Integer, nullable=False, default=1),
    Column("inject", Boolean, nullable=False, default=False),
    Column("created_at", TS, nullable=False),
    Column("created_by", ID, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("updated_by", ID, nullable=False),
    Column("verified_at", TS, nullable=False),
    Column("verified_by", ID, nullable=False),
    Column("archived_at", TS),
    Column("archived_by", ID),
    Column("archive_reason", Text),
    Column("is_searchable", Boolean, nullable=False, default=True),
    Column("an_head", Text, nullable=False, default=""),  # title + keywords
    Column("an_body", Text, nullable=False, default=""),
    Index(None, "node_id"),
    Index(None, "project_id"),
    sqlite_autoincrement=True,
)

entry_revisions = Table(
    "entry_revisions",
    metadata,
    _fk("entry_id", "entries.id", primary_key=True),
    Column("rev", Integer, primary_key=True),
    Column("type", String(16), nullable=False),
    Column("title", String(160), nullable=False),
    Column("body", Text, nullable=False),
    Column("keywords", JSON, nullable=False),
    Column("by", ID, nullable=False),
    Column("at", TS, nullable=False),
)

# Document frequency of each token among searchable entries, per project. Drives generic-term detection.
term_df = Table(
    "term_df",
    metadata,
    Column("term", TERM, primary_key=True),
    _fk("project_id", "projects.id", primary_key=True),
    Column("df", Integer, nullable=False),
)

# Every token ever indexed; substring expansion searches it (`term LIKE '%needle%'`, pg_trgm-indexed on Postgres).
vocab = Table("vocab", metadata, Column("term", TERM, primary_key=True))

change_log = Table(
    "change_log",
    metadata,
    Column("seq", ID, primary_key=True, autoincrement=True),
    Column("project_id", ID, nullable=False),
    Column("ref", String(64), nullable=False),
    Column("op", String(32), nullable=False),
    Column("by", ID, nullable=False),
    Column("at", TS, nullable=False),
    Column("summary", Text, nullable=False),
    Index(None, "project_id", "seq"),
    sqlite_autoincrement=True,
)

credentials = Table(
    "credentials",
    metadata,
    _id(),
    _fk("user_id", "users.id"),  # null for the one-time setup token
    Column("kind", String(16), nullable=False),  # api_key | setup
    Column("sha256", String(64), nullable=False, unique=True),
    Column("token_prefix", String(24), nullable=False),
    Column("name", String(100), nullable=False, default=""),
    Column("scopes", JSON, nullable=False, default=dict),  # {read_only, no_admin}
    Column("created_at", TS, nullable=False),
    Column("last_used_at", TS),
    Column("revoked_at", TS),
)

meta = Table("meta", metadata, Column("key", String(64), primary_key=True), Column("value", Text, nullable=False))
