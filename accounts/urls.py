"""
accounts/urls.py

URL map for the accounts app (included at /api/ by demenagement/urls.py):

    POST   /api/signup/                  → Sign up (role: client|driver)
    GET    /api/verify-email/            → Verify email (uid + token)
    POST   /api/resend-verification/     → Resend verification email (5/min)
    POST   /api/token/                   → Login by email (all roles, returns role)
    POST   /api/token/refresh/           → Refresh access token
    POST   /api/logout/                  → Blacklist refresh token
    GET    /api/users/me/                → Current user profile + role

    GET    /api/users/employees/          → List drivers    [boss only]
    GET    /api/users/employees/<id>/     → Driver detail   [boss only]
    DELETE /api/users/employees/<id>/     → Remove driver   [boss only]

Note: /api/users/employees/create/ was REMOVED — it now returns 404 for
every role. Drivers sign up themselves through POST /api/signup/.
"""
from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from authentication.views import LogoutView   # existing endpoint, unchanged
from .views import (
    DriverDetailView,
    DriverListView,
    LoginView,
    MeView,
    ResendVerificationView,
    SignUpView,
    VerifyEmailView,
)

urlpatterns = [
    # ── Public ─────────────────────────────────────────────────
    path('signup/', SignUpView.as_view(), name='signup'),
    path('verify-email/', VerifyEmailView.as_view(), name='verify-email'),
    path('resend-verification/', ResendVerificationView.as_view(), name='resend-verification'),
    path('token/', LoginView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # ── Authenticated ──────────────────────────────────────────
    path('logout/', LogoutView.as_view(), name='logout'),
    path('users/me/', MeView.as_view(), name='me'),

    # ── Boss only (driver management) ──────────────────────────
    path('users/employees/', DriverListView.as_view(), name='employee-list'),
    path('users/employees/<int:pk>/', DriverDetailView.as_view(), name='employee-detail'),
]
