"""Client library for Mongoose OS WiFi RPCs."""

from .exceptions import RPCError
from .transport import METHOD_NOT_FOUND_CODE, Transport


class WiFi:
    """Client library for Mongoose OS WiFi RPCs.

    A Mongoose core service rather than anything Dansons wrote, so none of it
    appears in the grill application and its shapes come from `Mongoose's own
    documentation
    <https://mongoose-os.com/docs/mongoose-os/api/rpc/rpc-service-wifi.md>`_.

    Not to be confused with the loader's own scan --
    `PitBoss.scan_wifi_networks()` drives `PBL.StartWifiScan`, which Dansons
    wrote and which reports the same networks under a different field name.
    """

    def __init__(self, conn: Transport) -> None:
        """Initializes the class.

        :param conn: Transport for the device.
        """
        self._conn = conn

    async def scan(self) -> list[dict]:
        """Returns the WiFi networks the device can see.

        A bare array taking no parameters, each entry carrying `ssid`,
        `bssid`, `auth`, `channel` and `rssi`. Returned as sent rather than
        remapped, since nothing here has seen a grill answer it.

        `auth` is an enum -- 0 open, 1 WEP, 2 WPA-PSK, 3 WPA2-PSK, 4
        WPA/WPA2-PSK, 5 WPA2-Enterprise. Note it is `auth` here and `authMode`
        from the loader's `PitBoss.get_wifi_scan_status()`, which goes through
        Mongoose's JS `Wifi.scan()` instead. Same device, same networks, two
        spellings.

        Empty when the device does not serve it -- an empty list reads the
        same as seeing no networks, so `PitBoss.list_rpcs()` is the way to
        tell those apart when it matters.

        Unauthenticated, and a read: it changes nothing about the device's own
        connection.
        """
        try:
            result = await self._conn.send_command("Wifi.Scan", {})
        except RPCError as ex:
            if ex.code == METHOD_NOT_FOUND_CODE:
                # The documented empty. Without this, "the device does not
                # serve it" raised instead of answering what the docstring
                # promises.
                return []
            raise
        return result if isinstance(result, list) else []
