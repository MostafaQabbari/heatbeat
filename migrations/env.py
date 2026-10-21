"""Alembic environment.

What this file does: connects Alembic to the database and runs the migration
scripts in `migrations/versions/`. Tests can pass their own URL; otherwise the
URL comes from the HB_* settings.
"""

from alembic import context
from sqlalchemy import create_engine, pool

from heatbeat.config import Settings


def database_url() -> str:
    url = context.config.get_main_option("sqlalchemy.url") or Settings().database_url  # type: ignore[call-arg]
    # SQLAlchemy needs to know which driver to use: psycopg 3.
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
