# pytboss

Python 3 library for interacting with Pitboss grills and smokers.

*Note that this project has no official relationship with Pitboss or Danson's. Use at your own risk.*

## Usage

```python
import asyncio
from bleak import BleakClient
from pytboss import BleConnection, PitBoss


async def state_callback(data):
    print(data)


async def main():
    # Perform a BLE scan for available grills.
    # Assume we only have one.
    [device] = await PitBoss.discover()
    async with BleakClient(device) as ble_client:
        grill = PitBoss(BleConnection(ble_client))
        # Subscribe to updates from the grill.
        await grill.subscribe_state(state_callback)
        await grill.start()
        while True:
            asyncio.sleep(0.1)


asyncio.run(main())
```
