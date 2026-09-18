from fastapi.testclient import TestClient
from unittest.mock import patch

from review_writer_api.security import Principal, Role
from review_writer_api.tests.figure_test_support import NativeFigureApiTestCase


class StorageAdminTests(NativeFigureApiTestCase):
    def test_storage_permissions_and_explicit_cleanup(self):
        with TestClient(self.app) as client:
            with patch.object(self.app.state.storage_maintenance, "run", return_value={"busy": True}) as run:
                self.assertEqual(403, client.get("/api/v1/admin/storage").status_code)
                self.assertEqual(403, client.post("/api/v1/admin/storage/cleanup").status_code)
                run.assert_not_called()
                self.current = Principal(self.first.user_id, frozenset({Role.ADMIN}))
                response = client.get("/api/v1/admin/storage")
                self.assertEqual(200, response.status_code)
                self.assertIn("free_bytes", response.json()["disk"])
                run.assert_not_called()
                response = client.post("/api/v1/admin/storage/cleanup")
                self.assertEqual(200, response.status_code)
                self.assertTrue(response.json()["busy"])
                run.assert_called_once_with(manual=True)
