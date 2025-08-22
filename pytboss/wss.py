"""WebSocket connection support for PitBoss grills."""

import asyncio
import logging
from asyncio import AbstractEventLoop, Event, Lock, Task
from typing import Any
from uuid import uuid4

from aiohttp import (
    ClientSession,
    ClientWebSocketResponse,
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
        self._session_owned = session is None  # Track if we created the session
        self._sock_lock = Lock()  # Protects access to self._sock operations
        self._sock: ClientWebSocketResponse | None = None
        self._url = f"{base_url}/to/{grill_id}"
        self._app_id = app_id or str(uuid4()).split("-")[-1]
        self._subscribe_task: Task | None = None
        self._subscribed = Event()
        self._keep_running = False

    async def connect(self) -> None:
        """Starts the connection to the device."""
        self._sock = await self._ws_connect()
        self._keep_running = True
        self._subscribe_task = self._loop.create_task(self._subscribe())
        await self._subscribed.wait()

    async def disconnect(self) -> None:
        """Stops the connection to the device."""
        self._keep_running = False
        if self._sock:
            await self._sock.close()
        # Only close the session if we created it (not if it was provided externally)
        if self._session_owned and not self._session.closed:
            await self._session.close()
        if self._subscribe_task:
            await self._subscribe_task

    async def _ws_connect(self) -> ClientWebSocketResponse:
        _LOGGER.debug("Connecting to WebSocket")
        try:
            return await self._session.ws_connect(self._url)
        except WSServerHandshakeError as ex:
            _LOGGER.debug("Failed to connect: %s", ex)
            raise GrillUnavailable(str(ex)) from ex

    async def _subscribe(self) -> None:
        """Subscribes to WebSocket updates."""
        attempt = 1
        backoff = 1.0
        while self._loop.is_running() and self._keep_running:
            if self._sock is None:
                try:
                    _LOGGER.debug("Reconnecting (attempt %d)", attempt)
                    self._sock = await self._ws_connect()
                except GrillUnavailable as ex:
                    _LOGGER.debug("Failed to connect (attempt %d): %s", attempt, ex)
                    _LOGGER.debug("Will try again in %.2fs", backoff)
                    await asyncio.sleep(backoff)
                    attempt += 1
                    backoff = min(_MAX_BACKOFF_TIME, backoff * 2)
                    continue

            attempt = 1
            backoff = 1.0

            async with self._sock:
                _LOGGER.debug("Waiting for payloads")
                self._subscribed.set()
                async for msg in self._sock:
                    async with self._sock_lock:
                        payload = msg.json()
                        _LOGGER.debug("WSS payload: %s", payload)
                        await self._handle_message(payload)
                _LOGGER.debug("WebSocket closed")

            self._sock = None
        _LOGGER.debug(
            "Exiting subscribe loop. is_running=%s, keep_running=%s",
            self._loop.is_running(),
            self._keep_running,
        )

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
            await self._state_callback(*payload["status"])
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
        return self._sock is not None and not self._sock.closed

    async def _send_prepared_command(self, cmd: dict) -> None:
        if not self.is_connected():
            raise NotConnectedError
        cmd["app_id"] = self._app_id
        _LOGGER.debug("Sending command: %s", cmd)
        async with self._sock_lock:
            await self._sock.send_json(cmd)  # type: ignore
