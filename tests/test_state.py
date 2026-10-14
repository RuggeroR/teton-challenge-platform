# Tests for allowed/forbidden state transitions and the start state after a restart.

import pytest
from structlog.testing import capture_logs

from teton_common.protocol import DeviceState
from teton_device.state import ALLOWED, InvalidTransition, StateMachine

S = DeviceState

ALLOWED_EDGES = [(a, b) for a, targets in ALLOWED.items() for b in sorted(targets)]

# Each one would be a real bug: skipping the handshake, provisioning without a successful
# attempt, advertising while provisioned, or reaching Wi-Fi without a session.
FORBIDDEN_EDGES = [
    (S.UNPROVISIONED, S.SESSION_ESTABLISHED),
    (S.UNPROVISIONED, S.CONNECTING),
    (S.UNPROVISIONED, S.PROVISIONED),
    (S.ADVERTISING, S.CONNECTING),
    (S.ADVERTISING, S.PROVISIONED),
    (S.SESSION_ESTABLISHED, S.PROVISIONED),
    (S.CONNECTING, S.ADVERTISING),
    (S.CONNECTING, S.UNPROVISIONED),
    (S.PROVISIONED, S.ADVERTISING),
    (S.PROVISIONED, S.SESSION_ESTABLISHED),
    (S.PROVISIONED, S.CONNECTING),
]


def test_the_table_covers_every_state() -> None:
    assert set(ALLOWED) == set(DeviceState)
    for targets in ALLOWED.values():
        assert targets <= set(DeviceState)


def test_the_design_transitions_are_exactly_the_allowed_ones() -> None:
    # DESIGN §4 with review Q13 (no CONNECTED, no FAILED). Reset from other states: Q21.
    assert set(ALLOWED_EDGES) == {
        (S.UNPROVISIONED, S.ADVERTISING),
        (S.ADVERTISING, S.UNPROVISIONED),
        (S.ADVERTISING, S.SESSION_ESTABLISHED),
        (S.SESSION_ESTABLISHED, S.ADVERTISING),
        (S.SESSION_ESTABLISHED, S.UNPROVISIONED),
        (S.SESSION_ESTABLISHED, S.CONNECTING),
        (S.CONNECTING, S.SESSION_ESTABLISHED),
        (S.CONNECTING, S.PROVISIONED),
        (S.PROVISIONED, S.UNPROVISIONED),
    }


@pytest.mark.parametrize(("start", "to"), ALLOWED_EDGES)
def test_every_allowed_transition(start: DeviceState, to: DeviceState) -> None:
    machine = StateMachine(start)
    assert machine.can(to)
    machine.transition(to, reason="test")
    assert machine.state == to


@pytest.mark.parametrize(("start", "to"), FORBIDDEN_EDGES)
def test_forbidden_transitions_raise_and_keep_the_state(
    start: DeviceState, to: DeviceState
) -> None:
    machine = StateMachine(start)
    assert not machine.can(to)
    with pytest.raises(InvalidTransition):
        machine.transition(to, reason="test")
    assert machine.state == start


@pytest.mark.parametrize("state", list(DeviceState))
def test_staying_in_the_same_state_is_not_a_transition(state: DeviceState) -> None:
    with pytest.raises(InvalidTransition):
        StateMachine(state).transition(state, reason="test")


def test_full_happy_path_with_a_failed_attempt_and_reset() -> None:
    machine = StateMachine()
    for to in (
        S.ADVERTISING,  # [P]
        S.SESSION_ESTABLISHED,  # handshake ok
        S.CONNECTING,  # wrong password
        S.SESSION_ESTABLISHED,  # failed, retry
        S.CONNECTING,
        S.PROVISIONED,  # connected
        S.UNPROVISIONED,  # [R][R]
    ):
        machine.transition(to, reason="test")
    assert machine.state == S.UNPROVISIONED


def test_every_transition_is_logged() -> None:
    machine = StateMachine()
    with capture_logs() as logs:
        machine.transition(S.ADVERTISING, reason="pair_button")
    assert logs == [
        {
            "event": "state_changed",
            "log_level": "info",
            "from_state": S.UNPROVISIONED,
            "to_state": S.ADVERTISING,
            "reason": "pair_button",
        }
    ]


def test_a_refused_transition_is_not_logged_as_a_change() -> None:
    with capture_logs() as logs, pytest.raises(InvalidTransition):
        StateMachine().transition(S.PROVISIONED, reason="test")
    assert logs == []


@pytest.mark.parametrize(
    ("provisioned", "state"), [(False, S.UNPROVISIONED), (True, S.PROVISIONED)]
)
def test_start_state_depends_only_on_provisioned(provisioned: bool, state: DeviceState) -> None:
    # A device never starts advertising by itself (plan_v5 D2).
    assert StateMachine.at_start(provisioned).state == state
