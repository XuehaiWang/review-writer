from types import SimpleNamespace
from unittest.mock import Mock

from review_writer_api.domain_services.final_history import FinalHistoryMixin
from review_writer_core.workflow.artifacts import FINAL_MANUSCRIPT, FINAL_DOCX, FINAL_PDF


def test_history_preserves_manuscripts_and_matches_exports():
    service = FinalHistoryMixin()
    principal = SimpleNamespace(user_id="owner")
    def artifact(id, metadata=None):
        return SimpleNamespace(id=id, created_at="today", metadata=metadata or {})
    items = {
        FINAL_MANUSCRIPT: [artifact("current"), artifact("old")],
        FINAL_DOCX: [artifact("word", {"source_final_artifact_id": "old"})],
        FINAL_PDF: [artifact("pdf", {"source_final_artifact_id": "current"})],
    }
    service.repository = Mock()
    service.repository.list_artifacts.side_effect = lambda user, project, name: items[name]
    result = service.manuscript_versions(principal, "project", "current")
    assert result[0]["current"] is True
    assert result[0]["downloads"] == [{"artifact_id": "pdf", "format": "PDF"}]
    assert result[1]["downloads"] == [{"artifact_id": "word", "format": "DOCX"}]
    assert all(call.args[:2] == ("owner", "project") for call in service.repository.list_artifacts.call_args_list)
