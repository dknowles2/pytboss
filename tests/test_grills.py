from pytboss import grills as grills_lib


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


def test_get_grill():
    grill = grills_lib.get_grill("PBV4PS2")
    assert grill != {}
    assert grill["name"] == "PBV4PS2"
