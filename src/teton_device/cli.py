# `teton-device` CLI: run [--simulate], reset, show-code, --state-dir, --verbose. See SPEC §9.1.
# Placeholder until Step 5.2: only parses --help so the console script can be installed.

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="teton-device",
        description="Teton Device: receives Wi-Fi credentials over Bluetooth and joins Wi-Fi.",
    )
    parser.parse_args(argv)
    print("teton-device: not implemented yet", file=sys.stderr)
    return 1
