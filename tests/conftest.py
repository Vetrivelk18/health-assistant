"""
Test isolation: point the whole suite at a throwaway database.

Before this, tests ran against whatever `DATABASE_URL` pointed at — i.e. the
developer's real database. Three consequences, two of which actually bit
during development:

  1. Tests mutated real rows. Every test file carries its own cleanup
     fixture, and when a test errored *before* its fixture ran, the leftover
     `pytest_*` rows broke the next run with a UNIQUE violation.
  2. A schema change made the whole suite fail with "column does not exist",
     because the dev database hadn't been migrated — the failure looked like
     a code bug and wasn't.
  3. A fresh clone couldn't run the tests at all without first creating and
     migrating a database by hand.

So: derive a sibling `*_test` database from whatever `DATABASE_URL` is
configured, create it if absent, build the schema from the models, and wipe
every table between tests.

Still Postgres, deliberately — not SQLite. The app relies on Postgres
behaviour (JSON columns, the upsert in `_summarise_user`), and a suite that
passes on SQLite while production runs Postgres tests the wrong thing.

Override the target with TEST_DATABASE_URL if the derived name isn't wanted.
"""

import os
from urllib.parse import urlparse, urlunparse

import pytest

# ---------------------------------------------------------------------------
# This block MUST run before any project module is imported: config.py reads
# os.environ at import time, and database.py builds its engine from whatever
# config saw. pytest imports conftest before the test modules, so this is the
# last moment the value can still be changed.
# ---------------------------------------------------------------------------


def _derive_test_url(url: str) -> str:
    """postgresql://…/health_assistant -> postgresql://…/health_assistant_test

    Reuses the configured host and credentials so there's nothing extra to
    set up, and only ever appends a suffix — it cannot resolve to the
    database it was derived from.
    """
    parts = urlparse(url)
    name = parts.path.lstrip("/") or "health_assistant"
    if name.endswith("_test"):
        return url
    return urlunparse(parts._replace(path=f"/{name}_test"))


_DEV_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/health_assistant")
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL") or _derive_test_url(_DEV_URL)

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# Keep the app in development mode: production refuses to start without a
# real SECRET_KEY, and init_db() is development-only by design.
os.environ["FASTAPI_ENV"] = "development"
# Guard against a real key leaking into a test run and calling a paid API.
os.environ.pop("GEMINI_API_KEY", None)


def _ensure_database_exists(url: str) -> None:
    """CREATE DATABASE if it isn't there, so a fresh clone just works."""
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    parts = urlparse(url)
    target = parts.path.lstrip("/")
    # Connect to the maintenance database — you cannot create a database
    # from inside a connection to the database being created.
    admin_url = urlunparse(parts._replace(path="/postgres"))

    try:
        conn = psycopg2.connect(admin_url)
    except psycopg2.OperationalError as e:
        raise RuntimeError(
            f"Cannot reach Postgres to create the test database ({target}).\n"
            f"Is it running? Set TEST_DATABASE_URL to point elsewhere.\n"
            f"Underlying error: {e}"
        ) from e

    try:
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,))
            if not cur.fetchone():
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
    finally:
        conn.close()


_ensure_database_exists(TEST_DATABASE_URL)

# Safe to import project modules now that DATABASE_URL points at the test DB.
from database import engine  # noqa: E402
from models import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Build the schema once per run, from the models.

    create_all rather than `alembic upgrade head`: this is a throwaway
    database rebuilt from scratch, so there's nothing to migrate, and going
    through models keeps the suite honest if a model is added without a
    migration — `alembic check` is what catches that, not the tests.
    """
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Empty every table after each test.

    Runs even when a test errors, which is what the old per-file cleanup
    fixtures could not guarantee — that's how leftover rows used to poison
    the following run. Deleting in reverse dependency order satisfies the
    foreign keys without needing CASCADE.
    """
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def db_session():
    """A plain session against the test database, for tests that want to set
    up or assert on rows directly."""
    from database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
