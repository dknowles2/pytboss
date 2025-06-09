#!/usr/bin/env python
"""
A simple client to help debug either the BLE or the WebSocket transports against an
actual grill. Remember to turn the grill on before trying to connect to it.
"""

from argparse import ArgumentParser
import asyncio
from bleak import BleakScanner
import logging
import pytboss as pb


async def state_callback(data):
    print(data)


async def main():
    parser = ArgumentParser(description=__doc__)

    parser.add_argument(
        "-t",
        "--transport",
        default="ble",
        help="Transport to use; either 'ble' or 'ws'. (default: %(default)s)",
    )

    parser.add_argument(
        "-i",
        "--grill_id",
        required=True,
        help="A grill's unique id. Can be retrieved from the pitboss app's grills, edit grill screen.",
    )

    parser.add_argument("-m", "--model", required=True, help="A grill's model.")

    args = parser.parse_args()

    # Log all HTTP requests to stderr.
    logging.basicConfig(level=logging.DEBUG)

    print(f"Using {args.transport} to connect to device")

    if args.transport == "ble":
        try:
            ble_device = await BleakScanner.find_device_by_name(args.grill_id)
        except FileNotFoundError as ex:
            print(
                "Can't find bluetooth hardware. Is this running in a container w/o bluetooth?"
            )
            print(ex)
            return
        transport = pb.BleConnection(ble_device)
    else:
        transport = pb.WebSocketConnection(args.grill_id)

    try:
        boss = pb.PitBoss(transport, args.model)

        await boss.subscribe_state(state_callback)
        await boss.start()
        while True:
            asyncio.sleep(0.1)

    except pb.exceptions.GrillUnavailable as ex:
        print(f"Could not connect to grill.\n{ex}")
        return


if __name__ == "__main__":
    asyncio.run(main())
