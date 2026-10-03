import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from retry import GiveUp, TransientError, call_with_retry


def test_alternating_failures_cause_runaway_retry():
    max_errors = 3
    max_timeouts = 3
    budget = max_errors + max_timeouts
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        assert calls["n"] <= budget, (
            "BoundedAttempts violated: call_with_retry made %d calls to op() without "
            "giving up, more than max_errors + max_timeouts = %d; alternating "
            "TransientError/TimeoutError resets both counters on every call, so neither "
            "limit is ever reached" % (calls["n"], budget)
        )
        if calls["n"] % 2 == 1:
            raise TransientError("simulated transient failure")
        raise TimeoutError("simulated timeout")

    with pytest.raises(GiveUp):
        call_with_retry(op, max_errors=max_errors, max_timeouts=max_timeouts)


def test_success_after_one_error():
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientError("first call fails")
        return "ok"

    assert call_with_retry(op, max_errors=3, max_timeouts=3) == "ok"
    assert calls["n"] == 2


def test_gives_up_after_max_errors_consecutive_errors():
    def op():
        raise TransientError("always fails")

    with pytest.raises(GiveUp):
        call_with_retry(op, max_errors=3, max_timeouts=3)
