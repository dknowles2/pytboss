"""Probe a grill's local Mongoose OS RPC endpoint over HTTP.

Tests the hypothesis in https://github.com/dknowles2/pytboss/issues/505: that
the ESP32 firmware serves the same RPC interface at `http://<grill-ip>/rpc`
that pytboss currently reaches through the Dansons websocket, and that the
existing command mappings and status decoders work against it unchanged.

Read-only by default. Nothing here changes the grill's configuration or its
state unless you pass `--send-command`, which is gated behind a confirmation.

Usage::

    uv run python -m scripts.probe_local_rpc 192.168.1.50
    uv run python -m scripts.probe_local_rpc 192.168.1.50 --model PBV4PS2
    uv run python -m scripts.probe_local_rpc 192.168.1.50 --model PBV4PS2 \
        --password hunter2

What the stages tell you:

1. **Reachability** -- whether anything answers on port 80 at all. The issue
   notes this may be board- or firmware-specific, so a refusal here is a
   result, not a failure of the script.
2. **RPC.List** -- the decisive one. If the `PB.*` methods are present, the
   local endpoint exposes the same namespace as the websocket rather than a
   subset.
3. **Sys.GetInfo / Config.Get** -- what board and firmware answered, so a
   negative result from someone else's grill can be compared against yours.
4. **PB.GetState decoded by pytboss** -- the actual claim. The raw frames are
   run through `ControlBoard.parse_status` / `parse_temperatures`, the same
   code the websocket transport feeds. Readable values out the other end mean
   a local transport is a wiring exercise rather than a protocol one.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from asyncio import AbstractEventLoop
from importlib import resources
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from pytboss import api, grills
from pytboss.exceptions import InvalidGrill, RPCError
from pytboss.transport import Transport

_LOGGER = logging.getLogger("probe_local_rpc")

# Config subtrees are dumped verbatim, and a grill's config holds the wifi
# credentials of whoever runs this. Blank anything whose key looks secret
# before it reaches a terminal or a pasted bug report.
_SECRET_KEY = re.compile(r"pass|psk|secret|token|_key$|^key$|cert", re.IGNORECASE)

# Methods worth reporting on individually: the ones pytboss actually uses.
_METHODS_PYTBOSS_USES = (
    "PB.GetState",
    "PB.SendMCUCommand",
    "PB.GetFirmwareVersion",
    "PB.SetDeviceStatus",
    "PB.GetVirtualData",
    "PB.SetVirtualData",
    "PB.GetTime",
    "Sys.GetInfo",
    "Config.Get",
)


class LocalRpcConnection(Transport):
    """Transport that speaks Mongoose OS RPC over plain HTTP.

    HTTP is strictly request/response, so unlike the BLE and websocket
    transports there is no push channel: `subscribe_state()` will never fire.
    Polling `get_state()` is the only way to see state, which is why
    #516 (folding the reply into the cache) matters for this transport.
    """

    def __init__(
        self,
        host: str,
        *,
        timeout: float = 5.0,
        loop: AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(loop=loop)
        self._url = f"http://{host}/rpc"
        self._timeout = ClientTimeout(total=timeout)
        self._session: ClientSession | None = None

    async def connect(self) -> None:
        self._session = ClientSession(timeout=self._timeout, loop=self._loop)

    async def disconnect(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def is_connected(self) -> bool:
        return self._session is not None and not self._session.closed

    async def _send_prepared_command(self, cmd: dict) -> None:
        if self._session is None:
            raise RPCError("Not connected")
        _LOGGER.debug("--> %s", cmd)
        async with self._session.post(self._url, json=cmd) as resp:
            resp.raise_for_status()
            # Mongoose does not always set a JSON content type.
            payload = await resp.json(content_type=None)
        _LOGGER.debug("<-- %s", payload)
        if not isinstance(payload, dict):
            raise RPCError(f"Expected a JSON object, got {type(payload).__name__}")
        # An HTTP reply belongs to the request that produced it, whatever id
        # the firmware chose to echo. Coerce it so a mismatch surfaces as a
        # note rather than as a mysterious timeout.
        if payload.get("id") != cmd["id"]:
            _LOGGER.debug("Reply id %r != request id %r", payload.get("id"), cmd["id"])
            payload["id"] = cmd["id"]
        await self._on_command_response(payload)


def _redact(value: Any) -> Any:
    """Blank anything that looks like a credential, preserving shape."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _SECRET_KEY.search(k) and v not in (None, "", 0):
                out[k] = f"<redacted, {len(str(v))} chars>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _section(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * max(4, 66 - len(title))}")


def _result(ok: bool | None, message: str) -> None:
    mark = {True: "PASS", False: "FAIL", None: "----"}[ok]
    print(f"[{mark}] {message}")


def _dump(value: Any) -> None:
    print(json.dumps(_redact(value), indent=2, sort_keys=True))


async def _call(conn: Transport, method: str, params: dict | None = None) -> Any:
    """Send one RPC, returning the result or raising."""
    return await conn.send_command(method, params or {})


async def _try(conn: Transport, method: str, params: dict | None = None) -> Any | None:
    """Send one RPC, reporting rather than raising on failure."""
    try:
        result = await _call(conn, method, params)
    except RPCError as ex:
        _result(False, f"{method} -> RPC error: {ex}")
    except TimeoutError:
        _result(False, f"{method} -> timed out")
    except ClientError as ex:
        _result(False, f"{method} -> transport error: {ex}")
    else:
        _result(True, f"{method} answered")
        return result
    return None


def _guess_boards(device_id: str | None) -> list[str]:
    """Control boards whose prefix matches the device id, if any."""
    if not device_id:
        return []
    prefix = device_id.split("-")[0].strip().upper()
    if not prefix:
        return []
    try:
        models = sorted(g.name for g in grills.get_grills(prefix))
    except Exception:  # noqa: BLE001 - a bad prefix is not worth failing over
        return []
    return models


def _find_device_id(sys_info: Any, config: Any) -> str | None:
    """Dig the grill's own identifier out of whatever answered."""
    for source, path in (
        (config, ("device", "id")),
        (sys_info, ("id",)),
        (sys_info, ("device_id",)),
    ):
        node = source
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str) and node:
            return node
    return None


async def _probe_raw(conn: Transport, show_config: bool) -> dict[str, Any]:
    """Stage 1: unauthenticated pokes that need no grill model."""
    findings: dict[str, Any] = {}

    _section("RPC.List -- which methods does the local endpoint expose?")
    methods = await _try(conn, "RPC.List")
    findings["methods"] = methods
    if isinstance(methods, list):
        available = {str(m) for m in methods}
        print(f"\n{len(available)} methods advertised. The ones pytboss uses:")
        for method in _METHODS_PYTBOSS_USES:
            _result(method in available, f"  {method}")
        extra = sorted(m for m in available if m.startswith("PB."))
        print(f"\nAll PB.* methods: {', '.join(extra) or '(none)'}")
    elif methods is not None:
        print("Unexpected RPC.List shape:")
        _dump(methods)

    _section("Sys.GetInfo -- what answered?")
    sys_info = await _try(conn, "Sys.GetInfo")
    findings["sys_info"] = sys_info
    if sys_info is not None:
        _dump(sys_info)

    _section("Config.Get -- is the HTTP server configured on, or incidental?")
    config = await _try(conn, "Config.Get")
    findings["config"] = config
    if isinstance(config, dict):
        http = config.get("http")
        if isinstance(http, dict):
            print("http section:")
            _dump(http)
            listening = http.get("enable")
            _result(
                bool(listening),
                f"http.enable is {listening!r}"
                + (
                    ""
                    if listening
                    else " -- reachable anyway, so something else is serving"
                ),
            )
        else:
            _result(None, "No `http` section in the config")
        if show_config:
            print("\nFull config (secrets redacted):")
            _dump(config)
        else:
            print("\n(pass --show-config for the whole tree, secrets redacted)")

    findings["device_id"] = _find_device_id(sys_info, config)
    if findings["device_id"]:
        models = _guess_boards(findings["device_id"])
        _result(None, f"Device id: {findings['device_id']}")
        if models:
            print(
                f"That prefix maps to {len(models)} known model(s), e.g. "
                f"{', '.join(models[:5])}" + (" ..." if len(models) > 5 else "")
            )
            print("Re-run with --model <name> to exercise the decoders.")

    _section("PB.GetState -- unauthenticated")
    state = await _try(conn, "PB.GetState")
    findings["raw_state"] = state
    if isinstance(state, dict):
        for key in ("sc_11", "sc_12"):
            _result(
                key in state,
                f"  {key} present" + (f": {state[key]}" if key in state else ""),
            )
        unexpected = sorted(set(state) - {"sc_11", "sc_12"})
        if unexpected:
            print(f"Other keys returned: {', '.join(unexpected)}")
    elif state is not None:
        _dump(state)
    return findings


def _decode_raw(model: str, state: dict[str, Any]) -> None:
    """Run raw frames through pytboss's own parsers."""
    try:
        spec = grills.get_grill(model)
    except InvalidGrill as ex:
        _result(False, f"Unknown model {model!r}: {ex}")
        return
    board = spec.control_board
    print(f"Decoding with control board {board.name} (model {spec.name}).")
    for key, parse in (
        ("sc_11", board.parse_status),
        ("sc_12", board.parse_temperatures),
    ):
        payload = state.get(key)
        if not payload:
            _result(None, f"{key} absent, nothing to decode")
            continue
        try:
            decoded = parse(payload)
        except Exception as ex:  # noqa: BLE001 - vendor JS, anything can happen
            _result(False, f"{key} failed to decode: {ex}")
            continue
        if not decoded:
            _result(False, f"{key} decoded to nothing -- wrong board for this grill?")
            continue
        _result(True, f"{key} decoded to {len(decoded)} fields")
        _dump(decoded)


async def _probe_with_model(
    host: str, model: str, password: str, timeout: float, board: str | None
) -> None:
    """Stage 2: drive the real PitBoss API over the local transport."""
    _section(f"pytboss end-to-end as model {model}")
    conn = LocalRpcConnection(host, timeout=timeout)
    boss = api.PitBoss(conn, model, password, control_board=board)
    try:
        await boss.start()
    except InvalidGrill as ex:
        _result(False, f"Could not resolve the model: {ex}")
        return
    except (ClientError, TimeoutError, RPCError) as ex:
        _result(False, f"start() failed: {ex}")
        return

    _result(True, f"start() resolved spec: board {boss.spec.control_board.name}")
    try:
        try:
            firmware = await boss.get_firmware_version()
            _result(True, f"get_firmware_version() -> {firmware}")
        except (ClientError, TimeoutError, RPCError) as ex:
            _result(False, f"get_firmware_version() failed: {ex}")

        try:
            state = await boss.get_state()
        except RPCError as ex:
            _result(False, f"get_state() rejected: {ex}")
            if password:
                print(
                    "A password was supplied. If this says the password is wrong, the\n"
                    "local endpoint is enforcing auth the same way the websocket does."
                )
            else:
                print(
                    "No password was supplied. If this looks like an auth failure,\n"
                    "retry with --password: the local endpoint is NOT open."
                )
            return
        except (ClientError, TimeoutError) as ex:
            _result(False, f"get_state() failed: {ex}")
            return

        if not state:
            _result(False, "get_state() returned nothing -- frames did not decode")
            return
        _result(True, f"get_state() decoded {len(state)} fields through pytboss")
        _dump(dict(state))
        _result(
            bool(boss._state),
            "the cached state was populated by the poll (needs pytboss > 2026.8.2)",
        )
    finally:
        await boss.stop()


async def _send_mcu_command(
    host: str, model: str, password: str, timeout: float, command: str, assume_yes: bool
) -> None:
    _section("PB.SendMCUCommand -- this changes the grill")
    print(f"About to send MCU command {command!r} to {host}.")
    print("This is a real command to a real appliance. Check it before continuing.")
    if not assume_yes:
        try:
            answer = input("Type the command again to confirm: ").strip()
        except EOFError:
            _result(None, "No terminal to confirm on; skipping. Use --yes to override.")
            return
        if answer != command:
            _result(None, "Did not match; nothing sent.")
            return

    conn = LocalRpcConnection(host, timeout=timeout)
    if model:
        boss = api.PitBoss(conn, model, password)
        await boss.start()
        try:
            result = await boss._send_hex_command(command)
            _result(True, f"sent with authentication -> {result}")
        except RPCError as ex:
            _result(False, f"rejected: {ex}")
        finally:
            await boss.stop()
        return

    await conn.connect()
    try:
        result = await _call(conn, "PB.SendMCUCommand", {"command": command})
        _result(True, f"sent unauthenticated -> {result}")
    except RPCError as ex:
        _result(False, f"rejected: {ex}")
    finally:
        await conn.disconnect()


def _control_board_prefixes() -> frozenset[str]:
    """Every board name in the definitions, for matching BLE local names.

    Read from the packaged JSON rather than `get_grills()`: that yields each
    model once, which drops the boards that only appear on shadow variants --
    `PBL2`, `PBVA` and `PBX1` today, and `PBL2` is the one most worth finding.
    """
    raw = json.loads(resources.files("pytboss").joinpath("grills.json").read_text())
    return frozenset(raw["control_boards"])


async def _find_ble_device(name_hint: str, timeout: float) -> Any:
    """Scan for a grill advertising a known control board prefix."""
    from bleak import BleakScanner

    prefixes = _control_board_prefixes()
    print(
        "Note: on macOS the scan needs Bluetooth permission for whichever app is\n"
        "running this. Without it CoreBluetooth aborts the process outright --\n"
        "exit 134, no traceback, no output. That is a permissions problem, not a\n"
        "grill problem: grant it under Privacy & Security > Bluetooth and retry.\n"
        "\n"
        "This also opens a BLE connection to the grill. If Home Assistant is\n"
        "talking to it over BLE right now, one of the two will lose.\n"
    )
    print(
        f"Scanning {timeout:.0f}s for a device named {name_hint or 'like a grill'}..."
    )
    devices = await BleakScanner.discover(timeout=timeout)
    named = [(d, str(d.name)) for d in devices if d.name]
    if name_hint:
        hint = name_hint.upper()
        matches = [d for d, name in named if name.upper().startswith(hint)]
    else:
        matches = [d for d, name in named if name.upper().split("-")[0] in prefixes]
    if not matches:
        _result(False, f"No grill found among {len(named)} named BLE devices")
        if named:
            print("Seen: " + ", ".join(sorted({name for _, name in named}))[:400])
        print(
            "\nPass --ble-name <prefix> if your grill advertises something "
            "unexpected,\nor move closer to it. Grills only advertise while awake."
        )
        return None
    device = matches[0]
    _result(True, f"Found {device.name} ({device.address})")
    return device


async def _probe_over_ble(args: argparse.Namespace) -> int:
    """Ask the grill over BLE whether it *could* serve RPC over HTTP.

    A closed port says nothing about why. This distinguishes the two answers
    that matter for the issue: the HTTP service exists in this firmware but is
    switched off, or it is not in the build at all.
    """
    from pytboss.ble import BleConnection

    _section("Bluetooth LE -- what does the firmware itself say?")
    device = await _find_ble_device(args.ble_name, args.ble_scan_timeout)
    if device is None:
        return 1

    conn = BleConnection(device)
    await conn.connect()
    try:
        _section("Sys.GetInfo over BLE")
        sys_info = await _try(conn, "Sys.GetInfo")
        if sys_info is not None:
            _dump(sys_info)
            if isinstance(sys_info, dict):
                print(
                    "\nRecord `fw_version` and `app` on the issue -- firmware build is\n"
                    "the variable the reporters and you differ on."
                )

        _section("RPC.List over BLE")
        methods = await _try(conn, "RPC.List")
        if isinstance(methods, list):
            available = {str(m) for m in methods}
            print(f"{len(available)} methods advertised.")
            for method in ("Config.Get", "Config.Set", "Config.Save", "PB.GetState"):
                _result(method in available, f"  {method}")

        _section("Config.Get http -- is the server off, or absent?")
        http = await _try(conn, "Config.Get", {"key": "http"})
        if http is None:
            print(
                "No `http` subtree came back. If Config.Get itself worked above,\n"
                "that points at the HTTP service not being in this firmware build."
            )
        else:
            _dump(http)
            enabled = http.get("enable") if isinstance(http, dict) else None
            if enabled:
                _result(
                    None,
                    "http.enable is true, yet nothing listens -- check the grill is on"
                    " wifi rather than only BLE, and that listen_addr matches",
                )
            else:
                _result(
                    None,
                    "http.enable is false: the service exists in this build but is off",
                )
                print(
                    "\nEnabling it would mean Config.Set + Config.Save, which writes\n"
                    "flash and reboots the grill. This script will not do that, and it\n"
                    "is worth deciding on the issue before anyone does it to a grill\n"
                    "they cannot physically reach."
                )

        if args.show_config:
            _section("Full config over BLE")
            config = await _try(conn, "Config.Get")
            if config is not None:
                _dump(config)
    finally:
        await conn.disconnect()
    return 0


async def _main(args: argparse.Namespace) -> int:
    if args.via == "ble":
        return await _probe_over_ble(args)

    _section(f"Reachability -- http://{args.host}/rpc")
    conn = LocalRpcConnection(args.host, timeout=args.timeout)
    await conn.connect()
    try:
        try:
            await _call(conn, "Sys.GetInfo")
        except (ClientError, TimeoutError) as ex:
            _result(False, f"Nothing usable answered: {ex}")
            print(
                "\nThat is a valid result for this issue: the endpoint is reported to\n"
                "be board- or firmware-specific. Worth recording your board and\n"
                "firmware version on the issue alongside this."
            )
            return 1
        except RPCError as ex:
            _result(True, f"Answered, but refused Sys.GetInfo: {ex}")
        else:
            _result(True, "The endpoint answered")

        findings = await _probe_raw(conn, args.show_config)
    finally:
        await conn.disconnect()

    raw_state = findings.get("raw_state")
    if args.model and isinstance(raw_state, dict):
        _section("Decoding the raw frames with pytboss")
        _decode_raw(args.model, raw_state)

    if args.model:
        await _probe_with_model(
            args.host, args.model, args.password, args.timeout, args.control_board
        )
    else:
        _section("Next step")
        print(
            "Re-run with --model <name> to exercise pytboss's decoders end to end.\n"
            "Without a model there is no control board to parse the frames with."
        )

    if args.send_command:
        await _send_mcu_command(
            args.host,
            args.model,
            args.password,
            args.timeout,
            args.send_command,
            args.yes,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "host",
        nargs="?",
        default="",
        help="Grill IP address or hostname. Not needed with --via ble.",
    )
    parser.add_argument(
        "--via",
        choices=("http", "ble"),
        default="http",
        help=(
            "http (default) probes the local endpoint. ble asks the grill over "
            "Bluetooth whether it could serve one at all -- use this when the "
            "port is closed, since that alone does not say why."
        ),
    )
    parser.add_argument(
        "--ble-name",
        default="",
        help="BLE local name prefix, if the grill advertises something unexpected.",
    )
    parser.add_argument(
        "--ble-scan-timeout", type=float, default=10.0, help="Seconds to scan for."
    )
    parser.add_argument(
        "--model",
        default="",
        help="Grill model, e.g. PBV4PS2. Needed to decode state frames.",
    )
    parser.add_argument(
        "--control-board",
        default=None,
        help="Pin the control board for models sold on two generations.",
    )
    parser.add_argument("--password", default="", help="Grill password, if one is set.")
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="Per-request timeout in seconds."
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Dump the whole config tree, with secrets redacted.",
    )
    parser.add_argument(
        "--send-command",
        default="",
        metavar="HEX",
        help="Send an MCU command. Changes the grill; asks for confirmation.",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the --send-command confirmation."
    )
    parser.add_argument("--debug", action="store_true", help="Log every request.")
    args = parser.parse_args()
    if args.via == "http" and not args.host:
        parser.error("a host is required unless --via ble is given")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
