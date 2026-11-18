"""OKX UTC anchor + process elapsed time. Windows wall time is diagnostic only."""
import math

from .okx_readonly import PreflightError

POLICY = "okx_utc_monotonic_v1"


class ExchangeClock:
    def __init__(self, wall, monotonic):
        self.wall, self.monotonic = wall, monotonic
        self.anchor = None

    @property
    def synchronized(self):
        return self.anchor is not None

    def now(self):
        anchor = self.anchor
        if anchor is None:
            # Passive metadata / a durable Stop before recovery synchronization.
            # No new run or exchange execution may use this fallback.
            return self.wall()
        server, mono = anchor
        elapsed = self.monotonic() - mono
        if not math.isfinite(elapsed) or elapsed < 0:
            raise PreflightError("session_monotonic_regression")
        return server + elapsed

    def synchronize(self, server_now, *, replace=False):
        if not math.isfinite(server_now) or not 0 < server_now < 253402300799:
            raise PreflightError("invalid_server_time")
        if self.anchor is None or replace:
            self.anchor = (server_now, self.monotonic())
        elif abs(self.now() - server_now) > 10:
            # Never jump an active session's elapsed clock to a new UTC anchor.
            raise PreflightError("session_clock_source_disagreement")
        return self.now()
