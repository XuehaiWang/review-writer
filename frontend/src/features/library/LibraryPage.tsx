import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { libraryQuery, queryKeys } from "../../api/queries";
import type { Job, LibraryPaper } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { useUiText } from "../../i18n/useUiText";
import { buildPaperDisplayLabels } from "../../utils/paperLabels";

type DetailTab = "metadata" | "markdown" | "pdf";
type UploadStatus = { name: string; status: "queued" | "uploading" | "done" | "failed"; message: string; messageEn?: string };
type Candidate = Record<string, unknown> & { candidate_id?: string; title?: string; year?: number; journal?: string; doi?: string; score?: number; landing_url?: string };
type DownloadResult = {
  added_count?: number;
  already_present_count?: number;
  failed_count?: number;
  results?: Array<{ status?: string; error?: string }>;
};

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
        <small>{[paper.year, paper.journal, paper.doi].filter(Boolean).join(" · ") || paper.original_filename}</small>
      </span>
      <span className={status.className} title={status.label} />
    </button>
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
  const [uploads, setUploads] = useState<UploadStatus[]>([]);
  const library = useQuery(libraryQuery(query));
  const libraryIndex = useQuery(libraryQuery(""));
  const paperLabels = useMemo(
    () => buildPaperDisplayLabels(libraryIndex.data?.items || library.data?.items || []),
    [library.data?.items, libraryIndex.data?.items],
  );
  const refreshLibrary = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ["library"] }),
    [queryClient],
  );
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
      setUploads([{ name: text("本批文件", "This batch"), status: "failed", message: "每批最多上传30个PDF文件。", messageEn: "A batch can contain at most 30 PDF files." }]);
      return;
    }
    const invalid = queue.find((file) => !file.name.toLocaleLowerCase().endsWith(".pdf"));
    if (invalid) {
      setUploads([{ name: invalid.name, status: "failed", message: "该文件不是PDF，未开始上传。", messageEn: "This file is not a PDF. Upload was not started." }]);
      return;
    }
    setUploads(queue.map((file) => ({ name: file.name, status: "queued", message: "等待上传", messageEn: "Waiting to upload" })));
    for (const file of queue) {
      setUploads((rows) => rows.map((row) => row.name === file.name ? { ...row, status: "uploading", message: "正在上传并执行MinerU解析", messageEn: "Uploading and running MinerU parsing" } : row));
      try {
        await apiRequest(`/api/v1/library/papers?filename=${encodeURIComponent(file.name)}`, {
          method: "POST",
          headers: { "Content-Type": file.type || "application/pdf" },
          body: file,
        });
        setUploads((rows) => rows.map((row) => row.name === file.name ? { ...row, status: "done", message: "上传与解析完成", messageEn: "Upload and parsing completed" } : row));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setUploads((rows) => rows.map((row) => row.name === file.name ? { ...row, status: "failed", message } : row));
      }
    }
    await queryClient.invalidateQueries({ queryKey: ["library"] });
  }

  return (
    <main className="workspace page-container workspace-page">
      <div className="workspace-heading">
        <div><p className="eyebrow">{text("阶段 1 · 共享文献集合", "Stage 1 · Shared source collection")}</p><h1>{text("文献库", "Literature library")}</h1><p className="muted">{text("上传PDF后必须完成MinerU解析，Metadata和正文才会供后续检索与写作使用。", "Uploaded PDFs must complete MinerU parsing before metadata and full text are available to later discovery and writing stages.")}</p></div>
        <ProjectSelector />
        <label className="button button-primary file-button">{text("批量上传PDF", "Upload PDFs in batch")}<input type="file" accept="application/pdf,.pdf" multiple onChange={(event) => void uploadFiles(event.target.files)} /></label>
      </div>
      <UploadBatchProgress uploads={uploads} />
      <AcquisitionPanel projectId={project?.project_id || ""} onLibraryChanged={refreshLibrary} />
      <div className="three-pane library-workspace">
        <section className="pane list-pane">
          <div className="pane-head"><div><span className="step-label">{text("论文", "Papers")}</span><h2>{text("文献", "Papers")} {library.data?.count ?? 0}</h2></div></div>
          <input className="pane-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={text("检索标题、作者或关键词", "Search title, author, or keyword")} />
          {library.error ? <ErrorState error={library.error} onRetry={() => library.refetch()} /> : null}
          <div className="paper-list">{library.data?.items.map((paper) => <PaperListItem key={paper.paper_id} paper={paper} displayLabel={paperLabels.get(paper.paper_id) || paper.paper_id} selected={paper.paper_id === selectedPaper?.paper_id} onSelect={() => setSelectedId(paper.paper_id)} />)}</div>
        </section>
        <section className="pane detail-pane">
          {selectedPaper ? (
            <>
              <div className="pane-head paper-title"><div><span className="step-label" title={selectedPaper.paper_id}>{paperLabels.get(selectedPaper.paper_id) || selectedPaper.paper_id}</span><h2>{selectedPaper.title}</h2><p>{selectedPaper.authors?.join(", ")}</p></div><button className="button button-quiet danger" type="button" disabled={deletePaper.isPending} onClick={() => { if (window.confirm(text(`确认删除 ${paperLabels.get(selectedPaper.paper_id) || selectedPaper.paper_id}？`, `Delete ${paperLabels.get(selectedPaper.paper_id) || selectedPaper.paper_id}?`))) deletePaper.mutate(); }}>{text("删除", "Delete")}</button></div>
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
