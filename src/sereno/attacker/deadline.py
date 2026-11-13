"""One wall clock for a whole invocation.

Every model call and every retry shares it, so a slow provider ends as a clean failure before the harness kills the
program. `alarm` is the hard stop for a blocking call: httpx timeouts apply to each read, so a provider that sends
bytes slowly could otherwise hold one call past the deadline.
"""

import contextlib
import signal
import threading
import time
from collections.abc import Callable


class DeadlineHit(BaseException):
    """Raised by the alarm when the shared deadline ends inside a call. A BaseException, so no library code between
    the alarm and the call swallows it."""


@contextlib.contextmanager
def alarm(seconds: float):
    """End a blocking call at the deadline. Needs the main thread; elsewhere only the client's timeouts apply."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def fire(signum, frame):
        raise DeadlineHit

    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.001))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class Deadline:
    """The invocation's wall clock: everything the program does has to end before it."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.clock, self.end = clock, clock() + max(seconds, 0.0)

    def remaining(self) -> float:
        return self.end - self.clock()
