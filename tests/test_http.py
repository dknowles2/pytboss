"""Tests for the local HTTP transport.

Driven against a real aiohttp server standing in for the firmware's `/rpc`
endpoint rather than a mocked session, so the JSON envelope, the status codes
and the content type are exercised as the grill would present them.
"""

import asyncio
from typing import Any
from unittest import mock

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from pytboss.config import Config
from pytboss.exceptions import NotConnectedError, RPCError, Unauthorized
from pytboss.http import HttpConnection
from pytboss.transport import Transport


class FakeMongooseRpc:
    """A stand-in for the firmware's `/rpc` endpoint.

    Mirrors the behaviours that matter: it echoes the request id, omits
    `result` for handlers that return nothing, and answers `Unauthorized` the
    way `checkPassword` does.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.password = ""
        self.content_type: str | None = None  # None => text/plain, as Mongoose does
        self.echo_id: bool = True
        self.status = 200

    async def handle(self, request: web.Request) -> web.Response:
        cmd = await request.json()
        self.requests.append(cmd)
        if self.status != 200:
            return web.Response(status=self.status, text="nope")

        resp: dict[str, Any] = {"id": cmd["id"] if self.echo_id else 9999}
        method = cmd["method"]
        params = cmd.get("params") or {}

        if self.password and method.startswith("PB.") and "psw" not in params:
            resp["error"] = {"code": 401, "message": "Unauthorized"}
        elif method == "RPC.Ping":
            resp["result"] = {}
        elif method == "PB.GetState":
            resp["result"] = {"sc_11": "FE0B", "sc_12": "FE0C"}
        elif method == "PB.WiFiAwakeWDT":
            pass  # Returns null: no `result` key at all.
        elif method == "Boom":
            resp["error"] = {"code": -1, "message": "kaboom"}
        else:
            resp["result"] = {"echo": method}
        return web.json_response(resp, content_type=self.content_type or "text/plain")


@pytest.fixture
async def rpc() -> FakeMongooseRpc:
    return FakeMongooseRpc()


@pytest.fixture
async def server(rpc: FakeMongooseRpc):
    app = web.Application()
    app.router.add_post("/rpc", rpc.handle)
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()


@pytest.fixture
def host(server: TestServer) -> str:
    return f"{server.host}:{server.port}"


async def test_connect_pings_and_reports_connected(host, rpc):
    conn = HttpConnection(host)
    assert not conn.is_connected()
    await conn.connect()
    try:
        assert conn.is_connected()
        assert rpc.requests[0]["method"] == "RPC.Ping"
    finally:
        await conn.disconnect()


async def test_send_command_round_trips(host):
    conn = HttpConnection(host)
    await conn.connect()
    try:
        assert await conn.send_command("PB.GetState", {}) == {
            "sc_11": "FE0B",
            "sc_12": "FE0C",
        }
    finally:
        await conn.disconnect()


async def test_result_less_reply_is_not_an_error(host):
    """`PB.WiFiAwakeWDT` returns null; the absence of `result` is a success."""
    conn = HttpConnection(host)
    await conn.connect()
    try:
        assert await conn.send_command("PB.WiFiAwakeWDT", {}) is None
    finally:
        await conn.disconnect()


async def test_error_payload_raises(host):
    conn = HttpConnection(host)
    await conn.connect()
    try:
        with pytest.raises(RPCError, match="kaboom"):
            await conn.send_command("Boom", {})
    finally:
        await conn.disconnect()


async def test_unauthorized_is_raised_as_unauthorized(host, rpc):
    """A password-checked method without `psw` fails the way the firmware fails it."""
    rpc.password = "hunter2"
    conn = HttpConnection(host)
    await conn.connect()  # RPC.Ping is not password-checked.
    try:
        with pytest.raises(Unauthorized):
            await conn.send_command("PB.GetState", {})
    finally:
        await conn.disconnect()


async def test_a_reply_with_the_wrong_id_still_resolves(host, rpc):
    """An HTTP reply belongs to its request, so a bad echo must not stall it."""
    rpc.echo_id = False
    conn = HttpConnection(host)
    await conn.connect()
    try:
        async with asyncio.timeout(5):
            assert await conn.send_command("PB.GetState", {}) == {
                "sc_11": "FE0B",
                "sc_12": "FE0C",
            }
    finally:
        await conn.disconnect()


async def test_json_content_type_is_also_accepted(host, rpc):
    rpc.content_type = "application/json"
    conn = HttpConnection(host)
    await conn.connect()
    try:
        assert await conn.send_command("PB.GetState", {}) is not None
    finally:
        await conn.disconnect()


async def test_http_error_status_raises_rpc_error(host, rpc):
    conn = HttpConnection(host)
    await conn.connect()
    rpc.status = 404
    try:
        with pytest.raises(RPCError, match="HTTP 404"):
            await conn.send_command("PB.GetState", {})
    finally:
        await conn.disconnect()


async def test_connect_fails_when_nothing_answers(server):
    """A grill with `http.enable` off looks like this: nothing on the port."""
    port = server.port
    await server.close()
    conn = HttpConnection(f"127.0.0.1:{port}")
    with pytest.raises(NotConnectedError):
        await conn.connect()
    assert not conn.is_connected()


async def test_an_unreachable_grill_reports_not_connected(host, server):
    conn = HttpConnection(host)
    await conn.connect()
    assert conn.is_connected()
    await server.close()
    with pytest.raises(NotConnectedError):
        await conn.send_command("PB.GetState", {})
    assert not conn.is_connected()
    await conn.disconnect()


async def test_the_transport_is_reusable_after_disconnect(host):
    """Reconnecting must work; an owned session is rebuilt rather than reused."""
    conn = HttpConnection(host)
    await conn.connect()
    await conn.disconnect()
    assert not conn.is_connected()
    await conn.connect()
    try:
        assert conn.is_connected()
        assert await conn.send_command("PB.GetState", {}) is not None
    finally:
        await conn.disconnect()


async def test_a_caller_supplied_session_is_left_open(host):
    session = ClientSession()
    try:
        conn = HttpConnection(host, session=session)
        await conn.connect()
        await conn.disconnect()
        assert not session.closed
        # And it can still be used again.
        await conn.connect()
        assert await conn.send_command("PB.GetState", {}) is not None
        await conn.disconnect()
    finally:
        await session.close()


async def test_state_callbacks_never_fire(host):
    """There is no push channel; the callbacks exist but stay silent."""
    seen: list = []

    async def on_state(status=None, temperatures=None):
        seen.append((status, temperatures))

    conn = HttpConnection(host)
    conn.set_state_callback(on_state)
    await conn.connect()
    try:
        await conn.send_command("PB.GetState", {})
        assert seen == []
    finally:
        await conn.disconnect()


async def test_a_silent_grill_is_reported_as_not_connected():
    """Unreachable and unresponsive are the same thing to a caller.

    A refused connection raises `ClientError`, but a host that answers
    nothing times out instead, and `ClientTimeout` surfaces that as
    `asyncio.TimeoutError` -- which is not a `ClientError`. Silence is the
    common case: a grill that is unplugged, asleep or on another network does
    not send a reset.
    """

    async def never_answers(request: web.Request) -> web.Response:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    app = web.Application()
    app.router.add_post("/rpc", never_answers)
    server = TestServer(app)
    await server.start_server()
    try:
        conn = HttpConnection(f"{server.host}:{server.port}", timeout=0.1)
        with pytest.raises(NotConnectedError):
            await conn.connect()
        assert conn.is_connected() is False
    finally:
        await server.close()


async def _documented_discovery(config: Config) -> bool:
    """The snippet from this module's docstring, verbatim.

    Kept executable rather than only rendered: it is what decides whether a
    caller offers the local option at all, so it has to work against grills
    that have no `http` section as well as those that do.
    """
    try:
        http = await config.get_config("http")
    except RPCError:
        http = {}
    return bool(http.get("enable"))


@pytest.mark.parametrize(
    "reply,want",
    [
        ({"enable": True}, True),
        ({"enable": False}, False),
        ({}, False),  # answered with nothing: no such section
    ],
)
async def test_documented_discovery_answers_for_every_grill(reply, want):
    conn = mock.AsyncMock(spec=Transport)
    conn.send_command.return_value = reply
    assert await _documented_discovery(Config(conn)) is want


async def test_documented_discovery_survives_a_missing_section():
    """A grill that errors on an unknown key must not raise past the caller.

    The ESP-IDF firmware answers `Config.Get`, but its schema has no `http`
    section, so this is the other shape the same question can fail in.
    """
    conn = mock.AsyncMock(spec=Transport)
    conn.send_command.side_effect = RPCError("No such key", -1)
    assert await _documented_discovery(Config(conn)) is False


async def test_a_command_after_disconnect_is_refused(host, rpc):
    """A caller's session stays open, so nothing else stops the request.

    Home Assistant always supplies one, so this is the ordinary case: a
    refresh still in flight when the entry unloads would otherwise keep
    polling the grill successfully.
    """
    async with ClientSession() as session:
        conn = HttpConnection(host, session=session)
        await conn.connect()
        sent = len(rpc.requests)
        await conn.disconnect()

        with pytest.raises(NotConnectedError):
            await conn.send_command("PB.GetState", {})

        assert len(rpc.requests) == sent
        assert conn.is_connected() is False


async def test_a_command_before_connect_is_refused(host, rpc):
    """`connect()` is what checks the grill is there at all."""
    async with ClientSession() as session:
        conn = HttpConnection(host, session=session)
        with pytest.raises(NotConnectedError):
            await conn.send_command("PB.GetState", {})
        assert rpc.requests == []


async def test_a_failed_request_does_not_end_the_transport(host, rpc):
    """Unlike a disconnect, a grill that goes away is expected to come back.

    The class promises exactly this: no reconnect loop, because each call is
    independent and one that fails does not stop the next from working.
    """
    conn = HttpConnection(host)
    await conn.connect()
    try:
        rpc.status = 503
        with pytest.raises(RPCError):
            await conn.send_command("PB.GetState", {})

        rpc.status = 200
        assert await conn.send_command("PB.GetState", {}) == {
            "sc_11": "FE0B",
            "sc_12": "FE0C",
        }
        assert conn.is_connected() is True
    finally:
        await conn.disconnect()


async def test_send_command_without_answer_does_not_wait(host, rpc):
    """`reboot()` picks this method because the board cannot answer.

    A request is only finished here when its response has been read, so
    awaiting the exchange means waiting out the whole timeout and then
    raising at a caller that is not expecting an exception.
    """
    hung = asyncio.Event()

    async def never_answers(request: web.Request) -> web.Response:
        hung.set()
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    app = web.Application()
    app.router.add_post("/rpc", never_answers)
    server = TestServer(app)
    await server.start_server()
    try:
        conn = HttpConnection(f"{server.host}:{server.port}", timeout=5)
        # Connected by hand: `connect()` pings, and this server never
        # answers anything, which is the whole point of it.
        conn._session = ClientSession()
        conn._started = True
        try:
            async with asyncio.timeout(2):
                await conn.send_command_without_answer("Sys.Reboot", {})
            # The request is on its way even though we did not wait for it.
            async with asyncio.timeout(2):
                await hung.wait()
        finally:
            # Owns the session it was given here, so this closes it too.
            await conn.disconnect()
    finally:
        await server.close()


async def test_disconnect_stops_a_request_still_in_flight(host, rpc):
    conn = HttpConnection(host)
    await conn.connect()
    await conn.send_command_without_answer("Sys.Reboot", {})
    assert conn._pending

    await conn.disconnect()
    assert conn._pending == set()


async def test_a_non_json_body_is_reported_as_an_rpc_error():
    """A mistyped address reaching something else answers like this.

    `JSONDecodeError` is a `ValueError`, so the `except (ClientError,
    TimeoutError)` in the transport does not see it and it escapes
    undocumented.
    """

    async def html(request: web.Request) -> web.Response:
        return web.Response(text="<html>router admin</html>", content_type="text/html")

    app = web.Application()
    app.router.add_post("/rpc", html)
    server = TestServer(app)
    await server.start_server()
    try:
        conn = HttpConnection(f"{server.host}:{server.port}")
        with pytest.raises(RPCError):
            await conn.connect()
    finally:
        await server.close()


async def test_http_401_arrives_as_unauthorized(host, rpc):
    """A build with `rpc.auth_file` set refuses at the HTTP layer.

    Callers catch `Unauthorized` to re-prompt for a password; a bare
    `RPCError` reads as a generic failure instead.
    """
    conn = HttpConnection(host)
    await conn.connect()
    try:
        rpc.status = 401
        with pytest.raises(Unauthorized):
            await conn.send_command("PB.GetState", {})
    finally:
        await conn.disconnect()


async def test_is_connected_stops_lying_when_the_grill_goes_silent():
    """A grill that answered once and then went quiet must read as gone.

    At the default configuration the RPC deadline fires before aiohttp's, so
    the request is cancelled rather than timing out inside the `except` that
    clears `_connected` -- and `is_connected()` kept answering `True`,
    indefinitely, for a grill that stopped answering.
    """
    calls = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        cmd = await request.json()
        if calls == 1:
            return web.json_response(
                {"id": cmd["id"], "result": {}}, content_type="text/plain"
            )
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    app = web.Application()
    app.router.add_post("/rpc", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        # aiohttp's deadline is high so the RPC one below beats it, the same
        # order the defaults produce (30.0 vs 30.0, ceil-rounded up).
        conn = HttpConnection(f"{server.host}:{server.port}", timeout=5)
        await conn.connect()
        assert conn.is_connected()

        with pytest.raises(TimeoutError):
            await conn.send_command("PB.GetState", {}, timeout=0.1)

        assert not conn.is_connected()
        await conn.disconnect()
    finally:
        await server.close()


async def test_a_cancelled_connect_rolls_back():
    """A config flow timing out around `connect()` cancels it.

    `except Exception` does not catch `CancelledError`, so the rollback was
    skipped entirely: the transport kept claiming to be connected and the
    owned session it had just opened leaked, once per retry.
    """

    async def never_answers(request: web.Request) -> web.Response:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    app = web.Application()
    app.router.add_post("/rpc", never_answers)
    server = TestServer(app)
    await server.start_server()
    try:
        conn = HttpConnection(f"{server.host}:{server.port}")
        task = asyncio.create_task(conn.connect())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not conn.is_connected()
        assert conn._session is None  # the owned session was closed, not leaked
    finally:
        await server.close()


async def test_connect_maps_a_bare_timeout_to_the_documented_error(host):
    """`connect()` promises `NotConnectedError` when nothing answers.

    At `send_command`'s own default deadline the RPC timeout beats aiohttp's,
    so the unreachable-grill mapping in `_send_prepared_command` never runs
    and the caller got a bare `TimeoutError` instead.
    """
    conn = HttpConnection(host)
    with (
        mock.patch.object(
            conn, "send_command", mock.AsyncMock(side_effect=TimeoutError)
        ),
        pytest.raises(NotConnectedError),
    ):
        await conn.connect()
    assert not conn.is_connected()


async def test_fire_and_forget_on_a_dead_transport_says_so(host):
    """The three transports must agree on what a doomed send does.

    BLE and WSS raise `NotConnectedError` synchronously from
    `send_command_without_answer`; here the guard lived inside the
    background task, where `_send_and_forget` swallowed it -- so
    `PitBoss.reboot()` over HTTP reported success on a transport that was
    never connected, or had been disconnected.
    """
    conn = HttpConnection(host)

    # Never connected.
    with pytest.raises(NotConnectedError):
        await conn.send_command_without_answer("Sys.Reboot", {})

    # Connected, then explicitly disconnected.
    await conn.connect()
    await conn.disconnect()
    with pytest.raises(NotConnectedError):
        await conn.send_command_without_answer("Sys.Reboot", {})


async def test_fire_and_forget_still_swallows_the_expected_silence(rpc, host):
    """A board that reboots before answering stays a clean ending."""
    conn = HttpConnection(host)
    await conn.connect()
    rpc.status = 500  # the in-flight request fails; nobody is waiting
    await conn.send_command_without_answer("Sys.Reboot", {})
    for task in list(conn._pending):
        await asyncio.gather(task, return_exceptions=True)
    await conn.disconnect()
