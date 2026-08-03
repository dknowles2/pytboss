"""Bluetooth LE connection support for Mongoose OS devices.

Also see:
  https://mongoose-os.com/docs/mongoose-os/api/rpc/rpc-gatts.md
  https://mongoose-os.com/docs/mongoose-os/api/net/bt-service-debug.md
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from uuid import UUID

import bleak_retry_connector
from bleak import BleakClient, BleakGATTCharacteristic, BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache

from .exceptions import NotConnectedError
from .transport import Transport

_LOGGER = logging.getLogger("pytboss")


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

DisconnectCallback = Callable[[BleakClient], None]
"""A callback function called when the BLE connection is disconnected."""


class BleConnection(Transport):
    """Bluetooth LE protocol transport for Mongoose OS devices."""

    _ble_device: BLEDevice | None = None
    _ble_client: BleakClient | None = None

    def __init__(
        self,
        ble_device: BLEDevice,
        disconnect_callback: DisconnectCallback | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initializes a BleConnection.

        :param ble_device: BLE device to use for transport.
        :param disconnect_callback: Function to call when the BLE connection is disconnected.
        :param loop: An asyncio loop to use. If `None`, the default loop will be used.
        """
        super().__init__(loop=loop)
        self._ble_device: BLEDevice = ble_device
        self._disconnect_callback = disconnect_callback
        self._is_connected = False
        self._reconnecting = False

    async def connect(self) -> None:
        """Starts the connection to the device.

        Does nothing if already connected or if no BLE device was set.
        """
        if self._is_connected:
            _LOGGER.warning("Already connected. Ignoring call to connect().")
            return
        if self._ble_device is None:
            return
        self._ble_client = await bleak_retry_connector.establish_connection(
            client_class=BleakClientWithServiceCache,
            device=self._ble_device,
            name=self._ble_device.name or "<unknown>",
            disconnected_callback=self._on_disconnected,
        )
        # Only once the notifications are registered: until they are there is
        # no receive path, so a command would be sent and never answered while
        # `is_connected()` claimed otherwise.
        await self._ble_client.start_notify(CHAR_RPC_RX_CTL, self._on_rpc_data_received)
        await self._ble_client.start_notify(CHAR_DEBUG_LOG, self._on_debug_log_received)
        self._is_connected = True

    def _on_disconnected(self, client: BleakClient) -> None:
        """Called when our Bluetooth client is disconnected."""
        _LOGGER.debug("Bluetooth disconnected.")
        self._is_connected = False
        # Bleak calls this synchronously, so the cleanup has to be scheduled.
        # Guarded because this also fires while the loop is shutting down,
        # where scheduling raises inside bleak's own callback.
        if not self._loop.is_closed():
            # The coroutine is built before `create_task` is called, so it has
            # to be closed explicitly when scheduling fails -- otherwise it is
            # abandoned unawaited and warns from wherever the collector runs.
            coro = self._fail_pending_commands()
            try:
                self._loop.create_task(coro)
            except RuntimeError:  # pragma: no cover - loop shutting down
                coro.close()
                _LOGGER.debug("Loop is closing; not failing pending commands.")
        if not self._reconnecting and self._disconnect_callback is not None:
            self._disconnect_callback(client)

    async def disconnect(self) -> None:
        """Stops the connection to the device."""
        _LOGGER.debug("Disconnecting from device.")
        if self._ble_client:
            try:
                await self._ble_client.disconnect()
            except Exception as ex:  # noqa: BLE001
                # Bluetooth is awful. Sometimes even disconnects fail.
                _LOGGER.debug("Failed to disconnect: %s", ex)
            # Released either way: a client kept after a disconnect answers a
            # later command with a raw `BleakError` instead of the
            # `NotConnectedError` every other transport raises.
            self._ble_client = None
        self._is_connected = False

    async def reset_device(self, ble_device: BLEDevice):
        """Resets the BLE device used for transport.

        :param ble_device: BLE device to use for transport.
        """
        self._reconnecting = True
        _LOGGER.debug("Resetting device to: %s", ble_device)
        try:
            await self.disconnect()
            self._is_connected = False
            self._ble_device = ble_device
            await self.connect()
        finally:
            self._reconnecting = False

    def is_connected(self) -> bool:
        """Whether the device is currently connected."""
        return self._is_connected

    async def _send_prepared_command(self, cmd: dict):
        if self._ble_client is None:
            # Raised rather than returned: swallowing the send left the caller
            # awaiting a reply for a command that was never put on the wire,
            # so it waited out its whole timeout to learn nothing.
            raise NotConnectedError("Not connected")
        payload = json.dumps(cmd)
        async with self._lock:
            await self._ble_client.write_gatt_char(
                CHAR_RPC_TX_CTL, _encode_len(len(payload))
            )
            for i in range(0, len(payload), 20):
                chunk = bytearray(payload[i : i + 20].encode("utf-8"))
                await self._ble_client.write_gatt_char(CHAR_RPC_DATA, chunk)

    async def _on_rpc_data_received(
        self, unused_char: BleakGATTCharacteristic, data: bytearray
    ):
        if self._ble_client is None:
            return
        resp_len = _decode_len(data)
        resp = bytearray()
        async with self._lock:
            while len(resp) < resp_len:
                resp += await self._ble_client.read_gatt_char(CHAR_RPC_DATA)

        payload = json.loads(resp.decode("utf-8"))
        await self._on_command_response(payload)

    async def _on_debug_log_received(
        self, unused_char: BleakGATTCharacteristic, data: bytearray
    ):
        _LOGGER.debug("Debug log received: %s", data)
        # Split off the head and an optional trailing "[len]" rather than on
        # every space: a virtual data payload is JSON, and any string value in
        # it with a space in it would otherwise either be dropped or parsed as
        # the length. Both are reachable through `set_virtual_data`, since the
        # grill echoes back what was written.
        head, _, rest = data.decode("utf-8").strip().partition(" ")
        payload = rest.strip()
        if not payload:
            # Unknown payload; ignore.
            return
        if payload.endswith("]"):
            body, _, tail = payload.rpartition("[")
            if body and tail[:-1].isdigit():
                payload = body.strip()
                checksum = int(tail[:-1])
                if len(payload) != checksum:
                    # Bad payload; ignore.
                    _LOGGER.debug(
                        "Ignoring message with bad checksum (%d != %d)",
                        len(payload),
                        checksum,
                    )
                    return
        if head == "<==PB:" and self._state_callback:
            status_payload = temperatures_payload = None
            match payload[:4]:
                case "FE0B":
                    status_payload = payload
                case "FE0C":
                    temperatures_payload = payload
            await self._state_callback(status_payload, temperatures_payload)
        elif head == "<==PBD:" and self._vdata_callback:
            # TODO: I think we want to decode this?
            await self._vdata_callback(payload)


def _encode_len(n: int) -> bytearray:
    ret = bytearray([0, 0, 0, 0])
    for i in range(4):
        ret[3 - i] = 255 & n
        n >>= 8
    return ret


def _decode_len(n: bytearray) -> int:
    return n[0] << 24 | n[1] << 16 | n[2] << 8 | n[3]
