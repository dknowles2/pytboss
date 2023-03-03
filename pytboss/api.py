"""Client library for interacting with PitBoss grills over Bluetooth LE."""

import asyncio
from base64 import b64decode
from math import floor

from bleak import BleakScanner

from .ble import BleConnection


class PitBoss:
    """API for interacting with PitBoss grills over Bluetooth LE."""

    def __init__(self, conn: BleConnection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()  # protects callbacks and state.
        self._state_callbacks = []
        self._vdata_callbacks = []
        self._state = {}

    @classmethod
    async def discover(cls, scanner: BleakScanner | None = None) -> list["PitBoss"]:
        if scanner is None:
            devices = await BleakScanner.discover()
        else:
            devices = scanner.discovered_devices
        discovered = []
        for device in devices:
            if await BleConnection.is_grill(device):
                discovered.append(device)
        return discovered

    async def start(self):
        await self._conn.start_subscriptions(
            self._on_state_received, self._on_vdata_received
        )

    async def subscribe_state(self, callback):
        # TODO: Return a handle for unsubscribe.
        async with self._lock:
            self._state_callbacks.append(callback)

    async def subscribe_vdata(self, callback):
        # TODO: Return a handle for unsubscribe.
        async with self._lock:
            self._vdata_callbacks.append(callback)

    async def _on_state_received(self, payload: bytearray):
        state = decode_state(payload)
        async with self._lock:
            self._state.update(state)
            # TODO: Run callbacks concurrently
            for callback in self._state_callbacks:
                await callback(self._state)

    async def _on_vdata_received(self, payload: dict):
        async with self._lock:
            # TODO: Run callbacks concurrently
            for callback in self._vdata_callbacks:
                await callback(payload)

    async def _send_command(self, method: str, params: dict) -> dict:
        return await self._conn.send_command(method, params)

    async def _send_command_without_answer(self, method: str, params: dict):
        return await self._conn.send_command_without_answer(method, params)

    async def _send_hex_command(self, cmd: str) -> dict:
        return await self._send_command("PB.SendMCUCommand", {"command": cmd})

    async def set_grill_temperature(self, temp: int) -> dict:
        return await self._send_hex_command(f"FE0501{encode_temp(temp)}FF")

    async def set_probe_temperature(self, temp: int) -> dict:
        return await self._send_hex_command(f"FE0502{encode_temp(temp)}FF")

    #
    # File operations
    #

    async def get_file_list(self) -> list[str]:
        return await self._send_command("FS.List", {})

    async def get_file_content(self, filename) -> str:
        length = 512
        offset = 0
        content = ""
        while True:
            resp = await self._send_command(
                "FS.Get", {"filename": filename, "offset": offset, "len": length}
            )
            content += str(b64decode(resp["data"]))
            offset += length
            if resp["left"] == 0:
                return content

    async def set_file_content(self, filename, data, append) -> dict:
        return await self._send_command(
            "FS.Put",
            {"filename": filename, "data": data, "append": append},
        )

    async def rename_file(self, src, dst) -> dict:
        return await self._send_command("FS.Rename", {"src": src, "dst": dst})

    async def copy_file(self, src, dst) -> dict:
        return await self._send_command("PBL.CopyFile", {"src": src, "dst": dst})

    async def delete_file(self, filename) -> dict:
        return await self._send_command("FS.Remove", {"filename": filename})

    #
    # System settings
    #

    async def get_command_list(self) -> list:
        return await self._send_command("RPC.List", {})

    async def get_loader_version(self):
        return await self._send_command("PBL.GetLoaderVersion", {})

    async def get_firmware_version(self):
        return await self._send_command("PB.GetFirmwareVersion", {})

    async def load_firmware(self, base_url, filename):
        return await self._send_command(
            "PBL.LoadFirmware",
            {"baseurl": base_url, "filename": filename},
        )

    async def load_firmware_status(self):
        return await self._send_command("PBL.LoadFirmwareStatus", {})

    async def verify_firmware_download(self, offset, r=True):
        params = {"filename": "temp.js", "offset": offset, "len": 8 if r else 12}
        return await self._send_command("FS.Get", params)

    async def perform_ota_update(self, url, commit_timeout=300):
        return await self._send_command(
            "OTA.Update", {"url": url, "commit_timeout": commit_timeout}
        )

    async def set_wifi_credentials(self, ssid, password):
        return await self._send_command("Config.Set", _wifi_params(ssid, password))

    async def set_wifi_ssid(self, ssid):
        return await self._send_command("Config.Set", _wifi_params(ssid=ssid))

    async def set_wifi_password(self, password):
        return await self._send_command("Config.Set", _wifi_params(password=password))

    async def reboot_system(self):
        return await self._send_command_without_answer("Sys.Reboot", {})

    async def get_config(self, key, level):
        return await self._send_command("Config.Get", {"key": key, "level": level})

    async def save_config(self, reboot=True):
        fn = self._send_command_without_answer if reboot else self._send_command
        return await fn("Config.Save", {"reboot": reboot})

    async def set_mcu_update_timer(self, frequency=2):
        return await self._send_command(
            "PB.SetMCU_UpdateFrequency", {"frequency": frequency}
        )

    async def set_wifi_update_frequency(self, fast=5, slow=60):
        return await self._send_command(
            "PB.SetWifiUpdateFrequency", {"slow": slow, "fast": fast}
        )

    async def get_ip_address(self):
        resp = await self._send_command("Sys.GetInfo", {})
        return resp.get("wifi", {})

    async def get_state(self) -> tuple[dict, dict]:
        state = await self._send_command("PB.GetState", {})
        status = decode_status(hex_to_array(state["sc_11"]))
        temps = decode_all_temps(hex_to_array(state["sc_12"]))
        return status, temps

    async def ping(self) -> dict:
        return await self._send_command("RPC.Ping", {})

    async def update_mqtt_server(self, server="mqtt.dansonscorp.com", enable=False):
        return await self._send_command(
            "Config.Set", {"config": {"mqtt": {"enable": enable, "server": server}}}
        )

    async def set_virtual_data(self, data):
        return await self._send_command("PB.SetVirtualData", data)

    async def get_virtual_data(self):
        return await self._send_command("PB.GetVirtualData", {})


def _wifi_params(ssid: str | None = None, password: str | None = None) -> dict:
    sta = {"enable": True}
    if ssid:
        sta["ssid"] = ssid
    if password:
        sta["pass"] = password
    return {"config": {"wifi": {"sta": sta}}}


def hex_to_array(data: str) -> list[int]:
    return [int(data[i : i + 2], 16) for i in range(0, len(data), 2)]  # noqa: E203


def encode_temp(temp: int) -> str:
    hundreds = floor(temp / 100)
    tens = floor((temp % 100) / 10)
    ones = floor(temp % 10)
    return f"{hundreds:02x}{tens:02x}{ones:02x}"


def decode_temp(hundreds: int, tens: int, ones: int) -> int:
    return hundreds * 100 + tens * 10 + ones


def decode_state(data: str) -> dict:
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
    return {
        "grillSetTemp": decode_temp(arr[0x00], arr[0x01], arr[0x02]),
        "p_1_Set_Temp": decode_temp(arr[0x03], arr[0x04], arr[0x05]),
    }
