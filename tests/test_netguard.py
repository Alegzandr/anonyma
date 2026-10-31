from __future__ import annotations

import socket
import threading
from collections.abc import Callable

import pytest

from anonyma import netguard


def test_blocks_outbound_until_allowed() -> None:
    netguard.install()
    with pytest.raises(netguard.NetworkBlockedError):
        socket.create_connection(("192.0.2.1", 80), timeout=0.1)
    with netguard.allow_network(), pytest.raises(OSError) as info:
        socket.create_connection(("192.0.2.1", 80), timeout=0.05)
    assert not isinstance(info.value, netguard.NetworkBlockedError)


@pytest.mark.parametrize(
    "call",
    [
        lambda: socket.gethostbyname("exemple.test"),
        lambda: socket.gethostbyname_ex("exemple.test"),
        lambda: socket.gethostbyaddr("192.0.2.1"),
        lambda: socket.getnameinfo(("192.0.2.1", 80), 0),
        lambda: socket.getaddrinfo("exemple.test", 80),
    ],
)
def test_name_resolution_is_blocked(call: Callable[[], object]) -> None:
    netguard.install()
    with pytest.raises(netguard.NetworkBlockedError):
        call()


def test_datagrams_are_blocked() -> None:
    netguard.install()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(netguard.NetworkBlockedError):
            sock.sendto(b"x", ("192.0.2.1", 53))
        with pytest.raises(netguard.NetworkBlockedError):
            sock.sendto(b"x", 0, ("192.0.2.1", 53))
        sock.sendto(b"x", ("127.0.0.1", 9))  # local: allowed


def test_allowance_is_per_thread() -> None:
    netguard.install()
    errors: list[BaseException] = []

    def other_thread() -> None:
        try:
            socket.gethostbyname("exemple.test")
        except BaseException as exc:
            errors.append(exc)

    with netguard.allow_network():
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join()
    assert len(errors) == 1 and isinstance(errors[0], netguard.NetworkBlockedError)
