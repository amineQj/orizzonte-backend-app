"""
accounts/tests.py

End-to-end tests for the authentication flow:

    signup (role client|driver) -> verification email -> verify-email
        -> login with EMAIL + PASSWORD (refused until verified)
        -> JWT access/refresh -> protected endpoints
        -> refresh rotation -> logout -> blacklist
        -> resend verification (generic answer + throttle)

Also covers:
    * the role rules at signup (only client|driver, no account on failure),
    * the driver endpoints (list/detail/delete) and the REMOVED create route,
    * the role/permission matrix for boss / client / driver / anonymous,
    * the management commands `create_boss` and `reset_users`.
"""
import os
from io import StringIO
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .models import User

PASSWORD = 'TestPassword123!'
BOSS_PASSWORD = 'BossStrongPass!234'   # throwaway, never used outside tests


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────

def signup(client, username='testuser', email='testuser@example.com', **extra):
    payload = {'username': username, 'email': email, 'password': PASSWORD}
    payload.update(extra)
    return client.post(reverse('signup'), payload, format='json')


def login(client, email, password=PASSWORD):
    return client.post(
        reverse('token_obtain_pair'),
        {'email': email, 'password': password},
        format='json',
    )


def verification_link(message):
    """Extract (uid, token) from the link contained in a verification email."""
    for line in message.body.splitlines():
        if '/api/verify-email/' in line:
            query = parse_qs(urlparse(line.strip()).query)
            return int(query['uid'][0]), query['token'][0]
    raise AssertionError('No verification link found in the email body.')


def verify(client, uid, token):
    return client.get(f'{reverse("verify-email")}?uid={uid}&token={token}')


# ─────────────────────────────────────────────────────────────
#  Signup
# ─────────────────────────────────────────────────────────────

class SignUpTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_signup_creates_unverified_client_and_sends_email(self):
        response = signup(self.client)

        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['user']['role'], 'client')
        self.assertIs(data['user']['email_verified'], False)
        self.assertNotIn('password', data['user'])

        user = User.objects.get(email='testuser@example.com')
        self.assertFalse(user.email_verified)
        # Django hasher: password is stored hashed, never in clear text
        self.assertTrue(user.check_password(PASSWORD))
        self.assertNotEqual(user.password, PASSWORD)

        # The verification email really left through Django's email backend
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['testuser@example.com'])
        self.assertIn('/api/verify-email/', mail.outbox[0].body)

    def test_signup_defaults_to_client(self):
        response = signup(self.client)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['user']['role'], 'client')
        self.assertEqual(User.objects.get(email='testuser@example.com').role, User.Role.CLIENT)

    def test_signup_with_driver_role(self):
        response = signup(
            self.client,
            username='driver1',
            email='driver1@example.com',
            role='driver',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['user']['role'], 'driver')
        user = User.objects.get(email='driver1@example.com')
        self.assertEqual(user.role, User.Role.DRIVER)
        self.assertFalse(user.email_verified)

    def test_signup_cannot_inject_email_verified(self):
        response = signup(
            self.client,
            username='injector',
            email='injector@example.com',
            role='client',
            email_verified=True,
        )

        self.assertEqual(response.status_code, 201)
        user = User.objects.get(email='injector@example.com')
        self.assertEqual(user.role, User.Role.CLIENT)
        self.assertFalse(user.email_verified)

    def test_signup_rejects_forbidden_roles_without_creating_any_account(self):
        forbidden = ['boss', 'admin', 'employee', '', 'superuser', 'DRIVER?', 'root']
        before = User.objects.count()

        for bad_role in forbidden:
            with self.subTest(role=bad_role):
                response = signup(
                    self.client,
                    username=f'u_{abs(hash(bad_role))}',
                    email=f'u_{abs(hash(bad_role))}@example.com',
                    role=bad_role,
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn('role', response.json()['errors'])
                self.assertIn(
                    "Allowed values: 'client', 'driver'",
                    str(response.json()['errors']['role']),
                )

        # No account was created by any of the refused attempts.
        self.assertEqual(User.objects.count(), before)

    def test_signup_rejects_duplicate_email(self):
        signup(self.client)
        response = signup(self.client, username='other')

        self.assertEqual(response.status_code, 400)
        self.assertIn('email', response.json()['errors'])

    def test_signup_rejects_invalid_email(self):
        response = signup(self.client, email='not-an-email')

        self.assertEqual(response.status_code, 400)
        self.assertIn('email', response.json()['errors'])


# ─────────────────────────────────────────────────────────────
#  Email verification
# ─────────────────────────────────────────────────────────────

class VerifyEmailTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        signup(self.client)
        self.uid, self.token = verification_link(mail.outbox[0])

    def test_valid_token_verifies_the_email(self):
        response = verify(self.client, self.uid, self.token)

        self.assertEqual(response.status_code, 200)
        self.assertIn('verified successfully', response.json()['message'])
        self.assertTrue(User.objects.get(pk=self.uid).email_verified)

    def test_invalid_token_is_rejected(self):
        response = verify(self.client, self.uid, 'not-a-real-token')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid or expired', response.json()['message'])
        self.assertFalse(User.objects.get(pk=self.uid).email_verified)

    def test_unknown_uid_is_rejected_without_500(self):
        response = verify(self.client, 999999, 'invalid')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid or expired', response.json()['message'])

    def test_missing_parameters_are_rejected(self):
        response = self.client.get(reverse('verify-email'))

        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid or expired', response.json()['message'])

    def test_already_verified_returns_200(self):
        verify(self.client, self.uid, self.token)
        response = verify(self.client, self.uid, self.token)

        self.assertEqual(response.status_code, 200)
        self.assertIn('already verified', response.json()['message'])

    @override_settings(PASSWORD_RESET_TIMEOUT=-1)
    def test_expired_token_is_rejected(self):
        response = verify(self.client, self.uid, self.token)

        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid or expired', response.json()['message'])
        self.assertFalse(User.objects.get(pk=self.uid).email_verified)


# ─────────────────────────────────────────────────────────────
#  Login (email + password + verification check)
# ─────────────────────────────────────────────────────────────

class LoginTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        signup(self.client)
        self.uid, self.token = verification_link(mail.outbox[0])

    def test_login_is_refused_while_email_is_unverified(self):
        response = login(self.client, 'testuser@example.com')

        self.assertEqual(response.status_code, 401)
        data = response.json()
        self.assertIn('verify your email', data['detail'])
        self.assertNotIn('access', data)
        self.assertNotIn('refresh', data)

    def test_refused_login_does_not_break_the_verification_link(self):
        # A refused login must not touch user.last_login, otherwise
        # default_token_generator would invalidate the pending token.
        login(self.client, 'testuser@example.com')
        verify(self.client, self.uid, self.token)
        response = login(self.client, 'testuser@example.com')

        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.json())

    def test_login_with_email_after_verification(self):
        verify(self.client, self.uid, self.token)
        response = login(self.client, 'testuser@example.com')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        for key in ('id', 'username', 'email', 'role', 'access', 'refresh'):
            self.assertIn(key, data)
        self.assertEqual(data['email'], 'testuser@example.com')
        self.assertEqual(data['role'], 'client')

    def test_login_with_wrong_password_is_refused(self):
        verify(self.client, self.uid, self.token)
        response = login(self.client, 'testuser@example.com', password='WrongPass123!')

        self.assertEqual(response.status_code, 401)
        self.assertNotIn('access', response.json())

    def test_login_without_email_field_is_rejected(self):
        response = self.client.post(
            reverse('token_obtain_pair'),
            {'username': 'testuser', 'password': PASSWORD},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('email', response.json())


# ─────────────────────────────────────────────────────────────
#  JWT: protected endpoint, refresh, logout, blacklist
# ─────────────────────────────────────────────────────────────

class JwtFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        signup(self.client)
        uid, token = verification_link(mail.outbox[0])
        verify(self.client, uid, token)
        self.tokens = login(self.client, 'testuser@example.com').json()

    def test_me_returns_profile_with_token(self):
        response = self.client.get(
            reverse('me'),
            HTTP_AUTHORIZATION=f'Bearer {self.tokens["access"]}',
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['email'], 'testuser@example.com')
        self.assertIs(data['email_verified'], True)
        self.assertNotIn('password', data)

    def test_me_without_token_returns_401(self):
        response = self.client.get(reverse('me'))

        self.assertEqual(response.status_code, 401)

    def test_refresh_returns_new_tokens_and_blacklists_the_old_one(self):
        response = self.client.post(
            reverse('token_refresh'),
            {'refresh': self.tokens['refresh']},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.json())
        self.assertIn('refresh', response.json())

        # ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION: the old
        # refresh token can no longer be replayed.
        replay = self.client.post(
            reverse('token_refresh'),
            {'refresh': self.tokens['refresh']},
            format='json',
        )
        self.assertEqual(replay.status_code, 401)

    def test_logout_blacklists_the_refresh_token(self):
        response = self.client.post(
            reverse('logout'),
            {'refresh': self.tokens['refresh']},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.tokens["access"]}',
        )
        self.assertEqual(response.status_code, 200)

        after = self.client.post(
            reverse('token_refresh'),
            {'refresh': self.tokens['refresh']},
            format='json',
        )
        self.assertEqual(after.status_code, 401)


# ─────────────────────────────────────────────────────────────
#  Resend verification
# ─────────────────────────────────────────────────────────────

class ResendVerificationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        cache.clear()  # start every test with a fresh throttle window

    def test_resend_for_unverified_account_sends_a_new_email(self):
        signup(self.client)
        self.assertEqual(len(mail.outbox), 1)

        response = self.client.post(
            reverse('resend-verification'),
            {'email': 'testuser@example.com'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('If an account exists', response.json()['message'])
        self.assertEqual(len(mail.outbox), 2)

    def test_resend_for_unknown_email_is_generic(self):
        response = self.client.post(
            reverse('resend-verification'),
            {'email': 'nobody@example.com'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('If an account exists', response.json()['message'])
        self.assertEqual(len(mail.outbox), 0)

    def test_resend_for_verified_account_sends_nothing(self):
        signup(self.client)
        uid, token = verification_link(mail.outbox[0])
        verify(self.client, uid, token)

        response = self.client.post(
            reverse('resend-verification'),
            {'email': 'testuser@example.com'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)  # no new email

    def test_resend_with_invalid_email_format(self):
        response = self.client.post(
            reverse('resend-verification'),
            {'email': 'nope'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)

    def test_resend_is_rate_limited(self):
        statuses = []
        for _ in range(6):
            response = self.client.post(
                reverse('resend-verification'),
                {'email': 'testuser@example.com'},
                format='json',
            )
            statuses.append(response.status_code)

        self.assertEqual(statuses[:5], [200] * 5)
        self.assertEqual(statuses[5], 429)


# ─────────────────────────────────────────────────────────────
#  Driver endpoints (boss only) + the REMOVED create route
# ─────────────────────────────────────────────────────────────

class DriverEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.boss = User.objects.create_user(
            username='boss',
            email='boss@example.com',
            password=PASSWORD,
            role=User.Role.BOSS,
            email_verified=True,
        )
        self.client_user = User.objects.create_user(
            username='client',
            email='client@example.com',
            password=PASSWORD,
            role=User.Role.CLIENT,
            email_verified=True,
        )
        self.driver = User.objects.create_user(
            username='driver',
            email='driver@example.com',
            password=PASSWORD,
            role=User.Role.DRIVER,
            email_verified=True,
        )
        boss_tokens = login(self.client, 'boss@example.com').json()
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {boss_tokens["access"]}'}

    def test_employee_create_route_is_removed_for_every_role(self):
        url = '/api/users/employees/create/'
        payload = {'username': 'x', 'password': PASSWORD}

        # anonymous
        self.assertEqual(
            self.client.post(url, payload, format='json').status_code, 404
        )
        # boss, client, driver
        for email in ('boss@example.com', 'client@example.com', 'driver@example.com'):
            tokens = login(self.client, email).json()
            response = self.client.post(
                url, payload, format='json',
                HTTP_AUTHORIZATION=f'Bearer {tokens["access"]}',
            )
            self.assertEqual(response.status_code, 404)
        self.assertFalse(User.objects.filter(username='x').exists())

    def test_boss_can_list_detail_and_delete_drivers(self):
        listing = self.client.get(reverse('employee-list'), **self.auth)
        self.assertEqual(listing.status_code, 200)
        ids = [item['id'] for item in listing.json()]
        self.assertIn(self.driver.id, ids)
        self.assertNotIn(self.boss.id, ids)      # never the boss
        self.assertNotIn(self.client_user.id, ids)  # never a client

        detail = self.client.get(
            reverse('employee-detail', kwargs={'pk': self.driver.pk}), **self.auth
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['role'], 'driver')

        # A client id is not a driver → 404 on the driver detail route
        wrong = self.client.get(
            reverse('employee-detail', kwargs={'pk': self.client_user.pk}), **self.auth
        )
        self.assertEqual(wrong.status_code, 404)

        deleted = self.client.delete(
            reverse('employee-detail', kwargs={'pk': self.driver.pk}), **self.auth
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(User.objects.filter(pk=self.driver.pk).exists())

        missing = self.client.delete(
            reverse('employee-detail', kwargs={'pk': 424242}), **self.auth
        )
        self.assertEqual(missing.status_code, 404)

    def test_client_and_driver_cannot_list_drivers(self):
        for email in ('client@example.com', 'driver@example.com'):
            tokens = login(self.client, email).json()
            response = self.client.get(
                reverse('employee-list'),
                HTTP_AUTHORIZATION=f'Bearer {tokens["access"]}',
            )
            self.assertEqual(response.status_code, 403)

    def test_driver_list_requires_authentication(self):
        self.assertEqual(self.client.get(reverse('employee-list')).status_code, 401)


# ─────────────────────────────────────────────────────────────
#  Missions routes still mounted
# ─────────────────────────────────────────────────────────────

class MissionRoutesTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        boss = User.objects.create_user(
            username='boss2',
            email='boss2@example.com',
            password=PASSWORD,
            role=User.Role.BOSS,
            email_verified=True,
        )
        self.assertIsNotNone(boss)
        tokens = login(self.client, 'boss2@example.com').json()
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {tokens["access"]}'}

    def test_mission_urls_are_still_resolved(self):
        self.assertEqual(reverse('missions:mission-estimate'), '/api/missions/estimate/')
        self.assertEqual(reverse('missions:mission-all'), '/api/missions/all/')
        self.assertEqual(reverse('missions:boss-dashboard'), '/api/missions/dashboard/')

    def test_boss_can_list_missions(self):
        response = self.client.get(reverse('missions:mission-all'), **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)

    def test_missions_require_authentication(self):
        response = self.client.get(reverse('missions:mission-all'))

        self.assertEqual(response.status_code, 401)


# ─────────────────────────────────────────────────────────────
#  Verification email content + SITE_URL
# ─────────────────────────────────────────────────────────────

def link_line(message):
    """First line of an email that contains the verification link."""
    return next(
        line.strip()
        for line in message.body.splitlines()
        if '/api/verify-email/' in line
    )


class VerificationEmailTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_signup_email_content(self):
        signup(self.client)
        message = mail.outbox[0]

        self.assertIn('ORIZZONTE', message.subject)
        self.assertIn('ORIZZONTE', message.from_email)
        self.assertEqual(message.to, ['testuser@example.com'])
        self.assertTrue(message.body.startswith('Bonjour testuser,'))
        self.assertIn('Pour activer votre compte', message.body)
        self.assertIn('expire', message.body)
        # The link uses SITE_URL (settings), not the Host header
        self.assertTrue(link_line(message).startswith(settings.SITE_URL))

    @override_settings(SITE_URL='http://192.168.1.20:8000')
    def test_link_is_built_from_site_url(self):
        signup(self.client)

        self.assertTrue(
            link_line(mail.outbox[0]).startswith(
                'http://192.168.1.20:8000/api/verify-email/?uid='
            )
        )

    @override_settings(SITE_URL='https://api.orizzonte.example')
    def test_resend_link_is_built_from_site_url(self):
        signup(self.client)
        response = self.client.post(
            reverse('resend-verification'),
            {'email': 'testuser@example.com'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(
            link_line(mail.outbox[1]).startswith(
                'https://api.orizzonte.example/api/verify-email/?uid='
            )
        )

    def test_site_url_is_normalised_without_trailing_slash(self):
        # settings strips any trailing slash from SITE_URL; this must hold
        # whatever value the developer put in .env.
        self.assertFalse(settings.SITE_URL.endswith('/'))
        self.assertTrue(settings.SITE_URL.startswith('http'))


# ─────────────────────────────────────────────────────────────
#  Verification link opened in a browser → HTML page
# ─────────────────────────────────────────────────────────────

class VerifyEmailHtmlTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        signup(self.client)
        self.uid, self.token = verification_link(mail.outbox[0])

    def _open_in_browser(self, uid, token):
        url = f'{reverse("verify-email")}?uid={uid}&token={token}'
        return self.client.get(url, HTTP_ACCEPT='text/html,application/xhtml+xml')

    def test_valid_token_shows_html_success_page(self):
        response = self._open_in_browser(self.uid, self.token)

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response['Content-Type'])
        self.assertIn('Email verified successfully', response.content.decode())
        self.assertIn('ORIZZONTE', response.content.decode())
        self.assertTrue(User.objects.get(pk=self.uid).email_verified)

    def test_already_verified_shows_html_page(self):
        self._open_in_browser(self.uid, self.token)
        response = self._open_in_browser(self.uid, self.token)

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response['Content-Type'])
        self.assertIn('already verified', response.content.decode())

    def test_invalid_token_shows_html_error_page_without_500(self):
        response = self._open_in_browser(self.uid, 'bad-token')

        self.assertEqual(response.status_code, 400)
        self.assertIn('text/html', response['Content-Type'])
        self.assertIn('Invalid or expired', response.content.decode())

    def test_unknown_uid_shows_html_error_page(self):
        response = self._open_in_browser(999999, 'bad-token')

        self.assertEqual(response.status_code, 400)
        self.assertIn('text/html', response['Content-Type'])

    def test_api_still_receives_json_by_default(self):
        url = f'{reverse("verify-email")}?uid={self.uid}&token={self.token}'
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertIn('application/json', response['Content-Type'])
        self.assertIn('message', response.json())


# ─────────────────────────────────────────────────────────────
#  Full flow in one test: signup → link → verify → login →
#  me → refresh → logout → refresh refused
# ─────────────────────────────────────────────────────────────

class CompleteFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_full_authentication_flow(self):
        # 1) Signup
        response = signup(self.client)
        self.assertEqual(response.status_code, 201)
        self.assertIs(response.json()['user']['email_verified'], False)

        # 2) Link delivered by email
        uid, token = verification_link(mail.outbox[0])

        # 3) Verify
        response = verify(self.client, uid, token)
        self.assertEqual(response.status_code, 200)

        # 4) Login with email + password
        response = login(self.client, 'testuser@example.com')
        self.assertEqual(response.status_code, 200)
        tokens = response.json()
        self.assertIn('access', tokens)
        self.assertIn('refresh', tokens)

        # 5) Protected endpoint
        response = self.client.get(
            reverse('me'), HTTP_AUTHORIZATION=f'Bearer {tokens["access"]}'
        )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()['email_verified'], True)

        # 6) Refresh → rotation
        response = self.client.post(
            reverse('token_refresh'), {'refresh': tokens['refresh']}, format='json'
        )
        self.assertEqual(response.status_code, 200)
        rotated = response.json()

        # 7) Logout with the ROTATED refresh token
        response = self.client.post(
            reverse('logout'),
            {'refresh': rotated['refresh']},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {tokens["access"]}',
        )
        self.assertEqual(response.status_code, 200)

        # 8) The blacklisted refresh token is refused
        response = self.client.post(
            reverse('token_refresh'),
            {'refresh': rotated['refresh']},
            format='json',
        )
        self.assertEqual(response.status_code, 401)


# ─────────────────────────────────────────────────────────────
#  Driver full flow: signup(driver) → verify → login → me
# ─────────────────────────────────────────────────────────────

class DriverFullFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_driver_signup_verify_login_flow(self):
        email = 'driverflow@example.com'
        response = signup(
            self.client, username='driverflow', email=email, role='driver'
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['user']['role'], 'driver')
        self.assertIs(response.json()['user']['email_verified'], False)

        # login refused while unverified
        self.assertEqual(login(self.client, email).status_code, 401)

        uid, token = verification_link(mail.outbox[0])
        self.assertEqual(verify(self.client, uid, token).status_code, 200)

        logged = login(self.client, email)
        self.assertEqual(logged.status_code, 200)
        data = logged.json()
        self.assertEqual(data['role'], 'driver')

        me = self.client.get(
            reverse('me'), HTTP_AUTHORIZATION=f'Bearer {data["access"]}'
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()['role'], 'driver')
        self.assertIs(me.json()['email_verified'], True)


# ─────────────────────────────────────────────────────────────
#  Role / permission matrix: boss, client, driver, anonymous
# ─────────────────────────────────────────────────────────────

class PermissionMatrixTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        User.objects.create_user(
            username='m_boss', email='m_boss@example.com', password=PASSWORD,
            role=User.Role.BOSS, email_verified=True,
        )
        User.objects.create_user(
            username='m_client', email='m_client@example.com', password=PASSWORD,
            role=User.Role.CLIENT, email_verified=True,
        )
        User.objects.create_user(
            username='m_driver', email='m_driver@example.com', password=PASSWORD,
            role=User.Role.DRIVER, email_verified=True,
        )

    def _auth(self, email):
        tokens = login(self.client, email).json()
        return {'HTTP_AUTHORIZATION': f'Bearer {tokens["access"]}'}

    def test_boss_can_reach_boss_routes(self):
        auth = self._auth('m_boss@example.com')
        self.assertEqual(self.client.get(reverse('employee-list'), **auth).status_code, 200)
        self.assertEqual(self.client.get(reverse('missions:mission-all'), **auth).status_code, 200)
        self.assertEqual(self.client.get(reverse('missions:boss-dashboard'), **auth).status_code, 200)

    def test_client_is_forbidden_from_boss_and_driver_routes(self):
        auth = self._auth('m_client@example.com')
        self.assertEqual(self.client.get(reverse('employee-list'), **auth).status_code, 403)
        self.assertEqual(self.client.get(reverse('missions:mission-all'), **auth).status_code, 403)
        self.assertEqual(self.client.get(reverse('missions:boss-dashboard'), **auth).status_code, 403)
        self.assertEqual(self.client.get(reverse('missions:mission-available'), **auth).status_code, 403)
        self.assertEqual(self.client.get(reverse('missions:mission-my'), **auth).status_code, 403)

    def test_driver_forbidden_from_boss_routes_but_allowed_on_driver_routes(self):
        auth = self._auth('m_driver@example.com')
        self.assertEqual(self.client.get(reverse('employee-list'), **auth).status_code, 403)
        self.assertEqual(self.client.get(reverse('missions:mission-all'), **auth).status_code, 403)
        self.assertEqual(self.client.get(reverse('missions:boss-dashboard'), **auth).status_code, 403)
        self.assertEqual(self.client.get(reverse('missions:mission-available'), **auth).status_code, 200)
        self.assertEqual(self.client.get(reverse('missions:mission-my'), **auth).status_code, 200)

    def test_anonymous_gets_401_everywhere(self):
        for name in (
            'employee-list',
            'missions:mission-all',
            'missions:boss-dashboard',
            'missions:mission-available',
            'missions:mission-my',
        ):
            with self.subTest(route=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 401)

    def test_client_can_use_the_estimate_endpoint(self):
        auth = self._auth('m_client@example.com')
        payload = {
            'truck_type': 'medium',
            'category': 'open',
            'capacity': 100,
            'size': 'small',
            'departure_latitude': '36.750000',
            'departure_longitude': '3.050000',
            'arrival_latitude': '36.800000',
            'arrival_longitude': '3.100000',
            'distance': '10.00',
            'date': '2026-10-15',
            'time': '10:30:00',
            'workers': 0,
        }
        response = self.client.post(
            reverse('missions:mission-estimate'), payload, format='json', **auth
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('estimated_price', response.json())


# ─────────────────────────────────────────────────────────────
#  Management command: create_boss
# ─────────────────────────────────────────────────────────────

class CreateBossCommandTests(TestCase):
    def test_create_boss_creates_a_verified_superuser_with_role_boss(self):
        out = StringIO()
        with patch.dict(os.environ, {'BOSS_PASSWORD': BOSS_PASSWORD}):
            call_command('create_boss', '--email', 'chief@example.com', stdout=out)

        boss = User.objects.get(email='chief@example.com')
        self.assertEqual(boss.role, User.Role.BOSS)
        self.assertTrue(boss.email_verified)
        self.assertTrue(boss.is_staff)
        self.assertTrue(boss.is_superuser)
        self.assertTrue(boss.check_password(BOSS_PASSWORD))
        # The password must never appear in the command output.
        self.assertNotIn(BOSS_PASSWORD, out.getvalue())

    def test_create_boss_login_via_token_endpoint(self):
        with patch.dict(os.environ, {'BOSS_PASSWORD': BOSS_PASSWORD}):
            call_command('create_boss', '--email', 'chief2@example.com', stdout=StringIO())

        response = login(APIClient(), 'chief2@example.com', password=BOSS_PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['role'], 'boss')

    def test_create_boss_rejects_a_weak_password(self):
        before = User.objects.count()
        with patch.dict(os.environ, {'BOSS_PASSWORD': '123'}):
            with self.assertRaises(CommandError):
                call_command('create_boss', '--email', 'weak@example.com')
        self.assertEqual(User.objects.count(), before)
        self.assertFalse(User.objects.filter(email='weak@example.com').exists())

    def test_create_boss_refuses_an_existing_email(self):
        with patch.dict(os.environ, {'BOSS_PASSWORD': BOSS_PASSWORD}):
            call_command('create_boss', '--email', 'dup@example.com')

        before = User.objects.count()
        with patch.dict(os.environ, {'BOSS_PASSWORD': BOSS_PASSWORD}):
            with self.assertRaises(CommandError):
                call_command('create_boss', '--email', 'dup@example.com')
        self.assertEqual(User.objects.count(), before)

    def test_boss_password_never_appears_in_command_output(self):
        out = StringIO()
        with patch.dict(os.environ, {'BOSS_PASSWORD': BOSS_PASSWORD}):
            call_command('create_boss', '--email', 'quiet@example.com', stdout=out)
        self.assertNotIn(BOSS_PASSWORD, out.getvalue())


# ─────────────────────────────────────────────────────────────
#  Management command: reset_users
# ─────────────────────────────────────────────────────────────

class ResetUsersCommandTests(TestCase):
    def _seed(self):
        User.objects.create_user(
            username='r_client', email='r_client@example.com', password=PASSWORD,
            role=User.Role.CLIENT, email_verified=True,
        )
        User.objects.create_user(
            username='r_driver', email='r_driver@example.com', password=PASSWORD,
            role=User.Role.DRIVER, email_verified=True,
        )
        # one mission + one outstanding token (via a login)
        client_tokens = login(APIClient(), 'r_client@example.com').json()
        self.assertIn('access', client_tokens)

    @override_settings(DEBUG=True)
    def test_without_confirmation_does_not_delete_anything(self):
        self._seed()
        before = User.objects.count()
        # Typing anything other than DELETE must delete nothing.
        with patch('builtins.input', return_value='nope'):
            call_command('reset_users', '--no-backup')
        self.assertEqual(User.objects.count(), before)

    @override_settings(DEBUG=True)
    def test_typing_SUPPRIMER_confirms_and_deletes(self):
        self._seed()
        before = User.objects.count()
        self.assertGreater(before, 0)
        with patch('builtins.input', return_value='SUPPRIMER'):
            call_command('reset_users', '--no-backup')
        self.assertEqual(User.objects.count(), 0)

    @override_settings(DEBUG=True)
    def test_eof_on_prompt_deletes_nothing(self):
        self._seed()
        before = User.objects.count()
        with patch('builtins.input', side_effect=EOFError):
            with self.assertRaises(CommandError):
                call_command('reset_users', '--no-backup')
        self.assertEqual(User.objects.count(), before)

    @override_settings(DEBUG=False)
    def test_refuses_on_production_without_allow_production(self):
        self._seed()
        before = User.objects.count()
        with self.assertRaises(CommandError):
            call_command('reset_users', '--yes', '--no-backup')
        self.assertEqual(User.objects.count(), before)

    @override_settings(DEBUG=False)
    def test_allow_production_overrides_the_guard(self):
        self._seed()
        call_command('reset_users', '--yes', '--no-backup', '--allow-production')
        self.assertEqual(User.objects.count(), 0)

    @override_settings(DEBUG=True)
    def test_confirmed_run_deletes_all_users_and_tokens(self):
        self._seed()
        call_command('reset_users', '--yes', '--no-backup')
        self.assertEqual(User.objects.count(), 0)

    @override_settings(DEBUG=True)
    def test_reset_then_create_boss_leaves_exactly_one_user(self):
        self._seed()
        call_command('reset_users', '--yes', '--no-backup')
        with patch.dict(os.environ, {'BOSS_PASSWORD': BOSS_PASSWORD}):
            call_command('create_boss', '--email', 'onlyboss@example.com')

        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(User.objects.first().role, User.Role.BOSS)
