"""Test whether a control board accepts the remote power-on command.

For https://github.com/dknowles2/ha-pitboss/issues/322, which reports that
boards accept `FE0101FF` as the mirror of the `FE0102FF` we already send for
off. pytboss declares no `turn-on` slug for any board, so it can only be sent
as a raw hex command.

**This lights a grill.** Not a diagnostic that reads something: a command that
starts a fire in an appliance. The script is built so that cannot happen by
accident, and so that a run which does light one tells you what happened.

    # Read-only. Reports whether ignition would be permitted, sends nothing.
    uv run python -m scripts.try_remote_start --model PBV4PS2

    # Actually send it. Asks you to type a phrase; there is no --yes.
    uv run python -m scripts.try_remote_start --model PBV4PS2 --ignite

    # Send the shutdown command. No confirmation -- off is the safe direction.
    uv run python -m scripts.try_remote_start --model PBV4PS2 --shutdown

Before igniting anything, from the issue and the vendor's own manuals: start a
pellet grill **with the lid open** and the burn pot clear of unburned pellets.
Pellets that accumulate unburned can deflagrate when they finally catch. Stand
at the grill. Do not run this from another room to see if it works.

The pre-flight refuses on the same conditions ha-pitboss#322 proposes gating a
service on -- already on, disconnected, or any of `noPellets`, `highTempErr`,
`erL`, `fanErr`, `motorErr`, `hotErr`. Those are the states where lighting a
grill is a bad idea rather than merely unsupported.
"""

import argparse
import asyncio
import json
import sys
from importlib import resources
from typing import Any

from pytboss import api
from pytboss.grills import StateDict

POWER_ON = "FE0101FF"
"""The command under test. `FE0102FF` is the off mirror pytboss already sends."""

BLOCKING_FLAGS = (
    ("noPellets", "the hopper reports no pellets"),
    ("highTempErr", "a high-temperature error is set"),
    ("erL", "the board reports ErL"),
    ("fanErr", "a fan error is set"),
    ("motorErr", "an auger motor error is set"),
    ("hotErr", "an igniter error is set"),
)

CONFIRM_PHRASE = "light the grill"


def _control_board_prefixes() -> frozenset[str]:
    """Board names from the definitions, for matching BLE local names."""
    raw = json.loads(resources.files("pytboss").joinpath("grills.json").read_text())
    return frozenset(raw["control_boards"])


async def _find_grill(name_hint: str, timeout: float) -> Any:
    from bleak import BleakScanner

    print(f"Scanning {timeout:.0f}s for a grill over Bluetooth...")
    print(
        "(macOS needs Bluetooth permission for this app, or CoreBluetooth aborts\n"
        "the process with exit 134 and no output. This also takes the BLE\n"
        "connection, so Home Assistant will lose it while this runs.)\n"
    )
    prefixes = _control_board_prefixes()
    devices = await BleakScanner.discover(timeout=timeout)
    named = [(d, str(d.name)) for d in devices if d.name]
    if name_hint:
        hint = name_hint.upper()
        matches = [d for d, name in named if name.upper().startswith(hint)]
    else:
        matches = [d for d, name in named if name.upper().split("-")[0] in prefixes]
    if not matches:
        print(f"No grill found among {len(named)} named BLE devices.")
        if named:
            print("Seen: " + ", ".join(sorted({n for _, n in named}))[:300])
        return None
    print(f"Found {matches[0].name} ({matches[0].address})\n")
    return matches[0]


def _describe(state: StateDict) -> None:
    """Print the state that bears on whether it is safe to light."""
    unit = "F" if state.get("isFahrenheit", True) else "C"
    fields = [
        ("moduleIsOn", "grill running"),
        ("fanState", "fan"),
        ("motorState", "auger"),
        ("hotState", "igniter"),
        ("primeState", "primer"),
    ]
    print("  state:")
    for key, label in fields:
        print(f"    {label:<14} {state.get(key)!r}")
    for key in ("grillTemp", "grillSetTemp"):
        if (value := state.get(key)) is not None:
            print(f"    {key:<14} {value}°{unit}")
    print("  error flags:")
    for key, _ in BLOCKING_FLAGS:
        value = state.get(key)
        mark = "  <-- blocking" if value else ""
        print(f"    {key:<14} {value!r}{mark}")


def _blockers(state: StateDict) -> list[str]:
    """Reasons not to light this grill right now."""
    reasons: list[str] = []
    if state.get("moduleIsOn"):
        reasons.append("the grill is already on")
    for key, description in BLOCKING_FLAGS:
        if state.get(key):
            reasons.append(description)
    return reasons


def _confirm() -> bool:
    """Require a typed phrase. Deliberately not scriptable."""
    print()
    print("=" * 72)
    print("  This sends FE0101FF, which LIGHTS THE GRILL.")
    print()
    print("  Only continue if you are standing at it, the lid is OPEN, and the")
    print("  burn pot is clear of unburned pellets. Pellets that pile up")
    print("  unburned can flash when they catch.")
    print("=" * 72)
    try:
        answer = input(f'\nType "{CONFIRM_PHRASE}" to send it: ').strip()
    except EOFError:
        print("\nNo terminal to confirm on. Nothing sent.")
        return False
    if answer != CONFIRM_PHRASE:
        print("Did not match. Nothing sent.")
        return False
    return True


async def _watch(boss: api.PitBoss, seconds: int) -> None:
    """Report what the grill does after the command, so you have evidence."""
    print(
        f"\nWatching for {seconds}s. Ctrl-C to stop watching (does not stop the grill)."
    )
    lit = False
    for elapsed in range(0, seconds, 5):
        await asyncio.sleep(5)
        try:
            state = await boss.get_state()
        except Exception as ex:  # noqa: BLE001 - a read failure here is not fatal
            print(f"  {elapsed + 5:>3}s  could not read state: {ex}")
            continue
        unit = "F" if state.get("isFahrenheit", True) else "C"
        print(
            f"  {elapsed + 5:>3}s  on={state.get('moduleIsOn')!r} "
            f"fan={state.get('fanState')!r} igniter={state.get('hotState')!r} "
            f"auger={state.get('motorState')!r} "
            f"temp={state.get('grillTemp')}°{unit}"
        )
        if state.get("moduleIsOn"):
            lit = True
    print()
    if lit:
        print("RESULT: the grill reported moduleIsOn. The command works on this board.")
        print("Shut it down from the panel, or re-run with --shutdown.")
    else:
        print("RESULT: the grill never reported moduleIsOn.")
        print("Either the board ignores FE0101FF, or it accepted it and did not light.")
        print("Check the grill itself before concluding it did nothing.")


async def _main(args: argparse.Namespace) -> int:
    from pytboss.ble import BleConnection

    device = await _find_grill(args.ble_name, args.ble_scan_timeout)
    if device is None:
        return 1

    conn = BleConnection(device)
    boss = api.PitBoss(conn, args.model, args.password)
    await boss.start()
    try:
        print(
            f"Connected as model {args.model} (board {boss.spec.control_board.name})."
        )
        state = await boss.get_state()
        if not state:
            print("The grill returned no state. Not going any further.")
            return 1
        _describe(state)

        if args.shutdown:
            print("\nSending the shutdown command via turn_grill_off().")
            await boss.turn_grill_off()
            print("Sent.")
            return 0

        blockers = _blockers(state)
        if blockers:
            print("\nIgnition REFUSED:")
            for reason in blockers:
                print(f"  - {reason}")
            return 1

        print("\nPre-flight passed: nothing in the reported state blocks ignition.")
        if not args.ignite:
            print("Read-only run. Nothing sent. Re-run with --ignite to send it.")
            return 0

        if not _confirm():
            return 1

        print(f"\nSending {POWER_ON}...")
        # No public wrapper: no board declares a `turn-on` slug, which is the
        # whole point of the issue. This is the raw-hex path pytboss uses
        # internally for every command it does declare.
        result = await boss._send_hex_command(POWER_ON)
        print(f"Accepted by the RPC layer: {result!r}")
        print("That the call returned does not mean the grill lit. Watching.")
        await _watch(boss, args.watch)
        return 0
    finally:
        await boss.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", required=True, help="Grill model, e.g. PBV4PS2.")
    parser.add_argument("--password", default="", help="Grill password, if one is set.")
    parser.add_argument(
        "--ignite",
        action="store_true",
        help="Actually send FE0101FF. Still asks you to type a phrase.",
    )
    parser.add_argument(
        "--shutdown",
        action="store_true",
        help="Send the off command instead. No confirmation; off is the safe way.",
    )
    parser.add_argument(
        "--watch", type=int, default=60, help="Seconds to watch after igniting."
    )
    parser.add_argument("--ble-name", default="", help="BLE local name prefix.")
    parser.add_argument(
        "--ble-scan-timeout", type=float, default=10.0, help="Seconds to scan for."
    )
    args = parser.parse_args()
    if args.ignite and args.shutdown:
        parser.error("--ignite and --shutdown are opposites; pick one")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
