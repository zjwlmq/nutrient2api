"""Rotate accounts; only fail over a rejected request before output starts."""

import time

from . import upstream


class AccountPool:
    def __init__(self, store):
        self.store = store

    def _attempts(self):
        excluded = set()
        for _ in range(50):
            account = self.store.select_account(excluded)
            if account is None:
                return
            excluded.add(account["id"])
            yield account

    @staticmethod
    def _empty():
        return upstream.UpstreamError(503, {"error": {
            "message": "No available Nutrient account. Add/enable an account in /admin/ or wait for cooldown.",
            "type": "configuration_error"}})

    def call(self, model, operation):
        last = None
        for account in self._attempts():
            start = time.monotonic()
            status = 500
            try:
                result = operation(account["api_key"])
                status = 200
                return result
            except upstream.UpstreamError as exc:
                status, last = exc.status, exc
                if status not in (401, 403, 429):
                    raise
            finally:
                self.store.record(account, model, status, int((time.monotonic() - start) * 1000))
        raise last or self._empty()

    def stream(self, model, operation):
        last = None
        for account in self._attempts():
            start = time.monotonic()
            events = operation(account["api_key"])
            status, started = 500, False
            try:
                first = next(events, None)
                if first is None:
                    raise upstream.UpstreamError(502, {"error": {"message": "empty upstream stream"}})
                started = True
                yield first
                yield from events
                status = 200
                return
            except GeneratorExit:
                status = 499
                raise
            except upstream.UpstreamError as exc:
                status, last = exc.status, exc
                if started or status not in (401, 403, 429):
                    raise
            finally:
                events.close()
                self.store.record(account, model, status, int((time.monotonic() - start) * 1000), streaming=True)
        raise last or self._empty()
