"""High-level client API for controlling PitBoss/Dansons grills."""

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from math import floor
from time import monotonic
from typing import Any

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


def _to_fahrenheit(temp: int, already_fahrenheit: bool) -> int:
    """Convert a grill-unit temperature for the virtual data store.

    The store is always Fahrenheit, whatever unit the grill is set to.
    """
    if already_fahrenheit:
        return temp
    return round(temp * 9 / 5 + 32)


def _from_fahrenheit(temp: float, want_fahrenheit: bool) -> int:
    """Convert a virtual data temperature into the grill's own unit."""
    if want_fahrenheit:
        return round(temp)
    return round((temp - 32) * 5 / 9)


async def _invoke(callback: Callable[[Any], Awaitable[None] | None], arg: Any) -> None:
    """Call a subscriber, awaiting the result when there is one to await.

    Decided from the returned value rather than the callback:
    `inspect.iscoroutinefunction` cannot see through `functools.partial` or
    an object with an `async __call__`, and calling those without awaiting
    silently discards their coroutine.
    """
    result = callback(arg)
    if inspect.isawaitable(result):
        await result


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
        self._lock = asyncio.Lock()  # protects state and the subscriber lists.
        self._callback_lock = asyncio.Lock()  # serializes subscriber dispatch.
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
            callbacks = list(self._state_callbacks)
        # Dispatched outside `_lock`: a subscriber that calls back into the
        # API (`get_state()` takes the same lock) would otherwise deadlock.
        # The dedicated lock keeps one update's callbacks from interleaving
        # with the next one's, which holding `_lock` used to guarantee.
        async with self._callback_lock:
            # TODO: Run callbacks concurrently
            # TODO: Send copies of state so subscribers can't modify it
            for callback in callbacks:
                await _invoke(callback, self._state)

    async def _on_vdata_received(self, payload: str):
        vdata = json.loads(payload)
        _LOGGER.debug("VData received: %s", vdata)
        async with self._lock:
            callbacks = list(self._vdata_callbacks)
        async with self._callback_lock:
            # TODO: Run callbacks concurrently
            # TODO: Send copies of state so subscribers can't modify it
            for callback in callbacks:
                await _invoke(callback, vdata)

    async def _authenticate(self, params: dict) -> dict:
        """Return `params` with the encoded password added.

        A copy: the caller's dict is left alone. `PB.SetVirtualData` hands its
        whole params object to the firmware, so mutating in place put the
        encoded password into the caller's payload as well as on the wire.
        """
        if not self._password:
            return params
        psw = encode(self._password, key=timed_key(await self.get_uptime())).hex()
        return {**params, "psw": psw}

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

    def accepted_setpoints(self, fahrenheit: bool = True) -> list[int]:
        """Grill setpoints the control board honours.

        The board ignores anything that is not on this list.

        :param fahrenheit: Whether to return the list in Fahrenheit.
        """
        increments = self.spec.temp_increments or []
        if fahrenheit:
            return list(increments)
        if celsius := self.spec.celsius_temp_increments:
            return list(celsius)
        # Match the boards that convert inside their own parsing routine:
        # they floor, so 190F is 87C to the grill rather than 88.
        return [floor((v - 32) / 1.8) for v in increments]

    async def set_grill_temperature(self, temp: int) -> dict:
        """Sets the target grill temperature.

        Snapped to the nearest value the control board accepts, expressed in
        the unit the grill is currently working in.

        :param temp: Target grill temperature, in the grill's own unit.
        """
        fahrenheit = self._is_fahrenheit()
        if accepted := self.accepted_setpoints(fahrenheit):
            temp = min(accepted, key=lambda value: abs(value - temp))
        # The unit flag travels with every temperature command: on eleven
        # boards the MCU always speaks Fahrenheit, and the vendor's command
        # routine converts a Celsius argument only when its second parameter
        # is `false`. Sent alone, a Celsius setpoint is read as Fahrenheit
        # (100C becomes 100F). Boards whose routine takes one parameter
        # ignore the extra argument, as do fixed-hex commands.
        return await self._send_command("set-temperature", temp, fahrenheit)

    async def set_probe_temperature(self, temp: int) -> dict:
        """Sets the target temperature for probe 1.

        :param temp: Target probe temperature, in the grill's own unit.
        """
        return await self._send_command(
            "set-probe-1-temperature", temp, self._is_fahrenheit()
        )

    async def set_probe_2_temperature(self, temp: int) -> dict:
        """Sets the target temperature for probe 2.

        :param temp: Target probe temperature, in the grill's own unit.
        :raise pytboss.exceptions.UnsupportedOperation: When probe 2's
            target temperature cannot be set.
        """
        cmd = "set-probe-2-temperature"
        if cmd not in self.spec.control_board.commands:
            raise UnsupportedOperation
        return await self._send_command(cmd, temp, self._is_fahrenheit())

    def probe_target_command(self, probe_number: int) -> str | None:
        """The board command that sets this probe's target, if it has one.

        Across the catalogue only `set-probe-1-temperature` (42 of 137 models)
        and `set-probe-2-temperature` (26) exist; no board declares anything
        for probes 3 or 4. Which route a probe takes is a fact about the
        board, so callers should not have to work it out.
        """
        command = f"set-probe-{probe_number}-temperature"
        if command in self.spec.control_board.commands:
            return command
        return None

    async def set_probe_target(self, probe_number: int, temp: int) -> None:
        """Sets the target temperature for a probe.

        Uses the board's command where one exists and the grill's virtual data
        store otherwise, which is what the vendor's own app does. The grill
        acts only on the control probe; a target stored in virtual data is a
        note to whoever is watching, not something the board enforces.

        :param probe_number: The probe to set a target for, counting from 1.
        :param temp: Target probe temperature, in the grill's own unit.
        :raise pytboss.exceptions.UnsupportedOperation: If the grill is off.
            The firmware rejects virtual data writes unless `moduleIsOn`, and
            clears the store when the grill is switched off.
        """
        if self.probe_target_command(probe_number) is not None:
            await self._send_command(
                f"set-probe-{probe_number}-temperature", temp, self._is_fahrenheit()
            )
            return
        if not self._state.get("moduleIsOn"):
            raise UnsupportedOperation(
                "Virtual data cannot be written while the grill is off"
            )
        # The firmware assigns the payload wholesale, so everything already
        # there has to be sent back or it is lost.
        data = await self.get_virtual_data()
        data[f"p{probe_number}T"] = _to_fahrenheit(temp, self._is_fahrenheit())
        await self.set_virtual_data(data)

    async def get_probe_targets(self) -> dict[int, int]:
        """Returns the target temperature set for each probe.

        Values are in the grill's own unit. Targets the board reports itself
        win over the virtual data store: `p1Target` is reported by every
        model and `p2Target` by 26 of them, and those are what the board is
        actually working to.

        Probes with no target set are absent rather than present as `None`.
        """
        fahrenheit = self._is_fahrenheit()
        targets: dict[int, int] = {}
        if self._state.get("moduleIsOn"):
            data = await self.get_virtual_data()
            for probe_number in range(1, (self.spec.meat_probes or 0) + 1):
                raw = data.get(f"p{probe_number}T")
                if isinstance(raw, (int, float)):
                    targets[probe_number] = _from_fahrenheit(raw, fahrenheit)
        for probe_number in range(1, (self.spec.meat_probes or 0) + 1):
            reported = self._state.get(f"p{probe_number}Target")
            if isinstance(reported, (int, float)):
                targets[probe_number] = int(reported)
        return targets

    def _is_fahrenheit(self) -> bool:
        """Whether the grill is currently working in Fahrenheit."""
        return self._state.get("isFahrenheit", True) is not False

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

    async def get_virtual_data(self) -> dict:
        """Retrieves virtual data previously set with `set_virtual_data()`.

        `psw` is removed: the firmware stores the params of the last write
        verbatim, so an authenticated write leaves the encoded password in the
        scratchpad and it would otherwise be handed back as if it were data.

        This is the only thing that removes it, rather than a belt-and-braces
        measure. Firmware 0.6.0 attempts the same strip and misses -- it
        clears `vData.pws` while `checkPassword` reads `params.psw` -- and the
        eight earlier mJS versions do not attempt it at all.

        :meta private:
        """
        data = await self._conn.send_command(
            "PB.GetVirtualData", await self._authenticate({})
        )
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k != "psw"}
        return {}

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
            # Deliberately the pre-request timestamp, though the grill
            # computed its uptime later, when the request reached it: every
            # extrapolation therefore runs ahead of the grill by about one
            # request latency, and ahead is the only direction the firmware
            # forgives. checkPassword accepts a key built from its current
            # 10-second bucket or the next one -- x or x + 1, never x - 1 --
            # which absorbs a client running ahead but rejects one running
            # behind. Stamping after the reply reads as more accurate and
            # is not: it flips the bias behind, where a slow link draws
            # spurious Unauthorized errors near bucket boundaries.
            self._last_uptime_check = now
            return self._last_uptime
        return self._last_uptime + (now - self._last_uptime_check)

    async def ping(self, timeout: float | None = None) -> dict:
        """Pings the device.

        :param timeout: Time (in seconds) after which to abandon the RPC.
        """
        return await self._conn.send_command("RPC.Ping", {}, timeout=timeout)
