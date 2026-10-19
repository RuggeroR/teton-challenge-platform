# Tests for the two CLIs, which only wire components (review Q79): argument parsing, the
# state directory, the simulated device run with injected buttons, the radio and Wi-Fi each
# mode uses, Bluetooth off on the configurator, startup errors, no bless/bleak import
# in simulation, and the real processes: a two-process smoke test over the socket (review
# Q80), SIGTERM on the device (Q76) and Ctrl+C on the configurator (Q75).
#
# The subprocess tests read output with a deadline instead of sleeping: each step waits
# for the line that shows the previous one has happened.

import argparse
import fcntl
import io
import json
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from fake_configurator import CODE, DEVICE_ID, ScriptedIO

from teton_common.setup_code import format_code
from teton_common.sim_wire import SOCKET_NAME
from teton_common.transport import TransportError
from teton_configurator import cli as configurator_cli
from teton_configurator import sim_client, ui_text
from teton_configurator.ble_client import BleDiscovery
from teton_configurator.flow import FoundDevice, run_flow
from teton_device import cli as device_cli
from teton_device.buttons import Button, QueueButtons
from teton_device.gatt_server import GattServerTransport
from teton_device.sim_server import SimServerTransport
from teton_device.storage import DEFAULT_STATE_DIR, Storage
from teton_device.wifi.fake import HOME_PSK_PASSWORD, FakeWifiBackend
from teton_device.wifi.nmcli import NmcliBackend

WAIT_S = 15  # real seconds; a step normally takes well under one (the fake Wi-Fi: 2 + 3 s)


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    """The CLIs configure structlog globally; put it back after each test."""
    yield
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


@pytest.fixture
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the simulation socket goes, private to the test."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path


def seed_state(state_dir: Path) -> None:
    """A device with a known identity and setup code, as if it had run before."""
    Storage(state_dir, new_code=lambda: CODE, new_id=lambda: DEVICE_ID).load_or_create()


def device_args(*extra: str) -> argparse.Namespace:
    return device_cli.parse_args(["run", *extra])


# --- argument parsing -------------------------------------------------------------------


def test_device_run_flags() -> None:
    args = device_args("--simulate", "--state-dir", "/tmp/teton-sim", "--verbose")
    assert args.command == "run"
    assert args.simulate and args.verbose
    assert args.state_dir == Path("/tmp/teton-sim")


def test_device_run_defaults() -> None:
    args = device_args()
    assert not args.simulate and not args.verbose and args.state_dir is None


@pytest.mark.parametrize("command", ["reset", "show-code", "provision"])
def test_device_has_no_other_commands(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Reset is the [R] button and the code is printed on start (plan_v5 D2)."""
    with pytest.raises(SystemExit) as exit_info:
        device_cli.parse_args([command])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_device_needs_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        device_cli.parse_args([])
    assert exit_info.value.code == 2
    assert "run" in capsys.readouterr().err


def test_state_dir_defaults() -> None:
    """A simulated run never touches a real device's state (review Q77)."""
    assert device_cli.state_dir_for(device_args()) == DEFAULT_STATE_DIR
    assert device_cli.state_dir_for(device_args("--simulate")) == device_cli.SIM_STATE_DIR
    assert device_cli.SIM_STATE_DIR != DEFAULT_STATE_DIR
    chosen = device_args("--simulate", "--state-dir", "/somewhere")
    assert device_cli.state_dir_for(chosen) == Path("/somewhere")


def test_configurator_flags() -> None:
    args = configurator_cli.parse_args(["--simulate", "--verbose"])
    assert args.simulate and args.verbose
    args = configurator_cli.parse_args([])
    assert not args.simulate and not args.verbose


def test_configurator_rejects_unknown_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        configurator_cli.parse_args(["run"])
    assert exit_info.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


# --- the radio and Wi-Fi each mode uses -------------------------------------------------


def test_without_simulate_the_device_uses_bluetooth_and_networkmanager(tmp_path: Path) -> None:
    """The BLE name comes from the stored device ID (review Q81); NetworkManager keeps CA
    certificates under --state-dir (Q88)."""
    seed_state(tmp_path)
    args = device_args("--state-dir", str(tmp_path))
    transport, wifi = device_cli.radio_and_wifi(args, Storage(tmp_path))
    assert isinstance(transport, GattServerTransport)
    assert transport.name == "Teton-9A2C"
    assert isinstance(wifi, NmcliBackend)
    assert wifi._certs == tmp_path / "certs"


def test_with_simulate_the_device_uses_the_socket_and_fake_wifi(
    tmp_path: Path, runtime_dir: Path
) -> None:
    args = device_args("--simulate", "--state-dir", str(tmp_path))
    transport, wifi = device_cli.radio_and_wifi(args, Storage(tmp_path))
    assert isinstance(transport, SimServerTransport)
    assert transport.path == runtime_dir / SOCKET_NAME
    assert isinstance(wifi, FakeWifiBackend)
    assert not (tmp_path / "state.json").exists()  # the simulation loads it in the service


def test_without_simulate_a_corrupt_state_file_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The BLE path loads the state before the service does (review Q81); the same one line."""
    (tmp_path / "state.json").write_text("{not json")
    assert device_cli.main(["run", "--state-dir", str(tmp_path)]) == 1
    assert capsys.readouterr().err.splitlines()[-1].endswith("is not readable JSON")


def test_without_simulate_the_configurator_uses_ble_discovery() -> None:
    discover, connect = configurator_cli._radio(simulate=False)
    assert isinstance(discover.__self__, BleDiscovery)  # type: ignore[attr-defined]
    assert connect.__self__ is discover.__self__  # type: ignore[attr-defined]


async def test_bluetooth_off_is_one_sentence_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No adapter or Bluetooth off: the technician gets one plain sentence (review Q99)."""

    async def no_bluetooth(seconds: float) -> list[tuple[object, object]]:
        raise OSError("org.bluez.Error.NotReady: Resource Not Ready")

    monkeypatch.setattr(configurator_cli, "BleDiscovery", lambda: BleDiscovery(no_bluetooth))
    io = ScriptedIO()
    assert await configurator_cli.run_configurator(configurator_cli.parse_args([]), io) == 1
    assert io.lines[-1] == ui_text.BLUETOOTH_UNAVAILABLE


# --- the simulated device, in this process ------------------------------------------------


async def test_run_simulate_starts_and_stops_cleanly(
    tmp_path: Path, runtime_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    buttons = QueueButtons()
    buttons.press(Button.PAIR, Button.POWER)
    args = device_args("--simulate", "--state-dir", str(state_dir))

    assert await device_cli.run_device(args, buttons) == 0

    assert (state_dir / "state.json").exists()  # --state-dir is respected
    assert not (runtime_dir / SOCKET_NAME).exists()  # removed on power off
    out = capsys.readouterr()
    assert "Pairing window open: 5:00." in out.out
    assert "LED blinking blue" in out.out
    events = [json.loads(line)["event"] for line in out.err.splitlines()]  # all JSON
    assert {"device_started", "advertising", "power_off", "device_stopped"} <= set(events)
    assert "{" not in out.out  # no log line on the screen (review Q73)


async def test_run_simulate_keeps_the_identity(tmp_path: Path, runtime_dir: Path) -> None:
    seed_state(tmp_path)
    buttons = QueueButtons()
    buttons.press(Button.POWER)
    await device_cli.run_device(device_args("--simulate", "--state-dir", str(tmp_path)), buttons)
    stored = Storage(tmp_path).load_or_create()
    assert (stored.device_id, stored.setup_code) == (DEVICE_ID, CODE)


async def test_a_second_simulated_device_does_not_start(tmp_path: Path, runtime_dir: Path) -> None:
    other = SimServerTransport()
    await other.start()
    try:
        buttons = QueueButtons()
        buttons.press(Button.POWER)
        args = device_args("--simulate", "--state-dir", str(tmp_path))
        with pytest.raises(TransportError, match="another simulated device is already running"):
            await device_cli.run_device(args, buttons)
        assert (runtime_dir / SOCKET_NAME).exists()  # the running one keeps its socket
    finally:
        await other.close()


def test_a_corrupt_state_file_is_one_line_and_exit_1(
    tmp_path: Path, runtime_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "state.json").write_text("{not json")
    assert device_cli.main(["run", "--simulate", "--state-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[-1] == f"teton-device: {tmp_path / 'state.json'} is not readable JSON"


async def test_configurator_simulate_with_no_device(runtime_dir: Path) -> None:
    io = ScriptedIO("q")
    assert await configurator_cli.run_configurator(configurator_cli.parse_args(["--simulate"]), io)
    assert ui_text.NO_DEVICE_FOUND in io.lines
    assert io.lines[-1] == ui_text.STOPPED


async def test_configurator_logs_only_warnings(
    runtime_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await configurator_cli.run_configurator(
        configurator_cli.parse_args(["--simulate"]), ScriptedIO()
    )
    structlog.get_logger().info("not_shown")
    structlog.get_logger().warning("shown")
    err = capsys.readouterr().err
    assert "not_shown" not in err and "shown" in err


# --- no bless/bleak in simulation (a fresh interpreter) ---------------------------------

NO_RADIO_LIBRARIES = """
import asyncio, sys
from teton_configurator import cli as configurator_cli
from teton_device import cli as device_cli
from teton_device.buttons import Button, QueueButtons
from teton_device.gatt_server import GattServerTransport

class Quit:
    async def ask(self, prompt): raise EOFError
    async def ask_secret(self, prompt): raise EOFError
    def show(self, text): pass

async def main():
    buttons = QueueButtons()
    buttons.press(Button.PAIR, Button.POWER)
    args = device_cli.parse_args(["run", "--simulate", "--state-dir", sys.argv[1]])
    await device_cli.run_device(args, buttons)
    await configurator_cli.run_configurator(configurator_cli.parse_args(["--simulate"]), Quit())

asyncio.run(main())
print("radio libraries:", sorted(m for m in sys.modules if m.split(".")[0] in ("bless", "bleak")))
"""


def test_simulation_never_imports_bless_or_bleak(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", NO_RADIO_LIBRARIES, str(tmp_path / "state")],
        env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=WAIT_S,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "radio libraries: []"


# --- real processes ----------------------------------------------------------------------


def read_until(fd: int, text: str, seen: io.BytesIO) -> str:
    """Read from a pipe or terminal until `text` shows up, or fail after WAIT_S."""
    deadline = time.monotonic() + WAIT_S
    while text.encode() not in seen.getvalue():
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise AssertionError(f"no {text!r} within {WAIT_S} s in:\n{_text(seen)}")
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # EIO: the process closed the terminal
            chunk = b""
        if not chunk:
            raise AssertionError(f"output ended before {text!r}:\n{_text(seen)}")
        seen.write(chunk)
    return _text(seen)


def _text(seen: io.BytesIO) -> str:
    return seen.getvalue().decode(errors="replace")


class DeviceProcess:
    """`teton-device run --simulate` in its own process, with a pseudo-terminal as stdin,
    so the real TerminalButtons read the keys (review Q80). stdout is a pipe, so the status
    line prints each LED change as a line."""

    def __init__(self, tmp_path: Path) -> None:
        self.state_dir = tmp_path / "state"
        self.socket = tmp_path / SOCKET_NAME
        seed_state(self.state_dir)
        self._keys, terminal = pty.openpty()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "teton_device", "run", "--simulate"]
            + ["--state-dir", str(self.state_dir)],
            stdin=terminal,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)},
        )
        os.close(terminal)
        self._screen = io.BytesIO()

    def wait_for(self, text: str) -> None:
        assert self.proc.stdout is not None
        read_until(self.proc.stdout.fileno(), text, self._screen)

    def press(self, key: bytes) -> None:
        os.write(self._keys, key)

    def stop(self) -> tuple[int, str, str]:
        """Wait for the exit; the exit code, the screen and the logs."""
        try:
            rest, logs = self.proc.communicate(timeout=WAIT_S)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.communicate()
            os.close(self._keys)
        return self.proc.returncode, _text(self._screen) + rest.decode(), logs.decode()


@pytest.fixture
def device_process(tmp_path: Path) -> Iterator[DeviceProcess]:
    device = DeviceProcess(tmp_path)
    yield device
    if device.proc.poll() is None:
        device.proc.kill()
        device.proc.communicate()


async def test_two_processes_provision_over_the_socket(device_process: DeviceProcess) -> None:
    """The device and the configurator as separate programs, as in the README (Q80)."""
    device = device_process
    # The first LED line comes after the keys are read one by one: setcbreak drops
    # anything typed before that.
    device.wait_for("LED off")
    device.press(b"p")
    device.wait_for("LED blinking blue")

    technician = ScriptedIO("", CODE, "1", HOME_PSK_PASSWORD)  # the only device, Home-PSK

    def connect(found: FoundDevice) -> sim_client.SimClientTransport:
        return sim_client.SimClientTransport(Path(found.address))

    code = await run_flow(lambda: sim_client.discover(device.socket), connect, technician)
    assert code == 0, technician.output
    assert ui_text.connected_text("192.168.1.23", "full") in technician.lines

    device.wait_for("LED green")
    device.press(b"q")
    exit_code, screen, logs = device.stop()

    assert exit_code == 0, logs
    assert not device.socket.exists()
    log_lines = [json.loads(line) for line in logs.splitlines()]  # stderr is only logs
    assert {"connected", "power_off", "device_stopped"} <= {line["event"] for line in log_lines}
    shown_code = format_code(CODE)
    assert shown_code in screen  # the code is on the screen only (review Q73)
    for secret in (CODE, shown_code, HOME_PSK_PASSWORD):
        assert secret not in logs


def test_sigterm_stops_the_device_cleanly(device_process: DeviceProcess) -> None:
    """Like Ctrl+C: the socket file is removed and the exit code is 0 (review Q76)."""
    device = device_process
    device.wait_for("LED off")
    assert device.socket.exists()
    device.proc.send_signal(signal.SIGTERM)
    exit_code, _, logs = device.stop()
    assert exit_code == 0, logs
    assert not device.socket.exists()
    assert '"device_stopped"' in logs


def test_ctrl_c_stops_the_configurator(device_process: DeviceProcess, tmp_path: Path) -> None:
    """Ctrl+C at the hidden password prompt: exit 130 at once, the "stopped" text, echo back
    on, and the connection closed, so the device is free again (review Q75)."""
    device = device_process
    device.wait_for("LED off")
    device.press(b"p")
    device.wait_for("LED blinking blue")

    screen, terminal = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "teton_configurator", "--simulate"],
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
        env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)},
        # Its own session with this terminal as the controlling one, so getpass() reads it
        # and Ctrl+C typed on it sends SIGINT, as in a real terminal window.
        start_new_session=True,
        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
    )
    os.close(terminal)
    try:
        seen = io.BytesIO()
        for prompt, answer in [
            (ui_text.ASK_DEVICE, ""),
            (ui_text.ASK_SETUP_CODE, CODE),
            (ui_text.ASK_NETWORK, "1"),
        ]:
            read_until(screen, prompt, seen)
            os.write(screen, answer.encode() + b"\n")
        read_until(screen, ui_text.ASK_PASSWORD, seen)
        assert not termios.tcgetattr(screen)[3] & termios.ECHO  # getpass: typing is hidden

        os.write(screen, b"\x03")  # Ctrl+C

        assert proc.wait(timeout=WAIT_S) == configurator_cli.EXIT_INTERRUPTED
        assert ui_text.STOPPED in read_until(screen, ui_text.STOPPED, seen)
        assert termios.tcgetattr(screen)[3] & termios.ECHO  # restored
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(screen)
    assert device.proc.stderr is not None
    read_until(device.proc.stderr.fileno(), '"sim_client_left"', io.BytesIO())
