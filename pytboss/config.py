"""Client library for Mongoose OS configuration RPCs."""

from typing import Any

from .transport import SendCommandFn, Transport


class Config:
    """Client library for Mongoose OS configuration RPCs.

    Also see: https://mongoose-os.com/docs/mongoose-os/api/rpc/rpc-service-config.md
    """

    def __init__(self, conn: Transport) -> None:
        """Initializes the class.

        :param conn: Transport for the device.
        """
        self._conn = conn

    async def get_info(self) -> dict:
        """Returns system information."""
        return await self._conn.send_command("Sys.GetInfo", {})

    async def get_config(self, key: str | None = None) -> dict:
        """Retrieves device configuration subtree.

        :param key: Optional path to config object. e.g. `wifi.sta.ssid`.
        """
        params = {}
        if key:
            params["key"] = key
        return await self._conn.send_command("Config.Get", params)

    async def save_config(self, reboot: bool = True):
        """Writes an existing device configuration on flash.

        :param reboot: Whether to reboot the device after the call.
        """
        fn: SendCommandFn = self._conn.send_command
        if reboot:
            fn = self._conn.send_command_without_answer
        await fn("Config.Save", {"reboot": reboot})

    async def set(self, **kwargs):
        """Sets device configuration parameters."""
        return await self._conn.send_command("Config.Set", {"config": kwargs})

    async def set_wifi_credentials(self, ssid: str, password: str) -> dict:
        """Sets the WiFi credentials on the device.

        :param ssid: The SSID to connect to.
        :param password: The password for the WiFi network.
        """
        return await self._conn.send_command("Config.Set", _wifi_params(ssid, password))

    async def set_wifi_ssid(self, ssid):
        """Sets the WiFi SSID.

        :param ssid: The SSID to connect to.
        """
        return await self._conn.send_command("Config.Set", _wifi_params(ssid=ssid))

    async def set_wifi_password(self, password):
        """Sets the WiFi password.

        :param password: The password for the WiFi network.
        """
        return await self._conn.send_command(
            "Config.Set", _wifi_params(password=password)
        )


def _wifi_params(ssid: str | None = None, password: str | None = None) -> dict:
    sta: dict[str, Any] = {"enable": True}
    if ssid:
        sta["ssid"] = ssid
    if password:
        sta["pass"] = password
    return {"config": {"wifi": {"sta": sta}}}
