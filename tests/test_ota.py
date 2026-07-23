"""Tests for OTA RPC client."""

from unittest import mock

import pytest

from pytboss.ota import OTA


@pytest.fixture
def mock_conn():
    conn = mock.AsyncMock()
    conn.send_command = mock.AsyncMock(return_value={})
    return conn


async def test_ota_start(mock_conn):
    ota = OTA(mock_conn)
    await ota.start("http://example.com/firmware.zip")
    mock_conn.send_command.assert_awaited_once_with(
        "OTA.Start", {"url": "http://example.com/firmware.zip"}
    )


async def test_ota_start_with_commit_timeout(mock_conn):
    ota = OTA(mock_conn)
    await ota.start("http://example.com/firmware.zip", commit_timeout=120)
    mock_conn.send_command.assert_awaited_once_with(
        "OTA.Start",
        {"url": "http://example.com/firmware.zip", "commit_timeout": 120},
    )


async def test_ota_get_status(mock_conn):
    mock_conn.send_command.return_value = {
        "state": "progress",
        "msg": "Downloading...",
        "progress": 42,
    }
    ota = OTA(mock_conn)
    result = await ota.get_status()
    mock_conn.send_command.assert_awaited_once_with("OTA.Status", {})
    assert result["state"] == "progress"
    assert result["progress"] == 42
