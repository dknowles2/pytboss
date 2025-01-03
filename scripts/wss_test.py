#!/usr/bin/env python
"""A simple client to test the wss transport against the actual websocket."""

import asyncio
import configparser
import logging
from pathlib import Path
import pytboss as pb


logging.basicConfig(level=logging.DEBUG)  # Log all HTTP requests to stderr.


async def state_callback(data):
    print(data)


async def main():
    cfg = configparser.ConfigParser()
    cfg.read(str(Path.home() / ".pitboss"))
    model = cfg["pitboss"]["model"]
    grill_id = cfg["pitboss"]["grill_id"]

    try:
        boss = pb.PitBoss(pb.WebSocketConnection(grill_id), model)

        # Subscribe to updates from the smoker.
        await boss.subscribe_state(state_callback)
        await boss.start()
        while True:
            asyncio.sleep(0.1)

    except pb.exceptions.GrillUnavailable as ex:
        print(f"Please make sure the grill is turned on\n{ex}")


if __name__ == "__main__":
    asyncio.run(main())