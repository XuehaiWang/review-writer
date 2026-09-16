"""Read immutable chapter baselines without restoring the whole manuscript."""
from review_writer_api.errors import WorkflowConflict, WorkflowNotFound
from review_writer_core.paragraph_revision import dialogue_sections
from review_writer_core.workflow.artifacts import DRAFT_MANUSCRIPT, DRAFT_INITIAL_MANUSCRIPT


class SectionVersionsMixin:
    def _section_version(self, principal, project_id, section_id, artifact_id):
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact_id)
        artifact = resolved.artifact
        if artifact.project_id != project_id or artifact.logical_name not in {DRAFT_MANUSCRIPT, DRAFT_INITIAL_MANUSCRIPT}:
            raise WorkflowNotFound("Chapter version not found.")
        sections = dialogue_sections(resolved.path.read_text(encoding="utf-8"), artifact.metadata, artifact.id)
        section = next((s for s in sections if s["section_id"] == section_id), None)
        if section is None:
            raise WorkflowNotFound("This version does not contain the chapter.")
        return {"artifact_id": artifact.id, "created_at": artifact.created_at.isoformat(), **section}

    def _initial_section_artifact(self, principal, project_id):
        current = self._artifact(principal, project_id, DRAFT_MANUSCRIPT)
        if not current:
            raise WorkflowNotFound("Current Draft not found.")
        source = current.metadata.get("source_sections_artifact_id")
        if not source:
            return None
        versions = self.repository.list_artifacts(principal.user_id, project_id, DRAFT_INITIAL_MANUSCRIPT)
        matches = [v for v in versions if v.metadata.get("source_sections_artifact_id") == source]
        if not matches:
            # Old projects: only an untouched assembly is a verified initial version.
            matches = [v for v in self.repository.list_artifacts(principal.user_id, project_id, DRAFT_MANUSCRIPT)
                       if v.metadata.get("source_sections_artifact_id") == source
                       and v.metadata.get("operation") == "assemble"
                       and not v.metadata.get("source_rewrite_overlay_artifact_id")]
        return matches[-1] if matches else None

    def section_versions(self, principal, project_id, section_id):
        self._owned_project(principal, project_id)
        current = self._artifact(principal, project_id, DRAFT_MANUSCRIPT)
        if not current:
            raise WorkflowNotFound("Current Draft not found.")
        initial = self._initial_section_artifact(principal, project_id)
        try:
            baseline = self._section_version(principal, project_id, section_id, initial.id) if initial else None
        except WorkflowNotFound:
            baseline = None
        versions = self.repository.list_artifacts(principal.user_id, project_id, DRAFT_MANUSCRIPT)
        return {"current": self._section_version(principal, project_id, section_id, current.id),
                "initial": baseline,
                "versions": [{"artifact_id": v.id, "created_at": v.created_at.isoformat()}
                             for v in versions if v.metadata.get("source_sections_artifact_id") == current.metadata.get("source_sections_artifact_id")]}

    def initial_section_for_restart(self, principal, project_id, section_id, artifact_id):
        initial = self._initial_section_artifact(principal, project_id)
        if initial is None or initial.id != artifact_id:
            raise WorkflowConflict("The initial chapter version changed or could not be verified. Reopen the chapter versions.")
        return self._section_version(principal, project_id, section_id, artifact_id)
