import base64
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base
from review_writer_api.origin_policy import normalize_browser_origin, parse_browser_origins

class BrowserOriginTests(unittest.TestCase):
    def test_origin_validation(self):
        self.assertEqual(parse_browser_origins('https://EXAMPLE.com:443/,http://192.168.0.5:5175'), ('https://example.com', 'http://192.168.0.5:5175'))
        for value in ['*', 'null', 'https://*.example.com', 'https://example.com/path', 'https://user@example.com', 'https://example.com:99999', 'https://example.com?x', 'https://example.com#x', 'https://example.com https://evil.com']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_browser_origin(value)

    def test_logout_allowed_origins_and_session_revocation(self):
        with tempfile.TemporaryDirectory() as raw:
            engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
            Base.metadata.create_all(engine)
            app = create_app(ApiSettings(review_root=Path(raw), deployment_mode='hosted', database_url='sqlite+pysqlite:///:memory:', public_origin='http://testserver', allowed_browser_origins=('http://192.168.0.5:5175',), credential_encryption_key=base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='), hosted_workspace_root=Path(raw)/'users'), session_factory_override=sessionmaker(bind=engine, expire_on_commit=False))
            with TestClient(app) as client:
                for origin in ['http://192.168.0.6:5175', 'https://evil.example', 'null']:
                    response = client.post('/api/v1/auth/logout', headers={'Origin': origin, 'Host': origin.removeprefix('http://'), 'X-Forwarded-Host': '192.168.0.5:5175'})
                    self.assertEqual(response.status_code, 403, response.text)
                for origin in ['http://testserver', 'http://192.168.0.5:5175']:
                    response = client.post('/api/v1/auth/logout', headers={'Origin': origin})
                    self.assertEqual(response.status_code, 204, response.text)
                    self.assertIn("Max-Age=0", response.headers["set-cookie"])
                email = 'origin-test@example.com'
                code = app.state.auth_service.issue_registration_code(email=email)
                response = client.post('/api/v1/auth/register', headers={'Origin': 'http://192.168.0.5:5175'}, json={'email': email, 'verification_code': code, 'password': 'strong-password-123', 'display_name': 'Test'})
                self.assertEqual(response.status_code, 201, response.text)
                token = client.cookies.get('review_writer_session')
                response = client.post('/api/v1/auth/logout', headers={'Origin': 'http://192.168.0.5:5175'})
                self.assertEqual(response.status_code, 204, response.text)
                self.assertEqual(client.post('/api/v1/auth/logout', headers={'Origin': 'http://192.168.0.5:5175'}).status_code, 204)
                self.assertEqual(client.get('/api/v1/me').status_code, 401)
                client.cookies.set('review_writer_session', token)
                self.assertEqual(client.get('/api/v1/me').status_code, 401)
                for stale_token in [token, 'invalid-session-token']:
                    response = client.post('/api/v1/auth/logout', headers={'Origin': 'http://192.168.0.5:5175', 'Cookie': f'review_writer_session={stale_token}'})
                    self.assertEqual(response.status_code, 204, response.text)
                    self.assertIn('Max-Age=0', response.headers['set-cookie'])
            engine.dispose()
