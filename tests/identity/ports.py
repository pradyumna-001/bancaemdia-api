"""Fixed sandbox ports, bound once per test process and never handed back mid-run.

The redirect URI, CSP and the consumer SPA all name 127.0.0.1:58000/58080, so these
ports cannot be random. They also lie inside Linux's ephemeral range (32768-60999):
every short client connection (the proxies open one per request) may leave a TIME_WAIT
socket on one of them for 60 s, and a client socket without SO_REUSEADDR blocks a
later bind even when the server sets it. Binding once, then giving each server or API
process a duplicate of the same listening socket, removes that race; while the port is
bound here the kernel never assigns it to an outgoing connection.
"""

import asyncio
import socket
import time

from tests.identity import diagnostics

_LISTENERS: dict[int, socket.socket] = {}
WAIT_SECONDS = 90  # TIME_WAIT lasts 60 s on Linux; leave room for the timer's granularity.


def _bind(port: int) -> socket.socket | OSError:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError as error:
        listener.close()
        return error
    listener.listen(2048)
    return listener


def _busy(port: int, error: OSError, journal: diagnostics.Journal | None, waited: float) -> None:
    holders = diagnostics.port_holders(port)
    if journal is not None:
        # Infrastructure wait only: no product step has run yet, nothing is repeated.
        journal.record("port.busy", f"{port} {error.strerror}; holders={holders}")
    if waited > WAIT_SECONDS:
        raise OSError(
            error.errno,
            f"127.0.0.1:{port} stayed unavailable for {WAIT_SECONDS} s ({error.strerror}); "
            f"holders={holders}",
        )


async def reserve(port: int, journal: diagnostics.Journal | None = None) -> None:
    started = time.monotonic()
    while port not in _LISTENERS:
        bound = _bind(port)
        if isinstance(bound, socket.socket):
            _LISTENERS[port] = bound
            if journal is not None and time.monotonic() - started > 0.5:
                journal.record("port.bound", f"{port} after={time.monotonic() - started:.1f}s")
            return
        _busy(port, bound, journal, time.monotonic() - started)
        await asyncio.sleep(0.5)


def listener(port: int, journal: diagnostics.Journal | None = None) -> socket.socket:
    """A fresh duplicate the caller may close; the reserved original stays bound."""
    started = time.monotonic()
    while port not in _LISTENERS:
        bound = _bind(port)
        if isinstance(bound, socket.socket):
            _LISTENERS[port] = bound
            break
        _busy(port, bound, journal, time.monotonic() - started)
        time.sleep(0.5)  # Only for synchronous server factories used before reserve().
    return _LISTENERS[port].dup()
