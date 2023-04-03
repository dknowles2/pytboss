import pytest

from pytboss import grills as grills_lib
from pytboss.exceptions import InvalidGrill


class TestCommand:
    def test_call_func(self):
        cmd = grills_lib.Command(
            "My Command", "my-command", None, "return formatHex(arguments[0]);"
        )
        assert cmd(11) == "0b"

    def test_call_hex(self):
        cmd = grills_lib.Command("My Command", "my-command", "0C", None)
        assert cmd() == "0C"


class TestController:
    def parse_status(self):
        ctrl = grills_lib.ControlBoard("PBx", [], "return {'foo': message}", None)
        assert ctrl.parse_status("bar") == {"foo": "bar"}

    def parse_temperatures(self):
        ctrl = grills_lib.ControlBoard("PBx", [], "", "return {'foo': message}")
        assert ctrl.parse_temperatures("bar") == {"foo": "bar"}


class TestGetGrills:
    def test_plain(self):
        grills = list(grills_lib.get_grills())
        assert len(grills) > 0

    def test_with_control_board(self):
        grills = list(grills_lib.get_grills("PBL"))
        assert len(grills) > 0

    @pytest.mark.parametrize("grill", grills_lib.get_grills())
    def test_js_functions(self, grill):
        ctrl = grill.control_board
        ctrl.parse_status("XXX")
        ctrl.parse_temperatures("XXX")
        for cmd in ctrl.commands.values():
            cmd(11)


class TestGetGrill:
    def test_valid(self):
        grill = grills_lib.get_grill("PBV4PS2")
        assert grill is not None
        assert grill.name == "PBV4PS2"

    def test_invalid(self):
        with pytest.raises(InvalidGrill):
            grills_lib.get_grill("unknown-grill")
