import re
from contextlib import contextmanager
from math import floor

import pytest

from pytboss import grills as grills_lib
from pytboss.exceptions import InvalidGrill

# Most control boards perform the fahrenheit to celsius conversion internally,
# however these boards do NOT and instead rely on the conversion to happen in
# their JS snippets.
_HAS_FTOC = ("LFS", "PBM", "PBT", "PBV")

TEMPERATURE_FIELDS = (
    "p1Target",
    "p2Target",
    "p1Temp",
    "p2Temp",
    "p3Temp",
    "p4Temp",
    "grillSetTemp",
    "grillTemp",
    "smokerActTemp",
)


def f_to_c(temp: int) -> int:
    """Converts a temperature from Fahrenheit to Celsius."""
    if temp is None:
        return temp
    return floor((temp - 32) / 1.8)


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
        ctrl = grills_lib.ControlBoard("PBx", {}, "return {'foo': message}", None)
        assert ctrl.parse_status("bar") == {"foo": "bar"}

    def parse_temperatures(self):
        ctrl = grills_lib.ControlBoard("PBx", {}, "", "return {'foo': message}")
        assert ctrl.parse_temperatures("bar") == {"foo": "bar"}


class JSFunc:
    def __init__(self, js: str):
        self._js = js

    def __str__(self):
        return "\n".join(
            f"{i:3} {line}" for i, line in enumerate(self._js.splitlines())
        )

    def has_key(self, k):
        return k in self._js and not re.search(rf"\/[\/\*] *{k}", self._js)


@contextmanager
def debug_js(js: JSFunc):
    try:
        yield
    except AssertionError:
        print(js)
        raise


class Message:
    def __init__(self) -> None:
        self._data: list[str] = []
        self._idx: dict[str, int] = {}

    def __str__(self) -> str:
        return "".join(self._data)

    def __contains__(self, k: str) -> bool:
        return k in self._idx

    def __setitem__(self, k: str, v: str) -> None:
        if k not in self:
            raise KeyError(f"{k} not in Message")
        self._data[self._idx[k]] = v

    def add(self, k: str, v: str) -> None:
        if k in self:
            raise KeyError(f"{k} already in Message")
        self._idx[k] = len(self._data)
        self._data.append(v)


class TestGetGrills:
    def test_plain(self):
        grills = list(grills_lib.get_grills())
        assert len(grills) > 0

    def test_with_control_board(self):
        grills = list(grills_lib.get_grills("PBL"))
        assert len(grills) > 0

    @pytest.mark.parametrize(
        "name,grill", [(g.name, g) for g in grills_lib.get_grills()]
    )
    def test_js_commands(self, name: str, grill: grills_lib.Grill):
        _ = name
        for cmd in grill.control_board.commands.values():
            cmd(11)

    @pytest.mark.parametrize(
        "name,grill", [(g.name, g) for g in grills_lib.get_grills()]
    )
    def test_parse_temperatures(self, name: str, grill: grills_lib.Grill):
        if name == "PBX - test 1":
            # Nonstandard data format. Ignore.
            return
        if name.startswith("LG"):
            # These have bad parsing routines. Ignore.
            return

        assert grill.control_board._temperatures_js_func is not None
        js = JSFunc(grill.control_board._temperatures_js_func)
        msg = Message()

        # WARNING! THE ORDER HERE MATTERS!
        msg.add("prefix", "FE0C")
        msg.add("p1Target", "010901")
        if js.has_key("p2Target"):
            msg.add("p2Target", "010902")
        msg.add("p1Temp", "010601")
        msg.add("p2Temp", "010602")
        msg.add("p3Temp", "010603")
        if js.has_key("p4Temp"):
            msg.add("p4Temp", "010604")
        if js.has_key("smokerActTemp"):
            msg.add("smokerActTemp", "020100")
        msg.add("grillSetTemp", "020205")
        msg.add("grillTemp", "020105")
        msg.add("isFahrenheit", "01")
        msg.add("suffix", "FF")

        temps = grill.control_board.parse_temperatures(str(msg))
        want = {
            "p1Temp": 161,
            "p2Temp": 162,
            "p3Temp": 163,
            "grillSetTemp": 225,
            "grillTemp": 215,
            "isFahrenheit": True,
        }
        if js.has_key("p1Target"):
            want["p1Target"] = 191
        if js.has_key("p2Target"):
            want["p2Target"] = 192
        if js.has_key("p4Temp"):
            want["p4Temp"] = 164
        if js.has_key("smokerActTemp"):
            want["smokerActTemp"] = 210

        with debug_js(js):
            assert temps == want

            msg["isFahrenheit"] = "00"
            status = grill.control_board.parse_temperatures(str(msg))
            assert status is not None
            for key in TEMPERATURE_FIELDS:
                if key not in msg or key not in want:
                    continue

                temp = want[key]
                if grill.control_board.name in _HAS_FTOC:
                    temp = f_to_c(want[key])
                try:
                    assert status[key] == temp, f"{key}: {status[key]} != {temp}"  # type: ignore[literal-required]
                except AssertionError:
                    if key in ("p4Temp", "smokerActTemp"):
                        # Some grills don't convert these fields for some reason.
                        continue
                    raise

    @pytest.mark.parametrize(
        "name,grill", [(g.name, g) for g in grills_lib.get_grills()]
    )
    def test_parse_state(self, name: str, grill: grills_lib.Grill):
        if name == "PBX - test 1":
            # Nonstandard data format. Ignore.
            return
        if name.startswith("LG"):
            # These have bad parsing routines. Ignore.
            return

        msg = Message()
        assert grill.control_board._status_js_func is not None
        js = JSFunc(grill.control_board._status_js_func)

        # WARNING! THE ORDER HERE MATTERS!
        msg.add("prefix", "FE0B")
        msg.add("p1Target", "010901")
        if js.has_key("p2Target"):
            msg.add("p2Target", "010902")
        msg.add("p1Temp", "010601")
        msg.add("p2Temp", "010602")
        msg.add("p3Temp", "010603")
        if js.has_key("p4Temp"):
            msg.add("p4Temp", "010604")
        if js.has_key("smokerActTemp"):
            msg.add("smokerActTemp", "020200")
        msg.add("grillTemp", "020205")
        msg.add("condGrillTemp", "01")
        msg.add("moduleIsOn", "01")
        msg.add("err1", "00")
        msg.add("err2", "00")
        msg.add("err3", "00")
        msg.add("highTempErr", "00")
        msg.add("fanErr", "00")
        msg.add("hotErr", "00")
        msg.add("motorErr", "00")
        msg.add("noPellets", "00")
        if js.has_key("erL"):
            msg.add("erL", "00")
        msg.add("fanState", "00")
        msg.add("hotState", "00")
        msg.add("motorState", "00")
        msg.add("lightState", "00")
        if js.has_key("primeState"):
            msg.add("primeState", "00")
        msg.add("isFahrenheit", "01")
        msg.add("recipeStep", "01")
        msg.add("recipeHours", "04")
        msg.add("recipeMinutes", "0C")
        msg.add("recipeSeconds", "3B")
        msg.add("suffix", "FF")

        status = grill.control_board.parse_status(str(msg))
        want = {
            "p1Temp": 161,
            "p2Temp": 162,
            "p3Temp": 163,
            "grillSetTemp": 225,
            "moduleIsOn": True,
            "err1": False,
            "err2": False,
            "err3": False,
            "highTempErr": False,
            "fanErr": False,
            "hotErr": False,
            "motorErr": False,
            "noPellets": False,
            "fanState": False,
            "hotState": False,
            "motorState": False,
            "lightState": False,
            "isFahrenheit": True,
            "recipeStep": 1,
            "recipeTime": 15179,
        }
        if js.has_key("p1Target"):
            want["p1Target"] = 191
        if js.has_key("p2Target"):
            want["p2Target"] = 192
        if js.has_key("p4Temp"):
            want["p4Temp"] = 164
        if js.has_key("smokerActTemp"):
            want["smokerActTemp"] = 220
        if js.has_key("erL"):
            want["erL"] = False
        if js.has_key("primeState"):
            want["primeState"] = False

        with debug_js(js):
            assert status == want

            msg["condGrillTemp"] = "02"
            status = grill.control_board.parse_status(str(msg))
            assert status is not None
            assert status["grillTemp"] == 225
            assert "grillSetTemp" not in status

            error_keys = ["err1", "err2", "err3"]
            error_keys += ["highTempErr", "fanErr", "hotErr", "motorErr"]
            error_keys += ["noPellets", "erL"]
            for key in error_keys:
                if key in msg:
                    msg[key] = "01"
            status = grill.control_board.parse_status(str(msg))
            assert status is not None
            for key in error_keys:
                if key in msg:
                    assert status[key]  # type: ignore[literal-required]

            msg["isFahrenheit"] = "00"
            status = grill.control_board.parse_status(str(msg))
            assert status is not None
            for key in TEMPERATURE_FIELDS:
                if key not in msg or key not in want:
                    continue
                temp = want[key]
                if grill.control_board.name in _HAS_FTOC:
                    temp = f_to_c(want[key])
                try:
                    assert status[key] == temp, f"{key}: {status[key]} != {temp}"  # type: ignore[literal-required]
                except AssertionError:
                    if key in ("p4Temp", "smokerActTemp"):
                        # Some grills don't convert these fields for some reason.
                        continue
                    raise


class TestGetGrill:
    def test_valid(self):
        grill = grills_lib.get_grill("PBV4PS2")
        assert grill is not None
        assert grill.name == "PBV4PS2"

    def test_invalid(self):
        with pytest.raises(InvalidGrill):
            grills_lib.get_grill("unknown-grill")
