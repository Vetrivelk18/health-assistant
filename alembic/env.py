"""
Alembic environment.

Two deliberate differences from the generated default:

1. **The database URL comes from `config.settings`, not `alembic.ini`.**
   The URL is a secret (it carries the Neon password) and `alembic.ini` is
   committed. Reading it from the same place the app does means there's one
   source of truth and nothing to leak.

2. **`target_metadata` is the app's real metadata**, so `--autogenerate`
   can diff models against the live schema.

`compare_type=True` is enabled because the default ignores column *type*
changes entirely — a String→Integer change would autogenerate an empty
migration and fail at runtime instead.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the project root importable — alembic runs env.py with its own
# directory on sys.path, not the repo root.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings  # noqa: E402
from models import Base  # noqa: E402

config = context.config

# Inject the real URL, overriding the deliberately-empty one in alembic.ini.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it — `alembic upgrade head --sql`.

    Useful for reviewing exactly what a migration will do to production
    before letting it touch Neon.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
