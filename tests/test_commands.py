"""Tests for the command state machine (src/heatbeat/commands.py): pure rules, no database."""

from hypothesis import given
from hypothesis import strategies as st

from heatbeat.commands import (
    ALLOWED,
    OPEN_STATES,
    CommandState,
    after_result,
    can_move,
    check_value,
    needs_approval,
)

FINAL = {CommandState.APPLIED, CommandState.REJECTED, CommandState.CANCELLED}


def test_normal_path():
    assert can_move(CommandState.REQUESTED, CommandState.SENT)
    assert after_result(CommandState.SENT, "applied") is CommandState.APPLIED


def test_s15_duplicate_result_is_ignored():
    """S15: the gateway's answer arrives twice; the second one changes nothing."""
    assert after_result(CommandState.APPLIED, "applied") is None


def test_s15_late_result_after_expiry_is_accepted():
    """The gateway is the truth: it applied the change, our clock just gave up waiting."""
    assert after_result(CommandState.EXPIRED, "applied") is CommandState.APPLIED


def test_result_can_overtake_our_sent_bookkeeping():
    assert after_result(CommandState.REQUESTED, "applied") is CommandState.APPLIED


def test_cancelled_command_never_becomes_applied():
    assert after_result(CommandState.CANCELLED, "applied") is None


def test_final_states_have_no_way_out():
    for state in FINAL:
        assert ALLOWED[state] == set()


def test_open_states_can_all_expire():
    for state in OPEN_STATES:
        assert can_move(state, CommandState.EXPIRED)


@given(st.lists(st.sampled_from(["applied", "rejected", "expired"]), max_size=20))
def test_property_a_command_is_applied_at_most_once(results):
    state = CommandState.SENT
    applied = 0
    for result in results:
        new = after_result(state, result)
        if new is not None:
            applied += new is CommandState.APPLIED
            state = new
    assert applied <= 1


def test_small_change_needs_no_approval():
    assert needs_approval("heating_curve_level", 30, 28, threshold=5) is False


def test_big_change_needs_a_second_person():
    assert needs_approval("heating_curve_level", 30, 20, threshold=5) is True


def test_switching_heating_off_always_needs_approval():
    assert needs_approval("heating_enabled", 1, 0, threshold=5) is True


def test_unknown_current_value_needs_approval():
    assert needs_approval("heating_curve_level", None, 28, threshold=5) is True


def test_value_checks():
    assert check_value("heating_curve_level", True, 30, 15, 50) is None
    assert "outside" in str(check_value("heating_curve_level", True, 80, 15, 50))
    assert "cannot be changed" in str(check_value("flow_temp", False, 40, None, None))
