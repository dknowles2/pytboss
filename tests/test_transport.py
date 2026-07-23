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
