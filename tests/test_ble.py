import asyncio
from itertools import zip_longest
import json
from unittest import mock

import bleak
from bleak_retry_connector import BleakClientWithServiceCache
from pytest import raises

from pytboss import ble
from pytboss.exceptions import RPCError


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_connect_disconnect(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client

    conn = ble.BleConnection(mock_device)
    cb = mock.AsyncMock()
    await conn.connect(cb, cb)
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
    cb = mock.AsyncMock()
    await conn.connect(cb, cb)

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
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_subscribe_debug_logs(
    mock_device, mock_bleak_client, mock_establish_connection
):
    mock_establish_connection.return_value = mock_bleak_client

    conn = ble.BleConnection(mock_device)
    state_cb = mock.AsyncMock()
    vdata_cb = mock.AsyncMock
    await conn.connect(state_cb, vdata_cb)

    state_data = bytearray("<==PB: STATE [5]".encode("utf-8"))
    await conn._on_debug_log_received(state_data)
    state_cb.assert_called_once_with("STATE")


@mock.patch("bleak_retry_connector.establish_connection")
@mock.patch("bleak.BleakClient", spec=True)
@mock.patch("bleak.BLEDevice", spec=True)
async def test_send_command(mock_device, mock_bleak_client, mock_establish_connection):
    mock_establish_connection.return_value = mock_bleak_client
    loop = asyncio.get_running_loop()
    conn = ble.BleConnection(mock_device, loop=loop)
    cb = mock.AsyncMock()
    await conn.connect(cb, cb)
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
    cb = mock.AsyncMock()
    await conn.connect(cb, cb)
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
    cb = mock.AsyncMock()
    await conn.connect(cb, cb)
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
