"""Routines for accessing grill metadata."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources
import json
import re
from typing import Any, TypedDict

from dukpy import evaljs

from .exceptions import InvalidGrill

_GRILLS = json.loads(resources.files(__package__).joinpath("grills.json").read_text())

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

    recipeStep: bool
    """The current recipe step number."""

    recipeTime: int
    """The time remaining for this recipe step (in seconds)."""


@dataclass
class Command:
    """A control board command."""

    name: str
    """Human readable name of the command."""

    slug: str
    """Serialized name of the command."""

    _hex: str | None
    """Hexadecimal command."""

    _js_func: str | None
    """JavaScript function body that creates the hexadecimal command."""

    @classmethod
    def from_dict(cls, cmd_dict) -> "Command":
        """Creates a Command from a JSON dict."""
        js_func = _scrub_js(cmd_dict["function"])
        return cls(
            name=cmd_dict["name"],
            slug=cmd_dict["slug"],
            _hex=cmd_dict["hexadecimal"],
            _js_func=js_func,
        )

    def __call__(self, *args) -> str:
        """Returns a hexadecimal command string."""
        if self._hex:
            return self._hex

        if self._js_func is None:
            raise NotImplementedError

        return evaljs(_COMMAND_JS_TMPL % self._js_func, args=args)


@dataclass
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
        """Creates a ControlBoard from a JSON dict."""
        return cls(
            name=ctrl_dict["name"],
            commands={
                c["slug"]: Command.from_dict(c)
                for c in ctrl_dict["control_board_commands"]
            },
            _status_js_func=_scrub_js(ctrl_dict["status_function"]),
            _temperatures_js_func=_scrub_js(ctrl_dict["temperature_function"]),
        )

    def _evaljs(self, js_func: str, message: str) -> StateDict | None:
        js = _CONTROLLER_JS_TMPL % js_func
        return evaljs(js, message=message)

    def parse_status(self, message: str) -> StateDict | None:
        """Parses a status message."""
        if not self._status_js_func:
            raise NotImplementedError
        return self._evaljs(self._status_js_func, message)

    def parse_temperatures(self, message: str) -> StateDict | None:
        """Parses a temperatures message."""
        if not self._temperatures_js_func:
            raise NotImplementedError
        return self._evaljs(self._temperatures_js_func, message)


@dataclass
class Grill:
    """Specifications for a particular grill model."""

    name: str
    """Human-readable name of the grill."""

    control_board: ControlBoard
    """Information about the grill control board."""

    has_lights: bool = False
    """Whether the grill has lights."""

    min_temp: int | None = None
    """Minimum grill temperature supported."""

    max_temp: int | None = None
    """Maximum grill temperature supported."""

    meat_probes: int = 0
    """The number of meat probes available on the grill."""

    temp_increments: list[int] | None = field(default_factory=list)
    """Supported temperature increments."""

    json: dict[str, Any] = field(default_factory=dict)
    """The raw JSON returned by the PitBoss API."""

    @classmethod
    def from_dict(cls, grill_dict) -> "Grill":
        """Creates a Grill from a JSON dict."""
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
            min_temp=min_temp,
            max_temp=max_temp,
            meat_probes=grill_dict["meat_probes"],
            temp_increments=list(
                int(t) for t in grill_dict["temp_increment"].split("/")
            ),
            json=grill_dict,
            control_board=ControlBoard.from_dict(grill_dict["control_board"]),
        )


def get_grills(control_board: str | None = None) -> Iterable[Grill]:
    """Retrieves grill specifications.

    :param control_board: If specified, returns only grills with this control board.
    :type control_board: str or None
    """
    for grill in _GRILLS.values():
        if not grill["control_board"].get("status_function"):
            continue
        if control_board is None or grill["control_board"]["name"] == control_board:
            yield Grill.from_dict(grill)


def get_grill(grill_name: str) -> Grill:
    """Retrieves a grill specification.

    :param grill_name: The name of the grill specification to retrieve.
    :type grill_name: str
    """
    if (grill := _GRILLS.get(grill_name, None)) is None:
        raise InvalidGrill(f"Unknown grill name: {grill_name}")
    return Grill.from_dict(grill)
