import socket

import pytest


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Every automated test must use Fake SMTP/IMAP and local Flask test clients."""

    def denied(*_args, **_kwargs):
        raise AssertionError("Real network access is forbidden during MailPilot tests")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
