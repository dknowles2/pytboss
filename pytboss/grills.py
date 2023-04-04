"""Routines for accessing grill metadata."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from importlib import resources

import js2py

from .exceptions import InvalidGrill

_COMMAND_JS_TMPL = """\
function() {
    let formatHex = function(n) {
        let t = '0' + parseInt(n).toString(16);
        return t.substring(t.length - 2)
    };
    let formatDecimal = function(n) {
        let t = '000' + parseInt(n).toString(10);
        return t.substring(t.length - 3);
    };
    %s
}
"""

_CONTROLLER_JS_TMPL = """\
// Basic polyfill for String.startsWith.
String.prototype.startsWith = function(search, pos){
    return this.slice(pos || 0, search.length) === search;
};
function(message) {
    let convertTemperature = function(parts, startIndex) {
        let temp = (
            parts[startIndex] * 100 +
            parts[startIndex + 1] * 10 +
            parts[startIndex + 2]
        );
        return temp === 960 ? null : temp;
    };
    let parseHexMessage = function(_data) {
        const parsed = [];
        for (let i = 0; i < _data.length; i+=2) {
            parsed.push(parseInt(_data.substring(i, i+2), 16));
        }
        return parsed;
    };
    %s
}
"""


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
        return cls(
            name=cmd_dict["name"],
            slug=cmd_dict["slug"],
            _hex=cmd_dict["hexadecimal"],
            _js_func=cmd_dict["function"],
        )

    def __call__(self, *args) -> str:
        """Returns a hexadecimal command string."""
        if self._hex:
            return self._hex

        if self._js_func is None:
            raise NotImplementedError

        return js2py.eval_js(_COMMAND_JS_TMPL % self._js_func)(*args)


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
            _status_js_func=ctrl_dict["status_function"],
            _temperatures_js_func=ctrl_dict["temperature_function"],
        )

    def parse_status(self, message) -> dict | None:
        """Parses a status message."""
        if not self._status_js_func:
            raise NotImplementedError
        return js2py.eval_js(_CONTROLLER_JS_TMPL % self._status_js_func)(message)

    def parse_temperatures(self, message) -> dict | None:
        """Parses a temperatures message."""
        if not self._temperatures_js_func:
            raise NotImplementedError
        return js2py.eval_js(_CONTROLLER_JS_TMPL % self._temperatures_js_func)(message)


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

    temp_increments: list[int] | None = 0
    """Supported temperature increments."""

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
            control_board=ControlBoard.from_dict(grill_dict["control_board"]),
        )


@cache
def _read_grills() -> dict:
    """Reads all known grill specifications."""
    grills_json = resources.files(__package__).joinpath("grills.json").read_text()
    return json.loads(grills_json)


def get_grills(control_board: str | None = None) -> Iterable[Grill]:
    """Retrieves grill specifications.

    :param control_board: If specified, returns only grills with this control board.
    :type control_board: str or None
    """
    for grill in _read_grills().values():
        if control_board is None or grill["control_board"]["name"] == control_board:
            yield Grill.from_dict(grill)


def get_grill(grill_name: str) -> Grill:
    """Retrieves a grill specification.

    :param grill_name: The name of the grill specification to retrieve.
    :type grill_name: str
    """
    if (grill := _read_grills().get(grill_name, None)) is None:
        raise InvalidGrill(f"Unknown grill name: {grill_name}")
    return Grill.from_dict(grill)
