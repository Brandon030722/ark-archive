from contextlib import contextmanager
from time import monotonic

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool


def make_engine(url: str):
    parsed = make_url(url)
    # SQLite pre-ping may wait for an exclusive lock before a tool can install
    # its connection-local busy timeout. A local file needs no network ping.
    options = {"pool_pre_ping": parsed.get_backend_name() != "sqlite"}
    if issubclass(parsed.get_dialect().get_pool_class(parsed), QueuePool):
        # Pool checkout is a separate limit from a tool statement's deadline.
        # Do not pass QueuePool options to SQLite's in-memory thread pool.
        options["pool_timeout"] = 5
    engine = create_engine(url, **options)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def readonly_session(engine, *, timeout_ms=2000):
    """Hold one bounded, read-only connection; audit writes use another session.

    The session is only supplied to reviewed tools, never to model-authored SQL.
    SQLite's progress handler bounds the entire tool context. PostgreSQL also
    applies its server-side timeout to each statement in the outer transaction.
    """
    if type(timeout_ms) is not int or not 100 <= timeout_ms <= 5000:
        raise ValueError("invalid database tool timeout")
    dialect = engine.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise ValueError("database dialect has no reviewed read-only tool contract")
    # Keep the connection checked out until both session rollback and SQLite
    # connection-local cleanup finish. A new checkout cannot reset another
    # pooled connection's query_only flag.
    with engine.connect() as connection:
        transaction = connection.begin()
        sqlite_connection = None
        previous_query_only = None
        previous_busy_timeout = None
        sqlite_deadline_handler = None
        try:
            if dialect == "sqlite":
                sqlite_connection = connection.connection.driver_connection
                cursor = sqlite_connection.execute("PRAGMA query_only")
                try:
                    previous_query_only = cursor.fetchone()[0]
                finally:
                    cursor.close()
                cursor = sqlite_connection.execute("PRAGMA busy_timeout")
                try:
                    previous_busy_timeout = cursor.fetchone()[0]
                finally:
                    cursor.close()
                sqlite_connection.execute("PRAGMA query_only=ON").close()
                deadline = monotonic() + timeout_ms / 1000
                sqlite_connection.execute(f"PRAGMA busy_timeout={timeout_ms}").close()
                sqlite_connection.set_progress_handler(lambda: monotonic() >= deadline, 1000)

                def sqlite_deadline_handler(_conn, cursor, _sql, _params, _context, _many):
                    remaining_ms = min(timeout_ms, int((deadline - monotonic()) * 1000))
                    if remaining_ms < 1:
                        raise TimeoutError("database tool deadline exceeded")
                    # Lock waiting does not invoke SQLite's progress handler.
                    # Reset its busy timeout before every reviewed statement.
                    cursor.execute(f"PRAGMA busy_timeout={remaining_ms}")

                event.listen(connection, "before_cursor_execute", sqlite_deadline_handler)
            else:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(
                    text("SELECT set_config('statement_timeout', :milliseconds, true)"),
                    {"milliseconds": str(timeout_ms)},
                )
            # Session commits/rollbacks affect a savepoint, not the connection's
            # outer read-only transaction. The context owns that transaction.
            with Session(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                try:
                    yield session
                finally:
                    # Teardown SQL must still work after the tool's deadline.
                    if sqlite_deadline_handler is not None:
                        event.remove(connection, "before_cursor_execute", sqlite_deadline_handler)
                        sqlite_deadline_handler = None
                    if sqlite_connection is not None:
                        sqlite_connection.set_progress_handler(None, 0)
        finally:
            if sqlite_deadline_handler is not None:
                event.remove(connection, "before_cursor_execute", sqlite_deadline_handler)
            if sqlite_connection is not None:
                sqlite_connection.set_progress_handler(None, 0)
            if transaction.is_active:
                transaction.rollback()
            if sqlite_connection is not None and previous_query_only is not None:
                try:
                    sqlite_connection.execute(
                        f"PRAGMA query_only={int(previous_query_only)}"
                    ).close()
                    if previous_busy_timeout is not None:
                        sqlite_connection.execute(
                            f"PRAGMA busy_timeout={int(previous_busy_timeout)}"
                        ).close()
                except Exception:
                    # Never return a connection with an uncertain local flag.
                    connection.invalidate()
                    raise
