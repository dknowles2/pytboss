"""Client library for interacting with PitBoss grills over Bluetooth LE."""

import asyncio
import json
from datetime import timedelta
from math import floor
from typing import Callable, TypedDict

from .ble import BleConnection
from .config import Config
from .fs import FileSystem

StateCallback = Callable[["StateDict"], None]
"""A callback function that receives updated grill state."""

VDataCallback = Callable[[dict], None]
"""A callback function that receives updated VData."""


class PitBoss:
    """API for interacting with PitBoss grills over Bluetooth LE."""

    fs: FileSystem
    """Filesystem operations."""

    config: Config
    """Configuration operations."""

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
        self._state: "StateDict" = {}

    async def start(self):
        """Sets up the API for use.

        Required to be called before the API can be used.
        """
        # TODO: Add support for stop()
        await self._conn.connect()
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
        # TODO: This return type should match the argument to StateCallback.
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


class TemperaturesDict(TypedDict, total=False):
    """All temperatures."""

    grill_target: int
    """Target temperature for the grill."""

    smoker_actual: int | None
    """Current temperature of the smoker."""

    grill_actual: int | None
    """Current temperature of the grill."""

    probe_1_target: int
    """Target temperature for meat probe 1."""

    probe_1_actual: int | None
    """Current temperature of meat probe 1 (if present)."""

    probe_2_actual: int | None
    """Current temperature of meat probe 2 (if present)."""

    probe_3_actual: int | None
    """Current temperature of meat probe 3 (if present)."""

    probe_4_actual: int | None
    """Current temperature of meat probe 4 (is present)."""

    is_fahrenheit: bool
    """Whether temperature readings are in Fahrenheit."""


class RecipeStepDict(TypedDict):
    """Information about the current recipe step."""

    step_number: int
    """The current recipe step number."""

    time_remaining: timedelta
    """The time remaining for this recipe step."""


class StateDict(TypedDict, total=False):
    """Overall state of the grill or smoker."""

    temperatures: TemperaturesDict
    """Temperature readings and targets."""

    is_on: bool
    """Whether the device is turned on."""

    error_1: bool
    """Error state 1."""

    error_2: bool
    """Error state 2."""

    error_3: bool
    """Error state 3."""

    error_l: bool
    """Error state L."""

    temp_high_error: bool
    """Whether the temperature is too high."""

    no_pellets: bool
    """Whether the pellet hopper is empty."""

    fan_is_on: bool
    """Whether the fan is currently on."""

    fan_error: bool
    """Whether there was an error with the fan."""

    igniter_is_on: bool
    """Whether the igniter is currently on."""

    igniter_error: bool
    """Whether there was an error with the igniter."""

    auger_is_on: bool
    """Whether the auger is currently on."""

    auger_error: bool
    """Whether there was an error with the auger."""

    light_is_on: bool
    """Whether the light is on."""

    prime_is_on: bool
    """Whether the prime mode is on."""

    recipe_step: RecipeStepDict
    """The current recipe step."""


def decode_state(data: str) -> TemperaturesDict | StateDict:
    """:meta private:"""
    arr = hex_to_array(data)
    assert arr.pop(0) == 254
    msg_type = arr.pop(0)
    handlers = {
        11: decode_status,
        12: decode_all_temps,
        13: decode_target_temps,
    }
    if msg_type not in handlers:
        return None
    return handlers[msg_type](arr)


def decode_status(arr: list[int]) -> StateDict:
    """:meta private:"""
    cond_grill_temp = {1: "grill_target", 2: "grill_actual"}[arr[0x15]]
    return {
        "temperatures": {
            "probe_1_target": decode_temp(arr[0x00], arr[0x01], arr[0x02]),
            "probe_1_actual": decode_temp(arr[0x03], arr[0x04], arr[0x05]),
            "probe_2_actual": decode_temp(arr[0x06], arr[0x07], arr[0x08]),
            "probe_3_actual": decode_temp(arr[0x09], arr[0x0A], arr[0x0B]),
            "probe_4_actual": decode_temp(arr[0x0C], arr[0x0D], arr[0x0E]),
            "smoker_actual": decode_temp(arr[0x0F], arr[0x10], arr[0x11]),
            cond_grill_temp: decode_temp(arr[0x12], arr[0x13], arr[0x14]),
            "is_fahrenheit": arr[0x25] == 1,
        },
        "is_on": arr[0x16] == 1,
        "error_1": arr[0x17] == 1,
        "error_2": arr[0x18] == 1,
        "error_3": arr[0x19] == 1,
        "error_l": arr[0x1F] == 1,
        "temp_high_error": arr[0x1A] == 1,
        "no_pellets": arr[0x1E] == 1,
        "fan_is_on": arr[0x20] == 1,
        "fan_error": arr[0x1B] == 1,
        "igniter_is_on": arr[0x21] == 1,
        "igniter_error": arr[0x1C] == 1,
        "auger_is_on": arr[0x22] == 1,
        "auger_error": arr[0x1D] == 1,
        "light_is_on": arr[0x23] == 1,
        "prime_is_on": arr[0x24] == 1,
        "recipe_step": {
            "step_number": arr[0x26],
            "time_remaining": timedelta(
                hours=arr[0x27],
                minutes=arr[0x28],
                seconds=arr[0x29],
            ),
        },
    }


def decode_all_temps(arr: list[int]) -> TemperaturesDict:
    """:meta private:"""
    return {
        "probe_1_target": decode_temp(arr[0x00], arr[0x01], arr[0x02]),
        "probe_1_actual": decode_temp(arr[0x03], arr[0x04], arr[0x05]),
        "probe_2_actual": decode_temp(arr[0x06], arr[0x07], arr[0x08]),
        "probe_3_actual": decode_temp(arr[0x09], arr[0x0A], arr[0x0B]),
        "probe_4_actual": decode_temp(arr[0x0C], arr[0x0D], arr[0x0E]),
        "smoker_actual": decode_temp(arr[0x0F], arr[0x10], arr[0x11]),
        "grill_target": decode_temp(arr[0x12], arr[0x13], arr[0x14]),
        "grill_actual": decode_temp(arr[0x15], arr[0x16], arr[0x17]),
        "is_fahrenheit": arr[0x18] == 1,
    }


def decode_target_temps(arr: list[int]) -> TemperaturesDict:
    """:meta private:"""
    return {
        "grill_target": decode_temp(arr[0x00], arr[0x01], arr[0x02]),
        "probe_1_target": decode_temp(arr[0x03], arr[0x04], arr[0x05]),
    }
