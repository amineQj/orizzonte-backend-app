"""
accounts/serializers.py

All serializers for the accounts app.
The existing authentication/ serializers (LogoutSerializer etc.) are untouched.
"""
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import User


# ─────────────────────────────────────────────────────────────
#  Signup — client or driver only (never boss)
# ─────────────────────────────────────────────────────────────

# Roles a public visitor may pick at signup.
SIGNUP_ROLES = (User.Role.CLIENT, User.Role.DRIVER)


class SignUpSerializer(serializers.ModelSerializer):
    """
    Used by POST /api/signup/.

    The visitor chooses either "client" (default) or "driver".
    Any other value ("boss", "admin", "employee", "", unknown, ...) is
    REFUSED with a 400 and NO account is created.
    `email_verified` can never be injected: it stays False until the user
    clicks the link in the verification email.
    """
    password = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password],
        style={'input_type': 'password'},
    )
    email = serializers.EmailField(required=True)
    role = serializers.ChoiceField(
        choices=SIGNUP_ROLES,
        required=False,
        default=User.Role.CLIENT,
        error_messages={
            'invalid_choice': (
                "Invalid role. Allowed values: 'client', 'driver'."
            ),
        },
    )

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'password', 'role')
        read_only_fields = ('id',)

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "A user with this email already exists."
            )
        return value.lower()

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError(
                "A user with this username already exists."
            )
        return value

    def create(self, validated_data):
        role = validated_data.get('role', User.Role.CLIENT)  # client | driver

        # One transaction: a driver account is never created without its
        # DriverProfile (and a client never gets one).
        with transaction.atomic():
            user = User.objects.create_user(
                username=validated_data['username'],
                email=validated_data['email'],
                password=validated_data['password'],
                role=role,
                # email_verified is deliberately NOT settable from input:
                # create_user leaves it at its model default (False).
            )
            if role == User.Role.DRIVER:
                from drivers.services import create_driver_profile
                create_driver_profile(user)

        return user


# ─────────────────────────────────────────────────────────────
#  Safe user response (no password, no sensitive fields)
# ─────────────────────────────────────────────────────────────

class UserResponseSerializer(serializers.ModelSerializer):
    """Read-only safe representation returned in all API responses."""
    class Meta:
        model = User
        # `email_verified` lets Flutter know whether the user must still
        # verify the address. `password` is never included.
        fields = ('id', 'username', 'email', 'role', 'email_verified', 'date_joined')
        read_only_fields = fields


class DriverListSerializer(serializers.ModelSerializer):
    """Compact driver list for the boss dashboard, including profile status."""
    profile_status = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'date_joined', 'profile_status')
        read_only_fields = fields

    def get_profile_status(self, obj):
        """``None`` when the driver has no profile yet."""
        profile = getattr(obj, 'driver_profile', None)
        return profile.status if profile is not None else None


class DriverDetailSerializer(serializers.ModelSerializer):
    """Full driver detail for the boss (same fields as the auth response plus
    ``profile_status``)."""
    profile_status = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'role', 'email_verified',
                  'date_joined', 'profile_status')
        read_only_fields = fields

    def get_profile_status(self, obj):
        profile = getattr(obj, 'driver_profile', None)
        return profile.status if profile is not None else None


# ─────────────────────────────────────────────────────────────
#  Custom JWT token — embeds role in the login response
# ─────────────────────────────────────────────────────────────

class RoleAwareTokenSerializer(TokenObtainPairSerializer):
    """
    Login with EMAIL + PASSWORD — POST /api/token/

    Extends SimpleJWT's serializer (never replaces it):
      - credentials are validated by accounts.backends.EmailBackend,
      - login is REFUSED if the email is not verified → no access token,
        no refresh token are ever issued for an unverified account,
      - the response keeps the role/user info used by the frontend.

    Request:
        { "email": "john@example.com", "password": "..." }

    Response shape:
        {
            "id": 1,
            "username": "john",
            "email": "john@example.com",
            "role": "driver",
            "access": "<jwt>",
            "refresh": "<jwt>"
        }
    """

    # SimpleJWT builds the input fields from username_field → email + password
    username_field = 'email'

    def validate(self, attrs):
        # 1) Check the credentials BEFORE SimpleJWT mints any JWT.
        #    (If we checked after super().validate(), UPDATE_LAST_LOGIN would
        #    already have changed user.last_login and invalidated the pending
        #    email-verification token, since default_token_generator hashes it.)
        user = authenticate(
            email=attrs.get('email', ''),
            password=attrs.get('password', ''),
        )

        # 2) Valid credentials but unverified email → hard refusal.
        if user is not None and not user.email_verified:
            raise AuthenticationFailed(
                'Please verify your email before logging in.',
                code='email_not_verified',
            )

        # 3) Standard SimpleJWT validation (auth + tokens). Wrong credentials
        #    still end here and produce SimpleJWT's usual 401 message.
        data = super().validate(attrs)

        # Append user identity + role to the token response
        data['id']       = self.user.id
        data['username'] = self.user.username
        data['email']    = self.user.email
        data['role']     = self.user.role

        return data


# ─────────────────────────────────────────────────────────────
#  Resend verification email
# ─────────────────────────────────────────────────────────────

class ResendVerificationSerializer(serializers.Serializer):
    """POST /api/resend-verification/ — only the email address is required."""
    email = serializers.EmailField(required=True)
