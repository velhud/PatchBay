"""Maintenance admission is shared by all domain mutation entrypoints."""
from functools import wraps
from .locks import file_lock


def mutation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with file_lock(self.store.home / "locks/maintenance", shared=True):
            return method(self, *args, **kwargs)
    return wrapped
