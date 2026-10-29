import socket

import pytest


def test_a_test_cannot_reach_a_remote_host():
    with pytest.raises(AssertionError, match="tried to resolve"):
        socket.getaddrinfo("caldav.icloud.com", 443)
    sock = socket.socket()
    try:
        with pytest.raises(AssertionError, match="tried to connect"):
            sock.connect(("17.0.0.1", 443))
    finally:
        sock.close()
