"""Bluetooth LE connection support for PitBoss grills."""

import asyncio
import json

from bleak import BleakClient, BleakGATTCharacteristic, BLEDevice

# fmt: off
SERVICE_SUBSCRIBE  = '5f6d4f53-5f44-4247-5f53-56435f49445f'  # noqa: E221
SERVICE_RPC        = '5f6d4f53-5f52-5043-5f53-56435f49445f'  # noqa: E221
CHAR_SUBSCRIBE_RW  = "306d4f53-5f44-4247-5f6c-6f675f5f5f30"  # noqa: E221
CHAR_RPC_WRITE_CMD = "5f6d4f53-5f52-5043-5f64-6174615f5f5f"
CHAR_RPC_WRITE_LEN = "5f6d4f53-5f52-5043-5f74-785f63746c5f"
CHAR_RPC_SUBSCRIBE = "5f6d4f53-5f52-5043-5f72-785f63746c5f"
# fmt: on
SERVICE_TO_CHARS = {
    SERVICE_SUBSCRIBE: (CHAR_SUBSCRIBE_RW,),
    SERVICE_RPC: (CHAR_RPC_WRITE_CMD, CHAR_RPC_WRITE_LEN, CHAR_RPC_SUBSCRIBE),
}


class BleConnection:
    """Bluetooth LE protocol transport for PitBoss grills."""

    def __init__(
        self, ble_client: BleakClient, loop: asyncio.AbstractEventLoop | None = None
    ) -> None:
        self._ble_client = ble_client
        if loop is None:
            loop = asyncio.get_running_loop()
        self._loop = loop

        self._lock = asyncio.Lock()  # Protects items below.
        self._last_command_id = 0
        self._rpc_futures = {}
        self._state_callback = None
        self._vdata_callback = None

    async def start_subscriptions(self, state_callback, vdata_callback):
        self._state_callback = state_callback
        self._vdata_callback = vdata_callback
        await self._ble_client.start_notify(
            CHAR_SUBSCRIBE_RW, self._on_std_data_received
        )
        await self._ble_client.start_notify(
            CHAR_RPC_SUBSCRIBE, self._on_rpc_data_received
        )

    @classmethod
    async def is_grill(cls, device: BLEDevice) -> bool:
        """Determines if the given BLE device is a PitBoss grill."""
        if not device.name.startswith("PB"):
            return False
        async with BleakClient(device) as client:
            services = client.services.services.values()
            if not all(s.uuid in SERVICE_TO_CHARS for s in services):
                return False
            for svc in services:
                svc_chars = [c.uuid for c in svc.characteristics]
                if not all(c in svc_chars for c in SERVICE_TO_CHARS[svc.uuid]):
                    return False
        return True

    async def _next_command_id(self) -> int:
        async with self._lock:
            self._last_command_id = self._last_command_id + 1 & 2047
            return self._last_command_id

    async def send_command(self, method: str, params: dict, timeout: int = 60) -> dict:
        command_id = await self._next_command_id()
        cmd = json.dumps({"id": command_id, "method": method, "params": params})
        future = self._loop.create_future()
        async with self._lock:
            self._rpc_futures[command_id] = future
        await asyncio.wait_for(self._send_prepared_command(cmd), timeout=timeout)
        return await future

    async def send_command_without_answer(self, method: str, params: dict):
        command_id = await self._next_command_id()
        cmd = json.dumps({"id": command_id, "method": method, "params": params})
        await self._send_prepared_command(cmd)

    async def _send_prepared_command(self, cmd: str):
        payload = bytearray([0, 0, 0, 0])
        n = len(cmd)
        for i in range(0, 4):
            payload[3 - i] = 255 & n
            n >>= 8
        await self._ble_client.write_gatt_char(CHAR_RPC_WRITE_LEN, payload)
        for i in range(0, len(cmd), 20):
            chunk = bytearray(cmd[i : i + 20].encode("utf-8"))  # noqa: E203
            await self._ble_client.write_gatt_char(CHAR_RPC_WRITE_CMD, chunk)

    async def _on_std_data_received(
        self, unused_char: BleakGATTCharacteristic, data: bytearray
    ):
        parts = data.decode("utf-8").split()
        if len(parts) != 3:
            # Unknown payload; ignore.
            return

        head, payload, tail = parts
        checksum = int(tail[1 : len(tail) - 1])  # noqa: E203
        if len(payload) != checksum:
            # Bad payload; ignore.
            return
        if head == "<==PB:":
            if self._state_callback:
                await self._state_callback(payload)
        elif head == "<==PBD:":
            if self._vdata_callback:
                await self._vdata_callback(json.loads(payload))

    async def _on_rpc_data_received(
        self, char: BleakGATTCharacteristic, data: bytearray
    ):
        resp_len = data[0] << 24 | data[1] << 16 | data[2] << 8 | data[3]
        resp = bytearray()
        while len(resp) < resp_len:
            resp += await self._ble_client.read_gatt_char(CHAR_RPC_WRITE_CMD)

        payload = json.loads(resp.decode("utf-8"))

        async with self._lock:
            fut = self._rpc_futures.pop(payload["id"], None)

        if fut and not fut.cancelled():
            fut.set_result(payload["result"])
