"""Alembic environment. The database URL is injected by ``clipfactory.db.migrate``."""

from alembic import context
from sqlalchemy import engine_from_config, pool

import clipfactory.models  # noqa: F401 - registers every table on Base.metadata
from clipfactory.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # render_as_batch: SQLite cannot ALTER most things in place.
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
