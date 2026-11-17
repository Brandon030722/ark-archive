from alembic import context
from sqlalchemy import engine_from_config, pool

from arknights_agent.config import get_settings
from arknights_agent.models import Base

config = context.config
target_metadata = Base.metadata


def configure(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    configure(config.attributes["connection"])
else:
    url = config.get_main_option("sqlalchemy.url")
    # Explicit fixture URLs override Settings, ordinary CLI uses ARK_DATABASE_URL/.env.
    if not config.attributes.get("use_config_url"):
        url = get_settings().database_url
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    engine = engine_from_config(
        config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with engine.connect() as connection:
        configure(connection)
