# `teton-configurator [--simulate] [--verbose]`: starts the guided flow. See SPEC §9.2.
#
# Only wiring (review Q79): pick discovery and the transport, set up the logs, and hand
# them to run_flow with the real terminal (TerminalIO). Logs go to stderr at WARNING, so
# they don't break up the technician's guided text; --verbose gives DEBUG (review Q74).
# Ctrl+C restores the terminal (getpass may have turned echo off), shows the "stopped"
# text and exits 130; run_flow has already closed the transport (review Q75).

import argparse
import asyncio
import logging
import os
import sys
import termios
from pathlib import Path
from typing import Any

from teton_common.log import configure_logging
from teton_configurator import ui_text
from teton_configurator.flow import Connect, Discover, FlowIO, FoundDevice, TerminalIO, run_flow

BLE_NOT_AVAILABLE = "BLE transport not available yet; use --simulate."
EXIT_INTERRUPTED = 130  # the shell convention for Ctrl+C (128 + SIGINT)


class StartupError(Exception):
    """The configurator can't start. The text is one line for stderr."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="teton-configurator",
        description="Guides a technician through connecting a Teton device to Wi-Fi.",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="connect to a simulated device on this computer instead of over Bluetooth",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logs (on stderr)")
    return parser.parse_args(argv)


async def run_configurator(args: argparse.Namespace, io: FlowIO | None = None) -> int:
    """The exit code of the guided flow: 0 when the device is connected, else 1."""
    discover, connect = _radio(args.simulate)
    configure_logging(verbose=args.verbose, stream=sys.stderr, default_level=logging.WARNING)
    return await run_flow(discover, connect, io or TerminalIO())


def _radio(simulate: bool) -> tuple[Discover, Connect]:
    if simulate:
        from teton_configurator import sim_client

        def connect(found: FoundDevice) -> sim_client.SimClientTransport:
            return sim_client.SimClientTransport(Path(found.address))

        return sim_client.discover, connect
    # Imported only here, so bleak is never loaded in simulation or in the tests. Until
    # Phase 6 writes them, this import fails.
    try:
        from teton_configurator.ble_client import (  # type: ignore[attr-defined]
            BleClientTransport,
            discover,
        )
    except ImportError:
        raise StartupError(BLE_NOT_AVAILABLE) from None
    return discover, BleClientTransport


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    saved = _terminal_settings()
    try:
        return asyncio.run(run_configurator(args))
    except KeyboardInterrupt:
        _restore_terminal(saved)
        print("\n" + ui_text.STOPPED, flush=True)
        sys.stderr.flush()
        # The thread reading the answer is still blocked in input() and holds the C
        # library's stdin lock, and a normal exit would wait for that lock for ever. The
        # transport is closed and the output flushed, so end the process right here.
        os._exit(EXIT_INTERRUPTED)
    except StartupError as exc:
        print(f"teton-configurator: {exc}", file=sys.stderr)
        return 1


def _terminal_settings() -> list[Any] | None:
    try:
        return termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, OSError, ValueError):
        return None  # not a terminal: nothing to restore


def _restore_terminal(saved: list[Any] | None) -> None:
    if saved is not None:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
