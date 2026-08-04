"""High-level client API for controlling PitBoss/Dansons grills."""

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from math import floor
from time import monotonic
from typing import Any

from .codec import WIFI_KEY, encode, timed_key
from .config import Config
from .exceptions import RPCError, UnsupportedOperation
from .fs import FileSystem
from .grills import Grill, StateDict, get_grill
from .ota import OTA
from .transport import METHOD_NOT_FOUND_CODE, RPCResult, Transport, as_dict
from .wifi import WiFi

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

    ota: OTA
    """Over-the-air firmware update operations."""

    wifi: WiFi
    """WiFi operations."""

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
        self.ota = OTA(conn)
        self.wifi = WiFi(conn)
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
        if not hasattr(self, "spec"):
            # The callback is registered in `__init__` but the spec only
            # exists once `start()` has resolved it -- and a transport can
            # already be connected and receiving before then: Home Assistant
            # connects through `reset_device` during discovery and calls
            # `start()` later. A frame in that window has nothing to parse
            # it; the next push arrives seconds after `start()` completes.
            _LOGGER.debug("Ignoring state received before start()")
            return
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
                try:
                    await _invoke(callback, self._state)
                except Exception:
                    # Isolated per subscriber: without this the first one to
                    # raise skips every subscriber registered after it for
                    # that update, and the exception escapes into the
                    # transport's dispatch -- on BLE, an unhandled task
                    # traceback in bleak's notification handler.
                    _LOGGER.exception("Error in state subscriber")

    async def _on_vdata_received(self, payload: str | dict):
        # Both, because the transports differ in what they can hand over.
        # `ble` reads virtual data off the debug log, where it is still the
        # text the firmware printed; `wss` receives it as a member of an
        # already-parsed status frame, so it arrives decoded.
        vdata = json.loads(payload) if isinstance(payload, str) else payload
        if isinstance(vdata, dict) and "psw" in vdata:
            # The same strip `get_virtual_data()` applies, for the same
            # reason: an authenticated write leaves the encoded password in
            # the firmware's scratchpad, and the firmware pushes that object
            # back verbatim -- on the BLE debug log and as `data` on the
            # websocket status frame. Without this, every subscriber is
            # handed the password, and anything that logs or persists vdata
            # records it. The encoding is not protection: the key is
            # `timed_key(uptime)` and this library implements both ends.
            vdata = {k: v for k, v in vdata.items() if k != "psw"}
        _LOGGER.debug("VData received: %s", vdata)
        async with self._lock:
            callbacks = list(self._vdata_callbacks)
        async with self._callback_lock:
            # TODO: Run callbacks concurrently
            # TODO: Send copies of state so subscribers can't modify it
            for callback in callbacks:
                try:
                    await _invoke(callback, vdata)
                except Exception:
                    # Isolated per subscriber, as in `_on_state_received`.
                    _LOGGER.exception("Error in vdata subscriber")

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

    async def _send_hex_command(self, cmd: str) -> RPCResult:
        return await self._conn.send_command(
            "PB.SendMCUCommand", await self._authenticate({"command": cmd})
        )

    async def _send_command(self, slug: str, *args) -> RPCResult:
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

    async def set_grill_temperature(self, temp: int) -> RPCResult:
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

    async def set_probe_temperature(self, temp: int) -> RPCResult:
        """Sets the target temperature for probe 1.

        :param temp: Target probe temperature, in the grill's own unit.
        :raise pytboss.exceptions.UnsupportedOperation: When probe 1's
            target temperature cannot be set. Only 42 of 137 models declare
            the command; on the rest this raised a bare `KeyError` while its
            sibling below raised the documented error.
        """
        cmd = "set-probe-1-temperature"
        if cmd not in self.spec.control_board.commands:
            raise UnsupportedOperation
        return await self._send_command(cmd, temp, self._is_fahrenheit())

    async def set_probe_2_temperature(self, temp: int) -> RPCResult:
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

    async def reboot(self) -> None:
        """Reboots the WiFi module.

        The standard remedy when the module wedges: it drops its connections
        and comes back, leaving the grill itself running. No response is
        expected, since the board reboots before it can answer.
        """
        await self._conn.send_command_without_answer("Sys.Reboot", {})

    async def request_fast_updates(self) -> None:
        """Asks the grill to push status to the cloud faster, for 5 minutes.

        Despite the RPC's name (`PB.WiFiAwakeWDT`) this does not keep the WiFi
        module awake. The firmware runs a one-second tick that counts `wsWDT`
        down and, each time its push timer expires, reschedules it to
        `wsFastInterval` while `wsWDT > 0` and `wsSlowInterval` otherwise --
        5 and 60 seconds by default, both settable via
        `PB.SetWiFiUpdateFrequency`. This call sets `wsWDT` to 300 and zeroes
        the push timer, so the grill pushes immediately and then every 5
        seconds until the five minutes lapse. Calling again restarts them.

        Two conditions decide whether it does anything at all:

        * **The cloud WebSocket only.** The push it accelerates is
          `WS.send(wsConn, ...)`, so a Bluetooth or local connection sees no
          difference.
        * **Only while the grill is on**, or was on at the previous tick --
          the firmware guards the push with `lastWasOn || moduleIsOn`. On a
          cold grill the timer still reschedules to the fast interval and
          nothing is sent.

        Useful for a client reading the grill through the Dansons relay while
        a user is watching; a no-op for anything else.

        Returns nothing: the firmware handler ends in `return null`, so there
        is no result to hand back.
        """
        await self._conn.send_command("PB.WiFiAwakeWDT", await self._authenticate({}))

    async def set_temperature_unit(self, fahrenheit: bool) -> RPCResult:
        """Switches the unit the grill itself works in.

        This is the grill's own setting -- the one shown on its panel and
        used by the values it reports -- not a display preference.

        :param fahrenheit: Whether the grill should work in Fahrenheit.
        """
        return await self._send_command(
            "set-fahrenheit" if fahrenheit else "set-celsius"
        )

    async def turn_light_on(self) -> RPCResult:
        """Turns the light on if the grill has a light."""
        if not self.spec.has_lights:
            return {}
        return await self._send_command("turn-light-on")

    async def turn_light_off(self) -> RPCResult:
        """Turns the light off if the grill has a light."""
        if not self.spec.has_lights:
            return {}
        return await self._send_command("turn-light-off")

    async def turn_grill_on(self) -> RPCResult:
        """Lights the grill.

        **This starts a fire in an appliance nobody may be standing next to.**
        The board accepts it whatever state the grill is in, so anything built
        on this should decide for itself whether starting is a good idea --
        the library only sends it.

        Sent as a raw MCU command because no board declares a slug for it. All
        137 models declare `turn-off`; **not one declares a `turn-on`**, which
        looks deliberate on the vendor's part rather than an oversight.

        What the catalogue actually contains:

        ==================  ==========  ===========
        slug                command     models
        ==================  ==========  ===========
        ``turn-off``        FE0102FF    137
        ``turn-light-on``   FE0201FF    132
        ``turn-light-off``  FE0200FF    121
        ``turn-light-off``  FE0202FF     11
        this                FE0101FF      0
        ==================  ==========  ===========

        The third byte is the subsystem -- 01 power, 02 light. The fourth is
        `01` wherever a command means "on"; "off" is spelled `02` or `00`
        depending on the board, so it is not a uniform flag. What holds is
        the narrower claim: `01` is the only value that ever means on, and
        `FE0101FF` is declared under no slug on any board, so it collides
        with nothing.

        Confirmed working on a PB1600PS1 and a PBV4PS2, which is better
        evidence than the byte pattern.
        """
        return await self._send_hex_command("FE0101FF")

    async def turn_grill_off(self) -> RPCResult:
        """Turns the grill off."""
        return await self._send_command("turn-off")

    async def turn_primer_motor_on(self) -> RPCResult:
        """Turns the primer motor on.

        :raise pytboss.exceptions.UnsupportedOperation: When the board has no
            primer motor command (26 of 137 models).
        """
        cmd = "turn-primer-motor-on"
        if cmd not in self.spec.control_board.commands:
            raise UnsupportedOperation
        return await self._send_command(cmd)

    async def turn_primer_motor_off(self) -> RPCResult:
        """Turns the primer motor off.

        :raise pytboss.exceptions.UnsupportedOperation: When the board has no
            primer motor command (26 of 137 models).
        """
        cmd = "turn-primer-motor-off"
        if cmd not in self.spec.control_board.commands:
            raise UnsupportedOperation
        return await self._send_command(cmd)

    async def get_state(self) -> StateDict:
        """Issues a live RPC to fetch and return the current grill state.

        Unlike `subscribe_state()`, this always queries the grill rather than
        returning the cached state. The reply does update that cache, so
        anything relying on it stays correct on a connection that only ever
        polls.
        """
        resp = as_dict(
            await self._conn.send_command("PB.GetState", await self._authenticate({}))
        )
        # Either frame can be absent or empty, so neither is indexed. The
        # firmware blanks both the moment it forwards a command to the MCU::
        #
        #     function sendMCUCommand(pCommand) {
        #       lastStatus.sc_11 = "";
        #       lastStatus.sc_12 = "";
        #
        # and refills them from the next reply, so a poll landing in that
        # window is answered with empty strings rather than an error.
        status = StateDict()
        if sc_11 := resp.get("sc_11"):
            status.update(self.spec.control_board.parse_status(sc_11) or {})
        if sc_12 := resp.get("sc_12"):
            status.update(self.spec.control_board.parse_temperatures(sc_12) or {})
        if status:
            async with self._lock:
                self._state.update(status)
        return status

    async def get_firmware_version(self) -> dict:
        """Returns the firmware version installed on the grill."""
        # `or {}`: the handler answers with an object in every vendor image,
        # so the empty dict stands in only for a broken exchange.
        return as_dict(await self._conn.send_command("PB.GetFirmwareVersion", {}))

    async def set_mcu_update_timer(self, frequency=2) -> RPCResult:
        """Sets how often (in seconds) the MCU sends status updates.

        :meta private:
        """
        return await self._conn.send_command(
            "PB.SetMCU_UpdateFrequency", {"frequency": frequency}
        )

    async def set_wifi_update_frequency(self, fast=5, slow=60) -> RPCResult:
        """Sets how often (in seconds) the device sends WiFi status updates.

        The method name is `WiFi`, not `Wifi`. RPC names are matched exactly,
        and every firmware image registers it with the capital F.

        :meta private:
        """
        return await self._conn.send_command(
            "PB.SetWiFiUpdateFrequency",
            await self._authenticate({"slow": slow, "fast": fast}),
        )

    async def set_virtual_data(self, data: dict) -> RPCResult:
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

        Nothing upstream removes it, so this is not a belt-and-braces
        measure: firmware 0.6.0 attempts the same strip and misses -- it
        clears `vData.pws` while `checkPassword` reads `params.psw` -- and
        the eight earlier mJS versions do not attempt it at all.
        `_on_vdata_received` applies the same strip on the push path.

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
            result = as_dict(await self._conn.send_command("PB.GetTime", {}))
            uptime = result.get("time")
            if not isinstance(uptime, (int, float)):
                # Not cached, because caching it is worse than not having it:
                # `timed_key` would be built from a wrong uptime for the whole
                # TTL, so one unreadable reply would cost a minute of rejected
                # commands on a password-protected grill.
                _LOGGER.debug("No uptime in the reply: %s", result)
                if self._last_uptime is None:
                    return 0.0
                return self._last_uptime + (now - self._last_uptime_check)
            self._last_uptime = float(uptime)
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

    async def ping(self, timeout: float | None = None) -> RPCResult:
        """Pings the device.

        :param timeout: Time (in seconds) after which to abandon the RPC.
        """
        return await self._conn.send_command("RPC.Ping", {}, timeout=timeout)

    async def list_rpcs(self) -> list[str]:
        """Returns the RPC methods this grill serves.

        What a grill answers is not uniform, so this is how a caller finds
        out rather than inferring it from a model or firmware version. A
        PB1600PS1 on 0.5.7 lists 56 methods; the same list is where
        `PBL.GetLoaderVersion` and the `Wifi.*` calls do or do not appear.

        Not universal itself: the ESP-IDF firmware line -- versioned `16.x`,
        on the PBC2, PBD, PBE, PBL2 and PBT boards -- names this one
        `RPC.ListEx` and does not serve `RPC.List` at all, so a caller
        covering both has to ask for each.
        """
        result = await self._conn.send_command("RPC.List", {})
        return result if isinstance(result, list) else []

    async def get_loader_version(self) -> str | None:
        """Returns the version of the loader application, if it has one.

        This is the `PitBoss Loader` that carries the grill's own
        application, and is versioned independently of the firmware
        `get_firmware_version()` reports: a grill on firmware 0.5.7 answers
        `0.2.2` here.

        `None` when the grill does not serve `PBL.GetLoaderVersion`.
        """
        try:
            result = await self._conn.send_command("PBL.GetLoaderVersion", {})
        except RPCError as ex:
            if ex.code == METHOD_NOT_FOUND_CODE:
                # The documented None. Without this, a grill without the
                # loader RPC raised instead of answering what the docstring
                # promises.
                return None
            raise
        if isinstance(result, dict):
            return result.get("loaderVersion")
        return None

    async def start_wifi_scan(self) -> dict:
        """Asks the loader to begin scanning for WiFi networks.

        Answers `{"scanning": bool, "results": list | None}` immediately
        rather than waiting. A scan already in flight is left alone and its
        state returned, so calling this twice does not restart anything.

        The loader serves this, not the grill application, and does not check
        the password. Prefer `scan_wifi_networks()` unless you want to drive
        the polling yourself -- `get_wifi_scan_status()` hands the results
        back only once.
        """
        return as_dict(await self._conn.send_command("PBL.StartWifiScan", {}))

    async def get_wifi_scan_status(self) -> dict:
        """Returns the state of the scan `start_wifi_scan()` began.

        **Reading the results consumes them.** The loader clears its stored
        results as it returns them, so the next call answers `None` again
        even though the scan completed. Whatever calls this owns the only
        copy.

        `{"scanning": True, "results": None}` while it runs, then
        `{"scanning": False, "results": [...]}` exactly once.

        Each entry carries `ssid`, `bssid`, `authMode`, `channel` and `rssi`
        -- `authMode`, because the loader goes through Mongoose's JS
        `Wifi.scan()`, where `PitBoss.wifi.scan()` calls the `Wifi.Scan` RPC and
        gets the same field named `auth`. Same grill, same networks, two
        spellings.
        """
        return as_dict(await self._conn.send_command("PBL.GetWifiScanStatus", {}))

    async def scan_wifi_networks(
        self, *, timeout: float = 15.0, poll_interval: float = 1.0
    ) -> list[dict]:
        """Runs the loader's scan to completion and returns what it found.

        The two-call dance in one place, so the single destructive read of
        `get_wifi_scan_status()` happens once, here, rather than in every
        caller that polls.

        Empty if the scan finds nothing, if the grill serves no loader, or if
        `timeout` elapses first -- none of which are distinguishable in the
        reply, so this does not pretend to tell them apart.

        :param timeout: Seconds to keep polling before giving up.
        :param poll_interval: Seconds between polls.
        """
        try:
            await self.start_wifi_scan()
        except RPCError as ex:
            if ex.code == METHOD_NOT_FOUND_CODE:
                # No loader: the documented empty, not an error. Only the
                # first call needs the guard -- once the start is served,
                # the status polls are too.
                return []
            raise
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            status = await self.get_wifi_scan_status()
            results = status.get("results")
            if isinstance(results, list):
                return results
            if not status.get("scanning"):
                # Not running and nothing held: either it never started or a
                # previous caller already consumed the results.
                return []
        return []

    async def rename_device(self, name: str) -> str:
        """Renames the grill, returning its new device id.

        Only the part after the first `-` is yours to set: the firmware keeps
        everything up to and including that separator and appends `name`, so
        a grill with the id `PBL-1234` renamed to `Patio` becomes
        `PBL-Patio`. The returned value is that whole id, not `name`.

        The firmware rejects an empty name, and one with a leading or
        trailing space, with an `Invalid parameters` error -- it does not
        trim. Anything else it accepts.

        Cosmetic: a grill is identified by its device id elsewhere, and this
        changes that id, so anything holding the old one has to be updated.

        :param name: The name to give the grill.
        :raise pytboss.exceptions.RPCError: If the firmware rejects the name.
        """
        result = as_dict(
            await self._conn.send_command(
                "PB.RenameDevice", await self._authenticate({"name": name})
            )
        )
        return result.get("newName", "")

    async def set_wifi_credentials(self, ssid: str, password: str) -> RPCResult:
        """Puts the grill on a WiFi network.

        The password travels obfuscated with the codec this library already
        uses for the grill password, under a key of its own. That is the only
        thing this offers over `Config.set_wifi_credentials()`, which reaches
        the same `wifi.sta` settings through Mongoose's own config service and
        sends the password as plain text in the RPC payload.

        Neither saves: the firmware writes the config without committing it,
        so call `Config.save_config()` if the change should survive a reboot.

        Take the usual care -- a wrong SSID or password puts the grill on a
        network it cannot reach, and recovering it means the panel.

        :param ssid: The network to join.
        :param password: The network's password.
        """
        return await self._conn.send_command(
            "PB.SetWifiCredentials",
            await self._authenticate(
                {
                    "ssid": ssid,
                    "pass": encode(password.encode("utf-8"), key=WIFI_KEY).hex(),
                }
            ),
        )

    async def debug_pstate(self) -> str:
        """Returns the pairing state the cloud last set on this grill.

        Opaque to the grill: it is written only by a `setPState` frame on the
        outbound cloud socket and never by the grill itself, which then echoes
        it back on every status push and here. So it reports what the vendor's
        service last said, and is empty on a grill that has never been paired
        -- or reached over Bluetooth or the local transport, where nothing
        writes it.

        Diagnostic only. Nothing in this library acts on it.
        """
        result = as_dict(
            await self._conn.send_command(
                "PB.DebugPState", await self._authenticate({})
            )
        )
        return result.get("pState", "")
