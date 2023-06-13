"""Client library for interacting with PitBoss grills over Bluetooth LE."""

import asyncio
import inspect
import json
import logging
from typing import Awaitable, Callable, TypedDict

from . import grills
from .ble import BleConnection
from .config import Config
from .fs import FileSystem

_LOGGER = logging.getLogger("pytboss")


class StateDict(TypedDict, total=False):
    """State of the grill."""

    p1Target: int
    """Target temperature for meat probe 1."""

    p1Temp: int | None
    """Current temperature of meat probe 1 (if present)."""

    p2Temp: int | None
    """Current temperature of meat probe 2 (if present)."""

    p3Temp: int
    """Current temperature of meat probe 3 (if present)."""

    p4Temp: int
    """Current temperature of meat probe 4 (if present)."""

    smokerActTemp: int
    """Current temperature of the smoker."""

    grillSetTemp: int
    """Target temperature for the grill."""

    grillTemp: int
    """Current temperature of the grill."""

    moduleIsOn: bool
    """Whether the control module is powered on."""

    err1: bool
    """Error state 1."""

    err2: bool
    """Error state 2."""

    err3: bool
    """Error state 3."""

    highTempErr: bool
    """Whether the temperature is too high."""

    fanErr: bool
    """WHether there was an error with the fan."""

    hotErr: bool
    """Whether there was an error with the igniter."""

    motorErr: bool
    """Whether there was an error with the auger."""

    noPellets: bool
    """Whether the pellet hopper is empty."""

    erL: bool
    """Undocumented."""

    fanState: bool
    """Whether the fan is currently on."""

    hotState: bool
    """Whether the igniter is currently on."""

    motorState: bool
    """Whether the auger is currently on."""

    lightState: bool
    """Whether the light is currently on."""

    primeState: bool
    """Whether the prime mode is on."""

    isFahrenheit: bool
    """Whether the temperature readings are in Fahrenheit."""

    recipeStep: bool
    """The current recipe step number."""

    recipeTime: int
    """The time remaining for this recipe step (in seconds)."""


StateCallback = Callable[[StateDict], Awaitable[None] | None]
"""A callback function that receives updated grill state."""

VDataCallback = Callable[[dict], Awaitable[None] | None]
"""A callback function that receives updated VData."""


class PitBoss:
    """API for interacting with PitBoss grills over Bluetooth LE."""

    fs: FileSystem
    """Filesystem operations."""

    config: Config
    """Configuration operations."""

    def __init__(self, conn: BleConnection, grill_model: str) -> None:
        """Initializes the class.

        :param conn: BLE transport for the grill.
        :type conn: pytboss.ble.BleConnection
        :param grill_model: The grill model. This is necessary to determine all
            supported commands and cannot be determined automatically.
        :type grill_model: str
        """
        self.fs = FileSystem(conn)
        self.config = Config(conn)
        self._spec: grills.Grill = grills.get_grill(grill_model)
        self._conn = conn
        self._lock = asyncio.Lock()  # protects callbacks and state.
        self._state_callbacks = []
        self._vdata_callbacks = []
        self._state = StateDict()

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
        _LOGGER.debug("Debug log received: %s", data)
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

    async def _on_state_received(self, payload: str):
        _LOGGER.debug("State received: %s", payload)
        state = None
        match payload[:4]:
            case "FE0B":
                state = self._spec.control_board.parse_status(payload)
            case "FE0C":
                state = self._spec.control_board.parse_temperatures(payload)

        if not state:
            # Unknown or invalid payload; ignore.
            return

        async with self._lock:
            self._state.update(state.to_dict())
            # TODO: Run callbacks concurrently
            # TODO: Send copies of state so subscribers can't modify it
            for callback in self._state_callbacks:
                if inspect.iscoroutinefunction(callback):
                    await callback(self._state)
                else:
                    callback(self._state)

    async def _on_vdata_received(self, payload: bytearray):
        vdata = json.loads(payload)
        _LOGGER.debug("VData received: %s", vdata)
        async with self._lock:
            # TODO: Run callbacks concurrently
            # TODO: Send copies of state so subscribers can't modify it
            for callback in self._vdata_callbacks:
                if inspect.iscoroutinefunction(callback):
                    await callback(vdata)
                else:
                    callback(vdata)

    async def _send_hex_command(self, cmd: str) -> dict:
        return await self._conn.send_command("PB.SendMCUCommand", {"command": cmd})

    async def _send_command(self, slug: str, *args) -> dict:
        cmd = self._spec.control_board.commands[slug]
        return await self._send_hex_command(cmd(*args))

    async def set_grill_temperature(self, temp: int) -> dict:
        """Sets the target grill temperature.

        :param temp: Target grill temperature.
        :type temp: int
        :rtype: dict
        """
        # TODO: Clamp to a value from self._spec.temp_increments.
        if self._spec.max_temp:
            temp = min(temp, self._spec.max_temp)
        if self._spec.min_temp:
            temp = max(temp, self._spec.min_temp)
        return await self._send_command("set-temperature", temp)

    async def set_probe_temperature(self, temp: int) -> dict:
        """Sets the target temperature for probe 1.

        :param temp: Target probe temperature.
        :type temp: int
        :rtype: dict
        """
        return await self._send_command("set-probe-1-temperature", temp)

    async def turn_light_on(self) -> dict:
        """Turns the light on if the grill has a light."""
        if not self._spec.has_lights:
            return {}
        return await self._send_command("turn-light-on")

    async def turn_light_off(self) -> dict:
        """Turns the light off if the grill has a light."""
        if not self._spec.has_lights:
            return {}
        return await self._send_command("turn-light-off")

    async def turn_grill_off(self) -> dict:
        """Turns the grill off."""
        return await self._send_command("turn-off")

    async def turn_primer_motor_on(self) -> dict:
        """Turns the primer motor on."""
        return await self._send_command("turn-primer-motor-on")

    async def turn_primer_motor_off(self) -> dict:
        """Turns the primer motor off."""
        return await self._send_command("turn-primer-motor-off")

    async def get_state(self) -> StateDict:
        """Retrieves the current grill state."""
        resp = await self._conn.send_command("PB.GetState", {})
        status = self._spec.control_board.parse_status(resp["sc_11"])
        status.update(self._spec.control_board.parse_temperatures(resp["sc_12"]))
        return status

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
