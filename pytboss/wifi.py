"""Client library for Mongoose OS WiFi RPCs."""

from .transport import Transport


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

        Empty when the device does not serve it. `PitBoss.list_rpcs()` reports
        whether it does without a round trip that errors -- worth checking
        first, since an empty list otherwise reads the same as seeing no
        networks.

        Unauthenticated, and a read: it changes nothing about the device's own
        connection.
        """
        result = await self._conn.send_command("Wifi.Scan", {})
        return result if isinstance(result, list) else []
