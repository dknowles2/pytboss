"""Base class for transport protocols."""

from abc import ABC, abstractmethod
from asyncio import Future, Lock
from typing import Any, Awaitable, Callable

from .exceptions import RPCError

RawStateCallback = Callable[[str], Awaitable[None]]
RawVDataCallback = Callable[[str], Awaitable[None]]


class Transport(ABC):
    """Base class for transport protocols."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._last_command_id = 0
        self._rpc_futures: dict[int, Future[Any]] = {}

    @abstractmethod
    async def connect(
        self, state_callback: RawStateCallback, vdata_callback: RawVDataCallback
    ) -> None:
        """Starts the connection to the device."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Whether there is an active connection to the device."""

    @abstractmethod
    async def _send_prepared_command(self, cmd: dict) -> None: ...

    async def send_command(self, method: str, params: dict) -> dict:
        """Sends a comand to the device.

        :param method: The method to call.
        :type method: str
        :param params: Parameters to send with the command.
        :type params: dict
        :rtype: dict
        """
        cmd = await self._prepare_command(method, params)
        future = self._loop.create_future()
        async with self._lock:
            self._rpc_futures[cmd["id"]] = future
        await self._send_prepared_command(cmd)
        return await future

    async def send_command_without_answer(self, method: str, params: dict) -> None:
        """Sends a command to the device and doesn't wait for the response.

        :param method: The method to call.
        :type method: str
        :param params: Parameters to send with the command.
        :type params: dict
        """
        await self._send_prepared_command(self._prepare_command(method, params))

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
