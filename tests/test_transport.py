import asyncio

import pytest

from pytboss.exceptions import GrillUnavailable, NotConnectedError
from pytboss.transport import Transport


class FakeTransport(Transport):
    """Minimal concrete Transport for exercising the base class directly."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def _send_prepared_command(self, cmd: dict) -> None:
        self.sent.append(cmd)


async def test_send_command_without_answer():
    conn = FakeTransport()
    await conn.send_command_without_answer("Some.Method", {"foo": "bar"})
    assert conn.sent == [{"id": 1, "method": "Some.Method", "params": {"foo": "bar"}}]


async def test_on_command_response_unknown_id():
    conn = FakeTransport()
    handled = await conn._on_command_response({"id": 999, "result": {}})
    assert handled is False


async def test_response_without_result_resolves_the_command():
    """Mongoose OS omits `result` when a handler returns nothing."""
    conn = FakeTransport()
    task = asyncio.create_task(conn.send_command("Sys.Reboot", {}, timeout=None))
    await asyncio.sleep(0)  # let it register its future

    handled = await conn._on_command_response({"id": 1})

    assert handled is True
    assert await task is None


async def test_send_command_gives_up_instead_of_waiting_forever():
    """A reply that never arrives must not block the caller indefinitely."""
    conn = FakeTransport()
    with pytest.raises(TimeoutError):
        await conn.send_command("PB.GetState", {}, timeout=0.01)
    # And the abandoned future must not be left behind.
    assert conn._rpc_futures == {}


async def test_fail_pending_commands_wakes_the_caller():
    """A reply that can no longer arrive must not be waited out."""
    conn = FakeTransport()
    task = asyncio.create_task(conn.send_command("PB.GetState", {}, timeout=None))
    await asyncio.sleep(0)  # let it register its future

    await conn._fail_pending_commands()

    with pytest.raises(NotConnectedError):
        await task
    assert conn._rpc_futures == {}


async def test_fail_pending_commands_can_carry_a_reason():
    conn = FakeTransport()
    task = asyncio.create_task(conn.send_command("PB.GetState", {}, timeout=None))
    await asyncio.sleep(0)

    await conn._fail_pending_commands(GrillUnavailable("gone"))

    with pytest.raises(GrillUnavailable):
        await task


async def test_fail_pending_commands_with_nothing_in_flight():
    conn = FakeTransport()
    await conn._fail_pending_commands()
    assert conn._rpc_futures == {}
