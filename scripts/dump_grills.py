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
from asyncio import run
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientResponseError

from pytboss.auth import async_login
from pytboss.exceptions import Error, InvalidGrill

logging.basicConfig(level=logging.DEBUG)  # Log all HTTP requests to stderr.

API_URL = "https://api-prod.dansonscorp.com/api/v1"


async def get_grill_details(session: ClientSession, grill_id: int) -> dict[str, Any]:
    logging.info("Fetching grill details for grill_id: %s", grill_id)
    resp = await session.get(f"{API_URL}/grills/{grill_id}")
    try:
        resp.raise_for_status()
    except ClientResponseError as ex:
        if ex.status == 404:
            logging.warning("Unknown grill ID: %s", grill_id)
            return {}
    resp_json = await resp.json()
    if resp_json["status"] != "success":
        raise Error(resp_json["message"])
    return resp_json["data"]["grill"]


async def main():
    cfg = ConfigParser()
    cfg.read(str(Path.home() / ".pitboss"))
    async with ClientSession(headers={"x-country": "US"}) as session:
        auth_headers = await async_login(
            session, cfg["pitboss"]["username"], cfg["pitboss"]["password"]
        )
    grills = {}
    async with ClientSession(headers=auth_headers) as session:
        for i in range(1, 150):
            try:
                grill = await get_grill_details(session, i)
                if not grill:
                    continue
            except InvalidGrill:
                break
            grills[grill["name"]] = grill
    print(json.dumps(grills, indent=2, sort_keys=True))


if __name__ == "__main__":
    run(main())
