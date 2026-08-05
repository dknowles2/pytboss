"""Client library for controlling PitBoss/Dansons grills.

Three transports reach a grill: Bluetooth LE, the Dansons WebSocket relay,
and -- on grills that serve it -- Mongoose OS's HTTP RPC endpoint on the
local network. All three present the same `PitBoss` API once connected.
"""

from .api import PitBoss  # noqa: F401
from .ble import BleConnection  # noqa: F401
from .http import HttpConnection  # noqa: F401
from .ota import OTA  # noqa: F401
from .wifi import WiFi  # noqa: F401
from .wss import WebSocketConnection  # noqa: F401
