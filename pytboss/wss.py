"""WebSocket connection support for PitBoss grills."""

import asyncio
import logging
from asyncio import AbstractEventLoop, Event, Lock, Queue, Task
from contextlib import suppress
from typing import Any
from uuid import uuid4

from aiohttp import (
    ClientError,
    ClientSession,
    ClientWebSocketResponse,
    WSMsgType,
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
        # A caller's session is theirs to close. Only one we made ourselves is
        # ours, and `connect()` rebuilds it, so this transport is reusable
        # after `disconnect()`.
        self._session = session
        self._session_owned = session is None
        self._sock_lock = Lock()  # Protects access to self._sock operations
        self._sock: ClientWebSocketResponse | None = None
        self._url = f"{base_url}/to/{grill_id}"
        self._app_id = app_id or str(uuid4()).split("-")[-1]
        self._subscribe_task: Task | None = None
        # Callbacks are delivered from their own task, fed by this queue, so
        # the read loop never awaits user code. A callback that issues an
        # RPC otherwise waits on a reply only the read loop it is blocking
        # can deliver -- burning the command's whole timeout -- and every
        # state update queues behind it. Rebuilt on each connect so a stale
        # frame from a previous session is never delivered into a new one.
        self._callback_queue: Queue[tuple[str, Any]] = Queue()
        self._dispatch_task: Task | None = None
        self._subscribed = Event()
        self._stopping = Event()
        self._keep_running = False
        self._connect_task: Task | None = None
        # Whether the cancellation about to arrive is ours. `_keep_running`
        # cannot answer that: the wind-down clears it before cancelling, so
        # during a disconnect it reads False whoever did the cancelling.
        self._handshake_cancelled = False
        # Serializes connect/disconnect. Opening a socket is an await, so two
        # callers otherwise both pass the wind-down check, both open one, and
        # the second assignment orphans the first -- along with its task,
        # which `disconnect()` can no longer reach. Distinct from
        # `_sock_lock`, which serializes sends.
        self._lifecycle_lock = Lock()

    async def connect(self) -> None:
        """Starts the connection to the device.

        Reusable: a transport that has been disconnected can be connected
        again, and calling this twice does not strand the first subscribe
        task.

        :raise pytboss.exceptions.NotConnectedError: If a session was supplied
            by the caller and has since been closed.
        """
        self._check_not_reentrant()
        async with self._lifecycle_lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        await self._stop_subscribing()
        if self._session is None or self._session.closed:
            if not self._session_owned:
                raise NotConnectedError("The session given to this transport is closed")
            self._session = ClientSession(loop=self._loop)
        # Run as a task for the same reason the reconnect loop runs its own:
        # no flag reaches a handshake in flight. `disconnect()` cancels
        # `_connect_task` *before* taking the lifecycle lock -- the lock this
        # call is holding -- so a caller is not parked behind a stalled
        # handshake for aiohttp's full session timeout.
        self._connect_task = self._loop.create_task(self._ws_connect())
        try:
            self._sock = await self._connect_task
            self._keep_running = True
            self._stopping.clear()
            self._callback_queue = Queue()
            self._subscribe_task = self._loop.create_task(self._subscribe())
            self._dispatch_task = self._loop.create_task(self._dispatch_callbacks())
        except BaseException as ex:
            # The rollback `http.connect()` already has: a failed or
            # cancelled connect must not leak the session this call opened.
            # A config flow constructing a fresh transport per attempt
            # otherwise leaks one per retry.
            if self._session_owned and self._session is not None:
                if not self._session.closed:
                    await self._session.close()
                self._session = None
            if isinstance(ex, asyncio.CancelledError) and self._handshake_cancelled:
                raise NotConnectedError(
                    "disconnect() was called while connecting"
                ) from ex
            raise
        finally:
            self._connect_task = None
            self._handshake_cancelled = False
        await self._subscribed.wait()

    def _check_not_reentrant(self) -> None:
        """Refuse a lifecycle call made from a callback we are dispatching.

        Awaiting the subscribe task from inside itself never returns, so say
        so rather than hang. Checked *before* the lifecycle lock is taken:
        past it, the caller would block on the lock while whoever holds the
        lock awaits the very subscribe task this caller is running on -- a
        deadlock the guard exists to prevent, not cause.
        """
        current = asyncio.current_task()
        if current is not None and (
            current is self._subscribe_task or current is self._dispatch_task
        ):
            raise RuntimeError(
                "Cannot stop this transport from a callback it is dispatching"
            )

    async def _stop_subscribing(self) -> None:
        """Wind down a running subscribe task, if there is one."""
        if self._subscribe_task is None:
            return
        self._check_not_reentrant()
        self._keep_running = False
        self._stopping.set()
        # The handshake is the one await neither flag reaches -- aiohttp
        # offers no way to interrupt `ws_connect` -- so it is cancelled
        # directly. The subscribe task itself is still awaited rather than
        # cancelled, so the `_fail_pending_commands()` below its read loop
        # still runs and in-flight commands fail now instead of timing out.
        if self._connect_task is not None:
            self._handshake_cancelled = True
            self._connect_task.cancel()
        if self._sock is not None and not self._sock.closed:
            await self._sock.close()
        try:
            # A task that died re-raises here. Letting that propagate made
            # `disconnect()` fail the same way on every call, forever, and
            # the lines below -- clearing state, and in `disconnect()`'s
            # case closing an owned session -- never ran.
            await self._subscribe_task
        except Exception:
            _LOGGER.exception("Subscribe task ended with an unhandled error")
        finally:
            self._subscribe_task = None
            # The event means "the loop is reading". Left set, the next
            # `connect()` returns before that is true again.
            self._subscribed.clear()
        if self._dispatch_task is not None:
            # Cancelled rather than drained: draining means awaiting user
            # callbacks, and a stuck callback would turn `disconnect()` into
            # exactly the hang this task exists to prevent.
            self._dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._dispatch_task
            self._dispatch_task = None

    async def disconnect(self) -> None:
        """Stops the connection to the device.

        Also closes the underlying aiohttp session if it was created
        internally (i.e. no `session` was passed to `__init__`), and waits
        for the background reconnect/subscribe task to finish.
        """
        self._check_not_reentrant()
        # Cancelled before taking the lock: a `connect()` parked in its
        # handshake is HOLDING the lock, so waiting for it first means
        # waiting out aiohttp's session timeout before this could act.
        if (handshake := self._connect_task) is not None:
            self._handshake_cancelled = True
            handshake.cancel()
        async with self._lifecycle_lock:
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        # Wakes the subscribe task if it is waiting out a reconnect backoff;
        # otherwise this call blocks for the remainder of that sleep.
        await self._stop_subscribing()
        # Only a session we created is ours to close.
        if self._session_owned and self._session is not None:
            if not self._session.closed:
                await self._session.close()
            self._session = None

    async def _ws_connect(self) -> ClientWebSocketResponse:
        _LOGGER.debug("Connecting to WebSocket")
        if self._session is None:
            raise NotConnectedError("Not connected")
        try:
            return await self._session.ws_connect(self._url)
        except (ClientError, OSError, TimeoutError) as ex:
            # Everything the network can do here means the same thing: the
            # grill cannot be reached right now. Mapping only the handshake
            # error left `ClientConnectorError` -- what an offline relay or a
            # dropped network actually raises -- escaping the reconnect
            # loop's `except GrillUnavailable`, ending the automatic
            # reconnection this class promises.
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
                    self._connect_task = self._loop.create_task(self._ws_connect())
                    self._sock = await self._connect_task
                except asyncio.CancelledError:
                    if not self._handshake_cancelled:
                        # Not ours: a caller's timeout, or a shutdown.
                        # Swallowing it would hand back a clean return to
                        # someone who asked for a bound and did not get one.
                        raise
                    break
                except GrillUnavailable as ex:
                    _LOGGER.debug("Failed to connect (attempt %d): %s", attempt, ex)
                    _LOGGER.debug("Will try again in %.2fs", backoff)
                    await self._backoff_wait(backoff)
                    attempt += 1
                    backoff = min(_MAX_BACKOFF_TIME, backoff * 2)
                    continue
                finally:
                    self._connect_task = None
                    # Cleared here rather than in the handler, so a cancel
                    # that never landed -- the handshake finished first --
                    # cannot make the next one look like ours.
                    self._handshake_cancelled = False
                if not self._keep_running:
                    # The handshake won the race against the wind-down that
                    # tried to cancel it. Without this the socket it just
                    # opened is served as though nothing had been asked.
                    if self._sock is not None and not self._sock.closed:
                        await self._sock.close()
                    self._sock = None
                    break

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

    async def _dispatch_callbacks(self) -> None:
        """Deliver state and vdata payloads from the read loop's queue.

        On its own task so the read loop never awaits user code. A callback
        that issues an RPC otherwise waits on a reply that only the read
        loop it is blocking can deliver -- the command burns its whole
        timeout with the answer sitting unread in the socket, and every
        state update queues behind it. A single consumer keeps delivery in
        arrival order. Cancelled by the wind-down; one failing callback is
        logged and must not starve the rest.
        """
        while True:
            kind, payload = await self._callback_queue.get()
            try:
                if kind == "status":
                    if self._state_callback:
                        await self._state_callback(*payload)
                elif self._vdata_callback:
                    await self._vdata_callback(payload)
            except Exception:
                _LOGGER.exception("Error in %s callback for: %s", kind, payload)
            finally:
                # So `join()` can observe delivery, not just consumption.
                self._callback_queue.task_done()

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        if "app_id" in payload and payload["app_id"] != self._app_id:
            _LOGGER.debug(
                "Ignoring payload. Received app_id %s != %s",
                payload["app_id"],
                self._app_id,
            )
            return

        if "status" in payload:
            # Virtual data rides on the status push rather than arriving on a
            # frame of its own. The firmware builds one object and attaches it
            # only when there is something to send::
            #
            #     let data = {id: -1, src: deviceId, status: wsStatus};
            #     if (sendVData === true && vData !== null) { data.data = vData; }
            #
            # so it has to be picked up here, before returning.
            if (vdata := payload.get("data")) is not None and self._vdata_callback:
                self._callback_queue.put_nowait(("vdata", vdata))
            if not self._state_callback:
                return
            frames = payload["status"]
            if (
                len(frames) == 1
                and isinstance(frames[0], str)
                and (frames[0][:4] == "FE0C")
            ):
                # The firmware builds this array conditionally -- a frame is
                # pushed only if it is non-empty, and `sendMCUCommand` blanks
                # both frames on every command, refilled one packet at a
                # time. So a push carrying only the temperatures frame is
                # producible, right in the window after a user-issued
                # command, and positional unpacking would hand it over as
                # the *status* payload -- where every board's status routine
                # requires an FE0B prefix and returns nothing. Routed by the
                # frame's own prefix instead, the way `ble` already does.
                self._callback_queue.put_nowait(("status", (None, frames[0])))
                return
            self._callback_queue.put_nowait(("status", tuple(frames)))
            return

        if "id" in payload:
            await self._on_command_response(payload)
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
