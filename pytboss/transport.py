"""Shared RPC-over-futures request/response machinery for transports.

Concrete transports (`pytboss.ble.BleConnection`, `pytboss.wss.WebSocketConnection`)
subclass `Transport` and implement the connection-specific details; this
module handles matching outgoing commands to their responses via futures.
"""

import asyncio
from abc import ABC, abstractmethod
from asyncio import AbstractEventLoop, Future, Lock, get_running_loop
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Protocol, Self

from mypy_extensions import DefaultNamedArg

from .exceptions import NotConnectedError, RPCError, Unauthorized

DEFAULT_TIMEOUT = 30.0
"""Seconds to wait for a reply before giving up."""


class RawStateCallback(Protocol):
    """Callback invoked by a `Transport` with raw, unparsed state payloads."""

    async def __call__(
        self, status_payload: str | None, temperatures_payload: str | None = None
    ) -> None: ...


RawVDataCallback = Callable[[str], Awaitable[None]]
"""Callback invoked by a `Transport` with a raw, unparsed VData payload."""

SendCommandFn = Callable[
    [str, dict[Any, Any], DefaultNamedArg(float | None, "timeout")],
    Awaitable[dict[Any, Any] | None],
]
"""Signature shared by `Transport.send_command` and `send_command_without_answer`."""


UNAUTHORIZED_CODE = 401
"""The code the firmware answers with when the password is wrong or missing."""


def _rpc_error(error: dict) -> RPCError:
    """Build the exception for an error payload from the device."""
    message = error.get("message", "Unknown error")
    code = error.get("code")
    if code == UNAUTHORIZED_CODE:
        return Unauthorized(message, code)
    return RPCError(message, code)


class Transport(ABC):
    """Abstract base class implementing the RPC request/response protocol
    shared by all connection types."""

    def __init__(self, loop: AbstractEventLoop | None = None) -> None:
        self._lock = Lock()
        self._last_command_id = 0
        self._rpc_futures: dict[int, Future[Any]] = {}
        self._state_callback: RawStateCallback | None = None
        self._vdata_callback: RawVDataCallback | None = None
        self._loop = loop or get_running_loop()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.disconnect()

    def set_state_callback(self, state_callback: RawStateCallback) -> None:
        self._state_callback = state_callback

    def set_vdata_callback(self, vdata_callback: RawVDataCallback) -> None:
        self._vdata_callback = vdata_callback

    @abstractmethod
    async def connect(self) -> None:
        """Starts the connection to the device."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Stop the connection to the device."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Whether there is an active connection to the device."""

    @abstractmethod
    async def _send_prepared_command(self, cmd: dict) -> None: ...

    async def send_command(
        self, method: str, params: dict, *, timeout: float | None = DEFAULT_TIMEOUT
    ) -> dict:
        """Sends a comand to the device.

        :param method: The method to call.
        :param params: Parameters to send with the command.
        :param timeout: Timeout for the call. Pass `None` to wait forever.
        :raise pytboss.exceptions.RPCError: If the device returns an error response.
        :raise pytboss.exceptions.Unauthorized: If the device rejects the
            password. This is an `RPCError`, so catching that still works.
        :raise TimeoutError: If `timeout` elapses before a response is received.
        """
        cmd = await self._prepare_command(method, params)
        future = self._loop.create_future()
        async with self._lock:
            self._rpc_futures[cmd["id"]] = future
        try:
            async with asyncio.timeout(timeout):
                await self._send_prepared_command(cmd)
                return await future
        finally:
            # A reply that never arrives would otherwise leave its future
            # behind forever.
            async with self._lock:
                self._rpc_futures.pop(cmd["id"], None)

    async def _fail_pending_commands(self, ex: Exception | None = None) -> None:
        """Fail commands still waiting for a reply that can no longer arrive.

        A subclass calls this wherever it notices the link has gone -- usually
        its receive loop rather than `disconnect()`, since a drop is not
        normally an explicit disconnect. Without it, a caller waits out the
        full `timeout` for a reply the device can no longer send.

        :param ex: The exception to fail them with. Defaults to
            `NotConnectedError`.
        """
        async with self._lock:
            futures = list(self._rpc_futures.values())
            self._rpc_futures.clear()
        for future in futures:
            if not future.done():
                future.set_exception(ex or NotConnectedError())

    async def send_command_without_answer(
        self, method: str, params: dict, *, timeout: float | None = None
    ) -> None:
        """Sends a command to the device and doesn't wait for the response.

        :param method: The method to call.
        :param params: Parameters to send with the command.
        :param timeout: Timeout for the call.
        :raise TimeoutError: If `timeout` elapses before the command is sent.
        """
        async with asyncio.timeout(timeout):
            await self._send_prepared_command(
                await self._prepare_command(method, params)
            )

    async def _next_command_id(self) -> int:
        async with self._lock:
            self._last_command_id = self._last_command_id + 1 & 2047
            return self._last_command_id

    async def _prepare_command(self, method: str, params: dict) -> dict:
        return {"id": await self._next_command_id(), "method": method, "params": params}

    async def _on_command_response(self, payload: dict) -> bool:
        async with self._lock:
            future = self._rpc_futures.pop(payload["id"], None)
        if not future:
            return False
        if not future.cancelled():
            if "error" in payload:
                future.set_exception(_rpc_error(payload["error"]))
            else:
                future.set_result(payload["result"])
        return True
