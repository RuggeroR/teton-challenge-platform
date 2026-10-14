# Provisioning state machine with validated transitions. See DESIGN §4.
#
# The allowed transitions are one table, so the whole state machine can be read (and
# defended) in one place. The pairing window is not a state: it is a timer in the service,
# and it only decides which of the allowed transitions the service asks for.

import structlog

from teton_common.protocol import DeviceState

log = structlog.get_logger()

S = DeviceState

# from-state -> the states it may move to (DESIGN §4, review Q13).
ALLOWED: dict[DeviceState, frozenset[DeviceState]] = {
    S.UNPROVISIONED: frozenset({S.ADVERTISING}),  # [P]
    S.ADVERTISING: frozenset(
        {
            S.UNPROVISIONED,  # window expired, no session
            S.SESSION_ESTABLISHED,  # handshake ok
        }
    ),
    S.SESSION_ESTABLISHED: frozenset(
        {
            S.ADVERTISING,  # session ended (idle, protocol error, new hello), window open
            S.UNPROVISIONED,  # session ended after the window expired
            S.CONNECTING,  # set_credentials
        }
    ),
    S.CONNECTING: frozenset(
        {
            S.SESSION_ESTABLISHED,  # failed: profile rolled back, retry in the same session
            S.PROVISIONED,  # connected: saved, advertising off
        }
    ),
    S.PROVISIONED: frozenset({S.UNPROVISIONED}),  # [R][R]
}


class InvalidTransition(Exception):
    """A transition that DESIGN §4 doesn't allow. Always a bug in the caller."""


class StateMachine:
    def __init__(self, initial: DeviceState = S.UNPROVISIONED) -> None:
        self._state = initial

    @classmethod
    def at_start(cls, provisioned: bool) -> "StateMachine":
        """The state after `run` starts: only `provisioned` survives a restart."""
        return cls(S.PROVISIONED if provisioned else S.UNPROVISIONED)

    @property
    def state(self) -> DeviceState:
        return self._state

    def can(self, to: DeviceState) -> bool:
        return to in ALLOWED[self._state]

    def transition(self, to: DeviceState, reason: str) -> None:
        """Move to `to`, or raise InvalidTransition and stay where we are."""
        if not self.can(to):
            raise InvalidTransition(f"{self._state} -> {to} is not allowed")
        log.info("state_changed", from_state=self._state, to_state=to, reason=reason)
        self._state = to
