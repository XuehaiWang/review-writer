from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
import threading
import time
from unittest.mock import patch

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, User, database_session, utc_now
from review_writer_api.errors import WorkflowValidationError
from review_writer_api.security import Principal, Role
from review_writer_api.system_errors import list_failures, safe_summary, prune_failures
from review_writer_api.workflow_models import SystemErrorEvent, WorkflowJob


class SystemErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        with database_session(self.sessions) as s:
            user = User(email='admin@test.invalid',password_hash='unused',role='admin')
            s.add(user); s.flush(); self.user_id = user.id
        self.principal = Principal(str(self.user_id), frozenset({Role.ADMIN}))
        self.app = create_app(ApiSettings(review_root=Path(self.tmp.name),deployment_mode='local'),
            principal_provider=lambda:self.principal,session_factory_override=self.sessions)
        dep = next(r for r in self.app.routes if r.path == '/api/v1/admin/errors').dependant.dependencies[0].call
        @self.app.get('/api/v1/admin/test-failure')
        def fail(principal=Depends(dep)):
            raise RuntimeError('password=secret-value sk-do-not-record manuscript-private-content')
        @self.app.get('/api/v1/admin/test-validation')
        def validation_fail(principal=Depends(dep)):
            raise WorkflowValidationError(
                'The bibliography record is missing required fields: journal, year',
                details={'fields': ['journal', 'year']},
            )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.state.failure_recorder.close()
        self.client.close(); self.engine.dispose(); self.tmp.cleanup()

    def test_api_failure_persists_safe_metadata_and_admin_only(self):
        response = self.client.get('/api/v1/admin/test-failure?secret=hidden')
        self.assertEqual(500,response.status_code)
        request_id = response.headers['x-request-id']
        listing = self.client.get('/api/v1/admin/errors').json()['items']
        self.assertEqual(1,len(listing))
        self.assertEqual(request_id,listing[0]['request_id'])
        self.assertEqual('RuntimeError',listing[0]['error_code'])
        for secret in ('secret-value','sk-do-not-record','manuscript-private-content','hidden'):
            self.assertNotIn(secret,str(listing))
        self.principal = Principal(str(self.user_id),frozenset({Role.USER}))
        self.assertEqual(403,self.client.get('/api/v1/admin/errors').status_code)

    def test_jobs_reused_search_pagination_and_redaction(self):
        with database_session(self.sessions) as s:
            for n in range(2):
                s.add(WorkflowJob(user_id=self.user_id,scope='library',job_type='library.ingest',
                    idempotency_key=str(n),status='failed',error_code='PROVIDER_FAILED',
                    error_message='Bearer abc-secret https://host/path?token=secret api_key=hidden'))
        first = list_failures(self.sessions,query='admin@test.invalid',source='job',limit=1)
        self.assertTrue(first['has_more'])
        second = list_failures(self.sessions,source='job',limit=1,offset=1)
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        self.assertNotIn('abc-secret',first['items'][0]['message'])
        self.assertNotIn('hidden',first['items'][0]['message'])
        self.assertEqual([],list_failures(self.sessions,query='nonexistent')['items'])
        self.assertEqual(422,self.client.get('/api/v1/admin/errors?limit=100000').status_code)

    def test_workflow_validation_failure_keeps_safe_actionable_summary(self):
        response = self.client.get('/api/v1/admin/test-validation')
        self.assertEqual(422, response.status_code)
        request_id = response.headers['x-request-id']
        listing = self.client.get(
            f'/api/v1/admin/errors?q={request_id}'
        ).json()['items']

        self.assertEqual(1, len(listing))
        self.assertEqual('WORKFLOW_VALIDATION_FAILED', listing[0]['error_code'])
        self.assertIn('missing required fields: journal, year', listing[0]['message'])
        self.assertIn('Fields: journal, year', listing[0]['message'])

    def test_retention_and_logging_failure_does_not_break_response(self):
        with database_session(self.sessions) as s:
            s.add(SystemErrorEvent(request_id='old',route='/old',method='GET',status_code=500,
                error_code='old',created_at=utc_now()-timedelta(days=31)))
        self.client.get('/api/v1/admin/test-failure')
        prune_failures(self.sessions)
        with database_session(self.sessions) as s:
            self.assertEqual(1,len(s.scalars(select(SystemErrorEvent)).all()))
        with patch('review_writer_api.system_errors.database_session',side_effect=RuntimeError('db down')):
            self.assertEqual(500,self.client.get('/api/v1/admin/test-failure').status_code)

    def test_summary_bounds(self):
        self.assertLessEqual(len(safe_summary('a'*2000)),1000)
        self.assertNotIn('mysecret',safe_summary('{"api_key":"mysecret"}'))

    def test_slow_log_write_does_not_hold_error_response(self):
        release = threading.Event()
        try:
            with patch('review_writer_api.app.record_failure', side_effect=lambda *a: release.wait(3)):
                started = time.monotonic()
                response = self.client.get('/api/v1/admin/test-failure')
                self.assertEqual(500, response.status_code)
                self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
