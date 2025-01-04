#!/usr/bin/env python
"""A simple client to test the BLE and WebSocket transports against an
actual grill and the actual websocket."""

import asyncio
from bleak import BleakScanner
import configparser
import logging
from pathlib import Path
import pytboss as pb
from pytboss import grills  # pylint: disable=import-error,wrong-import-position


logging.basicConfig(level=logging.DEBUG)  # Log all HTTP requests to stderr.


async def state_callback(data):
    print(data)


async def main():
    cfg = configparser.ConfigParser()
    cfg.read(str(Path.home() / ".pitboss"))
    model = cfg["pitboss"]["model"]

    grill_models = [grill.name for grill in grills.get_grills()]
    if model not in grill_models:
        print(f"Invalid grill model: {model}")
        print(grill_models)
        return

    try:
        print(f"Using BLE to find device: {model}")
        ble_device = await BleakScanner.find_device_by_name(model)
        boss = pb.PitBoss(pb.BleConnection(ble_device), model)
        await boss.subscribe_state(state_callback)
        await boss.start()
        while True:
            asyncio.sleep(0.1)
            if boss.is_connected():
                print(boss)
            break
    except FileNotFoundError as ex:
        print(
            "Can't find bluetooth hardware. Is this running in a container w/o bluetooth?"
        )
        print(ex)
        return

    except pb.exceptions.GrillUnavailable as ex:
        print(f"Could not connect to grill.\n{ex}")
        return

    if "grill_id" not in locals():
        if "grill_id" in cfg["pitboss"]:
            grill_id = cfg["pitboss"]["grill_id"]
        else:
            print(
                "Could not retrieve grill_id via BLE and not in ~/.pitboss config file."
            )
            return

    try:
        boss = pb.PitBoss(pb.WebSocketConnection(grill_id), model)

        await boss.subscribe_state(state_callback)
        await boss.start()
        while True:
            asyncio.sleep(0.1)

    except pb.exceptions.GrillUnavailable as ex:
        print(f"Please make sure the grill is turned on\n{ex}")


if __name__ == "__main__":
    asyncio.run(main())
