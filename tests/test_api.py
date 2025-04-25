from unittest import mock
from unittest.mock import AsyncMock, Mock

import pytest

from pytboss import api, grills
from pytboss.exceptions import InvalidGrill
from pytboss.transport import Transport


@pytest.fixture
def mock_conn() -> AsyncMock:
    return mock.create_autospec(Transport)


@pytest.fixture
def mock_cmd() -> Mock:
    return mock.create_autospec(grills.Command, instance=True)


@pytest.fixture
def mock_control_board() -> Mock:
    return mock.create_autospec(grills.ControlBoard)


@pytest.fixture
def mock_get_grill():
    with mock.patch("pytboss.api.get_grill", autospec=True) as mock_get_grill:
        yield mock_get_grill


class TestApi:
    def test_init(self, mock_conn):
        _ = api.PitBoss(mock_conn, "PBV4PS2")

    def test_init_bad_model(self, mock_conn):
        with pytest.raises(InvalidGrill):
            _ = api.PitBoss(mock_conn, "unknown-model")

    async def test_on_state_received(self, mock_conn):
        pitboss = api.PitBoss(mock_conn, "PBV4PS2")
        status = {}

        def cb(s):
            status.update(s)

        await pitboss.subscribe_state(cb)
        data = (
            "FE 0B 01 06 05 01 09 01 01 09 02 09 06 00 09 06 00 02 02 00 02 02 "
            "05 01 01 00 00 00 00 00 00 00 00 00 01 01 01 00 01 01 04 0C 3B 1F"
        ).split()
        await pitboss._on_state_received("".join(data), None)

        assert status == {
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

    async def test_on_temperatures_received(self, mock_conn):
        pitboss = api.PitBoss(mock_conn, "PBV4PS2")
        status = {}

        def cb(s):
            status.update(s)

        await pitboss.subscribe_state(cb)
        data = (
            "FE 0C 01 07 00 01 05 00 01 06 05 09 06 00 "
            "09 06 00 02 02 00 02 02 05 02 02 00 01"
        ).split()
        await pitboss._on_state_received(None, "".join(data))
        assert status == {
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

    async def test_set_password(
        self, mock_conn: AsyncMock, mock_get_grill: Mock, mock_control_board: Mock
    ):
        grill = grills.Grill(name="my-grill", control_board=mock_control_board)
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_conn.send_command.return_value = {}
        await pitboss.set_grill_password("_new-password_")
        mock_conn.send_command.assert_awaited_once_with(
            "PB.SetDevicePassword", {"newPassword": "5f6e65772d70617373776f72645f"}
        )

    async def test_set_password_with_old_password(
        self, mock_conn: AsyncMock, mock_get_grill: Mock, mock_control_board: Mock
    ):
        grill = grills.Grill(name="my-grill", control_board=mock_control_board)
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill", "_old-password_")
        mock_conn.send_command.return_value = {}
        await pitboss.set_grill_password("_new-password_")
        
        # Check that a call was made to SetDevicePassword with the correct new password
        found = False
        for call in mock_conn.send_command.await_args_list:
            args, kwargs = call
            if (args[0] == "PB.SetDevicePassword" and 
                isinstance(args[1], dict) and
                args[1].get("newPassword") == "5f6e65772d70617373776f72645f"):
                found = True
                break
        
        assert found, "No call to SetDevicePassword with the correct new password found"

    async def test_set_grill_temperature(
        self, mock_conn, mock_get_grill, mock_control_board, mock_cmd
    ):
        grill = grills.Grill(name="my-grill", control_board=mock_control_board)
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_control_board.commands = {"set-temperature": mock_cmd}
        mock_cmd.side_effect = ["HEXCMD"]
        mock_conn.send_command.return_value = {}
        assert (await pitboss.set_grill_temperature(225)) == {}

        mock_cmd.assert_called_once_with(225)
        mock_conn.send_command.assert_awaited_once_with(
            "PB.SendMCUCommand", {"command": "HEXCMD"}
        )

    async def test_set_grill_temperature_high(
        self, mock_conn, mock_get_grill, mock_control_board, mock_cmd
    ):
        grill = grills.Grill(
            name="my-grill",
            control_board=mock_control_board,
            max_temp=200,
        )
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_control_board.commands = {"set-temperature": mock_cmd}
        mock_cmd.side_effect = ["HEXCMD"]
        mock_conn.send_command.return_value = {}
        assert (await pitboss.set_grill_temperature(225)) == {}

        mock_cmd.assert_called_once_with(200)
        mock_conn.send_command.assert_awaited_once_with(
            "PB.SendMCUCommand", {"command": "HEXCMD"}
        )

    async def test_set_grill_temperature_low(
        self, mock_conn, mock_get_grill, mock_control_board, mock_cmd
    ):
        grill = grills.Grill(
            name="my-grill",
            control_board=mock_control_board,
            min_temp=300,
        )
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_control_board.commands = {"set-temperature": mock_cmd}
        mock_cmd.side_effect = ["HEXCMD"]
        mock_conn.send_command.return_value = {}
        assert (await pitboss.set_grill_temperature(225)) == {}

        mock_cmd.assert_called_once_with(300)
        mock_conn.send_command.assert_awaited_once_with(
            "PB.SendMCUCommand", {"command": "HEXCMD"}
        )

    async def test_set_probe_temperature(
        self, mock_conn, mock_get_grill, mock_control_board, mock_cmd
    ):
        grill = grills.Grill(name="my-grill", control_board=mock_control_board)
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_control_board.commands = {"set-probe-1-temperature": mock_cmd}
        mock_cmd.side_effect = ["HEXCMD"]
        mock_conn.send_command.return_value = {}
        assert (await pitboss.set_probe_temperature(225)) == {}

        mock_cmd.assert_called_once_with(225)
        mock_conn.send_command.assert_awaited_once_with(
            "PB.SendMCUCommand", {"command": "HEXCMD"}
        )

    async def test_turn_light_on(
        self, mock_conn, mock_get_grill, mock_control_board, mock_cmd
    ):
        grill = grills.Grill(
            name="my-grill", control_board=mock_control_board, has_lights=True
        )
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_control_board.commands = {"turn-light-on": mock_cmd}
        mock_cmd.side_effect = ["HEXCMD"]
        mock_conn.send_command.return_value = {}
        assert (await pitboss.turn_light_on()) == {}

        mock_cmd.assert_called_once()
        mock_conn.send_command.assert_awaited_once_with(
            "PB.SendMCUCommand", {"command": "HEXCMD"}
        )

    async def test_turn_light_on_no_lights(
        self, mock_conn, mock_get_grill, mock_control_board, mock_cmd
    ):
        grill = grills.Grill(
            name="my-grill", control_board=mock_control_board, has_lights=False
        )
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_control_board.commands = {"turn-light-on": mock_cmd}
        mock_cmd.side_effect = ["HEXCMD"]
        mock_conn.send_command.return_value = {}
        assert (await pitboss.turn_light_on()) == {}

        mock_cmd.assert_not_called()
        mock_conn.send_command.assert_not_awaited()

    async def test_turn_light_off(
        self, mock_conn, mock_get_grill, mock_control_board, mock_cmd
    ):
        grill = grills.Grill(
            name="my-grill", control_board=mock_control_board, has_lights=True
        )
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_control_board.commands = {
            "turn-light-off": mock_cmd,
        }
        mock_cmd.side_effect = ["HEXCMD"]
        mock_conn.send_command.return_value = {}
        assert (await pitboss.turn_light_off()) == {}

        mock_cmd.assert_called_once()
        mock_conn.send_command.assert_awaited_once_with(
            "PB.SendMCUCommand", {"command": "HEXCMD"}
        )

    async def test_turn_light_off_no_lights(
        self, mock_conn, mock_get_grill, mock_control_board, mock_cmd
    ):
        grill = grills.Grill(
            name="my-grill", control_board=mock_control_board, has_lights=False
        )
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_control_board.commands = {"turn-light-off": mock_cmd}
        mock_cmd.side_effect = ["HEXCMD"]
        mock_conn.send_command.return_value = {}
        assert (await pitboss.turn_light_off()) == {}

        mock_cmd.assert_not_called()
        mock_conn.send_command.assert_not_awaited()

    @pytest.mark.parametrize(
        "slug,method",
        [
            ("turn-off", "turn_grill_off"),
            ("turn-primer-motor-on", "turn_primer_motor_on"),
            ("turn-primer-motor-off", "turn_primer_motor_off"),
        ],
    )
    async def test_grill_functions(
        self, slug, method, mock_conn, mock_get_grill, mock_control_board, mock_cmd
    ):
        grill = grills.Grill(name="my-grill", control_board=mock_control_board)
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_control_board.commands = {slug: mock_cmd}
        mock_cmd.side_effect = ["HEXCMD"]
        mock_conn.send_command.return_value = {}
        assert (await getattr(pitboss, method)()) == {}

        mock_cmd.assert_called_once()
        mock_conn.send_command.assert_awaited_once_with(
            "PB.SendMCUCommand", {"command": "HEXCMD"}
        )

    async def test_get_state(self, mock_conn, mock_get_grill, mock_control_board):
        grill = grills.Grill(name="my-grill", control_board=mock_control_board)
        mock_get_grill.return_value = grill
        pitboss = api.PitBoss(mock_conn, "my-grill")
        mock_conn.send_command.return_value = {
            "sc_11": "status_payload",
            "sc_12": "temps_payload",
        }
        mock_control_board.parse_status.return_value = {"p1Target": 200}
        mock_control_board.parse_temperatures.return_value = {"p1Temp": 190}
        assert (await pitboss.get_state()) == {"p1Target": 200, "p1Temp": 190}

        mock_conn.send_command.assert_awaited_once_with("PB.GetState", {})
        mock_control_board.parse_status.assert_called_once_with("status_payload")
        mock_control_board.parse_temperatures.assert_called_once_with("temps_payload")

