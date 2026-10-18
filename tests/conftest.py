# Shared fixtures: a temp state dir, the in-memory transport pair, a running device (with
# and without [P]) and log capture through the real processor chain.
#
# Only fixtures live here; the classes they are built on (Device, FakeTime, Wire, ...) are
# in fake_configurator.py, because tests import those and pytest advises against importing
# from conftest (review Q48, Q49). There is no fake-time fixture: each rig owns its clock
# (`device.time`), so a separate one would be a second clock that nothing advances.

import io
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import structlog
from fake_configurator import Device, RecordingWifi, start_paired

from teton_common.log import configure_logging
from teton_common.memory_transport import MemoryClientTransport, MemoryDeviceTransport, pair


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "nested" / "teton-device"  # parents don't exist yet


@pytest.fixture
async def ends() -> AsyncIterator[tuple[MemoryDeviceTransport, MemoryClientTransport]]:
    """A device end, started and advertising, and a client end not yet connected."""
    device, client = pair()
    await device.start()
    await device.set_advertising(True)
    yield device, client
    await client.close()
    await device.close()


@pytest.fixture
async def device(state_dir: Path) -> AsyncIterator[Device]:
    """A running device, nothing pressed yet: not advertising."""
    d = Device(state_dir)
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
async def paired_device(state_dir: Path) -> AsyncIterator[Device]:
    """A running device whose [P] was pressed, with a Wi-Fi that records each request."""
    d = await start_paired(state_dir, RecordingWifi())
    yield d
    await d.stop()


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """The real logging setup (INFO), writing into a string. Not structlog's capture_logs,
    which replaces the processors and so would skip the redaction."""
    out = io.StringIO()
    configure_logging(verbose=False, stream=out)
    yield out
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()
