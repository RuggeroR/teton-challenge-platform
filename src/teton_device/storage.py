# State directory and state.json (device_id, setup_code, provisioned flag) with 0700/0600
# permissions. See SPEC §3, DESIGN §4.
#
# Every write is atomic: the new content goes to a temp file in the same directory, which
# then replaces state.json with os.replace(). A crash mid-write leaves the old file intact,
# never a half-written one, so the device can't lose its identity or setup code.

import json
import os
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import structlog

from teton_common import setup_code
from teton_common.messages import MessageError, check_device_id
from teton_common.protocol import DEVICE_ID_PREFIX

log = structlog.get_logger()

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "teton-device"  # SPEC §3
STATE_FILE = "state.json"
_FIELDS = {"device_id", "setup_code", "provisioned"}


class StorageError(Exception):
    """state.json can't be read or written. The text never contains the setup code."""


@dataclass(frozen=True)
class StoredState:
    """What survives a restart. SPEC §3 lists device_id and setup_code; DESIGN §4 adds
    `provisioned`, so a provisioned device doesn't start advertising again."""

    device_id: str
    setup_code: str = field(repr=False)
    provisioned: bool = False


def new_device_id() -> str:
    """TETON- + 6 random uppercase hex characters (SPEC §3)."""
    return DEVICE_ID_PREFIX + secrets.token_hex(3).upper()


class Storage:
    def __init__(
        self,
        state_dir: Path = DEFAULT_STATE_DIR,
        new_code: Callable[[], str] = setup_code.generate,
        new_id: Callable[[], str] = new_device_id,
    ) -> None:
        self._dir = state_dir
        self._path = state_dir / STATE_FILE
        self._new_code = new_code  # injectable, so tests get known codes
        self._new_id = new_id
        self._current: StoredState | None = None

    def load_or_create(self) -> StoredState:
        """Read state.json, or create the identity and setup code on first start."""
        if self._path.exists():
            self._current = self._read()
        else:
            self._current = StoredState(device_id=self._new_id(), setup_code=self._new_code())
            self._write(self._current)
            log.info("device_created", device_id=self._current.device_id)
        return self._current

    def mark_provisioned(self) -> StoredState:
        return self._save(replace(self._loaded(), provisioned=True))

    def reset(self) -> StoredState:
        """[R][R]: a new setup code and not provisioned. The device_id is kept."""
        return self._save(replace(self._loaded(), setup_code=self._new_code(), provisioned=False))

    def _loaded(self) -> StoredState:
        if self._current is None:
            raise StorageError("call load_or_create() first")
        return self._current

    def _save(self, state: StoredState) -> StoredState:
        self._write(state)
        self._current = state
        return state

    def _read(self) -> StoredState:
        # Every error names the file and the problem, never a value from it.
        try:
            data: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise StorageError(f"{self._path} is not readable JSON") from None
        if not isinstance(data, dict) or set(data) != _FIELDS:
            raise StorageError(f"{self._path} must hold exactly {sorted(_FIELDS)}")
        if not isinstance(data["provisioned"], bool):
            raise StorageError(f"{self._path}: provisioned must be true or false")
        try:
            check_device_id(data["device_id"])
            code = setup_code.normalize(data["setup_code"])
        except (TypeError, AttributeError, MessageError, setup_code.InvalidSetupCode):
            raise StorageError(f"{self._path}: device_id or setup_code is not valid") from None
        return StoredState(data["device_id"], code, data["provisioned"])

    def _write(self, state: StoredState) -> None:
        if not self._dir.exists():
            self._dir.mkdir(parents=True)
            self._dir.chmod(0o700)  # set explicitly: mkdir's mode is reduced by the umask
        content = {
            "device_id": state.device_id,
            "setup_code": state.setup_code,
            "provisioned": state.provisioned,
        }
        # mkstemp creates the file 0600, in the same directory so os.replace() is atomic.
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(content, f)
                f.flush()
                os.fsync(f.fileno())  # the data is on disk before the rename makes it visible
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
