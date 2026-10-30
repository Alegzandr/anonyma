"""Process-wide network guard.

Once :func:`install` is called, any outbound socket connection raises
:class:`NetworkBlockedError` unless it happens inside ``with allow_network():``.
Only the model manager opens that window, after an explicit user action.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_lock = threading.Lock()
_allowed = 0
_installed = False
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_getaddrinfo = socket.getaddrinfo


class NetworkBlockedError(ConnectionRefusedError):
    pass


def _is_local(address: Any) -> bool:
    if isinstance(address, str):  # AF_UNIX
        return True
    if isinstance(address, tuple) and address:
        host = str(address[0])
        return host in {"127.0.0.1", "::1", "localhost"}
    return False


def _check(address: Any) -> None:
    if _allowed <= 0 and not _is_local(address):
        raise NetworkBlockedError("Accès réseau bloqué par Anonyma")


def _guarded_connect(self: socket.socket, address: Any) -> None:
    _check(address)
    _original_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    _check(address)
    return _original_connect_ex(self, address)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if _allowed <= 0 and not _is_local((host,)):
        raise NetworkBlockedError("Résolution DNS bloquée par Anonyma")
    return _original_getaddrinfo(host, *args, **kwargs)


def install() -> None:
    global _installed
    with _lock:
        if _installed:
            return
        socket.socket.connect = _guarded_connect  # type: ignore[method-assign,assignment]
        socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign,assignment]
        socket.getaddrinfo = _guarded_getaddrinfo
        _installed = True


def is_installed() -> bool:
    return _installed


@contextmanager
def allow_network() -> Iterator[None]:
    """Temporarily allow outbound connections (model downloads only)."""
    global _allowed
    with _lock:
        _allowed += 1
    previous = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "0"
    try:
        yield
    finally:
        with _lock:
            _allowed -= 1
        if previous is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = previous
