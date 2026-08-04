"""Tests for OTA RPC client."""

from unittest import mock

import pytest

from pytboss.ota import OTA


@pytest.fixture
def mock_conn():
    conn = mock.AsyncMock()
    conn.send_command = mock.AsyncMock(return_value={})
    return conn


async def test_update_sends_the_mongoose_method(mock_conn):
    """`OTA.Update`, not `OTA.Start` -- no Mongoose image serves the latter."""
    ota = OTA(mock_conn)
    await ota.update("http://example.com/firmware.zip")
    mock_conn.send_command.assert_awaited_once_with(
        "OTA.Update", {"url": "http://example.com/firmware.zip"}
    )


async def test_update_with_commit_timeout(mock_conn):
    ota = OTA(mock_conn)
    await ota.update("http://example.com/firmware.zip", commit_timeout=120)
    mock_conn.send_command.assert_awaited_once_with(
        "OTA.Update",
        {"url": "http://example.com/firmware.zip", "commit_timeout": 120},
    )


async def test_update_omits_commit_timeout_rather_than_sending_null(mock_conn):
    """No timeout means no automatic rollback, not a rollback after `None`."""
    ota = OTA(mock_conn)
    await ota.update("http://example.com/firmware.zip")
    _, params = mock_conn.send_command.await_args.args
    assert "commit_timeout" not in params


async def test_start_is_the_esp_idf_method(mock_conn):
    """Kept because `OTA.Start` is correct on the `16.x` line, and only there."""
    ota = OTA(mock_conn)
    await ota.start("http://example.com/firmware.bin", commit_timeout=60)
    mock_conn.send_command.assert_awaited_once_with(
        "OTA.Start",
        {"url": "http://example.com/firmware.bin", "commit_timeout": 60},
    )


async def test_commit(mock_conn):
    ota = OTA(mock_conn)
    await ota.commit()
    mock_conn.send_command.assert_awaited_once_with("OTA.Commit", {})


async def test_revert(mock_conn):
    ota = OTA(mock_conn)
    await ota.revert()
    mock_conn.send_command.assert_awaited_once_with("OTA.Revert", {})


async def test_create_snapshot_defaults_to_no_parameters(mock_conn):
    ota = OTA(mock_conn)
    await ota.create_snapshot()
    mock_conn.send_command.assert_awaited_once_with("OTA.CreateSnapshot", {})


async def test_create_snapshot_with_parameters(mock_conn):
    ota = OTA(mock_conn)
    await ota.create_snapshot(set_as_revert=True, commit_timeout=90)
    mock_conn.send_command.assert_awaited_once_with(
        "OTA.CreateSnapshot", {"set_as_revert": True, "commit_timeout": 90}
    )


async def test_create_snapshot_sends_set_as_revert_false(mock_conn):
    """`False` is a value the caller chose; only `None` means "unset"."""
    ota = OTA(mock_conn)
    await ota.create_snapshot(set_as_revert=False)
    mock_conn.send_command.assert_awaited_once_with(
        "OTA.CreateSnapshot", {"set_as_revert": False}
    )


async def test_get_boot_state(mock_conn):
    """The shape Mongoose documents, and the one a real grill answered."""
    mock_conn.send_command.return_value = {
        "active_slot": 0,
        "is_committed": True,
        "revert_slot": 0,
        "commit_timeout": 0,
    }
    ota = OTA(mock_conn)
    result = await ota.get_boot_state()
    mock_conn.send_command.assert_awaited_once_with("OTA.GetBootState", {})
    assert result["is_committed"] is True


async def test_get_boot_state_when_a_rollback_is_pending(mock_conn):
    mock_conn.send_command.return_value = {
        "active_slot": 1,
        "is_committed": False,
        "revert_slot": 0,
        "commit_timeout": 300,
    }
    ota = OTA(mock_conn)
    result = await ota.get_boot_state()
    assert result["is_committed"] is False
    assert result["commit_timeout"] == 300


async def test_get_status_is_the_shape_a_grill_sends(mock_conn):
    """A PB1600PS1 on 0.5.7, verbatim.

    `state` is an int, the text is under `message`, and progress is
    `progress_percent`. The previous test here asserted `state="progress"`,
    `msg` and `progress` -- a shape invented from the method name, which is
    how the wrong docstring survived review.
    """
    mock_conn.send_command.return_value = {
        "state": 0,
        "message": "idle",
        "is_committed": True,
        "progress_percent": 0,
        "commit_timeout": 0,
        "partition": 0,
    }
    ota = OTA(mock_conn)
    result = await ota.get_status()
    mock_conn.send_command.assert_awaited_once_with("OTA.Status", {})
    assert result["state"] == 0
    assert result["message"] == "idle"
    assert result["progress_percent"] == 0


async def test_getters_narrow_a_result_less_reply(mock_conn):
    """`send_command` can answer `None`; a getter must still hand back a dict."""
    mock_conn.send_command.return_value = None
    ota = OTA(mock_conn)
    assert await ota.get_status() == {}
    assert await ota.get_boot_state() == {}
