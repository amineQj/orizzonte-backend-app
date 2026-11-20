"""
Django settings for demenagement project.
Authentication backend using JWT (djangorestframework-simplejwt)
"""

import os
from pathlib import Path
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load the .env file sitting next to manage.py (never committed: see .gitignore).
# Missing .env → development defaults below keep everything working.
load_dotenv(BASE_DIR / '.env')

# Secrets come from the environment (.env), never hard-coded for production.
DEBUG = os.environ.get('DEBUG', 'False') == 'True'

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-dev-only-change-me'
    else:
        raise ImproperlyConfigured('SECRET_KEY environment variable is required when DEBUG is False')
if not DEBUG and len(SECRET_KEY) < 50:
    raise ImproperlyConfigured('SECRET_KEY must be at least 50 characters when DEBUG is False')

ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get('ALLOWED_HOSTS', '').split(',') if h.strip()
]
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured('ALLOWED_HOSTS must be set when DEBUG is False')
if not DEBUG and '*' in ALLOWED_HOSTS:
    raise ImproperlyConfigured('ALLOWED_HOSTS cannot contain \"*\" when DEBUG is False')

# Public base URL of the backend — used to build the email-verification link
# so it is openable from a phone (e.g. http://192.168.1.20:8000 in the demo).
SITE_URL = os.environ.get('SITE_URL', 'http://127.0.0.1:8000').rstrip('/')


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',

    # Local apps
    'authentication',
    'accounts',
    'missions',
    'drivers',
]
AUTH_USER_MODEL = 'accounts.User'

# Email + password login: EmailBackend resolves the address (case-insensitive),
# ModelBackend keeps Django admin (username) and groups/permissions working.
AUTHENTICATION_BACKENDS = [
    'accounts.backends.EmailBackend',
    'django.contrib.auth.backends.ModelBackend',
]
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'demenagement.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'demenagement.wsgi.application'


# Database
# Default: the SQLite file next to manage.py.
# DB_NAME can point to another file (e.g. a copy for a test run) without
# touching the real database:  set DB_NAME=...\db.copy.sqlite3
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ.get('DB_NAME', str(BASE_DIR / 'db.sqlite3')),
        # For production, switch to PostgreSQL:
        # 'ENGINE': 'django.db.backends.postgresql',
        # 'NAME': os.environ.get('DB_NAME'),
        # 'USER': os.environ.get('DB_USER'),
        # 'PASSWORD': os.environ.get('DB_PASSWORD'),
        # 'HOST': os.environ.get('DB_HOST', 'localhost'),
        # 'PORT': os.environ.get('DB_PORT', '5432'),
    }
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ─────────────────────────────────────────────────────────────
#  Django REST Framework
# ─────────────────────────────────────────────────────────────
DEFAULT_THROTTLE_RATES = {
    'resend-verification': '5/min',
    'driver-upload': '20/hour',
    'agent': '60/min',
    'token': '10/min',
    'refresh': '30/min',
    'signup': '5/hour',
    'verification-check': '20/hour',
    'logout': '10/min',
}

REST_FRAMEWORK = {
    # All endpoints require authentication by default
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    # Return JSON for all responses
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    'DEFAULT_THROTTLE_CLASSES': (
        'demenagement.throttling.DynamicScopedRateThrottle',
    ),
    'EXCEPTION_HANDLER': 'demenagement.exceptions.custom_exception_handler',
    'DEFAULT_THROTTLE_RATES': DEFAULT_THROTTLE_RATES,
}


# ─────────────────────────────────────────────────────────────
#  SimpleJWT Configuration
# ─────────────────────────────────────────────────────────────
SIMPLE_JWT = {
    # Token lifetimes
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),   # Short-lived access token
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),      # Longer-lived refresh token

    # Security settings
    'ROTATE_REFRESH_TOKENS': True,        # Issue new refresh token on each refresh
    'BLACKLIST_AFTER_ROTATION': True,     # Blacklist old refresh tokens after rotation
    'UPDATE_LAST_LOGIN': True,            # Update user's last_login on token creation

    # Algorithm & signing
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),     # Authorization: Bearer <token>
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',

    # Token identification
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'TOKEN_TYPE_CLAIM': 'token_type',
}


CORS_ALLOW_ALL_ORIGINS = DEBUG and os.environ.get('CORS_ALLOW_ALL_ORIGINS', 'False') == 'True'
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        'CORS_ALLOWED_ORIGINS',
        'http://localhost:5173,http://127.0.0.1:5173',
    ).split(',')
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True
if CORS_ALLOW_ALL_ORIGINS and CORS_ALLOW_CREDENTIALS:
    raise ImproperlyConfigured('CORS_ALLOW_ALL_ORIGINS cannot be True together with CORS_ALLOW_CREDENTIALS')


# ─────────────────────────────────────────────────────────────
#  Email
#  Development (default): the verification email is printed in the
#  Django terminal — console backend, free, no credentials needed.
#
#  Production: put these in .env and nothing else changes
#      EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
#      EMAIL_HOST=smtp.gmail.com
#      EMAIL_PORT=587
#      EMAIL_HOST_USER=you@gmail.com
#      EMAIL_HOST_PASSWORD=xxxx xxxx xxxx xxxx   (Gmail app password)
#      EMAIL_USE_TLS=True
#      DEFAULT_FROM_EMAIL=ORIZZONTE <you@gmail.com>
# ─────────────────────────────────────────────────────────────
EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend'
)
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL', 'ORIZZONTE <noreply@orizzonte.local>'
)

# Email verification tokens (default_token_generator) expire after 3 days
PASSWORD_RESET_TIMEOUT = 60 * 60 * 24 * 3


# ─────────────────────────────────────────────────────────────
#  Driver documents (private storage)
# ─────────────────────────────────────────────────────────────
# Absolute path of the private folder holding identity documents. It must
# NEVER be served by Django and must stay outside STATIC_ROOT/STATICFILES_DIRS.
# There is deliberately NO default: the app refuses to start when it is
# missing (system check ``drivers.E001``), so identity files can never land
# in an implicit location.
PRIVATE_MEDIA_ROOT = os.environ.get('PRIVATE_MEDIA_ROOT', '')

# Maximum size of an uploaded document, in megabytes.
MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', '5'))

# Reject request bodies larger than this before they reach the upload view.
DATA_UPLOAD_MAX_MEMORY_SIZE = int(
    os.environ.get('DATA_UPLOAD_MAX_MEMORY_SIZE', str(6 * 1024 * 1024))
)
DATA_UPLOAD_MAX_NUMBER_FILES = 1


# ─────────────────────────────────────────────────────────────
#  AI agent (external document-validation service)
# ─────────────────────────────────────────────────────────────
# Shared secret sent in the X-Agent-Key header. Empty → agent endpoints are
# disabled. Never logged, never committed.
AI_AGENT_API_KEY = os.environ.get('AI_AGENT_API_KEY', '')

# When False, an agent "approve" is only recorded as a recommendation and the
# profile stays pending_review for the boss. When True, an "approve" is applied
# only if confidence >= AI_AUTO_APPROVE_MIN_CONFIDENCE.
AI_AUTO_APPROVE = os.environ.get('AI_AUTO_APPROVE', 'False') == 'True'
AI_AUTO_APPROVE_MIN_CONFIDENCE = float(
    os.environ.get('AI_AUTO_APPROVE_MIN_CONFIDENCE', '0.90')
)


# ─────────────────────────────────────────────────────────────
#  Image-quality / document-classification bridge
# ─────────────────────────────────────────────────────────────
# The standalone module has its own virtual environment with OpenCV/numpy.
# Leave empty to auto-detect verification_agent/.venv in the project.
VERIFICATION_AGENT_PYTHON = os.environ.get('VERIFICATION_AGENT_PYTHON', '')
VERIFICATION_AGENT_TIMEOUT_SECONDS = float(
    os.environ.get('VERIFICATION_AGENT_TIMEOUT_SECONDS', '180')
)
# Security hardening for production (DEBUG=False). Values are env-controlled
# where a deployment needs to tune them.
if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "False") == "True"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    VERIFICATION_REJECT_RETRY_LIMIT = int(
    os.environ.get("VERIFICATION_REJECT_RETRY_LIMIT", "3")
)
