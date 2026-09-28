"""database/schema.sql: generate it from the models and use it to create the database.

* :func:`generate_schema_sql` renders MySQL/MariaDB DDL from the SQLAlchemy
  models with SQLAlchemy's MySQL dialect (``python run.py export-schema``).
  Indexes are written *inside* each ``CREATE TABLE`` so the whole file is
  idempotent (``CREATE INDEX IF NOT EXISTS`` does not exist in MySQL 8).
* :func:`init_db` creates the tables (``python run.py init-db``): on
  MySQL/MariaDB it executes ``database/schema.sql`` statement by statement,
  on SQLite it calls ``db.create_all()``.
* :func:`friendly_db_error` turns driver errors into advice a student can act on.

tests/test_schema.py fails when schema.sql is out of date with the models.
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.config import BASE_DIR
from app.extensions import db

SCHEMA_PATH = BASE_DIR / "database" / "schema.sql"
DEFAULT_DATABASE_NAME = "pawbalik"
TABLE_OPTIONS = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"

HEADER = """\
-- =============================================================================
-- PawBalik database schema (MySQL 8 / MariaDB 10.4+, InnoDB, utf8mb4)
--
-- GENERATED FILE - do not edit by hand.
-- Source: the SQLAlchemy models in app/models/.
-- Regenerate after changing a model:   python run.py export-schema
-- Create the tables:                   python run.py init-db
--   (or import this file in phpMyAdmin: Import > choose schema.sql > Go)
--
-- Every statement uses IF NOT EXISTS, so importing it twice is harmless.
-- It does NOT alter existing tables: after a model change on a database that
-- already has data, drop the affected tables (or the whole database) first.
-- =============================================================================
"""


def _load_models() -> None:
    import app.models  # noqa: F401 - registers every table on db.metadata


def _create_table_sql(table: sa.Table, dialect: mysql.dialect) -> str:
    """CREATE TABLE IF NOT EXISTS ... with inline INDEX clauses and fixed table options."""
    preparer = dialect.identifier_preparer
    ddl = str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip()
    body_end = ddl.rindex("\n)")  # the ")" closing the column list, before table options
    body = ddl[:body_end].rstrip().rstrip(",")
    lines = [body]
    for index in sorted(table.indexes, key=lambda i: str(i.name)):
        cols = ", ".join(preparer.quote(col.name) for col in index.columns)
        kind = "UNIQUE INDEX" if index.unique else "INDEX"
        lines.append(f"\t{kind} {preparer.quote(str(index.name))} ({cols})")
    body = ",\n".join(lines)
    body = "\n".join(line.rstrip() for line in body.split("\n"))  # no trailing spaces
    return f"{body}\n) {TABLE_OPTIONS};"


def generate_schema_sql(database_name: str = DEFAULT_DATABASE_NAME) -> str:
    """Return the full contents of database/schema.sql (deterministic, no timestamps)."""
    _load_models()
    dialect = mysql.dialect()
    quoted_db = dialect.identifier_preparer.quote(database_name)
    parts = [
        HEADER,
        f"CREATE DATABASE IF NOT EXISTS {quoted_db}\n"
        "  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        f"USE {quoted_db};",
    ]
    # sorted_tables is dependency ordered: referenced tables come first (FK-safe).
    for table in db.metadata.sorted_tables:
        parts.append(f"-- Table: {table.name}")
        parts.append(_create_table_sql(table, dialect))
    return "\n\n".join(parts) + "\n"


def write_schema_sql(path: Path = SCHEMA_PATH) -> Path:
    """(Re)write database/schema.sql from the models."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_schema_sql(), encoding="utf-8", newline="\n")
    return path


def schema_is_current(path: Path = SCHEMA_PATH) -> bool:
    """True if database/schema.sql matches the models."""
    if not path.exists():
        return False
    return path.read_text(encoding="utf-8").replace("\r\n", "\n") == generate_schema_sql()


def split_sql_statements(sql: str) -> list[str]:
    """Split schema.sql into statements (drops ``--`` comment lines and blanks).

    Good enough for our generated file, which has no ``;`` inside strings.
    """
    lines = [ln for ln in sql.replace("\r\n", "\n").split("\n") if not ln.lstrip().startswith("--")]
    statements = [s.strip() for s in "\n".join(lines).split(";")]
    return [s for s in statements if s]


def table_statements(sql: str) -> list[str]:
    """Only the CREATE TABLE statements (the database name comes from DATABASE_URL instead)."""
    return [s for s in split_sql_statements(sql) if re.match(r"(?i)^CREATE\s+TABLE", s)]


def init_db(*, schema_path: Path = SCHEMA_PATH) -> str:
    """Create the database tables for the current app. Returns a message for the user.

    MySQL/MariaDB: creates the database named in DATABASE_URL (if missing) and runs
    every CREATE TABLE statement of database/schema.sql. SQLite: ``db.create_all()``.
    """
    _load_models()
    engine = db.engine
    if engine.dialect.name == "sqlite":
        db.create_all()
        return f"Created all tables in SQLite database {engine.url.database or ':memory:'}."

    if engine.dialect.name not in ("mysql", "mariadb"):
        raise RuntimeError(f"Unsupported database: {engine.dialect.name}")

    if not schema_path.exists():
        raise RuntimeError(f"{schema_path} not found. Run: python run.py export-schema")
    sql = schema_path.read_text(encoding="utf-8")
    notes = []
    if sql.replace("\r\n", "\n") != generate_schema_sql():
        notes.append(
            "WARNING: database/schema.sql is out of date with the models. "
            "Run `python run.py export-schema` and then init-db again."
        )

    db_name = engine.url.database
    if not db_name:
        raise RuntimeError("DATABASE_URL must name a database, e.g. .../pawbalik?charset=utf8mb4")
    server_engine = sa.create_engine(engine.url.set(database=None))
    quoted = engine.dialect.identifier_preparer.quote(db_name)
    try:
        with server_engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"CREATE DATABASE IF NOT EXISTS {quoted} "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
    finally:
        server_engine.dispose()

    statements = table_statements(sql)
    with engine.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)
    notes.insert(0, f"Created {len(statements)} tables (if missing) in MySQL database '{db_name}'.")
    return "\n".join(notes)


def friendly_db_error(exc: BaseException) -> str:
    """A helpful one-line explanation for common database connection problems."""
    orig = getattr(exc, "orig", exc)
    code = orig.args[0] if getattr(orig, "args", None) else None
    text = str(orig)
    if code == 2003 or "Can't connect" in text or "Connection refused" in text:
        return (
            "Cannot connect to MySQL - is XAMPP MySQL started? "
            "(Open the XAMPP Control Panel and click Start next to MySQL.)"
        )
    if code == 1045:
        return "MySQL refused the user name/password in DATABASE_URL (.env)."
    if code == 1049:
        return "The database in DATABASE_URL does not exist yet. Run: python run.py init-db"
    if code == 1146 or "no such table" in text:
        return "The database tables are missing. Run: python run.py init-db"
    return f"Database error: {text}"
