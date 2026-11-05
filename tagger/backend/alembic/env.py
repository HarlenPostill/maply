from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401  (register tables)
from app.config import get_settings
from app.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

url = os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().database_url
config.set_main_option("sqlalchemy.url", url)
target_metadata = Base.metadata

# PostGIS / tiger tables that ship with the postgis image are not ours.
_FOREIGN = {"spatial_ref_sys", "geography_columns", "geometry_columns", "raster_columns", "raster_overviews"}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and reflected and compare_to is None:
        return False
    if type_ == "table" and name in _FOREIGN:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
