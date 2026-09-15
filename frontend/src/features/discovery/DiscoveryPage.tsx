import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { ApiError, apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { ACTIVE_JOB_POLL_INTERVAL_MS } from "../../api/polling";
import { queryKeys } from "../../api/queries";
import type { Job } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { jobIsActive, useJob } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { DiscoveryJobProgress } from "./DiscoveryJobProgress";
import {
  CANDIDATE_FILTERS,
  buildDiscoveryPaperLabels,
  candidateMatchesFilter,
  externalActionLabel,
  externalCandidateId,
  groupLabel,
  localCandidateId,
  orderedQueryGroups,
  publicPlannerNotice,
  queryGroupSourceLabel,
  retrievalChannelLabel,
  selectedForMatrix,
  unifyDiscoveryRows,
  type CandidateFilter,
  type DiscoveryGroup,
  type DiscoveryJobState,
  type DiscoveryPayload,
  type DiscoveryRow,
} from "./model/discoveryModel";
import { buildMatrixRecommendation } from "./model/matrixRecommendation";

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
        <div className="screening-summary">
          <span>{text("推荐状态", "Recommendation")}：{row.recommendation_status || "review"}</span>
          <span>{text("获取状态", "Access")}：{row.access_status || "access_unknown"}</span>
          <span>{text("召回通道", "Retrieval channels")}：{row.retrieval_channels?.join(" · ") || text("外部题录检索", "External metadata search")}</span>
        </div>
        <p className="message message-info">{text("这里的标题和摘要只用于筛选论文；正式写作会在论文进入 Matrix 后重新建立问题级科学事实和证据包。", "Title and abstract are used only for paper screening. Question-level scientific facts and evidence packages are rebuilt after the paper enters the matrix.")}</p>
        {row.landing_url ? <a className="button button-secondary" href={row.landing_url} target="_blank" rel="noreferrer">{text("打开文章页面", "Open article page")}</a> : null}
        <details><summary>{text("查看完整筛选记录", "View full screening record")}</summary><pre>{JSON.stringify(row, null, 2)}</pre></details>
      </div>
    );
  }
  return (
    <div className="discovery-detail">
      <div className="detail-summary"><span className="step-label" title={paperId}>{displayLabel || paperId}</span><h2>{row.title}</h2><p>{row.authors?.join(", ")}</p></div>
      <section className="screening-detail">
        <div className="screening-summary"><span>{text("推荐状态", "Recommendation")}：{row.recommendation_status || "review"}</span><span>{text("召回通道", "Retrieval channels")}：{row.retrieval_channels?.join(" · ") || text("题录与规则", "Metadata and rules")}</span><span>{text("正式分类", "Formal classification")}：{text("进入 Matrix 后生成", "Generated after Matrix entry")}</span></div>
        {row.screening_chunks?.length ? <details><summary>{text(`查看 ${row.screening_chunks.length} 条筛选片段`, `View ${row.screening_chunks.length} screening excerpts`)}</summary>{row.screening_chunks.map((chunk) => <article key={String(chunk.chunk_id)}><strong>{[chunk.channel, chunk.page_start ? `p.${chunk.page_start}` : "", ...(chunk.section_path || [])].filter(Boolean).join(" · ")}</strong><p>{chunk.excerpt}</p></article>)}</details> : null}
        <p className="message message-info">{text("第二阶段只负责召回、排序和选择。命中片段与分区查询只作为检索线索；论文确认进入 Matrix 后，系统才会依据可定位的科学事实完成正式分类。", "Stage 02 only retrieves, ranks, and selects papers. Hit excerpts and partition queries remain retrieval hints; formal classification is produced from source-addressable scientific facts after Matrix entry.")}</p>
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
  const [selectedJob, setSelectedJob] = useState({ projectId: "", jobId: "" });
  const [candidateFilter, setCandidateFilter] = useState<CandidateFilter>("all");
  const [selectedPaper, setSelectedPaper] = useState<{ row: DiscoveryRow; kind: "local" | "web" } | null>(null);
  const [selectionFeedback, setSelectionFeedback] = useState("");
  const [externalDownloadJobId, setExternalDownloadJobId] = useState("");
  const [refreshWatch, setRefreshWatch] = useState<{ revision: number; startedAt: number } | null>(null);
  const [coverageNoticeDismissed, setCoverageNoticeDismissed] = useState(false);
  const discovery = useQuery({
    queryKey: ["discovery", project?.project_id || ""],
    queryFn: () => apiRequest<DiscoveryPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery`),
    enabled: Boolean(project),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 1,
  });
  const discoveryJobState = useQuery({
    queryKey: ["discovery-job-state", project?.project_id || ""],
    queryFn: () => apiRequest<DiscoveryJobState>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery/jobs/current`),
    enabled: Boolean(project),
    refetchInterval: (query) => query.state.data?.active_job ? ACTIVE_JOB_POLL_INTERVAL_MS : false,
    refetchIntervalInBackground: true,
  });
  const [groups, setGroups] = useState<DiscoveryGroup[]>([]);
  useEffect(() => {
    setTopic(project?.topic || "");
    setKeywords("");
    setWebSearch(false);
    setSelectedJob({ projectId: "", jobId: "" });
    setGroups([]);
    setCandidateFilter("all");
    setSelectedPaper(null);
    setSelectionFeedback("");
    setExternalDownloadJobId("");
    setRefreshWatch(null);
    setCoverageNoticeDismissed(false);
  }, [project?.project_id, project?.topic]);
  useEffect(() => {
    const payload = discovery.data;
    if (!payload || payload.project_id !== project?.project_id) return;
    setGroups(structuredClone(payload.results || []));
    setTopic(payload.topic || project?.topic || "");
  }, [discovery.data, project?.project_id, project?.topic]);
  const localJobId = selectedJob.projectId === project?.project_id ? selectedJob.jobId : "";
  const serverActiveJobId = discoveryJobState.data?.active_job?.id || "";
  const currentJobId = serverActiveJobId || localJobId;
  const job = useJob(currentJobId);
  const externalDownloadJob = useJob(externalDownloadJobId);
  useEffect(() => {
    if (currentJobId && job.data && !jobIsActive(job.data.status)) {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["discovery", project?.project_id || ""] }),
        queryClient.invalidateQueries({ queryKey: ["discovery-job-state", project?.project_id || ""] }),
        queryClient.invalidateQueries({ queryKey: queryKeys.projects }),
      ]);
    }
  }, [currentJobId, job.data, project?.project_id, queryClient]);
  const run = useMutation({
    mutationFn: (options?: { webSearch?: boolean }) => apiRequest<Job>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery/jobs`, {
      method: "POST",
      headers: { "Idempotency-Key": newIdempotencyKey() },
      ...jsonBody({ topic: topic.trim(), keywords: keywords.trim(), web_search: options?.webSearch ?? webSearch }),
    }),
    onSuccess: (submitted) => {
      setSelectedJob({ projectId: project!.project_id, jobId: submitted.id });
      void queryClient.invalidateQueries({ queryKey: ["discovery-job-state", project!.project_id] });
    },
  });
  const confirm = useMutation({
    mutationFn: async () => {
      const saved = await apiRequest<DiscoveryPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery`, {
        method: "PUT",
        ...jsonBody({ revision: discovery.data!.revision, results: groups, coverage_decision: discovery.data!.coverage_confirmation_required ? undefined : discovery.data!.coverage_mode === "local_bounded" ? "keep_local" : undefined }),
      });
      return apiRequest<Record<string, unknown>>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery/confirm`, {
        method: "POST",
        ...jsonBody({ revision: saved.revision }),
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      await queryClient.invalidateQueries({ queryKey: ["planning", project!.project_id] });
      navigate(`/planning?tab=matrix&project=${encodeURIComponent(project!.project_id)}`);
    },
  });
  const keepLocalCoverage = useMutation({
    mutationFn: () => apiRequest<DiscoveryPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/discovery`, {
      method: "PUT",
      ...jsonBody({ revision: discovery.data!.revision, results: groups, coverage_decision: "keep_local" }),
    }),
    onSuccess: async () => {
      setCoverageNoticeDismissed(true);
      await queryClient.invalidateQueries({ queryKey: ["discovery", project?.project_id || ""] });
    },
  });
  const downloadExternal = useMutation({
    mutationFn: (row: DiscoveryRow) => apiRequest<Job>(`/api/v1/library/download-jobs?project_id=${encodeURIComponent(project!.project_id)}`, {
      method: "POST",
      headers: { "Idempotency-Key": newIdempotencyKey() },
      ...jsonBody({ candidates: [{ ...row, selected_for_matrix: false }], discovery_revision: discovery.data!.revision }),
    }),
    onSuccess: (submitted) => {
      setExternalDownloadJobId(submitted.id);
      setRefreshWatch({ revision: discovery.data!.revision, startedAt: Date.now() });
      setSelectionFeedback(text("已提交下载与解析；全文和语义索引完成后，本页会自动载入新的待确认版本。", "Download and parsing were queued. This page will load a new reviewable revision after full-text and semantic indexing."));
    },
  });
  useEffect(() => {
    if (!refreshWatch || !externalDownloadJobId) return;
    const status = externalDownloadJob.data?.status;
    if (status === "failed" || status === "cancelled" || status === "interrupted") {
      setSelectionFeedback(externalDownloadJob.data?.error_message || text("外部论文下载或解析失败。", "External paper download or parsing failed."));
      setRefreshWatch(null);
      return;
    }
    if (status === "succeeded" && Number(externalDownloadJob.data?.result?.failed_count || 0) > 0) {
      setSelectionFeedback(text("没有找到可合法自动下载的开放获取 PDF；可打开来源页面使用机构权限下载后，再到文献库导入 PDF。", "No lawfully downloadable open-access PDF was found. Open the source with institutional access, then import the downloaded PDF in Library."));
      setRefreshWatch(null);
      return;
    }
    if (Number(discovery.data?.revision || 0) > refreshWatch.revision) {
      setSelectionFeedback(text("论文已完成解析与索引，并转换为可加入 Matrix 的本地候选。", "The paper was parsed and indexed and is now a local candidate eligible for the matrix."));
      setRefreshWatch(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.library("") });
      return;
    }
    if (Date.now() - refreshWatch.startedAt > 3 * 60 * 1000) {
      setSelectionFeedback(text("下载任务已结束，但索引刷新仍在后台排队；稍后刷新页面即可查看。", "The download finished, but indexing is still queued. Refresh the page shortly to view the update."));
      setRefreshWatch(null);
      return;
    }
    if (status !== "succeeded") return;
    const timer = window.setTimeout(() => void discovery.refetch(), ACTIVE_JOB_POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [discovery, discovery.data?.revision, externalDownloadJob.data, externalDownloadJobId, queryClient, refreshWatch, text]);

  const activeGroups = groups.filter((group) => group.keep !== false);
  const recommendation = useMemo(() => buildMatrixRecommendation(groups), [groups]);
  const selectedCount = new Set(activeGroups.flatMap((group) => group.local_results || []).filter(selectedForMatrix).map((row) => row.paper_id)).size;
  const hasAnySelection = groups.some((group) => [
    ...(group.local_results || []),
    ...(group.web_results || []),
  ].some((row) => row.selected_for_matrix === true));
  const uniqueCandidateCount = new Set(activeGroups.flatMap((group) => group.local_results || []).map((row) => row.paper_id).filter(Boolean)).size;
  const keywordHitCount = activeGroups.reduce((sum, group) => sum + (group.local_results?.length || 0), 0);
  const unifiedRows = useMemo(() => unifyDiscoveryRows(groups), [groups]);
  const filterMatches = (entry: (typeof unifiedRows)[number], filter: CandidateFilter): boolean => (
    candidateMatchesFilter(entry, filter, recommendation.recommendedIds, recommendation.reviewIds)
  );
  const rows = unifiedRows.filter((entry) => filterMatches(entry, candidateFilter));
  const candidateFilterCounts = Object.fromEntries(
    CANDIDATE_FILTERS.map((filter) => [filter, unifiedRows.filter((entry) => filterMatches(entry, filter)).length]),
  ) as Record<CandidateFilter, number>;
  const partitionLabels = useMemo(() => new Map(
    (discovery.data?.query_plan?.semantic_queries || [])
      .filter((query) => query.kind === "topic_partition" && query.query_id)
      .map((query) => [String(query.query_id), String(query.label || query.partition_id || query.query_id)]),
  ), [discovery.data?.query_plan?.semantic_queries]);
  const coverage = discovery.data?.coverage_diagnostics;
  const searchRecord = discovery.data?.search_record;
  const showCoverageNotice = Boolean(
    coverage?.online_search_suggested
    && discovery.data?.coverage_decision !== "keep_local"
    && !coverageNoticeDismissed
    && !jobIsActive(job.data?.status),
  );
  const showNormalizationNotice = Boolean(
    discovery.data?.coverage_confirmation_required
    && discovery.data?.coverage_decision !== "keep_local"
    && !jobIsActive(job.data?.status)
  );
  const paperLabels = useMemo(() => buildDiscoveryPaperLabels(groups), [groups]);
  const queryGroups = useMemo(() => orderedQueryGroups(groups), [groups]);

  function updateRow(target: DiscoveryRow, update: Partial<DiscoveryRow>) {
    const targetPaperId = String(target.paper_id || "");
    const matches = (row: DiscoveryRow) => targetPaperId ? String(row.paper_id || "") === targetPaperId : row === target;
    setSelectedPaper((current) => current && matches(current.row) ? { ...current, row: { ...current.row, ...update } } : current);
    setGroups((current) => current.map((group) => ({
      ...group,
      local_results: group.local_results?.map((row) => matches(row) ? { ...row, ...update } : row),
      web_results: group.web_results?.map((row) => row === target ? { ...row, ...update } : row),
    })));
    setSelectionFeedback("");
  }

  function setSelectedPaperIds(selectedIds: Set<string>, mode: "replace" | "add") {
    setGroups((current) => current.map((group) => ({
      ...group,
      local_results: group.local_results?.map((row) => {
        const id = String(row.paper_id || "").trim();
        const selected = id ? selectedIds.has(id) : false;
        return { ...row, selected_for_matrix: mode === "add" ? selectedForMatrix(row) || selected : selected };
      }),
      web_results: mode === "replace"
        ? group.web_results?.map((row) => ({ ...row, selected_for_matrix: false }))
        : group.web_results,
    })));
    setSelectedPaper((current) => {
      if (!current) return current;
      const id = String(current.row.paper_id || "").trim();
      const selected = current.kind === "local" && Boolean(id) && selectedIds.has(id);
      return {
        ...current,
        row: {
          ...current.row,
          selected_for_matrix: mode === "add" ? selectedForMatrix(current.row) || selected : selected,
        },
      };
    });
  }

  function applyRecommendation() {
    setSelectedPaperIds(recommendation.recommendedIds, "replace");
    setSelectionFeedback(text(
      `已采用系统推荐：预选 ${recommendation.recommendedIds.size} 篇，另有 ${recommendation.reviewIds.size} 篇建议人工复核。`,
      `Recommendation applied: ${recommendation.recommendedIds.size} preselected and ${recommendation.reviewIds.size} left for review.`,
    ));
  }

  function selectVisibleCandidates() {
    const selectedIds = new Set(
      rows
        .filter((entry) => entry.kind === "local" && entry.row.role !== "excluded")
        .map((entry) => localCandidateId(entry.row))
        .filter(Boolean),
    );
    setSelectedPaperIds(selectedIds, "add");
    setSelectionFeedback(text(
      `已加入当前筛选结果中的 ${selectedIds.size} 篇非排除论文。`,
      `Added ${selectedIds.size} non-excluded papers from the current filtered results.`,
    ));
  }

  function clearSelection() {
    setSelectedPaperIds(new Set(), "replace");
    setSelectionFeedback(text("已清空当前选择。", "The current selection was cleared."));
  }

  const noArtifact = discovery.error instanceof ApiError && discovery.error.status === 404;
  const insufficientCreditStop = Boolean(
    currentJobId
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
          <div className="run-form discovery-run-primary">
            <label>{text("综述主题", "Review topic")}<input value={topic} onChange={(event) => setTopic(event.target.value)} /></label>
            <button className="button button-primary" type="button" disabled={topic.trim().length < 3 || run.isPending || discoveryJobState.isPending || Boolean(discoveryJobState.data?.active_job) || jobIsActive(job.data?.status)} onClick={() => run.mutate({})}>{discovery.data ? text("重新检索", "Run search again") : text("开始检索", "Start search")}</button>
          </div>
          <details className="advanced-panel discovery-search-advanced">
            <summary>{text("高级检索设置（可选）", "Advanced search settings (optional)")}</summary>
            <div className="advanced-panel-body discovery-search-advanced-body">
              <label>{text("补充关键词", "Additional keywords")}<textarea rows={2} value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder={text("每行或逗号分隔", "Separate with lines or commas")} /></label>
              <label className="check-label"><input type="checkbox" checked={webSearch} onChange={(event) => setWebSearch(event.target.checked)} />{text("同时使用联网补充检索", "Also search online sources")}</label>
            </div>
          </details>
          {discovery.data?.has_published_matrix ? <p className="message message-info">{text("重新检索只生成新的待确认结果，不会删除或隐藏阶段 3–7 的现有内容。确认采用后，只有论文集合或主题确实变化时，依赖阶段才会标记为过期。", "A new search creates reviewable results without deleting or hiding existing Stage 3–7 work. After confirmation, dependent stages are marked stale only when the paper set or topic actually changes.")}</p> : null}
          {(run.isPending || currentJobId) ? <DiscoveryJobProgress job={job.data || discoveryJobState.data?.active_job || undefined} submitting={run.isPending && !currentJobId} /> : null}
          {insufficientCreditStop && discovery.data ? <p className="message message-info">{text("下方保留的是上一次成功检索的结果；本次余额不足的检索未执行，也没有覆盖这些结果。", "The results below are from the last successful search. The current search was not run because of insufficient credit and did not overwrite them.")}</p> : null}
          {!insufficientCreditStop && discovery.data?.query_plan?.planner_notice ? <p className="message message-warning">{publicPlannerNotice(discovery.data.query_plan, text)}</p> : null}
          {!insufficientCreditStop && discovery.data?.query_plan_source === "dashboard_llm" && !discovery.data?.query_plan?.planner_notice ? <p className="message message-info">{text("已使用当前所选文本模型完成查询规划。", "The current selected text model produced the query plan.")}</p> : null}
          {run.error ? <p className="message message-error">{run.error.message}</p> : null}
        </section>
      ) : null}
      {discovery.isPending && !noArtifact ? <div className="empty-state">{text("正在加载检索结果…", "Loading discovery results…")}</div> : null}
      {discovery.error && !noArtifact ? <ErrorState error={discovery.error} onRetry={() => discovery.refetch()} /> : null}
      {noArtifact && !discoveryJobState.data?.active_job ? <div className="empty-state">{text("当前项目还没有检索结果，请填写主题并开始检索。", "This project has no discovery results yet. Enter a topic and start searching.")}</div> : null}
      {discovery.data ? (
        <>
          {discovery.data.status === "review" && discovery.data.has_published_matrix ? <p className="message message-warning discovery-candidate-notice">{text("当前显示的是尚未采用的新检索结果；旧 Matrix、章节、图像、初稿和终稿仍被完整保留。请审核论文选择后再确认采用。", "These search results have not been adopted yet. The previous matrix, sections, figures, draft, and final output remain intact. Review the selection before adopting it.")}</p> : null}
          {discovery.data.hybrid_retrieval?.semantic_status === "degraded" || discovery.data.hybrid_retrieval?.semantic_status === "unavailable" ? <div className="message message-warning"><strong>{text("语义召回本次不可用，已降级为题录规则和全文词法检索。", "Semantic retrieval was unavailable; metadata rules and full-text lexical retrieval were used instead.")}</strong>{discovery.data.hybrid_retrieval?.semantic_reason ? <p>{text(`诊断：${discovery.data.hybrid_retrieval.semantic_reason}`, `Diagnostic: ${discovery.data.hybrid_retrieval.semantic_reason}`)}</p> : null}</div> : null}
          {showNormalizationNotice ? <section className="surface coverage-advisory-card" role="status"><div><span className="step-label">{text("能力降级", "Degraded capability")}</span><h2>{text("专用命名归一化本次不可用", "Specialized name normalization was unavailable")}</h2><p>{text("系统已继续使用通用检索，没有降低证据或引用标准。请复核当前候选是否覆盖主题范围，再确认继续。", "General retrieval continued without weakening evidence or citation standards. Review whether the candidates cover the topic before continuing.")}</p></div><div className="coverage-advisory-actions"><button className="button button-secondary" type="button" disabled={keepLocalCoverage.isPending} onClick={() => keepLocalCoverage.mutate()}>{keepLocalCoverage.isPending ? text("正在保存…", "Saving…") : text("已复核，继续", "Reviewed, continue")}</button></div></section> : null}
          {showCoverageNotice ? <section className="surface coverage-advisory-card" role="status">
            <div>
              <span className="step-label">{text("覆盖诊断", "Coverage diagnosis")}</span>
              <h2>{text("当前本地检索可能存在覆盖缺口", "The current local search may have coverage gaps")}</h2>
              <p>{text(
                `本次检索共找到 ${coverage?.candidate_paper_count || 0} 篇候选。${coverage?.missing_years?.length ? `缺少年份：${coverage.missing_years.join("、")}。` : ""}${coverage?.year_unknown_count ? `${coverage.year_unknown_count} 篇论文年份未确认。` : ""}${coverage?.empty_query_groups?.length ? `未命中检索组：${coverage.empty_query_groups.join("、")}。` : ""}是否手动开启联网补检？`,
                `This search found ${coverage?.candidate_paper_count || 0} candidates.${coverage?.missing_years?.length ? ` Missing years: ${coverage.missing_years.join(", ")}.` : ""}${coverage?.year_unknown_count ? ` ${coverage.year_unknown_count} papers have unconfirmed years.` : ""}${coverage?.empty_query_groups?.length ? ` Empty query groups: ${coverage.empty_query_groups.join(", ")}.` : ""} Start an online supplementary search?`,
              )}</p>
              <small>{text("保持当前结果不会重建 Matrix；如果直接继续，系统按限定本地语料处理覆盖声明。", "Keeping the current results does not rebuild the Matrix. Continuing treats the manuscript as a bounded local-corpus review.")}</small>
            </div>
            <div className="coverage-advisory-actions">
              <button className="button button-primary" type="button" disabled={run.isPending} onClick={() => { setWebSearch(true); run.mutate({ webSearch: true }); }}>{text("开启联网补检", "Start online supplement")}</button>
              <button className="button button-secondary" type="button" disabled={keepLocalCoverage.isPending} onClick={() => keepLocalCoverage.mutate()}>{keepLocalCoverage.isPending ? text("正在保存…", "Saving…") : text("保持当前结果", "Keep current results")}</button>
            </div>
          </section> : null}
          {searchRecord ? <details className="surface advanced-panel discovery-execution-record">
            <summary>{text("查看本次检索的实际执行记录", "View actual search execution record")}</summary>
            <div className="advanced-panel-body">
              <p><strong>{text("实际执行来源", "Executed sources")}：</strong>{searchRecord.executed_sources?.length ? searchRecord.executed_sources.join("、") : text("仅本地文献库", "Local Library only")}</p>
              {searchRecord.failed_sources?.length ? <p className="message message-warning"><strong>{text("失败来源", "Failed sources")}：</strong>{searchRecord.failed_sources.join("、")}</p> : null}
              <p>{text(`本地命中 ${searchRecord.initial_local_hit_count || 0} 次、去重 ${searchRecord.unique_local_candidate_count || 0} 篇；联网命中 ${searchRecord.initial_external_hit_count || 0} 次、去重 ${searchRecord.unique_external_candidate_count || 0} 篇。`, `Local search returned ${searchRecord.initial_local_hit_count || 0} hits (${searchRecord.unique_local_candidate_count || 0} unique); online search returned ${searchRecord.initial_external_hit_count || 0} hits (${searchRecord.unique_external_candidate_count || 0} unique).`)}</p>
              <small>{text("终稿方法部分只会使用这里记录的实际执行来源，不会把未执行的计划来源写成已使用。", "Final methods use only the executed sources recorded here, never merely requested sources.")}</small>
            </div>
          </details> : null}
          <details className="surface advanced-panel discovery-query-diagnostics">
            <summary>{text(`查看查询组诊断（${groups.length}组）`, `View query-group diagnostics (${groups.length})`)}</summary>
            <div className="advanced-panel-body query-diagnostic-list">
              <p className="muted">{text("查询组只用于解释检索覆盖，不是论文分类，也不决定Matrix章节。", "Query groups explain retrieval coverage only; they are not paper classifications and do not determine Matrix sections.")}</p>
              {queryGroups.map((group) => <article key={group.keyword}><div><strong>{groupLabel(group, text)}</strong><small>{queryGroupSourceLabel(group, text)} · {group.local_results?.length || 0} {text("篇本地", "local")} · {group.web_results?.length || 0} {text("篇联网", "online")}</small></div><button className="button button-quiet" type="button" onClick={() => setGroups((current) => current.map((item) => item.keyword === group.keyword ? { ...item, keep: item.keep === false } : item))}>{group.keep === false ? text("恢复该查询", "Restore query") : text("排除该查询", "Exclude query")}</button></article>)}
            </div>
          </details>
          <div className="discovery-stats"><span>{text("去重候选论文", "Unique candidate papers")} {uniqueCandidateCount}</span><span>{text("原始查询命中", "Raw query hits")} {keywordHitCount}</span><span>{text("当前显示", "Currently shown")} {rows.length}</span><span className="selected">{text("进入Matrix", "Selected for matrix")} {selectedCount}</span></div>
          <section className="surface matrix-selection-assistant" aria-label={text("Matrix批量选择", "Matrix bulk selection")}>
            <div className="matrix-selection-summary">
              <div><span className="step-label">{text("批量辅助", "Selection assistant")}</span><strong>{text("系统先推荐，用户只需复核例外", "Start from a recommendation and review exceptions")}</strong></div>
              <div className="matrix-recommendation-counts">
                <span className="recommended">{text("推荐加入", "Recommended")} {recommendation.recommendedIds.size}</span>
                <span className="review">{text("待复核", "Review")} {recommendation.reviewIds.size}</span>
                <span className="excluded">{text("建议排除", "Exclude")} {recommendation.excludedIds.size}</span>
              </div>
            </div>
            <p>{text("推荐依据去重后的论文相关度与候选角色生成；查询组和检索提示不会被当作正式学术分类。", "Recommendations use deduplicated paper relevance and candidate roles; query groups and retrieval hints are not treated as formal academic classifications.")}</p>
            <div className="matrix-selection-actions">
              <button className="button button-primary" type="button" disabled={!recommendation.recommendedIds.size} onClick={applyRecommendation}>{text(`采用系统推荐（${recommendation.recommendedIds.size}篇）`, `Apply recommendation (${recommendation.recommendedIds.size})`)}</button>
              <button className="button button-secondary" type="button" disabled={!rows.some((entry) => entry.kind === "local" && entry.row.role !== "excluded")} onClick={selectVisibleCandidates}>{text("加入当前筛选结果", "Add filtered results")}</button>
              <button className="button button-quiet" type="button" disabled={!hasAnySelection} onClick={clearSelection}>{text("清空选择", "Clear selection")}</button>
            </div>
            {selectionFeedback ? <p className="matrix-selection-feedback" role="status">{selectionFeedback}</p> : null}
            {downloadExternal.error ? <p className="message message-error">{downloadExternal.error.message}</p> : null}
          </section>
          <div className="discovery-grid unified-discovery-grid">
            <section className="pane result-pane">
              <div className="pane-head unified-candidate-head"><div><span className="step-label">{text("统一候选池", "Unified candidate pool")}</span><h2>{text("全部去重候选论文", "All deduplicated candidate papers")}</h2><p>{text("按综合相关度排序；检索提示仅用于复核，正式分类在Matrix生成。", "Sorted by combined relevance. Retrieval hints support review only; formal classifications are generated in the Matrix.")}</p></div></div>
              <div className="candidate-filter-bar">{([
                ["all", text("全部", "All")],
                ["recommended", text("推荐", "Recommended")],
                ["review", text("待复核", "Review")],
                ["selected", text("已选择", "Selected")],
                ["metadata_rules", text("题录/规则", "Metadata/rules")],
                ["fulltext_lexical", text("全文", "Full text")],
                ["semantic", text("语义", "Semantic")],
                ["online", text("联网", "Online")],
              ] as Array<[CandidateFilter, string]>).map(([filter, label]) => <button key={filter} type="button" className={candidateFilter === filter ? "active" : ""} onClick={() => setCandidateFilter(filter)}>{label}<small>{candidateFilterCounts[filter]}</small></button>)}</div>
              <div className="result-list">{rows.map(({ row, kind }, index) => {
                const active = selectedPaper?.kind === kind && (kind === "local" ? localCandidateId(selectedPaper.row) === localCandidateId(row) : externalCandidateId(selectedPaper.row) === externalCandidateId(row));
                const included = selectedForMatrix(row);
                const id = String(row.paper_id || "");
                const lexicalHintIds = row.lexical_partition_candidates || [];
                // Semantic facet similarity is too weak to present as a named
                // paper classification. Keep it in advanced diagnostics and
                // show only source-text discriminator hits in the main list.
                const retrievalHints = [...new Set(lexicalHintIds.map((queryId) => partitionLabels.get(queryId)).filter((label): label is string => Boolean(label)))].slice(0, 3);
                const recommendationStatus = kind === "local"
                  ? row.recommendation_status || (recommendation.recommendedIds.has(id) ? "recommended" : recommendation.excludedIds.has(id) ? "excluded" : "review")
                  : row.recommendation_status || "review";
                const externalProcessing = kind === "web"
                  && String(downloadExternal.variables?.candidate_id || "") === String(row.candidate_id || "")
                  && (downloadExternal.isPending || jobIsActive(externalDownloadJob.data?.status) || Boolean(refreshWatch));
                return (
                  <article key={`${kind}-${row.paper_id || row.candidate_id || index}`} className={active ? "result-card active" : "result-card"} onClick={() => setSelectedPaper({ row, kind })}>
                    <div>
                      <div className="result-card-labels">
                        <span className="result-kind" title={kind === "local" ? id : undefined}>{kind === "local" ? paperLabels.get(id) || text("本地", "Local") : text("外部", "External")}</span>
                        <span className={`matrix-recommendation-badge ${recommendationStatus}`}>{recommendationStatus === "recommended" ? text("推荐", "Recommended") : recommendationStatus === "excluded" ? text("建议排除", "Exclude") : text("待复核", "Review")}</span>
                      </div>
                      <h3>{row.title || text("无标题", "Untitled")}</h3>
                      <p>{[row.year, row.journal, row.source].filter(Boolean).join(" · ")}</p>
                      {row.retrieval_channels?.length ? <div className="retrieval-badges">{row.retrieval_channels.slice(0, 3).map((channel) => <span key={channel}>{retrievalChannelLabel(channel, text)}</span>)}</div> : null}
                      {retrievalHints.length ? <div className="retrieval-hints" title={text("全文鉴别词命中，仅供复核；正式分类在Matrix生成", "Full-text discriminator hits for review only; formal classifications are generated in the Matrix")}><small>{text("鉴别词命中 · 待Matrix核验", "Discriminator hit · verify in Matrix")}</small>{retrievalHints.map((hint) => <span key={hint}>{hint}</span>)}</div> : null}
                    </div>
                    <div className="result-actions">
                      {kind === "local" ? (
                        <>
                          <button type="button" className={included ? "button button-primary" : "button button-secondary"} onClick={(event) => { event.stopPropagation(); updateRow(row, { selected_for_matrix: !included, ...(included ? {} : row.role === "excluded" ? { role: "uncertain" } : {}) }); }}>{included ? text("已加入Matrix", "Added to matrix") : text("加入Matrix", "Add to matrix")}</button>
                          <select value={row.role || "uncertain"} onClick={(event) => event.stopPropagation()} onChange={(event) => updateRow(row, { role: event.target.value, ...(event.target.value === "excluded" ? { selected_for_matrix: false } : {}) })}>{["core_candidate", "supporting_candidate", "background", "uncertain", "excluded"].map((role) => <option key={role}>{role}</option>)}</select>
                        </>
                      ) : row.access_status === "open_access_downloadable" ? (
                        <button type="button" className="button button-secondary" disabled={externalProcessing || downloadExternal.isPending} onClick={(event) => { event.stopPropagation(); downloadExternal.mutate(row); }}>{externalProcessing ? text("处理中…", "Processing…") : externalActionLabel(row.access_status, text)}</button>
                      ) : row.landing_url ? (
                        <a className="button button-secondary" href={row.landing_url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>{externalActionLabel(row.access_status, text)}</a>
                      ) : (
                        <button type="button" className="button button-secondary" onClick={(event) => { event.stopPropagation(); navigate(`/library?project=${encodeURIComponent(project!.project_id)}`); }}>{text("导入PDF", "Import PDF")}</button>
                      )}
                    </div>
                  </article>
                );
              })}{!rows.length ? <div className="empty-state compact-empty">{text("当前筛选条件下没有候选论文。", "No candidate papers match the current filter.")}</div> : null}</div>
            </section>
            <section className="pane discovery-detail-pane"><PaperDetail row={selectedPaper?.row || null} kind={selectedPaper?.kind || "local"} displayLabel={selectedPaper?.kind === "local" ? paperLabels.get(String(selectedPaper.row.paper_id || "")) : undefined} /></section>
          </div>
          <div className="stage-action-bar"><div><strong>{text("论文选择", "Paper selection")}</strong><p>{text("选择需要进入 Matrix 的论文；确认采用时会自动保存当前选择，并只在输入确实变化时让后续阶段过期。", "Choose papers for the matrix. Adoption automatically saves the current selection and marks later stages stale only when the inputs actually changed.")}</p></div><button className="button button-primary" type="button" disabled={!selectedCount || confirm.isPending} onClick={() => confirm.mutate()}>{confirm.isPending ? text("同步中…", "Syncing…") : text(`确认采用并进入 Matrix（${selectedCount}篇）`, `Adopt and enter matrix (${selectedCount})`)}</button>{confirm.error ? <span className="message message-error">{confirm.error.message}</span> : null}</div>
        </>
      ) : null}
    </main>
  );
}
