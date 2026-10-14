# Tests for the state directory and state.json: creation, reuse, permissions, provisioned
# flag, reset, and clear errors on a corrupt file.

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from teton_common import setup_code
from teton_common.protocol import DeviceState
from teton_device.state import StateMachine
from teton_device.storage import STATE_FILE, Storage, StorageError, StoredState, new_device_id

CODES = ["7K2MQX9D4HTRB8WZ", "0123456789ABCDEF", "ZYXWVTSRQPNMKJHG"]


def code_source() -> Iterator[str]:
    yield from CODES


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "nested" / "teton-device"  # parents don't exist yet


def make_storage(state_dir: Path) -> Storage:
    codes = code_source()
    return Storage(state_dir, new_code=lambda: next(codes), new_id=lambda: "TETON-3F9A2C")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_first_start_creates_identity_and_code(state_dir: Path) -> None:
    state = make_storage(state_dir).load_or_create()
    assert state == StoredState("TETON-3F9A2C", CODES[0], provisioned=False)
    on_disk = json.loads((state_dir / STATE_FILE).read_text())
    assert on_disk == {"device_id": "TETON-3F9A2C", "setup_code": CODES[0], "provisioned": False}


def test_permissions_are_0700_and_0600(state_dir: Path) -> None:
    make_storage(state_dir).load_or_create()
    assert mode(state_dir) == 0o700
    assert mode(state_dir / STATE_FILE) == 0o600


def test_permissions_hold_with_a_permissive_umask(state_dir: Path) -> None:
    old = os.umask(0)
    try:
        make_storage(state_dir).load_or_create()
    finally:
        os.umask(old)
    assert mode(state_dir) == 0o700
    assert mode(state_dir / STATE_FILE) == 0o600


def test_file_is_created_once_and_reused(state_dir: Path) -> None:
    first = make_storage(state_dir).load_or_create()
    calls = 0

    def no_new_code() -> str:
        nonlocal calls
        calls += 1
        return CODES[1]

    again = Storage(state_dir, new_code=no_new_code, new_id=new_device_id).load_or_create()
    assert again == first
    assert calls == 0


def test_provisioned_survives_a_restart(state_dir: Path) -> None:
    storage = make_storage(state_dir)
    storage.load_or_create()
    assert storage.mark_provisioned().provisioned is True

    reloaded = make_storage(state_dir).load_or_create()
    assert reloaded.provisioned is True
    assert StateMachine.at_start(reloaded.provisioned).state == DeviceState.PROVISIONED


def test_reset_rotates_the_code_clears_provisioned_and_keeps_the_id(state_dir: Path) -> None:
    storage = make_storage(state_dir)
    before = storage.load_or_create()
    storage.mark_provisioned()

    after = storage.reset()
    assert after.device_id == before.device_id
    assert after.setup_code == CODES[1] != before.setup_code
    assert after.provisioned is False
    assert make_storage(state_dir).load_or_create() == after  # also on disk
    assert mode(state_dir / STATE_FILE) == 0o600


def test_no_temp_files_are_left_behind(state_dir: Path) -> None:
    storage = make_storage(state_dir)
    storage.load_or_create()
    storage.mark_provisioned()
    storage.reset()
    assert [p.name for p in state_dir.iterdir()] == [STATE_FILE]


def test_changes_need_a_loaded_state(state_dir: Path) -> None:
    with pytest.raises(StorageError):
        make_storage(state_dir).mark_provisioned()
    with pytest.raises(StorageError):
        make_storage(state_dir).reset()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"device_id": "TETON-3F9A2C", "setup_code": "7K2MQX9D4HTRB8WZ"}',  # no provisioned
        '{"device_id": "TETON-3F9A2C", "setup_code": "7K2MQX9D4HTRB8WZ", "provisioned": "yes"}',
        '{"device_id": "nope", "setup_code": "7K2MQX9D4HTRB8WZ", "provisioned": false}',
        '{"device_id": "TETON-3F9A2C", "setup_code": "SECRET-BUT-SHORT", "provisioned": false}',
        '{"device_id": "TETON-3F9A2C", "setup_code": 42, "provisioned": false}',
        '{"device_id": "TETON-3F9A2C", "setup_code": "7K2MQX9D4HTRB8WZ", "provisioned": false,'
        ' "extra": 1}',
    ],
)
def test_a_corrupt_file_gives_a_clear_error(state_dir: Path, content: str) -> None:
    state_dir.mkdir(parents=True)
    (state_dir / STATE_FILE).write_text(content)
    with pytest.raises(StorageError) as exc:
        make_storage(state_dir).load_or_create()
    assert STATE_FILE in str(exc.value)
    assert "7K2MQX9D4HTRB8WZ" not in str(exc.value)
    assert "SECRET-BUT-SHORT" not in str(exc.value)


def test_repr_hides_the_setup_code() -> None:
    assert CODES[0] not in repr(StoredState("TETON-3F9A2C", CODES[0]))


def test_new_device_id_format() -> None:
    ids = {new_device_id() for _ in range(50)}
    assert len(ids) > 1
    for device_id in ids:
        assert device_id.startswith("TETON-")
        assert len(device_id) == 12
        assert all(c in "0123456789ABCDEF" for c in device_id[6:])


def test_default_code_source_is_the_setup_code_module(state_dir: Path) -> None:
    state = Storage(state_dir).load_or_create()
    assert setup_code.normalize(state.setup_code) == state.setup_code
