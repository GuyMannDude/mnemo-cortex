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
