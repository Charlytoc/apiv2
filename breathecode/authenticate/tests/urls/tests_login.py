"""
Test cases for /user
"""
from rest_framework import status
from django.urls.base import reverse_lazy
from django.contrib.auth.hashers import make_password
from ..mixins.new_auth_test_case import AuthTestCase


def user_invite_serializer(self, user_invite, academy=None, cohort=None):
    return {
        'academy': academy,
        'cohort': cohort,
        'created_at': self.bc.datetime.to_iso_string(user_invite.created_at),
        'email': user_invite.email,
        'first_name': user_invite.first_name,
        'id': user_invite.id,
        'last_name': user_invite.last_name,
        'role': user_invite.role,
        'sent_at': user_invite.sent_at,
        'status': user_invite.status,
        'token': user_invite.token,
    }


class AuthenticateTestSuite(AuthTestCase):
    """Authentication test suite"""

    def test_login_with_bad_credentials(self):
        """Test /login with incorrect credentials"""

        url = reverse_lazy('authenticate:login')
        data = {'email': 'Konan@naruto.io', 'password': 'Pain!$%'}
        response = self.client.post(url, data)

        json = response.json()
        expected = {
            'non_field_errors': ['Unable to log in with provided credentials.'],
            'status_code': 400,
        }

        self.assertEqual(json, expected)
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

    def test_login_without_email(self):
        """Test /login with incorrect credentials"""

        url = reverse_lazy('authenticate:login')
        data = {'password': 'Pain!$%'}
        response = self.client.post(url, data)

        json = response.json()
        expected = {
            'email': ['This field is required.'],
            'status_code': 400,
        }

        self.assertEqual(json, expected)
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

    def test_login_without_password(self):
        """Test /login with incorrect credentials"""

        url = reverse_lazy('authenticate:login')
        data = {'email': 'Konan@naruto.io'}
        response = self.client.post(url, data)

        json = response.json()
        expected = {
            'password': ['This field is required.'],
            'status_code': 400,
        }

        self.assertEqual(json, expected)
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

    def test_login_email_not_verified__no_invites(self):
        """Test /login"""

        password = 'Pain!$%'
        user = {'email': 'Konan@naruto.io', 'password': make_password(password)}
        model = self.bc.database.create(user=user)

        url = reverse_lazy('authenticate:login')
        data = {'email': model.user.email.lower(), 'password': password}
        response = self.client.post(url, data)

        json = response.json()
        expected = {'detail': 'email-not-validated', 'status_code': 403, 'data': []}

        self.assertEqual(json, expected)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_login_email_not_verified__no_invites(self):
        """Test /login"""

        password = 'Pain!$%'
        user = {'email': 'Konan@naruto.io', 'password': make_password(password)}
        model = self.bc.database.create(user=user)

        url = reverse_lazy('authenticate:login')
        data = {'email': model.user.email.lower(), 'password': password}
        response = self.client.post(url, data)

        json = response.json()
        expected = {'detail': 'email-not-validated', 'status_code': 403, 'data': []}

        self.assertEqual(json, expected)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_login_email_not_verified__with_invites(self):
        """Test /login"""

        password = 'Pain!$%'
        user = {'email': 'Konan@naruto.io', 'password': make_password(password)}
        user_invite = {'email': 'Konan@naruto.io', 'status': 'ACCEPTED', 'is_email_validated': False}
        model = self.bc.database.create(user=user, user_invite=(2, user_invite))

        url = reverse_lazy('authenticate:login')
        data = {'email': model.user.email.lower(), 'password': password}
        response = self.client.post(url, data)

        json = response.json()
        expected = {
            'detail':
            'email-not-validated',
            'status_code':
            403,
            'data': [
                user_invite_serializer(self, model.user_invite[1]),
                user_invite_serializer(self, model.user_invite[0]),
            ],
        }

        self.assertEqual(json, expected)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_login_lowercase_email(self):
        """Test /login"""

        password = 'Pain!$%'
        user = {'email': 'Konan@naruto.io', 'password': make_password(password)}
        user_invite = {'email': 'Konan@naruto.io', 'status': 'ACCEPTED', 'is_email_validated': True}
        model = self.bc.database.create(user=user, user_invite=user_invite)

        url = reverse_lazy('authenticate:login')
        data = {'email': model.user.email.lower(), 'password': password}
        response = self.client.post(url, data)

        json = response.json()
        token = self.bc.database.get('authenticate.Token', 1, dict=False)
        expected = {
            'email': model.user.email,
            'expires_at': self.bc.datetime.to_iso_string(token.expires_at),
            'token': token.key,
            'user_id': 1
        }

        self.assertEqual(json, expected)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_login_uppercase_email(self):
        """Test /login"""

        password = 'Pain!$%'
        user = {'email': 'Konan@naruto.io', 'password': make_password(password)}
        user_invite = {'email': 'Konan@naruto.io', 'status': 'ACCEPTED', 'is_email_validated': True}
        model = self.bc.database.create(user=user, user_invite=user_invite)

        url = reverse_lazy('authenticate:login')
        data = {'email': model.user.email.upper(), 'password': password}
        response = self.client.post(url, data)

        json = response.json()
        token = self.bc.database.get('authenticate.Token', 1, dict=False)
        expected = {
            'email': model.user.email,
            'expires_at': self.bc.datetime.to_iso_string(token.expires_at),
            'token': token.key,
            'user_id': 1
        }

        self.assertEqual(json, expected)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
