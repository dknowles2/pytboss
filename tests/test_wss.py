import asyncio
import time
from asyncio import Event, Queue, create_task, sleep, timeout
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, call, patch

from aiohttp import ClientSession
from aiohttp.test_utils import BaseTestServer, TestServer
from aiohttp.web import (
    Application,
    HTTPInternalServerError,
    Request,
    WebSocketResponse,
    get,
)
from pytest import fixture, raises

from pytboss import wss
from pytboss.exceptions import GrillUnavailable, NotConnectedError


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
                payload = await state_payloads.get()
                if isinstance(payload, str):
                    # A raw frame, for exercising malformed payloads.
                    await ws.send_str(payload)
                else:
                    await ws.send_json(payload)
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
async def session() -> AsyncGenerator[ClientSession, None]:
    # Closed here rather than left to whoever takes it: `conn` happens to
    # close it with `async with`, so a test taking `session` alone leaked
    # one, and an "Unclosed client session" on every run is how a real leak
    # goes unnoticed later.
    async with ClientSession() as client_session:
        yield client_session


@fixture
async def conn(
    fake_server: TestServer,
    session: ClientSession,
) -> AsyncGenerator[wss.WebSocketConnection, None]:
    async with fake_server, session:
        yield make_conn(fake_server, session)


def make_conn(fake_server: BaseTestServer, session: ClientSession):
    return wss.WebSocketConnection(
        "_grill_id_",
        session=session,
        base_url=str(fake_server.make_url("")),
        app_id="_app_id_",
    )


async def test_connect_disconnect(conn: wss.WebSocketConnection) -> None:
    await conn.connect()
    await conn.disconnect()


async def test_connect_server_error(session: ClientSession) -> None:
    async def handler(request: Request):
        raise HTTPInternalServerError

    app = Application()
    app.add_routes([get("/to/_grill_id_", handler)])
    async with TestServer(app) as fake_server, session:
        conn = make_conn(fake_server, session)
        with raises(GrillUnavailable):
            await conn.connect()


@patch.object(wss.WebSocketConnection, "_backoff_wait")
async def test_reconnect_backoff(mock_wait: AsyncMock, session: ClientSession) -> None:
    responses = [True, False, False, False, False, False, False, False, True]

    async def handler(request: Request):
        if responses and not responses.pop(0):
            raise HTTPInternalServerError
        ws = WebSocketResponse()
        await ws.prepare(request)
        if not responses:
            # Send a status update to trigger the done event.
            await ws.send_json({"status": ["state"]})
            async for msg in ws:
                if msg.data == "close":
                    await ws.close()
        return ws

    app = Application()
    app.add_routes([get("/to/_grill_id_", handler)])
    done = Event()

    async def state_cb(
        status_payload: str | None, temperatures_payload: str | None = None
    ) -> None:
        done.set()

    async with TestServer(app) as fake_server, session:
        conn = make_conn(fake_server, session)
        conn.set_state_callback(state_cb)
        await conn.connect()
        await done.wait()
        await conn.disconnect()
    mock_wait.assert_has_awaits(
        [call(1.0), call(2.0), call(4.0), call(8.0), call(16.0), call(30.0), call(30.0)]
    )


async def test_status(conn: wss.WebSocketConnection, state_payloads: Queue):
    state_callback = MockCallback(1)
    vdata_callback = MockCallback(0)
    conn.set_state_callback(state_callback)
    conn.set_vdata_callback(vdata_callback)
    async with conn:
        await state_payloads.put({"status": ["state"]})
        await state_callback.wait()
        state_callback.assert_awaited_once_with("state")

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


async def test_command_not_connected(conn: wss.WebSocketConnection):
    with raises(NotConnectedError):
        await conn.send_command("cmd", {}, timeout=1)


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


async def test_malformed_payload_does_not_kill_the_stream(
    conn: wss.WebSocketConnection, state_payloads: Queue
) -> None:
    state_callback = MockCallback(1)
    conn.set_state_callback(state_callback)
    async with conn:
        await state_payloads.put("this is not json")
        await state_payloads.put({"status": ["state"]})
        async with timeout(5):
            await state_callback.wait()
    state_callback.assert_awaited_once_with("state")


async def test_subscriber_exception_does_not_kill_the_stream(
    conn: wss.WebSocketConnection, state_payloads: Queue
) -> None:
    state_callback = MockCallback(2)
    state_callback.mock.side_effect = [RuntimeError("boom"), None]
    conn.set_state_callback(state_callback)
    async with conn:
        await state_payloads.put({"status": ["first"]})
        await state_payloads.put({"status": ["second"]})
        async with timeout(5):
            await state_callback.wait()
    assert state_callback.mock.await_count == 2


async def test_state_callback_may_send_commands(
    conn: wss.WebSocketConnection,
    state_payloads: Queue,
) -> None:
    """The receive loop must not hold the send lock while dispatching.

    Awaiting the *response* inside a callback can never complete -- the
    response is read by the very loop the callback is blocking -- but the
    send itself must go through rather than deadlock the stream forever.
    """
    done = Event()

    async def state_cb(
        status_payload: str | None, temperatures_payload: str | None = None
    ) -> None:
        await conn.send_command_without_answer("cmd", {})
        done.set()

    conn.set_state_callback(state_cb)
    async with conn:
        await state_payloads.put({"status": ["state"]})
        async with timeout(5):
            await done.wait()


async def test_send_when_the_socket_is_gone_raises_not_connected(
    conn: wss.WebSocketConnection,
) -> None:
    async with conn:
        sock = conn._sock
        # Simulate the subscribe loop having dropped the socket.
        conn._sock = None
        with raises(NotConnectedError):
            await conn.send_command("cmd", {}, timeout=1)
        conn._sock = sock  # so disconnect() can close it cleanly


async def test_disconnect_does_not_wait_out_the_backoff(
    fake_server: TestServer, session: ClientSession
) -> None:
    async with fake_server, session:
        conn = make_conn(fake_server, session)
        await conn.connect()
        assert conn._sock is not None
        # Make the server unreachable so the subscribe loop enters a backoff
        # sleep after the socket drops.
        with patch.object(conn, "_ws_connect", side_effect=GrillUnavailable("gone")):
            await conn._sock.close()
            await sleep(0.2)  # let the loop reach the backoff wait
            # The first backoff is 1.0s; disconnect() must not wait it out.
            async with timeout(0.5):
                await conn.disconnect()


async def test_external_session_not_closed():
    """Test that external sessions are not closed when disconnect is called."""
    # Create an external session
    external_session = ClientSession()

    # Create a connection with the external session
    conn = wss.WebSocketConnection("_grill_id_", session=external_session)

    # Disconnect should not close the external session
    await conn.disconnect()

    # The external session should still be open
    assert not external_session.closed

    # Clean up
    await external_session.close()


async def test_status_no_state_callback(conn: wss.WebSocketConnection) -> None:
    # No state callback registered; the payload should be silently ignored.
    await conn._handle_message({"status": ["state"]})


async def test_vdata_arrives_on_the_status_push(
    conn: wss.WebSocketConnection,
) -> None:
    """The shape the firmware actually sends.

    `sendWSStatus` builds one object and attaches virtual data to it, so
    `data` rides along with `status` rather than arriving on a frame of its
    own -- and the state callback still has to run for the same payload.
    """
    vdata_callback = AsyncMock()
    state_callback = AsyncMock()
    conn.set_vdata_callback(vdata_callback)
    conn.set_state_callback(state_callback)

    await conn._handle_message(
        {"id": -1, "src": "PBx", "status": ["FE0B00"], "data": {"p1T": 165}}
    )

    vdata_callback.assert_awaited_once_with({"p1T": 165})
    state_callback.assert_awaited_once_with("FE0B00")


async def test_status_push_without_vdata(conn: wss.WebSocketConnection) -> None:
    """`data` is attached only when there is something to send."""
    vdata_callback = AsyncMock()
    conn.set_vdata_callback(vdata_callback)
    conn.set_state_callback(AsyncMock())

    await conn._handle_message({"id": -1, "status": ["FE0B00"]})

    vdata_callback.assert_not_awaited()


async def test_vdata_with_no_callback_registered(
    conn: wss.WebSocketConnection,
) -> None:
    # No vdata callback; the state half of the payload must still be handled.
    state_callback = AsyncMock()
    conn.set_state_callback(state_callback)
    await conn._handle_message({"status": ["FE0B00"], "data": {"p1T": 165}})
    state_callback.assert_awaited_once_with("FE0B00")


async def test_internal_session_closed():
    """An owned session is closed and released on disconnect.

    It is now created by `connect()` rather than by the constructor, so a
    transport that was never connected has none to close -- and building one
    no longer leaves an aiohttp session behind for the caller to clean up.
    """
    conn = wss.WebSocketConnection("_grill_id_")
    assert conn._session is None

    session = ClientSession()
    conn._session = session
    await conn.disconnect()

    assert session.closed
    assert conn._session is None


async def test_the_transport_is_reusable_after_disconnect(fake_server: TestServer):
    """A transport that has been disconnected can be connected again.

    `disconnect()` used to close a session the constructor had built and
    never rebuild it, so a second `connect()` failed on a closed session.
    """
    async with fake_server:
        conn = wss.WebSocketConnection(
            "_grill_id_", base_url=str(fake_server.make_url("")), app_id="_app_id_"
        )
        await conn.connect()
        await conn.disconnect()

        await conn.connect()
        assert conn._session is not None
        assert not conn._session.closed
        await conn.disconnect()


async def test_a_closed_caller_session_is_reported(fake_server: TestServer):
    """A session the caller owns is theirs to manage; we do not rebuild it."""
    async with fake_server:
        session = ClientSession()
        conn = wss.WebSocketConnection(
            "_grill_id_",
            session=session,
            base_url=str(fake_server.make_url("")),
            app_id="_app_id_",
        )
        await session.close()

        with raises(NotConnectedError):
            await conn.connect()


async def test_connecting_twice_does_not_strand_the_first_task(
    fake_server: TestServer,
):
    """The second `connect()` used to overwrite `_subscribe_task`."""
    async with fake_server:
        conn = wss.WebSocketConnection(
            "_grill_id_", base_url=str(fake_server.make_url("")), app_id="_app_id_"
        )
        await conn.connect()
        first = conn._subscribe_task

        await conn.connect()

        assert first is not None
        assert first.done()
        assert conn._subscribe_task is not first
        await conn.disconnect()


async def test_disconnect_clears_the_subscribed_flag(fake_server: TestServer):
    """The flag means "the loop is reading", and after a disconnect it is not.

    Left set, the next `connect()` returns from its wait before the new
    subscribe task has started reading.
    """
    async with fake_server:
        conn = wss.WebSocketConnection(
            "_grill_id_", base_url=str(fake_server.make_url("")), app_id="_app_id_"
        )
        await conn.connect()
        assert conn._subscribed.is_set()

        await conn.disconnect()

        assert not conn._subscribed.is_set()


async def test_concurrent_connects_leave_one_live_socket(
    session: ClientSession,
) -> None:
    """Both callers pass the wind-down check while the task is still None.

    Unserialized, both open a socket and the second assignment orphans the
    first along with its task, so `disconnect()` can no longer reach it and
    the session to the relay stays open for the life of the process.
    """
    sockets: list[WebSocketResponse] = []

    async def handler(request: Request):
        ws = WebSocketResponse()
        await ws.prepare(request)
        sockets.append(ws)
        async for _ in ws:
            pass
        return ws

    app = Application()
    app.add_routes([get("/to/_grill_id_", handler)])
    async with TestServer(app) as fake_server, session:
        conn = make_conn(fake_server, session)
        await asyncio.gather(conn.connect(), conn.connect())
        await conn.disconnect()

    assert [ws for ws in sockets if not ws.closed] == []


async def test_disconnect_does_not_wait_out_a_handshake(
    session: ClientSession,
) -> None:
    """`ws_connect` is the one await neither flag reaches."""
    conn = wss.WebSocketConnection("_grill_id_", session=session)
    started = Event()

    async def never_finishes():
        started.set()
        await sleep(30)
        raise AssertionError("unreachable")

    conn._ws_connect = never_finishes  # type: ignore[method-assign]
    conn._keep_running = True
    conn._sock = None
    conn._subscribe_task = conn._loop.create_task(conn._subscribe())
    await started.wait()

    started_stopping = time.monotonic()
    async with timeout(5):
        await conn.disconnect()
    # Asserted on the clock, not only by the timeout above: a version that
    # waits out the handshake absorbs that timeout and returns normally, so
    # the bound alone cannot tell the two apart.
    assert time.monotonic() - started_stopping < 1
    assert conn.is_connected() is False


async def test_a_handshake_that_beats_the_wind_down_is_not_served(
    session: ClientSession,
) -> None:
    """The cancel usually wins the race, but it does not have to.

    Driven directly rather than raced: the loop is entered with the stop
    already asked for, which is the state the losing cancel leaves behind.
    """
    conn = wss.WebSocketConnection("_grill_id_", session=session)
    sock = AsyncMock()
    sock.closed = False

    async def hands_over_a_socket_after_the_stop():
        # The order arrives mid-handshake and the cancel loses the race, so
        # the loop is handed a live socket it was told not to want.
        conn._keep_running = False
        return sock

    conn._ws_connect = hands_over_a_socket_after_the_stop  # type: ignore[method-assign]
    conn._keep_running = True
    conn._sock = None

    await conn._subscribe()

    sock.close.assert_awaited_once()
    assert conn._sock is None


async def test_stopping_from_a_dispatched_callback_says_so(
    session: ClientSession,
) -> None:
    """Awaiting the subscribe task from inside itself never returns.

    asyncio raises here on its own, but only once the await is attempted and
    with a message about a task awaiting itself. Naming the actual mistake
    saves the reader working back to it.
    """
    conn = wss.WebSocketConnection("_grill_id_", session=session)
    conn._subscribe_task = asyncio.current_task()

    with raises(RuntimeError, match="Cannot stop this transport"):
        await conn._stop_subscribing()


async def test_disconnect_still_fails_commands_in_flight(
    conn: wss.WebSocketConnection,
) -> None:
    """The reason the subscribe task is awaited rather than cancelled.

    Cancelling it would skip `_fail_pending_commands()` below the read loop,
    and every in-flight command would wait out its whole timeout instead.
    """
    async with conn:
        task = create_task(conn.send_command("PB.GetState", {}, timeout=30))
        await sleep(0.1)  # let it reach the wire
        await conn.disconnect()
        async with timeout(3):
            with raises(NotConnectedError):
                await task


async def test_a_cancellation_we_did_not_ask_for_is_not_swallowed(
    session: ClientSession,
) -> None:
    """`_keep_running` cannot say who cancelled.

    The wind-down clears it before cancelling, so during a disconnect it
    reads False whoever delivered the cancellation -- and a caller's timeout
    landing in that window would be turned into a clean return, telling
    someone who asked for a bound that everything went fine.
    """
    conn = wss.WebSocketConnection("_grill_id_", session=session)
    started = Event()

    async def never_finishes():
        started.set()
        await sleep(30)
        raise AssertionError("unreachable")

    conn._ws_connect = never_finishes  # type: ignore[method-assign]
    conn._keep_running = True
    conn._sock = None
    task = conn._loop.create_task(conn._subscribe())
    await started.wait()

    # A wind-down has flipped the flag, but the cancellation below is not
    # the one it delivers.
    conn._keep_running = False
    task.cancel()

    with raises(asyncio.CancelledError):
        await task
