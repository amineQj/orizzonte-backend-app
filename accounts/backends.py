"""
accounts/backends.py

Authentication backend: EMAIL + PASSWORD.

Why a custom backend?
    Django's ModelBackend looks users up by USERNAME_FIELD ('username').
    Our mobile clients log in with EMAIL, so we need a backend that
    resolves the email (case-insensitive) and checks the password with
    Django's hasher (check_password — never plain text).

    ModelBackend is kept in AUTHENTICATION_BACKENDS so that:
      - Django admin login (username + password) still works,
      - groups / permissions behaviour is unchanged.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

User = get_user_model()


class EmailBackend(ModelBackend):
    """Authenticate with email + password (email matched case-insensitively)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        # SimpleJWT passes the credential as `email=...`, Django admin
        # passes `username=...` — accept both.
        email = kwargs.get('email', username)
        if email is None or password is None:
            return None

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            # Run the password hasher anyway so that timing does not
            # reveal whether the email exists (anti user-enumeration).
            User().set_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
