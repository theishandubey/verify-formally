"""Code-level property tests for call_with_retry, run against the real retry.py.

Run by explicit path through run_code_check.py --tool hypothesis (see verification/rerun.sh);
the file name keeps it out of pytest's default discovery. Each test drives the unmodified
call_with_retry with an op() whose outcomes Hypothesis chooses, and asserts a property on the
number of calls it makes. derandomize=True makes every run explore the same examples, so a
re-run reproduces the stored result.
"""
import os
import sys

from hypothesis import given, settings, strategies as st

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from retry import GiveUp, TransientError, call_with_retry  # noqa: E402

SETTINGS = settings(derandomize=True, max_examples=300, database=None, deadline=None)
FAILURES = {"transient": TransientError, "timeout": TimeoutError}
LIMITS = st.integers(min_value=1, max_value=4)


class ScriptedOp:
    """op() that fails as scripted, then succeeds; it never runs more than `cap` times."""

    def __init__(self, script, cap):
        self.script = list(script)
        self.cap = cap
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls > self.cap:
            raise RuntimeError("op() called %d times; stopping the run" % self.calls)
        if self.script:
            raise FAILURES[self.script.pop(0)]("scripted failure")
        return "ok"


def run(op, max_errors, max_timeouts):
    try:
        return call_with_retry(op, max_errors=max_errors, max_timeouts=max_timeouts)
    except (GiveUp, RuntimeError) as e:
        return e


# CallsBoundedOnRealCode: for every sequence of failures, call_with_retry calls op() at most
# max_errors + max_timeouts times before it returns or gives up.
@SETTINGS
@given(LIMITS, LIMITS, st.lists(st.sampled_from(sorted(FAILURES)), max_size=12))
def test_calls_bounded_by_both_limits(max_errors, max_timeouts, script):
    op = ScriptedOp(script, cap=max_errors + max_timeouts + 1)
    run(op, max_errors, max_timeouts)
    assert op.calls <= max_errors + max_timeouts, (
        "CallsBoundedOnRealCode violated: %d calls to op() with max_errors=%d, "
        "max_timeouts=%d, failures %r" % (op.calls, max_errors, max_timeouts, script))


# StreakGivesUp: max_errors consecutive TransientErrors from the start make call_with_retry
# raise GiveUp after exactly max_errors calls (retry.py:21).
@SETTINGS
@given(LIMITS, LIMITS)
def test_error_streak_gives_up_exactly(max_errors, max_timeouts):
    op = ScriptedOp(["transient"] * (max_errors + max_timeouts + 2), cap=100)
    outcome = run(op, max_errors, max_timeouts)
    assert isinstance(outcome, GiveUp) and op.calls == max_errors, (
        "StreakGivesUp violated: %r after %d calls with max_errors=%d"
        % (outcome, op.calls, max_errors))


# Sanity for StreakGivesUp: the same inputs reach the end of the test, so the property above
# is not vacuously true. Must fail.
@SETTINGS
@given(LIMITS, LIMITS)
def test_error_streak_sanity(max_errors, max_timeouts):
    op = ScriptedOp(["transient"] * (max_errors + max_timeouts + 2), cap=100)
    run(op, max_errors, max_timeouts)
    assert False, "sanity: the StreakGivesUp harness reaches its end"
