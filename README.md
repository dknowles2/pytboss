# pytboss

Python 3 library for interacting with Pitboss grills and smokers.

*Note that this project has no official relationship with Pitboss or Danson's. Use at your own risk.*

## Usage

```python
import asyncio
from bleak import BleakScanner
from pytboss import BleConnection, PitBoss


async def state_callback(data):
    print(data)


async def main():
    ble_device = await BleakScanner.find_device_by_address(device_address)
    boss = PitBoss(BleConnection(ble_device))
    # Subscribe to updates from the smoker.
    await boss.subscribe_state(state_callback)
    await boss.start()
    while True:
        asyncio.sleep(0.1)


asyncio.run(main())
```
