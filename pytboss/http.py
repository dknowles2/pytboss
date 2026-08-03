"""HTTP connection support for PitBoss grills on the local network.

Mongoose OS serves the same RPC interface at `http://<grill-ip>/rpc` that the
Dansons websocket relays, so the command mappings and status decoders in this
library work against it unchanged. Talking to the grill directly removes the
vendor's cloud from the path.

**Not every grill answers.** The endpoint is gated by the firmware's
`http.enable` setting, and that is per-unit configuration rather than
something a model or firmware version predicts -- two grills on the same
board and the same firmware have been observed with opposite values. Nothing
here turns it on: `connect()` fails against a grill where it is off, and the
caller is expected to have established that the endpoint exists first, over a
transport that already works. `Config.Get` over Bluetooth reports it::

    http = await boss.config.get_config("http")
    if http["enable"]:
        ...  # an HttpConnection will work against this grill

See https://github.com/dknowles2/pytboss/issues/505.
"""

import logging
from asyncio import AbstractEventLoop
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .exceptions import NotConnectedError, RPCError
from .transport import DEFAULT_TIMEOUT, Transport

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
        self._connected = False

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
        # failure below clears it again.
        self._connected = True
        try:
            await self.send_command("RPC.Ping", {})
        except Exception:
            self._connected = False
            await self._close_session()
            raise

    async def disconnect(self) -> None:
        """Releases the session, if this transport owns one."""
        self._connected = False
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
            self._connected and self._session is not None and not self._session.closed
        )

    async def _send_prepared_command(self, cmd: dict) -> None:
        if self._session is None or self._session.closed:
            raise NotConnectedError("Not connected")
        _LOGGER.debug("--> %s", cmd)
        try:
            async with self._session.post(
                self._url, json=cmd, timeout=self._timeout
            ) as resp:
                if resp.status != 200:
                    raise RPCError(
                        f"{self._url} answered HTTP {resp.status}", resp.status
                    )
                # Mongoose does not always set a JSON content type.
                payload: Any = await resp.json(content_type=None)
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
