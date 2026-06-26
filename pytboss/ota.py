"""Client library for Mongoose OS OTA (Over-The-Air update) RPCs."""

from .transport import Transport


class OTA:
    """Client library for Mongoose OS OTA RPCs.

    Also see: https://mongoose-os.com/docs/mongoose-os/api/rpc/rpc-service-ota.md
    """

    def __init__(self, conn: Transport) -> None:
        """Initializes the class.

        :param conn: Transport for the device.
        """
        self._conn = conn

    async def start(self, url: str, commit_timeout: int | None = None) -> dict:
        """Initiates an OTA update.

        :param url: URL of the firmware zip file to download.
        :param commit_timeout: Optional timeout (in seconds) to auto-commit the
            new firmware. If the device is not committed within this time it
            rolls back automatically.
        """
        params: dict = {"url": url}
        if commit_timeout is not None:
            params["commit_timeout"] = commit_timeout
        return await self._conn.send_command("OTA.Start", params)

    async def get_status(self) -> dict:
        """Returns the current OTA update status.

        The returned dict typically contains:
          - ``state``: current OTA state string (e.g. ``"idle"``, ``"progress"``,
            ``"error"``)
          - ``msg``: human-readable message
          - ``progress``: download progress (0–100) while downloading
        """
        return await self._conn.send_command("OTA.Status", {})
