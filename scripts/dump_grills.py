#!/usr/bin/env python3
"""Script that dumps all grill specifications as JSON to stdout.

Application credentials should be stored in a file called ".pitboss" in your
home directory. The format is an INI style like this:

[pitboss]
username = email@address.com
password = my-secret-password

Run with python3 -m scripts.dump_grills
"""

import json
import logging
from asyncio import run, sleep
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientConnectionError, ClientResponseError

from pytboss.auth import async_login
from pytboss.exceptions import Error, InvalidGrill

logging.basicConfig(level=logging.DEBUG)  # Log all HTTP requests to stderr.

_LOGGER = logging.getLogger(__name__)

API_URL = "https://api-prod.dansonscorp.com/api/v1"

# Vendor bookkeeping the library never reads, dropped to keep the checked-in
# definitions to a manageable size. The timestamps are the bulk of it: they sit
# on every grill, board and command, and the vendor touches rows without
# changing anything that affects parsing.
_DROPPED_FIELDS = frozenset({"created_at", "updated_at", "deleted_at"})
_DROPPED_GRILL_FIELDS = _DROPPED_FIELDS | {"app_layout", "manual_url"}
# Commands are stored inside the board they belong to, so naming it again on
# every one of them says nothing the position does not.
_DROPPED_COMMAND_FIELDS = _DROPPED_FIELDS | {"control_board_id"}


def _drop(
    obj: dict[str, Any], fields: frozenset[str], **replace: Any
) -> dict[str, Any]:
    """Copies `obj` without `fields`, applying any `replace` overrides."""
    return {k: v for k, v in obj.items() if k not in fields} | replace


async def get_grill_details(
    session: ClientSession, grill_id: int, attempts: int = 3
) -> dict[str, Any]:
    """Fetches one grill definition, or an empty dict if the API can't serve it.

    The ID space has gaps, and the API is inconsistent about how it reports
    them: most return 404, but some return a persistent 500 (67 and 128 at the
    time of writing, verified over repeated requests). It also hangs up on the
    occasional request part way through a full sweep. None of these should
    abort the whole run, so connection drops and server errors are retried, and
    an ID that still won't load is skipped like a 404.

    Client errors other than 404 are raised: a 401 means the credentials are
    wrong, and retrying or skipping would quietly produce a partial catalogue.
    """
    _LOGGER.info("Fetching grill details for grill_id: %s", grill_id)
    for attempt in range(1, attempts + 1):
        try:
            resp = await session.get(f"{API_URL}/grills/{grill_id}")
            resp.raise_for_status()
            resp_json = await resp.json()
        except ClientResponseError as ex:
            if ex.status == 404:
                _LOGGER.warning("Unknown grill ID: %s", grill_id)
                return {}
            if ex.status < 500:
                raise
            if attempt == attempts:
                _LOGGER.warning(
                    "Skipping grill ID %s: server returned %s on all %s attempts",
                    grill_id,
                    ex.status,
                    attempts,
                )
                return {}
            _LOGGER.warning("Server error %s for grill_id %s", ex.status, grill_id)
        except (ClientConnectionError, TimeoutError) as ex:
            if attempt == attempts:
                raise
            _LOGGER.warning("Connection dropped for grill_id %s: %s", grill_id, ex)
        else:
            if resp_json["status"] != "success":
                raise Error(resp_json["message"])
            return resp_json["data"]["grill"]

        delay = 2**attempt
        _LOGGER.info("Retrying grill_id %s in %ss", grill_id, delay)
        await sleep(delay)

    raise Error(f"Could not fetch grill_id {grill_id}")


async def main():
    cfg = ConfigParser()
    cfg.read(str(Path.home() / ".pitboss"))
    async with ClientSession(headers={"x-country": "US"}) as session:
        auth_headers = await async_login(
            session, cfg["pitboss"]["username"], cfg["pitboss"]["password"]
        )
    grills = {}
    skipped = []
    async with ClientSession(headers=auth_headers) as session:
        for i in range(1, 150):
            try:
                grill = await get_grill_details(session, i)
                if not grill:
                    skipped.append(i)
                    continue
            except InvalidGrill:
                break

            # Some models are served twice, on two control board generations.
            # Keying by name alone lets the higher ID silently overwrite the
            # other board's definition, which hides that model from grills
            # advertising the older board -- and in PBL2's case discarded the
            # only rows that board appears in at all. Keep both, with the
            # higher ID under the plain model name.
            name = grill["name"]
            board = grill["control_board"]["name"]
            if (prev := grills.get(name)) is not None:
                prev_board = prev["control_board"]["name"]
                if prev_board == board:
                    _LOGGER.warning("Duplicate row for %s on board %s", name, board)
                else:
                    _LOGGER.info(
                        "%s is served on boards %s and %s; keeping both",
                        name,
                        prev_board,
                        board,
                    )
                    grills[f"{name} ({prev_board})"] = prev
            grills[name] = grill

    # Log a summary so a sweep that quietly collected less than usual is
    # visible in the run output rather than only in the resulting diff.
    if skipped:
        _LOGGER.warning("Skipped %d grill IDs: %s", len(skipped), skipped)
    _LOGGER.info("Collected %d grill definitions", len(grills))

    # Store each control board once and reference it by name. Twenty boards are
    # shared across every model, so inlining them more than tripled the file.
    control_boards: dict[str, Any] = {}
    for grill in grills.values():
        board = grill["control_board"]
        name = board["name"]
        if name in control_boards and control_boards[name] != board:
            raise Error(
                f"Control board {name} has two different definitions. Storing "
                "boards once by name would discard one of them."
            )
        control_boards[name] = board
    _LOGGER.info("Collected %d control boards", len(control_boards))

    print(
        json.dumps(
            {
                "control_boards": {
                    name: _drop(
                        board,
                        _DROPPED_FIELDS,
                        control_board_commands=[
                            _drop(command, _DROPPED_COMMAND_FIELDS)
                            for command in board["control_board_commands"]
                        ],
                    )
                    for name, board in control_boards.items()
                },
                "grills": {
                    name: _drop(
                        grill,
                        _DROPPED_GRILL_FIELDS,
                        control_board=grill["control_board"]["name"],
                    )
                    for name, grill in grills.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    run(main())
