"""
accounts/views.py

Views that EXTEND (never replace) the existing authentication/ views.

What is untouched in authentication/:
    - LogoutView      → works as-is, no changes

What lives here:
    - SignUpView      → POST /api/signup/  (role 'client' or 'driver')
    - VerifyEmailView → GET  /api/verify-email/
    - ResendVerificationView → POST /api/resend-verification/
    - LoginView       → POST /api/token/   (returns role + user info)
    - MeView          → GET  /api/users/me/
    - DriverListView  → GET  /api/users/employees/      (boss only)
    - DriverDetailView→ GET/DELETE /api/users/employees/<id>/ (boss only)
"""
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.http import HttpResponse
from django.urls import reverse
from django.utils.html import escape
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from urllib.parse import urlencode

import logging

from .models import User
from .permissions import IsBoss
from .serializers import (
    DriverDetailSerializer,
    DriverListSerializer,
    ResendVerificationSerializer,
    RoleAwareTokenSerializer,
    SignUpSerializer,
    UserResponseSerializer,
)

logger = logging.getLogger(__name__)

# The existing LogoutSerializer lives in authentication/views.py.
# We import it to reuse it in our new LogoutView wrapper — or you can
# keep using authentication/views.LogoutView directly. Either works.


# ─────────────────────────────────────────────────────────────
#  Email verification helpers
# ─────────────────────────────────────────────────────────────

def _build_verification_url(user):
    """
    Build <SITE_URL>/api/verify-email/?uid=<pk>&token=<token>

    SITE_URL comes from the environment (.env) so the link is openable
    from a phone on the same Wi-Fi (http://192.168.x.x:8000) or from the
    internet in production — never hard-coded to 127.0.0.1.
    Uses Django's official token generator (never a homemade one).
    """
    token = default_token_generator.make_token(user)
    path = reverse('verify-email')                     # → /api/verify-email/
    query = urlencode({'uid': user.pk, 'token': token})
    return f'{settings.SITE_URL}{path}?{query}'


def send_verification_email(user):
    """
    Generate a fresh verification token and email it to the user.
    The token is NEVER written to the logs — only sent inside the email.
    """
    verification_url = _build_verification_url(user)
    send_mail(
        subject='ORIZZONTE - Vérifiez votre adresse email',
        message=(
            f'Bonjour {user.username},\n\n'
            'Bienvenue sur ORIZZONTE.\n\n'
            'Pour activer votre compte, ouvrez ce lien dans votre navigateur :\n\n'
            f'{verification_url}\n\n'
            'Ce lien expire au bout de 3 jours.\n'
            "Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.\n\n"
            "L'équipe ORIZZONTE\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def _wants_html(request):
    """True when the request comes from a browser (clicking the link)."""
    return 'text/html' in request.headers.get('Accept', '')


def _verification_page(message, success):
    """
    Minimal, self-contained HTML page shown when the user clicks the
    verification link in a browser. The API/Flutter answer stays JSON.
    """
    color = '#0a7d34' if success else '#b3261e'
    title = 'ORIZZONTE - Email vérifié' if success else 'ORIZZONTE'
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
  body {{ font-family: Arial, sans-serif; background:#f4f6f8; margin:0;
         display:flex; align-items:center; justify-content:center;
         min-height:100vh; }}
  .card {{ background:#fff; border-radius:12px; padding:32px 24px;
           max-width:420px; text-align:center;
           box-shadow:0 2px 12px rgba(0,0,0,.08); }}
  .badge {{ font-size:40px; line-height:1; }}
  h1 {{ font-size:20px; color:{color}; margin:16px 0 8px; }}
  p {{ color:#444; font-size:15px; line-height:1.5; margin:0; }}
  .hint {{ color:#888; font-size:13px; margin-top:16px; }}
</style>
</head>
<body>
  <div class="card">
    <div class="badge">{'✅' if success else '⚠️'}</div>
    <h1>{escape(message)}</h1>
    <p>{"Vous pouvez maintenant vous connecter à l'application ORIZZONTE."
        if success else
        "Ce lien est invalide ou a expiré. Demandez un nouveau lien depuis l'application."}</p>
    <p class="hint">Vous pouvez fermer cette page.</p>
  </div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
#  Auth views (replace the ones in authentication/views.py)
# ─────────────────────────────────────────────────────────────

class SignUpView(APIView):
    """
    POST /api/signup/

    Public endpoint. The visitor picks the role: "client" (default) or "driver".
    Any other value ("boss", "admin", "employee", "", unknown, ...) is refused
    with a 400 and NO account is created.
    `email_verified` is not accepted from the request body: it is always
    False until the user clicks the link in the verification email.

    Body: { "username", "email", "password", "role" (optional: client|driver) }

    201 → { "message": "...", "user": { id, username, email, role, email_verified, date_joined } }
    400 → { "message": "...", "errors": { ... } }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SignUpSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "message": "Registration failed. Please check the errors below.",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = serializer.save()          # email_verified = False (model default)

        try:
            send_verification_email(user)
        except Exception:
            # Never log the token, only the user id.
            logger.warning(
                "Could not send the verification email to user id=%s", user.pk
            )

        return Response(
            {
                "message": "Account created successfully. A verification email has been sent.",
                "user": UserResponseSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


class VerificationHtmlRenderer(BaseRenderer):
    """
    Present ONLY so content negotiation accepts browsers on
    /api/verify-email/. VerifyEmailView.get() returns a ready-made
    HttpResponse, so this renderer is never actually asked to render —
    it just prevents DRF from answering 406 before reaching the view.
    """
    media_type = 'text/html'
    format = 'html'
    charset = 'utf-8'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, bytes):
            return data
        return str(data).encode(self.charset)


class VerifyEmailView(APIView):
    """
    GET /api/verify-email/?uid=<id>&token=<token>

    Browser (Accept: text/html) → a simple readable HTML page.
    API / Flutter (JSON)        → { "message": "..." }
    Status codes are identical in both cases (200 / 400).

    200 → { "message": "Email verified successfully. You can now log in." }
    200 → { "message": "Email is already verified." }
    400 → { "message": "Invalid or expired verification token." }
    """
    permission_classes = [AllowAny]
    # DRF negotiates the renderer BEFORE calling get(). Without a
    # text/html renderer, a browser (Accept: text/html) would get a 406
    # from the JSON-only default. This renderer is declared ONLY here —
    # every other endpoint stays JSON-only.
    renderer_classes = [JSONRenderer, VerificationHtmlRenderer]

    def get(self, request):
        message, success, code = self._resolve(
            request.query_params.get('uid'),
            request.query_params.get('token'),
        )

        if _wants_html(request):
            return HttpResponse(
                _verification_page(message, success),
                status=code,
                content_type='text/html; charset=utf-8',
            )
        return Response({"message": message}, status=code)

    @staticmethod
    def _resolve(uid, token):
        """Returns (message, success, http_status) — one single logic path."""
        invalid = (
            "Invalid or expired verification token.",
            False,
            status.HTTP_400_BAD_REQUEST,
        )

        if not uid or not token:
            return invalid

        try:
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError):
            # Unknown uid → same answer as a bad token (no information leak)
            return invalid

        if user.email_verified:
            return (
                "Email is already verified.",
                True,
                status.HTTP_200_OK,
            )

        # check_token() validates the signature AND the expiry
        # (settings.PASSWORD_RESET_TIMEOUT). It returns False if invalid.
        if not default_token_generator.check_token(user, token):
            return invalid

        user.email_verified = True
        user.save(update_fields=['email_verified'])
        # The token is now invalid: it depends on user state, so it cannot
        # be replayed (and it expires after PASSWORD_RESET_TIMEOUT anyway).

        return (
            "Email verified successfully. You can now log in.",
            True,
            status.HTTP_200_OK,
        )


class ResendVerificationView(APIView):
    """
    POST /api/resend-verification/

    Body: { "email": "..." }

    Always answers 200 with the same generic message so the endpoint
    cannot be used to find out whether an address exists.
    Rate-limited: 5 requests/minute per IP (DEFAULT_THROTTLE_RATES).
    """
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'resend-verification'

    def post(self, request):
        serializer = ResendVerificationSerializer(data=request.data)
        if not serializer.is_valid():
            # Only a *format* error — reveals nothing about existence
            return Response(
                {"message": "Invalid email address."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = serializer.validated_data['email']
        user = User.objects.filter(email__iexact=email).first()

        if user is not None and not user.email_verified:
            try:
                send_verification_email(user)
            except Exception:
                logger.warning(
                    "Could not resend the verification email to user id=%s", user.pk
                )

        return Response(
            {
                "message": "If an account exists with this email, "
                           "a verification email has been sent."
            },
            status=status.HTTP_200_OK,
        )


class LoginView(TokenObtainPairView):
    """
    POST /api/token/

    Works for ALL roles: boss, employee, client.
    Credentials: EMAIL + PASSWORD.
    Refused (401, no tokens) while the email is unverified.
    Returns role + user info in addition to JWT tokens.

    Body: { "email": "john@example.com", "password": "..." }

    200 → {
        "id": 1,
        "username": "john",
        "email": "john@example.com",
        "role": "driver",
        "access": "<jwt>",
        "refresh": "<jwt>"
    }
    401 → { "detail": "No active account found with the given credentials" }
    401 → { "detail": "Please verify your email before logging in." }
    """
    permission_classes = [AllowAny]
    serializer_class = RoleAwareTokenSerializer   # ← only change vs old LoginView


class MeView(APIView):
    """
    GET /api/users/me/

    Returns the authenticated user's full profile including role.
    Works for every role. Frontend uses `role` for dashboard routing.

    Header: Authorization: Bearer <access_token>

    200 → { id, username, email, role, date_joined }
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            UserResponseSerializer(request.user).data,
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────────────────────
#  Driver management (boss only)
# ─────────────────────────────────────────────────────────────

class DriverListView(APIView):
    """
    GET /api/users/employees/

    Boss-only. Lists every driver account.

    Header: Authorization: Bearer <boss_access_token>

    200 → [ { id, username, email, date_joined }, ... ]
    403 → { "detail": "Access denied. Only the Boss can perform this action." }
    """
    permission_classes = [IsAuthenticated, IsBoss]

    def get(self, request):
        drivers = User.objects.filter(role=User.Role.DRIVER).order_by('date_joined')
        return Response(
            DriverListSerializer(drivers, many=True).data,
            status=status.HTTP_200_OK,
        )


class DriverDetailView(APIView):
    """
    GET    /api/users/employees/<id>/   → view one driver
    DELETE /api/users/employees/<id>/   → remove a driver

    Boss-only.
    """
    permission_classes = [IsAuthenticated, IsBoss]

    def _get_driver(self, pk):
        try:
            return User.objects.get(pk=pk, role=User.Role.DRIVER)
        except User.DoesNotExist:
            return None

    def get(self, request, pk):
        driver = self._get_driver(pk)
        if not driver:
            return Response(
                {"message": "Driver not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            DriverDetailSerializer(driver).data,
            status=status.HTTP_200_OK,
        )

    def delete(self, request, pk):
        driver = self._get_driver(pk)
        if not driver:
            return Response(
                {"message": "Driver not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        username = driver.username
        driver.delete()
        return Response(
            {"message": f"Driver '{username}' has been removed."},
            status=status.HTTP_200_OK,
        )


# Create your views here.
