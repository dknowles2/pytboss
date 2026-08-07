"""Build realistic grill state for tests.

Consumers of this library otherwise test against hand-written state
dictionaries. That is fast and precise about the consumer's own reasoning,
and blind to this library: a renamed key, a dropped field, or a board that
never reported one in the first place looks identical to everything working
until a user says otherwise.

The helpers here synthesize a wire frame for a specific grill and hand it to
that board's own parsing routine, so the state that comes back is what the
library would really produce -- the same vendor JavaScript, the same
per-board field set, the same conversions. Nothing here fakes a parse.

    >>> from pytboss import grills, testing
    >>> grill = grills.get_grill("PBV4PS2")
    >>> state = testing.build_state(grill, grillTemp=275, moduleIsOn=True)
    >>> state["grillTemp"]
    275

Frames are synthesized, not captured: the field *values* are ours, the
layout and the decoding are the board's. A board that reads a field at an
offset the vendor got wrong will produce the wrong value here too, which is
the point -- see `DROPPED_STATUS_FIELDS` for the ones where that is known
and handled.

This module is part of the public API and is safe to import from a test
suite. It is not imported by the library at runtime.
"""

from __future__ import annotations

import re

from .grills import (
    DROPPED_STATUS_FIELDS,
    DROPPED_TEMPERATURE_FIELDS,
    Grill,
    StateDict,
)

__all__ = [
    "build_state",
    "encode_flag",
    "encode_temperature",
    "status_frame",
    "temperatures_frame",
]


def encode_temperature(value: int) -> str:
    """A temperature as the boards carry it: one decimal digit per byte.

    161 becomes "010601". Three digits, so the range the wire can express is
    0-999; 960 is the boards' own no-reading sentinel and decodes to None.
    """
    if not 0 <= value <= 999:
        raise ValueError(f"temperature {value} does not fit in three digits")
    return "".join(f"0{digit}" for digit in f"{value:03d}")


def encode_flag(value: bool) -> str:
    """A boolean as the boards carry it, in one byte."""
    return "01" if value else "00"


class _Frame:
    """A hex payload assembled in field order.

    Positional: the routines read fixed offsets, so a field skipped without
    reserving its bytes shifts everything after it. Fields are therefore
    added in the order the board expects, and only when that board's frame
    carries them.
    """

    def __init__(self, prefix: str) -> None:
        self._parts: list[str] = [prefix]
        self._index: dict[str, int] = {}

    def __str__(self) -> str:
        return "".join(self._parts)

    def __contains__(self, field: str) -> bool:
        return field in self._index

    def add(self, field: str, value: str) -> None:
        if field in self._index:
            raise KeyError(f"{field} is already in the frame")
        self._index[field] = len(self._parts)
        self._parts.append(value)


def _carries(js: str | None, field: str) -> bool:
    """Whether a routine's frame reserves bytes for `field`.

    Deliberately asks the raw source rather than the live code. A field the
    vendor commented out of the returned object is still *read* from the
    frame, so its bytes are still there -- treating it as absent would build
    a frame that is short, and every offset after it would be wrong.

    `ControlBoard.emits` answers the other question, which is whether the
    parsed result will contain the field. The two differ, and both are
    needed: one to lay the frame out, the other to know what to expect back.
    """
    return bool(js) and field in js  # type: ignore[operator]


def _live(js: str | None) -> str:
    return re.sub(r"//.*", "", re.sub(r"/\*.*?\*/", "", js or "", flags=re.DOTALL))


# Field order of the status (FE0B) frame, as `(field, default, conditional)`.
# A bool default is a one-byte flag, an int a three-byte temperature, a str
# raw bytes. Conditional fields are the ones some boards' routines do not
# read at all, and whose bytes those frames therefore do not reserve.
_STATUS_LAYOUT: tuple[tuple[str, object, bool], ...] = (
    ("p1Target", 191, False),
    ("p2Target", 192, True),
    ("p1Temp", 161, False),
    ("p2Temp", 162, False),
    ("p3Temp", 163, False),
    ("p4Temp", 164, True),
    ("smokerActTemp", 200, True),
    ("grillTemp", 225, False),
    ("condGrillTemp", "01", False),
    ("moduleIsOn", True, False),
    ("err1", False, False),
    ("err2", False, False),
    ("err3", False, False),
    ("highTempErr", False, False),
    ("fanErr", False, False),
    ("hotErr", False, False),
    ("motorErr", False, False),
    ("noPellets", False, False),
    ("erL", False, True),
    ("fanState", False, False),
    ("hotState", False, False),
    ("motorState", False, False),
    ("lightState", False, False),
    ("primeState", False, True),
    ("isFahrenheit", True, False),
    # One byte, not a three-digit temperature: the recipe clock
    # follows it, and encoding it wide shifts all of it.
    ("recipeStep", "01", False),
)

_TEMPERATURES_LAYOUT: tuple[tuple[str, object, bool], ...] = (
    ("p1Target", 191, False),
    ("p2Target", 192, True),
    ("p1Temp", 161, False),
    ("p2Temp", 162, False),
    ("p3Temp", 163, False),
    ("p4Temp", 164, True),
    ("smokerActTemp", 210, True),
    ("grillSetTemp", 225, False),
    ("grillTemp", 215, False),
    ("isFahrenheit", True, False),
)


def _encode(field: str, value: object) -> str:
    if isinstance(value, bool):
        return encode_flag(value)
    if isinstance(value, int):
        return encode_temperature(value)
    if isinstance(value, str):
        return value
    raise TypeError(f"cannot encode {field}={value!r}")


def _build(
    js: str | None,
    prefix: str,
    layout: tuple[tuple[str, object, bool], ...],
    dropped: frozenset[str],
    values: dict[str, object],
) -> str:
    unknown = set(values) - {field for field, _, _ in layout}
    if unknown:
        raise ValueError(f"not fields of the {prefix} frame: {sorted(unknown)}")
    # Which source answers "does the frame carry this" differs by reply, and
    # getting it wrong shifts every offset after the field. The status frame
    # reserves bytes for fields the vendor commented out of the returned
    # object -- they are still read -- so it asks the raw routine. The
    # temperatures frame does not, so it asks live code only.
    source = js if prefix == "FE0B" else _live(js)
    frame = _Frame(prefix)
    for field, default, conditional in layout:
        if conditional and not (
            _carries(source, field)
            # A field this board is known to read from bytes that do not hold
            # it. The frame reserves none for it, which is what makes the
            # rest of the reply line up.
            and field not in dropped
        ):
            continue
        frame.add(field, _encode(field, values.get(field, default)))
    # The recipe clock is three separate bytes rather than a temperature.
    if prefix == "FE0B":
        for part, default in (("recipeHours", "04"), ("recipeMinutes", "0C")):
            frame.add(part, default)
        frame.add("recipeSeconds", "3B")
    frame.add("suffix", "FF")
    return str(frame)


def status_frame(grill: Grill, **values: object) -> str:
    """A status (FE0B) payload this grill's board would accept.

    Values are given in the board's own terms: temperatures as integers,
    flags as booleans. Fields this board's frame does not carry are left out
    rather than zero-filled, because the routine reads by offset.
    """
    board = grill.control_board
    return _build(
        board._status_js_func,
        "FE0B",
        _STATUS_LAYOUT,
        DROPPED_STATUS_FIELDS.get(board.name, frozenset()),
        values,
    )


def temperatures_frame(grill: Grill, **values: object) -> str:
    """A temperatures (FE0C) payload this grill's board would accept."""
    board = grill.control_board
    return _build(
        board._temperatures_js_func,
        "FE0C",
        _TEMPERATURES_LAYOUT,
        DROPPED_TEMPERATURE_FIELDS.get(board.name, frozenset()),
        values,
    )


def build_state(grill: Grill, **values: object) -> StateDict:
    """The state this grill would report, parsed by its own board routine.

    Both frames are synthesized and both are parsed, then folded together
    the way the API folds a poll -- the boards answer with two independent
    replies and each carries only part of the picture.

    Any field named in `values` is applied to whichever frames carry it, so
    a caller can ask for `grillTemp=275` without knowing which reply their
    board reports it in.
    """
    every_field = {f for f, _, _ in _STATUS_LAYOUT} | {
        f for f, _, _ in _TEMPERATURES_LAYOUT
    }
    # Checked up front rather than per frame: a field only one reply carries
    # is legitimate, so neither frame alone can tell a typo from a field the
    # other one owns.
    if unknown := set(values) - every_field:
        raise ValueError(f"no frame carries: {sorted(unknown)}")

    state = StateDict()
    for build, parse, layout in (
        (status_frame, grill.control_board.parse_status, _STATUS_LAYOUT),
        (
            temperatures_frame,
            grill.control_board.parse_temperatures,
            _TEMPERATURES_LAYOUT,
        ),
    ):
        fields = {field for field, _, _ in layout}
        frame = build(grill, **{k: v for k, v in values.items() if k in fields})
        if parsed := parse(frame):
            state.update(parsed)  # type: ignore[typeddict-item]
    return state
