import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { apiRequest } from "../api/client";
import { projectsQuery, queryKeys } from "../api/queries";
import type { Project } from "../api/types";
import { useUiText } from "../i18n/useUiText";
import { DeleteProjectDialog } from "./DeleteProjectDialog";

export function useSelectedProject() {
  const projects = useQuery(projectsQuery);
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("project") || "";
  const selected = useMemo(
    () => projects.data?.items.find((project) => project.project_id === requested || project.slug === requested) || projects.data?.items[0],
    [projects.data?.items, requested],
  );
  useEffect(() => {
    if (selected && requested !== selected.project_id) {
      const next = new URLSearchParams(searchParams);
      next.set("project", selected.project_id);
      setSearchParams(next, { replace: true });
    }
  }, [requested, searchParams, selected, setSearchParams]);
  const selectProject = (projectId: string) => {
    const next = new URLSearchParams(searchParams);
    if (projectId) next.set("project", projectId); else next.delete("project");
    setSearchParams(next, { replace: true });
  };
  return { projects, selected, selectProject };
}

export function ProjectSelector({ label }: { label?: string }) {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const { projects, selected, selectProject } = useSelectedProject();
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);
  const deleteProject = useMutation({
    mutationFn: (project: Project) => apiRequest<void>(`/api/v1/projects/${encodeURIComponent(project.project_id)}`, { method: "DELETE" }),
    onSuccess: async () => {
      setDeleteTarget(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      selectProject("");
    },
  });
  const confirmDelete = () => {
    if (!selected) return;
    deleteProject.reset();
    setDeleteTarget(selected);
  };
  return (
    <div className="project-selector-control">
      <label className="project-selector">
        <span className="project-selector-label">{label || text("当前项目", "Current project")}</span>
        <select value={selected?.project_id || ""} disabled={!projects.data?.items.length} onChange={(event) => selectProject(event.target.value)}>
          {projects.data?.items.map((project) => <option key={project.project_id} value={project.project_id}>{project.slug}</option>)}
        </select>
      </label>
      <button className="button button-danger project-delete" title={text("永久删除当前项目", "Permanently delete the current project")} type="button" disabled={!selected || deleteProject.isPending} onClick={confirmDelete}>
        {deleteProject.isPending ? text("删除中…", "Deleting…") : text("删除项目", "Delete project")}
      </button>
      {deleteProject.error ? <span className="message message-error">{deleteProject.error.message}</span> : null}
      <DeleteProjectDialog
        project={deleteTarget}
        deleting={deleteProject.isPending}
        error={deleteProject.error?.message}
        onCancel={() => {
          if (!deleteProject.isPending) setDeleteTarget(null);
        }}
        onConfirm={(project) => deleteProject.mutate(project)}
      />
    </div>
  );
}
