from asyncio import Event, Queue, create_task
from typing import AsyncGenerator
from unittest.mock import AsyncMock, call

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from aiohttp.web import Application, Request, WebSocketResponse, get
from pytest import fixture

from pytboss import wss


@fixture
def state_payloads() -> Queue:
    return Queue()


@fixture
def command_payloads() -> Queue:
    return Queue()


@fixture
async def fake_server(
    state_payloads: Queue,
    command_payloads: Queue,
) -> TestServer:
    async def handler(request: Request):
        ws = WebSocketResponse()
        await ws.prepare(request)

        async def pump_status():
            while True:
                status = await state_payloads.get()
                await ws.send_json(status)
                state_payloads.task_done()

        task = create_task(pump_status())
        async for _ in ws:
            await ws.send_json(await command_payloads.get())
            command_payloads.task_done()

        task.cancel()
        await task
        return ws

    app = Application()
    app.add_routes([get("/to/_grill_id_", handler)])
    return TestServer(app)


class MockCallback(Event):
    def __init__(self, want_awaits: int = 0):
        super().__init__()
        self.want_awaits = want_awaits
        self.mock = AsyncMock()

    def reset_mock(self):
        self.mock.reset_mock()
        self.clear()

    def assert_awaited_once_with(self, *args, **kwargs):
        return self.mock.assert_awaited_once_with(*args, **kwargs)

    def assert_has_awaits(self, *args, **kwargs):
        return self.mock.assert_has_awaits(*args, **kwargs)

    def assert_not_awaited(self):
        return self.mock.assert_not_awaited()

    async def __call__(self, *args, **kwargs):
        try:
            return await self.mock(*args, **kwargs)
        finally:
            if self.mock.await_count == self.want_awaits:
                self.set()


@fixture
async def session() -> ClientSession:
    return ClientSession()


@fixture
async def conn(
    fake_server: TestServer,
    session: ClientSession,
) -> AsyncGenerator[wss.WebSocketConnection, None]:
    async with fake_server:
        async with session:
            yield wss.WebSocketConnection(
                "_grill_id_",
                session=session,
                base_url=fake_server.make_url(""),
                app_id="_app_id_",
            )


async def test_connect_disconnect(conn: wss.WebSocketConnection) -> None:
    await conn.connect()
    await conn.disconnect()


async def test_status(conn: wss.WebSocketConnection, state_payloads: Queue):
    state_callback = MockCallback(1)
    vdata_callback = MockCallback(0)
    conn.set_state_callback(state_callback)
    conn.set_vdata_callback(vdata_callback)
    async with conn:
        await state_payloads.put({"status": ["status-a"]})
        await state_callback.wait()
        state_callback.assert_awaited_once_with("status-a")
        state_callback.reset_mock()
        state_callback.want_awaits = 2
        await state_payloads.put({"status": ["status-b", "status-c"]})
        await state_callback.wait()
        state_callback.assert_has_awaits([call("status-b"), call("status-c")])

    vdata_callback.assert_not_awaited()


async def test_command(conn: wss.WebSocketConnection, command_payloads: Queue):
    state_callback = MockCallback()
    vdata_callback = MockCallback()
    conn.set_state_callback(state_callback)
    conn.set_vdata_callback(vdata_callback)
    async with conn:
        payload = {"app_id": "_app_id_", "id": 1, "result": "_result_"}
        await command_payloads.put(payload)
        assert await conn.send_command("cmd", {}, timeout=1) == "_result_"
    state_callback.assert_not_awaited()
    vdata_callback.assert_not_awaited()


async def test_command_wrong_app_id(
    conn: wss.WebSocketConnection,
    state_payloads: Queue,
    command_payloads: Queue,
):
    state_callback = AsyncMock()
    vdata_callback = AsyncMock()
    conn.set_state_callback(state_callback)
    conn.set_vdata_callback(vdata_callback)
    async with conn:
        # Initiate the command, but don't await so we can send payloads first.
        cmd_co = conn.send_command("cmd", {}, timeout=1)
        # Send a bad payload on the state queue. The client receives all payloads
        # in the same loop, so the distinction is irrelevant here.
        await state_payloads.put(
            {"app_id": "_WRONG_app_id_", "id": 1, "result": "_WRONG_result_"}
        )
        # Now send the correct payload on the command queue.
        await command_payloads.put(
            {"app_id": "_app_id_", "id": 1, "result": "_result_"}
        )
        # With all the payloads queued, we can now await.
        assert "_result_" == await cmd_co

    state_callback.assert_not_awaited()
    vdata_callback.assert_not_awaited()
