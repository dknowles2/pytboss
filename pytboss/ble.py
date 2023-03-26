"""Bluetooth LE connection support for Mongoose OS devices.

Also see:
  https://mongoose-os.com/docs/mongoose-os/api/rpc/rpc-gatts.md
  https://mongoose-os.com/docs/mongoose-os/api/net/bt-service-debug.md
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable
from uuid import UUID

import bleak_retry_connector
from bleak import BleakClient, BleakGATTCharacteristic, BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache

from .exceptions import RPCError


def _uuid(s: str) -> str:
    return str(UUID(bytes=s.encode()))


# See: https://mongoose-os.com/docs/mongoose-os/api/net/bt-service-debug.md
SERVICE_DEBUG = _uuid("_mOS_DBG_SVC_ID_")
CHAR_DEBUG_LOG = _uuid("0mOS_DBG_log___0")

# See: https://mongoose-os.com/docs/mongoose-os/api/rpc/rpc-gatts.md
SERVICE_RPC = _uuid("_mOS_RPC_SVC_ID_")
CHAR_RPC_DATA = _uuid("_mOS_RPC_data___")
CHAR_RPC_TX_CTL = _uuid("_mOS_RPC_tx_ctl_")
CHAR_RPC_RX_CTL = _uuid("_mOS_RPC_rx_ctl_")

DebugLogCallback = Callable[[bytearray], None]
"""A callback function that receives debug logs output from the device."""


class BleConnection:
    """Bluetooth LE protocol transport for Mongoose OS devices."""

    _ble_device: BLEDevice | None = None
    _ble_client: BleakClient | None = None

    def __init__(
        self, ble_device: BLEDevice, loop: asyncio.AbstractEventLoop | None = None
    ) -> None:
        """Initializes a BleConnection.

        :param ble_device: BLE device to use for transport.
        :type ble_device: bleak.BLEDevice
        :param loop: An asyncio loop to use. If `None`, the default loop will be used.
        :type loop: asyncio.AbstractEventLoop
        """
        if loop is None:
            loop = asyncio.get_running_loop()
        self._loop = loop

        self._ble_device: BLEDevice = ble_device
        self._is_connected = False

        self._lock = asyncio.Lock()  # Protects items below.
        self._last_command_id = 0
        self._rpc_futures = {}
        self._debug_log_callback: DebugLogCallback | None = None

    async def connect(self) -> None:
        """Starts the connection to the device."""
        if self._is_connected:
            return
        self._ble_client = await bleak_retry_connector.establish_connection(
            client_class=BleakClientWithServiceCache,
            device=self._ble_device,
            name=self._ble_device.name,
            disconnected_callback=self._on_disconnected,
        )
        self._is_connected = True
        await self._ble_client.start_notify(CHAR_RPC_RX_CTL, self._on_rpc_data_received)

    def _on_disconnected(self, unused_client):
        """Called when our Bluetooth client is disconnected."""
        self._is_connected = False

    async def disconnect(self) -> None:
        """Stops the connection to the device."""
        await self._ble_client.disconnect()
        self._is_connected = False

    async def reset_device(self, ble_device: BLEDevice):
        """Resets the BLE device used for transport.

        :param ble_device: BLE device to use for transport.
        :type ble_device: bleak.BLEDevice
        """
        await self.disconnect()
        self._is_connected = False
        self._ble_device = ble_device
        await self.connect()
        async with self._lock:
            if self._debug_log_callback:
                await self._ble_client.start_notify(
                    CHAR_DEBUG_LOG, self._on_debug_log_received
                )

    def is_connected(self) -> bool:
        """Whether the device is currently connected."""
        return self._is_connected

    async def subscribe_debug_logs(self, callback: DebugLogCallback) -> None:
        """Subscribes to debug log output from the device.

        :param callback: Function to call when debug logs are output by the device.
        :type callback: DebugLogCallback
        """
        async with self._lock:
            assert (
                self._debug_log_callback is None
            ), "Only one subscription is supported"
            self._debug_log_callback = callback
            await self._ble_client.start_notify(
                CHAR_DEBUG_LOG, self._on_debug_log_received
            )

    async def unsubscribe_debug_logs(self) -> None:
        """Unsubscribes from debug log output."""
        async with self._lock:
            if not self._debug_log_callback:
                # Assume we've already cancelled the subscription.
                return
            await self._ble_client.stop_notify(CHAR_DEBUG_LOG)
            self._debug_log_callback = None

    async def _next_command_id(self) -> int:
        async with self._lock:
            self._last_command_id = self._last_command_id + 1 & 2047
            return self._last_command_id

    async def send_command(self, method: str, params: dict) -> dict:
        """Sends a command to the device.

        :param method: The method to call.
        :type method: str
        :param params: Parameters to send with the command.
        :type params: dict
        :rtype: dict
        """
        command_id = await self._next_command_id()
        cmd = json.dumps({"id": command_id, "method": method, "params": params})
        future = self._loop.create_future()
        async with self._lock:
            self._rpc_futures[command_id] = future
        await self._send_prepared_command(cmd)
        return await future

    async def send_command_without_answer(self, method: str, params: dict):
        """Sends a command to the device and doesn't wait for the response.

        :param method: The method to call.
        :type method: str
        :param params: Parameters to send with the command.
        :type params: dict
        """
        command_id = await self._next_command_id()
        cmd = json.dumps({"id": command_id, "method": method, "params": params})
        await self._send_prepared_command(cmd)

    async def _send_prepared_command(self, cmd: str):
        async with self._lock:
            await self._ble_client.write_gatt_char(
                CHAR_RPC_TX_CTL, _encode_len(len(cmd))
            )
            for i in range(0, len(cmd), 20):
                chunk = bytearray(cmd[i : i + 20].encode("utf-8"))  # noqa: E203
                await self._ble_client.write_gatt_char(CHAR_RPC_DATA, chunk)

    async def _on_debug_log_received(
        self, unused_char: BleakGATTCharacteristic, data: bytearray
    ):
        async with self._lock:
            if not self._debug_log_callback:
                # This shouldn't happen, but protect against it anyway.
                return
            self._debug_log_callback(data)

    async def _on_rpc_data_received(
        self, unused_char: BleakGATTCharacteristic, data: bytearray
    ):
        resp_len = _decode_len(data)
        resp = bytearray()
        async with self._lock:
            while len(resp) < resp_len:
                resp += await self._ble_client.read_gatt_char(CHAR_RPC_DATA)

        payload = json.loads(resp.decode("utf-8"))

        async with self._lock:
            fut = self._rpc_futures.pop(payload["id"], None)

        if fut and not fut.cancelled():
            if "error" in payload:
                fut.set_exception(RPCError(payload.get("message", "Unknown error")))
                return

            fut.set_result(payload["result"])


def _encode_len(n: int) -> bytearray:
    ret = bytearray([0, 0, 0, 0])
    for i in range(0, 4):
        ret[3 - i] = 255 & n
        n >>= 8
    return ret


def _decode_len(n: bytearray) -> int:
    return n[0] << 24 | n[1] << 16 | n[2] << 8 | n[3]
