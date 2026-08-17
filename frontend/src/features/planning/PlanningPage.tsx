import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";

import { apiRequest, jsonBody } from "../../api/client";
import { queryKeys } from "../../api/queries";
import { ErrorState } from "../../components/ErrorState";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { useUiText } from "../../i18n/useUiText";
import { buildPaperDisplayLabels, OutlineBuilder, parseOutlineMarkdown, validateVisualOutline } from "./OutlineBuilder";

type MatrixPaper = Record<string, unknown> & {
  paper_id: string;
  title?: string | Record<string, unknown>;
  authors?: string[];
  year?: string | number;
  journal?: string;
  doi?: string;
  keywords?: string[];
  abstract?: string;
  main_content?: string;
  full_reading_complete?: boolean;
  reading_complete?: boolean;
  most_relevant_figure?: Record<string, unknown>;
};

type BlueprintSection = Record<string, unknown> & {
  section_id?: string;
  title?: string;
  section_thesis?: string;
  section_goal?: string;
  assigned_papers?: unknown[];
  major_papers?: unknown[];
  paragraph_plan?: unknown[];
  review_claims?: unknown[];
  required_figures?: unknown[];
  figure_or_table_needs?: unknown[];
};

type PlanningPayload = {
  topic?: string;
  matrix_revision: number;
  blueprint_revision: number;
  matrix_artifact_id?: string;
  literature_matrix?: { rows?: MatrixPaper[] };
  selected_outline_md?: string;
  outline_options_md?: string;
  outline_selection?: Record<string, unknown>;
  reference_outline_candidates?: Array<Record<string, unknown> & { candidate_id?: string; source_name?: string }>;
  legacy_reference_outline_count?: number;
  section_blueprint?: { sections?: BlueprintSection[] };
  section_writing_plan_md?: string;
  matrix_sync?: Record<string, unknown>;
};

const outlineStyles = [
  { id: "substrate", icon: "S", titleZh: "底物结构", titleEn: "Substrate structure", descriptionZh: "按底物类别和结构差异组织论文。", descriptionEn: "Organize papers by substrate class and structural differences." },
  { id: "catalyst", icon: "C", titleZh: "催化剂与方法", titleEn: "Catalysts and methods", descriptionZh: "比较催化体系、配体与方法学家族。", descriptionEn: "Compare catalytic systems, ligands, and method families." },
  { id: "reaction", icon: "R", titleZh: "反应类型", titleEn: "Reaction type", descriptionZh: "按照转化逻辑与机理策略组织内容。", descriptionEn: "Organize content by transformation logic and mechanistic strategy." },
  { id: "custom", icon: "E", titleZh: "自定义大纲", titleEn: "Custom outline", descriptionZh: "使用新手表单逐节填写，也可切换高级Markdown。", descriptionEn: "Fill sections with a beginner-friendly form, with optional advanced Markdown." },
];

function displayText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(displayText).filter(Boolean).join(", ");
  if (typeof value === "object" && "value" in value) return displayText((value as { value: unknown }).value);
  return JSON.stringify(value);
}

function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error || new Error("Unable to read the reference file."));
    reader.readAsDataURL(file);
  });
}

function MatrixWorkspace({ payload, projectId, refresh }: { payload: PlanningPayload; projectId: string; refresh: () => Promise<unknown> }) {
  const { text } = useUiText();
  const [mode, setMode] = useState<"reading" | "outline">("reading");
  const [filter, setFilter] = useState("");
  const papers = payload.literature_matrix?.rows || [];
  const [selectedId, setSelectedId] = useState(papers[0]?.paper_id || "");
  const selected = papers.find((paper) => paper.paper_id === selectedId) || papers[0];
  const [note, setNote] = useState(selected?.main_content || "");
  const [complete, setComplete] = useState(Boolean(selected?.full_reading_complete || selected?.reading_complete));
  const [outlineDraft, setOutlineDraft] = useState(payload.selected_outline_md || "");
  const paperLabels = useMemo(() => buildPaperDisplayLabels(papers), [papers]);
  useEffect(() => {
    setNote(selected?.main_content || "");
    setComplete(Boolean(selected?.full_reading_complete || selected?.reading_complete));
  }, [selected]);
  useEffect(() => setOutlineDraft(payload.selected_outline_md || ""), [payload.selected_outline_md]);

  const saveReading = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/matrix/${encodeURIComponent(selected!.paper_id)}`, {
      method: "PUT",
      ...jsonBody({ revision: payload.matrix_revision, main_content: note, most_relevant_figure: selected?.most_relevant_figure || null, mark_complete: complete }),
    }),
    onSuccess: refresh,
  });
  const chooseOutline = useMutation({
    mutationFn: (outlineStyle: string) => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/outline`, {
      method: "PUT",
      ...jsonBody({ revision: payload.matrix_revision, outline_style: outlineStyle }),
    }),
    onSuccess: refresh,
  });
  const saveOutline = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/outline`, {
      method: "PUT",
      ...jsonBody({ revision: payload.matrix_revision, outline_style: String(payload.outline_selection?.outline_style || "custom"), outline_md: outlineDraft }),
    }),
    onSuccess: refresh,
  });
  const uploadReference = useMutation({
    mutationFn: async (file: File) => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/reference-outlines`, {
      method: "POST",
      ...jsonBody({ revision: payload.matrix_revision, filename: file.name, content_base64: await fileBase64(file) }),
    }),
    onSuccess: refresh,
  });
  const visiblePapers = papers.filter((paper) => [paper.paper_id, paperLabels.get(paper.paper_id), displayText(paper.title), paper.keywords?.join(" "), paper.abstract].join(" ").toLowerCase().includes(filter.toLowerCase()));
  const selectedStyle = String(payload.outline_selection?.outline_style || "");
  const outlineReady = validateVisualOutline(parseOutlineMarkdown(outlineDraft)).ready;

  return (
    <>
      <nav className="workspace-mode-tabs"><button type="button" className={mode === "reading" ? "active" : ""} onClick={() => setMode("reading")}>{text("文献Matrix", "Literature matrix")}</button><button type="button" className={mode === "outline" ? "active" : ""} onClick={() => setMode("outline")}>{text("大纲选择与上传", "Choose or upload outline")}</button></nav>
      {mode === "reading" ? (
        <div className="planning-grid">
          <section className="pane planning-list-pane">
            <div className="pane-head"><div><span className="step-label">{text("文献Matrix", "Literature matrix")}</span><h2>{papers.length} {text("篇论文", "papers")}</h2></div></div>
            <input className="pane-search" type="search" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={text("检索Matrix", "Search matrix")} />
            <div className="paper-list">{visiblePapers.map((paper) => <button type="button" key={paper.paper_id} title={text(`内部论文 ID：${paper.paper_id}`, `Internal paper ID: ${paper.paper_id}`)} className={paper.paper_id === selected?.paper_id ? "paper-row active" : "paper-row"} onClick={() => setSelectedId(paper.paper_id)}><span className="paper-row-main"><strong>{paperLabels.get(paper.paper_id) || paper.paper_id} · {displayText(paper.title)}</strong><small>{paper.authors?.join(", ")}</small></span><span className={(paper.full_reading_complete || paper.reading_complete) ? "status-dot ok" : "status-dot warning"} /></button>)}</div>
          </section>
          <section className="pane planning-detail-pane">
            {selected ? <><div className="pane-head paper-title"><div><span className="step-label" title={text(`内部论文 ID：${selected.paper_id}`, `Internal paper ID: ${selected.paper_id}`)}>{paperLabels.get(selected.paper_id) || selected.paper_id}</span><h2>{displayText(selected.title)}</h2><p>{[selected.authors?.join(", "), selected.year, selected.journal, selected.doi].filter(Boolean).join(" · ")}</p></div></div><div className="planning-detail-content"><section className="reading-field"><h3>{text("摘要", "Abstract")}</h3><p>{displayText(selected.abstract) || text("没有摘要。", "No abstract available.")}</p></section><section className="reading-field"><h3>{text("全文阅读笔记", "Full-text reading notes")}</h3><textarea rows={14} value={note} onChange={(event) => setNote(event.target.value)} placeholder={text("转化、条件、证据、范围、限制与综述相关性", "Transformation, conditions, evidence, scope, limitations, and relevance")} /></section><label className="check-label"><input type="checkbox" checked={complete} onChange={(event) => setComplete(event.target.checked)} />{text("已完成该论文全文阅读", "Full-text reading completed")}</label><button className="button button-primary" type="button" disabled={saveReading.isPending} onClick={() => saveReading.mutate()}>{saveReading.isPending ? text("保存中…", "Saving…") : text("保存阅读笔记", "Save reading notes")}</button>{saveReading.error ? <p className="message message-error">{saveReading.error.message}</p> : null}<details className="figure-data"><summary>{text("最相关图像信息", "Most relevant figure")}</summary><pre>{JSON.stringify(selected.most_relevant_figure || {}, null, 2)}</pre></details></div></> : <div className="empty-state">{text("Discovery确认后会在这里显示Matrix。", "The matrix appears here after Discovery is confirmed.")}</div>}
          </section>
        </div>
      ) : (
        <section className="outline-workspace-react">
          <div className="outline-hero"><div><span className="step-label">{text("步骤 1 · 综述结构", "Step 1 · Review structure")}</span><h2>{text("选择综述组织逻辑", "Choose the review structure")}</h2><p>{text("只借鉴参考综述的组织方式，不复制其主题标题和具体内容。", "Reuse only the organizational style of a reference review, not its topic headings or content.")}</p></div><span className={selectedStyle ? "badge" : "badge pending"}>{selectedStyle ? text(`当前：${selectedStyle}`, `Current: ${selectedStyle}`) : text("尚未选择", "Not selected")}</span></div>
          <div className="outline-card-grid">{outlineStyles.map((style) => <article key={style.id} className={selectedStyle === style.id ? "outline-card current" : "outline-card"}><span>{style.icon}</span><h3>{text(style.titleZh, style.titleEn)}</h3><p>{text(style.descriptionZh, style.descriptionEn)}</p><button className="button button-secondary" type="button" disabled={chooseOutline.isPending || selectedStyle === style.id} onClick={() => chooseOutline.mutate(style.id)}>{selectedStyle === style.id ? text("当前选择", "Current selection") : text("使用此结构", "Use this structure")}</button></article>)}</div>
          <section className="surface reference-upload"><div><h3>{text("上传综述，仅学习格式与写法", "Upload a review to learn format only")}</h3><p>{text("支持PDF、DOCX、Markdown或TXT。系统分两步处理：先提取层级、节奏和写作方式，再只根据当前主题与Matrix生成全新标题；不会复制、翻译或改写上传综述的标题和内容。", "Supports PDF, DOCX, Markdown, or TXT. The system first extracts hierarchy, pacing, and writing conventions, then generates new headings only from the current topic and Matrix. Uploaded headings and content are never copied, translated, or paraphrased.")}</p></div><label className="button button-secondary file-button">{uploadReference.isPending ? text("正在分析格式…", "Analyzing format…") : text("选择参考综述", "Choose reference review")}<input type="file" accept=".pdf,.docx,.md,.txt" disabled={uploadReference.isPending} onChange={(event) => { const file = event.target.files?.[0]; event.currentTarget.value = ""; if (file) uploadReference.mutate(file); }} /></label></section>
          {uploadReference.error ? <p className="message message-error">{uploadReference.error.message}</p> : null}
          {payload.legacy_reference_outline_count ? <p className="message message-warning">{text(`已隐藏 ${payload.legacy_reference_outline_count} 个旧版参考大纲，因为它们没有通过“只学格式”的内容隔离校验；如需使用，请重新上传原参考综述。`, `${payload.legacy_reference_outline_count} legacy reference outlines were hidden because they did not pass format-only content isolation. Upload the source review again to use it safely.`)}</p> : null}
          {payload.reference_outline_candidates?.length ? <div className="reference-candidates">{payload.reference_outline_candidates.map((candidate) => { const style = `reference:${candidate.candidate_id}`; return <button key={String(candidate.candidate_id)} className={selectedStyle === style ? "active" : ""} type="button" disabled={chooseOutline.isPending || selectedStyle === style} onClick={() => chooseOutline.mutate(style)}><strong>{String(candidate.source_name || candidate.candidate_id)}</strong><small>{text("仅学习格式 · 内容来自当前Matrix", "Format only · content from current Matrix")}</small></button>; })}</div> : null}
          <section className="outline-editor-card"><div className="section-heading"><div><h2>{text("新手大纲编辑器", "Beginner outline editor")}</h2><p>{text("大标题和小标题必须对应当前检索主题；新手模式会自动生成系统需要的格式。", "Every heading and subheading must match the current discovery topic; beginner mode generates the required format automatically.")}</p></div><button className="button button-primary" type="button" disabled={!outlineReady || saveOutline.isPending} onClick={() => saveOutline.mutate()}>{saveOutline.isPending ? text("保存中…", "Saving…") : text("保存大纲", "Save outline")}</button></div><OutlineBuilder value={outlineDraft} papers={papers} onChange={setOutlineDraft} />{!outlineReady && outlineDraft.trim() ? <p className="message message-warning">{text("请补全每个章节的标题，并至少选择一篇Matrix论文。", "Complete every section title and select at least one Matrix paper per section.")}</p> : null}{saveOutline.error ? <p className="message message-error">{saveOutline.error.message}</p> : null}</section>
          <details className="outline-options"><summary>{text("查看系统生成的候选大纲", "View system-generated outline candidates")}</summary><pre>{payload.outline_options_md || text("暂无候选大纲。", "No candidate outlines yet.")}</pre></details>
        </section>
      )}
    </>
  );
}

function BlueprintWorkspace({ payload }: { payload: PlanningPayload }) {
  const { text } = useUiText();
  const sections = payload.section_blueprint?.sections || [];
  const [selectedId, setSelectedId] = useState(String(sections[0]?.section_id || ""));
  const [detailTab, setDetailTab] = useState<"section" | "plan" | "outline">("section");
  const section = sections.find((item) => String(item.section_id) === selectedId) || sections[0];
  return (
    <div className="blueprint-grid-react">
      <section className="pane blueprint-section-list"><div className="pane-head"><div><span className="step-label">{text("Blueprint章节", "Blueprint sections")}</span><h2>{sections.length} {text("个章节", "sections")}</h2></div></div><div className="keyword-list">{sections.map((item) => { const papers = item.major_papers || item.assigned_papers || []; const claims = item.review_claims || item.paragraph_plan || []; const missing = !papers.length || !claims.length; return <button key={String(item.section_id)} type="button" className={item === section ? "active" : ""} onClick={() => { setSelectedId(String(item.section_id)); setDetailTab("section"); }}><strong>{String(item.section_id || "")} · {String(item.title || text("无标题", "Untitled"))}</strong><small>{papers.length} {text("篇论文", "papers")} · {claims.length} {text("个段落计划", "paragraph plans")} · {missing ? text("需要检查", "Needs review") : text("就绪", "Ready")}</small></button>; })}</div></section>
      <section className="pane blueprint-detail-react"><div className="pane-head"><div><span className="step-label">{text("Blueprint详情", "Blueprint detail")}</span><h2>{section?.title || "Blueprint"}</h2><p>{String(section?.section_thesis || section?.section_goal || "")}</p></div></div><nav className="detail-tabs"><button type="button" className={detailTab === "section" ? "active" : ""} onClick={() => setDetailTab("section")}>{text("章节", "Section")}</button><button type="button" className={detailTab === "plan" ? "active" : ""} onClick={() => setDetailTab("plan")}>{text("写作计划", "Writing plan")}</button><button type="button" className={detailTab === "outline" ? "active" : ""} onClick={() => setDetailTab("outline")}>{text("选定大纲", "Selected outline")}</button></nav>{detailTab === "section" ? <div className="blueprint-json-fields">{section ? Object.entries(section).filter(([, value]) => value !== null && value !== "" && (!Array.isArray(value) || value.length)).map(([key, value]) => <section key={key}><h3>{key.replaceAll("_", " ")}</h3><pre>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre></section>) : <div className="empty-state">{text("请先生成Blueprint。", "Generate a blueprint first.")}</div>}</div> : <pre className="markdown-preview">{detailTab === "plan" ? payload.section_writing_plan_md : payload.selected_outline_md}</pre>}</section>
    </div>
  );
}

export function PlanningPage() {
  const { text } = useUiText();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get("tab") === "blueprint" ? "blueprint" : "matrix";
  const { selected: project } = useSelectedProject();
  const planning = useQuery({
    queryKey: ["planning", project?.project_id || ""],
    queryFn: () => apiRequest<PlanningPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning`),
    enabled: Boolean(project),
  });
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["planning", project?.project_id || ""] });
    return planning.refetch();
  };
  const generateBlueprint = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning/blueprint`, { method: "POST", ...jsonBody({ revision: planning.data!.blueprint_revision }) }),
    onSuccess: async () => {
      await refresh();
      const next = new URLSearchParams(searchParams); next.set("tab", "blueprint"); setSearchParams(next);
    },
  });
  const confirmBlueprint = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning/blueprint/confirm`, { method: "POST", ...jsonBody({ revision: planning.data!.blueprint_revision }) }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      navigate(`/sections?project=${encodeURIComponent(project!.project_id)}`);
    },
  });

  return (
    <main className="workspace page-container workspace-page">
      <div className="workspace-heading"><div><p className="eyebrow">{text("阶段 3 · 分析与规划", "Stage 3 · Analysis and planning")}</p><h1>{text("Matrix与综述大纲", "Matrix and review outline")}</h1><p className="muted">{text("从确认文献形成Matrix，选择大纲逻辑，再生成可审核的章节Blueprint。", "Build the matrix from confirmed papers, choose an outline logic, then generate a reviewable section blueprint.")}</p></div><ProjectSelector /></div>
      <nav className="workspace-step-tabs"><button type="button" className={tab === "matrix" ? "active" : ""} onClick={() => { const next = new URLSearchParams(searchParams); next.set("tab", "matrix"); setSearchParams(next); }}>1 {text("文献Matrix与大纲", "Literature matrix and outline")}</button><button type="button" className={tab === "blueprint" ? "active" : ""} onClick={() => { const next = new URLSearchParams(searchParams); next.set("tab", "blueprint"); setSearchParams(next); }}>2 {text("章节Blueprint", "Section blueprint")}</button></nav>
      {planning.isPending ? <div className="empty-state">{text("正在加载Planning产物…", "Loading planning artifacts…")}</div> : null}
      {planning.error ? <ErrorState error={planning.error} onRetry={() => planning.refetch()} /> : null}
      {planning.data && project ? <>{tab === "matrix" ? <MatrixWorkspace payload={planning.data} projectId={project.project_id} refresh={refresh} /> : <BlueprintWorkspace payload={planning.data} />}<div className="stage-action-bar"><div><strong>{tab === "matrix" ? text("生成Blueprint", "Generate blueprint") : text("确认Blueprint", "Confirm blueprint")}</strong><p>{tab === "matrix" ? text("使用当前Matrix与已保存大纲生成章节任务。", "Generate section tasks from the current matrix and saved outline.") : text("确认章节论点、论文分配、段落计划和图表需求后进入章节写作。", "Review section claims, paper assignments, paragraph plans, and figure needs before section writing.")}</p></div>{tab === "matrix" ? <button className="button button-primary" type="button" disabled={generateBlueprint.isPending || !planning.data.selected_outline_md?.trim()} onClick={() => generateBlueprint.mutate()}>{generateBlueprint.isPending ? text("生成中…", "Generating…") : text("生成Blueprint", "Generate blueprint")}</button> : <button className="button button-primary" type="button" disabled={confirmBlueprint.isPending || !planning.data.section_blueprint?.sections?.length} onClick={() => confirmBlueprint.mutate()}>{confirmBlueprint.isPending ? text("确认中…", "Confirming…") : text("确认并进入章节", "Confirm and enter sections")}</button>}{(generateBlueprint.error || confirmBlueprint.error) ? <span className="message message-error">{(generateBlueprint.error || confirmBlueprint.error)?.message}</span> : null}</div></> : null}
    </main>
  );
}
