"""Tests for the Mongoose WiFi RPC client."""

from unittest import mock

import pytest

from pytboss.wifi import WiFi

NETWORK = {
    "ssid": "Kitchen",
    "bssid": "12:34:56:78:90:ab",
    # `auth`, not `authMode`: the RPC service and the JS API name it
    # differently, and the loader's scan goes through the JS one.
    "auth": 3,
    "channel": 6,
    "rssi": -58,
}


@pytest.fixture
def mock_conn():
    conn = mock.AsyncMock()
    conn.send_command = mock.AsyncMock(return_value=[NETWORK])
    return conn


async def test_scan_returns_the_array_as_sent(mock_conn):
    wifi = WiFi(mock_conn)
    assert await wifi.scan() == [NETWORK]
    mock_conn.send_command.assert_awaited_once_with("Wifi.Scan", {})


async def test_scan_when_the_device_does_not_serve_it(mock_conn):
    """Must not hand back a non-list, the same trap `RPC.List` had."""
    mock_conn.send_command.return_value = None
    wifi = WiFi(mock_conn)
    assert await wifi.scan() == []


async def test_scan_when_the_device_answers_an_object(mock_conn):
    mock_conn.send_command.return_value = {"error": "nope"}
    wifi = WiFi(mock_conn)
    assert await wifi.scan() == []


async def test_scan_finding_nothing(mock_conn):
    mock_conn.send_command.return_value = []
    wifi = WiFi(mock_conn)
    assert await wifi.scan() == []
