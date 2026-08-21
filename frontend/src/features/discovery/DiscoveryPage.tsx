import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { ApiError, apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { queryKeys } from "../../api/queries";
import type { Job } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { jobIsActive, useJob } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { buildPaperDisplayLabels } from "../../utils/paperLabels";
import { DiscoveryJobProgress } from "./DiscoveryJobProgress";

type ProjectTagMap = Record<string, string[]>;

type ProjectTagEvidence = {
  keyword?: string;
  query_category?: string;
  matched_fields?: string[];
  score?: number;
  reason?: string;
};

type ProjectTagAssessment = {
  suggested_tags?: ProjectTagMap;
  unclassified_terms?: string[];
  relevance_score?: number;
  generated_by?: string;
  topic_fingerprint?: string;
  evidence?: ProjectTagEvidence[];
};

type DiscoveryRow = Record<string, unknown> & {
  paper_id?: string;
  candidate_id?: string;
  title?: string;
  authors?: string[];
  year?: number | string;
  journal?: string;
  score?: number;
  role?: string;
  keep?: boolean;
  selected_for_matrix?: boolean;
  source?: string;
  landing_url?: string;
  base_tags?: Record<string, string>;
  base_tags_verified?: boolean;
  project_tag_assessment?: ProjectTagAssessment;
  confirmed_project_tags?: ProjectTagMap;
  tag_review_status?: "pending" | "confirmed";
};

type DiscoveryGroup = {
  keyword: string;
  category?: string;
  keep?: boolean;
  local_results?: DiscoveryRow[];
  web_results?: DiscoveryRow[];
};

type DiscoveryPayload = {
  project_id: string;
  artifact_id: string;
  revision: number;
  status?: string;
  has_published_matrix?: boolean;
  topic: string;
  keywords?: string;
  query_plan_source?: string;
  query_plan?: {
    planner?: string;
    planner_notice?: string;
    planner_notice_code?: string;
    group_by?: string[];
  };
  results: DiscoveryGroup[];
  statistics?: {
    candidate_count?: number;
    keyword_hit_count?: number;
    selected_count?: number;
    keyword_group_count?: number;
    external_candidate_count?: number;
    category_count?: number;
    unclassified_keyword_group_count?: number;
    tag_reviewed_candidate_count?: number;
    tag_reviewed_selected_count?: number;
  };
};

const CATEGORY_ORDER = [
  "product",
  "substrate",
  "catalyst_or_method",
  "organometallic_partner",
  "ligand_or_chiral_source",
  "leaving_group",
  "reaction_type",
  "document_scope",
  "unclassified",
];

function publicPlannerNotice(
  plan: DiscoveryPayload["query_plan"],
  text: (zh: string, en: string) => string,
) {
  const raw = String(plan?.planner_notice || "");
  const insufficient = plan?.planner_notice_code === "insufficient_credit"
    || /INSUFFICIENT_CREDIT|HTTP\s*402|余额不足/i.test(raw);
  if (insufficient) {
    return text(
      "余额不足，智能查询规划未运行。本次检索已自动使用确定性查询规划；请在“API 设置”中查看余额，或联系管理员添加额度。",
      "Your balance is insufficient for intelligent query planning. Deterministic planning was used automatically; review your balance in API Settings or contact an administrator for credit.",
    );
  }
  return text(
    "智能查询规划暂不可用，本次检索已自动使用确定性查询规划。",
    "Intelligent query planning was temporarily unavailable, so deterministic planning was used automatically.",
  );
}

const CATEGORY_LABELS: Record<string, [string, string]> = {
  product: ["产物", "Product"],
  substrate: ["底物", "Substrate"],
  catalyst_or_method: ["催化剂与方法", "Catalyst / method"],
  organometallic_partner: ["有机金属试剂", "Organometallic partner"],
  ligand_or_chiral_source: ["配体与手性来源", "Ligand / chiral source"],
  leaving_group: ["离去基团", "Leaving group"],
  reaction_type: ["反应类型", "Reaction type"],
  document_scope: ["文献范围", "Document scope"],
  unclassified: ["待分类主题", "Unclassified theme"],
};

type TextSelector = (zh: string, en: string) => string;

function categoryLabel(category: string, text: TextSelector): string {
  const labels = CATEGORY_LABELS[category] || [category, category];
  return text(labels[0], labels[1]);
}

function normalizedTagMap(value: ProjectTagMap | undefined): ProjectTagMap {
  return Object.fromEntries(
    Object.entries(value || {})
      .filter(([category, tags]) => CATEGORY_ORDER.includes(category) && category !== "unclassified" && Array.isArray(tags))
      .map(([category, tags]) => [category, tags.map((tag) => String(tag).trim()).filter(Boolean)]),
  );
}

function selectedForMatrix(row: DiscoveryRow): boolean {
  return row.selected_for_matrix === true && row.role !== "excluded";
}

function PaperDetail({ row, kind, displayLabel }: { row: DiscoveryRow | null; kind: "local" | "web"; displayLabel?: string }) {
  const { text } = useUiText();
  const paperId = kind === "local" ? String(row?.paper_id || "") : "";
  const metadata = useQuery({
    queryKey: queryKeys.libraryMetadata(paperId),
    queryFn: () => apiRequest<Record<string, unknown>>(`/api/v1/library/papers/${encodeURIComponent(paperId)}/metadata`),
    enabled: Boolean(paperId),
  });
  const markdown = useQuery({
    queryKey: queryKeys.libraryMarkdown(paperId),
    queryFn: () => apiRequest<string>(`/api/v1/library/papers/${encodeURIComponent(paperId)}/markdown`),
    enabled: Boolean(paperId),
  });
  if (!row) return <div className="empty-state">{text("点击论文查看Metadata、Markdown和PDF。", "Select a paper to view its metadata, Markdown, and PDF.")}</div>;
  if (kind === "web") {
    return (
      <div className="external-detail">
        <span className="step-label">{text("外部结果", "External result")}</span>
        <h2>{row.title || text("无标题", "Untitled")}</h2>
        <p>{[row.year, row.journal, row.source].filter(Boolean).join(" · ")}</p>
        {row.landing_url ? <a className="button button-secondary" href={row.landing_url} target="_blank" rel="noreferrer">{text("打开文章页面", "Open article page")}</a> : null}
        <pre>{JSON.stringify(row, null, 2)}</pre>
      </div>
    );
  }
  const assessment = row.project_tag_assessment || {};
  const suggestedTags = normalizedTagMap(assessment.suggested_tags);

  return (
    <div className="discovery-detail">
      <div className="detail-summary"><span className="step-label" title={paperId}>{displayLabel || paperId}</span><h2>{row.title}</h2><p>{row.authors?.join(", ")}</p></div>
      <section className="project-tag-review">
        <div className="project-tag-review-head">
          <div><span className="step-label">{text("项目 Tag 自动评估", "Automatic project Tag assessment")}</span><h3>{text("已验证基础 Tag 与当前项目建议", "Verified base Tags and project suggestions")}</h3></div>
        </div>
        <p className="muted">{row.base_tags_verified
          ? text("系统仅使用经过人工确认的 Library 基础 Tag；当前项目建议在论文进入 Matrix 时自动应用。", "Only human-verified Library base Tags are used. Current project suggestions are applied automatically when the paper enters the matrix.")
          : text("这篇论文的历史 Library Tag 未经人工确认，已从召回和 Matrix 中忽略；当前项目只使用带匹配证据的建议 Tag。", "Unverified historical Library Tags are ignored for retrieval and Matrix. This project uses only suggested Tags backed by matching evidence.")}</p>
        <div className="project-tag-grid">
          {CATEGORY_ORDER.filter((category) => category !== "unclassified").map((category) => {
            const baseValue = String(row.base_tags?.[category] || "").trim();
            const suggestions = suggestedTags[category] || [];
            return <div className="project-tag-row" key={category}><div className="project-tag-label"><strong>{categoryLabel(category, text)}</strong><small>{category}</small></div><div className="base-tag-value"><span>{text("基础", "Base")}</span><p>{baseValue && baseValue !== "not specified" ? baseValue : text("未指定", "Not specified")}</p></div><div className="suggested-tag-value"><span>{text("项目建议（自动应用）", "Suggested (applied automatically)")}</span><p>{suggestions.length ? suggestions.join(" · ") : "—"}</p></div></div>;
          })}
        </div>
        {assessment.unclassified_terms?.length ? <div className="unclassified-tag-note"><strong>{text("未归类检索词：", "Unclassified query terms:")}</strong> {assessment.unclassified_terms.join(" · ")}</div> : null}
        {assessment.evidence?.length ? <details className="tag-evidence"><summary>{text(`查看 ${assessment.evidence.length} 条匹配证据`, `View ${assessment.evidence.length} matching signals`)}</summary><div>{assessment.evidence.map((item, index) => <article key={`${item.keyword || "evidence"}-${index}`}><strong>{item.keyword || text("未命名关键词", "Unnamed keyword")}</strong><span>{[item.query_category, ...(item.matched_fields || [])].filter(Boolean).join(" → ")}</span><small>{typeof item.score === "number" ? `${Math.round(item.score * 100)}% · ` : ""}{item.reason}</small></article>)}</div></details> : null}
      </section>
      <details open><summary>{text("元数据", "Metadata")}</summary><pre>{metadata.isPending ? text("正在加载…", "Loading…") : JSON.stringify(metadata.data, null, 2)}</pre></details>
      <details><summary>Markdown</summary><pre className="markdown-preview compact-preview">{markdown.isPending ? text("正在加载…", "Loading…") : markdown.data}</pre></details>
      <details><summary>PDF</summary><iframe className="pdf-frame" title={`${paperId} PDF`} src={`/api/v1/library/papers/${encodeURIComponent(paperId)}/pdf`} /></details>
    </div>
  );
}

export function DiscoveryPage() {
  const { text } = useUiText();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { selected: project, projects } = useSelectedProject();
  const [topic, setTopic] = useState("");
  const [keywords, setKeywords] = useState("");
  const [webSearch, setWebSearch] = useState(false);
  const [jobId, setJobId] = useState("");
  const [selectedKeyword, setSelectedKeyword] = useState("");
  const [selectedPaper, setSelectedPaper] = useState<{ row: DiscoveryRow; kind: "local" | "web" } | null>(null);
  const discovery = useQuery({
    queryKey: ["discovery", project?.project_id || ""],
    queryFn: () => apiRequest<DiscoveryPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery`),
    enabled: Boolean(project),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 1,
  });
  const [groups, setGroups] = useState<DiscoveryGroup[]>([]);
  useEffect(() => {
    setTopic(project?.topic || "");
    setKeywords("");
    setWebSearch(false);
    setJobId("");
    setGroups([]);
    setSelectedKeyword("");
    setSelectedPaper(null);
  }, [project?.project_id, project?.topic]);
  useEffect(() => {
    const payload = discovery.data;
    if (!payload || payload.project_id !== project?.project_id) return;
    setGroups(structuredClone(payload.results || []));
    setTopic(payload.topic || project?.topic || "");
    setSelectedKeyword((current) => (payload.results || []).some((group) => group.keyword === current) ? current : payload.results?.[0]?.keyword || "");
  }, [discovery.data, project?.project_id, project?.topic]);
  const job = useJob(jobId);
  useEffect(() => {
    if (jobId && job.data?.status === "succeeded") {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["discovery", project?.project_id || ""] }),
        queryClient.invalidateQueries({ queryKey: queryKeys.projects }),
      ]);
    }
  }, [job.data?.status, jobId, project?.project_id, queryClient]);
  const run = useMutation({
    mutationFn: () => apiRequest<Job>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery/jobs`, {
      method: "POST",
      headers: { "Idempotency-Key": newIdempotencyKey() },
      ...jsonBody({ topic: topic.trim(), keywords: keywords.trim(), web_search: webSearch }),
    }),
    onSuccess: (submitted) => setJobId(submitted.id),
  });
  const save = useMutation({
    mutationFn: () => apiRequest<DiscoveryPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery`, {
      method: "PUT",
      ...jsonBody({ revision: discovery.data!.revision, results: groups }),
    }),
    onSuccess: async (saved) => {
      queryClient.setQueryData(["discovery", project!.project_id], saved);
      await discovery.refetch();
    },
  });
  const confirm = useMutation({
    mutationFn: async () => {
      const saved = await apiRequest<DiscoveryPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery`, {
        method: "PUT",
        ...jsonBody({ revision: discovery.data!.revision, results: groups }),
      });
      return apiRequest<Record<string, unknown>>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery/confirm`, {
        method: "POST",
        ...jsonBody({ revision: saved.revision }),
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      navigate(`/planning?tab=matrix&project=${encodeURIComponent(project!.project_id)}`);
    },
  });

  const currentGroup = groups.find((group) => group.keyword === selectedKeyword) || groups[0];
  const rows = useMemo(() => [
    ...(currentGroup?.local_results || []).map((row) => ({ row, kind: "local" as const })),
    ...(currentGroup?.web_results || []).map((row) => ({ row, kind: "web" as const })),
  ], [currentGroup]);
  const activeGroups = groups.filter((group) => group.keep !== false);
  const selectedCount = new Set(activeGroups.flatMap((group) => group.local_results || []).filter(selectedForMatrix).map((row) => row.paper_id)).size;
  const uniqueCandidateCount = new Set(activeGroups.flatMap((group) => group.local_results || []).map((row) => row.paper_id).filter(Boolean)).size;
  const keywordHitCount = activeGroups.reduce((sum, group) => sum + (group.local_results?.length || 0), 0);
  const paperLabels = useMemo(() => {
    const ranked = new Map<string, { paper_id: string; score: number; order: number }>();
    const remaining: Array<{ paper_id: string }> = [];
    const seenRemaining = new Set<string>();
    let order = 0;
    for (const group of groups) {
      if (group.keep === false) continue;
      for (const row of group.local_results || []) {
        const paperId = String(row.paper_id || "").trim();
        if (!paperId) continue;
        if (!seenRemaining.has(paperId)) {
          seenRemaining.add(paperId);
          remaining.push({ paper_id: paperId });
        }
        if (selectedForMatrix(row)) {
          const score = Number(row.score || row.raw_score || 0);
          const previous = ranked.get(paperId);
          if (!previous || score > previous.score) {
            ranked.set(paperId, { paper_id: paperId, score, order });
          }
        }
        order += 1;
      }
    }
    const selected = [...ranked.values()].sort((left, right) => right.score - left.score || left.order - right.order);
    const selectedIds = new Set(selected.map((row) => row.paper_id));
    return buildPaperDisplayLabels([
      ...selected,
      ...remaining.filter((row) => !selectedIds.has(row.paper_id)),
    ]);
  }, [groups]);
  const groupedKeywords = useMemo(() => {
    const grouped = new Map<string, DiscoveryGroup[]>();
    for (const group of groups) {
      const category = String(group.category || "unclassified");
      grouped.set(category, [...(grouped.get(category) || []), group]);
    }
    return [...grouped.entries()].sort(([left], [right]) => {
      const leftIndex = CATEGORY_ORDER.indexOf(left);
      const rightIndex = CATEGORY_ORDER.indexOf(right);
      return (leftIndex < 0 ? CATEGORY_ORDER.length : leftIndex) - (rightIndex < 0 ? CATEGORY_ORDER.length : rightIndex);
    });
  }, [groups]);

  function updateRow(target: DiscoveryRow, update: Partial<DiscoveryRow>) {
    const targetPaperId = String(target.paper_id || "");
    const matches = (row: DiscoveryRow) => targetPaperId ? String(row.paper_id || "") === targetPaperId : row === target;
    setSelectedPaper((current) => current && matches(current.row) ? { ...current, row: { ...current.row, ...update } } : current);
    setGroups((current) => current.map((group) => ({
      ...group,
      local_results: group.local_results?.map((row) => matches(row) ? { ...row, ...update } : row),
      web_results: group.web_results?.map((row) => row === target ? { ...row, ...update } : row),
    })));
  }

  const noArtifact = discovery.error instanceof ApiError && discovery.error.status === 404;
  const insufficientCreditStop = Boolean(
    jobId
    && job.data?.status === "failed"
    && job.data.error_code === "INSUFFICIENT_CREDIT",
  );
  return (
    <main className="workspace page-container workspace-page">
      <div className="workspace-heading">
        <div><p className="eyebrow">{text("阶段 2 · 项目检索", "Stage 2 · Project discovery")}</p><h1>{text("文献检索与选择", "Literature discovery and selection")}</h1><p className="muted">{text("检索候选、排除不相关结果，并明确选择进入Matrix的本地文献。", "Find candidates, exclude irrelevant results, and explicitly select local papers for the matrix.")}</p></div>
        <ProjectSelector />
      </div>
      {!projects.data?.items.length ? <div className="empty-state">{text("请先在首页创建项目。", "Create a project on the home page first.")}</div> : null}
      {project ? (
        <section className="surface discovery-run-card">
          <div className="run-form">
            <label>{text("综述主题", "Review topic")}<input value={topic} onChange={(event) => setTopic(event.target.value)} /></label>
            <label>{text("补充关键词", "Additional keywords")}<textarea rows={2} value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder={text("每行或逗号分隔", "Separate with lines or commas")} /></label>
            <label className="check-label"><input type="checkbox" checked={webSearch} onChange={(event) => setWebSearch(event.target.checked)} />{text("同时使用联网补充检索", "Also search online sources")}</label>
            <button className="button button-primary" type="button" disabled={topic.trim().length < 3 || run.isPending || jobIsActive(job.data?.status)} onClick={() => run.mutate()}>{discovery.data ? text("重新检索", "Run search again") : text("开始检索", "Start search")}</button>
          </div>
          {discovery.data?.has_published_matrix ? <p className="message message-info">{text("重新检索只生成新的待确认结果，不会删除或隐藏阶段 3–7 的现有内容。确认采用后，只有论文集合或主题确实变化时，依赖阶段才会标记为过期。", "A new search creates reviewable results without deleting or hiding existing Stage 3–7 work. After confirmation, dependent stages are marked stale only when the paper set or topic actually changes.")}</p> : null}
          {(run.isPending || jobId) ? <DiscoveryJobProgress job={job.data} submitting={run.isPending && !jobId} /> : null}
          {insufficientCreditStop && discovery.data ? <p className="message message-info">{text("下方保留的是上一次成功检索的结果；本次余额不足的检索未执行，也没有覆盖这些结果。", "The results below are from the last successful search. The current search was not run because of insufficient credit and did not overwrite them.")}</p> : null}
          {!insufficientCreditStop && discovery.data?.query_plan?.planner_notice ? <p className="message message-warning">{publicPlannerNotice(discovery.data.query_plan, text)}</p> : null}
          {!insufficientCreditStop && discovery.data?.query_plan_source === "dashboard_llm" && !discovery.data?.query_plan?.planner_notice ? <p className="message message-info">{text("已使用当前所选文本模型完成查询规划。", "The current selected text model produced the query plan.")}</p> : null}
          {run.error ? <p className="message message-error">{run.error.message}</p> : null}
        </section>
      ) : null}
      {discovery.isPending && !noArtifact ? <div className="empty-state">{text("正在加载检索结果…", "Loading discovery results…")}</div> : null}
      {discovery.error && !noArtifact ? <ErrorState error={discovery.error} onRetry={() => discovery.refetch()} /> : null}
      {noArtifact ? <div className="empty-state">{text("当前项目还没有检索结果，请填写主题并开始检索。", "This project has no discovery results yet. Enter a topic and start searching.")}</div> : null}
      {discovery.data ? (
        <>
          {discovery.data.status === "review" && discovery.data.has_published_matrix ? <p className="message message-warning discovery-candidate-notice">{text("当前显示的是尚未采用的新检索结果；旧 Matrix、章节、图像、初稿和终稿仍被完整保留。请审核论文选择后再确认采用。", "These search results have not been adopted yet. The previous matrix, sections, figures, draft, and final output remain intact. Review the selection before adopting it.")}</p> : null}
          <div className="discovery-stats"><span>{text("主题/关键词组", "Theme / keyword groups")} {groups.length}</span><span>{text("去重候选论文", "Unique candidate papers")} {uniqueCandidateCount}</span><span>{text("关键词命中次数", "Keyword hits")} {keywordHitCount}</span><span className="selected">{text("进入Matrix", "Selected for matrix")} {selectedCount}</span></div>
          <div className="discovery-grid">
            <section className="pane keyword-pane">
              <div className="pane-head"><div><span className="step-label">{text("主题与分类", "Themes and categories")}</span><h2>{text("检索主题/关键词组", "Search themes / keywords")}</h2></div></div>
              <div className="keyword-list">{groupedKeywords.map(([category, categoryGroups]) => <div className="keyword-category" key={category}><div className="keyword-category-title"><span>{categoryLabel(category, text)}</span><small>{categoryGroups.length}</small></div>{categoryGroups.map((group) => <button key={group.keyword} className={`${group.keyword === currentGroup?.keyword ? "active" : ""} ${group.keep === false ? "deleted" : ""}`} type="button" onClick={() => { setSelectedKeyword(group.keyword); setSelectedPaper(null); }}><strong>{group.keyword}</strong><small>{group.local_results?.length || 0} {text("篇本地命中", "local hits")} · {group.web_results?.length || 0} {text("篇外部结果", "external results")}</small></button>)}</div>)}</div>
              {currentGroup ? <button className="button button-quiet danger keyword-delete" type="button" onClick={() => setGroups((current) => current.map((group) => group.keyword === currentGroup.keyword ? { ...group, keep: group.keep === false } : group))}>{currentGroup.keep === false ? text("恢复关键词组", "Restore keyword group") : text("排除关键词组", "Exclude keyword group")}</button> : null}
            </section>
            <section className="pane result-pane">
              <div className="pane-head"><div><span className="step-label">{text("候选", "Candidates")}</span><h2>{currentGroup?.keyword || text("结果", "Results")}</h2></div></div>
              <div className="result-list">{rows.map(({ row, kind }, index) => {
                const active = selectedPaper?.row === row;
                const included = selectedForMatrix(row);
                return <article key={`${kind}-${row.paper_id || row.candidate_id || index}`} className={active ? "result-card active" : "result-card"} onClick={() => setSelectedPaper({ row, kind })}><div><span className="result-kind" title={kind === "local" ? String(row.paper_id || "") : undefined}>{kind === "local" ? paperLabels.get(String(row.paper_id || "")) || text("本地", "Local") : text("外部", "External")}</span><h3>{row.title || text("无标题", "Untitled")}</h3><p>{[row.year, row.journal, row.source].filter(Boolean).join(" · ")}</p></div><div className="result-actions"><button type="button" className={included ? "button button-primary" : "button button-secondary"} onClick={(event) => { event.stopPropagation(); updateRow(row, { selected_for_matrix: !included, ...(included ? {} : row.role === "excluded" ? { role: "uncertain" } : {}) }); }}>{included ? text("已加入Matrix", "Added to matrix") : text("加入Matrix", "Add to matrix")}</button>{kind === "local" ? <select value={row.role || "uncertain"} onClick={(event) => event.stopPropagation()} onChange={(event) => updateRow(row, { role: event.target.value, ...(event.target.value === "excluded" ? { selected_for_matrix: false } : {}) })}>{["core_candidate", "supporting_candidate", "background", "uncertain", "excluded"].map((role) => <option key={role}>{role}</option>)}</select> : null}</div></article>;
              })}</div>
            </section>
            <section className="pane discovery-detail-pane"><PaperDetail row={selectedPaper?.row || null} kind={selectedPaper?.kind || "local"} displayLabel={selectedPaper?.kind === "local" ? paperLabels.get(String(selectedPaper.row.paper_id || "")) : undefined} /></section>
          </div>
          <div className="stage-action-bar"><div><strong>{text("论文选择", "Paper selection")}</strong><p>{text("选择需要进入 Matrix 的论文；项目 Tag 评估会自动同步。确认采用后，系统会先比较新旧输入，再决定是否让后续阶段过期。", "Choose the papers for the matrix; project Tag assessments synchronize automatically. On adoption, the system compares old and new inputs before deciding whether later stages are stale.")}</p></div><button className="button button-secondary" type="button" disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? text("保存中…", "Saving…") : text("保存选择", "Save selection")}</button><button className="button button-primary" type="button" disabled={!selectedCount || confirm.isPending} onClick={() => confirm.mutate()}>{confirm.isPending ? text("同步中…", "Syncing…") : text(`确认采用并进入 Matrix（${selectedCount}篇）`, `Adopt and enter matrix (${selectedCount})`)}</button>{(save.error || confirm.error) ? <span className="message message-error">{(save.error || confirm.error)?.message}</span> : null}</div>
        </>
      ) : null}
    </main>
  );
}
