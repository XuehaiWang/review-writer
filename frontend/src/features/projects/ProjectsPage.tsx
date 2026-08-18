import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { Link, useNavigate } from "react-router-dom";

import { apiRequest, jsonBody } from "../../api/client";
import { modelCatalogQuery, projectsQuery, queryKeys } from "../../api/queries";
import type { Project } from "../../api/types";
import { DeleteProjectDialog } from "../../components/DeleteProjectDialog";
import { ErrorState } from "../../components/ErrorState";
import { useUiText } from "../../i18n/useUiText";

type ProjectFields = {
  slug: string;
  topic: string;
  taxonomy_profile: string;
  model_tier: "sol" | "terra" | "luna";
};

function ProjectCard({ project, deleting, modelUpdating, onModelChange, onDelete }: { project: Project; deleting: boolean; modelUpdating: boolean; onModelChange: (project: Project, modelTier: Project["model_tier"]) => void; onDelete: (project: Project) => void }) {
  const { text } = useUiText();
  return (
    <article className="project-card">
      <div className="project-card-head">
        <div>
          <span className="project-kicker">{project.current_stage || "discovery"}</span>
          <h3>{project.slug}</h3>
        </div>
        <span className="badge">{project.discovery_status || "pending"}</span>
      </div>
      <p>{project.topic || text("尚未填写研究主题", "No research topic yet")}</p>
      <div className="project-meta">
        <span>{project.completed_stages.length ? text(`已完成 ${project.completed_stages.length} 个阶段`, `${project.completed_stages.length} stages completed`) : text("尚未完成阶段", "No stages completed")}</span>
        <span>{project.taxonomy_profile}</span>
        <label>{text("模型", "Model")}<select value={project.model_tier} disabled={modelUpdating} onChange={(event) => onModelChange(project, event.target.value as Project["model_tier"])}><option value="sol">Sol</option><option value="terra">Terra</option><option value="luna">Luna</option></select></label>
        <Link className="button button-secondary" to={`/library?project=${encodeURIComponent(project.project_id)}`}>{text("进入工作流", "Open workflow")}</Link>
        <button className="button button-danger" type="button" disabled={deleting} onClick={() => onDelete(project)}>
          {deleting ? text("删除中…", "Deleting…") : text("删除项目", "Delete project")}
        </button>
      </div>
    </article>
  );
}

export function ProjectsPage() {
  const { text } = useUiText();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);
  const projects = useQuery(projectsQuery);
  const modelCatalog = useQuery(modelCatalogQuery);
  const { register, handleSubmit, reset, formState } = useForm<ProjectFields>({
    defaultValues: { slug: "", topic: "", taxonomy_profile: "chemistry_general", model_tier: "terra" },
  });
  const createProject = useMutation({
    mutationFn: (values: ProjectFields) =>
      apiRequest<Project>("/api/v1/projects", {
        method: "POST",
        ...jsonBody({
          slug: values.slug.trim(),
          topic: values.topic.trim(),
          taxonomy_profile: values.taxonomy_profile,
          model_tier: values.model_tier,
        }),
      }),
    onSuccess: async (created) => {
      reset();
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      navigate(`/library?project=${encodeURIComponent(created.project_id)}`);
    },
  });
  const deleteProject = useMutation({
    mutationFn: (project: Project) => apiRequest<void>(`/api/v1/projects/${encodeURIComponent(project.project_id)}`, { method: "DELETE" }),
    onSuccess: async () => {
      setDeleteTarget(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
    },
  });
  const updateModelTier = useMutation({
    mutationFn: ({ project, modelTier }: { project: Project; modelTier: Project["model_tier"] }) =>
      apiRequest<Project>(`/api/v1/projects/${encodeURIComponent(project.project_id)}/model-tier`, {
        method: "PATCH",
        ...jsonBody({ model_tier: modelTier }),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
    },
  });
  const confirmDelete = (project: Project) => {
    deleteProject.reset();
    setDeleteTarget(project);
  };

  return (
    <main className="workspace page-container">
      <div className="workspace-heading">
        <div>
          <p className="eyebrow">{text("托管工作台", "Hosted workspace")}</p>
          <h1>{text("科学综述项目", "Scientific review projects")}</h1>
          <p className="muted">{text("从文献库开始，在同一项目中完成检索、写作、图像处理和最终审计。", "Start from the library and complete discovery, writing, figure processing, and final audit in one project.")}</p>
        </div>
        <a className="button button-primary" href="#create-project">{text("新建项目", "New project")}</a>
      </div>

      <div className="content-grid portal-grid">
        <section>
          <div className="section-heading">
            <div><h2>{text("我的项目", "My projects")}</h2><p>{text("项目和所有产物只对当前账户可见。", "Projects and artifacts are visible only to the current account.")}</p></div>
            <button className="button button-quiet" type="button" disabled={projects.isFetching} onClick={() => projects.refetch()}>{text("刷新", "Refresh")}</button>
          </div>
          {projects.isPending ? <div className="empty-state">{text("正在加载项目…", "Loading projects…")}</div> : null}
          {projects.error ? <ErrorState error={projects.error} onRetry={() => projects.refetch()} /> : null}
          {projects.data && projects.data.items.length === 0 ? <div className="empty-state">{text("还没有项目。请在右侧创建第一个项目。", "No projects yet. Create the first project on the right.")}</div> : null}
          <div className="project-list">
            {projects.data?.items.map((project) => <ProjectCard key={project.project_id} project={project} deleting={deleteProject.isPending && deleteProject.variables?.project_id === project.project_id} modelUpdating={updateModelTier.isPending && updateModelTier.variables?.project.project_id === project.project_id} onModelChange={(item, modelTier) => updateModelTier.mutate({ project: item, modelTier })} onDelete={confirmDelete} />)}
          </div>
          {deleteProject.error ? <p className="message message-error" role="alert">{deleteProject.error.message}</p> : null}
        </section>

        <aside id="create-project" className="surface sticky-card">
          <span className="step-label">{text("创建项目", "Create project")}</span>
          <h2>{text("新建综述项目", "Create review project")}</h2>
          <form onSubmit={handleSubmit((values) => createProject.mutate(values))}>
            <label>
              {text("项目ID", "Project ID")}
              <input
                required
                maxLength={96}
                pattern="[a-z0-9][a-z0-9-]*"
                placeholder="copper-mechanochemistry"
                {...register("slug", { required: true })}
              />
              <small>{text("使用小写字母、数字和连字符。", "Use lowercase letters, numbers, and hyphens.")}</small>
            </label>
            <label>{text("研究主题", "Research topic")}<textarea rows={5} maxLength={10_000} {...register("topic")} /></label>
            <label>
              {text("分类配置", "Taxonomy profile")}
              <select {...register("taxonomy_profile")}>
                <option value="chemistry_general">{text("通用化学", "General chemistry")}</option>
              </select>
            </label>
            <label>
              {text("文本模型", "Text model")}
              <select {...register("model_tier")}>
                {(modelCatalog.data?.items || []).map((tier) => <option key={tier.id} value={tier.id}>{text(tier.label_zh, tier.label_en)}</option>)}
                {!modelCatalog.data ? <option value="terra">Terra</option> : null}
              </select>
              <small>{text("当前用于评估与重写；任务启动时锁定档位，进行中的任务不受后续切换影响。", "Currently used for evaluation and rewriting. The tier is fixed when a job starts, so later changes do not affect a running job.")}</small>
            </label>
            <button className="button button-primary button-block" type="submit" disabled={createProject.isPending || formState.isSubmitting}>
              {createProject.isPending ? text("正在创建…", "Creating…") : text("创建项目", "Create project")}
            </button>
            {createProject.error ? <p className="message message-error" role="alert">{createProject.error.message}</p> : null}
            {createProject.isSuccess ? <p className="message" role="status">{text("项目已创建。", "Project created.")}</p> : null}
          </form>
        </aside>
      </div>
      <DeleteProjectDialog
        project={deleteTarget}
        deleting={deleteProject.isPending}
        error={deleteProject.error?.message}
        onCancel={() => {
          if (!deleteProject.isPending) setDeleteTarget(null);
        }}
        onConfirm={(project) => deleteProject.mutate(project)}
      />
    </main>
  );
}
