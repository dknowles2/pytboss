"""WebSocket connection support for PitBoss grills."""

import asyncio
import logging
from asyncio import AbstractEventLoop, Event, Lock, Task
from contextlib import suppress
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
    """WebSocket transport for PitBoss grills.

    Unlike `pytboss.ble.BleConnection`, this transport automatically
    reconnects with exponential backoff (up to `_MAX_BACKOFF_TIME` seconds
    between attempts) if the connection drops.
    """

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
        self._stopping = Event()
        self._keep_running = False

    async def connect(self) -> None:
        """Starts the connection to the device."""
        self._sock = await self._ws_connect()
        self._keep_running = True
        self._stopping.clear()
        self._subscribe_task = self._loop.create_task(self._subscribe())
        await self._subscribed.wait()

    async def disconnect(self) -> None:
        """Stops the connection to the device.

        Also closes the underlying aiohttp session if it was created
        internally (i.e. no `session` was passed to `__init__`), and waits
        for the background reconnect/subscribe task to finish.
        """
        self._keep_running = False
        # Wake the subscribe task if it is waiting out a reconnect backoff;
        # otherwise this call blocks for the remainder of that sleep.
        self._stopping.set()
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
                    await self._backoff_wait(backoff)
                    attempt += 1
                    backoff = min(_MAX_BACKOFF_TIME, backoff * 2)
                    continue

            attempt = 1
            backoff = 1.0

            async with self._sock:
                _LOGGER.debug("Waiting for payloads")
                self._subscribed.set()
                async for msg in self._sock:
                    # One bad payload or subscriber must not tear down the
                    # stream: an exception escaping this loop would end the
                    # task, and with it the automatic reconnects this class
                    # promises, while leaving in-flight commands to wait out
                    # their full timeout.
                    if msg.type is not WSMsgType.TEXT:
                        _LOGGER.debug("Ignoring %s frame", msg.type.name)
                        continue
                    try:
                        payload = msg.json()
                    except ValueError:
                        _LOGGER.warning("Ignoring malformed payload: %s", msg.data)
                        continue
                    _LOGGER.debug("WSS payload: %s", payload)
                    try:
                        await self._handle_message(payload)
                    except Exception:
                        _LOGGER.exception("Error handling payload: %s", payload)
                _LOGGER.debug("WebSocket closed")

            self._sock = None
            await self._fail_pending_commands()
        _LOGGER.debug(
            "Exiting subscribe loop. is_running=%s, keep_running=%s",
            self._loop.is_running(),
            self._keep_running,
        )

    async def _backoff_wait(self, backoff: float) -> None:
        """Wait out a reconnect backoff.

        Interruptible: `disconnect()` sets `_stopping` so it does not have to
        wait for the remainder of the backoff, which reaches
        `_MAX_BACKOFF_TIME` seconds.
        """
        with suppress(TimeoutError):
            async with asyncio.timeout(backoff):
                await self._stopping.wait()

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
        cmd["app_id"] = self._app_id
        _LOGGER.debug("Sending command: %s", cmd)
        async with self._sock_lock:
            # Re-read under the lock: the subscribe loop clears `_sock` when
            # the stream ends, so a check made before acquiring the lock can
            # be stale by the time the send happens.
            sock = self._sock
            if sock is None or sock.closed:
                raise NotConnectedError
            await sock.send_json(cmd)
