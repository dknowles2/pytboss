"""WebSocket connection support for PitBoss grills."""

import asyncio
import logging
from asyncio import AbstractEventLoop, Task
from typing import Any
from uuid import uuid4

from aiohttp import (
    ClientSession,
    ClientWebSocketResponse,
    WSMsgType,
    WSServerHandshakeError,
)

from .exceptions import GrillUnavailable, NotConnectedError
from .transport import Transport

_BASE_URL = "wss://socket.dansonscorp.com"
_LOGGER = logging.getLogger("pytboss")
_MAX_BACKOFF_TIME = 30.0


class WebSocketConnection(Transport):
    """WebSocket transport for PitBoss grills."""

    def __init__(
        self,
        grill_id: str,
        session: ClientSession | None = None,
        loop: AbstractEventLoop | None = None,
        app_id: str | None = None,
        base_url: str = _BASE_URL,
    ):
        """Initializes a WebSocketConnection.

        :param grill_id: The unique grill identifier.
        :param session: An aiohttp ClientSession to use. If `None`, one will be created.
        :param loop: An asyncio loop to use. If `None`, the default loop will be used.
        :param app_id: A unique identifier for this client session. If None,
            one will be generated automatically.
        :param base_url: Base URL to use for connections.
        """
        super().__init__(loop=loop)
        self._session = session or ClientSession(loop=self._loop)
        self._sock: ClientWebSocketResponse | None = None
        self._url = f"{base_url}/to/{grill_id}"
        self._app_id = app_id or str(uuid4()).split("-")[-1]
        self._subscribe_task: Task | None = None
        self._keep_running = False

    async def connect(self) -> None:
        """Starts the connection to the device."""
        self._keep_running = True
        await self._ws_connect()
        self._subscribe_task = self._loop.create_task(self._subscribe())

    async def disconnect(self) -> None:
        """Stops the connection to the device."""
        self._keep_running = False
        if self._sock:
            await self._sock.close()
        if not self._session.closed:
            await self._session.close()
        if self._subscribe_task:
            await self._subscribe_task

    async def _ws_connect(self) -> None:
        try:
            self._sock = await self._session.ws_connect(self._url)
        except WSServerHandshakeError as ex:
            raise GrillUnavailable from ex

    async def _subscribe(self) -> None:
        """Subscribes to WebSocket updates."""
        attempt = 0
        backoff = 1.0

        while self._loop.is_running() and self._keep_running:
            if self._sock is None:
                try:
                    await self._ws_connect()
                except GrillUnavailable as ex:
                    attempt += 1
                    _LOGGER.debug(
                        "Failed to connect (attempt %d); sleeping for %.2fs: %s",
                        attempt,
                        backoff,
                        ex,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(_MAX_BACKOFF_TIME, backoff * 2)
                    continue

            attempt = 0
            backoff = 1.0

            async with self._sock:
                async for msg in self._sock:
                    if msg.type == WSMsgType.CLOSED:
                        break
                    payload = msg.json()
                    _LOGGER.debug("WSS payload: %s", payload)
                    await self._handle_message(payload)

            self._sock = None

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        if "app_id" in payload and payload["app_id"] != self._app_id:
            _LOGGER.debug(
                "Ignoring payload. Received app_id %s != %s",
                payload["app_id"],
                self._app_id,
            )
            return

        if "status" in payload:
            if not self._state_callback:
                return
            for state in payload["status"]:
                await self._state_callback(state)
            return

        if "id" in payload:
            await self._on_command_response(payload)
            return

        if payload.get("result", None):
            # TODO: Verify this is actually a vdata response.
            if not self._vdata_callback:
                return
            await self._vdata_callback(payload["result"])
            return

    def is_connected(self) -> bool:
        """Whether the device is currently connected."""
        return self._sock is not None

    async def _send_prepared_command(self, cmd: dict) -> None:
        cmd["app_id"] = self._app_id
        _LOGGER.debug("Sending command: %s", cmd)
        if self._sock is None:
            raise NotConnectedError
        await self._sock.send_json(cmd)
