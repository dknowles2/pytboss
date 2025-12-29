#!/usr/bin/env python3
"""Script that fetches the newest firmware files for a grill model.

Application credentials should be stored in a file called ".pitboss" in your
home directory. The format is an INI style like this:

[pitboss]
username = email@address.com
password = my-secret-password

Run with python3 -m scripts.fetch_firmware <firmware_output_dir>
"""

import configparser
import logging
import sys
from asyncio import run
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiohttp import ClientSession, TraceConfig, TraceRequestStartParams

from pytboss.auth import async_login

logging.basicConfig(level=logging.DEBUG)  # Log all HTTP requests to stderr.

API_URL = "https://api-prod.dansonscorp.com/api/v1"
CONTROL_BOARD_ID = 5
CONTROL_BOARD_NAME = "PBV"


async def _onr_request_start(
    session: ClientSession, context: SimpleNamespace, params: TraceRequestStartParams
) -> None:
    logging.getLogger("aiohttp.client").debug("%s %s", params.method, params.url)


async def get_grills(session: ClientSession) -> list[dict[str, Any]]:
    resp = await session.get(f"{API_URL}/customer-grills")
    resp.raise_for_status()
    return (await resp.json())["data"]["customer_grills"]


async def get_firmware_grill(
    session: ClientSession, control_board_name: str
) -> dict[str, Any]:
    resp = await session.get(
        f"{API_URL}/grills", params={"control_board": control_board_name}
    )
    resp.raise_for_status()
    return (await resp.json())["data"]["grills"][0]


async def get_firmware_metadata(
    session: ClientSession, grill: dict[str, Any]
) -> dict[str, Any]:
    control_board_id = grill["id"]
    resp = await session.get(f"{API_URL}/firmware-platforms/{control_board_id}")
    resp.raise_for_status()
    return (await resp.json())["data"]["firmwarePlatform"]


async def fetch_firmware(
    session: ClientSession,
    control_board_name: str,
    grill: dict[str, Any],
    output_dir: Path,
) -> None:
    metadata = await get_firmware_metadata(session, grill)
    firmware_base_url = metadata["firmware_base_url"]
    firmware_version_path = metadata["firmware_version"].replace(".", "-")
    for file in metadata["platform_files"]:
        filename = file["filename"]
        fp = Path(filename)
        url = f"{firmware_base_url}{firmware_version_path}/"
        if file["board_specific"] > 0:
            filename = f"{fp.stem}_{control_board_name}{fp.suffix}"
        url += filename
        (output_dir / fp).write_text(await fetch_file(session, url))


async def fetch_file(session: ClientSession, url: str) -> str:
    resp = await session.get(url)
    resp.raise_for_status()
    return await resp.text()


async def main(argv):
    if len(argv) < 2:
        print("Must provide an output path for firmware files", file=sys.stderr)
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(str(Path.home() / ".pitboss"))
    tc = TraceConfig()
    tc.on_request_start.append(_onr_request_start)
    async with ClientSession(
        headers={"x-country": "US"}, trace_configs=[tc]
    ) as session:
        auth_headers = await async_login(
            session, cfg["pitboss"]["username"], cfg["pitboss"]["password"]
        )
    async with ClientSession(headers=auth_headers, trace_configs=[tc]) as session:
        grills = await get_grills(session)
        control_board_name = grills[0]["board_id"].split("-")[0]
        firmware_grill = await get_firmware_grill(session, control_board_name)
        await fetch_firmware(session, control_board_name, firmware_grill, Path(argv[1]))


if __name__ == "__main__":
    run(main(sys.argv))
