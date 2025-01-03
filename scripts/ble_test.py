#!/usr/bin/env python
"""A simple client to test the BLE LE connection against an actual grill."""

import asyncio
from bleak import BleakScanner
import configparser
import logging
from pathlib import Path
from pytboss import BleConnection, PitBoss

logging.basicConfig(level=logging.DEBUG)  # Log all HTTP requests to stderr.


async def state_callback(data):
    print(data)


async def main():
    cfg = configparser.ConfigParser()
    cfg.read(str(Path.home() / ".pitboss"))
    model = cfg["pitboss"]["model"]
    device_address = cfg["pitboss"]["device_address"]

    ble_device = await BleakScanner.find_device_by_address(device_address)
    boss = PitBoss(BleConnection(ble_device), model)
    # Subscribe to updates from the smoker.
    await boss.subscribe_state(state_callback)
    await boss.start()
    while True:
        asyncio.sleep(0.1)


asyncio.run(main())
