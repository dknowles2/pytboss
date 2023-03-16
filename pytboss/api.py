"""Client library for interacting with PitBoss grills over Bluetooth LE."""

import asyncio
import json
from math import floor
from typing import Callable

from .ble import BleConnection
from .config import Config
from .fs import FileSystem

StateCallback = Callable[[dict], None]
"""A callback function that receives updated grill state."""

VDataCallback = Callable[[dict], None]
"""A callback function that receives updated VData."""


class PitBoss:
    """API for interacting with PitBoss grills over Bluetooth LE."""

    def __init__(self, conn: BleConnection) -> None:
        """Initializes the class.

        :param conn: BLE transport for the grill.
        :type conn: pytboss.ble.BleConnection
        """
        self.fs = FileSystem(conn)
        self.config = Config(conn)
        self._conn = conn
        self._lock = asyncio.Lock()  # protects callbacks and state.
        self._state_callbacks = []
        self._vdata_callbacks = []
        self._state = {}

    async def start(self):
        """Sets up the API for use.

        Required to be called before the API can be used.
        """
        # TODO: Add support for stop()
        await self._conn.start()
        await self._conn.subscribe_debug_logs(self._on_debug_log_received)

    async def subscribe_state(self, callback: StateCallback):
        """Registers a callback to receive grill state updates.

        :param callback: Callback function that will receive updated grill state.
        :type callback: StateCallback
        """
        # TODO: Return a handle for unsubscribe.
        async with self._lock:
            self._state_callbacks.append(callback)

    async def subscribe_vdata(self, callback: VDataCallback):
        """Registers a callback to receive VData updates.

        :param callback: Callback function that will receive updated VData.
        :type callback: VDataCallback
        """
        # TODO: Return a handle for unsubscribe.
        async with self._lock:
            self._vdata_callbacks.append(callback)

    async def _on_debug_log_received(self, data: bytearray):
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
            await self._on_state_received(payload)
        elif head == "<==PBD:":
            await self._on_vdata_received(payload)

    async def _on_state_received(self, payload: bytearray):
        state = decode_state(payload)
        async with self._lock:
            self._state.update(state)
            # TODO: Run callbacks concurrently
            # TODO: Send copies of state so subscribers can't modify it
            for callback in self._state_callbacks:
                await callback(self._state)

    async def _on_vdata_received(self, payload: bytearray):
        vdata = json.loads(payload)
        async with self._lock:
            # TODO: Run callbacks concurrently
            # TODO: Send copies of state so subscribers can't modify it
            for callback in self._vdata_callbacks:
                await callback(vdata)

    async def _send_hex_command(self, cmd: str) -> dict:
        return await self._conn.send_command("PB.SendMCUCommand", {"command": cmd})

    async def set_grill_temperature(self, temp: int) -> dict:
        """Sets the target grill temperature.

        :param temp: Target grill temperature.
        :type temp: int
        :rtype: dict
        """
        return await self._send_hex_command(f"FE0501{encode_temp(temp)}FF")

    async def set_probe_temperature(self, temp: int) -> dict:
        """Sets the target temperature for probe 1.

        :param temp: Target probe temperature.
        :type temp: int
        :rtype: dict
        """
        return await self._send_hex_command(f"FE0502{encode_temp(temp)}FF")

    async def get_state(self) -> tuple[dict, dict]:
        """Retrieves the current grill state."""
        state = await self._conn.send_command("PB.GetState", {})
        status = decode_status(hex_to_array(state["sc_11"]))
        temps = decode_all_temps(hex_to_array(state["sc_12"]))
        return status, temps

    async def get_firmware_version(self) -> str:
        """Returns the firmware version installed on the grill."""
        return await self._conn.send_command("PB.GetFirmwareVersion", {})

    async def set_mcu_update_timer(self, frequency=2):
        """:meta private:"""
        return await self._conn.send_command(
            "PB.SetMCU_UpdateFrequency", {"frequency": frequency}
        )

    async def set_wifi_update_frequency(self, fast=5, slow=60):
        """:meta private:"""
        return await self._conn.send_command(
            "PB.SetWifiUpdateFrequency", {"slow": slow, "fast": fast}
        )

    async def set_virtual_data(self, data):
        """:meta private:"""
        return await self._conn.send_command("PB.SetVirtualData", data)

    async def get_virtual_data(self):
        """:meta private:"""
        return await self._conn.send_command("PB.GetVirtualData", {})


def hex_to_array(data: str) -> list[int]:
    """:meta private:"""
    return [int(data[i : i + 2], 16) for i in range(0, len(data), 2)]  # noqa: E203


def encode_temp(temp: int) -> str:
    """:meta private:"""
    hundreds = floor(temp / 100)
    tens = floor((temp % 100) / 10)
    ones = floor(temp % 10)
    return f"{hundreds:02x}{tens:02x}{ones:02x}"


def decode_temp(hundreds: int, tens: int, ones: int) -> int:
    """:meta private:"""
    return hundreds * 100 + tens * 10 + ones


def decode_state(data: str) -> dict:
    """:meta private:"""
    arr = hex_to_array(data)
    assert arr.pop(0) == 254
    msg_type = arr.pop(0)
    handlers = {
        11: decode_status,
        12: decode_all_temps,
        13: decode_set_temps,
    }
    if msg_type not in handlers:
        return None
    return handlers[msg_type](arr)


def decode_status(arr: list[int]) -> dict:
    """:meta private:"""
    cond_grill_temp = {1: "grillSetTemp", 2: "grillTemp"}[arr[0x15]]
    return {
        # fmt: off
        "p_1_Set_Temp":  decode_temp(arr[0x00], arr[0x01], arr[0x02]),
        "p_1_Temp":      decode_temp(arr[0x03], arr[0x04], arr[0x05]),
        "p_2_Temp":      decode_temp(arr[0x06], arr[0x07], arr[0x08]),
        "p_3_Temp":      decode_temp(arr[0x09], arr[0x0a], arr[0x0b]),
        "p_4_Temp":      decode_temp(arr[0x0c], arr[0x0d], arr[0x0e]),
        "smokerActTemp": decode_temp(arr[0x0f], arr[0x10], arr[0x11]),
        cond_grill_temp: decode_temp(arr[0x12], arr[0x13], arr[0x14]),
        "moduleIsOn":    arr[0x16] == 1,
        "err_1":         arr[0x17] == 1,
        "err_2":         arr[0x18] == 1,
        "err_3":         arr[0x19] == 1,
        "tempHighErr":   arr[0x1a] == 1,
        "fanErr":        arr[0x1b] == 1,
        "hotErr":        arr[0x1c] == 1,
        "motorErr":      arr[0x1d] == 1,
        "noPellets":     arr[0x1e] == 1,
        "erL":           arr[0x1f] == 1,
        "fanState":      arr[0x20] == 1,
        "hotState":      arr[0x21] == 1,
        "motorState":    arr[0x22] == 1,
        "lightState":    arr[0x23] == 1,
        "primeState":    arr[0x24] == 1,
        "isFahrenheit":   arr[0x25] == 1,
        "recipeStep":    arr[0x26],
        "time_H":        arr[0x27],
        "time_M":        arr[0x28],
        "time_S":        arr[0x29],
        # fmt: on
    }


def decode_all_temps(arr: list[int]) -> dict:
    """:meta private:"""
    return {
        # fmt: off
        "p_1_Set_Temp":  decode_temp(arr[0x00], arr[0x01], arr[0x02]),
        "p_1_Temp":      decode_temp(arr[0x03], arr[0x04], arr[0x05]),
        "p_2_Temp":      decode_temp(arr[0x06], arr[0x07], arr[0x08]),
        "p_3_Temp":      decode_temp(arr[0x09], arr[0x0a], arr[0x0b]),
        "p_4_Temp":      decode_temp(arr[0x0c], arr[0x0d], arr[0x0e]),
        "smokerActTemp": decode_temp(arr[0x0f], arr[0x10], arr[0x11]),
        "grillSetTemp":  decode_temp(arr[0x12], arr[0x13], arr[0x14]),
        "grillTemp":     decode_temp(arr[0x15], arr[0x16], arr[0x17]),
        "isFahrenheit":   arr[0x18] == 1,
        # fmt: on
    }


def decode_set_temps(arr: list[int]) -> dict:
    """:meta private:"""
    return {
        "grillSetTemp": decode_temp(arr[0x00], arr[0x01], arr[0x02]),
        "p_1_Set_Temp": decode_temp(arr[0x03], arr[0x04], arr[0x05]),
    }
