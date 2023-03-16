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

from bleak import BleakClient, BleakGATTCharacteristic

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

    def __init__(
        self, ble_client: BleakClient, loop: asyncio.AbstractEventLoop | None = None
    ) -> None:
        """Initializes a BleConnection.

        :param ble_client: Bluetooth client to use for transport.
        :type ble_client: bleak.BleakClient
        :param loop: An asyncio loop to use. If `None`, the default loop will be used.
        :type loop: asyncio.AbstractEventLoop
        """
        self._ble_client = ble_client
        if loop is None:
            loop = asyncio.get_running_loop()
        self._loop = loop

        self._lock = asyncio.Lock()  # Protects items below.
        self._last_command_id = 0
        self._rpc_futures = {}
        self._debug_log_callback: DebugLogCallback | None = None

    async def start(self):
        """Starts the connection to the device."""
        await self._ble_client.start_notify(CHAR_RPC_RX_CTL, self._on_rpc_data_received)

    async def stop(self):
        """Stops the connection to the device."""
        await self._ble_client.stop_notify(CHAR_RPC_RX_CTL)
        if self._debug_log_callback:
            await self._ble_client.stop_notify(CHAR_DEBUG_LOG)

    async def subscribe_debug_logs(
        self, callback: DebugLogCallback
    ) -> Callable[[None], None]:
        """Subscribes to debug log output from the device.

        Returns a function that can cancel the subscription.

        :param callback: Function to call when debug logs are output by the device.
        :type callback: DebugLogCallback
        """
        assert self._debug_log_callback is None, "Only one subscription is supported"
        self._debug_log_callback = callback
        self._ble_client.start_notify(CHAR_DEBUG_LOG, self._on_debug_log_received)

        def cancel():
            if not self._debug_log_callback:
                # Assume we've already cancelled the subscription.
                return
            self._loop.run_until_complete(self._ble_client.stop_notify(CHAR_DEBUG_LOG))
            self._debug_log_callback = None

        return cancel

    async def _next_command_id(self) -> int:
        async with self._lock:
            self._last_command_id = self._last_command_id + 1 & 2047
            return self._last_command_id

    async def send_command(self, method: str, params: dict, timeout: int = 60) -> dict:
        """Sends a command to the device.

        :param method: The method to call.
        :type method: str
        :param params: Parameters to send with the command.
        :type params: dict
        :param timeout: Time (in seconds) after which to abort the command.
        :type timeout: int
        :rtype: dict
        """
        command_id = await self._next_command_id()
        cmd = json.dumps({"id": command_id, "method": method, "params": params})
        future = self._loop.create_future()
        async with self._lock:
            self._rpc_futures[command_id] = future
        await asyncio.wait_for(self._send_prepared_command(cmd), timeout=timeout)
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
        payload = bytearray([0, 0, 0, 0])
        n = len(cmd)
        for i in range(0, 4):
            payload[3 - i] = 255 & n
            n >>= 8
        await self._ble_client.write_gatt_char(CHAR_RPC_TX_CTL, payload)
        for i in range(0, len(cmd), 20):
            chunk = bytearray(cmd[i : i + 20].encode("utf-8"))  # noqa: E203
            await self._ble_client.write_gatt_char(CHAR_RPC_DATA, chunk)

    async def _on_debug_log_received(
        self, unused_char: BleakGATTCharacteristic, data: bytearray
    ):
        if not self._debug_log_callback:
            # This shouldn't happen, but protect against it anyway.
            return
        self._debug_log_callback(data)

    async def _on_rpc_data_received(
        self, unused_char: BleakGATTCharacteristic, data: bytearray
    ):
        resp_len = data[0] << 24 | data[1] << 16 | data[2] << 8 | data[3]
        resp = bytearray()
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
