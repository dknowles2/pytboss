"""WebSocket connection support for PitBoss grills."""

import asyncio
import json
import logging

from websockets.asyncio.client import ClientConnection, connect

from .transport import RawStateCallback, RawVDataCallback, Transport

_LOGGER = logging.getLogger("pytboss")
_BASE_URL = "wss://socket.dansonscorp.com/to/"


class WebSocketConnection(Transport):
    """WebSocket transport for PitBoss grills."""

    def __init__(self, grill_id: str, loop: asyncio.AbstractEventLoop | None = None):
        """Initializes a WebSocketConnection.

        :param grill_id: The unique grill identifier.
        :type grill_id: str
        :param loop: An asyncio loop to use. If `None`, the default loop will be used.
        :type loop: asyncio.AbstractEventLoop
        """
        super().__init__()
        if loop is None:
            loop = asyncio.get_running_loop()
        self._loop = loop
        self._grill_id = grill_id
        self._sock: ClientConnection | None = None
        self._is_connected = False
        self._state_callback: RawStateCallback | None = None
        self._vdata_callback: RawVDataCallback | None = None

    async def connect(
        self, state_callback: RawStateCallback, vdata_callback: RawVDataCallback
    ) -> None:
        """Starts the connection to the device."""
        self._sock = await connect(_BASE_URL + self._grill_id)
        self._state_callback = state_callback
        self._vdata_callback = vdata_callback
        self._loop.create_task(self._subscribe())
        self._is_connected = True

    async def _subscribe(self) -> None:
        """Subscribes to WebSocket updates."""
        async for msg in self._sock:
            payload = json.loads(msg)
            _LOGGER.debug("WSS payload: %s", payload)
            if "status" in payload:
                _LOGGER.debug("--> status payload")
                for state in payload["status"]:
                    self._state_callback(state)
            if "id" in payload:
                _LOGGER.debug("--> command response payload")
                if await self._on_command_response(payload):
                    return
            if payload.get("result", None):
                _LOGGER.debug("--> (assume) VData payload")
                # TODO: I assume this is VData?
                await self._vdata_callback(payload)

    def is_connected(self) -> bool:
        """Whether the device is currently connected."""
        return self._is_connected

    async def _send_prepared_command(self, cmd: dict) -> None:
        cmd["app_id"] = "FIXME"
        await self._sock.send(json.dumps(cmd))
