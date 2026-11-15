"""Small shared cache primitives; backend-specific policies remain local."""
from __future__ import annotations

from collections import OrderedDict


class BoundedCache:
    def __init__(self, capacity: int):
        self.capacity = max(0, int(capacity))
        self._items = OrderedDict()

    def get(self, key, default=None):
        if key not in self._items:
            return default
        value = self._items.pop(key)
        self._items[key] = value
        return value

    def put(self, key, value):
        if not self.capacity:
            return
        self._items.pop(key, None)
        self._items[key] = value
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)
