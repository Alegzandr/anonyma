"""Process-wide network guard.

Once :func:`install` is called, any outbound socket connection, datagram
(``sendto``/``sendmsg``) or name resolution (``getaddrinfo``, ``gethostbyname*``,
``gethostbyaddr``, ``getnameinfo``) raises :class:`NetworkBlockedError` unless it
happens inside ``with allow_network():`` **in the same thread**: a download opening
the window never lets another thread (a worker, a plugin) reach the network. Only
the model manager opens that window, after an explicit user action.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_lock = threading.Lock()
#: ``depth``: nesting of ``allow_network()`` in the current thread.
_local = threading.local()
_installed = False
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_sendto = socket.socket.sendto
_original_sendmsg = getattr(socket.socket, "sendmsg", None)  # not on Windows
_original_getaddrinfo = socket.getaddrinfo
_original_gethostbyname = socket.gethostbyname
_original_gethostbyname_ex = socket.gethostbyname_ex
_original_gethostbyaddr = socket.gethostbyaddr
_original_getnameinfo = socket.getnameinfo


class NetworkBlockedError(ConnectionRefusedError):
    pass


def _is_local(address: Any) -> bool:
    if isinstance(address, str):  # AF_UNIX
        return True
    if isinstance(address, tuple) and address:
        host = str(address[0])
        return host in {"127.0.0.1", "::1", "localhost"}
    return False


def is_allowed() -> bool:
    """True inside ``allow_network()`` in the calling thread."""
    return int(getattr(_local, "depth", 0)) > 0


def _check(address: Any) -> None:
    if not is_allowed() and not _is_local(address):
        raise NetworkBlockedError("Accès réseau bloqué par Anonyma")


def _check_name(host: Any) -> None:
    if not is_allowed() and not _is_local((host,)):
        raise NetworkBlockedError("Résolution DNS bloquée par Anonyma")


def _guarded_connect(self: socket.socket, address: Any) -> None:
    _check(address)
    _original_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    _check(address)
    return _original_connect_ex(self, address)


def _guarded_sendto(self: socket.socket, data: Any, *args: Any) -> int:
    _check(args[-1] if args else None)  # sendto(data, address) / (data, flags, address)
    return _original_sendto(self, data, *args)


def _guarded_sendmsg(self: socket.socket, buffers: Any, *args: Any) -> int:
    if len(args) >= 3:  # (buffers, ancdata, flags, address)
        _check(args[2])
    elif not is_allowed() and self.type == socket.SOCK_DGRAM:
        _check(_peer(self))
    assert _original_sendmsg is not None
    result: int = _original_sendmsg(self, buffers, *args)
    return result


def _peer(sock: socket.socket) -> Any:
    try:
        return sock.getpeername()
    except OSError:
        return None


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    _check_name(host)
    return _original_getaddrinfo(host, *args, **kwargs)


def _guarded_gethostbyname(host: Any) -> Any:
    _check_name(host)
    return _original_gethostbyname(host)


def _guarded_gethostbyname_ex(host: Any) -> Any:
    _check_name(host)
    return _original_gethostbyname_ex(host)


def _guarded_gethostbyaddr(host: Any) -> Any:
    _check_name(host)
    return _original_gethostbyaddr(host)


def _guarded_getnameinfo(sockaddr: Any, flags: int) -> Any:
    _check(sockaddr)
    return _original_getnameinfo(sockaddr, flags)


def install() -> None:
    global _installed
    with _lock:
        if _installed:
            return
        socket.socket.connect = _guarded_connect  # type: ignore[method-assign,assignment]
        socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign,assignment]
        socket.socket.sendto = _guarded_sendto  # type: ignore[method-assign,assignment]
        if _original_sendmsg is not None:
            setattr(socket.socket, "sendmsg", _guarded_sendmsg)  # noqa: B010 (absent on Windows)
        socket.getaddrinfo = _guarded_getaddrinfo
        socket.gethostbyname = _guarded_gethostbyname
        socket.gethostbyname_ex = _guarded_gethostbyname_ex
        socket.gethostbyaddr = _guarded_gethostbyaddr
        socket.getnameinfo = _guarded_getnameinfo
        _installed = True


def is_installed() -> bool:
    return _installed


@contextmanager
def allow_network() -> Iterator[None]:
    """Temporarily allow outbound connections in the calling thread (model
    downloads only)."""
    _local.depth = int(getattr(_local, "depth", 0)) + 1
    previous = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "0"
    try:
        yield
    finally:
        _local.depth -= 1
        if previous is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = previous
