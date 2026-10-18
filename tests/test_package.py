# Smoke tests for the packaging: the packages import and both console scripts are registered.

import importlib
from importlib.metadata import entry_points

import pytest

from teton_configurator import cli as configurator_cli
from teton_device import cli as device_cli


@pytest.mark.parametrize("package", ["teton_common", "teton_device", "teton_configurator"])
def test_packages_import(package: str) -> None:
    importlib.import_module(package)


def test_console_scripts_are_registered() -> None:
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    assert scripts["teton-device"] == "teton_device.cli:main"
    assert scripts["teton-configurator"] == "teton_configurator.cli:main"


@pytest.mark.parametrize("main", [device_cli.main, configurator_cli.main])
def test_help_exits_cleanly(main) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
