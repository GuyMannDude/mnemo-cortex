"""`mnemo-cortex health` must not invent a memory count.

/health stopped sending `memory_entries` (v2.11.0), but check_api_server kept
reading it with a default of 0, so every working install printed
"0 memories" right after a save — the "prove it works" step looked broken.
"""
from unittest.mock import patch

from agentb import health


class _Resp:
    def __init__(self, body):
        self._body = body
        self.elapsed = type("E", (), {"total_seconds": lambda self: 0.012})()

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _detail(body):
    with patch.object(health.httpx, "get", return_value=_Resp(body)):
        return health.check_api_server("http://x")["detail"]


def test_no_count_when_health_sends_none():
    detail = _detail({"status": "ok", "version": "4.28.4"})
    assert "memories" not in detail
    assert detail.startswith("v4.28.4")


def test_count_shown_when_an_older_server_sends_one():
    assert "156 memories" in _detail({"status": "ok", "version": "2.10.0", "memory_entries": 156})
