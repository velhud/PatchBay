"""Bounded process-local reuse for observation only; no authority or event queue."""
from collections import OrderedDict
import threading
import time


class ScopedObservationCache:
    def __init__(self, *, interval=.25, ttl=60, max_entries=256, clock=None):
        self.interval, self.ttl, self.max_entries = interval, ttl, max_entries
        self.clock = clock or time.monotonic
        self._entries = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key, revision, build=None):
        """Join one synchronous refresh; never hold this lock across a wait.

        Revision readers and builders are read-only. A post-build recheck closes
        the sampling window; continued churn leaves the entry due immediately.
        Cancelling an observer consumes no event and cannot cancel execution.
        """
        with self._lock:
            now = self.clock()
            for old, entry in list(self._entries.items()):
                if now-entry['touched'] >= self.ttl:
                    self._entries.pop(old, None)
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                entry['touched'] = now
                if now-entry['checked'] < self.interval:
                    return entry['value']
            current = revision()
            if entry is not None and entry['revision'] == current:
                entry['checked'] = now
                return entry['value']
            value = current if build is None else build()
            checked = now
            if build is not None:
                after = revision()
                if after != current:
                    current, value = after, build()
                    if revision() != current:
                        checked = float('-inf')
            self._entries[key] = dict(revision=current, value=value, checked=checked, touched=now)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return value
