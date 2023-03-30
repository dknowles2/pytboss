"""Routines for accessing grill metadata."""

import json
import sys
from collections.abc import Iterable
from functools import cache
from importlib import resources


@cache
def _read_grills() -> dict:
    """Reads all known grill definitions."""
    grills_json = resources.files(__package__).joinpath("grills.json").read_text()
    return json.loads(grills_json)


def get_grills(control_board: str | None = None) -> Iterable[dict]:
    """Retrieves grill definitions.

    :param control_board: If specified, returns only grills with this control board.
    :type control_board: str or None
    """
    for grill in _read_grills().values():
        if control_board is None or grill["control_board"]["name"] == control_board:
            yield grill


def get_grill(grill_name: str) -> dict:
    """Retrieves a grill definition.

    :param grill_name: The name of the grill definition to retrieve.
    :type grill_name: str
    """
    return _read_grills().get(grill_name, {})
