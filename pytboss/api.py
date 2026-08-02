"""High-level client API for controlling PitBoss/Dansons grills."""

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from time import monotonic

from .codec import encode, timed_key
from .config import Config
from .exceptions import UnsupportedOperation
from .fs import FileSystem
from .grills import Grill, StateDict, get_grill
from .transport import Transport

_UPTIME_TTL = 60.0
"""Seconds before the cached uptime is re-read rather than extrapolated.

Kept short because a grill that restarts resets its uptime, and no transport
routes its reconnect through `PitBoss` -- `ble` and `wss` both reconnect
internally -- so nothing invalidates the cache when that happens. Until the
re-read, `timed_key` would be built from an uptime minutes too high, and every
authenticated command on a password-protected grill would be rejected. This
bounds that window to a minute while still saving most of the round trips."""

_LOGGER = logging.getLogger("pytboss")

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

    def __init__(
        self,
        conn: Transport,
        grill_model: str,
        password: str = "",
        control_board: str | None = None,
    ) -> None:
        """Initializes the class.

        :param conn: Connection transport for the grill.
        :param grill_model: The grill model. This is necessary to determine all
            supported commands and cannot be determined automatically.
        :param password: The grill password.
        :param control_board: The control board the grill ships with, for the
            few models sold on two board generations. Callers that know it --
            it is the prefix a grill advertises over Bluetooth -- should pass
            it, since the boards do not always parse identically. Omitting it
            selects the board the vendor lists most recently, which is what
            happens without this argument.
        """
        self.fs = FileSystem(conn)
        self.config = Config(conn)
        self._grill_model = grill_model
        self._control_board = control_board
        self._conn = conn
        self._conn.set_state_callback(self._on_state_received)
        self._conn.set_vdata_callback(self._on_vdata_received)
        self._password = password.encode("utf-8")
        self._lock = asyncio.Lock()  # protects callbacks and state.
        self._state_callbacks: list[StateCallback] = []
        self._vdata_callbacks: list[VDataCallback] = []
        self._state = StateDict()
        self._last_uptime: float | None = None
        self._last_uptime_check: float = 0.0

    def is_connected(self) -> bool:
        """Returns whether we are actively connected to the grill."""
        return self._conn.is_connected()

    async def start(self) -> None:
        """Sets up the API for use.

        Required to be called before the API can be used.

        :raise pytboss.exceptions.InvalidGrill: If the grill model is unknown,
            or has no variant on the control board given to the constructor.
        """
        self.spec: Grill = await asyncio.to_thread(
            get_grill, self._grill_model, self._control_board
        )
        await self._conn.connect()

    async def stop(self) -> None:
        """Disconnects from the grill and stops any background connection tasks."""
        await self._conn.disconnect()

    async def subscribe_state(self, callback: StateCallback):
        """Registers a callback to receive grill state updates.

        The callback receives the same `StateDict` instance on every call,
        shared across all subscribers; it should be treated as read-only.

        :param callback: Callback function that will receive updated grill state.
        """
        # TODO: Return a handle for unsubscribe.
        async with self._lock:
            self._state_callbacks.append(callback)

    async def subscribe_vdata(self, callback: VDataCallback):
        """Registers a callback to receive VData updates.

        If multiple callbacks are subscribed, they all receive the same
        `dict` instance for a given update; it should be treated as
        read-only.

        :param callback: Callback function that will receive updated VData.
        """
        # TODO: Return a handle for unsubscribe.
        async with self._lock:
            self._vdata_callbacks.append(callback)

    async def _on_state_received(
        self, status_payload: str | None, temperatures_payload: str | None = None
    ) -> None:
        _LOGGER.debug(
            "State received: status=%s, temperatures=%s",
            status_payload,
            temperatures_payload,
        )
        state = StateDict()
        if status_payload and (
            new_state := self.spec.control_board.parse_status(status_payload)
        ):
            state.update(new_state)
        if temperatures_payload and (
            new_state := self.spec.control_board.parse_temperatures(
                temperatures_payload
            )
        ):
            state.update(new_state)

        if not state:
            # Unknown or invalid payload; ignore.
            _LOGGER.debug("Could not parse state payload")
            return

        async with self._lock:
            self._state.update(state)
            # TODO: Run callbacks concurrently
            # TODO: Send copies of state so subscribers can't modify it
            for callback in self._state_callbacks:
                if inspect.iscoroutinefunction(callback):
                    await callback(self._state)
                else:
                    callback(self._state)

    async def _on_vdata_received(self, payload: str):
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

    async def _authenticate(self, params: dict) -> dict:
        if self._password:
            params["psw"] = encode(
                self._password, key=timed_key(await self.get_uptime())
            ).hex()
        return params

    async def _send_hex_command(self, cmd: str) -> dict:
        return await self._conn.send_command(
            "PB.SendMCUCommand", await self._authenticate({"command": cmd})
        )

    async def _send_command(self, slug: str, *args) -> dict:
        cmd = self.spec.control_board.commands[slug]
        return await self._send_hex_command(cmd(*args))

    async def set_grill_password(self, new_password: str) -> None:
        """Sets the grill password.

        :param new_password: The new password to set.
        """
        new_password_bytes = new_password.encode("utf-8")
        await self._conn.send_command(
            "PB.SetDevicePassword",
            await self._authenticate({"newPassword": encode(new_password_bytes).hex()}),
        )
        self._password = new_password_bytes

    async def set_grill_temperature(self, temp: int) -> dict:
        """Sets the target grill temperature.

        :param temp: Target grill temperature.
        """
        # TODO: Clamp to a value from self.spec.temp_increments.
        if self.spec.max_temp:
            temp = min(temp, self.spec.max_temp)
        if self.spec.min_temp:
            temp = max(temp, self.spec.min_temp)
        return await self._send_command("set-temperature", temp)

    async def set_probe_temperature(self, temp: int) -> dict:
        """Sets the target temperature for probe 1.

        :param temp: Target probe temperature.
        """
        return await self._send_command("set-probe-1-temperature", temp)

    async def set_probe_2_temperature(self, temp: int) -> dict:
        """Sets the target temperature for probe 2.

        :param temp: Target probe temperature.
        :raise pytboss.exceptions.UnsupportedOperation: When probe 2's
            target temperature cannot be set.
        """
        cmd = "set-probe-2-temperature"
        if cmd not in self.spec.control_board.commands:
            raise UnsupportedOperation
        return await self._send_command(cmd, temp)

    async def set_temperature_unit(self, fahrenheit: bool) -> dict:
        """Switches the unit the grill itself works in.

        This is the grill's own setting -- the one shown on its panel and
        used by the values it reports -- not a display preference.

        :param fahrenheit: Whether the grill should work in Fahrenheit.
        """
        return await self._send_command(
            "set-fahrenheit" if fahrenheit else "set-celsius"
        )

    async def turn_light_on(self) -> dict:
        """Turns the light on if the grill has a light."""
        if not self.spec.has_lights:
            return {}
        return await self._send_command("turn-light-on")

    async def turn_light_off(self) -> dict:
        """Turns the light off if the grill has a light."""
        if not self.spec.has_lights:
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
        """Issues a live RPC to fetch and return the current grill state.

        Unlike `subscribe_state()`, this always queries the grill rather than
        returning the cached state. The reply does update that cache, so
        anything relying on it stays correct on a connection that only ever
        polls.
        """
        resp = await self._conn.send_command(
            "PB.GetState", await self._authenticate({})
        )
        status = self.spec.control_board.parse_status(resp["sc_11"]) or {}
        status.update(self.spec.control_board.parse_temperatures(resp["sc_12"]) or {})
        if status:
            async with self._lock:
                self._state.update(status)
        return status

    async def get_firmware_version(self) -> dict:
        """Returns the firmware version installed on the grill."""
        return await self._conn.send_command("PB.GetFirmwareVersion", {})

    async def set_mcu_update_timer(self, frequency=2):
        """Sets how often (in seconds) the MCU sends status updates.

        :meta private:
        """
        return await self._conn.send_command(
            "PB.SetMCU_UpdateFrequency", {"frequency": frequency}
        )

    async def set_wifi_update_frequency(self, fast=5, slow=60):
        """Sets how often (in seconds) the device sends WiFi status updates.

        :meta private:
        """
        return await self._conn.send_command(
            "PB.SetWifiUpdateFrequency",
            await self._authenticate({"slow": slow, "fast": fast}),
        )

    async def set_virtual_data(self, data: dict):
        """Sets arbitrary virtual data on the device.

        :meta private:
        """
        return await self._conn.send_command(
            "PB.SetVirtualData", await self._authenticate(data)
        )

    async def get_virtual_data(self):
        """Retrieves virtual data previously set with `set_virtual_data()`.

        :meta private:
        """
        return await self._conn.send_command(
            "PB.GetVirtualData", await self._authenticate({})
        )

    async def get_uptime(self) -> float:
        """Returns the device's uptime, in seconds.

        Read once and then extrapolated, since uptime advances with the
        wall clock, and re-read every `_UPTIME_TTL` seconds so a grill that
        restarted cannot be extrapolated from for long.

        :meta private:
        """
        now = monotonic()
        if self._last_uptime is None or now - self._last_uptime_check > _UPTIME_TTL:
            result = await self._conn.send_command("PB.GetTime", {})
            self._last_uptime = result.get("time", 0.0)
            self._last_uptime_check = now
            return self._last_uptime
        return self._last_uptime + (now - self._last_uptime_check)

    async def ping(self, timeout: float | None = None) -> dict:
        """Pings the device.

        :param timeout: Time (in seconds) after which to abandon the RPC.
        """
        return await self._conn.send_command("RPC.Ping", {}, timeout=timeout)
