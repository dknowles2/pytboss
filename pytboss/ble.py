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
from bleak.exc import BleakError
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

MAX_RPC_RESPONSE_SIZE = 64 * 1024
"""Largest reply this transport will try to assemble, in bytes.

The length comes off the wire, so a corrupt or desynchronised control
notification can ask for up to 4 GiB. Nothing the library sends provokes a
reply anywhere near this bound: the largest are `FS.Get`, capped at 512 bytes
of content, and `RPC.List`, about 1.4 kB on a grill serving 56 methods."""


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
        # Serializes connect/disconnect/reset_device. Establishing a
        # connection takes seconds, and a consumer driving reconnects from
        # discovery -- Home Assistant schedules a reset per advertisement
        # while `is_connected()` is False -- piles several up in exactly that
        # window. Unserialized, their disconnect/connect steps interleave on
        # the same object and the losing `establish_connection` leaks a
        # connected client. Distinct from `self._lock`, which serializes GATT
        # I/O; holding that one for the length of a connect would stall the
        # receive path.
        self._lifecycle_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Starts the connection to the device.

        Does nothing if already connected or if no BLE device was set.
        """
        async with self._lifecycle_lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        if self._is_connected:
            # Debug, not a warning: this is a documented no-op, and consumers
            # hit it on a normal path -- Home Assistant connects through
            # `reset_device` during discovery and then calls `PitBoss.start`,
            # whose `connect()` lands here at every Bluetooth setup.
            _LOGGER.debug("Already connected. Ignoring call to connect().")
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
        try:
            await self._ble_client.start_notify(
                CHAR_RPC_RX_CTL, self._on_rpc_data_received
            )
            await self._ble_client.start_notify(
                CHAR_DEBUG_LOG, self._on_debug_log_received
            )
        except Exception:
            # The connection itself succeeded, so leaving it behind holds a
            # slot on an adapter that has few, and the next `connect()` would
            # overwrite the only reference to it.
            client, self._ble_client = self._ble_client, None
            try:
                await client.disconnect()
            except Exception as ex:  # noqa: BLE001
                _LOGGER.debug("Failed to release the client: %s", ex)
            raise
        self._is_connected = True

    def _on_disconnected(self, client: BleakClient) -> None:
        """Called when our Bluetooth client is disconnected."""
        _LOGGER.debug("Bluetooth disconnected.")
        self._is_connected = False
        # Released here as well as in `_disconnect_locked`, which drops it for
        # the same reason: `_send_prepared_command` decides whether it can
        # send from this reference alone, so a client left behind turns an
        # unsolicited drop into `BleakError` where an explicit disconnect
        # gives `NotConnectedError`. Guarded on identity because a reconnect
        # may already have put a live client here.
        if self._ble_client is client:
            self._ble_client = None
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
        async with self._lifecycle_lock:
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
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

        A reset that arrives while already connected to the same device --
        by address -- only adopts the fresher `BLEDevice` and keeps the
        connection. Discovery-driven consumers queue a reset per
        advertisement seen while disconnected, and by the time the later
        ones run, the first has already connected; tearing that connection
        down to rebuild it against the same grill was pure churn.

        :param ble_device: BLE device to use for transport.
        """
        async with self._lifecycle_lock:
            if (
                self._is_connected
                and self._ble_device is not None
                and self._ble_device.address == ble_device.address
            ):
                _LOGGER.debug("Already connected to %s; keeping it", ble_device)
                self._ble_device = ble_device
                return
            self._reconnecting = True
            _LOGGER.debug("Resetting device to: %s", ble_device)
            try:
                await self._disconnect_locked()
                self._ble_device = ble_device
                await self._connect_locked()
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
            # Re-read under the lock: `_on_disconnected` clears `_ble_client`,
            # so the check above can be stale by the time the writes happen --
            # every `await` between chunks is a window. Bound once so a drop
            # mid-payload surfaces as bleak's own error on a dead client
            # rather than `AttributeError` on `None`.
            client = self._ble_client
            if client is None:
                raise NotConnectedError("Not connected")
            await client.write_gatt_char(CHAR_RPC_TX_CTL, _encode_len(len(payload)))
            for i in range(0, len(payload), 20):
                chunk = bytearray(payload[i : i + 20].encode("utf-8"))
                await client.write_gatt_char(CHAR_RPC_DATA, chunk)

    async def _on_rpc_data_received(
        self, unused_char: BleakGATTCharacteristic, data: bytearray
    ):
        if self._ble_client is None:
            return
        resp_len = _decode_len(data)
        if resp_len > MAX_RPC_RESPONSE_SIZE:
            # The length is whatever the notification said, so a corrupt one
            # sends this loop after gigabytes that will never arrive. Reading
            # them would take hours of real GATT round trips, all of it while
            # holding the lock a send needs, so abandon the reply instead.
            # The caller times out, which is what already happens whenever a
            # reply goes missing.
            _LOGGER.warning(
                "Ignoring implausible RPC response length: %d bytes", resp_len
            )
            return
        resp = bytearray()
        async with self._lock:
            # Re-read under the lock, as in `_send_prepared_command`: a
            # disconnect between chunks clears `_ble_client`, and this
            # callback runs inside bleak's notification dispatch, where an
            # `AttributeError` surfaces as an unhandled-task traceback
            # rather than reaching any caller. A `return` matches the
            # truncated-response handling below; `_on_disconnected`
            # separately fails the commands in flight.
            client = self._ble_client
            if client is None:
                return
            while len(resp) < resp_len:
                try:
                    chunk = await client.read_gatt_char(CHAR_RPC_DATA)
                except BleakError:
                    # The connection dropped mid-reply. Abandoning it matches
                    # the truncated-response handling below; the caller's
                    # command is failed by `_on_disconnected`.
                    _LOGGER.warning(
                        "Abandoning RPC response after a disconnect: "
                        "got %d of %d bytes",
                        len(resp),
                        resp_len,
                    )
                    return
                if not chunk:
                    # Nothing left to read, but the announced length says
                    # otherwise. Without this the loop spins on an empty read
                    # forever -- and never awaits anything that suspends, so
                    # no other task on the loop runs again.
                    _LOGGER.warning(
                        "Abandoning truncated RPC response: got %d of %d bytes",
                        len(resp),
                        resp_len,
                    )
                    return
                resp += chunk

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
