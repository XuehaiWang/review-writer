import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { libraryQuery, queryKeys } from "../../api/queries";
import type { Job, LibraryPaper, UploadJob, UploadJobList } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { useUiText } from "../../i18n/useUiText";
import { buildPaperDisplayLabels } from "../../utils/paperLabels";
import { uploadJobsNeedingLibraryRefresh } from "./uploadRefresh";

type DetailTab = "metadata" | "markdown" | "pdf";
type UploadStatus = {
  id: string;
  name: string;
  status: "queued" | "uploading" | "done" | "failed";
  message: string;
  messageEn?: string;
  updatedAt?: string;
};
type Candidate = Record<string, unknown> & { candidate_id?: string; title?: string; year?: number; journal?: string; doi?: string; score?: number; landing_url?: string };
type DownloadResult = {
  added_count?: number;
  already_present_count?: number;
  failed_count?: number;
  results?: Array<{ status?: string; error?: string }>;
};

const UPLOAD_RESULT_VISIBLE_MS = 12_000;

function uploadResultIsVisible(updatedAt: string | undefined, now: number): boolean {
  const timestamp = Date.parse(updatedAt || "");
  return Number.isFinite(timestamp) && timestamp + UPLOAD_RESULT_VISIBLE_MS > now;
}

function UploadBatchProgress({ uploads }: { uploads: UploadStatus[] }) {
  const { language, text } = useUiText();
  if (!uploads.length) return null;

  const total = uploads.length;
  const done = uploads.filter((row) => row.status === "done").length;
  const failed = uploads.filter((row) => row.status === "failed").length;
  const uploading = uploads.filter((row) => row.status === "uploading").length;
  const queued = uploads.filter((row) => row.status === "queued").length;
  const finished = done + failed;
  const active = uploading > 0 || queued > 0;
  const progress = Math.round((finished / total) * 100);
  const current = uploads.find((row) => row.status === "uploading");
  const firstFailure = uploads.find((row) => row.status === "failed");
  const stateClass = active ? "running" : failed > 0 ? "failed" : "done";
  const title = active
    ? text("正在批量上传并执行 MinerU 解析", "Uploading and running MinerU parsing")
    : failed > 0
      ? text("批量处理已结束，部分文件失败", "Batch processing finished with failures")
      : text("批量上传与解析完成", "Batch upload and parsing completed");

  return (
    <section className={`upload-progress-panel ${stateClass}`} aria-live="polite">
      <div className="upload-progress-heading">
        <div>
          <span className="upload-progress-kicker">{text("批量 PDF 处理", "PDF batch processing")}</span>
          <strong>{title}</strong>
        </div>
        <span className="upload-progress-count">{finished}/{total}</span>
      </div>
      <div
        className="upload-progress-track"
        role="progressbar"
        aria-label={text("批量上传总体进度", "Overall batch upload progress")}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
      >
        <span style={{ width: `${progress}%` }} />
      </div>
      <div className="upload-progress-stats">
        <span><b>{done}</b>{text("已完成", "Completed")}</span>
        <span><b>{uploading}</b>{text("处理中", "Processing")}</span>
        <span><b>{queued}</b>{text("等待中", "Waiting")}</span>
        <span className={failed ? "has-failures" : ""}><b>{failed}</b>{text("失败", "Failed")}</span>
      </div>
      {current ? <p className="upload-progress-detail">{text("当前文件：", "Current file: ")}<strong>{current.name}</strong></p> : null}
      {!active && firstFailure ? (
        <p className="upload-progress-error" role="alert">
          {text("失败原因：", "Failure: ")}{language === "en" ? firstFailure.messageEn || firstFailure.message : firstFailure.message}
          {failed > 1 ? text(`（另有 ${failed - 1} 个文件失败）`, ` (${failed - 1} more failed)`) : null}
        </p>
      ) : null}
      <div className="upload-progress-list">
        {uploads.slice(0, 12).map((row) => (
          <div className={`upload-progress-row ${row.status}`} key={row.id}>
            <span className="upload-progress-state" aria-hidden="true" />
            <strong title={row.name}>{row.name}</strong>
            <span>{language === "en" ? row.messageEn || row.message : row.message}</span>
            {row.updatedAt ? <time dateTime={row.updatedAt}>{new Date(row.updatedAt).toLocaleTimeString(language === "en" ? "en-US" : "zh-CN", { hour: "2-digit", minute: "2-digit" })}</time> : null}
          </div>
        ))}
      </div>
    </section>
  );
}

function jobResult<T>(job: Job | null | undefined, key: string, fallback: T): T {
  const value = job?.result?.[key];
  return (value === undefined ? fallback : value) as T;
}

function candidateWebsite(candidate: Candidate): string {
  const values = [
    String(candidate.landing_url || "").trim(),
    candidate.doi ? `https://doi.org/${String(candidate.doi).trim()}` : "",
  ];
  for (const value of values) {
    if (!value) continue;
    try {
      const parsed = new URL(value);
      if (parsed.protocol === "https:" || parsed.protocol === "http:") return parsed.toString();
    } catch {
      // Ignore malformed provider URLs and try the DOI fallback.
    }
  }
  return "";
}

function acquisitionEndpoint(path: string, projectId: string) {
  return `${path}?project_id=${encodeURIComponent(projectId)}`;
}

function useCurrentLibraryJob(kind: "search" | "download", projectId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["library", `${kind}-job`, projectId],
    queryFn: () => apiRequest<{ job: Job | null }>(acquisitionEndpoint(`/api/v1/library/${kind}-jobs/current`, projectId)),
    enabled,
    refetchInterval: (query) => {
      const status = query.state.data?.job?.status;
      return status && ["queued", "running", "cancel_requested"].includes(status) ? 1100 : false;
    },
  });
}

function LibraryJobStatus({ kind, job }: { kind: "search" | "download"; job: Job | null | undefined }) {
  const { language, text } = useUiText();
  if (!job) return null;
  const label = kind === "search" ? text("检索", "Search") : text("下载", "Download");
  const statusLabels: Record<Job["status"], string> = {
    queued: text("等待中", "Queued"),
    running: text("进行中", "Running"),
    cancel_requested: text("正在取消", "Cancelling"),
    succeeded: text("已完成", "Completed"),
    failed: text("失败", "Failed"),
    cancelled: text("已取消", "Cancelled"),
    interrupted: text("已中断", "Interrupted"),
  };
  let errorMessage = job.error_message;
  if (job.status === "failed" && (!errorMessage || errorMessage === "Scientific task failed.")) {
    errorMessage = kind === "search"
      ? text("期刊检索未能连接 Crossref。请检查服务器的外网、DNS或代理设置后重试。", "The journal search could not reach Crossref. Check the server's outbound network, DNS, or proxy and retry.")
      : text("文献下载任务在开始处理前失败，请检查网络设置后重试。", "The literature download failed before processing began. Check the network settings and retry.");
  } else if (language !== "en" && job.error_code === "LITERATURE_SEARCH_FAILED") {
    const normalized = String(errorMessage || "").toLowerCase();
    if (normalized.includes("private or transparent-proxy")) {
      errorMessage = "Crossref 被解析到尚未受信任的透明代理地址。请配置 REVIEW_WRITER_TRUSTED_PROXY_NETWORKS 后重试。";
    } else if (normalized.includes("timeout")) {
      errorMessage = "Crossref 响应超时。请检查服务器外网或代理后重试。";
    } else if (normalized.includes("dns") || normalized.includes("resolved")) {
      errorMessage = "服务器无法通过 DNS 解析 Crossref。请检查 DNS 和外网连接后重试。";
    } else if (normalized.includes("certificate") || normalized.includes("tls")) {
      errorMessage = "Crossref 的 TLS 证书校验失败。请检查证书信任库或 HTTPS 代理。";
    } else if (normalized.includes("rate-limit")) {
      errorMessage = "Crossref 已限制本次检索频率，请稍后重试。";
    } else {
      errorMessage = "期刊检索未能访问 Crossref。请检查服务器的外网、DNS或代理设置后重试。";
    }
  }
  return (
    <div className={`library-job-status ${job.status}`} role={job.status === "failed" ? "alert" : "status"}>
      <div>
        <strong>{label}</strong>
        <span>{statusLabels[job.status]}</span>
        {job.progress_total > 0 ? <em>{job.progress_current}/{job.progress_total}</em> : null}
      </div>
      {job.status === "failed" && errorMessage ? <p>{errorMessage}{job.error_code ? <code>{job.error_code}</code> : null}</p> : null}
    </div>
  );
}

function PaperListItem({ paper, displayLabel, selected, onSelect }: { paper: LibraryPaper; displayLabel: string; selected: boolean; onSelect: () => void }) {
  const { text } = useUiText();
  const status = paper.human_review_status === "reviewed"
    ? { className: "status-dot reviewed", label: text("已人工审核", "Manually reviewed") }
    : paper.needs_human_check
      ? { className: "status-dot warning", label: text("需要人工检查", "Manual review required") }
      : { className: "status-dot ok", label: text("Metadata已就绪", "Metadata ready") };
  return (
    <button type="button" className={selected ? "paper-row active" : "paper-row"} onClick={onSelect}>
      <span className="paper-row-main">
        <strong><span className="paper-display-id" title={paper.paper_id}>{displayLabel}</span> · {paper.title || displayLabel}</strong>
        <small>{paper.search_match ? `${text("正文页", "Full text p.")} ${paper.search_match.page_start || "?"} · ${paper.search_match.content.slice(0, 90)}` : [paper.year, paper.journal, paper.doi].filter(Boolean).join(" · ") || paper.original_filename}</small>
      </span>
      <span className={status.className} title={status.label} />
    </button>
  );
}

function DocumentIndexStatus({ paper }: { paper: LibraryPaper }) {
  const { text } = useUiText();
  const index = paper.index_status;
  const fulltextLabels: Record<string, string> = {
    not_indexed: text("未建立", "Not indexed"),
    queued: text("等待中", "Queued"),
    building: text("建立中", "Building"),
    ready: text("已就绪", "Ready"),
    failed: text("失败", "Failed"),
    rebuild_required: text("需要重建", "Rebuild required"),
  };
  return (
    <div className="document-index-status" aria-live="polite">
      <span className={index?.mineru === "ready" ? "ready" : "disabled"}>
        <b>MinerU</b>{index?.mineru === "ready" ? text("解析完成", "Parsed") : text("产物不可用", "Unavailable")}
      </span>
      <span className={index?.fulltext === "ready" ? "ready" : ["failed", "rebuild_required"].includes(index?.fulltext || "") ? "failed" : "pending"} title={index?.error_message || ""}>
        <b>{text("全文索引", "Full-text index")}</b>{fulltextLabels[index?.fulltext || "not_indexed"]}{index?.fulltext === "ready" ? ` · ${index.chunk_count} chunks` : ""}
      </span>
      <span className="disabled"><b>{text("语义索引", "Semantic index")}</b>{text("未启用", "Disabled")}</span>
    </div>
  );
}

function AcquisitionPanel({ projectId, onLibraryChanged }: { projectId: string; onLibraryChanged: () => Promise<unknown> }) {
  const { text } = useUiText();
  const [topic, setTopic] = useState("");
  const [email, setEmail] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const searchJob = useCurrentLibraryJob("search", projectId, Boolean(projectId));
  const downloadJob = useCurrentLibraryJob("download", projectId, Boolean(projectId));
  const candidates = jobResult<Candidate[]>(searchJob.data?.job, "candidates", []);
  const downloadResult = downloadJob.data?.job?.result as DownloadResult | undefined;
  const search = useMutation({
    mutationFn: () => apiRequest<Job>(acquisitionEndpoint("/api/v1/library/search-jobs", projectId), {
      method: "POST",
      headers: { "Idempotency-Key": newIdempotencyKey() },
      ...jsonBody({ topic: topic.trim(), limit: 30, email: email.trim() }),
    }),
    onSuccess: async () => {
      setSelected(new Set());
      await searchJob.refetch();
    },
  });
  const download = useMutation({
    mutationFn: () => apiRequest<Job>(acquisitionEndpoint("/api/v1/library/download-jobs", projectId), {
      method: "POST",
      headers: { "Idempotency-Key": newIdempotencyKey() },
      ...jsonBody({ candidates: candidates.filter((row) => selected.has(String(row.candidate_id || ""))), email: email.trim() }),
    }),
    onSuccess: async () => downloadJob.refetch(),
  });
  useEffect(() => {
    if (downloadJob.data?.job?.status === "succeeded") void onLibraryChanged();
  }, [downloadJob.data?.job?.status, onLibraryChanged]);
  useEffect(() => {
    setSelected(new Set());
  }, [projectId]);

  const active = [searchJob.data?.job?.status, downloadJob.data?.job?.status].some((status) => status && ["queued", "running", "cancel_requested"].includes(status));
  return (
    <section className="surface acquisition-panel">
      <div className="section-heading compact">
        <div><h2>{text("联网检索开放获取文献", "Find open-access literature online")}</h2><p>{text("搜索候选后选择论文下载；下载成功后自动进入当前用户文献库。", "Search for candidates, select papers to download, and add successful downloads to your library.")}</p></div>
      </div>
      <div className="acquisition-form">
        <label>{text("英文主题", "Topic in English")}<input value={topic} onChange={(event) => setTopic(event.target.value)} placeholder="axially chiral allene catalysis" /></label>
        <label>{text("Unpaywall邮箱（可选）", "Unpaywall email (optional)")}<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></label>
        <button className="button button-secondary" type="button" disabled={!projectId || topic.trim().length < 3 || search.isPending || active} onClick={() => search.mutate()}>
          {search.isPending ? text("提交中…", "Submitting…") : text("检索期刊文章", "Search journal articles")}
        </button>
      </div>
      {search.error ? <p className="message message-error">{search.error.message}</p> : null}
      <LibraryJobStatus kind="search" job={searchJob.data?.job} />
      <div className="candidate-list">
        {candidates.map((candidate, index) => {
          const id = String(candidate.candidate_id || "");
          const checkboxId = `candidate-${index}`;
          const website = candidateWebsite(candidate);
          return (
            <div className="candidate-row" key={id || String(candidate.title)}>
              <input
                id={checkboxId}
                type="checkbox"
                checked={selected.has(id)}
                onChange={(event) => setSelected((current) => {
                  const next = new Set(current);
                  if (event.target.checked) next.add(id); else next.delete(id);
                  return next;
                })}
              />
              <label htmlFor={checkboxId} className="candidate-copy"><strong>{String(candidate.title || text("无标题", "Untitled"))}</strong><small>{[candidate.year, candidate.journal, candidate.doi].filter(Boolean).join(" · ")}</small></label>
              <div className="candidate-actions">
                <em>{Math.round(Number(candidate.score || 0) * 100)}%</em>
                {website ? <a href={website} target="_blank" rel="noopener noreferrer">{text("访问期刊页面", "Open article website")}</a> : null}
              </div>
            </div>
          );
        })}
      </div>
      {candidates.length ? (
        <button className="button button-primary" type="button" disabled={!selected.size || download.isPending || active} onClick={() => download.mutate()}>
          {text(`下载所选 ${selected.size} 篇`, `Download ${selected.size} selected papers`)}
        </button>
      ) : null}
      <LibraryJobStatus kind="download" job={downloadJob.data?.job} />
      {downloadJob.data?.job?.status === "succeeded" && downloadResult ? (
        <p className={`message ${Number(downloadResult.failed_count || 0) > 0 ? "message-warning" : ""}`} role="status">
          {text(
            `下载处理完成：新增 ${Number(downloadResult.added_count || 0)} 篇，已存在 ${Number(downloadResult.already_present_count || 0)} 篇，未下载 ${Number(downloadResult.failed_count || 0)} 篇。`,
            `Download processing completed: ${Number(downloadResult.added_count || 0)} added, ${Number(downloadResult.already_present_count || 0)} already present, ${Number(downloadResult.failed_count || 0)} not downloaded.`,
          )}
          {downloadResult.results?.some((row) => row.status === "no_open_access_pdf")
            ? text(" 所选论文没有找到可合法下载的开放获取 PDF。", " No lawfully downloadable open-access PDF was found for the selected paper.")
            : null}
        </p>
      ) : null}
      {download.error ? <p className="message message-error">{download.error.message}</p> : null}
    </section>
  );
}

export function LibraryPage() {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const { selected: project } = useSelectedProject();
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [tab, setTab] = useState<DetailTab>("metadata");
  const [metadataDraft, setMetadataDraft] = useState("");
  const [localUploads, setLocalUploads] = useState<UploadStatus[]>([]);
  const [uploadStatusNow, setUploadStatusNow] = useState(() => Date.now());
  const uploadJobStatuses = useRef(new Map<string, Job["status"]>());
  const locallySubmittedUploadJobs = useRef(new Set<string>());
  const refreshedUploadJobs = useRef(new Set<string>());
  const uploadJobs = useQuery({
    queryKey: queryKeys.libraryUploadJobs,
    queryFn: () => apiRequest<UploadJobList>("/api/v1/library/upload-jobs/recent?limit=30"),
    refetchInterval: (query) => query.state.data?.items.some((job) => ["queued", "running", "cancel_requested"].includes(job.status)) ? 1100 : false,
  });
  const persistedUploads = useMemo<UploadStatus[]>(() => (uploadJobs.data?.items || []).filter((job) => {
    if (["queued", "running", "cancel_requested"].includes(job.status)) return true;
    return uploadResultIsVisible(job.updated_at, uploadStatusNow);
  }).map((job) => {
    if (job.status === "queued") return { id: job.id, name: job.filename, status: "queued", message: "等待服务器处理", messageEn: "Waiting for server processing", updatedAt: job.updated_at };
    if (job.status === "running" || job.status === "cancel_requested") return { id: job.id, name: job.filename, status: "uploading", message: job.status === "cancel_requested" ? "正在取消解析" : "正在执行 MinerU 解析", messageEn: job.status === "cancel_requested" ? "Cancelling parsing" : "Running MinerU parsing", updatedAt: job.updated_at };
    if (job.status === "succeeded") {
      const duplicate = job.result?.status === "duplicate_file";
      return { id: job.id, name: job.filename, status: "done", message: duplicate ? "文件已存在，已复用解析结果和全文索引" : "上传与解析完成，全文索引已进入后台队列", messageEn: duplicate ? "Already exists; parsing and full-text index reused" : "Upload and parsing completed; full-text indexing was queued", updatedAt: job.updated_at };
    }
    return { id: job.id, name: job.filename, status: "failed", message: job.error_message || "上传或解析失败", messageEn: job.error_message || "Upload or parsing failed", updatedAt: job.updated_at };
  }), [uploadJobs.data?.items, uploadStatusNow]);
  const visibleLocalUploads = useMemo(() => localUploads.filter((row) => {
    if (row.status === "queued" || row.status === "uploading") return true;
    return uploadResultIsVisible(row.updatedAt, uploadStatusNow);
  }), [localUploads, uploadStatusNow]);
  const uploads = useMemo(() => [...visibleLocalUploads, ...persistedUploads], [visibleLocalUploads, persistedUploads]);
  useEffect(() => {
    const expirations = [
      ...localUploads
        .filter((row) => row.status === "done" || row.status === "failed")
        .map((row) => Date.parse(row.updatedAt || "") + UPLOAD_RESULT_VISIBLE_MS),
      ...(uploadJobs.data?.items || [])
        .filter((job) => !["queued", "running", "cancel_requested"].includes(job.status))
        .map((job) => Date.parse(job.updated_at) + UPLOAD_RESULT_VISIBLE_MS),
    ].filter((value) => Number.isFinite(value) && value > uploadStatusNow);
    if (!expirations.length) return;
    const delay = Math.max(100, Math.min(...expirations) - Date.now() + 25);
    const timeout = window.setTimeout(() => setUploadStatusNow(Date.now()), delay);
    return () => window.clearTimeout(timeout);
  }, [localUploads, uploadJobs.data?.items, uploadStatusNow]);
  const library = useQuery(libraryQuery(query));
  const libraryIndex = useQuery(libraryQuery(""));
  const paperLabels = useMemo(
    () => buildPaperDisplayLabels(libraryIndex.data?.items || library.data?.items || []),
    [library.data?.items, libraryIndex.data?.items],
  );
  const refreshLibrary = useCallback(
    async () => {
      const refreshes = [queryClient.invalidateQueries({ queryKey: queryKeys.library(""), exact: true })];
      if (query) refreshes.push(queryClient.invalidateQueries({ queryKey: queryKeys.library(query), exact: true }));
      await Promise.all(refreshes);
    },
    [query, queryClient],
  );
  useEffect(() => {
    const jobs = uploadJobs.data?.items;
    if (!jobs) return;
    const completed = uploadJobsNeedingLibraryRefresh(
      jobs,
      uploadJobStatuses.current,
      locallySubmittedUploadJobs.current,
      refreshedUploadJobs.current,
    );
    for (const job of jobs) {
      uploadJobStatuses.current.set(job.id, job.status);
      if (["failed", "cancelled", "interrupted"].includes(job.status)) {
        locallySubmittedUploadJobs.current.delete(job.id);
      }
    }
    if (!completed.length) return;
    for (const jobId of completed) {
      refreshedUploadJobs.current.add(jobId);
      locallySubmittedUploadJobs.current.delete(jobId);
    }
    void refreshLibrary();
  }, [refreshLibrary, uploadJobs.data?.items]);
  const selectedPaper = useMemo(
    () => library.data?.items.find((paper) => paper.paper_id === selectedId) || library.data?.items[0],
    [library.data?.items, selectedId],
  );
  useEffect(() => {
    if (selectedPaper && selectedPaper.paper_id !== selectedId) setSelectedId(selectedPaper.paper_id);
  }, [selectedId, selectedPaper]);
  const metadata = useQuery({
    queryKey: queryKeys.libraryMetadata(selectedPaper?.paper_id || ""),
    queryFn: () => apiRequest<Record<string, unknown>>(`/api/v1/library/papers/${encodeURIComponent(selectedPaper!.paper_id)}/metadata`),
    enabled: Boolean(selectedPaper),
  });
  const markdown = useQuery({
    queryKey: queryKeys.libraryMarkdown(selectedPaper?.paper_id || ""),
    queryFn: () => apiRequest<string>(`/api/v1/library/papers/${encodeURIComponent(selectedPaper!.paper_id)}/markdown`),
    enabled: Boolean(selectedPaper) && tab === "markdown",
  });
  useEffect(() => {
    if (metadata.data) setMetadataDraft(JSON.stringify(metadata.data, null, 2));
  }, [metadata.data]);
  const saveMetadata = useMutation({
    mutationFn: () => apiRequest<Record<string, unknown>>(`/api/v1/library/papers/${encodeURIComponent(selectedPaper!.paper_id)}/metadata`, {
      method: "PUT",
      ...jsonBody(JSON.parse(metadataDraft) as Record<string, unknown>),
    }),
    onSuccess: async (saved) => {
      setMetadataDraft(JSON.stringify(saved, null, 2));
      await queryClient.invalidateQueries({ queryKey: queryKeys.library(query) });
    },
  });
  const deletePaper = useMutation({
    mutationFn: () => apiRequest<void>(`/api/v1/library/papers/${encodeURIComponent(selectedPaper!.paper_id)}`, { method: "DELETE" }),
    onSuccess: async () => {
      setSelectedId("");
      await queryClient.invalidateQueries({ queryKey: ["library"] });
    },
  });
  const reindexPaper = useMutation({
    mutationFn: () => apiRequest<Job>(`/api/v1/library/papers/${encodeURIComponent(selectedPaper!.paper_id)}/reindex`, {
      method: "POST",
      headers: { "Idempotency-Key": newIdempotencyKey() },
    }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["library"] });
    },
  });
  const reindexMissing = useMutation({
    mutationFn: () => apiRequest<{ count: number }>("/api/v1/library/reindex-jobs", {
      method: "POST",
      ...jsonBody({ force: false }),
    }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["library"] });
    },
  });
  const markReviewed = useMutation({
    mutationFn: async () => {
      const parsed = JSON.parse(metadataDraft) as Record<string, unknown>;
      const humanReview = typeof parsed.human_review === "object" && parsed.human_review !== null
        ? { ...(parsed.human_review as Record<string, unknown>) }
        : {};
      humanReview.status = "reviewed";
      humanReview.reviewed_at = new Date().toISOString();
      humanReview.reviewer = humanReview.reviewer || "human";
      parsed.human_review = humanReview;
      return apiRequest<Record<string, unknown>>(`/api/v1/library/papers/${encodeURIComponent(selectedPaper!.paper_id)}/metadata`, {
        method: "PUT",
        ...jsonBody(parsed),
      });
    },
    onSuccess: async (saved) => {
      setMetadataDraft(JSON.stringify(saved, null, 2));
      await queryClient.invalidateQueries({ queryKey: ["library"] });
    },
  });

  async function uploadFiles(files: FileList | null) {
    if (!files?.length) return;
    const queue = Array.from(files);
    if (queue.length > 30) {
      setLocalUploads([{ id: newIdempotencyKey(), name: text("本批文件", "This batch"), status: "failed", message: "每批最多上传30个PDF文件。", messageEn: "A batch can contain at most 30 PDF files.", updatedAt: new Date().toISOString() }]);
      setUploadStatusNow(Date.now());
      return;
    }
    const invalid = queue.find((file) => !file.name.toLocaleLowerCase().endsWith(".pdf"));
    if (invalid) {
      setLocalUploads([{ id: newIdempotencyKey(), name: invalid.name, status: "failed", message: "该文件不是PDF，未开始上传。", messageEn: "This file is not a PDF. Upload was not started.", updatedAt: new Date().toISOString() }]);
      setUploadStatusNow(Date.now());
      return;
    }
    const batchId = newIdempotencyKey();
    const localRows = queue.map((file, index) => ({ id: `${batchId}:${index}`, name: file.name, status: "queued" as const, message: "等待上传", messageEn: "Waiting to upload" }));
    setLocalUploads(localRows);
    for (const [index, file] of queue.entries()) {
      const localId = `${batchId}:${index}`;
      setLocalUploads((rows) => rows.map((row) => row.id === localId ? { ...row, status: "uploading", message: "正在上传到服务器", messageEn: "Uploading to server" } : row));
      try {
        const submitted = await apiRequest<UploadJob>(`/api/v1/library/upload-jobs?filename=${encodeURIComponent(file.name)}&batch_id=${encodeURIComponent(batchId)}`, {
          method: "POST",
          headers: { "Content-Type": file.type || "application/pdf", "Idempotency-Key": newIdempotencyKey() },
          body: file,
        });
        locallySubmittedUploadJobs.current.add(submitted.id);
        uploadJobStatuses.current.set(submitted.id, submitted.status);
        setLocalUploads((rows) => rows.filter((row) => row.id !== localId));
        await uploadJobs.refetch();
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setLocalUploads((rows) => rows.map((row) => row.id === localId ? { ...row, status: "failed", message, updatedAt: new Date().toISOString() } : row));
        setUploadStatusNow(Date.now());
      }
    }
    await refreshLibrary();
  }

  return (
    <main className="workspace page-container workspace-page">
      <div className="workspace-heading">
        <div><p className="eyebrow">{text("阶段 1 · 共享文献集合", "Stage 1 · Shared source collection")}</p><h1>{text("文献库", "Literature library")}</h1><p className="muted">{text("上传PDF后必须完成MinerU解析，Metadata和正文才会供后续检索与写作使用。", "Uploaded PDFs must complete MinerU parsing before metadata and full text are available to later discovery and writing stages.")}</p></div>
        <div className="library-heading-actions"><ProjectSelector /><button className="button button-secondary" type="button" disabled={reindexMissing.isPending} onClick={() => reindexMissing.mutate()}>{reindexMissing.isPending ? text("提交中…", "Submitting…") : text("补建缺失索引", "Build missing indexes")}</button><label className="button button-primary file-button">{text("批量上传PDF", "Upload PDFs in batch")}<input type="file" accept="application/pdf,.pdf" multiple onChange={(event) => { const files = event.target.files; void uploadFiles(files); event.currentTarget.value = ""; }} /></label></div>
      </div>
      {reindexMissing.error ? <p className="message message-error">{reindexMissing.error.message}</p> : null}
      <UploadBatchProgress uploads={uploads} />
      <AcquisitionPanel projectId={project?.project_id || ""} onLibraryChanged={refreshLibrary} />
      <div className="three-pane library-workspace">
        <section className="pane list-pane">
          <div className="pane-head"><div><span className="step-label">{text("论文", "Papers")}</span><h2>{text("文献", "Papers")} <span aria-live="polite">{libraryIndex.data?.count ?? library.data?.count ?? 0}</span></h2></div></div>
          <input className="pane-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={text("检索标题、作者、关键词或正文", "Search title, author, keyword, or full text")} />
          {library.error ? <ErrorState error={library.error} onRetry={() => library.refetch()} /> : null}
          <div className="paper-list">{library.data?.items.map((paper) => <PaperListItem key={paper.paper_id} paper={paper} displayLabel={paperLabels.get(paper.paper_id) || paper.paper_id} selected={paper.paper_id === selectedPaper?.paper_id} onSelect={() => setSelectedId(paper.paper_id)} />)}</div>
        </section>
        <section className="pane detail-pane">
          {selectedPaper ? (
            <>
              <div className="pane-head paper-title"><div><span className="step-label" title={selectedPaper.paper_id}>{paperLabels.get(selectedPaper.paper_id) || selectedPaper.paper_id}</span><h2>{selectedPaper.title}</h2><p>{selectedPaper.authors?.join(", ")}</p></div><div className="paper-title-actions"><button className="button button-secondary" type="button" disabled={reindexPaper.isPending || ["queued", "building"].includes(selectedPaper.index_status?.fulltext || "")} onClick={() => reindexPaper.mutate()}>{reindexPaper.isPending ? text("提交中…", "Submitting…") : text("重建全文索引", "Rebuild full-text index")}</button><button className="button button-quiet danger" type="button" disabled={deletePaper.isPending} onClick={() => { if (window.confirm(text(`确认删除 ${paperLabels.get(selectedPaper.paper_id) || selectedPaper.paper_id}？`, `Delete ${paperLabels.get(selectedPaper.paper_id) || selectedPaper.paper_id}?`))) deletePaper.mutate(); }}>{text("删除", "Delete")}</button></div></div>
              <DocumentIndexStatus paper={selectedPaper} />
              {selectedPaper.search_match ? <button type="button" className="library-search-match" onClick={() => setTab("markdown")}><span>{text(`正文命中 · 第 ${selectedPaper.search_match.page_start || "?"} 页`, `Full-text match · Page ${selectedPaper.search_match.page_start || "?"}`)}</span><p>{selectedPaper.search_match.content}</p><code>{selectedPaper.search_match.chunk_id}</code></button> : null}
              {reindexPaper.error ? <p className="message message-error index-error">{reindexPaper.error.message}</p> : null}
              <nav className="detail-tabs">{(["metadata", "markdown", "pdf"] as const).map((value) => <button key={value} className={tab === value ? "active" : ""} type="button" onClick={() => setTab(value)}>{value === "metadata" ? "Metadata" : value === "markdown" ? "Markdown" : "PDF"}</button>)}</nav>
              {tab === "metadata" ? <div className="editor-panel"><textarea className="code-editor" value={metadataDraft} onChange={(event) => setMetadataDraft(event.target.value)} spellCheck={false} /><div className="editor-actions"><button className="button button-primary" type="button" disabled={saveMetadata.isPending || markReviewed.isPending} onClick={() => { try { JSON.parse(metadataDraft); saveMetadata.mutate(); } catch { window.alert(text("Metadata不是有效JSON。", "Metadata is not valid JSON.")); } }}>{saveMetadata.isPending ? text("保存中…", "Saving…") : text("保存Metadata", "Save metadata")}</button><button className="button button-secondary" type="button" disabled={saveMetadata.isPending || markReviewed.isPending} onClick={() => { try { JSON.parse(metadataDraft); markReviewed.mutate(); } catch { window.alert(text("Metadata不是有效JSON。", "Metadata is not valid JSON.")); } }}>{markReviewed.isPending ? text("标记中…", "Marking…") : text("标记为已审核", "Mark as reviewed")}</button>{saveMetadata.error || markReviewed.error ? <span className="message message-error">{(saveMetadata.error || markReviewed.error)?.message}</span> : null}</div></div> : null}
              {tab === "markdown" ? <pre className="markdown-preview">{markdown.isPending ? text("正在加载…", "Loading…") : markdown.data}</pre> : null}
              {tab === "pdf" ? <iframe className="pdf-frame" title={`${selectedPaper.paper_id} PDF`} src={`/api/v1/library/papers/${encodeURIComponent(selectedPaper.paper_id)}/pdf`} /> : null}
            </>
          ) : <div className="empty-state">{text("选择一篇文献查看详细内容。", "Select a paper to view details.")}</div>}
        </section>
      </div>
    </main>
  );
}
