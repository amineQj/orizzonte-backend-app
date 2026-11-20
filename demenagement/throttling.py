from django.conf import settings
from rest_framework.throttling import ScopedRateThrottle


class DynamicScopedRateThrottle(ScopedRateThrottle):
    """ScopedRateThrottle that reads rates from live Django settings."""

    def get_rate(self):
        if not getattr(self, "scope", None):
            raise RuntimeError("scope is required")
        rates = getattr(settings, "DEFAULT_THROTTLE_RATES", {})
        try:
            return rates[self.scope]
        except KeyError as exc:
            raise RuntimeError(f"No default throttle rate set for '{self.scope}' scope") from exc