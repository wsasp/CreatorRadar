from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import URL, Connection, Engine, create_engine, event

from .config import Config


def make_engine(config: Config) -> Engine:
    engine = create_engine(
        URL.create("sqlite", database=str(config.database_path)),
        connect_args={"timeout": 5},
        hide_parameters=True,
    )

    @event.listens_for(engine, "connect")
    def set_pragmas(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")

    return engine


@contextmanager
def write_transaction(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        yield connection
        connection.commit()


def migrate(config: Config) -> None:
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(config)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
            connection.commit()
        with write_transaction(engine) as connection:
            alembic = AlembicConfig()
            alembic.set_main_option(
                "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
            )
            alembic.attributes["connection"] = connection
            command.upgrade(alembic, "head")
    finally:
        engine.dispose()
