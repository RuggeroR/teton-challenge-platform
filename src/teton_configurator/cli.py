# `teton-configurator [--simulate] [--verbose]` entry point; starts the guided flow.
# Placeholder until Step 5.2: only parses --help so the console script can be installed.

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="teton-configurator",
        description="Guides a technician through connecting a Teton device to Wi-Fi.",
    )
    parser.parse_args(argv)
    print("teton-configurator: not implemented yet", file=sys.stderr)
    return 1
