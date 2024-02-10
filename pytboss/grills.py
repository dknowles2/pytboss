"""Routines for accessing grill metadata."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
import json
from typing import Any

import dukpy

from .exceptions import InvalidGrill

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
        js_func = cmd_dict["function"]
        if js_func:
            js_func = js_func.replace("let ", "var ")
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

        return dukpy.evaljs(_COMMAND_JS_TMPL % self._js_func, args=args)


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
        status_js_func = ctrl_dict["status_function"]
        temperatures_js_func = ctrl_dict["temperature_function"]
        return cls(
            name=ctrl_dict["name"],
            commands={
                c["slug"]: Command.from_dict(c)
                for c in ctrl_dict["control_board_commands"]
            },
            _status_js_func=status_js_func.replace("let ", "var "),
            _temperatures_js_func=temperatures_js_func.replace("let ", "var "),
        )

    def parse_status(self, message) -> dict | None:
        """Parses a status message."""
        if not self._status_js_func:
            raise NotImplementedError
        return dukpy.evaljs(_CONTROLLER_JS_TMPL % self._status_js_func, message=message)

    def parse_temperatures(self, message) -> dict | None:
        """Parses a temperatures message."""
        if not self._temperatures_js_func:
            raise NotImplementedError
        return dukpy.evaljs(
            _CONTROLLER_JS_TMPL % self._temperatures_js_func, message=message
        )


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
        if not grill["control_board"].get("status_function"):
            continue
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
