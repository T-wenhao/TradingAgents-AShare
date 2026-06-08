"""Alembic migration environment configuration."""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so `api` package can be imported when
# alembic is invoked from the repo root (e.g. `uv run alembic upgrade head`).
# ---------------------------------------------------------------------------
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Override sqlalchemy.url with DATABASE_URL env var if present.
# This keeps credentials out of alembic.ini and aligns with the
# runtime config used by api.database.
_db_url = os.getenv("DATABASE_URL", "sqlite:///./tradingagents.db")
config.set_main_option("sqlalchemy.url", _db_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Base.metadata reflects the full schema.
from api.database import Base  # noqa: E402  (must come after sys.path tweak)

target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# Render_as_batch_mode is required for SQLite ALTER TABLE support.
# SQLite cannot natively ALTER TABLE in most cases; Alembic's batch mode
# creates a temporary table, copies data, and swaps the table atomically.
# ---------------------------------------------------------------------------
_BATCH_MODE = _db_url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=_BATCH_MODE,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_BATCH_MODE,
            # compare_type=True catches column type changes in autogenerate
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
