from pytboss import grills as grills_lib


def test_get_grills():
    grills = list(grills_lib.get_grills())
    assert len(grills) > 0


def test_get_grills_with_control_board():
    grills = list(grills_lib.get_grills("PBL"))
    assert len(grills) > 0


def test_get_grill():
    grill = grills_lib.get_grill("PBV4PS2")
    assert grill != {}
    assert grill["name"] == "PBV4PS2"
