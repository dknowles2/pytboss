"""The frame builders, checked against every board's own parsing routine.

These assert concrete values rather than that a parse happened: a frame
whose fields are one slot out of place still parses, and still returns a
dict, so anything weaker would pass on exactly the bug this module is most
likely to have.
"""

import pytest

from pytboss import grills, testing


def idfn(arg):
    if isinstance(arg, grills.Grill):
        return f"{arg.control_board.name} {arg.name}"
    return None


def all_grills() -> list[grills.Grill]:
    return list(grills.get_grills())


def test_encode_temperature_is_one_decimal_digit_per_byte():
    assert testing.encode_temperature(161) == "010601"
    assert testing.encode_temperature(0) == "000000"
    assert testing.encode_temperature(999) == "090909"
    # The boards' no-reading sentinel, which decodes back to None.
    assert testing.encode_temperature(960) == "090600"


def test_encode_temperature_rejects_what_the_wire_cannot_carry():
    with pytest.raises(ValueError):
        testing.encode_temperature(1000)
    with pytest.raises(ValueError):
        testing.encode_temperature(-1)


def test_encode_flag():
    assert testing.encode_flag(True) == "01"
    assert testing.encode_flag(False) == "00"


@pytest.mark.parametrize("grill", all_grills(), ids=idfn)
def test_build_state_round_trips_the_values_it_was_given(grill: grills.Grill):
    """Every board, in its own unit, reporting what it was asked to.

    A shifted frame is the failure mode here -- a field added where the
    board reserves no bytes, or left out where it does -- and it shows up as
    one field reading another's value rather than as an error.
    """
    want = {
        "grillTemp": 275,
        "grillSetTemp": 250,
        "p1Temp": 145,
        "p2Temp": 146,
        "moduleIsOn": True,
    }
    state = testing.build_state(grill, isFahrenheit=True, **want)
    for field, value in want.items():
        assert state.get(field) == value, (
            f"{grill.name} on {grill.control_board.name}: {field} came back "
            f"{state.get(field)!r}, wanted {value} -- a frame laid out wrong "
            f"reads a neighbouring field"
        )


@pytest.mark.parametrize("grill", all_grills(), ids=idfn)
def test_build_state_reports_only_what_the_board_can_report(grill: grills.Grill):
    """The state matches `emits()`, which is the library's own answer.

    Two independent routes to the same question: one reads the routine's
    source, the other runs it. They disagreeing means one of them is wrong.
    """
    state = testing.build_state(grill)
    board = grill.control_board
    for field in state:
        assert board.emits(field), (
            f"{grill.name}: parsing produced {field}, which emits() denies"
        )


@pytest.mark.parametrize("grill", all_grills(), ids=idfn)
def test_the_no_reading_sentinel_comes_back_as_none(grill: grills.Grill):
    """960 is what an unplugged probe reads, and it must not become 960."""
    state = testing.build_state(grill, p1Temp=960)
    assert state.get("p1Temp") is None


def test_a_field_no_frame_carries_is_an_error():
    grill = grills.get_grill("PBV4PS2")
    with pytest.raises(ValueError, match="no frame carries"):
        testing.build_state(grill, notARealField=1)


def test_boards_that_convert_hand_back_celsius():
    """The split is real, so a caller cannot assume either unit.

    Verified against the whole catalogue rather than one model, because
    which boards convert is read off their routines and can change with a
    definitions refresh.
    """
    converted = as_sent = 0
    for grill in all_grills():
        state = testing.build_state(grill, isFahrenheit=False, grillTemp=275)
        if state.get("grillTemp") == 135:
            converted += 1
        elif state.get("grillTemp") == 275:
            as_sent += 1
        else:
            pytest.fail(f"{grill.name} reported {state.get('grillTemp')!r}")
    assert converted and as_sent
    assert converted + as_sent == len(all_grills())
