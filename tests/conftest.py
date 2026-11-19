"""Shared pytest configuration."""

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def _testclient_defaults_to_loopback(monkeypatch):
    """Model in-process TestClient calls as local traffic.

    Starlette's synthetic default client name is ``testclient`` rather than an
    IP address. Production ASGI servers provide the socket peer IP, and Mnemo's
    unauthenticated-mode guard intentionally rejects unknown/non-IP peers. Keep
    existing endpoint tests representative of the normal loopback deployment;
    security tests can still pass an explicit remote ``client=`` tuple.
    """
    original_init = TestClient.__init__

    def loopback_init(self, *args, **kwargs):
        kwargs.setdefault("client", ("127.0.0.1", 50000))
        # The default "testserver" Host would trip the DNS-rebinding guard.
        kwargs.setdefault("base_url", "http://127.0.0.1")
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", loopback_init)


class _ThreadRecordingConn:
    """Stands in for a VecStore's sqlite3 connection and records which thread
    made every call. Anything not wrapped here is still recorded through
    __getattr__ (cursor(), close(), ...)."""

    def __init__(self, conn, calls):
        self._conn, self._calls = conn, calls

    def _rec(self, op):
        import threading
        self._calls.append((threading.get_ident(), op))

    def execute(self, *a, **k):
        self._rec("execute")
        return self._conn.execute(*a, **k)

    def executemany(self, *a, **k):
        self._rec("executemany")
        return self._conn.executemany(*a, **k)

    def executescript(self, *a, **k):
        self._rec("executescript")
        return self._conn.executescript(*a, **k)

    def commit(self):
        self._rec("commit")
        return self._conn.commit()

    def rollback(self):
        self._rec("rollback")
        return self._conn.rollback()

    def __enter__(self):
        self._rec("enter")
        return self._conn.__enter__()

    def __exit__(self, *exc):
        self._rec("exit")
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        self._rec(name)
        return getattr(self._conn, name)


@pytest.fixture
def vec_thread_tripwire():
    """Item 17 (d). A tenant's VecStore is ONE sqlite3 connection shared with
    the event loop, and sqlite3 keeps transaction state per connection, not
    per thread. watch(store) returns a list that fills with (thread ident, op)
    for every call made on that store's connection from then on; a test
    asserts every ident is the loop's. Restored at teardown."""
    watched = []

    def watch(store):
        calls = []
        watched.append((store, store._conn))
        store._conn = _ThreadRecordingConn(store._conn, calls)
        return calls

    yield watch
    for store, conn in watched:
        store._conn = conn
