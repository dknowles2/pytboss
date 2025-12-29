"""Base class for transport protocols."""

import asyncio
from abc import ABC, abstractmethod
from asyncio import AbstractEventLoop, Future, Lock, get_running_loop
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Protocol, Self, Type

from mypy_extensions import DefaultNamedArg

from .exceptions import RPCError


class RawStateCallback(Protocol):
    async def __call__(
        self, status_payload: str | None, temperatures_payload: str | None = None
    ) -> None: ...


RawVDataCallback = Callable[[str], Awaitable[None]]
SendCommandFn = Callable[
    [str, dict[Any, Any], DefaultNamedArg(float | None, "timeout")],
    Awaitable[dict[Any, Any] | None],
]


class Transport(ABC):
    """Base class for transport protocols."""

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
        exc_type: Type[BaseException] | None,
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
        self, method: str, params: dict, *, timeout: float | None = None
    ) -> dict:
        """Sends a comand to the device.

        :param method: The method to call.
        :param params: Parameters to send with the command.
        :param timeout: Timeout for the call.
        """
        cmd = await self._prepare_command(method, params)
        future = self._loop.create_future()
        async with self._lock:
            self._rpc_futures[cmd["id"]] = future
        async with asyncio.timeout(timeout):
            await self._send_prepared_command(cmd)
            return await future

    async def send_command_without_answer(
        self, method: str, params: dict, *, timeout: float | None = None
    ) -> None:
        """Sends a command to the device and doesn't wait for the response.

        :param method: The method to call.
        :param params: Parameters to send with the command.
        :param timeout: Timeout for the call.
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
                future.set_exception(
                    RPCError(payload["error"].get("message", "Unknown error"))
                )
            else:
                future.set_result(payload["result"])
        return True
