class GiveUp(Exception):
    pass


class TransientError(Exception):
    pass


def call_with_retry(op, max_errors=3, max_timeouts=3):
    errors = 0
    timeouts = 0
    while True:
        try:
            return op()
        except TransientError:
            errors += 1
            timeouts = 0
        except TimeoutError:
            timeouts += 1
            errors = 0
        if errors >= max_errors or timeouts >= max_timeouts:
            raise GiveUp(f"gave up after {errors} errors, {timeouts} timeouts")
