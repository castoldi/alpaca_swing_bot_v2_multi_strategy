"""Default HTTP timeouts for alpaca-py clients.

alpaca-py sends every REST call through ``self._session.request(...)`` with no
``timeout``, and ``requests`` then waits forever on a half-open socket. One
such hang froze the whole bot loop until the watchdog noticed a stale
heartbeat (up to ~95 minutes). Wrapping the client's session gives every call
a connect/read deadline; alpaca-py's own 429 retry logic is unchanged.
"""
from __future__ import annotations

# (connect, read) seconds. Alpaca answers in well under a second normally; a
# 30 s read budget still covers large bar pages.
DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 30.0)


def apply_default_timeout(client, timeout=DEFAULT_TIMEOUT):
    """Make ``client``'s requests session time out unless a call sets its own.

    Idempotent, and a no-op for objects without a ``_session`` (test fakes).
    Returns the client for chaining.
    """
    session = getattr(client, "_session", None)
    if session is None or getattr(session, "_swing_default_timeout", None) is not None:
        return client
    original = session.request

    def request(method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return original(method, url, **kwargs)

    session.request = request
    session._swing_default_timeout = timeout
    return client
