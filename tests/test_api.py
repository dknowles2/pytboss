import json
from itertools import count
from typing import Generator
from unittest import mock
from unittest.mock import Mock

import pytest

from pytboss import api, grills
from pytboss.codec import decode, timed_key
from pytboss.exceptions import InvalidGrill, Unauthorized
from pytboss.transport import Transport

STATE_HEX = (
    "FE 0B 01 06 05 01 09 01 01 09 02 09 06 00 09 06 00 02 02 00 02 02 "
    "05 01 01 00 00 00 00 00 00 00 00 00 01 01 01 00 01 01 04 0C 3B 1F"
).replace(" ", "")
STATE_DICT = {
    "p1Target": 165,
    "p1Temp": 191,
    "p2Temp": 192,
    "p3Temp": None,
    "p4Temp": None,
    "smokerActTemp": 220,
    "grillSetTemp": 225,
    "isFahrenheit": True,
    "moduleIsOn": True,
    "err1": False,
    "err2": False,
    "err3": False,
    "highTempErr": False,
    "fanErr": False,
    "hotErr": False,
    "motorErr": False,
    "noPellets": False,
    "erL": False,
    "fanState": True,
    "hotState": True,
    "motorState": True,
    "lightState": False,
    "primeState": True,
    "recipeStep": 4,
    "recipeTime": 46771,
}
TEMPS_HEX = (
    "FE 0C 01 07 00 01 05 00 01 06 05 09 06 00 09 06 00 02 02 00 02 02 05 02 02 00 01"
).replace(" ", "")
TEMPS_DICT = {
    "p1Target": 170,
    "p1Temp": 150,
    "p2Temp": 165,
    "p3Temp": None,
    "p4Temp": None,
    "smokerActTemp": 220,
    "grillSetTemp": 225,
    "grillTemp": 220,
    "isFahrenheit": True,
}


class FakeTransport(Transport):
    """Fake implementation of a transport protocol."""

    _is_connected = False
    _clock = count()
    password = ""
    last_mcu_command = None

    def __init__(self, password: str = ""):
        super().__init__()
        self.password = password

    async def connect(self) -> None:
        self._is_connected = True
        return None

    async def disconnect(self) -> None:
        self._is_connected = False
        return None

    def is_connected(self) -> bool:
        return self._is_connected

    async def send_state(self, state: str | None) -> None:
        if self._state_callback:
            await self._state_callback(state, None)

    async def send_temps(self, temps: str | None) -> None:
        if self._state_callback:
            await self._state_callback(None, temps)

    async def _send_prepared_command(self, cmd: dict) -> None:
        _ = json.dumps(cmd)  # Ensure we can encode the cmd as JSON
        dispatch = {
            "PB.GetTime": self._get_time,
            "PB.GetVirtualData": self._get_virtual_data,
            "PB.SetDevicePassword": self._set_password,
            "PB.SendMCUCommand": self._send_mcu_command,
            "PB.GetState": self._get_state,
        }
        resp = {"id": cmd["id"]}
        try:
            resp["result"] = dispatch[cmd["method"]](cmd["params"])
        except Unauthorized:
            resp["error"] = {"code": 401, "message": "Unauthorized"}
        await self._on_command_response(resp)

    def _check_password(self, params: dict) -> None:
        if not self.password:
            return None
        if "psw" not in params:
            raise Unauthorized
        psw = bytes.fromhex(params["psw"])
        if not psw:
            raise Unauthorized
        if decode(psw, key=timed_key(self._uptime())).decode() != self.password:
            raise Unauthorized

    def _uptime(self) -> float:
        return float(next(self._clock))

    def _get_time(self, params: dict) -> dict:
        return {"time": self._uptime()}

    def _get_virtual_data(self, params: dict) -> dict:
        self._check_password(params)
        return {}

    def _set_password(self, params: dict) -> dict:
        self._check_password(params)
        if "newPassword" not in params:
            raise KeyError(f"newPassword not in {params}")
        self.password = decode(bytes.fromhex(params["newPassword"])).decode()
        return {}

    def _send_mcu_command(self, params: dict) -> dict:
        self._check_password(params)
        self.last_mcu_command = bytes.fromhex(params["command"]).decode()
        return {}

    def _get_state(self, params: dict) -> dict:
        self._check_password(params)
        return {"sc_11": STATE_HEX, "sc_12": TEMPS_HEX}


def make_cmd(slug: str) -> Mock:
    cmd = mock.create_autospec(grills.Command, instance=True)
    cmd.side_effect = lambda *p: f"{slug}{p}".encode("utf-8").hex()
    return cmd


@pytest.fixture
def mock_control_board() -> Mock:
    ctrl = mock.create_autospec(grills.ControlBoard)
    ctrl.commands = {
        cmd: make_cmd(cmd)
        for cmd in (
            "set-temperature",
            "set-probe-1-temperature",
            "turn-light-on",
            "turn-light-off",
            "turn-off",
            "turn-primer-motor-on",
            "turn-primer-motor-off",
        )
    }
    return ctrl


@pytest.fixture
def mock_get_grill(my_grill: grills.Grill) -> Generator[Mock, None, None]:
    with mock.patch("pytboss.api.get_grill", autospec=True) as mock_get_grill:
        mock_get_grill.return_value = my_grill
        yield mock_get_grill


@pytest.fixture
def my_grill(
    request: pytest.FixtureRequest,
    mock_control_board: Mock,
) -> grills.Grill:
    kwargs = {}
    if marker := request.node.get_closest_marker("grill_params"):
        kwargs = marker.args[0]
    return grills.Grill(
        name="my-grill",
        control_board=mock_control_board,
        min_temp=kwargs.get("min_temp", None),
        max_temp=kwargs.get("max_temp", None),
        has_lights=kwargs.get("has_lights", False),
    )


@pytest.fixture(params=["", "password"], ids=["no_password", "with_password"])
def password(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
async def conn(password: str) -> FakeTransport:
    return FakeTransport(password)


@pytest.fixture
def pitboss(mock_get_grill: Mock, conn: FakeTransport, password: str) -> api.PitBoss:
    return api.PitBoss(conn, "my-grill", password)


async def test_init_bad_model(conn: FakeTransport):
    with pytest.raises(InvalidGrill):
        _ = api.PitBoss(conn, "unknown-model")


async def test_on_state_received():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    status = {}
    await pitboss.subscribe_state(lambda s: status.update(s))
    await conn.send_state(STATE_HEX)
    assert status == STATE_DICT


async def test_on_temperatures_received():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    status = {}
    await pitboss.subscribe_state(lambda s: status.update(s))
    await conn.send_temps(TEMPS_HEX)
    assert status == TEMPS_DICT


async def test_set_password():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.set_grill_password("newpwd")
    assert conn.password == "newpwd"
    # Ensure we send the new password on subsequent actions
    await pitboss.get_virtual_data()


async def test_set_password_with_old_password():
    conn = FakeTransport("oldpwd")
    pitboss = api.PitBoss(conn, "PBV4PS2", "oldpwd")
    await pitboss.set_grill_password("newpwd")
    assert conn.password == "newpwd"
    # Ensure we send the new password on subsequent actions
    await pitboss.get_virtual_data()


async def test_set_grill_temperature(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.set_grill_temperature(225)) == {}
    assert conn.last_mcu_command == "set-temperature(225,)"


@pytest.mark.grill_params({"max_temp": 200})
async def test_set_grill_temperature_high(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.set_grill_temperature(225)) == {}
    assert conn.last_mcu_command == "set-temperature(200,)"


@pytest.mark.grill_params({"min_temp": 300})
async def test_set_grill_temperature_low(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.set_grill_temperature(225)) == {}
    assert conn.last_mcu_command == "set-temperature(300,)"


async def test_set_probe_temperature(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.set_probe_temperature(225)) == {}
    assert conn.last_mcu_command == "set-probe-1-temperature(225,)"


@pytest.mark.grill_params({"has_lights": True})
async def test_turn_light_on(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.turn_light_on()) == {}
    assert conn.last_mcu_command == "turn-light-on()"


async def test_turn_light_on_no_lights(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.turn_light_on()) == {}
    assert conn.last_mcu_command is None


@pytest.mark.grill_params({"has_lights": True})
async def test_turn_light_off(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.turn_light_off()) == {}
    assert conn.last_mcu_command == "turn-light-off()"


async def test_turn_light_off_no_lights(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.turn_light_off()) == {}
    assert conn.last_mcu_command is None


@pytest.mark.parametrize(
    "slug,method",
    [
        ("turn-off", "turn_grill_off"),
        ("turn-primer-motor-on", "turn_primer_motor_on"),
        ("turn-primer-motor-off", "turn_primer_motor_off"),
    ],
)
async def test_grill_functions(slug, method, pitboss: api.PitBoss, conn: FakeTransport):
    assert (await getattr(pitboss, method)()) == {}
    assert conn.last_mcu_command == f"{slug}()"


async def test_get_state(conn: FakeTransport, password: str):
    pitboss = api.PitBoss(conn, "PBV4PS2", password)
    want = STATE_DICT.copy()
    want.update(TEMPS_DICT)
    assert (await pitboss.get_state()) == want
