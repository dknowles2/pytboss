"""Routines for accessing grill metadata."""

import json
import re
import threading
from collections.abc import Container, Iterable
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any, TypedDict, cast

from dukpy.evaljs import JSInterpreter

from .exceptions import InvalidGrill

_INTERPRETERS = threading.local()
"""One JS interpreter per thread.

`dukpy.evaljs()` builds a fresh interpreter for every call, and each one reads
three runtime `.js` files from disk. Parsing a single state reply evaluates two
routines, so a poll costs six file reads -- which an asyncio caller does on its
event loop.

Reusing one is safe here: the routines are self-contained functions evaluated
as global scripts, and an interpreter survives a failed evaluation. It is kept
thread-local rather than global because `get_grill()` is called through
`asyncio.to_thread`, so parsing is not guaranteed to stay on one thread, and a
QuickJS context is not documented as thread-safe.
"""


def _run_js(code: str, **kwargs: Any) -> Any:
    """Evaluate `code` on this thread's interpreter."""
    interpreter = getattr(_INTERPRETERS, "interpreter", None)
    if interpreter is None:
        interpreter = JSInterpreter()
        _INTERPRETERS.interpreter = interpreter
    return interpreter.evaljs(code, **kwargs)


@cache
def _get_grills() -> dict[str, Any]:
    """Loads grill definitions, resolving their control board references.

    grills.json stores each control board once and has grills reference it by
    name. Twenty boards are shared across 147 models, and inlining them made
    the file more than three times larger than it needed to be. Attach the
    shared board object rather than a copy: every model on a board had a
    byte-identical definition anyway, so callers see the structure they always
    have, and models on one board now share a single dict.
    """
    data = json.loads(resources.files(__package__).joinpath("grills.json").read_text())
    control_boards = data["control_boards"]
    grills = data["grills"]
    for grill in grills.values():
        grill["control_board"] = control_boards[grill["control_board"]]
    return grills


UNSUPPORTED_MODELS = (
    "PBX - test 1",  # Nonstandard data format
    "PBV30DS",  # Nonstandard data format
    "PBV30DX",  # Nonstandard data format
)

# The Louisiana Grills boards (LBL, LFS) ship parsing routines derived from the
# PitBoss ones without adjusting for their shorter frame, which has no dedicated
# smoker temperature field. Two fields are dropped as a result:
#
#   * smokerActTemp reads no bytes of its own. It duplicates p4Temp's offset in
#     the FE0B frame but the grill setpoint's in FE0C, so the merged value would
#     flip depending on which frame arrived last. Neither reading holds up: the
#     LFS models carry four meat probes, so p4Temp there is a real probe rather
#     than a chamber sensor, and the LBL models carry two, so it reads 960 (None)
#     regardless.
#   * The grill temperature block spans bytes 20-22, overlapping moduleIsOn (21)
#     and err1 (22). These frames are digit-per-byte, so no frame can satisfy
#     both readings.
#
# Both are discarded after parsing rather than rewritten to guessed offsets, so
# the vendor routines stay byte-identical to what grills.json ships. Grill
# temperatures come from the FE0C reply instead, which is how every other board
# already works -- PBL comments the same switch block out of its own status
# routine.
DROPPED_STATUS_FIELDS = {
    "LBL": frozenset({"smokerActTemp", "grillSetTemp", "grillTemp"}),
    "LFS": frozenset({"smokerActTemp", "grillSetTemp", "grillTemp"}),
}

DROPPED_TEMPERATURE_FIELDS = {
    "LBL": frozenset({"smokerActTemp"}),
    "LFS": frozenset({"smokerActTemp"}),
}

# Typos in the vendor's command slugs, mapped to the canonical slug.
_COMMAND_SLUG_OVERRIDES = {
    "set-prove-1-temperature": "set-probe-1-temperature",
}


_COMMAND_JS_TMPL = """\
function command() {
    var formatHex = function(n) {
        var t = '0' + parseInt(n).toString(16);
        return t.substring(t.length - 2)
    };
    var formatDecimal = function(n) {
        var t = '000' + parseInt(n).toString(10);
        return t.substring(t.length - 3);
    };
    %s
}
command.apply(null, dukpy['args']);
"""

_CONTROLLER_JS_TMPL = """\
function parse(message) {
    var convertTemperature = function(parts, startIndex) {
        var temp = (
            parts[startIndex] * 100 +
            parts[startIndex + 1] * 10 +
            parts[startIndex + 2]
        );
        return temp === 960 ? null : temp;
    };
    var parseHexMessage = function(data) {
        var parsed = [];
        for (var i = 0; i < data.length; i+=2) {
            parsed.push(parseInt(data.substring(i, i+2), 16));
        }
        return parsed;
    };
    %s
}
parse(dukpy['message']);
"""

_FN_RE = re.compile(r"(.+ ?= ?)(\(.[^\)]+\))( ?=>)?(.+)")


def _scrub_js(s: str | None) -> str | None:
    if s is None:
        return s
    s = _FN_RE.sub(r"\1 function \2\4", s)
    s = s.replace("let ", "var ")
    s = s.replace("const ", "var ")
    return s


class StateDict(TypedDict, total=False):
    """State of the grill."""

    p1Target: int
    """Target temperature for meat probe 1."""

    p2Target: int | None
    """Target temperature for meat probe 2."""

    p1Temp: int | None
    """Current temperature of meat probe 1 (if present)."""

    p2Temp: int | None
    """Current temperature of meat probe 2 (if present)."""

    p3Temp: int
    """Current temperature of meat probe 3 (if present)."""

    p4Temp: int
    """Current temperature of meat probe 4 (if present)."""

    smokerActTemp: int
    """Current temperature of the smoker."""

    grillSetTemp: int
    """Target temperature for the grill."""

    grillTemp: int
    """Current temperature of the grill."""

    moduleIsOn: bool
    """Whether the control module is powered on."""

    err1: bool
    """Whether there is an error with meat probe 1."""

    err2: bool
    """Whether there is an error with meat probe 2."""

    err3: bool
    """Whether there is an error with meat probe 3."""

    highTempErr: bool
    """Whether the temperature is too high."""

    fanErr: bool
    """Whether there was an error with the fan."""

    hotErr: bool
    """Whether there was an error with the igniter."""

    motorErr: bool
    """Whether there was an error with the auger."""

    noPellets: bool
    """Whether the pellet hopper is empty."""

    erL: bool
    """Whether there was an error in the start-up cycle."""

    fanState: bool
    """Whether the fan is currently on."""

    hotState: bool
    """Whether the igniter is currently on."""

    motorState: bool
    """Whether the auger is currently on."""

    lightState: bool
    """Whether the light is currently on."""

    primeState: bool
    """Whether the prime mode is on."""

    isFahrenheit: bool
    """Whether the temperature readings are in Fahrenheit."""

    recipeStep: int
    """The current recipe step number."""

    recipeTime: int
    """The time remaining for this recipe step (in seconds)."""


def _drop_fields(state: StateDict | None, fields: Container[str]) -> StateDict | None:
    """Removes fields a control board reads from bytes that don't hold them."""
    if state is None:
        return None
    return cast(StateDict, {k: v for k, v in state.items() if k not in fields})


@dataclass
class Command:
    """A control board command."""

    slug: str
    """Serialized name of the command."""

    _hex: str | None
    """Hexadecimal command."""

    _js_func: str | None
    """JavaScript function body that creates the hexadecimal command."""

    @classmethod
    def from_dict(cls, cmd_dict) -> "Command":
        """Creates a Command from a JSON dict.

        :param cmd_dict: A `control_board_commands` entry from `grills.json`,
            with a `slug` key and either a `hexadecimal` or a `function` key.
            A command is built from one or the other, never both, so whichever
            does not apply may be absent.
        """
        js_func = _scrub_js(cmd_dict.get("function"))
        return cls(
            slug=cmd_dict["slug"],
            _hex=cmd_dict.get("hexadecimal"),
            _js_func=js_func,
        )

    def __call__(self, *args) -> str:
        """Returns a hexadecimal command string.

        :raise NotImplementedError: If this command has neither a static
            hexadecimal value nor a JavaScript function to generate one.
        """
        if self._hex:
            return self._hex

        if self._js_func is None:
            raise NotImplementedError

        return _run_js(_COMMAND_JS_TMPL % self._js_func, args=args)


def _live_code(js: str | None) -> str:
    """The routine with commented-out code removed."""
    if not js:
        return ""
    return re.sub(r"//.*", "", re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL))


@dataclass(frozen=True)
class ControlBoard:
    """Specifications for a control board connected via UART."""

    name: str
    """Name of the control board."""

    commands: dict[str, Command]
    """Controller commands indexed by their slug."""

    _status_js_func: str | None
    """JavaScript function body that parses a status reply."""

    _temperatures_js_func: str | None
    """JavaScript function body that parses a temperatures reply."""

    @classmethod
    def from_dict(cls, ctrl_dict) -> "ControlBoard":
        """Creates a ControlBoard from a JSON dict.

        :param ctrl_dict: A `control_board` entry from `grills.json`, with
            `name`, `control_board_commands`, `status_function`, and
            `temperature_function` keys.
        """
        return cls(
            name=ctrl_dict["name"],
            commands={
                _COMMAND_SLUG_OVERRIDES.get(c["slug"], c["slug"]): Command.from_dict(c)
                for c in ctrl_dict["control_board_commands"]
            },
            _status_js_func=_scrub_js(ctrl_dict["status_function"]),
            _temperatures_js_func=_scrub_js(ctrl_dict["temperature_function"]),
        )

    def _evaljs(self, js_func: str, message: str) -> StateDict | None:
        js = _CONTROLLER_JS_TMPL % js_func
        return _run_js(js, message=message)

    @property
    def converts_temperatures_to_celsius(self) -> bool:
        """Whether the temperatures routine converts fahrenheit itself.

        Most boards convert internally and report whichever unit the grill is
        set to; some report fahrenheit always and convert with an `ftoc()`
        helper in their JS. Consumers deciding what unit a value is in, or
        what unit to send, need to know which.

        Read off the routine rather than a list of board names: a definitions
        refresh can introduce a converting board, and only live code counts --
        PBL2 ships PBL3's conversion block entirely commented out, which is
        the whole difference between those two boards.
        """
        return "ftoc" in _live_code(self._temperatures_js_func)

    @property
    def converts_status_to_celsius(self) -> bool:
        """Whether the status routine converts fahrenheit itself.

        The two routines for one board do not always agree: PBL3 converts in
        its temperatures reply but not in its status reply.
        """
        return "ftoc" in _live_code(self._status_js_func)

    def parse_status(self, message: str) -> StateDict | None:
        """Parses a status message.

        :param message: Raw status payload received from the grill.
        :raise NotImplementedError: If this control board has no status-parsing
            routine.
        """
        if not self._status_js_func:
            raise NotImplementedError
        return _drop_fields(
            self._evaljs(self._status_js_func, message),
            DROPPED_STATUS_FIELDS.get(self.name, frozenset()),
        )

    def parse_temperatures(self, message: str) -> StateDict | None:
        """Parses a temperatures message.

        :param message: Raw temperatures payload received from the grill.
        :raise NotImplementedError: If this control board has no
            temperatures-parsing routine.
        """
        if not self._temperatures_js_func:
            raise NotImplementedError
        return _drop_fields(
            self._evaljs(self._temperatures_js_func, message),
            DROPPED_TEMPERATURE_FIELDS.get(self.name, frozenset()),
        )


@dataclass(frozen=True)
class Grill:
    """Specifications for a particular grill model."""

    name: str
    """Human-readable name of the grill."""

    control_board: ControlBoard
    """Information about the grill control board."""

    has_lights: bool = False
    """Whether the grill has lights."""

    has_mpc: bool = False
    """Whether the vendor's catalogue flags this grill as having MPC.

    What MPC is, the vendor does not say. No parsing routine or command
    mentions it, and the companion `mpc_type` field is null on every model.
    It is set on 34 of 147 grills and does not track the probe count, so it
    is passed through as the vendor reports it rather than interpreted.
    """

    min_temp: int | None = None
    """Minimum grill temperature supported."""

    max_temp: int | None = None
    """Maximum grill temperature supported."""

    meat_probes: int = 0
    """The number of meat probes available on the grill."""

    temp_increments: list[int] | None = field(default_factory=list)
    """Supported temperature increments, in Fahrenheit."""

    json: dict[str, Any] = field(default_factory=dict)
    """The raw JSON returned by the PitBoss API."""

    celsius_temp_increments: list[int] | None = field(default_factory=list)
    """Supported temperature increments in Celsius, where the grill declares
    its own list. Most do not; theirs is derived from `temp_increments`.

    Declared after `json` rather than beside `temp_increments`: `Grill` is not
    `kw_only`, so inserting a field earlier would shift every positional
    argument after it and silently change what an existing caller passes."""

    @classmethod
    def from_dict(cls, grill_dict) -> "Grill":
        """Creates a Grill from a JSON dict.

        :param grill_dict: A top-level entry from `grills.json`, with `name`,
            `control_board`, `lights`, `has_mpc`, `meat_probes`,
            `temp_increment`, `celsius_temp_increment`, `min_temp`, and
            `max_temp` keys.
        """
        min_temp = None
        try:
            min_temp = int(grill_dict["min_temp"])
        except ValueError:
            # Likely a string like "Smoke"
            pass

        max_temp = None
        try:
            max_temp = int(grill_dict["max_temp"])
        except ValueError:
            # Likely a string like "High"
            pass

        return cls(
            name=grill_dict["name"],
            has_lights=grill_dict["lights"] > 0,
            has_mpc=bool(grill_dict["has_mpc"]),
            min_temp=min_temp,
            max_temp=max_temp,
            meat_probes=grill_dict["meat_probes"],
            temp_increments=[int(t) for t in grill_dict["temp_increment"].split("/")],
            celsius_temp_increments=[
                int(t)
                for t in (grill_dict.get("celsius_temp_increment") or "").split("/")
                if t.strip().isdigit()
            ],
            json=grill_dict,
            control_board=ControlBoard.from_dict(grill_dict["control_board"]),
        )


def get_grills(control_board: str | None = None) -> Iterable[Grill]:
    """Retrieves grill specifications.

    Grills with no status-parsing routine, or whose name is listed in
    `UNSUPPORTED_MODELS`, are silently excluded.

    A few models are sold on two control board generations and appear once per
    board. Without `control_board` each model is yielded once, so callers
    listing every supported model do not see duplicates; with it, the models
    yielded are those that ship with that particular board.

    :param control_board: If specified, returns only grills with this control board.
    """
    seen = set()
    for grill in _get_grills().values():
        if not grill["control_board"].get("status_function"):
            continue
        if grill["name"] in UNSUPPORTED_MODELS:
            continue
        if control_board is None:
            if grill["name"] in seen:
                continue
            seen.add(grill["name"])
        elif grill["control_board"]["name"] != control_board:
            continue
        yield Grill.from_dict(grill)


def get_grill(grill_name: str, control_board: str | None = None) -> Grill:
    """Retrieves a grill specification.

    Unlike `get_grills()`, this does not exclude names listed in
    `UNSUPPORTED_MODELS`; requesting one of those names will succeed here
    but its control board's `parse_status`/`parse_temperatures` may raise
    `NotImplementedError` when actually used.

    A few models are sold on two control board generations, and the two boards
    do not always parse identically. Pass `control_board` -- the prefix the
    grill advertises over Bluetooth -- to select the right one. Without it, the
    board the vendor lists most recently is returned.

    :param grill_name: The name of the grill specification to retrieve.
    :param control_board: If specified, returns the variant of this model that
        ships with this control board.
    :raise pytboss.exceptions.InvalidGrill: If `grill_name` is not a known
        grill model, or has no variant on `control_board`.
    """
    if control_board is not None:
        for candidate in _get_grills().values():
            if (
                candidate["name"] == grill_name
                and candidate["control_board"]["name"] == control_board
            ):
                return Grill.from_dict(candidate)
        raise InvalidGrill(
            f"Unknown grill name for control board {control_board}: {grill_name}"
        )
    if (grill := _get_grills().get(grill_name, None)) is None:
        raise InvalidGrill(f"Unknown grill name: {grill_name}")
    return Grill.from_dict(grill)
