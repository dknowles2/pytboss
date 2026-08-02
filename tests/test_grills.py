import builtins
import re
import threading
from contextlib import contextmanager
from math import floor

import pytest

from pytboss import grills as grills_lib
from pytboss.exceptions import InvalidGrill

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
        cmd = grills_lib.Command("my-command", None, "return formatHex(arguments[0]);")
        assert cmd(11) == "0b"

    def test_call_hex(self):
        cmd = grills_lib.Command("my-command", "0C", None)
        assert cmd() == "0C"

    @pytest.mark.parametrize(
        "cmd_dict,want",
        (
            ({"slug": "my-command", "hexadecimal": "0C"}, "0C"),
            ({"slug": "my-command", "function": "return '0C';"}, "0C"),
        ),
        ids=("hex", "function"),
    )
    def test_from_dict_omits_the_key_it_does_not_use(self, cmd_dict, want):
        """A command is built from a hex string or a JS function, never both.

        grills.json stores only whichever applies, so the other key is absent
        rather than present and null.
        """
        assert grills_lib.Command.from_dict(cmd_dict)() == want


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

    def _live(self):
        """The routine with commented-out code removed."""
        js = re.sub(r"/\*.*?\*/", "", self._js, flags=re.DOTALL)
        return re.sub(r"//.*", "", js)

    def converts_to_celsius(self):
        """Whether this routine converts fahrenheit to celsius itself.

        Most control boards convert internally and report whichever unit the
        grill is set to, but some report fahrenheit always and rely on an
        ftoc() helper in their JS snippet. Read that off the routine rather
        than maintaining a list of board names: a definitions refresh can
        introduce a board that converts, and the two routines for one board
        do not always agree -- PBL3 converts in its temperatures reply but
        not in its status reply.

        Only live code counts. PBL2 ships the same conversion block as PBL3
        with the whole thing commented out, which is the difference between
        the two boards.
        """
        return "ftoc" in self._live()

    def has_key(self, k, ignore_comments=True):
        """Whether the routine reports `k`.

        With `ignore_comments`, this asks whether live code assigns `k` in the
        object it returns. Two cheaper checks both get it wrong:

        * Rejecting any key named inside a comment anywhere. Some routines
          parse a field and also name it in a commented-out block -- PBL2
          comments out a whole conversion block that mentions p4Temp, which it
          very much does parse. Treating those as absent builds a frame with no
          bytes for that field, shifting every offset after it.
        * Plain membership in comment-stripped source. LFS comments p1Target
          out of its object literal but still has a live
          `status.p1Target = ftoc(status.p1Target)`, which converts undefined
          and reports nothing.

        Without `ignore_comments`, the question is instead whether the frame
        carries bytes for `k` at all, which a commented-out field still does.
        """
        if not ignore_comments:
            return k in self._js
        return re.search(rf"\b{k}\s*:", self._live()) is not None


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


def idfn(arg):
    if isinstance(arg, grills_lib.Grill):
        grill = arg
        return f"{grill.control_board.name} {grill.name}"


def all_variants() -> list[grills_lib.Grill]:
    """Every model/control board pairing, not one entry per model.

    A handful of models are sold on two board generations. get_grills() without
    a filter yields each model once so that callers listing supported models do
    not see duplicates, which would leave the second board untested.
    """
    boards = {g["control_board"]["name"] for g in grills_lib._get_grills().values()}
    return [grill for board in sorted(boards) for grill in grills_lib.get_grills(board)]


class TestGetGrills:
    def test_plain(self):
        grills = list(grills_lib.get_grills())
        assert len(grills) > 0

    def test_with_control_board(self):
        grills = list(grills_lib.get_grills("PBL"))
        assert len(grills) > 0

    @pytest.mark.parametrize("grill", all_variants(), ids=idfn)
    def test_js_commands(self, grill: grills_lib.Grill):
        for cmd in grill.control_board.commands.values():
            cmd(11)

    @pytest.mark.parametrize("grill", all_variants(), ids=idfn)
    def test_has_mpc_is_a_bool(self, grill: grills_lib.Grill):
        """The vendor reports has_mpc as 0 or 1; it is exposed as a bool.

        `is` rather than `==`, since 0 and 1 would satisfy equality and let
        the raw int through unnoticed.
        """
        assert grill.has_mpc is True or grill.has_mpc is False

    @pytest.mark.parametrize("grill", all_variants(), ids=idfn)
    def test_parse_temperatures(self, grill: grills_lib.Grill):
        assert grill.control_board._temperatures_js_func is not None
        js = JSFunc(grill.control_board._temperatures_js_func)
        msg = Message()
        # A dropped field reads bytes that don't hold it, so the frame reserves
        # none for it.
        dropped = grills_lib.DROPPED_TEMPERATURE_FIELDS.get(
            grill.control_board.name, frozenset()
        )

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
        if js.has_key("smokerActTemp") and "smokerActTemp" not in dropped:
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
        if js.has_key("smokerActTemp") and "smokerActTemp" not in dropped:
            want["smokerActTemp"] = 210

        with debug_js(js):
            assert temps == dict(want)

            msg["isFahrenheit"] = "00"
            status = grill.control_board.parse_temperatures(str(msg))
            assert status is not None
            for key in TEMPERATURE_FIELDS:
                if key not in msg or key not in want:
                    continue

                temp = want[key]
                if js.converts_to_celsius():
                    temp = f_to_c(want[key])
                try:
                    assert status[key] == temp, f"{key}: {status[key]} != {temp}"  # type: ignore[literal-required]
                except AssertionError:
                    if key in ("p4Temp", "smokerActTemp"):
                        # Some grills don't convert these fields for some reason.
                        continue
                    raise

    @pytest.mark.parametrize("grill", all_variants(), ids=idfn)
    def test_parse_state(self, grill: grills_lib.Grill):
        msg = Message()
        assert grill.control_board._status_js_func is not None
        js = JSFunc(grill.control_board._status_js_func)
        # A dropped field reads bytes that don't hold it, so the frame reserves
        # none for it.
        dropped = grills_lib.DROPPED_STATUS_FIELDS.get(
            grill.control_board.name, frozenset()
        )

        # WARNING! THE ORDER HERE MATTERS!
        msg.add("prefix", "FE0B")
        msg.add("p1Target", "010901")
        if js.has_key("p2Target", ignore_comments=False):
            msg.add("p2Target", "010902")
        msg.add("p1Temp", "010601")
        msg.add("p2Temp", "010602")
        msg.add("p3Temp", "010603")
        if js.has_key("p4Temp", ignore_comments=False):
            msg.add("p4Temp", "010604")
        if (
            js.has_key("smokerActTemp", ignore_comments=False)
            and "smokerActTemp" not in dropped
        ):
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
        if js.has_key("erL", ignore_comments=False):
            msg.add("erL", "00")
        msg.add("fanState", "00")
        msg.add("hotState", "00")
        msg.add("motorState", "00")
        msg.add("lightState", "00")
        if js.has_key("primeState", ignore_comments=False):
            msg.add("primeState", "00")
        msg.add("isFahrenheit", "01")
        msg.add("recipeStep", "01")
        msg.add("recipeHours", "04")
        msg.add("recipeMinutes", "0C")
        msg.add("recipeSeconds", "3B")
        msg.add("suffix", "FF")

        status = grill.control_board.parse_status(str(msg))
        want = {
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
            "recipeStep": 1,
            "recipeTime": 15179,
        }
        if js.has_key("erL"):
            want["erL"] = False
        if js.has_key("primeState"):
            want["primeState"] = False
        # Most boards report probe temperatures only in the FE0C reply, but a
        # few parse them out of the FE0B status frame as well.
        if js.has_key("isFahrenheit"):
            want["isFahrenheit"] = True
        if js.has_key("p1Target"):
            want["p1Target"] = 191
        if js.has_key("p1Temp"):
            want["p1Temp"] = 161
            want["p2Temp"] = 162
            want["p3Temp"] = 163
        if js.has_key("p4Temp"):
            want["p4Temp"] = 164

        with debug_js(js):
            assert status == dict(want)

            msg["condGrillTemp"] = "02"
            status = grill.control_board.parse_status(str(msg))
            assert status is not None
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
                if js.converts_to_celsius():
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


def test_converts_to_celsius_reads_the_routine():
    """Live code only: a commented-out conversion does not count."""
    converting = grills_lib.ControlBoard(
        "PBx", {}, "", "var ftoc = function (t) { return t; };"
    )
    assert converting.converts_temperatures_to_celsius is True

    commented = grills_lib.ControlBoard(
        "PBx", {}, "", "/* var ftoc = function (t) { return t; }; */"
    )
    assert commented.converts_temperatures_to_celsius is False

    line_commented = grills_lib.ControlBoard(
        "PBx", {}, "", "// var ftoc = function (t) { return t; };"
    )
    assert line_commented.converts_temperatures_to_celsius is False


def test_converts_to_celsius_is_per_routine():
    """One board's two routines need not agree; PBL3 is the real example."""
    board = grills_lib.ControlBoard("PBx", {}, "return {};", "ftoc(1);")
    assert board.converts_temperatures_to_celsius is True
    assert board.converts_status_to_celsius is False


@contextmanager
def _count_js_reads():
    """Count reads of dukpy's runtime `.js` assets."""
    reads: list[str] = []
    real_open = builtins.open

    def counting_open(path, *args, **kwargs):
        if str(path).endswith(".js"):
            reads.append(str(path))
        return real_open(path, *args, **kwargs)

    builtins.open = counting_open
    try:
        yield reads
    finally:
        builtins.open = real_open


def test_the_interpreter_is_reused_across_parses():
    """A fresh interpreter per parse reads three runtime files each time."""
    board = grills_lib.get_grill("PBV4PS2").control_board
    message = "FE0B" + "0" * 60
    board.parse_status(message)  # warm this thread's interpreter

    with _count_js_reads() as reads:
        for _ in range(10):
            board.parse_status(message)
    assert reads == []


def test_reuse_does_not_change_the_result():
    board = grills_lib.get_grill("PBV4PS2").control_board
    message = "FE0B" + "0" * 60
    first = board.parse_status(message)
    assert [board.parse_status(message) for _ in range(20)] == [first] * 20


def test_the_interpreter_survives_a_failed_evaluation():
    """One bad message must not poison every parse that follows."""
    board = grills_lib.get_grill("PBV4PS2").control_board
    message = "FE0B" + "0" * 60
    expected = board.parse_status(message)

    with pytest.raises(Exception):
        grills_lib._run_js("throw new Error('boom');")

    assert board.parse_status(message) == expected


def test_each_thread_gets_its_own_interpreter():
    """`get_grill` runs under `asyncio.to_thread`, so parsing can move."""
    board = grills_lib.get_grill("PBV4PS2").control_board
    message = "FE0B" + "0" * 60
    expected = board.parse_status(message)
    results: list[object] = []

    def parse_on_this_thread() -> None:
        results.append(board.parse_status(message))

    threads = [threading.Thread(target=parse_on_this_thread) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [expected] * 4
