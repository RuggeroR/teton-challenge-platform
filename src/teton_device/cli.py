# `teton-device run [--simulate] [--state-dir PATH] [--verbose]`. See SPEC §9.1.
#
# Only wiring (review Q79): pick the transport, the Wi-Fi backend and the state directory,
# set up the screen and the logs, and run DeviceService until [Q], Ctrl+C or SIGTERM.
# There are no other subcommands: reset is the [R] button, and the code + QR are printed
# on start and after every reset (plan_v5 D2).
#
# The screen (code, QR, LED, messages) is stdout; the JSON logs go to stderr, through the
# status line so they don't break the LED line (review Q73).

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from teton_common.log import configure_logging
from teton_common.protocol import local_name
from teton_common.transport import DeviceTransport, TransportError
from teton_device.buttons import ButtonSource, StatusLine, TerminalButtons
from teton_device.gatt_server import GattServerTransport
from teton_device.service import DeviceService
from teton_device.sim_server import SimServerTransport
from teton_device.storage import DEFAULT_STATE_DIR, Storage, StorageError
from teton_device.wifi.backend import WifiBackend
from teton_device.wifi.fake import FakeWifiBackend
from teton_device.wifi.nmcli import NmcliBackend

SIM_STATE_DIR = Path.home() / ".local" / "state" / "teton-device-sim"  # review Q77


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="teton-device",
        description="Teton Device: receives Wi-Fi credentials over Bluetooth and joins Wi-Fi.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="run")
    run = commands.add_parser(
        "run",
        help="turn the device on",
        description="Turn the device on. Keys: P pairing, R R reset, S status, Q power off.",
    )
    run.add_argument(
        "--simulate",
        action="store_true",
        help="use a local socket and simulated Wi-Fi instead of Bluetooth and NetworkManager",
    )
    run.add_argument(
        "--state-dir",
        type=Path,
        help=f"where the identity and setup code are kept (default {DEFAULT_STATE_DIR}, "
        f"or {SIM_STATE_DIR} with --simulate)",
    )
    run.add_argument("--verbose", action="store_true", help="debug logs (on stderr)")
    return parser.parse_args(argv)


def state_dir_for(args: argparse.Namespace) -> Path:
    """A simulated run never touches a real device's state (review Q77)."""
    if args.state_dir is not None:
        return args.state_dir
    return SIM_STATE_DIR if args.simulate else DEFAULT_STATE_DIR


async def run_device(args: argparse.Namespace, buttons: ButtonSource | None = None) -> int:
    """Runs the device until [Q]. Ctrl+C and SIGTERM cancel this; the service still closes
    the transport and the buttons restore the terminal on the way out (review Q76)."""
    screen = StatusLine(sys.stdout, log_stream=sys.stderr)
    configure_logging(verbose=args.verbose, stream=screen)
    storage = Storage(state_dir_for(args))
    transport, wifi = radio_and_wifi(args, storage)
    service = DeviceService(
        transport, wifi, storage, buttons or TerminalButtons(), status_line=screen
    )
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    assert main_task is not None
    loop.add_signal_handler(signal.SIGTERM, main_task.cancel)  # like Ctrl+C
    try:
        await service.run()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        screen.end()
    return 0


def radio_and_wifi(
    args: argparse.Namespace, storage: Storage
) -> tuple[DeviceTransport, WifiBackend]:
    """The simulated pair, or Bluetooth and NetworkManager. Neither imports bless here: the
    BLE transport imports it when it starts (review Q84)."""
    if args.simulate:
        return SimServerTransport(), FakeWifiBackend()
    # The radio needs the name before the service loads the state, so load it here
    # (review Q81). A corrupt state.json fails now, with one line.
    device_id = storage.load_or_create().device_id
    return GattServerTransport(local_name(device_id)), NmcliBackend(state_dir=state_dir_for(args))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run_device(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0  # Ctrl+C or SIGTERM: run_device has already shut down cleanly
    except (StorageError, TransportError) as exc:
        print(f"teton-device: {exc}", file=sys.stderr)
        return 1
