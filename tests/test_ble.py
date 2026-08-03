import asyncio
import json
from itertools import zip_longest
from unittest import mock

import bleak
import pytest
from bleak_retry_connector import BleakClientWithServiceCache
from pytest import raises

from pytboss import ble
from pytboss.exceptions import NotConnectedError, RPCError


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_connect_disconnect(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client

    conn = ble.BleConnection(mock_device)
    await conn.connect()
    assert conn.is_connected()
    await conn.disconnect()
    assert not conn.is_connected()

    mock_establish_connection.assert_awaited()
    mock_bleak_client.start_notify.assert_has_awaits(
        [
            mock.call(ble.CHAR_RPC_RX_CTL, conn._on_rpc_data_received),
            mock.call(ble.CHAR_DEBUG_LOG, conn._on_debug_log_received),
        ]
    )
    mock_bleak_client.disconnect.assert_awaited()


@mock.patch("bleak_retry_connector.establish_connection")
async def test_reset_device(mock_establish_connection):
    mock_old_device = mock.create_autospec(bleak.BLEDevice)
    mock_new_device = mock.create_autospec(bleak.BLEDevice)
    mock_old_device.name = "OLD DEVICE NAME"
    mock_new_device.name = "NEW DEVICE NAME"
    mock_old_bleak_client = mock.create_autospec(bleak.BleakClient)
    mock_new_bleak_client = mock.create_autospec(bleak.BleakClient)
    mock_establish_connection.return_value = mock_old_bleak_client

    conn = ble.BleConnection(mock_old_device)
    await conn.connect()

    mock_establish_connection.assert_awaited_with(
        client_class=BleakClientWithServiceCache,
        device=mock_old_device,
        name=mock_old_device.name,
        disconnected_callback=conn._on_disconnected,
    )
    mock_old_bleak_client.disconnect.side_effect = lambda: conn._on_disconnected(None)
    mock_old_bleak_client.start_notify.assert_has_awaits(
        [
            mock.call(ble.CHAR_RPC_RX_CTL, conn._on_rpc_data_received),
            mock.call(ble.CHAR_DEBUG_LOG, conn._on_debug_log_received),
        ]
    )

    mock_establish_connection.return_value = mock_new_bleak_client
    await conn.reset_device(mock_new_device)
    assert conn.is_connected()

    mock_old_bleak_client.disconnect.assert_awaited()
    mock_establish_connection.assert_awaited_with(
        client_class=BleakClientWithServiceCache,
        device=mock_new_device,
        name=mock_new_device.name,
        disconnected_callback=conn._on_disconnected,
    )
    mock_new_bleak_client.start_notify.assert_has_awaits(
        [
            mock.call(ble.CHAR_RPC_RX_CTL, conn._on_rpc_data_received),
            mock.call(ble.CHAR_DEBUG_LOG, conn._on_debug_log_received),
        ]
    )


@mock.patch("bleak_retry_connector.establish_connection")
async def test_reset_device_connect_failure_resets_reconnecting_flag(
    mock_establish_connection,
):
    """Regression test: _reconnecting must be reset to False even if connect() fails.

    Before this fix, if establish_connection() raised during reset_device(),
    _reconnecting would be stuck at True, silently suppressing the
    disconnect_callback on any subsequent disconnection event.
    """
    mock_old_device = mock.create_autospec(bleak.BLEDevice)
    mock_new_device = mock.create_autospec(bleak.BLEDevice)
    mock_old_device.name = "OLD DEVICE NAME"
    mock_new_device.name = "NEW DEVICE NAME"
    mock_old_bleak_client = mock.create_autospec(bleak.BleakClient)
    mock_establish_connection.return_value = mock_old_bleak_client

    disconnect_callback = mock.Mock()
    conn = ble.BleConnection(mock_old_device, disconnect_callback=disconnect_callback)
    await conn.connect()

    # Simulate establish_connection failing during the reset (e.g. out of BLE slots)
    mock_establish_connection.side_effect = Exception("No connection slots available")

    try:
        await conn.reset_device(mock_new_device)
    except Exception:  # noqa: BLE001, S110
        pass

    # _reconnecting must be False so future disconnect events fire the callback
    assert not conn._reconnecting
    # And the disconnect callback should be invocable; simulate a disconnect event
    conn._on_disconnected(mock_old_bleak_client)
    disconnect_callback.assert_called_once_with(mock_old_bleak_client)


@mock.patch("bleak_retry_connector.establish_connection")
async def test_concurrent_resets_run_one_at_a_time(mock_establish_connection):
    """Two resets racing must not interleave their disconnect/connect steps.

    Establishing a connection takes seconds, and a consumer driving resets
    from discovery schedules one per advertisement seen while disconnected --
    so several are queued in exactly that window. Unserialized, the losing
    `establish_connection` leaked a connected client.
    """
    device_a = mock.create_autospec(bleak.BLEDevice)
    device_b = mock.create_autospec(bleak.BLEDevice)
    device_a.name = device_b.name = "GRILL"

    in_flight = 0
    max_in_flight = 0

    async def slow_establish(**kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        # Yield so the racing reset gets its chance to interleave.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        in_flight -= 1
        return mock.create_autospec(bleak.BleakClient)

    mock_establish_connection.side_effect = slow_establish

    conn = ble.BleConnection(device_a)
    await asyncio.gather(conn.reset_device(device_a), conn.reset_device(device_b))

    assert max_in_flight == 1
    assert conn.is_connected()


@mock.patch("bleak_retry_connector.establish_connection")
async def test_connect_racing_a_reset_runs_one_at_a_time(mock_establish_connection):
    """`connect()` takes the lifecycle lock in its own right, not via reset.

    The reachable interleaving: a consumer connects through `reset_device`
    (discovery), the link drops before its follow-up `connect()` call runs,
    and the drop queues another reset -- so `connect()` and `reset_device`
    race on a disconnected transport. Without the lock on `connect()`, both
    run `establish_connection` concurrently and the loser leaks.
    """
    device_a = mock.create_autospec(bleak.BLEDevice)
    device_b = mock.create_autospec(bleak.BLEDevice)
    device_a.name = device_b.name = "GRILL"
    device_a.address = "AA:BB:CC:DD:EE:FF"
    device_b.address = "AA:BB:CC:DD:EE:FF"

    in_flight = 0
    max_in_flight = 0

    async def slow_establish(**kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        # Yield so the racing call gets its chance to interleave.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        in_flight -= 1
        return mock.create_autospec(bleak.BleakClient)

    mock_establish_connection.side_effect = slow_establish

    conn = ble.BleConnection(device_a)
    await asyncio.gather(conn.connect(), conn.reset_device(device_b))

    assert max_in_flight == 1
    assert conn.is_connected()


@mock.patch("bleak_retry_connector.establish_connection")
async def test_disconnect_racing_a_reset_leaves_nothing_behind(
    mock_establish_connection,
):
    """`disconnect()` takes the lifecycle lock in its own right too.

    The reachable interleaving, from review of the lock: discovery connects
    through `reset_device`; the link drops, so the flag is cleared and the
    drop queues another reset -- which does the full teardown/reconnect
    rather than coalescing; the entry then unloads, calling `disconnect()`
    while that reset is mid-flight. Unserialized, `disconnect()` slots
    between the reset's teardown and its reconnect: the client the reset
    establishes afterwards survives the unload -- a live connection nobody
    disconnected, on a transport reporting connected after an explicit
    `disconnect()`.
    """
    first_seen = mock.create_autospec(bleak.BLEDevice)
    readvertised = mock.create_autospec(bleak.BLEDevice)
    first_seen.name = readvertised.name = "GRILL"
    first_seen.address = readvertised.address = "AA:BB:CC:DD:EE:FF"
    client_1 = mock.create_autospec(bleak.BleakClient)
    client_2 = mock.create_autospec(bleak.BleakClient)
    clients = iter([client_1, client_2])

    async def slow_establish(**kwargs):
        # Yield so the racing disconnect gets its chance to interleave.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return next(clients)

    mock_establish_connection.side_effect = slow_establish

    conn = ble.BleConnection(first_seen)
    await conn.reset_device(first_seen)
    assert conn.is_connected()

    # The link drops, which also queues the reset raced below.
    conn._on_disconnected(client_1)

    await asyncio.gather(conn.reset_device(readvertised), conn.disconnect())

    assert not conn.is_connected()
    assert conn._ble_client is None
    # The client the reset established was handed to `disconnect()` rather
    # than surviving the unload.
    client_2.disconnect.assert_awaited()
    assert mock_establish_connection.await_count == 2


@mock.patch("bleak_retry_connector.establish_connection")
async def test_a_queued_duplicate_reset_keeps_the_fresh_connection(
    mock_establish_connection,
):
    """Resets queued for the same grill coalesce instead of churning.

    By the time the later ones run, the first has already connected; tearing
    that connection down to rebuild it against the same address was pure
    churn. The fresher `BLEDevice` is still adopted.
    """
    first_seen = mock.create_autospec(bleak.BLEDevice)
    readvertised = mock.create_autospec(bleak.BLEDevice)
    first_seen.name = readvertised.name = "GRILL"
    first_seen.address = readvertised.address = "AA:BB:CC:DD:EE:FF"
    mock_bleak_client = mock.create_autospec(bleak.BleakClient)
    mock_establish_connection.return_value = mock_bleak_client

    conn = ble.BleConnection(first_seen)
    await asyncio.gather(conn.reset_device(first_seen), conn.reset_device(readvertised))

    assert conn.is_connected()
    mock_establish_connection.assert_awaited_once()
    mock_bleak_client.disconnect.assert_not_awaited()
    assert conn._ble_device is readvertised


@mock.patch("bleak_retry_connector.establish_connection")
async def test_reset_to_a_different_address_still_reconnects(
    mock_establish_connection,
):
    """Coalescing is by address; a different device still gets a full reset."""
    device_a = mock.create_autospec(bleak.BLEDevice)
    device_b = mock.create_autospec(bleak.BLEDevice)
    device_a.name = device_b.name = "GRILL"
    device_a.address = "AA:BB:CC:DD:EE:FF"
    device_b.address = "11:22:33:44:55:66"
    client_a = mock.create_autospec(bleak.BleakClient)
    client_b = mock.create_autospec(bleak.BleakClient)
    mock_establish_connection.side_effect = [client_a, client_b]

    conn = ble.BleConnection(device_a)
    await conn.connect()
    await conn.reset_device(device_b)

    assert conn.is_connected()
    client_a.disconnect.assert_awaited_once()
    assert conn._ble_device is device_b
    assert mock_establish_connection.await_count == 2


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_subscribe_debug_logs(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client

    conn = ble.BleConnection(mock_device)
    state_cb = mock.AsyncMock()
    conn.set_state_callback(state_cb)
    await conn.connect()

    state_data = bytearray(b"<==PB: FE0B.STATE [10]")
    await conn._on_debug_log_received(None, state_data)
    state_cb.assert_awaited_once_with("FE0B.STATE", None)


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_subscribe_debug_logs_pbv3m(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client

    conn = ble.BleConnection(mock_device)
    state_cb = mock.AsyncMock()
    conn.set_state_callback(state_cb)
    await conn.connect()

    # PBV3 M payloads have no checksum suffix — only 2 whitespace-separated parts.
    state_data = bytearray(
        b"<==PB:  FE0B020205090600090600090600000000000802000802010200000000000000000000000000000100000000FF"
    )
    await conn._on_debug_log_received(None, state_data)
    state_cb.assert_awaited_once_with(
        "FE0B020205090600090600090600000000000802000802010200000000000000000000000000000100000000FF",
        None,
    )

    state_cb.reset_mock()
    temp_data = bytearray(
        b"<==PB:  FE0C02020509060009060009060000000000080201030000080201FF"
    )
    await conn._on_debug_log_received(None, temp_data)
    state_cb.assert_awaited_once_with(
        None,
        "FE0C02020509060009060009060000000000080201030000080201FF",
    )


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_send_command(mock_device, mock_bleak_client, mock_establish_connection):
    mock_establish_connection.return_value = mock_bleak_client
    loop = asyncio.get_running_loop()
    conn = ble.BleConnection(mock_device, loop=loop)
    await conn.connect()
    # TODO: Rewrite this test to fake out notify routines instead of patching create_future.
    future = loop.create_future()
    with mock.patch.object(loop, "create_future") as mock_create_future:
        mock_create_future.return_value = future
        # Simulate an _on_rpc_data_received() call
        future.set_result({"my": "data"})
        result = await conn.send_command("Some.Command", {"foo": "bar"})
        assert result == {"my": "data"}

        mock_bleak_client.write_gatt_char.assert_has_awaits(
            [
                mock.call(ble.CHAR_RPC_TX_CTL, bytearray([0, 0, 0, 61])),
                mock.call(ble.CHAR_RPC_DATA, bytearray(b'{"id": 1, "method": ')),
                mock.call(ble.CHAR_RPC_DATA, bytearray(b'"Some.Command", "par')),
                mock.call(ble.CHAR_RPC_DATA, bytearray(b'ams": {"foo": "bar"}')),
                mock.call(ble.CHAR_RPC_DATA, bytearray(b"}")),
            ]
        )


def chunk(s: str, size: int = 10) -> list[str]:
    iterators = [iter(s)] * size
    return ["".join(x) for x in zip_longest(*iterators, fillvalue="")]


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_on_rpc_data_received(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client
    loop = asyncio.get_running_loop()
    conn = ble.BleConnection(mock_device, loop=loop)
    await conn.connect()
    future = loop.create_future()
    conn._rpc_futures[1] = future

    resp = {"id": 1, "result": {"foo": "bar"}}
    resp_json = json.dumps(resp)

    mock_bleak_client.read_gatt_char.side_effect = [
        bytearray(n.encode("utf-8")) for n in chunk(resp_json)
    ]
    await conn._on_rpc_data_received(mock.Mock(), bytearray([0, 0, 0, len(resp_json)]))
    assert future.done()
    assert future.result() == resp["result"]
    mock_bleak_client.read_gatt_char.assert_has_awaits(
        [mock.call(ble.CHAR_RPC_DATA)] * 3
    )


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_on_rpc_error_received(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client
    loop = asyncio.get_running_loop()
    conn = ble.BleConnection(mock_device, loop=loop)
    await conn.connect()
    future = loop.create_future()
    conn._rpc_futures[1] = future

    resp = {"id": 1, "error": {"code": 1234, "message": "Oh noes"}}
    resp_json = json.dumps(resp)

    mock_bleak_client.read_gatt_char.side_effect = [
        bytearray(n.encode("utf-8")) for n in chunk(resp_json)
    ]
    await conn._on_rpc_data_received(mock.Mock(), bytearray([0, 0, 0, len(resp_json)]))
    assert future.done()
    with raises(RPCError, match="Oh noes"):
        future.result()

    mock_bleak_client.read_gatt_char.assert_has_awaits(
        [mock.call(ble.CHAR_RPC_DATA)] * 3
    )


def test_encode_decode_len():
    assert ble._encode_len(2**32 - 1) == bytearray([255, 255, 255, 255])
    assert ble._decode_len(bytearray([255, 255, 255, 255])) == 2**32 - 1


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_connect_when_already_connected(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client

    conn = ble.BleConnection(mock_device)
    await conn.connect()
    assert conn.is_connected()

    # Connecting again should be a no-op (logged at debug level).
    await conn.connect()
    mock_establish_connection.assert_awaited_once()


@mock.patch("bleak_retry_connector.establish_connection")
async def test_connect_no_device(mock_establish_connection):
    mock_device = mock.create_autospec(bleak.BLEDevice)
    conn = ble.BleConnection(mock_device)
    conn._ble_device = None

    await conn.connect()

    assert not conn.is_connected()
    mock_establish_connection.assert_not_awaited()


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_disconnect_swallows_exception(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device)
    await conn.connect()

    mock_bleak_client.disconnect.side_effect = Exception("Bluetooth is awful")

    await conn.disconnect()  # Should not raise.
    assert not conn.is_connected()


async def test_send_prepared_command_no_client():
    mock_device = mock.create_autospec(bleak.BLEDevice)
    conn = ble.BleConnection(mock_device)
    # Never connected, so there is no BLE client yet. This used to return
    # silently, which left the caller awaiting a reply to a command that was
    # never sent.
    with raises(NotConnectedError):
        await conn._send_prepared_command({"id": 1, "method": "foo", "params": {}})


async def test_on_rpc_data_received_no_client():
    mock_device = mock.create_autospec(bleak.BLEDevice)
    conn = ble.BleConnection(mock_device)
    # Never connected, so there is no BLE client yet.
    await conn._on_rpc_data_received(mock.Mock(), bytearray([0, 0, 0, 0]))


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_debug_log_bad_checksum(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device)
    state_cb = mock.AsyncMock()
    conn.set_state_callback(state_cb)
    await conn.connect()

    # Checksum (5) doesn't match the actual payload length (10).
    bad_data = bytearray(b"<==PB: FE0B.STATE [5]")
    await conn._on_debug_log_received(None, bad_data)
    state_cb.assert_not_awaited()


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_debug_log_unknown_format(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device)
    state_cb = mock.AsyncMock()
    conn.set_state_callback(state_cb)
    await conn.connect()

    # Neither 2 nor 3 whitespace-separated parts; should be ignored.
    await conn._on_debug_log_received(None, bytearray(b"<==PB:"))
    state_cb.assert_not_awaited()


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_debug_log_vdata(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device)
    vdata_cb = mock.AsyncMock()
    conn.set_vdata_callback(vdata_cb)
    await conn.connect()

    data = bytearray(b'<==PBD:  {"foo":"bar"}')
    await conn._on_debug_log_received(None, data)
    vdata_cb.assert_awaited_once_with('{"foo":"bar"}')


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_a_failed_notify_does_not_report_connected(
    mock_device, mock_bleak_client, mock_establish_connection
):
    """Until the notifications are registered there is no receive path.

    Reporting connected before them meant a command could be sent and never
    answered, while `is_connected()` said the transport was fine.
    """
    mock_establish_connection.return_value = mock_bleak_client
    mock_bleak_client.start_notify.side_effect = bleak.exc.BleakError("nope")

    conn = ble.BleConnection(mock_device)
    with raises(bleak.exc.BleakError):
        await conn.connect()

    assert not conn.is_connected()


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_a_command_after_disconnect_reports_not_connected(
    mock_device, mock_bleak_client, mock_establish_connection
):
    """Every other transport raises `NotConnectedError`; this one raised a
    `BleakError` from a client it had already disconnected."""
    mock_establish_connection.return_value = mock_bleak_client

    conn = ble.BleConnection(mock_device)
    await conn.connect()
    await conn.disconnect()

    with raises(NotConnectedError):
        await conn.send_command("PB.GetState", {}, timeout=1.0)


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_disconnect_notification_during_shutdown_does_not_raise(
    mock_device, mock_bleak_client, mock_establish_connection
):
    """Bleak calls `_on_disconnected` synchronously, including as the loop
    goes away -- scheduling there would raise inside its own callback."""
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device)
    await conn.connect()

    with mock.patch.object(
        conn._loop, "create_task", side_effect=RuntimeError("loop is closing")
    ):
        conn._on_disconnected(mock_bleak_client)

    assert not conn.is_connected()


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_debug_log_vdata_with_a_space(
    mock_device, mock_bleak_client, mock_establish_connection
):
    """A JSON string value with a space in it is still one payload.

    Reachable through `set_virtual_data`: the grill echoes back what was
    written, so a consumer storing any string with a space made this parse
    raise `ValueError` inside the notification callback.
    """
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device)
    vdata_cb = mock.AsyncMock()
    conn.set_vdata_callback(vdata_cb)
    await conn.connect()

    data = bytearray(b'<==PBD:  {"note":"hello world"}')
    await conn._on_debug_log_received(None, data)

    vdata_cb.assert_awaited_once_with('{"note":"hello world"}')


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_debug_log_checksum_counts_the_whole_payload(
    mock_device, mock_bleak_client, mock_establish_connection
):
    """A payload with a space is measured whole, not up to the first one."""
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device)
    vdata_cb = mock.AsyncMock()
    conn.set_vdata_callback(vdata_cb)
    await conn.connect()

    payload = '{"note":"hello world"}'
    data = bytearray(f"<==PBD:  {payload} [{len(payload)}]".encode())
    await conn._on_debug_log_received(None, data)

    vdata_cb.assert_awaited_once_with(payload)


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_on_rpc_data_received_gives_up_on_an_empty_read(
    mock_device, mock_bleak_client, mock_establish_connection
):
    """A read that returns nothing cannot make progress.

    Without this the loop spins on the empty read forever, and because
    nothing in it suspends, no other task on the event loop runs again.
    """
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device, loop=asyncio.get_running_loop())
    await conn.connect()
    mock_bleak_client.read_gatt_char.side_effect = [
        bytearray(b"partial"),
        bytearray(b""),
        bytearray(b"never reached"),
    ]

    await asyncio.wait_for(
        conn._on_rpc_data_received(mock.Mock(), bytearray([0, 0, 0, 64])), timeout=5
    )

    assert mock_bleak_client.read_gatt_char.await_count == 2


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_on_rpc_data_received_rejects_an_implausible_length(
    mock_device, mock_bleak_client, mock_establish_connection
):
    """The length is whatever the notification claimed, up to 4 GiB."""
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device, loop=asyncio.get_running_loop())
    await conn.connect()

    await conn._on_rpc_data_received(mock.Mock(), bytearray([0xFF, 0xFF, 0xFF, 0xFF]))

    mock_bleak_client.read_gatt_char.assert_not_awaited()


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_an_unsolicited_disconnect_releases_the_client(
    mock_device, mock_bleak_client, mock_establish_connection
):
    """A dropped link has to look like a disconnect to the send path.

    `_send_prepared_command` decides from `_ble_client` alone, so a client
    left behind raises `BleakError` where an explicit `disconnect()` gives
    `NotConnectedError`.
    """
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device, loop=asyncio.get_running_loop())
    await conn.connect()

    # Bleak calls this itself when the grill goes away.
    conn._on_disconnected(mock_bleak_client)
    # Let the cleanup it schedules finish, so what follows is a fresh command
    # rather than one caught by `_fail_pending_commands`.
    await asyncio.sleep(0)
    # A real client refuses once the link is gone.
    mock_bleak_client.write_gatt_char.side_effect = bleak.exc.BleakError(
        "Not connected"
    )

    assert conn.is_connected() is False
    with pytest.raises(NotConnectedError):
        await conn.send_command("PB.GetState", {}, timeout=1)


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_a_disconnect_for_a_replaced_client_is_ignored(
    mock_device, mock_bleak_client, mock_establish_connection
):
    """A late callback for the previous client must not drop the live one."""
    mock_establish_connection.return_value = mock_bleak_client
    conn = ble.BleConnection(mock_device, loop=asyncio.get_running_loop())
    await conn.connect()

    conn._on_disconnected(mock.Mock())  # some earlier client

    assert conn._ble_client is mock_bleak_client


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_a_failed_start_notify_releases_the_connection(
    mock_device, mock_bleak_client, mock_establish_connection
):
    """The connection succeeded, so nothing else will release it.

    Each retry would otherwise burn a slot on an adapter that has few, and
    the next `connect()` overwrites the only reference to it.
    """
    mock_establish_connection.return_value = mock_bleak_client
    mock_bleak_client.start_notify.side_effect = bleak.exc.BleakError("no notify")
    conn = ble.BleConnection(mock_device, loop=asyncio.get_running_loop())

    with pytest.raises(bleak.exc.BleakError):
        await conn.connect()

    mock_bleak_client.disconnect.assert_awaited_once()
    assert conn._ble_client is None
    assert conn.is_connected() is False
