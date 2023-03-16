Fake PitBoss Firmware
=====================

This directory (and the parent directory's `mos.yml` file) can be used as
a fake PitBoss firmware implementation that can be flashed to an ESP32. This
is useful for testing the Pytboss library without needing to run the grill
or smoker.

The firmware (`init.js`) is a copy of the official Dansons firmware used by
the grill. It can be installed to an ESP32 using the utilities provided by
Mongoose OS (http://www.mongoose-os.com), which is the same base operating
system installed on PitBoss devices. The additioanl files in this directory
implement fake versions of the firmware dependencies that return reasonable
approximations of data that would be returned by a PitBoss device.

Files of note:

*   `api_uart.js` - UART library implementation that mimics the microcontroller
    attached to a PitBoss device's ESP32 UART pins.
*   `lib_ws.js` - WebSocket library implementation that replaces calls with
    NOOP functions. This is a custom library written for the PitBoss firmware
    and is not shipped with Mongoose OS. The fake firware does not need
    WebSocket support (since it doesn't need to talk to the PitBoss cloud
    servers), so the implementation details are irrelevant.
*   `../mos.yml` - A Mongoose OS configuration file that sets up a base image
    and pulls in the required dependencies. Note that this must live in the
    root source directory to appease the VSCode plugin.
