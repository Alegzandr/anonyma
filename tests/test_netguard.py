from __future__ import annotations

import socket

import pytest

from anonyma import netguard


def test_blocks_outbound_until_allowed() -> None:
    netguard.install()
    with pytest.raises(netguard.NetworkBlockedError):
        socket.create_connection(("192.0.2.1", 80), timeout=0.1)
    with netguard.allow_network(), pytest.raises(OSError) as info:
        socket.create_connection(("192.0.2.1", 80), timeout=0.05)
    assert not isinstance(info.value, netguard.NetworkBlockedError)
