import asyncio
import json
from collections.abc import Generator
from itertools import count
from typing import Any, ClassVar
from unittest import mock
from unittest.mock import AsyncMock, Mock

import pytest
from freezegun import freeze_time

from pytboss import api, grills
from pytboss.codec import WIFI_KEY, decode, encode, timed_key
from pytboss.exceptions import (
    InvalidGrill,
    RPCError,
    Unauthorized,
    UnsupportedOperation,
)
from pytboss.transport import Transport

STATE_HEX = (
    "FE 0B 01 06 05 01 09 01 01 09 02 09 06 00 09 06 00 02 02 00 02 02 "
    "05 01 01 00 00 00 00 00 00 00 00 00 01 01 01 00 01 01 04 0C 3B 1F"
).replace(" ", "")
STATE_DICT = {
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

    def __init__(self, password: str = ""):
        self.virtual_data: dict = {}
        self.fast_updates_requested = False
        self.wifi_update_frequency: dict | None = None
        super().__init__()
        self.last_mcu_command: str | None = None
        self.device_id = "PBL-1234"
        self.wifi_credentials: dict | None = None
        self.pstate = ""
        self.wifi_scan_polls_until_done = 1
        self._wifi_scan: dict = {"scanning": False, "results": None}
        self._wifi_scan_polls = 0
        self.password = password
        self._clock = count(1.0)
        self._is_connected = False

    async def connect(self) -> None:
        self._is_connected = True

    async def disconnect(self) -> None:
        self._is_connected = False

    def is_connected(self) -> bool:
        return self._is_connected

    async def send_state(self, state: str | None) -> None:
        if self._state_callback:
            await self._state_callback(state)

    async def send_temps(self, temps: str | None) -> None:
        if self._state_callback:
            await self._state_callback(None, temps)

    async def _send_prepared_command(self, cmd: dict) -> None:
        _ = json.dumps(cmd)  # Ensure we can encode the cmd as JSON
        dispatch = {
            "PB.GetTime": self._get_time,
            "PB.GetVirtualData": self._get_virtual_data,
            "PB.WiFiAwakeWDT": self._wifi_awake_wdt,
            # Registered under the name the firmware uses -- `WiFi`, not
            # `Wifi` -- so a wrong spelling falls through to the unknown
            # method path rather than being quietly accepted.
            "PB.SetWiFiUpdateFrequency": self._set_wifi_update_frequency,
            "PB.SetVirtualData": self._set_virtual_data,
            "PB.SetDevicePassword": self._set_password,
            "PB.SendMCUCommand": self._send_mcu_command,
            "PB.GetState": self._get_state,
            "RPC.List": self._list_rpcs,
            "PBL.GetLoaderVersion": self._get_loader_version,
            "PB.RenameDevice": self._rename_device,
            "PB.SetWifiCredentials": self._set_wifi_credentials,
            "PB.DebugPState": self._debug_pstate,
            "Wifi.Scan": self._scan_wifi,
            "PBL.StartWifiScan": self._start_wifi_scan,
            "PBL.GetWifiScanStatus": self._get_wifi_scan_status,
        }
        resp = {"id": cmd["id"]}
        try:
            # Like the firmware: handlers that return null produce a reply
            # without a `result` key.
            result = dispatch[cmd["method"]](cmd["params"])
            if result is not None:
                resp["result"] = result
        except Unauthorized:
            resp["error"] = {"code": 401, "message": "Unauthorized"}
        except Exception as ex:  # noqa: BLE001
            resp["error"] = {"code": -1, "message": str(ex)}
        await self._on_command_response(resp)

    def _check_password(self, params: dict) -> None:
        if not self.password:
            return
        if "psw" not in params:
            raise Unauthorized
        psw = bytes.fromhex(params["psw"])
        if not psw:
            raise Unauthorized
        if decode(psw, key=timed_key(self._uptime())).decode() != self.password:
            raise Unauthorized

    def _uptime(self) -> float:
        return next(self._clock)

    def _get_time(self, params: dict) -> dict:
        return {"time": self._uptime()}

    def _wifi_awake_wdt(self, params: dict) -> None:
        """Password-checked, and ends in `return null` like the firmware."""
        self._check_password(params)
        self.fast_updates_requested = True

    def _set_wifi_update_frequency(self, params: dict) -> None:
        """Password-checked, and ends in `return null` like the firmware."""
        self._check_password(params)
        self.wifi_update_frequency = {k: params[k] for k in ("slow", "fast")}

    def _get_virtual_data(self, params: dict) -> dict:
        self._check_password(params)
        return dict(self.virtual_data)

    def _set_virtual_data(self, params: dict) -> None:
        """Assign the payload wholesale and return nothing, as the firmware
        does (its success path has no return statement at all)."""
        self._check_password(params)
        self.virtual_data = dict(params)

    def _set_password(self, params: dict) -> None:
        """Ends in `return null` in every vendor image."""
        self._check_password(params)
        if "newPassword" not in params:
            raise KeyError(f"newPassword not in {params}")
        self.password = decode(bytes.fromhex(params["newPassword"])).decode()

    def _send_mcu_command(self, params: dict) -> None:
        """Ends in `return null` in every vendor image."""
        self._check_password(params)
        if "command" not in params:
            raise KeyError("Command parameter missing")
        command = params["command"]
        if not command:
            raise ValueError("Empty command")
        self.last_mcu_command = bytes.fromhex(params["command"]).decode()

    def _list_rpcs(self, params: dict) -> list:
        """Answers with a bare list, as the firmware does."""
        return ["RPC.List", "RPC.Ping", "PB.GetState", "PBL.GetLoaderVersion"]

    def _get_loader_version(self, params: dict) -> dict:
        return {"loaderVersion": "0.2.2"}

    def _get_state(self, params: dict) -> dict:
        self._check_password(params)
        return {"sc_11": STATE_HEX, "sc_12": TEMPS_HEX}

    def _rename_device(self, params: dict) -> dict:
        """Keeps the prefix and rejects rather than trims, as the firmware does.

        The firmware splits on the first `-`, keeps everything up to and
        including it, and appends the name -- so the reply is a whole device
        id, not the name it was given. An empty name, or one with a leading
        or trailing space, falls through to its `Invalid parameters` error.
        """
        self._check_password(params)
        name = params.get("name")
        if not isinstance(name, str):
            raise TypeError("Invalid parameters")
        if not name or name[0] == " " or name[-1] == " ":
            raise ValueError("Invalid parameters")
        prefix = self.device_id[: self.device_id.index("-") + 1]
        self.device_id = prefix + name
        return {"newName": self.device_id}

    def _set_wifi_credentials(self, params: dict) -> None:
        """Decodes the password under the WiFi key, as the firmware does.

        A separate key from the grill password's, so decoding with the wrong
        one produces bytes rather than an error -- which is why the test
        asserts the round trip rather than that a call was made.
        """
        self._check_password(params)
        if not isinstance(params.get("ssid"), str) or not isinstance(
            params.get("pass"), str
        ):
            raise TypeError("Invalid parameters")
        self.wifi_credentials = {
            "ssid": params["ssid"],
            "pass": decode(bytes.fromhex(params["pass"]), key=WIFI_KEY).decode(),
        }

    def _debug_pstate(self, params: dict) -> dict:
        self._check_password(params)
        return {"pState": self.pstate}

    LOADER_NETWORKS: ClassVar[list[dict]] = [
        {
            "ssid": "Kitchen",
            "bssid": "12:34:56:78:90:ab",
            # `authMode`, not `auth`: the loader goes through the JS
            # `Wifi.scan()` rather than the `Wifi.Scan` RPC.
            "authMode": 3,
            "channel": 6,
            "rssi": -58,
        }
    ]

    def _start_wifi_scan(self, params: dict) -> dict:
        """Unauthenticated, and leaves a scan already running alone."""
        if self._wifi_scan["scanning"]:
            return dict(self._wifi_scan)
        self._wifi_scan = {"scanning": True, "results": None}
        self._wifi_scan_polls = 0
        return dict(self._wifi_scan)

    def _get_wifi_scan_status(self, params: dict) -> dict:
        """Destructive read, as the firmware is: it clears what it returns."""
        if self._wifi_scan["scanning"]:
            self._wifi_scan_polls += 1
            if self._wifi_scan_polls >= self.wifi_scan_polls_until_done:
                self._wifi_scan = {
                    "scanning": False,
                    "results": list(self.LOADER_NETWORKS),
                }
        if self._wifi_scan["results"] is not None:
            copy = dict(self._wifi_scan)
            self._wifi_scan["results"] = None
            return copy
        return dict(self._wifi_scan)

    def _scan_wifi(self, params: dict) -> list:
        """A bare array, in the shape Mongoose documents. Unauthenticated."""
        return [
            {
                "ssid": "Kitchen",
                "bssid": "12:34:56:78:90:ab",
                "auth": 3,
                "channel": 6,
                "rssi": -58,
            }
        ]


def make_cmd(slug: str) -> Mock:
    cmd = mock.create_autospec(grills.Command, instance=True)
    cmd.side_effect = lambda *p: f"{slug}{p}".encode().hex()
    return cmd


@pytest.fixture
def mock_control_board() -> Mock:
    ctrl = mock.create_autospec(grills.ControlBoard)
    ctrl.commands = {
        cmd: make_cmd(cmd)
        for cmd in (
            "set-temperature",
            "set-probe-1-temperature",
            "set-celsius",
            "set-fahrenheit",
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
        temp_increments=kwargs.get("temp_increments", []),
        celsius_temp_increments=kwargs.get("celsius_temp_increments", []),
        meat_probes=kwargs.get("meat_probes", 0),
    )


@pytest.fixture(params=["", "password"], ids=["no_password", "with_password"])
def password(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
async def conn(password: str) -> FakeTransport:
    return FakeTransport(password)


@pytest.fixture
async def pitboss(
    mock_get_grill: Mock, conn: FakeTransport, password: str
) -> api.PitBoss:
    pb = api.PitBoss(conn, "my-grill", password)
    await pb.start()
    return pb


async def test_init_bad_model(conn: FakeTransport):
    pitboss = api.PitBoss(conn, "unknown-model")
    with pytest.raises(InvalidGrill):
        await pitboss.start()


async def test_init_selects_the_given_control_board(conn: FakeTransport):
    """PB850DX ships on two board generations that parse differently."""
    default = api.PitBoss(conn, "PB850DX")
    await default.start()
    assert default.spec.control_board.name == "PBL3"

    pbl2 = api.PitBoss(conn, "PB850DX", control_board="PBL2")
    await pbl2.start()
    assert pbl2.spec.control_board.name == "PBL2"

    # Not merely a different label: PBL3 converts fahrenheit to celsius in its
    # temperatures routine and PBL2 does not, so resolving one as the other
    # converts a celsius reading twice.
    assert (
        default.spec.control_board._temperatures_js_func
        != pbl2.spec.control_board._temperatures_js_func
    )


async def test_init_bad_control_board(conn: FakeTransport):
    pitboss = api.PitBoss(conn, "PB850DX", control_board="PBV")
    with pytest.raises(InvalidGrill):
        await pitboss.start()


async def test_on_state_received():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    status = {}
    await pitboss.subscribe_state(lambda s: status.update(s))
    await conn.send_state(STATE_HEX)
    assert status == STATE_DICT


async def test_on_temperatures_received():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
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
    assert (await pitboss.set_grill_temperature(225)) is None
    assert conn.last_mcu_command == "set-temperature(225, True)"


@pytest.mark.grill_params({"temp_increments": [180, 200, 220]})
async def test_set_grill_temperature_high(pitboss: api.PitBoss, conn: FakeTransport):
    """Above the range, the highest accepted setpoint."""
    assert (await pitboss.set_grill_temperature(400)) is None
    assert conn.last_mcu_command == "set-temperature(220, True)"


@pytest.mark.grill_params({"temp_increments": [180, 200, 220]})
async def test_set_grill_temperature_low(pitboss: api.PitBoss, conn: FakeTransport):
    """Below the range, the lowest accepted setpoint."""
    assert (await pitboss.set_grill_temperature(100)) is None
    assert conn.last_mcu_command == "set-temperature(180, True)"


@pytest.mark.grill_params({"temp_increments": [180, 200, 220]})
async def test_set_grill_temperature_snaps_to_the_nearest(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """The board ignores anything that is not on its list."""
    assert (await pitboss.set_grill_temperature(206)) is None
    assert conn.last_mcu_command == "set-temperature(200, True)"


@pytest.mark.grill_params({"temp_increments": [180, 190, 200]})
async def test_set_grill_temperature_in_celsius(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """A grill reporting Celsius is sent Celsius, not a Fahrenheit bound."""
    pitboss._state["isFahrenheit"] = False
    # 180/190/200F floor to 82/87/93C.
    assert (await pitboss.set_grill_temperature(88)) is None
    assert conn.last_mcu_command == "set-temperature(87, False)"


@pytest.mark.grill_params(
    {"temp_increments": [180, 190], "celsius_temp_increments": [40, 45, 50]}
)
async def test_set_grill_temperature_prefers_a_declared_celsius_list(
    pitboss: api.PitBoss, conn: FakeTransport
):
    pitboss._state["isFahrenheit"] = False
    assert (await pitboss.set_grill_temperature(44)) is None
    assert conn.last_mcu_command == "set-temperature(45, False)"


async def test_set_probe_temperature(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.set_probe_temperature(225)) is None
    assert conn.last_mcu_command == "set-probe-1-temperature(225, True)"


@pytest.mark.grill_params({"has_lights": True})
async def test_turn_light_on(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.turn_light_on()) is None
    assert conn.last_mcu_command == "turn-light-on()"


async def test_turn_light_on_no_lights(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.turn_light_on()) == {}
    assert conn.last_mcu_command is None


@pytest.mark.grill_params({"has_lights": True})
async def test_turn_light_off(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.turn_light_off()) is None
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
    assert (await getattr(pitboss, method)()) is None
    assert conn.last_mcu_command == f"{slug}()"


async def test_get_state(conn: FakeTransport, password: str):
    pitboss = api.PitBoss(conn, "PBV4PS2", password)
    await pitboss.start()
    want: dict[str, Any] = STATE_DICT.copy()
    want.update(TEMPS_DICT)
    assert (await pitboss.get_state()) == want


async def test_get_uptime_is_extrapolated_between_reads(pitboss: api.PitBoss):
    """Uptime advances with the clock, so it need not be asked for again."""
    # Expressed as a fraction of the TTL so that changing the TTL cannot
    # silently turn this into a test of the re-read path instead.
    steps = [api._UPTIME_TTL / 8, api._UPTIME_TTL / 4, api._UPTIME_TTL / 2]
    with freeze_time("2025-06-01 00:00:00") as ft:
        first = await pitboss.get_uptime()
        for elapsed in steps:
            ft.tick(elapsed)
        assert await pitboss.get_uptime() == first + sum(steps)


async def test_get_uptime_extrapolation_runs_ahead_not_behind():
    """The RPC round trip biases extrapolations ahead of the grill, on purpose.

    The firmware's checkPassword accepts a key built from its current
    10-second bucket or the next one (x or x + 1, never x - 1), so an
    estimate that runs ahead is forgiven and one that runs behind is
    rejected. The cache timestamp is therefore taken before the request
    goes out; stamping it after the reply would flip the bias behind.
    """
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    with freeze_time("2025-06-01 00:00:00") as ft:

        def slow_get_time(params: dict) -> dict:
            ft.tick(5)  # the request takes 5 seconds to reach the grill
            return {"time": 100.0}

        conn._get_time = slow_get_time  # type: ignore[method-assign]
        assert await pitboss.get_uptime() == 100.0
        # The grill is at ~100s; the extrapolation must not lag behind it.
        assert await pitboss.get_uptime() >= 100.0 + 5.0


async def test_get_uptime_is_re_read_once_the_cache_is_stale(pitboss: api.PitBoss):
    """The fake grill counts up per read, so a fresh read is far lower."""
    with freeze_time("2025-06-01 00:00:00") as ft:
        first = await pitboss.get_uptime()
        ft.tick(api._UPTIME_TTL + 1)
        assert await pitboss.get_uptime() < first + api._UPTIME_TTL


async def test_is_connected():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    assert pitboss.is_connected() is False
    await pitboss.start()
    assert pitboss.is_connected() is True


async def test_stop():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    assert pitboss.is_connected() is True
    await pitboss.stop()
    assert pitboss.is_connected() is False


async def test_on_state_received_unparseable_payload():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    called = False

    async def cb(_):
        nonlocal called
        called = True

    await pitboss.subscribe_state(cb)
    # Neither a status nor a temperatures payload; nothing to parse.
    await conn.send_state(None)
    assert not called


async def test_on_state_received_async_callback():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    received = []

    async def cb(state):
        received.append(dict(state))

    await pitboss.subscribe_state(cb)
    await conn.send_state(STATE_HEX)
    assert received == [STATE_DICT]


async def test_state_callback_may_call_back_into_the_api():
    """`get_state()` takes the api lock, so dispatch must not hold it."""
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    seen = []

    async def cb(state):
        seen.append(await pitboss.get_state())

    await pitboss.subscribe_state(cb)
    await asyncio.wait_for(conn.send_state(STATE_HEX), timeout=5)
    assert len(seen) == 1


async def test_state_callback_may_subscribe_another():
    """`subscribe_state()` takes the api lock, so dispatch must not hold it."""
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()

    async def late(state):
        pass

    async def cb(state):
        await pitboss.subscribe_state(late)

    await pitboss.subscribe_state(cb)
    await asyncio.wait_for(conn.send_state(STATE_HEX), timeout=5)
    assert late in pitboss._state_callbacks


async def test_state_subscriber_with_async_call_method():
    """`iscoroutinefunction` cannot see through an `async __call__`."""

    class Subscriber:
        def __init__(self):
            self.seen = []

        async def __call__(self, state):
            self.seen.append(dict(state))

    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    subscriber = Subscriber()
    await pitboss.subscribe_state(subscriber)
    await conn.send_state(STATE_HEX)
    assert subscriber.seen == [STATE_DICT]


async def test_on_vdata_received():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    sync_received = []
    async_received = []

    async def async_cb(vdata):
        async_received.append(vdata)

    await pitboss.subscribe_vdata(lambda v: sync_received.append(v))
    await pitboss.subscribe_vdata(async_cb)

    await pitboss._on_vdata_received(json.dumps({"foo": "bar"}))

    assert sync_received == [{"foo": "bar"}]
    assert async_received == [{"foo": "bar"}]


async def test_set_probe_2_temperature_unsupported(pitboss: api.PitBoss):
    # The default mock_control_board fixture doesn't register this command.
    with pytest.raises(UnsupportedOperation):
        await pitboss.set_probe_2_temperature(120)


async def test_set_probe_2_temperature(
    pitboss: api.PitBoss, conn: FakeTransport, mock_control_board: Mock
):
    mock_control_board.commands["set-probe-2-temperature"] = make_cmd(
        "set-probe-2-temperature"
    )
    assert (await pitboss.set_probe_2_temperature(120)) is None
    assert conn.last_mcu_command == "set-probe-2-temperature(120, True)"


async def test_get_firmware_version():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    with mock.patch.object(
        conn, "send_command", AsyncMock(return_value={"version": "1.2.3"})
    ) as send_command:
        assert await pitboss.get_firmware_version() == {"version": "1.2.3"}
        send_command.assert_awaited_once_with("PB.GetFirmwareVersion", {})


async def test_set_mcu_update_timer():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    with mock.patch.object(
        conn, "send_command", AsyncMock(return_value=None)
    ) as send_command:
        assert await pitboss.set_mcu_update_timer(frequency=3) is None
        send_command.assert_awaited_once_with(
            "PB.SetMCU_UpdateFrequency", {"frequency": 3}
        )


async def test_set_wifi_update_frequency(pitboss: api.PitBoss, conn: FakeTransport):
    """Reaches the grill, rather than merely being sent.

    Driven through the fake's own handler instead of a mocked `send_command`:
    a mock asserts the name the code happens to send, which is how the
    `Wifi`/`WiFi` mismatch survived. The fake registers only the name the
    firmware does, so a wrong spelling fails here as it does on a grill.
    """
    assert await pitboss.set_wifi_update_frequency(fast=1, slow=10) is None
    assert conn.wifi_update_frequency == {"slow": 10, "fast": 1}


async def test_set_wifi_update_frequency_uses_the_name_the_firmware_registers():
    """`Wifi` is answered with "name not found"; the F is capital."""
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    with mock.patch.object(
        conn, "send_command", AsyncMock(return_value=None)
    ) as send_command:
        await pitboss.set_wifi_update_frequency(fast=1, slow=10)
        assert send_command.await_args is not None
        assert send_command.await_args.args[0] == "PB.SetWiFiUpdateFrequency"


async def test_set_virtual_data():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    with mock.patch.object(
        conn, "send_command", AsyncMock(return_value=None)
    ) as send_command:
        assert await pitboss.set_virtual_data({"foo": "bar"}) is None
        send_command.assert_awaited_once_with("PB.SetVirtualData", {"foo": "bar"})


async def test_ping():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    with mock.patch.object(
        conn, "send_command", AsyncMock(return_value={})
    ) as send_command:
        assert await pitboss.ping(timeout=5.0) == {}
        send_command.assert_awaited_once_with("RPC.Ping", {}, timeout=5.0)


async def test_list_rpcs(pitboss: api.PitBoss):
    """The reply is a bare list rather than an object."""
    assert await pitboss.list_rpcs() == [
        "RPC.List",
        "RPC.Ping",
        "PB.GetState",
        "PBL.GetLoaderVersion",
    ]


async def test_list_rpcs_when_the_grill_answers_with_nothing():
    """A grill that does not serve it must not hand back a non-list."""
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    with mock.patch.object(conn, "send_command", AsyncMock(return_value=None)):
        assert await pitboss.list_rpcs() == []


async def test_get_loader_version(pitboss: api.PitBoss):
    assert await pitboss.get_loader_version() == "0.2.2"


async def test_get_loader_version_when_the_grill_has_no_loader():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    with mock.patch.object(conn, "send_command", AsyncMock(return_value=None)):
        assert await pitboss.get_loader_version() is None


async def test_get_state_updates_the_cached_state(conn: FakeTransport, password: str):
    """A polling-only connection must still keep the cache current."""
    pitboss = api.PitBoss(conn, "PBV4PS2", password)
    await pitboss.start()
    assert pitboss._state == {}

    await pitboss.get_state()
    want: dict[str, Any] = STATE_DICT.copy()
    want.update(TEMPS_DICT)
    assert pitboss._state == want


async def test_set_temperature_unit(pitboss: api.PitBoss, conn: FakeTransport):
    assert (await pitboss.set_temperature_unit(fahrenheit=False)) is None
    assert conn.last_mcu_command == "set-celsius()"

    assert (await pitboss.set_temperature_unit(fahrenheit=True)) is None
    assert conn.last_mcu_command == "set-fahrenheit()"


async def test_a_rejected_password_raises_unauthorized():
    """The 401 has to be distinguishable without matching on the message."""
    conn = FakeTransport("thepassword")
    pitboss = api.PitBoss(conn, "PBV4PS2", "wrongpassword")
    await pitboss.start()

    with pytest.raises(Unauthorized) as caught:
        await pitboss.get_state()
    assert caught.value.code == 401

    # Still an RPCError, so anything already catching that keeps working.
    assert isinstance(caught.value, RPCError)


async def test_other_rpc_errors_keep_their_code():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()

    with pytest.raises(RPCError) as caught:
        await pitboss._conn.send_command("No.SuchMethod", {})
    assert not isinstance(caught.value, Unauthorized)
    assert caught.value.code == -1


async def test_an_error_without_a_code_is_still_an_rpc_error():
    """Nothing guarantees the device sends one."""
    assert RPCError("boom").code is None
    # And the message-only constructor still works, as does the bare
    # `Unauthorized` that `auth` and the fake transport both raise.
    assert str(RPCError("boom")) == "boom"
    assert str(Unauthorized()) == ""


@pytest.mark.grill_params({"meat_probes": 4})
async def test_set_probe_target_uses_the_board_command_when_there_is_one(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """The mock board declares `set-probe-1-temperature` and nothing else."""
    await pitboss.set_probe_target(1, 165)

    assert conn.last_mcu_command == "set-probe-1-temperature(165, True)"
    assert conn.virtual_data == {}


@pytest.mark.grill_params({"meat_probes": 4})
async def test_set_probe_target_falls_back_to_virtual_data(
    pitboss: api.PitBoss, conn: FakeTransport
):
    pitboss._state["moduleIsOn"] = True

    await pitboss.set_probe_target(2, 165)

    assert conn.virtual_data["p2T"] == 165
    assert conn.last_mcu_command is None


@pytest.mark.grill_params({"meat_probes": 4})
async def test_virtual_data_writes_preserve_what_is_already_there(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """The firmware assigns wholesale, so a write must merge client-side."""
    conn.virtual_data = {"p2T": 165}
    pitboss._state["moduleIsOn"] = True

    await pitboss.set_probe_target(3, 190)

    assert conn.virtual_data["p2T"] == 165
    assert conn.virtual_data["p3T"] == 190


@pytest.mark.grill_params({"meat_probes": 4})
async def test_virtual_data_is_always_fahrenheit(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """Whatever unit the grill itself is working in."""
    pitboss._state["moduleIsOn"] = True
    pitboss._state["isFahrenheit"] = False

    await pitboss.set_probe_target(2, 74)

    assert conn.virtual_data["p2T"] == 165
    # ...and comes back in the grill's unit, round-tripping.
    assert (await pitboss.get_probe_targets())[2] == 74


@pytest.mark.grill_params({"meat_probes": 4})
async def test_set_probe_target_refuses_while_the_grill_is_off(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """The firmware rejects the write and wipes the store at power-off."""
    pitboss._state["moduleIsOn"] = False

    with pytest.raises(UnsupportedOperation):
        await pitboss.set_probe_target(2, 165)
    assert conn.virtual_data == {}


@pytest.mark.grill_params({"meat_probes": 4})
async def test_a_reported_target_wins_over_virtual_data(
    pitboss: api.PitBoss, conn: FakeTransport
):
    conn.virtual_data = {"p1T": 200}
    pitboss._state["moduleIsOn"] = True
    pitboss._state["p1Target"] = 165

    assert (await pitboss.get_probe_targets())[1] == 165


@pytest.mark.grill_params({"meat_probes": 4})
async def test_the_password_never_reaches_the_scratchpad_as_data(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """`_authenticate` used to mutate the caller's payload."""
    pitboss._state["moduleIsOn"] = True

    payload = {"p2T": 165}
    await pitboss.set_virtual_data(payload)

    # The caller's dict is untouched...
    assert payload == {"p2T": 165}
    # ...and reads never hand the credential back as if it were data.
    assert "psw" not in await pitboss.get_virtual_data()
    # It does still reach the device, though: the firmware stores the params
    # of the last write verbatim and authentication travels in that same
    # object, so this is a protocol constraint rather than something the
    # library can avoid. Stripping it on read is what keeps it out of every
    # consumer's hands.
    if pitboss._password:
        assert "psw" in conn.virtual_data


@pytest.mark.grill_params({"meat_probes": 4})
async def test_probe_target_command_reports_the_route(pitboss: api.PitBoss):
    assert pitboss.probe_target_command(1) == "set-probe-1-temperature"
    assert pitboss.probe_target_command(2) is None
    assert pitboss.probe_target_command(4) is None


async def test_reboot():
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    with mock.patch.object(
        conn, "send_command_without_answer", AsyncMock(return_value=None)
    ) as send:
        assert await pitboss.reboot() is None
        # Without an answer: the board reboots before it could send one.
        send.assert_awaited_once_with("Sys.Reboot", {})


async def test_request_fast_updates(pitboss: api.PitBoss, conn: FakeTransport):
    """Reaches the grill, and returns nothing because the reply carries nothing.

    Driven through the fake's own handler rather than a mocked
    `send_command`: a mock returning a dict would be asserting on a reply no
    grill sends, and one returning `None` would also break `PB.GetTime`,
    which authentication needs. The fake refuses a wrong password, so the
    `with_password` case proves this call is authenticated.
    """
    await pitboss.request_fast_updates()
    assert conn.fast_updates_requested is True


async def test_turn_grill_on(pitboss: api.PitBoss, conn: FakeTransport):
    """Sent as a raw MCU command: no board declares a slug for it.

    The hex is asserted rather than the decoded command, because that is what
    identifies it -- FE0101FF against FE0102FF for off.
    """
    with mock.patch.object(
        conn, "send_command", AsyncMock(return_value=None)
    ) as send_command:
        assert (await pitboss.turn_grill_on()) is None
        assert send_command.await_args is not None
        method, params = send_command.await_args.args
        assert method == "PB.SendMCUCommand"
        assert params["command"] == "FE0101FF"


async def test_no_board_declares_a_turn_on_command():
    """The reason `turn_grill_on` cannot mirror `turn_grill_off`.

    Every model declares `turn-off`; none declares a counterpart, which is
    why this is a raw command rather than a slug.
    """
    with_off = with_on = 0
    for grill in grills.get_grills():
        commands = grill.control_board.commands
        with_off += "turn-off" in commands
        with_on += "turn-on" in commands

    assert with_off > 0
    assert with_on == 0


def test_the_turn_on_command_collides_with_nothing():
    """`FE0101FF` must not be some other board's command under a slug.

    The safety property behind sending a raw command: whatever else the
    catalogue contains, nothing already means this.
    """
    for grill in grills.get_grills():
        for slug, command in grill.control_board.commands.items():
            try:
                rendered = command()
            except TypeError:
                continue  # takes arguments; not a fixed command
            assert rendered != "FE0101FF", f"{grill.name} declares it as {slug}"


async def test_on_vdata_received_accepts_a_decoded_object():
    """`wss` hands over an object; `ble` hands over the text it was printed as.

    Virtual data reaches `wss` as a member of an already-parsed status frame,
    so it arrives decoded, while `ble` reads it off the debug log as text.
    """
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    received = []
    await pitboss.subscribe_vdata(received.append)

    await pitboss._on_vdata_received({"p1T": 165})
    await pitboss._on_vdata_received('{"p2T": 170}')

    assert received == [{"p1T": 165}, {"p2T": 170}]


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"sc_11": "", "sc_12": ""},
        {"sc_11": STATE_HEX},
        {"sc_12": TEMPS_HEX},
    ],
    ids=["empty", "blanked", "status_only", "temperatures_only"],
)
async def test_get_state_survives_a_partial_reply(reply):
    """The firmware blanks both frames while a command is in flight.

    `sendMCUCommand` clears `lastStatus.sc_11` and `sc_12` before it writes to
    the MCU and refills them from the next reply, so a poll landing in that
    window is answered with empty strings rather than an error.
    """
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    with mock.patch.object(conn, "send_command", AsyncMock(return_value=reply)):
        state = await pitboss.get_state()
    assert isinstance(state, dict)


async def test_get_uptime_does_not_cache_a_reply_it_cannot_read():
    """Caching it would keep `timed_key` wrong for the whole TTL."""
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()

    with mock.patch.object(conn, "send_command", AsyncMock(return_value={})):
        assert await pitboss.get_uptime() == 0.0

    # The next call must go back to the grill rather than extrapolate from a
    # value it never got.
    with mock.patch.object(
        conn, "send_command", AsyncMock(return_value={"time": 500.0})
    ):
        assert await pitboss.get_uptime() == 500.0


async def test_rename_device_keeps_the_prefix(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """The reply is a whole device id, not the name that was sent."""
    assert await pitboss.rename_device("Patio") == "PBL-Patio"
    assert conn.device_id == "PBL-Patio"


@pytest.mark.parametrize("name", ["", " Patio", "Patio "])
async def test_rename_device_rejects_rather_than_trims(pitboss: api.PitBoss, name: str):
    """The firmware does not trim; it falls through to its error path."""
    with pytest.raises(RPCError):
        await pitboss.rename_device(name)


async def test_rename_device_requires_the_password():
    conn = FakeTransport("thepassword")
    pitboss = api.PitBoss(conn, "PBV4PS2", "wrongpassword")
    await pitboss.start()
    with pytest.raises(Unauthorized):
        await pitboss.rename_device("Patio")
    assert conn.device_id == "PBL-1234"


async def test_set_wifi_credentials_obfuscates_the_password(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """Round trip, because the WiFi key is not the grill password's key.

    Decoding with the wrong key yields bytes rather than raising, so
    asserting the value that comes back out is what pins the key.
    """
    await pitboss.set_wifi_credentials("Kitchen", "hunter2")
    assert conn.wifi_credentials == {"ssid": "Kitchen", "pass": "hunter2"}


async def test_set_wifi_credentials_does_not_send_the_password_in_the_clear(
    pitboss: api.PitBoss, conn: FakeTransport
):
    sent: dict = {}
    original = conn._send_prepared_command

    async def capture(cmd: dict) -> None:
        sent.update(cmd)
        await original(cmd)

    with mock.patch.object(conn, "_send_prepared_command", capture):
        await pitboss.set_wifi_credentials("Kitchen", "hunter2")
    assert "hunter2" not in json.dumps(sent)


async def test_set_wifi_credentials_is_not_the_config_service_key(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """Encoding under the grill password key must not decode as the WiFi one."""
    await pitboss.set_wifi_credentials("Kitchen", "hunter2")
    assert conn.wifi_credentials is not None
    wrong = decode(bytes.fromhex(encode(b"hunter2").hex()), key=WIFI_KEY)
    assert wrong != b"hunter2"


async def test_debug_pstate(pitboss: api.PitBoss, conn: FakeTransport):
    conn.pstate = "paired"
    assert await pitboss.debug_pstate() == "paired"


async def test_debug_pstate_is_empty_when_the_cloud_never_set_it(pitboss: api.PitBoss):
    """Nothing on the grill writes it, so a never-paired grill answers empty."""
    assert await pitboss.debug_pstate() == ""


async def test_debug_pstate_requires_the_password():
    conn = FakeTransport("thepassword")
    pitboss = api.PitBoss(conn, "PBV4PS2", "wrongpassword")
    await pitboss.start()
    with pytest.raises(Unauthorized):
        await pitboss.debug_pstate()


async def test_start_wifi_scan_reports_it_began(pitboss: api.PitBoss):
    assert await pitboss.start_wifi_scan() == {"scanning": True, "results": None}


async def test_start_wifi_scan_does_not_restart_one_in_flight(
    pitboss: api.PitBoss, conn: FakeTransport
):
    conn.wifi_scan_polls_until_done = 99
    await pitboss.start_wifi_scan()
    await pitboss.get_wifi_scan_status()
    assert conn._wifi_scan_polls == 1

    await pitboss.start_wifi_scan()
    assert conn._wifi_scan_polls == 1


async def test_get_wifi_scan_status_hands_the_results_back_only_once(
    pitboss: api.PitBoss,
):
    """The firmware clears its stored results as it returns them."""
    await pitboss.start_wifi_scan()
    first = await pitboss.get_wifi_scan_status()
    assert first["results"] == FakeTransport.LOADER_NETWORKS

    second = await pitboss.get_wifi_scan_status()
    assert second["results"] is None


async def test_the_loader_names_the_auth_field_differently(pitboss: api.PitBoss):
    """`authMode` from the loader, `auth` from the `Wifi.Scan` RPC."""
    await pitboss.start_wifi_scan()
    status = await pitboss.get_wifi_scan_status()
    assert "authMode" in status["results"][0]
    assert "auth" not in status["results"][0]

    assert "auth" in (await pitboss.wifi.scan())[0]


async def test_scan_wifi_networks_drives_the_whole_dance(
    pitboss: api.PitBoss, conn: FakeTransport
):
    conn.wifi_scan_polls_until_done = 3
    got = await pitboss.scan_wifi_networks(poll_interval=0)
    assert got == FakeTransport.LOADER_NETWORKS
    assert conn._wifi_scan_polls == 3


async def test_scan_wifi_networks_gives_up_at_the_timeout(
    pitboss: api.PitBoss, conn: FakeTransport
):
    """A scan that never finishes must not poll forever."""
    conn.wifi_scan_polls_until_done = 10**6
    assert await pitboss.scan_wifi_networks(timeout=0.05, poll_interval=0.01) == []


async def test_scan_wifi_networks_starts_a_fresh_scan_each_time(pitboss: api.PitBoss):
    """A consumed result must not make the next call come back empty."""
    await pitboss.start_wifi_scan()
    await pitboss.get_wifi_scan_status()  # consumes them
    assert await pitboss.scan_wifi_networks(poll_interval=0) == (
        FakeTransport.LOADER_NETWORKS
    )


async def test_scan_wifi_networks_stops_when_the_grill_is_not_scanning():
    """Not scanning and holding nothing: no point polling to the timeout."""
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    idle = AsyncMock(return_value={"scanning": False, "results": None})
    with mock.patch.object(conn, "send_command", idle):
        assert await pitboss.scan_wifi_networks(poll_interval=0) == []
    # start + a single status poll, rather than polling to the timeout.
    assert idle.await_count == 2


async def test_on_vdata_received_strips_the_password():
    """The push path leaks what `get_virtual_data()` carefully removes.

    An authenticated `PB.SetVirtualData` hands its whole params object to the
    firmware, which stores it verbatim and pushes it back -- on the BLE debug
    log and as `data` on the websocket status frame. Every subscriber was
    handed the encoded password, and the encoding is not protection: the key
    is `timed_key(uptime)` and this library implements both ends.
    """
    conn = FakeTransport()
    pitboss = api.PitBoss(conn, "PBV4PS2")
    await pitboss.start()
    received = []
    await pitboss.subscribe_vdata(received.append)

    await pitboss._on_vdata_received({"p1T": 165, "psw": "a1b2c3deadbeef"})
    await pitboss._on_vdata_received('{"p2T": 170, "psw": "a1b2c3deadbeef"}')

    assert received == [{"p1T": 165}, {"p2T": 170}]
