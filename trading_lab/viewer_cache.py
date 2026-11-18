"""Short-lived shared copies of the state payloads that every polling viewer asks for.

The first reader loads and concurrent readers share that load, so any number of open tabs costs one status() call per TTL.
The TTL stretches with the measured load time (a duty cap): however long the history grows, polling cannot hold the lab
lock for more than `duty` of the time. Owner writes drop the copies at once, and a copy is never stored when a write
happened while it was loading. Nothing here talks to an exchange or changes state."""

import threading
import time

MISS = object()


class _Flight:
    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error = None


class ViewerSnapshots:
    def __init__(self, loaders, *, base_ttl=1.5, duty=0.25, clock=time.monotonic):
        if not 0 < duty < 1:
            raise ValueError("duty must be between 0 and 1")
        self.loaders = dict(loaders)
        self.base_ttl, self.duty, self.clock = float(base_ttl), float(duty), clock
        self.loads = {name: 0 for name in self.loaders}  # completed loader calls, for tests and diagnostics
        self._guard = threading.Lock()                   # protects the fields below; never held while a loader runs
        self._entries = {}                               # name -> (value, stored_at, ttl)
        self._flights = {}                               # name -> _Flight in progress
        self._epoch = 0                                  # bumped by invalidate()

    def _hit(self, name):
        entry = self._entries.get(name)
        return entry[0] if entry is not None and self.clock() - entry[1] < entry[2] else MISS

    def get(self, name):
        with self._guard:
            hit = self._hit(name)
            if hit is not MISS:
                return hit
            flight = self._flights.get(name)
            leader = flight is None
            if leader:
                flight = self._flights[name] = _Flight()
                epoch = self._epoch
        if not leader:
            flight.done.wait()  # share the load that is already running
            if flight.error is not None:
                raise flight.error
            return flight.value
        try:
            started = self.clock()
            value = self.loaders[name]()
            took = self.clock() - started
            ttl = max(self.base_ttl, took * (1 - self.duty) / self.duty)
            with self._guard:
                self.loads[name] += 1
                if epoch == self._epoch:
                    self._entries[name] = (value, self.clock(), ttl)
            flight.value = value
            return value
        except BaseException as error:
            flight.error = error
            raise
        finally:
            with self._guard:
                self._flights.pop(name, None)
            flight.done.set()

    def invalidate(self):
        """Never waits for a loader: an owner write must not queue behind a slow status() call."""
        with self._guard:
            self._epoch += 1
            self._entries.clear()
