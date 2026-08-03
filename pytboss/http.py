"""HTTP connection support for PitBoss grills on the local network.

Mongoose OS serves the same RPC interface at `http://<grill-ip>/rpc` that the
Dansons websocket relays, so this library works against it unchanged, with the
vendor's cloud out of the path.

**Not every grill answers.** The ESP-IDF firmware line (versioned `16.x`, on
the PBC2, PBD, PBE, PBL2 and PBT boards) links no HTTP server at all, and
Mongoose grills serve the endpoint only when `http.enable` is set -- per-unit
configuration that no model or firmware version predicts.

Nothing here turns it on; `connect()` simply fails. Callers are expected to
check first, over a transport that already works::

    try:
        http = await boss.config.get_config("http")
    except RPCError:
        http = {}  # no such section: not a Mongoose grill
    if http.get("enable"):
        ...  # an HttpConnection will work against this grill

See https://github.com/dknowles2/pytboss/issues/505.
"""

import asyncio
import logging
from asyncio import AbstractEventLoop
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .exceptions import NotConnectedError, RPCError
from .transport import DEFAULT_TIMEOUT, Transport, _rpc_error

_LOGGER = logging.getLogger("pytboss")


class HttpConnection(Transport):
    """Local network transport for PitBoss grills, over Mongoose OS's HTTP RPC.

    **There is no push channel.** HTTP is strictly request/response, so unlike
    `pytboss.ble.BleConnection` and `pytboss.wss.WebSocketConnection` this
    transport never invokes the state or VData callbacks -- `subscribe_state()`
    and `subscribe_vdata()` register callbacks that will not fire. Polling
    `PitBoss.get_state()` is the way to observe state here; it issues a live
    RPC and updates the cache the rest of the API reads, so nothing else
    behaves differently.

    Also unlike the websocket transport, there is no reconnect loop. Each call
    is an independent request, so there is no connection to lose and nothing to
    re-establish; a grill that goes away surfaces as `NotConnectedError` on the
    next call and starts working again by itself.
    """

    def __init__(
        self,
        host: str,
        *,
        session: ClientSession | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        loop: AbstractEventLoop | None = None,
    ) -> None:
        """Initializes an HttpConnection.

        :param host: The grill's address on the local network. A bare host or
            `host:port`; the scheme is always plain HTTP, since the firmware
            serves no TLS certificate.
        :param session: An aiohttp ClientSession to use. If `None`, one is
            created on `connect()` and closed on `disconnect()`.
        :param timeout: Seconds to wait for a reply to any single request.
        :param loop: An asyncio loop to use. If `None`, the running loop.
        """
        super().__init__(loop=loop)
        self._url = f"http://{host}/rpc"
        self._timeout = ClientTimeout(total=timeout)
        self._session = session
        # A caller's session is theirs to close. Only one we made ourselves is
        # ours to close, and it is rebuilt by `connect()` so this transport is
        # reusable after `disconnect()`.
        self._session_owned = session is None
        # Two different questions. `_started` is whether this transport has
        # been connected and not since disconnected; `_connected` is whether
        # the grill answered the last time we spoke to it.
        self._started = False
        self._connected = False
        self._pending: set[asyncio.Task] = set()

    async def connect(self) -> None:
        """Verifies the grill answers RPC at this address.

        HTTP has no connection to open, so this establishes reachability
        instead: it sends an unauthenticated `RPC.Ping`. A wrong address, or a
        grill with `http.enable` off, fails here rather than on some later
        call.

        :raise pytboss.exceptions.NotConnectedError: If nothing answers.
        """
        if self._session is None or self._session.closed:
            if not self._session_owned:
                raise NotConnectedError("The session given to this transport is closed")
            self._session = ClientSession(loop=self._loop)
        # Set before the ping so `_send_prepared_command` will proceed; a
        # failure below clears them again.
        self._started = True
        self._connected = True
        try:
            await self.send_command("RPC.Ping", {})
        except Exception:
            self._started = False
            self._connected = False
            await self._close_session()
            raise

    async def disconnect(self) -> None:
        """Stops using the grill, and releases the session if this owns one.

        Final until `connect()` is called again. A caller's session stays
        open, since it is theirs, so nothing else here would stop a command
        from going out on it.
        """
        self._started = False
        self._connected = False
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()
        await self._close_session()

    async def _close_session(self) -> None:
        if self._session_owned and self._session is not None:
            if not self._session.closed:
                await self._session.close()
            self._session = None

    def is_connected(self) -> bool:
        """Whether the grill answered the last time we spoke to it.

        There is no persistent connection to report on, so this reports the
        outcome of the most recent exchange: `True` from a successful
        `connect()` until a request fails to reach the grill.
        """
        return (
            self._started
            and self._connected
            and self._session is not None
            and not self._session.closed
        )

    async def send_command_without_answer(
        self, method: str, params: dict, *, timeout: float | None = None
    ) -> None:
        """Sends a command without waiting for the reply.

        Issued in the background, unlike the other transports, where writing
        to the socket is the whole of it. Here a request is only finished
        when its response has been read, and the callers that choose this
        method choose it for commands the board cannot answer: `Sys.Reboot`
        is gone before it could, so awaiting the exchange means waiting out
        the full timeout and then raising `NotConnectedError` at a caller
        that is not expecting one.

        :param method: The method to call.
        :param params: Parameters to send with the command.
        :param timeout: Ignored. Nothing is awaited, so nothing can elapse.
        """
        cmd = await self._prepare_command(method, params)
        task = self._loop.create_task(self._send_and_forget(cmd))
        # Held so the task is not collected mid-flight, and so `disconnect()`
        # can stop one that is still going.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send_and_forget(self, cmd: dict) -> None:
        try:
            await self._send_prepared_command(cmd)
        except Exception as ex:  # noqa: BLE001
            # Nobody is waiting to be told, and for a command the board was
            # never going to answer this is the expected ending.
            _LOGGER.debug("No answer to %s, as expected: %s", cmd["method"], ex)

    async def _send_prepared_command(self, cmd: dict) -> None:
        # `_started` rather than `_connected`: a request that failed leaves
        # the latter false, and this transport is meant to recover from that
        # by itself on the next call. An explicit `disconnect()` is the one
        # that has to stick.
        if not self._started or self._session is None or self._session.closed:
            raise NotConnectedError("Not connected")
        _LOGGER.debug("--> %s", cmd)
        try:
            async with self._session.post(
                self._url, json=cmd, timeout=self._timeout
            ) as resp:
                if resp.status != 200:
                    # Built through `_rpc_error` so a 401 arrives as
                    # `Unauthorized`, which is what a caller re-prompting for
                    # a password catches and what a Mongoose build with
                    # `rpc.auth_file` set answers with.
                    raise _rpc_error(
                        {
                            "code": resp.status,
                            "message": f"{self._url} answered HTTP {resp.status}",
                        }
                    )
                # Mongoose does not always set a JSON content type.
                try:
                    payload: Any = await resp.json(content_type=None)
                except ValueError as ex:
                    # A 200 carrying something that is not JSON is not this
                    # protocol at all -- a mistyped address landing on a
                    # router's admin page answers exactly like that. Left
                    # alone it escapes as `JSONDecodeError`, which is neither
                    # documented here nor caught by callers.
                    raise RPCError(
                        f"{self._url} answered with something that is not JSON"
                    ) from ex
        except (ClientError, TimeoutError) as ex:
            # The grill is unreachable rather than refusing the call. Reported
            # as a lost connection so callers treat it the way they treat a
            # dropped websocket.
            #
            # `TimeoutError` as well as `ClientError`: a refused connection
            # raises the latter, but a host that simply does not answer --
            # unplugged, asleep, or on another network -- times out instead,
            # and `ClientTimeout` surfaces that as `asyncio.TimeoutError`,
            # which is not a `ClientError`. Silence is the common case.
            self._connected = False
            raise NotConnectedError(f"Could not reach {self._url}: {ex}") from ex
        _LOGGER.debug("<-- %s", payload)
        if not isinstance(payload, dict):
            raise RPCError(f"Expected a JSON object, got {type(payload).__name__}")
        self._connected = True
        # An HTTP reply provably belongs to the request that produced it,
        # whatever id the firmware echoed. Coercing it keeps a firmware that
        # echoes nothing -- or something else -- from stalling the caller
        # until its timeout expires.
        if payload.get("id") != cmd["id"]:
            _LOGGER.debug("Reply id %r != request id %r", payload.get("id"), cmd["id"])
            payload["id"] = cmd["id"]
        await self._on_command_response(payload)
