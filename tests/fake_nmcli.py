# Test helpers for the NetworkManager backend: recorded nmcli outputs and a fake runner
# that answers from them. Shared by test_nmcli_parsing.py and test_no_secrets_in_logs.py
# (review Q61). Not a test module; tests import it as `fake_nmcli`.

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from teton_device.wifi.nmcli import RunResult

FIXTURES = Path(__file__).parent / "fixtures" / "nmcli"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def ok(stdout: str = "") -> RunResult:
    return RunResult(0, stdout, "")


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    stdin: str | None
    timeout_s: float


class FakeRunner:
    """Records every command and answers from rules: the first rule whose words all
    appear in argv wins; otherwise the command succeeds with no output."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.rules: list[tuple[tuple[str, ...], RunResult]] = []
        self.on("DEVICE,TYPE,STATE", result=ok(fixture("devices.txt")))
        self.on("NAME,UUID", result=ok(fixture("connections.txt")))
        self.on("device", "show", result=ok(fixture("device-show-full.txt")))

    def on(self, *words: str, result: RunResult) -> None:
        self.rules.insert(0, (words, result))  # newest first, so tests can override

    async def __call__(
        self, argv: Sequence[str], timeout_s: float, stdin: str | None = None
    ) -> RunResult:
        self.calls.append(Call(tuple(argv), stdin, timeout_s))
        for words, result in self.rules:
            if all(w in argv for w in words):
                return result
        return ok()

    def commands(self) -> list[str]:
        return [" ".join(c.argv) for c in self.calls]

    def deleted(self) -> list[str]:
        return [c.argv[-1] for c in self.calls if c.argv[1:3] == ("connection", "delete")]
