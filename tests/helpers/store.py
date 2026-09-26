"""In-memory HA storage backend shared by persistence/lifecycle tests."""

class FakeStore:
    """Minimal stand-in for homeassistant.helpers.storage.Store.

    Records delayed/immediate saves so the tests can assert the write gating
    and the migration-on-load behaviour without a running event loop's disk.
    """

    def __init__(self, initial=None):
        self._initial = initial
        self.saved = None  # last snapshot handed to async_save
        self.delay_saves = 0  # count of async_delay_save calls
        self.immediate_saves = 0
        self.removed = False
        self._last_delay_factory = None

    async def async_load(self):
        return self._initial

    def async_delay_save(self, data_func, delay):
        self.delay_saves += 1
        self._last_delay_factory = data_func

    async def async_save(self, data):
        self.immediate_saves += 1
        self.saved = data

    async def async_remove(self):
        self.removed = True

    def pending_snapshot(self):
        """Materialise the last delayed-save data factory (what would hit disk)."""
        return None if self._last_delay_factory is None else self._last_delay_factory()
