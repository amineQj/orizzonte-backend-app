"""
drivers/agent_auth.py
~~~~~~~~~~~~~~~~~~~~~

Authentication for the external AI agent.

The agent is NOT a Django user and never uses JWT. It authenticates with a
shared secret sent in the ``X-Agent-Key`` header, compared in constant time
with ``hmac.compare_digest``.

Rules:
* the expected key is read from ``AI_AGENT_API_KEY`` (.env);
* when the key is missing/empty the endpoints are DISABLED (an empty key is
  never accepted);
* the key is never logged.
"""
import hmac

from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


class AgentPrincipal:
    """Minimal authenticated principal representing the AI agent."""

    is_authenticated = True
    is_anonymous = False
    is_agent = True
    pk = None

    def __str__(self):
        return "ai-agent"


def _expected_key() -> str:
    return str(getattr(settings, "AI_AGENT_API_KEY", "") or "").strip()


class AgentKeyAuthentication(BaseAuthentication):
    """
    Authenticate a request carrying a valid ``X-Agent-Key`` header.

    Returns ``None`` when the header is absent so that other authenticators
    (e.g. JWT on the document download endpoint) can still be tried.
    """

    header = "X-Agent-Key"

    def authenticate(self, request):
        provided = request.headers.get(self.header)
        if provided is None:
            return None

        expected = _expected_key()
        if not expected:
            raise AuthenticationFailed(
                "AI agent endpoints are disabled.", code="agent_disabled"
            )
        if not hmac.compare_digest(provided, expected):
            raise AuthenticationFailed(
                "Invalid X-Agent-Key.", code="agent_invalid_key"
            )
        return (AgentPrincipal(), None)

    def authenticate_header(self, request):
        # Presence of this method makes DRF answer 401 (not 403) when the
        # request is not authenticated.
        return self.header
