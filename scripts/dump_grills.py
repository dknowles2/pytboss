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
# changing anything that affects parsing. `description` is null on all but two
# grills and three commands, and where it is set it says "N/A" or restates the
# slug it sits on. The `id` is the vendor's own primary key: this script walks
# the ID space to fetch grills, but nothing ever reads one back out of the
# definitions, which are keyed by model name and reference boards by name.
_DROPPED_FIELDS = frozenset(
    {"created_at", "updated_at", "deleted_at", "description", "id", "site_id"}
)
# The rest is storefront and app-presentation data: how the vendor's own app
# renders a grill and how its store sells one. None of it describes the grill's
# protocol, so none of it is reachable through `Grill`, whose `json` attribute
# is the only thing that ever exposed these.
#
# `celsius_temp_increment` was dropped here once and should not be again. It is
# null on all but two grills -- PBV30DS and PBV30DX -- and the reasoning was
# that where set it merely restates `temp_increment` in celsius. It does not:
# those two declare 40..150 in steps of 5, where deriving it from the
# fahrenheit list gives 37..148 at irregular intervals, so the two disagree at
# nearly every entry. That is presumably why the vendor publishes one at all.
# ha-pitboss reads it to decide which setpoints the board will honour, so
# dropping it silently offered those grills values their board ignores.
_DROPPED_GRILL_FIELDS = _DROPPED_FIELDS | {
    "app_layout",
    "control_board_id",
    "friendly_name",
    "has_indicators",
    "has_no_app_indicators",
    "image",
    "manual_url",
    "mpc_type",
    "name_text_color",
    "part_number",
    "screen_orientation",
    "shopify_product_id",
    "sku",
}
# Commands are stored inside the board they belong to, so naming it again on
# every one of them says nothing the position does not. Commands are looked up
# by slug and nothing reads the human-readable `name`, which is only ever a
# prettier spelling of the slug beside it.
_DROPPED_COMMAND_FIELDS = _DROPPED_FIELDS | {"control_board_id", "name"}
# A command is built either from a static hexadecimal string or from a JS
# function, never both, so exactly one of these is null on every command. Which
# one it is carries the meaning; storing the other as an explicit null does not.
_EITHER_OR_COMMAND_FIELDS = ("function", "hexadecimal")


def _drop(
    obj: dict[str, Any], fields: frozenset[str], **replace: Any
) -> dict[str, Any]:
    """Copies `obj` without `fields`, applying any `replace` overrides."""
    return {k: v for k, v in obj.items() if k not in fields} | replace


def _trim_command(command: dict[str, Any]) -> dict[str, Any]:
    """Returns the form of a control board command that gets written out."""
    unset = {k for k in _EITHER_OR_COMMAND_FIELDS if command.get(k) is None}
    return _drop(command, _DROPPED_COMMAND_FIELDS | unset)


def _trim_board(board: dict[str, Any]) -> dict[str, Any]:
    """Returns the form of a control board that gets written out."""
    return _drop(
        board,
        _DROPPED_FIELDS,
        control_board_commands=[
            _trim_command(command) for command in board["control_board_commands"]
        ],
    )


# Re-logins allowed across one whole sweep, rather than per request. A stale
# or rotated session shows up as a sporadic 401 part way through the sweep, and
# a fresh login recovers it, but an account whose credentials really are
# rejected must not be allowed to re-login once per ID for 149 IDs.
MAX_RELOGINS = 3

# Sent on the login request only. The sweep deliberately does not carry it: it
# selects the vendor's storefront country, and the definitions this script
# writes out should not depend on it.
_LOGIN_HEADERS = {"x-country": "US"}


class GrillApi:
    """Fetches grill definitions, re-authenticating when a session goes stale.

    Auth headers are held here and passed per request rather than baked into
    the `ClientSession`'s defaults, so a mid-sweep re-login can swap them for
    fresh ones. Retrying a 401 against the same stale headers would only get
    another 401.
    """

    def __init__(
        self,
        session: ClientSession,
        username: str,
        password: str,
        max_relogins: int = MAX_RELOGINS,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._relogins_left = max_relogins
        self._headers: dict[str, str] = {}

    async def _login(self) -> dict[str, str]:
        """Authenticates on a session of its own, returning auth headers."""
        async with ClientSession(headers=_LOGIN_HEADERS) as session:
            return await async_login(session, self._username, self._password)

    async def login(self) -> None:
        """Authenticates before the sweep starts.

        A failure here is the genuine bad-credentials case and is left to
        propagate: nothing has been fetched yet, so there is no partial
        catalogue to protect, and the run should say plainly that the stored
        credentials were rejected.
        """
        self._headers = await self._login()

    async def _relogin(self) -> bool:
        """Swaps in fresh auth headers, if the run has re-logins left.

        Returns False once the budget is spent, which makes the 401 that
        prompted it fatal. A re-login that is itself rejected raises: the
        credentials have genuinely stopped working mid-run.
        """
        if self._relogins_left <= 0:
            _LOGGER.error(
                "Still unauthorized after %s re-logins; giving up", MAX_RELOGINS
            )
            return False
        self._relogins_left -= 1
        _LOGGER.warning(
            "API returned 401; re-authenticating (%s re-logins left afterwards)",
            self._relogins_left,
        )
        self._headers = await self._login()
        return True

    async def get_grill_details(
        self, grill_id: int, attempts: int = 3
    ) -> dict[str, Any]:
        """Fetches one grill definition, or an empty dict if the API can't serve it.

        The ID space has gaps, and the API is inconsistent about how it reports
        them: most return 404, but some return a persistent 500 (67 and 128 at
        the time of writing, verified over repeated requests). It also hangs up
        on the occasional request part way through a full sweep. None of these
        should abort the whole run, so connection drops and server errors are
        retried, and an ID that still won't load is skipped like a 404.

        A 401 is retried, but only behind a fresh `async_login()`: the vendor
        expires or rotates sessions mid-sweep, and a single blip used to cost a
        whole week's refresh. It is never skipped the way a 404 is -- a 401 the
        re-login budget can't clear aborts the run, because committing a
        catalogue that is missing whatever the API refused to serve is worse
        than refreshing nothing.

        Client errors other than 401 and 404 are raised unchanged.
        """
        _LOGGER.info("Fetching grill details for grill_id: %s", grill_id)
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._session.get(
                    f"{API_URL}/grills/{grill_id}", headers=self._headers
                )
                resp.raise_for_status()
                resp_json = await resp.json()
            except ClientResponseError as ex:
                if ex.status == 404:
                    _LOGGER.warning("Unknown grill ID: %s", grill_id)
                    return {}
                if ex.status == 401:
                    if attempt == attempts or not await self._relogin():
                        raise
                elif ex.status < 500:
                    raise
                elif attempt == attempts:
                    _LOGGER.warning(
                        "Skipping grill ID %s: server returned %s on all %s attempts",
                        grill_id,
                        ex.status,
                        attempts,
                    )
                    return {}
                else:
                    _LOGGER.warning(
                        "Server error %s for grill_id %s", ex.status, grill_id
                    )
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
    grills = {}
    skipped = []
    async with ClientSession() as session:
        api = GrillApi(session, cfg["pitboss"]["username"], cfg["pitboss"]["password"])
        await api.login()
        for i in range(1, 150):
            try:
                grill = await api.get_grill_details(i)
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
        # Compare the trimmed form, not the raw row. Two rows differing only in
        # a field this script drops are not two different definitions, and
        # failing the whole sweep over one would be a false alarm.
        board = _trim_board(grill["control_board"])
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
                "control_boards": control_boards,
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
