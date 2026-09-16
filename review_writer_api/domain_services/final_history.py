"""Read-only final manuscript history and associated exports."""

from review_writer_core.workflow.artifacts import FINAL_MANUSCRIPT, FINAL_DOCX, FINAL_PDF


class FinalHistoryMixin:
    def manuscript_versions(self, principal, project_id, current_id):
        exports = []
        for logical_name, kind in ((FINAL_DOCX, "DOCX"), (FINAL_PDF, "PDF")):
            exports.extend((artifact, kind) for artifact in self.repository.list_artifacts(
                principal.user_id, project_id, logical_name))
        return [{"artifact_id": a.id, "created_at": a.created_at,
                 "operation": a.metadata.get("operation", "final-build"),
                 "current": a.id == current_id,
                 "downloads": [{"artifact_id": output.id, "format": kind} for output, kind in exports
                               if output.metadata.get("source_final_artifact_id") == a.id]}
                for a in self.repository.list_artifacts(principal.user_id, project_id, FINAL_MANUSCRIPT)]
