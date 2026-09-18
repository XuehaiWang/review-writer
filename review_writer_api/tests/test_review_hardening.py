import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from review_writer_api.ttl_cache import TTLCache
from review_writer_api.persistent_storage import LocalPersistentStorage
from review_writer_api.errors import WorkflowValidationError
from review_writer_api.domain_services.library_index import LibraryIndexService
from review_writer_api.system_errors import FailureRecorder


class HardeningTests(unittest.TestCase):
    def test_bounded_cache_expiry_and_lru(self):
        with patch('review_writer_api.ttl_cache.time.monotonic', return_value=0) as clock:
            cache = TTLCache(2, 10)
            cache['a'] = 1
            cache['b'] = 2
            self.assertEqual(1, cache.get('a'))
            cache['c'] = 3
            self.assertIsNone(cache.get('b'))
            clock.return_value = 11
            self.assertIsNone(cache.get('a'))
            self.assertEqual(0, len(cache._items))

    def test_semantic_failure_isolated_by_user_and_model(self):
        gateway = Mock()
        gateway.embedding_profile.return_value = {'model': 'm', 'enabled': True}
        gateway.embed_for_active_job.side_effect = [RuntimeError('credit'),
            {'model': 'm', 'dimension': 2, 'embeddings': [[1, 0]]}]
        service = LibraryIndexService(None, None, vector_enabled=True, embedding_gateway=gateway)
        service.vector_store.rank = Mock(return_value=[])
        a, b = [SimpleNamespace(user_id=str(uuid.uuid4())) for _ in range(2)]
        with self.assertRaises(RuntimeError):
            service._semantic_ranked_ids(a, 'q', allowed_papers=[], limit=1)
        self.assertTrue(service._semantic_cooling_down(a))
        service._semantic_ranked_ids(b, 'q', allowed_papers=[], limit=1)
        self.assertEqual(2, gateway.embed_for_active_job.call_count)
        gateway.embedding_profile.return_value = {'model': 'new', 'enabled': True}
        self.assertFalse(service._semantic_cooling_down(a))

    def test_object_hash_cache_rechecks_change_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.write_bytes(b'abc')
            storage = LocalPersistentStorage()
            obj = storage.commit_object(source, root, 'objects/item')
            with patch.object(storage, 'inspect_object', wraps=storage.inspect_object) as inspect:
                with storage.borrow_object(root, obj):
                    pass
                with storage.borrow_object(root, obj):
                    pass
                self.assertEqual(1, inspect.call_count)
                with patch('review_writer_api.ttl_cache.time.monotonic', return_value=time.monotonic()+301):
                    with storage.borrow_object(root, obj):
                        pass
                self.assertEqual(2, inspect.call_count)
                (root / obj.key).write_bytes(b'xyz')
                with self.assertRaises(WorkflowValidationError):
                    with storage.borrow_object(root, obj):
                        pass

    def test_failure_queue_bounded_when_database_stalls(self):
        recorder = FailureRecorder()
        release = threading.Event()
        try:
            futures = [recorder.submit(lambda: release.wait(5), str(i)) for i in range(64)]
            self.assertTrue(all(futures))
            self.assertIsNone(recorder.submit(lambda: None, 'overflow'))
        finally:
            release.set()
            recorder.close()

    def test_maintenance_continues_after_one_user_fails(self):
        from review_writer_api.worker_main import maintain_storage
        app = SimpleNamespace(state=SimpleNamespace(session_factory=Mock(), storage_maintenance=Mock(),
            library_index_service=SimpleNamespace(vector_store=Mock())))
        app.state.library_index_service.vector_store.prune.side_effect = [RuntimeError(), []]
        app.state.storage_maintenance.run.side_effect = RuntimeError("Storage unavailable")
        with patch('review_writer_api.database.database_session') as db, \
             patch('review_writer_api.system_errors.prune_failures') as prune:
            db.return_value.__enter__.return_value.scalars.return_value = ['a', 'b']
            maintain_storage(app)
        self.assertEqual(2, app.state.library_index_service.vector_store.prune.call_count)
        prune.assert_called_once()
