"""Client library for interacting with PitBoss grills over Bluetooth LE."""

import asyncio
import inspect
import json
import logging
import random
import time
from typing import Awaitable, Callable

from .config import Config
from .exceptions import GrillUnavailable, NotConnectedError, RPCError
from .fs import FileSystem
from .grills import Grill, StateDict, get_grill
from .transport import Transport

_LOGGER = logging.getLogger("pytboss")
_LOGGER.setLevel(logging.DEBUG)

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

    _grill_time_offset = None  # Time difference between client and grill

    def __init__(self, conn: Transport, grill_model: str, password: str = "") -> None:
        """Initializes the class.

        :param conn: Connection transport for the grill.
        :param grill_model: The grill model. This is necessary to determine all
            supported commands and cannot be determined automatically.
        :param password: The grill password.
        """
        self.fs = FileSystem(conn)
        self.config = Config(conn)
        self.spec: Grill = get_grill(grill_model)
        print("init")
        print(get_grill(grill_model))
        self._conn = conn
        self._conn.set_state_callback(self._on_state_received)
        self._conn.set_vdata_callback(self._on_vdata_received)
        self._password = password
        self._lock = asyncio.Lock()  # protects callbacks and state.
        self._state_callbacks: list[StateCallback] = []
        self._vdata_callbacks: list[VDataCallback] = []
        self._state = StateDict()
        print("password")
        print(password)

    def is_connected(self) -> bool:
        """Returns whether we are actively connected to the grill."""
        print("is_connected")
        print(self._conn.is_connected())
        return self._conn.is_connected()

    async def start(self) -> None:
        """Sets up the API for use.

        Required to be called before the API can be used.
        """
        await self._conn.connect()
        print("start connect await done")

    async def stop(self) -> None:
        """Stops any background polling."""
        print("STOP")
        await self._conn.disconnect()

    async def subscribe_state(self, callback: StateCallback):
        """Registers a callback to receive grill state updates.

        :param callback: Callback function that will receive updated grill state.
        """
        # TODO: Return a handle for unsubscribe.
        print("SUBSCRIBESTATE")
        async with self._lock:
            self._state_callbacks.append(callback)

    async def subscribe_vdata(self, callback: VDataCallback):
        """Registers a callback to receive VData updates.

        :param callback: Callback function that will receive updated VData.
        """
        print("SUBSCRIBEVDATA")
        # TODO: Return a handle for unsubscribe.
        async with self._lock:
            self._vdata_callbacks.append(callback)

    async def _on_state_received(
        self, status_payload: str | None, temperatures_payload: str | None
    ) -> None:
        _LOGGER.debug(
            "State received: status=%s, temperatures=%s",
            status_payload,
            temperatures_payload,
        )
        print(
            "State received: status=%s, temperatures=%s",
            status_payload,
            temperatures_payload,
        )
        state = StateDict()
        if status_payload:
            if new_state := self.spec.control_board.parse_status(status_payload):
                state.update(new_state)
        if temperatures_payload:
            if new_state := self.spec.control_board.parse_temperatures(
                temperatures_payload
            ):
                state.update(new_state)

        if not state:
            # Unknown or invalid payload; ignore.
            _LOGGER.debug("Could not parse state payload")
            print("Could not parse state payload")
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
        printf("VData received: %s", vdata)
        async with self._lock:
            # TODO: Run callbacks concurrently
            # TODO: Send copies of state so subscribers can't modify it
            for callback in self._vdata_callbacks:
                if inspect.iscoroutinefunction(callback):
                    await callback(vdata)
                else:
                    callback(vdata)

    async def _get_codec_time(self) -> int:
        """Port of getCodecTime() from JavaScript using actual grill time."""
        try:
            response = await self.get_time()
            uptime = response.get("time", 0)
            return int(max(uptime - 5, 0) / 10)
        except (RPCError, GrillUnavailable, NotConnectedError) as e:
            # Fallback to local time if we can't get grill time
            _LOGGER.debug("Failed to get grill time, using local time: %s", e)
            return int(max(time.time() - 5, 0) / 10)

    def _get_codec_key(self, key_bytes: list[int], time_val: int) -> list[int]:
        """Port of getCodecKey() from JavaScript."""
        key = key_bytes.copy()  # Make a copy to avoid modifying the original
        x = []
        l = time_val
        
        while len(key) > 1:
            p = l % len(key)
            v = key[p]
            key.pop(p)
            x.append((v ^ l) & 0xff)
            l = (l * v + v) & 0xff
            
        x.append(key[0])
        return x

    def _codec(self, data: bytes, key: list[int], padding_len: int) -> bytes:
        """Port of codec() from JavaScript."""
        result = bytearray()
        
        # Apply padding if needed
        if padding_len > 0:
            data = b'\xff' + data
            for _ in range(padding_len):
                rnd_val = random.randint(0, 254)  # Avoid 0xff
                data = bytes([rnd_val]) + data
        
        # Make a copy of the key to modify during processing
        key_copy = key.copy()
        
        # Process each byte
        for i in range(len(data)):
            k = key_copy[i % len(key_copy)]
            m = (data[i] ^ k) & 0xff
            result.append(m)
            
            k2 = (i + 1) % len(key_copy)
            if padding_len > 0:
                key_copy[k2] = ((key_copy[k2] ^ m) + i) & 0xff
            else:
                key_copy[k2] = ((key_copy[k2] ^ data[i]) + i) & 0xff
        
        # Remove padding marker if decoding
        if padding_len < 1:
            try:
                ff_index = result.index(0xff)
                result = result[ff_index + 1:]
            except ValueError:
                pass
        
        return bytes(result)

    async def _authenticate(self, params: dict) -> dict:
        if not self._password:
            return params
            
        # Get codec time (with fallback to local time if needed)
        base_time = await self._get_codec_time()
        
        # Try multiple time values around the base time
        # This appears to be more reliable based on logs
        time_values = [base_time + 1, base_time, base_time - 1]
              
        # Get the raw password bytes
        password_bytes = self._password.encode("utf-8")
        
        results = []
        # Try authentication with each time value
        for x in time_values:
            try:
                # Get the key for encryption
                key = self._get_codec_key([0x8f, 0x80, 0x19, 0xcf, 0x77, 0x6c, 0xfe, 0xb7], x)
                
                # Encrypt the password using the exact same approach as the grill
                encrypted = self._codec(password_bytes, key, 4)
                
                # Convert to hex string
                hex_result = encrypted.hex()
                results.append((x, hex_result))
            except (ValueError, IndexError, TypeError, OverflowError) as e:
                _LOGGER.debug("Error encrypting with time value %s: %s", x, e)
        
        # Use the first result (highest priority) from our ordered list
        if results:
            x, hex_result = results[0]
            params["psw"] = hex_result
            _LOGGER.debug("Using time value %s for authentication", x)
            return params
        
        # This should never happen since we're trying multiple time values
        raise RPCError("Failed to generate authentication token")

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
        await self._conn.send_command(
            "PB.SetDevicePassword",
            await self._authenticate({"newPassword": new_password.encode("utf-8").hex()}),
        )

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
        """Retrieves the current grill state."""
        resp = await self._conn.send_command("PB.GetState", await self._authenticate({}))
        print(resp)
        status = self.spec.control_board.parse_status(resp["sc_11"]) or {}
        status.update(self.spec.control_board.parse_temperatures(resp["sc_12"]) or {})
        return status

    async def get_firmware_version(self) -> dict:
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
            "PB.SetWifiUpdateFrequency",
            await self._authenticate({"slow": slow, "fast": fast}),
        )

    async def set_virtual_data(self, data: dict):
        """:meta private:"""
        return await self._conn.send_command(
            "PB.SetVirtualData", await self._authenticate(data)
        )

    async def get_virtual_data(self):
        """:meta private:"""
        return await self._conn.send_command(
            "PB.GetVirtualData", await self._authenticate({})
        )

    async def get_time(self):
        """:meta private:"""
        print("GETTING TIME")
        print(await self._conn.send_command("PB.GetTime", {}))
        return await self._conn.send_command("PB.GetTime", {})

    async def ping(self, timeout: float | None = None) -> dict:
        """Pings the device.

        :param timeout: Time (in seconds) after which to abandon the RPC.
        """
        print("PING")
        print(await self._conn.send_command("RPC.Ping", {}))
        return await self._conn.send_command("RPC.Ping", {}, timeout=timeout)

