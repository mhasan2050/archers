from alembic import context
from sqlalchemy import engine_from_config, pool

from archers.config import load_settings
from archers.storage import Base

config = context.config
settings = load_settings()
url = settings.database_url.get_secret_value()
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    with engine.connect() as connection:
        with connection.begin():
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(734818101)")
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    engine.dispose()
